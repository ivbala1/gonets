"""Прогрев сессий (перенесено из bot.py, шаг 7 разбиения 2)."""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime

from telegram.ext import Application, ContextTypes

from . import limits, warm
from .claude_runner import ClaudeResult, SessionStore
from .config import Config, local_zone
from .i18n import _

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


# Прогрев кэша разговора (warm.py). Потолок пинга: обычно он идёт 5-15 с.
WARM_RUNTIME_SEC = 180
# Насколько раньше срока задание job_queue может сработать без отказа.
WARM_EARLY_SEC = 30


def _warm_prompt() -> str:
    return _("Служебный ход бота для прогрева кэша, не сообщение человека. "
             "Ответь одним словом «ок», не вызывая инструментов.")


def _warm_signature(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, chat_key: str) -> tuple:
    """Всё, что входит в начало запроса рана темы: модель, effort, окно
    сжатия, персона и подсказка памяти. Другое значение у следующего рана
    значит другой кэш, и пинг старого был бы впустую (ревью Codex 19.10:
    /model default и /model pin меняют модель без ожидающего выбора)."""
    persona = _b._persona_for(ctx, cfg, chat_key)
    return (_b._model_for(ctx, cfg, chat_key), _b._effort_for(ctx, cfg, chat_key),
            _b._autocompact_for(cfg, chat_key), hashlib.sha256(persona.encode()).hexdigest(),
            _b._memory_scope_hint(ctx, cfg, chat_key))


def _warm_after_run(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, chat_key: str,
                    result: ClaudeResult, sig: tuple) -> None:
    """Отметка прогрева после рана темы: сессия, начало последнего запроса
    к модели, размер разговора и параметры рана; пинг через IDLE минут.
    Ран без обращения к Claude (отказ до API, не та сессия) отметку не
    двигает: кэш он не обновил (ревью Codex 19.10)."""
    if chat_key not in getattr(cfg, "warm_chats", frozenset()):
        return
    store: SessionStore = ctx.application.bot_data["sessions"]
    sid = store.get(chat_key)
    if not result.last_request_at or not sid or result.session_id != sid:
        return
    marks: dict = ctx.application.bot_data.setdefault("warm", {})
    if not result.context_tokens:
        # Разговор сжат в последнем запросе, ожидаемый размер неизвестен.
        marks.pop(chat_key, None)
        return
    marks[chat_key] = {"sid": sid, "at": result.last_request_at,
                       "expected": result.context_tokens, "sig": sig}
    _b._warm_schedule(ctx.application, chat_key)


def _warm_forget(app: Application, chat_key: str) -> None:
    if app.bot_data.get("warm", {}).pop(chat_key, None) is not None:
        _warm_cancel(app, chat_key)


def _warm_cancel(app: Application, chat_key: str) -> None:
    jq = app.job_queue
    if jq is not None:
        for job in jq.get_jobs_by_name(f"warm:{chat_key}"):
            job.schedule_removal()


def _warm_schedule(app: Application, chat_key: str) -> None:
    """Пинг через IDLE минут от начала последнего запроса. Поколение отметки
    едет в задании: уже запущенное задание schedule_removal не снимает, и без
    сверки оно пинговало бы по отметке нового рана сразу (ревью Codex 19.10)."""
    jq = app.job_queue
    if jq is None:
        return
    _warm_cancel(app, chat_key)
    bd = app.bot_data
    mark = bd["warm"][chat_key]
    bd["warm_gen"] = gen = bd.get("warm_gen", 0) + 1
    mark["gen"] = gen
    cfg: Config = bd["cfg"]
    delay = max(1.0, mark["at"] + cfg.warm_idle_min * 60 - time.time())
    jq.run_once(_warm_tick, when=delay, name=f"warm:{chat_key}", data=(chat_key, gen))


def _warm_skip_reason(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, chat_key: str,
                      mark: dict | None, gen: int) -> str:
    """Почему пинга сейчас не будет; пусто: можно. Цепочка на этом кончается,
    следующую назначит настоящий ран."""
    bd = ctx.application.bot_data
    if mark is None:
        return "no mark"
    if mark.get("gen") != gen:
        return "mark replaced"
    if time.time() - mark["at"] < cfg.warm_idle_min * 60 - WARM_EARLY_SEC:
        return "not idle yet"
    if bd.get("stopping") or bd.get("draining"):
        return "bot stopping or restart pending"
    if limits.reserve() is not None:
        # Остаток лимита Claude оставлен разговору с Домовым (limits.py); прогрев
        # это фон и уступает даже в его теме (ревью Codex 20.10.2026).
        return "subscription reserve"
    if not warm.in_window(datetime.now(local_zone()), cfg.warm_hours):
        return "outside window"
    if time.time() - mark["at"] > warm.LATE_SEC:
        # Таймер опоздал (сон машины): кэш, скорее всего, уже истёк, и пинг
        # переписал бы весь разговор.
        return "late"
    store: SessionStore = bd["sessions"]
    if store.get(chat_key) != mark["sid"]:
        return "session changed"
    if _b._warm_signature(ctx, cfg, chat_key) != mark["sig"]:
        return "run settings changed"
    if warm.near_compaction(mark["expected"], _b._autocompact_for(cfg, chat_key)):
        return "near autocompact window"
    return ""


async def _warm_tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Пинг прогрева: ход в ответвление сессии темы (run_claude,
    warm_fork_sid) под замком темы, с замками рана по чужому тексту (у темы
    может быть автоодобрение, а человека рядом нет). В чат ничего не идёт,
    итог в журнале бота. Прочитал разговор: следующий пинг через IDLE минут."""
    app = ctx.application
    bd = app.bot_data
    chat_key, gen = ctx.job.data
    cfg: Config = bd["cfg"]
    reason = _warm_skip_reason(ctx, cfg, chat_key, bd.get("warm", {}).get(chat_key), gen)
    if reason:
        log.info("warm skip chat=%s: %s", chat_key, reason)
        return
    lock = _b._chat_lock(ctx, chat_key)
    if lock.locked():
        # Идёт ран: он сам назначит пинг после себя.
        log.info("warm skip chat=%s: run in progress", chat_key)
        return
    chat_id_s, _sep, thread_s = chat_key.partition(":")
    # Учёт работы: перезапуск по заявке ждёт и пинг (ревью Codex 19.10).
    bd["active_work"] = bd.get("active_work", 0) + 1
    try:
        async with lock:
            mark = bd.get("warm", {}).get(chat_key)
            reason = _warm_skip_reason(ctx, cfg, chat_key, mark, gen)
            if reason:
                log.info("warm skip chat=%s: %s", chat_key, reason)
                return
            result = await _b.run_claude(
                cfg.claude_bin, chat_key, int(chat_id_s), int(thread_s) if thread_s else None,
                _warm_prompt(), bd["sessions"],
                cwd=cfg.claude_cwd,
                hook_settings=cfg.hook_settings_path,
                persona=_b._persona_for(ctx, cfg, chat_key),
                memory_scope_hint=_b._memory_scope_hint(ctx, cfg, chat_key),
                effort=_b._effort_for(ctx, cfg, chat_key),
                model=_b._model_for(ctx, cfg, chat_key),
                autocompact=_b._autocompact_for(cfg, chat_key),
                max_runtime_sec=WARM_RUNTIME_SEC,
                # Реестр процессов: остановка бота и поправка «!» гасят пинг.
                # Реестр сессий нет: поправка восстановила бы в тему id
                # ответвления, которого нет на диске.
                process_registry=bd["procs"],
                session_targets=bd["session_targets"],
                cost_totals=bd.get("cost_totals"),
                unattended_sids=bd.setdefault("unattended_sids", set()),
                untrusted_sids=bd.setdefault("untrusted_sids", set()),
                should_stop=lambda: bool(bd.get("stopping") or bd.get("draining")),
                warm_fork_sid=warm.fork_sid(chat_key),
            )
            ok = (not result.is_error and result.requests == 1 and not result.compacted
                  and warm.healthy(result.first_cache_read, result.first_cache_write, mark["expected"]))
            if not ok:
                log.warning("warm chain stopped chat=%s: error=%s requests=%d compacted=%d "
                            "read=%d write=%d expected=%d text=%r",
                            chat_key, result.is_error, result.requests, result.compacted,
                            result.first_cache_read, result.first_cache_write, mark["expected"],
                            result.text[:200])
                return
            mark["at"] = result.last_request_at
            _b._warm_schedule(app, chat_key)
    finally:
        bd["active_work"] -= 1


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

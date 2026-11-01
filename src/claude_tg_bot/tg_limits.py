"""Лимиты Claude: отложенные запросы и ожидание сброса (перенесено из bot.py, шаг 10 разбиения 2)."""
from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import time
import uuid
from typing import Any, Callable

from telegram.ext import ContextTypes

from . import limits
from .claude_runner import ClaudeResult, SessionStore
from .config import Config
from .i18n import _

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


def _limit_movable(task: dict | None, exec_task: dict | None, from_callback: bool,
                   permission_mode: str) -> bool:
    """Можно ли перенести запрос отложенным промптом (/at) на после сброса.
    Нельзя: выполнение задачи и тема задачи (свой порядок одобрения), /plan,
    ответ кнопкой. Там Иван повторяет сам."""
    return task is None and exec_task is None and not from_callback and permission_mode != "plan"


def _limit_retry_pending(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> bool:
    """Ждёт ли тема отложенного после лимита повтора (задание /at с limit_retry)."""
    store: SessionStore | None = ctx.application.bot_data.get("at_jobs")
    for entry in list((store.data if store is not None else {}).values()):
        with contextlib.suppress(Exception):
            job = json.loads(entry["v"])
            if job.get("chat_key") == chat_key and int(job.get("limit_retry") or 0) > 0:
                return True
    return False


def _limit_defer(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, chat_id: int, thread_id: int | None,
                 prompt: str, fire_at: float, retry: int, event_ref: dict | None = None) -> bool:
    """Отложенный промпт, как /at: переживает рестарт, уходит фоновым раном."""
    store: SessionStore | None = ctx.application.bot_data.get("at_jobs")
    if store is None or getattr(ctx.application, "job_queue", None) is None:
        return False
    expires_at = (event_ref or {}).get("expires_at")
    if expires_at is not None and float(expires_at) <= fire_at:
        # Событие со сроком (поиск в своём окне) после сброса уже не нужно.
        return False
    jid = uuid.uuid4().hex[:6]
    payload = {"chat_key": chat_key, "chat_id": chat_id, "thread_id": thread_id,
               "fire_at": fire_at, "prompt": prompt, "limit_retry": retry}
    # Событие с диска передаёт заданию id, срок и тишину: после рестарта оно не
    # будится второй раз, а задание само проверит срок (ревью Codex 22.10.2026).
    payload.update({k: v for k, v in (event_ref or {}).items() if v is not None})
    try:
        store.set(jid, json.dumps(payload, ensure_ascii=False))
        _b._schedule_at_job(ctx.application, jid, payload)
    except Exception:
        # Готовый ответ важнее отложенного задания: он уходит с пометкой.
        log.exception("limit: deferral failed chat=%s", chat_key)
        with contextlib.suppress(Exception):
            store.remove(jid)
        return False
    log.info("limit: deferred chat=%s jid=%s until=%s retry=%d", chat_key, jid, limits.clock(fire_at), retry)
    return True


def _limit_note_once(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, until: float) -> bool:
    """Уведомление о пропуске фонового рана: одно на тему за время резерва."""
    seen: set = ctx.application.bot_data.setdefault("limit_noticed", set())
    key = (chat_key, int(until))
    if key in seen:
        return False
    seen.add(key)
    return True


async def _limit_hold(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, msg: Any, chat_key: str,
                      chat_id: int, thread_id: int | None, prompt: str, *,
                      unattended: bool, untrusted: bool, movable: bool, limit_retry: int,
                      event_ref: dict | None = None,
                      on_moved: Callable[[], None] | None = None) -> bool:
    """Резерв лимита Claude (limits.py): с 97% пятичасового или 99% недельного
    окна идёт только разговор Ивана в чате Домового. Остальное откладывается до
    сброса, а что ждать не может, пропускается с уведомлением. True: ран не
    начинать."""
    until = limits.reserve()
    if until is None:
        return False
    if chat_key in getattr(cfg, "limit_reserve_chats", frozenset()) and not unattended:
        return False
    fire_at = limits.fire_at(until)
    head = _("⏸ лимит Claude на исходе ({where}), остаток оставлен разговору с Домовым").format(
        where=limits.summary() or "?")
    if untrusted or (unattended and fire_at - time.time() > limits.BACKGROUND_MAX_WAIT_SEC):
        # Ран по чужому тексту отложенным промптом не повторить: /at идёт с
        # полными правами. Фоновое дело после долгого ожидания несвоевременно.
        log.info("limit: skipped chat=%s untrusted=%s until=%s", chat_key, untrusted, limits.clock(until))
        text = head + _(": фоновый запуск пропущен, сброс в {when}.").format(when=limits.clock(until)) \
            if _limit_note_once(ctx, chat_key, until) else ""
    elif movable and limit_retry < limits.MAX_RETRIES \
            and _limit_defer(ctx, chat_key, chat_id, thread_id, prompt, fire_at, limit_retry + 1,
                             event_ref):
        if on_moved is not None:
            on_moved()
        text = head + _(": запрос отложен до {when} и уйдёт сам фоновым раном.").format(
            when=limits.clock(fire_at))
    else:
        text = head + _(": запрос не ушёл в Claude, повтори после {when}.").format(when=limits.clock(fire_at))
    if text:
        with contextlib.suppress(Exception):
            await msg.reply_text(text)
    return True


def _limit_after_error(ctx: ContextTypes.DEFAULT_TYPE, result: ClaudeResult, chat_key: str,
                       chat_id: int, thread_id: int | None, prompt: str, *,
                       unattended: bool, untrusted: bool, movable: bool,
                       limit_retry: int, event_ref: dict | None = None) -> ClaudeResult:
    """Ран упал на исчерпании подписки: повторить после сброса, если можно.
    Повторяется оставшаяся работа: агент сначала смотрит, что уже сделано
    (ревью Codex 20.10.2026)."""
    reset = result.limit_reset
    note = ""
    when = limits.fire_at(reset) if reset is not None else None
    if when is None:
        note = _("время сброса неизвестно, повтори сам позже")
    elif untrusted or not movable:
        note = _("повтори после {when}").format(when=limits.clock(when))
    elif limit_retry >= limits.MAX_RETRIES:
        note = _("повторы исчерпаны, продолжи сам после {when}").format(when=limits.clock(when))
    elif unattended and when - time.time() > limits.BACKGROUND_MAX_WAIT_SEC:
        note = _("фоновый запуск не повторяю, сброс в {when}").format(when=limits.clock(when))
    else:
        # Обёртка «проверь сделанное» и после откладывания до первого запуска:
        # тот мог частично выполниться (ревью Codex 20.10.2026). Не вкладывается.
        again = prompt if prompt.startswith(LIMIT_RETRY_HEAD) else _(
            "{head} Лимит Claude сбросился. Прошлый запуск по этому запросу оборвался на исчерпании "
            "лимита: сначала проверь, что уже сделано, и продолжи, не повторяя сделанного.\n\n"
            "Запрос:\n{prompt}").format(head=LIMIT_RETRY_HEAD, prompt=prompt)
        if _limit_defer(ctx, chat_key, chat_id, thread_id, again, when, limit_retry + 1, event_ref):
            note = _("продолжу сам в {when}").format(when=limits.clock(when))
        else:
            note = _("повтор не поставлен, повтори сам после {when}").format(when=limits.clock(when))
    return dataclasses.replace(result, text=f"{result.text}\n\n⏸ {note}" if note else result.text)


# Начало отложенного повтора после исчерпания: по нему повтор не оборачивается дважды.
LIMIT_RETRY_HEAD = "⏸ limit-retry:"


# Метка в ответе Codex: в сообщении было поручение, Claude выполнит его после
# сброса. Без метки продолжения нет, лишнего ответа тоже (ревью Codex 20.10.2026).
LIMIT_TODO_MARK = "[[поручения]]"


def _limit_talk_prompt(prompt: str, reset: float | None) -> str:
    when = limits.clock(limits.fire_at(reset)) if reset is not None else _("неизвестного времени")
    return _(
        "Claude сейчас недоступен: лимит подписки исчерпан до {when}. Ты отвечаешь Ивану вместо "
        "Домового только разговором: памяти и инструментов Домового у тебя нет, ничего не меняй и "
        "не записывай. Если в сообщении есть поручение (записать, отправить, изменить, напомнить), "
        "не выполняй его, а скажи, что Claude выполнит его после сброса лимита, и последней строкой "
        "ответа напиши ровно {mark}. Без поручения эту строку не пиши.\n\n"
        "Сообщение Ивана:\n{prompt}").format(when=when, prompt=prompt, mark=LIMIT_TODO_MARK)


def _limit_follow_up(ctx: ContextTypes.DEFAULT_TYPE, answer: ClaudeResult, failed: ClaudeResult,
                     chat_key: str, chat_id: int, thread_id: int | None, prompt: str, *,
                     limit_retry: int) -> ClaudeResult:
    """После ответа Codex поставить Claude на после сброса: выполнить поручения
    из сообщения, не отвечая заново (ревью Codex 20.10.2026). Только если Codex
    отметил поручение."""
    lines = answer.text.rstrip().splitlines()
    if not lines or lines[-1].strip() != LIMIT_TODO_MARK:
        # Метка только отдельной последней строкой: упоминание в тексте не в счёт.
        return answer
    answer = dataclasses.replace(answer, text="\n".join(lines[:-1]).rstrip())
    reset = failed.limit_reset
    if reset is None:
        return dataclasses.replace(answer, text=answer.text + "\n\n" + _(
            "⏸ время сброса неизвестно: поручения из сообщения повтори сам позже"))
    if limit_retry >= limits.MAX_RETRIES:
        return dataclasses.replace(answer, text=answer.text + "\n\n" + _(
            "⏸ повторы исчерпаны: поручения из сообщения повтори сам после {when}").format(
                when=limits.clock(limits.fire_at(reset))))
    done = "\n\n".join(failed.texts)[-2000:]
    task = _(
        "Лимит Claude сбросился. Пока его не было, на сообщение Ивана ответил Codex только "
        "разговором, без действий. Заново на сообщение не отвечай. До исчерпания Claude мог "
        "успеть часть поручений: сначала проверь журнал, дневник и прочие следы, что уже "
        "сделано, и не повторяй сделанного. Оставшиеся поручения выполни и коротко отчитайся.\n\n"
        "Сообщение Ивана:\n{prompt}\n\nЧто Claude успел ответить до исчерпания:\n{done}"
        "\n\nОтвет Codex:\n{body}"
    ).format(prompt=prompt, done=done or "—", body=answer.text[-3000:])
    fire_at = limits.fire_at(reset)
    if _limit_defer(ctx, chat_key, chat_id, thread_id, task, fire_at, limit_retry + 1):
        return dataclasses.replace(answer, text=answer.text + "\n\n" + _(
            "⏸ поручения из сообщения Claude выполнит сам в {when}").format(when=limits.clock(fire_at)))
    return dataclasses.replace(answer, text=answer.text + "\n\n" + _(
        "⏸ задание не поставлено: поручения из сообщения повтори сам после {when}").format(
            when=limits.clock(fire_at)))


async def _limit_handle(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, result: ClaudeResult,
                        chat_key: str, chat_id: int, thread_id: int | None, prompt: str, *,
                        unattended: bool, untrusted: bool, limit_retry: int, movable: bool,
                        agent_context: str, procs: dict, unexited: list,
                        event_ref: dict | None = None,
                        ) -> tuple[ClaudeResult, bool, tuple[str, ...]]:
    """Ран упал на исчерпании подписки. Сообщение Ивана (не фон, не чужой
    текст, не задача, не /plan, не кнопка): ответ Codex только разговором,
    всегда read-only. Остальное: повтор Claude после сброса. Возвращает
    результат, был ли Codex и ответы Claude до сбоя."""
    if movable and not unattended and not untrusted and cfg.codex_bin and cfg.codex_fallback:
        log.warning("claude subscription limit — conversation via codex chat=%s", chat_key)
        if result.session_id:
            await _b._cleanup_abandoned_approvals(
                ctx, result.session_id, "deny:claude-exited", _("🚫 карточка больше не действует: ран закончился"))
        if not ctx.application.bot_data.get("stopping"):
            reset = result.limit_reset
            answer = await _b._codex_as_claude_result(
                cfg, _limit_talk_prompt(prompt, reset),
                header=_("⚠️ лимит Claude до {when} → ответ через GPT (codex), только разговор").format(
                    when=limits.clock(limits.fire_at(reset)) if reset is not None else "?"),
                agent_context=agent_context, sandbox="read-only",
                chat_key=chat_key, process_registry=procs,
                should_stop=lambda: bool(ctx.application.bot_data.get("stopping")),
                unexited=unexited)
            if not answer.is_error:
                return (_limit_follow_up(ctx, answer, result, chat_key, chat_id, thread_id, prompt,
                                         limit_retry=limit_retry), True, result.texts)
            if answer.auth_error:
                return answer, True, result.texts
            # У Codex тоже отказ: остаётся повтор Claude после сброса.
    return (_limit_after_error(ctx, result, chat_key, chat_id, thread_id, prompt,
                               unattended=unattended, untrusted=untrusted, movable=movable,
                               limit_retry=limit_retry, event_ref=event_ref), False, ())


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

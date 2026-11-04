"""Пробуждения по событиям (POST /event) (перенесено из bot.py, шаг 11 разбиения 2)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram.ext import Application, ContextTypes

from . import events, tasks
from .claude_runner import SessionStore
from .config import Config
from .i18n import _

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


def _make_event_wake(app: Application) -> Callable[..., Awaitable[None]]:
    """Wake-фабрика для POST /event {"wake": true} и для пробуждений после рестарта."""
    cfg: Config = app.bot_data["cfg"]

    async def _event_wake(
        chat_id: int, thread_id: int | None, prompt: str, fresh: bool = False,
        untrusted: bool = False, silent: bool = False, event_id: str | None = None,
        deal_asks: bool = False,
    ) -> None:
        if untrusted:
            # Текст события в ограждении: ран идёт свежей своей сессией, и её
            # первое сообщение иначе закрепило бы память за заказчиком из
            # чужого письма (ограждённое роутер Свода не считает).
            prompt = _(_b.UNTRUSTED_PROMPT_NOTE) + tasks.fence(prompt)
        store = events.event_dir(cfg.state_dir)
        seen = {"started": False, "expired": False}

        def run_started() -> None:
            # До подачи промпта: после рестарта такое событие не повторяется.
            # Пометка не записалась: файл остаётся принятым и держит ключ от
            # повтора отправителя; повтор после аварии посреди этого рана
            # возможен, это сбой диска, в журнал (ревью Codex 22.10.2026).
            seen["started"] = True
            if not _event_mark(store, event_id, status="started", started_at=time.time()):
                log.error("event %s: started mark failed, a crash now would repeat the run", event_id)

        def still_live() -> bool:
            # Срок проверяется и под замком темы: событие могло ждать чужой ран.
            rec = events.load(store, event_id)
            seen["expired"] = rec is not None and events.expired(rec)
            return not seen["expired"]

        # handle_event запускает пробуждение отдельной задачей, флаг живёт в её
        # копии контекста; reset нужен, только если позвать без своей задачи.
        token = _b._SILENT_RUN.set(silent)
        deal_token = _b._DEAL_ASKS.set(deal_asks and not untrusted)
        outcome = None
        try:
            update = _b._synthetic_update(app.bot, chat_id, thread_id)
            ctx = ContextTypes.DEFAULT_TYPE(application=app)
            # Событие уже принято (200): ожидание перезапуска его не отклоняет.
            refused = await _b._dispatch_to_claude(
                update, ctx, prompt, send_voice_reply=False, unattended=True, fresh=fresh,
                untrusted=untrusted, admitted=True,
                on_run_start=run_started if event_id else None,
                still_wanted=still_live if event_id else None, deferred=event_id is not None,
                event_ref=_event_ref(store, event_id, silent, deal_asks),
            )
            # Отказ остановкой до старта: файл остаётся принятым, после
            # рестарта пробуждение уйдёт само. Иначе событие отработано, в
            # том числе пропуском по лимиту или переносом в /at (один владелец).
            # Остановка бота до старта процесса тоже ждёт рестарта: run_claude
            # тогда отвечает «отменено», а не отказом (ревью 22.10.2026).
            stopped = not seen["started"] and bool(app.bot_data.get("stopping"))
            if not (refused is True and not seen["started"]) and not stopped:
                outcome = ("ran" if seen["started"] else "expired" if seen["expired"] else "not started")
        except Exception:
            log.exception("event wake failed chat=%s", chat_id)
            outcome = "error"
        finally:
            _b._DEAL_ASKS.reset(deal_token)
            _b._SILENT_RUN.reset(token)
        noted = False
        if event_id and outcome:
            if outcome == "expired":
                log.warning("event %s expired while waiting, not started", event_id)
                noted = await _event_expired_note(app, events.load(store, event_id))
            _event_mark(store, event_id, status="done", outcome=outcome, done_at=time.time())
        key = f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)
        if outcome in ("not started", "expired", "error") and not noted and _b._is_workshop(cfg, key):
            # Тема мастерской заглушена: что наряд не запустился (лимит, срок,
            # сбой), Иван узнаёт в личке (проект Домового 22.10.2026). Строка
            # «устарело» уже ушла в личку: второй о том же не нужно. Звук по
            # тишине события.
            with contextlib.suppress(Exception):
                await app.bot.send_message(cfg.primary_chat_id, _(
                    "🛠 Мастерская: «{what}» не запустилось ({why}), подробности в теме мастерской").format(
                        what=(events.load(store, event_id) or {}).get("title") or "?",
                        why={"not started": _("лимит или отказ, перенос назван в теме"),
                             "expired": _("срок вышел"), "error": _("сбой")}[outcome]),
                    disable_notification=silent)

    return _event_wake


def _event_mark(store: Path, event_id: str | None, **fields: Any) -> bool:
    if not event_id:
        return True
    try:
        return events.update(store, event_id, **fields) is not None
    except (OSError, ValueError):
        log.exception("event store update failed id=%s", event_id)
        return False


def _event_ref(store: Path, event_id: str | None, silent: bool, deal_asks: bool = False) -> dict | None:
    """Что событие передаёт отложенному запуску при лимите: сам id (после
    рестарта событие с заданием /at не будится второй раз), срок и тишину
    (ревью Codex 22.10.2026)."""
    if not event_id:
        return None
    rec = events.load(store, event_id)
    # deal_asks только истиной: задание /at без поля шлёт обычные карточки.
    return {"event_id": event_id, "expires_at": rec.get("expires_at") if rec else None, "silent": silent,
            "deal_asks": True if deal_asks else None}


def _event_show_target(app: Application, rec: dict) -> tuple[int, int | None, str]:
    """Куда сказать о событии: в его чат, а для мастерской в личку с 🛠."""
    cfg: Config = app.bot_data["cfg"]
    key = f"{rec['chat_id']}:{rec['thread_id']}" if rec.get("thread_id") is not None else str(rec["chat_id"])
    if _b._is_workshop(cfg, key):
        return cfg.primary_chat_id, None, _("🛠 Мастерская: ")
    return rec["chat_id"], rec.get("thread_id"), ""


async def _event_expired_note(app: Application, rec: dict | None) -> bool:
    """Строка в чат: пробуждение устарело, не дождавшись очереди или рестарта.
    Всегда без звука. True: строка ушла."""
    if not rec:
        return False
    with contextlib.suppress(Exception):
        chat_id, thread_id, head = _event_show_target(app, rec)
        await app.bot.send_message(
            chat_id, head + _("⌛ Пробуждение «{source}: {title}» устарело, пока ждало очереди или "
                              "перезапуска, и не запускалось.").format(
                source=rec.get("source"), title=rec.get("title")),
            message_thread_id=thread_id, disable_notification=True)
        return True
    return False


async def _events_resume(app: Application) -> None:
    """После рестарта: непринятые к старту пробуждения будятся с пометкой,
    сколько ждали; прерванные посреди рана не повторяются, а называются в
    чате; просроченные выбрасываются со строкой в чат; перенесённые лимитом
    в /at остаются заданию (ревью Codex 22.10.2026). Каждая запись отдельно:
    испорченная не мешает остальным."""
    cfg: Config = app.bot_data["cfg"]
    store = events.event_dir(cfg.state_dir)
    try:
        wake, interrupted, stale = await asyncio.to_thread(events.startup, store, None,
                                                           app.bot_data.get("boot_id"))
    except Exception:
        log.exception("event store scan failed")
        return
    deferred: set[str] = set()
    at_store: SessionStore | None = app.bot_data.get("at_jobs")
    for entry in list((at_store.data if at_store is not None else {}).values()):
        with contextlib.suppress(Exception):
            deferred.add(str(json.loads(entry["v"]).get("event_id") or ""))
    now = time.time()
    wake_fn = app.bot_data.get("event_wake")
    tasks_set: set[asyncio.Task] = app.bot_data.setdefault("event_wake_tasks", set())
    for kind, rec in [("stale", r) for r in stale] + [("cut", r) for r in interrupted] + [("wake", r) for r in wake]:
        try:
            eid = rec["id"]
            if rec["id"] in deferred:
                _event_mark(store, eid, status="done", outcome="deferred", done_at=now)
            elif kind == "stale":
                log.warning("event %s (%s: %s) expired while the bot was down, dropped",
                            eid, rec.get("source"), rec.get("title"))
                await _event_expired_note(app, rec)
                _event_mark(store, eid, status="done", outcome="expired", done_at=now)
            elif kind == "cut":
                log.warning("event %s interrupted by restart mid-run, not repeated", eid)
                when = datetime.fromtimestamp(float(rec.get("accepted_at") or now)).strftime("%H:%M")
                show_chat, show_thread, head = _event_show_target(app, rec)
                await app.bot.send_message(
                    show_chat, head + _("⚠️ Пробуждение «{source}: {title}» (получено в {when}) оборвал "
                                        "перезапуск бота посреди рана. Повторять не стал: что успело "
                                        "сделаться, видно по журналу и чату.").format(
                        source=rec.get("source"), title=rec.get("title"), when=when),
                    message_thread_id=show_thread, disable_notification=rec.get("silent") is True)
                # Пометка после доставки: не дошло, скажет при следующем старте.
                _event_mark(store, eid, status="done", outcome="interrupted", done_at=now)
            elif wake_fn is not None:
                fresh, untrusted, silent = events.resume_flags(rec, now)
                accepted = float(rec["accepted_at"])
                note = _("(Событие получено в {when}, ждало {minutes} мин: бот перезапускался.)\n\n").format(
                    when=datetime.fromtimestamp(accepted).strftime("%H:%M"), minutes=int((now - accepted) // 60))
                log.warning("event %s resumed after restart (untrusted=%s fresh=%s)", eid, untrusted, fresh)
                # Учёт как у принятого события: перезапуск по заявке его дождётся.
                app.bot_data["admitted_events"] = app.bot_data.get("admitted_events", 0) + 1
                task = asyncio.create_task(wake_fn(
                    rec["chat_id"], rec.get("thread_id"), note + str(rec.get("prompt") or ""),
                    fresh, untrusted=untrusted, silent=silent, event_id=eid,
                    deal_asks=rec.get("deal_asks") is True))
                tasks_set.add(task)
                task.add_done_callback(tasks_set.discard)

                def _release(_t: asyncio.Task) -> None:
                    app.bot_data["admitted_events"] -= 1

                task.add_done_callback(_release)
        except Exception:
            log.exception("event %s resume failed", rec.get("id"))


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

"""Команда /at и отложенные запуски (перенесено из bot.py, шаг 6 разбиения 2)."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

from telegram.ext import Application, ContextTypes

from .burst import parse_duration
from .claude_runner import SessionStore
from .config import Config
from .i18n import _
from .replies import split_for_telegram

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


def _parse_at_when(spec: str) -> float | None:
    """Секунды до срабатывания: '45m'/'2h'/'90s' или 'HH:MM' (ближайшее)."""
    dur = parse_duration(spec)
    if dur is not None:
        return float(dur)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", spec.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _at_fire(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data: dict = ctx.job.data
    # at_running с первой строки: пока задание выполняется, отмена заявки на
    # перезапуск не ставит его в очередь второй раз (ревью Codex 13.10.2026).
    running: set[str] = ctx.application.bot_data.setdefault("at_running", set())
    if data.get("drained") and data["jid"] in running:
        return  # запись уже везёт новое сообщение темы (_drained_claim)
    running.add(data["jid"])
    try:
        await _at_fire_running(ctx, data)
    finally:
        running.discard(data["jid"])


async def _at_fire_running(ctx: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    store: SessionStore = ctx.application.bot_data["at_jobs"]
    prompt = data["prompt"]
    expires_at = data.get("expires_at")
    if expires_at is not None and float(expires_at) <= time.time():
        # Перенесённое лимитом событие со сроком: окно прошло, не запускать.
        log.warning("at job %s expired (event %s), dropped", data["jid"], data.get("event_id"))
        store.remove(data["jid"])
        return
    token = _b._SILENT_RUN.set(bool(data.get("silent")))
    deal_token = _b._DEAL_ASKS.set(data.get("deal_asks") is True)
    try:
        await _at_fire_dispatch(ctx, data, store, prompt)
    finally:
        _b._DEAL_ASKS.reset(deal_token)
        _b._SILENT_RUN.reset(token)


async def _at_fire_dispatch(ctx: ContextTypes.DEFAULT_TYPE, data: dict, store: SessionStore,
                            prompt: str) -> None:
    drained = bool(data.get("drained"))
    if drained and data["jid"] not in store.data:
        # Отложенное на перезапуск увезло новое сообщение темы или снял /cancel.
        return
    # Слова самого Ивана, отложенные на перезапуск: пометка «⏰ отложенный
    # промпт» не нужна, о переносе он знает из ответа на своё сообщение.
    if not drained:
        try:
            await ctx.bot.send_message(
                chat_id=data["chat_id"],
                text=_("⏰ отложенный промпт: {prompt}").format(prompt=prompt[:200]),
                message_thread_id=data["thread_id"],
            )
        except Exception:
            log.exception("at-notice failed jid=%s", data["jid"])
    update = _b._synthetic_update(ctx.bot, data["chat_id"], data["thread_id"])
    # Запись снимается, когда ран получил тему, а не до очереди замка: задание,
    # ждавшее занятую тему, переживает рестарт (восстановление в post_init).
    # Начатый ран после аварии не повторяется, чтобы не задвоить действия
    # (ревью 12.10.2026). Отклонённое на время ожидания перезапуска тоже
    # остаётся на диске и уйдёт после рестарта (ревью Codex 13.10.2026).
    rejected = raised = False
    try:
        expires_at = data.get("expires_at")
        ref = {k: data[k] for k in ("event_id", "expires_at", "silent", "deal_asks")
               if data.get(k) is not None} or None
        rejected = bool(await _b._dispatch_to_claude(
            # Отложенное на перезапуск это сообщение Ивана: идёт с его правами
            # (окно ⏩, «gpt:»), а не как фоновый /at.
            update, ctx, prompt, send_voice_reply=drained and bool(data.get("voice")),
            unattended=not drained,
            on_run_start=lambda: store.remove(data["jid"]), deferred=True,
            limit_retry=int(data.get("limit_retry") or 0),
            # Срок события и под замком темы: задание могло ждать чужой ран.
            # Отложенное на перезапуск: /cancel снимает запись, пока задание ждёт тему.
            still_wanted=(lambda: float(expires_at) > time.time()) if expires_at is not None
            else (lambda: data["jid"] in store.data) if drained else None,
            event_ref=ref,
        ))
    except Exception:
        raised = True  # отложенное Ивана ждёт следующего старта, а не пропадает молча
        raise
    finally:
        bd = ctx.application.bot_data
        if not rejected and not bd.get("stopping") and not (drained and raised):
            # Ран не понадобился (идёт вход, ошибка до старта): задание отработано.
            store.remove(data["jid"])
        elif rejected and not bd.get("stopping") and not bd.get("draining") \
                and data["jid"] in store.data:
            # Заявку отменили, пока шёл отказ: отмена пропустила это задание
            # как выполняющееся, вернуть его в очередь здесь.
            _b._schedule_at_job(ctx.application, data["jid"],
                             {k: v for k, v in data.items() if k != "jid"})


def _schedule_at_job(application: Application, jid: str, payload: dict) -> None:
    delay = max(1.0, float(payload["fire_at"]) - time.time())
    application.job_queue.run_once(
        _at_fire, when=delay, data={"jid": jid, **payload}, name=f"at:{jid}",
    )


async def on_at(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/at 45m <текст> | /at 14:30 <текст> | /at (список) | /at cancel <id>."""
    cfg: Config = ctx.application.bot_data["cfg"]
    if not _b._is_allowed(cfg, update):
        return
    msg = update.message
    chat_key, chat_id, thread_id = _b._chat_key(update)
    store: SessionStore = ctx.application.bot_data["at_jobs"]

    if not ctx.args:
        rows = []
        for jid, entry in store.data.items():
            try:
                payload = json.loads(entry["v"])
            except (json.JSONDecodeError, KeyError):
                continue
            if payload.get("chat_key") != chat_key:
                continue
            when = datetime.fromtimestamp(payload["fire_at"]).strftime("%d.%m %H:%M")
            rows.append(f"• {jid} · {when} · {payload['prompt'][:60]}")
        if not rows:
            await msg.reply_text(
                _("Отложенных промптов нет.\n"
                  "Формат: /at 45m <текст> | /at 14:30 <текст> | /at cancel <id>")
            )
            return
        # Длинный список режем тем же сплиттером, что и ответы claude.
        for chunk in split_for_telegram(
            _("Отложенные промпты этой темы:\n{rows}").format(rows="\n".join(rows)), cfg.telegram_text_limit
        ):
            await msg.reply_text(chunk)
        return

    if ctx.args[0].lower() == "cancel":
        if len(ctx.args) < 2:
            await msg.reply_text(_("Формат: /at cancel <id>"))
            return
        jid = ctx.args[1].strip()
        for j in ctx.application.job_queue.get_jobs_by_name(f"at:{jid}"):
            j.schedule_removal()
        removed = store.remove(jid)
        await msg.reply_text(_("🗑 отменено.") if removed else _("⚠️ id {jid} не найден.").format(jid=jid))
        return

    delay = _parse_at_when(ctx.args[0])
    parts = (msg.text or "").split(maxsplit=2)
    prompt = parts[2].strip() if len(parts) >= 3 else ""
    if delay is None or not prompt:
        await msg.reply_text(_("Формат: /at 45m <текст> | /at 2h <текст> | /at 14:30 <текст>"))
        return
    jid = uuid.uuid4().hex[:6]
    fire_at = time.time() + delay
    payload = {
        "chat_key": chat_key, "chat_id": chat_id, "thread_id": thread_id,
        "fire_at": fire_at, "prompt": prompt,
    }
    store.set(jid, json.dumps(payload, ensure_ascii=False))
    _b._schedule_at_job(ctx.application, jid, payload)
    when = datetime.fromtimestamp(fire_at).strftime("%d.%m %H:%M:%S")
    await msg.reply_text(_("⏰ ок, {when}: «{prompt}» (отмена: /at cancel {jid})").format(when=when, prompt=prompt[:80], jid=jid))


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

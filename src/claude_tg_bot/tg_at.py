"""Команда /at и отложенные запуски (перенесено из bot.py, шаг 6 разбиения 2)."""
from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

from telegram.ext import Application, ContextTypes

from .burst import parse_duration
from .claude_runner import SessionStore
from .config import Config, local_zone, now_local
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
    # В поясе бота, не системы. Разность через timestamp: вычитание aware в
    # одном поясе считает по стрелкам и ошибается на час в день перевода часов.
    now = now_local()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp() - now.timestamp()


# Повторная проверка, которую агент назначил себе сам (<<LATER:...>>, решение
# Ивана 04.11.2026): срок от 5 минут до 14 дней, в теме ждут не больше трёх,
# подряд без слова Ивана не больше трёх шагов. С 04.11.2026 это обычный фоновый
# ран (решение Ивана: замка «только чтение» нет), правило в тексте запроса.
LATER_MIN_SEC = 300
LATER_MAX_SEC = 14 * 86400
LATER_MAX_PENDING = 3
LATER_MAX_HOPS = 3


def _parse_later_when(spec: str) -> float | None:
    """Секунды до проверки: как у /at, плюс 'Nd'; вне 5 минут..14 дней None."""
    spec = spec.strip()
    if len(spec) > 8:
        return None  # огромное число переполнило бы float (ревью Codex 04.11.2026)
    m = re.fullmatch(r"(\d{1,2})\s*d", spec.lower())
    delay = float(int(m.group(1)) * 86400) if m else _parse_at_when(spec)
    if delay is None or not LATER_MIN_SEC <= delay <= LATER_MAX_SEC:
        return None
    return delay


def _later_hops(data: dict) -> int:
    """Шаг цепочки из записи задания; испорченное значение считается первым."""
    try:
        return max(1, int(data.get("hops") or 1))
    except (TypeError, ValueError, OverflowError):
        return 1


def schedule_later(app: Application, chat_key: str, chat_id: int, thread_id: int | None,
                   spec: str, about: str, hops: int, *, silent: bool = False) -> tuple[str | None, str]:
    """Поставить повторную проверку агента обычной записью /at с пометкой
    later → (id или None, строка для футера). silent: назначил ран без звука,
    проверка тоже без звука."""
    if hops > LATER_MAX_HOPS:
        return None, _("⏰ цепочка «позже» дошла до {n}, дальше по твоему слову").format(n=LATER_MAX_HOPS)
    delay = _parse_later_when(spec)
    if delay is None:
        return None, _("⏰ «позже» не поставлено: срок «{spec}» не понят или вне пределов "
                       "(от 5 минут до 14 дней: Nm, Nh, Nd или ЧЧ:ММ)").format(spec=spec[:20])
    store: SessionStore | None = app.bot_data.get("at_jobs")
    if store is None or getattr(app, "job_queue", None) is None:
        return None, _("⏰ «позже» не поставлено: очередь заданий недоступна")
    pending = 0
    for entry in list(store.data.values()):
        try:
            job = json.loads(entry["v"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if job.get("later") and job.get("chat_key") == chat_key:
            pending += 1
    if pending >= LATER_MAX_PENDING:
        return None, _("⏰ «позже» не поставлено: в теме уже ждут {n} проверки агента (/at)").format(
            n=LATER_MAX_PENDING)
    jid = uuid.uuid4().hex[:6]
    fire_at = time.time() + delay
    prompt = _("Повторная проверка, которую ты назначил себе {when}: «{about}». Проверь и "
               "коротко доложи; ничего не меняй: нужно действие, спроси Ивана маркером "
               "ASK.").format(
        when=now_local().strftime("%d.%m %H:%M"), about=about)
    payload = {
        "chat_key": chat_key, "chat_id": chat_id, "thread_id": thread_id,
        "fire_at": fire_at, "prompt": prompt, "later": True, "hops": hops, "about": about,
    }
    if silent:
        payload["silent"] = True
    try:
        store.set(jid, json.dumps(payload, ensure_ascii=False))
        _b._schedule_at_job(app, jid, payload)
    except Exception:
        log.exception("later: scheduling failed chat=%s", chat_key)
        with contextlib.suppress(Exception):
            store.remove(jid)
        return None, _("⏰ «позже» не поставлено: сбой записи задания, смотри журнал бота")
    log.info("later: scheduled chat=%s jid=%s hops=%d in=%.0fs", chat_key, jid, hops, delay)
    when = datetime.fromtimestamp(fire_at, local_zone()).strftime("%d.%m %H:%M")
    return jid, _("⏰ позже: {when} «{about}» (отмена /at cancel {jid})").format(
        when=when, about=about[:80], jid=jid)


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
    # Шаг цепочки «позже»: маркер из ответа этого рана получит следующий.
    hops_token = _b._LATER_HOPS.set(_later_hops(data)) if data.get("later") else None
    try:
        await _at_fire_dispatch(ctx, data, store, prompt)
    finally:
        if hops_token is not None:
            _b._LATER_HOPS.reset(hops_token)
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
    later = bool(data.get("later"))
    # Проверка агента отдельным уведомлением не пишет: строка шага идёт в
    # футер её ответа (Иван на пробе 04.11.2026 получил два сообщения).
    step_note = _("⏰ позже (сам назначил, шаг {n} из {total}): {about}").format(
        n=_later_hops(data), total=LATER_MAX_HOPS,
        about=str(data.get("about") or prompt)[:80]) if later else ""
    if not drained and not later:
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
            # Проверку назначил агент, а не Иван: метка для переноса лимитом.
            read_only=later, step_note=step_note,
            later_about=str(data.get("about") or "") if later else "", at_jid=data["jid"],
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
            when = datetime.fromtimestamp(payload["fire_at"], local_zone()).strftime("%d.%m %H:%M")
            # Назначил агент сам (<<LATER:...>>): пометка и его текст.
            later = bool(payload.get("later"))
            text = str(payload.get("about") or payload["prompt"]) if later else payload["prompt"]
            rows.append(f"• {jid} · {when} · {'🤖 ' if later else ''}{text[:60]}")
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
    when = datetime.fromtimestamp(fire_at, local_zone()).strftime("%d.%m %H:%M:%S")
    await msg.reply_text(_("⏰ ок, {when}: «{prompt}» (отмена: /at cancel {jid})").format(when=when, prompt=prompt[:80], jid=jid))


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

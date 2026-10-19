"""Burst auto-approve: окно времени, внутри которого approve-карточка не
отправляется, а решение «allow» возвращается сразу. Окно scoped по Telegram
target: чат + topic/thread. Так auto в одной ветке не глушит approve-карточки
в другой, но продолжает работать для всех текущих/следующих Claude-сессий
внутри той же ветки. State в bot_data — переживает sessions Claude, но НЕ
рестарт бота.

Выделено из bot.py: ключ окна нужен и bot.py (команда /auto, кнопки ⏩), и
approval.py (проверка окна в handle_approve) — раньше burst_target_key жил
дублем в обоих файлах, и расхождение формата тихо ломало матчинг.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from telegram.ext import Application, ContextTypes

from .i18n import _

log = logging.getLogger(__name__)

BURST_DEFAULT_SEC = 15 * 60
BURST_LONG_SEC = 60 * 60
BURST_MIN_SEC = 60
BURST_MAX_SEC = BURST_LONG_SEC


def burst_target_key(chat_id: int, thread_id: int | None) -> str:
    return f"{chat_id}:{thread_id}" if thread_id is not None else f"{chat_id}"


def burst_until_ts(ctx: ContextTypes.DEFAULT_TYPE, target_key: str) -> float:
    windows: dict[str, float] = ctx.application.bot_data.setdefault("burst_until_by_target", {})
    return float(windows.get(target_key, 0.0))


def format_burst_local(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


async def _burst_expiry_watcher(
    application: Application,
    target_key: str,
    until_ts: float,
    chat_id: int,
    thread_id: int | None,
) -> None:
    """Спит до истечения окна и шлёт уведомление, ЕСЛИ окно не было заменено
    другим вызовом /auto и не отключено. Если кто-то перезапустил окно — этот
    watcher просыпается и видит другое значение target-window → молча выходит.
    """
    delay = max(0.0, until_ts - time.time())
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    windows: dict[str, float] = application.bot_data.setdefault("burst_until_by_target", {})
    cur = float(windows.get(target_key, 0.0))
    if abs(cur - until_ts) > 0.5:
        # Окно перезаписано или сдвинуто — другой watcher отвечает за уведомление.
        return
    windows.pop(target_key, None)
    tasks: dict[str, asyncio.Task] = application.bot_data.setdefault("burst_expiry_tasks", {})
    tasks.pop(target_key, None)
    try:
        await application.bot.send_message(
            chat_id=chat_id, text=_("⏸ auto-approve истёк"), message_thread_id=thread_id,
        )
    except Exception:
        log.exception("burst expiry notification failed")


def burst_set(
    ctx: ContextTypes.DEFAULT_TYPE,
    target_key: str,
    duration_sec: int,
    chat_id: int,
    thread_id: int | None,
) -> float:
    """Активирует/сдвигает окно, отменяет предыдущий watcher (если был),
    ставит новый. Возвращает absolute until_ts."""
    duration_sec = max(BURST_MIN_SEC, min(int(duration_sec), BURST_MAX_SEC))
    until = time.time() + duration_sec
    windows: dict[str, float] = ctx.application.bot_data.setdefault("burst_until_by_target", {})
    windows[target_key] = until
    tasks: dict[str, asyncio.Task] = ctx.application.bot_data.setdefault("burst_expiry_tasks", {})
    prev = tasks.get(target_key)
    if prev is not None and not prev.done():
        prev.cancel()
    tasks[target_key] = asyncio.create_task(
        _burst_expiry_watcher(ctx.application, target_key, until, chat_id, thread_id)
    )
    return until


def burst_clear(ctx: ContextTypes.DEFAULT_TYPE, target_key: str) -> bool:
    """Сбрасывает окно, отменяет watcher. True если окно было активным."""
    was_active = burst_until_ts(ctx, target_key) > time.time()
    windows: dict[str, float] = ctx.application.bot_data.setdefault("burst_until_by_target", {})
    windows.pop(target_key, None)
    tasks: dict[str, asyncio.Task] = ctx.application.bot_data.setdefault("burst_expiry_tasks", {})
    prev = tasks.pop(target_key, None)
    if prev is not None and not prev.done():
        prev.cancel()
    return was_active


def burst_duration_for_decision(decision: str) -> int | None:
    if decision == "auto15":
        return BURST_DEFAULT_SEC
    if decision == "auto60":
        return BURST_LONG_SEC
    if decision == "auto":
        # Backward compatibility for already-sent Telegram cards from older bot versions.
        return BURST_DEFAULT_SEC
    return None


_DURATION_RE = re.compile(r"^(\d+)\s*([smh]?)$", re.IGNORECASE)


def parse_duration(spec: str) -> int | None:
    """`15m` / `1h` / `45m` / `15` (минуты по дефолту). None если не парсится."""
    m = _DURATION_RE.match(spec.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower() or "m"
    return n * {"s": 1, "m": 60, "h": 3600}[unit]

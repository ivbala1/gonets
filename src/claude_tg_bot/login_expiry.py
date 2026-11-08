"""Напоминание о конце сохранённого входа claude.ai.

С Claude Code 2.1.318 `claude auth status` отдаёт `loginExpiresAt`, дату конца
входа. Сам CLI считает её верхней границей: вход может кончиться в любой
момент за 3 дня до неё («Sign-in ends: between ... and ...»), поэтому пороги
отсчитываются от самого раннего конца. Без входа встают бот, Домовой и
сводки, поэтому за 10, 3 и 1 день до раннего конца и после конца бот пишет
владельцу в личку с кнопкой входа. Каждый порог один раз; новая дата (был
перевход) сбрасывает отметки.

После настоящего конца CLI на первом же обновлении токена стирает его, и
`auth status` отдаёт `loggedIn: false`, код выхода 1 и пустую дату. Тогда
конец объявляется по дате, сохранённой в отметках, если окно уже открылось.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

from . import jsonstore
from . import procs
from .codex_runner import CODEX_OWN_SANDBOX_MARKERS
from .config import agent_env, local_zone
from .i18n import _
from .replies import is_unconfirmed

log = logging.getLogger(__name__)

THRESHOLDS = (10, 3, 1)
EXPIRED = "expired"
# Отметка «вышел до окна»: дата не означает конец, «кончился» по ней не слать.
LEFT = "left"
# Последняя секунда, которую понимает datetime (9999-12-31).
MAX_TS = 253402300799
STATE_FILE = "login_expiry.json"
DAY = 86400
# Окно раннего конца как в CLI 2.1.318: earliest = loginExpiresAt - 3 дня.
WINDOW = 3 * DAY
TIMEOUT = 15


class LoginStatus(NamedTuple):
    """Ответ `claude auth status`: logged_in None значит ответа не разобрать."""
    logged_in: bool | None
    expires: float | None


UNKNOWN = LoginStatus(None, None)


def parse_expiry(value) -> float | None:
    """Дата конца входа в секундах эпохи: строка ISO, секунды или миллисекунды."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        # Миллисекунды: 1e11 секунд это 5138 год, 1e11 миллисекунд это 1973.
        value = value / 1000 if value > 1e11 else float(value)
        return value if value <= MAX_TS else None
    return None


async def read_login_status(claude_bin: str, cwd: Path | str | None = None) -> LoginStatus:
    """`claude auth status`: есть ли вход и дата его конца (None: нет поля)."""
    # Окружение и каталог как у входа в auth.py: песочница Codex процессу не нужна.
    env = {k: v for k, v in agent_env().items() if k not in CODEX_OWN_SANDBOX_MARKERS}
    env["NO_COLOR"] = "1"
    # Таймаут или отмена: run_bounded добивает всю группу, даже если сам CLI
    # вышел, а потомок держит вывод. stderr в журнал не идёт.
    try:
        rc, stdout, _err = await procs.run_bounded(
            [claude_bin, "auth", "status", "--json"], timeout=TIMEOUT, env=env,
            cwd=cwd or None, stderr=asyncio.subprocess.DEVNULL)
    except OSError as exc:
        log.warning("login expiry: claude auth status not started (%s)", type(exc).__name__)
        return UNKNOWN
    except TimeoutError:
        log.warning("login expiry: claude auth status timed out")
        return UNKNOWN
    # Без входа CLI печатает тот же JSON и выходит с кодом 1.
    if rc not in (0, 1):
        log.warning("login expiry: claude auth status exited %s", rc)
        return UNKNOWN
    # В выводе почта и организация: в журнал только итог разбора.
    try:
        data = json.loads(stdout)
    except ValueError:
        log.warning("login expiry: claude auth status is not JSON")
        return UNKNOWN
    logged_in = data.get("loggedIn") if isinstance(data, dict) else None
    if logged_in is False:
        log.warning("login expiry: claude is not logged in")
        return LoginStatus(False, None)
    if logged_in is not True or rc != 0:
        log.warning("login expiry: unclear claude auth status (exit %s)", rc)
        return UNKNOWN
    expires = parse_expiry(data.get("loginExpiresAt"))
    if expires is None:
        log.warning("login expiry: no usable loginExpiresAt")
    return LoginStatus(True, expires)


def days_left(expires_ts: float, now: float, tz: ZoneInfo | None = None) -> int:
    """Сколько календарных дней до конца входа по часовому поясу бота."""
    tz = tz or local_zone()
    end = datetime.fromtimestamp(expires_ts, tz).date()
    return (end - datetime.fromtimestamp(now, tz).date()).days


def due_notice(expires_ts: float, now: float, state: dict,
               tz: ZoneInfo | None = None) -> tuple[str | None, dict]:
    """Какой порог пора объявить (`"10"`, `"3"`, `"1"`, `"expired"`) и новое состояние.

    Дни считаются до самого раннего конца (`expires_ts - WINDOW`). Пройденные
    пороги отмечаются все разом: после простоя бота приходит одно сообщение о
    ближайшем, а не три подряд.
    """
    # Перевход двигает дату вперёд; сдвиг вперёд меньше суток (округление) и
    # сокращение срока (обновление токена берёт меньшую дату) отметки не
    # сбрасывают, иначе пороги пошли бы повторно.
    old = _stored_expiry(state)
    same = old is not None and expires_ts - old < DAY
    sent = _sent(state) if same else []
    if expires_ts <= now:
        crossed = [str(t) for t in THRESHOLDS] + [EXPIRED]
    else:
        days = days_left(expires_ts - WINDOW, now, tz)
        crossed = [str(t) for t in THRESHOLDS if days <= t]
    # Отметки только у пройденных порогов: если срок уехал вперёд мелкими
    # сдвигами, порог снова станет ожидаемым и сработает в свой день.
    # «Кончился» уже объявлен по стёртому токену, а потом CLI снова ответил с
    # той же датой (временный сбой чтения): второй раз не объявлять.
    keep = [EXPIRED] if EXPIRED in sent and EXPIRED not in crossed else []
    new_state = {"expires_at": expires_ts, "sent": sorted(crossed + keep)}
    if not crossed or crossed[-1] in sent:
        return None, new_state
    return crossed[-1], new_state


def _stored_expiry(state: dict) -> float | None:
    value = state.get("expires_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _sent(state: dict) -> list[str]:
    """Отметки из файла: только строки, битое значение считается пустым."""
    sent = state.get("sent")
    return [s for s in sent if isinstance(s, str)] if isinstance(sent, list) else []


def logged_out_notice(now: float, state: dict) -> tuple[str | None, dict]:
    """Входа нет: объявить конец один раз, если по сохранённой дате окно открылось.

    Без сохранённой даты молчим. До окна это не конец срока (выход вручную,
    вход не через claude.ai): ставится отметка LEFT, иначе с открытием окна
    пришло бы ложное «кончился». Отметки порогов и дата остаются, LEFT снимет
    следующий ответ CLI с датой (временный сбой не повторит порог). Внутри
    окна ручной выход от конца не отличить.
    """
    old = _stored_expiry(state)
    if old is None or state.get(LEFT) is True:
        return None, state
    if now < old - WINDOW:
        return None, {**state, LEFT: True}
    sent = _sent(state)
    new_state = {"expires_at": old, "sent": sorted({*sent, *map(str, THRESHOLDS), EXPIRED})}
    if EXPIRED in sent:
        return None, new_state
    return EXPIRED, new_state


def load_state(path: Path) -> dict:
    return jsonstore.read_dict(path)


def save_state(path: Path, state: dict) -> None:
    jsonstore.write_json(path, state)


def notice_text(key: str, expires_ts: float, now: float, tz: ZoneInfo | None = None) -> str:
    tz = tz or local_zone()
    date = datetime.fromtimestamp(expires_ts, tz).strftime("%d.%m")
    start = datetime.fromtimestamp(expires_ts - WINDOW, tz).strftime("%d.%m")
    host = socket.gethostname()
    if key == EXPIRED:
        return _("⏳ Вход Claude на {host} кончился. Без него встанут бот, Домовой "
                 "и сводки. Обнови:").format(host=host)
    earliest = expires_ts - WINDOW
    if now >= earliest:
        return _("⏳ Вход Claude на {host} может кончиться в любой момент, не позже "
                 "{date}. Без него встанут бот, Домовой и сводки. Обнови сейчас:").format(
            host=host, date=date)
    days = days_left(earliest, now, tz)
    if days <= 0:
        time = datetime.fromtimestamp(earliest, tz).strftime("%H:%M")
        return _("⏳ Вход Claude на {host} может кончиться уже сегодня после {time}, "
                 "не позже {date}. Без него встанут бот, Домовой и сводки. Обнови "
                 "сейчас:").format(host=host, time=time, date=date)
    return _("⏳ Вход Claude на {host} кончается между {start} и {date} (самое раннее "
             "через {days} дн.). Без него встанут бот, Домовой и сводки. Обнови "
             "заранее:").format(host=host, start=start, date=date, days=days)


async def check_login_expiry(ctx) -> None:
    """Задача job_queue: прочитать срок входа и при пороге написать владельцу."""
    # Стартовая и ежедневная проверки могут сойтись: одна за раз. Блокировка
    # в данных приложения, а не в модуле: она привязана к своему циклу событий.
    async with ctx.application.bot_data.setdefault("login_expiry_lock", asyncio.Lock()):
        await _check(ctx)


async def _check(ctx) -> None:
    from .tg_login import _login_keyboard  # tg_login импортирует bot: не при загрузке

    cfg = ctx.application.bot_data["cfg"]
    status = await read_login_status(cfg.claude_bin, getattr(cfg, "claude_cwd", None))
    if status.logged_in is None:
        return
    now = datetime.now(timezone.utc).timestamp()
    tz = local_zone()
    path = cfg.state_dir / STATE_FILE
    state = load_state(path)
    if status.logged_in and status.expires is None:
        # Вход без срока: прежняя дата к нему может не относиться, «кончился»
        # по ней не слать; ответ с той же датой вернёт её в силу.
        key, expires = None, None
        new_state = {**state, LEFT: True} if _stored_expiry(state) is not None else state
    elif status.logged_in:
        expires = status.expires
        log.info("login expiry: %s, earliest %s, %d days left",
                 datetime.fromtimestamp(expires, tz).isoformat(timespec="minutes"),
                 datetime.fromtimestamp(expires - WINDOW, tz).isoformat(timespec="minutes"),
                 days_left(expires - WINDOW, now, tz))
        key, new_state = due_notice(expires, now, state, tz)
    else:
        key, new_state = logged_out_notice(now, state)
        expires = new_state.get("expires_at")
    if key is not None:
        try:
            await ctx.bot.send_message(
                cfg.primary_chat_id, notice_text(key, expires, now, tz),
                reply_markup=_login_keyboard(ctx, ("claude",), private=True))
        except Exception as exc:
            if is_unconfirmed(exc):
                # Сообщение обычно доходит: отметка остаётся, повтора не будет.
                log.warning("login expiry: notice unconfirmed by Telegram, not repeated")
            else:
                # Отметку не ставим: повторит следующая проверка. Дату сохраняем:
                # после стирания токена CLI её уже не отдаст.
                log.warning("login expiry: notice not sent (%s)", type(exc).__name__)
                new_state = {**new_state, "sent": [s for s in new_state["sent"] if s != key]}
    if new_state != state:
        try:
            save_state(path, new_state)
        except OSError as exc:
            log.warning("login expiry: state not saved (%s)", type(exc).__name__)

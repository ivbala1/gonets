"""Лимит подписки Claude: последний снимок окон и резерв для разговора.

Источник: событие rate_limit_event, которое `claude -p --output-format
stream-json` присылает при каждом запросе (проверено 20.10.2026):
rate_limit_info.unifiedWindows.{five_hour,seven_day}.{utilization 0..1,
resetsAt в секундах Unix}. Снимок пишется в state/claude_limits.json и
переживает рестарт. Окно с прошедшим сбросом не учитывается.

Решения Ивана 20.10.2026 (сверено с Codex): с 97% пятичасового или 99%
недельного окна, пока до сброса больше 15 минут, остаток только у разговора с Домовым (чаты
CLAUDE_TG_LIMIT_RESERVE_CHATS), фоновые дела Домового тоже уступают. Резерв
приблизительный: снимок отстаёт от расхода на маке и в идущих ранах, уже
начатые раны не прерываются. Те же пороги у мода limit-resume на маке.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import local_zone
from .i18n import _

log = logging.getLogger(__name__)

WINDOWS = ("five_hour", "seven_day")
RESERVE_AT = {"five_hour": 97.0, "seven_day": 99.0}
# Запас после сброса: сервер считает окно чуть позже часов бота.
GRACE_SEC = 60
# Фоновое дело (утро, вечер, поиск, /at), которому ждать сброса дольше этого,
# не догоняется: после сброса оно уже несвоевременно (ревью Codex 20.10.2026).
BACKGROUND_MAX_WAIT_SEC = 2 * 3600
# Ближе к сбросу резерв не держится: в худшем случае подождать несколько минут
# (слово Ивана 20.10.2026: «если до сброса 5 минут, нет смысла ограничивать»).
NEAR_RESET_SEC = 15 * 60
# Повторов после ошибки лимита одного запроса не больше этого.
MAX_RETRIES = 3

_path: Path | None = None


def configure(state_dir: Path) -> None:
    """Включить запись снимка (бот на старте). Без вызова снимок не пишется."""
    global _path
    _path = Path(state_dir) / "claude_limits.json"


def parse_event(evt: dict) -> dict | None:
    """Окна из rate_limit_event: {окно: {"pct", "resets_at"}, "status"}.
    Учёт вспомогательный: странное событие даёт None, а не исключение в ране."""
    if evt.get("type") != "rate_limit_event":
        return None
    try:
        return _parse_event(evt)
    except Exception:
        log.warning("rate_limit_event not understood", exc_info=True)
        return None


def _parse_event(evt: dict) -> dict | None:
    info = evt.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    out: dict = {"status": info.get("status") if isinstance(info.get("status"), str) else ""}
    unified = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    for kind in WINDOWS:
        w = unified.get(kind)
        if not isinstance(w, dict):
            continue
        util, reset = _num(w.get("utilization")), _num(w.get("resetsAt"))
        if util is not None and reset is not None:
            # Без округления: порог сравнивается с точным значением (ревью Codex).
            out[kind] = {"pct": util * 100, "resets_at": reset}
    if not any(k in out for k in WINDOWS):
        # Старый вид события: одно окно в rateLimitType и resetsAt.
        kind, reset = info.get("rateLimitType"), _num(info.get("resetsAt"))
        if kind in WINDOWS and reset is not None and out["status"] == "rejected":
            out[kind] = {"pct": 100.0, "resets_at": reset}
    return _clean(out)


def _num(v) -> float | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _clean(data) -> dict:
    """Только окна с конечными числами: битый снимок или событие не должны
    ломать обработку сообщений (ревью Codex 20.10.2026)."""
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for kind in WINDOWS:
        w = data.get(kind)
        if not isinstance(w, dict):
            continue
        pct, reset = _num(w.get("pct")), _num(w.get("resets_at"))
        if pct is None or reset is None or not 0 <= pct <= 1000 or not 0 < reset < 1e11:
            continue
        clean = {"pct": pct, "resets_at": reset}
        seen = _num(w.get("observed_at"))
        if seen is not None:
            clean["observed_at"] = seen
        out[kind] = clean
    if isinstance(data.get("status"), str):
        out["status"] = data["status"]
    return out


def read() -> dict:
    if _path is None:
        return {}
    try:
        return _clean(json.loads(_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def record(windows: dict, observed_at: float | None = None) -> None:
    """Слить наблюдение в снимок. Каждое окно заменяется только более свежим
    наблюдением: поздно пришедшее старое не затирает новое (ревью Codex).
    Писатель один, процесс бота: record синхронный, без await, поэтому раны
    одного цикла asyncio друг друга не теряют. Второй процесс-писатель
    потребовал бы файлового замка на весь цикл чтения и записи."""
    try:
        _record(windows, observed_at)
    except Exception:
        log.warning("claude limits snapshot not updated", exc_info=True)


def _record(windows: dict, observed_at: float | None) -> None:
    windows = _clean(windows)
    if _path is None or not windows:
        return
    seen = observed_at if observed_at is not None else time.time()
    data = read()
    changed = False
    for kind in WINDOWS:
        w = windows.get(kind)
        if not isinstance(w, dict):
            continue
        old = data.get(kind)
        if isinstance(old, dict) and (old.get("observed_at") or 0) > seen:
            continue
        data[kind] = {**w, "observed_at": seen}
        changed = True
    if not changed:
        return
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_path.parent, prefix=".claude_limits.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, _path)
    except (OSError, TypeError, ValueError):
        log.warning("claude limits snapshot not written", exc_info=True)


def reserve(snapshot: dict | None = None, now: float | None = None) -> float | None:
    """Время сброса всех окон за порогом (максимум), пока до него больше
    NEAR_RESET_SEC. None: резерва нет, в том числе за 15 минут до сброса."""
    data = read() if snapshot is None else _clean(snapshot)
    at = time.time() if now is None else now
    resets = []
    for kind in WINDOWS:
        w = data.get(kind)
        if not isinstance(w, dict):
            continue
        pct, reset = w.get("pct"), w.get("resets_at")
        if isinstance(pct, (int, float)) and isinstance(reset, (int, float)) \
                and reset - at > NEAR_RESET_SEC and pct >= RESERVE_AT[kind]:
            resets.append(float(reset))
    return max(resets) if resets else None


def summary(snapshot: dict | None = None, now: float | None = None) -> str:
    """«5 ч 98%, неделя 60%» по действующим окнам."""
    data = read() if snapshot is None else _clean(snapshot)
    at = time.time() if now is None else now
    names = {"five_hour": _("5 ч"), "seven_day": _("неделя")}
    parts = []
    for kind in WINDOWS:
        w = data.get(kind)
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)) \
                and float(w.get("resets_at") or 0) > at:
            parts.append(f"{names[kind]} {round(w['pct'])}%")
    return ", ".join(parts)


_LIMIT_RE = re.compile(
    r"^(?:you['’]ve (?:hit|reached) your (?:session|weekly|usage|opus|sonnet).*limit"
    r"|you['’]re out of (?:usage credits|extra usage)"
    r"|(?:out of usage credits|session limit reached|weekly limit reached|usage limit reached)"
    r"|rate limit reached)", re.IGNORECASE)


def is_limit_error(text: str) -> bool:
    """Ошибка исчерпания подписки, а не обычный 429 или сбой API."""
    if not isinstance(text, str):
        return False
    clean = re.sub(r"^Claude Code returned an error result:\s*", "", text.strip(), flags=re.IGNORECASE)
    clean = re.sub(r"^API Error:?\s*(?:\d{3}\s*)?", "", clean, flags=re.IGNORECASE).strip()
    return bool(_LIMIT_RE.match(clean))


# Время в тексте ошибки CLI печатает в поясе машины, где запущен; бот считает
# его в своём поясе (config.local_zone).
def reset_from_text(text: str, now: float | None = None) -> float | None:
    """«resets 2:40am (Europe/Berlin)» в секунды Unix, пояс бота. Недельный и
    чужой пояс не угадываем: пояс из текста годится, только если сейчас он идёт
    вровень с поясом бота (Europe/Kiev и Europe/Kyiv), неизвестное имя отказ."""
    if not is_limit_error(text) or re.search(r"weekly|seven.day", text, re.IGNORECASE):
        return None
    tz = local_zone()
    at = time.time() if now is None else now
    zone = re.search(r"resets?\b[^()]*\(([^()]+)\)", text, re.IGNORECASE)
    if zone and zone.group(1).strip() != tz.key:
        try:
            named = ZoneInfo(zone.group(1).strip())
        except (ZoneInfoNotFoundError, ValueError):
            return None
        moment = datetime.fromtimestamp(at, tz)
        if moment.utcoffset() != moment.astimezone(named).utcoffset():
            return None
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?!\w)", text, re.IGNORECASE)
    if not m:
        return None
    hours, minutes = int(m.group(1)), int(m.group(2) or 0)
    if minutes > 59 or (hours > 12 if m.group(3) else hours > 23) or (m.group(3) and hours < 1):
        return None
    if m.group(3):
        hours = hours % 12 + (12 if m.group(3).lower() == "pm" else 0)
    # Ближайшее подходящее из сегодня и завтра; оба fold, чтобы в час перевода
    # стрелок осенью взять вторые 02:30, а не сутки спустя. Пропущенное весной
    # время на часах не бывает: такой момент отбрасывается (ревью Codex).
    today = datetime.fromtimestamp(at, tz).date()
    moments = (datetime.combine(today + timedelta(days=d), dtime(hours, minutes), tz).replace(fold=f).timestamp()
               for d in (0, 1) for f in (0, 1))
    real = [t for t in moments if t >= at - 300
            and datetime.fromtimestamp(t, tz).time() == dtime(hours, minutes)]
    return min(real) if real else None


def hit_reset(rate: dict | None, error_text: str, now: float | None = None) -> float | None:
    """Время сброса для упавшего на лимите рана: из события рана, снимка, текста."""
    try:
        return _hit_reset(rate, error_text, now)
    except Exception:
        log.warning("limit reset time not understood", exc_info=True)
        return None


def _hit_reset(rate: dict | None, error_text: str, now: float | None) -> float | None:
    at = time.time() if now is None else now
    for source in (_clean(rate or {}), read()):
        resets = [float(w["resets_at"]) for k in WINDOWS
                  if isinstance(w := source.get(k), dict)
                  and isinstance(w.get("resets_at"), (int, float)) and w["resets_at"] > at
                  # Снимок отстаёт: окно за порогом резерва уже считается блокирующим.
                  and float(w.get("pct") or 0) >= RESERVE_AT[k]]
        if resets:
            return max(resets)
    return reset_from_text(error_text, at)


def span(first: dict | None, last: dict | None) -> str:
    """«5h=41.2>43.0,7d=58.0>58.3» для журнала: проценты в начале и в конце рана,
    по ним считается цена дела в процентах окна. «-»: событий не было."""
    a, b = _clean(first or {}), _clean(last or {})
    names = {"five_hour": "5h", "seven_day": "7d"}
    parts = [f"{names[k]}={a[k]['pct']:.1f}>{b[k]['pct']:.1f}" for k in WINDOWS if k in a and k in b]
    return ",".join(parts) or "-"


def fire_at(reset: float, now: float | None = None) -> float:
    """Когда пробовать снова: сброс плюс запас, но не раньше пяти минут от
    сейчас. Сброс из текста ошибки бывает в прошлом (ревью Codex)."""
    at = time.time() if now is None else now
    return max(reset + GRACE_SEC, at + 5 * GRACE_SEC)


def clock(ts: float) -> str:
    """Время в поясе бота для сообщений: «14:40» или «24.10 14:40» не сегодня."""
    try:
        t = datetime.fromtimestamp(ts, local_zone())
    except (OverflowError, OSError, ValueError, TypeError):
        return "?"
    today = datetime.now(t.tzinfo).date()
    return t.strftime("%H:%M") if t.date() == today else t.strftime("%d.%m %H:%M")

"""Прогрев кэша разговора: решения без бота и процесса.

Кэш промптов живёт час от последнего запроса. Когда тема молчит дольше,
следующее сообщение пишет весь разговор в кэш заново (на Opus при 150-250
тысячах это 1-2 $). Бот после IDLE минут тишины шлёт в ответвление сессии
служебный ход: он читает разговор из кэша и продлевает ему срок, а в саму
сессию ничего не пишет (claude_runner, warm_fork_sid). Замер и экономика
19.10.2026: ход около 0,16 $, неделя 13-19.10 дала бы около 18 $ экономии
в окне 09-22. Здесь только решения: окно, срок, здоровье ответа пинга.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime

# Пинг не позже этого срока от начала последнего запроса: кэш живёт 60 минут
# от него, дальше пинг переписал бы весь разговор (около 1-2 $).
LATE_SEC = 58 * 60
# Хвост ответвления: заново добавленное определение инструмента и вывод
# хуков SessionStart:fork, 15-16 тысяч (замер 19.10.2026). Больше этого
# значит, что разговор переписан, а не прочитан.
TAIL_MAX_TOKENS = 40_000
# Доля ожидаемого разговора, которую пинг обязан прочитать из кэша.
READ_MIN_SHARE = 0.9
# Ближе этой доли к окну автосжатия пинг вызвал бы сжатие копии разговора:
# дорого и впустую, следующий настоящий ход всё равно сожмёт.
COMPACT_GUARD_SHARE = 0.9

_HOURS_RE = re.compile(r"(\d{1,2})-(\d{1,2})")
_FORK_NS = uuid.UUID("6f1d3c2e-8a4b-4f7e-9c21-5d0b7e3a9f14")


def parse_hours(raw: str) -> tuple[int, int]:
    """"09-22": пинги с 09:00 до 22:00 местного времени (конец не включён)."""
    m = _HOURS_RE.fullmatch(raw.strip())
    if not m:
        raise ValueError(f"нужно ЧЧ-ЧЧ, например 09-22, а не {raw!r}")
    start, end = int(m.group(1)), int(m.group(2))
    if not 0 <= start < end <= 24:
        raise ValueError(f"окно {raw!r}: нужно 0 <= начало < конец <= 24")
    return start, end


def in_window(moment: datetime, hours: tuple[int, int]) -> bool:
    start, end = hours
    return start <= moment.hour < end


def fork_sid(chat_key: str) -> str:
    """Постоянный id ответвления для темы: роутер памяти держит по id
    ответвления свою запись, и с постоянным id они не копятся. Два пинга
    одной темы не идут одновременно: оба под замком темы."""
    return str(uuid.uuid5(_FORK_NS, f"warm:{chat_key}"))


def compact_window_tokens(autocompact: str) -> int | None:
    """Окно автосжатия в токенах из значения CLAUDE_AUTOCOMPACT ("300k",
    "1m", "auto", ""). None: окна нет или оно выбирается CLI."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", autocompact.strip().lower())
    if not m:
        return None
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    return int(float(m.group(1)) * mult)


def near_compaction(expected: int, autocompact: str) -> bool:
    window = compact_window_tokens(autocompact)
    return window is not None and expected >= window * COMPACT_GUARD_SHARE


def healthy(first_read: int, first_write: int, expected: int) -> bool:
    """Пинг прочитал разговор, а не переписал: чтение первого запроса не
    меньше 90% ожидаемого разговора и запись не длиннее хвоста ответвления."""
    return (expected > 0 and first_read >= expected * READ_MIN_SHARE
            and first_write <= TAIL_MAX_TOKENS)

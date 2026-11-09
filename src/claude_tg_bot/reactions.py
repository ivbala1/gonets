"""Реакции Ивана на сообщения бота (решение Ивана 01.11.2026): 👍 сделано или
согласен, 👎 такое не присылать.

Telegram сообщает только номер сообщения, текста в событии нет, поэтому бот и
notify пишут свои отправки в личку в короткий журнал. Реакция ложится заметкой
и уходит в следующий ход лички: отдельный ран на каждую реакцию занимал бы
личку и стоил бы хода."""
from __future__ import annotations

import logging
from pathlib import Path

from . import jsonstore

log = logging.getLogger(__name__)

SENT_FILE = "sent_texts.jsonl"
MEANINGS = ("👍", "👎")
TEXT_LIMIT = 500
_TAIL_BYTES = 256 * 1024
# Подрезка до окна lookup: старше хвоста _TAIL_BYTES строки никто не читает
# (этап 2 уборки, 09.11.2026; до того 2 МБ -> 1 МБ при старте бота).
_MAX_BYTES = 2 * _TAIL_BYTES

_state_dir: Path | None = None


def configure(state_dir: Path) -> None:
    """Запомнить каталог состояния и подрезать журнал (раз за запуск бота)."""
    global _state_dir
    _state_dir = state_dir
    _trim(state_dir / SENT_FILE)


def _trim_file(path: Path) -> bool:
    """Подрезка до окна lookup под замком дописывания (notify, другой процесс,
    пишет сюда же). Нет файла: False."""
    try:
        return jsonstore.jsonl_trim(path, _MAX_BYTES, _MAX_BYTES // 2)
    except FileNotFoundError:
        return False


def trim(state_dir: Path) -> bool:
    """Подрезка из суточной уборки бота (retention.owner_passes): сбой
    пробрасывается в отчёт уборки."""
    return _trim_file(state_dir / SENT_FILE)


def _trim(path: Path) -> None:
    """Подрезка при старте: сбой только в журнал, старт не мешает."""
    try:
        _trim_file(path)
    except OSError as e:
        log.warning("sent log trim failed: %s", e)


def remember(chat_id: object, message_id: object, text: object, state_dir: Path | None = None) -> None:
    """Строка в журнал отправок. Только личка: у групп положительного номера нет.
    Сбой записи не мешает отправке."""
    base = state_dir or _state_dir
    if base is None or not isinstance(chat_id, int) or chat_id <= 0:
        return
    if not isinstance(message_id, int) or not isinstance(text, str) or not text.strip():
        return
    try:
        jsonstore.jsonl_append(base / SENT_FILE, {"c": chat_id, "m": message_id, "t": text[:TEXT_LIMIT]})
    except OSError as e:
        log.warning("sent log write failed: %s", e)


def remember_sent(response: object, text: object, state_dir: Path | None = None) -> None:
    """То же по ответу Bot API на send* (handoff, otklik, счёт): чат и номер
    из `result`. Сообщение уже ушло: никакой сбой здесь наружу не выходит,
    иначе зовущий счёл бы отправку неудачной и повторил её."""
    try:
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            return
        chat = result.get("chat")
        remember(chat.get("id") if isinstance(chat, dict) else None, result.get("message_id"), text, state_dir)
    except Exception:
        log.warning("sent log write failed", exc_info=True)


def lookup(state_dir: Path, chat_id: int, message_id: int) -> str:
    """Текст своего сообщения; пусто, если его нет в хвосте журнала."""
    for rec in reversed(jsonstore.jsonl_tail(state_dir / SENT_FILE, _TAIL_BYTES)):
        if rec.get("c") == chat_id and rec.get("m") == message_id:
            return str(rec.get("t") or "")
    return ""


def added_and_removed(old: tuple, new: tuple) -> tuple[list[str], list[str]]:
    """Значимые эмодзи, которые появились и которые сняли."""
    def emojis(reactions: tuple) -> set[str]:
        return {getattr(r, "emoji", "") for r in reactions} & set(MEANINGS)
    before, after = emojis(old), emojis(new)
    return sorted(after - before), sorted(before - after)

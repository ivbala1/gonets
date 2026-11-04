"""Реакции Ивана на сообщения бота (решение Ивана 01.11.2026): 👍 сделано или
согласен, 👎 такое не присылать.

Telegram сообщает только номер сообщения, текста в событии нет, поэтому бот и
notify пишут свои отправки в личку в короткий журнал. Реакция ложится заметкой
и уходит в следующий ход лички: отдельный ран на каждую реакцию занимал бы
личку и стоил бы хода."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SENT_FILE = "sent_texts.jsonl"
MEANINGS = ("👍", "👎")
TEXT_LIMIT = 500
_TAIL_BYTES = 256 * 1024
_MAX_BYTES = 2 * 1024 * 1024

_state_dir: Path | None = None


def configure(state_dir: Path) -> None:
    """Запомнить каталог состояния и подрезать журнал (раз за запуск бота)."""
    global _state_dir
    _state_dir = state_dir
    _trim(state_dir / SENT_FILE)


def _trim(path: Path) -> None:
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        with path.open("rb") as f:
            f.seek(-_MAX_BYTES // 2, os.SEEK_END)
            tail = f.read()
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(tail[tail.find(b"\n") + 1:])
        os.replace(tmp, path)
    except FileNotFoundError:
        pass
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
    line = json.dumps({"c": chat_id, "m": message_id, "t": text[:TEXT_LIMIT]}, ensure_ascii=False)
    try:
        fd = os.open(base / SENT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.warning("sent log write failed: %s", e)


def lookup(state_dir: Path, chat_id: int, message_id: int) -> str:
    """Текст своего сообщения; пусто, если его нет в хвосте журнала."""
    try:
        with (state_dir / SENT_FILE).open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _TAIL_BYTES))
            raw = f.read()
    except OSError:
        return ""
    for line in reversed(raw.splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:   # обрезанная первая строка хвоста
            continue
        if isinstance(rec, dict) and rec.get("c") == chat_id and rec.get("m") == message_id:
            return str(rec.get("t") or "")
    return ""


def added_and_removed(old: tuple, new: tuple) -> tuple[list[str], list[str]]:
    """Значимые эмодзи, которые появились и которые сняли."""
    def emojis(reactions: tuple) -> set[str]:
        return {getattr(r, "emoji", "") for r in reactions} & set(MEANINGS)
    before, after = emojis(old), emojis(new)
    return sorted(after - before), sorted(before - after)

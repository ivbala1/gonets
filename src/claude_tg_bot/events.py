"""Принятые пробуждения /event на диске (ревью Codex 22.10.2026).

Событие с wake, принятое ответом 200, раньше жило только в памяти: рестарт
бота между ответом и стартом рана терял его молча. Теперь до ответа оно
ложится файлом state/events/<id>.json со всеми полями запуска. Ран стартовал:
пометка started (до подачи промпта); кончился: done с исходом. После
рестарта бот будит то, что не стартовало и не просрочено, с пометкой
«получено ЧЧ:ММ, ждало N мин»; стартовавшее и не кончившееся не повторяет,
а пишет в чат, что исход неизвестен.

Ключ повтора: отправитель (слушатель, сторож) после неудачи шлёт то же
событие снова, а ответ 200 мог потеряться. Событие с уже известным ключом
бот второй раз не будит. Файл ключа держится KEEP_DONE после конца.

Писатель один: процесс бота. Файл создаётся атомарно и только если его нет
(os.link), иначе два одновременных запроса с одним ключом оба прошли бы.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

EVENTS_DIRNAME = "events"
DEFAULT_TTL_SEC = 2 * 3600
MAX_TTL_SEC = 24 * 3600
MIN_TTL_SEC = 60
KEEP_DONE_SEC = 48 * 3600
# Старше этого после рестарта событие не сбрасывает сессию: утреннее
# fresh днём оборвало бы дневной разговор.
FRESH_MAX_AGE_SEC = 600
KEY_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_ID_RE = re.compile(r"[0-9a-f]{24}")


def event_dir(state_dir: Path) -> Path:
    return state_dir / EVENTS_DIRNAME


def event_id(key: str | None) -> str:
    """Имя файла: от ключа повтора, если он есть, иначе случайное."""
    seed = key if key else uuid.uuid4().hex
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _path(base: Path, eid: str) -> Path:
    if not _ID_RE.fullmatch(eid):
        raise ValueError("bad event id")
    return base / f"{eid}.json"


def load(base: Path, eid: str) -> dict[str, Any] | None:
    try:
        rec = json.loads(_path(base, eid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write_tmp(base: Path, rec: dict[str, Any]) -> str:
    base.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return tmp


def reserve(base: Path, rec: dict[str, Any]) -> bool:
    """Положить принятое событие. False: файл с этим id уже есть (повтор по ключу)."""
    tmp = _write_tmp(base, rec)
    try:
        os.link(tmp, _path(base, rec["id"]))
    except FileExistsError:
        return False
    finally:
        Path(tmp).unlink(missing_ok=True)
    return True


def update(base: Path, eid: str, **fields: Any) -> dict[str, Any] | None:
    rec = load(base, eid)
    if rec is None:
        return None
    rec.update(fields)
    tmp = _write_tmp(base, rec)
    os.replace(tmp, _path(base, eid))
    return rec


def drop(base: Path, eid: str) -> None:
    """Событие не принято (Telegram не взял уведомление): ключ свободен для повтора."""
    _path(base, eid).unlink(missing_ok=True)


def fingerprint(chat_id: int, thread_id: int | None, untrusted: bool) -> str:
    """Куда и с какими правами принято: повтор с тем же ключом в другой чат
    или с другим доверием получает отказ (ревью Codex 22.10.2026). Текст в
    отпечаток не входит: отправитель пересобирает промпт с текущим временем."""
    return hashlib.sha256(f"{chat_id}\0{thread_id}\0{untrusted}".encode("utf-8")).hexdigest()[:16]


def new_record(*, key: str | None, chat_id: int, thread_id: int | None, prompt: str,
               fresh: bool, untrusted: bool, silent: bool, source: str, title: str,
               ttl: int | None, now: float | None = None, expires_at: float | None = None,
               receiving: bool = False, boot: str | None = None) -> dict[str, Any]:
    """receiving: уведомление в Telegram ещё уходит, ответа 200 не было;
    повтор с тем же ключом ждёт (503), обрыв здесь событие не принимает."""
    now = time.time() if now is None else now
    return {
        "id": event_id(key), "key": key, "chat_id": chat_id, "thread_id": thread_id,
        "prompt": prompt, "fresh": fresh, "untrusted": untrusted, "silent": silent,
        "source": source, "title": title, "accepted_at": now,
        "expires_at": expires_at if expires_at is not None else now + (ttl or DEFAULT_TTL_SEC),
        "status": "receiving" if receiving else "accepted", "boot": boot,
        "fp": fingerprint(chat_id, thread_id, untrusted),
    }


def expired(rec: dict[str, Any], now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        return float(rec["expires_at"]) <= now
    except (KeyError, TypeError, ValueError):
        return True


def startup(base: Path, now: float | None = None,
            boot: str | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """После рестарта: (будить, прерванные, просроченные). Конченные старше
    KEEP_DONE удаляются, битые файлы тоже (с ними нельзя знать права рана)."""
    now = time.time() if now is None else now
    wake, interrupted, stale = [], [], []
    if not base.is_dir():
        return wake, interrupted, stale
    for tmp in base.glob(".tmp-*.json"):
        # Обрыв между созданием и ссылкой: черновик записи, не событие.
        with contextlib.suppress(OSError):
            if now - tmp.stat().st_mtime > 3600:
                tmp.unlink()
    for path in sorted(base.glob("*.json")):
        if not _ID_RE.fullmatch(path.stem):
            continue
        rec = load(base, path.stem)
        if boot is not None and rec is not None and rec.get("boot") == boot:
            # Принято уже этим запуском бота (сервер открыт раньше восстановления).
            continue
        try:
            status = rec.get("status") if rec is not None else None
            if (rec is None or rec.get("id") != path.stem or not isinstance(rec.get("chat_id"), int)
                    or status == "receiving"):
                # Битая запись (права рана по ней не узнать) или приём, не
                # дошедший до ответа 200: события не было.
                path.unlink(missing_ok=True)
            elif status == "done":
                if now - float(rec.get("done_at") or rec.get("accepted_at") or 0) > KEEP_DONE_SEC:
                    path.unlink(missing_ok=True)
            elif status == "started":
                interrupted.append(rec)
            elif status != "accepted":
                path.unlink(missing_ok=True)
            elif expired(rec, now):
                stale.append(rec)
            else:
                float(rec.get("accepted_at"))
                wake.append(rec)
        except (TypeError, ValueError, OSError):
            # Одна испорченная запись не мешает остальным.
            path.unlink(missing_ok=True)
    return wake, interrupted, stale


def resume_flags(rec: dict[str, Any], now: float | None = None) -> tuple[bool, bool, bool]:
    """(fresh, untrusted, silent) для повторного пробуждения. Неясный флаг
    чужого текста значит чужой текст: полные права ему давать нельзя."""
    now = time.time() if now is None else now
    untrusted = rec.get("untrusted")
    untrusted = True if not isinstance(untrusted, bool) else untrusted
    age = now - float(rec.get("accepted_at") or 0)
    fresh = rec.get("fresh") is True and age < FRESH_MAX_AGE_SEC
    return fresh, untrusted, rec.get("silent") is True

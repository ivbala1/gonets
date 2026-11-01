"""Работа, оборванная аварией бота: `state/inflight.json` (аудит ранов, находка 4).

Запись на запрос Ивана (сообщение, ответ кнопкой, начатое `/at`) живёт от
приёма до конца обработки, с отправкой ответа. Прерванный остановкой ран сам
говорит «бот перезапускается, ран прерван» и снимает запись; остаток чистит
конец штатной остановки, а не её начало: умер посреди остановки, значит авария.
Осталось что-то при старте, значит процесс умер без штатной остановки (OOM,
SIGKILL, падение цикла): каждой записи одно сообщение «оборвано сбоем» ответом
на исходное. Запись снимается после подтверждённой доставки этого сообщения,
поэтому авария при старте весть не теряет, а может её повторить.
Без зависимостей.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from datetime import datetime
from collections.abc import Collection
from pathlib import Path

log = logging.getLogger(__name__)

FILE = "inflight.json"
HEAD_CHARS = 80
# Решения журнала, которые футер показывает отметками, и сколько его хвоста читать.
AUDIT_KINDS = {"allow:whitelist": "auto", "allow:memory-write": "memory"}
AUDIT_TAIL = 512 * 1024


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / FILE


def _load(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("inflight: unreadable %s, ignored", path)
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, dict]) -> None:
    if not data:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def head(prompt: str) -> str:
    """Начало запроса одной строкой, для вести."""
    line = " ".join(prompt.split())
    return line if len(line) <= HEAD_CHARS else line[:HEAD_CHARS - 1] + "…"


def add(state_dir: Path, *, chat_id: int, thread_id: int | None, message_id: int | None,
        prompt: str, kind: str) -> str | None:
    """Записать запрос; id записи или None, если записать не вышло (диск):
    учёт вспомогательный и запрос не останавливает."""
    rid = uuid.uuid4().hex[:8]
    path = _path(state_dir)
    try:
        data = _load(path)
        data[rid] = {"chat_id": chat_id, "thread_id": thread_id,
                     "message_id": message_id or None, "head": head(prompt), "kind": kind,
                     "ts": time.time()}
        _save(path, data)
    except OSError:
        log.exception("inflight: add failed")
        return None
    return rid


def remove(state_dir: Path, rid: str | None) -> None:
    if rid is None:
        return
    path = _path(state_dir)
    try:
        data = _load(path)
        if data.pop(rid, None) is not None:
            _save(path, data)
    except OSError:
        log.exception("inflight: remove failed")


def note_sid(state_dir: Path, rids: Collection[str | None], sid: str) -> None:
    """Сессия рана: по ней весть находит его следы в журнале (`footer_trail`)."""
    path = _path(state_dir)
    with contextlib.suppress(OSError):
        data = _load(path)
        for rid in rids:
            if rid in data:
                # С этого часа: запись, ждавшая замка, старше своего рана.
                data[rid].update(sid=sid, ts=time.time())
        _save(path, data)


def footer_trail(audit_path: Path, sid: str | None, since: float | None) -> list[tuple[str, str]]:
    """Отметки футера оборванного рана из state/audit.jsonl: при SIGKILL футера
    нет, а записи без карточки и подачи в память были. Виды как у
    approval.add_run_note. Журнал только дополняет весть: его сбой молчит."""
    out: list[tuple[str, str]] = []
    if not sid or since is None:
        return out
    with contextlib.suppress(OSError):
        with open(audit_path, "rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - AUDIT_TAIL))
            tail = f.read().decode("utf-8", "replace")
        for line in tail.splitlines():
            with contextlib.suppress(ValueError, KeyError, TypeError):
                ev = json.loads(line)
                kind, snippet = AUDIT_KINDS.get(ev["decision"]), ev["snippet"][-60:]
                if (kind and ev.get("sid") == sid and "--dry-run" not in ev["snippet"]
                        and datetime.fromisoformat(ev["ts"]).timestamp() >= int(since)):
                    out.append((kind, f"{ev['tool']} {snippet}" if kind == "auto" else snippet))
    return out


def clear(state_dir: Path, keep: Collection[str] = ()) -> None:
    """Конец штатной остановки: оборванное получило свою весть, сбоем оно не
    считается. keep: записи прошлой аварии, чья весть ещё не доставлена."""
    path = _path(state_dir)
    try:
        data = _load(path)
        _save(path, {rid: rec for rid, rec in data.items() if rid in keep})
    except OSError:
        log.exception("inflight: clear failed")


def load(state_dir: Path) -> dict[str, dict]:
    """При старте: записи прошлого процесса (их оборвала авария). Файл не
    меняется, запись снимает доставка вести (`remove`)."""
    return {rid: rec for rid, rec in _load(_path(state_dir)).items() if isinstance(rec, dict)}

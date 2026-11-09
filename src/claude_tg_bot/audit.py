"""Append-only журнал tool-approvals. Пишется строго JSONL, одна строка = одно событие.

Формат строки: {"ts": "...", "aid": "...", "tool": "...", "decision": "...", "snippet": "..."}
и, когда известны, "sid" (сессия), "run" (запуск процесса claude, runs.py) и
"plan" (aid карточки плана, по которой действие пропущено).

Дописывание через jsonstore.jsonl_append: одна запись под flock файла, так что
одновременные вызовы из тасков бота и процессов хука строк не перемешают.

Файлы месячные (с 09.11.2026, этап 1 уборки): вызывающие передают базовый путь
`state/audit.jsonl`, запись идёт в `state/audit-ГГГГ-ММ.jsonl` по месяцу
события; файл выбирает этот модуль, внешней ротации нет. Хранится KEEP_MONTHS
месяцев (`prune`, зовёт суточная уборка бота). Прежний общий файл один раз
разносится по месяцам (`split_legacy`). Читать через `files` и `tail`, не
открывать базовый путь напрямую.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, jsonstore

log = logging.getLogger(__name__)

SNIPPET_LIMIT = 500
# Журнал нужен разбору карточек и случаев (tools/replay_reads.py, разборы
# docs/audit-*): год истории, не меньше шести месяцев по решению 09.11.2026.
KEEP_MONTHS = 12
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def month_file(path: Path, when: datetime | None = None) -> Path:
    """Файл месяца для базового пути: state/audit.jsonl -> state/audit-2026-11.jsonl."""
    when = when or config.now_local()
    return path.with_name(f"{path.stem}-{when:%Y-%m}{path.suffix}")


def _month_of(path: Path, f: Path) -> tuple[int, int] | None:
    prefix, suffix = f"{path.stem}-", path.suffix
    if not (f.name.startswith(prefix) and f.name.endswith(suffix)):
        return None
    m = _MONTH_RE.match(f.name[len(prefix):len(f.name) - len(suffix)])
    return (int(m.group(1)), int(m.group(2))) if m else None


def month_files(path: Path) -> list[Path]:
    """Месячные файлы базового пути, от старых к новым (ссылки не берутся)."""
    try:
        found = [(m, f) for f in path.parent.iterdir()
                 if (m := _month_of(path, f)) and f.is_file() and not f.is_symlink()]
    except OSError:
        return []
    return [f for _m, f in sorted(found)]


def files(path: Path) -> list[Path]:
    """Всё, что читать: прежний общий файл (пока не разнесён), затем месяцы.
    Путь к одному месячному файлу даёт его самого."""
    legacy = [path] if path.is_file() else []
    return legacy + month_files(path)


def tail(path: Path, max_bytes: int) -> list[dict]:
    """Хвост журнала: по max_bytes с двух последних месяцев (событие рана на
    стыке месяцев не теряется) и прежнего файла, если он ещё есть."""
    out: list[dict] = []
    for f in ([path] if path.is_file() else []) + month_files(path)[-2:]:
        out.extend(jsonstore.jsonl_tail(f, max_bytes))
    return out


def _ts_month(line: bytes) -> str | None:
    try:
        ts = json.loads(line.decode("utf-8"))["ts"]
        datetime.fromisoformat(ts)
        return ts[:7] if _MONTH_RE.match(ts[:7]) else None
    except (ValueError, KeyError, TypeError):
        return None


def split_legacy(path: Path) -> int:
    """Разнести прежний общий файл по месячным (один раз, при первой уборке
    после выкатки). Строка без читаемого времени идёт в месяц соседней
    строки выше. Каждый месяц вставляется перед уже записанным в его файл
    (новые события там позже), затем прежний файл переписывается без него:
    обрыв посреди переноса месяц не удваивает (вставленный в начало файла
    месяца не вставляется снова). Писатель этого
    файла один, процесс бота (approval через мост), новый код в него не пишет.
    Возвращает число перенесённых строк."""
    if path.is_symlink() or not path.is_file():
        return 0
    # Две уборки разом (второй экземпляр бота) не вставят месяц дважды.
    with jsonstore.file_lock(path.with_name(path.name + ".split.lock")):
        if path.is_symlink() or not path.is_file():
            return 0
        return _split_locked(path)


def _split_locked(path: Path) -> int:
    lines = [ln for ln in path.read_bytes().split(b"\n") if ln.strip()]
    order: list[str] = []
    by_month: dict[str, list[bytes]] = {}
    current = None
    for ln in lines:
        current = _ts_month(ln) or current
        key = current or ""
        if key not in by_month:
            by_month[key] = []
            order.append(key)
        by_month[key].append(ln)
    if "" in by_month:
        # Битые строки до первой читаемой: в первый настоящий месяц.
        first = next((k for k in order if k), None)
        if first is None:
            first = datetime.fromtimestamp(path.stat().st_mtime, config.local_zone()).strftime("%Y-%m")
            by_month[first] = []
            order.append(first)
        by_month[first][:0] = by_month.pop("")
        order.remove("")
    moved = 0
    for i, key in enumerate(order):
        target = path.with_name(f"{path.stem}-{key}{path.suffix}")
        chunk = b"\n".join(by_month[key]) + b"\n"
        # Обрыв после вставки, но до перезаписи прежнего файла: месяц уже
        # стоит в начале своего файла, второй раз его не вставлять.
        try:
            with open(target, "rb") as f:
                done = f.read(len(chunk)) == chunk
        except FileNotFoundError:
            done = False
        if not done:
            jsonstore.jsonl_prepend(target, chunk)
        moved += len(by_month[key])
        rest = [ln for k in order[i + 1:] for ln in by_month[k]]
        if rest:
            jsonstore.write_text(path, b"\n".join(rest) + b"\n")
        else:
            path.unlink()
    return moved


def prune(path: Path, keep_months: int = KEEP_MONTHS, now: datetime | None = None) -> list[Path]:
    """Удалить месячные файлы старше keep_months месяцев (текущий считается)."""
    now = now or config.now_local()
    border = now.year * 12 + now.month - 1 - (keep_months - 1)
    gone = []
    for f in month_files(path):
        y, m = _month_of(path, f)
        if y * 12 + m - 1 < border:
            f.unlink(missing_ok=True)
            gone.append(f)
    return gone


def _snippet(tool_input: dict[str, Any]) -> str:
    if "chat_id" in tool_input:
        # Отправка MCP: адресат нужен замеру карточек (себе или нет), а не только путь файла.
        s = json.dumps(tool_input, ensure_ascii=False)
    elif "command" in tool_input:
        s = str(tool_input.get("command", ""))
    elif "file_path" in tool_input:
        s = str(tool_input.get("file_path", ""))
    elif "url" in tool_input:
        s = str(tool_input.get("url", ""))
    else:
        s = json.dumps(tool_input, ensure_ascii=False)
    return s[:SNIPPET_LIMIT]


def write_event(
    path: Path,
    *,
    approval_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    decision: str,
    session_id: str = "",
    run_id: str = "",
    plan: str = "",
    why: list[str] | None = None,
) -> None:
    event = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "aid": approval_id,
        "tool": tool_name,
        "decision": decision,
        "snippet": _snippet(tool_input),
    }
    for key, value in (("sid", session_id), ("run", run_id), ("plan", plan)):
        if value:
            event[key] = value
    if why:
        # Почему карточка: причины serious.py (замер tools/replay_reads.py).
        event["why"] = why
    try:
        # 0o600 (и старому файлу): snippet содержит tool-input (команды/пути/url),
        # который может включать секреты, журнал не должен быть world-readable.
        jsonstore.jsonl_append(month_file(path), event)
    except Exception:
        log.exception("audit log write failed (aid=%s)", approval_id)

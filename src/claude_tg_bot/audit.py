"""Append-only журнал tool-approvals. Пишется строго JSONL, одна строка = одно событие.

Формат строки: {"ts": "...", "aid": "...", "tool": "...", "decision": "...", "snippet": "..."}

append-write в < PIPE_BUF атомарно на POSIX при open("a"), так что concurrent
вызовы из разных async-тасков безопасны без locks.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SNIPPET_LIMIT = 500


def _snippet(tool_input: dict[str, Any]) -> str:
    if "command" in tool_input:
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
) -> None:
    line = json.dumps(
        {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "aid": approval_id,
            "tool": tool_name,
            "decision": decision,
            "snippet": _snippet(tool_input),
        },
        ensure_ascii=False,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600: snippet содержит tool-input (команды/пути/url), который может
        # включать секреты — не оставляем журнал world-readable. O_CREAT mode
        # действует только на новый файл, поэтому fchmod чинит и ранее созданный.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(line + "\n")
    except Exception:
        log.exception("audit log write failed (aid=%s)", approval_id)

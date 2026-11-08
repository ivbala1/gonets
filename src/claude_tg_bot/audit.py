"""Append-only журнал tool-approvals. Пишется строго JSONL, одна строка = одно событие.

Формат строки: {"ts": "...", "aid": "...", "tool": "...", "decision": "...", "snippet": "..."}
и, когда известны, "sid" (сессия), "run" (запуск процесса claude, runs.py) и
"plan" (aid карточки плана, по которой действие пропущено).

Дописывание через jsonstore.jsonl_append: одна запись под flock файла, так что
одновременные вызовы из тасков бота и процессов хука строк не перемешают.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from . import jsonstore

log = logging.getLogger(__name__)

SNIPPET_LIMIT = 500


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
        jsonstore.jsonl_append(path, event)
    except Exception:
        log.exception("audit log write failed (aid=%s)", approval_id)

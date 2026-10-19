"""Работа с локальными транскриптами Claude Code (~/.claude/projects/<slug>/).

Для /export (сессия → Markdown), /resume (браузер последних сессий) и
/handback (перенос сессии в терминал). Только чтение: транскрипты пишет сам
Claude CLI.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .i18n import _

log = logging.getLogger(__name__)

# Сколько первых строк jsonl читать для превью сессии: первое user-сообщение
# почти всегда в самом начале, весь файл (мегабайты) читать незачем.
_PREVIEW_SCAN_LINES = 80


def project_dir(cwd: Path) -> Path:
    """Каталог транскриптов для рабочего каталога cwd.

    Claude Code превращает путь в slug заменой каждого не-alphanumeric символа
    на дефис: /home/user/src → -home-user-src.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / slug


def _content_text(content: object) -> str:
    """Текст из message.content: строка или список блоков."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                name = block.get("name", "?")
                parts.append(f"[tool: {name}]")
    return "\n".join(p for p in parts if p)


@dataclass
class SessionInfo:
    session_id: str
    mtime: float
    preview: str  # первые слова первого user-сообщения

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.mtime).strftime("%d.%m %H:%M")


def _session_preview(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _PREVIEW_SCAN_LINES:
                    break
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(evt, dict) or evt.get("type") != "user":
                    continue
                if evt.get("isMeta"):
                    continue
                msg = evt.get("message") or {}
                text = _content_text(msg.get("content")).strip()
                # Служебные обвязки (persona-блоки, command-output) пропускаем.
                if not text or text.startswith(("<", "[Пользователь")):
                    continue
                return " ".join(text.split())[:80]
    except OSError:
        pass
    return _("(без превью)")


def list_sessions(cwd: Path, limit: int = 5) -> list[SessionInfo]:
    """Последние сессии проекта cwd, новые сверху."""
    pdir = project_dir(cwd)
    if not pdir.is_dir():
        return []
    files = sorted(
        (p for p in pdir.glob("*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out: list[SessionInfo] = []
    for p in files[:limit]:
        out.append(SessionInfo(
            session_id=p.stem,
            mtime=p.stat().st_mtime,
            preview=_session_preview(p),
        ))
    return out


def export_markdown(cwd: Path, session_id: str) -> str | None:
    """Транскрипт сессии как Markdown. None, если файла нет."""
    path = project_dir(cwd) / f"{session_id}.jsonl"
    if not path.is_file():
        return None
    lines: list[str] = [_("# Сессия {session}").format(session=session_id[:8]), ""]
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(evt, dict):
                    continue
                etype = evt.get("type")
                if etype not in ("user", "assistant") or evt.get("isMeta"):
                    continue
                msg = evt.get("message") or {}
                text = _content_text(msg.get("content")).strip()
                if not text:
                    continue
                # tool_result-обвязку (ответы инструментов через user-события)
                # не включаем: экспорт — про разговор, не про полный сырой лог.
                if etype == "user" and isinstance(msg.get("content"), list):
                    if any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in msg["content"]
                    ):
                        continue
                who = _("Владелец") if etype == "user" else "Claude"
                lines.append(f"## {who}")
                lines.append("")
                lines.append(text)
                lines.append("")
    except OSError as e:
        log.warning("export failed for %s: %s", path, e)
        return None
    return "\n".join(lines)

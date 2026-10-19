"""Ран по чужому тексту (/event untrusted): списки замков и независимая проверка.

Набор инструментов у такого рана тот же, что у обычного: другой набор меняет
начало запроса, и весь разговор заново пишется в кэш (28 из 31 пробуждения
сторожа за 08-19.10.2026 по 1,1 $). Поэтому инструменты не убираются, а
закрываются, и у каждого два замка:

- хук бота (approval._untrusted_tool_allowed) отклоняет всё, кроме MCP на
  чтение; его matcher видит Bash, Write, Edit, WebFetch, MCP;
- этот модуль, запущенный хуком на все инструменты (matcher "*") без
  обращения к серверу бота: пропускает только чтение (UNTRUSTED_READ_TOOLS и
  MCP на чтение по APPROVE_MCP_AUTO_ALLOW_REGEX у UNTRUSTED_MCP_READ_SERVERS),
  остальному отказ. Отказ хука сильнее allow другого хука; у обоих хуков
  onFailure block, сбой и таймаут тоже отказ;
- для перечисленных в UNTRUSTED_ASK_TOOLS ещё режим dontAsk с правилами
  permissions.ask: отказ даже при allow обоих хуков.

permissions.deny и --tools меняют набор и ломают кэш; Grep и Glob в
--allowedTools добавляют в набор инструменты, которых у обычного рана нет
(всё проверено на Opus 19.10.2026).

Как хук: python -m claude_tg_bot.untrusted, JSON PreToolUse на stdin.
"""
from __future__ import annotations

import json
import os
import re
import sys

from dotenv import dotenv_values

from .config import BOT_ENV_FILE, DEFAULT_MCP_AUTO_ALLOW_REGEX
from .i18n import _

_MCP_RE_ENV = "APPROVE_MCP_AUTO_ALLOW_REGEX"

UNTRUSTED_ALLOWED_TOOLS = ("Read", "ToolSearch")
# Встроенные инструменты, которые только читают.
UNTRUSTED_READ_TOOLS = (
    "Read", "ToolSearch", "Grep", "Glob", "ListMcpResourcesTool", "ReadMcpResourceTool",
    "ReadMcpResourceDirTool", "CronList", "ListAgents",
)
_ASK_BUILTIN = (
    "Bash", "Monitor", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
    # Субагенты, публикация страниц на claude.ai и отложенный перезапуск: мимо
    # хука бота, а ран по чужому тексту ничего из этого не требует.
    "Task", "Agent", "Artifact", "ArtifactComments", "ArtifactData", "DesignSync", "ScheduleWakeup",
    # Планировщики, удалённые запуски, сообщения другим сессиям и скиллы тоже:
    # ран по чужому тексту не должен ничего заводить на потом.
    "CronCreate", "CronDelete", "RemoteTrigger", "PushNotification", "SendMessage",
    "Skill", "Workflow", "TaskStop", "EnterWorktree", "ExitWorktree", "ExitPlanMode",
    "ReportFindings",
)
# MCP, которые ран по чужому тексту не читает, закрыты целиком (ask на сервер
# закрывает и чтение); у Telegram, Gmail и календаря поимённо те, что
# действуют, чтение остаётся (список 19.10.2026).
_ASK_MCP_SERVERS = (
    "chrome-devtools", "claude_ai_Claude_Docs", "claude_ai_Develop21", "claude_ai_Google_Docs",
    "claude_ai_Google_Drive", "claude_ai_Google_Sheets", "claude_ai_Slack", "plugin_pdf-viewer_pdf",
)
_ASK_MCP_TOOLS = {
    "telegram": (
        "send_message", "send_file", "reply_to_message", "export_chat_invite", "export_contacts",
        "get_invite_link", "download_media",
    ),
    "claude_ai_Gmail": (
        "send_message", "reply", "forward", "create_draft", "update_draft", "delete_draft",
        "create_label", "update_label", "delete_label", "label_message", "label_thread",
        "unlabel_message", "unlabel_thread", "update_message_labels", "apply_sensitive_message_label",
        "apply_sensitive_thread_label", "mark_message_spam", "mark_thread_spam",
        "unmark_message_spam", "unmark_thread_spam", "trash_message", "trash_thread",
        "untrash_message", "untrash_thread",
    ),
    "claude_ai_Google_Calendar": ("create_event", "update_event", "delete_event", "respond_to_event"),
}
# MCP, чьё чтение нужно совету: только у них независимая проверка пропускает
# чтение по имени. Прочие серверы закрыты по умолчанию: инструмент вроде
# query(sql=...) по имени не отличить от записи (ревью Codex 19.10.2026).
UNTRUSTED_MCP_READ_SERVERS = ("telegram", "claude_ai_Gmail", "claude_ai_Google_Calendar")
UNTRUSTED_ASK_TOOLS = (
    *_ASK_BUILTIN,
    *(f"mcp__{server}" for server in _ASK_MCP_SERVERS),
    *(f"mcp__{server}__{tool}" for server, tools in _ASK_MCP_TOOLS.items() for tool in tools),
)


def unknown_tools(tools: object) -> list[str]:
    """Встроенные инструменты из события init без замка ask и не на чтение.
    Неверный список (не непустой список строк) тоже повод остановить ран."""
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
        return [_("(список инструментов не пришёл)")]
    known = set(UNTRUSTED_READ_TOOLS) | set(UNTRUSTED_ASK_TOOLS)
    return sorted({t for t in tools if not t.startswith("mcp__") and t not in known})


def tool_allowed(tool_name: str, mcp_re: re.Pattern[str] | None) -> bool:
    """Решение независимой проверки: только чтение."""
    if tool_name in UNTRUSTED_READ_TOOLS:
        return True
    parts = tool_name.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp" or mcp_re is None or parts[1] not in UNTRUSTED_MCP_READ_SERVERS:
        return False
    if tool_name in UNTRUSTED_ASK_TOOLS or f"mcp__{parts[1]}" in UNTRUSTED_ASK_TOOLS:
        return False
    return bool(mcp_re.fullmatch(parts[2]))


def _mcp_re() -> re.Pattern[str] | None:
    """Та же настройка, что у хука бота: окружение, иначе .env бота."""
    raw = os.environ.get(_MCP_RE_ENV)
    if raw is None:
        raw = dotenv_values(BOT_ENV_FILE).get(_MCP_RE_ENV)
    raw = (DEFAULT_MCP_AUTO_ALLOW_REGEX if raw is None else raw).strip()
    return re.compile(raw) if raw else None


def main() -> int:
    """Хук PreToolUse: чтение без решения (решают хук бота и режим), прочему
    отказ. Любой сбой тоже отказ."""
    try:
        tool_name = str(json.load(sys.stdin).get("tool_name") or "")
        if tool_name and tool_allowed(tool_name, _mcp_re()):
            return 0
        reason = _("ран по чужому тексту: разрешено только чтение")
    except Exception as e:  # отказ при любом сбое
        reason = _("сбой проверки рана по чужому тексту: {error}").format(error=e)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""PreToolUse-хук для Claude Code. Читает JSON со stdin, спрашивает approval
в Telegram через HTTP-endpoint нашего бота, возвращает решение в Claude
через JSON на stdout.

Формат stdin (от Claude Code, PreToolUse):
    {
      "session_id": "...",
      "hook_event_name": "PreToolUse",
      "tool_name": "Bash",
      "tool_input": {"command": "...", "description": "..."}
    }

Формат stdout (для Claude Code):
    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                             "permissionDecision": "allow" | "deny"}}
"""
from __future__ import annotations

import json
import os
import sys

import httpx

from .config import load_config
from .skills._oneshot import SCHEDULED_ENV


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        print(f"hook_approve: failed to read stdin: {e}", file=sys.stderr)
        return 2

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or ""
    if not tool_name:
        print("hook_approve: no tool_name", file=sys.stderr)
        return 2

    # skip_hook_validation=True: мы уже внутри hook-процесса. Повторная
    # валидация бесполезна и RuntimeError здесь сломал бы approve-протокол
    # (хук вернул бы трейсбэк вместо JSON-решения).
    # Fail-closed: uncaught RuntimeError (опечатка в CODEX_SANDBOX и т.п. в
    # .env) дал бы exit 1, а для PreToolUse любой код кроме 2 — это
    # «non-blocking error»: tool call ПРОДОЛЖИЛСЯ бы под bypassPermissions
    # без approve. Печатаем deny-JSON, как в HTTP-ветке ниже.
    try:
        cfg = load_config(skip_hook_validation=True)
    except Exception as e:
        print(f"hook_approve: config error: {e}", file=sys.stderr)
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"hook config error: {e}",
            }
        }
        print(json.dumps(result))
        return 0
    # IPv6-адреса (например `::1`) в URL должны быть в квадратных скобках,
    # иначе httpx ругается InvalidURL: `http://::1:8788/approve` парсится
    # криво. Hostname без `:` (DNS-имя или IPv4) проходит как есть.
    host = cfg.approve_http_host
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"
    url = f"http://{host}:{cfg.approve_http_port}/approve"
    # permission_mode нужен серверу, чтобы отличить plan-ран от фонового;
    # scheduled помечает oneshot-ран скилла, чью сессию бот не знает.
    body = {
        "tool_name": tool_name, "tool_input": tool_input, "session_id": session_id,
        "permission_mode": payload.get("permission_mode") or "",
        "scheduled": os.environ.get(SCHEDULED_ENV) == "1",
        # Каталог агента: от него danger оценивает относительные пути
        # (ревью 11.10.2026).
        "cwd": payload.get("cwd") or "",
    }
    headers = {}
    if cfg.approve_shared_secret:
        headers["X-Approve-Token"] = cfg.approve_shared_secret

    # timeout с запасом к серверному: +30s на сеть и обработку
    timeout = cfg.approve_timeout_sec + 30
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        # Бот недоступен — безопаснее отказать.
        print(f"hook_approve: HTTP error: {e}", file=sys.stderr)
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"approval bridge unreachable: {e}",
            }
        }
        print(json.dumps(result))
        return 0

    decision = data.get("decision") or "deny"
    reason = data.get("reason") or ""
    if decision not in ("allow", "deny", "ask"):
        decision = "deny"

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # последний рубеж: любой неожиданный сбой = deny
        print(f"hook_approve: unexpected error: {e}", file=sys.stderr)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"hook crashed: {e}",
            }
        }))
        sys.exit(0)

#!/usr/bin/python3
"""Сообщение в Telegram, что бот упал и systemd его больше не поднимает.

Запускается юнитом claude-tg-bot-down.service из OnFailure= основного юнита.
Нарочно без пакета claude_tg_bot и без его venv: упасть мог как раз новый код
или окружение. Только стандартная библиотека системного Python. Токен и чат
берутся из .env рядом; в сообщение не идёт ничего из журнала.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip().removeprefix("export ").strip()] = value
    return env


def unit_failed() -> bool:
    """Юнит правда в failed: страховка от вызова при штатном перезапуске."""
    try:
        r = subprocess.run(["systemctl", "--user", "is-failed", "--quiet", "claude-tg-bot.service"],
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    return r.returncode == 0


def main() -> int:
    if "--test" not in sys.argv and not unit_failed():
        print("bot-down-notify: юнит не в failed, сообщение не нужно", file=sys.stderr)
        return 0
    env = read_env(REPO / ".env")
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    users = [int(x) for x in env.get("TELEGRAM_ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x]
    if not token or not users:
        print("bot-down-notify: нет токена или пользователей в .env", file=sys.stderr)
        return 2
    try:
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    if env.get("CLAUDE_TG_BOT_LANG", "").strip().lower() == "ru":
        text = (
            ("Проверка сообщения о падении, бот на самом деле работает.\n" if "--test" in sys.argv else "")
            + "🔴 Бот claude-tg-bot упал и systemd его больше не поднимает"
            + (f" (версия {head})" if head else "") + ".\n"
            "Из Telegram его не починить: по ssh смотреть "
            "journalctl --user -u claude-tg-bot -n 50, затем systemctl --user restart claude-tg-bot."
        )
    else:
        text = (
            ("Test of the crash message, the bot is actually running.\n" if "--test" in sys.argv else "")
            + "🔴 The bot is down and systemd no longer restarts it"
            + (f" (version {head})" if head else "") + ".\n"
            "It cannot be fixed from Telegram: over ssh check "
            "journalctl --user -u claude-tg-bot -n 50, then systemctl --user restart claude-tg-bot."
        )
    body = json.dumps({"chat_id": min(users), "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = json.load(r).get("ok")
    except Exception as e:
        # Текст исключения может содержать URL с токеном: печатаем только тип.
        print(f"bot-down-notify: отправка не удалась: {type(e).__name__}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

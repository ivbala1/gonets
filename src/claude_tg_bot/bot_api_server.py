"""Запуск своего сервера telegram-bot-api на ПК (служба telegram-bot-api.service).

Ключи приложения Telegram (TELEGRAM_API_ID, TELEGRAM_API_HASH) берутся из .env
бота, тот же источник, что у входа MTProto (bigfile.py): второй копии ключей
нет. Сервер читает их из окружения, в командной строке (её видит `ps`) их нет.

Сервер слушает только 127.0.0.1:8081, режим `--local`:
файлы до 2 ГБ, getFile отдаёт путь на диске. Каталог работы с файлами ботов
`~/.local/share/telegram-bot-api`, только владельцу: в
именах подкаталогов токены ботов. Бинарь `~/.local/bin/telegram-bot-api`,
собирается из `~/.local/src/telegram-bot-api`, обновляет его update-tools.sh
Свода. BACKLOG, «Намерение (07.11.2026)».
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from . import paths

BINARY = Path.home() / ".local" / "bin" / "telegram-bot-api"
WORK_DIR = Path.home() / ".local" / "share" / "telegram-bot-api"
TEMP_DIR = WORK_DIR / "tmp"
PORT = 8081


def command(env: dict[str, str | None]) -> tuple[list[str], dict[str, str]] | str:
    """Команда и добавка к окружению; строка вместо них: чего не хватает."""
    api_id = (env.get("TELEGRAM_API_ID") or "").strip()
    api_hash = (env.get("TELEGRAM_API_HASH") or "").strip()
    if not api_id or not api_hash:
        return "нет TELEGRAM_API_ID или TELEGRAM_API_HASH в .env бота"
    # Сервер принимает только положительное 32-битное число, иначе код 1 и
    # бесконечный перезапуск службы; код 2 перезапуск останавливает.
    if not (api_id.isascii() and api_id.isdigit() and 0 < int(api_id) < 2**31):
        return "TELEGRAM_API_ID не положительное число"
    argv = [str(BINARY), "--local", "--http-ip-address=127.0.0.1", f"--http-port={PORT}",
            f"--dir={WORK_DIR}", f"--temp-dir={TEMP_DIR}", "--verbosity=1",
            # Буфер журнала в памяти сервер при падении выгружает в журнал
            # systemd, а в строках уровня WARNING бывают токены ботов.
            "--memory-verbosity=1"]
    return argv, {"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": api_hash}


def main() -> int:
    got = command(dotenv_values(paths.ENV_FILE))
    if isinstance(got, str):
        print(f"bot_api_server: {got}", file=sys.stderr)
        return 2
    argv, extra = got
    if not os.access(argv[0], os.X_OK):
        print(f"bot_api_server: нет {argv[0]}, собрать: README, «Свой сервер Bot API на ПК»", file=sys.stderr)
        return 2
    for d in (WORK_DIR, TEMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    os.environ.update(extra)
    os.execv(argv[0], argv)
    return 0  # не достигается


if __name__ == "__main__":
    raise SystemExit(main())

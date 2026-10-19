from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

from .config import load_config


def _check_state_dir_writable(state_dir: Path) -> None:
    """Non-destructive probe: создать директорию если нет, mkstemp + unlink.

    Раньше использовалось фиксированное имя `.writetest` через touch()+unlink() —
    мини-риск удалить настоящий файл с тем же именем у пользователя. mkstemp
    гарантирует уникальное имя в каждом запуске.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writetest-", dir=state_dir)
    os.close(fd)
    Path(name).unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-tg-bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="load config and exit (skeleton smoke-test)",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx INFO логирует полный URL запроса, включая Telegram bot token
    # (`https://api.telegram.org/bot<TOKEN>/...`). В systemd journal это
    # пишется в plain-text и расходится через journalctl-export. Поднимаем
    # его до WARNING — теряем низкоуровневые HTTP-логи, но защищаем токен.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("claude_tg_bot")

    if args.check:
        try:
            _check_state_dir_writable(cfg.state_dir)
        except OSError as e:
            log.error("state_dir not writable: %s (%s)", cfg.state_dir, e)
            return 2
        log.info("config ok: allowed_users=%s state_dir=%s claude_bin=%s",
                 sorted(cfg.allowed_user_ids), cfg.state_dir, cfg.claude_bin)
        return 0

    from .bot import run
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())

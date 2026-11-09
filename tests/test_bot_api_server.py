"""Запускалка своего сервера Bot API: ключи из .env только в окружение,
слушать только 127.0.0.1."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import bot_api_server  # noqa: E402


class CommandTest(unittest.TestCase):
    def test_keys_in_env_not_argv(self):
        argv, extra = bot_api_server.command({"TELEGRAM_API_ID": " 12345 ", "TELEGRAM_API_HASH": "abcdef"})
        self.assertEqual(extra, {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "abcdef"})
        self.assertNotIn("abcdef", " ".join(argv))
        self.assertNotIn("12345", " ".join(argv))
        self.assertIn("--http-ip-address=127.0.0.1", argv)
        self.assertIn("--local", argv)

    def test_missing_or_bad_keys(self):
        self.assertIsInstance(bot_api_server.command({}), str)
        self.assertIsInstance(bot_api_server.command({"TELEGRAM_API_ID": "x", "TELEGRAM_API_HASH": "h"}), str)
        for bad in ("0", "2147483648", "-5", "²", "٣"):
            self.assertIsInstance(bot_api_server.command({"TELEGRAM_API_ID": bad, "TELEGRAM_API_HASH": "h"}), str)


class MainTest(unittest.TestCase):
    def test_dirs_700_and_exec_with_keys_in_env(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "w"
            binary = Path(d) / "telegram-bot-api"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            env_file = Path(d) / ".env"
            env_file.write_text("TELEGRAM_API_ID=5\nTELEGRAM_API_HASH=hh\n")
            calls = []
            with patch.object(bot_api_server, "WORK_DIR", work), \
                    patch.object(bot_api_server, "TEMP_DIR", work / "tmp"), \
                    patch.object(bot_api_server, "BINARY", binary), \
                    patch.object(bot_api_server.paths, "ENV_FILE", env_file), \
                    patch.dict(os.environ, {}, clear=False), \
                    patch.object(bot_api_server.os, "execv", lambda p, a: calls.append((p, a, dict(os.environ)))):
                bot_api_server.main()
            self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((work / "tmp").stat().st_mode), 0o700)
            path, argv, env = calls[0]
            self.assertEqual(path, str(binary))
            self.assertEqual((env["TELEGRAM_API_ID"], env["TELEGRAM_API_HASH"]), ("5", "hh"))
            self.assertNotIn("hh", " ".join(argv))

    def test_no_binary_is_code_2(self):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_text("TELEGRAM_API_ID=5\nTELEGRAM_API_HASH=hh\n")
            with patch.object(bot_api_server, "BINARY", Path(d) / "nope"), \
                    patch.object(bot_api_server.paths, "ENV_FILE", env_file):
                self.assertEqual(bot_api_server.main(), 2)


if __name__ == "__main__":
    unittest.main()

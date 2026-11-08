"""Карточка: кнопка «Полностью», таймаут хука подтверждения и версия CLI
при старте бота."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot.approval import (  # noqa: E402
    _format_tool_call,
    needs_full_view,
)


class FullViewTests(unittest.TestCase):
    def test_button_when_escaping_hides_the_tail(self) -> None:
        # b3.5: 600 знаков `<` после экранирования занимают всю карточку,
        # хвост `; rm` пропадал, а кнопки «Полностью» не было.
        tool_input = {"command": "echo '" + "<" * 600 + "'; rm -rf /tmp/victim"}
        self.assertNotIn("rm -rf", _format_tool_call("Bash", tool_input, None))
        self.assertTrue(needs_full_view(tool_input, "Bash"))
        self.assertTrue(needs_full_view(tool_input))

    def test_button_when_description_is_cut(self) -> None:
        tool_input = {"command": "ls", "description": "п" * 300}
        self.assertTrue(needs_full_view(tool_input, "Bash"))

    def test_no_button_for_short_card(self) -> None:
        self.assertFalse(needs_full_view({"command": "ls -la", "description": "список"}, "Bash"))


class HookTimeoutTests(unittest.TestCase):
    def _cap(self, approve_timeout: int, hook_timeout: int) -> tuple[int, str]:
        from claude_tg_bot.config import HOOK_APPROVE_MODULE, _cap_approve_timeout
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": f"python -P -m {HOOK_APPROVE_MODULE}",
                 "timeout": hook_timeout}]}]}}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                capped = _cap_approve_timeout(path, approve_timeout)
        return capped, err.getvalue()

    def test_wait_is_capped_below_hook_timeout(self) -> None:
        # B2: бот отвечает раньше, чем CLI снимет хук: ожидание урезается до
        # timeout хука минус 60 с (решение владельца 12.10.2026).
        capped, err = self._cap(1800, 960)
        self.assertEqual(capped, 900)
        self.assertIn("APPROVE_TIMEOUT_SEC=1800", err)
        self.assertEqual(self._cap(900, 960), (900, ""))
        self.assertEqual(self._cap(600, 960), (600, ""))

    def test_hook_process_gets_capped_wait_too(self) -> None:
        # Процесс хука грузит настройки без проверки, но ждёт урезанное
        # значение; наименьший срок из хуков, булев игнорируется.
        from claude_tg_bot.config import HOOK_APPROVE_MODULE, load_config
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            hooks = [{"type": "command", "command": f"python -P -m {HOOK_APPROVE_MODULE}", "timeout": t}
                     for t in (960, 600, True)]
            path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}}))
            from claude_tg_bot import config as cfgmod
            self.assertEqual(cfgmod._cap_approve_timeout(path, 1800, warn=False), 540)
            env = {"TELEGRAM_BOT_TOKEN": "1:test", "TELEGRAM_ALLOWED_USER_IDS": "1",
                   "CLAUDE_BIN": "/usr/bin/true", "CLAUDE_HOOK_SETTINGS": str(path),
                   "APPROVE_TIMEOUT_SEC": "1800"}
            with mock.patch.object(cfgmod, "load_dotenv"), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(load_config(skip_hook_validation=True).approve_timeout_sec, 540)

    def _settings(self, d: str, timeout: object) -> Path:
        from claude_tg_bot.config import HOOK_APPROVE_MODULE
        path = Path(d) / "settings.json"
        path.write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "Bash|Monitor|Write|Edit|MultiEdit|NotebookEdit|WebFetch|mcp__.*",
            "hooks": [{"type": "command", "command": f"python -P -m {HOOK_APPROVE_MODULE}",
                       "timeout": timeout, "onFailure": "block"}]}]}}))
        return path

    def _env(self, path: Path) -> dict[str, str]:
        return {"TELEGRAM_BOT_TOKEN": "1:test", "TELEGRAM_ALLOWED_USER_IDS": "1",
                "CLAUDE_BIN": "/usr/bin/true", "CLAUDE_HOOK_SETTINGS": str(path),
                "APPROVE_TIMEOUT_SEC": "900"}

    def test_short_hook_timeout_refuses_start(self) -> None:
        # B2: при timeout хука 60 с и меньше бот не стартует, иначе карточка
        # ждала бы клика дольше, чем CLI держит хук (решение владельца 14.10.2026).
        from claude_tg_bot import config as cfgmod
        for timeout in (60, 60.0, 30, 0, -5):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as d:
                path = self._settings(d, timeout)
                with self.assertRaises(RuntimeError) as cm:
                    cfgmod._validate_hook_settings(path)
                self.assertIn("нужно больше 60 с", str(cm.exception))
                with mock.patch.object(cfgmod, "load_dotenv"), \
                        mock.patch.dict(os.environ, self._env(path), clear=True), \
                        self.assertRaises(RuntimeError):
                    cfgmod.load_config()
        with tempfile.TemporaryDirectory() as d:
            cfgmod._validate_hook_settings(self._settings(d, 61))

    def test_hook_process_wait_never_below_one_second(self) -> None:
        # Процесс хука настройки не проверяет: короткий timeout даёт 1 с
        # ожидания, а не ноль или минус (решение владельца 14.10.2026).
        from claude_tg_bot import config as cfgmod
        for timeout in (61, 60, 30, 0, -5):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as d:
                path = self._settings(d, timeout)
                self.assertEqual(cfgmod._cap_approve_timeout(path, 900, warn=False), 1)
                with mock.patch.object(cfgmod, "load_dotenv"), \
                        mock.patch.dict(os.environ, self._env(path), clear=True):
                    self.assertEqual(
                        cfgmod.load_config(skip_hook_validation=True).approve_timeout_sec, 1)


class ClaudeVersionTests(unittest.TestCase):
    """onFailure понимает Claude Code 2.1.295 и новее: со старым CLI, без
    CLI или с непонятной версией бот не стартует (ревью Codex 19.10.2026)."""

    @staticmethod
    def _env(d: str, claude: Path) -> dict[str, str]:
        from claude_tg_bot.config import HOOK_APPROVE_MODULE
        path = Path(d) / "settings.json"
        path.write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "Bash|Monitor|Write|Edit|MultiEdit|NotebookEdit|WebFetch|mcp__.*",
            "hooks": [{"type": "command", "command": f"python -P -m {HOOK_APPROVE_MODULE}",
                       "timeout": 960, "onFailure": "block"}]}]}}))
        return {"TELEGRAM_BOT_TOKEN": "1:test", "TELEGRAM_ALLOWED_USER_IDS": "1",
                "CLAUDE_BIN": str(claude), "CLAUDE_HOOK_SETTINGS": str(path)}

    def _load(self, d: str, version_output: str | None) -> None:
        from claude_tg_bot import config as cfgmod
        claude = Path(d) / "claude"
        if version_output is not None:
            claude.write_text(f"#!/bin/sh\necho '{version_output}'\n")
            claude.chmod(0o755)
        with mock.patch.object(cfgmod, "load_dotenv"), \
                mock.patch.dict(os.environ, self._env(d, claude), clear=True):
            cfgmod.load_config()

    def test_new_cli_starts(self) -> None:
        for out in ("2.1.295 (Claude Code)", "2.1.303 (Claude Code)", "2.2.0", "3.0.1"):
            with self.subTest(out=out), tempfile.TemporaryDirectory() as d:
                self._load(d, out)

    def test_old_missing_or_unknown_cli_refuses_start(self) -> None:
        for out in ("2.1.294 (Claude Code)", "1.9.999", "", "Claude Code", None):
            with self.subTest(out=out), tempfile.TemporaryDirectory() as d, \
                    self.assertRaises(RuntimeError) as cm:
                self._load(d, out)
            self.assertIn("2.1.295", str(cm.exception))

    def test_hook_process_skips_version_check(self) -> None:
        # Процесс хука и прочие утилиты грузят настройки без проверки: CLI
        # для них не запускается.
        from claude_tg_bot import config as cfgmod
        with mock.patch.object(cfgmod, "_validate_claude_version") as check, \
                tempfile.TemporaryDirectory() as d, mock.patch.object(cfgmod, "load_dotenv"), \
                mock.patch.dict(os.environ, self._env(d, Path("/usr/bin/true")), clear=True):
            cfgmod.load_config(skip_hook_validation=True)
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""APPROVE_AUTO_CHAT_KEYS: разбор из окружения и решение по чату."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import approval  # noqa: E402
from claude_tg_bot.approval import _chat_auto_allowed  # noqa: E402
from claude_tg_bot.burst import burst_target_key  # noqa: E402

KEYS = frozenset({"123456789", "-100:7"})


class ChatAutoAllowedTests(unittest.TestCase):
    def test_registered_private_chat_allowed(self):
        target = (123456789, None)
        self.assertTrue(_chat_auto_allowed(target, burst_target_key(*target), KEYS))

    def test_registered_topic_allowed_only_for_that_topic(self):
        self.assertTrue(_chat_auto_allowed((-100, 7), burst_target_key(-100, 7), KEYS))
        self.assertFalse(_chat_auto_allowed((-100, 8), burst_target_key(-100, 8), KEYS))
        self.assertFalse(_chat_auto_allowed((-100, None), burst_target_key(-100, None), KEYS))

    def test_other_chat_not_allowed(self):
        self.assertFalse(_chat_auto_allowed((42, None), burst_target_key(42, None), KEYS))

    def test_unknown_session_falls_back_and_is_not_allowed(self):
        # Неизвестный sid резолвится в primary-чат (target=None): даже если
        # это чат из списка, автоаппрув ему не достаётся.
        self.assertFalse(_chat_auto_allowed(None, burst_target_key(123456789, None), KEYS))

    def test_empty_keys_off(self):
        self.assertFalse(_chat_auto_allowed((123456789, None), "123456789", frozenset()))


class ChatAutoSecretTests(unittest.TestCase):
    """Секрет в чате с автоаппрувом и в фоновом ране идёт карточкой (находка
    Codex 15.10.2026: chat-auto стоял раньше проверок секретов)."""

    def _decide(self, tool_name: str, tool_input: dict, unattended: bool = False):
        import asyncio
        import json
        import re
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(
                approve_shared_secret="", primary_chat_id=1, state_dir=Path(d),
                approve_auto_chat_keys=frozenset() if unattended else frozenset({"1"}),
                approve_whitelist_paths=(), mcp_auto_allow_re=re.compile(r"get[A-Za-z_]*"),
                bash_auto_allow_re=re.compile(r"(cat|ls|grep) .*"),
            )
            bot_data = {"unattended_sids": {"sid"} if unattended else set(),
                        "untrusted_sids": set(), "burst_until_by_target": {}}
            request = SimpleNamespace(
                app={"cfg": cfg, "application": SimpleNamespace(bot_data=bot_data),
                     "registry": approval.ApprovalRegistry(),
                     "session_targets": {"sid": (1, None)}},
                headers={}, remote="127.0.0.1",
                json=AsyncMock(return_value={
                    "tool_name": tool_name, "tool_input": tool_input, "session_id": "sid",
                    "permission_mode": "bypassPermissions", "cwd": d,
                }),
            )
            card = AsyncMock(side_effect=OSError)
            with patch.object(approval, "send_approval_request", card), \
                    patch.object(approval.asyncio, "sleep", AsyncMock()):
                resp = asyncio.run(approval.handle_approve(request))
            return json.loads(resp.text), card

    def test_secret_read_goes_to_card(self):
        for cmd in ("cat ~/.env", "cat /home/u/src/bot/.env", "grep TOKEN ~/.ssh/config",
                    "python3 - <<'EOF'\nprint(open('/home/u/.env').read())\nEOF",
                    "cat /proc/1/environ", "ls; cat .en*"):
            with self.subTest(cmd=cmd):
                out, card = self._decide("Bash", {"command": cmd})
                self.assertEqual(out["decision"], "deny")
                card.assert_called()

    def test_secret_write_goes_to_card(self):
        out, card = self._decide("Write", {"file_path": "/home/u/.ssh/authorized_keys", "content": "x"})
        self.assertEqual(out["decision"], "deny")
        card.assert_called()

    def test_ordinary_commands_stay_auto(self):
        for cmd in ("git status", "rm -rf /tmp/x", "ssh host uptime", "grep -rn TODO src"):
            with self.subTest(cmd=cmd):
                out, card = self._decide("Bash", {"command": cmd})
                self.assertEqual(out["decision"], "allow")
                card.assert_not_called()

    def test_unattended_secret_read_goes_to_card(self):
        out, card = self._decide("Bash", {"command": "cat ~/.env"}, unattended=True)
        self.assertEqual(out["decision"], "deny")
        card.assert_called()


class ConfigParseTests(unittest.TestCase):
    def test_env_reaches_config(self):
        from claude_tg_bot import config as cfgmod
        env = {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_IDS": "1",
            "APPROVE_AUTO_CHAT_KEYS": " 1, 2:3 ,",
            "CLAUDE_BIN": sys.executable,
            "PATH": os.environ.get("PATH", ""),
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = cfgmod.load_config(skip_hook_validation=True)
        self.assertEqual(cfg.approve_auto_chat_keys, frozenset({"1", "2:3"}))

    def test_empty_env_is_off(self):
        from claude_tg_bot import config as cfgmod
        env = {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_IDS": "1",
            # Пустая строка явно: load_config подхватывает .env репозитория для
            # отсутствующих ключей, а там ключ задан.
            "APPROVE_AUTO_CHAT_KEYS": "",
            "CLAUDE_BIN": sys.executable, "PATH": os.environ.get("PATH", ""),
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = cfgmod.load_config(skip_hook_validation=True)
        self.assertEqual(cfg.approve_auto_chat_keys, frozenset())


if __name__ == "__main__":
    unittest.main()

"""Режим plan, обдумывание задач и фоновые раны: решение Ивана 04.11.2026,
замка «только чтение» нет, карточка только на отправку людям. Этап плана,
обдумывание задачи с карточками, чтение по разбору danger.py удалены."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import approval  # noqa: E402


class _HookBase(unittest.TestCase):
    def _decide(self, tool_name: str, tool_input: dict, *, mode: str = "plan",
                unattended: bool = False) -> tuple[dict, AsyncMock]:
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(
                approve_shared_secret="", primary_chat_id=1, state_dir=Path(d),
                approve_auto_chat_keys=frozenset(),
                approve_whitelist_paths=(Path(d),),
                mcp_auto_allow_re=re.compile(r"(get|list|search)[A-Za-z_]*"),
            )
            bot_data = {
                "unattended_sids": {"sid"} if unattended else set(), "untrusted_sids": set(),
            }
            request = SimpleNamespace(
                app={"cfg": cfg, "application": SimpleNamespace(bot_data=bot_data),
                     "registry": approval.ApprovalRegistry(),
                     "session_targets": {"sid": (1, 7)}},
                headers={}, remote="127.0.0.1",
                json=AsyncMock(return_value={
                    "tool_name": tool_name, "tool_input": tool_input, "cwd": d,
                    "session_id": "sid", "permission_mode": mode,
                }),
            )
            card = AsyncMock(side_effect=OSError)
            with patch.object(approval, "send_approval_request", card), \
                    patch.object(approval.asyncio, "sleep", AsyncMock()):
                resp = asyncio.run(approval.handle_approve(request))
            return json.loads(resp.text), card


class NoReadLockTests(_HookBase):
    """Решение Ивана 04.11.2026: замка «только чтение» нет, карточка только на
    отправку людям. Режим plan, фоновый ран и обдумывание ничего не запирают."""

    CALLS = (
        ("Bash", {"command": "rm -rf ~/src/x"}),
        ("Bash", {"command": "cat ~/src/claude-tg-bot/.env"}),
        ("Bash", {"command": "ssh immich docker ps"}),
        ("Write", {"file_path": "/home/user/.claude/plans/x.md", "content": "x"}),
        ("Edit", {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}),
        ("Monitor", {"command": "ls"}),
    )

    def test_plan_mode_allows_everything_but_sends(self) -> None:
        for tool, inp in self.CALLS:
            with self.subTest(tool=tool):
                with patch.object(approval.audit, "write_event") as audit_write:
                    out, card = self._decide(tool, inp, mode="plan")
                self.assertEqual(out["decision"], "allow")
                card.assert_not_called()
                self.assertEqual(audit_write.call_args.kwargs["decision"], "allow:default")

    def test_background_run_allows_everything_but_sends(self) -> None:
        for tool, inp in self.CALLS:
            with self.subTest(tool=tool):
                out, card = self._decide(tool, inp, mode="plan", unattended=True)
                self.assertEqual(out["decision"], "allow")
                card.assert_not_called()

    def test_send_in_plan_mode_goes_to_card(self) -> None:
        out, card = self._decide("mcp__telegram__send_message", {"chat_id": "@x", "message": "hi"})
        self.assertEqual(out["decision"], "deny")
        card.assert_called()


class DangerReadTests(_HookBase):
    """Бывший разбор чтения: теперь всё, что не отправка, идёт без карточки."""

    NOT_READS = ("ssh host docker ps", "python3 x.py", "echo x > /tmp/f", "git push",
                 "cat ~/src/claude-tg-bot/.env", "ss -K dst 1.2.3.4", "npm version patch",
                 "env --split-string='touch /tmp/p'", "sed -f p.sed in", "curl -K r.conf",
                 "curl http://127.0.0.1:8080/", "git remote update", "kubectl get secrets",
                 "echo $ANTHROPIC_API_KEY", "cat ~/.claude.json", "bash -c 'ls'",
                 "gh api -X POST repos/a/b/issues -f title=x", "curl -s http://127.0.0.1:8770/mcp -d '{}'")

    def test_changes_pass_without_card(self) -> None:
        # С 27.11.2026 (этап 3 защит) и публикация на GitHub, и сервер Telegram
        # из Bash без карточки: разбор команд на отправку снят.
        for cmd in self.NOT_READS:
            with self.subTest(cmd=cmd):
                out, card = self._decide("Bash", {"command": cmd}, mode="default")
                card.assert_not_called()
                self.assertEqual(out["decision"], "allow")

    def test_send_goes_to_card(self) -> None:
        out, card = self._decide("mcp__plugin_engineering_github__add_issue_comment", {"body": "x"},
                                 mode="default")
        card.assert_called()
        self.assertEqual(out["decision"], "deny")


class SecretTests(_HookBase):
    """Секрет на машине без карточки (03.11.2026); окно ⏩ снято 27.11.2026."""

    SECRET_CALLS = (
        ("Bash", {"command": "cat ~/src/claude-tg-bot/.env"}),
        ("Write", {"file_path": str(Path.home() / ".ssh" / "authorized_keys"), "content": "x"}),
    )
    def test_removed_classes_go_without_card_2026_11_04(self) -> None:
        """решение Ивана 04.11.2026: карточка только на отправку людям
        (docs/design-2026-11-04-system-boundaries.md)"""
        calls = (
            ("Bash", {"command": "rm -rf /srv/data"}),
            ("Bash", {"command": "curl -s -H \"Authorization: Bearer $(cat ~/.grafana_api_token)\" "
                                 "https://grafana.example.com/api/health"}),
            ("Write", {"file_path": str(Path.home() / ".ssh" / "config"), "content": "x"}),
        )
        for tool, inp in calls:
            with self.subTest(tool=tool, inp=inp):
                with patch.object(approval.audit, "write_event") as audit_write:
                    out, card = self._decide(tool, inp, mode="bypassPermissions")
                self.assertEqual(out["decision"], "allow")
                card.assert_not_called()
                self.assertEqual(audit_write.call_args.kwargs["decision"], "allow:default")

    def test_without_window_secret_passes(self) -> None:
        # С 03.11.2026 карточка только на опасное (serious.py, решение Ивана):
        # секрет на машине без карточки (guard3, Н6: принятый риск).
        for tool, inp in self.SECRET_CALLS:
            with self.subTest(tool=tool, inp=inp):
                out, card = self._decide(tool, inp, mode="bypassPermissions")
                card.assert_not_called()
                self.assertEqual(out["decision"], "allow")

if __name__ == "__main__":
    unittest.main()

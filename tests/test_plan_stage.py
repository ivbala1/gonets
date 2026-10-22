"""Этап плана (permission_mode plan: /plan и задачи до ✅): чтение без
карточки, запись отказом, окно /auto, автоаппрув чата, белый список и вызовы
скиллов не действуют (решение Ивана после ревью Codex 13.10.2026)."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import approval  # noqa: E402

# Настройка, разрешающая всё: этап плана на неё не опирается (ревью Codex 13.10).
READ_RE = re.compile(r".*")


class _HookBase(unittest.TestCase):
    def _decide(self, tool_name: str, tool_input: dict, *, mode: str = "plan",
                burst: bool = False, auto_chat: bool = False,
                marked: bool = False, think: bool = False, unattended: bool = False,
                file_in_state: str = "") -> tuple[dict, AsyncMock]:
        with tempfile.TemporaryDirectory() as d:
            if file_in_state:
                tool_input = {**tool_input, "file_path": f"{d}/{file_in_state}"}
            cfg = SimpleNamespace(
                approve_shared_secret="", primary_chat_id=1, state_dir=Path(d),
                approve_auto_chat_keys=frozenset({"1:7"}) if auto_chat else frozenset(),
                approve_whitelist_paths=(Path(d),),
                mcp_auto_allow_re=re.compile(r"(get|list|search)[A-Za-z_]*"),
                bash_auto_allow_re=READ_RE,
            )
            bot_data = {
                "unattended_sids": {"sid"} if unattended else set(), "untrusted_sids": set(),
                "plan_stage_sids": {"sid"} if marked or think else set(),
                "task_think_sids": {"sid"} if think else set(),
                "burst_until_by_target": {"1:7": time.time() + 600} if burst else {},
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


class PlanStageTests(_HookBase):
    def test_reads_pass_without_card(self) -> None:
        for cmd in ("ls -la ~/src", "which uv tailscale; tailscale status 2>&1 | head -12",
                    "codex --version 2>&1 | head -2",
                    "ps -eo pid,etime,args | grep -i codex | grep -v grep",
                    "git status --short", "ss -ltnp", "grep -n tailscale README.md | head"):
            with self.subTest(cmd=cmd):
                out, card = self._decide("Bash", {"command": cmd})
                self.assertEqual(out["decision"], "allow")
                card.assert_not_called()

    def test_secret_read_denied(self) -> None:
        out, card = self._decide("Bash", {"command": "cat ~/src/claude-tg-bot/.env"})
        self.assertEqual(out["decision"], "deny")

    def test_file_writes_denied_even_with_window_and_auto_chat(self) -> None:
        for tool in ("Write", "Edit"):
            with self.subTest(tool=tool):
                out, card = self._decide(tool, {"file_path": "/home/user/.claude/plans/x.md", "content": "x"},
                                         burst=True, auto_chat=True)
                self.assertEqual(out["decision"], "deny")
                self.assertIn("этап плана", out["reason"])
                card.assert_not_called()

    def test_mutating_denied_without_card_even_with_window(self) -> None:
        # до ✅ запись не разрешается и карточкой: одобряется план целиком
        for kw in ({}, {"burst": True}, {"auto_chat": True}):
            for cmd in ("rm -rf ~/src/x", "ss -K dst 1.2.3.4", "sort -o /tmp/x a", "find . -delete",
                        "./evil.sh --version", "npm version patch"):
                with self.subTest(cmd=cmd, **kw):
                    out, card = self._decide("Bash", {"command": cmd}, **kw)
                    self.assertEqual(out["decision"], "deny")
                    self.assertIn("этап плана", out["reason"])
                    card.assert_not_called()

    def test_mcp_and_monitor(self) -> None:
        out, _ = self._decide("mcp__telegram__get_history", {"chat_id": 1})
        self.assertEqual(out["decision"], "allow")
        for tool in ("mcp__telegram__send_message", "mcp__x__create_issue", "Monitor"):
            with self.subTest(tool=tool):
                out, card = self._decide(tool, {"chat_id": 1, "command": "ls"})
                self.assertEqual(out["decision"], "deny")
                card.assert_not_called()

    def test_printf_v_cannot_swap_commands(self) -> None:
        cmd = "printf -v 'BASH_CMDS[ls]' /usr/bin/rm; ls -rf /tmp/x"
        self.assertFalse(approval._plan_read_allowed(cmd))
        self.assertFalse(approval._bash_auto_allowed(cmd, re.compile(r"(?:ls|printf)(?: .*)?")))
        self.assertTrue(approval._plan_read_allowed("printf '%s\\n' a"))

    def test_plan_mcp_list_ignores_permissive_setting(self) -> None:
        # в _decide настройка MCP пропускает get|list|search; create_issue она бы не пустила,
        # а здесь проверяем собственный список этапа плана
        for name, ok in (("get_history", True), ("slack_read_channel", True), ("mark_read", False),
                         ("download_media", False), ("export_chat_invite", False), ("create_issue", False)):
            with self.subTest(name=name):
                self.assertEqual(bool(approval._PLAN_MCP_READ_RE.fullmatch(name)), ok)

    def test_bot_mark_holds_when_cli_mode_missing(self) -> None:
        out, _ = self._decide("Bash", {"command": "rm -rf ~/src/x"}, mode="", marked=True, burst=True)
        self.assertEqual(out["decision"], "deny")
        out, _ = self._decide("Bash", {"command": "ls"}, mode="", marked=True)
        self.assertEqual(out["decision"], "allow")

    def test_skill_call_is_not_read(self) -> None:
        cmd = f"{approval._SKILL_PYTHON} -m claude_tg_bot.skills.news_digest --preview"
        out, card = self._decide("Bash", {"command": cmd})
        self.assertEqual(out["decision"], "deny")
        card.assert_not_called()
        out, card = self._decide("Bash", {"command": cmd}, mode="bypassPermissions")
        self.assertEqual(out["decision"], "allow")
        card.assert_not_called()

    def test_window_works_after_approval(self) -> None:
        out, card = self._decide("Bash", {"command": "rm -rf ~/src/x"}, mode="bypassPermissions", burst=True)
        self.assertEqual(out["decision"], "allow")
        card.assert_not_called()



class TaskThinkTests(_HookBase):
    """Обдумывание темы задачи (переделка 18.10.2026): то же чтение и /2mind,
    прочее карточкой мимо окна, автоаппрува чата, regex и белого списка; файлы
    и отправка наружу отказом; фоновый ран строгий (ревью Codex 18.10)."""

    def test_unlisted_goes_to_card_past_every_auto_allow(self) -> None:
        skill = f"{approval._SKILL_PYTHON} -m claude_tg_bot.skills.task_new 'обнови Immich'"
        for kw in ({}, {"burst": True}, {"auto_chat": True}):
            for cmd in ("ssh immich docker ps", "rm -rf ~/src/x", skill):
                with self.subTest(cmd=cmd, **kw):
                    out, card = self._decide("Bash", {"command": cmd}, think=True, **kw)
                    card.assert_called()
                    self.assertEqual(out["decision"], "deny")  # карточка не ушла (OSError)

    def test_mcp_write_goes_to_card(self) -> None:
        out, card = self._decide("mcp__x__create_issue", {"title": "t"}, think=True, burst=True)
        card.assert_called()

    def test_reads_and_two_mind_pass_without_card(self) -> None:
        ask = (f"python3 ~/.claude/skills/2mind/ask.py "
               f"{approval._SKILL_PYTHON.parents[2]}/state/2mind/q.txt --cd ~/src/claude-tg-bot")
        for cmd in ("git log --oneline -3", ask):
            with self.subTest(cmd=cmd):
                out, card = self._decide("Bash", {"command": cmd}, think=True)
                self.assertEqual(out["decision"], "allow")
                card.assert_not_called()
        out, card = self._decide("Write", {"content": "вопрос"}, think=True, file_in_state="2mind/q.txt")
        self.assertEqual(out["decision"], "allow")
        card.assert_not_called()

    def test_files_and_outbound_denied_without_card(self) -> None:
        cases = (("Write", {"content": "x"}, "notes/x.md"), ("Edit", {"old_string": "a"}, "2mind/q.txt"),
                 ("MultiEdit", {"edits": []}, "2mind/q.txt"),
                 ("mcp__telegram__send_message", {"chat_id": 1, "message": "hi"}, ""))
        for tool, inp, rel in cases:
            with self.subTest(tool=tool, rel=rel):
                out, card = self._decide(tool, inp, think=True, burst=True, auto_chat=True, file_in_state=rel)
                self.assertEqual(out["decision"], "deny")
                card.assert_not_called()

    def test_mailsend_denied_not_carded(self) -> None:
        cmd = "bash -c '.venv/bin/python -m claude_tg_bot.mailsend --to a@b.c --subject s'"
        out, card = self._decide("Bash", {"command": cmd}, think=True)
        self.assertEqual(out["decision"], "deny")
        self.assertIn("обдумывание задачи", out["reason"])
        card.assert_not_called()

    def test_mcp_past_permissive_setting_goes_to_card(self) -> None:
        # настройка MCP пустила бы listing_purge, список этапа плана нет
        out, card = self._decide("mcp__x__listing_purge", {"id": 1}, think=True)
        card.assert_called()

    def test_card_has_no_window_buttons(self) -> None:
        out, card = self._decide("Bash", {"command": "ssh immich docker ps"}, think=True)
        self.assertTrue(card.call_args.args[2].no_burst)
        # обычная карточка (запись вне белого списка) кнопки ⏩ сохраняет
        out, card = self._decide("Write", {"file_path": "/etc/x", "content": "x"}, mode="bypassPermissions")
        self.assertFalse(card.call_args.args[2].no_burst)

    def test_background_run_stays_strict(self) -> None:
        out, card = self._decide("Bash", {"command": "ssh immich docker ps"}, think=True, unattended=True)
        self.assertEqual(out["decision"], "deny")
        card.assert_not_called()

    def test_plain_plan_unchanged(self) -> None:
        out, card = self._decide("Bash", {"command": "ssh immich docker ps"})
        self.assertIn("этап плана", out["reason"])
        card.assert_not_called()


class ThinkReadListTests(_HookBase):
    """Разбор 20.10.2026: за 8 минут обдумывания ~17 карточек на чтение.
    Читающие формы без карточки, пишущие и секреты по-прежнему нет."""

    READS = (
        'iconv -f utf-16le -t utf-8 "/mnt/c/Windows/System32/Tasks/WSL-autostart-claude-tg-bot"'
        " | sed 's/\\r//' | head -70",
        "iconv -futf-16le -tutf-8 a.txt", "iconv -l",
        '/mnt/c/Windows/System32/tasklist.exe 2>&1 | grep -i -E "chrome|explorer" | head -20',
        '/mnt/c/Windows/System32/tasklist.exe /FI "IMAGENAME eq chrome.exe" /FO CSV /NH',
        "/mnt/c/Windows/System32/tasklist.exe /v /fi \"SESSION eq 1\"",
        "date", "date +%F",
        "file -L ~/.local/bin/agent-browser | cut -c1-200",
        "strings -n 8 -a ~/.local/bin/agent-browser | grep -E dashboard | head -40",
        "tailscale serve status 2>&1 | head", "tailscale serve status --json",
        "tailscale funnel status", "tailscale status --json",
        'agent-browser --help 2>&1 | grep -i -n -E "stream|screencast" | head -30',
        "agent-browser --version", "~/.local/bin/agent-browser --help",
        f"{Path.home()}/.local/bin/chrome-relay --help", "restic --help", "claude --help",
        "systemctl --user status chrome-relay 2>&1 | head -12",
        # `;` в конце ничего не запускает
        'cd ~/src && grep -l -i -E "chrome.relay|browser" README.md | head; ',
        # GitHub чтением (задача «Обновить Immich» 21.10.2026)
        "gh release list -R immich-app/immich --limit 6",
        "gh api repos/immich-app/immich/releases/tags/v3.3.4 --jq .body",
        'gh api repos/immich-app/immich/compare/v3.3.3...v3.3.4 --jq ".files[].filename"',
    )
    WRITES = (
        "iconv -f utf-16le -t utf-8 -o /tmp/x a.txt", "iconv -f a -t b --output=/tmp/x a.txt",
        "iconv -f a -t b --out /tmp/x a.txt", "iconv -co /tmp/x a.txt", "iconv -f a -t b *.txt",
        "date -s 2026-01-01", "date --set=now",
        "tailscale serve 8080", "tailscale serve --bg 8080", "tailscale serve reset",
        "tailscale funnel 443 on", "tailscale up", "tailscale down",
        "/mnt/c/Windows/System32/tasklist.exe /S host /U u /P pass",
        "/mnt/c/Windows/System32/tasklist.exe /FI", "tasklist.exe",
        # справка только у известных программ и без подкоманды
        "chrome-relay status", "chrome-relay status --help", "foo --help",
        "./bin/agent-browser --help", "/tmp/agent-browser --help",
        "systemctl --user restart chrome-relay", "file -C -m x",
        # склейка, переменные, подстановка и интерпретаторы как раньше
        "export PATH=$HOME/.local/bin:$PATH; agent-browser --version",
        'strings -n 8 "$(readlink -f ~/.local/bin/agent-browser)"',
        "tailscale status --json | python3 -c 'import sys'",
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command Get-Date",
        "find . -exec rm {} \\;",
        "gh api -X POST repos/a/b/issues -f title=x", "gh api repos/a/b/issues --input x.json",
        "gh api graphql -f 'query=mutation { x }'", "gh release create v1", "gh pr merge 1", "gh repo view --web",
    )
    SECRETS = (
        "iconv -f utf-8 -t utf-8 ~/src/claude-tg-bot/.env", "iconv -f utf-8 -t ascii ~/.ssh/id_rsa",
        "iconv -f utf-8 -t utf-8 ~/.claude/.credentials.json", "strings ~/src/claude-tg-bot/.env",
        "file ~/.ssh/id_ed25519",
    )

    def test_reads_pass_without_card(self) -> None:
        for cmd in self.READS:
            with self.subTest(cmd=cmd):
                self.assertTrue(approval._plan_read_allowed(cmd))
                out, card = self._decide("Bash", {"command": cmd}, think=True)
                self.assertEqual(out["decision"], "allow")
                card.assert_not_called()

    def test_writing_forms_are_not_reads(self) -> None:
        for cmd in self.WRITES + self.SECRETS:
            with self.subTest(cmd=cmd):
                self.assertFalse(approval._plan_read_allowed(cmd))
                self.assertFalse(approval._plan_read_allowed(cmd, two_mind=True))
                # /plan: отказ без карточки
                out, card = self._decide("Bash", {"command": cmd})
                self.assertEqual(out["decision"], "deny")
                card.assert_not_called()
                # обдумывание задачи: карточка мимо окна ⏩ и автоаппрува чата
                out, card = self._decide("Bash", {"command": cmd}, think=True, burst=True, auto_chat=True)
                card.assert_called()
                self.assertTrue(card.call_args.args[2].no_burst)


class BurstSecretTests(_HookBase):
    def test_window_does_not_pass_secret(self) -> None:
        out, card = self._decide("Bash", {"command": "cat ~/src/claude-tg-bot/.env"},
                                 mode="bypassPermissions", burst=True)
        card.assert_called()
        out, card = self._decide("Write", {"file_path": str(Path.home() / ".ssh" / "config"), "content": "x"},
                                 mode="bypassPermissions", burst=True)
        card.assert_called()


if __name__ == "__main__":
    unittest.main()

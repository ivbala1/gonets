"""Карточка плана: одно «✅ Делай план» на запуск (шаг 2 карточек, 29.10.2026)."""
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

from claude_tg_bot import approval, runs  # noqa: E402

PY = str(approval._SKILL_PYTHON)
# отправка через сервер Telegram: единственный Bash, который ещё идёт карточкой
SEND_CMD = "curl -s http://127.0.0.1:8770/mcp -d '{}'"
PLAN_CMD = f"{PY} -m claude_tg_bot.skills.plan 'обновить nginx на web1, проверю curl'"


class PlanRequestTests(unittest.TestCase):
    def test_ok(self) -> None:
        self.assertEqual(approval._plan_request("Bash", {"command": PLAN_CMD}),
                         ("ok", "обновить nginx на web1, проверю curl"))

    def test_dollar_is_literal_in_single_quotes(self) -> None:
        self.assertEqual(approval._plan_request("Bash", {"command": f"{PY} -m claude_tg_bot.skills.plan '$HOME x'"}),
                         ("ok", "$HOME x"))

    def test_not_a_plan(self) -> None:
        for cmd in ("ls", f"{PY} -m claude_tg_bot.skills.task_new 'сделай план миграции'",
                    "rg 'skills.plan' src", "cat src/claude_tg_bot/skills/plan/__main__.py",
                    "grep -rn claude_tg_bot.skills.plan src"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(approval._plan_request("Bash", {"command": cmd}))
        self.assertIsNone(approval._plan_request("Write", {"file_path": "/x/skills/plan"}))

    def test_bad_forms(self) -> None:
        for cmd in (
            PLAN_CMD + "; rm -rf x",
            PLAN_CMD + " && echo ok",
            PLAN_CMD + " > /tmp/x",
            f"{PY} -m claude_tg_bot.skills.plan \"$(cat ~/.ssh/id_rsa)\"",
            f"{PY} -m claude_tg_bot.skills.plan `id`",
            f"python3 -m claude_tg_bot.skills.plan 'x'",
            f"{PY} -m claude_tg_bot.skills.plan 'a' 'b'",
            f"{PY} -m claude_tg_bot.skills.plan ''",
            f"{PY} -m claude_tg_bot.skills.plan '" + "x" * (approval.PLAN_TEXT_MAX + 1) + "'",
            f"cd /tmp; {PY} -m claude_tg_bot.skills.plan 'x'",
            f"{PY} -m claude_tg_bot.skills.plan\n/usr/bin/id",
            f"{PY} -m claude_tg_bot.skills.plan 'x'\n/usr/bin/id",
            f"{PY} -m claude_tg_bot.skills.plan ~",
            f"{PY} -m claude_tg_bot.skills.plan x*",
            f"{PY} -m claude_tg_bot.skills.pl\"\"an 'x'",
        ):
            with self.subTest(cmd=cmd[:80]):
                self.assertEqual(approval._plan_request("Bash", {"command": cmd})[0], "bad")


class RunsTests(unittest.TestCase):
    def test_grant_lives_until_run_end(self) -> None:
        rid = runs.start("s1")
        self.assertTrue(runs.grant_plan("s1", rid, "aid1"))
        self.assertEqual(runs.plan_for("s1").plan_aid, "aid1")
        runs.end("s1", rid)
        self.assertIsNone(runs.plan_for("s1"))

    def test_old_run_cannot_grant_new_run(self) -> None:
        old = runs.start("s2")
        runs.end("s2", old)
        new = runs.start("s2")
        try:
            self.assertFalse(runs.grant_plan("s2", old, "aid"))
            self.assertIsNone(runs.plan_for("s2"))
        finally:
            runs.end("s2", new)

    def test_two_runs_one_sid_get_nothing(self) -> None:
        a, b = runs.start("s4"), runs.start("s4")
        try:
            self.assertEqual(runs.current("s4"), "")
            self.assertFalse(runs.grant_plan("s4", a, "aid"))
        finally:
            runs.end("s4", a)
            runs.end("s4", b)

    def test_rebind_keeps_grant(self) -> None:
        rid = runs.start("tmp")
        runs.grant_plan("tmp", rid, "aid")
        runs.rebind("tmp", "real", rid)
        try:
            self.assertEqual(runs.plan_for("real").plan_aid, "aid")
            self.assertIsNone(runs.plan_for("tmp"))
        finally:
            runs.end("real", rid)

    def test_ceiling(self) -> None:
        rid = runs.start("s3")
        try:
            runs.grant_plan("s3", rid, "aid")
            with patch.object(runs.time, "time", return_value=time.time() + runs.PLAN_MAX_SEC + 1):
                self.assertIsNone(runs.plan_for("s3"))
        finally:
            runs.end("s3", rid)


class HandleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = runs.start("sid")
        self.addCleanup(runs.end, "sid", self.run_id)

    def _decide(self, tool_name: str, tool_input: dict, *, press: str = "allow", plan_cards: bool = True,
                auto_chat: bool = False, unattended: bool = False, burst: bool = False,
                mode: str = "bypassPermissions") -> tuple[dict, AsyncMock, list]:
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(
                approve_shared_secret="", primary_chat_id=1, state_dir=Path(d), plan_cards=plan_cards,
                approve_auto_chat_keys=frozenset({"1:7"}) if auto_chat else frozenset(),
                approve_whitelist_paths=(), approve_timeout_sec=60,
                mcp_auto_allow_re=re.compile(r"(get|list|search)[A-Za-z_]*"),
            )
            registry = approval.ApprovalRegistry()
            bot_data = {
                "unattended_sids": {"sid"} if unattended else set(), "untrusted_sids": set(),
                "burst_until_by_target": {"1:7": time.time() + 600} if burst else {},
            }
            request = SimpleNamespace(
                app={"cfg": cfg, "application": SimpleNamespace(bot_data=bot_data),
                     "registry": registry, "session_targets": {"sid": (1, 7)}},
                headers={}, remote="127.0.0.1",
                json=AsyncMock(return_value={
                    "tool_name": tool_name, "tool_input": tool_input, "cwd": d,
                    "session_id": "sid", "permission_mode": mode,
                }),
            )

            async def _press(application, cfg, pending):
                pending.telegram_message_id = 5
                asyncio.get_running_loop().call_soon(registry.resolve, pending.approval_id, press)

            card = AsyncMock(side_effect=_press)
            with patch.object(approval, "send_approval_request", card), \
                    patch.object(approval.audit, "write_event",
                                 side_effect=lambda *a, **k: events.append(k)):
                resp = asyncio.run(approval.handle_approve(request))
            return json.loads(resp.text), card, events

    def test_plan_card_press_grants_run(self) -> None:
        out, card, events = self._decide("Bash", {"command": PLAN_CMD})
        self.assertEqual(out["decision"], "allow")
        pending = card.call_args.args[2]
        self.assertEqual(pending.tool_input[approval.PLAN_CARD_KEY]["text"],
                         "обновить nginx на web1, проверю curl")
        self.assertEqual(pending.run_id, self.run_id)
        self.assertEqual(runs.plan_for("sid").plan_aid, pending.approval_id)
        self.assertEqual(events[-1]["decision"], "allow:plan-card")
        self.assertEqual(events[-1]["run_id"], self.run_id)

    def test_deny_press_grants_nothing(self) -> None:
        out, _card, _events = self._decide("Bash", {"command": PLAN_CMD}, press="deny")
        self.assertEqual(out["decision"], "deny")
        self.assertIsNone(runs.plan_for("sid"))

    def test_plan_field_from_agent_is_dropped(self) -> None:
        out, card, _events = self._decide("Bash", {"command": SEND_CMD,
                                                   approval.PLAN_CARD_KEY: {"text": "обычная проверка"}},
                                          press="allow")
        self.assertNotIn(approval.PLAN_CARD_KEY, card.call_args.args[2].tool_input)
        self.assertIsNone(runs.plan_for("sid"))

    def test_deny_on_new_plan_revokes_old(self) -> None:
        runs.grant_plan("sid", self.run_id, "planaid")
        out, _card, _events = self._decide("Bash", {"command": PLAN_CMD}, press="deny")
        self.assertEqual(out["decision"], "deny")
        self.assertIsNone(runs.plan_for("sid"))

    def test_burst_window_does_not_swallow_plan_request(self) -> None:
        out, card, _events = self._decide("Bash", {"command": PLAN_CMD}, burst=True)
        card.assert_called_once()
        self.assertIsNotNone(runs.plan_for("sid"))

    def test_plan_request_refused_where_not_offered(self) -> None:
        for kwargs in ({"auto_chat": True}, {"unattended": True, "mode": "plan"},
                       {"plan_cards": False}):
            with self.subTest(**kwargs):
                out, card, events = self._decide("Bash", {"command": PLAN_CMD}, **kwargs)
                self.assertEqual(out["decision"], "deny")
                card.assert_not_called()
                self.assertEqual(events[-1]["decision"], "deny:plan-request")
                self.assertIsNone(runs.plan_for("sid"))

    def test_attended_plan_mode_gets_plan_card(self) -> None:
        """решение Ивана 04.11.2026: замка «только чтение» нет, карточка только на
        отправку людям; режим plan карточку плана не запрещает"""
        out, card, _events = self._decide("Bash", {"command": PLAN_CMD}, mode="plan")
        self.assertEqual(out["decision"], "allow")
        card.assert_called_once()

    def test_steps_after_plan(self) -> None:
        runs.grant_plan("sid", self.run_id, "planaid")
        out, card, events = self._decide("Bash", {"command": "sudo systemctl restart nginx"}, press="deny")
        self.assertEqual(out["decision"], "allow")
        card.assert_not_called()
        # С 03.11.2026 карточка только на опасное (serious.py, решение Ивана): 
        # шаг идёт без карточки и без плана.
        self.assertEqual(events[-1]["decision"], "allow:default")
        out, card, _events = self._decide("Write", {"file_path": "/tmp/plan-test/x.conf", "content": "x"},
                                          press="deny")
        self.assertEqual(out["decision"], "allow")
        card.assert_not_called()

    def test_exceptions_still_card_after_plan(self) -> None:
        runs.grant_plan("sid", self.run_id, "planaid")
        for tool, inp in (
            ("mcp__telegram__send_message", {"chat_id": "@x", "message": "hi"}),
            ("Bash", {"command": PLAN_CMD}),
        ):
            with self.subTest(tool=tool, inp=inp):
                out, card, _events = self._decide(tool, inp, press="deny")
                card.assert_called_once()
                self.assertEqual(out["decision"], "deny")
            runs.grant_plan("sid", self.run_id, "planaid")

    def test_removed_classes_go_without_card_2026_11_04(self) -> None:
        """решение Ивана 04.11.2026: карточка только на отправку людям
        (docs/design-2026-11-04-system-boundaries.md)"""
        for tool, inp in (("Bash", {"command": "rm -rf /srv/data"}),
                          ("Bash", {"command": "git push origin main"}),
                          ("mcp__todoist__delete-object", {"id": "1"})):
            with self.subTest(tool=tool, inp=inp):
                out, card, _events = self._decide(tool, inp, press="deny")
                card.assert_not_called()
                self.assertEqual(out["decision"], "allow")

    def test_run_end_drops_plan(self) -> None:
        runs.grant_plan("sid", self.run_id, "planaid")
        runs.end("sid", self.run_id)
        self.run_id = runs.start("sid")
        self.addCleanup(runs.end, "sid", self.run_id)
        out, card, _events = self._decide("Bash", {"command": SEND_CMD}, press="deny")
        card.assert_called_once()
        self.assertEqual(out["decision"], "deny")


class PersonaNoteTests(unittest.TestCase):
    def test_note_only_where_plan_offered(self) -> None:
        from claude_tg_bot import bot
        cfg = SimpleNamespace(plan_cards=True, approve_auto_chat_keys=frozenset({"1:7"}))
        self.assertIn("claude_tg_bot.skills.plan", bot._plan_note(cfg, "1:8"))
        self.assertIn(PY, bot._plan_note(cfg, "1:8"))
        self.assertEqual(bot._plan_note(cfg, "1:7"), "")
        self.assertEqual(bot._plan_note(cfg, "1:8", task_topic=True), "")
        self.assertEqual(bot._plan_note(SimpleNamespace(plan_cards=False, approve_auto_chat_keys=frozenset()),
                                        "1:8"), "")


if __name__ == "__main__":
    unittest.main()

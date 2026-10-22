"""Честный футер: контекст последнего запроса основного диалога и цена ответа
разницей накопительного total_cost_usd (claude_runner, replies, 13.10.2026)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import claude_runner as cr  # noqa: E402
from claude_tg_bot import replies  # noqa: E402


def _assistant(inp: int, read: int, create: int, parent: str | None = None) -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"id": "m", "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                              "cache_creation_input_tokens": create, "output_tokens": 9}}}


class RequestContextTests(unittest.TestCase):
    def test_main_thread_request_size(self) -> None:
        self.assertEqual(cr._request_context(_assistant(2, 57668, 1068)), 58738)

    def test_subagent_and_other_events_ignored(self) -> None:
        self.assertIsNone(cr._request_context(_assistant(2, 100, 0, parent="toolu_1")))
        self.assertIsNone(cr._request_context({"type": "user"}))
        self.assertIsNone(cr._request_context({"type": "assistant", "message": {}}))


class AnswerCostTests(unittest.TestCase):
    def test_new_session_whole_total(self) -> None:
        totals: dict[str, str] = {}
        self.assertAlmostEqual(cr._answer_cost(totals, "s1", 0.5, resumed=False), 0.5)
        self.assertEqual(totals["s1"], "0.500000")

    def test_resumed_session_difference(self) -> None:
        totals = {"s1": "11.660000"}
        self.assertAlmostEqual(cr._answer_cost(totals, "s1", 12.04, resumed=True), 0.38)
        self.assertEqual(totals["s1"], "12.040000")

    def test_resumed_unknown_previous(self) -> None:
        totals: dict[str, str] = {}
        self.assertIsNone(cr._answer_cost(totals, "s1", 35.9, resumed=True))
        self.assertEqual(totals["s1"], "35.900000")

    def test_total_went_down_is_unknown(self) -> None:
        totals = {"s1": "5.0"}
        self.assertIsNone(cr._answer_cost(totals, "s1", 1.0, resumed=True))

    def test_store_failure_does_not_raise(self) -> None:
        class Broken(dict):
            def __setitem__(self, k, v):
                raise OSError("disk full")

        self.assertAlmostEqual(cr._answer_cost(Broken(), "s1", 0.3, resumed=False), 0.3)

    def test_store_keeps_recent_sessions(self) -> None:
        totals = {f"s{i}": "1.0" for i in range(cr.COST_TOTALS_KEEP)}
        cr._answer_cost(totals, "s0", 2.0, resumed=True)
        cr._answer_cost(totals, "new", 0.1, resumed=False)
        self.assertEqual(len(totals), cr.COST_TOTALS_KEEP)
        self.assertIn("s0", totals)
        self.assertNotIn("s1", totals)

    def test_no_store(self) -> None:
        self.assertAlmostEqual(cr._answer_cost(None, "s1", 0.3, resumed=False), 0.3)
        self.assertIsNone(cr._answer_cost(None, "s1", 0.3, resumed=True))


class FooterTests(unittest.IsolatedAsyncioTestCase):
    async def _footer(self, result: cr.ClaudeResult, warn: int = 200_000) -> str | None:
        cfg = SimpleNamespace(show_cost_footer=True, context_warn_tokens=warn)
        msg = SimpleNamespace(reply_text=AsyncMock())
        await replies.send_cost_footer(cfg, SimpleNamespace(message=msg), result)
        return msg.reply_text.call_args.args[0] if msg.reply_text.await_count else None

    async def test_answer_session_and_context(self) -> None:
        text = await self._footer(cr.ClaudeResult(
            text="", cost_usd=0.47, session_cost_usd=35.91, context_tokens=292_000, context_max=300_000))
        self.assertEqual(text, "💸 $0.47 (сессия $35.91) · 🧠 контекст 292k\n"
                               "⚠️ контекст большой: /reset удешевит и ускорит следующие ответы")

    async def test_max_shown_when_notably_larger(self) -> None:
        text = await self._footer(cr.ClaudeResult(
            text="", cost_usd=0.1, session_cost_usd=1.0, context_tokens=50_000, context_max=120_000))
        self.assertEqual(text, "💸 $0.10 (сессия $1.00) · 🧠 контекст 50k (макс 120k)")

    async def test_unknown_answer_cost_and_compacted(self) -> None:
        text = await self._footer(cr.ClaudeResult(
            text="", cost_usd=None, session_cost_usd=3.0, context_tokens=0, compacted=True))
        self.assertEqual(text, "💸 сессия $3.00 · 🧠 контекст сжат")
        text = await self._footer(cr.ClaudeResult(
            text="", cost_usd=0.2, session_cost_usd=3.0, context_tokens=0, context_max=300_000,
            compacted=True))
        self.assertEqual(text, "💸 $0.20 (сессия $3.00) · 🧠 контекст сжат (макс 300k)")

    async def test_no_reset_hint_with_autocompact(self) -> None:
        cfg = SimpleNamespace(show_cost_footer=True, context_warn_tokens=200_000)
        msg = SimpleNamespace(reply_text=AsyncMock())
        await replies.send_cost_footer(cfg, SimpleNamespace(message=msg), cr.ClaudeResult(
            text="", cost_usd=0.1, session_cost_usd=2.0, context_tokens=250_000), warn_reset=False)
        self.assertEqual(msg.reply_text.call_args.args[0], "💸 $0.10 (сессия $2.00) · 🧠 контекст 250k")

    async def test_no_spend_no_footer(self) -> None:
        self.assertIsNone(await self._footer(cr.ClaudeResult(text="", cost_usd=0.0)))



class AutocompactConfigTests(unittest.TestCase):
    def test_values(self) -> None:
        from claude_tg_bot import config
        for raw, want in (("", ""), ("auto", "auto"), (" 300K ", "300k"), ("1m", "1m"), ("100000", "100000")):
            self.assertEqual(config._autocompact_env(raw), want)
        for bad in ("50k", "2m", "много", "300kk"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                config._autocompact_env(bad)

    def test_only_listed_chats(self) -> None:
        from claude_tg_bot import bot
        cfg = SimpleNamespace(autocompact="300k", autocompact_chats=frozenset({"1"}))
        self.assertEqual(bot._autocompact_for(cfg, "1"), "300k")
        self.assertEqual(bot._autocompact_for(cfg, "-100:5"), "")
        self.assertEqual(bot._autocompact_for(SimpleNamespace(autocompact="auto", autocompact_chats=frozenset()), "x"), "auto")
        self.assertEqual(bot._autocompact_for(SimpleNamespace(), "1"), "")


class CompactionFooterTests(unittest.IsolatedAsyncioTestCase):
    async def test_compactions_counted(self) -> None:
        cfg = SimpleNamespace(show_cost_footer=True, context_warn_tokens=0)
        msg = SimpleNamespace(reply_text=AsyncMock())
        await replies.send_cost_footer(cfg, SimpleNamespace(message=msg), cr.ClaudeResult(
            text="", cost_usd=0.5, session_cost_usd=2.0, context_tokens=220_000,
            context_max=230_000, compacted=2))
        self.assertEqual(msg.reply_text.call_args.args[0],
                         "💸 $0.50 (сессия $2.00) · 🧠 контекст 220k, сжатий 2")


if __name__ == "__main__":
    unittest.main()

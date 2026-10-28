"""Отправка от имени Ивана (Telegram, Gmail) всегда через карточку: мимо
автоаппрува чата, фонового рана и окна /auto (решение Ивана 12.10.2026)."""

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

from claude_tg_bot import approval, bot  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402


class OutboundCardTests(unittest.TestCase):
    def _decide(self, tool_name: str, tool_input: dict, *, unattended: bool = False,
                burst: bool = False) -> tuple[dict, AsyncMock]:
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(
                approve_shared_secret="", primary_chat_id=1, state_dir=Path(d),
                # Чат с автоаппрувом, как у Домового.
                approve_auto_chat_keys=frozenset({"1"}), approve_whitelist_paths=(),
                mcp_auto_allow_re=re.compile(r"(get|list|search|send)[A-Za-z_]*"),
            )
            bot_data = {
                "unattended_sids": {"sid"} if unattended else set(),
                "untrusted_sids": set(),
                "burst_until_by_target": {"1": time.time() + 600} if burst else {},
            }
            request = SimpleNamespace(
                app={"cfg": cfg, "application": SimpleNamespace(bot_data=bot_data),
                     "registry": approval.ApprovalRegistry(),
                     "session_targets": {"sid": (1, None)}},
                headers={}, remote="127.0.0.1",
                json=AsyncMock(return_value={
                    "tool_name": tool_name, "tool_input": tool_input,
                    "session_id": "sid", "permission_mode": "bypassPermissions",
                }),
            )
            # Карточка: отправка падает, повторы без пауз, итог deny.
            card = AsyncMock(side_effect=OSError)
            with patch.object(approval, "send_approval_request", card), \
                    patch.object(approval.asyncio, "sleep", AsyncMock()):
                resp = asyncio.run(approval.handle_approve(request))
            return json.loads(resp.text), card

    def test_telegram_send_goes_to_card_in_auto_chat(self) -> None:
        for tool in ("mcp__telegram__send_message", "mcp__telegram__reply_to_message",
                     "mcp__telegram__send_file", "mcp__claude_ai_Gmail__send_message",
                     "mcp__claude_ai_Gmail__reply"):
            with self.subTest(tool=tool):
                out, card = self._decide(tool, {"chat_id": "@x", "message": "hi"})
                self.assertEqual(out["decision"], "deny")
                card.assert_called()

    def test_send_not_allowed_in_unattended_run(self) -> None:
        out, card = self._decide("mcp__telegram__send_message", {"chat_id": "@x", "message": "hi"},
                                 unattended=True)
        self.assertEqual(out["decision"], "deny")
        card.assert_called()

    def test_send_not_allowed_in_burst_window(self) -> None:
        out, card = self._decide("mcp__telegram__send_message", {"chat_id": "@x", "message": "hi"},
                                 burst=True)
        self.assertEqual(out["decision"], "deny")
        card.assert_called()

    def test_other_send_verbs_go_to_card(self) -> None:
        for tool in ("mcp__telegram__send_voice", "mcp__telegram__edit_message",
                     "mcp__telegram__send_scheduled_message",
                     "mcp__claude_ai_Slack__slack_send_message"):
            with self.subTest(tool=tool):
                out, card = self._decide(tool, {"chat_id": "@x", "message": "hi"})
                self.assertEqual(out["decision"], "deny")
                card.assert_called()

    def test_forward_and_draft_refused_without_card(self) -> None:
        for tool, inp in (("mcp__telegram__forward_message", {"to_chat_id": "@x"}),
                          ("mcp__claude_ai_Gmail__forward", {"to": ["a@b.c"]}),
                          ("mcp__claude_ai_Gmail__send_message", {"draftId": "r-1"})):
            with self.subTest(tool=tool):
                out, card = self._decide(tool, inp)
                self.assertEqual(out["decision"], "deny")
                self.assertNotEqual(out["reason"], "telegram send failed")
                card.assert_not_called()

    def test_reading_and_drafts_still_auto(self) -> None:
        for tool in ("mcp__telegram__get_messages", "mcp__claude_ai_Gmail__create_draft",
                     "mcp__slack__send_message"):
            with self.subTest(tool=tool):
                out, card = self._decide(tool, {})
                self.assertEqual(out["decision"], "allow")
                card.assert_not_called()


class OutboundCardTextTests(unittest.TestCase):
    def test_telegram_card_shows_recipient_text_and_file(self) -> None:
        body = approval._format_tool_body(
            "mcp__telegram__send_file",
            {"chat_id": "@hr_example", "file_path": "/home/u/r.pdf", "caption": "Резюме <b>"},
            None)
        self.assertIn(_("📤 <b>отправка от твоего имени</b>"), body)
        self.assertIn("@hr_example", body)
        self.assertIn("/home/u/r.pdf", body)
        self.assertIn("Резюме &lt;b&gt;", body)

    def test_gmail_card_shows_attachment_names_not_content(self) -> None:
        body = approval._format_tool_body(
            "mcp__claude_ai_Gmail__send_message",
            {"to": ["hr@example.com"], "subject": "DevOps", "body": "Hello",
             "attachments": [{"filename": "cv.pdf", "content": "QUJD" * 1000}]},
            None)
        self.assertIn("hr@example.com", body)
        self.assertIn("cv.pdf", body)
        self.assertNotIn("QUJD", body)

    def test_all_text_fields_account_and_clip_warning_shown(self) -> None:
        body = approval._format_tool_body(
            "mcp__claude_ai_Gmail__send_message",
            {"to": ["a@b.c"], "body": "plain", "htmlBody": "<p>other</p>", "account": "work"},
            None)
        self.assertIn("plain", body)
        self.assertIn("&lt;p&gt;other", body)
        self.assertIn("work", body)
        self.assertNotIn(_("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>"), body)
        long = approval._format_tool_body(
            "mcp__telegram__send_message", {"chat_id": "@x", "message": "a" * 3000}, None)
        self.assertIn(_("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>"), long)

    def test_full_view_hides_attachment_content(self) -> None:
        text = approval.full_view_text(
            "mcp__claude_ai_Gmail__send_message",
            {"to": ["a@b.c"], "body": "Hello",
             "attachments": [{"filename": "cv.pdf", "content": "QUJD" * 1000}]})
        self.assertIn("cv.pdf", text)
        self.assertIn("Hello", text)
        self.assertNotIn("QUJD", text)

    def test_outbound_detection(self) -> None:
        self.assertTrue(approval._is_outbound_send("mcp__telegram__send_message"))
        self.assertTrue(approval._is_outbound_send("mcp__claude_ai_Gmail__reply"))
        self.assertFalse(approval._is_outbound_send("mcp__telegram__get_messages"))
        self.assertFalse(approval._is_outbound_send("mcp__claude_ai_Gmail__create_draft"))
        self.assertFalse(approval._is_outbound_send("mcp__other__send_message"))


class OutboundCallbackTests(unittest.TestCase):
    """Кнопки карточки отправки: окно ⏩ не открывается, чужое сообщение
    решения не принимает, своя ✅ разрешает."""

    def _press(self, decision: str, *, message_id: int = 10) -> object:
        async def go() -> object:
            registry = approval.ApprovalRegistry()
            pending = registry.create(
                tool_name="mcp__telegram__send_message",
                tool_input={"chat_id": "@x", "message": "hi"},
                session_id="sid", target_chat_id=1, target_thread_id=None)
            pending.telegram_message_id = 10
            cfg = SimpleNamespace(allowed_user_ids={7}, primary_chat_id=1)
            msg = SimpleNamespace(message_id=message_id, chat_id=1, is_topic_message=False,
                                  reply_text=AsyncMock(), text="card", text_html="card")
            query = SimpleNamespace(data=f"appr:{pending.approval_id}:{decision}", message=msg,
                                    answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                                    edit_message_text=AsyncMock())
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                                     effective_chat=SimpleNamespace(id=1))
            ctx = SimpleNamespace(application=SimpleNamespace(
                bot_data={"cfg": cfg, "registry": registry}), bot_data={})
            try:
                await bot.on_callback(update, ctx)
            except Exception:
                pass
            return pending.future.result() if pending.future.done() else None
        return asyncio.run(go())

    def test_own_card_approve_allows(self) -> None:
        self.assertEqual(self._press("allow"), "allow")

    def test_burst_button_does_not_allow_outbound(self) -> None:
        self.assertIsNone(self._press("auto15"))
        self.assertIsNone(self._press("auto60"))

    def test_press_from_other_message_ignored(self) -> None:
        self.assertIsNone(self._press("allow", message_id=11))

if __name__ == "__main__":
    unittest.main()

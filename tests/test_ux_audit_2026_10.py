"""Аудит удобства 31.10.2026: лишние сообщения на один ход.

Правило Ивана: каждый ответ несёт только новое. Нажатие на карточке, /cancel
и «стоп» раньше давали по одному-два лишних сообщения со звуком.
"""

from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import approval, bot, replies  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402


def _card_press(decision: str, *, card_html: str = "🟠 <b>Меняет:</b> файлы\n<code>ls</code>",
                edit_fails: bool | BaseException = False):
    async def go():
        registry = approval.ApprovalRegistry()
        pending = registry.create(tool_name="Bash", tool_input={"command": "touch x"},
                                  session_id="sid", target_chat_id=1, target_thread_id=None)
        pending.telegram_message_id = 10
        pending.card_text = "card"
        # Клавиатура, которую нарисовал бот: на ✅ сверяется с сообщением.
        kb = SimpleNamespace(inline_keyboard=[[SimpleNamespace(text="✅ Approve", callback_data="x")]])
        pending.card_keyboard = approval.keyboard_rows(kb)
        cfg = SimpleNamespace(allowed_user_ids={7}, primary_chat_id=1)
        msg = SimpleNamespace(message_id=10, chat_id=1, is_topic_message=False,
                              message_thread_id=None, reply_text=AsyncMock(),
                              text="card", text_html=card_html, reply_markup=kb)
        edit = AsyncMock(side_effect=(edit_fails if isinstance(edit_fails, BaseException)
                                      else RuntimeError("edit failed")) if edit_fails else None)
        query = SimpleNamespace(data=f"appr:{pending.approval_id}:{decision}", message=msg,
                                answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                                edit_message_text=edit)
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                                 effective_chat=SimpleNamespace(id=1))
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg, "registry": registry}),
                              bot_data={})
        with patch.object(bot, "burst_set", return_value=0.0):
            await bot.on_callback(update, ctx)
        return pending, msg, query
    return asyncio.run(go())


class CardDecisionInPlaceTests(unittest.TestCase):
    """Решение по карточке дописывается в саму карточку, а не новым
    сообщением «✅ allow · aid=…» со звуком."""

    def test_allow_edits_card_without_new_message(self) -> None:
        pending, msg, query = _card_press("allow")
        self.assertEqual(pending.future.result(), "allow")
        msg.reply_text.assert_not_awaited()
        query.edit_message_text.assert_awaited_once()
        text = query.edit_message_text.await_args.args[0]
        self.assertTrue(text.startswith("🟠 <b>Меняет:</b>"), text)
        self.assertIn(_("✅ одобрено"), text)
        self.assertEqual(query.edit_message_text.await_args.kwargs.get("parse_mode"), "HTML")

    def test_deny_edits_card(self) -> None:
        pending, msg, query = _card_press("deny")
        self.assertEqual(pending.future.result(), "deny")
        msg.reply_text.assert_not_awaited()
        self.assertIn(_("❌ отклонено"), query.edit_message_text.await_args.args[0])

    def test_edit_failure_falls_back_to_reply(self) -> None:
        pending, msg, query = _card_press("allow", edit_fails=True)
        self.assertEqual(pending.future.result(), "allow")
        query.edit_message_reply_markup.assert_awaited()
        msg.reply_text.assert_awaited_once()

    def test_unconfirmed_edit_no_reply(self) -> None:
        # Правка без подтверждения Telegram обычно прошла: строка решения
        # отдельным сообщением была бы вторым показом (ревью Codex 05.11.2026)
        import httpx
        from telegram.error import TimedOut
        err = TimedOut()
        err.__cause__ = httpx.ReadTimeout("")
        pending, msg, query = _card_press("allow", edit_fails=err)
        self.assertEqual(pending.future.result(), "allow")
        msg.reply_text.assert_not_awaited()

    def test_abandoned_card_unconfirmed_edit_no_reply(self) -> None:
        import httpx
        from telegram.error import TimedOut
        err = TimedOut()
        err.__cause__ = httpx.ReadTimeout("")

        async def go():
            registry = approval.ApprovalRegistry()
            p = registry.create(tool_name="Bash", tool_input={"command": "x"}, session_id="sid",
                                target_chat_id=1, target_thread_id=None)
            p.telegram_message_id = 5
            p.card_html = "карточка"
            fake_bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=err),
                                       edit_message_reply_markup=AsyncMock(), send_message=AsyncMock())
            ctx = SimpleNamespace(bot=fake_bot, application=SimpleNamespace(bot_data={
                "cfg": SimpleNamespace(primary_chat_id=1), "registry": registry}))
            await bot._cleanup_abandoned_approvals(ctx, "sid", "deny:claude-exited", "🚫 x")
            return fake_bot
        fake_bot = asyncio.run(go())
        fake_bot.send_message.assert_not_awaited()

    def test_timeout_mark_unconfirmed_edit_no_reply(self) -> None:
        import httpx
        from telegram.error import TimedOut
        err = TimedOut()
        err.__cause__ = httpx.ReadTimeout("")
        async def go():
            registry = approval.ApprovalRegistry()
            p = registry.create(tool_name="Bash", tool_input={"command": "x"}, session_id="sid",
                                target_chat_id=1, target_thread_id=None)
            p.telegram_message_id = 5
            p.card_html = "карточка"
            fake_bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=err),
                                       edit_message_reply_markup=AsyncMock(), send_message=AsyncMock())
            await approval._mark_card_timeout(SimpleNamespace(bot=fake_bot), p, 1, None, 600)
            return fake_bot
        fake_bot = asyncio.run(go())
        fake_bot.send_message.assert_not_awaited()

    def test_card_at_limit_falls_back_to_reply(self) -> None:
        pending, msg, query = _card_press("allow", card_html="x" * 4090)
        query.edit_message_text.assert_not_awaited()
        msg.reply_text.assert_awaited_once()



class CostFooterSilentTests(unittest.TestCase):
    """Футер с расходом идёт сразу за ответом: второй звук на один ход лишний."""

    def test_footer_without_sound(self) -> None:
        cfg = SimpleNamespace(show_cost_footer=True, context_warn_tokens=0, cost_footer_min_usd=0.0)
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=msg)
        result = ClaudeResult(text="ok", cost_usd=0.5, session_cost_usd=1.0)
        asyncio.run(replies.send_cost_footer(cfg, update, result))
        msg.reply_text.assert_awaited_once()
        self.assertIs(msg.reply_text.await_args.kwargs.get("disable_notification"), True)


class HelpCommandTests(unittest.TestCase):
    def test_help_registered_as_start(self) -> None:
        # build_app тянет конфиг и сеть: вызовы CommandHandler сверяются по тексту модуля.
        src = Path(bot.__file__).read_text(encoding="utf-8")
        found = re.findall(r'CommandHandler\("(\w+)",\s*on_start\b', src)
        self.assertIn("help", found)

    def test_start_text_matches_current_cards(self) -> None:
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=7))
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": SimpleNamespace()}))
        with patch.object(bot, "_is_allowed", return_value=True):
            asyncio.run(bot.on_start(update, ctx))
        text = msg.reply_text.await_args.args[0]
        # Устаревшее: карточка на каждую команду и «итоговый план» темы задачи.
        self.assertNotIn("destructive", text)
        self.assertNotIn("итоговый план", text)
        self.assertIn("«стоп»", text)
        self.assertIn("📋", text)


class UsageLimitLineTests(unittest.TestCase):
    """/usage называет долю окон подписки, которую бот и так знает."""

    def test_line_from_snapshot(self) -> None:
        with patch.object(bot.limits, "summary", return_value="5 ч 62%, неделя 40%"):
            line = bot._usage_limit_line()
        self.assertIn("5 ч 62%, неделя 40%", line)

    def test_no_snapshot_no_line(self) -> None:
        with patch.object(bot.limits, "summary", return_value=""):
            self.assertEqual(bot._usage_limit_line(), "")


class UserStopTests(unittest.TestCase):
    def _ctx(self, proc):
        bd = {
            "cfg": SimpleNamespace(workshop_chat_key=""),
            "procs": {"1": proc}, "sessions": SimpleNamespace(get=lambda k: "sid"),
            "active_sessions": {"1": "sid"}, "registry": approval.ApprovalRegistry(),
        }
        return SimpleNamespace(application=SimpleNamespace(bot_data=bd), args=[])

    def test_cancel_ack_is_plain_and_marks_stop(self) -> None:
        async def go():
            proc = SimpleNamespace(pid=4242, returncode=None)
            ctx = self._ctx(proc)
            ctx.application.bot_data["run_stops"] = {"1": bot.RunStop()}
            with patch.object(bot, "_terminate_and_wait", AsyncMock()), \
                    patch.object(bot, "_track_cancel_task", lambda app, t: None):
                text = await bot._cancel_running(ctx, "1", proc)
                await asyncio.sleep(0)
            return text, ctx
        text, ctx = asyncio.run(go())
        self.assertNotIn("SIGTERM", text)
        self.assertNotIn("pid", text)
        # Отметка у рана, а не у темы: следующий ран темы заводит свою.
        self.assertTrue(ctx.application.bot_data["run_stops"]["1"].stopped)

    def test_stopped_reply_suppressed_only_for_signal(self) -> None:
        stopped = ClaudeResult(text="⛔", is_error=True, signalled=True)
        other = ClaudeResult(text="⚠️ claude error", is_error=True)
        self.assertTrue(bot._quiet_after_user_stop(stopped, True))
        self.assertFalse(bot._quiet_after_user_stop(stopped, False))
        self.assertFalse(bot._quiet_after_user_stop(other, True))

    def test_abandoned_card_on_cancel_no_extra_message(self) -> None:
        n, fake_bot = self._abandon("deny:cancelled-by-user")
        self.assertEqual(n, 1)
        fake_bot.edit_message_reply_markup.assert_awaited_once()
        fake_bot.send_message.assert_not_awaited()

    def test_abandoned_card_after_run_end_quiet_reply(self) -> None:
        n, fake_bot = self._abandon("deny:claude-exited")
        kw = fake_bot.send_message.await_args.kwargs
        self.assertIs(kw.get("disable_notification"), True)
        self.assertEqual(kw.get("reply_to_message_id"), 5)
        self.assertNotIn("aid", kw.get("text"))

    def _abandon(self, reason: str):
        async def go():
            registry = approval.ApprovalRegistry()
            p = registry.create(tool_name="Bash", tool_input={"command": "x"}, session_id="sid",
                                target_chat_id=1, target_thread_id=None)
            p.telegram_message_id = 5
            fake_bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(), send_message=AsyncMock())
            ctx = SimpleNamespace(bot=fake_bot, application=SimpleNamespace(bot_data={
                "cfg": SimpleNamespace(primary_chat_id=1), "registry": registry}))
            n = await bot._cleanup_abandoned_approvals(ctx, "sid", reason, "🚫 x")
            return n, fake_bot
        return asyncio.run(go())

    def test_nosend_single_message(self) -> None:
        async def go():
            msg = SimpleNamespace(text="ничего не отправляй", reply_text=AsyncMock(),
                                  reply_to_message=None, forward_origin=None)
            update = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=7),
                                     effective_chat=SimpleNamespace(id=1))
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": SimpleNamespace()}))
            cancel = AsyncMock(return_value=True)
            with patch.object(bot, "_is_allowed", return_value=True), \
                    patch.object(bot, "_handoff_reply", AsyncMock(return_value=False)), \
                    patch.object(bot, "_something_to_stop", return_value=True), \
                    patch.object(bot, "_chat_key", return_value=("1", 1, None)), \
                    patch.object(bot, "on_cancel", cancel):
                await bot.on_text(update, ctx)
            return msg, cancel
        msg, cancel = asyncio.run(go())
        cancel.assert_awaited_once()
        self.assertTrue(cancel.await_args.kwargs.get("nosend"))
        msg.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

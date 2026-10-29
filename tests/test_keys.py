"""Кнопки под полем ввода (/keys, 29.10.2026): «Новый диалог» сбрасывает
сессию без Claude, в занятой теме сначала спрашивает; «Стоп» и «Статус» зовут
/cancel и /status; текст кнопки не уходит в on_text."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from telegram import (Chat, InlineKeyboardMarkup, Message, MessageOriginUser,  # noqa: E402
                      ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, User)
from telegram.ext import MessageHandler  # noqa: E402

from claude_tg_bot import bot as botmod  # noqa: E402
from tests.test_review_bot_main import GROUP, USER, _Base, _msg, _update  # noqa: E402

KEY = str(GROUP)


def _label(action: str) -> str:
    return next(botmod._(label) for label, a in botmod.KEY_BUTTONS if a == action)


def _pick(data: str, *, user: int = USER) -> SimpleNamespace:
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user))


def _proc(pid: int) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, returncode=None)


class KeyButtonTests(_Base):
    async def press(self, action: str, *, thread: int | None = None, user: int = USER) -> SimpleNamespace:
        msg = _msg(_label(action), thread=thread)
        await botmod.on_key_button(_update(msg, user=user), self.ctx)
        return msg

    async def busy(self, key: str = KEY, pid: int = 42) -> SimpleNamespace:
        """Тема занята живым раном: lock взят, процесс в procs."""
        lock = botmod._chat_lock(self.ctx, key)
        await lock.acquire()
        self.addCleanup(lock.release)
        proc = _proc(pid)
        self.bot_data["procs"][key] = proc
        return proc

    def ask_data(self, msg: SimpleNamespace) -> list[str]:
        markup = msg.reply_text.await_args.kwargs["reply_markup"]
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        return [b.callback_data for b in markup.inline_keyboard[0]]

    async def test_new_chat_in_idle_topic_resets_without_claude(self) -> None:
        self.store.set(KEY, "sid-old")
        msg = await self.press("reset")
        self.assertIsNone(self.store.get(KEY))
        self.assertEqual(botmod._session_epoch(self.ctx, KEY), 1)
        self.assertEqual(self.runs, [])
        msg.reply_text.assert_awaited_once()
        self.assertIsNone(msg.reply_text.await_args.kwargs.get("reply_markup"))

    async def test_stranger_button_does_nothing(self) -> None:
        self.store.set(KEY, "sid-old")
        msg = await self.press("reset", user=USER + 1)
        self.assertEqual(self.store.get(KEY), "sid-old")
        msg.reply_text.assert_not_awaited()

    async def test_busy_topic_asks_then_kills_and_resets_once(self) -> None:
        self.store.set(KEY, "sid-old")
        proc = await self.busy()
        msg = await self.press("reset")
        self.assertEqual(self.store.get(KEY), "sid-old")
        reset_data, keep_data = self.ask_data(msg)
        self.assertRegex(reset_data, r"^keys:reset:[0-9a-f]{8}$")
        self.assertEqual(keep_data, "keys:keep:" + reset_data.rsplit(":", 1)[1])

        with patch.object(botmod, "_terminate_and_wait", AsyncMock()) as term:
            first = _pick(reset_data)
            await botmod.on_keys_pick(first, self.ctx)
            term.assert_awaited_once_with(KEY, proc)
            self.assertIsNone(self.store.get(KEY))
            self.assertEqual(botmod._session_epoch(self.ctx, KEY), 1)

            # Второе нажатие того же вопроса: токен уже снят, новую сессию не трогает.
            self.store.set(KEY, "sid-new")
            again = _pick(reset_data)
            await botmod.on_keys_pick(again, self.ctx)
            term.assert_awaited_once()
        self.assertEqual(self.store.get(KEY), "sid-new")
        self.assertEqual(botmod._session_epoch(self.ctx, KEY), 1)
        again.callback_query.edit_message_text.assert_awaited_once_with(
            botmod._("Вопрос устарел: нажми «🔄 Новый диалог» ещё раз"))

    async def test_keep_leaves_session(self) -> None:
        self.store.set(KEY, "sid-old")
        await self.busy()
        msg = await self.press("reset")
        _reset, keep_data = self.ask_data(msg)
        pick = _pick(keep_data)
        await botmod.on_keys_pick(pick, self.ctx)
        self.assertEqual(self.store.get(KEY), "sid-old")
        pick.callback_query.edit_message_text.assert_awaited_once_with(botmod._("Оставил как есть"))

    async def test_question_about_finished_run_does_not_kill_next_one(self) -> None:
        self.store.set(KEY, "sid-old")
        await self.busy(pid=42)
        msg = await self.press("reset")
        reset_data, _keep = self.ask_data(msg)
        self.bot_data["procs"][KEY] = _proc(43)
        with patch.object(botmod, "_terminate_and_wait", AsyncMock()) as term:
            await botmod.on_keys_pick(_pick(reset_data), self.ctx)
        term.assert_not_awaited()
        self.assertEqual(self.store.get(KEY), "sid-old")

    async def test_question_after_other_reset_or_restart_is_refused(self) -> None:
        self.store.set(KEY, "sid-old")
        await self.busy()
        msg = await self.press("reset")
        reset_data, _keep = self.ask_data(msg)
        botmod._bump_session_epoch(self.ctx, KEY)
        stale = _pick(reset_data)
        await botmod.on_keys_pick(stale, self.ctx)
        self.assertEqual(self.store.get(KEY), "sid-old")
        stale.callback_query.edit_message_text.assert_awaited_once_with(botmod._("Сессия уже сброшена"))

        # Рестарт: реестр вопросов в памяти пуст, старая кнопка ничего не делает.
        msg = await self.press("reset")
        reset_data, _keep = self.ask_data(msg)
        self.bot_data["keys_resets"].clear()
        await botmod.on_keys_pick(_pick(reset_data), self.ctx)
        self.assertEqual(self.store.get(KEY), "sid-old")

    async def test_forum_topic_question_resets_that_topic(self) -> None:
        topic = f"{GROUP}:7"
        self.store.set(KEY, "sid-general")
        self.store.set(topic, "sid-topic")
        await self.busy(topic)
        msg = await self.press("reset", thread=7)
        reset_data, _keep = self.ask_data(msg)
        with patch.object(botmod, "_terminate_and_wait", AsyncMock()):
            await botmod.on_keys_pick(_pick(reset_data), self.ctx)
        self.assertIsNone(self.store.get(topic))
        self.assertEqual(self.store.get(KEY), "sid-general")

    async def test_pick_from_stranger_is_ignored(self) -> None:
        self.store.set(KEY, "sid-old")
        await self.busy()
        msg = await self.press("reset")
        reset_data, _keep = self.ask_data(msg)
        with patch.object(botmod, "_terminate_and_wait", AsyncMock()) as term:
            await botmod.on_keys_pick(_pick(reset_data, user=USER + 1), self.ctx)
        term.assert_not_awaited()
        self.assertEqual(self.store.get(KEY), "sid-old")

    async def test_stop_and_status_call_their_commands(self) -> None:
        with patch.object(botmod, "on_cancel", AsyncMock()) as cancel, \
                patch.object(botmod, "on_status", AsyncMock()) as status:
            await self.press("cancel")
            cancel.assert_awaited_once()
            status.assert_not_awaited()
            await self.press("status")
            status.assert_awaited_once()
        self.assertEqual(self.runs, [])

    async def test_keys_off_removes_keyboard(self) -> None:
        msg = _msg("/keys off")
        self.ctx.args = ["off"]
        await botmod.on_keys(_update(msg), self.ctx)
        self.assertIsInstance(msg.reply_text.await_args.kwargs["reply_markup"], ReplyKeyboardRemove)

    async def test_keys_shows_collapsible_keyboard(self) -> None:
        msg = _msg("/keys")
        await botmod.on_keys(_update(msg), self.ctx)
        markup = msg.reply_text.await_args.kwargs["reply_markup"]
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        self.assertFalse(markup.is_persistent)
        self.assertTrue(markup.resize_keyboard)
        self.assertEqual([b.text for b in markup.keyboard[0]],
                         [_label("reset"), _label("cancel"), _label("status")])


def _text_update(text: str, forwarded: bool) -> Update:
    now = datetime.now(timezone.utc)
    origin = MessageOriginUser(now, User(id=8, first_name="Петя", is_bot=False)) if forwarded else None
    msg = Message(message_id=5, date=now, chat=Chat(id=USER, type=Chat.PRIVATE),
                  from_user=User(id=USER, first_name="Иван", is_bot=False), text=text,
                  forward_origin=origin)
    return Update(update_id=1, message=msg)


class KeyRoutingTests(unittest.TestCase):
    def test_button_handler_precedes_text_and_skips_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(telegram_bot_token="1:test", claude_bin="claude", codex_bin="",
                                  claude_cwd=Path(d), state_dir=Path(d))
            app = botmod.build_app(cfg)
        handlers = app.handlers[0]
        key_i = next(i for i, h in enumerate(handlers) if getattr(h, "callback", None) is botmod.on_key_button)
        text_i = next(i for i, h in enumerate(handlers) if getattr(h, "callback", None) is botmod.on_text)
        first_message = next(i for i, h in enumerate(handlers)
                             if isinstance(h, MessageHandler) and h.callback in
                             (botmod.on_voice, botmod.on_photo, botmod.on_file, botmod.on_text))
        self.assertLess(key_i, first_message)
        self.assertLess(key_i, text_i)
        key_h, text_h = handlers[key_i], handlers[text_i]
        own, forwarded = _text_update(_label("reset"), False), _text_update(_label("reset"), True)
        self.assertTrue(key_h.filters.check_update(own))
        self.assertFalse(key_h.filters.check_update(forwarded))
        self.assertTrue(text_h.filters.check_update(forwarded))
        self.assertFalse(key_h.filters.check_update(_text_update(_label("reset") + ", пожалуйста", False)))


if __name__ == "__main__":
    unittest.main()

"""Неподтверждённая отправка (replies.is_unconfirmed) не даёт повтора того же
сообщения: карточки дел и вопросов, notify и новости, пробуждения, срок входа,
статус рана, handoff и otklik. Определённые отказы ведут себя как раньше."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import BadRequest, TimedOut

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot import deals, events, handoff, login_expiry as le, notify, otklik, tg_cards, tg_deals, tg_events  # noqa: E402
from claude_tg_bot.skills.news_digest import __main__ as news  # noqa: E402


def _unconfirmed(cause: Exception | None = None) -> TimedOut:
    err = TimedOut()
    err.__cause__ = cause or httpx.ReadTimeout("")
    return err


# ---- 1. карточка дела ----

class DealCardTests(unittest.IsolatedAsyncioTestCase):
    async def _open(self, error: Exception):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = SimpleNamespace(state_dir=Path(tmp.name))
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}), bot=SimpleNamespace())
        msg = SimpleNamespace(reply_text=AsyncMock(side_effect=error))
        ok = await tg_deals._deal_open(ctx, msg, 1, None, ("Звонить?", ["да", "нет"]),
                                       qsid="s", quuid="u", qepoch=0)
        with deals.locked(deals.deals_dir(Path(tmp.name))) as data:
            states = [d["state"] for d in data.values()]
        return ok, states

    async def test_unconfirmed_card_keeps_deal_open(self) -> None:
        ok, states = await self._open(_unconfirmed())
        self.assertTrue(ok, "False отправил бы тот же вопрос обычной карточкой")
        self.assertEqual(states, ["asked"])

    async def test_definite_failure_still_closes_deal(self) -> None:
        ok, states = await self._open(BadRequest("nope"))
        self.assertFalse(ok)
        self.assertEqual(states, ["closed"])


# ---- 2. карточка вопроса ----

class AskCardTests(unittest.IsolatedAsyncioTestCase):
    async def _send(self, error: Exception):
        old = {"chat_key": "7", "answer": "старый", "question": "Старый?", "card": "c0",
               "epoch": 0, "at": time.time(), "msg": (7, 50, None)}
        bot_data = {"ask_options": {"o": old}}
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data=bot_data),
                              bot=SimpleNamespace(edit_message_text=AsyncMock()))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock(side_effect=error)))
        with patch.object(botmod, "_session_epoch", return_value=0):
            await tg_cards._send_ask_card(ctx, update, "7", ("Новый?", ["да"]),
                                          permission_mode="default", epoch=0)
        return bot_data["ask_options"], ctx.bot.edit_message_text

    async def test_unconfirmed_card_stays_in_registry(self) -> None:
        pending, edit = await self._send(_unconfirmed())
        self.assertEqual([v["question"] for v in pending.values()], ["Новый?"])
        edit.assert_awaited_once()  # старая погашена, как при доставленной новой

    async def test_definite_failure_restores_old_card(self) -> None:
        pending, _edit = await self._send(BadRequest("nope"))
        self.assertEqual([v["question"] for v in pending.values()], ["Старый?"])


# ---- 3. notify и новости ----

def _cfg():
    return SimpleNamespace(primary_chat_id=111, telegram_bot_token="TOKEN", telegram_text_limit=4096,
                           allowed_user_ids=frozenset({111}), allowed_chat_ids=frozenset(), state_dir=None)


def _ok(mid: int):
    return SimpleNamespace(status_code=200, text="ok", json=lambda: {"ok": True, "result": {"message_id": mid}})


class NotifyTests(unittest.TestCase):
    def _run(self, argv, effect):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(notify, "load_config", return_value=_cfg()), \
                patch.object(notify.httpx, "post", side_effect=effect) as post, \
                patch.object(notify.tg_api.time, "sleep"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc, mid = notify._run(argv)
        return rc, mid, post

    def test_pin_with_unconfirmed_send_not_repeated(self) -> None:
        # Код 1 без message_id: промт Домового читает его «повторно не слать».
        for cause in (httpx.ReadTimeout(""),):
            with self.subTest(cause=type(cause).__name__):
                rc, mid, post = self._run(["--pin", "текст"], [cause])
                self.assertEqual((rc, mid), (1, None))
                self.assertEqual(post.call_count, 1, "ни повтора, ни закрепа без номера")

    def test_connect_error_is_still_failure(self) -> None:
        rc, _mid, _post = self._run(["--pin", "текст"], [httpx.ConnectError("x")] * 3)
        self.assertEqual(rc, 1)

    def test_digest_send_counts_unconfirmed_as_delivered(self) -> None:
        with patch.object(news, "send_message", return_value=(0, None)):
            self.assertIsNotNone(news._send(1, 2, "пункт"))
        with patch.object(news, "send_message", return_value=(1, None)):
            self.assertIsNone(news._send(1, 2, "пункт"))


# ---- 4. события и срок входа ----

class EventCutTests(unittest.IsolatedAsyncioTestCase):
    async def _resume(self, error: Exception):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        store = events.event_dir(state)
        rec = events.new_record(key=None, chat_id=-100, thread_id=7, prompt="p", fresh=False,
                                untrusted=False, silent=False, source="Домовой", title="Утро", ttl=3600)
        events.reserve(store, rec)
        events.update(store, rec["id"], status="started")
        cfg = SimpleNamespace(state_dir=state, primary_chat_id=1, workshop_chat_key="")
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=error)),
                              bot_data={"cfg": cfg})
        await tg_events._events_resume(app)
        return events.load(store, rec["id"])

    async def test_unconfirmed_cut_note_is_marked_done(self) -> None:
        rec = await self._resume(_unconfirmed())
        self.assertEqual((rec["status"], rec["outcome"]), ("done", "interrupted"))

    async def test_definite_failure_leaves_cut_for_next_start(self) -> None:
        rec = await self._resume(BadRequest("nope"))
        self.assertEqual(rec["status"], "started")


class LoginExpiryTests(unittest.TestCase):
    def _check(self, error: Exception) -> dict:
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            cfg = SimpleNamespace(claude_bin="claude", state_dir=state_dir, primary_chat_id=42)
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}),
                                  bot=SimpleNamespace(send_message=AsyncMock(side_effect=error), username="b"))
            expires = time.time() + 5 * 86400
            with patch.object(le, "read_login_status", AsyncMock(return_value=le.LoginStatus(True, expires))):
                asyncio.run(le.check_login_expiry(ctx))
            return le.load_state(state_dir / le.STATE_FILE)

    def test_unconfirmed_notice_is_marked_sent(self) -> None:
        self.assertIn("3", self._check(_unconfirmed())["sent"])

    def test_definite_failure_is_not_marked(self) -> None:
        self.assertNotIn("3", self._check(BadRequest("nope"))["sent"])


# ---- 5. статус рана ----

class RunStatusTests(unittest.IsolatedAsyncioTestCase):
    def _status(self, bot):
        return botmod._RunStatus(bot, -100, 5, silence_alert_sec=1, progress_note_sec=1,
                                 clock=lambda: 1000.0)

    async def test_unconfirmed_status_is_not_recreated(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=_unconfirmed()))
        st = self._status(bot)
        await st.on_tool("a")
        st._last_edit = 0.0
        st._status_pause_until = 0.0
        await st.on_tool("b")
        st._status_pause_until = 0.0
        await st.on_tool("c")
        self.assertEqual(bot.send_message.await_count, 1)

    async def test_definite_failure_still_retried(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=BadRequest("nope")))
        st = self._status(bot)
        await st.on_tool("a")
        st._status_pause_until = 0.0
        await st.on_tool("b")
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_loud_unconfirmed_is_not_repeated(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=_unconfirmed()))
        st = self._status(bot)
        mid, left = await st._send_loud("тревога", 4)
        self.assertIsNone(mid)
        self.assertLessEqual(left, 0, "эпизод закрыт: тик не пошлёт то же со звуком снова")

    async def test_loud_definite_failure_keeps_tries(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=BadRequest("nope")))
        st = self._status(bot)
        _mid, left = await st._send_loud("тревога", 4)
        self.assertEqual(left, 3)


# ---- 6. handoff и otklik ----

class HandoffTests(unittest.TestCase):
    def _ask(self, effect):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            args = argparse.Namespace(reason="капча", site="hh.ru", quiet=False, image="", region="")
            env = {"SCREEN_PUBLIC_BASE": "https://h", "CLAUDE_TG_CHAT_KEY": "5", "AGENT_BROWSER_SESSION": "s1"}
            err = io.StringIO()
            with patch.dict("os.environ", env), patch.object(handoff, "stream_port", return_value=1), \
                    patch("claude_tg_bot.config.load_config", return_value=_cfg()), \
                    patch.object(httpx, "post", side_effect=effect) as post, \
                    patch("claude_tg_bot.tg_api.time.sleep"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = handoff._cmd_ask(args, base, 1)
            recs = [handoff.load(base, p.stem) for p in base.glob("*.json")]
            return rc, recs, post

    def test_unconfirmed_send_is_failure(self) -> None:
        # Без номера кнопки не прикрепить: запись закрыта, повтор с кнопками
        # нужен; открытая без кнопок висела бы до срока.
        rc, recs, post = self._ask([httpx.ReadTimeout("")])
        self.assertEqual(rc, 1)
        self.assertNotIn("open", [r["status"] for r in recs])
        self.assertEqual(post.call_count, 1)

    def test_definite_failure_closes_record(self) -> None:
        rc, recs, _post = self._ask([httpx.ConnectError("x")] * 3)
        self.assertEqual(rc, 1)
        self.assertNotIn("open", [r["status"] for r in recs])


class OtklikTests(unittest.TestCase):
    def _new(self, effect):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            text = base / "t.txt"
            text.write_text("Hello", encoding="utf-8")
            args = argparse.Namespace(title="Acme", channel="почта", file=str(text), replaces="")
            with patch("claude_tg_bot.config.load_config", return_value=_cfg()), \
                    patch.object(httpx, "post", side_effect=effect) as post, \
                    patch("claude_tg_bot.tg_api.time.sleep"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = otklik._cmd_new(args, base / "o", 1, "1", frozenset({"1"}))
            recs = [otklik.load(base / "o", p.stem) for p in (base / "o").glob("*.json")]
            return rc, recs, post

    def test_unconfirmed_send_is_failure(self) -> None:
        # Без номера кнопки не прикрепить: запись pending без кнопок никто
        # не одобрил бы, поэтому её нет и повтор нужен.
        rc, recs, post = self._new([httpx.ReadTimeout("")])
        self.assertEqual(rc, 1)
        self.assertEqual(recs, [])
        self.assertEqual(post.call_count, 1)

    def test_definite_failure_removes_record(self) -> None:
        rc, recs, _post = self._new([httpx.ConnectError("x")] * 3)
        self.assertEqual(rc, 1)
        self.assertEqual(recs, [])


if __name__ == "__main__":
    unittest.main()

"""Строка «⌛ Пробуждение устарело» всегда без звука, как на main. Оба места
вызова: срок вышел в очереди (_event_wake) и пока бот лежал (_events_resume).
Мастерская: после строки «устарело» вторая «🛠 ... не запустилось» не идёт, а
когда идёт (лимит, сбой), звук по тишине (silent) события."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from claude_tg_bot import bot as botmod
from claude_tg_bot import events, tg_events

WORKSHOP = "-100:7"


def _app(state_dir: Path, workshop: str = "") -> SimpleNamespace:
    cfg = SimpleNamespace(state_dir=state_dir, primary_chat_id=1, workshop_chat_key=workshop)
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), bot_data={"cfg": cfg})


def _rec(silent: bool | None, **kw) -> dict:
    rec = events.new_record(key=None, chat_id=-100, thread_id=7, prompt="p", fresh=False,
                            untrusted=False, silent=bool(silent), source="Домовой",
                            title="Утро", ttl=60, **kw)
    if silent is None:
        rec.pop("silent")
    return rec


class ExpiredNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def _note(self, rec: dict, workshop: str = "") -> AsyncMock:
        app = _app(self.state, workshop)
        self.assertTrue(asyncio.run(tg_events._event_expired_note(app, rec)))
        return app.bot.send_message

    def test_note_always_muted(self) -> None:
        for silent in (True, False, None):
            with self.subTest(silent=silent):
                send = self._note(_rec(silent))
                send.assert_awaited_once()
                self.assertEqual(send.call_args.args[0], -100)
                self.assertEqual(send.call_args.kwargs["message_thread_id"], 7)
                self.assertIs(send.call_args.kwargs["disable_notification"], True)

    def test_workshop_note_goes_to_primary_muted(self) -> None:
        for silent in (True, False):
            with self.subTest(silent=silent):
                send = self._note(_rec(silent), workshop=WORKSHOP)
                self.assertEqual(send.call_args.args[0], 1)
                self.assertIsNone(send.call_args.kwargs["message_thread_id"])
                self.assertIs(send.call_args.kwargs["disable_notification"], True)

    def test_failed_or_missing_note_reports_false(self) -> None:
        app = _app(self.state)
        self.assertFalse(asyncio.run(tg_events._event_expired_note(app, None)))
        app.bot.send_message.side_effect = RuntimeError("down")
        self.assertFalse(asyncio.run(tg_events._event_expired_note(app, _rec(False))))

    def test_resume_stale_one_muted_message(self) -> None:
        """Срок вышел, пока бот лежал: одна строка при старте, без звука, и в
        обычном чате, и в мастерской."""
        for workshop in ("", WORKSHOP):
            for silent in (True, False):
                with self.subTest(workshop=workshop, silent=silent), tempfile.TemporaryDirectory() as tmp:
                    state = Path(tmp)
                    store = events.event_dir(state)
                    rec = _rec(silent, now=time.time() - 3600)
                    self.assertTrue(events.reserve(store, rec))
                    app = _app(state, workshop)
                    asyncio.run(tg_events._events_resume(app))
                    send = app.bot.send_message
                    send.assert_awaited_once()
                    self.assertEqual(send.call_args.args[0], 1 if workshop else -100)
                    self.assertIs(send.call_args.kwargs["disable_notification"], True)
                    self.assertEqual(events.load(store, rec["id"])["outcome"], "expired")


class QueueWakeTests(unittest.IsolatedAsyncioTestCase):
    """Срок вышел в очереди (_event_wake): сколько сообщений и со звуком ли."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.store = events.event_dir(self.state)
        self.dispatch = AsyncMock(return_value=None)
        for name, value in {"_dispatch_to_claude": self.dispatch,
                            "_synthetic_update": MagicMock(), "ContextTypes": MagicMock()}.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def _wake(self, rec: dict, workshop: str, check_live: bool = True,
                    boom: bool = False) -> AsyncMock:
        events.reserve(self.store, rec)

        async def fake(*_a, still_wanted=None, **_kw):
            if boom:
                raise RuntimeError("boom")
            if check_live and still_wanted is not None:
                still_wanted()
            return None

        self.dispatch.side_effect = fake
        app = _app(self.state, workshop)
        wake = tg_events._make_event_wake(app)
        await wake(rec["chat_id"], rec["thread_id"], rec["prompt"], silent=rec["silent"],
                   event_id=rec["id"])
        return app.bot.send_message

    async def test_expired_in_queue_one_muted_message(self) -> None:
        for workshop in ("", WORKSHOP):
            for silent in (True, False):
                with self.subTest(workshop=workshop, silent=silent):
                    rec = _rec(silent, now=1.0)
                    send = await self._wake(rec, workshop)
                    send.assert_awaited_once()
                    self.assertIn("⌛", send.call_args.args[1])
                    self.assertEqual(send.call_args.args[0], 1 if workshop else -100)
                    self.assertIs(send.call_args.kwargs["disable_notification"], True)
                    self.assertEqual(events.load(self.store, rec["id"])["outcome"], "expired")

    async def test_workshop_not_started_follows_silent(self) -> None:
        """Лимит или отказ: строка «🛠 не запустилось» осталась, звук по silent."""
        for silent in (True, False):
            with self.subTest(silent=silent):
                send = await self._wake(_rec(silent, now=None), WORKSHOP, check_live=False)
                send.assert_awaited_once()
                self.assertEqual(send.call_args.args[0], 1)
                self.assertIn("🛠", send.call_args.args[1])
                self.assertIs(send.call_args.kwargs["disable_notification"], silent)

    async def test_workshop_error_follows_silent(self) -> None:
        for silent in (True, False):
            with self.subTest(silent=silent):
                send = await self._wake(_rec(silent, now=None), WORKSHOP, boom=True)
                send.assert_awaited_once()
                self.assertIs(send.call_args.kwargs["disable_notification"], silent)

    async def test_workshop_expired_note_failed_sends_line(self) -> None:
        """Строка «устарело» не ушла: о сроке скажет строка мастерской."""
        rec = _rec(False, now=1.0)
        events.reserve(self.store, rec)

        async def fake(*_a, still_wanted=None, **_kw):
            still_wanted()

        self.dispatch.side_effect = fake
        app = _app(self.state, WORKSHOP)
        app.bot.send_message.side_effect = [RuntimeError("down"), None]
        await tg_events._make_event_wake(app)(-100, 7, "p", silent=False, event_id=rec["id"])
        self.assertEqual(app.bot.send_message.await_count, 2)
        self.assertIn("🛠", app.bot.send_message.call_args.args[1])

    async def test_ordinary_chat_not_started_no_message(self) -> None:
        send = await self._wake(_rec(False, now=None), "", check_live=False)
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

"""Неподтверждённая отправка (запрос ушёл, ответа нет, replies.is_unconfirmed)
в задачах-темах: сообщение обычно доходит, повтор уборкой или стартом даёт
дубль. Определённые отказы повторяются как прежде. Оснастка из test_task_topics."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from telegram.error import NetworkError, TimedOut

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_task_topics import GROUP, PLAN, SHA, THREAD, USER, _TaskBase, botmod, tasks  # noqa: E402


def unconfirmed() -> TimedOut:
    err = TimedOut()
    err.__cause__ = httpx.ReadTimeout("")
    return err


class UnconfirmedTests(_TaskBase):
    def fail_first(self, match) -> list:
        """Первая отправка, подходящая под match, неподтверждённая, дальше успех."""
        errs = [unconfirmed]

        async def send(chat_id, text, **kw):
            if match(chat_id, kw) and errs:
                raise errs.pop()()

        self.bot.send_message.side_effect = send
        return errs

    def sent(self, match) -> int:
        return sum(1 for c in self.bot.send_message.await_args_list if match(c.args[0], c.kwargs))

    async def test_result_post_not_repeated(self) -> None:
        self.put_command("t7-5", status="done", result="✅ Готово\nобновил README", result_ok=True,
                         result_unsent=True, result_at="2026-10-19T10:00:00+04:00")
        in_topic = lambda chat, kw: chat == GROUP and kw.get("message_thread_id") == THREAD  # noqa: E731
        self.fail_first(in_topic)
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_housekeeping(self.ctx)
        self.assertIsNone(self.task("t7-5")["result_unsent"])
        await botmod._tasks_housekeeping(self.ctx)
        await botmod._tasks_housekeeping(self.ctx, startup=True)
        self.assertEqual(self.sent(in_topic), 1)

    async def test_result_post_definite_failure_still_repeated(self) -> None:
        self.put_command("t7-5", status="done", result="✅ Готово\nобновил README", result_ok=True,
                         result_unsent=True, result_at="2026-10-19T10:00:00+04:00")
        in_topic = lambda chat, kw: chat == GROUP and kw.get("message_thread_id") == THREAD  # noqa: E731
        errs = [lambda: NetworkError("сеть")]

        async def send(chat_id, text, **kw):
            if in_topic(chat_id, kw) and errs:
                raise errs.pop()()

        self.bot.send_message.side_effect = send
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_housekeeping(self.ctx)
        self.assertTrue(self.task("t7-5")["result_unsent"])
        await botmod._tasks_housekeeping(self.ctx, startup=True)
        self.assertIsNone(self.task("t7-5")["result_unsent"])
        self.assertEqual(self.sent(in_topic), 2)

    def put_closing(self) -> None:
        self.put_command("t7-3", thread=73, status="done", result="✅ Готово", result_ok=True,
                         exec_started="2026-10-19T10:00:00+04:00", name="1 README",
                         origin={"chat_id": USER, "thread_id": None, "ask_id": 41})

    async def test_close_note_not_repeated(self) -> None:
        self.put_closing()
        to_user = lambda chat, kw: chat == USER  # noqa: E731
        self.fail_first(to_user)
        with self.assertLogs(botmod.log, "WARNING"):
            await self.press("nt:t7-3:close")
        self.assertIsNone(self.task("t7-3")["note_due"])
        self.assertTrue(self.task("t7-3")["noted"])
        with tasks.locked(tasks.registry_dir(self.state)) as data:
            data["t7-3"]["closed"] = (datetime.now().astimezone() - timedelta(minutes=11)).isoformat()
        await botmod._tasks_housekeeping(self.ctx)
        self.assertEqual(self.sent(to_user), 1)

    async def test_close_note_definite_failure_still_repeated(self) -> None:
        self.put_closing()
        to_user = lambda chat, kw: chat == USER  # noqa: E731
        errs = [lambda: NetworkError("сеть")]

        async def send(chat_id, text, **kw):
            if to_user(chat_id, kw) and errs:
                raise errs.pop()()

        self.bot.send_message.side_effect = send
        with self.assertLogs(botmod.log, "WARNING"):
            await self.press("nt:t7-3:close")
        self.assertTrue(self.task("t7-3")["note_due"])
        with tasks.locked(tasks.registry_dir(self.state)) as data:
            data["t7-3"]["closed"] = (datetime.now().astimezone() - timedelta(minutes=11)).isoformat()
        await botmod._tasks_housekeeping(self.ctx)
        self.assertEqual(self.sent(to_user), 2)

    async def test_new_card_not_resent_on_next_start(self) -> None:
        self.put_command("t7-5", status="thinking", card_id=None)
        card = lambda chat, kw: chat == GROUP and kw.get("message_thread_id") == THREAD  # noqa: E731
        self.fail_first(card)
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_recover(self.ctx)
        await botmod._tasks_recover(self.ctx)
        self.assertEqual(self.sent(card), 1)

    async def test_offer_do_not_repeated_on_next_start(self) -> None:
        self.put_command("t7-5", status="running", plan=PLAN, plan_sha=SHA, exec_gen=1)
        offer = lambda chat, kw: kw.get("reply_markup") is not None  # noqa: E731
        self.fail_first(offer)
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_recover(self.ctx)
        self.assertIsNone(self.task("t7-5")["offer_due"])
        await botmod._tasks_recover(self.ctx)
        self.assertEqual(self.sent(offer), 1)

    async def test_cut_thinking_notice_not_repeated(self) -> None:
        self.put_command("t7-5", status="thinking", think_open=True)
        notice = lambda chat, kw: chat == GROUP and kw.get("reply_markup") is None  # noqa: E731
        self.fail_first(notice)
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_recover(self.ctx)
        self.assertIsNone(self.task("t7-5").get("think_open"))
        await botmod._tasks_recover(self.ctx)
        self.assertEqual(self.sent(notice), 1)

    async def test_reminder_not_repeated(self) -> None:
        self.put_command("t7-5", status="thinking",
                         created=(datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds"))
        remind = lambda chat, kw: chat == GROUP and kw.get("reply_markup") is not None  # noqa: E731
        self.fail_first(remind)
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_housekeeping(self.ctx)
        self.assertTrue(self.task("t7-5").get("reminded"))
        await botmod._tasks_housekeeping(self.ctx)
        self.assertEqual(self.sent(remind), 1)

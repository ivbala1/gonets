"""Живой статус рана и сторож тишины (bot._RunStatus): шаг и тишина в
статусе, тревога после RUN_SILENCE_ALERT_SEC без событий с кнопками «Ждать» и
«Прервать», разовая весточка после RUN_PROGRESS_NOTE_SEC, уборка по
завершении рана. Часы подменяются, Telegram на SimpleNamespace, без сети."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot import claude_runner  # noqa: E402
from claude_tg_bot.i18n import _, plural  # noqa: E402
from tests.texts import msg_re  # noqa: E402

USER = 7


def status_line(desc: str, steps: int, total: int, silence: int | None = None) -> str:
    """Строка живого статуса, как её собирает _RunStatus._fmt (секунды до минуты)."""
    parts = [f"🔧 {desc}", _("шаг {steps}").format(steps=steps),
             _("всего {dur}").format(dur=_("{s}с").format(s=total))]
    if silence is not None:
        parts.append(_("тишина {dur}").format(dur=_("{s}с").format(s=silence)))
    return " · ".join(parts)


def silence_alert(mins: int, last: str | None = None) -> str:
    """Тревога тишины; last None: действий ещё не было."""
    return _("⏳ {mins} {unit} тишины: "
             "последнее действие {last}. Похоже, завис или долгая команда.").format(
        mins=mins, unit=plural(mins, ("минута", "минуты", "минут"), ("minute", "minutes")),
        last=last if last is not None else _("ещё не было"))


def progress_note(mins: int, steps: int, last: str | None = None) -> str:
    """Заметка «Работаю N минут»; last None: действий ещё не было."""
    return _("Работаю {mins} {unit}, "
             "шагов {steps}, сейчас: {last}").format(
        mins=mins, unit=plural(mins, ("минуту", "минуты", "минут"), ("minute", "minutes")),
        steps=steps, last=last if last is not None else _("ещё не было"))
CHAT = -100
THREAD = 5


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Wire:
    """Бот без сети: копит отправки, правки и удаления."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[int] = []
        self._next = 100

    async def send_message(self, **kw):
        self._next += 1
        self.sent.append({**kw, "message_id": self._next})
        return SimpleNamespace(message_id=self._next)

    async def edit_message_text(self, **kw):
        self.edits.append(kw)

    async def delete_message(self, **kw):
        self.deleted.append(kw["message_id"])

    def loud(self) -> list[dict]:
        return [m for m in self.sent if m.get("disable_notification") is False]


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = _Clock()
        self.wire = _Wire()
        self.registry: dict = {}
        self.waiting = False
        self.status = botmod._RunStatus(
            self.wire, CHAT, THREAD, chat_key=f"{CHAT}:{THREAD}",
            silence_alert_sec=600, progress_note_sec=900,
            registry=self.registry, waiting=lambda: self.waiting, clock=self.clock,
        )

    async def at(self, t: float) -> None:
        """Тик сторожа в момент t секунд от старта рана."""
        self.clock.t = 1000.0 + t
        await self.status._tick()

    def alerts(self) -> list[dict]:
        return [m for m in self.wire.loud() if m["text"].startswith("⏳")]


class StatusTextTests(_Base):
    async def test_step_total_and_silence_from_30s(self) -> None:
        await self.status.on_tool("Edit: prompt.md")
        self.clock.t += 2
        await self.status.on_tool("Bash: ls")
        self.assertEqual(len(self.wire.sent), 1)  # второе в окне троттлинга 3 с
        self.assertTrue(self.wire.sent[0]["disable_notification"])
        self.assertEqual(self.wire.sent[0]["message_thread_id"], THREAD)
        self.assertEqual(self.wire.sent[0]["text"], status_line("Edit: prompt.md", 1, 0))
        await self.at(2 + 29)
        self.assertEqual(self.wire.edits[-1]["text"], status_line("Bash: ls", 2, 31))
        await self.at(2 + 40)
        self.assertEqual(self.wire.edits[-1]["text"], status_line("Bash: ls", 2, 42, 40))

    async def test_tick_redraws_without_events_but_respects_throttle(self) -> None:
        await self.status.on_tool("Bash: make")
        await self.at(1)  # 1 с после отправки: правка не нужна
        self.assertEqual(self.wire.edits, [])
        await self.at(25)
        await self.at(50)
        self.assertEqual(len(self.wire.edits), 2)
        self.assertEqual(self.wire.edits[-1]["text"], status_line("Bash: make", 1, 50, 50))

    async def test_no_status_message_before_first_tool(self) -> None:
        await self.at(60)
        self.assertEqual(self.wire.sent, [])

    def test_plural(self) -> None:
        forms = ("минута", "минуты", "минут")
        self.assertEqual([botmod._RunStatus._plural(n, forms) for n in (1, 2, 5, 10, 11, 21, 22, 25)],
                         ["минута", "минуты", "минут", "минут", "минут", "минута", "минуты", "минут"])


class SilenceAlertTests(_Base):
    async def test_alert_after_silence_once_per_episode(self) -> None:
        await self.status.on_tool("Bash: pytest")
        await self.at(599)
        self.assertEqual(self.alerts(), [])
        await self.at(600)
        (alert,) = self.alerts()
        self.assertEqual(alert["text"], silence_alert(10, "Bash: pytest"))
        self.assertEqual(alert["message_thread_id"], THREAD)
        buttons = alert["reply_markup"].inline_keyboard[0]
        self.assertEqual([b.text for b in buttons], [_("Ждать"), _("Прервать")])
        rid = self.status.run_id
        self.assertEqual([b.callback_data for b in buttons],
                         [f"runwatch:wait:{rid}", f"runwatch:stop:{rid}"])
        # Тишина продолжается, но тревога на эпизод одна.
        await self.at(1300)
        self.assertEqual(len(self.alerts()), 1)

    async def test_new_event_resets_episode(self) -> None:
        await self.at(600)
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.alerts()[0]["text"], silence_alert(10))
        self.clock.t = 1000.0 + 700
        self.status.on_event()  # любое событие stream-json, не только tool_use
        await self.at(1299)
        self.assertEqual(len(self.alerts()), 1)
        await self.at(1300)
        self.assertEqual(len(self.alerts()), 2)

    async def test_wait_button_postpones_next_alert(self) -> None:
        await self.at(600)
        alert_id = self.wire.sent[-1]["message_id"]
        self.clock.t = 1000.0 + 650
        self.status.snooze(alert_id)
        await self.at(1249)
        self.assertEqual(len(self.alerts()), 1)
        await self.at(1250)
        self.assertEqual(len(self.alerts()), 2)
        self.assertEqual(self.alerts()[1]["text"], silence_alert(20))

    async def test_pending_approval_is_not_silence(self) -> None:
        self.waiting = True
        await self.at(300)
        await self.at(900)
        self.assertEqual(self.alerts(), [])
        self.waiting = False
        await self.at(1499)
        self.assertEqual(self.alerts(), [])
        await self.at(1500)
        self.assertEqual(len(self.alerts()), 1)

    async def test_pending_approval_time_not_shown_as_silence(self) -> None:
        await self.status.on_tool("Bash: deploy")
        self.waiting = True
        await self.at(300)
        self.assertNotRegex(self.wire.edits[-1]["text"], msg_re("тишина {dur}"))
        self.waiting = False
        await self.at(340)
        self.assertTrue(self.wire.edits[-1]["text"].endswith(_("тишина {dur}").format(dur=_("{s}с").format(s=40))))
        await self.at(900)
        (alert,) = self.alerts()
        self.assertEqual(alert["text"], silence_alert(10, "Bash: deploy"))

    async def test_disabled_by_zero(self) -> None:
        self.status._silence_alert_sec = 0
        self.status._progress_note_sec = 0
        await self.at(5000)
        self.assertEqual(self.wire.loud(), [])


class ProgressNoteTests(_Base):
    async def test_note_once_after_15_minutes(self) -> None:
        for i in range(3):
            await self.status.on_tool(f"Read: f{i}.py")
            self.status.on_event()
        self.clock.t = 1000.0 + 890
        self.status.on_event()  # события идут, тревоги нет
        await self.at(899)
        self.assertEqual(self.wire.loud(), [])
        await self.at(900)
        (note,) = self.wire.loud()
        self.assertEqual(note["text"], progress_note(15, 3, "Read: f2.py"))
        self.assertIsNone(note["reply_markup"])
        await self.at(1000)
        self.assertEqual(len(self.wire.loud()), 1)


class _FlakyWire(_Wire):
    """Telegram отказывает: громкие отправки по очереди из fail_loud, правки
    статуса из fail_edit, удаления из fail_delete."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_loud: list[Exception] = []
        self.fail_edit: list[Exception] = []
        self.fail_delete: list[Exception] = []
        self.edit_calls = 0
        self.loud_calls = 0

    async def send_message(self, **kw):
        if kw.get("disable_notification") is False:
            self.loud_calls += 1
            if self.fail_loud:
                raise self.fail_loud.pop(0)
        return await super().send_message(**kw)

    async def delete_message(self, **kw):
        if self.fail_delete:
            raise self.fail_delete.pop(0)
        await super().delete_message(**kw)

    async def edit_message_text(self, **kw):
        self.edit_calls += 1
        if self.fail_edit:
            raise self.fail_edit.pop(0)
        await super().edit_message_text(**kw)


class DeliveryFailureTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.wire = _FlakyWire()
        self.status._bot = self.wire

    async def test_alert_retried_after_retry_after(self) -> None:
        self.wire.fail_loud = [RetryAfter(40)]
        with self.assertLogs(botmod.log, "WARNING"):
            await self.at(600)
        self.assertEqual(self.alerts(), [])
        await self.at(625)  # 429 просил 40 с: ещё рано
        self.assertEqual(self.alerts(), [])
        await self.at(650)
        (alert,) = self.alerts()
        self.assertEqual(alert["text"], silence_alert(10))
        await self.at(700)
        self.assertEqual(len(self.alerts()), 1)

    async def test_progress_note_retried_after_network_error(self) -> None:
        self.status._silence_alert_sec = 0
        self.wire.fail_loud = [NetworkError("timeout")]
        with self.assertLogs(botmod.log, "WARNING"):
            await self.at(900)
        self.assertEqual(self.wire.loud(), [])
        await self.at(925)
        (note,) = self.wire.loud()
        self.assertEqual(note["text"], progress_note(15, 0))
        await self.at(950)
        self.assertEqual(len(self.wire.loud()), 1)

    async def test_event_during_alert_send_starts_new_episode(self) -> None:
        status = self.status

        async def send_message(**kw):
            status.on_event()  # событие пришло, пока тревога летела
            return await _Wire.send_message(self.wire, **kw)

        self.wire.send_message = send_message
        await self.at(600)
        self.assertEqual(len(self.alerts()), 1)
        self.assertFalse(status._alerted)

    async def test_failed_edit_holds_throttle(self) -> None:
        await self.status.on_tool("Bash: a")
        self.wire.fail_edit = [RetryAfter(10)]
        self.clock.t += 5
        await self.status.on_tool("Bash: b")
        self.assertEqual(self.wire.edit_calls, 1)
        self.clock.t += 5
        await self.status.on_tool("Bash: c")  # retry_after ещё идёт
        self.assertEqual(self.wire.edit_calls, 1)
        self.clock.t += 6
        await self.status.on_tool("Bash: d")
        self.assertEqual(self.wire.edit_calls, 2)
        self.wire.fail_edit = [NetworkError("reset")]
        self.clock.t += 5
        await self.status.on_tool("Bash: e")
        self.clock.t += 1  # после сетевой ошибки пауза в окно троттлинга
        await self.status.on_tool("Bash: f")
        self.assertEqual(self.wire.edit_calls, 3)
        self.clock.t += 3
        await self.status.on_tool("Bash: g")
        self.assertEqual(self.wire.edit_calls, 4)
        self.assertEqual(self.wire.edits[-1]["text"].split(" · ")[0], "🔧 Bash: g")


    async def test_deleted_status_resent_without_blocking_alerts(self) -> None:
        await self.status.on_tool("Bash: pytest")
        old_id = self.wire.sent[0]["message_id"]
        self.wire.fail_edit = [BadRequest("Message to edit not found")]
        await self.at(30)
        self.assertIsNone(self.status._message_id)
        self.assertEqual(self.status._pause_until, 0.0)
        await self.at(600)
        self.assertEqual(self.wire.edit_calls, 1)  # старый id больше не правим
        status_msgs = [m for m in self.wire.sent if m["disable_notification"] is True]
        self.assertEqual(len(status_msgs), 2)
        self.assertEqual(self.status._message_id, status_msgs[1]["message_id"])
        self.assertNotEqual(self.status._message_id, old_id)
        self.assertEqual(len(self.alerts()), 1)

    async def test_failed_edit_does_not_hold_alert(self) -> None:
        await self.status.on_tool("Bash: pytest")
        self.wire.fail_edit = [NetworkError("reset")]
        await self.at(600)
        self.assertEqual(self.wire.edit_calls, 1)
        self.assertEqual(len(self.alerts()), 1)

    async def test_unsure_alert_retried_once(self) -> None:
        self.status._progress_note_sec = 0
        self.wire.fail_loud = [TimedOut(), TimedOut()]
        with self.assertLogs(botmod.log, "WARNING"):
            await self.at(600)
            await self.at(625)
        await self.at(650)
        await self.at(1300)
        self.assertEqual(self.wire.loud_calls, 2)
        # Новый эпизод тишины снова со своими попытками.
        self.clock.t = 1000.0 + 1310
        self.status.on_event()
        await self.at(1910)
        self.assertEqual(self.wire.loud_calls, 3)
        self.assertEqual(len(self.alerts()), 1)

    async def test_unsure_after_explicit_failure_still_one_retry(self) -> None:
        self.wire.fail_loud = [BadRequest("x"), NetworkError("reset"), TimedOut()]
        with self.assertLogs(botmod.log, "WARNING"):
            for t in (600, 625, 650, 675, 700):
                await self.at(t)
        self.assertEqual(self.wire.loud_calls, 3)
        self.assertEqual(self.alerts(), [])

    async def test_explicit_failures_bounded(self) -> None:
        self.status._progress_note_sec = 0
        self.wire.fail_loud = [BadRequest("x")] * 10
        with self.assertLogs(botmod.log, "WARNING"):
            for i in range(10):
                await self.at(600 + 25 * i)
        self.assertEqual(self.wire.loud_calls, botmod._RunStatus._LOUD_TRIES)

    async def test_unsure_progress_note_retried_once(self) -> None:
        self.status._silence_alert_sec = 0
        self.wire.fail_loud = [TimedOut(), TimedOut(), TimedOut()]
        with self.assertLogs(botmod.log, "WARNING"):
            for t in (900, 925, 950, 975):
                await self.at(t)
        self.assertEqual(self.wire.loud_calls, 2)

    async def _finish(self) -> list[float]:
        slept: list[float] = []

        async def fake_sleep(sec: float) -> None:
            slept.append(sec)
            self.clock.t += sec

        with patch.object(botmod.asyncio, "sleep", fake_sleep):
            await self.status.finish()
        return slept

    async def test_finish_waits_retry_after_and_deletes(self) -> None:
        await self.status.on_tool("Bash: x")
        await self.at(600)
        ids = [m["message_id"] for m in self.wire.sent]
        self.wire.fail_delete = [RetryAfter(7)]
        slept = await self._finish()
        self.assertEqual(slept, [7.0])
        self.assertEqual(self.wire.deleted, ids)

    async def test_finish_waits_earlier_pause(self) -> None:
        await self.status.on_tool("Bash: x")
        self.status._pause_until = self.clock.t + 5
        slept = await self._finish()
        self.assertEqual(slept, [5.0])
        self.assertEqual(len(self.wire.deleted), 1)

    async def test_finish_gives_up_within_budget(self) -> None:
        await self.status.on_tool("Bash: x")
        await self.at(600)
        self.wire.fail_delete = [RetryAfter(20), RetryAfter(20)]
        started = self.clock.t
        with self.assertLogs(botmod.log, "WARNING") as logs:
            await self._finish()
        self.assertLessEqual(self.clock.t - started, botmod._RunStatus._FINISH_BUDGET_SEC)
        self.assertEqual(self.wire.deleted, [])
        self.assertEqual(len([r for r in logs.output if "left in chat" in r]), 2)


class FinishTests(_Base):
    async def test_finish_removes_status_and_unanswered_alerts(self) -> None:
        self.status.start()
        self.assertIs(self.registry[self.status.run_id], self.status)
        await self.status.on_tool("Bash: x")
        status_id = self.wire.sent[0]["message_id"]
        await self.at(600)
        unanswered = self.alerts()[-1]["message_id"]
        self.clock.t = 1000.0 + 610
        self.status.on_event()
        await self.at(1210)
        answered = self.alerts()[-1]["message_id"]
        self.status.snooze(answered)
        ticker = self.status._ticker
        await self.status.finish()
        self.assertTrue(ticker.done())
        self.assertNotIn(self.status.run_id, self.registry)
        self.assertEqual(self.wire.deleted, [status_id, unanswered])
        # После завершения ни правок, ни тревог.
        sent = len(self.wire.sent)
        await self.status.on_tool("Bash: y")
        await self.at(5000)
        self.assertEqual(len(self.wire.sent), sent)


class FinishDuringTickTests(_Base):
    async def test_no_alert_after_finish_while_status_edit_in_flight(self) -> None:
        await self.status.on_tool("Bash: sleep")
        gate = asyncio.Event()
        entered = asyncio.Event()
        edit = self.wire.edit_message_text

        async def slow_edit(**kw):
            entered.set()
            await gate.wait()
            await edit(**kw)

        self.wire.edit_message_text = slow_edit
        self.clock.t = 1000.0 + 900  # пора и тревоге, и весточке
        tick = asyncio.create_task(self.status._tick())
        await entered.wait()
        fin = asyncio.create_task(self.status.finish())
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(tick, fin)
        self.assertEqual(self.wire.loud(), [])


class ButtonTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.registry[self.status.run_id] = self.status
        self.proc = SimpleNamespace(pid=4242, returncode=None)
        self.ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
            "cfg": SimpleNamespace(allowed_user_ids={USER}),
            "run_watch": self.registry,
            "procs": {self.status.chat_key: self.proc},
        }))

    def press(self, action: str, run_id: str | None = None, user: int = USER):
        card = SimpleNamespace(message_id=555, text="⏳ 10 минут тишины: …")
        query = SimpleNamespace(
            data=f"runwatch:{action}:{run_id or self.status.run_id}", message=card,
            answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user))
        return update, query

    async def test_wait_snoozes_and_clears_buttons(self) -> None:
        self.status._alerted = True
        self.status._alert_ids = [555]
        update, query = self.press("wait")
        await botmod.on_run_watch_pick(update, self.ctx)
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.assertFalse(self.status._alerted)
        self.assertEqual(self.status._alert_ids, [])
        self.assertEqual(self.status._quiet_since, self.clock.t)

    async def test_stop_cancels_like_cancel_command(self) -> None:
        self.status._alert_ids = [555]
        cancel = AsyncMock(return_value="SIGTERM отправлен (pid=4242). SIGKILL через 5с если не выйдет.")
        update, query = self.press("stop")
        with patch.object(botmod, "_cancel_running", cancel):
            await botmod.on_run_watch_pick(update, self.ctx)
        cancel.assert_awaited_once_with(self.ctx, self.status.chat_key, self.proc)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("🛑 SIGTERM отправлен (pid=4242)", text)
        # Отвеченная тревога по завершении рана не удаляется.
        self.assertEqual(self.status._alert_ids, [])

    async def test_finished_run_button_is_stale(self) -> None:
        cancel = AsyncMock()
        update, query = self.press("stop", run_id="gone")
        with patch.object(botmod, "_cancel_running", cancel):
            await botmod.on_run_watch_pick(update, self.ctx)
        cancel.assert_not_awaited()
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.assertEqual(_("Ран уже закончился"), query.answer.await_args.args[0])

    async def test_stranger_ignored(self) -> None:
        cancel = AsyncMock()
        update, query = self.press("stop", user=999)
        with patch.object(botmod, "_cancel_running", cancel):
            await botmod.on_run_watch_pick(update, self.ctx)
        cancel.assert_not_awaited()
        query.answer.assert_not_awaited()


class WaitsApprovalTests(unittest.TestCase):
    def test_only_cards_of_this_session(self) -> None:
        registry = SimpleNamespace(_items={"a": SimpleNamespace(session_id="other")})
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
            "active_sessions": {"k": "sid-1"}, "registry": registry,
        }))
        self.assertFalse(botmod._run_waits_approval(ctx, "k"))
        registry._items["b"] = SimpleNamespace(session_id="sid-1")
        self.assertTrue(botmod._run_waits_approval(ctx, "k"))
        self.assertFalse(botmod._run_waits_approval(ctx, "no-run"))


class _Stdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Stdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        if not self._lines:
            raise asyncio.IncompleteReadError(b"", None)
        return self._lines.pop(0)


class _Stderr:
    async def read(self) -> bytes:
        return b""


class _Proc:
    pid = 1
    returncode = 0

    def __init__(self, events: list[dict]) -> None:
        self.stdin = _Stdin()
        self.stdout = _Stdout([json.dumps(e).encode() + b"\n" for e in events])
        self.stderr = _Stderr()

    async def wait(self) -> int:
        return 0


class RunnerActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_on_every_event_progress_on_tool_use(self) -> None:
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "думаю"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a"}]}},
            {"type": "result", "result": "ok", "session_id": "s"},
        ]
        activity: list[int] = []
        tools: list[str] = []

        async def progress(desc: str) -> None:
            tools.append(desc)

        store = SimpleNamespace(get=lambda k: None, set=lambda k, v: None, remove=lambda k: None)
        with patch.object(claude_runner, "_ensure_hook_still_valid", lambda *_: None), \
                patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()), \
                patch.object(claude_runner.asyncio, "create_subprocess_exec",
                             AsyncMock(return_value=_Proc(events))):
            result = await claude_runner.run_claude(
                "claude", "k", 1, None, "x", store, cwd=Path("/tmp"),
                hook_settings=Path("/tmp/x.json"), progress_cb=progress,
                activity_cb=lambda: activity.append(1),
            )
        self.assertEqual(result.text, "ok")
        self.assertEqual(tools, ["Bash: ls"])
        self.assertEqual(len(activity), 5)


class ConfigTests(unittest.TestCase):
    def _load(self, extra: dict[str, str]):
        from claude_tg_bot import config as cfgmod
        env = {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_IDS": "1",
            "CLAUDE_BIN": sys.executable, "PATH": os.environ.get("PATH", ""),
            **extra,
        }
        with patch.dict(os.environ, env, clear=True):
            return cfgmod.load_config(skip_hook_validation=True)

    def test_env_overrides_and_defaults(self) -> None:
        cfg = self._load({"RUN_SILENCE_ALERT_SEC": "120", "RUN_PROGRESS_NOTE_SEC": "0"})
        self.assertEqual((cfg.run_silence_alert_sec, cfg.run_progress_note_sec), (120, 0))
        # Пустая строка явно: load_config подхватывает .env репозитория для
        # отсутствующих ключей.
        cfg = self._load({"RUN_SILENCE_ALERT_SEC": "", "RUN_PROGRESS_NOTE_SEC": "мусор"})
        self.assertEqual((cfg.run_silence_alert_sec, cfg.run_progress_note_sec), (600, 900))


if __name__ == "__main__":
    unittest.main()

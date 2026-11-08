"""Хвосты бота (docs/audit-2026-11/BRIEFS.md, третий круг, задание rest).
Порядок событий задан явно, «авария» это новый объект приложения над тем же
каталогом: всё, что не на диске, пропало."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError

from claude_tg_bot import bot as botmod
from claude_tg_bot import audit, inflight, limits, tg_limits
from claude_tg_bot.claude_runner import SessionStore
from tests.test_review_bot_main import GROUP, _Base, _msg, _update
from tests.test_queue2 import _settle
from tests.texts import msg_re


class _CrashBase(_Base):
    def records(self) -> dict[str, dict]:
        path = self.state / "inflight.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def restarted(self, send: AsyncMock | None = None) -> SimpleNamespace:
        """Новый процесс над тем же state: только то, что на диске."""
        data = {**self.bot_data, "at_jobs": SessionStore(self.state / "at_jobs.json")}
        for key in ("inflight_lost", "inflight_tries", "stopping"):
            data.pop(key, None)
        bot = SimpleNamespace(send_message=send or AsyncMock())
        return SimpleNamespace(bot_data=data, bot=bot)

    def add(self, text: str = "удали ветки") -> str:
        return inflight.add(self.state, chat_id=GROUP, thread_id=None, message_id=42,
                            prompt=text, kind="ask")


class InflightDeliveryTests(_CrashBase):
    """Запись снимается после подтверждённой доставки вести (пункт 1)."""

    async def announce(self, app: SimpleNamespace) -> None:
        await botmod._inflight_announce(SimpleNamespace(application=app))

    async def test_crash_before_notice_keeps_record(self) -> None:
        self.add()
        botmod._inflight_recover(self.restarted())
        # Умер после разбора, до отправки: следующий старт весть не теряет.
        app = self.restarted()
        botmod._inflight_recover(app)
        await self.announce(app)
        app.bot.send_message.assert_awaited_once()
        self.assertEqual(self.records(), {})

    async def test_failed_send_keeps_record_for_next_start(self) -> None:
        self.add()
        app = self.restarted(AsyncMock(side_effect=NetworkError("нет сети")))
        botmod._inflight_recover(app)
        await self.announce(app)
        self.assertEqual(len(self.records()), 1)
        next_app = self.restarted()
        botmod._inflight_recover(next_app)
        await self.announce(next_app)
        next_app.bot.send_message.assert_awaited_once()
        self.assertEqual(self.records(), {})

    async def test_unconfirmed_send_not_repeated(self) -> None:
        # Telegram не подтвердил весть: она обычно дошла, следующий старт её
        # не повторяет (ревью Codex 05.11.2026)
        import httpx
        from telegram.error import TimedOut
        err = TimedOut()
        err.__cause__ = httpx.ReadTimeout("")
        self.add()
        app = self.restarted(AsyncMock(side_effect=err))
        botmod._inflight_recover(app)
        await self.announce(app)
        self.assertEqual(self.records(), {})

    async def test_failed_send_retried_while_tries_left(self) -> None:
        self.add()
        app = self.restarted(AsyncMock(side_effect=NetworkError("нет сети")))
        app.job_queue = SimpleNamespace(run_once=lambda *a, **k: calls.append(k))
        calls: list = []
        botmod._inflight_recover(app)
        for _try in range(botmod._INFLIGHT_TRIES):
            await self.announce(app)
        self.assertEqual(len(calls), botmod._INFLIGHT_TRIES - 1)

    async def test_telegram_refusal_drops_record(self) -> None:
        # Темы больше нет: повтор не поможет, а каждый старт слал бы снова.
        self.add()
        app = self.restarted(AsyncMock(side_effect=BadRequest("Message thread not found")))
        botmod._inflight_recover(app)
        await self.announce(app)
        self.assertEqual(self.records(), {})

    async def test_partial_delivery_removes_only_delivered(self) -> None:
        one, two = self.add("первое"), self.add("второе")
        app = self.restarted(AsyncMock(side_effect=[None, NetworkError("нет сети")]))
        botmod._inflight_recover(app)
        await self.announce(app)
        self.assertEqual(list(self.records()), [two])
        self.assertNotIn(one, self.records())

    async def test_stop_does_not_clear_before_its_end(self) -> None:
        rid = self.add()
        app = self.restarted()
        botmod._inflight_recover(app)
        app.bot_data["inflight_lost"].clear()
        botmod._terminate_runs(app)
        # Авария посреди остановки: запись на месте, весть после старта будет.
        self.assertIn(rid, self.records())
        botmod._inflight_stopped(app)
        self.assertEqual(self.records(), {})

    async def test_stop_keeps_undelivered_crash_notice(self) -> None:
        old = self.add("старое")
        app = self.restarted()
        botmod._inflight_recover(app)
        mine = self.add("новое")
        botmod._terminate_runs(app)
        botmod._inflight_stopped(app)
        self.assertEqual(list(self.records()), [old])
        self.assertNotIn(mine, self.records())

    async def test_stop_after_failed_start_keeps_file(self) -> None:
        old = self.add()
        botmod._inflight_stopped(self.restarted())  # post_init не дошёл до разбора
        self.assertEqual(list(self.records()), [old])


def _drained(store) -> list[dict]:
    payloads = [json.loads(e["v"]) for e in store.data.values()]
    return [p for p in payloads if p.get("drained")]


class GatherOnStopTests(_CrashBase):
    """Сообщение в трёхсекундной склейке при остановке ложится в отложенное,
    а не просит повторить (пункт 2)."""

    REFUSED = "сообщение не ушло"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.first = _msg("первое")
        self.bot_data["stopping"] = True

    def batch(self, items: list, *rest) -> botmod.GatherBatch:
        return botmod.GatherBatch(update=_update(self.first), ctx=self.ctx, items=items,
                                  messages=list(rest))

    async def flush(self, batch: botmod.GatherBatch) -> None:
        await botmod._gather_flush_impl(str(GROUP), batch)

    async def test_ready_piece_deferred_and_survives_restart(self) -> None:
        await self.flush(self.batch(["первое"]))
        self.assertEqual(self.runs, [])
        [job] = _drained(SessionStore(self.state / "at_jobs.json"))
        self.assertEqual(job["prompt"], "первое")
        said = str(self.first.reply_text.await_args.args[0])
        self.assertNotIn(self.REFUSED, said)
        self.assertRegex(said, msg_re("♻️ бот ждёт конца работы для перезапуска: "
                                      "сообщение уйдёт в Claude сразу после него (отмена: /cancel)"))

    async def test_stopping_while_app_still_running(self) -> None:
        self.ctx.application.running = True
        await self.flush(self.batch(["первое"]))
        self.assertEqual(len(_drained(self.bot_data["at_jobs"])), 1)
        self.assertEqual(self.runs, [])

    async def test_unready_piece_told_to_repeat(self) -> None:
        late = _msg("голосовое")
        await self.flush(self.batch(["первое", None], late))
        [job] = _drained(self.bot_data["at_jobs"])
        self.assertEqual(job["prompt"], "первое")
        self.assertIn(self.REFUSED, str(late.reply_text.await_args.args[0]))
        self.assertNotIn(self.REFUSED, str(self.first.reply_text.await_args.args[0]))

    async def test_nothing_ready_refused(self) -> None:
        await self.flush(self.batch([None]))
        self.assertEqual(_drained(self.bot_data["at_jobs"]), [])
        self.assertIn(self.REFUSED, str(self.first.reply_text.await_args.args[0]))

    async def test_arrival_order_kept_between_batches(self) -> None:
        later, earlier = self.batch(["позднее"]), self.batch(["раннее"])
        earlier.arrived, later.arrived = 100.0, 200.0
        await self.flush(later)
        await self.flush(earlier)
        [job] = _drained(self.bot_data["at_jobs"])
        self.assertLess(job["prompt"].index("раннее"), job["prompt"].index("позднее"))

    async def test_disk_failure_falls_back_to_refusal(self) -> None:
        with patch.object(self.bot_data["at_jobs"], "set", side_effect=OSError("диск")):
            await self.flush(self.batch(["первое"]))
        self.assertIn(self.REFUSED, str(self.first.reply_text.await_args.args[0]))


class DeferCrashTests(_CrashBase):
    """Авария между сохранением отложенного и снятием записи inflight: одно
    выполнение после старта, без предложения повторить (пункт 2)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.lock = botmod._chat_lock(self.ctx, str(GROUP))
        await self.lock.acquire()

    async def after_crash(self) -> SimpleNamespace:
        app = self.restarted()
        botmod._inflight_recover(app)
        await botmod._inflight_announce(SimpleNamespace(application=app))
        return app

    async def test_crash_right_after_defer_gives_no_notice(self) -> None:
        early = asyncio.ensure_future(self.dispatch("раннее"))
        await _settle()
        self.assertEqual(len(self.records()), 1)
        self.bot_data["draining"] = True
        # Запись inflight не снята: процесс умер сразу после сохранения.
        with patch.object(botmod.inflight, "remove"):
            self.lock.release()
            await asyncio.wait_for(early, 1)
        app = await self.after_crash()
        app.bot.send_message.assert_not_awaited()
        self.assertEqual(self.records(), {})
        self.assertEqual(len(_drained(app.bot_data["at_jobs"])), 1)

    async def test_joined_message_covered_too(self) -> None:
        one = asyncio.ensure_future(self.dispatch("первое", _msg("первое")))
        await _settle()
        two = asyncio.ensure_future(self.dispatch("второе", _msg("второе")))
        await _settle()
        self.assertEqual(len(self.records()), 2)
        self.bot_data["draining"] = True
        with patch.object(botmod.inflight, "remove"):
            self.lock.release()
            await asyncio.wait_for(asyncio.gather(one, two), 1)
        app = await self.after_crash()
        app.bot.send_message.assert_not_awaited()
        self.assertEqual(self.records(), {})

    async def test_defer_removes_record_at_once(self) -> None:
        early = asyncio.ensure_future(self.dispatch("раннее"))
        await _settle()
        self.bot_data["draining"] = True
        seen: list[int] = []
        real = botmod._drain_defer

        def spy(*a, **k):
            text = real(*a, **k)
            seen.append(len(self.records()))
            return text

        with patch.object(botmod, "_drain_defer", spy):
            self.lock.release()
            await asyncio.wait_for(early, 1)
        self.assertEqual(seen, [0])

    async def test_uncovered_record_still_announced(self) -> None:
        self.add()
        app = await self.after_crash()
        app.bot.send_message.assert_awaited_once()


class CarriedDeferredTests(_CrashBase):
    """Новое сообщение везёт отложенное, но запись живёт до старта рана:
    авария и отказ до него текста не теряют (пункт 2)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.scheduled: list[dict] = []
        self.ctx.application.job_queue = SimpleNamespace(
            get_jobs_by_name=lambda _name: [],
            run_once=lambda _cb, when, data, name: self.scheduled.append(data))
        patcher = patch.object(botmod, "_synthetic_update",
                               lambda bot, cid, tid: _update(_msg(chat_id=cid)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot_data["draining"] = True
        await self.dispatch("отложенное")
        self.bot_data["draining"] = False
        self.lock = botmod._chat_lock(self.ctx, str(GROUP))

    @property
    def jobs(self) -> list[dict]:
        return _drained(self.bot_data["at_jobs"])

    def fire_ctx(self) -> SimpleNamespace:
        [jid] = list(self.bot_data["at_jobs"].data)
        data = {"jid": jid, **json.loads(self.bot_data["at_jobs"].data[jid]["v"])}
        return SimpleNamespace(application=self.ctx.application, bot=self.ctx.bot,
                               job=SimpleNamespace(data=data))

    async def waiting(self, text: str = "новое") -> asyncio.Future:
        await self.lock.acquire()
        fut = asyncio.ensure_future(self.dispatch(text, _msg(text)))
        await _settle()
        return fut

    async def test_record_survives_until_run_start(self) -> None:
        seen: list[int] = []

        async def hook(_run):
            seen.append(len(self.jobs))

        self.run_hook = hook
        fut = await self.waiting()
        self.assertEqual(len(_drained(SessionStore(self.state / "at_jobs.json"))), 1)
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        self.assertEqual(seen, [0])
        self.assertEqual(self.jobs, [])
        prompt = self.runs[0]["prompt"]
        self.assertLess(prompt.index("отложенное"), prompt.index("новое"))
        self.assertEqual(self.scheduled, [])

    async def test_crash_before_start_keeps_full_text(self) -> None:
        long_text = "отложенное " * 30
        self.bot_data["at_jobs"].remove(next(iter(self.bot_data["at_jobs"].data)))
        self.bot_data["draining"] = True
        await self.dispatch(long_text)
        self.bot_data["draining"] = False
        fut = await self.waiting()
        # Авария: на диске весь текст, весть называет только новое сообщение.
        app = self.restarted()
        [job] = _drained(app.bot_data["at_jobs"])
        self.assertEqual(job["prompt"], long_text)
        botmod._inflight_recover(app)
        await botmod._inflight_announce(SimpleNamespace(application=app))
        text = app.bot.send_message.await_args.kwargs["text"]
        self.assertIn("новое", text)
        self.assertNotIn("отложенное", text)
        self.lock.release()
        await asyncio.wait_for(fut, 1)

    async def test_job_firing_while_claimed_does_nothing(self) -> None:
        jctx = self.fire_ctx()
        fut = await self.waiting()
        await botmod._at_fire(jctx)
        self.assertIn(jctx.job.data["jid"], self.bot_data["at_running"])
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        self.assertEqual(len(self.runs), 1)
        self.assertFalse(self.bot_data["at_running"])

    async def test_claim_blocks_second_carrier_and_defer(self) -> None:
        fut = await self.waiting()
        two = asyncio.ensure_future(self.dispatch("ещё", _msg("ещё")))
        await _settle()
        self.lock.release()
        await asyncio.wait_for(asyncio.gather(fut, two), 1)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.runs[0]["prompt"].count("отложенное"), 1)
        self.assertEqual(self.jobs, [])

    async def test_refused_before_start_goes_back_to_queue(self) -> None:
        fut = await self.waiting()
        self.bot_data["login_manager"] = SimpleNamespace(session=object())
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        self.assertEqual(self.runs, [])
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual([d["prompt"] for d in self.scheduled], ["отложенное"])
        self.assertFalse(self.bot_data["at_running"])

    async def test_stop_before_start_keeps_record_for_next_start(self) -> None:
        fut = await self.waiting()
        self.bot_data["stopping"] = True
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.scheduled, [])

    async def test_drain_under_lock_merges_without_duplicate(self) -> None:
        fut = await self.waiting()
        self.bot_data["draining"] = True
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        [job] = self.jobs
        self.assertEqual(job["prompt"].count("отложенное"), 1)
        self.assertIn("новое", job["prompt"])
        self.assertEqual(self.scheduled, [])
        on_disk = _drained(SessionStore(self.state / "at_jobs.json"))
        self.assertEqual(len(on_disk), 1)

    async def test_limit_move_takes_carried_with_it(self) -> None:
        fut = await self.waiting()

        seen: list[int] = []

        async def moved(*_a, **kw):
            kw["on_moved"]()
            seen.append(len(self.jobs))  # снято до ответа Ивану
            return True

        with patch.object(botmod, "_limit_hold", moved):
            self.lock.release()
            await asyncio.wait_for(fut, 1)
        self.assertEqual(seen, [0])
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.scheduled, [])

    async def test_limit_move_replaces_carried_in_one_write(self) -> None:
        # Повтор лимитом и снятие забранного отложенного одной записью файла:
        # авария до limit_moved не оставит на диске оба текста (ревью Codex 04.11.2026).
        fut = await self.waiting()
        seen: list[list[dict]] = []
        real = tg_limits._limit_defer

        def defer(*a, **kw):
            ok = real(*a, **kw)
            on_disk = SessionStore(self.state / "at_jobs.json")
            seen.append([json.loads(e["v"]) for e in on_disk.data.values()])
            return ok

        with patch.object(limits, "reserve", lambda *a, **k: time.time() + 1800), \
                patch.object(tg_limits, "_limit_defer", defer):
            self.lock.release()
            await asyncio.wait_for(fut, 1)
        [[job]] = seen
        self.assertFalse(job.get("drained"))
        self.assertEqual(job["limit_retry"], 1)
        self.assertEqual((job["prompt"].count("отложенное"), job["prompt"].count("новое")), (1, 1))
        self.assertEqual(self.jobs, [])

    async def test_cancel_drops_record_and_message(self) -> None:
        fut = await self.waiting()
        stop = _msg("/cancel")
        await botmod.on_cancel(_update(stop), self.ctx)
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.scheduled, [])
        self.assertRegex(str(stop.reply_text.await_args.args[0]),
                         msg_re("🗑 отменено до отправки в Claude: {count} сообщ."))

    async def test_cancel_during_limit_check_stops_run(self) -> None:
        # Замок свободен, пачки нет, процесса нет: /cancel находит только запись.
        async def hold(*_a, **_kw):
            await botmod.on_cancel(_update(_msg("/cancel")), self.ctx)
            return False

        with patch.object(botmod, "_limit_hold", hold):
            await self.dispatch("новое")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.jobs, [])

    async def test_codex_path_removes_record(self) -> None:
        self.cfg.codex_bin = "codex"
        result = botmod.ClaudeResult(text="ok", session_id="")
        with patch.object(botmod, "_codex_as_claude_result", AsyncMock(return_value=result)):
            await self.dispatch("gpt: вопрос")
        self.assertEqual(self.jobs, [])

    async def test_crash_after_start_names_carried_text(self) -> None:
        # Запись снята стартом: повтора нет, весть называет и увезённый текст.
        crash: list[SimpleNamespace] = []

        async def hook(_run):
            app = self.restarted()
            botmod._inflight_recover(app)
            await botmod._inflight_announce(SimpleNamespace(application=app))
            crash.append(app)

        self.run_hook = hook
        fut = await self.waiting()
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        texts = [c.kwargs["text"] for c in crash[0].bot.send_message.await_args_list]
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("отложенное" in t for t in texts), texts)
        self.assertTrue(any("новое" in t for t in texts), texts)
        self.assertEqual(self.jobs, [])


class FooterTrailTests(_CrashBase):
    """Отметки футера, потерянные при SIGKILL, сверяются с state/audit.jsonl
    при старте и уходят в весть об аварии (пункт 3)."""

    def event(self, decision: str, target: str, *, sid: str = "s1", tool: str = "Write") -> None:
        key = "command" if tool == "Bash" else "file_path"
        audit.write_event(self.state / "audit.jsonl", approval_id="a", tool_name=tool,
                          tool_input={key: target}, decision=decision, session_id=sid)

    async def notice(self) -> str:
        app = self.restarted()
        botmod._inflight_recover(app)
        await botmod._inflight_announce(SimpleNamespace(application=app))
        return app.bot.send_message.await_args.kwargs["text"]

    def lost(self, sid: str = "s1", ago: float = 60.0) -> str:
        rid = self.add()
        inflight.note_sid(self.state, [rid], sid)
        path = self.state / "inflight.json"
        data = json.loads(path.read_text())
        data[rid]["ts"] -= ago
        path.write_text(json.dumps(data))
        return rid

    async def test_session_recorded_during_run(self) -> None:
        seen: list[dict] = []

        async def hook(run):
            seen.extend(self.records().values())
            self.assertEqual(seen[0]["sid"], run["sid"])

        self.run_hook = hook
        await self.dispatch("x")
        self.assertEqual(len(seen), 1)

    async def test_notes_of_cut_run_in_notice(self) -> None:
        self.lost()
        self.event("allow:whitelist", "/x/notes.md")
        self.event("allow:memory-write", "memory remember --file d.md", tool="Bash")
        text = await self.notice()
        self.assertIn("/x/notes.md", text)
        self.assertRegex(text, "memory remember")

    async def test_foreign_and_old_events_ignored(self) -> None:
        self.event("allow:whitelist", "/старое.md")
        path = self.state / "audit.jsonl"
        old = json.loads(path.read_text())
        old["ts"] = "2020-01-01T00:00:00+03:00"
        path.write_text(json.dumps(old, ensure_ascii=False) + "\n")
        self.lost()
        self.event("allow:whitelist", "/своё.md")
        self.event("allow:whitelist", "/чужое.md", sid="s2")
        self.event("allow:read", "/чтение.md")
        self.event("allow:memory-write", "memory remember --dry-run --file d.md", tool="Bash")
        text = await self.notice()
        self.assertIn("своё", text)
        for word in ("старое", "чужое", "чтение", "dry-run"):
            self.assertNotIn(word, text)

    async def test_broken_journal_does_not_stop_notice(self) -> None:
        self.lost()
        (self.state / "audit.jsonl").write_bytes("не json\n{\"ts\": 5}\n".encode() + b"\xff\n")
        self.assertRegex(await self.notice(), "ответ не дошёл")

    async def test_no_session_no_trail(self) -> None:
        self.add()
        self.event("allow:whitelist", "/x/notes.md", sid="")
        self.assertNotIn("/x/notes.md", await self.notice())


class EditWhileWaitingTests(_Base):
    """Правка сообщения, ещё ждущего склейки или пачки, подменяет текст (пункт 5)."""

    DELIVERED = "✏️ правка учтена: Claude получит новый текст"
    MISSED = "✏️ правка до Claude не дошла: напиши поправку новым сообщением"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.allowed_chat_ids = {GROUP}
        self.ctx.application.running = True
        self.lock = botmod._chat_lock(self.ctx, str(GROUP))

    def message(self, text: str, mid: int, **extra) -> SimpleNamespace:
        msg = _msg(text, **extra)
        msg.message_id = mid
        return msg

    async def edit(self, mid: int, text: str, **extra) -> str:
        msg = self.message(text, mid, **extra)
        upd = _update(msg)
        upd.message, upd.edited_message, upd.effective_message = None, msg, msg
        await botmod.on_edited_text(upd, self.ctx)
        self.assertTrue(msg.reply_text.await_args.kwargs.get("disable_notification"))
        return str(msg.reply_text.await_args.args[0])

    async def flushed(self) -> None:
        await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 2)

    async def test_edit_in_gather_window(self) -> None:
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0.03):
            await botmod.on_text(_update(self.message("удаляй", 1)), self.ctx)
            self.assertRegex(await self.edit(1, "не удаляй"), msg_re(self.DELIVERED))
            await self.flushed()
        self.assertEqual(self.runs[0]["prompt"], "не удаляй")

    async def test_edit_second_piece_of_gather(self) -> None:
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0.03):
            await botmod.on_text(_update(self.message("первое", 1)), self.ctx)
            await botmod.on_text(_update(self.message("второе", 2)), self.ctx)
            await self.edit(2, "исправленное")
            await self.flushed()
        self.assertIn("первое", self.runs[0]["prompt"])
        self.assertIn("исправленное", self.runs[0]["prompt"])
        self.assertNotIn("второе", self.runs[0]["prompt"])

    async def test_edit_keeps_quote(self) -> None:
        quoted = SimpleNamespace(text="цитата", caption=None, message_id=7)
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0.03):
            await botmod.on_text(_update(self.message("да", 1, reply_to_message=quoted)), self.ctx)
            await self.edit(1, "нет", reply_to_message=quoted)
            await self.flushed()
        self.assertIn("цитата", self.runs[0]["prompt"])
        self.assertIn("нет", self.runs[0]["prompt"])

    async def test_edit_in_waiting_pack(self) -> None:
        await self.lock.acquire()
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0):
            one = asyncio.ensure_future(botmod.on_text(_update(self.message("первое", 1)), self.ctx))
            await _settle()
            two = asyncio.ensure_future(botmod.on_text(_update(self.message("второе", 2)), self.ctx))
            await _settle()
            self.assertRegex(await self.edit(2, "исправленное"), msg_re(self.DELIVERED))
            self.assertRegex(await self.edit(1, "начало"), msg_re(self.DELIVERED))
            self.lock.release()
            await asyncio.wait_for(asyncio.gather(one, two), 1)
        prompt = self.runs[0]["prompt"]
        self.assertLess(prompt.index("начало"), prompt.index("исправленное"))
        self.assertNotIn("первое", prompt)

    async def test_edit_twice(self) -> None:
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0.03):
            await botmod.on_text(_update(self.message("раз", 1)), self.ctx)
            await self.edit(1, "два")
            await self.edit(1, "три")
            await self.flushed()
        self.assertEqual(self.runs[0]["prompt"], "три")

    async def test_edit_after_start_told_not_delivered(self) -> None:
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0):
            await botmod.on_text(_update(self.message("удаляй", 1)), self.ctx)
        self.assertRegex(await self.edit(1, "не удаляй"), msg_re(self.MISSED))

    async def test_edit_after_cancel_not_swapped(self) -> None:
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0.03):
            await botmod.on_text(_update(self.message("удаляй", 1)), self.ctx)
            await botmod.on_cancel(_update(_msg("/cancel")), self.ctx)
            self.assertRegex(await self.edit(1, "не удаляй"), msg_re(self.MISSED))
            await self.flushed()
        self.assertEqual(self.runs, [])

    async def test_edit_of_cancelled_leaves_waiting_twin(self) -> None:
        await self.lock.acquire()
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0):
            one = asyncio.ensure_future(botmod.on_text(_update(self.message("да", 1)), self.ctx))
            await _settle()
            botmod._pack_cancel(self.ctx, str(GROUP))
            two = asyncio.ensure_future(botmod.on_text(_update(self.message("да", 2)), self.ctx))
            await _settle()
            self.assertRegex(await self.edit(1, "нет"), msg_re(self.MISSED))
            self.lock.release()
            await asyncio.wait_for(asyncio.gather(one, two), 1)
        self.assertIn("да", self.runs[0]["prompt"])
        self.assertNotIn("нет", self.runs[0]["prompt"])

    async def test_memory_of_texts_is_bounded(self) -> None:
        for mid in range(botmod._PENDING_TEXTS_MAX + 5):
            botmod._pending_text_remember(self.ctx, str(GROUP), self.message("x", mid), "x")
        self.assertEqual(len(self.bot_data["pending_texts"]), botmod._PENDING_TEXTS_MAX)


class ReviewRoundTests(_CrashBase):
    """Замечания ревью: сессия у записей старта, правка ушедшего текста."""

    async def test_at_record_gets_session(self) -> None:
        data = {"jid": "j1", "chat_id": GROUP, "thread_id": None, "prompt": "напомни", "fire_at": 0}
        self.bot_data["at_jobs"].set("j1", json.dumps(data))
        seen: list[dict] = []

        async def hook(run):
            seen.extend(self.records().values())
            self.assertEqual(seen[0]["sid"], run["sid"])

        self.run_hook = hook
        jctx = SimpleNamespace(application=self.ctx.application, bot=self.ctx.bot,
                               job=SimpleNamespace(data=data))
        with patch.object(botmod, "_synthetic_update", lambda bot, cid, tid: _update(_msg(chat_id=cid))):
            await botmod._at_fire(jctx)
        self.assertEqual([r["kind"] for r in seen], ["at"])

    async def test_session_note_moves_trail_start(self) -> None:
        rid = self.add()
        path = self.state / "inflight.json"
        data = json.loads(path.read_text())
        data[rid]["ts"] = 5.0
        path.write_text(json.dumps(data))
        inflight.note_sid(self.state, [rid], "s1")
        self.assertGreater(self.records()[rid]["ts"], 5.0)

    async def test_edit_of_sent_text_leaves_waiting_twin(self) -> None:
        self.cfg.allowed_chat_ids = {GROUP}
        self.ctx.application.running = True
        first, second = _msg("да"), _msg("да")
        second.message_id = 12
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0):
            await botmod.on_text(_update(first), self.ctx)  # ушло в Claude
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0.03):
            await botmod.on_text(_update(second), self.ctx)
            edited = _msg("нет")
            upd = _update(edited)
            upd.message, upd.edited_message, upd.effective_message = None, edited, edited
            await botmod.on_edited_text(upd, self.ctx)
            self.assertIn("не дошла", str(edited.reply_text.await_args.args[0]))
            await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 2)
        self.assertEqual(self.runs[1]["prompt"], "да")


class WaitNoteTests(_Base):
    """Через 3 минуты ожидания личной темы бот сам говорит, что её держит (пункт 6)."""

    NOTE = "⏳ жду: {what}, «!» прервёт"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.allowed_chat_ids = {GROUP}
        self.cfg.primary_chat_id = GROUP
        patcher = patch.object(botmod, "WAIT_NOTE_SEC", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lock = botmod._chat_lock(self.ctx, str(GROUP))
        await self.lock.acquire()
        self.bot_data["run_watch"] = {"r1": SimpleNamespace(
            chat_key=str(GROUP), headline=lambda: "▶️ отвечаю 3м05с, шаг 2: Bash: pytest")}

    def notes(self, msg: SimpleNamespace) -> list[str]:
        said = [str(c.args[0]) for c in msg.reply_text.await_args_list]
        return [s for s in said if re.search(msg_re(self.NOTE), s)]

    async def waited(self, msg: SimpleNamespace, **kw) -> None:
        fut = asyncio.ensure_future(self.dispatch("новое", msg, **kw))
        # Ждём записку опросом, а не ровно 0,03 с: под нагрузкой (проверка
        # перед перезапуском на ПК) она не успевала и тест падал (05.11.2026).
        for _try in range(50):
            await asyncio.sleep(0.01)
            if self.notes(msg):
                break
        self.lock.release()
        await asyncio.wait_for(fut, 1)

    async def test_note_names_what_holds(self) -> None:
        msg = _msg("новое")
        await self.waited(msg)
        [note] = self.notes(msg)
        self.assertIn("отвечаю 3м05с, шаг 2: Bash: pytest", note)
        self.assertNotIn("▶️", note)
        self.assertEqual([c.kwargs.get("disable_notification") for c in msg.reply_text.await_args_list
                          if "жду" in str(c.args[0])], [True])

    async def test_unknown_holder(self) -> None:
        self.bot_data["run_watch"] = {}
        msg = _msg("новое")
        await self.waited(msg)
        self.assertIn("прошлый ход", self.notes(msg)[0])

    async def test_short_wait_silent(self) -> None:
        msg = _msg("новое")
        fut = asyncio.ensure_future(self.dispatch("новое", msg))
        await _settle()
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        await asyncio.sleep(0.03)
        self.assertEqual(self.notes(msg), [])

    async def test_other_topic_silent(self) -> None:
        self.cfg.primary_chat_id = GROUP + 1
        msg = _msg("новое")
        await self.waited(msg)
        self.assertEqual(self.notes(msg), [])

    async def test_button_silent(self) -> None:
        msg = _msg("вариант")
        await self.waited(msg, from_callback=True)
        self.assertEqual(self.notes(msg), [])

    async def test_cancelled_pack_silent(self) -> None:
        msg = _msg("новое")
        fut = asyncio.ensure_future(self.dispatch("новое", msg))
        await _settle()
        botmod._pack_cancel(self.ctx, str(GROUP))
        await asyncio.sleep(0.03)
        self.lock.release()
        await asyncio.wait_for(fut, 1)
        self.assertEqual(self.notes(msg), [])

    async def test_joined_message_no_second_note(self) -> None:
        one, two = _msg("первое"), _msg("второе")
        first = asyncio.ensure_future(self.dispatch("первое", one))
        await _settle()
        second = asyncio.ensure_future(self.dispatch("второе", two))
        await asyncio.sleep(0.03)
        self.lock.release()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(len(self.notes(one)), 1)
        self.assertEqual(self.notes(two), [])

    async def test_cancelled_waiter_leaves_no_timer(self) -> None:
        msg = _msg("новое")
        fut = asyncio.ensure_future(self.dispatch("новое", msg))
        await _settle()
        fut.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fut
        await asyncio.sleep(0.03)
        self.assertEqual(self.notes(msg), [])
        self.lock.release()

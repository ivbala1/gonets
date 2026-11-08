"""Второй круг очереди (docs/audit-2026-11/BRIEFS.md, queue2): ни одно слово
Ивана не теряется. Оснастка из test_review_bot_main.py, порядок событий
задаётся явно (события asyncio, ручной захват замка темы)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import TimedOut

from claude_tg_bot import bot as botmod
from tests.test_review_bot_main import GROUP, _Base, _msg, _update
from tests.texts import msg_re


async def _settle(n: int = 20) -> None:
    """Дать отработать фоновым задачам (гашение процесса в /cancel)."""
    for _ in range(n):
        await asyncio.sleep(0)


class _VoiceBase(_Base):
    """Голосовое без сети: скачивание и распознавание подменены."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.allowed_chat_ids = {GROUP}
        self.bot_data["openai"] = object()
        self.heard = "Стоп."
        self.killed: list = []

        async def transcribe(_client, _cfg, _path: Path) -> str:
            return self.heard

        async def term(_key, proc, **_kw):
            self.killed.append(proc)

        for name, value in {"transcribe": transcribe, "_download_retrying": AsyncMock(),
                            "_terminate_and_wait": term}.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    def voice(self) -> SimpleNamespace:
        return _msg(voice=SimpleNamespace(duration=2, file_size=100))

    def busy(self) -> SimpleNamespace:
        proc = SimpleNamespace(pid=4242, returncode=None)
        self.bot_data["procs"][str(GROUP)] = proc
        return proc

    def said(self, msg: SimpleNamespace) -> list[str]:
        return [str(c.args[0]) for c in msg.reply_text.await_args_list]


class VoiceStopTests(_VoiceBase):
    """Голосовое «стоп» за рулём действует как текстовое слово (queue.md, 6)."""

    async def test_voice_stop_cancels_running(self) -> None:
        proc = self.busy()
        msg = self.voice()
        await botmod.on_voice(_update(msg), self.ctx)
        await _settle()
        self.assertEqual(self.killed, [proc])
        self.assertEqual(self.runs, [])
        said = self.said(msg)
        self.assertTrue(any(re.search(msg_re("⛔ Остановлено{note}"), s) for s in said), said)

    async def test_voice_nosend_cancels_with_tail(self) -> None:
        self.busy()
        self.heard = "Ничего не отправляй!"
        msg = self.voice()
        await botmod.on_voice(_update(msg), self.ctx)
        said = "\n".join(self.said(msg))
        self.assertRegex(said, msg_re("Ничего не отправляю, жду твоего слова."))
        self.assertEqual(self.runs, [])

    async def test_voice_stop_with_gather_on_cancels_running(self) -> None:
        # Склейка включена: своё же место голосового в пачке не считается
        # «есть что отменить до отправки».
        self.ctx.application.running = True
        proc = self.busy()
        msg = self.voice()
        await botmod.on_voice(_update(msg), self.ctx)
        await _settle()
        self.assertEqual(self.killed, [proc])
        said = "\n".join(self.said(msg))
        self.assertNotRegex(said, msg_re("🗑 отменено до отправки в Claude: {count} сообщ."))

    async def test_voice_stop_nothing_running_goes_to_claude(self) -> None:
        # Остановить нечего: «стоп» это ответ на вопрос Claude и уходит ему.
        msg = self.voice()
        await botmod.on_voice(_update(msg), self.ctx)
        self.assertEqual(self.killed, [])
        self.assertEqual(len(self.runs), 1)
        self.assertIn("Стоп.", self.runs[0]["prompt"])

    async def test_voice_stop_nothing_running_with_gather_on_is_not_cancel(self) -> None:
        self.ctx.application.running = True
        msg = self.voice()
        await botmod.on_voice(_update(msg), self.ctx)
        said = "\n".join(self.said(msg))
        self.assertNotRegex(said, msg_re("🗑 отменено до отправки в Claude: {count} сообщ."))
        self.assertNotRegex(said, msg_re("Сейчас ничего не идёт, останавливать нечего."))
        batch = self.bot_data["gather_batches"][str(GROUP)]
        self.assertEqual([p for p in batch.pieces], ["Стоп."])

    async def test_voice_long_phrase_with_stop_is_not_urgent(self) -> None:
        self.busy()
        self.heard = "Стоп, а что с веткой?"
        msg = self.voice()
        lock = botmod._chat_lock(self.ctx, str(GROUP))
        await lock.acquire()
        try:
            task = asyncio.ensure_future(botmod.on_voice(_update(msg), self.ctx))
            await _settle()
            self.assertEqual(self.killed, [])
        finally:
            lock.release()
        self.bot_data["procs"].clear()
        await asyncio.wait_for(task, 1)


def _drained(store) -> list[dict]:
    import json
    return [json.loads(e["v"]) for e in store.data.values() if json.loads(e["v"]).get("drained")]


class DrainDeferTests(_Base):
    """Сообщение во время ожидания перезапуска кладётся отложенным запросом
    и уходит после рестарта, порядок сохраняется (queue.md 8, runs.md 5, ux.md 9)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.allowed_chat_ids = {GROUP}
        p = patch.object(botmod, "_synthetic_update", lambda bot, cid, tid: _update(_msg(chat_id=cid)))
        p.start()
        self.addCleanup(p.stop)

    def fire_ctx(self) -> SimpleNamespace:
        [jid] = [k for k, v in self.bot_data["at_jobs"].data.items() if '"drained"' in v["v"]]
        import json
        data = {"jid": jid, **json.loads(self.bot_data["at_jobs"].data[jid]["v"])}
        return SimpleNamespace(application=self.ctx.application, bot=self.ctx.bot,
                               job=SimpleNamespace(data=data))

    async def test_message_deferred_and_sent_after_restart_in_order(self) -> None:
        self.bot_data["draining"] = True
        one, two = _msg("A"), _msg("B")
        await self.dispatch("первое", one)
        await self.dispatch("второе", two)
        self.assertEqual(self.runs, [])
        said = str(one.reply_text.await_args.args[0])
        self.assertRegex(said, msg_re("♻️ бот ждёт конца работы для перезапуска: "
                                      "сообщение уйдёт в Claude сразу после него (отмена: /cancel)"))
        self.assertNotIn("повтори", said)
        self.assertRegex(str(two.reply_text.await_args.args[0]),
                         msg_re("♻️ и это уйдёт после перезапуска, всего {count} сообщ."))
        # Рестарт: запись на диске, заново из файла.
        from claude_tg_bot.claude_runner import SessionStore
        self.bot_data["at_jobs"] = SessionStore(self.state / "at_jobs.json")
        self.assertEqual(len(_drained(self.bot_data["at_jobs"])), 1)
        self.bot_data["draining"] = False
        await botmod._at_fire(self.fire_ctx())
        self.assertEqual(len(self.runs), 1)
        prompt = self.runs[0]["prompt"]
        self.assertLess(prompt.index("первое"), prompt.index("второе"))
        self.assertEqual(_drained(self.bot_data["at_jobs"]), [])
        # Ответ на своё сообщение не помечается как «⏰ отложенный промпт».
        self.ctx.bot.send_message.assert_not_awaited()

    async def test_new_message_after_restart_carries_deferred_first(self) -> None:
        self.bot_data["draining"] = True
        await self.dispatch("первое")
        self.bot_data["draining"] = False
        jctx = self.fire_ctx()
        await self.dispatch("новое")
        self.assertEqual(len(self.runs), 1)
        prompt = self.runs[0]["prompt"]
        self.assertLess(prompt.index("первое"), prompt.index("новое"))
        # Задание, сработавшее позже, уже пусто: второго рана нет.
        await botmod._at_fire(jctx)
        self.assertEqual(len(self.runs), 1)

    async def test_cancel_drops_deferred(self) -> None:
        self.bot_data["draining"] = True
        await self.dispatch("первое")
        await self.dispatch("второе")
        stop = _msg("/cancel")
        await botmod.on_cancel(_update(stop), self.ctx)
        said = str(stop.reply_text.await_args.args[0])
        self.assertRegex(said, msg_re("🗑 отменено до отправки в Claude: {count} сообщ."))
        self.assertIn("2", said)
        self.assertEqual(_drained(self.bot_data["at_jobs"]), [])

    async def test_stop_word_drops_deferred(self) -> None:
        self.bot_data["draining"] = True
        await self.dispatch("первое")
        stop = _msg("стоп")
        await botmod.on_text(_update(stop), self.ctx)
        self.assertEqual(_drained(self.bot_data["at_jobs"]), [])
        self.assertRegex(str(stop.reply_text.await_args.args[0]),
                         msg_re("🗑 отменено до отправки в Claude: {count} сообщ."))

    async def test_queued_before_drain_keeps_order(self) -> None:
        # Сообщение ждало замок темы до начала ожидания: оно раньше тех, что
        # пришли во время ожидания, и после рестарта идёт первым.
        lock = botmod._chat_lock(self.ctx, str(GROUP))
        await lock.acquire()
        early = asyncio.ensure_future(self.dispatch("раннее"))
        await _settle()
        self.bot_data["draining"] = True
        await self.dispatch("позднее")
        lock.release()
        await asyncio.wait_for(early, 1)
        [job] = _drained(self.bot_data["at_jobs"])
        self.assertLess(job["prompt"].index("раннее"), job["prompt"].index("позднее"))

    async def test_button_answer_keeps_old_refusal(self) -> None:
        # Ответ кнопкой это данные под свою сессию: в отложенные не идёт.
        self.bot_data["draining"] = True
        msg = await self.dispatch("вариант", from_callback=True)
        self.assertEqual(_drained(self.bot_data["at_jobs"]), [])
        self.assertIn("не ушло", str(msg.reply_text.await_args.args[0]))


class InflightTests(_Base):
    """Авария посреди рана: state/inflight.json и одно сообщение «оборвано
    сбоем» при старте; при штатной остановке вести нет (runs.md, 4)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.allowed_chat_ids = {GROUP}
        self.seen: list[list[dict]] = []

        async def hook(_run):
            self.seen.append(self.records())

        self.run_hook = hook

    def records(self) -> list[dict]:
        import json
        path = self.state / "inflight.json"
        return list(json.loads(path.read_text()).values()) if path.exists() else []

    def app(self) -> SimpleNamespace:
        bot = SimpleNamespace(send_message=AsyncMock())
        return SimpleNamespace(bot_data=self.bot_data, bot=bot)

    async def test_record_lives_while_run_goes(self) -> None:
        msg = _msg("удали ветки")
        await self.dispatch("удали ветки", msg)
        [[rec]] = self.seen
        self.assertEqual(rec["message_id"], 11)
        self.assertEqual(rec["chat_id"], GROUP)
        self.assertIn("удали ветки", rec["head"])
        self.assertEqual(self.records(), [])

    async def test_crash_left_record_announced_once(self) -> None:
        from claude_tg_bot import inflight
        inflight.add(self.state, chat_id=GROUP, thread_id=5, message_id=42, prompt="удали ветки", kind="ask")
        app = self.app()
        botmod._inflight_recover(app)
        await botmod._inflight_announce(SimpleNamespace(application=app))
        self.assertEqual(self.records(), [])
        app.bot.send_message.assert_awaited_once()
        kw = app.bot.send_message.await_args.kwargs
        self.assertEqual(kw["chat_id"], GROUP)
        self.assertEqual(kw["message_thread_id"], 5)
        self.assertEqual(kw["reply_parameters"].message_id, 42)
        self.assertRegex(kw["text"], msg_re("⚠️ бот упал посреди работы и поднялся сам: ответ не дошёл "
                                            "(«{head}»). Повтори, если ещё нужно."))
        self.assertIn("удали ветки", kw["text"])
        # Второй старт ничего не повторяет.
        app2 = self.app()
        botmod._inflight_recover(app2)
        await botmod._inflight_announce(SimpleNamespace(application=app2))
        app2.bot.send_message.assert_not_awaited()

    async def test_graceful_stop_clears_record(self) -> None:
        from claude_tg_bot import inflight
        inflight.add(self.state, chat_id=GROUP, thread_id=None, message_id=42, prompt="x", kind="ask")
        app = self.app()
        botmod._inflight_recover(app)
        app.bot_data["inflight_lost"].clear()  # запись этого процесса, не прошлого
        botmod._terminate_runs(app)
        botmod._inflight_stopped(app)
        app2 = self.app()
        botmod._inflight_recover(app2)
        await botmod._inflight_announce(SimpleNamespace(application=app2))
        app2.bot.send_message.assert_not_awaited()

    async def test_no_record_while_stopping(self) -> None:
        self.bot_data["stopping"] = True
        await self.dispatch("x")
        self.assertEqual(self.records(), [])

    async def test_event_run_not_recorded(self) -> None:
        # У пробуждений своя весть после рестарта (_events_resume).
        await self.dispatch("x", unattended=True, event_ref={"event_id": "e1"})
        self.assertEqual(self.seen, [[]])

    async def test_at_recorded_only_after_start(self) -> None:
        import json
        data = {"jid": "j1", "chat_id": GROUP, "thread_id": None, "prompt": "напомни", "fire_at": 0}
        self.bot_data["at_jobs"].set("j1", json.dumps(data))
        jctx = SimpleNamespace(application=self.ctx.application, bot=self.ctx.bot,
                               job=SimpleNamespace(data=data))
        lock = botmod._chat_lock(self.ctx, str(GROUP))
        await lock.acquire()
        with patch.object(botmod, "_synthetic_update", lambda bot, cid, tid: _update(_msg(chat_id=cid))):
            t = asyncio.ensure_future(botmod._at_fire(jctx))
            await _settle()
            # Ждёт замка: запись /at на диске сама уйдёт после рестарта.
            self.assertEqual(self.records(), [])
            lock.release()
            await asyncio.wait_for(t, 1)
        [[rec]] = self.seen
        self.assertEqual(rec["kind"], "at")
        self.assertEqual(self.records(), [])


class UserStopPerRunTests(_Base):
    """Отметка остановки Иваном привязана к рану, а не к теме: /cancel и
    сразу новое сообщение не дают лишнего «отменено» (BACKLOG, ревью слияния)."""

    async def test_cancel_then_new_message_no_extra_cancelled(self) -> None:
        from claude_tg_bot.claude_runner import ClaudeResult
        self.cfg.allowed_chat_ids = {GROUP}
        ck = str(GROUP)
        a_in, a_go, b_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            proc = SimpleNamespace(pid=1 if prompt == "A" else 2, returncode=None)
            kw["process_registry"][chat_key] = proc
            self.runs.append({"prompt": prompt})
            try:
                if prompt == "A":
                    a_in.set()
                    await a_go.wait()
                    return ClaudeResult(text="⛔ отменено (signal -15)", session_id="s",
                                        is_error=True, signalled=True)
                b_started.set()
                return ClaudeResult(text="сделано B", session_id="s")
            finally:
                kw["process_registry"].pop(chat_key, None)

        async def term(_key, proc, **_kw):
            a_go.set()

        async def cleanup(*_a, **_kw):
            # Конец рана A после отпуска замка: B успевает начаться.
            if not b_started.is_set():
                await asyncio.wait_for(b_started.wait(), 1)
            return 0

        for name, value in {"run_claude": run, "_terminate_and_wait": term,
                            "_cleanup_abandoned_approvals": cleanup}.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)
        task_a = asyncio.ensure_future(self.dispatch("A", _msg("A")))
        await asyncio.wait_for(a_in.wait(), 1)
        self.assertIn(ck, self.bot_data["procs"])
        await botmod.on_cancel(_update(_msg("/cancel")), self.ctx)
        task_b = asyncio.ensure_future(self.dispatch("B", _msg("B")))
        await asyncio.wait_for(asyncio.gather(task_a, task_b), 2)
        sent = [str(c.args[2]) for c in self.send_reply_text.await_args_list]
        self.assertFalse(any("signal -15" in s for s in sent), sent)
        self.assertTrue(any("сделано B" in s for s in sent), sent)

    async def test_stale_mark_does_not_mute_next_run(self) -> None:
        # Отметка снятого рана, не дошедшего до ответа, не глушит следующий
        # ран, оборванный сигналом не Иваном.
        self.bot_data.setdefault("run_stops", {})
        botmod._mark_user_stop(self.ctx, str(GROUP))  # рана нет: отметить нечего
        self.result_error = True
        await self.dispatch("x")
        sent = [str(c.args[2]) for c in self.send_reply_text.await_args_list]
        self.assertTrue(any("signal -15" in s for s in sent), sent)


class EditedMessageTests(_Base):
    """Правка отправленного сообщения не доходит до Claude: Иван узнаёт об
    этом одной строкой без звука, а не молча (queue.md, 7)."""

    def edited(self, text: str) -> tuple[SimpleNamespace, SimpleNamespace]:
        msg = _msg(text)
        upd = _update(msg)
        upd.message = None
        upd.edited_message = msg
        upd.effective_message = msg
        return upd, msg

    async def test_edit_told_not_delivered(self) -> None:
        self.cfg.allowed_chat_ids = {GROUP}
        upd, msg = self.edited("не удаляй")
        await botmod.on_edited_text(upd, self.ctx)
        msg.reply_text.assert_awaited_once()
        self.assertRegex(str(msg.reply_text.await_args.args[0]),
                         msg_re("✏️ правка до Claude не дошла: напиши поправку новым сообщением"))
        self.assertTrue(msg.reply_text.await_args.kwargs.get("disable_notification"))
        self.assertEqual(self.runs, [])

    async def test_edit_from_stranger_ignored(self) -> None:
        self.cfg.allowed_chat_ids = set()
        upd, msg = self.edited("x")
        await botmod.on_edited_text(upd, self.ctx)
        msg.reply_text.assert_not_awaited()

    async def test_edited_command_ignored(self) -> None:
        self.cfg.allowed_chat_ids = {GROUP}
        upd, msg = self.edited("/reset force")
        await botmod.on_edited_text(upd, self.ctx)
        msg.reply_text.assert_not_awaited()


class DrainCancelWhileBusyTests(DrainDeferTests):
    """Ревью второго круга: отложенное снимается и при живом процессе, и пока
    его задание ждёт тему; дописка не теряется."""

    async def test_cancel_with_live_process_drops_record(self) -> None:
        self.bot_data["draining"] = True
        await self.dispatch("отправь файл", _msg("A"))
        self.bot_data["draining"] = False
        proc = SimpleNamespace(pid=1, returncode=None)
        with patch.object(botmod, "_terminate_and_wait", AsyncMock()):
            await botmod._cancel_running(self.ctx, botmod._chat_key(_update(_msg("x")))[0], proc)
        self.assertEqual(_drained(self.bot_data["at_jobs"]), [])

    async def test_cancel_while_job_waits_for_topic(self) -> None:
        self.bot_data["draining"] = True
        await self.dispatch("отправь файл", _msg("A"))
        self.bot_data["draining"] = False
        fire = self.fire_ctx()
        self.bot_data.setdefault("at_running", set()).add(fire.job.data["jid"])
        self.assertEqual(botmod._drained_cancel(self.ctx.application, botmod._chat_key(_update(_msg("x")))[0]), 1)
        self.bot_data["at_running"].clear()
        await botmod._at_fire(fire)
        self.assertEqual(self.runs, [])

    async def test_new_message_goes_to_own_record_when_job_waits(self) -> None:
        self.bot_data["draining"] = True
        await self.dispatch("первое", _msg("A"))
        fire = self.fire_ctx()
        self.bot_data.setdefault("at_running", set()).add(fire.job.data["jid"])
        await self.dispatch("второе", _msg("B"))
        self.assertEqual(len(_drained(self.bot_data["at_jobs"])), 2)


class QuietRunTests(_Base):
    """Метка тишины глотает только весь ответ и только в ране без звука."""

    async def _silent(self, answer: str) -> int:
        self.answer = answer
        token = botmod._SILENT_RUN.set(True)
        try:
            await self.dispatch("событие", _msg("событие"))
        finally:
            botmod._SILENT_RUN.reset(token)
        return self.send_reply_text.await_count

    async def test_silent_run_sends_nothing(self) -> None:
        self.assertEqual(await self._silent("<<QUIET>>"), 0)

    async def test_text_beside_marker_is_sent(self) -> None:
        self.assertEqual(await self._silent("Срочно ответить <<QUIET>>"), 1)
        self.assertNotIn("QUIET", self.send_reply_text.await_args.args[2])

    async def test_marker_in_chat_keeps_answer(self) -> None:
        self.answer = "Ответ по делу <<QUIET>>"
        await self.dispatch("вопрос", _msg("вопрос"))
        self.assertEqual(self.send_reply_text.await_count, 1)
        self.assertNotIn("QUIET", self.send_reply_text.await_args.args[2])


class VoiceMtprotoFallbackTests(_VoiceBase):
    """Голосовое качается общим _fetch_attachment: TimedOut getFile (случаи
    23.10, 29.10, 06.11.2026) уходит в MTProto, /cancel видит загрузку сразу."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.telegram_api_id = 1
        self.cfg.telegram_api_hash = "h"
        self.big = AsyncMock(return_value="big")
        self.dl = AsyncMock(side_effect=TimedOut())
        for name, value in {"_download_retrying": self.dl, "_fetch_big": self.big}.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def _send(self) -> SimpleNamespace:
        msg = self.voice()
        await botmod.on_voice(_update(msg), self.ctx)
        await _settle()
        return msg

    async def test_timeout_goes_to_mtproto_and_heard(self) -> None:
        msg = await self._send()
        self.big.assert_awaited_once()
        kw = self.big.await_args.kwargs
        self.assertFalse(kw["hold"])
        self.assertIsNotNone(kw["retry"])  # сбой MTProto ещё раз пробует Bot API
        self.assertTrue(self.dl.await_args.kwargs["timeout_to_mtproto"]())
        self.assertNotIn("⚠️ не удалось скачать голосовое", self.said(msg))
        self.assertEqual(len(self.runs), 1)

    async def test_mtproto_failure_stops_quietly(self) -> None:
        self.big.return_value = None  # _fetch_big уже объяснил сбой сам
        msg = await self._send()
        self.assertEqual(self.runs, [])
        self.assertNotIn("⚠️ не удалось скачать голосовое", self.said(msg))

    async def test_timeout_without_mtproto_reports_failure(self) -> None:
        self.cfg.telegram_api_id = 0
        msg = await self._send()
        self.assertFalse(self.dl.await_args.kwargs["timeout_to_mtproto"]())
        self.big.assert_not_awaited()
        self.assertIn("⚠️ не удалось скачать голосовое", self.said(msg))
        self.assertEqual(self.runs, [])

    async def test_other_error_not_sent_to_mtproto(self) -> None:
        self.dl.side_effect = RuntimeError("x")
        msg = await self._send()
        self.big.assert_not_awaited()
        self.assertIn("⚠️ не удалось скачать голосовое", self.said(msg))

    async def test_cancel_during_bot_api_drops_voice(self) -> None:
        # Код-ревью 07.11.2026: /cancel во время попытки Bot API не видел
        # загрузки, голосовое потом распознавалось и уходило к Claude.
        started = asyncio.Event()

        async def hang(*_a, **_kw):
            started.set()
            await asyncio.Event().wait()

        self.dl.side_effect = hang
        msg = self.voice()
        task = asyncio.ensure_future(botmod.on_voice(_update(msg), self.ctx))
        await started.wait()
        self.assertEqual(botmod._downloads_cancel(self.ctx.application, str(GROUP)), 1)
        await task
        await _settle()
        self.big.assert_not_awaited()
        self.assertEqual(self.runs, [])
        self.assertNotIn("⚠️ не удалось скачать голосовое", self.said(msg))

    async def test_retry_waits_for_transcription(self) -> None:
        # Повтор свайпом во время распознавания не запускает второй ход.
        self.dl.side_effect = None
        gate, seen = asyncio.Event(), []

        async def slow_stt(*_a, **_kw):
            seen.append(botmod._media_busy(self.ctx.application, msg))
            await gate.wait()
            return "Привет."
        msg = self.voice()
        with patch.object(botmod, "transcribe", slow_stt):
            task = asyncio.ensure_future(botmod.on_voice(_update(msg), self.ctx))
            while not seen:
                await asyncio.sleep(0)
            gate.set()
            await task
        self.assertEqual(seen, [True])
        self.assertFalse(botmod._media_busy(self.ctx.application, msg))

"""Аудит очереди 31.10.2026 (docs/audit-2026-10/queue.md): потеря ввода,
молчаливые отказы и гонки между await в приёме сообщений bot.py. Оснастка
из test_review_bot_main.py, порядок событий задаётся явно."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from claude_tg_bot import bot as botmod
from claude_tg_bot.claude_runner import ClaudeResult
from tests.test_review_bot_main import GROUP, _Base, _msg, _update
from tests.texts import msg_re


class CancelBeforeProcessTests(_Base):
    """/cancel и «стоп», пока ран под замком темы, а процесса claude ещё нет
    (освежение OAuth до 45 с, сверка лимита, повтор свежей сессией)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.allowed_chat_ids = {GROUP}
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.killed: list = []

        async def slow_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            self.entered.set()
            await self.gate.wait()  # ensure_oauth_fresh
            proc = SimpleNamespace(pid=4242, returncode=None)
            kw["process_registry"][chat_key] = proc
            if kw.get("on_run_start") is not None:
                kw["on_run_start"]()
            self.runs.append({"prompt": prompt})
            await asyncio.sleep(0)
            kw["process_registry"].pop(chat_key, None)
            if proc in self.killed:
                return ClaudeResult(text="⛔ отменено (signal -15)", session_id="s", is_error=True)
            return ClaudeResult(text="сделано", session_id="s")

        async def term(_key, proc, **_kw):
            self.killed.append(proc)

        for name, value in {"run_claude": slow_run, "_terminate_and_wait": term}.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def _cancel_in_window(self, text: str) -> SimpleNamespace:
        run = asyncio.ensure_future(self.dispatch("удали старые ветки"))
        await asyncio.wait_for(self.entered.wait(), 1)
        stop = _msg(text)
        if text == "/cancel":
            await botmod.on_cancel(_update(stop), self.ctx)
        else:
            await botmod.on_text(_update(stop), self.ctx)
        self.gate.set()
        await asyncio.wait_for(run, 1)
        return stop

    async def test_cancel_before_process_stops_run(self) -> None:
        stop = await self._cancel_in_window("/cancel")
        said = str(stop.reply_text.await_args.args[0])
        self.assertNotRegex(said, msg_re("Сейчас ничего не идёт, останавливать нечего."))
        # Процесс, созданный после отмены, гасится до промпта.
        self.assertEqual(len(self.killed), 1)

    async def test_stop_word_before_process_stops_run(self) -> None:
        stop = await self._cancel_in_window("стоп")
        said = str(stop.reply_text.await_args.args[0])
        self.assertNotRegex(said, msg_re("Сейчас ничего не идёт, останавливать нечего."))
        self.assertEqual(len(self.killed), 1)

    async def test_cancel_after_run_end_says_nothing_running(self) -> None:
        self.gate.set()
        await self.dispatch("раз")
        stop = _msg("/cancel")
        await botmod.on_cancel(_update(stop), self.ctx)
        self.assertRegex(str(stop.reply_text.await_args.args[0]),
                         msg_re("Сейчас ничего не идёт, останавливать нечего."))
        self.assertEqual(self.killed, [])


class SplitUtf16Tests(_Base):
    """Лимит текста Telegram считается в UTF-16: эмодзи вне BMP по две
    единицы. Кусок по числу символов Python проходил разбиение целым и
    отвергался Telegram («Message is too long»), ответ терялся весь."""

    def test_astral_emoji_chunks_fit_limit(self) -> None:
        from claude_tg_bot.replies import utf16_len, split_for_telegram
        text = "\n".join("🟢 пункт " + str(i) for i in range(600))  # около 6000 знаков
        chunks = split_for_telegram(text, 4000)
        for chunk in chunks:
            self.assertLessEqual(utf16_len(chunk), 4000)
        self.assertEqual("\n".join(chunks), text)

    def test_line_of_emoji_cut_hard(self) -> None:
        from claude_tg_bot.replies import utf16_len, split_for_telegram
        text = "😀" * 3000
        chunks = split_for_telegram(text, 4000)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(utf16_len(c) <= 4000 for c in chunks))


class ComposeLostOnStopTests(_Base):
    """Буфер /compose живёт в памяти: перезапуск (сигнал или заявка) его
    теряет. Без пометки Иван после рестарта шлёт куски дальше, и каждый
    уходит своим раном, а /go отвечает «Compose-режим не включён»."""

    async def test_stop_tells_about_lost_buffer(self) -> None:
        self.bot_data["compose_buffers"] = {
            f"{GROUP}:5": botmod.ComposeBuffer(generation=1, items=["голос", "фото"]),
            "77": botmod.ComposeBuffer(generation=1, items=[]),
        }
        bot = SimpleNamespace(send_message=AsyncMock())
        app = SimpleNamespace(bot_data=self.bot_data, bot=bot)
        await botmod._on_post_stop(app)
        calls = {(c.kwargs["chat_id"], c.kwargs.get("message_thread_id")): c.kwargs["text"]
                 for c in bot.send_message.await_args_list}
        self.assertIn((GROUP, 5), calls)
        self.assertIn("2", calls[(GROUP, 5)])
        self.assertIn((77, None), calls)
        self.assertEqual(self.bot_data["compose_buffers"], {})

    async def test_stop_without_buffers_sends_nothing(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        await botmod._on_post_stop(SimpleNamespace(bot_data=self.bot_data, bot=bot))
        bot.send_message.assert_not_awaited()


class CancelBehindBigDownloadTests(_Base):
    """Пачка склейки, ушедшая по тишине, ждёт прежнюю пачку с большой
    загрузкой (порядок темы). /cancel в это время снимал только прежнюю, а
    эта после неё уходила в Claude: отменённое воскресало."""

    async def test_cancel_drops_batch_waiting_for_previous(self) -> None:
        self.cfg.allowed_chat_ids = {GROUP}
        self.ctx.application.running = True
        ck = str(GROUP)
        prev = botmod.GatherBatch(update=_update(_msg("файл")), ctx=self.ctx, items=[None], slow=1)
        prev.closed = True
        self.bot_data["gather_waiting"] = {ck: {prev}}
        later = botmod.GatherBatch(update=_update(_msg("и вот ещё")), ctx=self.ctx,
                                   items=["и вот ещё"], prev=prev)
        later.last_arrival = 0.0
        self.bot_data["gather_batches"] = {ck: later}
        flush = asyncio.ensure_future(botmod._gather_flush(ck, later))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertTrue(later.sealed)  # ушла по тишине и ждёт прежнюю
        stop = _msg("/cancel")
        await botmod.on_cancel(_update(stop), self.ctx)
        prev.done.set()  # флаш прежней кончился после отмены
        await asyncio.wait_for(flush, 1)
        self.assertEqual(self.runs, [])
        self.assertRegex(str(stop.reply_text.await_args.args[0]),
                         msg_re("🗑 отменено до отправки в Claude: {count} сообщ."))
        self.assertIn("2", str(stop.reply_text.await_args.args[0]))


class UrgentInTopicTests(_Base):
    """В теме форума у сообщения без ответа reply_to_message может быть
    служебным сообщением создания темы. «стоп» тогда считался свайп-ответом
    и не останавливал ничего во всех темах (догадка, вживую не проверено)."""

    def test_stop_in_topic_with_header_reply(self) -> None:
        header = _msg("", thread=5, message_id=5, forum_topic_created=SimpleNamespace(name="Тема"))
        self.assertEqual(botmod._urgent_word(_msg("стоп", thread=5, reply_to_message=header)), "stop")

    def test_real_swipe_reply_is_not_urgent(self) -> None:
        quoted = _msg("ответ бота", thread=5, message_id=40, forum_topic_created=None)
        self.assertIsNone(botmod._urgent_word(_msg("стоп", thread=5, reply_to_message=quoted)))

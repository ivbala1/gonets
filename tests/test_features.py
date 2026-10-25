"""Тесты фич и защит, найденных в ревью: ASK-маркер (включая маркеры в
кодовых блоках и переросшие варианты), compose-буфер с поколением и
запечатыванием, склейка сообщений, пришедших разом, синтетический Update, политика фоновых ранов, эпоха сессии,
diff-карточка, парсер /at."""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot.approval import _format_tool_call  # noqa: E402
from claude_tg_bot.bot import (  # noqa: E402
    ComposeBuffer,
    _bump_session_epoch,
    _chat_key,
    _compose_add,
    _compose_fill,
    _compose_release,
    _compose_reserve,
    _forward_note,
    _gather_cancel,
    _gather_fill,
    _gather_release,
    _gather_reserve,
    _gather_send_now,
    _gather_take,
    _parse_at_when,
    _session_epoch,
    _synthetic_update,
)
from claude_tg_bot.replies import (  # noqa: E402
    extract_ask_marker,
    load_full_text,
    plan_voice_series,
    render_markup,
    save_full_text,
    send_files,
    send_reply_text,
    send_voice_caption,
    voice_text,
    with_counter,
)
from telegram import MessageEntity  # noqa: E402
import httpx  # noqa: E402
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut  # noqa: E402


def _ctx(bot_data: dict | None = None) -> SimpleNamespace:
    """Минимальный ctx: хелперам нужен только ctx.application.bot_data."""
    return SimpleNamespace(application=SimpleNamespace(bot_data=bot_data if bot_data is not None else {}))


class AskMarkerTests(unittest.TestCase):
    def test_marker_parsed_and_removed(self) -> None:
        ask, cleaned = extract_ask_marker(
            "Есть два пути.\n\n<<ASK:Что делаем?|Откатить|Чинить>>"
        )
        self.assertEqual(ask, ("Что делаем?", ["Откатить", "Чинить"]))
        self.assertEqual(cleaned, "Есть два пути.")

    def test_marker_without_options_is_left_as_text(self) -> None:
        # Одного вопроса без вариантов мало для кнопок — пользователь должен
        # хотя бы прочитать вопрос, а не потерять его вместе с маркером.
        ask, cleaned = extract_ask_marker("<<ASK:А просто вопрос?>>")
        self.assertIsNone(ask)
        self.assertIn("просто вопрос", cleaned)

    def test_only_first_marker_becomes_buttons_second_stays_readable(self) -> None:
        ask, cleaned = extract_ask_marker("<<ASK:A?|1|2>> хвост <<ASK:B?|3|4>>")
        self.assertEqual(ask[0], "A?")
        # Второй вопрос кнопками не станет, но и потеряться не должен.
        self.assertIn("B?", cleaned)

    def test_options_capped_at_four(self) -> None:
        ask, _ = extract_ask_marker("<<ASK:Q?|1|2|3|4|5|6>>")
        self.assertEqual(len(ask[1]), 4)

    def test_marker_inside_code_fence_ignored(self) -> None:
        """Claude цитирует README/файл с примером маркера — это не вопрос."""
        text = "Формат такой:\n```\n<<ASK:Пример?|да|нет>>\n```\nвот и всё"
        ask, cleaned = extract_ask_marker(text)
        self.assertIsNone(ask)
        self.assertIn("<<ASK:Пример?|да|нет>>", cleaned)

    def test_overlong_option_rejected(self) -> None:
        """Хвост, который не влезет на кнопку, не должен уходить в промпт."""
        ask, cleaned = extract_ask_marker(f"<<ASK:Q?|ок|{'x' * 200}>>")
        self.assertIsNone(ask)
        self.assertIn("ASK:Q?", cleaned)

    def test_plain_text_untouched(self) -> None:
        ask, cleaned = extract_ask_marker("обычный ответ без маркеров")
        self.assertIsNone(ask)
        self.assertEqual(cleaned, "обычный ответ без маркеров")


class VoiceTextTests(unittest.TestCase):
    def test_sources_block_cut(self) -> None:
        reply = (
            "Крепость Клдекари стоит на скале.\n\n"
            "**Источники:**\n"
            "- [Википедия](https://ru.wikipedia.org/wiki/Клдекари)\n"
            "- https://example.ge/kldekari\n"
            "3. Путеводитель по Картли\n\n"
            "Хорошей дороги!"
        )
        spoken, dropped = voice_text(reply)
        self.assertTrue(dropped)
        self.assertEqual(spoken, "Крепость Клдекари стоит на скале.\n\nХорошей дороги!")

    def test_inline_urls_removed(self) -> None:
        spoken, dropped = voice_text(
            "Подробнее на сайте (https://example.ge/a), а карта в [OSM](https://osm.org/x).\n"
            "https://example.ge/b")
        self.assertTrue(dropped)
        self.assertEqual(spoken, "Подробнее на сайте, а карта в OSM.")

    def test_one_line_sources_and_english_head(self) -> None:
        self.assertEqual(voice_text("Текст.\nИсточники: https://a.ge, https://b.ge")[0], "Текст.")
        self.assertEqual(voice_text("Текст.\n## Sources\n1. https://a.ge")[0], "Текст.")

    def test_prose_about_source_kept(self) -> None:
        reply = "Источник пишет, что крепость построили в десятом веке.\nСсылки на неё есть в летописях."
        self.assertEqual(voice_text(reply), (reply, False))

    def test_markup_stripped_without_links(self) -> None:
        self.assertEqual(voice_text("**Жирно** и `код`"), ("Жирно и код", False))


class VoiceSeriesTests(unittest.TestCase):
    def test_short_reply_one_chunk(self) -> None:
        reply = "Коротко.\n\n```\nls -la\n```\nВсё."
        self.assertEqual(plan_voice_series(reply, 1024), (["Коротко.\n\nВсё."], ["```\nls -la\n```"]))
        self.assertEqual(with_counter("Кусок", 1, 1), "Кусок")
        self.assertEqual(with_counter("Кусок", 2, 3), "Кусок\n\n2/3")

    def test_long_reply_fits_captions(self) -> None:
        para = "**Абзац** про очередь в Доме юстиции 🎫, с [ссылкой](https://a.ge/x). " * 6
        reply = "\n\n".join(f"{i}. {para}" for i in range(8))
        chunks, fences = plan_voice_series(reply, 1024)
        self.assertGreater(len(chunks), 2)
        self.assertEqual(fences, [])
        for k, chunk in enumerate(chunks):
            plain, entities = render_markup(with_counter(chunk, k + 1, len(chunks)))
            self.assertLessEqual(len(plain.encode("utf-16-le")) // 2, 1024)
            # Перенос по строкам не рвёт разметку: значков в тексте нет.
            self.assertNotIn("**", plain)
            self.assertTrue(entities)
        # Текст не теряется и не повторяется.
        self.assertEqual("\n\n".join(chunks).split(), reply.split())

    def test_one_huge_line_split_by_sentences(self) -> None:
        reply = " ".join(f"Предложение номер {i} про штрафы." for i in range(200))
        chunks, _fences = plan_voice_series(reply, 1024)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 1012 for c in chunks))
        self.assertEqual(" ".join(chunks), reply)

    def test_review_cases(self) -> None:
        # Ревью Codex 21.10.2026: крошечный лимит без зацикливания, эмодзи в
        # UTF-16 не больше подписи, ссылка с пробелами в тексте не рвётся.
        self.assertTrue(plan_voice_series("слово " * 50, 12)[0])
        chunks, _f = plan_voice_series("🎫" * 1100, 1024)
        for k, c in enumerate(chunks):
            plain = render_markup(with_counter(c, k + 1, len(chunks)))[0]
            self.assertLessEqual(len(plain.encode("utf-16-le")) // 2, 1024)
        link = "[очень длинная ссылка " + "про очередь " * 20 + "в Доме юстиции](https://a.ge/x)"
        reply = " ".join(["Текст про штрафы и сроки подачи."] * 20 + [link] + ["Ещё текст."] * 20)
        chunks, _f = plan_voice_series(reply, 1024)
        self.assertEqual(sum(link in c for c in chunks), 1)

    def test_series_first_chunk_only_links(self) -> None:
        # Ревью Codex 21.10.2026: первый кусок без слов, второй длиннее лимита
        # синтеза; раньше max() падал на пустом списке и ответ терялся.
        links = "\n".join(f"https://a.ge/{i}/" + "x" * 80 for i in range(10))
        reply = links + "\n\n" + "слово " * 150
        ok, msg = self._series(reply, max_chars=100)
        self.assertTrue(ok)
        self.assertEqual(msg.reply_voice.await_count, 1)

    def test_series_without_button_when_save_fails(self) -> None:
        reply = "\n\n".join(f"Абзац {i}. " + "слово " * 150 for i in range(3))
        with patch.object(botmod, "save_full_text", side_effect=OSError("disk full")):
            ok, msg = self._series(reply)
        self.assertTrue(ok)
        self.assertTrue(all(c.kwargs["reply_markup"] is None for c in msg.reply_voice.call_args_list))

    def _series(self, reply: str, tts=None, max_chars: int = 4000):
        msg = SimpleNamespace(reply_voice=AsyncMock(), reply_text=AsyncMock())
        ctx = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(telegram_caption_limit=1024, voice_reply_max_chars=max_chars,
                                  state_dir=Path(tmp), telegram_text_limit=4000,
                                  large_reply_as_file_threshold=12000)
            with patch.object(botmod, "synthesize", tts or AsyncMock(return_value=b"ogg")):
                ok = asyncio.run(botmod._send_voice_series(
                    cfg, ctx, SimpleNamespace(message=msg), 42, object(), reply))
        return ok, msg

    def test_series_sends_in_order_with_button_on_last(self) -> None:
        reply = "\n\n".join(f"Абзац {i}. " + "слово " * 150 for i in range(4)) + "\n```\nls\n```"
        ok, msg = self._series(reply)
        self.assertTrue(ok)
        calls = msg.reply_voice.call_args_list
        self.assertGreater(len(calls), 1)
        n = len(calls)
        for k, c in enumerate(calls):
            self.assertTrue(c.kwargs["caption"].endswith(f"{k + 1}/{n}"))
            self.assertEqual(c.kwargs["reply_markup"] is not None, k == n - 1)
        self.assertTrue(calls[0].kwargs["caption"].startswith("Абзац 0."))
        # Блок кода текстом после серии, без копии ответа.
        msg.reply_text.assert_awaited_once()
        self.assertEqual(msg.reply_text.call_args.args[0], "ls")

    def test_series_rest_as_text_over_tts_limit(self) -> None:
        reply = "\n\n".join(f"Абзац {i}. " + "слово " * 150 for i in range(4))
        ok, msg = self._series(reply, max_chars=1000)
        self.assertTrue(ok)
        self.assertEqual(msg.reply_voice.await_count, 1)
        self.assertIn("Абзац 3.", msg.reply_text.call_args.args[0])
        self.assertNotIn("Абзац 0.", msg.reply_text.call_args.args[0])

    def test_series_tts_fail_first_gives_up(self) -> None:
        ok, msg = self._series("Короткий ответ.", tts=AsyncMock(side_effect=RuntimeError))
        self.assertFalse(ok)
        msg.reply_voice.assert_not_awaited()

    def test_full_text_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            token = save_full_text(state, "весь ответ\nв две строки", 42)
            self.assertEqual(load_full_text(state, token, 42), "весь ответ\nв две строки")
            self.assertIsNone(load_full_text(state, token, 43))
            self.assertIsNone(load_full_text(state, "../../etc/passwd", 42))
            self.assertIsNone(load_full_text(state, "0" * 16, 42))


class MarkupTests(unittest.TestCase):
    def _spans(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        plain, ents = render_markup(text)
        utf16 = plain.encode("utf-16-le")
        spans = [(e.type, utf16[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")) for e in ents]
        return plain, spans

    def test_coords_become_code(self) -> None:
        plain, spans = self._spans("Точка `41.9343, 46.1606`, телефон `+995 32 222 24 42`.")
        self.assertEqual(plain, "Точка 41.9343, 46.1606, телефон +995 32 222 24 42.")
        self.assertEqual(spans, [("code", "41.9343, 46.1606"), ("code", "+995 32 222 24 42")])

    def test_emoji_offsets_utf16(self) -> None:
        plain, spans = self._spans("🏰 Крепость 👉 `41.8524, 46.2046` ⛰ **жирно** 🎉")
        self.assertEqual(spans, [("code", "41.8524, 46.2046"), ("bold", "жирно")])
        self.assertEqual(plain, "🏰 Крепость 👉 41.8524, 46.2046 ⛰ жирно 🎉")

    def test_all_inline_styles(self) -> None:
        plain, spans = self._spans(
            "**ж** *к* _к2_ __п__ ~~з~~ ||с|| [ссылка](https://ya.ru)")
        self.assertEqual(plain, "ж к к2 п з с ссылка")
        self.assertEqual([t for t, _ in spans],
                         ["bold", "italic", "italic", "underline", "strikethrough", "spoiler", "text_link"])
        self.assertEqual(render_markup("[a](https://ya.ru)")[1][0].url, "https://ya.ru")

    def test_nested_bold_italic(self) -> None:
        plain, spans = self._spans("**жирный и _курсив_ внутри**")
        self.assertEqual(plain, "жирный и курсив внутри")
        self.assertIn(("bold", "жирный и курсив внутри"), spans)
        self.assertIn(("italic", "курсив"), spans)

    def test_code_inside_bold_is_plain(self) -> None:
        # Telegram не допускает code внутри bold: кавычки убраны, сущности code нет.
        plain, spans = self._spans("**см. `x`**")
        self.assertEqual((plain, spans), ("см. x", [("bold", "см. x")]))

    def test_fence_becomes_pre(self) -> None:
        plain, ents = render_markup("до\n```bash\nls `pwd` **x**\n```\nпосле `x`")
        self.assertEqual(plain, "до\nls `pwd` **x**\nпосле x")
        self.assertEqual([(e.type, e.language) for e in ents], [("pre", "bash"), ("code", None)])

    def test_quote_and_heading(self) -> None:
        plain, spans = self._spans("# Итог\n> строка 1\n> **строка 2**\nхвост")
        self.assertEqual(plain, "Итог\nстрока 1\nстрока 2\nхвост")
        self.assertIn(("bold", "Итог"), spans)
        self.assertIn(("blockquote", "строка 1\nстрока 2"), spans)
        self.assertIn(("bold", "строка 2"), spans)

    def test_false_positives_untouched(self) -> None:
        for text in (
            "одна ` кавычка", "`начало\nконец`", "пусто ``", "пробелы `  `",
            "набери *111# и жди", "snake_case_name и 2*3*4", "a ** b ** c",
            "#хештег", "->стрелка", "x > y", "[текст](не ссылка)", "обычный текст",
        ):
            plain, ents = render_markup(text)
            self.assertEqual((plain, ents), (text, []), text)


class SendReplyInlineCodeTests(unittest.TestCase):
    def _cfg(self):
        return SimpleNamespace(large_reply_as_file_threshold=12000, telegram_text_limit=4000)

    def test_entities_passed(self) -> None:
        msg = SimpleNamespace(reply_text=AsyncMock())
        asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "к `41.1, 46.2`"))
        args, kwargs = msg.reply_text.call_args
        self.assertEqual(args[0], "к 41.1, 46.2")
        self.assertEqual(kwargs["entities"][0].offset, 2)

    def test_plain_without_backticks(self) -> None:
        msg = SimpleNamespace(reply_text=AsyncMock())
        asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "просто текст"))
        msg.reply_text.assert_awaited_once_with("просто текст")

    def test_fallback_on_bad_request(self) -> None:
        msg = SimpleNamespace(reply_text=AsyncMock(side_effect=[BadRequest("bad entities"), None]))
        asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "к `x`"))
        self.assertEqual(msg.reply_text.await_args_list[-1].args, ("к `x`",))

    @staticmethod
    def _net_err(exc: Exception, cause: Exception) -> Exception:
        exc.__cause__ = cause
        return exc

    def test_retry_when_connect_failed(self) -> None:
        # Соединение не установлено: сообщение не ушло, повтор без дубля.
        err = self._net_err(NetworkError("httpx.ConnectError: "), httpx.ConnectError(""))
        msg = SimpleNamespace(reply_text=AsyncMock(side_effect=[err, None]))
        with patch("claude_tg_bot.replies.asyncio.sleep", AsyncMock()) as sleep:
            asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "просто текст"))
        self.assertEqual(msg.reply_text.await_count, 2)
        sleep.assert_awaited_once()

    def test_no_retry_on_read_timeout(self) -> None:
        # Тайм-аут чтения: сообщение могло дойти, повтор дал бы дубль.
        err = self._net_err(TimedOut(), httpx.ReadTimeout(""))
        msg = SimpleNamespace(reply_text=AsyncMock(side_effect=[err, None]))
        with patch("claude_tg_bot.replies.asyncio.sleep", AsyncMock()):
            with self.assertRaises(TimedOut):
                asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "просто текст"))
        self.assertEqual(msg.reply_text.await_count, 1)

    def test_retry_gives_up(self) -> None:
        errs = [self._net_err(NetworkError("httpx.ConnectError: "), httpx.ConnectError("")) for _ in range(3)]
        msg = SimpleNamespace(reply_text=AsyncMock(side_effect=errs))
        with patch("claude_tg_bot.replies.asyncio.sleep", AsyncMock()):
            with self.assertRaises(NetworkError):
                asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "просто текст"))
        self.assertEqual(msg.reply_text.await_count, 3)

    def test_retry_after_flood_control(self) -> None:
        # 429: Telegram запрос не выполнил, пережидаем паузу и шлём снова (случай 14.10.2026).
        msg = SimpleNamespace(reply_text=AsyncMock(side_effect=[RetryAfter(22), None]))
        with patch("claude_tg_bot.replies.asyncio.sleep", AsyncMock()) as sleep:
            asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "просто текст"))
        self.assertEqual(msg.reply_text.await_count, 2)
        sleep.assert_awaited_once_with(23.0)

    def test_retry_after_gives_up(self) -> None:
        # Третий 429 подряд и слишком долгая пауза пробрасываются: ответ не висит бесконечно.
        msg = SimpleNamespace(reply_text=AsyncMock(side_effect=[RetryAfter(1) for _ in range(3)]))
        with patch("claude_tg_bot.replies.asyncio.sleep", AsyncMock()):
            with self.assertRaises(RetryAfter):
                asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "просто текст"))
        self.assertEqual(msg.reply_text.await_count, 3)
        msg = SimpleNamespace(reply_text=AsyncMock(side_effect=[RetryAfter(300), None]))
        with patch("claude_tg_bot.replies.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaises(RetryAfter):
                asyncio.run(send_reply_text(self._cfg(), SimpleNamespace(message=msg), "просто текст"))
        sleep.assert_not_awaited()

    def test_voice_caption_entities(self) -> None:
        msg = SimpleNamespace(reply_voice=AsyncMock())
        asyncio.run(send_voice_caption(msg, b"ogg", "точка `1, 2`"))
        kwargs = msg.reply_voice.call_args.kwargs
        self.assertEqual(kwargs["caption"], "точка 1, 2")
        self.assertEqual(len(kwargs["caption_entities"]), 1)


class SendFilesTests(unittest.TestCase):
    def _send(self, err: Exception | None) -> SimpleNamespace:
        msg = SimpleNamespace(reply_document=AsyncMock(side_effect=err), reply_text=AsyncMock())
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "track.gpx"
            f.write_text("x")
            asyncio.run(send_files(SimpleNamespace(message=msg), [f]))
        return msg

    def test_long_timeouts(self) -> None:
        msg = self._send(None)
        kwargs = msg.reply_document.await_args.kwargs
        self.assertGreaterEqual(kwargs["read_timeout"], 60)
        self.assertGreaterEqual(kwargs["write_timeout"], 60)
        msg.reply_text.assert_not_awaited()

    def test_read_timeout_says_probably_delivered(self) -> None:
        # Случай 25.10.2026: файл загружен, не пришло подтверждение.
        err = TimedOut()
        err.__cause__ = httpx.ReadTimeout("")
        msg = self._send(err)
        msg.reply_document.assert_awaited_once()  # без повтора: дубль файла
        text = msg.reply_text.await_args.args[0]
        self.assertIn("track.gpx", text)
        self.assertIn("мог дойти", text)

    def test_other_error_says_failed(self) -> None:
        err = TimedOut()
        err.__cause__ = httpx.WriteTimeout("")
        text = self._send(err).reply_text.await_args.args[0]
        self.assertIn("не удалось отправить файл track.gpx", text)


class ComposeBufferTests(unittest.TestCase):
    def _ctx_with_buffer(self, generation: int = 1):
        return _ctx({"compose_buffers": {"chat": ComposeBuffer(generation=generation)}})

    def _buf(self, ctx) -> ComposeBuffer:
        return ctx.application.bot_data["compose_buffers"]["chat"]

    def test_inactive_buffer_returns_none(self) -> None:
        self.assertIsNone(_compose_add(_ctx(), "chat", "текст"))
        self.assertIsNone(_compose_reserve(_ctx(), "chat"))

    def test_slot_reserved_before_slow_io_keeps_order_and_blocks_go(self) -> None:
        """Регрессия: /go во время STT не должен терять голосовое."""
        ctx = self._ctx_with_buffer()
        res = _compose_reserve(ctx, "chat")  # начало обработки голосового
        _compose_add(ctx, "chat", "текст, пришедший следом")
        buf = self._buf(ctx)
        self.assertFalse(buf.ready)  # /go обязан подождать
        self.assertEqual(buf.filled, 1)
        self.assertEqual(_compose_fill(ctx, "chat", res, "распознанный голос"), 2)
        self.assertTrue(buf.ready)
        self.assertEqual(buf.items, ["распознанный голос", "текст, пришедший следом"])

    def test_release_unblocks_go_when_handler_fails(self) -> None:
        """Регрессия: сбой STT не должен заставлять /go ждать все 30 секунд."""
        ctx = self._ctx_with_buffer()
        res = _compose_reserve(ctx, "chat")
        self.assertFalse(self._buf(ctx).ready)
        _compose_release(ctx, "chat", res)  # скачивание/распознавание упало
        self.assertTrue(self._buf(ctx).ready)
        self.assertEqual([x for x in self._buf(ctx).items if x], [])

    def test_fill_after_go_returns_none(self) -> None:
        """Если /go уже забрал буфер, кусок уходит обычным путём, не теряясь."""
        ctx = self._ctx_with_buffer()
        res = _compose_reserve(ctx, "chat")
        ctx.application.bot_data["compose_buffers"].pop("chat")  # /go забрал
        self.assertIsNone(_compose_fill(ctx, "chat", res, "поздний кусок"))

    def test_late_piece_does_not_land_in_new_buffer(self) -> None:
        """ABA: отмена буфера и новый /compose — старый кусок не должен
        попасть в новый буфер по совпадающему индексу слота."""
        ctx = self._ctx_with_buffer(generation=1)
        res = _compose_reserve(ctx, "chat")
        ctx.application.bot_data["compose_buffers"]["chat"] = ComposeBuffer(generation=2)
        _compose_add(ctx, "chat", "кусок нового буфера")
        self.assertIsNone(_compose_fill(ctx, "chat", res, "кусок старого буфера"))
        self.assertEqual(self._buf(ctx).items, ["кусок нового буфера"])

    def test_sealed_buffer_refuses_new_reservations(self) -> None:
        """После /go новые куски идут обычным путём, а не в уезжающий буфер."""
        ctx = self._ctx_with_buffer()
        self._buf(ctx).sealed = True
        self.assertIsNone(_compose_reserve(ctx, "chat"))
        self.assertIsNone(_compose_add(ctx, "chat", "поздний текст"))


def _msg_update() -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(set_reaction=AsyncMock()))


class GatherTests(unittest.IsolatedAsyncioTestCase):
    """Склейка сообщений, пришедших почти разом (пересылка с комментарием)."""

    def setUp(self) -> None:
        self.dispatch = AsyncMock()
        for p in (
            patch.object(botmod, "_dispatch_to_claude", self.dispatch),
            patch.object(botmod, "GATHER_WINDOW_SEC", 0.05),
            patch.object(botmod, "GATHER_MAX_WAIT_SEC", 0.5),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.tasks: list[asyncio.Task] = []

        def create_task(coro, update=None, *, name=None):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        self.ctx = SimpleNamespace(application=SimpleNamespace(
            bot_data={}, running=True, create_task=create_task))

    async def _drain(self) -> None:
        # Флаш сам заводит задачу реакций, поэтому ждём, пока новые не перестанут появляться.
        done = 0
        while done < len(self.tasks):
            pending = self.tasks[done:]
            done = len(self.tasks)
            await asyncio.gather(*pending)

    def _sent(self) -> list[str]:
        return [c.args[2] for c in self.dispatch.await_args_list]

    async def test_comment_and_forwarded_photo_go_as_one_request(self) -> None:
        """Регрессия: комментарий уезжал отдельным раном, пересланное получало
        «claude ещё работает над прошлым запросом»."""
        comment, photo = _msg_update(), _msg_update()
        g1 = _gather_reserve(comment, self.ctx, "chat")
        self.assertTrue(_gather_fill(g1, "вакансия от Ксюши"))
        g2 = _gather_reserve(photo, self.ctx, "chat")  # фото ещё скачивается
        await asyncio.sleep(0.1)  # окно тишины прошло, но фото не готово
        self.dispatch.assert_not_awaited()
        self.assertTrue(_gather_fill(g2, "[изображение]"))
        await self._drain()
        self.assertEqual(self._sent(), ["вакансия от Ксюши\n\n---\n\n[изображение]"])
        self.assertIs(self.dispatch.await_args.args[0], comment)  # ответ на первое
        photo.message.set_reaction.assert_awaited_once_with("👀")

    async def test_single_message_dispatched_unchanged(self) -> None:
        upd = _msg_update()
        _gather_fill(_gather_reserve(upd, self.ctx, "chat"), "привет")
        await self._drain()
        self.assertEqual(self._sent(), ["привет"])
        self.assertFalse(self.dispatch.await_args.kwargs["send_voice_reply"])

    async def test_failed_piece_released_rest_still_sent(self) -> None:
        g1 = _gather_reserve(_msg_update(), self.ctx, "chat")
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "текст")
        _gather_release(g1)  # скачивание упало
        await self._drain()
        self.assertEqual(self._sent(), ["текст"])

    async def test_all_failed_nothing_sent(self) -> None:
        _gather_release(_gather_reserve(_msg_update(), self.ctx, "chat"))
        await self._drain()
        self.dispatch.assert_not_awaited()

    async def test_late_piece_not_lost(self) -> None:
        """Кусок, не успевший за GATHER_MAX_WAIT_SEC, обработчик шлёт сам."""
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "текст")
        slow = _gather_reserve(_msg_update(), self.ctx, "chat")
        await self._drain()
        self.assertEqual(self._sent(), ["текст"])
        self.assertFalse(_gather_fill(slow, "долгое голосовое"))

    async def test_voice_piece_keeps_voice_reply(self) -> None:
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "текст")
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "голос", voice=True)
        await self._drain()
        self.assertTrue(self.dispatch.await_args.kwargs["send_voice_reply"])

    async def test_next_batch_does_not_wait_for_previous_run(self) -> None:
        """Регрессия 22.10.2026: сообщение, написанное во время долгого рана,
        ждало его конца и вставало в очередь замка за кнопками и событиями,
        пришедшими позже (сообщение 11:00 дошло до Claude в 11:57)."""
        release = asyncio.Event()

        async def long_run(*args, **kwargs):
            await release.wait()

        self.dispatch.side_effect = long_run
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "первое")
        await asyncio.sleep(0.3)  # первая пачка ушла (флаш проверяет раз в 0,2 с), её ран идёт
        self.assertEqual(self._sent(), ["первое"])
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "второе")
        await asyncio.sleep(0.3)
        self.assertEqual(self._sent(), ["первое", "второе"])  # второе уже у замка
        release.set()
        await self._drain()

    async def test_next_message_after_flush_opens_new_batch(self) -> None:
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "первое")
        await self._drain()
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "второе")
        await self._drain()
        self.assertEqual(self._sent(), ["первое", "второе"])

    async def test_topics_are_separate(self) -> None:
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat:1"), "тема 1")
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat:2"), "тема 2")
        await self._drain()
        self.assertEqual(sorted(self._sent()), ["тема 1", "тема 2"])

    async def test_disabled_window_returns_none(self) -> None:
        with patch.object(botmod, "GATHER_WINDOW_SEC", 0):
            self.assertIsNone(_gather_reserve(_msg_update(), self.ctx, "chat"))

    async def test_continuous_stream_capped_by_max_wait(self) -> None:
        """Сообщения чаще окна тишины не должны держать пачку бесконечно."""
        with patch.object(botmod, "GATHER_MAX_WAIT_SEC", 0.3):
            loop = asyncio.get_running_loop()
            start = loop.time()
            while not self.dispatch.await_count and loop.time() - start < 1.0:
                _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "кусок")
                await asyncio.sleep(0.02)  # чаще окна 0.05
            self.assertTrue(self.dispatch.await_count)
            self.assertLess(loop.time() - start, 0.6)
        _gather_cancel(self.ctx, "chat")  # хвост потока, начавший новую пачку
        await self._drain()

    async def test_cancel_drops_pending_and_late_piece(self) -> None:
        """Регрессия: /cancel сразу после сообщения отвечал «нет процесса», а
        запрос потом всё равно уходил; позднее вложение воскрешало его."""
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "текст")
        photo = _gather_reserve(_msg_update(), self.ctx, "chat")  # ещё скачивается
        self.assertEqual(_gather_cancel(self.ctx, "chat"), 2)
        self.assertTrue(_gather_fill(photo, "[изображение]"))  # выброшено, не шлётся
        await self._drain()
        self.dispatch.assert_not_awaited()
        self.assertEqual(_gather_cancel(self.ctx, "chat"), 0)

    async def test_take_for_correction(self) -> None:
        """«!»-поправка забирает ожидающее, флаш его второй раз не шлёт, а
        недокачанное вложение уходит само, не теряясь."""
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "сделай X")
        slow = _gather_reserve(_msg_update(), self.ctx, "chat")
        batch = _gather_take(self.ctx, "chat")
        self.assertEqual(batch.pieces, ["сделай X"])
        self.assertFalse(_gather_fill(slow, "[изображение]"))
        await self._drain()
        self.dispatch.assert_not_awaited()

    async def test_shutdown_drops_with_notice(self) -> None:
        upd = _msg_update()
        upd.message.reply_text = AsyncMock()
        _gather_fill(_gather_reserve(upd, self.ctx, "chat"), "текст")
        self.ctx.application.running = False  # stop() начался
        await self._drain()
        self.dispatch.assert_not_awaited()
        upd.message.reply_text.assert_awaited_once()

    async def test_shutdown_with_only_unfinished_piece(self) -> None:
        """Уведомление и без готовых кусков; докачавшийся кусок в Claude не уходит."""
        upd = _msg_update()
        upd.message.reply_text = AsyncMock()
        slow = _gather_reserve(upd, self.ctx, "chat")
        self.ctx.application.running = False
        await self._drain()
        upd.message.reply_text.assert_awaited_once()
        self.assertTrue(_gather_fill(slow, "[изображение]"))  # выброшено, не шлётся сам
        self.dispatch.assert_not_awaited()

    async def test_no_new_batch_during_shutdown(self) -> None:
        """Задачу, созданную после начала stop(), PTB уже не ждёт."""
        self.ctx.application.running = False
        self.assertIsNone(_gather_reserve(_msg_update(), self.ctx, "chat"))

    async def test_send_now_before_compose(self) -> None:
        """Текст до /compose уходит сразу, а не после содержимого буфера."""
        _gather_fill(_gather_reserve(_msg_update(), self.ctx, "chat"), "текст A")
        _gather_send_now(self.ctx, "chat")
        await self._drain()
        self.assertEqual(self._sent(), ["текст A"])  # флаш второй раз не шлёт


class DispatchOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_reaction_does_not_lose_queue_place(self) -> None:
        """Регрессия (ревью): await реакции до замка давал следующему запросу
        темы обогнать этот: текст перед /compose уходил после буфера."""
        order: list[str] = []
        tasks: list[asyncio.Task] = []

        def upd(name: str, slow: bool) -> SimpleNamespace:
            async def set_reaction(_emoji: str) -> None:
                if slow:
                    await asyncio.sleep(0.1)

            async def reply_text(_text: str, **_kw) -> None:
                order.append(name)  # внутри замка: вход ещё идёт, ран не стартует

            return SimpleNamespace(
                effective_chat=SimpleNamespace(id=1),
                message=SimpleNamespace(
                    message_thread_id=None, is_topic_message=False,
                    set_reaction=set_reaction, reply_text=reply_text,
                ),
            )

        def create_task(coro, update=None, *, name=None):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        bot_data = {
            "cfg": SimpleNamespace(codex_trigger_prefix="gpt:", codex_bin=""),
            "sessions": None, "procs": {}, "active_sessions": {}, "session_targets": {},
            "chat_locks": {}, "login_manager": SimpleNamespace(session=object()),
        }
        ctx = SimpleNamespace(
            application=SimpleNamespace(bot_data=bot_data, running=True, create_task=create_task),
            bot=SimpleNamespace(send_chat_action=AsyncMock()),
        )
        a = asyncio.create_task(botmod._dispatch_to_claude(upd("A", True), ctx, "A", send_voice_reply=False))
        await asyncio.sleep(0)  # A уже начал отправку
        b = asyncio.create_task(botmod._dispatch_to_claude(upd("B", False), ctx, "B", send_voice_reply=False))
        await asyncio.gather(a, b)
        await asyncio.gather(*tasks)
        self.assertEqual(order, ["A", "B"])

    async def test_busy_notice_comes_before_answer(self) -> None:
        """Уведомление «встанет в очередь» фоном, но не позже ответа, и
        удаляется, когда очередь дошла до запроса."""
        order: list[str] = []

        async def delete() -> None:
            order.append("deleted")

        async def reply_text(text: str, **_kw) -> object:
            if text.startswith("⏳"):
                await asyncio.sleep(0.1)  # медленная сеть
                order.append("notice")
                return SimpleNamespace(delete=delete)
            order.append("answer")
            return None

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1),
            message=SimpleNamespace(
                message_thread_id=None, is_topic_message=False,
                set_reaction=AsyncMock(), reply_text=reply_text,
            ),
        )
        lock = asyncio.Lock()
        bot_data = {
            "cfg": SimpleNamespace(codex_trigger_prefix="gpt:", codex_bin=""),
            "sessions": None, "procs": {}, "active_sessions": {}, "session_targets": {},
            "chat_locks": {"1": lock}, "login_manager": SimpleNamespace(session=object()),
        }
        ctx = SimpleNamespace(
            application=SimpleNamespace(
                bot_data=bot_data, running=True,
                create_task=lambda coro, update=None, *, name=None: asyncio.create_task(coro),
            ),
            bot=SimpleNamespace(send_chat_action=AsyncMock()),
        )
        await lock.acquire()  # тема занята прошлым раном
        run = asyncio.create_task(botmod._dispatch_to_claude(update, ctx, "B", send_voice_reply=False))
        await asyncio.sleep(0)
        lock.release()  # прошлый ран закончился раньше, чем ушло уведомление
        await run
        self.assertEqual(order, ["notice", "deleted", "answer"])


class ForwardNoteTests(unittest.TestCase):
    def test_own_message_has_no_note(self) -> None:
        self.assertEqual(_forward_note(SimpleNamespace(forward_origin=None)), "")

    def test_forwarded_from_user(self) -> None:
        origin = SimpleNamespace(
            sender_user=SimpleNamespace(full_name="Kseniia Safronova", first_name="Kseniia"),
            sender_user_name=None, chat=None, sender_chat=None,
        )
        self.assertEqual(
            _forward_note(SimpleNamespace(forward_origin=origin)),
            "[Переслано от: Kseniia Safronova]\n",
        )

    def test_forwarded_from_hidden_user(self) -> None:
        origin = SimpleNamespace(sender_user=None, sender_user_name="Скрытый", chat=None)
        self.assertEqual(_forward_note(SimpleNamespace(forward_origin=origin)), "[Переслано от: Скрытый]\n")

    def test_forwarded_from_channel(self) -> None:
        origin = SimpleNamespace(
            sender_user=None, sender_user_name=None,
            chat=SimpleNamespace(title="Канал про горы", id=-1),
        )
        self.assertEqual(
            _forward_note(SimpleNamespace(forward_origin=origin)), "[Переслано от: Канал про горы]\n",
        )


class SyntheticUpdateTests(unittest.TestCase):
    """Раны без входящего сообщения: /at и POST /event."""

    def test_routing_into_forum_topic(self) -> None:
        upd = _synthetic_update(None, -1001234567890, 42)
        self.assertEqual(_chat_key(upd), ("-1001234567890:42", -1001234567890, 42))

    def test_routing_private(self) -> None:
        upd = _synthetic_update(None, 555, None)
        self.assertEqual(_chat_key(upd), ("555", 555, None))

    def test_reply_does_not_quote_fake_message(self) -> None:
        """Регрессия: у синтетического сообщения message_id=0, цитировать его
        бессмысленно. PTB добавляет ReplyParameters только для не-private."""
        captured: dict = {}

        class FakeBot:
            async def send_message(self, **kw):
                captured.update(kw)
                return SimpleNamespace(message_id=1)

        upd = _synthetic_update(FakeBot(), -1001234567890, 42)
        asyncio.run(upd.message.reply_text("x"))
        self.assertIsNone(captured.get("reply_parameters"))
        self.assertEqual(captured.get("message_thread_id"), 42)


class UnattendedPolicyTests(unittest.TestCase):
    """Фоновые раны (/at, /event wake, plan): всё через карточку."""

    def _cfg(self, **over):
        base = dict(
            approve_whitelist_paths=frozenset({Path("/home/user/src")}),
            bash_auto_allow_re=re.compile(r"ls( .*)?"),
            mcp_auto_allow_re=re.compile(r"get[A-Za-z]*"),
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_whitelist_and_bash_disabled_for_unattended(self) -> None:
        """Регрессия: whitelist включает рабочие каталоги, а bash-regex
        допускает запись аргументами (`sort -o`), — для фонового рана нельзя."""
        cfg = self._cfg()
        for is_unattended, expect_whitelist, expect_bash in ((False, True, True), (True, False, False)):
            with self.subTest(unattended=is_unattended):
                whitelist = () if is_unattended else cfg.approve_whitelist_paths
                bash_re = None if is_unattended else cfg.bash_auto_allow_re
                self.assertEqual(bool(whitelist), expect_whitelist)
                self.assertEqual(bash_re is not None, expect_bash)

    def test_mcp_read_only_still_allowed(self) -> None:
        """Чтение фоновому разбору нужно, мутирующие имена под regex не подпадают."""
        cfg = self._cfg()
        self.assertTrue(cfg.mcp_auto_allow_re.fullmatch("getMessage"))
        self.assertIsNone(cfg.mcp_auto_allow_re.fullmatch("createIssue"))

    def test_burst_skipped_for_unattended_session(self) -> None:
        unattended = {"sid-bg"}
        for sid, eligible in (("sid-human", True), ("sid-bg", False)):
            with self.subTest(sid=sid):
                self.assertEqual(sid not in unattended, eligible)


class SessionEpochTests(unittest.TestCase):
    """Эпоха ловит то, что per-chat lock не ловит: решение принято по одному
    состоянию, а применяется после сброса сессии."""

    def test_epoch_starts_at_zero_and_grows(self) -> None:
        ctx = _ctx()
        self.assertEqual(_session_epoch(ctx, "chat"), 0)
        self.assertEqual(_bump_session_epoch(ctx, "chat"), 1)
        self.assertEqual(_session_epoch(ctx, "chat"), 1)

    def test_epochs_are_per_chat(self) -> None:
        ctx = _ctx()
        _bump_session_epoch(ctx, "chat-a")
        self.assertEqual(_session_epoch(ctx, "chat-b"), 0)

    def test_interrupt_must_not_restore_session_after_reset(self) -> None:
        """Регрессия: «!» захватил sid, пока он ждал смерти процесса прошёл
        /reset force — восстанавливать сессию уже нельзя."""
        ctx = _ctx()
        epoch_at_start = _session_epoch(ctx, "chat")
        _bump_session_epoch(ctx, "chat")  # /reset force между захватом и записью
        self.assertNotEqual(_session_epoch(ctx, "chat"), epoch_at_start)

    def test_ask_button_stale_after_reset(self) -> None:
        ctx = _ctx()
        token_epoch = _session_epoch(ctx, "chat")
        _bump_session_epoch(ctx, "chat")
        self.assertNotEqual(token_epoch, _session_epoch(ctx, "chat"))


class AtParserTests(unittest.TestCase):
    def test_relative_durations(self) -> None:
        self.assertEqual(_parse_at_when("45m"), 2700.0)
        self.assertEqual(_parse_at_when("2h"), 7200.0)
        self.assertEqual(_parse_at_when("90s"), 90.0)

    def test_wall_clock_is_in_the_future(self) -> None:
        delay = _parse_at_when("14:30")
        self.assertIsNotNone(delay)
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 24 * 3600)

    def test_garbage_rejected(self) -> None:
        for bad in ("завтра", "25:00", "", "12:99"):
            self.assertIsNone(_parse_at_when(bad), bad)


class DiffCardTests(unittest.TestCase):
    def test_normal_diff_shows_both_sides(self) -> None:
        card = _format_tool_call(
            "Edit", {"file_path": "/t/a.py", "old_string": "x = 1", "new_string": "x = 2"}, None
        )
        self.assertIn("-x = 1", card)
        self.assertIn("+x = 2", card)

    def test_newline_only_edit_is_visible(self) -> None:
        """Регрессия: splitlines() без keepends давал пустой diff."""
        card = _format_tool_call(
            "Edit", {"file_path": "/t/a", "old_string": "x\n", "new_string": "x"}, None
        )
        self.assertNotIn("нет текстовых изменений", card)
        self.assertIn("␊", card)

    def test_huge_input_is_capped(self) -> None:
        """Регрессия: difflib на мегабайтном входе блокировал событийный цикл."""
        big = "line\n" * 200000
        card = _format_tool_call(
            "Edit", {"file_path": "/t/b", "old_string": big, "new_string": big + "tail"}, None
        )
        self.assertLess(len(card), 4096)
        self.assertIn("20000", card)


if __name__ == "__main__":
    unittest.main()

"""Пустой баланс OpenAI (BACKLOG «Новое (02.11.2026)»): 429 с кодом
insufficient_quota или credit_balance_exhausted не повторяется, а Иван
получает строку, где пополнить. Прочие сбои голоса как раньше."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from claude_tg_bot import bot as botmod
from claude_tg_bot import voice
from claude_tg_bot.i18n import _
from tests.test_queue2 import _VoiceBase, _settle, _update

_HAS_OPENAI = importlib.util.find_spec("openai") is not None
# msgid; _() там, где сверяют: язык на уровне модуля ещё не выбран.
_STT_QUOTA = ("⚠️ Голос не распознан: кончились деньги на OpenAI или достигнут "
              "лимит расходов проекта: пополнить или поднять лимит {url}")
_TTS_QUOTA = ("⚠️ Озвучки не будет: кончились деньги на OpenAI или достигнут лимит расходов "
              "проекта: пополнить или поднять лимит {url} (дальше текстом)")


class _FakeStatusError(Exception):
    """Как openai.APIStatusError: status_code, code, type, body."""

    def __init__(self, status: int, code: str | None = None, type_: str | None = None,
                 body: object = None) -> None:
        super().__init__(f"Error code: {status}")
        self.status_code, self.code, self.type, self.body = status, code, type_, body


def _quota_error() -> _FakeStatusError:
    return _FakeStatusError(429, "insufficient_quota", "insufficient_quota",
                            {"code": "insufficient_quota", "type": "insufficient_quota"})


class QuotaDetectTests(unittest.TestCase):
    def test_quota_codes_recognized(self) -> None:
        self.assertTrue(voice.quota_exhausted(_quota_error()))
        self.assertTrue(voice.quota_exhausted(_FakeStatusError(429, type_="credit_balance_exhausted")))
        # Код только в теле, вложенный объект error.
        self.assertTrue(voice.quota_exhausted(_FakeStatusError(
            429, body={"error": {"code": "credit_balance_exhausted"}})))

    def test_other_errors_not_quota(self) -> None:
        self.assertFalse(voice.quota_exhausted(_FakeStatusError(429, "rate_limit_exceeded", "requests")))
        self.assertFalse(voice.quota_exhausted(_FakeStatusError(500, "insufficient_quota")))
        self.assertFalse(voice.quota_exhausted(_FakeStatusError(429, body="not json")))
        self.assertFalse(voice.quota_exhausted(RuntimeError("boom")))

    def test_non_string_codes_no_type_error(self) -> None:
        """Список или словарь вместо строки в чужом теле: не квота, без TypeError."""
        odd = (["insufficient_quota"], {"k": 1}, 7, None)
        for value in odd:
            with self.subTest(value=value):
                self.assertFalse(voice.quota_exhausted(_FakeStatusError(429, value, value)))
                self.assertFalse(voice.quota_exhausted(_FakeStatusError(
                    429, body={"code": value, "type": value, "error": {"code": value, "type": value}})))
        # Строковый код рядом с нестроковым всё ещё узнаётся.
        self.assertTrue(voice.quota_exhausted(_FakeStatusError(
            429, ["x"], body={"error": {"code": {"k": 1}, "type": "insufficient_quota"}})))


class _Base:
    def _should_retry(self, response) -> bool:
        return True


def _resp(status: int, body: object) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, content=json.dumps(body).encode())


class NoRetryClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = voice._no_quota_retry(_Base)()

    def test_quota_response_not_retried(self) -> None:
        for code in voice.QUOTA_CODES:
            with self.subTest(code=code):
                self.assertFalse(self.client._should_retry(_resp(429, {"error": {"code": code}})))
                self.assertFalse(self.client._should_retry(_resp(429, {"error": {"type": code}})))

    def test_other_responses_keep_sdk_retry(self) -> None:
        self.assertTrue(self.client._should_retry(_resp(429, {"error": {"code": "rate_limit_exceeded"}})))
        self.assertTrue(self.client._should_retry(_resp(500, {"error": {"code": "insufficient_quota"}})))

    def test_unread_body_defers_to_sdk(self) -> None:
        class Unread:
            status_code = 429

            @property
            def content(self) -> bytes:
                raise RuntimeError("not read")

        self.assertTrue(self.client._should_retry(Unread()))


@unittest.skipUnless(_HAS_OPENAI, "пакет openai не установлен (extra voice)")
class RealClientTests(unittest.TestCase):
    """Настоящий клиент SDK, подменён только транспорт: сколько запросов ушло."""

    def _run(self, body: dict, headers: dict | None = None) -> tuple[int, BaseException]:
        import httpx
        from openai import AsyncOpenAI

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(429, json=body, headers=headers or {})

        async def go() -> BaseException:
            client = voice._no_quota_retry(AsyncOpenAI)(
                api_key="sk-test", max_retries=2,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            cfg = SimpleNamespace(openai_tts_model="tts-1", openai_tts_voice="alloy",
                                  openai_tts_instructions="", voice_reply_max_chars=100)
            try:
                await voice.synthesize(client, cfg, "привет")
            except Exception as e:
                return e
            finally:
                await client.close()
            raise AssertionError("ожидался сбой")

        err = asyncio.run(go())
        return len(calls), err

    def test_quota_429_sent_once(self) -> None:
        for code in voice.QUOTA_CODES:
            with self.subTest(code=code):
                n, err = self._run({"error": {"message": "x", "type": code, "code": code}})
                self.assertEqual(n, 1)
                self.assertTrue(voice.quota_exhausted(err))

    def test_rate_limit_429_still_retried(self) -> None:
        n, err = self._run({"error": {"message": "x", "type": "requests", "code": "rate_limit_exceeded"}},
                           {"retry-after-ms": "1"})
        self.assertEqual(n, 3)
        self.assertFalse(voice.quota_exhausted(err))


@unittest.skipUnless(_HAS_OPENAI, "пакет openai не установлен (extra voice)")
class MakeClientTests(unittest.TestCase):
    """Клиент, которым пользуется бот (voice.make_client): возврат к голому
    AsyncOpenAI(api_key=...) вернул бы повтор запроса при пустом балансе."""

    def _run(self, call: str, body: dict, headers: dict | None = None) -> tuple[int, BaseException]:
        import httpx

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(429, json=body, headers=headers or {})

        cfg = SimpleNamespace(openai_api_key="sk-test", openai_tts_model="tts-1", openai_tts_voice="alloy",
                              openai_tts_instructions="", voice_reply_max_chars=100,
                              openai_stt_model="gpt-4o-transcribe", openai_stt_prompt="",
                              openai_stt_keywords=(), openai_stt_languages=())

        async def go(audio: Path) -> BaseException:
            # copy() сохраняет класс клиента, подменяется только транспорт.
            client = voice.make_client(cfg).copy(
                max_retries=2, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            try:
                if call == "transcribe":
                    await voice.transcribe(client, cfg, audio)
                else:
                    await voice.synthesize(client, cfg, "привет")
            except Exception as e:
                return e
            finally:
                await client.close()
            raise AssertionError("ожидался сбой")

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "v.oga"
            audio.write_bytes(b"OggS")
            err = asyncio.run(go(audio))
        return len(calls), err

    def test_client_class_guards_retry(self) -> None:
        from openai import AsyncOpenAI
        client = voice.make_client(SimpleNamespace(openai_api_key="sk-test"))
        self.assertIsInstance(client, AsyncOpenAI)
        self.assertIsNot(type(client)._should_retry, AsyncOpenAI._should_retry)

    def test_quota_429_sent_once_for_stt_and_tts(self) -> None:
        for call in ("transcribe", "synthesize"):
            for code in voice.QUOTA_CODES:
                with self.subTest(call=call, code=code):
                    n, err = self._run(call, {"error": {"message": "x", "type": code, "code": code}})
                    self.assertEqual(n, 1)
                    self.assertTrue(voice.quota_exhausted(err))

    def test_rate_limit_429_still_retried(self) -> None:
        for call in ("transcribe", "synthesize"):
            with self.subTest(call=call):
                n, err = self._run(call, {"error": {"message": "x", "type": "requests",
                                                    "code": "rate_limit_exceeded"}},
                                   {"retry-after-ms": "1"})
                self.assertEqual(n, 3)
                self.assertFalse(voice.quota_exhausted(err))


class VoiceQuotaTests(_VoiceBase):
    async def test_stt_quota_names_billing(self) -> None:
        stt = AsyncMock(side_effect=_quota_error())
        with patch.object(botmod, "transcribe", stt):
            msg = self.voice()
            await botmod.on_voice(_update(msg), self.ctx)
            await _settle()
        stt.assert_awaited_once()
        said = "\n".join(self.said(msg))
        self.assertIn(_(_STT_QUOTA).format(url=voice.BILLING_URL), said)
        self.assertEqual(self.runs, [])

    async def test_stt_other_failure_unchanged(self) -> None:
        with patch.object(botmod, "transcribe", AsyncMock(side_effect=RuntimeError("boom"))):
            msg = self.voice()
            await botmod.on_voice(_update(msg), self.ctx)
            await _settle()
        said = "\n".join(self.said(msg))
        self.assertIn(_("⚠️ не удалось распознать голос"), said)
        self.assertNotIn(voice.BILLING_URL, said)


class TtsQuotaTests(unittest.TestCase):
    _LONG = "\n\n".join(f"Абзац {i}. " + "слово " * 150 for i in range(4))

    def _series(self, err: Exception | list, reply: str = "Короткий ответ."):
        msg = SimpleNamespace(reply_voice=AsyncMock(), reply_text=AsyncMock())
        ctx = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(telegram_caption_limit=1024, voice_reply_max_chars=4000,
                                  state_dir=Path(tmp), telegram_text_limit=4000,
                                  large_reply_as_file_threshold=12000)
            with patch.object(botmod, "synthesize", AsyncMock(side_effect=err)):
                ok = asyncio.run(botmod._send_voice_series(
                    cfg, ctx, SimpleNamespace(message=msg), 42, object(), reply))
        return ok, msg

    def _billing_notes(self, msg) -> list[str]:
        return [c.args[0] for c in msg.reply_text.await_args_list if voice.BILLING_URL in c.args[0]]

    def test_tts_quota_names_billing_and_falls_back_to_text(self) -> None:
        ok, msg = self._series(_quota_error())
        self.assertFalse(ok)
        msg.reply_voice.assert_not_awaited()
        self.assertEqual(msg.reply_text.call_args.args[0], _(_TTS_QUOTA).format(url=voice.BILLING_URL))

    def test_tts_quota_mid_series_names_billing_once(self) -> None:
        """Первый кусок озвучен, дальше 429 о балансе: одна записка о балансе,
        остаток текстом."""
        for later in ([_quota_error()] * 3, [_quota_error(), b"ogg", b"ogg"]):
            with self.subTest(later=len([x for x in later if isinstance(x, Exception)])):
                ok, msg = self._series([b"ogg"] + later, reply=self._LONG)
                self.assertTrue(ok)
                self.assertEqual(msg.reply_voice.await_count, 1)
                self.assertEqual(self._billing_notes(msg), [_(_TTS_QUOTA).format(url=voice.BILLING_URL)])
                rest = msg.reply_text.await_args_list[-1].args[0]
                self.assertIn("Абзац 1.", rest)
                self.assertNotIn("Абзац 0.", rest)

    def test_tts_other_failure_mid_series_no_note(self) -> None:
        ok, msg = self._series([b"ogg", RuntimeError("boom"), b"ogg", b"ogg"], reply=self._LONG)
        self.assertTrue(ok)
        self.assertEqual(self._billing_notes(msg), [])
        msg.reply_text.assert_awaited_once()
        self.assertIn("Абзац 1.", msg.reply_text.call_args.args[0])

    def test_mid_series_note_failure_keeps_rest(self) -> None:
        msg = SimpleNamespace(reply_voice=AsyncMock(), reply_text=AsyncMock(side_effect=[RuntimeError("x"), None]))
        ctx = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(telegram_caption_limit=1024, voice_reply_max_chars=4000,
                                  state_dir=Path(tmp), telegram_text_limit=4000,
                                  large_reply_as_file_threshold=12000)
            with patch.object(botmod, "synthesize", AsyncMock(side_effect=[b"ogg"] + [_quota_error()] * 3)):
                ok = asyncio.run(botmod._send_voice_series(
                    cfg, ctx, SimpleNamespace(message=msg), 42, object(), self._LONG))
        self.assertTrue(ok)
        self.assertIn("Абзац 1.", msg.reply_text.await_args_list[-1].args[0])

    def test_tts_other_failure_unchanged(self) -> None:
        ok, msg = self._series(RuntimeError("boom"))
        self.assertFalse(ok)
        self.assertEqual(msg.reply_text.call_args.args[0], _("⚠️ TTS не сработал (пришлю только текст)"))


class TranscribeSkillQuotaTests(unittest.TestCase):
    """skills.transcribe при пустом балансе: подсказка, а не трейсбек."""

    def _main(self, err: Exception):
        import dataclasses

        from claude_tg_bot.skills.transcribe import __main__ as skill

        @dataclasses.dataclass
        class Cfg:
            openai_stt_model: str = "gpt-4o-transcribe"
            openai_stt_prompt: str = ""
            openai_stt_languages: tuple = ()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(skill, "check_input", return_value=Path(tmp) / "a.m4a"), \
                patch.object(skill, "probe_duration", return_value=60.0), \
                patch.object(skill, "load_config", return_value=Cfg()), \
                patch.object(skill.voice, "make_client", return_value=object()), \
                patch.object(skill, "split_audio", return_value=[Path(tmp) / "c000.mp3"]), \
                patch.object(skill, "transcribe_chunks", AsyncMock(side_effect=err)):
            return skill.main(["a.m4a"])

    def test_quota_gives_hint(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._main(_quota_error())
        self.assertEqual(str(cm.exception.code), _(
            "расшифровка не удалась: кончились деньги на OpenAI или достигнут "
            "лимит расходов проекта: пополнить или поднять лимит {url}").format(url=voice.BILLING_URL))

    def test_other_failure_unchanged(self) -> None:
        with self.assertRaises(RuntimeError):
            self._main(RuntimeError("boom"))


if __name__ == "__main__":
    unittest.main()

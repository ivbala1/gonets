"""Скачивание вложения и голосового через Bot API повторяется при сетевом
сбое (случай 23.10.2026: getFile голосового упал по TimedOut), а BadRequest
(«File is too big» и прочее) повтором не лечится и уходит наверх сразу."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from telegram.error import BadRequest, NetworkError, TimedOut  # noqa: E402

from claude_tg_bot import bot  # noqa: E402


def _file_obj(*failures: Exception):
    tg_file = SimpleNamespace(download_to_drive=AsyncMock())
    get_file = AsyncMock(side_effect=[*failures, tg_file])
    return SimpleNamespace(get_file=get_file), tg_file


def _run(file_obj):
    with patch.object(bot.asyncio, "sleep", AsyncMock()) as sleep:
        asyncio.run(bot._download_retrying(file_obj, Path("/tmp/x.oga")))
    return sleep


class DownloadRetryTest(unittest.TestCase):
    def test_timeout_then_success(self):
        obj, tg_file = _file_obj(TimedOut())
        sleep = _run(obj)
        self.assertEqual(obj.get_file.await_count, 2)
        tg_file.download_to_drive.assert_awaited_once_with(
            custom_path="/tmp/x.oga", read_timeout=bot._DOWNLOAD_READ_TIMEOUT)
        self.assertEqual(sleep.await_count, 1)

    def test_read_timeout_longer_than_default(self):
        # Случай 29.10.2026: умолчание 5 с не переждало заминку Telegram.
        obj, _ = _file_obj()
        _run(obj)
        obj.get_file.assert_awaited_once_with(read_timeout=bot._DOWNLOAD_READ_TIMEOUT)

    def test_download_marks_holders_slow(self):
        # Скачивание дольше 30 с ожидания пачки и /go: держатели слота помечены
        # slow с первой попытки и сняты после, в том числе при отказе.
        batch, buf = SimpleNamespace(slow=0), SimpleNamespace(slow=0)
        seen = []
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())

        def get_file(**_kw):
            seen.append((batch.slow, buf.slow))
            if len(seen) < 3:
                raise TimedOut()
            return tg_file

        obj = SimpleNamespace(get_file=AsyncMock(side_effect=get_file))
        with patch.object(bot.asyncio, "sleep", AsyncMock()):
            asyncio.run(bot._download_retrying(obj, Path("/tmp/x"), (batch, None, buf)))
        self.assertEqual(seen, [(1, 1), (1, 1), (1, 1)])
        self.assertEqual((batch.slow, buf.slow), (0, 0))

        seen.clear()

        def always_fail(**_kw):
            seen.append(batch.slow)
            raise TimedOut()

        obj = SimpleNamespace(get_file=AsyncMock(side_effect=always_fail))
        with self.assertRaises(TimedOut):
            with patch.object(bot.asyncio, "sleep", AsyncMock()):
                asyncio.run(bot._download_retrying(obj, Path("/tmp/x"), (batch,)))
        self.assertEqual(seen, [1, 1, 1, 1])
        self.assertEqual(batch.slow, 0)

    def test_cancel_releases_holders(self):
        # /cancel во время паузы между попытками: счётчик slow вернулся.
        batch = SimpleNamespace(slow=0)
        obj, _ = _file_obj(TimedOut())
        with self.assertRaises(asyncio.CancelledError):
            with patch.object(bot.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)):
                asyncio.run(bot._download_retrying(obj, Path("/tmp/x"), (batch,)))
        self.assertEqual(batch.slow, 0)

    def test_network_errors_up_to_limit(self):
        obj, _ = _file_obj(NetworkError("reset"), TimedOut(), TimedOut())
        sleep = _run(obj)
        self.assertEqual(obj.get_file.await_count, 4)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [2.0, 5.0, 15.0])
        for call in obj.get_file.await_args_list:  # таймаут у каждой попытки, не только у первой
            self.assertEqual(call.kwargs, {"read_timeout": bot._DOWNLOAD_READ_TIMEOUT})

    def test_gives_up_after_retries(self):
        obj, _ = _file_obj(TimedOut(), TimedOut(), TimedOut(), TimedOut())
        with self.assertRaises(TimedOut):
            _run(obj)
        self.assertEqual(obj.get_file.await_count, 4)

    def test_bad_request_not_retried(self):
        obj, _ = _file_obj(BadRequest("File is too big"))
        with self.assertRaises(BadRequest):
            _run(obj)
        self.assertEqual(obj.get_file.await_count, 1)

    def test_download_timeout_retried(self):
        tg_file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=[TimedOut(), None]))
        obj = SimpleNamespace(get_file=AsyncMock(return_value=tg_file))
        _run(obj)
        self.assertEqual(tg_file.download_to_drive.await_count, 2)
        for call in tg_file.download_to_drive.await_args_list:
            self.assertEqual(call.kwargs["read_timeout"], bot._DOWNLOAD_READ_TIMEOUT)

    def test_too_big_goes_to_mtproto_without_retry(self):
        obj, _ = _file_obj(BadRequest("File is too big"))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock(), chat_id=1, message_id=1))
        with patch.object(bot, "_fetch_big", AsyncMock(return_value="big")) as big, \
                patch.object(bot.asyncio, "sleep", AsyncMock()) as sleep:
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
                "cfg": SimpleNamespace(telegram_api_id=1, telegram_api_hash="h")}))
            got = asyncio.run(bot._fetch_attachment(
                update, ctx, file_obj=obj, size=None, target=Path("/tmp/x"),
                label="файл", chat_key="1", gslot=None, slot=None, hold=False))
        self.assertEqual(got, "big")
        big.assert_awaited_once()
        self.assertEqual(obj.get_file.await_count, 1)
        sleep.assert_not_awaited()
        update.message.reply_text.assert_not_awaited()


    def _fetch(self, obj, api_id, mtproto_busy=False):
        cfg = SimpleNamespace(telegram_api_id=api_id, telegram_api_hash="h" if api_id else "")
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
            "cfg": cfg, "mtproto": SimpleNamespace(busy=mtproto_busy)}))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock(), chat_id=1, message_id=1))
        with patch.object(bot, "_fetch_big", AsyncMock(return_value="big")) as big, \
                patch.object(bot.asyncio, "sleep", AsyncMock()):
            got = asyncio.run(bot._fetch_attachment(
                update, ctx, file_obj=obj, size=1000, target=Path("/tmp/x"),
                label="файл", chat_key="1", gslot=None, slot=None, hold=False))
        return got, big, update.message.reply_text

    def test_timeout_falls_back_to_mtproto(self):
        # Случай 06.11.2026: getFile фото висел по TimedOut во всех попытках.
        obj, _ = _file_obj(*[TimedOut()] * (len(bot._DOWNLOAD_RETRY_DELAYS) + 1))
        got, big, reply = self._fetch(obj, api_id=1)
        self.assertEqual(got, "big")
        big.assert_awaited_once()
        reply.assert_not_awaited()
        # Сразу после первого тайм-аута: повторы при сбое не помогали.
        self.assertEqual(obj.get_file.await_count, 1)

    def test_timeout_retried_while_mtproto_busy(self):
        # Ревью 07.11.2026: маленький файл не ждёт в очереди за большим.
        obj, _ = _file_obj(TimedOut())
        got, big, _reply = self._fetch(obj, api_id=1, mtproto_busy=True)
        self.assertEqual(got, "small")
        self.assertEqual(obj.get_file.await_count, 2)
        big.assert_not_awaited()

    def test_mtproto_freed_between_timeouts(self):
        # Занят на первом тайм-ауте, свободен на втором: уходим в MTProto, не
        # дожидаясь конца повторов.
        obj, _ = _file_obj(*[TimedOut()] * (len(bot._DOWNLOAD_RETRY_DELAYS) + 1))
        loader = SimpleNamespace(busy=True)
        cfg = SimpleNamespace(telegram_api_id=1, telegram_api_hash="h")
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg, "mtproto": loader}))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock(), chat_id=1, message_id=1))

        async def free_later(_delay):
            loader.busy = False

        with patch.object(bot, "_fetch_big", AsyncMock(return_value="big")) as big, \
                patch.object(bot.asyncio, "sleep", free_later):
            got = asyncio.run(bot._fetch_attachment(
                update, ctx, file_obj=obj, size=1000, target=Path("/tmp/x"),
                label="файл", chat_key="1", gslot=None, slot=None, hold=False))
        self.assertEqual(got, "big")
        self.assertEqual(obj.get_file.await_count, 2)
        big.assert_awaited_once()

    def test_timeout_retried_without_mtproto(self):
        obj, _ = _file_obj(*[TimedOut()] * (len(bot._DOWNLOAD_RETRY_DELAYS) + 1))
        self._fetch(obj, api_id=0)
        self.assertEqual(obj.get_file.await_count, len(bot._DOWNLOAD_RETRY_DELAYS) + 1)

    def test_other_network_error_still_retried_with_mtproto(self):
        obj, tg_file = _file_obj(NetworkError("boom"))
        got, big, _reply = self._fetch(obj, api_id=1)
        self.assertEqual(got, "small")
        self.assertEqual(obj.get_file.await_count, 2)
        big.assert_not_awaited()

    def test_other_network_error_not_sent_to_mtproto(self):
        # Только TimedOut: прочие сбои сети MTProto не лечит.
        obj, _ = _file_obj(*[NetworkError("boom")] * (len(bot._DOWNLOAD_RETRY_DELAYS) + 1))
        got, big, reply = self._fetch(obj, api_id=1)
        self.assertIsNone(got)
        big.assert_not_awaited()
        reply.assert_awaited_once()

    def test_timeout_without_mtproto_reports_failure(self):
        obj, _ = _file_obj(*[TimedOut()] * (len(bot._DOWNLOAD_RETRY_DELAYS) + 1))
        got, big, reply = self._fetch(obj, api_id=0)
        self.assertIsNone(got)
        big.assert_not_awaited()
        reply.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

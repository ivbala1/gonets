"""Вложения больше 20 МБ через MTProto (bigfile.py и обработчики bot.py):
выбор пути по размеру, сбой загрузки, отмена, склейка и перезапуск.
Без сети: клиент Telethon и загрузчик поддельные."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from telegram.error import BadRequest  # noqa: E402

from claude_tg_bot import bigfile  # noqa: E402
from claude_tg_bot import bot as botmod  # noqa: E402

from test_review_bot_main import GROUP, _Base, _msg, _update  # noqa: E402

MB = 1024 * 1024


# ---- bigfile.py ----

class RouteTests(unittest.TestCase):
    def test_route_by_size(self) -> None:
        self.assertFalse(bigfile.needs_mtproto(None))
        self.assertFalse(bigfile.needs_mtproto(10 * MB))
        self.assertFalse(bigfile.needs_mtproto(20 * MB))
        self.assertTrue(bigfile.needs_mtproto(20 * MB + 1))

    def test_too_big_answer_recognized(self) -> None:
        self.assertTrue(bigfile.too_big_for_bot_api(BadRequest("File is too big")))
        self.assertFalse(bigfile.too_big_for_bot_api(BadRequest("Wrong file_id")))

    def test_clean_parts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            (root / "a" / "1-x.m4a.part").write_bytes(b"x")
            (root / "a" / "2-y.pdf").write_bytes(b"y")
            self.assertEqual(bigfile.clean_parts(root), 1)
            self.assertEqual([p.name for p in (root / "a").iterdir()], ["2-y.pdf"])


class _FakeTelethon:
    """Клиент Telethon: get_messages отдаёт сообщение с файлом заданного
    размера, download_media пишет столько байт, сколько велено."""

    def __init__(self, *, size: int = 30, write: int | None = None, me_id: int = 42,
                 bot: bool = True, msg_present: bool = True, drops: int = 0) -> None:
        self.size = size
        self.write = size if write is None else write
        self.me = SimpleNamespace(id=me_id, bot=bot)
        self.msg_present = msg_present
        self.drops = drops
        self.connected = False
        self.starts = 0

    async def start(self, bot_token: str) -> None:
        self.starts += 1
        self.connected = True

    async def get_me(self):
        return self.me

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def get_messages(self, chat_id, ids):
        if not self.msg_present:
            return None
        return SimpleNamespace(file=SimpleNamespace(size=self.size))

    async def download_media(self, msg, file, progress_callback):
        if self.drops:
            self.drops -= 1
            Path(file).write_bytes(b"x")
            raise ConnectionError("dropped")
        Path(file).write_bytes(b"x" * self.write)
        progress_callback(self.write, self.size)
        return file


class BotMTProtoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.target = self.dir / "5-call.m4a"

    def _client(self, fake: _FakeTelethon, timeout: float = 5) -> bigfile.BotMTProto:
        return bigfile.BotMTProto(
            session_path=self.dir / "telethon-bot.session", api_id=1, api_hash="h",
            bot_token="42:secret", timeout_sec=timeout, client_factory=lambda *_a: fake)

    async def _download(self, client, **kw):
        args = dict(max_bytes=1000, min_free=0, progress=bigfile.Progress())
        args.update(kw)
        return await client.download(GROUP, 5, self.target, **args)

    async def test_success_writes_target_without_part(self) -> None:
        fake = _FakeTelethon(size=30)
        client = self._client(fake)
        self.assertEqual(await self._download(client), 30)
        self.assertEqual(self.target.stat().st_size, 30)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["5-call.m4a", "telethon-bot.session"])
        self.assertEqual((self.dir / "telethon-bot.session").stat().st_mode & 0o777, 0o600)
        await self._download(client)
        self.assertEqual(fake.starts, 1)  # клиент один на процесс
        self.assertNotIn("secret", repr(client))

    async def test_message_gone(self) -> None:
        with self.assertRaisesRegex(bigfile.FetchError, "удалено или изменено"):
            await self._download(self._client(_FakeTelethon(msg_present=False)))

    async def test_real_size_checked_against_limit(self) -> None:
        with self.assertRaisesRegex(bigfile.FetchError, "слишком большой"):
            await self._download(self._client(_FakeTelethon(size=2000)), max_bytes=1000)
        self.assertFalse(self.target.exists())

    async def test_not_enough_disk(self) -> None:
        usage = SimpleNamespace(free=100)
        with patch.object(bigfile.shutil, "disk_usage", return_value=usage), \
                self.assertRaisesRegex(bigfile.FetchError, "не хватает места"):
            await self._download(self._client(_FakeTelethon(size=30)), min_free=80)

    async def test_incomplete_file_removed(self) -> None:
        with self.assertRaisesRegex(bigfile.FetchError, "не целиком"):
            await self._download(self._client(_FakeTelethon(size=30, write=10)))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["telethon-bot.session"])

    async def test_one_drop_retried(self) -> None:
        self.assertEqual(await self._download(self._client(_FakeTelethon(drops=1))), 30)

    async def test_two_drops_fail_in_words(self) -> None:
        with self.assertRaisesRegex(bigfile.FetchError, "связь с Telegram оборвалась"):
            await self._download(self._client(_FakeTelethon(drops=2)))
        self.assertFalse(self.target.with_name(self.target.name + ".part").exists())

    async def test_foreign_session_refused(self) -> None:
        for fake in (_FakeTelethon(bot=False), _FakeTelethon(me_id=7)):
            with self.assertRaisesRegex(bigfile.FetchError, "не этому боту"):
                await self._download(self._client(fake))
            self.assertFalse(fake.connected)

    async def test_timeout_in_words(self) -> None:
        fake = _FakeTelethon()

        async def hang(*_a, **_k):
            await asyncio.sleep(10)

        fake.download_media = hang
        with self.assertRaisesRegex(bigfile.FetchError, "не уложилась"):
            await self._download(self._client(fake, timeout=0.05))


# ---- обработчики bot.py ----

class _FakeLoader:
    """Подмена BotMTProto в bot_data: пишет файл или падает по команде."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.busy = False

    async def download(self, chat_id, message_id, target, *, max_bytes, min_free, progress):
        self.calls.append((chat_id, message_id, target))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        target.write_bytes(b"x" * 10)
        return 30 * MB

    async def close(self) -> None:
        pass


class HandlerTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.document_max_size_bytes = 500 * MB
        self.cfg.photo_max_size_bytes = 10 * MB
        self.cfg.telegram_api_id = 1
        self.cfg.telegram_api_hash = "h"
        self.cfg.telegram_bot_token = "42:t"
        self.cfg.download_min_free_bytes = 0
        self.cfg.big_download_timeout_sec = 60
        self.loader = _FakeLoader()
        self.bot_data["mtproto"] = self.loader
        self.status = SimpleNamespace(edit_text=AsyncMock())
        self.dispatch = AsyncMock()
        p = patch.object(botmod, "_dispatch_to_claude", self.dispatch)
        p.start()
        self.addCleanup(p.stop)

    def _doc_msg(self, size: int | None, get_file=None) -> SimpleNamespace:
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())
        doc = SimpleNamespace(file_size=size, file_name="call.m4a", mime_type="audio/mp4",
                              file_unique_id="u1",
                              get_file=get_file or AsyncMock(return_value=tg_file))
        msg = _msg(document=doc, video=None, audio=None, video_note=None, animation=None)
        msg.reply_text = AsyncMock(return_value=self.status)
        return msg

    def _texts(self, msg) -> list[str]:
        return ([c.args[0] for c in msg.reply_text.await_args_list]
                + [c.args[0] for c in self.status.edit_text.await_args_list])

    async def test_small_file_goes_through_bot_api(self) -> None:
        msg = self._doc_msg(10 * MB)
        await botmod.on_file(_update(msg), self.ctx)
        msg.document.get_file.assert_awaited_once()
        self.assertEqual(self.loader.calls, [])
        self.dispatch.assert_awaited_once()
        self.assertNotIn("admitted", self.dispatch.await_args.kwargs)

    async def test_big_file_goes_through_mtproto(self) -> None:
        msg = self._doc_msg(30 * MB)
        await botmod.on_file(_update(msg), self.ctx)
        msg.document.get_file.assert_not_awaited()
        self.assertEqual(self.loader.calls[0][:2], (GROUP, 11))
        self.assertTrue(self.loader.calls[0][2].name.startswith("11-call"))
        self.dispatch.assert_awaited_once()
        self.assertTrue(self.dispatch.await_args.kwargs["admitted"])
        self.assertEqual(self.bot_data.get("active_work", 0), 0)
        self.assertIn("✅ скачал", self._texts(msg)[-1])

    async def test_unknown_size_falls_back_on_too_big(self) -> None:
        msg = self._doc_msg(None, get_file=AsyncMock(side_effect=BadRequest("File is too big")))
        await botmod.on_file(_update(msg), self.ctx)
        self.assertEqual(len(self.loader.calls), 1)
        self.dispatch.assert_awaited_once()

    async def test_over_limit_refused_before_network(self) -> None:
        msg = self._doc_msg(600 * MB)
        await botmod.on_file(_update(msg), self.ctx)
        self.assertEqual(self.loader.calls, [])
        self.assertIn("слишком большой", self._texts(msg)[0])

    async def test_failure_shown_in_words_and_slots_released(self) -> None:
        ck = str(GROUP)
        await botmod.on_compose(_update(_msg("/compose")), self.ctx)
        self.loader.error = bigfile.FetchError("⚠️ связь с Telegram оборвалась на 40%, пришли файл ещё раз")
        msg = self._doc_msg(30 * MB)
        await botmod.on_file(_update(msg), self.ctx)
        self.dispatch.assert_not_awaited()
        self.assertIn("оборвалась на 40%", self._texts(msg)[-1])
        self.assertTrue(botmod._compose_buffers(self.ctx)[ck].ready)
        self.assertEqual(botmod._compose_buffers(self.ctx)[ck].slow, 0)
        self.assertEqual(self.bot_data.get("active_work", 0), 0)
        self.assertEqual(self.bot_data.get("downloads"), {})

    async def test_unexpected_error_named(self) -> None:
        self.loader.error = RuntimeError("boom")
        msg = self._doc_msg(30 * MB)
        await botmod.on_file(_update(msg), self.ctx)
        self.assertIn("не удалось скачать «call.m4a»: RuntimeError", self._texts(msg)[-1])
        self.assertEqual(self.bot_data.get("active_work", 0), 0)

    async def test_no_keys_refused(self) -> None:
        self.cfg.telegram_api_id = 0
        msg = self._doc_msg(30 * MB)
        await botmod.on_file(_update(msg), self.ctx)
        self.assertEqual(self.loader.calls, [])
        self.assertIn("TELEGRAM_API_ID", self._texts(msg)[0])

    async def test_draining_refuses_new_big_download(self) -> None:
        self.bot_data["draining"] = True
        msg = self._doc_msg(30 * MB)
        await botmod.on_file(_update(msg), self.ctx)
        self.assertEqual(self.loader.calls, [])
        self.assertIn("перезапуску", self._texts(msg)[0])

    async def test_restart_sees_download_and_cancel_stops_it(self) -> None:
        self.loader.gate = asyncio.Event()
        msg = self._doc_msg(30 * MB)
        t = asyncio.ensure_future(botmod.on_file(_update(msg), self.ctx))
        await self.loader.started.wait()
        self.assertEqual(botmod._restart_active(self.ctx.application), 1)
        cancel_msg = _msg("/cancel")
        await botmod.on_cancel(_update(cancel_msg), self.ctx)
        await asyncio.wait_for(t, 2)
        self.dispatch.assert_not_awaited()
        self.assertIn("загрузка файла отменена", cancel_msg.reply_text.await_args.args[0])
        self.assertIn("отменена", self._texts(msg)[-1])
        self.assertEqual(botmod._restart_active(self.ctx.application), 0)

    async def test_progress_edit_failure_does_not_break_download(self) -> None:
        self.status.edit_text = AsyncMock(side_effect=RuntimeError("message deleted"))
        self.loader.gate = asyncio.Event()
        msg = self._doc_msg(30 * MB)
        with patch.object(botmod, "_PROGRESS_EVERY_SEC", 0.01):
            t = asyncio.ensure_future(botmod.on_file(_update(msg), self.ctx))
            await self.loader.started.wait()
            await asyncio.sleep(0.05)
            self.loader.gate.set()
            await asyncio.wait_for(t, 2)
        self.dispatch.assert_awaited_once()

    async def test_go_waits_for_big_download(self) -> None:
        ck = str(GROUP)
        await botmod.on_compose(_update(_msg("/compose")), self.ctx)
        self.loader.gate = asyncio.Event()
        t = asyncio.ensure_future(botmod.on_file(_update(self._doc_msg(30 * MB)), self.ctx))
        await self.loader.started.wait()
        self.assertEqual(botmod._compose_buffers(self.ctx)[ck].slow, 1)
        go = asyncio.ensure_future(botmod.on_go(_update(_msg("/go")), self.ctx))
        with patch.object(botmod.asyncio, "sleep", _fast_sleep):
            await asyncio.sleep(0)
            for _ in range(100):  # «30 с» ожидания прошли, загрузка ещё идёт
                await asyncio.sleep(0)
        self.assertFalse(go.done())
        self.loader.gate.set()
        await asyncio.wait_for(asyncio.gather(t, go), 2)
        self.assertIn("call.m4a", self.dispatch.await_args.args[2])


_real_sleep = asyncio.sleep


async def _fast_sleep(_sec, *a, **k):
    await _real_sleep(0)


class GatherBigTests(_Base):
    """Пересланная запись с комментарием: пачка ждёт загрузку до конца,
    следующее сообщение уходит после неё."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.ctx.application.running = True
        for p in (patch.object(botmod, "GATHER_WINDOW_SEC", 0.05),
                  patch.object(botmod, "GATHER_MAX_WAIT_SEC", 0.2)):
            p.start()
            self.addCleanup(p.stop)
        self.sent: list[tuple[str, dict]] = []

        async def dispatch(_upd, _ctx, prompt, **kw):
            self.sent.append((prompt, kw))

        p = patch.object(botmod, "_dispatch_to_claude", dispatch)
        p.start()
        self.addCleanup(p.stop)

    async def test_comment_waits_for_slow_file_and_order_kept(self) -> None:
        ck = "chat"
        comment = _update(_msg("вот запись"))
        g1 = botmod._gather_reserve(comment, self.ctx, ck)
        botmod._gather_fill(g1, "вот запись")
        file_upd = _update(_msg())
        g2 = botmod._gather_reserve(file_upd, self.ctx, ck)
        g2[0].slow += 1  # большая загрузка идёт
        self.bot_data["active_work"] = 1
        await asyncio.sleep(0.4)  # и окно, и 30 с (0.2) прошли
        self.assertEqual(self.sent, [])
        self.assertTrue(g2[0].closed)
        later = _update(_msg("и ещё"))
        g3 = botmod._gather_reserve(later, self.ctx, ck)
        self.assertIsNot(g3[0], g2[0])  # новое сообщение открыло следующую пачку
        botmod._gather_fill(g3, "и ещё")
        await asyncio.sleep(0.2)
        self.assertEqual(self.sent, [])  # ждёт закрытую пачку перед собой
        g2[0].slow -= 1
        self.assertTrue(botmod._gather_fill(g2, "[файл]", work=True))
        self.assertEqual(self.bot_data["active_work"], 1)
        for _ in range(40):
            if len(self.sent) == 2:
                break
            await asyncio.sleep(0.05)
        self.assertEqual([s[0] for s in self.sent], ["вот запись\n\n---\n\n[файл]", "и ещё"])
        self.assertTrue(self.sent[0][1]["admitted"])
        self.assertNotIn("admitted", self.sent[1][1])
        self.assertEqual(self.bot_data["active_work"], 0)

    async def test_cancel_drops_closed_batch(self) -> None:
        ck = str(GROUP)
        g = botmod._gather_reserve(_update(_msg()), self.ctx, ck)
        g[0].slow += 1
        await asyncio.sleep(0.4)
        self.assertTrue(g[0].closed)
        self.assertEqual(botmod._gather_cancel(self.ctx, ck), 1)
        g[0].slow -= 1
        self.assertTrue(botmod._gather_fill(g, "[файл]", work=True))  # выброшен
        await asyncio.sleep(0.6)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()

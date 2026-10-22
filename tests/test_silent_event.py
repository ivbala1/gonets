"""Ран без звука (/event с silent): уведомление и всё, что бот шлёт в рамках
рана пробуждения, уходят с disable_notification, а соседние раны, сообщения
Ивана и события без silent звучат как раньше. Отправки проверяются на уровне
запроса к Bot API: настоящий класс бота, подменён только транспорт."""
from __future__ import annotations

import asyncio
import io
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

from telegram.ext import Application, ContextTypes  # noqa: E402

from claude_tg_bot import approval  # noqa: E402
from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult, SessionStore  # noqa: E402


def _event_request(payload: dict):
    cfg = SimpleNamespace(
        approve_shared_secret="", primary_chat_id=1, state_dir=Path(tempfile.mkdtemp()),
        allowed_chat_ids=frozenset({-100}), allowed_user_ids=frozenset({1}),
    )
    wake = AsyncMock()
    application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5))),
        bot_data={"event_wake": wake},
    )
    request = SimpleNamespace(
        app={"cfg": cfg, "application": application}, headers={},
        json=AsyncMock(return_value=payload), remote="127.0.0.1",
    )
    return request, wake, application


class HandleEventSilentTests(unittest.TestCase):
    def _call(self, payload: dict):
        request, wake, application = _event_request(payload)

        async def go():
            resp = await approval.handle_event(request)
            await asyncio.sleep(0)
            return resp

        return asyncio.run(go()), wake, application

    def test_silent_notice_and_wake(self) -> None:
        resp, wake, app = self._call({"text": "x", "wake": True, "silent": True})
        self.assertEqual(resp.status, 200)
        self.assertIs(app.bot.send_message.await_args.kwargs["disable_notification"], True)
        self.assertIs(wake.call_args.kwargs["silent"], True)

    def test_without_silent_unchanged(self) -> None:
        resp, wake, app = self._call({"text": "x", "wake": True})
        self.assertEqual(resp.status, 200)
        self.assertFalse(app.bot.send_message.await_args.kwargs.get("disable_notification", False))
        self.assertIs(wake.call_args.kwargs["silent"], False)

    def test_silent_must_be_bool(self) -> None:
        for bad in ("true", 1, None):
            with self.subTest(silent=bad):
                resp, wake, app = self._call({"text": "x", "wake": True, "silent": bad})
                self.assertEqual(resp.status, 400)
                app.bot.send_message.assert_not_awaited()
                wake.assert_not_called()


_MSG = {"message_id": 7, "date": 0, "chat": {"id": 1, "type": "private"}}


class _Wire:
    """Транспорт бота: запоминает (метод Bot API, параметры) после того, как
    PTB подставил умолчания и выкинул None, то есть ровно то, что ушло бы в
    Telegram. Отправки отвечают сообщением, остальное True."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def install(self, case: unittest.TestCase) -> None:
        wire = self

        async def fake_do_post(_bot, endpoint: str, data: dict, **_kw):
            wire.calls.append((endpoint, dict(data)))
            # Уступить цикл: одновременные раны перемешивают отправки.
            await asyncio.sleep(0)
            if endpoint.startswith("send") and endpoint != "sendChatAction":
                return {**_MSG, "chat": {"id": data.get("chat_id", 1), "type": "private"}}
            return True

        p = patch.object(botmod._SilentAwareBot, "_do_post", fake_do_post)
        p.start()
        case.addCleanup(p.stop)

    def sends(self) -> list[tuple[str, dict]]:
        """Отправки, которые дают уведомление (send*, кроме «печатает»)."""
        return [
            (endpoint, data) for endpoint, data in self.calls
            if endpoint.startswith("send") and endpoint != "sendChatAction"
        ]

    def texts(self) -> dict[str, dict]:
        return {(data.get("text") or ""): data for _, data in self.sends()}


class SilentAwareBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.wire = _Wire()
        self.wire.install(self)
        self.bot = botmod._SilentAwareBot("1:test")

    async def _send_all(self) -> None:
        msg = botmod._synthetic_update(self.bot, 1, None).message
        await msg.reply_text("ответ")
        await msg.reply_document(document=io.BytesIO(b"x"), filename="a.txt")
        await msg.reply_voice(voice=b"OggS")
        await self.bot.send_message(chat_id=1, text="служебное", disable_notification=False)
        await self.bot.edit_message_text(chat_id=1, message_id=7, text="правка")

    async def test_silent_run_mutes_every_send(self) -> None:
        async def silent() -> None:
            botmod._SILENT_RUN.set(True)
            await self._send_all()

        await asyncio.create_task(silent())
        endpoints = [e for e, _ in self.wire.calls]
        self.assertEqual(endpoints, ["sendMessage", "sendDocument", "sendVoice", "sendMessage",
                                     "editMessageText"])
        for endpoint, data in self.wire.calls[:4]:
            with self.subTest(endpoint=endpoint):
                self.assertIs(data["disable_notification"], True)
        # Правка параметра не принимает и не получает его.
        self.assertNotIn("disable_notification", self.wire.calls[4][1])
        # Флаг жил в своей задаче, текущий контекст его не видит.
        self.assertFalse(botmod._SILENT_RUN.get())

    async def test_ordinary_sends_unchanged(self) -> None:
        await self._send_all()
        for endpoint, data in self.wire.calls[:3] + self.wire.calls[4:]:
            with self.subTest(endpoint=endpoint):
                self.assertNotIn("disable_notification", data)
        self.assertIs(self.wire.calls[3][1]["disable_notification"], False)


class BuildAppBotTests(unittest.TestCase):
    def test_app_uses_silent_aware_bot_with_builder_pool(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(
                telegram_bot_token="1:test", claude_bin="claude", codex_bin="",
                claude_cwd=Path(d), state_dir=Path(d),
            )
            app = botmod.build_app(cfg)
        self.assertIsInstance(app.bot, botmod._SilentAwareBot)
        # Пул соединений тот же, что ApplicationBuilder ставит с .token().
        default = Application.builder().token("1:test").build()
        self.assertEqual(app.bot.request._client_kwargs["limits"],
                         default.bot.request._client_kwargs["limits"])


class _Stop(Exception):
    pass


class SilentWakeRunTests(unittest.IsolatedAsyncioTestCase):
    """Пробуждение целиком: POST /event, фабрика из _on_post_init, настоящий
    _dispatch_to_claude и доставка ответа; подменены только claude и транспорт."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name)
        self.wire = _Wire()
        self.wire.install(self)
        self.bot = botmod._SilentAwareBot("1:test")
        self.cfg = SimpleNamespace(
            approve_shared_secret="", primary_chat_id=1,
            allowed_chat_ids=frozenset({-100}), allowed_user_ids=frozenset({1, 2}),
            codex_trigger_prefix="gpt:", codex_bin="", codex_fallback=False,
            session_idle_ttl_sec=0, claude_bin="claude", claude_cwd=Path("/"),
            hook_settings_path=Path("/x.json"), max_claude_runtime_sec=0,
            large_reply_as_file_threshold=100_000, telegram_text_limit=40,
            show_cost_footer=True, context_warn_tokens=0, state_dir=state,
        )
        self.app = SimpleNamespace(
            bot=self.bot, running=True,
            create_task=lambda coro, update=None, *, name=None: asyncio.create_task(coro),
            bot_data={
                "cfg": self.cfg, "sessions": SessionStore(state / "sessions.json"),
                "procs": {}, "active_sessions": {}, "session_targets": {}, "chat_locks": {},
                "registry": approval.ApprovalRegistry(),
                "untrusted_sessions": SessionStore(state / "untrusted_sessions.json"),
            },
        )
        self.release = asyncio.Event()
        self.release.set()
        self.started: list[str] = []
        self.answer = "ответ: {prompt}"
        self.cost = 0.0
        attachment = state / "plan.txt"
        attachment.write_text("план")
        self.attachment = attachment

        async def fake_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            self.started.append(prompt)
            # Живой статус рана «🔧 …» (_RunStatus) шлёт первое сообщение.
            await kw["progress_cb"]("Bash: ls")
            await self.release.wait()
            sid = f"sid-{chat_key}"
            store.set(chat_key, sid)
            return ClaudeResult(text=self.answer.format(prompt=prompt), session_id=sid,
                                cost_usd=self.cost, session_cost_usd=self.cost,
                                context_tokens=1000)

        for name, value in {
            "run_claude": fake_run,
            "_persona_for": lambda *a: "",
            "_memory_scope_hint": lambda *a: "",
            "_effort_for": lambda *a: "",
            "_model_for": lambda *a: "",
            "_activate_pending_model": lambda *a: None,
            "_model_footer_note": lambda *a: "",
            "send_file_whitelist": lambda cfg: [state.resolve()],
        }.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

        # Фабрика пробуждения ставится в _on_post_init до старта HTTP-сервера;
        # сервер здесь не нужен, поэтому выходим сразу после неё.
        with patch.object(botmod, "build_aiohttp_app", side_effect=_Stop):
            with self.assertRaises(_Stop):
                await botmod._on_post_init(self.app)
        self.assertIn("event_wake", self.app.bot_data)

    async def _event(self, payload: dict) -> asyncio.Task:
        """POST /event; задача пробуждения, которую он запустил."""
        request = SimpleNamespace(
            app={"cfg": self.cfg, "application": self.app}, headers={},
            json=AsyncMock(return_value=payload), remote="127.0.0.1",
        )
        before = set(self.app.bot_data.get("event_wake_tasks") or ())
        resp = await approval.handle_event(request)
        self.assertEqual(resp.status, 200)
        (task,) = set(self.app.bot_data["event_wake_tasks"]) - before
        return task

    def _loud(self) -> list[tuple[str, dict]]:
        # Статус рана «🔧 …» без звука всегда (_RunStatus), его не считаем.
        return [(e, d) for e, d in self.wire.sends() if not (d.get("text") or "").startswith("🔧")]

    async def test_every_message_of_silent_wake_is_muted(self) -> None:
        self.answer = ("Доброе утро. Сегодня три дела, первое в десять, второе после обеда. "
                       f"<<SEND_FILE:{self.attachment}>> <<ASK:Начинаем?|да|позже>>")
        self.cost = 0.42
        task = await self._event({"source": "Домовой", "title": "Утро", "wake": True,
                                  "fresh": True, "silent": True, "prompt": "утро"})
        await task
        sends = self.wire.sends()
        texts = [d.get("text") or "" for _, d in sends]
        # Уведомление, статус, ответ кусками, файл, футер и вопрос кнопками дошли.
        self.assertTrue(texts[0].startswith("🔔"))
        self.assertTrue(any(t.startswith("🔧") for t in texts))
        chunks = [t for t in texts if t and not t.startswith(("🔔", "🔧", "💸", "❓"))]
        self.assertGreaterEqual(len(chunks), 2)
        self.assertIn("sendDocument", [e for e, _ in sends])
        self.assertTrue(any(t.startswith("💸") for t in texts))
        self.assertTrue(any(t.startswith("❓") for t in texts))
        for endpoint, data in sends:
            with self.subTest(endpoint=endpoint, text=(data.get("text") or "")[:30]):
                self.assertIs(data.get("disable_notification"), True)

    async def test_wake_without_silent_sounds(self) -> None:
        self.cost = 0.42
        task = await self._event({"source": "Домовой", "title": "Вечер", "wake": True,
                                  "prompt": "вечер"})
        await task
        loud = self._loud()
        # Уведомление, ответ и футер.
        self.assertEqual(len(loud), 3)
        for endpoint, data in loud:
            with self.subTest(endpoint=endpoint, text=(data.get("text") or "")[:30]):
                self.assertFalse(data.get("disable_notification", False))

    async def test_flag_does_not_leak_into_concurrent_runs(self) -> None:
        """Иван пишет в 08:30, пока идёт его ран приходит тихое утро (ждёт в
        очереди того же чата), и одновременно событие без silent в другой чат."""
        self.release.clear()
        human_update = botmod._synthetic_update(self.bot, 1, None)
        human = asyncio.create_task(botmod._dispatch_to_claude(
            human_update, ContextTypes.DEFAULT_TYPE(application=self.app), "Иван",
            send_voice_reply=False,
        ))
        while not self.started:
            await asyncio.sleep(0)
        morning = await self._event({"source": "Домовой", "title": "Утро", "wake": True,
                                     "fresh": True, "silent": True, "prompt": "утро"})
        other = await self._event({"source": "cron", "title": "бэкап", "wake": True,
                                   "chat_id": 2, "prompt": "бэкап"})
        while len(self.started) < 2:
            await asyncio.sleep(0)
        self.release.set()
        await asyncio.gather(human, morning, other)
        self.assertEqual(self.started, ["Иван", "бэкап", "утро"])

        by_text = self.wire.texts()
        loud = ["ответ: Иван", "🔔 <b>cron</b>: бэкап", "ответ: бэкап"]
        quiet = ["🔔 <b>Домовой</b>: Утро", "ответ: утро"]
        for text in loud:
            with self.subTest(loud=text):
                self.assertFalse(by_text[text].get("disable_notification", False))
        for text in quiet:
            with self.subTest(quiet=text):
                self.assertIs(by_text[text]["disable_notification"], True)
        # «Встанет в очередь» только на текст Ивана: утро ждёт замок молча
        # (проект Домового 22.10.2026, 1.7).
        busy = [d for t, d in by_text.items() if t.startswith("⏳")]
        self.assertEqual(busy, [])


if __name__ == "__main__":
    unittest.main()

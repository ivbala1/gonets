"""Ревью 11.10.2026, основной код бота (bot.py, claude_runner.py, replies.py):
обработчики кнопок плана и вопроса, /compose и /go, /resume, переименование
темы, /at, остановка бота, голосовой ответ и SEND_FILE у рана по чужому тексту.
Фейки на SimpleNamespace по образцу test_untrusted_session.py, без сети."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot import claude_runner  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult, SessionStore  # noqa: E402
from claude_tg_bot.config import Project  # noqa: E402
from claude_tg_bot.replies import extract_send_file_markers  # noqa: E402

USER = 7
GROUP = -100


class _Status:
    def __init__(self, *_a, **_kw) -> None:
        pass

    def start(self) -> None:
        pass

    def on_event(self) -> None:
        pass

    async def on_tool(self, _desc: str) -> None:
        pass

    async def finish(self) -> None:
        pass


def _msg(text: str = "", *, chat_id: int = GROUP, thread: int | None = None, **extra) -> SimpleNamespace:
    m = SimpleNamespace(
        text=text, caption=None, chat_id=chat_id, message_id=11,
        message_thread_id=thread, is_topic_message=thread is not None,
        forward_origin=None, reply_to_message=None,
        set_reaction=AsyncMock(), reply_text=AsyncMock(), delete=AsyncMock(),
    )
    for k, v in extra.items():
        setattr(m, k, v)
    return m


def _update(msg: SimpleNamespace, *, user: int = USER, chat_id: int = GROUP,
            chat_type: str = "supergroup") -> SimpleNamespace:
    return SimpleNamespace(
        update_id=1, message=msg, effective_message=msg, callback_query=None,
        edited_message=None, edited_channel_post=None,
        effective_user=SimpleNamespace(id=user),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
    )


class _Base(unittest.IsolatedAsyncioTestCase):
    """Бот без сети: run_claude подменён, хранилища в temp-каталоге."""

    scope = ""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.store = SessionStore(self.state / "sessions.json")
        self.marks_path = self.state / "untrusted_sessions.json"
        self.runs: list[dict] = []
        self.answer = "ok"
        self.result_error = False
        self.run_hook = None  # async (kw) -> None, зовётся внутри рана
        self.cfg = SimpleNamespace(
            codex_trigger_prefix="gpt:", codex_bin="", codex_fallback=False,
            codex_fallback_sandbox="read-only",
            session_idle_ttl_sec=0, claude_bin="claude", claude_cwd=Path("/"),
            hook_settings_path=Path("/x.json"), max_claude_runtime_sec=0,
            allowed_user_ids={USER}, allowed_chat_ids={GROUP}, projects=(),
            persona_text="", telegram_text_limit=4096, telegram_caption_limit=1024,
            voice_reply_max_chars=4000, voice_max_duration_sec=180, state_dir=self.state,
        )
        self.bot_data = {
            "cfg": self.cfg, "sessions": self.store, "procs": {}, "active_sessions": {},
            "session_targets": {}, "chat_locks": {},
            "untrusted_sessions": SessionStore(self.marks_path),
            "session_scopes": SessionStore(self.state / "session_scopes.json"),
            "scope_fingerprints": SessionStore(self.state / "scope_fingerprints.json"),
            "topic_names": SessionStore(self.state / "topic_names.json"),
            "scopes": SessionStore(self.state / "scopes.json"),
            "at_jobs": SessionStore(self.state / "at_jobs.json"),
        }
        self.tasks: list[asyncio.Task] = []

        def create_task(coro, update=None, *, name=None):
            t = asyncio.ensure_future(coro)
            self.tasks.append(t)
            return t

        self.ctx = SimpleNamespace(
            application=SimpleNamespace(
                bot_data=self.bot_data, running=False, create_task=create_task,
            ),
            bot=SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock()),
            args=[],
        )

        async def fake_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            existing = store.get(chat_key)
            fp = kw.get("scope_store")
            if fp is not None:
                existing = claude_runner.resume_target(
                    store, fp, chat_key, kw.get("memory_scope_hint", ""))
            sid = existing or f"sid-{len(self.runs)}"
            if kw.get("on_session_start") is not None:
                kw["on_session_start"](sid)
            if kw.get("on_run_start") is not None:
                # Как настоящий run_claude: процесс создан, промпт ещё не подан.
                kw["on_run_start"]()
            run = {"resumed": existing, "prompt": prompt, "sid": sid, "kw": kw}
            self.runs.append(run)
            if self.run_hook is not None:
                await self.run_hook(run)
            if self.result_error:
                return ClaudeResult(text="⛔ отменено (signal -15)", session_id=sid, is_error=True)
            store.set(chat_key, sid)
            return ClaudeResult(text=self.answer, session_id=sid)

        self.send_reply_text = AsyncMock()
        self.send_files = AsyncMock()
        self.send_cost_footer = AsyncMock()
        for name, value in {
            "run_claude": fake_run,
            "_persona_for": lambda *a: "",
            "_memory_scope_hint": lambda *a: self.scope,
            "_effort_for": lambda *a: "",
            "_model_for": lambda *a: "",
            "_activate_pending_model": lambda *a: None,
            "_model_footer_note": lambda *a: "",
            "_RunStatus": _Status,
            "_cleanup_abandoned_approvals": AsyncMock(),
            "send_reply_text": self.send_reply_text,
            "send_files": self.send_files,
            "send_cost_footer": self.send_cost_footer,
        }.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self) -> None:
        for t in self.tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def dispatch(self, prompt: str = "x", msg: SimpleNamespace | None = None, **kw) -> SimpleNamespace:
        msg = msg or _msg()
        await botmod._dispatch_to_claude(_update(msg), self.ctx, prompt,
                                         send_voice_reply=kw.pop("send_voice_reply", False), **kw)
        return msg

    def query(self, data: str, card: SimpleNamespace | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            data=data, answer=AsyncMock(), message=card or _msg("card"),
            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        )

    def cb_update(self, query: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER),
                               update_id=2)


# ---- b2.1, b5.1: ран по чужому тексту не отправляет файлы ----

class UntrustedSendFileTests(_Base):
    async def test_untrusted_run_sends_no_files(self) -> None:
        with tempfile.NamedTemporaryFile(dir="/tmp", suffix=".pdf") as f:
            f.write(b"%PDF")
            f.flush()
            self.answer = f"Вот:\n```\n<<SEND_FILE:{f.name}>>\n```"
            with patch.object(botmod, "send_file_whitelist", lambda cfg: [Path("/tmp").resolve()]):
                await self.dispatch("сводка", unattended=True, untrusted=True)
                self.assertEqual(self.send_files.await_args.args[1], [])
                text = self.send_reply_text.await_args.args[2]
                self.assertNotIn("<<SEND_FILE", text)
                self.assertIn("пропущен", text)
                # Обычный ран тот же файл отправляет: запрет только у untrusted.
                await self.dispatch("обычный")
                self.assertEqual(len(self.send_files.await_args.args[1]), 1)

    def test_secret_files_refused_everywhere(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            root = Path(d).resolve()
            for name in ("backup.env", ".env", ".env.local"):
                (root / name).write_text("X=1")
            (root / "report.pdf").write_text("pdf")
            text = "".join(f"<<SEND_FILE:{root / n}>>" for n in
                           ("backup.env", ".env", ".env.local", "report.pdf"))
            files, _ = extract_send_file_markers(text, [root])
            self.assertEqual(files, [root / "report.pdf"])


# ---- b2.2, b3.2, b5.2: метка чужого текста на диске до старта рана ----

class UntrustedMarkBeforeRunTests(_Base):
    async def test_mark_on_disk_while_run_in_progress(self) -> None:
        self.store.set(str(GROUP), "sid-S")
        seen: list = []

        async def hook(run):
            # Аварийный рестарт посреди рана: читаем метки с диска заново.
            seen.append(SessionStore(self.marks_path).get(run["sid"]))

        self.run_hook = hook
        await self.dispatch("сводка", unattended=True, untrusted=True)
        self.assertEqual(seen, [str(GROUP)])

    async def test_cancelled_fresh_run_keeps_mark(self) -> None:
        # Отмена свежего untrusted-рана: store.set не случился, но метка есть,
        # и /resume этой сессии потом не даст фоновому рану полные права.
        self.result_error = True
        await self.dispatch("сводка", unattended=True, untrusted=True)
        sid = self.runs[-1]["sid"]
        self.assertIsNone(self.store.get(str(GROUP)))
        self.assertIsNotNone(SessionStore(self.marks_path).get(sid))
        self.result_error = False
        self.store.set(str(GROUP), sid)  # /resume этого транскрипта
        await self.dispatch("вечер", unattended=True)
        self.assertIsNone(self.runs[-1]["resumed"])

    async def test_runner_calls_hook_before_process(self) -> None:
        calls: list[str] = []

        async def boom(*_a, **_k):
            calls.append("exec")
            raise OSError("no claude")

        store = SessionStore(self.state / "s2.json")
        with patch.object(claude_runner.asyncio, "create_subprocess_exec", boom), \
                patch.object(claude_runner, "_ensure_hook_still_valid", lambda *_: None), \
                patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()):
            with self.assertRaises(OSError):
                await claude_runner.run_claude(
                    "claude", "1", 1, None, "x", store, cwd=Path("/"),
                    hook_settings=Path("/x.json"), on_session_start=lambda sid: calls.append("start:" + sid),
                )
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith("start:"))
        self.assertEqual(calls[1], "exec")


# ---- B23, b2.5: голосовой ответ ----

class VoiceReplyTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot_data["openai"] = object()

    async def test_tts_failure_sends_text_once(self) -> None:
        with patch.object(botmod, "synthesize", AsyncMock(side_effect=RuntimeError("tts"))):
            await self.dispatch("озвучь", send_voice_reply=True)
        self.assertEqual(self.send_reply_text.await_count, 1)

    async def test_voice_send_failure_keeps_text_files_footer_ask(self) -> None:
        self.answer = "Ответ<<ASK:Деплоить?|да|нет>>"
        with patch.object(botmod, "synthesize", AsyncMock(return_value=b"ogg")), \
                patch.object(botmod, "send_voice_caption", AsyncMock(side_effect=TimeoutError)), \
                patch.object(botmod, "_send_ask_card", AsyncMock()) as ask:
            await self.dispatch("озвучь", send_voice_reply=True)
        self.assertEqual(self.send_reply_text.await_count, 1)
        self.send_files.assert_awaited()
        self.send_cost_footer.assert_awaited()
        ask.assert_awaited()


# ---- B24: план от GPT без кнопки «Выполнять» ----

class PlanFallbackTests(_Base):
    async def test_no_plan_card_after_codex_fallback(self) -> None:
        self.cfg.codex_bin = "codex"
        self.cfg.codex_fallback = True
        self.result_error = True
        with patch.object(botmod, "_is_claude_unavailable", lambda r: True), \
                patch.object(botmod, "_codex_as_claude_result",
                             AsyncMock(return_value=ClaudeResult(text="план GPT"))), \
                patch.object(botmod, "_send_plan_card", AsyncMock()) as card:
            msg = await self.dispatch("/plan x", permission_mode="plan")
        card.assert_not_awaited()
        self.assertIn("GPT", msg.reply_text.await_args.args[0])


# ---- B36, b1.1: старая карточка плана ----

class PlanCardTests(_Base):
    async def _card(self) -> str:
        msg = _msg()
        await botmod._send_plan_card(self.ctx, _update(msg), str(GROUP))
        markup = msg.reply_text.await_args.kwargs["reply_markup"]
        return markup.inline_keyboard[0][0].callback_data  # plan:<token>:ok

    async def _pick(self, data: str) -> SimpleNamespace:
        q = self.query(data)
        with patch.object(botmod, "Update", lambda update_id, message: _update(message)), \
                patch.object(botmod, "_dispatch_to_claude", AsyncMock()) as disp:
            await botmod.on_plan_pick(self.cb_update(q), self.ctx)
        q.dispatched = disp.await_count
        return q

    async def test_card_after_reset_does_not_execute(self) -> None:
        self.store.set(str(GROUP), "sid-A")
        data = await self._card()
        self.store.remove(str(GROUP))
        botmod._bump_session_epoch(self.ctx, str(GROUP))  # /reset
        q = await self._pick(data)
        self.assertEqual(q.dispatched, 0)
        self.assertIn("сменилась", q.edit_message_text.await_args.args[0])

    async def test_card_after_resume_other_session_does_not_execute(self) -> None:
        self.store.set(str(GROUP), "sid-A")
        data = await self._card()
        self.store.set(str(GROUP), "sid-B")  # подменили сессию мимо эпохи
        q = await self._pick(data)
        self.assertEqual(q.dispatched, 0)

    async def test_new_plan_invalidates_old_card(self) -> None:
        self.store.set(str(GROUP), "sid-A")
        old = await self._card()
        new = await self._card()
        self.assertEqual((await self._pick(old)).dispatched, 0)
        self.assertEqual((await self._pick(new)).dispatched, 1)

    async def test_epoch_checked_again_under_lock(self) -> None:
        self.store.set(str(GROUP), "sid-A")
        epoch = botmod._session_epoch(self.ctx, str(GROUP))
        botmod._bump_session_epoch(self.ctx, str(GROUP))  # сменилась, пока ждал замка
        await self.dispatch("План одобрен — выполняй его.", expect_epoch=epoch)
        self.assertEqual(self.runs, [])

    async def test_still_wanted_checked_under_lock(self) -> None:
        # Разбор 22.10.2026: нажатие, ждавшее замка, а дело уже сделано, рана не даёт.
        await self.dispatch("[Кнопка отклика] ✅ ок", still_wanted=lambda: False)
        self.assertEqual(self.runs, [])
        await self.dispatch("[Кнопка отклика] ✅ ок", still_wanted=lambda: True)
        self.assertEqual(len(self.runs), 1)


    async def test_run_end_marks_taken_otklik_unknown(self) -> None:
        """Ревью Codex 22.10.2026: ран взял отклик и кончился без итога: под
        замком темы запись получает «результат неизвестен», а не висит sending."""
        from claude_tg_bot import otklik
        base = otklik.otklik_dir(self.state)
        rec = otklik.new_record("Acme", "почта", "Hello")
        rec.update(chat_id=GROUP, source=str(GROUP), status="approved")
        otklik.save(base, rec)
        self.ctx.bot.edit_message_text = AsyncMock()

        async def takes(_run) -> None:
            otklik.take(base, rec["id"], str(GROUP))
        self.run_hook = takes
        await self.dispatch("[Кнопка отклика] ✅ ок")
        self.assertEqual(otklik.load(base, rec["id"])["status"], "unknown")


# ---- B32: кнопка вопроса после рестарта ----

class AskStaleTests(_Base):
    async def test_unknown_token_explains(self) -> None:
        q = self.query("ask:deadbeef")
        await botmod.on_ask_pick(self.cb_update(q), self.ctx)
        self.assertEqual(q.answer.await_count, 1)
        args, kwargs = q.answer.await_args
        self.assertIn("устарел", args[0])
        self.assertTrue(kwargs.get("show_alert"))


# ---- B38, b1.2: смена скоупа поднимает эпоху ----

class ScopeEpochTests(_Base):
    async def test_scope_change_bumps_epoch(self) -> None:
        self.store.set(str(GROUP), "sid-A")
        before = botmod._session_epoch(self.ctx, str(GROUP))
        self.assertTrue(await botmod._drop_session_for_scope_change(self.ctx, str(GROUP), _msg()))
        self.assertGreater(botmod._session_epoch(self.ctx, str(GROUP)), before)

    async def test_resume_gate_reset_bumps_epoch_and_stops_ask(self) -> None:
        # Скоуп сменился мимо /scope (занятый чат, рестарт): resume_target
        # сбрасывает сессию, ответ кнопкой старого вопроса не уходит.
        self.bot_data["scope_fingerprints"].set(str(GROUP), "alpha")
        self.store.set(str(GROUP), "sid-A")
        epoch = botmod._session_epoch(self.ctx, str(GROUP))
        self.scope = "gamma"
        await self.dispatch("[Ответ на вопрос «Деплоить?»]: да", expect_epoch=epoch,
                            unattended=True, from_callback=True)
        self.assertEqual(self.runs, [])


# ---- B33, b1.3: /compose cancel во время /go ----

class ComposeCancelTests(_Base):
    async def test_go_waiting_then_cancel_sends_nothing(self) -> None:
        ck = str(GROUP)
        upd = _update(_msg("/compose"))
        await botmod.on_compose(upd, self.ctx)
        botmod._compose_add(self.ctx, ck, "текст 1")
        slot = botmod._compose_reserve(self.ctx, ck)  # голосовое ещё распознаётся
        with patch.object(botmod, "_dispatch_to_claude", AsyncMock()) as disp:
            go_msg = _msg("/go")
            go = asyncio.ensure_future(botmod.on_go(_update(go_msg), self.ctx))
            await asyncio.sleep(0)
            self.ctx.args = ["cancel"]
            await botmod.on_compose(_update(_msg("/compose cancel")), self.ctx)
            await asyncio.wait_for(go, 5)
            # Распознанное после отмены голосовое тоже выбрасывается.
            self.assertIsNone(botmod._compose_fill(self.ctx, ck, slot, "голос"))
            self.assertTrue(botmod._compose_cancelled(self.ctx, ck, slot))
        disp.assert_not_awaited()
        self.assertIn("отменён", go_msg.reply_text.await_args.args[0])

    async def test_late_voice_after_cancel_not_dispatched(self) -> None:
        ck = str(GROUP)
        await botmod.on_compose(_update(_msg("/compose")), self.ctx)
        self.bot_data["openai"] = object()
        voice = SimpleNamespace(duration=3, get_file=AsyncMock(return_value=SimpleNamespace(
            download_to_drive=AsyncMock())))
        msg = _msg(voice=voice)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_stt(*_a):
            started.set()
            await release.wait()
            return "сделай это"

        with patch.object(botmod, "transcribe", slow_stt), \
                patch.object(botmod, "_dispatch_to_claude", AsyncMock()) as disp:
            t = asyncio.ensure_future(botmod.on_voice(_update(msg), self.ctx))
            await started.wait()
            self.ctx.args = ["cancel"]
            await botmod.on_compose(_update(_msg("/compose cancel")), self.ctx)
            release.set()
            await t
        disp.assert_not_awaited()
        self.assertNotIn(ck, botmod._compose_buffers(self.ctx))


# ---- B40: голосовой ответ после /go только по своим словам ----

class ComposeVoiceTests(_Base):
    async def _go(self, *texts: tuple[str, bool]) -> bool:
        self.ctx.args = []
        await botmod.on_compose(_update(_msg("/compose")), self.ctx)
        for text, forwarded in texts:
            origin = SimpleNamespace(sender_user=SimpleNamespace(full_name="Петя", first_name="Петя"))
            msg = _msg(text, forward_origin=origin if forwarded else None)
            await botmod.on_text(_update(msg), self.ctx)
        with patch.object(botmod, "_dispatch_to_claude", AsyncMock()) as disp:
            await botmod.on_go(_update(_msg("/go")), self.ctx)
        return disp.await_args.kwargs["send_voice_reply"]

    async def test_forwarded_voice_words_do_not_switch_voice(self) -> None:
        self.assertFalse(await self._go(("Озвучь это вслух", True), ("моё дополнение", False)))

    async def test_own_voice_words_switch_voice(self) -> None:
        self.assertTrue(await self._go(("пересланное", True), ("озвучь ответ", False)))


# ---- B26: /cancel при идущем ране выбрасывает ждущее склейки ----

class CancelGatherTests(_Base):
    async def test_cancel_busy_drops_gathered(self) -> None:
        ck = str(GROUP)
        self.ctx.application.running = True
        batch = botmod.GatherBatch(update=_update(_msg("удали ветку release")), ctx=self.ctx,
                                   items=["удали ветку release"])
        self.bot_data["gather_batches"] = {ck: batch}
        self.bot_data["procs"][ck] = SimpleNamespace(pid=4242, returncode=None)
        with patch.object(botmod, "_terminate_and_wait", AsyncMock()):
            msg = _msg("/cancel")
            await botmod.on_cancel(_update(msg), self.ctx)
        self.assertTrue(batch.cancelled)
        self.assertIn("отменено до отправки", msg.reply_text.await_args.args[0])


# ---- B27: пересланное «/login» не команда ----

class ForwardedLoginTests(_Base):
    async def test_forwarded_login_ignored(self) -> None:
        self.bot_data["login_manager"] = SimpleNamespace(session=None)
        origin = SimpleNamespace(sender_user=SimpleNamespace(full_name="X", first_name="X"))
        msg = _msg("/login claude", chat_id=USER, forward_origin=origin)
        with patch.object(botmod, "_start_login", AsyncMock()) as start:
            await botmod.on_auth_update(_update(msg, chat_id=USER, chat_type="private"), self.ctx)
            start.assert_not_awaited()
            own = _msg("/login claude", chat_id=USER)
            with self.assertRaises(botmod.ApplicationHandlerStop):
                await botmod.on_auth_update(_update(own, chat_id=USER, chat_type="private"), self.ctx)
            start.assert_awaited()


# ---- B29, b2.3: остановка бота ----

class ShutdownTests(_Base):
    def test_stop_signal_sets_flag_and_terms_runs(self) -> None:
        proc = SimpleNamespace(pid=4242, returncode=None)
        self.bot_data["procs"]["1"] = proc
        app = SimpleNamespace(bot_data=self.bot_data)
        loop = MagicMock()
        with patch.object(botmod, "kill_process_tree") as kill, \
                patch.object(botmod.asyncio, "get_running_loop", return_value=loop):
            with self.assertRaises(SystemExit):
                botmod._on_stop_signal(app)
            kill.assert_called_once_with(proc, signal.SIGTERM)
            # Через grace оставшимся SIGKILL.
            delay, cb = loop.call_later.call_args.args
            cb()
            kill.assert_called_with(proc, signal.SIGKILL)
        self.assertTrue(self.bot_data["stopping"])

    async def test_interrupted_run_says_restart(self) -> None:
        async def hook(_run):
            self.bot_data["stopping"] = True

        self.run_hook = hook
        self.result_error = True
        await self.dispatch("долгий ран")
        self.assertIn("перезапускается", self.send_reply_text.await_args.args[2])

    async def test_no_new_run_while_stopping(self) -> None:
        self.bot_data["stopping"] = True
        msg = await self.dispatch("ждал замка")
        self.assertEqual(self.runs, [])
        self.assertIn("перезапускается", msg.reply_text.await_args.args[0])


# ---- b1.4: /at переживает рестарт, пока ждёт занятую тему ----

class AtJobTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        p = patch.object(botmod, "_synthetic_update", lambda bot, cid, tid: _update(_msg(chat_id=cid)))
        p.start()
        self.addCleanup(p.stop)

    def _job(self) -> SimpleNamespace:
        import json
        data = {"jid": "j1", "chat_id": GROUP, "thread_id": None, "prompt": "напомни", "fire_at": 0}
        self.bot_data["at_jobs"].set("j1", json.dumps(data))
        return SimpleNamespace(application=self.ctx.application, bot=self.ctx.bot,
                               job=SimpleNamespace(data=data))

    async def test_record_kept_while_topic_busy(self) -> None:
        jctx = self._job()
        lock = botmod._chat_lock(self.ctx, str(GROUP))
        await lock.acquire()
        t = asyncio.ensure_future(botmod._at_fire(jctx))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertIsNotNone(self.bot_data["at_jobs"].get("j1"))
        seen: list = []

        async def hook(_run):
            seen.append(SessionStore(self.state / "at_jobs.json").get("j1"))

        self.run_hook = hook
        lock.release()
        await t
        self.assertEqual(seen, [None])  # снята при старте рана
        self.assertEqual(len(self.runs), 1)

    async def test_record_kept_when_stopping(self) -> None:
        jctx = self._job()
        self.bot_data["stopping"] = True
        await botmod._at_fire(jctx)
        self.assertEqual(self.runs, [])
        self.assertIsNotNone(SessionStore(self.state / "at_jobs.json").get("j1"))


# ---- B30, b2.4: голосовое ----

class VoiceInputTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot_data["openai"] = object()

    def _voice_msg(self) -> SimpleNamespace:
        voice = SimpleNamespace(duration=3, get_file=AsyncMock(return_value=SimpleNamespace(
            download_to_drive=AsyncMock())))
        return _msg(voice=voice)

    async def test_reserve_before_chat_action(self) -> None:
        ck = str(GROUP)
        await botmod.on_compose(_update(_msg("/compose")), self.ctx)
        reserved: list[int] = []

        async def chat_action(**_k):
            reserved.append(len(botmod._compose_buffers(self.ctx)[ck].items))

        self.ctx.bot.send_chat_action = chat_action
        with patch.object(botmod, "transcribe", AsyncMock(return_value="задача")):
            await botmod.on_voice(_update(self._voice_msg()), self.ctx)
        self.assertEqual(reserved, [1])

    async def test_long_transcript_echo_split_and_request_sent(self) -> None:
        msg = self._voice_msg()

        async def reply(text, **_k):
            if len(text) > 4096:
                raise RuntimeError("Message is too long")

        msg.reply_text = AsyncMock(side_effect=reply)
        heard = "слово " * 900
        with patch.object(botmod, "transcribe", AsyncMock(return_value=heard)), \
                patch.object(botmod, "_dispatch_to_claude", AsyncMock()) as disp:
            await botmod.on_voice(_update(msg), self.ctx)
        disp.assert_awaited()
        self.assertGreater(msg.reply_text.await_count, 1)

    async def test_echo_failure_does_not_lose_request(self) -> None:
        msg = self._voice_msg()
        msg.reply_text = AsyncMock(side_effect=RuntimeError("network"))
        with patch.object(botmod, "transcribe", AsyncMock(return_value="задача")), \
                patch.object(botmod, "_dispatch_to_claude", AsyncMock()) as disp:
            await botmod.on_voice(_update(msg), self.ctx)
        disp.assert_awaited()


# ---- B31: длинное русское имя вложения ----

class AttachmentNameTests(unittest.TestCase):
    def test_long_cyrillic_name_fits_ext4(self) -> None:
        name = ("Заключение_государственной_экспертизы_" * 5) + "корпус_2.pdf"
        safe = botmod._clip_file_name(name, botmod._ATTACHMENT_NAME_MAX_BYTES)
        self.assertLessEqual(len(f"123456-{safe}".encode("utf-8")), 255)
        self.assertTrue(safe.endswith(".pdf"))
        safe.encode("utf-8").decode("utf-8")  # граница символа
        self.assertEqual(botmod._clip_file_name("a.pdf", 200), "a.pdf")


# ---- B37, B39: переименование темы ----

class ForumTopicTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.projects = (
            Project(key="alpha", match=("альфа",), append_prompt="ALPHA"),
            Project(key="gamma", match=("гамма",), append_prompt="GAMMA"),
        )
        # Настоящие функции скоупа, а не подмена из _Base.
        for name in ("_memory_scope_hint", "_persona_for"):
            p = patch.object(botmod, name, self._orig[name])
            p.start()
            self.addCleanup(p.stop)
        self.ck = f"{GROUP}:5"
        self.bot_data["topic_names"].set(self.ck, "Разное")
        self.store.set(self.ck, "sid-A")

    _orig = {"_memory_scope_hint": botmod._memory_scope_hint, "_persona_for": botmod._persona_for}

    def _event(self, name: str, user: int = USER, chat_id: int = GROUP) -> SimpleNamespace:
        msg = _msg(chat_id=chat_id, thread=5, forum_topic_created=None,
                   forum_topic_edited=SimpleNamespace(name=name))
        return _update(msg, user=user, chat_id=chat_id)

    async def test_rename_without_scope_change_keeps_session(self) -> None:
        await botmod.on_forum_topic(self._event("Разное 2"), self.ctx)
        self.assertEqual(self.store.get(self.ck), "sid-A")
        self.ctx.bot.send_message.assert_not_awaited()

    async def test_rename_with_scope_change_resets_and_tells(self) -> None:
        epoch = botmod._session_epoch(self.ctx, self.ck)
        await botmod.on_forum_topic(self._event("Гамма поддержка"), self.ctx)
        self.assertIsNone(self.store.get(self.ck))
        self.assertGreater(botmod._session_epoch(self.ctx, self.ck), epoch)
        self.ctx.bot.send_message.assert_awaited()

    async def test_rename_by_stranger_ignored(self) -> None:
        await botmod.on_forum_topic(self._event("Альфа", user=999), self.ctx)
        self.assertEqual(self.bot_data["topic_names"].get(self.ck), "Разное")
        self.assertEqual(self.store.get(self.ck), "sid-A")

    async def test_event_from_foreign_chat_not_cached(self) -> None:
        await botmod.on_forum_topic(self._event("Альфа", chat_id=-555), self.ctx)
        self.assertIsNone(self.bot_data["topic_names"].get("-555:5"))


# ---- B34: /resume не делит сессию между темами ----

class ResumeTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.a, self.b = f"{GROUP}:1", f"{GROUP}:2"
        self.scopes = {self.a: "alpha", self.b: "gamma"}
        p = patch.object(botmod, "_memory_scope_hint", lambda ctx, cfg, ck: self.scopes.get(ck, ""))
        p.start()
        self.addCleanup(p.stop)

    async def _pick(self, chat_key: str, sid: str) -> SimpleNamespace:
        self.bot_data["resume_options"] = {"t1": (chat_key, sid)}
        q = self.query("resume:t1")
        await botmod.on_resume_pick(self.cb_update(q), self.ctx)
        return q

    async def test_other_scope_refused(self) -> None:
        # Сессия темы A (ALPHA) запускалась ботом: скоуп записан при старте.
        self.store.set(self.a, "sid-alpha")
        botmod._note_session_scope(self.ctx, "sid-alpha", "alpha")
        q = await self._pick(self.b, "sid-alpha")
        self.assertIsNone(self.store.get(self.b))
        self.assertEqual(self.store.get(self.a), "sid-alpha")
        self.assertIn("скоуп", q.edit_message_text.await_args.args[0])

    async def test_other_scope_refused_by_fingerprint(self) -> None:
        # Сессия до этого выпуска: скоупа по sid нет, есть отпечаток темы-владельца.
        self.store.set(self.a, "sid-alpha")
        self.bot_data["scope_fingerprints"].set(self.a, "alpha")
        await self._pick(self.b, "sid-alpha")
        self.assertIsNone(self.store.get(self.b))

    async def test_same_scope_moves_session(self) -> None:
        self.scopes[self.b] = "alpha"
        self.store.set(self.a, "sid-alpha")
        self.bot_data["scope_fingerprints"].set(self.a, "alpha")
        await self._pick(self.b, "sid-alpha")
        self.assertEqual(self.store.get(self.b), "sid-alpha")
        self.assertIsNone(self.store.get(self.a))

    async def test_running_elsewhere_refused(self) -> None:
        self.scopes[self.b] = "alpha"
        self.bot_data["active_sessions"][self.a] = "sid-alpha"
        await self._pick(self.b, "sid-alpha")
        self.assertIsNone(self.store.get(self.b))

    async def test_session_scope_noted_on_run(self) -> None:
        self.scopes[str(GROUP)] = "alpha"
        await self.dispatch("x")
        sid = self.runs[-1]["sid"]
        self.assertEqual(botmod._session_scope(self.ctx, sid), "alpha")


# ---- Ревью 11.10.2026, раунд 4 ----

class ResumeUnknownScopeTests(ResumeTests):
    """B34: сессия с неизвестным скоупом (терминал, до выпуска, после /reset)."""

    async def test_unknown_scope_refused_in_customer_topic(self) -> None:
        q = await self._pick(self.b, "sid-term")
        self.assertIsNone(self.store.get(self.b))
        self.assertIsNone(self.bot_data["scope_fingerprints"].get(self.b))
        self.assertIn("неизвестно", q.edit_message_text.await_args.args[0])

    async def test_unknown_scope_allowed_in_topic_without_scope(self) -> None:
        self.scopes[self.b] = ""
        await self._pick(self.b, "sid-term")
        self.assertEqual(self.store.get(self.b), "sid-term")

    async def test_own_topic_session_resumes(self) -> None:
        # Обычный случай: сессия, начатая ботом в этой же теме.
        self.store.set(self.b, "sid-mine")
        self.bot_data["scope_fingerprints"].set(self.b, "gamma")
        await self._pick(self.b, "sid-mine")
        self.assertEqual(self.store.get(self.b), "sid-mine")
        self.store.remove(self.b)  # /reset, потом /resume своей сессии
        botmod._note_session_scope(self.ctx, "sid-noted", "gamma")
        await self._pick(self.b, "sid-noted")
        self.assertEqual(self.store.get(self.b), "sid-noted")


class PlanGenerationTests(PlanCardTests):
    """B36: старая карточка плана, пока идёт или ждёт новый /plan."""

    async def test_old_card_pressed_while_new_plan_runs(self) -> None:
        self.store.set(str(GROUP), "sid-A")
        old = await self._card()
        picks: list = []

        async def hook(_run):
            picks.append(await self._pick(old))

        self.run_hook = hook
        await self.dispatch("/plan B", permission_mode="plan", unattended=True)
        self.assertEqual(picks[0].dispatched, 0)

    async def test_old_card_waiting_lock_behind_new_plan(self) -> None:
        self.store.set(str(GROUP), "sid-A")
        old = await self._card()
        lock = botmod._chat_lock(self.ctx, str(GROUP))
        await lock.acquire()
        plan_b = asyncio.ensure_future(
            self.dispatch("/plan B", permission_mode="plan", unattended=True))
        await asyncio.sleep(0)
        q = self.query(old)
        with patch.object(botmod, "Update", lambda update_id, message: _update(message)):
            pick = asyncio.ensure_future(botmod.on_plan_pick(self.cb_update(q), self.ctx))
            for _ in range(5):
                await asyncio.sleep(0)
            lock.release()
            await asyncio.wait_for(asyncio.gather(plan_b, pick), 5)
        self.assertEqual([r["prompt"] for r in self.runs], ["/plan B"])


    async def test_delayed_card_of_older_plan_not_published(self) -> None:
        # Ответ плана A задержался, план B за это время прошёл целиком:
        # карточка A не получает поколение B (ревью 11.10.2026).
        self.store.set(str(GROUP), "sid-A")
        cards: list = []
        real_card = botmod._send_plan_card

        async def card(*a, **kw):
            cards.append(a[1].message)
            await real_card(*a, **kw)

        state = {"n": 0}

        async def slow_reply(_cfg, _upd, _text):
            state["n"] += 1
            if state["n"] == 1:
                await self.dispatch("/plan B", permission_mode="plan", unattended=True)

        self.send_reply_text.side_effect = slow_reply
        with patch.object(botmod, "_send_plan_card", card):
            msg_a = await self.dispatch("/plan A", permission_mode="plan", unattended=True)
        pending = self.bot_data["plan_options"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(next(iter(pending.values()))["plan"],
                         botmod._plan_generation(self.ctx, str(GROUP)))
        published_a = [c for c in cards if c is msg_a and any(
            "reply_markup" in call.kwargs for call in c.reply_text.await_args_list)]
        self.assertEqual(published_a, [])


class StopBeforeProcessTests(_Base):
    """B29: остановка во время подготовки OAuth не запускает процесс."""

    async def _run(self, oauth, exec_fn, stop: dict) -> ClaudeResult:
        store = SessionStore(self.state / "s3.json")
        with patch.object(claude_runner.asyncio, "create_subprocess_exec", exec_fn), \
                patch.object(claude_runner, "_ensure_hook_still_valid", lambda *_: None), \
                patch.object(claude_runner, "ensure_oauth_fresh", oauth):
            return await claude_runner.run_claude(
                "claude", "1", 1, None, "x", store, cwd=Path("/"),
                hook_settings=Path("/x.json"), should_stop=lambda: stop["v"],
            )

    async def test_stop_during_oauth_prep(self) -> None:
        stop = {"v": False}

        async def oauth(*_a, **_k):
            stop["v"] = True

        exec_fn = AsyncMock()
        res = await self._run(oauth, exec_fn, stop)
        exec_fn.assert_not_awaited()
        self.assertTrue(res.is_error)

    async def test_stop_right_after_process_created(self) -> None:
        stop = {"v": False}
        real = asyncio.create_subprocess_exec
        procs: list = []

        async def exec_fn(*_a, **kw):
            p = await real("sleep", "30", stdin=asyncio.subprocess.PIPE,
                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                           start_new_session=True)
            procs.append(p)
            stop["v"] = True  # сигнал пришёл, пока процесс создавался
            return p

        res = await asyncio.wait_for(self._run(AsyncMock(), exec_fn, stop), 10)
        self.assertTrue(res.is_error)
        self.assertIsNotNone(procs[0].returncode)

    async def test_dispatch_passes_should_stop_and_skips_codex(self) -> None:
        self.cfg.codex_bin = "codex"
        self.cfg.codex_fallback = True
        self.result_error = True

        async def hook(run):
            self.assertFalse(run["kw"]["should_stop"]())
            self.bot_data["stopping"] = True
            self.assertTrue(run["kw"]["should_stop"]())

        self.run_hook = hook
        with patch.object(botmod, "_is_claude_unavailable", lambda r: True), \
                patch.object(botmod, "_codex_as_claude_result",
                             AsyncMock(return_value=ClaudeResult(text="GPT"))) as codex:
            await self.dispatch("x")
        codex.assert_not_awaited()
        self.assertIn("перезапускается", self.send_reply_text.await_args.args[2])


    async def test_forced_gpt_prefix_passes_should_stop(self) -> None:
        # Ручная ветка «gpt:» передаёт ту же проверку остановки, что и запасной
        # переход на codex (ревью 11.10.2026).
        self.cfg.codex_bin = "codex"
        prefix = self.cfg.codex_trigger_prefix
        with patch.object(botmod, "_codex_as_claude_result",
                          AsyncMock(return_value=ClaudeResult(text="GPT"))) as codex:
            await self.dispatch(f"{prefix} привет")
        codex.assert_awaited_once()
        should_stop = codex.await_args.kwargs["should_stop"]
        self.assertFalse(should_stop())
        self.bot_data["stopping"] = True
        self.assertTrue(should_stop())

    async def test_stop_during_approval_cleanup_skips_codex(self) -> None:
        # Остановка пришла, пока гасились подтверждения упавшего claude:
        # codex после неё не запускается (ревью 11.10.2026).
        self.cfg.codex_bin = "codex"
        self.cfg.codex_fallback = True
        self.result_error = True

        async def cleanup(*_a, **_k):
            self.bot_data["stopping"] = True

        with patch.object(botmod, "_is_claude_unavailable", lambda r: True), \
                patch.object(botmod, "_cleanup_abandoned_approvals", AsyncMock(side_effect=cleanup)), \
                patch.object(botmod, "_codex_as_claude_result",
                             AsyncMock(return_value=ClaudeResult(text="GPT"))) as codex:
            await self.dispatch("x")
        codex.assert_not_awaited()

    async def test_codex_runner_stop_right_after_process_created(self) -> None:
        from claude_tg_bot import codex_runner
        stop = {"v": False}
        real = asyncio.create_subprocess_exec
        procs: list = []

        async def exec_fn(*_a, **kw):
            p = await real("sleep", "30", stdin=asyncio.subprocess.PIPE,
                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                           start_new_session=True)
            procs.append(p)
            stop["v"] = True
            return p

        with patch.object(codex_runner.asyncio, "create_subprocess_exec", exec_fn):
            res = await asyncio.wait_for(codex_runner.run_codex(
                "codex", "x", cwd=Path("/"), should_stop=lambda: stop["v"]), 10)
        self.assertTrue(res.is_error)
        self.assertIsNotNone(procs[0].returncode)

    async def test_codex_runner_stop_before_start(self) -> None:
        from claude_tg_bot import codex_runner
        exec_fn = AsyncMock()
        with patch.object(codex_runner.asyncio, "create_subprocess_exec", exec_fn):
            res = await codex_runner.run_codex(
                "codex", "x", cwd=Path("/"), should_stop=lambda: True)
        exec_fn.assert_not_awaited()
        self.assertTrue(res.is_error)


class PhotoComposeTests(_Base):
    """B33: фото занимает место в /compose до get_file."""

    def _photo(self, get_file) -> SimpleNamespace:
        ps = SimpleNamespace(file_size=10, get_file=get_file)
        return _msg(photo=[ps], document=None)

    async def test_compose_cancel_during_get_file_drops_photo(self) -> None:
        ck = str(GROUP)
        self.cfg.photo_max_size_bytes = 0
        await botmod.on_compose(_update(_msg("/compose")), self.ctx)

        async def get_file():
            self.ctx.args = ["cancel"]
            await botmod.on_compose(_update(_msg("/compose cancel")), self.ctx)
            return SimpleNamespace(download_to_drive=AsyncMock())

        with patch.object(botmod, "_dispatch_to_claude", AsyncMock()) as disp:
            await botmod.on_photo(_update(self._photo(get_file)), self.ctx)
        disp.assert_not_awaited()

    async def test_get_file_error_releases_slot(self) -> None:
        ck = str(GROUP)
        self.cfg.photo_max_size_bytes = 0
        await botmod.on_compose(_update(_msg("/compose")), self.ctx)
        msg = self._photo(AsyncMock(side_effect=RuntimeError("net")))
        await botmod.on_photo(_update(msg), self.ctx)
        self.assertTrue(botmod._compose_buffers(self.ctx)[ck].ready)


class SecretNameTests(unittest.TestCase):
    """b2.1, b5.1: SEND_FILE не отдаёт ключи и учётные данные."""

    def test_key_and_credential_names_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            root = Path(d).resolve()
            names = ("id_rsa", "id_ed25519", "backup.key", "server.pem",
                     "credentials", "credentials.json", "Server.KEY")
            for name in (*names, "report.pdf"):
                (root / name).write_text("x")
            text = "".join(f"<<SEND_FILE:{root / n}>>" for n in (*names, "report.pdf"))
            files, _ = extract_send_file_markers(text, [root])
            self.assertEqual(files, [root / "report.pdf"])


if __name__ == "__main__":
    unittest.main()

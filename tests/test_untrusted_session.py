"""Чужой текст в сессии. С 26.10.2026 (проект шага 1, 2.5) ран /event с
untrusted идёт в своей свежей сессии и переписку темы не трогает; метка
сессии с чужим текстом (ревью 07.10.2026) остаётся для сессий, помеченных
раньше: следующий фоновый ран с полными правами в такой стартует свежей."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult, SessionStore  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402


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


class UntrustedSessionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name)
        self.store = SessionStore(state / "sessions.json")
        self.marks_path = state / "untrusted_sessions.json"
        self.foreign = SessionStore(state / "foreign_sessions.json")
        self.runs: list[dict] = []
        self.answer = "ok"
        cfg = SimpleNamespace(
            codex_trigger_prefix="gpt:", codex_bin="", codex_fallback=False,
            session_idle_ttl_sec=0, claude_bin="claude", claude_cwd=Path("/"),
            hook_settings_path=Path("/x.json"), max_claude_runtime_sec=0,
        )
        self.bot_data = {
            "cfg": cfg, "sessions": self.store, "procs": {}, "active_sessions": {},
            "session_targets": {}, "chat_locks": {},
            "untrusted_sessions": SessionStore(self.marks_path),
            "foreign_sessions": self.foreign,
        }
        self.ctx = SimpleNamespace(
            application=SimpleNamespace(
                bot_data=self.bot_data, running=True,
                create_task=lambda coro, update=None, *, name=None: asyncio.create_task(coro),
            ),
            bot=SimpleNamespace(send_chat_action=AsyncMock()),
        )

        async def fake_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            existing = store.get(chat_key)
            sid = existing or f"sid-{len(self.runs)}"
            self.runs.append({
                "resumed": existing, "prompt": prompt,
                "untrusted": kw.get("untrusted_sids") is not None,
                "foreign": store is self.foreign,
            })
            store.set(chat_key, sid)
            return ClaudeResult(text=self.answer, session_id=sid)

        for name, value in {
            "run_claude": fake_run,
            "_persona_for": lambda *a: "",
            "_memory_scope_hint": lambda *a: "",
            "_effort_for": lambda *a: "",
            "_model_for": lambda *a: "",
            "_activate_pending_model": lambda *a: None,
            "_model_footer_note": lambda *a: "",
            "_RunStatus": _Status,
            "_cleanup_abandoned_approvals": AsyncMock(),
            "send_file_whitelist": lambda cfg: (),
            "send_reply_text": AsyncMock(),
            "send_files": AsyncMock(),
            "send_cost_footer": AsyncMock(),
        }.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _update(self) -> SimpleNamespace:
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=1),
            update_id=1,
            message=SimpleNamespace(
                message_thread_id=None, is_topic_message=False,
                set_reaction=AsyncMock(), reply_text=AsyncMock(),
            ),
        )

    async def _run(self, prompt: str = "x", **kw) -> dict:
        await botmod._dispatch_to_claude(self._update(), self.ctx, prompt, send_voice_reply=False, **kw)
        return self.runs[-1]

    def _legacy_mark(self, sid: str) -> None:
        """Сессия темы, помеченная ранами по чужому тексту до 26.10.2026."""
        botmod._mark_untrusted_session(self.ctx, "1", sid)

    async def test_untrusted_run_uses_own_fresh_session(self) -> None:
        await self._run("утро", unattended=True)
        epoch = botmod._session_epoch(self.ctx, "1")
        run = await self._run("сводка сторожа", unattended=True, untrusted=True)
        self.assertTrue(run["foreign"])
        self.assertIsNone(run["resumed"])
        self.assertEqual(self.store.get("1"), "sid-0")
        self.assertEqual(botmod._session_epoch(self.ctx, "1"), epoch)
        # Сессия темы не помечена: вечер её продолжает.
        self.assertIsNone(SessionStore(self.marks_path).get("sid-0"))
        run = await self._run("вечер", unattended=True)
        self.assertEqual(run["resumed"], "sid-0")

    async def test_dispatcher_marks_foreign_run_before_process(self) -> None:
        seen = []
        real = botmod.run_claude

        async def spy(*a, **kw):
            seen.append(dict(self.bot_data.get("foreign_running", {})))
            return await real(*a, **kw)

        with patch.object(botmod, "run_claude", spy):
            await self._run("сводка", unattended=True, untrusted=True)
            await self._run("Иван пишет")
        self.assertEqual(seen, [{"1": True}, {}])
        self.assertEqual(self.bot_data["foreign_running"], {})

    async def test_foreign_run_reschedules_warm_on_error(self) -> None:
        self.bot_data["warm"] = {"1": {"sid": "sid-0"}}

        async def boom(*_a, **_kw):
            raise RuntimeError("x")

        with patch.object(botmod, "run_claude", boom), patch.object(botmod, "_warm_schedule") as sched:
            await self._run_noresult("сводка", unattended=True, untrusted=True)
        sched.assert_called_once_with(self.ctx.application, "1")
        self.assertEqual(self.bot_data["foreign_running"], {})

    async def _run_noresult(self, prompt: str, **kw) -> None:
        await botmod._dispatch_to_claude(self._update(), self.ctx, prompt, send_voice_reply=False, **kw)

    async def test_untrusted_runs_always_fresh(self) -> None:
        await self._run("сводка 1", unattended=True, untrusted=True)
        run = await self._run("сводка 2", unattended=True, untrusted=True)
        self.assertTrue(run["foreign"])
        self.assertIsNone(run["resumed"])
        # Своя сессия помечена: /resume её в тему унесёт метку с собой.
        self.assertEqual(SessionStore(self.marks_path).get("sid-1"), "1")

    async def test_untrusted_run_keeps_pending_model(self) -> None:
        seen = []
        with patch.object(botmod, "_activate_pending_model", lambda *a: seen.append(a)):
            await self._run("сводка", unattended=True, untrusted=True)
            self.assertEqual(seen, [])
            await self._run("Иван пишет")
        self.assertEqual(len(seen), 1)

    async def test_wake_after_legacy_mark_starts_fresh_session(self) -> None:
        await self._run("утро", unattended=True)
        self._legacy_mark("sid-0")
        run = await self._run("вечер", unattended=True)
        self.assertIsNone(run["resumed"])
        self.assertFalse(run["foreign"])

    async def test_human_message_continues_legacy_marked_session(self) -> None:
        await self._run("утро", unattended=True)
        self._legacy_mark("sid-0")
        run = await self._run("Иван пишет")
        self.assertEqual(run["resumed"], "sid-0")
        run = await self._run("вечер", unattended=True)
        self.assertIsNone(run["resumed"])

    async def test_plan_run_continues_legacy_marked_session(self) -> None:
        await self._run("утро", unattended=True)
        self._legacy_mark("sid-0")
        run = await self._run("план", unattended=True, permission_mode="plan")
        self.assertEqual(run["resumed"], "sid-0")

    async def test_mark_stops_working_after_reset(self) -> None:
        await self._run("утро", unattended=True)
        self._legacy_mark("sid-0")
        self.store.remove("1")  # /reset
        await self._run("Иван пишет")  # новая чистая сессия sid-1
        run = await self._run("вечер", unattended=True)
        self.assertEqual(run["resumed"], "sid-1")

    async def test_untrusted_question_comes_as_text(self) -> None:
        # Свежая сессия рана по чужому тексту только читает: вопрос текстом,
        # отвечать Иван будет Домовому (проект шага 1, 2.5).
        self.answer = "Что делать?<<ASK:Ответить?|да|нет>>"
        upd = self._update()
        with patch.object(botmod, "send_reply_text", AsyncMock()) as send:
            await botmod._dispatch_to_claude(upd, self.ctx, "сводка", send_voice_reply=False,
                                             unattended=True, untrusted=True)
        self.assertFalse(self.bot_data.get("ask_options"))
        self.assertIn(_("Вопрос: {question} ({options})").format(question="Ответить?", options="да; нет"),
                      send.await_args.args[2])

    async def _interrupt(self, *, foreign: bool) -> None:
        """«!»-поправка при живом ране: sid рана в active_sessions, тема без
        сохранённой сессии (первый ран, /reset)."""
        self.bot_data["cfg"].allowed_user_ids = {7}
        self.bot_data["procs"]["1"] = SimpleNamespace(returncode=None, pid=1)
        self.bot_data["active_sessions"]["1"] = "sid-run"
        if foreign:
            self.bot_data["foreign_running"] = {"1": True}

        async def died(*_a):
            # Конец рана снимает метку раньше, чем «!» дождётся процесса.
            self.bot_data.get("foreign_running", {}).pop("1", None)

        lock = botmod._chat_lock(self.ctx, "1")
        await lock.acquire()
        upd = self._update()
        upd.message.text = "!не то, проверь почту"
        upd.message.forward_origin = None
        upd.effective_user = SimpleNamespace(id=7)
        upd.effective_chat = SimpleNamespace(id=1, type="private")
        try:
            for name, value in {
                "_handoff_reply": AsyncMock(return_value=False), "_is_task_ask_reply": lambda *a: False,
                "_task_touch": AsyncMock(return_value=False), "_gather_take": lambda *a: None,
                "_terminate_and_wait": AsyncMock(side_effect=died), "_news_context": lambda c, m, t: t,
                "_dispatch_to_claude": AsyncMock(),
            }.items():
                p = patch.object(botmod, name, value)
                p.start()
                self.addCleanup(p.stop)
            await botmod.on_text(upd, self.ctx)
        finally:
            lock.release()

    async def test_interrupt_of_foreign_run_keeps_its_sid_out(self) -> None:
        # «!» во время рана сторожа в своей сессии: его sid не становится
        # сессией темы (проект шага 1, 2.5).
        await self._interrupt(foreign=True)
        botmod._terminate_and_wait.assert_awaited_once()
        self.assertIsNone(self.store.get("1"))

    async def test_interrupt_of_own_run_restores_sid(self) -> None:
        await self._interrupt(foreign=False)
        self.assertEqual(self.store.get("1"), "sid-run")

    async def test_current_sid_during_foreign_run_is_topic_session(self) -> None:
        self.store.set("1", "sid-topic")
        self.bot_data["active_sessions"]["1"] = "sid-run"
        self.assertEqual(botmod._current_sid(self.ctx, "1"), "sid-run")
        self.bot_data["foreign_running"] = {"1": True}
        self.assertEqual(botmod._current_sid(self.ctx, "1"), "sid-topic")

    async def test_foreign_run_reschedules_waiting_warm(self) -> None:
        self.bot_data["warm"] = {"1": {"sid": "sid-0"}}
        locked = []
        with patch.object(botmod, "_warm_schedule",
                          side_effect=lambda *a: locked.append(botmod._chat_lock(self.ctx, "1").locked())) as sched:
            await self._run("сводка", unattended=True, untrusted=True)
        sched.assert_called_once_with(self.ctx.application, "1")
        # Пинг назначен, когда замок темы уже отпущен (ревью Codex 26.10.2026).
        self.assertEqual(locked, [False])

    async def _ask_card(self, **kw) -> tuple[SimpleNamespace, str]:
        """Ран задаёт вопрос кнопками; вернуть карточку и токен первого варианта."""
        self.answer = "Что делать?<<ASK:Понижать базу?|Да, удалить|Оставить>>"
        card = SimpleNamespace(text="❓ Понижать базу?", message_thread_id=None, is_topic_message=False,
                               set_reaction=AsyncMock(), reply_text=AsyncMock())
        upd = self._update()
        upd.message.reply_text = AsyncMock(return_value=card)
        await botmod._dispatch_to_claude(upd, self.ctx, "утренний план", send_voice_reply=False, **kw)
        self.answer = "ok"
        token = next(t for t, v in self.bot_data["ask_options"].items() if v["answer"] == "Да, удалить")
        return card, token

    async def _tap(self, card: SimpleNamespace, token: str) -> SimpleNamespace:
        query = SimpleNamespace(
            data=f"ask:{token}", answer=AsyncMock(), message=card,
            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        )
        self.bot_data["cfg"].allowed_user_ids = {7}
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                                 effective_chat=SimpleNamespace(id=7, type="private"),
                                 update_id=2)
        with patch.object(botmod, "Update", lambda update_id, message: SimpleNamespace(
                update_id=update_id, message=message, effective_chat=SimpleNamespace(id=1))):
            await botmod.on_ask_pick(update, self.ctx)
        return query

    async def test_ask_pick_after_untrusted_run_is_not_lost(self) -> None:
        # Случай 23.10.2026: вопрос утреннего плана (фоновый ран), затем
        # сторож с чужим текстом в той же сессии, затем нажатие Ивана: ответ
        # уходит в ту же сессию, как его сообщение.
        card, token = await self._ask_card(unattended=True)
        await self._run("сводка сторожа", unattended=True, untrusted=True)
        before = len(self.runs)
        await self._tap(card, token)
        self.assertEqual(len(self.runs), before + 1, "ответ кнопкой потерян")
        run = self.runs[-1]
        self.assertEqual(run["resumed"], "sid-0")
        self.assertFalse(run["untrusted"])
        self.assertIn("Да, удалить", run["prompt"])
        # Сторож шёл в своей сессии: вечер продолжает разговор (проект шага 1, 2.5).
        run = await self._run("вечер", unattended=True)
        self.assertEqual(run["resumed"], "sid-0")

    async def test_ask_pick_runs_as_human_message(self) -> None:
        card, token = await self._ask_card(unattended=True)
        seen = []
        real = botmod._dispatch_to_claude

        async def spy(upd, ctx, prompt, **kw):
            seen.append((kw.get("unattended"), kw.get("permission_mode")))
            return await real(upd, ctx, prompt, **kw)

        with patch.object(botmod, "_dispatch_to_claude", spy):
            await self._tap(card, token)
        self.assertEqual(seen, [(False, "bypassPermissions")])

    async def test_plan_question_pick_stays_plan(self) -> None:
        card, token = await self._ask_card(permission_mode="plan", unattended=True)
        seen = []
        real = botmod._dispatch_to_claude

        async def spy(upd, ctx, prompt, **kw):
            seen.append(kw.get("permission_mode"))
            return await real(upd, ctx, prompt, **kw)

        with patch.object(botmod, "_dispatch_to_claude", spy):
            await self._tap(card, token)
        self.assertEqual(seen, ["plan"])

    async def test_ask_pick_after_reset_names_question(self) -> None:
        card, token = await self._ask_card()
        self.store.remove("1")  # /reset
        botmod._bump_session_epoch(self.ctx, "1")
        before = len(self.runs)
        query = await self._tap(card, token)
        self.assertEqual(len(self.runs), before)
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Понижать базу?", text)
        self.assertIn("Да, удалить", text)

    async def test_epoch_change_while_waiting_for_lock_names_question(self) -> None:
        ref = {"question": "Понижать базу?", "answer": "Да, удалить"}
        await self._run("первый")
        epoch = botmod._session_epoch(self.ctx, "1")
        botmod._bump_session_epoch(self.ctx, "1")
        upd = self._update()
        before = len(self.runs)
        await botmod._dispatch_to_claude(upd, self.ctx, "ответ", send_voice_reply=False,
                                         from_callback=True, expect_epoch=epoch, ask_ref=ref)
        self.assertEqual(len(self.runs), before)
        text = upd.message.reply_text.await_args.args[0]
        self.assertIn("Понижать базу?", text)
        self.assertIn("Да, удалить", text)

    async def test_plan_button_epoch_change_still_refused(self) -> None:
        await self._run("первый")
        epoch = botmod._session_epoch(self.ctx, "1")
        botmod._bump_session_epoch(self.ctx, "1")
        upd = self._update()
        before = len(self.runs)
        await botmod._dispatch_to_claude(upd, self.ctx, "одобряю", send_voice_reply=False,
                                         from_callback=True, expect_epoch=epoch)
        self.assertEqual(len(self.runs), before)
        self.assertIn(_("⚠️ сессия темы сменилась, ответ в новую сессию не отправлен"),
                      upd.message.reply_text.await_args.args[0])

    async def test_stale_button_refused_before_limit(self) -> None:
        # Скоуп темы сменился (сессия сбросится в этом ране), а лимит на резерве:
        # отказ называет вопрос, лимит не спрашивается (проект шага 1, 2.2).
        fps = SessionStore(Path(self.tmp.name) / "fps.json")
        self.bot_data["scope_fingerprints"] = fps
        card, token = await self._ask_card()
        fps.set("1", "другой-скоуп")
        hold = AsyncMock(return_value=True)
        before = len(self.runs)
        with patch.object(botmod, "_limit_hold", hold):
            query = await self._tap(card, token)
        self.assertEqual(len(self.runs), before)
        hold.assert_not_awaited()
        self.assertIn("Понижать базу?", card.reply_text.await_args.args[0])
        self.assertIsNotNone(query)

    async def test_same_session_button_still_checks_limit(self) -> None:
        card, token = await self._ask_card()
        hold = AsyncMock(return_value=True)
        before = len(self.runs)
        with patch.object(botmod, "_limit_hold", hold):
            await self._tap(card, token)
        hold.assert_awaited_once()
        self.assertEqual(len(self.runs), before)

    async def test_session_will_change_on_idle(self) -> None:
        self.bot_data["cfg"].session_idle_ttl_sec = 60
        self.store.set("1", "sid-old")
        epoch = botmod._session_epoch(self.ctx, "1")
        self.assertFalse(botmod._session_will_change(self.ctx, self.bot_data["cfg"], "1", epoch))
        self.store.data["1"]["ts"] = time.time() - 120
        self.assertTrue(botmod._session_will_change(self.ctx, self.bot_data["cfg"], "1", epoch))
        self.assertTrue(botmod._session_will_change(self.ctx, self.bot_data["cfg"], "1", epoch + 1))

    def test_card_message_matched_by_chat(self) -> None:
        # У InaccessibleMessage нет chat_id, только chat.
        picked = {"msg": (5, 9, None)}
        self.assertTrue(botmod._card_msg_matches(picked, SimpleNamespace(chat=SimpleNamespace(id=5), message_id=9)))
        self.assertFalse(botmod._card_msg_matches(picked, SimpleNamespace(chat=SimpleNamespace(id=6), message_id=9)))
        self.assertTrue(botmod._card_msg_matches({}, None))

    async def test_limit_refusal_names_dropped_plan(self) -> None:
        upd = self._update()
        with patch.object(botmod, "_limit_hold", AsyncMock(return_value=True)):
            await botmod._dispatch_to_claude(upd, self.ctx, "ответ", send_voice_reply=False, from_callback=True,
                                             expect_epoch=0, ask_ref={"dropped_plan": True})
        self.assertIn(_("Проект решения снят, как после твоего сообщения; ответь текстом."),
                      upd.message.reply_text.await_args.args[0])

    async def test_card_epoch_taken_under_lock(self) -> None:
        # /reset, пока ответ с вопросом уходил после замка: карточка получила бы
        # эпоху новой сессии и ответила бы в неё (проект шага 1, 2.3).
        async def reset_during_send(*_a, **_kw):
            self.store.remove("1")
            botmod._bump_session_epoch(self.ctx, "1")

        with patch.object(botmod, "send_reply_text", AsyncMock(side_effect=reset_during_send)):
            card, token = await self._ask_card()
        before = len(self.runs)
        query = await self._tap(card, token)
        self.assertEqual(len(self.runs), before)
        self.assertIn("Понижать базу?", query.edit_message_text.await_args.args[0])

    async def test_ask_pick_from_disallowed_chat_ignored(self) -> None:
        card, token = await self._ask_card()
        query = SimpleNamespace(data=f"ask:{token}", answer=AsyncMock(), message=card,
                                edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
        self.bot_data["cfg"].allowed_user_ids = {7}
        self.bot_data["cfg"].allowed_chat_ids = set()
        before = len(self.runs)
        await botmod.on_ask_pick(SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=-555, type="supergroup"), update_id=2), self.ctx)
        self.assertEqual(len(self.runs), before)
        self.assertIn(token, self.bot_data["ask_options"])

    async def test_ask_pick_under_other_message_rejected(self) -> None:
        self.answer = "Что делать?<<ASK:Понижать базу?|Да, удалить|Оставить>>"
        card = SimpleNamespace(text="❓ Понижать базу?", chat_id=1, message_id=50, message_thread_id=None,
                               is_topic_message=False, set_reaction=AsyncMock(), reply_text=AsyncMock())
        upd = self._update()
        upd.message.reply_text = AsyncMock(return_value=card)
        await botmod._dispatch_to_claude(upd, self.ctx, "план", send_voice_reply=False)
        self.answer = "ok"
        token = next(t for t, v in self.bot_data["ask_options"].items() if v["answer"] == "Да, удалить")
        other = SimpleNamespace(text="другое", chat_id=1, message_id=51, message_thread_id=None,
                                is_topic_message=False, set_reaction=AsyncMock(), reply_text=AsyncMock())
        before = len(self.runs)
        query = await self._tap(other, token)
        self.assertEqual(len(self.runs), before)
        self.assertIn(_("Ответ не принят: кнопка не от этого вопроса."), query.answer.await_args.args[0])
        self.assertEqual(len(self.bot_data["ask_options"]), 2)
        await self._tap(card, token)
        self.assertEqual(len(self.runs), before + 1)


if __name__ == "__main__":
    unittest.main()

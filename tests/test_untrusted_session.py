"""Чужой текст в сессии (ревью 07.10.2026): после рана /event с untrusted
следующий фоновый ран с полными правами в этом чате стартует свежей сессией,
а кнопки вопроса из такого рана продолжают его только на чтение."""
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

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult, SessionStore  # noqa: E402


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

    async def test_wake_after_untrusted_starts_fresh_session(self) -> None:
        await self._run("утро", unattended=True)
        await self._run("сводка сторожа", unattended=True, untrusted=True)
        self.assertEqual(self.runs[-1]["resumed"], "sid-0")  # untrusted продолжает
        run = await self._run("вечер", unattended=True)
        self.assertIsNone(run["resumed"])
        # Метка переживает рестарт: новый стор с того же файла её видит.
        self.assertEqual(SessionStore(self.marks_path).get("sid-0"), "1")

    async def test_human_message_continues_session(self) -> None:
        await self._run("сводка", unattended=True, untrusted=True)
        run = await self._run("Иван пишет")
        self.assertEqual(run["resumed"], "sid-0")
        # Сообщение Ивана метку не снимает: вечерний ран всё равно свежий.
        run = await self._run("вечер", unattended=True)
        self.assertIsNone(run["resumed"])

    async def test_plan_run_continues_session(self) -> None:
        await self._run("сводка", unattended=True, untrusted=True)
        run = await self._run("план", unattended=True, permission_mode="plan")
        self.assertEqual(run["resumed"], "sid-0")

    async def test_mark_stops_working_after_reset(self) -> None:
        await self._run("сводка", unattended=True, untrusted=True)
        self.store.remove("1")  # /reset
        await self._run("Иван пишет")  # новая чистая сессия sid-1
        run = await self._run("вечер", unattended=True)
        self.assertEqual(run["resumed"], "sid-1")

    async def test_untrusted_followups_continue(self) -> None:
        await self._run("сводка 1", unattended=True, untrusted=True)
        run = await self._run("сводка 2", unattended=True, untrusted=True)
        self.assertEqual(run["resumed"], "sid-0")

    async def test_ask_buttons_keep_untrusted(self) -> None:
        self.answer = "Что делать?<<ASK:Ответить?|да|нет>>"
        card = SimpleNamespace(text="❓ Ответить?", message_thread_id=None, is_topic_message=False,
                               set_reaction=AsyncMock(), reply_text=AsyncMock())
        upd = self._update()
        upd.message.reply_text = AsyncMock(return_value=card)
        await botmod._dispatch_to_claude(upd, self.ctx, "сводка", send_voice_reply=False,
                                         unattended=True, untrusted=True)
        pending = self.bot_data["ask_options"]
        self.assertTrue(pending)
        token = next(iter(pending))
        self.assertTrue(pending[token]["untrusted"])

        self.answer = "ok"
        query = SimpleNamespace(
            data=f"ask:{token}", answer=AsyncMock(), message=card,
            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        )
        self.bot_data["cfg"].allowed_user_ids = {7}
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                                 update_id=2)
        with patch.object(botmod, "Update", lambda update_id, message: SimpleNamespace(
                update_id=update_id, message=message, effective_chat=SimpleNamespace(id=1))):
            await botmod.on_ask_pick(update, self.ctx)
        run = self.runs[-1]
        self.assertTrue(run["untrusted"])
        self.assertTrue(run["prompt"].startswith(botmod.UNTRUSTED_PROMPT_NOTE))
        self.assertEqual(run["resumed"], "sid-0")


if __name__ == "__main__":
    unittest.main()

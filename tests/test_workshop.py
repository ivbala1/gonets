"""Мастерская помощника (проект Домового 22.10.2026, решение Ивана 3а): тема для
долгой работы; модель и усилие по умолчанию, итог и сбой в личку, вопрос
текстом, карточки в личке, /cancel и /status из лички, перезапуск в два шага."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_tg_bot import approval  # noqa: E402
from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402
from test_review_bot_main import GROUP, _Base, _msg  # noqa: E402

WS = f"{GROUP}:77"


class WorkshopDefaultsTests(unittest.TestCase):
    def _ctx(self, **bot_data):
        cfg = SimpleNamespace(workshop_chat_key=WS, workshop_model="opus", workshop_effort="high",
                              workshop_scope="Мастерская: прочитай workshop.md", model="", effort_level="medium")
        return SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg, **bot_data})), cfg

    def test_model_effort_scope_defaults_and_overrides(self) -> None:
        ctx, cfg = self._ctx()
        self.assertEqual(botmod._model_for(ctx, cfg, WS), "opus")
        self.assertEqual(botmod._effort_for(ctx, cfg, WS), "high")
        self.assertIn("workshop.md", botmod._scope_note(ctx, WS))
        self.assertEqual(botmod._effort_for(ctx, cfg, str(GROUP)), "medium")
        self.assertIsNone(botmod._scope_note(ctx, str(GROUP)))
        ctx.application.bot_data["effort_by_target"] = {WS: "low"}
        ctx.application.bot_data["model_by_target"] = {WS: "sonnet"}
        self.assertEqual((botmod._model_for(ctx, cfg, WS), botmod._effort_for(ctx, cfg, WS)), ("sonnet", "low"))

    def test_status_line(self) -> None:
        ctx, cfg = self._ctx()
        self.assertIn(_("🛠 мастерская: свободна"), botmod._workshop_status(ctx, cfg))
        ctx.application.bot_data.update(workshop_run={"since": 0.0, "what": "поиск"}, lock_waiting={WS: 2})
        line = botmod._workshop_status(ctx, cfg)
        self.assertIn("поиск", line)
        self.assertIn("2", line)
        self.assertEqual(botmod._workshop_status(ctx, SimpleNamespace()), "")


class WorkshopDispatchTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.workshop_chat_key = WS
        self.cfg.primary_chat_id = 1
        self.ctx.bot.send_message = AsyncMock()

    def _forwarded(self) -> list[str]:
        return [c.args[1] for c in self.ctx.bot.send_message.await_args_list if c.args and c.args[0] == 1]

    async def test_background_result_forwarded_quietly(self) -> None:
        self.answer = "готово: нашёл 3 вакансии"
        await self.dispatch("Поиск", msg=_msg(thread=77), unattended=True)
        sent = self._forwarded()
        self.assertEqual(len(sent), 1)
        self.assertIn("нашёл 3 вакансии", sent[0])
        self.assertTrue(self.ctx.bot.send_message.await_args.kwargs["disable_notification"])

    async def test_owner_message_in_topic_not_forwarded(self) -> None:
        await self.dispatch("сделай", msg=_msg(thread=77))
        self.assertEqual(self._forwarded(), [])

    async def test_ask_becomes_text(self) -> None:
        self.answer = "готово\n<<ASK: Отправлять? | да | нет>>"
        sent_cards = []
        orig = botmod._send_ask_card

        async def card(*a, **kw):
            sent_cards.append(a)
            return await orig(*a, **kw)
        botmod._send_ask_card = card
        try:
            await self.dispatch("Наряд", msg=_msg(thread=77), unattended=True)
        finally:
            botmod._send_ask_card = orig
        self.assertEqual(sent_cards, [])
        self.assertIn(_("Вопрос: {question} ({options})").format(question="Отправлять?", options="да; нет"),
                      self._forwarded()[0])

    async def test_show_key_passed_to_run(self) -> None:
        await self.dispatch("Наряд", msg=_msg(thread=77), unattended=True)
        self.assertEqual(self.runs[-1]["kw"]["show_chat_key"], "1")
        await self.dispatch("x")
        self.assertEqual(self.runs[-1]["kw"]["show_chat_key"], "")


class WorkshopRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_step_refuses_only_workshop(self) -> None:
        cfg = SimpleNamespace(workshop_chat_key=WS, primary_chat_id=1, state_dir=Path(tempfile.mkdtemp()))
        lock = asyncio.Lock()
        await lock.acquire()
        app = SimpleNamespace(bot_data={"cfg": cfg, "chat_locks": {WS: lock}, "active_work": 0})
        self.assertTrue(botmod._workshop_busy(app))
        app.bot_data["draining_keys"] = {WS}
        impl = AsyncMock(return_value=None)
        orig = botmod._dispatch_to_claude_impl
        botmod._dispatch_to_claude_impl = impl
        try:
            def upd(thread):
                m = _msg(thread=thread)
                return SimpleNamespace(update_id=1, message=m, effective_message=m, callback_query=None,
                                       effective_chat=SimpleNamespace(id=GROUP, type="supergroup"),
                                       effective_user=SimpleNamespace(id=7))
            ctx = SimpleNamespace(application=app)
            refused = await botmod._dispatch_to_claude(upd(77), ctx, "x", send_voice_reply=False)
            await botmod._dispatch_to_claude(upd(None), ctx, "y", send_voice_reply=False)
        finally:
            botmod._dispatch_to_claude_impl = orig
        self.assertTrue(refused)
        self.assertEqual(impl.await_count, 1)  # прошла только личка
        lock.release()
        self.assertFalse(botmod._workshop_busy(app))


class WorkshopCardTests(unittest.TestCase):
    def test_card_shown_in_private_chat_with_origin(self) -> None:
        async def go():
            registry = approval.ApprovalRegistry()
            pending = registry.create("mcp__telegram__send_message", {"chat_id": "@hr", "message": "hi"},
                                      session_id="sid", target_chat_id=1, origin_key=WS)
            bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
            await approval.send_approval_request(
                SimpleNamespace(bot=bot), SimpleNamespace(primary_chat_id=1, state_dir=Path(tempfile.mkdtemp())),
                pending)
            return bot.send_message.call_args.kwargs
        kw = asyncio.run(go())
        self.assertEqual(kw["chat_id"], 1)
        self.assertIn(_("🛠 <b>Мастерская</b>"), kw["text"])


if __name__ == "__main__":
    unittest.main()


class CostFooterTests(unittest.TestCase):
    """Решение Ивана 22.10.2026: строка цены только у дорогих ответов."""

    def _send(self, cost, model_note="", ctx_tokens=0):
        from claude_tg_bot import replies
        cfg = SimpleNamespace(show_cost_footer=True, cost_footer_min_usd=1.0, context_warn_tokens=100_000)
        result = SimpleNamespace(session_cost_usd=5.0, cost_usd=cost, context_tokens=ctx_tokens, context_max=0,
                                 compacted=0)
        msg = SimpleNamespace(reply_text=AsyncMock())
        asyncio.run(replies.send_cost_footer(cfg, SimpleNamespace(message=msg), result, model_note))
        return msg.reply_text.await_count

    def test_threshold(self) -> None:
        self.assertEqual(self._send(0.4), 0)
        self.assertEqual(self._send(1.5), 1)
        self.assertEqual(self._send(0.4, model_note="задача ≈$2"), 1)
        self.assertEqual(self._send(0.4, ctx_tokens=200_000), 1)

    def test_day_sum(self) -> None:
        from claude_tg_bot.overrides import PersistentDict
        with tempfile.TemporaryDirectory() as d:
            days = PersistentDict(Path(d) / "c.json")
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cost_by_day": days}))
            botmod._cost_day_add(ctx, 1.25)
            botmod._cost_day_add(ctx, 0.5)
            self.assertIn("$1.75", botmod._cost_day_line(ctx))


class AgentRoleTests(unittest.TestCase):
    """Проект Домового 22.10.2026, добавка А: в чатах Домового субагент только с ролью цены."""

    cfg = SimpleNamespace(agent_role_chat_keys=frozenset({"1", WS}))

    def test_rules(self) -> None:
        ok = approval._agent_role_refusal
        self.assertIsNone(ok(self.cfg, "1", {"subagent_type": "scout"}))
        self.assertIsNone(ok(self.cfg, "1", {"subagent_type": "worker"}))
        self.assertIsNone(ok(self.cfg, WS, {"subagent_type": "general-purpose", "model": "opus", "effort": "high"}))
        self.assertIn("fork", ok(self.cfg, "1", {"subagent_type": "fork"}))
        self.assertIn("model", ok(self.cfg, "1", {"subagent_type": "general-purpose", "model": "sonnet"}))
        self.assertIn("model", ok(self.cfg, "1", {"prompt": "x"}))  # без типа это general-purpose
        self.assertIsNone(ok(self.cfg, "-5:9", {"subagent_type": "fork"}))  # другие чаты как раньше
        self.assertIsNone(ok(SimpleNamespace(), "1", {"subagent_type": "fork"}))

    def test_hook_matches_agent_tools(self) -> None:
        import json
        root = Path(__file__).resolve().parents[1]
        # Рабочий файл и пример для открытой копии (в ней лежит только пример).
        paths = [p for p in (root / "claude-hook-settings.json",
                             root / "public" / "claude-hook-settings.example.json",
                             root / "claude-hook-settings.example.json") if p.exists()]
        self.assertTrue(paths)
        for path in paths:
            matcher = json.loads(path.read_text())["hooks"]["PreToolUse"][0]["matcher"]
            self.assertIn("Agent", matcher.split("|"), path.name)
            self.assertIn("Task", matcher.split("|"), path.name)


class AskCardTests(_Base):
    """1.5: один вопрос, одна живая карточка; срок кнопок 12 часов."""

    async def test_repeat_question_retires_old_card(self) -> None:
        self.ctx.bot.edit_message_text = AsyncMock()
        msg = _msg()
        msg.reply_text = AsyncMock(side_effect=[SimpleNamespace(chat_id=GROUP, message_id=101),
                                                SimpleNamespace(chat_id=GROUP, message_id=102)])
        upd = SimpleNamespace(message=msg)
        for _ in range(2):
            await botmod._send_ask_card(self.ctx, upd, str(GROUP), ("Отправлять?", ["да", "нет"]),
                                        permission_mode="bypassPermissions", epoch=0)
        pending = self.bot_data["ask_options"]
        self.assertEqual({v["msg"][1] for v in pending.values()}, {102})
        # Старая карточка правится на месте, без отдельного сообщения (29.10.2026).
        self.ctx.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.ctx.bot.edit_message_text.await_args.kwargs["message_id"], 101)

    async def test_old_card_expires(self) -> None:
        pending = self.bot_data.setdefault("ask_options", {})
        pending["tok"] = {"chat_key": str(GROUP), "answer": "да", "question": "q", "card": "c", "at": 0.0,
                          "permission_mode": "bypassPermissions", "epoch": 0}
        query = SimpleNamespace(data="ask:tok", answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                                message=_msg())
        upd = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                              effective_chat=SimpleNamespace(id=GROUP, type="supergroup"))
        await botmod.on_ask_pick(upd, self.ctx)
        self.assertEqual(_("Ответ не принят: вопрос устарел (старше 12 часов, заменён новым, бот "
                           "перезапускался) или на него уже ответили. Если нужно, напиши ответ текстом."),
                         query.answer.await_args.args[0])
        self.assertEqual(self.runs, [])


class WorkshopOtklikFreshTests(unittest.TestCase):
    """Разбор 22.10.2026, решение Ивана: ✅ в мастерской свежей сессией, модель та же."""

    def _press(self, source: str, at_jobs=None):
        from claude_tg_bot import otklik
        with tempfile.TemporaryDirectory() as d:
            base = otklik.otklik_dir(Path(d))
            rec = otklik.new_record("Acme", "почта", "Hello")
            rec.update(chat_id=1, message_id=10, source=source)
            otklik.save(base, rec)
            cfg = SimpleNamespace(allowed_user_ids={7}, state_dir=Path(d), workshop_chat_key=WS)
            query = SimpleNamespace(data=f"otk:{rec['id']}:{rec['sha']}:0:o",
                                    message=SimpleNamespace(message_id=10, chat_id=1),
                                    answer=AsyncMock(), edit_message_text=AsyncMock(),
                                    edit_message_reply_markup=AsyncMock())
            update = SimpleNamespace(callback_query=query, update_id=1, effective_user=SimpleNamespace(id=7))
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg, "at_jobs": at_jobs}),
                                  bot=SimpleNamespace(edit_message_text=AsyncMock()))
            dispatch = AsyncMock(return_value=None)
            orig_d, orig_u, orig_s = botmod._dispatch_to_claude, botmod.Update, botmod._synthetic_update
            botmod._dispatch_to_claude, botmod.Update = dispatch, (lambda **kw: kw)
            botmod._synthetic_update = lambda *a: "synthetic"
            try:
                asyncio.run(botmod.on_otklik_pick(update, ctx))
            finally:
                botmod._dispatch_to_claude, botmod.Update, botmod._synthetic_update = orig_d, orig_u, orig_s
            return dispatch.call_args

    def test_workshop_source_runs_fresh(self) -> None:
        call = self._press(WS)
        self.assertTrue(call.kwargs["fresh"]())
        self.assertEqual(call.args[0], "synthetic")

    def test_private_source_keeps_session(self) -> None:
        self.assertFalse(self._press("1").kwargs["fresh"]())

    def test_pending_limit_retry_keeps_session(self) -> None:
        # Ревью 23.10.2026: повтор после лимита продолжит сессию темы, ✅ её не сносит.
        import json
        jobs = SimpleNamespace(data={"j1": {"v": json.dumps({"chat_key": WS, "limit_retry": 1})}})
        self.assertFalse(self._press(WS, jobs).kwargs["fresh"]())
        other = SimpleNamespace(data={"j1": {"v": json.dumps({"chat_key": "1", "limit_retry": 1})}})
        self.assertTrue(self._press(WS, other).kwargs["fresh"]())

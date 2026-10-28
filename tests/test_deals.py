"""Дела: вопросы утреннего плана, ответ на которые продолжает разговор с
момента вопроса (опыт В1, docs/design-2026-10-26-v1.md)."""
from __future__ import annotations

import asyncio
import json
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
from claude_tg_bot import claude_runner, deals  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult, SessionStore  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402
from tests.texts import msg_re  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_warm import _Proc, _usage  # noqa: E402

QSID = "11111111-1111-4111-8111-111111111111"
FORK = "33333333-3333-4333-8333-333333333333"


class DealRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _deal(self, **kw) -> dict:
        d = deals.new_deal(chat_id=1, thread_id=None, qsid=QSID, quuid="u1",
                           question="Звонить маме сегодня?", options=["да", "нет"])
        d.update(kw)
        return d

    def test_head_round_trip(self) -> None:
        d = self._deal()
        self.assertEqual(deals.id_from_text(deals.head(d) + "\nтекст"), d["id"])
        self.assertIsNone(deals.id_from_text("🧵 не дело"))
        self.assertIsNone(deals.id_from_text(None))
        self.assertLessEqual(len(deals.make_label("очень " * 20)), deals.LABEL_CAP)

    def test_button_accepts_once_swipe_after_turn(self) -> None:
        d = self._deal()
        self.assertTrue(deals.accept(d, "да"))
        self.assertTrue(d["answering"])
        self.assertFalse(deals.accept(d, "нет"), "второе нажатие")
        self.assertFalse(deals.accept(d, "текст", states=deals.SWIPE_STATES), "дело занято")
        self.assertFalse(deals.start(d, "нет"), "чужой ответ не стартует")
        self.assertTrue(deals.start(d, "да"))
        self.assertEqual((d["state"], d["turns"]), ("running", 1))
        d["state"] = "idle"
        self.assertFalse(deals.accept(d, "ещё"), "кнопка только у вопроса")
        self.assertTrue(deals.accept(d, "ещё", states=deals.SWIPE_STATES))
        self.assertFalse(d["answering"])

    def test_closed_and_stale_not_accepted(self) -> None:
        d = self._deal(last_at=time.time() - deals.DEAL_IDLE_SEC - 1)
        self.assertFalse(deals.accept(d, "да"))
        d = self._deal(state="closed")
        self.assertFalse(deals.accept(d, "да", states=deals.SWIPE_STATES))

    def test_cap_counts_open_deals_of_chat(self) -> None:
        data = {d["id"]: d for d in (self._deal(), self._deal(), self._deal(state="closed"),
                                     self._deal(chat_id=2))}
        self.assertEqual(deals.open_count(data, 1, None), 2)

    def test_startup_wakes_accepted_cuts_running(self) -> None:
        acc, run, old = self._deal(state="accepted", answered_at=time.time()), self._deal(state="running"), self._deal(
            last_at=time.time() - deals.KEEP_CLOSED_SEC - 1)
        stale = self._deal(last_at=time.time() - deals.DEAL_IDLE_SEC - 1)
        data = {d["id"]: d for d in (acc, run, old, stale)}
        data["bad"] = {"x": 1}
        wake, cut = deals.startup(data)
        self.assertEqual([d["id"] for d in wake], [acc["id"]])
        self.assertEqual([d["id"] for d in cut], [run["id"]])
        self.assertEqual(run["state"], "interrupted")
        self.assertNotIn(old["id"], data)
        self.assertNotIn("bad", data)
        self.assertEqual(data[stale["id"]]["state"], "closed")

    def test_startup_old_accepted_named_not_run(self) -> None:
        old_acc = self._deal(state="accepted", answered_at=time.time() - deals.DEAL_IDLE_SEC - 1,
                             last_at=time.time() - deals.KEEP_CLOSED_SEC - 1)
        old_run = self._deal(state="running", last_at=time.time() - deals.KEEP_CLOSED_SEC - 1)
        data = {d["id"]: d for d in (old_acc, old_run)}
        wake, cut = deals.startup(data)
        self.assertEqual(wake, [])
        self.assertEqual({d["id"] for d in cut}, {old_acc["id"], old_run["id"]})
        self.assertEqual((old_acc["state"], old_acc["error"]), ("failed", "expired"))
        self.assertEqual(old_run["state"], "interrupted")

    def test_notes_dropped_exactly(self) -> None:
        d = self._deal()
        deals.add_note(d, "первая", 1.0)
        data = {d["id"]: d}
        taken = deals.pending_notes(data, 1, None)
        deals.add_note(d, "вторая", 2.0)
        deals.drop_notes(data, taken)
        self.assertEqual([n["text"] for n in d["notes"]], ["вторая"])
        self.assertEqual(deals.pending_notes(data, 2, None), [])

    def test_locked_refuses_broken_file(self) -> None:
        self.base.mkdir(exist_ok=True)
        (self.base / "deals.json").write_text("[]")
        with self.assertRaises(ValueError):
            with deals.locked(self.base):
                pass
        self.assertEqual(deals.load(self.base), {})


class RunClaudeForkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "deal_sessions.json")
        self.costs = {QSID: "1.000000"}
        self.calls: list[tuple] = []
        self.registry: dict = {}
        self.targets: dict = {}
        self.seen: list[str] = []

    async def _run(self, procs: list[_Proc], **kw) -> ClaudeResult:
        queue = list(procs)

        async def fake_exec(*args, **kwargs):
            self.calls.append(args)
            proc = queue.pop(0)
            # Во время рана: учёт уже на настоящем номере ответвления.
            proc.registry_at_start = dict(self.registry)
            return proc

        with patch.object(claude_runner, "_ensure_hook_still_valid", lambda *_: None), \
                patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()), \
                patch.object(claude_runner.asyncio, "create_subprocess_exec", fake_exec):
            return await claude_runner.run_claude(
                "claude", "1", 1, None, "ответ", self.store, cwd=Path("/tmp"),
                hook_settings=Path("/tmp/x.json"), cost_totals=self.costs,
                session_registry=self.registry, session_targets=self.targets,
                on_session_start=self.seen.append, **kw)

    def _events(self) -> list[dict]:
        return [
            {"type": "system", "subtype": "init", "session_id": FORK, "tools": ["Read"]},
            {"type": "assistant", "uuid": "a1", "session_id": FORK,
             "message": {"id": "m1", "usage": _usage(50_000, 1_000), "content": [{"type": "tool_use"}]}},
            {"type": "assistant", "uuid": "sub", "parent_tool_use_id": "t", "session_id": FORK,
             "message": {"id": "s1", "usage": _usage(1, 1)}},
            {"type": "assistant", "uuid": "a2", "session_id": FORK,
             "message": {"id": "m2", "usage": _usage(51_000, 500), "content": [{"type": "text"}]}},
            {"type": "result", "result": "ок<<ASK:Ещё?|да|нет>>", "session_id": FORK,
             "total_cost_usd": 1.25, "usage": _usage(51_000, 500)},
        ]

    async def test_fork_args_session_and_point(self) -> None:
        result = await self._run([_Proc(self._events())], fork_at=(QSID, "u-question"), fresh_retry=False,
                                 deal_id="abcd1234")
        args = list(self.calls[0])
        tail = args[args.index("--resume"):]
        self.assertEqual(tail, ["--resume", QSID, "--resume-session-at", "u-question", "--fork-session"])
        self.assertNotIn("--session-id", args)
        self.assertEqual(result.session_id, FORK)
        self.assertEqual(self.store.get("1"), FORK)
        # Точка: последний ответ основного диалога перед result, не подагент.
        self.assertEqual(result.last_uuid, "a2")
        # Учёт переписан на настоящий номер и снят в конце.
        self.assertIn(FORK, self.seen)
        self.assertEqual(self.registry, {})
        self.assertEqual(self.targets, {})
        # Цена первого хода: сумма ответвления минус сумма сессии вопроса.
        self.assertAlmostEqual(result.cost_usd, 0.25, places=6)
        self.assertEqual(self.costs[FORK], "1.250000")

    async def test_point_follows_last_result(self) -> None:
        events = self._events() + [
            {"type": "assistant", "uuid": "a3", "session_id": FORK,
             "message": {"id": "m3", "usage": _usage(52_000, 10), "content": [{"type": "text"}]}},
            {"type": "result", "result": "второй ответ", "session_id": FORK, "total_cost_usd": 1.3,
             "usage": _usage(52_000, 10)},
        ]
        result = await self._run([_Proc(events)], fork_at=(QSID, "u"))
        self.assertEqual(result.last_uuid, "a3")

    async def test_compacted_point_fails_without_fresh_retry(self) -> None:
        gone = _Proc([], returncode=1, stderr=b"No message found with message.uuid of: u")
        result = await self._run([gone], fork_at=(QSID, "u"), fresh_retry=False)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result.is_error)
        self.assertTrue(result.fork_failed)
        self.assertIsNone(self.store.get("1"))

    async def test_deal_resume_not_replaced_by_fresh(self) -> None:
        self.store.set("1", FORK)
        stale = _Proc([], returncode=1, stderr=b"No conversation found with session ID")
        result = await self._run([stale], fresh_retry=False)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result.is_error)
        self.assertFalse(result.fork_failed)


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


class DealDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name)
        self.state = state
        self.store = SessionStore(state / "sessions.json")
        self.deal_store = SessionStore(state / "deal_sessions.json")
        self.runs: list[dict] = []
        self.answer = "ok"
        self.fail_fork = False
        cfg = SimpleNamespace(
            codex_trigger_prefix="gpt:", codex_bin="", codex_fallback=False,
            session_idle_ttl_sec=0, claude_bin="claude", claude_cwd=Path("/"),
            hook_settings_path=Path("/x.json"), max_claude_runtime_sec=0,
            state_dir=state, allowed_user_ids={7}, allowed_chat_ids=set(),
            telegram_text_limit=4096,
        )
        self.bot_data = {
            "cfg": cfg, "sessions": self.store, "procs": {}, "active_sessions": {},
            "session_targets": {}, "chat_locks": {},
            "untrusted_sessions": SessionStore(state / "untrusted_sessions.json"),
            "foreign_sessions": SessionStore(state / "foreign_sessions.json"),
            "deal_sessions": self.deal_store,
        }
        self.sent: list[tuple[str, str]] = []
        self.card_ids = iter(range(100, 200))
        self.ctx = SimpleNamespace(
            application=SimpleNamespace(
                bot_data=self.bot_data, running=True,
                create_task=lambda coro, update=None, *, name=None: asyncio.create_task(coro),
            ),
            bot=SimpleNamespace(send_chat_action=AsyncMock(), edit_message_reply_markup=AsyncMock(), id=99),
        )

        async def fake_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            fork_at = kw.get("fork_at")
            on_start = kw.get("on_run_start")
            if fork_at and self.fail_fork:
                return ClaudeResult(text="⚠️ No message found", is_error=True, fork_failed=True)
            if on_start is not None:
                on_start()
            existing = None if fork_at else store.get(chat_key)
            sid = existing or (f"fork-{len(self.runs)}" if fork_at else f"sid-{len(self.runs)}")
            self.runs.append({"resumed": existing, "fork_at": fork_at, "prompt": prompt,
                              "deal": store is self.deal_store, "deal_id": kw.get("deal_id"),
                              "fresh_retry": kw.get("fresh_retry")})
            store.set(chat_key, sid)
            return ClaudeResult(text=self.answer, session_id=sid, last_uuid=f"u-{len(self.runs)}")

        async def fake_send_text_to(_cfg, _msg, text, head=""):
            self.sent.append((head, text))

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
            "send_text_to": fake_send_text_to,
            "send_files": AsyncMock(),
            "send_cost_footer": AsyncMock(),
        }.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _msg(self, **kw) -> SimpleNamespace:
        msg = SimpleNamespace(message_thread_id=None, is_topic_message=False, chat_id=1,
                              chat=SimpleNamespace(id=1), set_reaction=AsyncMock(), forward_origin=None)
        msg.reply_text = AsyncMock(side_effect=lambda text, **_kw: SimpleNamespace(
            message_id=next(self.card_ids), text=text, chat=SimpleNamespace(id=1), chat_id=1,
            message_thread_id=None, is_topic_message=False, set_reaction=AsyncMock(),
            reply_text=msg.reply_text))
        msg.__dict__.update(kw)
        return msg

    def _update(self, msg=None) -> SimpleNamespace:
        return SimpleNamespace(effective_chat=SimpleNamespace(id=1, type="private"), update_id=1,
                               effective_user=SimpleNamespace(id=7), message=msg or self._msg())

    async def _morning(self, answer: str = "План дня.<<ASK:Звонить маме?|да|нет>>") -> dict:
        self.answer = answer
        upd = self._update()
        token = botmod._DEAL_ASKS.set(True)
        try:
            await botmod._dispatch_to_claude(upd, self.ctx, "утро", send_voice_reply=False, unattended=True)
        finally:
            botmod._DEAL_ASKS.reset(token)
        self.answer = "ok"
        self.morning_msg = upd.message
        data = deals.load(deals.deals_dir(self.state))
        return list(data.values())[-1] if data else {}

    async def _tap(self, deal: dict, n: int = 1, card=None) -> SimpleNamespace:
        deal = deals.load(deals.deals_dir(self.state))[deal["id"]]
        card = card or SimpleNamespace(
            message_id=deal["card_msg"], chat=SimpleNamespace(id=1), chat_id=1,
            text=f"{deals.head(deal)}\n❓ {deal['question']}",
            message_thread_id=None, is_topic_message=False, set_reaction=AsyncMock(),
            reply_text=self._msg().reply_text)
        query = SimpleNamespace(data=f"deal:{deal['id']}:{n}", answer=AsyncMock(), message=card,
                                edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                                 effective_chat=SimpleNamespace(id=1, type="private"), update_id=2)
        with patch.object(botmod, "Update", lambda update_id, message: SimpleNamespace(
                update_id=update_id, message=message, effective_chat=SimpleNamespace(id=1))):
            await botmod.on_deal_pick(update, self.ctx)
        return query

    def _deal(self, deal_id: str) -> dict:
        return deals.load(deals.deals_dir(self.state))[deal_id]

    async def test_morning_question_becomes_deal_card(self) -> None:
        deal = await self._morning()
        self.assertEqual(deal["state"], "asked")
        self.assertEqual((deal["qsid"], deal["quuid"]), ("sid-0", "u-1"))
        self.assertEqual(self.bot_data.get("ask_options", {}), {})
        card_text = self.morning_msg.reply_text.await_args_list[-1].args[0]
        self.assertTrue(card_text.startswith(deals.head(deal)))
        self.assertIsNotNone(deal["card_msg"])

    async def test_without_flag_ordinary_card(self) -> None:
        self.answer = "План.<<ASK:Звонить маме?|да|нет>>"
        await botmod._dispatch_to_claude(self._update(), self.ctx, "утро", send_voice_reply=False,
                                         unattended=True)
        self.assertEqual(deals.load(deals.deals_dir(self.state)), {})
        self.assertEqual(len(self.bot_data["ask_options"]), 2)

    async def test_tap_forks_from_question_point_main_untouched(self) -> None:
        deal = await self._morning()
        epoch = botmod._session_epoch(self.ctx, "1")
        await botmod._dispatch_to_claude(self._update(), self.ctx, "ещё вопрос", send_voice_reply=False)
        self.answer = "Позвоню вечером."
        await self._tap(deal)
        run = self.runs[-1]
        self.assertEqual(run["fork_at"], ("sid-0", "u-1"))
        self.assertTrue(run["deal"])
        self.assertFalse(run["fresh_retry"])
        self.assertIn(_("[Ответ на вопрос «{question}»]: {answer}").format(question="Звонить маме?", answer="да"),
                      run["prompt"])
        self.assertIn(_(botmod.DEAL_FIRST_NOTE), run["prompt"])
        # Сессия темы, её эпоха и активные сессии не тронуты.
        self.assertEqual(self.store.get("1"), "sid-0")
        self.assertEqual(botmod._session_epoch(self.ctx, "1"), epoch)
        d = self._deal(deal["id"])
        self.assertEqual((d["state"], d["sid"], d["turns"]), ("idle", run["resumed"] or "fork-2", 1))
        head, text = self.sent[-1]
        self.assertEqual(head, deals.head(d))
        self.assertEqual(text, "Позвоню вечером.")
        self.assertEqual(self.bot_data["foreign_running"], {})

    async def test_double_tap_runs_once(self) -> None:
        deal = await self._morning()
        await self._tap(deal, 1)
        before = len(self.runs)
        query = await self._tap(deal, 2)
        self.assertEqual(len(self.runs), before)
        self.assertEqual(_("На этот вопрос уже ответили или дело ещё отвечает."), query.answer.await_args.args[0])

    async def test_tap_under_other_message_rejected(self) -> None:
        deal = await self._morning()
        other = SimpleNamespace(message_id=9999, chat=SimpleNamespace(id=1), chat_id=1, text="x",
                                message_thread_id=None, is_topic_message=False)
        before = len(self.runs)
        query = await self._tap(deal, 1, card=other)
        self.assertEqual(len(self.runs), before)
        self.assertEqual(_("Ответ не принят: кнопка не от этого вопроса."), query.answer.await_args.args[0])
        self.assertEqual(self._deal(deal["id"])["state"], "asked")

    async def test_swipe_continues_deal_session_and_notes_reach_main(self) -> None:
        deal = await self._morning()
        await self._tap(deal)
        d = self._deal(deal["id"])
        reply_to = SimpleNamespace(from_user=SimpleNamespace(is_bot=True, id=99),
                                   text=deals.head(d) + "\nПозвоню вечером.", caption=None)
        msg = self._msg(text="а если не возьмёт трубку?", reply_to_message=reply_to)
        self.assertEqual(botmod._deal_reply_target(self.ctx, msg), d["id"])
        with patch.object(botmod, "_is_allowed", lambda *a: True), \
                patch.object(botmod, "_handoff_reply", AsyncMock(return_value=False)), \
                patch.object(botmod, "_task_touch", AsyncMock(return_value=False)) as touch:
            await botmod.on_text(self._update(msg), self.ctx)
        touch.assert_not_awaited()
        run = self.runs[-1]
        self.assertEqual(run["resumed"], d["sid"])
        self.assertIsNone(run["fork_at"])
        self.assertEqual(run["prompt"], "а если не возьмёт трубку?")
        # Следующий человеческий ход главной сессии получает итоги дела один раз.
        await botmod._dispatch_to_claude(self._update(), self.ctx, "что ещё на сегодня?", send_voice_reply=False)
        self.assertRegex(self.runs[-1]["prompt"], msg_re("[Тем временем в делах утреннего плана:\n{lines}\n]\n\n"))
        # Ограждение закрыто: запрос Ивана после него не код (второй круг ревью 26.10).
        self.assertIn("\n```\n]\n\nчто ещё на сегодня?", self.runs[-1]["prompt"])
        self.assertIn("Звонить маме", self.runs[-1]["prompt"])
        self.assertFalse(self.runs[-1]["deal"])
        await botmod._dispatch_to_claude(self._update(), self.ctx, "и всё", send_voice_reply=False)
        self.assertNotRegex(self.runs[-1]["prompt"], msg_re("[Тем временем в делах утреннего плана:\n{lines}\n]\n\n"))

    async def test_notes_not_given_to_background_run(self) -> None:
        deal = await self._morning()
        await self._tap(deal)
        await botmod._dispatch_to_claude(self._update(), self.ctx, "вечер", send_voice_reply=False,
                                         unattended=True)
        self.assertNotRegex(self.runs[-1]["prompt"], msg_re("[Тем временем в делах утреннего плана:\n{lines}\n]\n\n"))
        self.assertTrue(deals.pending_notes(deals.load(deals.deals_dir(self.state)), 1, None))

    async def test_busy_deal_refuses_swipe(self) -> None:
        deal = await self._morning()
        with deals.locked(deals.deals_dir(self.state)) as data:
            data[deal["id"]].update(state="running")
        reply_to = SimpleNamespace(from_user=SimpleNamespace(is_bot=True, id=99),
                                   text=deals.head(deal) + "\n...", caption=None)
        msg = self._msg(text="ну что?", reply_to_message=reply_to)
        before = len(self.runs)
        self.assertTrue(await botmod._deal_swipe(self._update(msg), self.ctx, deal["id"], "ну что?"))
        self.assertEqual(len(self.runs), before)
        self.assertRegex(msg.reply_text.await_args.args[0],
                         msg_re("🧵 дело «{label}» ещё отвечает: дождись ответа и ответь на него"))

    async def test_closed_deal_swipe_goes_main(self) -> None:
        deal = await self._morning()
        with deals.locked(deals.deals_dir(self.state)) as data:
            data[deal["id"]].update(state="closed")
        msg = self._msg(text="ну что?")
        self.assertFalse(await botmod._deal_swipe(self._update(msg), self.ctx, deal["id"], "ну что?"))

    async def test_new_question_inside_deal_stays_in_deal(self) -> None:
        deal = await self._morning()
        self.answer = "Когда?<<ASK:Во сколько?|в 18|в 20>>"
        await self._tap(deal)
        data = deals.load(deals.deals_dir(self.state))
        self.assertEqual(len(data), 1)
        d = data[deal["id"]]
        self.assertEqual((d["state"], d["question"]), ("asked", "Во сколько?"))
        self.answer = "ок"
        await self._tap(d, 2)
        self.assertEqual(self.runs[-1]["resumed"], d["sid"])
        self.assertIn(_("[Ответ на вопрос «{question}»]: {answer}").format(question="Во сколько?", answer="в 20"),
                      self.runs[-1]["prompt"])

    async def test_same_question_again_no_second_deal(self) -> None:
        await self._morning()
        await self._morning()
        self.assertEqual(len(deals.load(deals.deals_dir(self.state))), 1)
        self.assertEqual(self.bot_data.get("ask_options", {}), {})

    async def test_cap_third_question_ordinary_card(self) -> None:
        await self._morning()
        await self._morning("<<ASK:Второй?|да|нет>>")
        await self._morning("<<ASK:Третий?|да|нет>>")
        self.assertEqual(len(deals.load(deals.deals_dir(self.state))), 2)
        self.assertEqual(len(self.bot_data["ask_options"]), 2)

    async def test_fork_failed_without_question_is_named(self) -> None:
        deal = await self._morning()
        with deals.locked(deals.deals_dir(self.state)) as data:
            data[deal["id"]].update(state="failed")
        self.fail_fork = True
        msg = self._msg(text="ещё")
        before = len(self.runs)
        await botmod._deal_swipe(self._update(msg), self.ctx, deal["id"], "ещё")
        self.assertEqual(len(self.runs), before)
        self.assertIn(_(
            "⚠️ дело закрыто: его разговор не открылся (сессию сжали или её нет), ответ «{answer}» "
            "не выполнен; напиши его в главный разговор").format(answer="ещё"), msg.reply_text.await_args.args[0])
        self.assertEqual(self._deal(deal["id"])["state"], "closed")

    async def test_fork_failed_answers_main_session(self) -> None:
        deal = await self._morning()
        self.fail_fork = True
        await self._tap(deal)
        run = self.runs[-1]
        self.assertFalse(run["deal"])
        self.assertEqual(run["resumed"], "sid-0")
        self.assertIn(_("[Ответ на вопрос «{question}»]: {answer}").format(question="Звонить маме?", answer="да"),
                      run["prompt"])
        self.assertEqual(self._deal(deal["id"])["state"], "closed")

    async def test_refused_before_start_is_visible(self) -> None:
        deal = await self._morning()
        with patch.object(botmod, "_limit_hold", AsyncMock(return_value=True)) as hold:
            await self._tap(deal)
        self.assertFalse(hold.await_args.kwargs["movable"], "ход дела не переносится в /at")
        d = self._deal(deal["id"])
        self.assertEqual((d["state"], d["error"]), ("failed", "not started"))

    async def test_swipe_turn_not_moved_to_at_by_limit(self) -> None:
        deal = await self._morning()
        await self._tap(deal)
        with patch.object(botmod, "_limit_hold", AsyncMock(return_value=True)) as hold:
            await botmod._deal_swipe(self._update(self._msg(text="дальше")), self.ctx, deal["id"], "дальше")
        self.assertFalse(hold.await_args.kwargs["movable"], "свайп в дело не переносится в /at")
        self.assertEqual(self._deal(deal["id"])["state"], "failed")

    async def test_deal_turn_does_not_idle_reset_main_session(self) -> None:
        deal = await self._morning()
        epoch = botmod._session_epoch(self.ctx, "1")
        self.bot_data["cfg"].session_idle_ttl_sec = 60
        self.store.data["1"]["ts"] = time.time() - 3600
        await self._tap(deal)
        self.assertEqual(self.store.get("1"), "sid-0")
        self.assertEqual(botmod._session_epoch(self.ctx, "1"), epoch)

    async def test_swipe_on_answered_card_goes_to_deal(self) -> None:
        deal = await self._morning()
        query = await self._tap(deal)
        edited = query.edit_message_text.await_args.args[0]
        reply_to = SimpleNamespace(from_user=SimpleNamespace(is_bot=True, id=99), text=edited, caption=None)
        msg = self._msg(text="и ещё", reply_to_message=reply_to)
        self.assertEqual(botmod._deal_reply_target(self.ctx, msg), deal["id"])

    async def test_closed_deal_not_a_swipe_target(self) -> None:
        deal = await self._morning()
        with deals.locked(deals.deals_dir(self.state)) as data:
            data[deal["id"]].update(state="closed")
        reply_to = SimpleNamespace(from_user=SimpleNamespace(is_bot=True, id=99),
                                   text=deals.head(deal) + "\n...", caption=None)
        self.assertIsNone(botmod._deal_reply_target(self.ctx, self._msg(text="x", reply_to_message=reply_to)))
        forwarded = self._msg(text="x", reply_to_message=reply_to, forward_origin=object())
        self.assertIsNone(botmod._deal_reply_target(self.ctx, forwarded))

    async def test_voice_swipe_goes_to_deal_without_buffers(self) -> None:
        deal = await self._morning()
        await self._tap(deal)
        reply_to = SimpleNamespace(from_user=SimpleNamespace(is_bot=True, id=99),
                                   text=deals.head(deal) + "\n...", caption=None)
        msg = self._msg(reply_to_message=reply_to, voice=SimpleNamespace(duration=3, file_size=10))
        self.bot_data["openai"] = object()
        self.bot_data["cfg"].voice_max_duration_sec = 600
        with patch.object(botmod, "_is_allowed", lambda *a: True), \
                patch.object(botmod, "_download_retrying", AsyncMock()), \
                patch.object(botmod, "transcribe", AsyncMock(return_value="перенеси на завтра")), \
                patch.object(botmod, "_compose_reserve") as compose, \
                patch.object(botmod, "_gather_reserve") as gather, \
                patch.object(botmod, "_task_touch", AsyncMock(return_value=False)) as touch:
            await botmod.on_voice(self._update(msg), self.ctx)
        compose.assert_not_called()
        gather.assert_not_called()
        touch.assert_not_awaited()
        self.assertEqual(self.runs[-1]["prompt"], "перенеси на завтра")
        self.assertTrue(self.runs[-1]["deal"])

    async def test_bang_in_swipe_is_plain_deal_turn(self) -> None:
        deal = await self._morning()
        await self._tap(deal)
        await botmod._deal_swipe(self._update(self._msg(text="!стоп")), self.ctx, deal["id"], "!стоп")
        self.assertEqual(self.runs[-1]["prompt"], "стоп")
        self.assertTrue(self.runs[-1]["deal"])

    async def test_exception_mid_turn_gets_outcome(self) -> None:
        deal = await self._morning()

        async def boom(*_a, **kw):
            kw["on_run_start"]()
            raise RuntimeError("сбой")

        with patch.object(botmod, "run_claude", boom):
            await self._tap(deal)
        d = self._deal(deal["id"])
        self.assertEqual((d["state"], d["error"]), ("failed", "run aborted"))

    async def test_same_answer_twice_is_two_turns(self) -> None:
        deal = await self._morning()
        await self._tap(deal)
        await botmod._deal_swipe(self._update(self._msg(text="да")), self.ctx, deal["id"], "да")
        await botmod._deal_swipe(self._update(self._msg(text="да")), self.ctx, deal["id"], "да")
        d = self._deal(deal["id"])
        self.assertEqual((d["state"], d["turns"]), ("idle", 3))

    async def test_new_morning_closes_yesterdays_deals(self) -> None:
        a = await self._morning()
        await self._morning("<<ASK:Второй?|да|нет>>")
        with deals.locked(deals.deals_dir(self.state)) as data:
            for d in data.values():
                d["created_at"] -= 13 * 3600
        await self._morning("<<ASK:Сегодня?|да|нет>>")
        data = deals.load(deals.deals_dir(self.state))
        self.assertEqual(data[a["id"]]["state"], "closed")
        self.assertEqual(sum(deals.is_open(d) for d in data.values()), 1)

    async def test_draining_keeps_accepted_for_restart(self) -> None:
        deal = await self._morning()
        self.bot_data["draining"] = True
        await self._tap(deal)
        self.assertEqual(self._deal(deal["id"])["state"], "accepted")
        del self.bot_data["draining"]
        app = SimpleNamespace(bot_data=self.bot_data, bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(botmod, "_synthetic_update", lambda bot, c, t: self._update()), \
                patch.object(botmod.ContextTypes, "DEFAULT_TYPE", lambda application: self.ctx):
            await botmod._deals_resume(app)
            await asyncio.gather(*self.bot_data["deal_tasks"])
        self.assertEqual(self.runs[-1]["fork_at"], ("sid-0", "u-1"))
        self.assertEqual(self._deal(deal["id"])["state"], "idle")

    async def test_restart_mid_turn_named_not_repeated(self) -> None:
        deal = await self._morning()
        with deals.locked(deals.deals_dir(self.state)) as data:
            data[deal["id"]].update(state="running", answer="да")
        app = SimpleNamespace(bot_data=self.bot_data, bot=SimpleNamespace(send_message=AsyncMock()))
        before = len(self.runs)
        await botmod._deals_resume(app)
        self.assertEqual(len(self.runs), before)
        self.assertEqual(self._deal(deal["id"])["state"], "interrupted")
        self.assertIn(_(
            "⚠️ ответ «{answer}» прерван перезапуском бота, исход неизвестен; если нужно, ответь "
            "здесь свайпом").format(answer="да"), app.bot.send_message.await_args.args[1])

    async def test_interrupt_marker_does_not_leak_deal_sid(self) -> None:
        deal = await self._morning()
        seen = {}

        async def slow_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            seen["mark"] = self.bot_data["foreign_running"].get(chat_key)
            kw["on_run_start"]()
            store.set(chat_key, "fork-x")
            return ClaudeResult(text="ok", session_id="fork-x")

        with patch.object(botmod, "run_claude", slow_run):
            await self._tap(deal)
        self.assertEqual(seen["mark"], f"deal:{deal['id']}")
        self.assertEqual(self.store.get("1"), "sid-0")


class EventDealFlagTests(unittest.TestCase):
    def test_flag_survives_record(self) -> None:
        from claude_tg_bot import events
        rec = events.new_record(key=None, chat_id=1, thread_id=None, prompt="p", fresh=True, untrusted=False,
                                silent=True, source="s", title="t", ttl=None, deal_asks=True)
        self.assertIs(json.loads(json.dumps(rec))["deal_asks"], True)

    def test_event_ref_carries_flag_only_when_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ref = botmod._event_ref(Path(tmp), "abc", False, True)
            self.assertIs(ref["deal_asks"], True)
            ref = botmod._event_ref(Path(tmp), "abc", False)
            self.assertIsNone(ref["deal_asks"])


if __name__ == "__main__":
    unittest.main()

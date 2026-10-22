"""Лимит подписки Claude: снимок окон, резерв для разговора с Домовым,
отложенный повтор после сброса (limits.py, решения Ивана 20.10.2026)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from claude_tg_bot import bot as botmod
from claude_tg_bot import limits
from claude_tg_bot.claude_runner import ClaudeResult, SessionStore, run_claude

NOW = 1_792_500_000.0
# Событие из живого прогона `claude -p --output-format stream-json` 20.10.2026.
EVENT = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "resetsAt": 1792506600, "rateLimitType": "five_hour",
    "unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": 1792506600},
                       "seven_day": {"utilization": 0.57, "resetsAt": 1792684800}}}}
# Пояс бота в тестах (config.local_zone): UTC+4 без летнего времени, как у
# владельца, но не зависит от пояса машины, где идут тесты.
ZONE = "Asia/Dubai"
LIMIT_TEXT = f"You've hit your session limit · resets 2:40am ({ZONE})"
_zone_env = patch.dict("os.environ", {"CLAUDE_TG_BOT_TZ": ZONE})


def setUpModule() -> None:
    _zone_env.start()


def tearDownModule() -> None:
    _zone_env.stop()


def windows(five: float, week: float, five_reset: float = NOW + 3600, week_reset: float = NOW + 86400 * 3):
    return {"five_hour": {"pct": five, "resets_at": five_reset},
            "seven_day": {"pct": week, "resets_at": week_reset}}


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        limits.configure(Path(self.dir.name))

    def tearDown(self) -> None:
        limits._path = None
        self.dir.cleanup()

    def test_live_event_shape(self) -> None:
        seen = limits.parse_event(EVENT)
        self.assertAlmostEqual(seen["five_hour"]["pct"], 10.0)
        self.assertEqual(seen["five_hour"]["resets_at"], 1792506600.0)
        self.assertAlmostEqual(seen["seven_day"]["pct"], 57.0)
        self.assertIsNone(limits.parse_event({"type": "assistant"}))

    def test_older_observation_does_not_overwrite_newer(self) -> None:
        limits.record(windows(96, 50), observed_at=NOW)
        limits.record(windows(82, 40), observed_at=NOW - 600)
        self.assertEqual(limits.read()["five_hour"]["pct"], 96)
        limits.record(windows(98, 51), observed_at=NOW + 1)
        self.assertEqual(limits.read()["five_hour"]["pct"], 98)

    def test_corrupt_snapshot_is_ignored_not_raised(self) -> None:
        limits._path.write_text(json.dumps({
            "five_hour": {"pct": 98, "resets_at": NOW + 600, "observed_at": "x"},
            "seven_day": {"pct": "bad", "resets_at": "bad"}}))
        self.assertEqual(limits.read()["five_hour"]["pct"], 98)
        self.assertNotIn("seven_day", limits.read())
        limits.record({"five_hour": {"pct": 99, "resets_at": NOW + 600}}, observed_at=NOW)
        limits._path.write_text(json.dumps({"five_hour": {"pct": 98, "resets_at": float("inf")}}))
        self.assertIsNone(limits.reserve(None, NOW))
        self.assertEqual(limits.clock(float("inf")), "?")
        limits._path.write_text("{broken")
        self.assertEqual(limits.read(), {})

    def test_threshold_uses_exact_value(self) -> None:
        evt = json.loads(json.dumps(EVENT))
        evt["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = 0.9696
        seen = limits.parse_event(evt)
        self.assertIsNone(limits.reserve(seen, NOW))

    def test_no_reserve_within_fifteen_minutes_of_the_reset(self) -> None:
        self.assertIsNone(limits.reserve(windows(99.9, 10, five_reset=NOW + 14 * 60), NOW))
        self.assertIsNotNone(limits.reserve(windows(99.9, 10, five_reset=NOW + 16 * 60), NOW))

    def test_reserve_thresholds_per_window(self) -> None:
        self.assertIsNone(limits.reserve(windows(96.9, 50), NOW))
        self.assertEqual(limits.reserve(windows(97, 50), NOW), NOW + 3600)
        self.assertIsNone(limits.reserve(windows(10, 98.9), NOW))
        # Оба окна за порогом: ждать оба, сброс пятичасового недельное не снимает.
        self.assertEqual(limits.reserve(windows(99, 99), NOW), NOW + 86400 * 3)
        # Прошедший сброс не держит резерв.
        self.assertIsNone(limits.reserve(windows(100, 10, five_reset=NOW - 1), NOW))

    def test_limit_error_and_reset_from_text(self) -> None:
        self.assertTrue(limits.is_limit_error(LIMIT_TEXT))
        self.assertTrue(limits.is_limit_error("API Error: You're out of usage credits"))
        self.assertFalse(limits.is_limit_error("Rate limit exceeded on tool output"))
        now = datetime(2026, 10, 20, 22, 0, tzinfo=timezone(timedelta(hours=4))).timestamp()
        reset = limits.reset_from_text(LIMIT_TEXT, now)
        self.assertEqual(datetime.fromtimestamp(reset, timezone(timedelta(hours=4))).strftime("%d %H:%M"), "21 02:40")
        self.assertIsNone(limits.reset_from_text("You've hit your weekly limit · resets Oct 24", now))
        self.assertIsNone(limits.reset_from_text("You've hit your session limit · resets 2am (Europe/Moscow)", now))

    def test_reset_zone_names(self) -> None:
        """Ревью Codex 23.10.2026: пояс без косой черты и синоним пояса."""
        now = datetime(2026, 10, 20, 22, 0, tzinfo=timezone(timedelta(hours=4))).timestamp()
        for zone in ("UTC", "PST", "Mars/Olympus", "Unknown Zone"):
            with self.subTest(zone=zone):
                self.assertIsNone(limits.reset_from_text(f"You've hit your session limit · resets 2:40am ({zone})", now))
        with patch.dict("os.environ", {"CLAUDE_TG_BOT_TZ": "Europe/Kyiv"}):
            reset = limits.reset_from_text("You've hit your session limit · resets 2:40am (Europe/Kiev)", now)
        self.assertIsNotNone(reset)

    def test_reset_in_repeated_hour(self) -> None:
        """Осенний перевод стрелок: вторые 02:30 через 45 минут, а не сутки спустя."""
        from zoneinfo import ZoneInfo
        berlin = ZoneInfo("Europe/Berlin")
        first = datetime(2026, 10, 25, 2, 45, tzinfo=berlin)  # fold=0, ещё летнее время
        with patch.dict("os.environ", {"CLAUDE_TG_BOT_TZ": "Europe/Berlin"}):
            reset = limits.reset_from_text("You've hit your session limit · resets 2:30am (Europe/Berlin)",
                                           first.timestamp())
        self.assertEqual(reset - first.timestamp(), 45 * 60)

    def test_reset_in_skipped_hour(self) -> None:
        """Весенний перевод: 02:30 в этот день нет, ближайшее настоящее 02:30 завтра."""
        from zoneinfo import ZoneInfo
        berlin = ZoneInfo("Europe/Berlin")
        text = "You've hit your session limit · resets 2:30am (Europe/Berlin)"
        with patch.dict("os.environ", {"CLAUDE_TG_BOT_TZ": "Europe/Berlin"}):
            for start in (datetime(2026, 3, 29, 1, 0, tzinfo=berlin), datetime(2026, 3, 29, 3, 0, tzinfo=berlin)):
                with self.subTest(start=start):
                    reset = datetime.fromtimestamp(limits.reset_from_text(text, start.timestamp()), berlin)
                    self.assertEqual(reset.strftime("%d %H:%M"), "30 02:30")


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        limits.configure(Path(self.dir.name))

    def tearDown(self) -> None:
        limits._path = None
        self.dir.cleanup()

    async def run_stream(self, events, rc):
        proc = MagicMock(returncode=rc, pid=123456)
        proc.stdin.drain = AsyncMock()
        proc.wait = AsyncMock(return_value=rc)
        proc.stdout = asyncio.StreamReader()
        for evt in events:
            proc.stdout.feed_data((json.dumps(evt) + "\n").encode())
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()
        proc.stderr.feed_eof()
        store = MagicMock()
        store.get.return_value = None
        with (
            patch("claude_tg_bot.claude_runner._ensure_hook_still_valid"),
            patch("claude_tg_bot.claude_runner.ensure_oauth_fresh", new=AsyncMock()),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            return await run_claude("claude", "1", 1, None, "hello", store,
                                    cwd=Path("/tmp"), hook_settings=Path("/tmp/test-settings"))

    async def test_success_records_snapshot_without_limit_flag(self) -> None:
        result = await self.run_stream([EVENT, {"type": "result", "result": "ок", "session_id": "s"}], 0)
        self.assertFalse(result.limit_hit)
        self.assertAlmostEqual(limits.read()["seven_day"]["pct"], 57.0)

    async def test_exhausted_subscription_is_a_limit_hit_with_reset(self) -> None:
        rejected = json.loads(json.dumps(EVENT))
        rejected["rate_limit_info"]["status"] = "rejected"
        rejected["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = 1.0
        # Время до сброса из события: после него сброс берётся из текста ошибки.
        with patch.object(limits, "time", SimpleNamespace(time=lambda: NOW)):
            result = await self.run_stream(
                [rejected, {"type": "result", "result": LIMIT_TEXT, "is_error": True}], 1)
        self.assertTrue(result.is_error)
        self.assertTrue(result.limit_hit)
        self.assertEqual(result.limit_reset, 1792506600.0)
        self.assertTrue(botmod._is_claude_unavailable(result))

    async def test_text_alone_is_enough(self) -> None:
        result = await self.run_stream([{"type": "result", "result": LIMIT_TEXT, "is_error": True}], 1)
        self.assertTrue(result.limit_hit)
        self.assertIsNotNone(result.limit_reset)


class _Ctx:
    def __init__(self, state_dir: Path) -> None:
        self.at = SessionStore(state_dir / "at_jobs.json")
        self.jobs: list = []
        queue = SimpleNamespace(run_once=lambda cb, when, data, name: self.jobs.append((when, data)))
        self.application = SimpleNamespace(bot_data={"at_jobs": self.at}, job_queue=queue)


class _HoldBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        limits.configure(Path(self.dir.name))
        self.ctx = _Ctx(Path(self.dir.name))
        self.cfg = SimpleNamespace(limit_reserve_chats=frozenset({"100"}))
        self.msg = MagicMock()
        self.msg.reply_text = AsyncMock()

    def tearDown(self) -> None:
        limits._path = None
        self.dir.cleanup()


class HoldTests(_HoldBase):
    async def hold(self, chat_key="200", *, unattended=False, untrusted=False, movable=True):
        return await botmod._limit_hold(self.ctx, self.cfg, self.msg, chat_key, int(chat_key), None, "сделай",
                                        unattended=unattended, untrusted=untrusted, movable=movable,
                                        limit_retry=0)

    async def test_below_reserve_everything_runs(self) -> None:
        limits.record(windows(96, 98), observed_at=time.time())
        self.assertFalse(await self.hold())

    async def test_reserve_keeps_owner_conversation_and_defers_others(self) -> None:
        now = time.time()
        limits.record(windows(98, 40, five_reset=now + 1800), observed_at=now)
        self.assertFalse(await self.hold("100"))
        self.assertTrue(await self.hold("200"))
        self.assertEqual(len(self.ctx.jobs), 1)
        payload = json.loads(next(iter(self.ctx.at.data.values()))["v"])
        self.assertEqual(payload["prompt"], "сделай")
        self.assertAlmostEqual(payload["fire_at"], now + 1800 + limits.GRACE_SEC, delta=1)
        self.assertIn("отложен", self.msg.reply_text.call_args.args[0])

    async def test_domovoy_background_yields_too(self) -> None:
        now = time.time()
        limits.record(windows(98, 40, five_reset=now + 1800), observed_at=now)
        self.assertTrue(await self.hold("100", unattended=True))
        self.assertEqual(len(self.ctx.jobs), 1)

    async def test_untrusted_and_long_waits_are_skipped_with_one_notice(self) -> None:
        now = time.time()
        limits.record(windows(98, 40, five_reset=now + 4 * 3600), observed_at=now)
        self.assertTrue(await self.hold("100", untrusted=True, unattended=True))
        self.assertTrue(await self.hold("100", untrusted=True, unattended=True))
        self.assertTrue(await self.hold("100", unattended=True))
        self.assertEqual(self.ctx.jobs, [])
        self.assertEqual(self.msg.reply_text.await_count, 1)

    async def test_task_and_plan_runs_are_not_moved(self) -> None:
        now = time.time()
        limits.record(windows(99, 40, five_reset=now + 1800), observed_at=now)
        self.assertTrue(await self.hold(movable=False))
        self.assertEqual(self.ctx.jobs, [])
        self.assertIn("повтори после", self.msg.reply_text.call_args.args[0])

    def test_retry_after_error_counts_attempts(self) -> None:
        failed = ClaudeResult(text="⚠️ claude error", is_error=True, limit_hit=True, limit_reset=time.time() + 600)
        first = botmod._limit_after_error(self.ctx, failed, "200", 200, None, "сделай",
                                          unattended=False, untrusted=False, movable=True, limit_retry=0)
        self.assertIn("продолжу сам", first.text)
        payload = json.loads(next(iter(self.ctx.at.data.values()))["v"])
        self.assertEqual(payload["limit_retry"], 1)
        self.assertIn("проверь, что уже сделано", payload["prompt"])
        last = botmod._limit_after_error(self.ctx, failed, "200", 200, None, "сделай",
                                         unattended=False, untrusted=False, movable=True,
                                         limit_retry=limits.MAX_RETRIES)
        self.assertIn("повторы исчерпаны", last.text)
        self.assertEqual(len(self.ctx.jobs), 1)

    def test_codex_answer_leaves_requests_for_claude_after_reset(self) -> None:
        failed = ClaudeResult(text="err", is_error=True, limit_hit=True, limit_reset=time.time() + 600)
        answer = ClaudeResult(text=f"GPT: запишу после сброса\n{botmod.LIMIT_TODO_MARK}")
        out = botmod._limit_follow_up(self.ctx, answer, failed, "100", 100, None, "запиши в дневник",
                                      limit_retry=0)
        self.assertIn("Claude выполнит сам", out.text)
        self.assertNotIn(botmod.LIMIT_TODO_MARK, out.text)
        payload = json.loads(next(iter(self.ctx.at.data.values()))["v"])
        self.assertIn("Заново на сообщение не отвечай", payload["prompt"])
        self.assertIn("проверь журнал", payload["prompt"])
        self.assertIn("запиши в дневник", payload["prompt"])
        self.assertEqual(payload["limit_retry"], 1)

    def test_codex_answer_without_requests_ends_the_chain(self) -> None:
        failed = ClaudeResult(text="err", is_error=True, limit_hit=True, limit_reset=time.time() + 600)
        out = botmod._limit_follow_up(self.ctx, ClaudeResult(text="GPT: всё хорошо"), failed,
                                      "100", 100, None, "как дела", limit_retry=0)
        self.assertEqual(out.text, "GPT: всё хорошо")
        self.assertEqual(self.ctx.jobs, [])

    def test_follow_up_respects_retry_limit(self) -> None:
        failed = ClaudeResult(text="err", is_error=True, limit_hit=True, limit_reset=time.time() + 600)
        answer = ClaudeResult(text=f"GPT\n{botmod.LIMIT_TODO_MARK}")
        out = botmod._limit_follow_up(self.ctx, answer, failed, "100", 100, None, "запиши",
                                      limit_retry=limits.MAX_RETRIES)
        self.assertIn("повторы исчерпаны", out.text)
        self.assertEqual(self.ctx.jobs, [])

    async def test_hold_deferral_is_counted(self) -> None:
        now = time.time()
        limits.record(windows(98, 40, five_reset=now + 1800), observed_at=now)
        await botmod._limit_hold(self.ctx, self.cfg, self.msg, "200", 200, None, "сделай",
                                 unattended=True, untrusted=False, movable=True, limit_retry=2)
        payload = json.loads(next(iter(self.ctx.at.data.values()))["v"])
        self.assertEqual(payload["limit_retry"], 3)
        self.assertTrue(await botmod._limit_hold(self.ctx, self.cfg, self.msg, "200", 200, None, "сделай",
                                                 unattended=True, untrusted=False, movable=True,
                                                 limit_retry=3))
        self.assertEqual(len(self.ctx.jobs), 1)

    def test_past_reset_is_never_scheduled_in_the_past(self) -> None:
        failed = ClaudeResult(text="err", is_error=True, limit_hit=True, limit_reset=time.time() - 120)
        botmod._limit_after_error(self.ctx, failed, "200", 200, None, "сделай",
                                  unattended=False, untrusted=False, movable=True, limit_retry=0)
        payload = json.loads(next(iter(self.ctx.at.data.values()))["v"])
        self.assertGreater(payload["fire_at"], time.time() + 200)

    def test_talk_prompt_forbids_actions(self) -> None:
        text = botmod._limit_talk_prompt("запиши в дневник", time.time() + 600)
        self.assertIn("ничего не меняй", text)
        self.assertIn("запиши в дневник", text)


if __name__ == "__main__":
    unittest.main()


from tests.test_review_bot_main import GROUP, _Base  # noqa: E402


class DispatchTests(_Base):
    """Резерв и исчерпание в настоящем диспетчере (run_claude подменён)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        limits.configure(self.state)
        self.addCleanup(setattr, limits, "_path", None)
        self.jobs: list = []
        self.ctx.application.job_queue = SimpleNamespace(
            run_once=lambda cb, when, data, name: self.jobs.append(data))

    async def test_reserve_defers_other_chats_before_claude_starts(self) -> None:
        now = time.time()
        limits.record(windows(98, 40, five_reset=now + 1800), observed_at=now)
        self.cfg.limit_reserve_chats = frozenset({"1"})
        msg = await self.dispatch("сделай")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.jobs[0]["prompt"], "сделай")
        self.assertIn("отложен", msg.reply_text.await_args.args[0])

    async def test_reserve_chat_owner_message_still_runs(self) -> None:
        now = time.time()
        limits.record(windows(98, 40, five_reset=now + 1800), observed_at=now)
        self.cfg.limit_reserve_chats = frozenset({str(GROUP)})
        await self.dispatch("как дела")
        self.assertEqual(len(self.runs), 1)

    async def test_limit_hit_in_reserve_chat_talks_via_read_only_codex(self) -> None:
        self.cfg.limit_reserve_chats = frozenset({str(GROUP)})
        self.cfg.codex_bin = "codex"
        self.cfg.codex_fallback = True
        self.cfg.codex_fallback_sandbox = "workspace-write"
        reset = time.time() + 900

        async def failed(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            self.runs.append({"prompt": prompt})
            return ClaudeResult(text="⚠️ claude error", session_id="s", is_error=True,
                                limit_hit=True, limit_reset=reset)

        with patch.object(botmod, "run_claude", failed), \
                patch.object(botmod, "_codex_as_claude_result",
                             AsyncMock(return_value=ClaudeResult(
                                 text=f"GPT: запишу потом\n{botmod.LIMIT_TODO_MARK}"))) as codex:
            await self.dispatch("запиши в дневник")
        self.assertEqual(codex.await_args.kwargs["sandbox"], "read-only")
        self.assertIn("ничего не меняй", codex.await_args.args[1])
        self.assertIn("Заново на сообщение не отвечай", self.jobs[0]["prompt"])
        self.assertIn("Claude выполнит сам", self.send_reply_text.await_args.args[2])

    async def test_limit_hit_without_codex_is_retried_after_reset(self) -> None:
        reset = time.time() + 900

        async def failed(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            self.runs.append({"prompt": prompt})
            return ClaudeResult(text="⚠️ claude error", session_id="s", is_error=True,
                                limit_hit=True, limit_reset=reset)

        with patch.object(botmod, "run_claude", failed):
            await self.dispatch("сделай")
        self.assertEqual(self.jobs[0]["limit_retry"], 1)
        self.assertIn("продолжу сам", self.send_reply_text.await_args.args[2])

    async def test_plan_after_limit_is_not_moved_or_sent_to_codex(self) -> None:
        self.cfg.codex_bin = "codex"
        self.cfg.codex_fallback = True
        reset = time.time() + 900

        async def failed(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            return ClaudeResult(text="⚠️ claude error", session_id="s", is_error=True,
                                limit_hit=True, limit_reset=reset)

        with patch.object(botmod, "run_claude", failed), \
                patch.object(botmod, "_codex_as_claude_result", AsyncMock()) as codex:
            await self.dispatch("план", permission_mode="plan")
        codex.assert_not_awaited()
        self.assertEqual(self.jobs, [])
        self.assertIn("повтори после", self.send_reply_text.await_args.args[2])

    async def test_other_chat_limit_talks_read_only_never_with_write_sandbox(self) -> None:
        self.cfg.codex_bin = "codex"
        self.cfg.codex_fallback = True
        self.cfg.codex_fallback_sandbox = "workspace-write"
        reset = time.time() + 900

        async def failed(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            return ClaudeResult(text="⚠️ claude error", session_id="s", is_error=True,
                                limit_hit=True, limit_reset=reset)

        with patch.object(botmod, "run_claude", failed), \
                patch.object(botmod, "_codex_as_claude_result",
                             AsyncMock(return_value=ClaudeResult(text="GPT"))) as codex:
            await self.dispatch("сделай")
        self.assertEqual(codex.await_args.kwargs["sandbox"], "read-only")
        self.assertEqual(self.jobs, [])

    async def test_broken_reserve_check_does_not_block_the_message(self) -> None:
        with patch.object(botmod.limits, "reserve", side_effect=RuntimeError("x")):
            await self.dispatch("как дела")
        self.assertEqual(len(self.runs), 1)


class SecondRoundTests(_HoldBase):
    def test_huge_numbers_never_raise(self) -> None:
        evt = json.loads(json.dumps(EVENT))
        evt["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = 10 ** 400
        self.assertNotIn("five_hour", limits.parse_event(evt))
        limits._path.write_text('{"five_hour": {"pct": 1' + "0" * 400 + ', "resets_at": 1}}')
        self.assertEqual(limits.read(), {})
        limits.record({"five_hour": {"pct": 10 ** 400, "resets_at": 10 ** 400}})
        self.assertIsNone(limits.hit_reset({"five_hour": {"pct": "x"}}, "nonsense"))

    def test_marker_counts_only_as_the_last_line(self) -> None:
        failed = ClaudeResult(text="err", is_error=True, limit_hit=True, limit_reset=time.time() + 600)
        mention = ClaudeResult(text=f"Метка {botmod.LIMIT_TODO_MARK} означает поручения")
        self.assertEqual(botmod._limit_follow_up(self.ctx, mention, failed, "100", 100, None, "x",
                                                 limit_retry=0).text, mention.text)
        self.assertEqual(self.ctx.jobs, [])

    def test_deferred_first_run_is_still_wrapped_on_limit(self) -> None:
        failed = ClaudeResult(text="err", is_error=True, limit_hit=True, limit_reset=time.time() + 600)
        botmod._limit_after_error(self.ctx, failed, "200", 200, None, "сделай",
                                  unattended=True, untrusted=False, movable=True, limit_retry=1)
        first = json.loads(next(iter(self.ctx.at.data.values()))["v"])["prompt"]
        self.assertTrue(first.startswith(botmod.LIMIT_RETRY_HEAD))
        self.assertIn("проверь, что уже сделано", first)
        self.ctx.at.data.clear()
        botmod._limit_after_error(self.ctx, failed, "200", 200, None, first,
                                  unattended=True, untrusted=False, movable=True, limit_retry=2)
        again = json.loads(next(iter(self.ctx.at.data.values()))["v"])["prompt"]
        self.assertEqual(again, first)

    def test_failed_deferral_keeps_the_answer_and_says_so(self) -> None:
        failed = ClaudeResult(text="err", is_error=True, limit_hit=True, limit_reset=time.time() + 600)
        self.ctx.application.job_queue.run_once = MagicMock(side_effect=OSError("disk full"))
        out = botmod._limit_follow_up(self.ctx, ClaudeResult(text=f"GPT\n{botmod.LIMIT_TODO_MARK}"),
                                      failed, "100", 100, None, "запиши", limit_retry=0)
        self.assertIn("задание не поставлено", out.text)
        self.assertEqual(self.ctx.at.data, {})
        err = botmod._limit_after_error(self.ctx, failed, "200", 200, None, "сделай",
                                        unattended=False, untrusted=False, movable=True, limit_retry=0)
        self.assertIn("повтор не поставлен", err.text)

    def test_warm_yields_in_reserve_even_in_domovoy_chat(self) -> None:
        now = time.time()
        limits.record(windows(98, 40, five_reset=now + 1800), observed_at=now)
        cfg = SimpleNamespace(limit_reserve_chats=frozenset({"100"}), warm_idle_min=55)
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}))
        mark = {"gen": 1, "at": now - 3600}
        self.assertEqual(botmod._warm_skip_reason(ctx, cfg, "100", mark, 1), "subscription reserve")


class SpanTests(unittest.TestCase):
    def test_span_for_the_run_log(self) -> None:
        a = {"five_hour": {"pct": 41.23, "resets_at": NOW + 600}, "seven_day": {"pct": 58, "resets_at": NOW + 9000}}
        b = {"five_hour": {"pct": 43.0, "resets_at": NOW + 600}, "seven_day": {"pct": 58.3, "resets_at": NOW + 9000}}
        self.assertEqual(limits.span(a, b), "5h=41.2>43.0,7d=58.0>58.3")
        self.assertEqual(limits.span(None, b), "-")

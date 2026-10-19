"""Прогрев кэша разговора (warm.py, run_claude warm_fork_sid, таймер бота):
пинг в ответвление не меняет сессию темы и учёт цены, а цепочка пингов идёт,
только пока кэш читается (замер и ревью Codex 19.10.2026)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot import claude_runner, warm  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult, SessionStore  # noqa: E402

MAIN = "11111111-1111-4111-8111-111111111111"


class WarmRulesTests(unittest.TestCase):
    def test_hours(self) -> None:
        self.assertEqual(warm.parse_hours("09-22"), (9, 22))
        for bad in ("9", "22-09", "09-25", "a-b", "10-10"):
            with self.assertRaises(ValueError):
                warm.parse_hours(bad)
        self.assertTrue(warm.in_window(datetime(2026, 10, 19, 9, 0), (9, 22)))
        self.assertTrue(warm.in_window(datetime(2026, 10, 19, 21, 59), (9, 22)))
        self.assertFalse(warm.in_window(datetime(2026, 10, 19, 22, 0), (9, 22)))
        self.assertFalse(warm.in_window(datetime(2026, 10, 19, 8, 59), (9, 22)))

    def test_fork_sid_stable_per_chat(self) -> None:
        a = warm.fork_sid("183")
        self.assertEqual(a, warm.fork_sid("183"))
        self.assertNotEqual(a, warm.fork_sid("-100:5"))
        uuid.UUID(a)

    def test_compaction_guard(self) -> None:
        self.assertEqual(warm.compact_window_tokens("300k"), 300_000)
        self.assertEqual(warm.compact_window_tokens("1m"), 1_000_000)
        self.assertIsNone(warm.compact_window_tokens("auto"))
        self.assertIsNone(warm.compact_window_tokens(""))
        self.assertTrue(warm.near_compaction(275_000, "300k"))
        self.assertFalse(warm.near_compaction(250_000, "300k"))
        self.assertFalse(warm.near_compaction(900_000, ""))

    def test_healthy(self) -> None:
        # Замер 19.10: прочитано 121 580 при ожидаемых 121 582, хвост 16 357.
        self.assertTrue(warm.healthy(121_580, 16_357, 121_582))
        # Потеря кэша: чтение 90k при ожидаемых 160k (ревью Codex 19.10).
        self.assertFalse(warm.healthy(90_000, 70_000, 160_000))
        # Холодный кэш: прочитано только общее начало запроса.
        self.assertFalse(warm.healthy(14_766, 189_676, 190_000))
        self.assertFalse(warm.healthy(121_580, 50_000, 121_582))
        self.assertFalse(warm.healthy(100, 10, 0))


class WarmConfigTests(unittest.TestCase):
    def _load(self, extra: dict[str, str]):
        from claude_tg_bot import config as cfgmod
        env = {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_IDS": "1",
            "CLAUDE_BIN": sys.executable, "PATH": os.environ.get("PATH", ""),
            # Пустые явно: load_config подхватывает .env репозитория.
            "CLAUDE_TG_WARM_CHATS": "", "CLAUDE_TG_WARM_HOURS": "", "CLAUDE_TG_WARM_IDLE_MIN": "",
            **extra,
        }
        with patch.dict(os.environ, env, clear=True):
            return cfgmod.load_config(skip_hook_validation=True)

    def test_defaults_off(self) -> None:
        cfg = self._load({})
        self.assertEqual((cfg.warm_chats, cfg.warm_hours, cfg.warm_idle_min), (frozenset(), (9, 22), 55))

    def test_values(self) -> None:
        cfg = self._load({"CLAUDE_TG_WARM_CHATS": "183, -100:5", "CLAUDE_TG_WARM_HOURS": "08-23",
                          "CLAUDE_TG_WARM_IDLE_MIN": "50"})
        self.assertEqual(cfg.warm_chats, frozenset({"183", "-100:5"}))
        self.assertEqual((cfg.warm_hours, cfg.warm_idle_min), ((8, 23), 50))

    def test_bad_values_refuse_start(self) -> None:
        for extra in ({"CLAUDE_TG_WARM_HOURS": "22-09"}, {"CLAUDE_TG_WARM_IDLE_MIN": "60"}):
            with self.assertRaises(RuntimeError):
                self._load(extra)


class _Stdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Stdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        if not self._lines:
            raise asyncio.IncompleteReadError(b"", None)
        return self._lines.pop(0)


class _Stderr:
    def __init__(self, text: bytes = b"") -> None:
        self._text = text

    async def read(self) -> bytes:
        return self._text


class _Proc:
    pid = 1

    def __init__(self, events: list[dict], returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdin = _Stdin()
        self.stdout = _Stdout([json.dumps(e).encode() + b"\n" for e in events])
        self.stderr = _Stderr(stderr)

    async def wait(self) -> int:
        return self.returncode


def _usage(read: int, write: int) -> dict:
    return {"input_tokens": 2, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write, "output_tokens": 4}


PING_EVENTS = [
    {"type": "system", "subtype": "init", "tools": ["Read"]},
    {"type": "assistant", "message": {"id": "m1", "usage": _usage(121_580, 16_357),
                                      "content": [{"type": "text", "text": "ок"}]}},
    {"type": "result", "result": "ок", "session_id": "fork", "total_cost_usd": 1.4626,
     "usage": _usage(121_580, 16_357)},
]


class RunClaudeWarmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = MagicMock()
        self.store.get.return_value = MAIN
        self.costs = {MAIN: "1.307400"}
        self.calls: list[tuple] = []

    async def _run(self, procs: list[_Proc], *, oauth_exp: float | None = None, **kw) -> ClaudeResult:
        queue = list(procs)

        async def fake_exec(*args, **kwargs):
            self.calls.append(args)
            return queue.pop(0)

        with patch.object(claude_runner, "_ensure_hook_still_valid", lambda *_: None), \
                patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()) as oauth, \
                patch.object(claude_runner, "_oauth_expires_at", lambda: oauth_exp), \
                patch.object(claude_runner.asyncio, "create_subprocess_exec", fake_exec):
            result = await claude_runner.run_claude(
                "claude", "183", 183, None, "пинг", self.store, cwd=Path("/tmp"),
                hook_settings=Path("/tmp/x.json"), cost_totals=self.costs,
                warm_fork_sid="22222222-2222-4222-8222-222222222222", **kw,
            )
        oauth.assert_not_awaited()
        return result

    async def test_fork_args_and_no_state_writes(self) -> None:
        result = await self._run([_Proc(PING_EVENTS)])
        args = self.calls[0]
        tail = list(args[args.index("--resume"):])
        self.assertEqual(tail, ["--resume", MAIN, "--fork-session", "--session-id",
                                "22222222-2222-4222-8222-222222222222",
                                "--no-session-persistence", "--max-turns", "1"])
        self.store.set.assert_not_called()
        self.store.remove.assert_not_called()
        self.assertEqual(self.costs, {MAIN: "1.307400"})
        self.assertEqual(result.session_id, MAIN)
        self.assertAlmostEqual(result.cost_usd, 1.4626 - 1.3074, places=6)
        self.assertEqual((result.requests, result.first_cache_read, result.first_cache_write),
                         (1, 121_580, 16_357))
        self.assertGreater(result.last_request_at, 0)
        self.assertFalse(result.is_error)

    async def test_untrusted_locks_keep_same_tools(self) -> None:
        with patch.object(claude_runner, "untrusted_settings_path", lambda p: Path("/tmp/u.json")):
            await self._run([_Proc(PING_EVENTS)], untrusted_sids=set())
        args = self.calls[0]
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--tools", args)

    async def test_stale_session_not_replaced_by_fresh(self) -> None:
        stale = _Proc([], returncode=1, stderr=b"No conversation found with session ID")
        result = await self._run([stale])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result.is_error)
        self.store.remove.assert_not_called()
        self.store.set.assert_not_called()

    async def test_no_session_no_process(self) -> None:
        self.store.get.return_value = None
        result = await self._run([])
        self.assertTrue(result.is_error)
        self.assertEqual(self.calls, [])

    async def test_expiring_login_skips_ping(self) -> None:
        result = await self._run([], oauth_exp=time.time() + 60)
        self.assertTrue(result.is_error)
        self.assertEqual(self.calls, [])


class RequestTrackTests(unittest.TestCase):
    def test_last_request_starts_at_tool_result(self) -> None:
        clock = iter([100.0, 105.0, 400.0])
        with patch.object(claude_runner.time, "time", lambda: next(clock)):
            track = claude_runner._RequestTrack(next_at=time.time())
            track.note({"type": "system", "subtype": "init"})  # 105
            track.note({"type": "assistant", "message": {"id": "a", "usage": _usage(10, 5)}})
            # Повтор того же запроса и подагент не в счёт.
            track.note({"type": "assistant", "message": {"id": "a", "usage": _usage(10, 5)}})
            track.note({"type": "assistant", "parent_tool_use_id": "t",
                        "message": {"id": "s", "usage": _usage(1, 1)}})
            track.note({"type": "user", "message": {"content": []}})  # 400
            track.note({"type": "assistant", "message": {"id": "b", "usage": _usage(15, 3)}})
        self.assertEqual((track.count, track.last_at), (2, 400.0))
        self.assertEqual((track.first_read, track.first_write), (10, 5))


class _Job:
    def __init__(self, name: str) -> None:
        self.name = name
        self.removed = False

    def schedule_removal(self) -> None:
        self.removed = True


class _JobQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[float, str, object]] = []
        self.named: list[_Job] = []

    def get_jobs_by_name(self, name: str) -> list[_Job]:
        return [j for j in self.named if j.name == name and not j.removed]

    def run_once(self, callback, *, when: float, name: str, data: object) -> None:
        self.jobs.append((when, name, data))
        self.named.append(_Job(name))


class BotWarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "sessions.json")
        self.store.set("183", MAIN)
        self.cfg = SimpleNamespace(
            warm_chats=frozenset({"183"}), warm_hours=(0, 24), warm_idle_min=55,
            claude_bin="claude", claude_cwd=Path("/"), hook_settings_path=Path("/x.json"),
            autocompact="300k", autocompact_chats=frozenset(),
        )
        self.jq = _JobQueue()
        self.bot_data = {"cfg": self.cfg, "sessions": self.store, "procs": {},
                         "session_targets": {}, "chat_locks": {}, "active_work": 0}
        self.ctx = SimpleNamespace(
            application=SimpleNamespace(bot_data=self.bot_data, job_queue=self.jq),
            job=SimpleNamespace(data=("183", 0)),
        )
        self.sig = ("opus", "high", "300k", "p", "h")
        p = patch.object(botmod, "_warm_signature", lambda *a: self.sig)
        p.start()
        self.addCleanup(p.stop)
        for name in ("_persona_for", "_memory_scope_hint", "_effort_for", "_model_for"):
            p = patch.object(botmod, name, lambda *a: "")
            p.start()
            self.addCleanup(p.stop)
        self.pings: list[dict] = []
        self.ping_result = ClaudeResult(text="ок", session_id=MAIN, requests=1,
                                        last_request_at=time.time(),
                                        first_cache_read=150_000, first_cache_write=16_000)

        async def fake_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            self.pings.append(kw)
            self.assertEqual(self.bot_data["active_work"], 1)
            return self.ping_result

        p = patch.object(botmod, "run_claude", fake_run)
        p.start()
        self.addCleanup(p.stop)

    def _after_run(self, **kw) -> None:
        """Ран, чей последний запрос начался 55 минут назад: пинг пора слать.
        Задание таймера срабатывает с данными, которые ему дал бот."""
        fields = {"text": "ok", "session_id": MAIN, "requests": 2,
                  "last_request_at": time.time() - 55 * 60, "context_tokens": 150_500, **kw}
        botmod._warm_after_run(self.ctx, self.cfg, "183", ClaudeResult(**fields), self.sig)
        if self.jq.jobs:
            self.ctx.job.data = self.jq.jobs[-1][2]

    def test_schedule_from_last_request_start(self) -> None:
        self._after_run(last_request_at=time.time() - 60)
        when, name, data = self.jq.jobs[-1]
        self.assertEqual((name, data[0]), ("warm:183", "183"))
        # 55 минут от начала последнего запроса, а не от конца рана.
        self.assertAlmostEqual(when, 54 * 60, delta=5)

    async def test_started_job_of_replaced_mark_does_not_ping(self) -> None:
        """Уже запущенное задание schedule_removal не снимает: новая отметка
        настоящего рана его не запускает (ревью Codex 19.10)."""
        self._after_run()
        old = self.ctx.job.data
        self._after_run(last_request_at=time.time() - 5)
        self.ctx.job.data = old
        await botmod._warm_tick(self.ctx)
        self.ctx.job.data = self.jq.jobs[-1][2]
        await botmod._warm_tick(self.ctx)  # новое задание до срока
        self.assertEqual(self.pings, [])

    def test_plan_run_drops_mark(self) -> None:
        self._after_run()
        botmod._warm_forget(self.ctx.application, "183")
        self.assertNotIn("183", self.bot_data["warm"])
        self.assertTrue(all(j.removed for j in self.jq.named))

    def test_run_without_claude_keeps_previous_mark(self) -> None:
        self._after_run()
        mark = dict(self.bot_data["warm"]["183"])
        self._after_run(last_request_at=0.0)  # отказ до API, Codex
        self._after_run(session_id="other")
        self.assertEqual(self.bot_data["warm"]["183"], mark)
        self.assertEqual(len(self.jq.jobs), 1)

    def test_other_chat_not_scheduled(self) -> None:
        botmod._warm_after_run(self.ctx, self.cfg, "999", ClaudeResult(
            text="ok", session_id=MAIN, last_request_at=time.time(), context_tokens=1), self.sig)
        self.assertEqual(self.jq.jobs, [])

    async def test_ping_reschedules_chain(self) -> None:
        self._after_run()
        await botmod._warm_tick(self.ctx)
        self.assertEqual(len(self.pings), 1)
        kw = self.pings[0]
        self.assertEqual(kw["warm_fork_sid"], warm.fork_sid("183"))
        self.assertIsNotNone(kw["untrusted_sids"])
        self.assertIsNone(kw.get("session_registry"))
        self.assertIsNone(kw.get("scope_store"))
        self.assertEqual(len(self.jq.jobs), 2)
        self.assertTrue(self.jq.named[0].removed)
        self.assertEqual(self.bot_data["active_work"], 0)
        self.assertEqual(self.store.get("183"), MAIN)

    async def test_cold_ping_stops_chain(self) -> None:
        self._after_run()
        self.ping_result = ClaudeResult(text="ок", session_id=MAIN, requests=1,
                                        last_request_at=time.time(),
                                        first_cache_read=14_000, first_cache_write=150_000)
        await botmod._warm_tick(self.ctx)
        self.assertEqual(len(self.jq.jobs), 1)

    async def test_tool_attempt_stops_chain(self) -> None:
        self._after_run()
        self.ping_result = ClaudeResult(text="", session_id=MAIN, requests=1, is_error=True,
                                        last_request_at=time.time(),
                                        first_cache_read=150_000, first_cache_write=16_000)
        await botmod._warm_tick(self.ctx)
        self.assertEqual(len(self.jq.jobs), 1)

    async def test_skips(self) -> None:
        cases = {
            "late": lambda: self._after_run(last_request_at=time.time() - 59 * 60),
            "session changed": lambda: (self._after_run(), self.store.set("183", "new")),
            "settings changed": lambda: (self._after_run(), setattr(self, "sig", ("sonnet",))),
            "near compaction": lambda: self._after_run(context_tokens=280_000),
            "restart pending": lambda: (self._after_run(), self.bot_data.update(draining=True)),
        }
        for label, setup in cases.items():
            with self.subTest(label):
                self.bot_data.pop("draining", None)
                self.store.set("183", MAIN)
                self.sig = ("opus", "high", "300k", "p", "h")
                self.pings.clear()
                setup()
                await botmod._warm_tick(self.ctx)
                self.assertEqual(self.pings, [])

    async def test_outside_window(self) -> None:
        self.cfg.warm_hours = (0, 1) if datetime.now(botmod.local_zone()).hour >= 1 else (1, 2)
        self._after_run()
        await botmod._warm_tick(self.ctx)
        self.assertEqual(self.pings, [])

    async def test_busy_lock_waits_for_run(self) -> None:
        self._after_run()
        lock = botmod._chat_lock(self.ctx, "183")
        async with lock:
            await botmod._warm_tick(self.ctx)
        self.assertEqual(self.pings, [])
        self.assertEqual(self.bot_data["active_work"], 0)



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


class DispatchWarmTests(unittest.IsolatedAsyncioTestCase):
    """Место запуска: обычный ран ставит отметку, ран plan снимает её до
    себя, даже если упадёт (ревью Codex 19.10)."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "sessions.json")
        cfg = SimpleNamespace(
            codex_trigger_prefix="gpt:", codex_bin="", codex_fallback=False,
            session_idle_ttl_sec=0, claude_bin="claude", claude_cwd=Path("/"),
            hook_settings_path=Path("/x.json"), max_claude_runtime_sec=0,
            warm_chats=frozenset({"1"}), warm_hours=(0, 24), warm_idle_min=55,
        )
        self.jq = _JobQueue()
        self.bot_data = {
            "cfg": cfg, "sessions": self.store, "procs": {}, "active_sessions": {},
            "session_targets": {}, "chat_locks": {},
            "untrusted_sessions": SessionStore(Path(self.tmp.name) / "u.json"),
        }
        self.ctx = SimpleNamespace(
            application=SimpleNamespace(
                bot_data=self.bot_data, running=True, job_queue=self.jq,
                create_task=lambda coro, update=None, *, name=None: asyncio.create_task(coro),
            ),
            bot=SimpleNamespace(send_chat_action=AsyncMock()),
        )
        self.run_fails = False

        async def fake_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            if self.run_fails:
                raise RuntimeError("сбой рана")
            store.set(chat_key, MAIN)
            return ClaudeResult(text="ok", session_id=MAIN, requests=1,
                                last_request_at=time.time(), context_tokens=120_000)

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
            "_send_plan_card": AsyncMock(),
        }.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def _run(self, **kw) -> None:
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1), update_id=1,
            message=SimpleNamespace(message_thread_id=None, is_topic_message=False,
                                    set_reaction=AsyncMock(), reply_text=AsyncMock()),
        )
        await botmod._dispatch_to_claude(update, self.ctx, "x", send_voice_reply=False, **kw)

    async def test_run_marks_and_failed_plan_run_drops(self) -> None:
        await self._run()
        self.assertEqual(self.bot_data["warm"]["1"]["sid"], MAIN)
        self.assertEqual([j[1] for j in self.jq.jobs], ["warm:1"])
        self.run_fails = True
        await self._run(permission_mode="plan")
        self.assertNotIn("1", self.bot_data["warm"])
        self.assertTrue(all(j.removed for j in self.jq.named))


if __name__ == "__main__":
    unittest.main()

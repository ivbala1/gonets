"""Перезапуск бота по заявке: заявка на диске, состояние git, цикл ожидания,
учёт работы в _dispatch_to_claude, /restart и 503 у /event (restart.py)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import approval, restart  # noqa: E402
from claude_tg_bot import bot as botmod  # noqa: E402


class RequestFileTests(unittest.TestCase):
    def test_roundtrip_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            self.assertIsNone(restart.load_request(state))
            req = restart.new_request("1:7", "правка", "abc")
            restart.save_request(state, req)
            self.assertEqual(restart.load_request(state), req)
            self.assertEqual(req["phase"], restart.PHASE_PENDING)
            self.assertEqual([p.name for p in state.iterdir()], [restart.REQUEST_NAME])
            restart.clear_request(state)
            restart.clear_request(state)
            self.assertIsNone(restart.load_request(state))

    def test_broken_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            restart.request_path(state).write_text("{не json")
            self.assertIsNone(restart.load_request(state))
            restart.request_path(state).write_text(json.dumps({"reason": "без id"}))
            self.assertIsNone(restart.load_request(state))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class CodeProblemTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.repo = Path(self._dir.name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("x = 1\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "init")
        self.head = _git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_clean_same_commit(self) -> None:
        self.assertEqual(restart.code_problem(self.head, self.repo), "")

    def test_dirty_and_untracked_code(self) -> None:
        (self.repo / "src" / "a.py").write_text("x = 2\n")
        self.assertIn("незакоммиченные", restart.code_problem(self.head, self.repo))
        _git(self.repo, "checkout", "-q", "--", "src/a.py")
        (self.repo / "src" / "new.py").write_text("")
        self.assertIn("src/new.py", restart.code_problem(self.head, self.repo))

    def test_files_outside_code_do_not_matter(self) -> None:
        (self.repo / "state").mkdir()
        (self.repo / "state" / "x.json").write_text("{}")
        self.assertEqual(restart.code_problem(self.head, self.repo), "")

    def test_commit_changed(self) -> None:
        (self.repo / "src" / "a.py").write_text("x = 3\n")
        _git(self.repo, "commit", "-qam", "next")
        self.assertIn("коммит сменился", restart.code_problem(self.head, self.repo))


class ValidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_failing_step_is_reported(self) -> None:
        problem = await restart.validate_code(Path("/"), python=shutil.which("false"), timeout=10)
        self.assertTrue(problem.startswith("импорт модулей: код 1"), problem)

    async def test_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            slow = Path(d) / "slow"
            slow.write_text("#!/bin/sh\nsleep 5\n")
            slow.chmod(0o755)
            problem = await restart.validate_code(Path(d), python=str(slow), timeout=0.2)
        self.assertIn("не уложилось", problem)


class DrainTests(unittest.IsolatedAsyncioTestCase):
    def _drain(self, active: list[int], *, problem: str = "", invalid: str = "",
               clock: list[float] | None = None) -> tuple[restart.Drain, list, list]:
        notes: list[tuple[str, bool]] = []
        events: list[str] = []

        async def notify(text: str, ask: bool) -> None:
            notes.append((text, ask))

        async def validate() -> str:
            events.append("validate")
            return invalid

        async def do_restart() -> None:
            events.append("restart")

        async def abort(text: str) -> None:
            events.append(f"abort:{text}")

        def code_problem(commit: str) -> str:
            events.append(f"code:{commit}")
            return problem

        t = clock if clock is not None else [0.0]
        drain = restart.Drain(
            req={"id": "r1", "reason": "правка", "commit": "c1"},
            active=lambda: active[0], notify=notify, validate=validate,
            code_problem=code_problem, restart=do_restart, abort=abort,
            poll_sec=0.01, ask_after_sec=60, clock=lambda: t[0],
        )
        return drain, notes, events

    async def test_idle_bot_restarts_after_checks(self) -> None:
        drain, notes, events = self._drain([0])
        await drain.run()
        self.assertEqual(events, ["code:c1", "validate", "code:c1", "restart"])
        self.assertIn("Активной работы нет", notes[0][0])

    async def test_code_rechecked_after_validation(self) -> None:
        checks = iter(["", "коммит сменился после заявки"])
        drain, _, events = self._drain([0])
        drain.code_problem = lambda commit: next(checks)
        await drain.run()
        self.assertNotIn("restart", events)
        self.assertIn("коммит сменился", events[-1])

    async def test_waits_for_active_work(self) -> None:
        active = [2]
        drain, notes, events = self._drain(active)
        task = asyncio.create_task(drain.run())
        await asyncio.sleep(0.05)
        self.assertEqual(events, [])
        self.assertIn("Жду окончания работы: 2", notes[0][0])
        active[0] = 0
        await asyncio.wait_for(task, 1)
        self.assertEqual(events[-1], "restart")

    async def test_asks_once_after_long_wait(self) -> None:
        active, clock = [1], [0.0]
        drain, notes, events = self._drain(active, clock=clock)
        task = asyncio.create_task(drain.run())
        await asyncio.sleep(0.03)
        clock[0] = 61
        await asyncio.sleep(0.05)
        asks = [n for n in notes if n[1]]
        self.assertEqual(len(asks), 1)
        self.assertIn("Перезапустить сейчас", asks[0][0])
        drain.now_event.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(events[-1], "restart")
        self.assertEqual(active[0], 1)

    async def test_code_problem_aborts(self) -> None:
        drain, _, events = self._drain([0], problem="коммит сменился")
        await drain.run()
        self.assertEqual(len(events), 2)
        self.assertTrue(events[1].startswith("abort:") and "коммит сменился" in events[1])

    async def test_failed_validation_aborts(self) -> None:
        drain, _, events = self._drain([0], invalid="тесты: код 1")
        await drain.run()
        self.assertEqual(events[:2], ["code:c1", "validate"])
        self.assertIn("тесты: код 1", events[2])
        self.assertNotIn("restart", events)

    async def test_cancel_while_waiting(self) -> None:
        active = [1]
        drain, _, events = self._drain(active)
        task = asyncio.create_task(drain.run())
        await asyncio.sleep(0.03)
        drain.cancelled = True
        await asyncio.wait_for(task, 1)
        self.assertEqual(events, [])


class DispatchCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_while_running_and_rejects_when_draining(self) -> None:
        app = SimpleNamespace(bot_data={})
        ctx = SimpleNamespace(application=app)
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=msg)
        seen: list[int] = []
        gate = asyncio.Event()

        async def impl(*a, **kw) -> None:
            seen.append(app.bot_data["active_work"])
            await gate.wait()

        with patch.object(botmod, "_dispatch_to_claude_impl", impl):
            task = asyncio.create_task(botmod._dispatch_to_claude(update, ctx, "x", send_voice_reply=False))
            await asyncio.sleep(0)
            self.assertEqual(app.bot_data["active_work"], 1)
            app.bot_data["draining"] = True
            await botmod._dispatch_to_claude(update, ctx, "y", send_voice_reply=False)
            self.assertEqual(seen, [1])
            self.assertIn("повтори", msg.reply_text.call_args.args[0])
            await botmod._dispatch_to_claude(update, ctx, "z", send_voice_reply=False,
                                             on_run_start=lambda: None, deferred=True)
            self.assertIn("отложенный промпт", msg.reply_text.call_args.args[0])
            # on_run_start есть и у выполнения задачи, а само оно после
            # рестарта не уйдёт: текст решает явный флаг /at (проект шага 3, 1.5)
            await botmod._dispatch_to_claude(update, ctx, "w", send_voice_reply=False,
                                             on_run_start=lambda: None)
            self.assertIn("повтори", msg.reply_text.call_args.args[0])
            gate.set()
            await task
        self.assertEqual(app.bot_data["active_work"], 0)

    async def test_failure_still_decrements(self) -> None:
        app = SimpleNamespace(bot_data={})
        ctx = SimpleNamespace(application=app)

        async def boom(*a, **kw) -> None:
            raise RuntimeError("x")

        with patch.object(botmod, "_dispatch_to_claude_impl", boom):
            with self.assertRaises(RuntimeError):
                await botmod._dispatch_to_claude(SimpleNamespace(message=None), ctx, "x")
        self.assertEqual(app.bot_data["active_work"], 0)


    async def test_admitted_work_is_not_rejected(self) -> None:
        app = SimpleNamespace(bot_data={"draining": True})
        ctx = SimpleNamespace(application=app)
        impl = AsyncMock(return_value=None)
        with patch.object(botmod, "_dispatch_to_claude_impl", impl):
            rejected = await botmod._dispatch_to_claude(
                SimpleNamespace(message=None), ctx, "x", send_voice_reply=False, admitted=True)
        self.assertFalse(rejected)
        self.assertTrue(impl.call_args.kwargs["admitted"])


class AtDuringDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_at_stays_on_disk_and_is_rescheduled_once(self) -> None:
        removed: list[str] = []
        store = SimpleNamespace(remove=removed.append, data={"j1": {"v": json.dumps({"fire_at": 0})}})
        bot = SimpleNamespace(send_message=AsyncMock())
        app = SimpleNamespace(bot_data={"at_jobs": store, "draining": True}, bot=bot)
        job = SimpleNamespace(data={"jid": "j1", "prompt": "p", "chat_id": 1, "thread_id": None})
        ctx = SimpleNamespace(application=app, bot=bot, job=job)
        with patch.object(botmod, "_synthetic_update",
                          return_value=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))):
            await botmod._at_fire(ctx)
        self.assertEqual(removed, [])
        self.assertEqual(app.bot_data["at_running"], set())

        scheduled: list[str] = []
        app.job_queue = SimpleNamespace(get_jobs_by_name=lambda name: [])
        app.bot_data["at_running"] = {"j2"}
        store.data["j2"] = {"v": json.dumps({"fire_at": 0})}
        with patch.object(botmod, "_schedule_at_job", lambda a, jid, payload: scheduled.append(jid)):
            botmod._restart_reschedule_at(app)
        self.assertEqual(scheduled, ["j1"])


    async def test_rejected_at_requeued_when_drain_cancelled_meanwhile(self) -> None:
        store = SimpleNamespace(remove=lambda jid: None, data={"j1": {"v": "{}"}})
        bot = SimpleNamespace(send_message=AsyncMock())
        app = SimpleNamespace(bot_data={"at_jobs": store}, bot=bot)
        job = SimpleNamespace(data={"jid": "j1", "prompt": "p", "chat_id": 1, "thread_id": None,
                                    "fire_at": 0})
        ctx = SimpleNamespace(application=app, bot=bot, job=job)
        seen_running: list[set] = []
        seen_kw: list[dict] = []

        async def rejected_dispatch(*a, **kw):
            seen_running.append(set(app.bot_data["at_running"]))
            seen_kw.append(kw)
            return True  # отказ из-за ожидания, а заявку тем временем отменили

        scheduled: list[tuple] = []
        with patch.object(botmod, "_synthetic_update", return_value=SimpleNamespace()), \
                patch.object(botmod, "_dispatch_to_claude", rejected_dispatch), \
                patch.object(botmod, "_schedule_at_job", lambda a, jid, p: scheduled.append((jid, p))):
            await botmod._at_fire(ctx)
        self.assertEqual(seen_running, [{"j1"}])
        self.assertTrue(seen_kw[0]["deferred"])  # текст «уйдёт после рестарта» только у /at
        self.assertEqual(scheduled, [("j1", {"prompt": "p", "chat_id": 1, "thread_id": None, "fire_at": 0})])
        self.assertEqual(app.bot_data["at_running"], set())


class FreshEnvTests(unittest.TestCase):
    def test_snapshot_before_dotenv_is_used(self) -> None:
        from claude_tg_bot import config
        with patch.object(config, "ENV_BEFORE_DOTENV", {"PATH": "/x", "KEEP": "1"}):
            self.assertEqual(restart.fresh_env(), {"PATH": "/x", "KEEP": "1"})
        with patch.object(config, "ENV_BEFORE_DOTENV", None), patch.dict(os.environ, {"ZZ": "1"}):
            self.assertEqual(restart.fresh_env()["ZZ"], "1")


class BeginLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_make_one_drain(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            app = SimpleNamespace(bot_data={"cfg": SimpleNamespace(state_dir=Path(d))})
            gate = asyncio.Event()

            def slow_problem(commit: str) -> str:
                return ""

            async def fake_to_thread(fn, *a):
                await gate.wait()
                return fn(*a)

            created: list = []
            with patch.object(botmod.asyncio, "to_thread", fake_to_thread), \
                    patch.object(botmod.restart, "code_problem", slow_problem), \
                    patch.object(botmod.asyncio, "create_task", lambda coro: created.append(coro) or coro.close()):
                t1 = asyncio.ensure_future(botmod._restart_begin(app, "1", "a", "c"))
                t2 = asyncio.ensure_future(botmod._restart_begin(app, "1", "b", "c"))
                await asyncio.sleep(0.01)
                gate.set()
                r1, r2 = await asyncio.gather(t1, t2)
            self.assertEqual(len(created), 1)
            self.assertEqual(r1[1]["id"], r2[1]["id"])
            self.assertTrue(r2[1].get("duplicate"))


class _Req:
    def __init__(self, app, cfg, body, token: str = "s") -> None:
        self.app = {"application": app, "cfg": cfg}
        self.headers = {"X-Approve-Token": token}
        self._body = body
        self.remote = "127.0.0.1"

    async def json(self):
        return self._body


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(approve_shared_secret="s", primary_chat_id=1, state_dir=Path(tempfile.mkdtemp()),
                           allowed_chat_ids={-100}, allowed_user_ids={1})


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_auth_chat_and_begin(self) -> None:
        begin = AsyncMock(return_value=(202, {"ok": True, "id": "r1"}))
        app = SimpleNamespace(bot_data={"restart_begin": begin})
        cfg = _cfg()
        r = await approval.handle_restart(_Req(app, cfg, {"reason": "x"}, token="bad"))
        self.assertEqual(r.status, 403)
        r = await approval.handle_restart(_Req(app, cfg, {"reason": "x", "chat_key": "999:5"}))
        self.assertEqual(r.status, 403)
        r = await approval.handle_restart(_Req(app, cfg, {"reason": "x", "chat_key": "-100:5", "commit": "c"}))
        self.assertEqual(r.status, 202)
        begin.assert_awaited_with("-100:5", "x", "c")
        await approval.handle_restart(_Req(app, cfg, {"reason": "y"}))
        begin.assert_awaited_with("1", "y", "")

    async def test_accepted_event_is_counted_until_wake_ends(self) -> None:
        gate = asyncio.Event()

        async def wake(*a, **kw) -> None:
            await gate.wait()

        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
        app = SimpleNamespace(bot_data={"event_wake": wake}, bot=bot)
        r = await approval.handle_event(_Req(app, _cfg(), {"source": "t", "text": "x", "wake": True}))
        self.assertEqual(r.status, 200)
        self.assertEqual(app.bot_data["admitted_events"], 1)
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(app.bot_data["admitted_events"], 0)

    async def test_event_without_wake_releases_count(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("tg")))
        app = SimpleNamespace(bot_data={}, bot=bot)
        r = await approval.handle_event(_Req(app, _cfg(), {"source": "t", "text": "x"}))
        self.assertEqual(r.status, 502)
        self.assertEqual(app.bot_data["admitted_events"], 0)

    async def test_event_is_refused_while_draining(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        app = SimpleNamespace(bot_data={"draining": True}, bot=bot)
        r = await approval.handle_event(_Req(app, _cfg(), {"source": "t", "text": "x", "wake": True}))
        self.assertEqual(r.status, 503)
        bot.send_message.assert_not_awaited()



class SelfRestartRefusalTests(unittest.TestCase):
    def test_direct_stop_forms_are_refused(self) -> None:
        for cmd in (
            "systemctl --user restart claude-tg-bot",
            "systemctl --user restart claude-tg-bot.service",
            "cd x && systemctl --user stop claude-tg-bot",
            "sh -c 'systemctl --user restart claude-tg-bot'",
            "systemctl --user kill claude-tg-bot",
            "pkill -f claude_tg_bot",
            "kill $(systemctl --user show -p MainPID --value claude-tg-bot)",
        ):
            with self.subTest(cmd=cmd):
                self.assertIn("restart_request", approval._self_restart_refusal("Bash", {"command": cmd}))

    def test_reading_and_other_units_pass(self) -> None:
        for cmd in (
            "systemctl --user status claude-tg-bot",
            "systemctl --user restart claude-tg-bot-down",
            "systemctl --user restart news-digest",
            "journalctl --user -u claude-tg-bot -n 5",
            "systemctl --user is-active claude-tg-bot && systemctl --user restart domovoy-sync",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(approval._self_restart_refusal("Bash", {"command": cmd}))
        self.assertIsNone(approval._self_restart_refusal(
            "Write", {"command": "systemctl --user restart claude-tg-bot"}))


if __name__ == "__main__":
    unittest.main()

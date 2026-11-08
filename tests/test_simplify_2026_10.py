"""Аудит simplify 31.10.2026: важные ветки без тестов и найденные расхождения."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import approval  # noqa: E402


# ---------------------------------------------------------------------------
# hook_approve: хук стоит перед каждым инструментом агента под
# bypassPermissions. Любой код выхода кроме 0 с JSON и 2 Claude Code считает
# «не блокирующей ошибкой» и пускает вызов, поэтому каждая ветка сбоя обязана
# напечатать deny. До 31.10.2026 тестом была покрыта только удачная ветка.

import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import httpx  # noqa: E402

from claude_tg_bot import hook_approve, restart_request  # noqa: E402

_HOOK_CFG = SimpleNamespace(approve_http_host="127.0.0.1", approve_http_port=8788,
                            approve_shared_secret="s", approve_timeout_sec=1)
_HOOK_IN = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}


def _hook(stdin: str, *, cfg=_HOOK_CFG, post=None) -> tuple[int, dict | None]:
    out = io.StringIO()
    load = (patch.object(hook_approve, "load_config", side_effect=cfg) if isinstance(cfg, Exception)
            else patch.object(hook_approve, "load_config", return_value=cfg))
    with load, patch.object(hook_approve.httpx, "post", post or MagicMock()), \
            patch.object(sys, "stdin", io.StringIO(stdin)), patch.object(sys, "stdout", out), \
            patch.object(sys, "stderr", io.StringIO()):
        rc = hook_approve.main()
    text = out.getvalue().strip()
    return rc, json.loads(text) if text else None


def _decision(out: dict | None) -> str:
    assert out is not None
    return out["hookSpecificOutput"]["permissionDecision"]


class HookFailClosedTests(unittest.TestCase):
    def test_bad_stdin_blocks_with_exit_2(self) -> None:
        for raw in ("", "{не json", json.dumps({"tool_input": {}})):
            with self.subTest(raw=raw):
                rc, out = _hook(raw)
                self.assertEqual(rc, 2)
                self.assertIsNone(out)

    def test_config_error_denies(self) -> None:
        rc, out = _hook(json.dumps(_HOOK_IN), cfg=RuntimeError("CODEX_SANDBOX typo"))
        self.assertEqual(rc, 0)
        self.assertEqual(_decision(out), "deny")
        self.assertIn("config", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_bridge_down_or_error_denies(self) -> None:
        bad_status = MagicMock()
        bad_status.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=MagicMock())
        not_json = MagicMock()
        not_json.json.side_effect = ValueError("not json")
        for post in (MagicMock(side_effect=httpx.ConnectError("refused")),
                     MagicMock(side_effect=httpx.ReadTimeout("slow")),
                     MagicMock(return_value=bad_status), MagicMock(return_value=not_json)):
            with self.subTest(post=post):
                rc, out = _hook(json.dumps(_HOOK_IN), post=post)
                self.assertEqual(rc, 0)
                self.assertEqual(_decision(out), "deny")

    def test_unknown_or_missing_decision_denies(self) -> None:
        for data in ({}, {"decision": "ALLOW"}, {"decision": "yes"}, {"decision": None}):
            with self.subTest(data=data):
                resp = MagicMock()
                resp.json.return_value = data
                _rc, out = _hook(json.dumps(_HOOK_IN), post=MagicMock(return_value=resp))
                self.assertEqual(_decision(out), "deny")

    def test_allow_passes_reason_and_token(self) -> None:
        resp = MagicMock()
        resp.json.return_value = {"decision": "allow", "reason": "bash:safe"}
        post = MagicMock(return_value=resp)
        _rc, out = _hook(json.dumps(_HOOK_IN), post=post)
        self.assertEqual(_decision(out), "allow")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], "bash:safe")
        self.assertEqual(post.call_args.kwargs["headers"], {"X-Approve-Token": "s"})
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:8788/approve")

    def test_ipv6_host_bracketed(self) -> None:
        cfg = SimpleNamespace(**{**vars(_HOOK_CFG), "approve_http_host": "::1"})
        resp = MagicMock()
        resp.json.return_value = {"decision": "deny"}
        post = MagicMock(return_value=resp)
        _hook(json.dumps(_HOOK_IN), cfg=cfg, post=post)
        self.assertEqual(post.call_args.args[0], "http://[::1]:8788/approve")

    def test_crash_in_module_run_denies(self) -> None:
        # Настоящий запуск `python -m`, как в claude-hook-settings.json: JSON
        # не словарём роняет main(), последний рубеж печатает deny с кодом 0.
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(root / "src")}
        proc = subprocess.run([sys.executable, "-m", "claude_tg_bot.hook_approve"],
                              input="[1, 2]", capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_decision(json.loads(proc.stdout)), "deny")


# ---------------------------------------------------------------------------
# restart_request: агент узнаёт итог заявки по коду выхода и строке. Было 0 %.

class RestartRequestTests(unittest.TestCase):
    def _run(self, *, problem: str = "", post=None, cfg=None) -> tuple[int, str, MagicMock]:
        cfg = cfg or SimpleNamespace(approve_http_host="127.0.0.1", approve_http_port=8788,
                                     approve_shared_secret="")
        post = post or MagicMock()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(restart_request, "git_state", return_value=("abc123", [])), \
                patch.object(restart_request, "code_problem", return_value=problem), \
                patch.object(restart_request, "load_config", return_value=cfg), \
                patch.object(restart_request.httpx, "post", post), \
                patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            rc = restart_request.main(["--reason", " правка "])
        return rc, out.getvalue() + err.getvalue(), post

    @staticmethod
    def _resp(status: int, data: dict | None) -> MagicMock:
        r = MagicMock(status_code=status, text="oops")
        r.headers = {"content-type": "application/json"} if data is not None else {}
        r.json.return_value = data
        return r

    def test_uncommitted_code_refused_before_request(self) -> None:
        rc, text, post = self._run(problem="в коде есть незакоммиченные правки: x")
        self.assertEqual(rc, 2)
        self.assertIn("в коде есть незакоммиченные правки: x", text)
        post.assert_not_called()

    def test_bot_unreachable(self) -> None:
        rc, _text, _post = self._run(post=MagicMock(side_effect=httpx.ConnectError("refused")))
        self.assertEqual(rc, 3)

    def test_rejected(self) -> None:
        # 4xx отказ (2), 5xx исход неизвестен (3, таблица кодов в README).
        for resp, want in ((self._resp(409, {"error": "уже есть"}), 2), (self._resp(500, None), 3)):
            with self.subTest(status=resp.status_code):
                rc, _text, _post = self._run(post=MagicMock(return_value=resp))
                self.assertEqual(rc, want)

    def test_accepted_sends_commit_and_chat(self) -> None:
        post = MagicMock(return_value=self._resp(200, {"id": "r1"}))
        with patch.dict(os.environ, {restart_request.CHAT_KEY_ENV: "1:7"}):
            rc, text, post = self._run(post=post)
        self.assertEqual(rc, 0)
        self.assertIn("r1", text)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body, {"reason": "правка", "commit": "abc123", "chat_key": "1:7"})


# ---------------------------------------------------------------------------
# run_claude: ветки восстановления с настоящим дочерним процессом вместо
# claude. Были не покрыты: убийство по таймауту, повтор свежей сессией после
# протухшего --resume, поток без события result, отмена рана.

import asyncio  # noqa: E402
import tempfile  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from claude_tg_bot import claude_runner as runner  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402


def _result_line(text: str, sid: str) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": text, "session_id": sid})


async def _run_script(script: str | list[str], store, **kw):
    """run_claude, где каждый запуск claude это `python -c <script>`; список
    задаёт сценарий по запускам. Возвращает (итог, argv запусков)."""
    scripts = [script] if isinstance(script, str) else list(script)
    real_exec = asyncio.create_subprocess_exec
    calls: list[tuple] = []

    async def spawn(*args, **skw):
        calls.append(args)
        return await real_exec(sys.executable, "-c", scripts.pop(0), **skw)

    with patch.object(runner, "_ensure_hook_still_valid"), \
            patch.object(runner, "ensure_oauth_fresh", AsyncMock()), \
            patch.object(runner, "agent_env", return_value={}), \
            patch.object(runner.asyncio, "create_subprocess_exec", spawn):
        result = await runner.run_claude("claude", "1", 1, None, "test", store,
                                         cwd=Path("/tmp"), hook_settings=Path("/tmp/fake"), **kw)
    return result, calls


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RunClaudeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        self.store = runner.SessionStore(self.dir / "sessions.json")

    def test_timeout_kills_process_group_and_reports_error(self) -> None:
        # Внук в той же группе процессов тоже должен умереть: claude держит
        # MCP-серверы и Bash детьми.
        pidfile = self.dir / "child.pid"
        script = ("import subprocess, sys, time\n"
                  "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                  f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
                  "time.sleep(60)\n")
        registry: dict = {}
        result, _calls = asyncio.run(_run_script(script, self.store, max_runtime_sec=2,
                                                 process_registry=registry))
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, _("⛔ claude убит по таймауту ({timeout}s)").format(timeout=2))
        self.assertEqual(registry, {})
        grandchild = int(pidfile.read_text())
        for _try in range(50):
            if not _pid_alive(grandchild):
                break
            import time
            time.sleep(0.1)
        self.assertFalse(_pid_alive(grandchild), "внук claude пережил таймаут")

    def test_stale_resume_retries_once_with_fresh_session(self) -> None:
        self.store.set("1", "dead-sid")
        stale = ("import sys\nsys.stderr.write('No conversation found with session ID: dead-sid')\n"
                 "sys.exit(1)\n")
        fresh = f"print({_result_line('ответ', 'new-sid')!r})\n"
        result, calls = asyncio.run(_run_script([stale, fresh], self.store))
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.text, "ответ")
        self.assertEqual(len(calls), 2)
        self.assertIn("--resume", calls[0])
        self.assertNotIn("--resume", calls[1])
        self.assertEqual(self.store.get("1"), "new-sid")

    def test_other_resume_error_does_not_drop_session(self) -> None:
        self.store.set("1", "live-sid")
        script = "import sys\nsys.stderr.write('API overloaded')\nsys.exit(1)\n"
        result, calls = asyncio.run(_run_script(script, self.store))
        self.assertTrue(result.is_error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get("1"), "live-sid")

    def test_stream_without_result_event(self) -> None:
        script = "print('{\"type\": \"system\", \"subtype\": \"init\", \"session_id\": \"s1\"}')\n"
        result, _calls = asyncio.run(_run_script(script, self.store))
        self.assertTrue(result.is_error)
        self.assertNotIn('"type"', result.text)

    def test_cancel_kills_child(self) -> None:
        pidfile = self.dir / "pid"
        script = f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"

        async def go() -> None:
            task = asyncio.create_task(_run_script(script, self.store))
            for _try in range(100):
                await asyncio.sleep(0.05)
                if pidfile.exists() and pidfile.read_text():
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(go())
        self.assertFalse(_pid_alive(int(pidfile.read_text())))

    def test_corrupt_sessions_file_does_not_crash(self) -> None:
        path = self.dir / "s.json"
        path.write_text("{обрыв")
        self.assertIsNone(runner.SessionStore(path).get("1"))
        path.write_text(json.dumps({"1": "legacy-sid", "2": {"v": "sid2", "ts": 5}}))
        store = runner.SessionStore(path)
        self.assertEqual((store.get("1"), store.get("2")), ("legacy-sid", "sid2"))

    def test_undecodable_or_bad_ts_sessions_file_does_not_crash_start(self) -> None:
        # Хранилище открывается в build_app: исключение здесь это бот, который
        # не стартует, и цикл перезапусков systemd.
        path = self.dir / "s.json"
        path.write_bytes(b"\xff\xfe{")
        self.assertIsNone(runner.SessionStore(path).get("1"))
        path.write_text(json.dumps({"1": {"v": "sid1", "ts": "abc"}, "2": {"v": "sid2", "ts": 5}}))
        store = runner.SessionStore(path)
        self.assertEqual((store.get("1"), store.get("2")), ("sid1", "sid2"))

    def test_corrupt_sessions_file_kept_aside_before_overwrite(self) -> None:
        # Следующая запись перезаписала бы файл: сессии всех тем терялись бы
        # без следа. Копия рядом позволяет восстановить руками.
        path = self.dir / "s.json"
        path.write_text("{обрыв")
        store = runner.SessionStore(path)
        store.set("1", "new")
        kept = list(self.dir.glob("s.json.corrupt*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), "{обрыв")



# ---------------------------------------------------------------------------
# Мутации, которые выживали при полном наборе тестов (аудит 31.10.2026):
# правка ломала поведение, ни один тест не падал.

from claude_tg_bot import replies, runs  # noqa: E402


class SurvivedMutationTests(unittest.TestCase):
    def test_self_restart_refused_for_monitor_too(self) -> None:
        # Monitor запускает команду так же, как Bash: `("Bash",)` вместо
        # `("Bash", "Monitor")` проходил незамеченным.
        cmd = {"command": "systemctl --user restart claude-tg-bot"}
        for tool in ("Bash", "Monitor"):
            with self.subTest(tool=tool):
                self.assertIsNotNone(approval._self_restart_refusal(tool, cmd))

    def test_card_truncate_never_cuts_html_entity(self) -> None:
        # Битая сущность `&l` в HTML карточки: Telegram отвергает сообщение.
        for raw, limit in (("<" * 5, 10), ("a&b" * 10, 9), ("x" + ">" * 9, 7)):
            with self.subTest(raw=raw, limit=limit):
                out = approval._esc_truncate(raw, limit)
                body = out.removesuffix(" …")
                self.assertLessEqual(len(body), limit)
                last = body.rfind("&")
                self.assertTrue(last < 0 or ";" in body[last:], out)

    def test_sentence_split_keeps_link_whole(self) -> None:
        text = "Начало. [сайт. тут](https://a.b/c) конец. Ещё раз."
        self.assertEqual(replies._split_outside_markup(text, replies._SENTENCE_END_RE),
                         ["Начало.", "[сайт. тут](https://a.b/c) конец.", "Ещё раз."])

    def test_hard_cut_keeps_markup_that_renders_short(self) -> None:
        link = "[ab](https://example.org/" + "x" * 50 + ")"
        self.assertEqual(replies._hard_cut(link, 10), [link])
        self.assertEqual(replies._hard_cut("y" * 25, 10), ["y" * 10, "y" * 10, "y" * 5])

    def test_split_for_telegram_chunks_do_not_start_with_newline(self) -> None:
        parts = replies.split_for_telegram("a" * 8 + "\n\n\n" + "b" * 8, 10)
        self.assertEqual(parts, ["a" * 8 + "\n", "b" * 8])

    def test_run_end_drops_plan_grant(self) -> None:
        run_id = runs.start("sid-mut")
        self.assertTrue(runs.grant_plan("sid-mut", run_id, "aid"))
        runs.end("sid-mut", run_id)
        self.assertNotIn(run_id, runs._plans)
        self.assertIsNone(runs.plan_for("sid-mut"))



# ---------------------------------------------------------------------------
# Окно ⏩ (burst.py): внутри него карточек нет вовсе. Было 48 %: не
# проверялись истечение, замена окна и сброс.

from claude_tg_bot import burst  # noqa: E402


def _burst_ctx() -> SimpleNamespace:
    bot = SimpleNamespace(send_message=AsyncMock())
    app = SimpleNamespace(bot_data={}, bot=bot)
    return SimpleNamespace(application=app)


class BurstWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_duration_clamped(self) -> None:
        ctx = _burst_ctx()
        now = burst.time.time()
        until = burst.burst_set(ctx, "1", 10 ** 6, 1, None)
        self.assertLessEqual(until - now, burst.BURST_MAX_SEC + 1)
        until = burst.burst_set(ctx, "1", 0, 1, None)
        self.assertGreaterEqual(until - now, burst.BURST_MIN_SEC - 1)
        burst.burst_clear(ctx, "1")

    async def test_scoped_by_topic_and_clear(self) -> None:
        ctx = _burst_ctx()
        burst.burst_set(ctx, burst.burst_target_key(1, 7), 600, 1, 7)
        self.assertGreater(burst.burst_until_ts(ctx, "1:7"), burst.time.time())
        self.assertEqual(burst.burst_until_ts(ctx, "1"), 0.0)
        task = ctx.application.bot_data["burst_expiry_tasks"]["1:7"]
        self.assertTrue(burst.burst_clear(ctx, "1:7"))
        self.assertEqual(burst.burst_until_ts(ctx, "1:7"), 0.0)
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled() or task.done())
        self.assertFalse(burst.burst_clear(ctx, "1:7"))
        ctx.application.bot.send_message.assert_not_awaited()

    async def test_expiry_closes_window_and_notifies_once(self) -> None:
        ctx = _burst_ctx()
        with patch.object(burst, "BURST_MIN_SEC", 0):
            burst.burst_set(ctx, "1:7", 0, 1, 7)
        await ctx.application.bot_data["burst_expiry_tasks"]["1:7"]
        self.assertNotIn("1:7", ctx.application.bot_data["burst_until_by_target"])
        self.assertNotIn("1:7", ctx.application.bot_data["burst_expiry_tasks"])
        ctx.application.bot.send_message.assert_awaited_once()
        self.assertEqual(ctx.application.bot.send_message.await_args.kwargs["message_thread_id"], 7)

    async def test_replaced_window_old_watcher_silent(self) -> None:
        # Сторож старого окна, проснувшись после продления, окно не закрывает.
        ctx = _burst_ctx()
        windows = ctx.application.bot_data.setdefault("burst_until_by_target", {})
        windows["1"] = burst.time.time() + 600
        await burst._burst_expiry_watcher(ctx.application, "1", burst.time.time(), 1, None)
        self.assertIn("1", windows)
        ctx.application.bot.send_message.assert_not_awaited()

    async def test_notify_failure_still_closes_window(self) -> None:
        ctx = _burst_ctx()
        ctx.application.bot.send_message.side_effect = RuntimeError("telegram down")
        until = burst.time.time()
        ctx.application.bot_data["burst_until_by_target"] = {"1": until}
        with patch.object(burst.log, "exception"):
            await burst._burst_expiry_watcher(ctx.application, "1", until, 1, None)
        self.assertNotIn("1", ctx.application.bot_data["burst_until_by_target"])

    def test_decision_and_duration_parse(self) -> None:
        self.assertEqual(burst.burst_duration_for_decision("auto15"), burst.BURST_DEFAULT_SEC)
        self.assertEqual(burst.burst_duration_for_decision("auto60"), burst.BURST_LONG_SEC)
        self.assertIsNone(burst.burst_duration_for_decision("allow"))


if __name__ == "__main__":
    unittest.main()

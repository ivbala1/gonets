"""Общий модуль подпроцессов procs (ревизия повторов 07.11.2026, п. 1).

Настоящие процессы: лидер группы выходит, а его внук держит stdout. Так
выглядит типичный таймаут рана, и раньше kill_process_tree тут молчала.
"""
from __future__ import annotations

import ast
import asyncio
import os
import signal
import time
import unittest
import unittest.mock
from pathlib import Path

from claude_tg_bot import claude_runner, lifecycle, procs

ROOT = Path(__file__).resolve().parents[1]


def _gone(pid: int, wait: float = 3.0) -> bool:
    """Процесса нет или он зомби (снимет приёмный родитель)."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):  # исчез посреди чтения
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _spawn_orphan(script: str) -> tuple[asyncio.subprocess.Process, int]:
    """Лидер печатает pid фонового внука и выходит; внук держит stdout."""
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", script, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, start_new_session=True)
    line = await asyncio.wait_for(proc.stdout.readline(), 5)
    # Не proc.wait(): с 3.12 он ждёт и закрытия труб, а их держит внук.
    deadline = time.monotonic() + 5
    while proc.returncode is None:
        assert time.monotonic() < deadline, "лидер не вышел"
        await asyncio.sleep(0.02)
    return proc, int(line)


class KillGroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for pid in getattr(self, "_pids", ()):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def test_leader_exited_grandchild_killed(self) -> None:
        proc, child = await _spawn_orphan("sleep 60 & echo $!")
        self._pids = [child]
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(_gone(child, wait=0.2))
        self.assertTrue(procs.group_alive(proc))
        # Старое имя ведёт в ту же функцию: на нём висят патчи тестов.
        claude_runner.kill_process_tree(proc, signal.SIGKILL)
        self.assertTrue(_gone(child))

    async def test_empty_group_is_quiet(self) -> None:
        proc = await asyncio.create_subprocess_exec("true", start_new_session=True)
        await proc.wait()
        procs.kill_group(proc, signal.SIGKILL)
        self.assertFalse(procs.group_alive(proc))

    async def test_no_pid_is_noop(self) -> None:
        procs.kill_group(None, signal.SIGKILL)
        self.assertFalse(procs.group_alive(None))

    async def test_running_sees_group(self) -> None:
        # Пока только остановка бота; /cancel ждёт решения (BACKLOG.md).
        self.assertFalse(procs.running(None))
        proc, child = await _spawn_orphan("sleep 60 & echo $!")
        self._pids = [child]
        # Остановка бота судит так: лидер вышел, а ран ещё идёт.
        self.assertTrue(procs.running(proc))
        procs.kill_group(proc, signal.SIGKILL)
        self.assertTrue(_gone(child))
        deadline = time.monotonic() + 3
        while procs.running(proc) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(procs.running(proc))

    async def test_without_own_group_kills_process_itself(self) -> None:
        proc = await asyncio.create_subprocess_exec("sleep", "60")
        self._pids = [proc.pid]
        with self.assertLogs("claude_tg_bot.procs", "WARNING"):
            procs.kill_group(proc, signal.SIGKILL)
        self.assertEqual(await asyncio.wait_for(proc.wait(), 3), -signal.SIGKILL)

    async def test_terminate_runs_kills_left_group(self) -> None:
        from types import SimpleNamespace
        proc, child = await _spawn_orphan("(trap '' TERM; exec sleep 60) & echo $!")
        self._pids = [child]
        app = SimpleNamespace(bot_data={"procs": {"1": proc}})
        with unittest.mock.patch.object(lifecycle, "_STOP_GRACE_SEC", 0.2), \
                unittest.mock.patch.object(lifecycle._b, "_downloads_cancel"), \
                unittest.mock.patch.object(lifecycle._b, "_local_stt_cancel"):
            lifecycle._terminate_runs(app)
            self.assertFalse(_gone(child, wait=0.1))
            await asyncio.sleep(0.4)
        self.assertTrue(_gone(child))

    async def test_terminate_and_wait_kills_left_group(self) -> None:
        # Внук не реагирует на SIGTERM, лидер уже вышел: остановка бота
        # раньше выходила по returncode лидера и внука не трогала.
        proc, child = await _spawn_orphan("(trap '' TERM; exec sleep 60) & echo $!")
        self._pids = [child]
        await lifecycle._terminate_and_wait("1", proc, grace_sec=0.3)
        self.assertTrue(_gone(child))

    async def test_terminate_and_wait_sigterm_enough(self) -> None:
        proc, child = await _spawn_orphan("sleep 60 & echo $!")
        self._pids = [child]
        await lifecycle._terminate_and_wait("1", proc, grace_sec=2.0)
        self.assertTrue(_gone(child))


class ChildEnvTests(unittest.IsolatedAsyncioTestCase):
    """agent-browser и воркер распознавания без секретов бота (п. 1, шаг 2)."""

    SECRET = {"TELEGRAM_BOT_TOKEN": "123:secret", "OPENAI_API_KEY": "sk-x"}

    def _check(self, env: dict) -> None:
        from claude_tg_bot import config
        for name in self.SECRET:
            self.assertNotIn(name, env)
        for name, value in config.STREAM_CAP_ENV.items():
            self.assertEqual(env[name], value)

    def test_handoff_ab(self) -> None:
        from unittest.mock import patch
        from claude_tg_bot import handoff
        with patch.dict(os.environ, self.SECRET), \
                patch.object(handoff.subprocess, "run") as run:
            handoff._ab("s1", "stream", "status")
        env = run.call_args.kwargs["env"]
        self._check(env)
        self.assertEqual(env[handoff.SESSION_ENV], "s1")

    async def test_screen_eval(self) -> None:
        from unittest.mock import AsyncMock, patch
        from claude_tg_bot import handoff, screen_server
        proc = AsyncMock(returncode=0, pid=None)
        proc.communicate.return_value = (b"1", b"")
        spawn = AsyncMock(return_value=proc)
        with patch.dict(os.environ, self.SECRET), patch.object(asyncio, "create_subprocess_exec", spawn):
            self.assertEqual(await screen_server._eval("s1", "1", 3.0), 1)
        env = spawn.call_args.kwargs["env"]
        self._check(env)
        self.assertEqual(env[handoff.SESSION_ENV], "s1")

    async def test_local_stt_worker(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from claude_tg_bot import voice
        proc = AsyncMock(returncode=0, pid=None)
        proc.communicate.return_value = (b'{"text": "ok"}\n', b"")
        spawn = AsyncMock(return_value=proc)
        cfg = SimpleNamespace(local_stt_python="python3", local_stt_model_dir="m", local_stt_vad_model="v")
        with patch.dict(os.environ, self.SECRET), patch.object(asyncio, "create_subprocess_exec", spawn):
            self.assertEqual(await voice._transcribe_local_once(cfg, Path("a.ogg"), 1.0), "ok")
        self._check(spawn.call_args.kwargs["env"])


class RunBoundedTests(unittest.IsolatedAsyncioTestCase):
    """procs.run_bounded: своя группа, убийство на таймауте и отмене (шаг 3)."""

    async def asyncTearDown(self) -> None:
        for pid in getattr(self, "_pids", ()):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def test_ok_output_and_err_tail(self) -> None:
        rc, out, err = await procs.run_bounded(
            ["sh", "-c", "cat; echo oops >&2; exit 3"], timeout=5, env=None, input=b"hi")
        self.assertEqual((rc, out, err), (3, b"hi", "oops"))
        rc, out, err = await procs.run_bounded(
            ["sh", "-c", "echo a; echo b >&2"], timeout=5, env={"PATH": os.environ["PATH"]},
            stderr=asyncio.subprocess.STDOUT, cwd="/")
        self.assertEqual((rc, sorted(out.split()), err), (0, [b"a", b"b"], ""))

    async def test_err_tail_is_tail(self) -> None:
        _rc, _out, err = await procs.run_bounded(
            ["sh", "-c", "printf 'x%.0s' $(seq 3000) >&2; echo END >&2"], timeout=5, env=None)
        self.assertTrue(err.endswith("END"))
        self.assertEqual(len(err), procs.ERR_TAIL_CHARS)

    async def test_timeout_kills_group_even_after_leader_exit(self) -> None:
        pidfile = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"procs-test-{os.getpid()}")
        self.addCleanup(lambda: os.path.exists(pidfile) and os.unlink(pidfile))
        with self.assertRaises(TimeoutError):
            await procs.run_bounded(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; exit 0"],
                                    timeout=0.5, env=None)
        child = int(open(pidfile).read())
        self._pids = [child]
        self.assertTrue(_gone(child))

    async def test_cancel_kills_group(self) -> None:
        pidfile = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"procs-test-c-{os.getpid()}")
        self.addCleanup(lambda: os.path.exists(pidfile) and os.unlink(pidfile))
        task = asyncio.create_task(procs.run_bounded(
            ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], timeout=30, env=None))
        deadline = time.monotonic() + 5
        while not os.path.exists(pidfile) or not open(pidfile).read().strip():
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(0.02)
        child = int(open(pidfile).read())
        self._pids = [child]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(_gone(child))

    async def test_missing_binary_is_oserror(self) -> None:
        with self.assertRaises(OSError):
            await procs.run_bounded(["/nonexistent/prog"], timeout=5, env=None)

    async def test_spawn_cancelled_midway_kills_born_process(self) -> None:
        from unittest.mock import patch
        born = await asyncio.create_subprocess_exec("sleep", "60", start_new_session=True)
        self._pids = [born.pid]
        gate = asyncio.Event()

        async def slow_spawn(*_a, **_kw):
            await gate.wait()
            return born
        with patch.object(asyncio, "create_subprocess_exec", slow_spawn):
            task = asyncio.create_task(procs.spawn(["sleep", "60"]))
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.sleep(0.05)
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(born.returncode, -signal.SIGKILL)


class RunBoundedSyncTests(unittest.TestCase):
    """procs.run_bounded_sync для скиллов: как subprocess.run, но группой (шаг 4)."""

    def test_like_run(self) -> None:
        import subprocess
        res = procs.run_bounded_sync(["sh", "-c", "cat; echo e >&2; exit 2"], timeout=5, env=None,
                                     input="hi", text=True)
        self.assertIsInstance(res, subprocess.CompletedProcess)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (2, "hi", "e\n"))
        res = procs.run_bounded_sync(["pwd"], timeout=5, env={"PATH": os.environ["PATH"]}, cwd="/")
        self.assertEqual(res.stdout, b"/\n")

    def test_timeout_kills_group(self) -> None:
        import subprocess
        pidfile = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"procs-test-s-{os.getpid()}")
        self.addCleanup(lambda: os.path.exists(pidfile) and os.unlink(pidfile))
        with self.assertRaises(subprocess.TimeoutExpired):
            procs.run_bounded_sync(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; exit 0"],
                                   timeout=0.5, env=None)
        child = int(open(pidfile).read())
        self.addCleanup(_kill_quietly, child)
        self.assertTrue(_gone(child))

    def test_any_error_kills_group(self) -> None:
        import subprocess
        from unittest.mock import patch
        pidfile = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"procs-test-k-{os.getpid()}")
        self.addCleanup(lambda: os.path.exists(pidfile) and os.unlink(pidfile))
        real = subprocess.Popen.communicate
        calls = []

        def interrupted(proc, *a, **kw):
            calls.append(1)
            if len(calls) == 1:
                deadline = time.monotonic() + 5
                while not (os.path.exists(pidfile) and open(pidfile).read().strip()):
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                raise KeyboardInterrupt
            return real(proc, *a, **kw)
        with patch.object(subprocess.Popen, "communicate", interrupted), self.assertRaises(KeyboardInterrupt):
            procs.run_bounded_sync(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], timeout=30, env=None)
        child = int(open(pidfile).read())
        self.addCleanup(_kill_quietly, child)
        self.assertTrue(_gone(child))

    def test_stdin_closed_without_input(self) -> None:
        # Без input stdin пуст, а не терминал бота: программа не ждёт ввода.
        res = procs.run_bounded_sync(["cat"], timeout=5, env=None)
        self.assertEqual((res.returncode, res.stdout), (0, b""))

    def test_push_ssh_batch_unless_own(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from claude_tg_bot.skills.news_digest import backlog

        def env_for(environ: dict, ssh_config: str = "") -> dict:
            with patch.dict(os.environ, environ), \
                    patch.object(backlog, "_git", lambda *a, **kw: SimpleNamespace(stdout=ssh_config)):
                for name in ("GIT_SSH_COMMAND", "GIT_SSH"):
                    if name not in environ:
                        os.environ.pop(name, None)
                return backlog._push_env(Path("/r"))
        self.assertEqual(env_for({})["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes")
        self.assertEqual(env_for({"GIT_SSH_COMMAND": "my-ssh"})["GIT_SSH_COMMAND"], "my-ssh")
        self.assertNotIn("GIT_SSH_COMMAND", env_for({"GIT_SSH": "/bin/plink"}))
        self.assertNotIn("GIT_SSH_COMMAND", env_for({}, ssh_config="ssh -i k\n"))

    def test_backlog_git_never_prompts(self) -> None:
        from pathlib import Path
        from unittest.mock import patch
        from claude_tg_bot.skills.news_digest import backlog
        with patch.object(backlog.procs, "run_bounded_sync") as run, \
                patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:secret"}):
            backlog._git(Path("/r"), "status")
            backlog._git(Path("/r"), "write-tree", env={"GIT_INDEX_FILE": "/i"})
        first, second = (c.kwargs["env"] for c in run.call_args_list)
        self.assertEqual(first["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", first)
        self.assertEqual(second, {"GIT_INDEX_FILE": "/i", "GIT_TERMINAL_PROMPT": "0"})


class RunClaudeJsonTests(unittest.TestCase):
    """Одна общая _oneshot.run_claude_json вместо копий в сводках (п. 6, шаг 5)."""

    def _run(self, **kw):
        import subprocess
        from unittest.mock import patch
        from claude_tg_bot.skills import _oneshot
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = args
            return subprocess.CompletedProcess(args, 0, b'{"result": " ok \\u2014 x "}', b"")
        with patch.object(_oneshot, "run_claude_oneshot", fake), \
                patch.object(_oneshot, "resolve_claude_bin", lambda: "/bin/claude"):
            return _oneshot.run_claude_json("p", timeout=5, **kw), seen["args"]

    def test_no_tools_no_model_by_default(self) -> None:
        from unittest.mock import patch

        from claude_tg_bot import paths
        text, args = self._run()
        self.assertEqual(text, "ok - x")
        self.assertEqual(args, [
            "/bin/claude", "-p", "--output-format", "json", "--permission-mode", "plan",
            "--tools", "", "--no-session-persistence", "--settings", str(paths.hook_settings()),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])
        # Перенос в _oneshot: корень репозитория тот же, настройки хука на месте.
        with patch.dict("os.environ", {"CLAUDE_HOOK_SETTINGS": ""}):
            self.assertEqual(paths.hook_settings(), ROOT / "claude-hook-settings.json")
        self.assertEqual(paths.BOT_ROOT, ROOT)

    def test_model(self) -> None:
        _text, args = self._run(model="claude-haiku")
        self.assertEqual(args[args.index("--model") + 1], "claude-haiku")

    def test_digests_share_it(self) -> None:
        from unittest.mock import patch
        from claude_tg_bot.skills.chat_digest import __main__ as chat
        from claude_tg_bot.skills.news_digest import __main__ as news
        with patch.object(chat, "run_claude_json", return_value="a") as one:
            self.assertEqual(chat.run_digest_claude("p", 7), "a")
        one.assert_called_once_with("p", timeout=7)
        with patch.object(news, "run_claude_json", return_value="b") as two:
            self.assertEqual(news.run_claude("p", 7, "m"), "b")
        two.assert_called_once_with("p", timeout=7, model="m")

    def test_installed_summary_uses_configured_claude(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        from claude_tg_bot.skills.news_digest import __main__ as news
        calls = []

        def fake(argv, **kw):
            calls.append(argv[0])
            return SimpleNamespace(stdout="1.0\n")
        with patch.object(news, "resolve_claude_bin", lambda: "/opt/claude"), \
                patch.object(news.procs, "run_bounded_sync", fake):
            news.installed_summary()
        self.assertEqual(calls, ["/opt/claude", "codex"])


class ReapAndBornTests(unittest.IsolatedAsyncioTestCase):
    """Ревью 08.11.2026: недочитанный канал не держит reap, двойная отмена
    запуска не бросает родившийся процесс."""

    async def test_reap_drains_full_pipe(self) -> None:
        proc = await procs.spawn(["yes"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.sleep(0.3)  # буфер полон, чтение на паузе
        procs.kill_group(proc, signal.SIGKILL)
        t = time.monotonic()
        await procs.reap(proc)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(proc.returncode, -signal.SIGKILL)

    async def test_born_survives_second_cancel(self) -> None:
        started = asyncio.Event()
        proc_box = []

        async def slow_spawn():
            started.set()
            await asyncio.sleep(0.2)
            p = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
            proc_box.append(p)
            return p

        task = asyncio.ensure_future(slow_spawn())

        async def caller():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                got = await procs.born(task)
                self.assertIs(got, proc_box[0])
                procs.kill_group(got, signal.SIGKILL)
                await procs.reap(got)
                raise

        c = asyncio.ensure_future(caller())
        await started.wait()
        c.cancel()
        await asyncio.sleep(0.05)
        c.cancel()  # вторая отмена, пока запуск ещё идёт
        with self.assertRaises(asyncio.CancelledError):
            await c
        self.assertTrue(proc_box and proc_box[0].returncode is not None)


# ---- преграда: подпроцессы только через procs (шаг 6) ----

# Запуск мимо procs, файл::функция (Класс.метод, внешняя.вложенная) -> причина.
# Новая строка сюда только с причиной; служебный запуск с таймаутом звать
# procs.run_bounded(_sync).
SPAWN_ALLOWED = {
    "src/claude_tg_bot/local_stt_worker.py::_decode": "воркер под python -I без пакета бота",
    "src/claude_tg_bot/tg_probe.py::ensure_voice": "замерщик сессии tg-probe, ffmpeg без потомков",
    "src/claude_tg_bot/restart.py::git_state": "git rev-parse/status без потомков, timeout",
    "src/claude_tg_bot/restart.py::changed_since": "git diff без потомков, timeout",
    "src/claude_tg_bot/handoff.py::crop_image": "ffmpeg без потомков, timeout",
    "src/claude_tg_bot/handoff.py::_ab": "клиент agent-browser, фоновый процесс в своей сессии; env без секретов",
    "src/claude_tg_bot/config.py::_validate_claude_version": "claude --version при старте, timeout",
    "src/claude_tg_bot/skills/transcribe/__main__.py::probe_duration": "ffprobe без потомков, timeout",
    "src/claude_tg_bot/skills/transcribe/__main__.py::split_audio": "ffmpeg без потомков, timeout",
    "src/claude_tg_bot/skills/immich_relay/__main__.py::_ssh": "ssh BatchMode, timeout",
    "tools/export_public.py::*": "отдельный инструмент без пакета бота",
    "bot-down-notify.py::*": "сторож без пакета бота",
}
# Копия всего os.environ (дальше она уходит потомку с секретами бота), файл::функция.
# Окружение потомка собирать config.agent_env().
ENV_COPY_ALLOWED = {
    "src/claude_tg_bot/config.py::agent_env": "сама сборка окружения без секретов",
    "src/claude_tg_bot/config.py::load_config": "снимок окружения до .env (ENV_BEFORE_DOTENV), потомку не уходит",
    "src/claude_tg_bot/restart.py::fresh_env": "проверка --check перед перезапуском: окружение свежего запуска с .env",
    "tools/export_public.py::*": "отдельный инструмент без пакета бота",
}
# Потомок без env= или с env=None наследует окружение процесса, а после
# load_dotenv/load_config в нём секреты бота (ревью Codex 08.11.2026): env
# собирать config.agent_env(). Сюда только запуск, которому окружение уже
# очистил родитель или которому пакет бота недоступен.
ENV_INHERIT_ALLOWED = {
    "src/claude_tg_bot/local_stt_worker.py::_decode": "воркер под python -I без пакета бота, env ему собрал бот (agent_env)",
    "bot-down-notify.py::*": "сторож без пакета бота, .env не грузит",
}
# Запуски через procs: env у них обязательный, но None пропускал наследование.
PROCS_SPAWN = {"run_bounded", "run_bounded_sync", "spawn"}
SPAWN_FULL = {
    *(f"subprocess.{n}" for n in ("run", "Popen", "call", "check_call", "check_output",
                                   "getoutput", "getstatusoutput")),
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "asyncio.subprocess.create_subprocess_exec", "asyncio.subprocess.create_subprocess_shell",
    "os.system", "os.popen", "os.posix_spawn", "os.posix_spawnp", "pty.spawn",
    *(f"os.spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
}
# Метод цикла событий: имена уникальны, владелец не важен.
SPAWN_ANY_ATTR = {"subprocess_exec", "subprocess_shell"}


def _sources():
    yield from sorted((ROOT / "src/claude_tg_bot").rglob("*.py"))
    yield from sorted((ROOT / "tools").glob("*.py"))
    yield ROOT / "bot-down-notify.py"


def _full_name(node, aliases: dict[str, str]) -> str | None:
    """Полное имя вызываемого с учётом import X as Y и from X import Y."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _full_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_environ(node, aliases: dict[str, str]) -> bool:
    return _full_name(node, aliases) == "os.environ"


def _spawn_calls(path: Path):
    """(функция, строка, что): запуски и копии всего os.environ в файле."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[(a.asname or a.name).split(".")[0]] = (a.name if a.asname else a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    # from .procs import spawn as launch: имя launch -> spawn;
    # from . import procs as p, import x.procs as p: модуль под именем p.
    procs_names: dict[str, str] = {}
    procs_mods = {"procs"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if (node.module or "").split(".")[-1] == "procs":
                    procs_names[a.asname or a.name] = a.name
                elif a.name == "procs":
                    procs_mods.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] == "procs" and a.asname:
                    procs_mods.add(a.asname)
    found = []

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if func == "<module>" else f"{func}.{child.name}"
            if isinstance(child, ast.Call):
                f = child.func
                full = _full_name(f, aliases)
                spawn = full in SPAWN_FULL or (isinstance(f, ast.Attribute) and f.attr in SPAWN_ANY_ATTR)
                if spawn:
                    found.append((func, child.lineno, full or f.attr))
                via_procs = (isinstance(f, ast.Attribute) and f.attr in PROCS_SPAWN and (
                    (isinstance(f.value, ast.Name) and (f.value.id in procs_mods or "procs" in f.value.id))
                    or (_full_name(f.value, aliases) or "").endswith(".procs"))) or (
                    isinstance(f, ast.Name) and procs_names.get(f.id) in PROCS_SPAWN)
                if spawn or via_procs:
                    env_kw = next((kw.value for kw in child.keywords if kw.arg == "env"), None)
                    if env_kw is None or (isinstance(env_kw, ast.Constant) and env_kw.value is None):
                        found.append((func, child.lineno, "env-inherit"))
                # {k: v for k, v in os.environ.items()}
                if (isinstance(f, ast.Attribute) and f.attr == "items" and _is_environ(f.value, aliases)):
                    found.append((func, child.lineno, "env-copy"))
                # dict(os.environ ...), os.environ.copy()
                if (full == "dict" or (isinstance(f, ast.Name) and f.id == "dict")) and any(
                        _is_environ(n, aliases) for a in child.args for n in ast.walk(a)):
                    found.append((func, child.lineno, "env-copy"))
                if isinstance(f, ast.Attribute) and f.attr == "copy" and _is_environ(f.value, aliases):
                    found.append((func, child.lineno, "env-copy"))
                for kw in child.keywords:
                    if kw.arg == "env" and _is_environ(kw.value, aliases):
                        found.append((func, child.lineno, "env-copy"))
            if isinstance(child, ast.Dict) and any(
                    k is None and _is_environ(v, aliases) for k, v in zip(child.keys, child.values)):
                found.append((func, child.lineno, "env-copy"))
            walk(child, name)
    walk(tree, "<module>")
    return found


def _check(found_by_file, allowed: dict[str, str], kind: str) -> None:
    bad, used = [], set()
    for rel, items in found_by_file:
        for func, line, what in items:
            for key in (f"{rel}::{func}", f"{rel}::*"):
                if key in allowed:
                    used.add(key)
                    break
            else:
                bad.append(f"{rel}:{line} {func}: {what}")
    assert bad == [], f"{kind}:\n" + "\n".join(bad)
    # Строки без находки: убрать, чтобы список не разрешал лишнего. Файла нет
    # (открытая копия без закрытых модулей): строка не в счёт.
    present = {k for k in allowed if (ROOT / k.split("::")[0]).is_file()}
    assert present - used == set(), f"лишние строки списка ({kind}): {sorted(present - used)}"


def test_spawn_only_through_procs() -> None:
    """Подпроцессы и окружение дочерних программ только через procs/agent_env:
    своя копия запуска уже давала внуков после таймаута и токен в agent-browser."""
    scans = []
    for path in _sources():
        rel = path.relative_to(ROOT).as_posix()
        if rel != "src/claude_tg_bot/procs.py":
            scans.append((rel, _spawn_calls(path)))
    _check([(r, [x for x in it if x[2] not in ("env-copy", "env-inherit")]) for r, it in scans],
           SPAWN_ALLOWED, "запуск мимо procs")
    _check([(r, [x for x in it if x[2] == "env-inherit"]) for r, it in scans], ENV_INHERIT_ALLOWED,
           "потомок наследует окружение (env= нет или None) мимо config.agent_env")
    _check([(r, [x for x in it if x[2] == "env-copy"]) for r, it in scans], ENV_COPY_ALLOWED,
           "копия всего os.environ мимо config.agent_env")


def test_spawn_barrier_sees_env_inherit(tmp_path) -> None:
    """Потомок без env, с env=None, через procs с env=None и копия через
    os.environ.items() ловятся; env=agent_env() проходит (ревью Codex 08.11.2026)."""
    src = tmp_path / "m.py"
    src.write_text(
        "import os, subprocess\nfrom . import procs\nfrom .procs import run_bounded\n"
        "from .procs import spawn as launch\nfrom .. import procs as p\n"
        "def a():\n    subprocess.run(['x'])\n    subprocess.run(['x'], env=None)\n"
        "    procs.run_bounded_sync(['x'], timeout=1, env=None)\n"
        "async def b():\n    await run_bounded(['x'], timeout=1, env=None)\n"
        "def c():\n    subprocess.run(['x'], env=agent_env())\n"
        "    procs.run_bounded_sync(['x'], timeout=1, env=agent_env())\n"
        "    e = {k: v for k, v in os.environ.items()}\n"
        "async def d():\n    await launch(['x'], env=None)\n    await p.spawn(['x'])\n"
        "    import claude_tg_bot.procs\n    await claude_tg_bot.procs.spawn(['x'])\n", encoding="utf-8")
    got = [(f, w) for f, _l, w in _spawn_calls(src) if w in ("env-inherit", "env-copy")]
    assert got == [("a", "env-inherit"), ("a", "env-inherit"), ("a", "env-inherit"),
                   ("b", "env-inherit"), ("c", "env-copy"), ("d", "env-inherit"), ("d", "env-inherit"),
                   ("d", "env-inherit")]


def test_spawn_barrier_sees_import_forms(tmp_path) -> None:
    """Преграда видит формы, которые пишут по ошибке: импорт имени, псевдоним,
    метод цикла, копию окружения в переменной."""
    src = tmp_path / "m.py"
    src.write_text(
        "import os, asyncio\nimport subprocess as sp\nfrom subprocess import run\n"
        "from asyncio import create_subprocess_exec as cse\n"
        "def a():\n    run(['x'])\n    sp.Popen(['x'])\n"
        "async def b(loop):\n    await cse('x')\n    await asyncio.subprocess.create_subprocess_exec('x')\n"
        "    await loop.subprocess_exec(None, 'x')\n"
        "def c():\n    env = {**os.environ, 'A': '1'}\n    e2 = os.environ.copy()\n"
        "    ok = {'PATH': os.environ['PATH']}\n    other.run()\n"
        "class K:\n    def m(self):\n        os.system('x')\n", encoding="utf-8")
    got = [(f, w) for f, _l, w in _spawn_calls(src) if w != "env-inherit"]
    assert got == [("a", "subprocess.run"), ("a", "subprocess.Popen"),
                   ("b", "asyncio.create_subprocess_exec"), ("b", "asyncio.subprocess.create_subprocess_exec"),
                   ("b", "subprocess_exec"), ("c", "env-copy"), ("c", "env-copy"), ("K.m", "os.system")]


if __name__ == "__main__":
    unittest.main()

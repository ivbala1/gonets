"""Секреты вне окружения агента, /event с untrusted и проверкой chat_id."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import approval, claude_runner, config  # noqa: E402
from claude_tg_bot.skills import _oneshot  # noqa: E402


class AgentEnvTests(unittest.TestCase):
    def test_known_and_dotenv_secrets_removed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_text("FOO_TOKEN=x\nSOME_PASSWORD=y\nCLAUDE_BIN=/bin/claude\n")
            base = {
                "TELEGRAM_BOT_TOKEN": "t", "APPROVE_SHARED_SECRET": "s",
                "OPENAI_API_KEY": "k", "IMMICH_API_KEY": "k", "GEMINI_API_KEY": "k",
                "FOO_TOKEN": "x", "SOME_PASSWORD": "y",
                "CLAUDE_BIN": "/bin/claude", "PATH": "/usr/bin", "HOME": "/home/u",
                # Чужой токен не из .env бота остаётся: его может ждать сам CLI.
                "GH_TOKEN": "g",
            }
            env = config.agent_env(base, env_file=env_file)
        self.assertEqual(env, {"CLAUDE_BIN": "/bin/claude", "PATH": "/usr/bin",
                               "HOME": "/home/u", "GH_TOKEN": "g"})

    def test_missing_env_file_is_fine(self) -> None:
        env = config.agent_env({"TELEGRAM_BOT_TOKEN": "t", "A": "b"},
                               env_file=Path("/nonexistent/.env"))
        self.assertEqual(env, {"A": "b"})

    def test_oneshot_child_has_no_bot_token(self) -> None:
        proc = MagicMock(returncode=0)
        proc.communicate.return_value = (b"", b"")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret"}), \
                patch.object(_oneshot.subprocess, "Popen", return_value=proc) as popen:
            _oneshot.run_claude_oneshot(["claude"], input=b"", timeout=5, cwd="/")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", popen.call_args.kwargs["env"])


class _Spawned(Exception):
    pass


class RunClaudeSpawnTests(unittest.TestCase):
    def _spawn(self, hook_settings_path: Path = Path("/x.json"), **kw) -> tuple[tuple, dict]:
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"], captured["kwargs"] = args, kwargs
            raise _Spawned

        async def go():
            store = MagicMock()
            store.get.return_value = None
            with patch.object(claude_runner, "_ensure_hook_still_valid"), \
                    patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()), \
                    patch.object(claude_runner.asyncio, "create_subprocess_exec", fake_exec), \
                    patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret"}):
                with self.assertRaises(_Spawned):
                    await claude_runner.run_claude(
                        "claude", "1", 1, None, "hi", store,
                        cwd=Path("/"), hook_settings=hook_settings_path, **kw,
                    )

        asyncio.run(go())
        return captured["args"], captured["kwargs"]

    def test_env_without_token(self) -> None:
        _, kwargs = self._spawn()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", kwargs["env"])

    def test_ordinary_run_keeps_bash(self) -> None:
        args, _ = self._spawn()
        self.assertNotIn("Bash", args)
        self.assertIn("Workflow", args)

    @staticmethod
    def _values(args: tuple, flag: str) -> list[str]:
        if flag not in args:
            return []
        out = []
        for a in args[args.index(flag) + 1:]:
            if a.startswith("-"):
                break
            out.append(a)
        return out

    def _spawn_untrusted(self) -> tuple[tuple, dict]:
        base = {"permissions": {"deny": ["Read(~/.ssh/**)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash|mcp__.*",
                                          "hooks": [{"type": "command", "command": "hook"}]}]}}
        with tempfile.TemporaryDirectory() as d:
            hook = Path(d) / "hook.json"
            hook.write_text(json.dumps(base))
            args, kwargs = self._spawn(untrusted_sids=set(), hook_settings_path=hook)
            settings = Path(args[args.index("--settings") + 1])
            self.assertNotEqual(settings, hook)
            kwargs["settings_data"] = json.loads(settings.read_text())
            settings.unlink()
        return args, kwargs

    def test_untrusted_run_keeps_toolset(self) -> None:
        """Набор инструментов как у обычного рана: иначе промах кэша (19.10.2026)."""
        ordinary, _ = self._spawn()
        args, _ = self._spawn_untrusted()
        self.assertNotIn("--tools", args)
        self.assertEqual(self._values(args, "--disallowedTools"), self._values(ordinary, "--disallowedTools"))
        allowed = self._values(args, "--allowedTools")
        self.assertEqual(allowed, list(claude_runner.UNTRUSTED_ALLOWED_TOOLS))
        # Grep и Glob в --allowedTools добавляют инструменты, которых нет у обычного рана.
        self.assertFalse({"Grep", "Glob"} & set(allowed))
        self.assertNotIn("--allowedTools", ordinary)

    def test_untrusted_run_locks_by_ask(self) -> None:
        args, kwargs = self._spawn_untrusted()
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        data = kwargs["settings_data"]
        ask = set(data["permissions"]["ask"])
        for t in ("Bash", "Monitor", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch",
                  "WebSearch", "Task", "Agent", "Skill", "mcp__telegram__send_message",
                  "mcp__claude_ai_Gmail__create_draft", "mcp__claude_ai_Gmail__send_message"):
            self.assertIn(t, ask)
        for t in ("Read", "ToolSearch", "mcp__telegram__get_messages", "mcp__claude_ai_Gmail",
                  "mcp__claude_ai_Gmail__search_threads", "mcp__telegram"):
            self.assertNotIn(t, ask)
        # Запреты чтения секретов и хук бота остаются, к ним проверка на все инструменты.
        self.assertEqual(data["permissions"]["deny"], ["Read(~/.ssh/**)"])
        pre = data["hooks"]["PreToolUse"]
        self.assertEqual(pre[0]["matcher"], "Bash|mcp__.*")
        guard = pre[-1]
        self.assertEqual(guard["matcher"], "*")
        cmd = guard["hooks"][0]["command"]
        self.assertIn("-m claude_tg_bot.untrusted", cmd)
        self.assertTrue(cmd.endswith("|| exit 2"))
        # Сбой и таймаут любого хука здесь отказ.
        self.assertEqual({h.get("onFailure") for e in pre for h in e["hooks"]}, {"block"})

    def test_untrusted_settings_stable_by_content(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            hook = Path(d) / "hook.json"
            hook.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
            first = claude_runner.untrusted_settings_path(hook)
            self.assertEqual(claude_runner.untrusted_settings_path(hook), first)
            hook.write_text(json.dumps({"hooks": {"PreToolUse": []}, "x": 1}))
            second = claude_runner.untrusted_settings_path(hook)
            self.assertNotEqual(second, first)
            # Прежний файл остаётся: им может пользоваться идущий ран.
            self.assertTrue(first.exists() and second.exists())
            self.assertEqual(oct(second.stat().st_mode & 0o777), "0o600")

    def test_untrusted_ask_covers_former_disallowed(self) -> None:
        """Всё, что до 19.10.2026 снимал --disallowedTools, закрыто и теперь."""
        former = ("Bash", "Monitor", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch",
                  "WebSearch", "Task", "Agent", "Artifact", "ScheduleWakeup", "CronCreate",
                  "CronDelete", "RemoteTrigger", "PushNotification", "SendMessage", "Skill", "Workflow")
        self.assertFalse(set(former) - set(claude_runner.UNTRUSTED_ASK_TOOLS))

    def test_untrusted_hook_settings_unreadable_refuses(self) -> None:
        async def go():
            store = MagicMock()
            store.get.return_value = None
            spawn = AsyncMock()
            with patch.object(claude_runner, "_ensure_hook_still_valid"), \
                    patch.object(claude_runner.asyncio, "create_subprocess_exec", spawn):
                result = await claude_runner.run_claude(
                    "claude", "1", 1, None, "hi", store, cwd=Path("/"),
                    hook_settings=Path("/nonexistent/hook.json"), untrusted_sids=set(),
                )
            spawn.assert_not_called()
            return result

        self.assertTrue(asyncio.run(go()).is_error)


class UntrustedInitGuardTests(unittest.TestCase):
    # Встроенные инструменты окружения бота на 19.10.2026 (событие init).
    TOOLS_1019 = [
        "Task", "Artifact", "ArtifactComments", "ArtifactData", "Bash", "CronCreate", "CronDelete",
        "CronList", "DesignSync", "Edit", "EnterWorktree", "ExitWorktree", "ListAgents",
        "ListMcpResourcesTool", "Monitor", "NotebookEdit", "PushNotification", "Read",
        "ReadMcpResourceDirTool", "ReadMcpResourceTool", "RemoteTrigger", "ReportFindings",
        "ScheduleWakeup", "SendMessage", "Skill", "TaskStop", "ToolSearch", "WebFetch",
        "WebSearch", "Write", "mcp__telegram__send_message", "mcp__new_server__do_it",
    ]

    def test_known_tools_pass(self) -> None:
        self.assertEqual(claude_runner.untrusted_unknown_tools(self.TOOLS_1019), [])

    def test_new_builtin_blocks(self) -> None:
        self.assertEqual(claude_runner.untrusted_unknown_tools([*self.TOOLS_1019, "Teleport"]), ["Teleport"])
        for bad in (None, [], [{"name": "Teleport"}], "Bash"):
            self.assertTrue(claude_runner.untrusted_unknown_tools(bad), bad)

    def _run(self, tools: list[str] | None, untrusted: bool):
        async def go():
            proc = MagicMock(returncode=0, pid=123456789)
            proc.stdin.drain = AsyncMock()
            proc.wait = AsyncMock(return_value=0)
            proc.stdout = asyncio.StreamReader()
            events = [{"type": "result", "result": "ок", "session_id": "s1"}]
            if tools is not None:
                events.insert(0, {"type": "system", "subtype": "init", "tools": tools})
            for evt in events:
                proc.stdout.feed_data((json.dumps(evt) + "\n").encode())
            proc.stdout.feed_eof()
            proc.stderr = asyncio.StreamReader()
            proc.stderr.feed_eof()
            store = MagicMock()
            store.get.return_value = None
            with patch.object(claude_runner, "_ensure_hook_still_valid"), \
                    patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()), \
                    patch.object(claude_runner, "untrusted_settings_path", return_value=Path("/u.json")), \
                    patch.object(claude_runner, "kill_process_tree") as kill, \
                    patch.object(claude_runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
                result = await claude_runner.run_claude(
                    "claude", "1", 1, None, "hi", store, cwd=Path("/"), hook_settings=Path("/x.json"),
                    untrusted_sids=set() if untrusted else None,
                )
            return result, kill

        return asyncio.run(go())

    def test_untrusted_run_with_unlocked_builtin_stops(self) -> None:
        result, kill = self._run([*self.TOOLS_1019, "Teleport"], untrusted=True)
        self.assertTrue(result.is_error)
        self.assertIn("Teleport", result.text)
        kill.assert_called_once()

    def test_untrusted_run_with_known_tools_answers(self) -> None:
        result, kill = self._run(self.TOOLS_1019, untrusted=True)
        self.assertFalse(result.is_error)
        self.assertEqual(result.text, "ок")
        kill.assert_not_called()

    def test_ordinary_run_ignores_guard(self) -> None:
        result, kill = self._run([*self.TOOLS_1019, "Teleport"], untrusted=False)
        self.assertFalse(result.is_error)
        kill.assert_not_called()

    def test_untrusted_run_without_init_stops(self) -> None:
        result, kill = self._run(None, untrusted=True)
        self.assertTrue(result.is_error)
        kill.assert_called_once()

    def test_untrusted_run_with_bad_tool_list_stops(self) -> None:
        for bad in ([], [{"name": "Teleport"}]):
            result, kill = self._run(bad, untrusted=True)
            self.assertTrue(result.is_error, bad)
            kill.assert_called_once()


class UntrustedGuardHookTests(unittest.TestCase):
    """Независимая проверка (python -m claude_tg_bot.untrusted): только чтение."""

    RE = re.compile(config.DEFAULT_MCP_AUTO_ALLOW_REGEX)

    def test_decisions(self) -> None:
        from claude_tg_bot import untrusted
        allowed = ("Read", "ToolSearch", "mcp__telegram__get_messages", "mcp__claude_ai_Gmail__search_threads",
                   "mcp__claude_ai_Google_Calendar__list_events")
        denied = ("Bash", "Write", "Edit", "WebFetch", "Task", "Agent", "Skill", "CronCreate", "TaskStop",
                  "Teleport", "mcp__telegram__send_message", "mcp__claude_ai_Gmail__create_draft",
                  "mcp__claude_ai_Google_Drive__search_files", "mcp__new_server__delete_items",
                  # Чужой сервер закрыт и для «чтения»: query(sql=...) по имени не отличить от записи.
                  "mcp__new_server__list_items", "mcp__db__query",
                  "mcp__telegram__get_invite_link", "mcp__x", "")
        for t in allowed:
            self.assertTrue(untrusted.tool_allowed(t, self.RE), t)
        for t in denied:
            self.assertFalse(untrusted.tool_allowed(t, self.RE), t)
        self.assertFalse(untrusted.tool_allowed("mcp__telegram__get_messages", None))

    def _main(self, stdin: str) -> str:
        from claude_tg_bot import untrusted
        out = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(stdin)), patch.object(sys, "stdout", out), \
                patch.dict(os.environ, {"APPROVE_MCP_AUTO_ALLOW_REGEX": config.DEFAULT_MCP_AUTO_ALLOW_REGEX}):
            self.assertEqual(untrusted.main(), 0)
        return out.getvalue()

    def test_main_read_is_silent(self) -> None:
        self.assertEqual(self._main(json.dumps({"tool_name": "Read"})), "")

    def test_main_denies_action_and_garbage(self) -> None:
        for stdin in (json.dumps({"tool_name": "Bash"}), "not json", json.dumps({})):
            out = json.loads(self._main(stdin))
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", stdin)


def _event_request(payload: dict, *, allowed_chats=frozenset({-100}), users=frozenset({1})):
    cfg = SimpleNamespace(
        approve_shared_secret="", primary_chat_id=min(users),
        allowed_chat_ids=allowed_chats, allowed_user_ids=users,
    )
    wake = AsyncMock()
    application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5))),
        bot_data={"event_wake": wake},
    )
    request = SimpleNamespace(
        app={"cfg": cfg, "application": application}, headers={},
        json=AsyncMock(return_value=payload), remote="127.0.0.1",
    )
    return request, wake, application


class HandleEventTests(unittest.TestCase):
    def _call(self, payload: dict, **kw):
        request, wake, application = _event_request(payload, **kw)

        async def go():
            resp = await approval.handle_event(request)
            # Дать задаче пробуждения стартовать.
            await asyncio.sleep(0)
            return resp

        resp = asyncio.run(go())
        return resp, wake, application

    def test_foreign_chat_rejected(self) -> None:
        resp, wake, app = self._call({"text": "x", "chat_id": 999, "wake": True})
        self.assertEqual(resp.status, 403)
        app.bot.send_message.assert_not_awaited()
        wake.assert_not_called()

    def test_allowed_group_and_private_chat_accepted(self) -> None:
        for chat in (-100, 1):
            with self.subTest(chat=chat):
                resp, _, _ = self._call({"text": "x", "chat_id": chat})
                self.assertEqual(resp.status, 200)

    def test_default_chat_is_primary(self) -> None:
        resp, _, app = self._call({"text": "x"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(app.bot.send_message.await_args.kwargs["chat_id"], 1)

    def test_untrusted_passed_to_wake(self) -> None:
        resp, wake, _ = self._call({"text": "x", "wake": True, "untrusted": True})
        self.assertEqual(resp.status, 200)
        self.assertIs(wake.call_args.kwargs["untrusted"], True)

    def test_without_untrusted_full_rights(self) -> None:
        _, wake, _ = self._call({"text": "x", "wake": True})
        self.assertIs(wake.call_args.kwargs["untrusted"], False)

    def test_untrusted_must_be_bool(self) -> None:
        resp, wake, app = self._call({"text": "x", "wake": True, "untrusted": "true"})
        self.assertEqual(resp.status, 400)
        app.bot.send_message.assert_not_awaited()
        wake.assert_not_called()


class HandleApproveUntrustedTests(unittest.TestCase):
    def _decide(self, tool_name: str, tool_input: dict, *, untrusted: bool) -> dict:
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(
                approve_shared_secret="", primary_chat_id=1, state_dir=Path(d),
                # Чат с автоаппрувом: untrusted обязан сработать раньше него.
                approve_auto_chat_keys=frozenset({"1"}),
                mcp_auto_allow_re=re.compile(r"(get|list|search)[A-Za-z_]*"),
            )
            bot_data = {
                "unattended_sids": {"sid"},
                "untrusted_sids": {"sid"} if untrusted else set(),
            }
            request = SimpleNamespace(
                app={"cfg": cfg, "application": SimpleNamespace(bot_data=bot_data),
                     "registry": MagicMock(), "session_targets": {"sid": (1, None)}},
                headers={}, remote="127.0.0.1",
                json=AsyncMock(return_value={
                    "tool_name": tool_name, "tool_input": tool_input,
                    "session_id": "sid", "permission_mode": "bypassPermissions",
                }),
            )
            resp = asyncio.run(approval.handle_approve(request))
            return json.loads(resp.text)

    def test_untrusted_denies_bash_even_in_auto_chat(self) -> None:
        self.assertEqual(self._decide("Bash", {"command": "ls"}, untrusted=True)["decision"], "deny")

    def test_untrusted_denies_mutating_mcp(self) -> None:
        out = self._decide("mcp__slack__send_message", {"text": "x"}, untrusted=True)
        self.assertEqual(out["decision"], "deny")

    def test_untrusted_allows_read_only_mcp(self) -> None:
        out = self._decide("mcp__telegram__get_messages", {}, untrusted=True)
        self.assertEqual(out["decision"], "allow")

    def test_trusted_run_unchanged(self) -> None:
        self.assertEqual(self._decide("Bash", {"command": "ls"}, untrusted=False)["decision"], "allow")


class HandleApproveWebFetchTests(unittest.TestCase):
    """WebFetch без карточки только на публичное доменное имя, как curl."""

    def _decide(self, url: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(
                approve_shared_secret="", primary_chat_id=1, state_dir=Path(d),
                approve_auto_chat_keys=frozenset(), approve_whitelist_paths=(),
                mcp_auto_allow_re=None,
            )
            request = SimpleNamespace(
                app={"cfg": cfg, "application": SimpleNamespace(bot_data={}),
                     "registry": approval.ApprovalRegistry(), "session_targets": {"sid": (1, None)}},
                headers={}, remote="127.0.0.1",
                json=AsyncMock(return_value={
                    "tool_name": "WebFetch", "tool_input": {"url": url, "prompt": "p"},
                    "session_id": "sid", "permission_mode": "bypassPermissions",
                }),
            )
            # Карточка: отправка падает, повторы без пауз, итог deny.
            with patch.object(approval, "send_approval_request", AsyncMock(side_effect=OSError)), \
                    patch.object(approval.asyncio, "sleep", AsyncMock()):
                resp = asyncio.run(approval.handle_approve(request))
            return json.loads(resp.text)

    def test_public_url_passes(self) -> None:
        self.assertEqual(self._decide("https://api.open-meteo.com/v1/forecast?x=1"),
                         {"decision": "allow", "reason": "webfetch:auto"})

    def test_local_and_ip_urls_go_to_card(self) -> None:
        for url in ("http://127.0.0.1:8788/health", "http://localhost/x", "http://192.168.1.50/",
                    "http://127.0.0.1.nip.io/", "file:///home/u/.env", "http://router.lan/"):
            with self.subTest(url=url):
                self.assertEqual(self._decide(url)["reason"], "telegram send failed")


class SharedSecretWarningTests(unittest.TestCase):
    def test_empty_secret_is_logged(self) -> None:
        with self.assertLogs(approval.log, "WARNING") as cm:
            self.assertTrue(approval.warn_if_no_shared_secret(SimpleNamespace(approve_shared_secret="")))
        self.assertIn("APPROVE_SHARED_SECRET", cm.output[0])

    def test_set_secret_is_quiet(self) -> None:
        self.assertFalse(approval.warn_if_no_shared_secret(SimpleNamespace(approve_shared_secret="s")))


def _hook_settings_path() -> Path:
    """Настройки хука репозитория; в открытой копии есть только пример."""
    root = Path(__file__).resolve().parents[1]
    path = root / "claude-hook-settings.json"
    return path if path.exists() else root / "claude-hook-settings.example.json"


class HookSettingsDenyEnvTests(unittest.TestCase):
    def test_settings_deny_reading_env_files(self) -> None:
        path = _hook_settings_path()
        deny = json.loads(path.read_text())["permissions"]["deny"]
        for rule in ("Read(//**/.env)", "Read(//**/.env.*)",
                     "Read(~/.claude/.credentials.json)", "Read(~/.ssh/**)"):
            self.assertIn(rule, deny)
        config._validate_hook_settings(path)

    def test_matcher_without_monitor_refuses_start(self) -> None:
        """Monitor запускает shell-команду: без него в matcher хук его не видит."""
        path = _hook_settings_path()
        data = json.loads(path.read_text())
        data["hooks"]["PreToolUse"][0]["matcher"] = "Bash|Write|Edit|MultiEdit|NotebookEdit|WebFetch|mcp__.*"
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "s.json"
            bad.write_text(json.dumps(data))
            with self.assertRaises(RuntimeError) as cm:
                config._validate_hook_settings(bad)
        self.assertIn("Monitor", str(cm.exception))

    def test_approve_hook_without_on_failure_block_refuses_start(self) -> None:
        """Хук, который упал до ответа или снят по таймауту, без onFailure block
        пропускает действие без проверки (проверено на 2.1.303 19.10.2026)."""
        path = _hook_settings_path()
        root = Path(__file__).resolve().parents[1]
        for good in (path, root / "public" / "claude-hook-settings.example.json"):
            if good.exists():
                config._validate_hook_settings(good)
        for value in (None, "allow", "Block"):
            data = json.loads(path.read_text())
            hook = data["hooks"]["PreToolUse"][0]["hooks"][0]
            if value is None:
                del hook["onFailure"]
            else:
                hook["onFailure"] = value
            with self.subTest(onFailure=value), tempfile.TemporaryDirectory() as d:
                bad = Path(d) / "s.json"
                bad.write_text(json.dumps(data))
                with self.assertRaises(RuntimeError) as cm:
                    config._validate_hook_settings(bad)
                self.assertIn("onFailure", str(cm.exception))
        # Проверяется каждый хук подтверждений, а не первый: вторая запись
        # без block тоже отказ (ревью Codex 19.10.2026).
        data = json.loads(path.read_text())
        second = json.loads(json.dumps(data["hooks"]["PreToolUse"][0]))
        second["matcher"] = "Bash"
        del second["hooks"][0]["onFailure"]
        data["hooks"]["PreToolUse"].append(second)
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "s.json"
            bad.write_text(json.dumps(data))
            with self.assertRaises(RuntimeError) as cm:
                config._validate_hook_settings(bad)
            self.assertIn("onFailure", str(cm.exception))

    def _validate_changed(self, change) -> None:
        """Проверить настройки репозитория, изменённые change(entry, hook)."""
        data = json.loads(_hook_settings_path().read_text())
        entry = data["hooks"]["PreToolUse"][0]
        change(entry, entry["hooks"][0])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text(json.dumps(data))
            config._validate_hook_settings(path)

    def test_approve_hook_must_be_strict(self) -> None:
        """Имя модуля в команде не значит, что хук решает: `|| true` делает
        сбой разрешением, async, once и if выключают его для части вызовов
        (ревью Codex 19.10.2026)."""
        module = config.HOOK_APPROVE_MODULE
        commands = (f"/v/bin/python -m {module} || true", f"/v/bin/python -m {module}; exit 0",
                    f"echo {module}", f'bash -c "python -m {module}"',
                    f"/v/bin/python -m {module} --x", f"/v/bin/node -m {module}")
        for command in commands:
            with self.subTest(command=command), self.assertRaises(RuntimeError) as cm:
                self._validate_changed(lambda e, h: h.update(command=command))
            self.assertIn("строгой форме", str(cm.exception))
        extras = {"async": True, "asyncRewake": True, "once": True, "if": "Bash(git *)",
                  "args": [], "shell": "bash", "type": "prompt"}
        for key, value in extras.items():
            with self.subTest(key=key), self.assertRaises(RuntimeError) as cm:
                self._validate_changed(lambda e, h: h.update({key: value}))
            self.assertIn("строгой форме", str(cm.exception))
        for command in (f"python3 -m {module}", f"'/a b/.venv/bin/python3.14' -m {module}"):
            with self.subTest(command=command):
                self._validate_changed(lambda e, h: h.update(command=command, statusMessage="ok"))

    def test_match_all_matcher_accepted(self) -> None:
        """Пустой, отсутствующий и "*" matcher CLI понимает как все
        инструменты (код 2.1.303): это не повод отказать в старте."""
        for value in ("*", "", ".*", None):
            def change(entry, _hook, value=value):
                if value is None:
                    del entry["matcher"]
                else:
                    entry["matcher"] = value
            with self.subTest(matcher=value):
                self._validate_changed(change)


if __name__ == "__main__":
    unittest.main()

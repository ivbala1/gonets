from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from claude_tg_bot.bot import _memory_scope_hint
from claude_tg_bot.claude_runner import run_claude
from claude_tg_bot.config import Project


class _Store:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def remove(self, key: str) -> str | None:
        return self.values.pop(key, None)


def _context(*, scopes: _Store | None = None, topic_names: _Store | None = None):
    bot_data = {}
    if scopes is not None:
        bot_data["scopes"] = scopes
    if topic_names is not None:
        bot_data["topic_names"] = topic_names
    return SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))


class MemoryScopeHintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Project(
            key="beta",
            match=("beta", "бета"),
            append_prompt="project prompt",
        )
        self.cfg = SimpleNamespace(projects=(self.project,))
        self.chat_key = "100:7"

    def test_explicit_scope_has_priority_over_auto_project(self) -> None:
        ctx = _context(
            scopes=_Store({self.chat_key: "Alpha"}),
            topic_names=_Store({self.chat_key: "Beta support"}),
        )

        self.assertEqual(_memory_scope_hint(ctx, self.cfg, self.chat_key), "Alpha")

    def test_empty_or_cleared_scope_falls_back_to_auto_project(self) -> None:
        cleared = _Store({self.chat_key: "Alpha"})
        cleared.remove(self.chat_key)
        for scopes in (_Store({self.chat_key: ""}), cleared):
            with self.subTest(scopes=scopes.values):
                ctx = _context(
                    scopes=scopes,
                    topic_names=_Store({self.chat_key: "Beta support"}),
                )
                self.assertEqual(
                    _memory_scope_hint(ctx, self.cfg, self.chat_key),
                    "beta",
                )

    def test_empty_or_cleared_scope_without_auto_project_is_empty(self) -> None:
        cleared = _Store({self.chat_key: "Alpha"})
        cleared.remove(self.chat_key)
        for scopes in (_Store({self.chat_key: ""}), cleared):
            with self.subTest(scopes=scopes.values):
                ctx = _context(scopes=scopes, topic_names=_Store())
                self.assertEqual(_memory_scope_hint(ctx, self.cfg, self.chat_key), "")


class _FakeStdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    """Отдаёт stream-json события построчно, затем EOF (b'')."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        # _drive читает через readuntil (ревью 11.10.2026); EOF как у StreamReader.
        if not self._lines:
            raise asyncio.IncompleteReadError(b"", None)
        return self._lines.pop(0)


class _FakeStderr:
    async def read(self) -> bytes:
        return b""


class _Process:
    pid = 12345
    returncode = 0

    def __init__(self) -> None:
        payload = {"type": "result", "result": "ok", "session_id": "returned-session"}
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout([json.dumps(payload).encode() + b"\n"])
        self.stderr = _FakeStderr()

    async def wait(self) -> int:
        return 0


class RunClaudeMemoryScopeEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def _spawn_env(self, *, existing_session: str | None, hint: str) -> tuple[dict, tuple]:
        store = _Store({"chat": existing_session} if existing_session else None)
        create_process = AsyncMock(return_value=_Process())

        with (
            patch("claude_tg_bot.claude_runner._ensure_hook_still_valid"),
            patch(
                "claude_tg_bot.claude_runner.ensure_oauth_fresh",
                new=AsyncMock(),
            ),
            patch(
                "claude_tg_bot.claude_runner.asyncio.create_subprocess_exec",
                new=create_process,
            ),
            patch("claude_tg_bot.claude_runner.new_session_id", return_value="fresh-session"),
            patch.dict(os.environ, {"AGENT_MEMORY_SCOPE_HINT": "inherited"}),
        ):
            result = await run_claude(
                "/fake/claude",
                "chat",
                100,
                None,
                "prompt",
                store,
                cwd=Path("/tmp"),
                hook_settings=Path("/tmp/settings.json"),
                memory_scope_hint=hint,
            )

        self.assertEqual(result.text, "ok")
        args, kwargs = create_process.call_args
        return kwargs["env"], args

    async def test_nonempty_hint_is_passed_for_fresh_and_resumed_sessions(self) -> None:
        for existing_session in (None, "resume-session"):
            with self.subTest(existing_session=existing_session):
                env, args = await self._spawn_env(
                    existing_session=existing_session,
                    hint="beta",
                )
                self.assertEqual(env["AGENT_MEMORY_SCOPE_HINT"], "beta")
                expected_flag = "--resume" if existing_session else "--session-id"
                self.assertIn(expected_flag, args)

    async def test_empty_hint_removes_inherited_value_for_fresh_and_resumed_sessions(self) -> None:
        for existing_session in (None, "resume-session"):
            with self.subTest(existing_session=existing_session):
                env, args = await self._spawn_env(
                    existing_session=existing_session,
                    hint="",
                )
                self.assertNotIn("AGENT_MEMORY_SCOPE_HINT", env)
                expected_flag = "--resume" if existing_session else "--session-id"
                self.assertIn(expected_flag, args)


if __name__ == "__main__":
    unittest.main()


class ScopeChangeResetsSessionTests(unittest.IsolatedAsyncioTestCase):
    """Смена скоупа темы обязана сбрасывать сессию Claude.

    С 31.07.2026 роутер памяти закрепляет проект за сессией по первому
    сообщению и внутри сессии его не меняет. Без сброса бот подавал бы один
    проект персоной и переменной окружения, а память сессии оставалась бы от
    прежнего: рамка одного заказчика, факты другого.
    """

    def _ctx(self, locked: bool):
        import asyncio

        lock = asyncio.Lock()
        if locked:
            self.addCleanup(lambda: None)
        sessions = _Store({"chat:1": "sid-old"})
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
            "sessions": sessions, "locks": {"chat:1": lock},
        }))
        return ctx, sessions, lock

    async def test_session_is_dropped_when_scope_changes(self):
        from claude_tg_bot import bot as bot_module

        ctx, sessions, lock = self._ctx(locked=False)
        msg = SimpleNamespace(reply_text=AsyncMock())
        with patch.object(bot_module, "_chat_lock", return_value=lock):
            ok = await bot_module._drop_session_for_scope_change(ctx, "chat:1", msg)
        self.assertTrue(ok)
        self.assertNotIn("chat:1", sessions.values)

    async def test_running_claude_blocks_the_scope_change(self):
        """Молча ронять активный запуск нельзя."""
        from claude_tg_bot import bot as bot_module

        ctx, sessions, lock = self._ctx(locked=False)
        await lock.acquire()
        self.addCleanup(lock.release)
        msg = SimpleNamespace(reply_text=AsyncMock())
        with patch.object(bot_module, "_chat_lock", return_value=lock):
            ok = await bot_module._drop_session_for_scope_change(ctx, "chat:1", msg)
        self.assertFalse(ok)
        self.assertIn("chat:1", sessions.values, "сессия не должна пропасть при отказе")
        msg.reply_text.assert_awaited()


class ScopeFingerprintTests(unittest.TestCase):
    """Сессия возобновляется только пока эффективный scope темы тот же.

    Обработчики /scope и переименования сбрасывают сессию, но могут не
    сработать: занятый чат, рестарт бота. Тогда run_claude сохранял прежний
    session_id обратно, и следующее сообщение получало новую рамку темы при
    старой закреплённой памяти.
    """

    def test_changed_scope_refuses_to_resume(self):
        from claude_tg_bot.claude_runner import resume_target

        sessions = _Store({"chat:1": "sid-old"})
        prints = _Store({"chat:1": "alpha"})
        self.assertIsNone(resume_target(sessions, prints, "chat:1", "beta"))
        self.assertIsNone(sessions.get("chat:1"), "старая сессия обязана быть забыта")
        self.assertEqual(prints.get("chat:1"), "beta")

    def test_same_scope_resumes(self):
        from claude_tg_bot.claude_runner import resume_target

        sessions = _Store({"chat:1": "sid-old"})
        prints = _Store({"chat:1": "alpha"})
        self.assertEqual(resume_target(sessions, prints, "chat:1", "alpha"), "sid-old")

    def test_first_run_records_the_fingerprint(self):
        from claude_tg_bot.claude_runner import resume_target

        sessions = _Store({})
        prints = _Store({})
        self.assertIsNone(resume_target(sessions, prints, "chat:1", "alpha"))
        self.assertEqual(prints.get("chat:1"), "alpha")

    def test_dropping_the_scope_also_refuses_to_resume(self):
        """Снятие пометки это тоже смена скоупа."""
        from claude_tg_bot.claude_runner import resume_target

        sessions = _Store({"chat:1": "sid-old"})
        prints = _Store({"chat:1": "alpha"})
        self.assertIsNone(resume_target(sessions, prints, "chat:1", ""))


class ScheduledJobScopeTests(unittest.TestCase):
    """Плановая задача обязана называть канонический scope.

    У неё нет человека рядом: без scope роутер закрепит личный режим, и
    задача отработает без проектной памяти вообще, ничем это не показав.
    Падать надо ДО запуска claude.
    """

    def test_known_scope_passes(self):
        from claude_tg_bot.skills._oneshot import MEMORY_SCOPES_ENV, require_memory_scope

        with patch.dict(os.environ, {MEMORY_SCOPES_ENV: "alpha, Gamma"}):
            self.assertEqual(require_memory_scope("Alpha"), "alpha")
            self.assertEqual(require_memory_scope("gamma"), "gamma")
            # Нет в списке (например, заказчик в архиве): не канонический.
            with self.assertRaises(SystemExit):
                require_memory_scope("beta")

    def test_unknown_scope_fails_before_launch(self):
        from claude_tg_bot.skills._oneshot import MEMORY_SCOPES_ENV, require_memory_scope

        with patch.dict(os.environ, {MEMORY_SCOPES_ENV: "alpha"}):
            for bad in ("", "   ", "какой-то текст", "поддержка"):
                with self.assertRaises(SystemExit):
                    require_memory_scope(bad)

    def test_no_scopes_configured_fails(self):
        from claude_tg_bot.skills._oneshot import MEMORY_SCOPES_ENV, require_memory_scope

        with patch.dict(os.environ, {MEMORY_SCOPES_ENV: ""}):
            with self.assertRaises(SystemExit):
                require_memory_scope("alpha")


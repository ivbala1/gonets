"""Метка планового рана: oneshot-скилл ставит её в окружение claude, хук
передаёт её боту вместе с permission_mode, иначе бот не отличит такой ран от
неизвестной сессии и пошлёт карточку, которую никто не ждёт."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import hook_approve  # noqa: E402
from claude_tg_bot.skills import _oneshot  # noqa: E402


class OneshotMarksScheduledTests(unittest.TestCase):
    def test_env_has_scheduled_mark(self) -> None:
        proc = MagicMock(returncode=0)
        proc.communicate.return_value = (b"", b"")
        with patch.object(_oneshot.subprocess, "Popen", return_value=proc) as popen:
            result = _oneshot.run_claude_oneshot(["claude"], input=b"", timeout=5, cwd="/")
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(popen.call_args.kwargs["env"][_oneshot.SCHEDULED_ENV], "1")
        self.assertNotIn(_oneshot.SCHEDULED_ENV, os.environ)

    def test_mark_name_is_public_contract(self) -> None:
        # Сторож Домового (~/src/domovoy, domovoy/oneshot.py) ставит эту же
        # переменную своим кодом, без импорта бота. Переименование здесь молча
        # оставит его без решения «без карточки»: менять только вместе.
        self.assertEqual(_oneshot.SCHEDULED_ENV, "CLAUDE_TG_BOT_SCHEDULED")


class HookForwardsModeTests(unittest.TestCase):
    def _run_hook(self, env: dict[str, str]) -> dict:
        cfg = SimpleNamespace(
            approve_http_host="127.0.0.1", approve_http_port=8788,
            approve_shared_secret="", approve_timeout_sec=1,
        )
        stdin = io.StringIO(json.dumps({
            "session_id": "s", "permission_mode": "plan",
            "tool_name": "Bash", "tool_input": {"command": "ls"},
        }))
        response = MagicMock()
        response.json.return_value = {"decision": "deny"}
        with patch.object(hook_approve, "load_config", return_value=cfg), \
                patch.object(hook_approve.httpx, "post", return_value=response) as post, \
                patch.object(sys, "stdin", stdin), \
                patch.object(sys, "stdout", io.StringIO()), \
                patch.dict(os.environ, env):
            if not env:
                os.environ.pop(_oneshot.SCHEDULED_ENV, None)
            self.assertEqual(hook_approve.main(), 0)
        return post.call_args.kwargs["json"]

    def test_scheduled_run_marked(self) -> None:
        body = self._run_hook({_oneshot.SCHEDULED_ENV: "1"})
        self.assertIs(body["scheduled"], True)
        self.assertEqual(body["permission_mode"], "plan")

    def test_ordinary_run_not_marked(self) -> None:
        self.assertIs(self._run_hook({})["scheduled"], False)


if __name__ == "__main__":
    unittest.main()

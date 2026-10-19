"""Тесты сетевого ключа codex: сеть в sandbox открывается только явным
`-c sandbox_workspace_write.network_access=true`, сам по себе workspace-write
её не даёт (проверено на codex-cli 0.144.6: без ключа curl отдаёт 000)."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot.codex_runner import run_codex  # noqa: E402

NET_ON = "sandbox_workspace_write.network_access=true"
NET_OFF = "sandbox_workspace_write.network_access=false"


def _capture_args(**kwargs) -> list[str]:
    """Прогнать run_codex с заглушкой процесса и вернуть argv, с которым
    бот позвал бы codex."""
    seen: list[str] = []

    async def fake_exec(*args, **_kw):
        seen.extend(args)
        proc = mock.MagicMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", None))
        proc.returncode = 0
        proc.wait = mock.AsyncMock(return_value=0)
        return proc

    with mock.patch("asyncio.create_subprocess_exec", new=fake_exec):
        asyncio.run(run_codex("/bin/true", "prompt", cwd=Path("/tmp"), **kwargs))
    return seen


class TestCodexNetworkFlag(unittest.TestCase):
    def test_network_off_by_default(self):
        """Дефолт закрывает сеть ЯВНЫМ false, а не отсутствием ключа: у codex
        0.149.0 workspace-write открывает сеть сам, и молчание означало бы
        право на мутирующий HTTP-запрос без approve-карточки."""
        args = _capture_args()
        self.assertIn(NET_OFF, args)
        self.assertNotIn(NET_ON, args)

    def test_network_flag_added(self):
        args = _capture_args(network_access=True)
        self.assertIn("-c", args)
        self.assertIn(NET_ON, args)
        self.assertNotIn(NET_OFF, args)

    def test_prompt_still_from_stdin(self):
        """Ключ вставлен перед `-`, иначе codex принял бы его за prompt."""
        args = _capture_args(network_access=True)
        self.assertEqual(args[-1], "-")
        self.assertLess(args.index(NET_ON), args.index("-o"))


if __name__ == "__main__":
    unittest.main()


class TestCodexEnvIsolation(unittest.TestCase):
    """Маркеры собственной песочницы codex не должны доезжать до дочернего
    процесса: получив их на входе, codex считает себя вложенным запуском и
    режет возможности (замер 21.08.2026: с CODEX_SANDBOX curl отдаёт 000)."""

    def _child_env(self) -> dict:
        seen: dict = {}

        async def fake_exec(*_a, **kw):
            seen.update(kw.get("env") or {})
            proc = mock.MagicMock()
            proc.communicate = mock.AsyncMock(return_value=(b"", None))
            proc.returncode = 0
            proc.wait = mock.AsyncMock(return_value=0)
            return proc

        with mock.patch.dict(os.environ, {"CODEX_SANDBOX": "workspace-write",
                                          "CODEX_SANDBOX_NETWORK_DISABLED": "1",
                                          "CODEX_PREFIX": "gpt:"}):
            with mock.patch("asyncio.create_subprocess_exec", new=fake_exec):
                asyncio.run(run_codex("/bin/true", "prompt", cwd=Path("/tmp")))
        return seen

    def test_markers_stripped(self):
        env = self._child_env()
        self.assertNotIn("CODEX_SANDBOX", env)
        self.assertNotIn("CODEX_SANDBOX_NETWORK_DISABLED", env)

    def test_rest_of_env_survives(self):
        """Вырезаем только маркеры: остальное окружение (в т.ч. PATH и HOME)
        codex по-прежнему нужно."""
        env = self._child_env()
        self.assertEqual(env.get("CODEX_PREFIX"), "gpt:")
        self.assertIn("PATH", env)

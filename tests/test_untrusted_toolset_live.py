"""Живая сверка: у рана по чужому тексту тот же набор инструментов, что у
обычного рана, иначе промах кэша и весь разговор пишется заново (замер
19.10.2026). Запускает настоящий claude с флагами, которые собирает
run_claude, читает событие init и гасит процесс. Нужны claude и вход в него:
CLAUDE_TG_LIVE_TOOLSET=1 .venv/bin/python -m pytest -q tests/test_untrusted_toolset_live.py"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_tg_bot import claude_runner, config

pytestmark = pytest.mark.skipif(os.environ.get("CLAUDE_TG_LIVE_TOOLSET") != "1",
                                reason="живой запуск claude: CLAUDE_TG_LIVE_TOOLSET=1")


class _Spawned(Exception):
    pass


def _bot_args(cfg, **kw) -> list[str]:
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        raise _Spawned

    async def go():
        store = MagicMock()
        store.get.return_value = None
        with patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()), \
                patch.object(claude_runner.asyncio, "create_subprocess_exec", fake_exec):
            with pytest.raises(_Spawned):
                await claude_runner.run_claude(
                    cfg.claude_bin, "live", 1, None, "ок", store, cwd=cfg.claude_cwd,
                    hook_settings=cfg.hook_settings_path, persona=cfg.persona_text,
                    model="haiku", **kw,
                )

    asyncio.run(go())
    return captured["args"]


def _init_tools(args: list[str], cwd: Path) -> list[str]:
    proc = subprocess.Popen(
        [*args, "--no-session-persistence"], cwd=cwd, env=claude_runner.agent_env(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # init приходит за секунды; зависший claude гасится, тест падает.
    timer = threading.Timer(120, os.killpg, (proc.pid, signal.SIGKILL))
    timer.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write("ок".encode())
        proc.stdin.close()
        for line in proc.stdout:
            evt = json.loads(line)
            if evt.get("type") == "system" and evt.get("subtype") == "init":
                return list(evt.get("tools") or [])
        raise AssertionError("claude не прислал init")
    finally:
        timer.cancel()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)


def test_untrusted_toolset_matches_ordinary() -> None:
    cfg = config.load_config()
    ordinary = _init_tools(_bot_args(cfg), cfg.claude_cwd)
    untrusted_args = _bot_args(cfg, untrusted_sids=set())
    untrusted = _init_tools(untrusted_args, cfg.claude_cwd)
    assert ordinary, "пустой набор инструментов"
    assert untrusted == ordinary
    assert claude_runner.untrusted_unknown_tools(untrusted) == []

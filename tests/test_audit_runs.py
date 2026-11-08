"""Аудит 10.2026, задание runs: запуски claude, сессии тем и итог фоновой работы."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot import claude_runner as runner  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult  # noqa: E402
from tests.test_review_bot_main import GROUP, _Base  # noqa: E402


class FreshSessionKeptAfterErrorTests(_Base):
    """Первый ран новой сессии (первое сообщение в теме, после idle-TTL,
    /reset, «делай») оборвался: таймаут, /cancel, сбой API, рестарт бота.
    run_claude пишет sid в store только при удачном итоге, и следующее
    сообщение начинало разговор без контекста сделанной работы."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.error_text = "⛔ claude убит по таймауту (3600s)"
        self.requests = 5
        self.during = None  # async () -> None, внутри рана

        async def fake_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            existing = store.get(chat_key)
            sid = existing or f"sid-{len(self.runs)}"
            if kw.get("on_session_start") is not None:
                kw["on_session_start"](sid)
            self.runs.append({"resumed": existing, "prompt": prompt, "sid": sid})
            if self.during is not None:
                await self.during()
            if len(self.runs) == 1:
                return ClaudeResult(text=self.error_text, session_id=sid, is_error=True,
                                    requests=self.requests)
            store.set(chat_key, sid)
            return ClaudeResult(text="ok", session_id=sid)

        p = patch.object(botmod, "run_claude", fake_run)
        p.start()
        self.addCleanup(p.stop)

    async def test_timeout_keeps_session(self) -> None:
        await self.dispatch("долгая работа")
        await self.dispatch("продолжай")
        self.assertEqual(self.runs[1]["resumed"], self.runs[0]["sid"])

    async def test_cancel_keeps_session(self) -> None:
        self.error_text = "⛔ отменено (signal -15)"
        await self.dispatch("долгая работа")
        await self.dispatch("нет, сделай иначе")
        self.assertEqual(self.runs[1]["resumed"], self.runs[0]["sid"])

    async def test_no_requests_no_session(self) -> None:
        # До модели ран не дошёл: сессии на диске нет, --resume был бы протухшим.
        self.requests = 0
        await self.dispatch("x")
        await self.dispatch("y")
        self.assertIsNone(self.runs[1]["resumed"])

    async def test_reset_during_run_wins(self) -> None:
        # /reset force посреди рана: сброс не откатывается.
        async def reset() -> None:
            self.during = None
            self.store.remove(str(GROUP))
            botmod._bump_session_epoch(self.ctx, str(GROUP))

        self.during = reset
        self.error_text = "⛔ отменено (signal -15)"
        await self.dispatch("долгая работа")
        await self.dispatch("с чистого листа")
        self.assertIsNone(self.runs[1]["resumed"])

    async def test_untrusted_run_not_kept(self) -> None:
        # Ран по чужому тексту идёт в своей сессии: в сессию темы его sid не ложится.
        foreign = MagicMock()
        foreign.get.return_value = None
        self.bot_data["foreign_sessions"] = foreign
        await self.dispatch("письмо", untrusted=True)
        self.assertIsNone(self.store.get(str(GROUP)))


def _fake_proc(events: list[dict], returncode: int) -> MagicMock:
    lines = [json.dumps(e).encode() + b"\n" for e in events] + [b""]
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = returncode
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = MagicMock()
    proc.stdout.readuntil = AsyncMock(side_effect=lines)
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=returncode)
    return proc


async def _run(proc: MagicMock) -> ClaudeResult:
    store = MagicMock()
    store.get.return_value = None

    async def spawn(*_a, **_kw):
        return proc

    with (patch.object(runner, "_ensure_hook_still_valid"),
          patch.object(runner, "ensure_oauth_fresh", AsyncMock()),
          patch.object(runner, "agent_env", return_value={}),
          patch.object(runner.asyncio, "create_subprocess_exec", spawn)):
        return await runner.run_claude("claude", "1", 1, None, "x", store,
                                       cwd=Path("/tmp"), hook_settings=Path("/tmp/fake"))


_ASSISTANT = {"type": "assistant", "uuid": "u1",
              "message": {"id": "m1", "usage": {"input_tokens": 10}, "content": []}}


def test_cancelled_run_reports_requests():
    # Отмена после запросов к модели: бот по requests решает, что сессия есть на диске.
    result = asyncio.run(_run(_fake_proc([_ASSISTANT], -15)))
    assert result.is_error
    assert result.requests == 1


def test_stream_without_result_is_error_without_raw_json():
    # claude вышел с 0 без события result: сырой NDJSON Ивану не показывается,
    # и такой ответ не считается удачным (не становится проектом задачи).
    result = asyncio.run(_run(_fake_proc([_ASSISTANT], 0)))
    assert result.is_error
    assert '"type"' not in result.text


class _SlowWire:
    """Telegram отвечает по 10 с (таймауты, как 23.10): часы идут во время запроса."""

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.calls = 0

    async def send_message(self, **_kw):
        self.calls += 1
        self.clock[0] += 10
        return MagicMock(message_id=1)

    async def edit_message_text(self, **_kw):
        self.calls += 1
        self.clock[0] += 10


def _status(clock: list[float], wire) -> "botmod._RunStatus":
    return botmod._RunStatus(wire, GROUP, None, clock=lambda: clock[0])


def test_slow_telegram_does_not_edit_on_every_event():
    # Чтение потока claude ждёт правку статуса. Окно троттлинга считалось от
    # начала запроса: при запросе дольше 3 с каждое следующее событие снова
    # ждало Telegram, и конец рана отставал на число событий, умноженное на
    # время запроса (замер: 300 событий по 20 мс сдвинули конец на 6 с).
    async def go() -> int:
        clock = [1000.0]
        wire = _SlowWire(clock)
        status = _status(clock, wire)
        for i in range(10):
            await status.on_tool(f"Read: f{i}.py")
        return wire.calls
    assert asyncio.run(go()) == 1


def test_tool_event_does_not_wait_for_tick_send():
    # Тик держит замок статуса, пока шлёт тревогу и весточку (до 30 с каждая):
    # событие инструмента ждало его, и с ним чтение потока claude.
    async def go() -> None:
        clock = [1000.0]
        status = _status(clock, _SlowWire(clock))
        async with status._lock:
            await asyncio.wait_for(status.on_tool("Bash: ls"), timeout=1)
        assert status._steps == 1 and status._desc == "Bash: ls"
    asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

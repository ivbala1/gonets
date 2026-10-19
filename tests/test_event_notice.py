"""/event с notice=false: карточки «🔔» нет, первым в чате ответ рана
(утреннее пробуждение Домового). Без пробуждения флаг не принимается, а при
недоступной фабрике пробуждения карточка уходит всё равно."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import approval  # noqa: E402


def _call(payload: dict, *, factory: bool = True):
    cfg = SimpleNamespace(
        approve_shared_secret="", primary_chat_id=1,
        allowed_chat_ids=frozenset(), allowed_user_ids=frozenset({1}),
    )
    wake = AsyncMock()
    application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5))),
        bot_data={"event_wake": wake} if factory else {},
    )
    request = SimpleNamespace(
        app={"cfg": cfg, "application": application}, headers={},
        json=AsyncMock(return_value=payload), remote="127.0.0.1",
    )

    async def go():
        resp = await approval.handle_event(request)
        await asyncio.sleep(0)
        return resp

    return asyncio.run(go()), wake, application


class EventNoticeTests(unittest.TestCase):
    def test_no_notice_with_wake(self) -> None:
        resp, wake, app = _call({"title": "Утро", "wake": True, "prompt": "p", "notice": False})
        self.assertEqual(resp.status, 200)
        app.bot.send_message.assert_not_awaited()
        wake.assert_called_once()
        self.assertEqual(wake.call_args.args[2], "p")
        self.assertIsNone(json.loads(resp.text)["message_id"])
        # Учёт принятого события передан задаче пробуждения и снят ею.
        self.assertEqual(app.bot_data["admitted_events"], 0)

    def test_default_sends_notice(self) -> None:
        resp, wake, app = _call({"title": "Вечер", "wake": True, "prompt": "p"})
        self.assertEqual(resp.status, 200)
        app.bot.send_message.assert_awaited_once()
        self.assertEqual(json.loads(resp.text)["message_id"], 5)
        wake.assert_called_once()

    def test_no_notice_requires_wake(self) -> None:
        for payload in ({"title": "x", "notice": False},
                        {"title": "x", "wake": False, "notice": False}):
            with self.subTest(payload=payload):
                resp, wake, app = _call(payload)
                self.assertEqual(resp.status, 400)
                app.bot.send_message.assert_not_awaited()
                wake.assert_not_called()

    def test_notice_must_be_bool(self) -> None:
        for bad in ("false", 0, None):
            with self.subTest(notice=bad):
                resp, wake, app = _call({"title": "x", "wake": True, "notice": bad})
                self.assertEqual(resp.status, 400)
                app.bot.send_message.assert_not_awaited()
                wake.assert_not_called()

    def test_notice_kept_when_wake_unavailable(self) -> None:
        resp, _wake, app = _call(
            {"title": "Утро", "wake": True, "notice": False, "silent": True}, factory=False)
        self.assertEqual(resp.status, 202)
        app.bot.send_message.assert_awaited_once()
        self.assertIs(app.bot.send_message.await_args.kwargs["disable_notification"], True)


if __name__ == "__main__":
    unittest.main()

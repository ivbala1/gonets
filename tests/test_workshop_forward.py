"""Звук итога мастерской в личку (сторож билетов Домового, 05.11.2026): итог
с меткой <<LOUD>> приходит со звуком, без метки остаётся тихим, сбой со звуком."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_tg_bot import bot as botmod  # noqa: E402
from test_review_bot_main import GROUP, _Base, _msg  # noqa: E402

WS = f"{GROUP}:77"


class WorkshopForwardSoundTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.workshop_chat_key = WS
        self.cfg.primary_chat_id = 1
        self.ctx.bot.send_message = AsyncMock()

    def _forwards(self) -> list:
        return [c for c in self.ctx.bot.send_message.await_args_list if c.args and c.args[0] == 1]

    async def test_loud_marker_forwards_with_sound(self) -> None:
        self.answer = "готово: билеты ниже порога\n1 015 EUR, 01.12\n<<LOUD>>"
        await self.dispatch("наряд", msg=_msg(thread=77), unattended=True)
        sent = self._forwards()
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0].kwargs["disable_notification"])
        self.assertIn("билеты ниже порога", sent[0].args[1])
        self.assertNotIn("<<LOUD>>", sent[0].args[1])

    async def test_without_marker_stays_quiet(self) -> None:
        self.answer = "готово: нашёл 3 вакансии"
        await self.dispatch("наряд", msg=_msg(thread=77), unattended=True)
        sent = self._forwards()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].kwargs["disable_notification"])

    async def test_direct_call_flags(self) -> None:
        # Внутри тихого рана (поиск, наряд): обёртка бота _SilentAwareBot глушит
        # любую отправку при _SILENT_RUN, поэтому проверяется и флаг в момент отправки.
        cfg = SimpleNamespace(telegram_text_limit=4000, primary_chat_id=1)
        silent_at_send: list[bool] = []
        self.ctx.bot.send_message = AsyncMock(side_effect=lambda *a, **k: silent_at_send.append(
            botmod._SILENT_RUN.get()))
        outer = botmod._SILENT_RUN.set(True)
        try:
            for failed, loud, quiet in ((False, False, True), (False, True, False), (True, False, False)):
                silent_at_send.clear()
                await botmod._workshop_forward(self.ctx, cfg, WS, 77, "готово", failed, loud=loud)
                self.assertIs(self.ctx.bot.send_message.await_args.kwargs["disable_notification"], quiet)
                self.assertEqual(silent_at_send, [quiet])
                self.assertTrue(botmod._SILENT_RUN.get())   # флаг рана восстановлен
        finally:
            botmod._SILENT_RUN.reset(outer)

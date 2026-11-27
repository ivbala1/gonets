"""Звук итога мастерской в личку (сторож билетов Домового, 05.11.2026): итог
с меткой <<LOUD>> приходит со звуком, без метки остаётся тихим, сбой со звуком."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult  # noqa: E402
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

    async def test_quiet_marker_with_text_stays_in_topic(self) -> None:
        # Подготовка утра Домового 26.11.2026: отчёт и метка в конце.
        self.answer = "готово: подготовка утра 26.11\nВопроса Ивану нет.\n\n<<QUIET>>"
        await self.dispatch("наряд", msg=_msg(thread=77), unattended=True)
        self.assertEqual(self._forwards(), [])
        self.send_reply_text.assert_awaited()   # в теме итог остаётся
        self.assertIn("подготовка утра", self.send_reply_text.await_args.args[2])

    async def test_quiet_marker_with_failure_still_forwards(self) -> None:
        async def failed_run(*_a, **_kw):
            return ClaudeResult(text="не вышло: поиск оборвался\n<<QUIET>>", session_id="s1", is_error=True)
        with patch.object(botmod, "run_claude", failed_run):
            await self.dispatch("наряд", msg=_msg(thread=77), unattended=True)
        sent = self._forwards()
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0].kwargs["disable_notification"])

    async def test_quiet_marker_with_loud_still_forwards(self) -> None:
        self.answer = "готово: цена ниже порога\n<<QUIET>>\n<<LOUD>>"
        await self.dispatch("наряд", msg=_msg(thread=77), unattended=True)
        self.assertEqual(len(self._forwards()), 1)

    async def test_quiet_marker_with_ask_still_forwards(self) -> None:
        self.answer = "готово: подготовка\n<<QUIET>>\n<<ASK: Ставить бассейн? | да | нет>>"
        await self.dispatch("наряд", msg=_msg(thread=77), unattended=True)
        self.assertEqual(len(self._forwards()), 1)

    async def test_quiet_marker_after_button_still_forwards(self) -> None:
        # ✅ отклика идёт рана по кнопке: итог для Ивана единственное подтверждение.
        self.answer = "готово: отправлено\n<<QUIET>>"
        await self.dispatch("[✅]", msg=_msg(thread=77), from_callback=True)
        self.assertEqual(len(self._forwards()), 1)

    async def test_quiet_marker_with_codex_fallback_still_forwards(self) -> None:
        self.cfg.codex_bin = "codex"
        self.cfg.codex_fallback = True
        self.result_error = True
        with patch.object(botmod, "_is_claude_unavailable", lambda r: True), \
                patch.object(botmod, "_codex_as_claude_result",
                             AsyncMock(return_value=ClaudeResult(text="готово: подготовка\n<<QUIET>>"))) as codex:
            await self.dispatch("наряд", msg=_msg(thread=77), unattended=True)
        codex.assert_awaited()
        self.assertEqual(len(self._forwards()), 1)

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

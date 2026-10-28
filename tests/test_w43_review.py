"""Разбор недели помощника W43 (решение Ивана 25.10.2026): строка «⏳ Висит»
дописывает бот (1), тот же вопрос кнопками не повторяется, пока прежний ждёт
ответа (3), второй закреп «Вопрос дня» за дату получает отказ (7)."""
from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot import hangs, notify  # noqa: E402
from test_review_bot_main import GROUP, _Base, _msg  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402
from tests.texts import msg_re  # noqa: E402

TODAY = date(2026, 10, 25)
SRO = ("# Срочное\n\nСтрока `- ГГГГ-ММ-ДД ЧЧ:ММ | что | закреп N`.\n\n"
       "- 2026-10-25 12:29 | ответить рекрутеру Acme до вечера | закреп 501\n"
       "- 2026-10-24 18:05 | подписать оффер\n")


class HangLinesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "srochno.md"

    def test_lines_from_file(self) -> None:
        self.path.write_text(SRO, encoding="utf-8")
        self.assertEqual(hangs.hang_lines(self.path, TODAY), [
            "⏳ Висит: ответить рекрутеру Acme до вечера (с 12:29)",
            "⏳ Висит: подписать оффер (с 24.10 18:05)",
        ])

    def test_empty_or_missing_file(self) -> None:
        self.assertEqual(hangs.hang_lines(self.path, TODAY), [])
        self.path.write_text("# Срочное\n\nСтрока `- ГГГГ-ММ-ДД ЧЧ:ММ | что`.\n", encoding="utf-8")
        self.assertEqual(hangs.hang_lines(self.path, TODAY), [])

    def test_model_line_replaced_not_doubled(self) -> None:
        reply = "Готово.\n⏳ Висит: ответить рекрутеру с 12:29\n"
        out = hangs.with_hangs(reply, ["⏳ Висит: x (с 12:29)"])
        self.assertEqual(out, "Готово.\n\n⏳ Висит: x (с 12:29)")

    def test_list_and_bold_model_lines_removed_and_cap(self) -> None:
        out = hangs.with_hangs("ок\n- ⏳ Висит: a\n**⏳ Висит**: b", ["⏳ Висит: x (с 12:29)"])
        self.assertEqual(out, "ок\n\n⏳ Висит: x (с 12:29)")
        self.path.write_text("".join(f"- 2026-10-25 1{i}:00 | дело {i}\n" for i in range(8)), encoding="utf-8")
        lines = hangs.hang_lines(self.path, TODAY)
        self.assertEqual(len(lines), hangs.MAX_LINES + 1)
        self.assertEqual(lines[-1], "⏳ Висит: и ещё 3")

    def test_empty_reply_stays_empty(self) -> None:
        self.assertEqual(hangs.with_hangs("", ["⏳ Висит: x (с 12:29)"]), "")
        self.assertEqual(hangs.with_hangs("ок", []), "ок")


class HangsDispatchTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cfg.hangs_file = self.state / "srochno.md"
        self.cfg.hangs_chat_keys = frozenset({str(GROUP)})

    def _sent(self) -> str:
        return self.send_reply_text.await_args.args[2]

    async def test_reply_ends_with_hangs(self) -> None:
        self.cfg.hangs_file.write_text("- 2026-10-25 12:29 | ответить рекрутеру\n", encoding="utf-8")
        self.answer = "План на вечер готов."
        await self.dispatch("что дальше")
        sent = self._sent()
        self.assertTrue(sent.startswith("План на вечер готов."))
        self.assertTrue(sent.endswith(")"))
        self.assertIn("⏳ Висит: ответить рекрутеру (с ", sent)

    async def test_ask_only_reply_gets_hangs(self) -> None:
        self.cfg.hangs_file.write_text("- 2026-10-25 12:29 | ответить рекрутеру\n", encoding="utf-8")
        self.answer = "<<ASK: Отправлять? | да | нет>>"
        with patch.object(botmod, "_send_ask_card", AsyncMock()) as card:
            await self.dispatch("x")
        self.assertTrue(self._sent().startswith("⏳ Висит: ответить рекрутеру"))
        card.assert_awaited_once()

    async def test_voice_reply_hangs_sent_last_as_text(self) -> None:
        self.cfg.hangs_file.write_text("- 2026-10-25 12:29 | ответить\n", encoding="utf-8")
        self.answer = "Голосом ответ."
        voice = AsyncMock(return_value=True)
        with patch.object(botmod, "_send_voice_series", voice), \
                patch.dict(self.bot_data, {"openai": object()}):
            await self.dispatch("x", send_voice_reply=True)
        voice.assert_awaited_once()
        self.assertEqual(voice.await_args.args[-1], "Голосом ответ.")
        self.assertTrue(self._sent().startswith("⏳ Висит: ответить"))

    async def test_workshop_gets_no_hangs(self) -> None:
        self.cfg.workshop_chat_key = f"{GROUP}:77"
        self.cfg.hangs_chat_keys = frozenset({f"{GROUP}:77"})
        self.cfg.primary_chat_id = 1
        self.cfg.hangs_file.write_text("- 2026-10-25 12:29 | ответить\n", encoding="utf-8")
        self.answer = "ок"
        await self.dispatch("x", msg=_msg(thread=77))
        self.assertNotIn("Висит", self._sent())

    async def test_other_chat_and_empty_file_untouched(self) -> None:
        self.answer = "ок"
        await self.dispatch("x")
        self.assertEqual(self._sent(), "ок")
        self.cfg.hangs_file.write_text("- 2026-10-25 12:29 | ответить\n", encoding="utf-8")
        self.cfg.hangs_chat_keys = frozenset({"999"})
        await self.dispatch("x")
        self.assertEqual(self._sent(), "ок")


class AskRepeatTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.cards: list[tuple] = []

        async def card(ctx, update, chat_key, ask, **kw):
            self.cards.append(ask)
            pending = ctx.application.bot_data.setdefault("ask_options", {})
            for i, opt in enumerate(ask[1]):
                pending[f"t{len(self.cards)}{i}"] = {
                    "chat_key": chat_key, "answer": opt, "question": ask[0], "card": f"c{len(self.cards)}",
                    "at": time.time(), "msg": (GROUP, 100 + len(self.cards), None),
                    "epoch": botmod._session_epoch(ctx, chat_key),
                    "permission_mode": kw["permission_mode"], "untrusted": kw.get("untrusted", False),
                }
        p = patch.object(botmod, "_send_ask_card", card)
        p.start()
        self.addCleanup(p.stop)

    async def test_waiting_question_not_repeated(self) -> None:
        self.answer = "Смотрю.\n<<ASK: Отправлять? | да | нет>>"
        await self.dispatch("a")
        await self.dispatch("b")
        self.assertEqual(len(self.cards), 1)
        self.assertIn(_("❓ Вопрос с кнопками выше ещё ждёт ответа."), self.send_reply_text.await_args.args[2])

    async def test_other_options_or_answered_or_old_sent_again(self) -> None:
        self.answer = "<<ASK: Отправлять? | да | нет>>"
        await self.dispatch("a")
        self.answer = "<<ASK: Отправлять? | да | нет | позже>>"
        await self.dispatch("b")
        self.assertEqual(len(self.cards), 2)
        pending = self.bot_data["ask_options"]
        for v in pending.values():
            v["at"] -= botmod.ASK_REPEAT_HOLD_SEC + 1
        await self.dispatch("c")
        self.assertEqual(len(self.cards), 3)
        await self.dispatch("e")
        self.assertEqual(len(self.cards), 3)   # свежая карточка ждёт
        pending.clear()  # нажали: реестр пуст
        await self.dispatch("d")
        self.assertEqual(len(self.cards), 4)


class DailyPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(primary_chat_id=111, telegram_bot_token="T", telegram_text_limit=4096,
                                   state_dir=Path(self.tmp.name))
        self.mid = 700

    def _post(self, url, json=None, timeout=None):
        self.mid += 1
        return SimpleNamespace(status_code=200, text="ok",
                               json=lambda: {"ok": True, "result": {"message_id": self.mid}})

    def _send(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(notify, "load_config", return_value=self.cfg), \
                patch.object(notify.httpx, "post", side_effect=self._post) as post, \
                redirect_stdout(out), redirect_stderr(err):
            rc, mid = notify.send_message(list(argv))
        return rc, mid, out.getvalue(), err.getvalue(), post.call_count

    def test_second_question_of_day_refused(self) -> None:
        rc, mid, *_ = self._send("--silent", "--pin", "Вопрос дня: что делает kubectl drain?")
        self.assertEqual((rc, mid), (0, 701))
        rc, mid, out, err, calls = self._send("--silent", "--pin", "Вопрос дня: другой")
        self.assertEqual((rc, mid, calls), (1, 701, 0))
        self.assertIn("message_id: 701", out)
        self.assertRegex(err, msg_re("notify: «{prefix}» за {day} уже отправлен и закреплён, второй не шлю"))

    def test_other_pins_and_failed_send_not_counted(self) -> None:
        self.assertEqual(self._send("--pin", "⚠️ Срочно: x")[0], 0)
        self.assertEqual(self._send("--pin", "⚠️ Срочно: y")[0], 0)
        bad = SimpleNamespace(status_code=500, text="err", json=lambda: {})
        with patch.object(notify, "load_config", return_value=self.cfg), \
                patch.object(notify.httpx, "post", return_value=bad), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(notify.send_message(["--pin", "Вопрос дня: q"])[0], 1)
        self.assertEqual(self._send("--pin", "Вопрос дня: q")[0], 0)

    def test_without_pin_no_guard(self) -> None:
        self._send("--pin", "Вопрос дня: q")
        self.assertEqual(self._send("Вопрос дня: q")[0], 0)


if __name__ == "__main__":
    unittest.main()

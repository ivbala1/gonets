"""Карточка подтверждения: заголовок отправки людям, лимит Telegram,
экранирование описания, полный вид. Оценки опасности по разбору команды нет
(решение Ивана 04.11.2026: замка «только чтение» нет, карточка только на
отправку людям)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot.approval import (  # noqa: E402
    _format_tool_call,
    full_view_text,
    needs_full_view,
)
from claude_tg_bot.bot import _ask_button_label  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402

HEADER = _("📤 <b>отправка людям от твоего имени</b>: проверь адресата и текст")


class CardLimitTests(unittest.TestCase):
    def test_card_never_exceeds_telegram_limit(self) -> None:
        path = "/tmp/" + "/".join(["a" * 100] * 25)
        card = _format_tool_call("Write", {"file_path": path, "content": "<" * 2000}, "s")
        self.assertLessEqual(len(card), 4096)
        self.assertTrue(card.startswith(HEADER))

    def test_full_view_keeps_extra_fields(self) -> None:
        text = full_view_text("Edit", {"file_path": "/a", "old_string": "x", "new_string": "y",
                                       "replace_all": True})
        self.assertIn('"replace_all": true', text)


class CardTests(unittest.TestCase):
    def test_card_order_header_then_agent_note_then_command(self) -> None:
        card = _format_tool_call("Bash", {"command": "rm -rf ~/x", "description": "проверка бэкапа"}, "abcdef123")
        lines = card.split("\n")
        self.assertEqual(HEADER, lines[0])
        self.assertIn(_("💬 <i>пояснение агента:</i> {desc}").format(desc="проверка бэкапа"), lines[2])
        self.assertIn("<pre>rm -rf ~/x</pre>", card)

    def test_card_escapes_and_caps_description(self) -> None:
        card = _format_tool_call("Bash", {"command": "ls", "description": "<b>x</b>" * 100}, None)
        self.assertNotIn("<b>x</b>", card)
        self.assertLess(len(card), 900)

    def test_card_has_only_send_header_no_danger_line(self) -> None:
        """решение Ивана 04.11.2026: замка «только чтение» нет, карточка только на отправку людям"""
        card = _format_tool_call("Bash", {"command": "ls"}, None)
        self.assertEqual(HEADER, card.split("\n")[0])
        for mark in ("🔴", "🟠", "🟡", "⚪"):
            self.assertNotIn(mark, card)
        self.assertIn(_("💬 <i>пояснения агента нет</i>"), card)

    def test_full_view(self) -> None:
        big = {"command": "echo " + "x" * 3000, "description": "d"}
        self.assertTrue(needs_full_view(big))
        self.assertFalse(needs_full_view({"command": "ls"}))
        self.assertIn("x" * 3000, full_view_text("Bash", big))


class AskButtonTests(unittest.TestCase):
    def test_label_numbered_and_capped(self) -> None:
        self.assertEqual(_ask_button_label(1, "Да"), "1 · Да")
        long = _ask_button_label(2, "Через файл, общие правила на карточке")
        self.assertTrue(long.startswith("2 · Через файл"))
        self.assertTrue(long.endswith("…"))
        self.assertLessEqual(len(long), 28)


if __name__ == "__main__":
    unittest.main()

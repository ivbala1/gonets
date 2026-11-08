"""Аудит guard (ноябрь 2026): отправка через плагины почты и Slack остаётся отправкой людям.
Остальное (распознавание чтения, теги плана) удалено решением Ивана 04.11.2026."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import outbound  # noqa: E402


class OutboundPluginTests(unittest.TestCase):
    def test_plugin_gmail_and_slack_send_is_outbound(self) -> None:
        # Сервер плагина называется plugin_<плагин>_<сервер> (как
        # plugin_pdf-viewer_pdf в untrusted.py).
        for tool in ("mcp__plugin_engineering_gmail__send_email",
                     "mcp__plugin_engineering_slack__slack_send_message",
                     "mcp__plugin_operations_gmail__reply_to_email"):
            with self.subTest(tool=tool):
                self.assertTrue(outbound.is_send(tool))
        for tool in ("mcp__plugin_engineering_gmail__search_emails",
                     "mcp__plugin_telegram_telegram__reply", "mcp__plugin_pdf-viewer_pdf__display_pdf"):
            with self.subTest(tool=tool):
                self.assertFalse(outbound.is_send(tool))


if __name__ == "__main__":
    unittest.main()

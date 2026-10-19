"""Forced command ключа скиллов: запускается только скилл бота, аргументы
уходят списком, shell-синтаксис в них остаётся текстом."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.remote_skill import PYTHON, resolve  # noqa: E402


class ResolveTests(unittest.TestCase):
    def test_skill_with_quoted_argument(self) -> None:
        self.assertEqual(
            resolve('chat_digest --chat "IT Jobs Chat"'),
            [str(PYTHON), "-m", "claude_tg_bot.skills.chat_digest", "--chat", "IT Jobs Chat"],
        )

    def test_shell_syntax_stays_argument_text(self) -> None:
        argv = resolve("chat_digest --chat 'x; rm -rf ~' '$(id)' '`id`'")
        self.assertEqual(argv[3:], ["--chat", "x; rm -rf ~", "$(id)", "`id`"])
        # Неэкранированная `;` тоже просто слово: shell здесь нет.
        self.assertEqual(resolve("chat_digest ; rm -rf ~")[3:], [";", "rm", "-rf", "~"])

    def test_only_existing_skills(self) -> None:
        for command in (
            "", "   ", "rm -rf ~", "_oneshot", "beta_os_update",
            "chat_digest.core", "../bot", "Chat_digest", "invoice_beta",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    resolve(command)

    def test_unclosed_quote_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve("chat_digest --chat 'oops")


if __name__ == "__main__":
    unittest.main()

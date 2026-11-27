"""Forced command ключа скиллов: запускается только скилл бота или модуль
Домового из явного списка, аргументы уходят списком, shell-синтаксис в них
остаётся текстом."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import remote_skill  # noqa: E402
from claude_tg_bot.remote_skill import (  # noqa: E402
    DOMOVOY_ROOT, PYTHON, REPO_ROOT, resolve,
)

# Открытая копия гоняет тесты без Домового: его python подменён на
# существующий файл, отказ без него проверяет отдельный тест.
FAKE_DOMOVOY_PYTHON = Path(sys.executable)


class _WithDomovoy(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(remote_skill, "DOMOVOY_PYTHON", FAKE_DOMOVOY_PYTHON)
        patcher.start()
        self.addCleanup(patcher.stop)


class ResolveTests(_WithDomovoy):
    def test_bot_skill_with_quoted_argument(self) -> None:
        self.assertEqual(
            resolve('yt_subs "https://example.com/v?x=1" --lang "ru,en"'),
            ([str(PYTHON), "-m", "claude_tg_bot.skills.yt_subs", "https://example.com/v?x=1", "--lang", "ru,en"],
             REPO_ROOT),
        )

    def test_chat_digest_goes_to_domovoy(self) -> None:
        """Имя для мака прежнее, модуль Домового, его python без каталога запуска в sys.path."""
        self.assertEqual(
            resolve('chat_digest --chat "IT Jobs Chat"'),
            ([str(FAKE_DOMOVOY_PYTHON), "-P", "-m", "domovoy.chat_digest", "--chat", "IT Jobs Chat"], DOMOVOY_ROOT),
        )

    def test_missing_domovoy_is_a_refusal(self) -> None:
        with patch.object(remote_skill, "DOMOVOY_PYTHON", Path("/nonexistent/python")), \
                self.assertRaises(ValueError):
            resolve("chat_digest --help")

    def test_shell_syntax_stays_argument_text(self) -> None:
        argv, _cwd = resolve("chat_digest --chat 'x; rm -rf ~' '$(id)' '`id`'")
        self.assertEqual(argv[4:], ["--chat", "x; rm -rf ~", "$(id)", "`id`"])
        # Неэкранированная `;` тоже просто слово: shell здесь нет.
        self.assertEqual(resolve("chat_digest ; rm -rf ~")[0][4:], [";", "rm", "-rf", "~"])
        self.assertEqual(resolve("yt_subs ; rm -rf ~")[0][3:], [";", "rm", "-rf", "~"])

    def test_only_existing_skills(self) -> None:
        for command in (
            "", "   ", "rm -rf ~", "_oneshot", "beta_os_update", "watch",
            "chat_digest.core", "domovoy.chat_digest", "domovoy", "../bot", "Chat_digest", "invoice_beta",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    resolve(command)

    def test_unclosed_quote_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve("chat_digest --chat 'oops")


class MainTests(_WithDomovoy):
    def _main(self, command: str, extra: dict | None = None):
        seen: dict = {}
        env = {"SSH_ORIGINAL_COMMAND": command, "PATH": "/usr/bin:/bin", "LC_ALL": "ru_RU.KOI8-R", **(extra or {})}

        def fake_exec(path, argv):
            seen.update(path=path, argv=argv, env=dict(os.environ))
            raise SystemExit(0)

        with patch.dict(os.environ, env, clear=True), \
                patch.object(remote_skill.os, "execv", fake_exec), \
                patch.object(remote_skill.os, "chdir", lambda d: seen.update(cwd=d)):
            try:
                rc = remote_skill.main()
            except SystemExit:
                rc = None
        return rc, seen

    def test_domovoy_exec_env_and_cwd(self) -> None:
        rc, seen = self._main("chat_digest --dry-run")
        self.assertIsNone(rc)
        self.assertEqual(seen["cwd"], DOMOVOY_ROOT)
        self.assertEqual(seen["argv"][:4], [str(FAKE_DOMOVOY_PYTHON), "-P", "-m", "domovoy.chat_digest"])
        self.assertEqual(seen["path"], str(FAKE_DOMOVOY_PYTHON))
        self.assertNotIn("SSH_ORIGINAL_COMMAND", seen["env"])   # строка команды дальше не идёт
        self.assertEqual(seen["env"]["PYTHONUTF8"], "1")          # локаль с мака не ломает вывод
        self.assertTrue(seen["env"]["PATH"].startswith(str(remote_skill.LOCAL_BIN) + ":"))

    def test_bot_skill_cwd_is_repo(self) -> None:
        _rc, seen = self._main("yt_subs --help")
        self.assertEqual(seen["cwd"], REPO_ROOT)

    def test_unknown_is_rc_2_without_exec(self) -> None:
        rc, seen = self._main("bash")
        self.assertEqual(rc, 2)
        self.assertEqual(seen, {})


if __name__ == "__main__":
    unittest.main()

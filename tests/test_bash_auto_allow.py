"""Живые части бывшего теста автоодобрения bash: разбор сегментов команды
(нужен записи памяти), запись памяти без карточки (черновик и область),
whitelist путей бота, защита локального HTTP от форм из браузера. Распознавания
чтения нет (решение Ивана 04.11.2026: замка «только чтение» нет, карточка
только на отправку людям)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.approval import (  # noqa: E402
    _BOT_ROOT,
    _browser_forgery_reason,
    _scan_bash_segments,
    _whitelist_target_ok,
    memory_write_auto_allowed,
)


class ScanSegmentsTests(unittest.TestCase):
    def test_quoted_pipe_single_quotes_one_segment(self) -> None:
        self.assertEqual(_scan_bash_segments("grep -E 'a|b' file"), ["grep -E 'a|b' file"])

    def test_quoted_pipe_double_quotes_one_segment(self) -> None:
        self.assertEqual(_scan_bash_segments('find . -iname "*a|b*"'), ['find . -iname "*a|b*"'])

    def test_real_pipe_splits(self) -> None:
        self.assertEqual(_scan_bash_segments("ls | wc -l"), ["ls ", " wc -l"])

    def test_escaped_pipe_not_separator(self) -> None:
        self.assertEqual(_scan_bash_segments(r"echo a\|b"), [r"echo a\|b"])

    def test_all_separators_split(self) -> None:
        # Сверяем строки, а не только количество: пропавший разделитель оставил
        # бы хвост внутри сегмента, и счётчик этого не показал бы.
        self.assertEqual(
            _scan_bash_segments("ls && ls1 || ls2; ls3\nls4"),
            ["ls ", " ls1 ", " ls2", " ls3", "ls4"],
        )

    def test_background_separator_splits(self) -> None:
        # `&` отдельным тестом: `ls & rm -f x`: два сегмента, второй под
        # allowlist не подпадает и уходит в карточку.
        self.assertEqual(_scan_bash_segments("ls & rm -f x"), ["ls ", " rm -f x"])

    def test_parens_rejected(self) -> None:
        # `ls () (rm -rf x); ls`: объявление функции ls с телом rm: под
        # allowlist сегмент выглядит как безобидный `ls …`.
        self.assertIsNone(_scan_bash_segments("ls () (rm -f /tmp/victim); ls"))
        self.assertIsNone(_scan_bash_segments("ls (echo x)"))

    def test_variable_expansion_rejected(self) -> None:
        # word splitting подставит значение отдельным словом уже после нашей
        # проверки аргументов: F=-delete превратит `find . $F` в `find . -delete`.
        self.assertIsNone(_scan_bash_segments("find . $F"))
        self.assertIsNone(_scan_bash_segments('find . "$F"'))
        self.assertIsNone(_scan_bash_segments("find . -dele${UNSET}te"))

    def test_dollar_in_single_quotes_literal(self) -> None:
        self.assertEqual(_scan_bash_segments("grep '$HOME' f"), ["grep '$HOME' f"])

    def test_substitution_outside_quotes_rejected(self) -> None:
        self.assertIsNone(_scan_bash_segments("echo $(whoami)"))

    def test_backtick_rejected(self) -> None:
        self.assertIsNone(_scan_bash_segments("echo `whoami`"))

    def test_substitution_in_double_quotes_rejected(self) -> None:
        # В двойных кавычках $() и backtick всё ещё раскрываются bash'ем.
        self.assertIsNone(_scan_bash_segments('echo "$(whoami)"'))

    def test_substitution_in_single_quotes_literal(self) -> None:
        self.assertEqual(_scan_bash_segments("echo '$(whoami)'"), ["echo '$(whoami)'"])

    def test_redirects_rejected(self) -> None:
        self.assertIsNone(_scan_bash_segments("cat a > b"))
        self.assertIsNone(_scan_bash_segments("sort < b"))

    def test_unclosed_quote_rejected(self) -> None:
        self.assertIsNone(_scan_bash_segments("grep 'oops"))


class MemoryWriteAutoAllowTests(unittest.TestCase):
    """Запись памяти без карточки: только черновик из state/memory-drafts и
    только personal или clients/<имя>; global и всё нестандартное на карточку."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.drafts = Path(self._tmp.name) / "memory-drafts"
        self.drafts.mkdir()
        self.draft = str(self.drafts / "rec.md")
        self.mem = "MEMORY_CONFIG_DIR=~/.agent-memory-config ~/src/svod/engine/bin/memory remember"

    def allowed(self, cmd: str):
        return memory_write_auto_allowed(cmd, self.drafts)

    def test_personal_and_client_drafts_pass_with_names(self) -> None:
        calls = self.allowed(
            f"{self.mem} --scope personal --id user_x-20261004 --record user_x "
            f"--source claude-code --session s1 --base abc1234 --json --file {self.draft} 2>&1 | head -3")
        self.assertEqual(calls, [{"scope": "personal", "name": "user_x", "dry_run": ""}])
        calls = self.allowed(
            f"~/src/svod/engine/bin/memory remember --scope=clients/alpha --id k1 --record r "
            f"--section 'Релизы' --line '- [[r]] заметка' --file={self.draft} --dry-run")
        self.assertEqual(calls, [{"scope": "clients/alpha", "name": "r", "dry_run": "1"}])
        calls = self.allowed(
            f"export MEMORY_CONFIG_DIR=~/.agent-memory-config; ~/src/svod/engine/bin/memory remember "
            f"--scope personal --id i --file '{self.draft}' --content-type markdown")
        self.assertEqual(calls, [{"scope": "personal", "name": "i", "dry_run": ""}])

    def test_risky_forms_keep_card(self) -> None:
        d = self.draft
        for cmd in (
            f"{self.mem} --scope global --id x --file {d}",
            f"{self.mem} --scope personal --scope global --id x --file {d}",
            f"{self.mem} --sc global --id x --file {d}",
            f"{self.mem} --scope personal --id x --fi {d}",
            f"{self.mem} --scope clients/../global --id x --file {d}",
            f"{self.mem} --scope personal --id x",
            f"cat ~/.ssh/id_rsa | {self.mem} --scope personal --id x",
            f"{self.mem} --scope personal --id x --file ~/.ssh/id_rsa",
            f"{self.mem} --scope personal --id x --file {self.drafts}/../../.env",
            f"{self.mem} --scope personal --id x --file {self.drafts}",
            f"{self.mem} --scope personal --id x --file memory-drafts/rec.md",
            f"{self.mem} --scope personal --id x --file {self.drafts}/*",
            f"{self.mem} --scope personal --id x --file <(cat <<'EOF'\nтело\nEOF\n)",
            f"{self.mem} --scope personal --id x <<'EOF'\nтело\nEOF",
            f"{self.mem} --scope personal --id x --file {d} extra",
            f"{self.mem} --scope personal --id $X --file {d}",
            f"MEMORY_CONFIG_DIR=/tmp/evil ~/src/svod/engine/bin/memory remember --scope personal --id x --file {d}",
            f"/tmp/src/svod/engine/bin/memory remember --scope personal --id x --file {d}",
            f"~/src/svod/engine/bin/memory search x",
            # Находки ревью 04.10.2026: манифест тянет файлы по своим путям
            # и удаляет записи; `~` без раскрытия bash'ем читает другой файл.
            f"{self.mem} --scope personal --id x --content-type manifest --file {d}",
            f"{self.mem} --scope personal --id x --content-type=manifest --file {d}",
            "cd /home/user/src/claude-tg-bot; " + self.mem
            + " --scope personal --id x --file='~/src/claude-tg-bot/state/memory-drafts/rec.md'",
            self.mem + " --scope personal --id x --file=~/src/claude-tg-bot/state/memory-drafts/rec.md",
            self.mem + " --scope personal --id x --file ~/src/claude-tg-bot/state/memory-drafts/rec.md",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.allowed(cmd))

    def test_other_segments_not_checked(self) -> None:
        """решение Ивана 04.11.2026: замка «только чтение» нет, карточка только на отправку людям"""
        calls = self.allowed(f"{self.mem} --scope personal --id x --file {self.draft}; ls")
        self.assertEqual(calls, [{"scope": "personal", "name": "x", "dry_run": ""}])

    def test_symlink_out_of_drafts_rejected(self) -> None:
        link = self.drafts / "link.md"
        link.symlink_to(Path.home() / ".bashrc")
        self.assertIsNone(self.allowed(f"{self.mem} --scope personal --id x --file {link}"))

    def test_symlink_inside_drafts_rejected(self) -> None:
        """Ссылку можно подменить между проверкой и чтением: не принимаем
        даже ту, что сейчас ведёт внутрь черновиков."""
        target = self.drafts / "real.md"
        target.write_text("x")
        link = self.drafts / "inner.md"
        link.symlink_to(target)
        self.assertIsNone(self.allowed(f"{self.mem} --scope personal --id x --file {link}"))


class BotRepoWhitelistTests(unittest.TestCase):
    """Код и настройки бота не пишутся без карточки даже при whitelist на весь
    репозиторий: правка скилла плюс его вызов без карточки это запуск кода."""

    def test_only_memory_drafts_inside_bot_repo(self) -> None:
        state = _BOT_ROOT / "state"
        self.assertTrue(_whitelist_target_ok(state / "memory-drafts" / "r.md", state))
        self.assertTrue(_whitelist_target_ok(state / "2mind" / "review.txt", state))
        for target in (
            state / "2mind",
            state / "2mindx" / "r.txt",
            state / "2mind" / "sub" / "r.txt",
            state / "2mind" / ".codex" / "config.toml",
            state / "2mind" / ".r.txt",
            state / "2mind" / "r.log",
            Path("/mnt/c/Users/user/!Obsidian/.codex/config.toml"),
            _BOT_ROOT / "src" / "claude_tg_bot" / "skills" / "chat_digest.py",
            _BOT_ROOT / "src" / "claude_tg_bot" / "approval.py",
            _BOT_ROOT / "telegram-persona.md",
            _BOT_ROOT / ".env",
            _BOT_ROOT / ".venv" / "bin" / "python",
            state / "at_jobs.json",
            state / "memory-drafts",
            _BOT_ROOT,
        ):
            with self.subTest(target=target):
                self.assertFalse(_whitelist_target_ok(target, state))

    def test_git_and_obsidian_config_closed(self) -> None:
        # Плагины Obsidian и git-хуки исполняются: это запуск кода.
        state = _BOT_ROOT / "state"
        for target in (
            Path("/mnt/c/Users/user/!Obsidian/.obsidian/plugins/x/main.js"),
            Path("/mnt/c/Users/user/!Obsidian/.obsidian/hotkeys.json"),
            Path("/home/user/src/domovoy/journal/.git/hooks/post-commit"),
        ):
            with self.subTest(target=target):
                self.assertFalse(_whitelist_target_ok(target, state))
        self.assertTrue(_whitelist_target_ok(
            Path("/mnt/c/Users/user/!Obsidian/Машина/service.md"), state))
        self.assertTrue(_whitelist_target_ok(
            Path("/home/user/src/domovoy/journal/2026-10.md"), state))

    def test_paths_outside_bot_repo_unaffected(self) -> None:
        state = _BOT_ROOT / "state"
        for target in (Path("/mnt/c/Users/user/!Obsidian/Геология/x.md"),
                       Path("/home/user/.claude/projects/-home-user-src/memory/a.md"),
                       _BOT_ROOT.parent / "claude-tg-bot-other" / "x.py"):
            with self.subTest(target=target):
                self.assertTrue(_whitelist_target_ok(target, state))


class BrowserForgeryTests(unittest.TestCase):
    def test_own_clients_pass(self) -> None:
        for host in ("127.0.0.1:8788", "localhost:8788", "[::1]:8788", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual(
                    _browser_forgery_reason("POST", "application/json", host, "127.0.0.1"), "")

    def test_non_json_post_rejected(self) -> None:
        # Так шлёт форма или fetch(mode: "no-cors") со страницы в браузере.
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            with self.subTest(ctype=ctype):
                self.assertTrue(
                    _browser_forgery_reason("POST", ctype, "127.0.0.1:8788", "127.0.0.1"))

    def test_rebinding_host_rejected_on_loopback_bind(self) -> None:
        self.assertTrue(
            _browser_forgery_reason("POST", "application/json", "evil.example:8788", "127.0.0.1"))
        self.assertTrue(
            _browser_forgery_reason("POST", "application/json", "127.0.0.1.evil.example", "127.0.0.1"))

    def test_non_loopback_bind_checks_only_content_type(self) -> None:
        # Внешний bind требует секрет, Host там за туннелем любой.
        self.assertEqual(
            _browser_forgery_reason("POST", "application/json", "bot.example", "0.0.0.0"), "")

    def test_get_not_checked(self) -> None:
        self.assertEqual(_browser_forgery_reason("GET", "", "evil.example", "127.0.0.1"), "")


if __name__ == "__main__":
    unittest.main()

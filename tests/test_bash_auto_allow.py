"""Тесты quote-aware сплиттера bash-команд и денилиста мутирующих флагов:
кавычки не должны резаться на ложные сегменты (`grep -E 'a|b'`), а find/fd
с исполняющими флагами (-delete/-exec/--exec) обязаны уходить на карточку."""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.approval import (  # noqa: E402
    _SKILL_PYTHON,
    _bash_auto_allowed,
    _bash_command_is_safe,
    _browser_forgery_reason,
    _scan_bash_segments,
    _segment_args_are_safe,
    _BOT_ROOT,
    _unattended_auto_allowed,
    _whitelist_target_ok,
    memory_write_auto_allowed,
)

# Форма как у APPROVE_BASH_AUTO_ALLOW_REGEX в .env: имя команды + аргументы.
# Список сокращён до команд, которые участвуют в тестах ниже, но включает те,
# у которых есть свои исполняющие флаги (rg, sort, tree, git grep).
_ALLOW = re.compile(
    r"(?:(?:ls|cat|grep|rg|find|fd|echo|wc|head|sort|tree|jq)|git grep)(?: .*)?"
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
        # `&` отдельным тестом: `ls & rm -f x` — два сегмента, второй под
        # allowlist не подпадает и уходит в карточку.
        self.assertEqual(_scan_bash_segments("ls & rm -f x"), ["ls ", " rm -f x"])

    def test_parens_rejected(self) -> None:
        # `ls () (rm -rf x); ls` — объявление функции ls с телом rm: под
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


class DangerousArgsTests(unittest.TestCase):
    def test_every_find_flag_covered(self) -> None:
        # Пофлажно: общий тест на пару флагов не заметил бы пропажу остальных.
        for flag in (
            "-delete", "-exec", "-execdir", "-ok", "-okdir",
            "-fls", "-fprint", "-fprint0", "-fprintf",
        ):
            with self.subTest(flag=flag):
                self.assertFalse(_segment_args_are_safe(f"find . {flag} x"))

    def test_find_exec_family_unsafe(self) -> None:
        self.assertFalse(_segment_args_are_safe("find . -delete"))
        self.assertFalse(_segment_args_are_safe(r"find . -name x -exec rm {} \;"))
        self.assertFalse(_segment_args_are_safe("find . -okdir vi {} ;"))
        self.assertFalse(_segment_args_are_safe("find . -fprint /tmp/out"))

    def test_quoted_and_escaped_flags_still_caught(self) -> None:
        # bash видит один и тот же -delete во всех трёх формах, поэтому
        # денилист снимает токены через shlex, а не сырым split по пробелам.
        for cmd in ("find . '-delete'", 'find . -de"lete"', r"find . -de\lete"):
            with self.subTest(cmd=cmd):
                self.assertFalse(_segment_args_are_safe(cmd))
        self.assertFalse(_segment_args_are_safe("fd pattern . '--exec' rm '{}'"))

    def test_equals_form_caught(self) -> None:
        self.assertFalse(_segment_args_are_safe("sort --compress-program=/tmp/evil f"))
        self.assertFalse(_segment_args_are_safe("sort --output=/tmp/victim f"))

    def test_short_flag_bundle_caught(self) -> None:
        # `sort -uo out` bash разбирает как `-u -o out`.
        self.assertFalse(_segment_args_are_safe("sort -uo /tmp/victim f"))

    def test_exec_flags_of_other_allowlisted_commands(self) -> None:
        for cmd in (
            "rg --pre touch needle /tmp/victim",
            "rg --hostname-bin /tmp/evil pattern",
            "git grep --open-files-in-pager='rm -f /tmp/victim' needle",
            "git grep -O/tmp/evil needle",
            "git log --ext-diff",
            "sort -S 1K --compress-program=/tmp/evil f",
            "sort -o /tmp/victim f",
            "tree -o /tmp/victim .",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(_segment_args_are_safe(cmd))

    def test_unclosed_quote_in_segment_is_unsafe(self) -> None:
        self.assertFalse(_segment_args_are_safe("find . -name 'x"))

    def test_plain_find_safe(self) -> None:
        self.assertTrue(_segment_args_are_safe("find . -name '*.py' -type f"))

    def test_fd_exec_variants_unsafe(self) -> None:
        for cmd in (
            "fd -x rm pat", "fd --exec rm pat", "fd -X rm pat",
            "fdfind --exec-batch rm pat",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(_segment_args_are_safe(cmd))

    def test_fd_plain_safe(self) -> None:
        self.assertTrue(_segment_args_are_safe("fd pattern src"))

    def test_denylist_is_per_command(self) -> None:
        # -delete опасен только у find: у другой команды тот же токен не флаг.
        self.assertTrue(_segment_args_are_safe("echo -delete"))


class BashCommandIsSafeTests(unittest.TestCase):
    def _safe(self, cmd: str) -> bool:
        return _bash_command_is_safe(cmd, _ALLOW)

    def test_quoted_pipes_allowed(self) -> None:
        self.assertTrue(self._safe("grep -E 'a|b' src/x.py"))
        self.assertTrue(self._safe('find . -iname "*a|b*"'))
        self.assertTrue(self._safe("git grep -n 'foo|bar'"))

    def test_pipe_of_allowed_commands(self) -> None:
        self.assertTrue(self._safe("ls | wc -l"))

    def test_pipe_into_disallowed_command(self) -> None:
        self.assertFalse(self._safe("ls | rm -rf /"))

    def test_find_delete_and_exec_denied(self) -> None:
        self.assertFalse(self._safe("find . -delete"))
        self.assertFalse(self._safe(r"find /tmp -name '*.log' -exec rm {} \;"))

    def test_fd_exec_denied(self) -> None:
        self.assertFalse(self._safe("fd --exec rm pat"))

    def test_safe_redirects_stripped(self) -> None:
        self.assertTrue(self._safe("cat missing 2>/dev/null"))
        self.assertTrue(self._safe("ls x 2>&1"))

    def test_real_redirect_denied(self) -> None:
        self.assertFalse(self._safe("cat a > /etc/passwd"))
        self.assertFalse(self._safe("echo x >/dev/null.evil"))

    def test_substitution_denied(self) -> None:
        self.assertFalse(self._safe("echo $(rm -rf /)"))

    def test_function_definition_denied(self) -> None:
        self.assertFalse(self._safe("ls () (rm -f /tmp/victim); ls"))

    def test_variable_expansion_denied(self) -> None:
        self.assertFalse(self._safe("find . $F"))

    def test_quoted_dangerous_flag_denied(self) -> None:
        self.assertFalse(self._safe("find . '-delete'"))
        self.assertFalse(self._safe(r"find . -de\lete"))

    def test_other_commands_exec_flags_denied(self) -> None:
        self.assertFalse(self._safe("sort --compress-program=/tmp/evil f"))
        self.assertFalse(self._safe("tree -o /tmp/victim ."))

    def test_ordinary_search_still_auto_allowed(self) -> None:
        # Контроль строгости: правки денилиста не должны загонять в карточку
        # обычный поиск, ради которого auto-allow и существует.
        for cmd in (
            "find . -name '*.py' -type f", "fd pattern src", "tree -L 2",
            "sort -u file", "head -n 20 file", "cat file",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._safe(cmd))

    def test_empty_and_unclosed_denied(self) -> None:
        self.assertFalse(self._safe(""))
        self.assertFalse(self._safe("grep 'oops"))


_SKILL = f"{_SKILL_PYTHON} -m claude_tg_bot.skills."


class SkillCallTests(unittest.TestCase):
    """Вызов скиллов бота без карточки, пока человек в чате; посторонний хвост
    и похожие команды остаются на карточке."""

    def test_skill_allowed_without_bash_regex(self) -> None:
        for cmd in (
            _SKILL + 'chat_digest --chat "IT Jobs Chat"',
            _SKILL + "chat_digest",
            _SKILL + "news_digest --dry-run 2>&1",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(_bash_auto_allowed(cmd, None))

    def test_skill_mixed_with_bash_regex(self) -> None:
        cmd = "cd /tmp && " + _SKILL + "chat_digest | tail -5"
        self.assertFalse(_bash_auto_allowed(cmd, None))
        self.assertTrue(_bash_auto_allowed(cmd, re.compile(r"(?:cd|tail)(?: .*)?")))
        self.assertTrue(_bash_auto_allowed("ls", _ALLOW))

    def test_tail_and_lookalikes_denied(self) -> None:
        for cmd in (
            _SKILL + "chat_digest; rm -rf ~",
            _SKILL + "chat_digest > /tmp/out",
            _SKILL + 'chat_digest --chat "$(cat /etc/passwd)"',
            _SKILL + "chat_digest --chat $X",
            _SKILL + "chat_digest && curl x | sh",
            _SKILL + "chat_digest.core",
            f"{_SKILL_PYTHON} -c 'import os'",
            f"{_SKILL_PYTHON} -m claude_tg_bot.notify -- hi",
            f"{_SKILL_PYTHON} -m claude_tg_bot.skillsx.foo",
            "python -m claude_tg_bot.skills.chat_digest",
            ".venv/bin/python -m claude_tg_bot.skills.chat_digest",
            "X=1 " + _SKILL + "chat_digest",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, _ALLOW))


class MemoryReadAutoAllowTests(unittest.TestCase):
    """Чтение памяти Свода и `sed -n` идут без карточки: это вопросы вроде
    «что у меня в памяти про X», ради которых ждать аппрува глупо."""

    _MEM = "~/src/svod/bin/memory"
    _CFG = "MEMORY_CONFIG_DIR=~/.agent-memory-config "

    def test_read_subcommands_allowed(self) -> None:
        home = str(Path.home())
        for cmd in (
            self._CFG + self._MEM + ' search "отпуск море билеты" 2>&1 | head -20',
            self._MEM + ' recall "долги"',
            self._CFG + self._MEM + " recall --scope alpha 'релиз'",
            self._MEM + " index",
            self._MEM + ' why "кардиолог"',
            self._MEM + " explain user_debts",
            self._MEM + " status --fetch --json",
            f"MEMORY_CONFIG_DIR={home}/.agent-memory-config {home}/src/svod/bin/memory search x",
            "export MEMORY_CONFIG_DIR=~/.agent-memory-config; " + self._MEM + ' search "x"',
            "sed -n 1,12p ~/.agent-memory/personal/memory/project_domovoy.md",
            "sed -n '40,$p' a.md b.md | head -5",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(_bash_auto_allowed(cmd, _ALLOW))
        # Без списка из .env проходят сами по себе, не только в цепочке.
        self.assertTrue(_bash_auto_allowed(self._MEM + ' search "x" 2>&1', None))
        self.assertTrue(_bash_auto_allowed("sed -n 1,5p a.md", None))

    def test_case_from_telegram_with_env_regex(self) -> None:
        """Команда со скриншота 04.10.2026: память, grep и ls одной цепочкой."""
        env_re = re.compile(r"(?:(?:ls|grep|head|echo)(?: .*)?)")
        cmd = (
            'export MEMORY_CONFIG_DIR=~/.agent-memory-config; ~/src/svod/bin/memory search '
            '"отпуск море билеты отель виза страховка" 2>&1 | head -20; '
            'echo ---; grep -rli -e "отпуск" -e "Отпуск" -e "билет[а-я]* " '
            '~/.agent-memory/personal/memory/ --include=*.md 2>/dev/null | grep -v archive '
            '| head -20; echo ---; ls "/mnt/c/Users/user/!Obsidian/" | head -40'
        )
        self.assertFalse(_bash_auto_allowed(cmd, None))
        # grep -rli по каталогу памяти теперь на карточке, без рекурсии
        # цепочка проходит (решение владельца 14.10.2026).
        self.assertFalse(_bash_auto_allowed(cmd, env_re))
        self.assertTrue(_bash_auto_allowed(cmd.replace("grep -rli", "grep -li"), env_re))

    def test_writes_and_lookalikes_keep_card(self) -> None:
        for cmd in (
            self._MEM + " remember --scope personal --id x --record y --file z",
            self._CFG + self._MEM + " remember --dry-run --file z",
            self._MEM + " status --nudge",
            self._MEM + " status --json --nudge",
            self._MEM + " status --nu",
            self._MEM + ' status --nud""ge',
            self._MEM + " status --fetch x",
            self._MEM + ' "remember" --file z',
            self._MEM + "x search a",
            "~/src/svod/bin/memory-eval probes",
            "/tmp/src/svod/bin/memory search x",
            "MEMORY_CONFIG_DIR=/tmp/evil " + self._MEM + " search x",
            "export MEMORY_CONFIG_DIR=/tmp/evil",
            "export PATH=/tmp/x",
            "PATH=/tmp/x " + self._MEM + " search x",
            self._MEM + " search $(cat /etc/passwd)",
            self._MEM + ' search "x" > /tmp/out',
            self._MEM + " search x; rm -rf ~",
            "sed -i 1d a.md",
            "sed -n '1p;w /tmp/x' a.md",
            "sed -n '1e id' a.md",
            "sed -n '1,2p' a.md > b.md",
            "sed -n 1p -i a.md",
            "sed -n 1p a.md -i",
            "sed -n 1p --in-place=.bak a.md",
            "sed -n 1p -e 'w /tmp/x' a.md",
            "sed -n 1p -f s.sed a.md",
            "sed -n 1p a.md -s -i",
            # Находки ревью 04.10.2026: флаг в кавычках, экранированный,
            # скрипт через -e и раскрытие шаблонов.
            "sed -n '1p' '-i' /tmp/victim",
            "sed -n 1p \\-i /tmp/victim",
            "sed -n 1p '-e' 'w /tmp/out' /etc/hostname",
            "sed -n 1p '-e' '1e touch /tmp/pwned' /etc/hostname",
            "sed -n 1p *",
            "sed -n 1p a?.md",
            "sed -n 1p {-i,a.md}",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, _ALLOW))


class GlobExpansionTests(unittest.TestCase):
    """Шаблон раскрывается после проверки: файл с именем флага в каталоге
    превращает разрешённую команду в запуск программы или перезапись."""

    def test_glob_with_dangerous_flag_commands_keeps_card(self) -> None:
        for cmd in ("rg x *", "find * -name a", "sort *", "tree [a-z]*",
                    "git grep x -- *.py", "rg x {--pre=sh,a}", "fd x ?"):
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, _ALLOW))

    def test_quoted_or_escaped_glob_and_safe_commands_pass(self) -> None:
        # rg по каталогу всегда на карточке, поэтому здесь rg по файлам
        # (решение владельца 14.10.2026).
        with tempfile.TemporaryDirectory() as d:
            for name in ("a.md", "*"):
                (Path(d) / name).write_text("x")
            for cmd in ("rg 'x*' a.md", "find . -name '*.py'", 'rg "a[bc]" a.md',
                        "rg x \\*", "ls *.md", "grep -n x *.py", "cat a?.md"):
                with self.subTest(cmd=cmd):
                    self.assertTrue(_bash_auto_allowed(cmd, _ALLOW, cwd=d))


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
        self.mem = "MEMORY_CONFIG_DIR=~/.agent-memory-config ~/src/svod/bin/memory remember"

    def allowed(self, cmd: str):
        return memory_write_auto_allowed(cmd, _ALLOW, self.drafts)

    def test_personal_and_client_drafts_pass_with_names(self) -> None:
        calls = self.allowed(
            f"{self.mem} --scope personal --id user_x-20261004 --record user_x "
            f"--source claude-code --session s1 --base abc1234 --json --file {self.draft} 2>&1 | head -3")
        self.assertEqual(calls, [{"scope": "personal", "name": "user_x", "dry_run": ""}])
        calls = self.allowed(
            f"~/src/svod/bin/memory remember --scope=clients/alpha --id k1 --record r "
            f"--section 'Релизы' --line '- [[r]] заметка' --file={self.draft} --dry-run")
        self.assertEqual(calls, [{"scope": "clients/alpha", "name": "r", "dry_run": "1"}])
        calls = self.allowed(
            f"export MEMORY_CONFIG_DIR=~/.agent-memory-config; ~/src/svod/bin/memory remember "
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
            f"{self.mem} --scope personal --id x --file {d}; rm -rf ~",
            f"{self.mem} --scope personal --id $X --file {d}",
            f"MEMORY_CONFIG_DIR=/tmp/evil ~/src/svod/bin/memory remember --scope personal --id x --file {d}",
            f"/tmp/src/svod/bin/memory remember --scope personal --id x --file {d}",
            f"~/src/svod/bin/memory search x",
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

    def test_git_output_flag_keeps_card(self) -> None:
        for cmd in ("git diff --no-index --output=/tmp/o a b", "git log --output /tmp/o"):
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, re.compile(r"git (?:diff|log)(?: .*)?")))

    def test_bash_safe_path_never_allows_remember(self) -> None:
        """Ветка без уведомления (bash-safe) запись не пропускает ни в каком виде."""
        cmd = f"{self.mem} --scope personal --id x --file {self.draft}"
        self.assertFalse(_bash_auto_allowed(cmd, _ALLOW))
        self.assertFalse(_bash_auto_allowed(cmd, re.compile(r".*")))


class TwoMindTests(unittest.TestCase):
    """Скилл /2mind без карточки: остаток лимита, имя модели, обёртка ask.py.
    Прямой codex exec и посторонний хвост остаются на карточке."""

    _DIR = "~/.claude/skills/2mind/"

    def test_allowed(self) -> None:
        for cmd in (
            f"python3 {self._DIR}codex_limits.py",
            f"python3 {self._DIR}latest_sol.py",
            f"python3 {self._DIR}latest_sol.py --all",
            f"python3 {Path.home()}/.claude/skills/2mind/codex_limits.py",
            f"python3 {self._DIR}ask.py {_BOT_ROOT}/state/2mind/review.txt",
            f"python3 {self._DIR}ask.py {_BOT_ROOT}/state/2mind/r-1.v2.txt"
            " --effort xhigh --cd ~/src/domovoy 2>&1",
            f"python3 {self._DIR}ask.py {_BOT_ROOT}/state/2mind/r.txt --fallback",
            f"python3 {self._DIR}codex_limits.py; python3 {self._DIR}latest_sol.py",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(_bash_auto_allowed(cmd, None))

    def test_denied(self) -> None:
        two = f"{_BOT_ROOT}/state/2mind"
        for cmd in (
            # Ответ и лог ложатся рядом с запросом: только state/2mind/.
            f"python3 {self._DIR}ask.py /tmp/p.txt",
            f"python3 {self._DIR}ask.py {two}/../at_jobs.txt",
            f"python3 {self._DIR}ask.py {two}/sub/p.txt",
            f"python3 {self._DIR}ask.py {two}/.p.txt",
            f"python3 {self._DIR}ask.py {two}/p.log",
            f"python3 {self._DIR}ask.py {two}/p.txt --effort low",
            f"python3 {self._DIR}ask.py {two}/p.txt --sandbox danger-full-access",
            f"python3 {self._DIR}ask.py {two}/p.txt --cd $HOME",
            f"python3 {self._DIR}ask.py ~/.ssh/id_ed25519",
            f"python3 {self._DIR}ask.py /tmp/p.txt > ~/.bashrc",
            f'python3 {self._DIR}ask.py "$(cat /tmp/p.txt)"',
            f"python3 {self._DIR}ask.py /tmp/p.txt; rm -rf ~",
            f"python3 {self._DIR}other.py",
            f"python3 {self._DIR}ask.pyc /tmp/p.txt",
            f"python3 /tmp/.claude/skills/2mind/ask.py /tmp/p.txt",
            f"python3 -c 'import os' {self._DIR}ask.py",
            "codex-native exec --skip-git-repo-check -m gpt-6.1-sol -o /tmp/a.txt - < /tmp/p.txt",
            "codex exec --sandbox danger-full-access hi",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, None))


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


class SystemAndGhReadTests(unittest.TestCase):
    """Просмотр служб и GitHub без карточки, но только по списку глаголов и
    флагов: `--host` уводит systemctl на другую машину, `--web` открывает
    браузер, `--log` пишет кэш, `journalctl --vacuum-*` удаляет журналы."""

    def test_allowed(self) -> None:
        for cmd in (
            "systemctl --user status claude-tg-bot.service --no-pager",
            "systemctl --user show x -p ActiveEnterTimestamp",
            "systemctl --user list-timers --all", "systemctl status nginx",
            "systemctl --user is-active x", "systemctl cat x --property=Id",
            "gh pr list --state open --limit 30", "gh pr view 12 --json title -q .title",
            "gh issue view 3 -R owner/repo", "gh run list", "gh repo view",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(_bash_auto_allowed(cmd, None))

    def test_card(self) -> None:
        for cmd in (
            "systemctl --user status --host=prod x", "systemctl -H prod status x",
            "systemctl -Hprod status x", "systemctl --machine=m status x",
            "systemctl --user restart x", "systemctl --user edit x", "systemctl status 'x'*",
            "systemctl --user --unknown status x", "systemctl",
            "gh pr view 12 --web", "gh pr view 12 '--web'", "gh pr checks 12 -w",
            "gh pr checks 12 -iw", "gh run view 1 --log", "gh run view 1 --log-failed",
            "gh pr merge 12", "gh api -X POST repos/x/y", "gh pr list *", "gh alias set x y",
            "gh pr", "gh extension exec x",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, None))

    def test_journalctl_write_flags_denied_even_with_env_regex(self) -> None:
        env_re = re.compile(r"journalctl(?: .*)?")
        for ok in ("journalctl --user -u x -n 50", "journalctl --user -u x --since '-15min' --no-pager",
                   "journalctl -u x -n50 -r", "journalctl _SYSTEMD_UNIT=x.service -o json",
                   "gh pr list -R owner/repo", "gh pr view 3 --repo=owner/repo"):
            with self.subTest(ok=ok):
                self.assertTrue(_bash_auto_allowed(ok, env_re))
        for cmd in ("journalctl --vacuum-time=1d", "journalctl --rotate", "journalctl '--flush'",
                    "journalctl --rot", "journalctl --vacuum-t=1s", "journalctl --upd",
                    "journalctl -f", "journalctl -xe", "gh pr list --repo=example.org/a/b",
                    "gh pr list -R evil.example/a/b"):
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, env_re))


class UnattendedAutoAllowTests(unittest.TestCase):
    def test_background_run_allowed(self) -> None:
        for mode in ("bypassPermissions", "default", "acceptEdits"):
            with self.subTest(mode=mode):
                self.assertTrue(_unattended_auto_allowed(True, mode))

    def test_plan_and_unknown_mode_keep_card(self) -> None:
        self.assertFalse(_unattended_auto_allowed(True, "plan"))
        self.assertFalse(_unattended_auto_allowed(True, ""))

    def test_attended_run_not_affected(self) -> None:
        self.assertFalse(_unattended_auto_allowed(False, "bypassPermissions"))


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

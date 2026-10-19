"""Секреты через Bash без карточки (ревью 07.10.2026): разрешённые читалки
не выводят .env, ключи ssh и токены, ни по имени, ни шаблоном, ни скобками.
grep с рекурсией и rg по каталогу идут на карточку всегда (решение владельца
14.10.2026)."""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.approval import _bash_auto_allowed  # noqa: E402

# Тот же список, что в .env на ПК (как в test_bash_read_extra).
_ENV = re.compile(
    r"^(?:(?:ls|pwd|cd|find|fd|fdfind|tree|realpath|readlink|basename|dirname|whoami|date"
    r"|hostname|uname|uptime|head|tail|wc|cat|grep|rg|jq|which|type|echo|file|stat|du|df"
    r"|free|cut|sort|uniq|column|tr|true|false|journalctl)"
    r"|systemctl --user (?:status|is-active|is-failed)"
    r"|git (?:status|diff|log|show|branch|remote -v|ls-files|describe|stash list|grep))(?: .*)?$"
)
_HOME = str(Path.home())


class _Base(unittest.TestCase):
    def ok(self, *cmds: str) -> None:
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertTrue(_bash_auto_allowed(cmd, _ENV))

    def card(self, *cmds: str) -> None:
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, _ENV))


class SecretFileReadTests(_Base):
    def test_secret_names_and_bypasses_go_to_card(self) -> None:
        self.card(
            "cat .env",
            "cat ./.env",
            "cat state/../.env",
            "cat .en''v",
            "cat .en\\v",
            "cat .env.bak-20261005-whitelist",
            "head -5 .env.local",
            "sed -n p .env",
            "awk 1 .env",
            "diff .env /dev/null",
            "strings .env",
            "od -c .env",
            "hexdump -C .env",
            "nl .env",
            "tac .env",
            "rev .env",
            "paste .env",
            "cut -c1- .env",
            "sort .env",
            "grep -f.env x.txt",
            "grep --file=.env x.txt",
            "strings ~/.claude/.credentials.json",
            f"cat {_HOME}/.claude/.credentials.json",
            "cat ~/.ssh/id_ed25519",
            "cat ~/.ssh/config",
            "ls ~/.ssh",
            "cat /proc/self/environ",
            "strings /proc/1/environ",
            "cat ~/.codex/auth.json",
            "cat ~/.config/gh/hosts.yml",
            "cat ~/.grafana_ami_token",
            "git diff --no-index .env /dev/null",
            "git grep --no-index TOKEN",
        )

    def test_globs_and_braces_that_reach_secrets_go_to_card(self) -> None:
        self.card(
            "cat .en*",
            "cat .en[v]",
            "cat .e?v",
            "cat .*",
            "sed -n p .e*",
            "sed -n p .en*",
            "strings ~/.claude/.c*",
            "cat ~/.s*/id*",
            "cat /proc/*/environ",
            "cat /proc/[0-9]*/env*",
            "cat {.e,}nv",
            "cat .{env,x}",
            "head x{1..3}",
        )

    def test_ordinary_reads_still_pass(self) -> None:
        self.ok(
            "sed -n 1,20p README.md",
            "cat src/x.py",
            "cat .env.example",
            "head -20 .env.example",
            "ls *.md",
            "cat docs/*.md",
            "wc -l src/*.py",
            "grep -n env src/x.py",
            "grep -n '\\.env' README.md",
            "awk '{print $1}' a.txt",
            "find . -name '*.py' | xargs -I{} basename {}",
            "diff a.md b.md",
            "strings bin/x",
        )


class RecursiveGrepTests(_Base):
    # grep с рекурсией и rg по каталогу идут на карточку и по дереву без
    # секретов: какие файлы прочтёт обход, по именам в дереве не угадать.
    # Поиск по названным обычным файлам проверяется по их именам
    # (решение владельца 14.10.2026).
    def _tree(self, d: str) -> Path:
        root = Path(d)
        for name in ("src/a.py", "src/b.py", "notes/a.md", "keys/id_rsa", ".env"):
            (root / name).parent.mkdir(exist_ok=True)
            (root / name).write_text("x")
        (root / "src" / "link").symlink_to(root / "notes", target_is_directory=True)
        return root

    def test_tree_with_secret_names_goes_to_card(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._tree(d)
            for cmd in ("grep -rl x .", "grep -rn TOKEN", f"grep -rn foo {d}", "grep -R . .",
                        f"cd {d} && grep -rln y", "grep --recursive x .", "grep -r x keys",
                        "grep -rn --exclude='.*' x .", "grep -R x src",
                        "grep -rli -e a -e b . --include=*.md --include=.env"):
                with self.subTest(cmd=cmd):
                    self.assertFalse(_bash_auto_allowed(cmd, _ENV, cwd=d))
            self.card("grep -Rn x ~")

    def test_tree_without_secrets_goes_to_card_too(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._tree(d)
            for cmd in ("grep -rn x src", "grep -rn x src notes", "grep -rn x src/*.py",
                        "grep -rli -e a -e b . --include=*.md",
                        "grep -r --exclude='.*' --exclude-dir='.*' --exclude-dir=keys x .",
                        "grep -rn x missing", "grep -rn credentials src", "grep -rn x src/a.py",
                        "rg -u x .", "rg -n foo src", "rg foo", "rg foo src/a.py src",
                        "rg foo src/link", "rg foo src/missing.py", "rg foo -"):
                with self.subTest(cmd=cmd):
                    self.assertFalse(_bash_auto_allowed(cmd, _ENV, cwd=d))
            # Без рекурсии grep каталог не читает; rg по обычным файлам.
            for cmd in ("grep -n x src", "grep -n x src/a.py", "rg -n foo src/a.py",
                        "rg foo src/a.py notes/a.md", "rg -e foo -- src/b.py"):
                with self.subTest(cmd=cmd):
                    self.assertTrue(_bash_auto_allowed(cmd, _ENV, cwd=d))
        self.ok("grep -n x .env.example")

    def test_secret_named_or_expanded_goes_to_card(self) -> None:
        self.card(
            "grep TOKEN .env",
            "grep -r x .env",
            "grep -rn x ~/.ssh",
            f"grep -rl x {_HOME}/.claude/.credentials.json",
            "grep -r x .en*",
            "grep -r x ~/src/bot/.env.local",
        )

    def test_hidden_file_walkers_go_to_card(self) -> None:
        self.card(
            "rg --hidden x",
            "rg --hidden TOKEN /opt",
            "rg -uu TOKEN /opt",
            "rg -. TOKEN /opt",
            "diff -r /home/u/src/claude-tg-bot /tmp/empty",
            "diff -rN a b",
        )


class XargsReadersTests(_Base):
    def test_content_readers_behind_xargs_go_to_card(self) -> None:
        self.card(
            "ls -a | xargs cat",
            "find . -name '.e?v' | xargs head",
            "find . -type f | xargs tail -n 5",
        )

    def test_grep_behind_xargs_requires_card(self) -> None:
        self.card(
            "find . -name '*.py' | xargs grep -n z",
            "find . -name '*.md' | xargs grep -l foo",
            "find . -name '*.md' | xargs grep -c foo",
        )
        self.ok(
            "find . -type f | xargs wc -l",
        )


if __name__ == "__main__":
    unittest.main()

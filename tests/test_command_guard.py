"""Защита команд, ревью 11.10.2026: что без карточки пишет файлы, меняет
репозиторий или читает секреты (approval), и чем подписана карточка (danger).
Обычное чтение остаётся без карточки и жёлтым."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot.approval import (  # noqa: E402
    _bash_auto_allowed,
    _format_tool_call,
    needs_full_view,
)
from claude_tg_bot.danger import ORANGE, RED, YELLOW, classify  # noqa: E402

BOT = Path(__file__).resolve().parents[1]
# Боевое значение APPROVE_BASH_AUTO_ALLOW_REGEX из .env.example.
_ENV = re.compile(
    r"^(?:(?:ls|pwd|cd|find|fd|fdfind|tree|realpath|readlink|basename|dirname|whoami|date"
    r"|hostname|uname|uptime|head|tail|wc|cat|grep|rg|jq|which|type|echo|file|stat|du|df"
    r"|free|cut|sort|uniq|column|tr|true|false|journalctl)"
    r"|systemctl --user (?:status|is-active|is-failed)"
    r"|git (?:status|diff|log|show|branch|remote -v|ls-files|describe|stash list|grep))(?: .*)?$"
)


def verdict(cmd: str, cwd: str | None = None):
    kwargs = {"cwd": cwd} if cwd is not None else {}
    return classify("Bash", {"command": cmd}, **kwargs)


def level(cmd: str, cwd: str | None = None) -> str:
    return verdict(cmd, cwd).level


class AutoAllowTests(unittest.TestCase):
    def card(self, *cmds: str) -> None:
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertFalse(_bash_auto_allowed(cmd, _ENV))

    def ok(self, *cmds: str) -> None:
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertTrue(_bash_auto_allowed(cmd, _ENV))

    def test_xargs_arg_file_goes_to_card(self) -> None:
        # B1: xargs -a читает строки файла, имя после снятия xargs терялось.
        self.card("xargs -a ~/x/.env echo", "xargs -a .env grep x", "xargs -a.env echo",
                  "xargs --arg-file=.env echo")
        self.card("find . -name '*.py' | xargs grep -n z")
        self.ok("find . | xargs -n 1 echo")

    def test_uniq_output_operand_goes_to_card(self) -> None:
        # b4.1: второй операнд uniq это файл вывода.
        self.card("uniq /tmp/payload " + str(BOT / "src" / "claude_tg_bot" / "skills" / "x.py"),
                  "echo x | uniq - f", "uniq /dev/null ~/.bashrc", "uniq -f 1 a b", "uniq -cf2 a b",
                  "uniq -- a b", "uniq a*")
        self.ok("uniq a", "sort a | uniq -c", "uniq -f 2 a", "uniq --skip-fields 2 a", "uniq -c")

    def test_git_branch_and_remote_only_view(self) -> None:
        # b4.2: удаление и перестановка веток, правка remote шли без карточки.
        self.card("git branch -D work", "git branch -f main HEAD~1", "git branch -m old new",
                  "git branch --set-upstream-to=origin/x", "git branch newbranch",
                  "git branch -c a b", "git branch --del x",
                  "git remote -v add evil https://example.org/r.git",
                  "git remote -v set-url origin https://example.org/r.git",
                  "git remote -v remove origin")
        self.ok("git branch", "git branch -a", "git branch -vv", "git branch -r",
                "git branch --show-current", "git branch --list 'feat*'",
                "git branch --contains HEAD", "git branch --merged main",
                "git branch --sort=-committerdate", "git branch --format '%(refname)'",
                "git remote -v", "git -C /tmp/x branch -a")

    def test_awk_cannot_choose_input_or_read_environment(self) -> None:
        # b4.3: ARGV собирает имя .env по частям, ENVIRON отдаёт переменные.
        self.card("awk 'BEGIN{ARGV[1]=\".en\" \"v\";ARGC=2}{print}'",
                  "awk -v a=.en -v b=v 'BEGIN{ARGV[1]=a b;ARGC=2}{print}'",
                  "awk 'BEGIN{print ENVIRON[\"HOME\"]}'",
                  "awk 'BEGIN{print PROCINFO[\"pid\"]}'")

    def test_awk_print_redirect_hidden_by_string(self) -> None:
        # b3.1: `;` или `{` в строке обрывали поиск `>` после print.
        self.card("awk 'BEGIN {print \";\" > \"/tmp/victim\"}'",
                  "awk 'BEGIN {print \"{\" > \"/tmp/victim\"}'",
                  "awk '{print $0 ~ /;/ > \"/tmp/victim\"}' f",
                  "awk 'BEGIN {printf \"%s;\", 1 > \"/tmp/victim\"}'")
        self.card("awk '{print /\"/ \";\" > \"/tmp/victim\"}' f")
        self.ok("awk '{print $1}' f", "awk '$3 > 5 {print $1}' f",
                "awk 'NR > 1 && $2 >= 10 {n++} END {print n}' f")

    def test_curl_get_flag_as_value_is_not_get(self) -> None:
        # b3.3: `-G` значением --user-agent не делает запрос GET.
        self.card("curl --user-agent -G --data x=1 https://example.org/api/items",
                  "curl -A -G -d x=1 https://example.org/api/items",
                  "curl -H --get --data x=1 https://example.org/api/items")
        self.ok("curl -G --data x=1 https://example.org/api/items",
                "curl -sG --data-urlencode q=a https://example.org/s",
                "curl -s https://example.org/api")

    def test_curl_url_globbing_goes_to_card(self) -> None:
        # b3.4: curl сам раскрывает {a,b} и [1-2], кавычки shell не мешают.
        self.card("curl -s 'http://{example.org,localhost}:8788/health'",
                  "curl -s 'http://{example.org,127.0.0.1}:8788/health'",
                  "curl -s 'https://example.org/[1-2]'",
                  "curl -g -s 'http://{example.org,localhost}:8788/health'")

    def test_file_compile_goes_to_card(self) -> None:
        self.card("file -C -m magic", "file --compile -m magic")
        self.ok("file a.txt", "file -b a.txt")


class RoundFourTests(unittest.TestCase):
    # Регрессии пунктов 1-8 (ревью 11.10.2026).
    def card(self, cmd: str, cwd: str | None = None) -> None:
        allowed = (_bash_auto_allowed(cmd, _ENV) if cwd is None else
                   _bash_auto_allowed(cmd, _ENV, cwd=cwd))
        self.assertFalse(allowed, cmd)
        self.assertNotEqual(level(cmd, cwd), YELLOW, cmd)

    def test_1_recursive_and_dynamic_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for name in ("src/bot.py", ".env", "lib/credentials.json"):
                (Path(d) / name).parent.mkdir(exist_ok=True)
                (Path(d) / name).write_text("x")
            for cmd in ("rg -g '.*' . .", "rg --glob=**/.* x .", "rg --no-ignore x .",
                        "rg -uu x .", "ls -a | xargs grep .", "rg --follow x .",
                        "grep -r --include='.*' x ."):
                with self.subTest(cmd=cmd):
                    self.card(cmd, d)
            # Поиск по каталогу идёт на карточку всегда, но без названного
            # секрета остаётся жёлтым чтением (решение владельца 14.10.2026).
            for cmd in ("grep -R . .", "grep -rn x .", "rg x .", "rg -n x src",
                        "rg -g '*.py' x .", "grep -rn x src",
                        "grep -R --exclude='.*' --exclude-dir='.*' x .",
                        "grep -r --exclude='.*' --exclude-dir='.*' --exclude-dir=lib x .",
                        "grep -r --exclude '.*' --exclude-dir '.*' --exclude-dir lib x .",
                        "grep -rn -A 3 credentials src"):
                with self.subTest(cmd=cmd):
                    self.assertFalse(_bash_auto_allowed(cmd, _ENV, cwd=d), cmd)
                    self.assertEqual(level(cmd, d), YELLOW, cmd)
            for cmd in ("grep -n x src/bot.py", "rg -n x src/bot.py", "find . -name '*.py'"):
                with self.subTest(cmd=cmd):
                    self.assertTrue(_bash_auto_allowed(cmd, _ENV, cwd=d), cmd)
                    self.assertEqual(level(cmd, d), YELLOW, cmd)

    def test_2_secret_sources_and_environment(self) -> None:
        for cmd in ("jq -n env", "jq -n '$ENV'", "ps eww -p 123", "ps -eww -p 123",
                    "ps ax -o pid,command,e", "getent shadow", "getent gshadow",
                    "cat ~/.gnupg/private-keys-v1.d/*.key", "cat ~/.kube/config",
                    "cat ~/credentials.txt"):
            with self.subTest(cmd=cmd):
                self.card(cmd)
        self.card("cat ~/.npmrc")
        # macOS ps -E печатает окружение процессов; облачные токены rclone и
        # истории оболочек тоже секреты (ревью 11.10.2026).
        for cmd in ("ps -E", "ps -Ef", "ps -axE", "ps -E -p 123",
                    "cat ~/.config/rclone/rclone.conf", "cat ~/.zsh_history",
                    "tail ~/.bash_history", "cat ~/.local/share/fish/fish_history",
                    "cat ~/.python_history", "cat ~/.psql_history", "cat ~/.mysql_history"):
            with self.subTest(cmd=cmd):
                self.card(cmd)
        # Значения флагов ps не флаг окружения e (ревью 11.10.2026).
        for cmd in ("jq '.items' data.json", "ps aux", "ps -e -o pid,comm", "getent hosts x",
                    "ps -ef", "ps -o pid,user,command", "ps -u steve", "ps -U EVE", "ps -aU eve"):
            self.assertTrue(_bash_auto_allowed(cmd, _ENV), cmd)
            self.assertEqual(level(cmd), YELLOW, cmd)

    def test_2_symlinks_and_changed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'project').mkdir()
            (root / 'alias').symlink_to(root / '.env')
            (root / 'keys').symlink_to(Path.home() / '.ssh', target_is_directory=True)
            (root / 'plain').write_text('project text')
            (root / 'safe').symlink_to(root / 'plain')
            (root / 'alias with spaces').symlink_to(root / '.env')
            self.card('cat alias', d)
            self.card("cat 'alias with spaces'", d)
            self.card('cat alias*', d)
            self.card('cat < alias', d)
            self.card('cat .*', d)
            self.card('cat keys/id_rsa', d)
            self.card('cd project; cat ../alias', d)
            self.card('git -C project diff --no-index ../alias ../plain', d)
            self.assertTrue(_bash_auto_allowed('cat safe', _ENV, cwd=d))
            self.assertTrue(_bash_auto_allowed('git -C project status', _ENV, cwd=d))
            # Каталог меняет только -C самого git до подкоманды, а не слово
            # git среди аргументов или -C (контекст) у git grep (ревью 11.10.2026).
            self.card('cat alias git -C project', d)
            self.card('git grep -C project x alias', d)
            self.assertFalse(_bash_auto_allowed('ls \\', _ENV, cwd=d))

    def test_3_state_changing_readers(self) -> None:
        for cmd in ("git fetch origin", "mkdir -p /tmp/newdir",
                    "gh api repos/x/y --cache 1h", "gh api repos/x/y --cache=1h",
                    "date --set '2026-10-12'", "date -s now", "hostname new-name",
                    "hostname -F names", "hostname --boot"):
            with self.subTest(cmd=cmd):
                self.card(cmd)
        for cmd in ("date", "date -u '+%F'", "hostname", "hostname -f", "git status",
                    "git log -1", "git diff", "git show HEAD", "gh api repos/x/y"):
            self.assertTrue(_bash_auto_allowed(cmd, _ENV), cmd)

    def test_4_flag_values_and_bundles(self) -> None:
        for cmd in ("file -C -m magic", "file -bC -m magic",
                    "curl -w '%output{/tmp/file}' https://example.org",
                    "curl -sw '%output{/tmp/file}' https://example.org"):
            self.assertEqual(level(cmd), ORANGE, cmd)
        for cmd in ("npm --prefix /tmp/project publish", "npm --prefix=/tmp/project publish",
                    "xargs -0I {} rm -rf {}", "xargs -0I{} rm -rf {}",
                    "xargs -0n 1 rm -rf"):
            self.assertEqual(level(cmd), RED, cmd)
        self.assertEqual(level("curl -A '-w' https://example.org"), YELLOW)
        self.assertEqual(level('uniq -cf 2 input'), YELLOW)
        self.assertEqual(level('uniq -cf 2 input ~/.ssh/authorized_keys'), RED)

    def test_5_cloud_actions_ignore_values(self) -> None:
        for cmd in ("az vm deallocate --name get --resource-group x",
                    "az --subscription get vm deallocate --name x",
                    "gcloud compute instances mystery --zone get",
                    "doctl compute droplet mystery --format get",
                    "az vm mystery get", "az vm get-mystery --name x"):
            self.assertEqual(level(cmd), RED, cmd)
        self.assertEqual(level("gcloud app deploy"), RED)
        for cmd in ("az vm show --name get --resource-group x",
                    "az storage blob list --account-name x",
                    "az --debug vm list",
                    "gcloud --project x compute instances list",
                    "doctl compute droplet list --format ID"):
            self.assertEqual(level(cmd), ORANGE, cmd)
            self.assertTrue(any('чтение' in r for r in verdict(cmd).orange))

    def test_7_kube_verb_after_flag_values(self) -> None:
        # Значение флага не глагол kubectl и helm (ревью 11.10.2026).
        for cmd in ("kubectl --namespace get delete pod x", "kubectl -n get delete pod x",
                    "kubectl --context list -l app=get apply -f x.yml",
                    "kubectl -nget delete pod x", "helm --namespace list uninstall x",
                    "helm -n status --kube-context get uninstall x"):
            self.assertEqual(level(cmd), RED, cmd)
        for cmd in ("kubectl -n prod get pods", "kubectl --namespace=prod get pods",
                    "kubectl get pods -o wide", "helm -n prod list"):
            self.assertEqual(level(cmd), YELLOW, cmd)

    def test_6_specific_program_paths(self) -> None:
        from claude_tg_bot.approval import _SKILL_PYTHON
        for cmd in ("~/.local/bin/cat a", str(BOT / '.venv/bin/cat') + ' a',
                    "~/src/svod/bin/ls", "/opt/homebrew/bin/cat a"):
            self.assertEqual(level(cmd), ORANGE, cmd)
        with tempfile.TemporaryDirectory() as d:
            alias = Path(d) / 'cat'
            alias.symlink_to('/usr/bin/cat')
            self.assertEqual(level(str(alias) + ' a'), ORANGE)
        cmd = str(_SKILL_PYTHON) + ' -m claude_tg_bot.skills.yt_subs --help'
        self.assertTrue(_bash_auto_allowed(cmd, None))
        self.assertEqual(level(cmd), ORANGE)
        self.assertEqual(level('~/src/svod/bin/memory recall test'), YELLOW)
        self.assertEqual(level('/usr/bin/ls'), YELLOW)

    def test_7_redirect_cwd_and_symlink(self) -> None:
        for cmd in ('cd ~/.ssh; echo x > authorized_keys',
                    'cd ~/.ssh && echo x > authorized_keys'):
            self.assertEqual(level(cmd), RED, cmd)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'alias').symlink_to(Path.home() / '.ssh/authorized_keys')
            self.assertEqual(level('echo x > alias', d), RED)
            self.assertEqual(level('sort -o alias input', d), RED)
            self.assertEqual(level('echo x > ordinary', d), ORANGE)

    def test_8_ordinary_reads_and_safe_glob(self) -> None:
        for cmd in ("awk '{print (NR > 5)}' f", "awk '{printf \"%s\", (NR > 5)}' f",
                    "git diff --no-index a.txt b.txt"):
            self.assertTrue(_bash_auto_allowed(cmd, _ENV), cmd)
            self.assertEqual(level(cmd), YELLOW, cmd)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'a1').write_text('ordinary')
            self.assertTrue(_bash_auto_allowed('uniq ./a*', _ENV, cwd=d))
            self.assertEqual(level('uniq ./a*', d), YELLOW)
            (Path(d) / 'a2').write_text('ordinary')
            self.card('uniq ./a*', d)
            (Path(d) / 'left').mkdir()
            (Path(d) / 'right').mkdir()
            self.card('git diff --no-index left right', d)
        self.card('git diff --no-index .env a.txt')
        self.card('git diff --no-index --output=out a.txt b.txt')


class RoundFiveTests(unittest.TestCase):
    # Регрессии пунктов 1-5 пятого раунда (ревью 11.10.2026).
    def card(self, cmd: str, cwd: str | None = None) -> None:
        allowed = (_bash_auto_allowed(cmd, _ENV) if cwd is None else
                   _bash_auto_allowed(cmd, _ENV, cwd=cwd))
        self.assertFalse(allowed, cmd)
        self.assertNotEqual(level(cmd, cwd), YELLOW, cmd)

    def ok(self, cmd: str, cwd: str | None = None) -> None:
        allowed = (_bash_auto_allowed(cmd, _ENV) if cwd is None else
                   _bash_auto_allowed(cmd, _ENV, cwd=cwd))
        self.assertTrue(allowed, cmd)
        self.assertEqual(level(cmd, cwd), YELLOW, cmd)

    def search_card(self, cmd: str, cwd: str) -> None:
        # Поиск по каталогу: карточка всегда, цвет чтения (решение владельца 14.10.2026).
        self.assertFalse(_bash_auto_allowed(cmd, _ENV, cwd=cwd), cmd)
        self.assertEqual(level(cmd, cwd), YELLOW, cmd)

    @staticmethod
    def _tree(d: str, *names: str) -> None:
        for name in names:
            (Path(d) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(d) / name).write_text("token")

    def test_1_grep_directories_recurse(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, "src/bot.py", ".env")
            for cmd in ("grep -d recurse token .", "grep --directories=recurse token .",
                        "grep --directories recurse token .", "grep -drecurse token .",
                        "grep --dir=recurse token .", "grep -nd recurse token .",
                        "grep -d recurse token src"):
                with self.subTest(cmd=cmd):
                    self.search_card(cmd, d)
            self.ok("grep -d skip token .", d)

    def test_2_grep_filters_do_not_skip_card(self) -> None:
        # Фильтры grep -r не разбираются: карточка всегда, .env во
        # включающем шаблоне красит её (решение владельца 14.10.2026).
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, "src/bot.py", ".env")
            for cmd in ("grep -r --exclude='*' --include='.env' token .",
                        "grep -r --exclude=.env --include .env token ."):
                with self.subTest(cmd=cmd):
                    self.card(cmd, d)
            for cmd in ("grep -r --exclude='.env' --include='*' token .",
                        "grep -r --include='*' --exclude='.env' token .",
                        "grep -r --include='*.py' token .",
                        "grep -r --exclude='.env' token ."):
                with self.subTest(cmd=cmd):
                    self.search_card(cmd, d)

    def test_3_rg_overrides_of_ignore_rules(self) -> None:
        # Проверено настоящим rg 14.1.1: -u возвращает файлы из .gitignore
        # и .ignore, положительный -g/--iglob возвращает и игнорируемые, и
        # скрытые совпадения, отрицательный только исключает.
        # С 14.10.2026 rg по каталогу на карточке при любых шаблонах; флаги,
        # открывающие игнорируемые файлы, красят её (решение владельца).
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, "src/bot.py", "credentials.txt", "sub/.secret.key", "notes.md")
            (Path(d) / ".gitignore").write_text("credentials.txt\n")
            for cmd in ("rg -u token .", "rg -nu token .", "rg -g '*.key' token .",
                        "rg --no-ignore-vcs token .", "rg --no-ignore-dot token ."):
                with self.subTest(cmd=cmd):
                    self.card(cmd, d)
            for cmd in ("rg -g '*.txt' token .", "rg --iglob '*.TXT' token .",
                        "rg -g '*' token .", "rg --glob=sub/** token .",
                        "rg -g '!*.md' -g '*.txt' token .", "rg -g '!*.md' token ."):
                with self.subTest(cmd=cmd):
                    self.assertFalse(_bash_auto_allowed(cmd, _ENV, cwd=d), cmd)
            for cmd in ("rg -g '*.py' token .", "rg -tpy token .",
                        "rg -g '*.md' token .", "rg -g '*.txt' token src"):
                with self.subTest(cmd=cmd):
                    self.search_card(cmd, d)

    def test_4_search_goes_to_card_without_walking_tree(self) -> None:
        # Обход дерева убран: каталог не читается, а поиск по нему сразу
        # идёт на карточку (решение владельца 14.10.2026).
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, "src/bot.py", "notes.md")
            with mock.patch("os.scandir", side_effect=AssertionError("обход дерева")):
                for cmd in ("grep -r token .", "grep -rn token src", "rg token .", "rg token"):
                    with self.subTest(cmd=cmd):
                        self.search_card(cmd, d)

    def test_5_bsd_date_arithmetic_and_format(self) -> None:
        for cmd in ("date -v-1d +%F", "date -v -1d '+%F'", "date -v+1m -v-1d +%Y-%m",
                    "date -j -f '%Y-%m-%d' 2026-10-12 +%s", "date -jf %s 1700000000 +%F",
                    "date -r 1700000000", "date -ju", "date -z UTC +%H", "date -Iseconds",
                    "date -j 101212002026", "date -u -v-1H '+%FT%T'"):
            with self.subTest(cmd=cmd):
                self.ok(cmd)
        for cmd in ("date 101212002026", "date -u 1012", "date -f '%Y' 2026",
                    "date -f /tmp/dates", "date -s now", "date --set=now", "date -n 1012",
                    "date -t 0"):
            with self.subTest(cmd=cmd):
                self.card(cmd)


class RoundSixTests(unittest.TestCase):
    # Регрессии ревью коммита f4c32a9 (ревью 11.10.2026). Поведение rg
    # сверено с rg 14.1.1: вне репозитория git .gitignore не действует,
    # шаблон -g с ** и фигурными скобками отдаёт и игнорируемые файлы.
    card = RoundFiveTests.card
    ok = RoundFiveTests.ok
    search_card = RoundFiveTests.search_card
    _tree = staticmethod(RoundFiveTests._tree)

    def test_1_rg_globs_go_to_card(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, "src/bot.py", "credentials.txt", "notes.md")
            (Path(d) / ".gitignore").write_text("credentials.txt\n")
            for cmd in ("rg -g '**/*.txt' token src", "rg -g '**/credentials.txt' token src",
                        "rg -g '*.{txt,md}' token src", "rg --glob='{a,b}' token src",
                        "rg --iglob '**/*.py' token src", "rg -g '*.py' token src",
                        "rg -g '!**/*.md' token src"):
                with self.subTest(cmd=cmd):
                    self.assertFalse(_bash_auto_allowed(cmd, _ENV, cwd=d), cmd)

    def test_2_rg_without_files_searches_directory(self) -> None:
        # rg без пути ищет в текущем каталоге; путь не к обычному файлу
        # считается каталогом. По названным файлам проверяются их имена
        # (решение владельца 14.10.2026).
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, "src/bot.py", "keys/id_rsa", "notes.md", ".env")
            for cmd in ("rg token", "rg -n token .", "rg -l token src keys", "rg token src",
                        "rg -g '!*.md' token .", "rg -tpy -e token --type-add 'py:*' .",
                        "rg -tpy token .", "rg -g '!id_rsa' token .", "rg token missing.md",
                        "rg token notes.md src"):
                with self.subTest(cmd=cmd):
                    self.search_card(cmd, d)
            for cmd in ("rg -n token notes.md", "rg token src/bot.py notes.md"):
                with self.subTest(cmd=cmd):
                    self.ok(cmd, d)
            self.card("rg token keys/id_rsa", d)
            self.card("rg token .env", d)
            with mock.patch.dict(os.environ, {"HOME": d}):
                self.search_card("rg token ~", "/")

    def test_3_grep_recursion_abbreviations(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, "src/bot.py", ".env")
            for cmd in ("grep -d rec token .", "grep -d recu token .", "grep -drecur token .",
                        "grep --directories=rec token .", "grep --dir=recurs token .",
                        "grep --rec token .", "grep --recursive token .", "grep -Rn token .",
                        "grep -nr token src/bot.py", "grep --der token .",
                        "grep --dereference-r token .", "grep --dereference-recursive token .",
                        "egrep -r token .", "fgrep --recursive token ."):
                with self.subTest(cmd=cmd):
                    self.search_card(cmd, d)
            for cmd in ("grep -d re token .", "grep -d read token .", "grep -d skip token .",
                        "grep -n token src/bot.py", "grep -c token src"):
                with self.subTest(cmd=cmd):
                    self.ok(cmd, d)


class SecretNamesTests(unittest.TestCase):
    # Ключи, сертификаты, хранилища ключей и secrets.yaml при прямом чтении
    # (решение владельца 14.10.2026). Регистр не важен.
    _NAMES = ("privkey.pem", "SERVER.PEM", "cert.p12", "a.pfx", "release.jks", "app.keystore",
              "secrets.yaml", "SECRETS.YML", "deploy/secrets.json", "notes.md", "my-secrets.yaml")

    def test_named_key_files_go_to_card(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            RoundFiveTests._tree(d, *self._NAMES)
            for cmd in ("grep -n . privkey.pem", "cat SERVER.PEM", "head cert.p12", "cat a.pfx",
                        "tail release.jks", "cat app.keystore", "cat secrets.yaml",
                        "grep -n x SECRETS.YML", "cat deploy/secrets.json", "rg -n x privkey.pem",
                        "rg -g .env token notes.md", "grep -n --include=.env x notes.md"):
                with self.subTest(cmd=cmd):
                    self.assertFalse(_bash_auto_allowed(cmd, _ENV, cwd=d), cmd)
                    self.assertEqual(level(cmd, d), ORANGE, cmd)
            for cmd in ("grep -n . notes.md", "cat my-secrets.yaml", "rg -n x notes.md",
                        "rg -g '*.md' token notes.md"):
                with self.subTest(cmd=cmd):
                    self.assertTrue(_bash_auto_allowed(cmd, _ENV, cwd=d), cmd)
                    self.assertEqual(level(cmd, d), YELLOW, cmd)


class GlobOptionNameTests(unittest.TestCase):
    # Файл с именем `-r` после раскрытия `*` делает `grep x *` рекурсивным:
    # такой шаблон идёт на карточку (решение владельца 14.10.2026).
    def test_glob_expanding_to_option_name_goes_to_card(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            RoundFiveTests._tree(d, "notes.md", "-r", "sub/.env")
            self.assertFalse(_bash_auto_allowed("grep x *", _ENV, cwd=d))
        with tempfile.TemporaryDirectory() as d:
            RoundFiveTests._tree(d, "notes.md", "readme.md")
            self.assertTrue(_bash_auto_allowed("grep x *", _ENV, cwd=d))


class FullViewTests(unittest.TestCase):
    def test_button_when_escaping_hides_the_tail(self) -> None:
        # b3.5: 600 знаков `<` после экранирования занимают всю карточку,
        # хвост `; rm` пропадал, а кнопки «Полностью» не было.
        tool_input = {"command": "echo '" + "<" * 600 + "'; rm -rf /tmp/victim"}
        self.assertNotIn("rm -rf", _format_tool_call("Bash", tool_input, None))
        self.assertTrue(needs_full_view(tool_input, "Bash"))
        self.assertTrue(needs_full_view(tool_input))

    def test_button_when_description_is_cut(self) -> None:
        tool_input = {"command": "ls", "description": "п" * 300}
        self.assertTrue(needs_full_view(tool_input, "Bash"))

    def test_no_button_for_short_card(self) -> None:
        self.assertFalse(needs_full_view({"command": "ls -la", "description": "список"}, "Bash"))


class DangerLevelTests(unittest.TestCase):
    def check(self, expected: str, *cmds: str, cwd: str | None = None) -> None:
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertEqual(level(cmd, cwd), expected, verdict(cmd, cwd))

    def test_comments_here_strings_and_quoted_delimiters(self) -> None:
        # B3: строки после комментария, here-string и `<<E"OF"` исполняются.
        self.check(RED, "echo a # it's\nrm -rf ~/work", "echo a # <<EOF\nrm -rf ~/work",
                   "cat <<<x\nrm -rf ~/work", "cat <<E\"OF\"\nx\nEOF\nrm -rf ~/work")
        self.check(YELLOW, "cat <<'EOF'\nrm -rf ~/work\nEOF", "echo '# x' ; ls # rm -rf ~")

    def test_redirect_ampersand_and_glued_operators(self) -> None:
        # B4: `>&файл` пишет в файл, `;>` не разделитель.
        self.check(RED, "echo pwned >& ~/.bashrc", "true;>~/.ssh/authorized_keys")
        self.check(YELLOW, "ls >&2", "ls 2>&1 | head")

    def test_shell_keywords_do_not_hide_commands(self) -> None:
        # B5
        self.check(RED, 'for f in a b; do rm -rf "$f"; done', "if true; then git push; fi",
                   "{ rm -rf ~/x; }", "! rm -rf ~/x",
                   "while true; do systemctl --user restart x; done")
        self.check(YELLOW, "for f in a b; do echo $f; done", "while read l; do echo $l; done < f",
                   "if test -f a; then cat a; fi")

    def test_writers_hidden_among_readers(self) -> None:
        # B6, b4.5: запись файлов и запуск программ у «читающих» команд.
        self.check(ORANGE, "tree -o F", "sort --out=F a", "iconv -o F a", "shuf -o F a",
                   "gzip F", "gunzip F.gz", "xxd -r a F", "uniq a b", "curl -sSLo F https://x",
                   "curl -D F https://x", "git archive -o F HEAD", "awk '{print $1 | c}' f",
                   "awk '{print > f}' x", "awk '{print |& \"c\"}' x", "awk -f prog.awk x",
                   "awk '{print /\"/ \";\" > \"f\"}' x", "rg --pre sh x .", "rg --pre=sh x",
                   "tar -xlf a.tar -C ~",
                   "tar -tf a.tar --checkpoint-action=exec=sh", "openssl s_client -connect x:443",
                   "sort --compress-program=x a")
        self.check(RED, "uniq a ~/.bashrc", "openssl genrsa -out ~/.ssh/id_rsa",
                   "curl -o ~/.bashrc https://x", "curl -sSLo ~/.bashrc https://x",
                   "curl -sX DELETE https://x", "curl -sXDELETE https://x",
                   "sed -n '1e id' f", "sed 's/a/b/e' f", "sed 's/a/b/w out' f")
        self.check(YELLOW, "uniq -c a", "gzip -dc a.gz", "gunzip -l a.gz", "xxd a", "tree",
                   "rg x", "sed -n 1p words.txt", "sed -n '/we/p' f",
                   "awk '$3 > 5 {print $1}' f", "awk -F, '{print $2}' f",
                   "openssl x509 -in c.crt -noout -text", "curl -sI https://x")

    def test_service_publish_and_install_verbs(self) -> None:
        # B7
        self.check(RED, "systemctl force-reload x", "systemctl --user try-reload-or-restart x",
                   "systemctl reenable x", "busctl call org.freedesktop.systemd1 /x y StopUnit",
                   "loginctl terminate-user user", "loginctl disable-linger",
                   "hostnamectl set-hostname x", "npm i", "pnpm i", "yarn", "npm rm x",
                   "npm publish", "cargo publish", "gem push x.gem", "git send-email x",
                   "git daemon", "git checkout-index -f -a", "terraform workspace delete x",
                   "terraform test", "journalctl --rot", "git lfs push origin main", "go get x")
        self.check(ORANGE, "go run .", "cargo run", "git reset HEAD~1",
                   "git ls-remote --upload-pack=x origin", "git grep --open-files-in-pager=vi x")
        self.check(YELLOW, "busctl list", "loginctl", "hostnamectl", "timedatectl status",
                   "git bundle list-heads x.bundle", "git symbolic-ref HEAD")

    def test_environment_assignments_that_run_code(self) -> None:
        # B8
        self.check(ORANGE, "LD_PRELOAD=/tmp/x.so ls", "GIT_EXTERNAL_DIFF=prog git diff",
                   "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=x git status",
                   "git --config-env=core.fsmonitor=VAR status", "env PAGER=x git log")
        self.check(YELLOW, "TZ=UTC date", "LANG=C ls", "GIT_PAGER=cat git log")

    def test_program_by_path(self) -> None:
        # B9
        self.check(ORANGE, "./ls", "/tmp/x/cat ~/notes")
        self.check(YELLOW, "/usr/bin/ls", "/bin/cat a")

    def test_home_variable_and_cwd_paths(self) -> None:
        # B10: $HOME, ${HOME} и путь от каталога агента.
        self.check(RED, "echo x >> $HOME/.ssh/authorized_keys",
                   "echo x >> ${HOME}/.ssh/authorized_keys")
        rel = f"{BOT.name}/src/claude_tg_bot/bot.py"
        self.check(RED, f"echo x > {rel}", cwd=str(BOT.parent))
        self.check(ORANGE, f"echo x > {rel}")
        self.assertEqual(classify("Write", {"file_path": rel}, cwd=str(BOT.parent)).level, RED)
        self.assertEqual(classify("Write", {"file_path": rel}).level, ORANGE)
        card = _format_tool_call("Write", {"file_path": rel, "content": "x"}, None, str(BOT.parent))
        self.assertTrue(card.startswith("🔴"))

    def test_monitor_parsed_as_shell(self) -> None:
        # B11
        self.assertEqual(classify("Monitor", {"command": "rm -rf ~/work"}).level, RED)
        self.assertEqual(classify("Monitor", {"command": "tail -f /tmp/log"}).level, YELLOW)
        self.assertEqual(classify("Monitor", {}).level, ORANGE)

    def test_cloud_verb_not_flag_value(self) -> None:
        # B12
        self.check(RED, "aws lambda delete-function --function-name getUser",
                   "aws iam delete-user --user-name lsmith",
                   "aws dynamodb delete-table --table-name list_items",
                   "aws configure set x y --profile target",
                   "gcloud compute instances delete listener --zone z")
        self.check(ORANGE, "aws s3 ls", "aws sts get-caller-identity",
                   "aws --region us-east-1 ec2 describe-instances", "aws configure get region",
                   "gcloud compute instances list")

    def test_wrappers_keep_inner_red(self) -> None:
        # B13
        self.check(RED, 'bash -lc "rm -rf ~/work"', "sh -ec 'git push'", "xargs -I {} rm -rf {}",
                   "xargs -n 1 rm -rf", "rtk proxy git push --force", "watch -n 1 'rm -rf ~/x'",
                   "ssh -oProxyCommand=sh host cmd", "echo x > >(rm -rf ~/x)")


class HookTimeoutTests(unittest.TestCase):
    def _cap(self, approve_timeout: int, hook_timeout: int) -> tuple[int, str]:
        from claude_tg_bot.config import HOOK_APPROVE_MODULE, _cap_approve_timeout
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": f"python -m {HOOK_APPROVE_MODULE}",
                 "timeout": hook_timeout}]}]}}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                capped = _cap_approve_timeout(path, approve_timeout)
        return capped, err.getvalue()

    def test_wait_is_capped_below_hook_timeout(self) -> None:
        # B2: бот отвечает раньше, чем CLI снимет хук: ожидание урезается до
        # timeout хука минус 60 с (решение владельца 12.10.2026).
        capped, err = self._cap(1800, 960)
        self.assertEqual(capped, 900)
        self.assertIn("APPROVE_TIMEOUT_SEC=1800", err)
        self.assertEqual(self._cap(900, 960), (900, ""))
        self.assertEqual(self._cap(600, 960), (600, ""))

    def test_hook_process_gets_capped_wait_too(self) -> None:
        # Процесс хука грузит настройки без проверки, но ждёт урезанное
        # значение; наименьший срок из хуков, булев игнорируется.
        from claude_tg_bot.config import HOOK_APPROVE_MODULE, load_config
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            hooks = [{"type": "command", "command": f"python -m {HOOK_APPROVE_MODULE}", "timeout": t}
                     for t in (960, 600, True)]
            path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}}))
            from claude_tg_bot import config as cfgmod
            self.assertEqual(cfgmod._cap_approve_timeout(path, 1800, warn=False), 540)
            env = {"TELEGRAM_BOT_TOKEN": "1:test", "TELEGRAM_ALLOWED_USER_IDS": "1",
                   "CLAUDE_BIN": "/usr/bin/true", "CLAUDE_HOOK_SETTINGS": str(path),
                   "APPROVE_TIMEOUT_SEC": "1800"}
            with mock.patch.object(cfgmod, "load_dotenv"), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(load_config(skip_hook_validation=True).approve_timeout_sec, 540)

    def _settings(self, d: str, timeout: object) -> Path:
        from claude_tg_bot.config import HOOK_APPROVE_MODULE
        path = Path(d) / "settings.json"
        path.write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "Bash|Monitor|Write|Edit|MultiEdit|NotebookEdit|WebFetch|mcp__.*",
            "hooks": [{"type": "command", "command": f"python -m {HOOK_APPROVE_MODULE}",
                       "timeout": timeout, "onFailure": "block"}]}]}}))
        return path

    def _env(self, path: Path) -> dict[str, str]:
        return {"TELEGRAM_BOT_TOKEN": "1:test", "TELEGRAM_ALLOWED_USER_IDS": "1",
                "CLAUDE_BIN": "/usr/bin/true", "CLAUDE_HOOK_SETTINGS": str(path),
                "APPROVE_TIMEOUT_SEC": "900"}

    def test_short_hook_timeout_refuses_start(self) -> None:
        # B2: при timeout хука 60 с и меньше бот не стартует, иначе карточка
        # ждала бы клика дольше, чем CLI держит хук (решение владельца 14.10.2026).
        from claude_tg_bot import config as cfgmod
        for timeout in (60, 60.0, 30, 0, -5):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as d:
                path = self._settings(d, timeout)
                with self.assertRaises(RuntimeError) as cm:
                    cfgmod._validate_hook_settings(path)
                self.assertIn("нужно больше 60 с", str(cm.exception))
                with mock.patch.object(cfgmod, "load_dotenv"), \
                        mock.patch.dict(os.environ, self._env(path), clear=True), \
                        self.assertRaises(RuntimeError):
                    cfgmod.load_config()
        with tempfile.TemporaryDirectory() as d:
            cfgmod._validate_hook_settings(self._settings(d, 61))

    def test_hook_process_wait_never_below_one_second(self) -> None:
        # Процесс хука настройки не проверяет: короткий timeout даёт 1 с
        # ожидания, а не ноль или минус (решение владельца 14.10.2026).
        from claude_tg_bot import config as cfgmod
        for timeout in (61, 60, 30, 0, -5):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as d:
                path = self._settings(d, timeout)
                self.assertEqual(cfgmod._cap_approve_timeout(path, 900, warn=False), 1)
                with mock.patch.object(cfgmod, "load_dotenv"), \
                        mock.patch.dict(os.environ, self._env(path), clear=True):
                    self.assertEqual(
                        cfgmod.load_config(skip_hook_validation=True).approve_timeout_sec, 1)


class ClaudeVersionTests(unittest.TestCase):
    """onFailure понимает Claude Code 2.1.295 и новее: со старым CLI, без
    CLI или с непонятной версией бот не стартует (ревью Codex 19.10.2026)."""

    @staticmethod
    def _env(d: str, claude: Path) -> dict[str, str]:
        from claude_tg_bot.config import HOOK_APPROVE_MODULE
        path = Path(d) / "settings.json"
        path.write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "Bash|Monitor|Write|Edit|MultiEdit|NotebookEdit|WebFetch|mcp__.*",
            "hooks": [{"type": "command", "command": f"python -m {HOOK_APPROVE_MODULE}",
                       "timeout": 960, "onFailure": "block"}]}]}}))
        return {"TELEGRAM_BOT_TOKEN": "1:test", "TELEGRAM_ALLOWED_USER_IDS": "1",
                "CLAUDE_BIN": str(claude), "CLAUDE_HOOK_SETTINGS": str(path)}

    def _load(self, d: str, version_output: str | None) -> None:
        from claude_tg_bot import config as cfgmod
        claude = Path(d) / "claude"
        if version_output is not None:
            claude.write_text(f"#!/bin/sh\necho '{version_output}'\n")
            claude.chmod(0o755)
        with mock.patch.object(cfgmod, "load_dotenv"), \
                mock.patch.dict(os.environ, self._env(d, claude), clear=True):
            cfgmod.load_config()

    def test_new_cli_starts(self) -> None:
        for out in ("2.1.295 (Claude Code)", "2.1.303 (Claude Code)", "2.2.0", "3.0.1"):
            with self.subTest(out=out), tempfile.TemporaryDirectory() as d:
                self._load(d, out)

    def test_old_missing_or_unknown_cli_refuses_start(self) -> None:
        for out in ("2.1.294 (Claude Code)", "1.9.999", "", "Claude Code", None):
            with self.subTest(out=out), tempfile.TemporaryDirectory() as d, \
                    self.assertRaises(RuntimeError) as cm:
                self._load(d, out)
            self.assertIn("2.1.295", str(cm.exception))

    def test_hook_process_skips_version_check(self) -> None:
        # Процесс хука и прочие утилиты грузят настройки без проверки: CLI
        # для них не запускается.
        from claude_tg_bot import config as cfgmod
        with mock.patch.object(cfgmod, "_validate_claude_version") as check, \
                tempfile.TemporaryDirectory() as d, mock.patch.object(cfgmod, "load_dotenv"), \
                mock.patch.dict(os.environ, self._env(d, Path("/usr/bin/true")), clear=True):
            cfgmod.load_config(skip_hook_validation=True)
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Оценка опасности действия для approve-карточки.

Только подсказка человеку: ничего не разрешает и не запрещает. Уровни:
RED (необратимое или наружу: удаление, публикация, службы, удалённые
серверы, отправка данных, права администратора), ORANGE (меняет файлы,
запускает программы, команда не разобрана), YELLOW («опасного не нашёл»,
это не «безопасно»). Bash разбирается на звенья с учётом кавычек, а не
поиском слов по тексту: `echo 'rm -rf x'` не красный, `r''m x` красный.
"""
from __future__ import annotations

import contextvars
import glob
import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import _, N_

RED = "red"
ORANGE = "orange"
YELLOW = "yellow"

_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", ";;", "|&"})
_REDIRECTS = frozenset({">", ">>", "&>", "&>>", ">|", "<>"})
_FD_DUPS = frozenset({">&", "<&", "<", "<<", "<<<"})
# Операторы shell, на которые делится слитный знаковый токен shlex (`;>`,
# `>(`), длинные раньше коротких (ревью 11.10.2026).
_OPERATORS = (
    "&>>", "<<<", ";;&", "&&", "||", "|&", ";;", ";&", ">>", ">|", "&>", ">&", "<&", "<<", "<>",
    ";", "|", "&", "(", ")", "<", ">",
)
_OPERATOR_SET = frozenset(_OPERATORS)
# Рабочий каталог агента из входа хука: относительный путь в команде
# проверяется от него (ревью 11.10.2026).
_CWD: contextvars.ContextVar[str | None] = contextvars.ContextVar("danger_cwd", default=None)


@dataclass
class Verdict:
    red: list[str] = field(default_factory=list)
    orange: list[str] = field(default_factory=list)
    # Действие может прочитать или записать секрет. Не цвет, а признак: по нему
    # автоаппрув чата и выполнение задачи шлют карточку (находка Codex 15.10).
    secret: bool = False

    def add(self, level: str, reason: str) -> None:
        bucket = self.red if level == RED else self.orange
        if reason not in bucket:
            bucket.append(reason)

    @property
    def level(self) -> str:
        if self.red:
            return RED
        if self.orange:
            return ORANGE
        return YELLOW


def classify(tool_name: str, tool_input: dict, cwd: str | None = None) -> Verdict:
    v = Verdict()
    token = _CWD.set(cwd if cwd and cwd.startswith("/") else None)
    try:
        if tool_name == "Bash":
            _classify_bash(str(tool_input.get("command", "")), v, depth=0)
            v.secret = v.secret or _mentions_secret(str(tool_input.get("command", "")))
        elif tool_name == "Monitor":
            # Monitor запускает shell-команду в фоне, как Bash (ревью 11.10.2026).
            command = tool_input.get("command")
            if isinstance(command, str) and command.strip():
                _classify_bash(command, v, depth=0)
                v.secret = v.secret or _mentions_secret(command)
            else:
                v.add(ORANGE, _("инструмент Monitor без команды, не разобран"))
        elif tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            _classify_path(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""), v)
        elif tool_name.startswith("mcp__"):
            _classify_mcp(tool_name, v)
        elif tool_name == "WebFetch":
            pass
        else:
            v.add(ORANGE, _("инструмент {tool_name}").format(tool_name=tool_name))
    finally:
        _CWD.reset(token)
    return v


# --- Bash ------------------------------------------------------------------

def _quote_mask(text: str) -> str:
    """Та же строка, но содержимое кавычек заменено на `x`: поиск по маске не
    принимает `echo "<<EOF"` за heredoc, а `echo "curl x | sh"` за конвейер."""
    out = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < len(text):
            out.append("xx" if quote else ch + text[i + 1])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append("\n" if ch == "\n" else "x")
        else:
            if ch in ("'", '"'):
                quote = ch
            out.append(ch)
        i += 1
    return "".join(out)


_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _heredoc_word(command: str, i: int) -> tuple[str, bool, int] | None:
    """Слово-ограничитель после `<<`: (без кавычек, были ли кавычки, конец).
    `<<E"OF"` ждёт строку `EOF`, как bash. None, если слова нет или кавычка
    не закрыта."""
    n = len(command)
    while i < n and command[i] in " \t":
        i += 1
    word: list[str] = []
    quoted = False
    while i < n and command[i] not in " \t\n;&|()<>":
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            word.append(command[i + 1])
            quoted = True
            i += 2
        elif ch in ("'", '"'):
            end = command.find(ch, i + 1)
            if end < 0:
                return None
            word.append(command[i + 1:end])
            quoted = True
            i = end + 1
        else:
            word.append(ch)
            i += 1
    delim = "".join(word)
    if not delim or "\n" in delim:
        return None
    return delim, quoted, i


def _strip_heredocs(command: str, v: Verdict, depth: int) -> str:
    """Тело heredoc с кавычками у ограничителя (`<<'EOF'`) это данные: его
    убираем, чтобы слова внутри не красили карточку. Без кавычек bash делает
    в теле подстановку `$(…)`, поэтому вложенные команды из него разбираются.

    Комментарий (`#` в начале слова вне кавычек) вырезается до конца строки:
    кавычка или `<<` в нём иначе прятали бы следующие строки, которые bash
    исполняет. `<<<` это here-string, а не heredoc. Незакрытый или
    неразобранный heredoc не вырезается и красит карточку (ревью 11.10.2026)."""
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote: str | None = None
    word_start = True
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(command[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            word_start = False
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            word_start = False
            out.append(ch)
            i += 1
            continue
        if ch == "#" and word_start:
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if command.startswith("<<<", i):
            out.append("<<<")
            word_start = True
            i += 3
            continue
        if command.startswith("<<", i):
            j = i + 3 if command.startswith("<<-", i) else i + 2
            parsed = _heredoc_word(command, j)
            if parsed is not None and not parsed[1] and parsed[0].isdigit():
                # `$((1<<2))`: сдвиг в арифметике, а не heredoc.
                out.append(command[i:j])
                i = j
                continue
            if parsed is None:
                v.add(ORANGE, _("heredoc не разобран"))
                out.append(command[i:j])
                word_start = True
                i = j
                continue
            delim, quoted, end = parsed
            pending.append((delim, quoted))
            out.append(command[i:end])
            word_start = False
            i = end
            continue
        if ch == "\n" and pending:
            out.append("\n")
            body_start = i + 1
            i = body_start
            while pending:
                if i >= n:
                    v.add(ORANGE, _("heredoc не закрыт"))
                    out.append(command[body_start:])
                    return "".join(out)
                end = command.find("\n", i)
                line = command[i:n if end < 0 else end]
                i = n if end < 0 else end + 1
                delim, quoted = pending[0]
                if line.strip() == delim:
                    pending.pop(0)
                elif not quoted:
                    for m in _SUBST_RE.finditer(line):
                        v.add(ORANGE, _("heredoc без кавычек с подстановкой команды"))
                        _classify_bash(m.group(1) or m.group(2) or "", v, depth + 1)
            word_start = True
            continue
        out.append(ch)
        word_start = ch in " \t\n;&|()<>"
        i += 1
    if pending:
        v.add(ORANGE, _("heredoc не закрыт"))
    return "".join(out)


def _newlines_to_separators(command: str) -> str:
    """Перевод строки вне кавычек это конец команды; shlex сам его не видит."""
    out = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'" and i + 1 < len(command):
            out.append(ch + command[i + 1])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _tokenize(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    return [p for t in tokens for p in _split_operators(t)]


def _split_operators(token: str) -> list[str]:
    """shlex склеивает подряд идущие знаки: `true;>~/.ssh/x` даёт `;>`, а
    `> >(cmd)` даёт `>(`. Делим их на операторы shell, иначе запись в файл
    терялась как «разделитель» (ревью 11.10.2026)."""
    if not token or any(c not in "();<>|&" for c in token) or token in _OPERATOR_SET:
        return [token]
    out: list[str] = []
    i = 0
    while i < len(token):
        op = next((o for o in _OPERATORS if token.startswith(o, i)), token[i])
        out.append(op)
        i += len(op)
    return out


def _segments(tokens: list[str], v: Verdict) -> Iterator[list[str]]:
    cur: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        target = tokens[i + 1] if i + 1 < len(tokens) else ""
        # `>&файл` пишет в файл так же, как `>файл 2>&1`; дублирование
        # дескриптора только `>&2` и `>&-` (ревью 11.10.2026).
        if t in _REDIRECTS or (t == ">&" and not (target.isdigit() or target == "-")):
            if cur and cur[-1].isdigit():
                cur.pop()
            if target in _OPERATOR_SET:
                # `> >(cmd)`: вывод уходит процессу, его команда идёт своим звеном.
                v.add(ORANGE, _("пишет вывод в процесс >(…)"))
                i += 1
                continue
            if target != "/dev/null":
                _write_target(target, v)
            i += 2
            continue
        if t in _FD_DUPS:
            if cur and cur[-1].isdigit():
                cur.pop()
            # Входное перенаправление тоже может читать секретную ссылку
            # (ревью 11.10.2026).
            if t == "<" and _secret_path(target, _CWD.get()):
                v.secret = True
                v.add(ORANGE, _("читает секретный файл через перенаправление"))
            i += 1 if target in _OPERATOR_SET else 2
            continue
        if t in _SEPARATORS or (t and all(c in "();|&<>" for c in t) and any(c in "();|&" for c in t)):
            if cur:
                yield cur
            cur = []
            i += 1
            continue
        cur.append(t)
        i += 1
    if cur:
        yield cur


def _classify_bash(command: str, v: Verdict, depth: int) -> None:
    if depth > 2:
        v.add(ORANGE, _("вложенная команда не разобрана"))
        return
    text = _strip_heredocs(command, v, depth)
    if "`" in _quote_mask(text) or "`" in text:
        v.add(ORANGE, _("есть команда в обратных кавычках"))
        text = text.replace("`", " ; ")
    if "$(" in text or "<(" in text or ">(" in text:
        v.add(ORANGE, _("есть вложенная команда $(…), <(…) или >(…)"))
    text = _pipe_to_interpreter(text, v)
    # Шаблон скрытых имён вне кавычек может подставить .env. Внутри
    # программы jq или awk это обычный текст (ревью 11.10.2026).
    masked_tokens = _tokenize(_quote_mask(text)) or []
    if any(part.startswith(".") and glob.has_magic(part)
           for word in masked_tokens for part in word.split("/")):
        v.secret = True
        v.add(ORANGE, _("шаблон может подставить скрытые секретные файлы"))
    tokens = _tokenize(_newlines_to_separators(text))
    if tokens is None:
        v.add(ORANGE, _("команда не разобрана: незакрытая кавычка"))
        return
    token = _CWD.set(_CWD.get())
    try:
        # Генератор читает следующий путь записи после обработки cd
        # предыдущего звена (ревью 11.10.2026).
        for seg in _segments(tokens, v):
            _classify_segment(seg, v, depth)
    finally:
        _CWD.reset(token)


# Скачанное идёт интерпретатору как программа: `| sh`, `| bash -s`, `| python3`
# или `| python3 -`. `| python3 -c '…'` и `| sh -c '…'` лишь читают ввод.
_PIPE_EXEC_RE = re.compile(
    r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+(?:-\S+\s+)*)?"
    r"(?:(?:ba|z|da)?sh|python3?|perl|ruby|node)(?:\s+-s\b|\s+-(?=\s|$))?\s*(?:$|[;&|)\n])"
)


def _pipe_to_interpreter(text: str, v: Verdict) -> str:
    if _PIPE_EXEC_RE.search(_quote_mask(text)):
        v.add(RED, _("скачивает и сразу запускает код"))
    return text


# Обёртка -> (флаги со значением, сколько позиционных до команды).
_WRAPPERS = {
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "--class", "-n", "--classdata", "-p", "--pid"}, 0),
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    # `-d` у watch значение берёт только слитно (`-d=permanent`).
    "watch": ({"-n", "--interval"}, 0),
    "env": ({"-u", "--unset", "-C", "--chdir"}, 0),
    "nohup": (set(), 0), "time": (set(), 0), "command": (set(), 0), "exec": (set(), 0),
    "builtin": (set(), 0), "rtk": (set(), 0), "chronic": (set(), 0),
}
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_XARGS_VALUE_FLAGS = frozenset({
    "-I", "-n", "-L", "-P", "-d", "-a", "-s", "-E", "--max-args", "--max-procs",
    "--delimiter", "--arg-file", "--max-chars", "--process-slot-var",
})
# Слова shell перед командой и закрывающие (ревью 11.10.2026): `do rm -rf x`
# это rm, а не «незнакомая команда do». В `for x in …` слова это значения,
# подстановки в них разобраны своими звеньями.
_SHELL_LEADERS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{"})
_SHELL_CLOSERS = frozenset({"fi", "done", "esac", "}"})
_SHELL_LISTS = frozenset({"for", "select", "case"})
# Переменные окружения, с которыми программа подгружает или запускает чужой
# код: `LD_PRELOAD=x.so ls`, `GIT_EXTERNAL_DIFF=prog git diff`, GIT_CONFIG_*
# с core.fsmonitor равносильны `git -c` (ревью 11.10.2026).
_EXEC_ENV = frozenset({
    "BASH_ENV", "ENV", "PROMPT_COMMAND", "PATH", "IFS", "SHELLOPTS", "BASHOPTS", "PS4",
    "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH", "PERL5OPT",
    "PERL5LIB", "PERLLIB", "RUBYOPT", "RUBYLIB", "PAGER", "GIT_PAGER", "MANPAGER", "EDITOR",
    "VISUAL", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_EXTERNAL_DIFF", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR",
    "GIT_PROXY_COMMAND", "LESSOPEN", "LESSCLOSE",
})
_EXEC_ENV_PREFIXES = ("LD_", "DYLD_", "GIT_CONFIG")
_PLAIN_PAGERS = frozenset({"", "cat", "less", "more"})
# Системные программы плюс конкретные штатные команды. Каталог venv,
# ~/.local/bin или Homebrew сам по себе доверия не даёт. Штатная программа
# доверена, пока её ссылка ведёт туда же, куда при запуске бота
# (ревью 11.10.2026).
_SYSTEM_BIN_DIRS = frozenset({"/bin", "/usr/bin", "/sbin", "/usr/sbin"})
# В Ubuntu 26.04 coreutils это uutils: /usr/bin/ls ведёт в этот каталог пакета
# rust-coreutils. sudo и su из соседнего /usr/lib/cargo/bin сюда не входят.
_SYSTEM_REAL_DIRS = _SYSTEM_BIN_DIRS | {"/usr/lib/cargo/bin/coreutils"}
_KNOWN_PROGRAMS = (
    Path(__file__).resolve().parents[2] / ".venv/bin/python",
    Path.home() / "src/svod/engine/bin/memory",
    Path.home() / "src/svod/engine/bin/memory-context",
)
_KNOWN_TARGETS = {path: path.resolve() for path in _KNOWN_PROGRAMS}


def _program(name: str) -> str:
    return os.path.basename(name)


def _check_assignment(word: str, v: Verdict) -> None:
    name, _unused, value = word.partition("=")
    if name in ("PAGER", "GIT_PAGER", "MANPAGER") and value in _PLAIN_PAGERS:
        return
    if name in _EXEC_ENV or name.startswith(_EXEC_ENV_PREFIXES):
        v.add(ORANGE, _("переменная {name} подгружает или запускает чужой код").format(name=name))


def _check_program_path(name: str, v: Verdict) -> None:
    """Проверяем конкретную программу и цель её ссылки (ревью 11.10.2026)."""
    path = Path(os.path.normpath(os.path.expanduser(name)))
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        real = None
    if real is not None and path.is_absolute() and (
        str(path.parent) in _SYSTEM_BIN_DIRS and str(real.parent) in _SYSTEM_REAL_DIRS
        or path in _KNOWN_TARGETS and real == _KNOWN_TARGETS[path]
    ):
        return
    v.add(ORANGE, _("запускает программу по пути {name}").format(name=name[:60]))


# Общая проверка секретных источников для карточки и автоматического
# разрешения. Содержимое файлов не читается (ревью 11.10.2026).
_SECRET_WORD_RE = re.compile(
    r"(?:^|[/=:@]|^-[A-Za-z0-9]+)\.env(?:\.(?!example(?:/|$))[^/]*)?(?:/|$)"
    r"|\.credentials\.json"
    r"|(?:^|[/=:@])\.(?:ssh|aws|netrc|git-credentials|pgpass|gnupg|kube|npmrc|pypirc)(?:/|$)"
    r"|(?:^|/)environ(?:/|$)"
    r"|\.codex/auth\.json|gh/hosts\.ya?ml|\.docker/config\.json"
    # Облачные токены rclone и истории оболочек (ревью 11.10.2026).
    r"|rclone/rclone\.conf|fish/fish_history"
    r"|(?:^|[/=:@])\.(?:zsh|bash|python|psql|mysql)_history(?:$|/)"
    r"|(?:^|[/=:@])\.[^/]*(?:token|secret|passw|credential)"
    r"|(?:^|/)(?:credentials?(?:\.[^/]*)?|id_(?:rsa|dsa|ecdsa|ed25519)|shadow|gshadow)(?:/|$)"
    # Ключи, сертификаты с ключом, хранилища ключей Java и secrets.yaml
    # (решение владельца 14.10.2026).
    r"|(?:^|[/=:@])secrets\.(?:ya?ml|json)(?:$|/)"
    r"|\.(?:key|pem|p12|pfx|jks|keystore)(?:$|/)",
    re.IGNORECASE,
)


# Слова команды целиком, включая строки внутри кавычек и тела heredoc:
# программа на python или node, читающая `.env`, разбором оболочки не видна.
_MENTION_SPLIT_RE = re.compile(r"[\s'\"()\[\]{},;`<>|&]+")


def _mentions_secret(command: str) -> bool:
    """Секретное имя где угодно в тексте команды (признак Verdict.secret, цвет
    карточки не меняет). Грубо и с запасом: автоаппрув чата и выполнение
    задачи такую команду шлют карточкой (находка Codex 15.10.2026)."""
    return any(_SECRET_WORD_RE.search(word) for word in _MENTION_SPLIT_RE.split(command) if word)


def _secret_path(raw: str, cwd: str | None = None) -> bool:
    if _SECRET_WORD_RE.search(raw):
        return True
    if not raw or raw.startswith("-") or any(c in raw for c in "\n\x00"):
        return False
    try:
        path = Path(raw).expanduser()
        # Ссылки среди совпадений не читают содержимое, но могут передать
        # секрет читалке (ревью 11.10.2026).
        if glob.has_magic(raw) and cwd and any(
                _SECRET_WORD_RE.search(str(Path(cwd, p).resolve()))
                for p in glob.glob(str(path), root_dir=cwd)):
            return True
        if not path.is_absolute():
            if cwd is None:
                return False
            path = Path(cwd) / path
        return bool(_SECRET_WORD_RE.search(str(path.resolve())))
    except (OSError, RuntimeError, ValueError):
        return True


# Флаги grep и rg со значением: значение не имя файла и не шаблон поиска.
# У grep -r и -E без значения, у rg это --replace и --encoding (ревью 11.10.2026).
_GREP_VALUES = frozenset({
    "-e", "--regexp", "-f", "--file", "--include", "--exclude", "--exclude-dir",
    "--exclude-from", "-m", "--max-count", "-A", "--after-context", "-B", "--before-context",
    "-C", "--context", "--label", "-d", "--directories", "-D", "--devices", "--binary-files",
    "--group-separator",
})
_RG_VALUES = frozenset({
    "-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob", "-t", "--type", "-T",
    "--type-not", "--type-add", "--type-clear", "-M", "--max-columns", "-j", "--threads", "-r",
    "--replace", "--pre", "--pre-glob", "-d", "--max-depth", "--max-filesize", "--sort",
    "--sortr", "-E", "--encoding", "--engine", "--ignore-file", "--path-separator",
    "--context-separator", "--field-match-separator", "--field-context-separator", "--colors",
    "--color", "--hostname-bin", "--dfa-size-limit", "--regex-size-limit", "-m", "--max-count",
    "-A", "--after-context", "-B", "--before-context", "-C", "--context",
})
_GREP_PROGS = ("grep", "egrep", "fgrep", "rg", "ripgrep")
# Значения ps: поля -o, номера, имена пользователей (ревью 11.10.2026).
_PS_VALUES = frozenset({
    "-o", "-O", "--format", "-p", "--pid", "-q", "--quick-pid", "-u", "-U", "--user", "--User",
    "-g", "-G", "--group", "--Group", "-t", "--tty", "-C", "--sort", "--ppid", "--sid",
    "--cols", "--columns", "--rows", "--width", "-s",
})


def _rg_roots(args: list[str]) -> list[str]:
    pos = _operands(args, _RG_VALUES)
    explicit = _option_values(args, "e", "--regexp", _RG_VALUES) \
        or _option_values(args, "f", "--file", _RG_VALUES)
    return (pos if explicit else pos[1:]) or ["."]


def _searches_tree(parts: list[str], cwd: str | None = None) -> bool:
    """grep с рекурсией и rg по каталогу без карточки не идут: какие файлы
    прочтёт обход, по именам в дереве не угадать (решение владельца
    14.10.2026). rg без пути ищет в текущем каталоге, путь не к обычному
    файлу считается каталогом. Рекурсия grep: -r или -R в связке, -d и
    --directories со значением recurse или его сокращением от rec,
    --recursive и --dereference-recursive с сокращениями. Сокращение,
    общее с другим флагом (`--re`), grep отвергает, здесь оно тоже
    рекурсия. Цвет карточки от этой проверки не зависит."""
    if not parts:
        return False
    cmd, args = _program(parts[0]), parts[1:]
    if cmd in ("rg", "ripgrep"):
        base = cwd or os.getcwd()
        return not all(os.path.isfile(os.path.join(base, os.path.expanduser(r)))
                       for r in _rg_roots(args))
    if cmd not in ("grep", "egrep", "fgrep"):
        return False
    short = [a for a in args if a.startswith("-") and not a.startswith("--")]
    longs = [a.split("=", 1)[0] for a in args if a.startswith("--")]
    return any("r" in a or "R" in a for a in short) \
        or any(len(n) > 2 and ("--recursive".startswith(n)
                               or "--dereference-recursive".startswith(n)) for n in longs) \
        or any(len(d) >= 3 and "recurse".startswith(d)
               for d in _option_values(args, "d", "--directories", _GREP_VALUES))


def _include_names_secret(pattern: str) -> bool:
    """Включающий шаблон (-g у rg, --include у grep) называет секрет или
    скрытые имена, как шаблон оболочки: `.env`, `*.key`, `.*`
    (решение владельца 14.10.2026)."""
    return bool(_SECRET_WORD_RE.search(pattern)) or any(
        part.startswith(".") and glob.has_magic(part) for part in pattern.split("/"))


def _reads_secret(parts: list[str], cwd: str | None = None) -> bool:
    if not parts:
        return False
    cmd, args = _program(parts[0]), parts[1:]
    # Шаблоны фильтров и шаблон поиска не имена читаемых файлов:
    # `grep -rn credentials src` ищет слово. rg ниже отдельно проверяет
    # включение скрытых файлов (ревью 11.10.2026).
    skip: set[int] = set()
    if cmd in _GREP_PROGS:
        values = _RG_VALUES if cmd in ("rg", "ripgrep") else _GREP_VALUES
        files = ("-f", "--file", "--ignore-file", "--exclude-from")
        pattern_seen = bool(_option_values(args, "e", "--regexp", values)
                            or _option_values(args, "f", "--file", values))
        i = 1
        while i < len(parts):
            a = parts[i]
            if a == "--":
                if not pattern_seen:
                    skip.add(i + 1)
                break
            if a.startswith("--"):
                name = a.split("=", 1)[0]
                if "=" in a:
                    if name not in files:
                        skip.add(i)
                elif a in values:
                    if a not in files:
                        skip.add(i + 1)
                    i += 2
                    continue
            elif a.startswith("-") and len(a) > 1:
                k = next((j for j, c in enumerate(a[1:], 1) if "-" + c in values), None)
                if k is not None and "-" + a[k] not in files:
                    skip.add(i if k < len(a) - 1 else i + 1)
                if k is not None and k == len(a) - 1:
                    i += 2
                    continue
            elif not pattern_seen:
                skip.add(i)
                pattern_seen = True
            i += 1
    for i, p in enumerate(parts[1:], 1):
        if i not in skip and _secret_path(p, cwd):
            return True
    short = [a for a in args if a.startswith("-") and not a.startswith("--")]
    if cmd in ("rg", "ripgrep"):
        # Одиночный -u уже возвращает файлы из .gitignore и .ignore, как
        # --no-ignore-vcs и --no-ignore-dot (проверено rg 14.1.1, ревью 11.10.2026).
        globs = _option_values(args, "g", "--glob", _RG_VALUES) \
            + _option_values(args, "", "--iglob", _RG_VALUES)
        return any(a.startswith(("--hid", "--no-ignore", "--follow", "--unrestricted"))
                   for a in args) or any("." in a or "L" in a or "u" in a for a in short) \
            or any(_include_names_secret(g) for g in globs)
    if cmd in ("grep", "egrep", "fgrep"):
        return any(_include_names_secret(g)
                   for g in _option_values(args, "", "--include", _GREP_VALUES))
    if cmd == "diff":
        return any(a.startswith("--rec") for a in args) or any("r" in a for a in short)
    if cmd == "jq":
        return any(re.search(r"\b(?:env|ENV)\b", a) for a in args)
    if cmd in ("awk", "gawk", "mawk"):
        return any(re.search(r"\b(?:ARGV|ARGC|ENVIRON|PROCINFO)\b", a) for a in args)
    if cmd == "ps":
        # BSD e показывает окружение, GNU -e только выбирает все процессы.
        # Значения флагов (`-o pid,user`, `-u steve`) не флаги (ревью 11.10.2026).
        i = 0
        while i < len(args):
            a = args[i]
            if a in _PS_VALUES:
                i += 2
                continue
            if not a.startswith("-") and a.isalpha() and "e" in a:
                return True
            if a.startswith("-") and not a.startswith("--") and "e" in a and "w" in a:
                return True
            # macOS -E (отдельно и в связке) печатает окружение, как BSD e.
            # Хвост после флага со значением (`-UEVE`) это значение (ревью 11.10.2026).
            if a.startswith("-") and not a.startswith("--"):
                k = next((j for j, c in enumerate(a[1:], 1) if "-" + c in _PS_VALUES), len(a))
                if "E" in a[1:k]:
                    return True
                if k == len(a) - 1:
                    i += 2
                    continue
            i += 1
        return "--environment" in args or any(
            any(f.split("=", 1)[0] in ("e", "env", "environ", "environment")
                for f in re.split(r"[, ]+", fmt))
            for fmt in _option_values(args, "o", "--format", _PS_VALUES)
            + _option_values(args, "O", "", _PS_VALUES))
    return cmd in ("env", "printenv")


def _system_read_ok(prog: str, args: list[str]) -> bool:
    if prog == "hostname":
        return all(a in ("-f", "--fqdn", "--long", "-s", "--short", "-i", "--ip-address",
                         "-I", "--all-ip-addresses", "-d", "--domain", "-a", "--alias",
                         "-A", "--all-fqdns", "--help", "--version") for a in args)
    if prog == "date":
        return _date_read_ok(args)
    return False


def _date_read_ok(args: list[str]) -> bool:
    """date только показывает время. BSD/macOS -v (сдвиг), -j (не ставить
    время), -f (формат разбора), -r, -z, -u, -I и +ФОРМАТ без карточки;
    установка времени (-s, --set, операнд MMDDhhmm[[CC]YY] без -j) и GNU
    -f ФАЙЛ (без -j это чтение файла) идут на карточку (ревью 11.10.2026)."""
    no_set = parse = False
    operands: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name in ("--date", "--reference"):
                i += 1 if "=" in a else 2
                continue
            if name not in ("--iso-8601", "--rfc-3339") and a not in (
                    "--utc", "--universal", "--rfc-email", "--help", "--version", "--debug"):
                return False
        elif a.startswith("-") and len(a) > 1:
            for k, c in enumerate(a[1:], 1):
                if c in "jnRu":
                    no_set = no_set or c == "j"
                    continue
                if c == "I":
                    break
                if c in "vfrzd":
                    parse = parse or c == "f"
                    if k == len(a) - 1:
                        i += 1
                    break
                return False
        elif not a.startswith("+"):
            operands.append(a)
        i += 1
    return no_set or not (operands or parse)


def _h_system_identity(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if not _system_read_ok(prog, args):
        v.add(ORANGE, _("меняет системные настройки ({prog})").format(prog=prog))


def _h_file(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if "-C" in _flags(args) or any(a.startswith("--compile") for a in args):
        v.add(ORANGE, _("компилирует базу file и пишет magic.mgc"))


def _classify_segment(seg: list[str], v: Verdict, depth: int) -> None:
    parts = list(seg)
    if parts and all(p in _SHELL_CLOSERS for p in parts):
        return
    while parts:
        head = parts[0]
        prog = _program(head)
        if head in _SHELL_LEADERS:
            parts = parts[1:]
            continue
        if head in _SHELL_LISTS:
            return
        if _ASSIGN_RE.match(head):
            _check_assignment(head, v)
            parts = parts[1:]
            continue
        if "/" in head:
            _check_program_path(head, v)
        if prog in ("sudo", "doas"):
            v.add(RED, _("с правами администратора"))
            rest = parts[1:]
            while rest and rest[0].startswith("-"):
                takes_value = rest[0] in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U")
                rest = rest[2:] if takes_value else rest[1:]
            parts = rest
            continue
        if prog == "su":
            v.add(RED, _("с правами администратора"))
            if "-c" in parts:
                idx = parts.index("-c")
                if idx + 1 < len(parts):
                    _classify_bash(parts[idx + 1], v, depth + 1)
            return
        if prog in _WRAPPERS:
            value_flags, skip = _WRAPPERS[prog]
            rest = parts[1:]
            # watch склеивает аргументы и отдаёт их `sh -c`, с -x запускает как есть.
            as_string = prog == "watch"
            while rest and (rest[0].startswith("-") or (prog == "env" and _ASSIGN_RE.match(rest[0]))):
                flag = rest[0]
                if prog == "env" and flag in ("-S", "--split-string"):
                    v.add(ORANGE, _("env -S: команда из строки, не разобрана"))
                    return
                if prog == "env" and _ASSIGN_RE.match(flag):
                    _check_assignment(flag, v)
                if prog == "watch" and flag in ("-x", "--exec"):
                    as_string = False
                rest = rest[2:] if flag in value_flags else rest[1:]
            if prog == "rtk" and rest[:1] == ["proxy"]:
                rest = rest[1:]
            if as_string:
                if rest:
                    _classify_bash(" ".join(rest), v, depth + 1)
                return
            parts = rest[skip:]
            continue
        if prog == "xargs":
            v.add(ORANGE, _("запускает команду для каждой строки ввода (xargs)"))
            rest = parts[1:]
            i = 0
            while i < len(rest) and rest[i].startswith("-"):
                a = rest[i]
                if a == "--":
                    i += 1
                    break
                if a in _XARGS_VALUE_FLAGS:
                    i += 2
                elif a.startswith("--"):
                    i += 1
                else:
                    k = next((j for j, c in enumerate(a[1:], 1)
                              if "-" + c in _XARGS_VALUE_FLAGS), None)
                    i += 2 if k is not None and k == len(a) - 1 else 1
            parts = rest[i:]
            continue
        break
    if not parts:
        return
    prog = _program(parts[0])
    args = parts[1:]
    if prog.startswith("$"):
        v.add(ORANGE, _("команда из переменной, не разобрана"))
        return
    token = None
    if prog == "git":
        # git -C меняет основу относительных путей только в этом вызове
        # (ревью 11.10.2026).
        token = _CWD.set(_CWD.get())
        sub_at = len(args) - len(_git_sub(args)[1]) - 1
        for i, a in enumerate(args[:max(sub_at, 0)]):
            if a == "-C" and i + 1 < len(args):
                _CWD.set(_absolute(args[i + 1]))
    try:
        if _reads_secret(parts, _CWD.get()):
            v.secret = True
            v.add(ORANGE, _("может вывести секреты или окружение процессов"))
        if prog == "cd":
            target = next((a for a in args if a != "--"), str(Path.home()))
            _CWD.set(None if target.startswith("-") else _absolute(target))
        handler = _HANDLERS.get(prog)
        if handler is not None:
            handler(prog, args, v, depth)
            return
        if prog.startswith("mkfs"):
            v.add(RED, _("форматирует диск"))
        elif prog not in _READ_ONLY:
            v.add(ORANGE, _("незнакомая команда {prog}: что делает, не знаю").format(prog=prog[:40]))
    finally:
        if token is not None:
            _CWD.reset(token)


_READ_ONLY = frozenset({
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "wc", "echo", "printf", "read",
    "pwd", "cd", "date", "whoami", "hostname", "uname", "uptime", "which", "type",
    "file", "stat", "du", "df", "free", "cut", "sort", "column", "tr", "true",
    "false", "jq", "realpath", "readlink", "basename", "dirname", "diff", "cmp",
    "less", "more", "fd", "fdfind", "find", "sleep", "test", "[", "env",
    "id", "ps", "pgrep", "lsof", "ss", "netstat", "ip", "ping", "dig", "host",
    "nslookup", "journalctl", "md5sum", "sha256sum", "base64", "od", "awk",
    "sed", "tee", "nl", "paste", "join", "comm", "fold", "fmt", "rev", "seq",
    "yes", "expr", "bc", "locale", "getent", "groups", "last", "w", "who", "top",
    "htop", "nproc", "lscpu", "lsblk", "findmnt", "blkid", "printenv", "true", ":",
    "nvidia-smi", "lsusb", "lspci", "dmesg", "ss", "curl", "wget", "zcat", "zgrep",
    "bzcat", "xzcat", "strings", "hexdump", "pstree", "vmstat", "iostat",
    "date", "cal", "tput", "clear", "column", "numfmt",
})


def _flags(args: list[str]) -> set[str]:
    out: set[str] = set()
    for a in args:
        if a.startswith("--"):
            out.add(a.split("=", 1)[0])
        elif a.startswith("-") and len(a) > 1:
            out.add(a)
            out.update(f"-{c}" for c in a[1:])
    return out


def _positionals(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _operands(args: list[str], value_flags: frozenset[str]) -> list[str]:
    """Позиционные аргументы без значений флагов из `value_flags` (`uniq -f 2`)."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i + 1:])
            break
        if a in value_flags:
            i += 2
            continue
        # Связка -cf 2 съедает 2 так же, как -f 2; -cf2 уже содержит
        # значение. Оно не становится операндом (ревью 11.10.2026).
        if a.startswith("-") and not a.startswith("--"):
            k = next((j for j, c in enumerate(a[1:], 1) if "-" + c in value_flags), None)
            if k is not None:
                i += 2 if k == len(a) - 1 else 1
                continue
        if not a.startswith("-") or a == "-":
            out.append(a)
        i += 1
    return out


def _option_values(args: list[str], short: str, long: str,
                   value_flags: frozenset[str] = frozenset()) -> list[str]:
    """Значения опции отдельно, слитно и в связке. Значение предыдущего
    флага не становится следующей опцией (ревью 11.10.2026)."""
    out: list[str] = []
    values = value_flags | {"-" + short, long}
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if a == "--":
            break
        if a.startswith("--"):
            # Точное имя раньше сокращения: `--exclude=x` не `--exclude-dir`
            # (ревью 11.10.2026).
            option = name if name in values else next(
                (f for f in values if f.startswith("--") and len(name) >= 3
                 and f.startswith(name)), None)
            if option is not None:
                if not eq:
                    i += 1
                    val = args[i] if i < len(args) else ""
                if option == long:
                    out.append(val)
        elif a.startswith("-"):
            k = next((j for j, c in enumerate(a[1:], 1) if "-" + c in values), None)
            if k is not None:
                val = a[k + 1:]
                if not val:
                    i += 1
                    val = args[i] if i < len(args) else ""
                if a[k] == short:
                    out.append(val)
        i += 1
    return out


def _write_target(target: str, v: Verdict, reason: str = N_("пишет вывод в файл")) -> None:
    if target in ("", "-", "/dev/null"):
        return
    if _protected_path(target):
        v.add(RED, _("перезаписывает {target}").format(target=target))
    else:
        v.add(ORANGE, _(reason))


def _h_delete(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    targets = _positionals(args)
    if prog == "rm" and targets and all(os.path.normpath(t).startswith("/tmp/") for t in targets):
        v.add(ORANGE, _("удаляет временные файлы в /tmp"))
        return
    if prog == "dd":
        v.add(RED, _("пишет на диск или в файл напрямую (dd)"))
        return
    v.add(RED, _("удаляет или затирает файлы"))


_TAR_EXEC_OPTS = ("--to-command", "--checkpoint-action", "--use-compress-program", "--info-script",
                  "--new-volume-script", "--rsh-command", "--rmt-command")


def _h_write(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # У GNU tar `-l` это --check-links, а не список; программу tar запускает
    # и при просмотре (ревью 11.10.2026).
    if prog == "tar" and (any(a.startswith(_TAR_EXEC_OPTS) for a in args)
                          or {"-I", "-F"} & _flags(args)):
        v.add(ORANGE, _("tar запускает внешнюю программу"))
    if prog == "tar" and ({"-t", "--list"} & _flags(args)):
        return
    if prog == "unzip" and ({"-t", "--list", "-l"} & _flags(args)):
        return
    targets = _positionals(args)
    if prog in ("cp", "install", "ln", "rsync", "tar", "unzip", "patch"):
        touched = targets[-1:] if prog in ("cp", "install", "ln", "rsync") else []
    else:
        touched = targets
    if any(_protected_path(t) for t in touched):
        v.add(RED, _("меняет ключи, настройки или код бота"))
    if prog == "rsync" and any(":" in t and not t.startswith("/") for t in targets):
        v.add(RED, _("копирует на удалённый сервер или с него"))
    if "--delete" in _flags(args) or any(a.startswith("--delete") for a in args):
        v.add(RED, _("удаляет файлы в месте назначения (--delete)"))
    v.add(ORANGE, _("копирует, перемещает или создаёт файлы"))


def _h_sed(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    flags = _flags(args)
    if "-i" in flags or "--in-place" in flags or any(a.startswith("-i") for a in args):
        v.add(ORANGE, _("правит файлы на месте"))
    for a in args:
        if re.search(r"(^|[;}\s])[0-9,$]*\s*[we]\s|/[we]\s+\S", a):
            v.add(RED, _("скрипт sed пишет файл или запускает команду"))
    # Команды w, W (файл) и e (запуск) после адреса без пробела, флаги w и e у
    # s///: `1e id`, `$Wf`, `s/a/b/e`. Смотрим только скрипт, а не имена
    # файлов (ревью 11.10.2026).
    scripts = _option_values(args, "e", "--expression")
    if not scripts and not _option_values(args, "f", "--file"):
        scripts = [a for a in args if not a.startswith("-")][:1]
    for script in scripts:
        if _SED_EXEC_RE.search(script):
            v.add(RED, _("скрипт sed пишет файл или запускает команду"))


_SED_EXEC_RE = re.compile(
    r"(?:^|[;{}\n])\s*(?:[0-9,$~+]+|/[^/]*/)?\s*!?\s*"
    r"(?:[wW]\s*\S|e(?:$|[\s;}])|s(.).*?\1.*?\1[gpiImM0-9]*(?:e|w\s*\S))"
)


def _h_tee(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    targets = [t for t in _positionals(args) if t != "/dev/null"]
    if any(_protected_path(t) for t in targets):
        v.add(RED, _("меняет ключи, настройки или код бота"))
    if targets:
        v.add(ORANGE, _("пишет вывод в файл"))


def _h_find(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if any(a in ("-delete",) for a in args):
        v.add(RED, _("удаляет найденные файлы"))
    if any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args):
        v.add(ORANGE, _("запускает команду для найденных файлов"))
        for i, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                inner = []
                for b in args[i + 1:]:
                    if b in (";", "+", "\\;"):
                        break
                    inner.append(b)
                if inner:
                    _classify_segment(inner, v, depth + 1)
    if any(a.startswith("-fprint") or a == "-fls" for a in args):
        v.add(ORANGE, _("пишет вывод в файл"))


def _awk_writes(program: str) -> bool:
    """В print сравнение внутри скобок не перенаправление. Строки и
    регулярки пропускаются посимвольно (ревью 11.10.2026)."""
    active = False
    parens = 0
    i = 0
    previous = ""
    while i < len(program):
        ch = program[i]
        if ch == '"' or ch == "/" and (not previous or previous in "~(=,{;" or previous == "print"):
            delim = ch
            i += 1
            brackets = False
            while i < len(program):
                c = program[i]
                if c == "\\":
                    i += 2
                    continue
                if delim == "/" and c == "[":
                    brackets = True
                elif delim == "/" and c == "]":
                    brackets = False
                elif c == delim and not brackets:
                    break
                i += 1
            if i >= len(program):
                return True
            previous = "literal"
        elif ch.isalpha() or ch == "_":
            end = i + 1
            while end < len(program) and (program[end].isalnum() or program[end] == "_"):
                end += 1
            previous = program[i:end]
            if previous in ("print", "printf"):
                active = True
            i = end
            continue
        elif ch == "(":
            parens += 1
            previous = ch
        elif ch == ")":
            parens -= 1
            if parens < 0:
                return True
            previous = ch
        elif ch in ";{}\n":
            active = False
            previous = ch
        elif ch == ">" and active and parens == 0:
            return True
        elif not ch.isspace():
            previous = ch
        i += 1
    return parens != 0


def _h_awk(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # Программа это первое слово после флагов (`-F x`, `-v a=b`); с `-f` её
    # берут из файла, и что в ней, не видно (ревью 11.10.2026).
    program = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--file", "-e", "--source", "-i", "--include", "-l", "--load", "-E", "--exec") \
                or a.startswith(("-f", "--file=", "--include=", "--load=", "--exec=")):
            v.add(ORANGE, _("awk берёт программу или код из файла"))
            return
        if a in ("-F", "-v", "--field-separator", "--assign"):
            i += 2
            continue
        if a.startswith("-") and a != "-":
            i += 1
            continue
        program = a
        break
    if program is None:
        return
    # Общий разбор отличает сравнение от записи (ревью 11.10.2026).
    if re.search(r"\bsystem\s*\(|(?<!\|)\|(?!\|)", program):
        v.add(ORANGE, _("awk запускает команды (system или конвейер)"))
    if _awk_writes(program):
        v.add(ORANGE, _("awk пишет в файл"))


def _h_fd(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    for i, a in enumerate(args):
        if a in ("-x", "--exec", "-X", "--exec-batch"):
            v.add(ORANGE, _("запускает команду для найденных файлов"))
            _classify_segment(args[i + 1:], v, depth + 1)
            return


def _h_ip(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    if len(pos) >= 2 and pos[1] in ("set", "add", "del", "delete", "flush", "change", "replace",
                                     "append", "prepend"):
        v.add(RED, _("меняет настройки сети (ip {pos0} {pos1})").format(pos0=pos[0], pos1=pos[1]))


def _h_sort(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    flags = _flags(args)
    if "-o" in flags or "--output" in flags:
        v.add(ORANGE, _("пишет вывод в файл"))
    # `-o F` у sort, shuf, iconv и tree, сокращение GNU `--out=F`; защищённый
    # путь красный (ревью 11.10.2026).
    for target in _option_values(args, "o", "--output" if prog != "tree" else ""):
        _write_target(target, v)
    if any(a.startswith("--compress-prog") for a in args):
        v.add(ORANGE, _("sort запускает внешнюю программу"))


# Второй операнд uniq и xxd это файл вывода (ревью 11.10.2026).
_SECOND_OPERAND_VALUES = {
    "uniq": frozenset({"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}),
    "xxd": frozenset({"-c", "-cols", "-g", "-groupsize", "-l", "-len", "-s", "-seek", "-o",
                      "-offset", "-n", "-name"}),
}


def _h_second_operand(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    operands = _operands(args, _SECOND_OPERAND_VALUES[prog])
    # Шаблон может дать два операнда и превратить второй в цель записи
    # (ревью 11.10.2026).
    if prog == "uniq" and any(glob.has_magic(a) for a in operands):
        cwd = _CWD.get()
        if cwd and all(not glob.has_magic(a) or a.startswith("./") for a in operands):
            operands = [p for a in operands for p in (glob.glob(a, root_dir=cwd) or [a])]
        else:
            v.add(ORANGE, _("шаблон uniq может подставить файл вывода"))
    if len(operands) >= 2:
        _write_target(operands[1], v)


def _h_gzip(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    """gzip и gunzip без -c, -l, -t заменяют файл сжатым или распакованным."""
    flags = _flags(args)
    if {"-c", "--stdout", "--to-stdout", "-l", "--list", "-t", "--test", "-h", "--help",
            "-V", "--version"} & flags:
        return
    targets = _positionals(args)
    if not targets:
        return
    if any(_protected_path(t) for t in targets):
        v.add(RED, _("меняет ключи, настройки или код бота"))
    v.add(ORANGE, _("заменяет файлы сжатыми или распакованными ({prog})").format(prog=prog))


def _h_openssl(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    for i, a in enumerate(args):
        if a in ("-out", "-keyout") and i + 1 < len(args):
            _write_target(args[i + 1], v)
    pos = _positionals(args)
    if pos and pos[0] in ("s_client", "s_server", "s_time", "ocsp"):
        v.add(ORANGE, _("соединяется по сети (openssl {pos0})").format(pos0=pos[0]))


def _h_rg(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if any(a.startswith(("--pre", "--hostname-bin")) for a in args):
        v.add(ORANGE, _("rg запускает внешнюю программу (--pre)"))


# Системные настройки через systemd и D-Bus: читающие глаголы списком,
# остальное меняет машину (ревью 11.10.2026).
_SYSTEM_TOOL_READ = {
    "busctl": frozenset({"list", "status", "monitor", "capture", "tree", "introspect",
                         "get-property", "help"}),
    "loginctl": frozenset({"list-sessions", "session-status", "show-session", "list-users",
                           "user-status", "show-user", "list-seats", "seat-status", "show-seat",
                           "help"}),
    "hostnamectl": frozenset({"status", "help"}),
    "timedatectl": frozenset({"status", "show", "list-timezones", "timesync-status",
                              "show-timesync", "help"}),
    "localectl": frozenset({"status", "list-locales", "list-keymaps", "list-x11-keymap-models",
                            "list-x11-keymap-layouts", "list-x11-keymap-variants",
                            "list-x11-keymap-options", "help"}),
    "resolvectl": frozenset({"query", "status", "statistics", "show-cache", "show-server-state",
                             "monitor", "service", "openpgp", "tlsa", "help"}),
    "networkctl": frozenset({"list", "status", "lldp", "label", "cat", "help"}),
}


def _h_system_tool(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    if not pos or pos[0] in _SYSTEM_TOOL_READ[prog]:
        return
    if prog == "hostnamectl" and len(pos) == 1 and pos[0] in (
            "hostname", "icon-name", "chassis", "deployment", "location"):
        return
    v.add(RED, _("меняет системные настройки ({prog} {pos0})").format(prog=prog, pos0=pos[0]))


def _git_sub(args: list[str]) -> tuple[str, list[str], bool]:
    i = 0
    has_config = False
    while i < len(args):
        a = args[i]
        # `--config-env=core.fsmonitor=VAR` равносильно `-c` (ревью 11.10.2026).
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"):
            if a in ("-c", "--config-env"):
                has_config = True
            i += 2
            continue
        if a.startswith(("--git-dir=", "--work-tree=", "-c", "--config-env=")):
            has_config = has_config or a.startswith(("-c", "--config-env="))
            i += 1
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:], has_config
    return "", [], has_config


_GIT_RED = N_("меняет историю git или выбрасывает изменения")


def _h_git(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    sub, rest, has_config = _git_sub(args)
    flags = _flags(rest)
    dry = "--dry-run" in flags or "-n" in flags
    if has_config:
        v.add(ORANGE, _("git с разовыми настройками (-c)"))
    # --no-index обходит каталоги вместе со скрытыми файлами. Для обычного
    # сравнения двух названных файлов карточка не нужна (ревью 11.10.2026).
    if sub == "diff" and "--no-index" in rest and any(
            (absolute := _absolute(p)) and Path(absolute).is_dir() for p in _positionals(rest)):
        v.add(ORANGE, _("сравнивает каталоги вместе со скрытыми файлами"))
    if sub == "push" or (sub == "lfs" and rest[:1] == ["push"]):
        if not dry:
            v.add(RED, _("публикует в git"))
        return
    # Отправка наружу и запуск программ (ревью 11.10.2026).
    if sub in ("send-email", "send-pack", "http-push", "daemon", "imap-send") or (
            sub == "svn" and rest[:1] == ["dcommit"]):
        v.add(RED, _("отправляет наружу (git {sub})").format(sub=sub))
        return
    if any(a.startswith(("--upload-pack", "--receive-pack", "--exec", "--open-files-in-pager"))
           for a in rest) or (sub == "grep" and "-O" in flags):
        v.add(ORANGE, _("git запускает внешнюю программу"))
    if sub == "archive":
        for target in _option_values(rest, "o", "--output"):
            _write_target(target, v)
    if sub == "reset" and "--hard" in flags:
        v.add(RED, _(_GIT_RED))
    elif sub == "checkout-index" and ({"-f", "--force"} & flags):
        v.add(RED, _(_GIT_RED))
    elif sub == "reset":
        v.add(ORANGE, _("меняет репозиторий (git reset)"))
    elif sub in ("read-tree", "update-index", "checkout-index", "format-patch") or (
            sub == "bundle" and rest[:1] == ["create"]) or (
            sub == "symbolic-ref" and (len(_positionals(rest)) >= 2 or {"-d", "--delete"} & flags)):
        v.add(ORANGE, _("меняет репозиторий или пишет файлы (git {sub})").format(sub=sub))
    elif sub == "clean" and not dry:
        v.add(RED, _("удаляет неотслеживаемые файлы (git clean)"))
    elif sub == "checkout" and ("-f" in flags or "--force" in flags or "--" in rest or "." in rest):
        v.add(RED, _(_GIT_RED))
    elif sub == "restore":
        v.add(RED, _(_GIT_RED))
    elif sub == "switch" and ("--discard-changes" in flags or "-f" in flags or "--force" in flags):
        v.add(RED, _(_GIT_RED))
    elif sub == "branch" and ({"-D", "-d", "--delete", "-M", "-m", "--force", "-f"} & flags):
        v.add(RED, _("удаляет или переписывает ветки"))
    elif sub == "reflog" and rest and rest[0] in ("expire", "delete"):
        v.add(RED, _(_GIT_RED))
    elif sub in ("rebase", "filter-branch", "filter-repo", "gc", "prune", "update-ref"):
        v.add(RED, _(_GIT_RED))
    elif sub == "commit" and "--amend" in flags:
        v.add(RED, _("переписывает последний коммит"))
    elif sub == "stash" and rest and rest[0] in ("drop", "clear", "pop"):
        v.add(RED, _(_GIT_RED))
    elif sub == "worktree" and rest and rest[0] in ("remove", "prune"):
        v.add(RED, _("удаляет рабочие копии git"))
    elif sub == "tag" and ({"-d", "--delete", "-f", "--force"} & flags):
        v.add(RED, _("удаляет или переписывает метки git"))
    elif sub == "remote" and rest and rest[0] in ("add", "remove", "rm", "set-url", "rename"):
        v.add(ORANGE, _("меняет настройки удалённых репозиториев"))
    elif sub == "config" and not ({"--get", "--list", "-l", "--get-all", "--get-regexp"} & flags):
        v.add(ORANGE, _("меняет настройки git"))
    elif sub in ("commit", "add", "merge", "pull", "fetch", "am", "cherry-pick", "revert", "mv",
                 "rm", "init", "clone", "submodule", "apply", "checkout", "switch", "stash", "tag",
                 "notes", "worktree", "sparse-checkout", "lfs", "maintenance"):
        v.add(ORANGE, _("меняет репозиторий (git {sub})").format(sub=sub))
    if any(a.startswith("--output") for a in rest):
        v.add(ORANGE, _("пишет вывод в файл"))


_SVC_ACTIONS = frozenset({
    "start", "stop", "restart", "reload", "try-restart", "reload-or-restart", "kill",
    "disable", "enable", "mask", "unmask", "edit", "set-property", "daemon-reload",
    "daemon-reexec", "isolate", "reboot", "poweroff", "halt", "suspend", "hibernate",
    "revert", "preset", "link", "reset-failed", "set-environment", "unset-environment",
    "import-environment", "default", "rescue", "emergency", "exit", "soft-reboot", "clean",
    "freeze", "thaw",
    # ревью 11.10.2026
    "force-reload", "try-reload-or-restart", "condrestart", "condreload", "condstop", "reenable",
    "add-wants", "add-requires", "cancel", "suspend-then-hibernate", "hybrid-sleep", "sleep",
    "kexec", "switch-root", "bind", "mount-image", "set-default", "preset-all",
    "log-level", "log-target", "service-watchdogs", "enqueue-marked-jobs",
})


def _h_systemctl(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if any(a.startswith(("--host", "--machine")) or a in ("-H", "-M") for a in args):
        v.add(RED, _("на другой машине (systemctl --host)"))
    acts = [a for a in _positionals(args) if a in _SVC_ACTIONS]
    if acts:
        v.add(RED, _("управляет службами (systemctl {acts0})").format(acts0=acts[0]))


def _h_service(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    if len(pos) >= 2 and pos[1] not in ("status",):
        v.add(RED, _("управляет службами (service {pos1})").format(pos1=pos[1]))


def _h_power(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    v.add(RED, _("перезагружает или выключает машину"))


def _h_kill(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    flags = _flags(args)
    if prog == "kill" and ("-l" in flags or "-L" in flags):
        return
    v.add(RED, _("завершает процессы"))


_JOURNAL_WRITE_OPTS = ("--vacuum-time", "--vacuum-size", "--vacuum-files", "--rotate", "--flush",
                       "--sync", "--relinquish-var", "--smart-relinquish-var", "--setup-keys",
                       "--update-catalog")


def _h_journalctl(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # getopt принимает однозначное сокращение: `--rot` это `--rotate`
    # (ревью 11.10.2026).
    for a in args:
        name = a.split("=", 1)[0]
        if a.startswith(("--vacuum", "--rotate", "--flush", "--sync", "--relinquish-var",
                         "--smart-relinquish-var", "--setup-keys", "--update-catalog")) or (
                len(name) >= 5 and any(o.startswith(name) for o in _JOURNAL_WRITE_OPTS)):
            v.add(RED, _("удаляет или переписывает системные журналы"))
            return


def _h_docker(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    if prog == "docker-compose":
        pos = ["compose", *pos]
    if not pos:
        return
    read = {"ps", "images", "logs", "inspect", "version", "info", "stats", "top", "port",
            "events", "history", "search", "diff", "config", "ls", "context"}
    words = set(pos[:3])
    if pos[0] == "compose":
        # Значения флагов (`-f c.yml`) попадают в позиционные: берём первое
        # известное действие compose.
        acts = {"up", "down", "rm", "stop", "restart", "kill", "pull", "build", "run", "exec",
                "create", "start", "pause", "unpause", "push", "scale", "cp", "watch"}
        sub = next((w for w in pos[1:] if w in acts), "")
        if sub:
            v.add(RED, _("управляет контейнерами (docker compose {sub})").format(sub=sub))
        return
    if pos[0] in ("volume", "network", "image", "container", "system", "builder", "buildx"):
        sub = pos[1] if len(pos) > 1 else ""
        if sub and sub not in {"ls", "list", "inspect", "logs", "ps", "top", "stats", "history",
                               "port", "diff", "df", "info"}:
            v.add(RED, _("управляет ресурсами Docker ({pos0} {sub})").format(pos0=pos[0], sub=sub))
        return
    if pos[0] not in read:
        v.add(RED, _("управляет контейнерами (docker {pos0})").format(pos0=pos[0]))


# Флаги kubectl и helm со значением: значение не глагол, `-n get delete`
# удаляет (ревью 11.10.2026).
_KUBE_VALUES = frozenset({
    "-n", "--namespace", "--context", "--kube-context", "--kubeconfig", "--cluster", "--user",
    "-l", "--selector", "-o", "--output", "-f", "--filename", "-c", "--container", "-s",
    "--server", "--token", "--as", "--as-group", "--request-timeout", "--field-selector",
    "--cache-dir", "--certificate-authority", "--client-certificate", "--client-key",
    "--tls-server-name", "-k", "--kustomize", "--template", "--sort-by", "--values",
    "--set", "--set-string", "--set-file", "--version", "--repo", "--registry-config",
    "--repository-cache", "--repository-config", "--kube-apiserver", "--kube-as-user",
    "--kube-as-group", "--kube-token", "--kube-ca-file", "--burst-limit", "--timeout",
    "--description", "--max", "--revision",
})


def _h_kubectl(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _operands(args, _KUBE_VALUES)
    read = {"get", "describe", "logs", "top", "explain", "api-resources", "api-versions",
            "version", "cluster-info", "config", "auth", "diff", "events", "wait"}
    if pos and pos[0] not in read:
        v.add(RED, _("меняет кластер Kubernetes (kubectl {pos0})").format(pos0=pos[0]))


def _h_helm(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _operands(args, _KUBE_VALUES)
    read = {"list", "ls", "status", "get", "history", "show", "search", "template", "lint",
            "version", "env", "repo", "dependency", "verify"}
    if pos and pos[0] not in read:
        v.add(RED, _("меняет установку в кластере (helm {pos0})").format(pos0=pos[0]))


def _h_terraform(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    # `terraform test` создаёт настоящие ресурсы, `workspace delete` удаляет
    # состояние (ревью 11.10.2026).
    if pos and (pos[0] in ("apply", "destroy", "import", "taint", "untaint", "state", "force-unlock",
                           "test")
                or pos[:2] == ["workspace", "delete"]):
        what = " ".join(pos[:2]) if pos[0] == "workspace" else pos[0]
        v.add(RED, _("меняет облачную инфраструктуру (terraform {what})").format(what=what))
    elif pos[:1] == ["workspace"] and pos[1:2] and pos[1] in ("new", "select"):
        v.add(ORANGE, f"terraform workspace {pos[1]}")
    elif pos and pos[0] in ("init", "plan", "refresh"):
        v.add(ORANGE, f"terraform {pos[0]}")


def _h_ansible(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    flags = _flags(args)
    if "--check" in flags or "-C" in flags or "--syntax-check" in flags or "--list-hosts" in flags:
        v.add(ORANGE, _("проверочный прогон Ansible на серверах"))
    else:
        v.add(RED, _("меняет серверы через Ansible"))


def _h_remote(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if prog in ("scp", "sftp"):
        v.add(RED, _("копирует на удалённый сервер или с него"))
        return
    if prog == "mosh":
        v.add(RED, _("открывает сессию на удалённом сервере"))
        return
    value_flags = {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-W", "-b", "-c", "-E",
                   "-e", "-I", "-m", "-O", "-Q", "-S", "-w", "-B"}
    i = 0
    host = ""
    while i < len(args):
        a = args[i]
        if a in value_flags:
            if a == "-o" and i + 1 < len(args) and args[i + 1].lower().startswith(("proxycommand", "localcommand")):
                v.add(RED, _("ssh запускает локальную команду (ProxyCommand)"))
            i += 2
            continue
        # Слитная форма `-oProxyCommand=…` (ревью 11.10.2026).
        if a.startswith("-o") and a[2:].lower().startswith(("proxycommand", "localcommand")):
            v.add(RED, _("ssh запускает локальную команду (ProxyCommand)"))
        if a.startswith("-"):
            i += 1
            continue
        host = a
        i += 1
        break
    remote = args[i:]
    if not remote:
        v.add(RED, _("открывает сессию на сервере {host}").format(host=host or '?'))
        return
    inner = Verdict()
    _classify_bash(" ".join(remote), inner, depth + 1)
    for r in inner.red:
        v.add(RED, _("на сервере {host}: {r}").format(host=host, r=r))
    for r in inner.orange:
        v.add(ORANGE, _("на сервере {host}: {r}").format(host=host, r=r))
    v.add(ORANGE, _("команда на сервере {host}").format(host=host))


_CLOUD_READ = frozenset({"describe", "list", "get", "show", "ls", "wait", "help", "version",
                         "whoami", "info"})
# Глобальные флаги aws без значения: у остальных `--флаг значение`.
_AWS_BOOL = frozenset({"--debug", "--no-verify-ssl", "--no-paginate", "--no-sign-request",
                       "--no-cli-pager", "--dry-run", "--no-dry-run", "--recursive",
                       # az, gcloud, doctl (ревью 11.10.2026)
                       "--verbose", "--only-show-errors", "--yes", "-y", "--quiet", "-q",
                       "--no-wait", "--all", "--force"})


def _cloud_words(args: list[str]) -> list[str]:
    """Позиционные слова без значений флагов `--флаг значение` (ревью 11.10.2026):
    имя `getUser` после `--function-name` не глагол чтения."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if "=" not in a and a not in _AWS_BOOL and i + 1 < len(args) \
                    and not args[i + 1].startswith("-"):
                i += 1
        else:
            out.append(a)
        i += 1
    return out


def _cloud_read_verb(word: str) -> bool:
    return word in _CLOUD_READ or word.startswith(("describe-", "list-", "get-"))


def _h_cloud(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # Глагол берётся точным словом, а не префиксом любого аргумента: у aws
    # это второе слово после значений флагов, у gcloud, az и doctl первое слово
    # после известных групп команд (ревью 11.10.2026).
    if prog == "aws":
        pos = _cloud_words(args)
        verb = pos[1] if len(pos) > 1 else ""
        if pos[:1] == ["configure"]:
            read = verb in ("get", "list", "list-profiles")
        else:
            read = pos[:2] == ["s3", "ls"] or _cloud_read_verb(verb)
    else:
        pos = _cloud_words(args)
        # Для переменной глубины групп известны только читающие конечные
        # действия. Неизвестное слово перед get/list не становится чтением
        # (ревью 11.10.2026).
        groups = {
            "az": {"vm", "vmss", "network", "vnet", "subnet", "nsg", "nic", "public-ip", "lb",
                   "dns", "zone", "record-set", "resource", "group", "account", "storage",
                   "blob", "container", "share", "queue", "table", "keyvault", "secret", "key",
                   "certificate", "sql", "server", "db", "postgres", "mysql", "flexible-server",
                   "webapp", "functionapp", "appservice", "plan", "aks", "acr", "repository",
                   "disk", "snapshot", "image", "identity", "role", "assignment", "definition",
                   "ad", "sp", "app", "user", "monitor", "cosmosdb", "redis", "deployment",
                   "policy", "lock", "provider", "feature", "extension"},
            "gcloud": {"alpha", "beta", "compute", "instances", "disks", "images", "snapshots",
                       "networks", "subnets", "firewall-rules", "addresses", "routers", "zones",
                       "regions", "operations", "projects", "config", "auth", "storage",
                       "buckets", "objects", "iam", "service-accounts", "roles", "container",
                       "clusters", "node-pools", "functions", "run", "services", "revisions",
                       "pubsub", "topics", "subscriptions", "sql", "databases", "logging",
                       "secrets", "versions", "kms", "keys", "keyrings", "dns", "managed-zones",
                       "record-sets", "artifacts", "repositories", "builds", "organizations",
                       "billing", "accounts", "scheduler", "jobs", "redis", "app"},
            "doctl": {"compute", "droplet", "image", "volume", "domain", "records", "firewall",
                      "load-balancer", "ssh-key", "snapshot", "kubernetes", "cluster",
                      "node-pool", "apps", "registry", "repository", "account", "auth",
                      "databases", "database", "projects", "vpcs", "spaces", "monitoring"},
        }
        verb = next((p for p in pos if p not in groups.get(prog, set())), "")
        read = verb in _CLOUD_READ
    if read:
        v.add(ORANGE, _("обращается к облаку (чтение)"))
        return
    v.add(RED, _("меняет ресурсы в облаке ({prog} {args})").format(prog=prog, args=' '.join(pos[:2])))


# Опции curl, которые съедают следующее слово или хвост связки. Имена
# файлов и заголовки не разбираются как новые флаги (ревью 11.10.2026).
_CURL_VALUE_FLAGS = frozenset({
    "-A", "--user-agent", "-H", "--header", "-e", "--referer", "-u", "--user",
    "-U", "--proxy-user", "-x", "--proxy", "-m", "--max-time", "--connect-timeout",
    "--retry", "-K", "--config", "--url", "-b", "--cookie", "--cacert", "--capath",
    "-E", "--cert", "--key", "--resolve", "--interface", "--unix-socket", "--output-dir",
    "-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar", "-w", "--write-out",
    "-X", "--request", "-d", "--data", "--data-binary", "--data-raw", "--data-urlencode",
    "-F", "--form", "-T", "--upload-file", "--json",
})


def _h_http(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    send_flags = ("-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "-F",
                  "--form", "-T", "--upload-file", "--json", "--post-data", "--post-file",
                  "--body-data", "--body-file")
    method = ""
    for i, a in enumerate(args):
        if a in ("-X", "--request", "--method") and i + 1 < len(args):
            method = args[i + 1].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
        elif a.startswith(("--request=", "--method=")):
            method = a.split("=", 1)[1].upper()
    if prog == "curl":
        # Метод в связке флагов: `-sX DELETE`, `-sXDELETE` (ревью 11.10.2026).
        method = next((m.upper() for m in _option_values(args, "X", "--request", _CURL_VALUE_FLAGS)
                       if m.upper() not in ("GET", "HEAD", "OPTIONS")), method)
        outputs = (_option_values(args, "o", "--output", _CURL_VALUE_FLAGS)
                   + _option_values(args, "D", "--dump-header", _CURL_VALUE_FLAGS)
                   + _option_values(args, "c", "--cookie-jar", _CURL_VALUE_FLAGS))
    elif prog == "wget":
        outputs = (_option_values(args, "O", "--output-document")
                   + _option_values(args, "o", "--output-file"))
    else:
        outputs = []
    if prog == "curl":
        # -w принимает собственную цель вывода, в том числе в связке -sw
        # (ревью 11.10.2026).
        for fmt in _option_values(args, "w", "--write-out", _CURL_VALUE_FLAGS):
            for target in re.findall(r"%output\{([^}]*)\}", fmt):
                _write_target(target.removeprefix(">>"), v)
    # Файл, куда пишет скачанное: защищённый путь красный (ревью 11.10.2026).
    for target in outputs:
        _write_target(target, v, N_("скачивает файл"))
    if method and method not in ("GET", "HEAD", "OPTIONS"):
        v.add(RED, _("отправляет запрос {method} в сеть").format(method=method))
    if any(a.startswith(send_flags) for a in args if a.startswith("-")) or any(
            a.startswith("-") and not a.startswith("--") and set(a[1:]) & {"d", "F", "T"}
            for a in args):
        v.add(RED, _("отправляет данные в сеть"))
    if any(a in ("-o", "-O", "--output", "--remote-name", "-P", "--output-document") or
           a.startswith(("--output=", "--output-document=")) for a in args) or prog == "wget":
        v.add(ORANGE, _("скачивает файл"))


def _h_perms(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    flags = _flags(args)
    level = RED if ("-R" in flags or "--recursive" in flags or
                    any(_protected_path(t) for t in _positionals(args))) else ORANGE
    v.add(level, _("меняет права или владельца файлов"))


_PKG_VERBS = frozenset({"install", "uninstall", "remove", "purge", "upgrade", "update",
                        "add", "ci", "autoremove", "dist-upgrade", "full-upgrade", "reinstall",
                        "link", "unlink", "sync",
                        # Сокращения npm, pnpm, brew (ревью 11.10.2026).
                        "i", "in", "ins", "inst", "isnt", "it", "cit", "install-test", "rm", "r",
                        "un", "up", "udpate"})
# Публикация пакета наружу (ревью 11.10.2026).
_PKG_PUBLISH = frozenset({"publish", "unpublish", "push", "yank", "deprecate", "owner",
                          "dist-tag", "login", "adduser"})
# Запуск кода проекта у сборщиков (ревью 11.10.2026).
_PKG_RUN = {
    "cargo": frozenset({"run", "r", "build", "b", "test", "t", "bench", "check", "c", "clippy"}),
    "go": frozenset({"run", "generate", "test", "build", "vet", "tool"}),
}


def _h_pkg(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # Значения глобальных флагов не подкоманды (ревью 11.10.2026).
    pos = _operands(args, frozenset({"--prefix", "--cwd", "--dir", "-C", "--cache",
                                   "--registry", "--userconfig", "--globalconfig", "--workspace",
                                   "-w", "--config", "--manifest-path", "--target-dir"}))
    if prog == "dpkg" and ({"-i", "-r", "-P", "--install", "--remove", "--purge"} & _flags(args)):
        v.add(RED, _("ставит или удаляет программы"))
        return
    if prog in ("npm", "pnpm", "yarn") and pos and pos[0] in ("run", "run-script", "exec", "start",
                                                             "test", "x", "dlx"):
        v.add(ORANGE, _("запускает скрипт проекта ({prog} {pos0})").format(prog=prog, pos0=pos[0]))
        return
    if prog == "npx":
        v.add(ORANGE, _("запускает пакет из npm (npx)"))
        return
    if pos and pos[0] in _PKG_PUBLISH and prog not in ("apt", "apt-get", "dpkg", "snap"):
        v.add(RED, _("публикует пакет или меняет доступ к нему ({prog} {pos0})").format(prog=prog, pos0=pos[0]))
        return
    if pos and pos[0] in _PKG_RUN.get(prog, ()):
        v.add(ORANGE, _("собирает или запускает код проекта ({prog} {pos0})").format(prog=prog, pos0=pos[0]))
        return
    if prog == "yarn" and not pos:
        v.add(RED, _("ставит зависимости (yarn)"))
        return
    if prog == "go" and pos[:1] == ["get"]:
        v.add(RED, _("ставит или удаляет программы (go get)"))
        return
    if prog == "uv" and pos and pos[0] == "pip":
        pos = pos[1:]
    if prog == "uv" and pos and pos[0] in ("run", "tool"):
        v.add(ORANGE, _("запускает программу через uv"))
        return
    if pos and pos[0] in _PKG_VERBS:
        v.add(RED, _("ставит или удаляет программы ({prog} {pos0})").format(prog=prog, pos0=pos[0]))


def _h_interpreter(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # `-c` бывает в связке: `bash -lc 'rm …'`, `sh -ec …` (ревью 11.10.2026).
    idx = next((i for i, a in enumerate(args)
                if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:])),
               None) if prog in ("bash", "sh", "zsh", "dash") else None
    if idx is not None:
        if idx + 1 < len(args):
            v.add(ORANGE, _("запускает код из строки ({prog} -c)").format(prog=prog))
            _classify_bash(args[idx + 1], v, depth + 1)
            return
    if prog == "eval":
        v.add(ORANGE, _("запускает код из строки (eval)"))
        _classify_bash(" ".join(args), v, depth + 1)
        return
    if prog.startswith("python") and len(args) >= 2 and args[0] == "-m":
        mod = args[1]
        if mod.startswith("claude_tg_bot.skills."):
            v.add(ORANGE, _("запускает скилл бота ({skill})").format(skill=mod.rsplit('.', 1)[-1]))
            return
        if mod in ("json.tool", "pytest", "py_compile", "this", "calendar"):
            return
    v.add(ORANGE, _("запускает программу или скрипт ({prog})").format(prog=prog))


def _h_schedule(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if prog == "crontab" and ("-l" in args):
        return
    v.add(RED, _("планирует запуск команд"))


def _h_db(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    text = " ".join(args).upper()
    if re.search(r"\b(DROP|DELETE|TRUNCATE|UPDATE|ALTER|INSERT|GRANT|REVOKE|CREATE|FLUSHALL|FLUSHDB|DEL)\b", text):
        v.add(RED, _("меняет базу данных"))
    else:
        v.add(ORANGE, _("обращается к базе данных"))


def _h_firewall(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    flags = _flags(args)
    pos = _positionals(args)
    if {"-L", "-S", "--list", "--list-rules"} & flags or (pos and pos[0] in ("status", "list", "show")):
        return
    v.add(RED, _("меняет сетевой экран"))


_GH_RED = {
    "pr": {"merge", "close", "create", "edit", "comment", "review", "ready", "reopen", "lock",
           "unlock", "update-branch", "checkout", "revert"},
    "issue": {"create", "close", "edit", "comment", "delete", "reopen", "transfer", "lock",
              "unlock", "pin", "unpin", "develop"},
    "release": {"create", "delete", "edit", "upload", "delete-asset"},
    "repo": {"create", "delete", "edit", "rename", "archive", "unarchive", "fork", "sync",
             "set-default", "deploy-key"},
    "run": {"cancel", "rerun", "delete", "download"},
    "workflow": {"run", "enable", "disable"},
    "secret": {"set", "delete", "remove"},
    "variable": {"set", "delete"},
    "label": {"create", "edit", "delete", "clone"},
    "gist": {"create", "edit", "delete", "clone", "rename"},
    "auth": {"login", "logout", "refresh", "setup-git", "switch", "token"},
    "ssh-key": {"add", "delete"},
    "gpg-key": {"add", "delete"},
    "cache": {"delete"},
    "extension": {"install", "remove", "upgrade", "exec", "create"},
    "alias": {"set", "delete", "import"},
    "config": {"set", "clear-cache"},
}


def _h_gh(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    if not pos:
        return
    group = pos[0]
    sub = pos[1] if len(pos) > 1 else ""
    if group == "api":
        flags = _flags(args)
        if any(a == "--cache" or a.startswith("--cache=") for a in args):
            v.add(ORANGE, _("пишет ответ GitHub в локальный кеш"))
        method = ""
        for i, a in enumerate(args):
            if a in ("-X", "--method") and i + 1 < len(args):
                method = args[i + 1].upper()
            elif a.startswith("--method="):
                method = a.split("=", 1)[1].upper()
            elif a.startswith("-X") and len(a) > 2:
                method = a[2:].upper()
        if (method and method != "GET") or ({"-f", "-F", "--field", "--raw-field", "--input"} & flags):
            v.add(RED, _("действие на GitHub от твоего имени (gh api)"))
        return
    generic_red = {"delete", "create", "edit", "close", "archive", "remove", "set", "add",
                   "merge", "transfer", "copy", "link", "unlink", "mark-template", "item-add",
                   "item-delete", "item-edit", "item-archive", "field-create", "field-delete"}
    if sub in _GH_RED.get(group, set()) or (group not in _GH_RED and sub in generic_red):
        v.add(RED, _("действие на GitHub от твоего имени (gh {group} {sub})").format(group=group, sub=sub))
    elif group not in _GH_RED and group not in ("status", "browse", "search", "help", "version",
                                                "completion"):
        v.add(ORANGE, _("gh {group} {sub}: команда не распознана").format(group=group, sub=sub))


def _h_memory(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    if not pos or pos[0] != "remember":
        return
    scope = ""
    for i, a in enumerate(args):
        if a == "--scope" and i + 1 < len(args):
            scope = args[i + 1]
        elif a.startswith("--scope="):
            scope = a.split("=", 1)[1]
    if scope == "global":
        v.add(RED, _("меняет общие правила всех сессий (память global)"))
    else:
        v.add(ORANGE, _("пишет в память ({scope})").format(scope=scope or _("область не указана")))


_HANDLERS = {
    "rm": _h_delete, "rmdir": _h_delete, "shred": _h_delete, "truncate": _h_delete,
    "unlink": _h_delete, "dd": _h_delete, "wipefs": _h_delete,
    "cp": _h_write, "mv": _h_write, "install": _h_write, "ln": _h_write, "rsync": _h_write,
    "tar": _h_write, "unzip": _h_write, "touch": _h_write, "mkdir": _h_write, "patch": _h_write,
    "sed": _h_sed, "perl": _h_interpreter, "tee": _h_tee, "find": _h_find, "sort": _h_sort,
    "awk": _h_awk, "gawk": _h_awk, "mawk": _h_awk, "fd": _h_fd, "fdfind": _h_fd, "ip": _h_ip,
    "git": _h_git, "systemctl": _h_systemctl, "service": _h_service,
    "reboot": _h_power, "shutdown": _h_power, "poweroff": _h_power, "halt": _h_power,
    "kill": _h_kill, "pkill": _h_kill, "killall": _h_kill, "journalctl": _h_journalctl,
    "docker": _h_docker, "docker-compose": _h_docker, "podman": _h_docker,
    "kubectl": _h_kubectl, "helm": _h_helm, "terraform": _h_terraform, "tofu": _h_terraform,
    "ansible": _h_ansible, "ansible-playbook": _h_ansible,
    "ssh": _h_remote, "scp": _h_remote, "sftp": _h_remote, "mosh": _h_remote,
    "aws": _h_cloud, "gcloud": _h_cloud, "az": _h_cloud, "doctl": _h_cloud,
    "curl": _h_http, "wget": _h_http, "http": _h_http, "httpie": _h_http,
    "chmod": _h_perms, "chown": _h_perms, "chgrp": _h_perms, "chattr": _h_perms,
    "setfacl": _h_perms,
    "apt": _h_pkg, "apt-get": _h_pkg, "dpkg": _h_pkg, "snap": _h_pkg, "pip": _h_pkg,
    "pip3": _h_pkg, "uv": _h_pkg, "npm": _h_pkg, "pnpm": _h_pkg, "yarn": _h_pkg,
    "npx": _h_pkg, "brew": _h_pkg, "cargo": _h_pkg, "gem": _h_pkg, "go": _h_pkg,
    "bash": _h_interpreter, "sh": _h_interpreter, "zsh": _h_interpreter, "dash": _h_interpreter,
    "eval": _h_interpreter, "source": _h_interpreter, ".": _h_interpreter,
    "python": _h_interpreter, "python3": _h_interpreter, "node": _h_interpreter,
    "ruby": _h_interpreter, "make": _h_interpreter, "deno": _h_interpreter, "bun": _h_interpreter,
    "crontab": _h_schedule, "at": _h_schedule, "batch": _h_schedule, "systemd-run": _h_schedule,
    "psql": _h_db, "mysql": _h_db, "sqlite3": _h_db, "redis-cli": _h_db, "mongosh": _h_db,
    "mongo": _h_db, "clickhouse-client": _h_db,
    "iptables": _h_firewall, "ip6tables": _h_firewall, "nft": _h_firewall, "ufw": _h_firewall,
    "firewall-cmd": _h_firewall,
    "gh": _h_gh, "memory": _h_memory,
    # ревью 11.10.2026
    "shuf": _h_sort, "iconv": _h_sort, "tree": _h_sort, "uniq": _h_second_operand,
    "xxd": _h_second_operand, "gzip": _h_gzip, "gunzip": _h_gzip, "openssl": _h_openssl,
    "rg": _h_rg, "file": _h_file, "date": _h_system_identity,
    "hostname": _h_system_identity, **{name: _h_system_tool for name in _SYSTEM_TOOL_READ},
}


# --- Пути и MCP -------------------------------------------------------------

_BOT_ROOT = Path(__file__).resolve().parents[2]
_HOME = Path.home()
_PROTECTED_DIRS = (
    _HOME / ".ssh", _HOME / ".gnupg", _HOME / ".config" / "systemd", _HOME / ".claude" / "hooks",
    _HOME / ".codex", _HOME / ".agent-memory", _HOME / ".agent-memory-config",
    _HOME / ".local" / "bin", _HOME / ".config" / "autostart", Path("/etc"), Path("/usr"),
    Path("/boot"), Path("/var/spool/cron"),
    # Решение владельца 12.10.2026: ~/.config/systemd/user уже внутри ~/.config/systemd.
    _HOME / ".local" / "share" / "systemd" / "user", _HOME / ".aws",
    _HOME / ".config" / "environment.d", _HOME / ".claude" / "skills",
)
_PROTECTED_FILES = (
    _HOME / ".bashrc", _HOME / ".profile", _HOME / ".bash_profile", _HOME / ".zshrc",
    _HOME / ".gitconfig", _HOME / ".claude" / "settings.json",
    _HOME / ".claude" / "settings.local.json", _HOME / ".claude" / "CLAUDE.md",
    _HOME / ".claude.json", _HOME / ".zshenv",  # решение владельца 12.10.2026
)


def _with_targets(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Пути плюс их настоящие цели: ~/.claude/hooks бывает ссылкой на
    ~/src/svod/agent/hooks, и запись по настоящему пути тоже красная
    (ревью 11.10.2026)."""
    out = list(paths)
    for p in paths:
        try:
            real = p.resolve()
        except (OSError, RuntimeError):
            continue
        if real != p and real not in out:
            out.append(real)
    return tuple(out)


_PROTECTED_DIRS = _with_targets(_PROTECTED_DIRS)
_PROTECTED_FILES = _with_targets(_PROTECTED_FILES)
_HOME_VAR_RE = re.compile(r"^(?:\$HOME|\$\{HOME\})(?=/|$)")


def _absolute(raw: str) -> str | None:
    """Путь из команды в абсолютной форме: `~`, `$HOME` и `${HOME}`
    раскрываются, относительный берётся от рабочего каталога агента, если он
    известен (ревью 11.10.2026). None, если путь не определить."""
    raw = _HOME_VAR_RE.sub(lambda _unused: str(_HOME), raw)
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    if raw.startswith("/"):
        return raw
    cwd = _CWD.get()
    if cwd is None or raw.startswith(("$", "~")):
        return None
    return os.path.join(cwd, raw)


def _protected_path(raw: str) -> bool:
    absolute = _absolute(raw) if raw else None
    if absolute is None:
        return False
    try:
        p = Path(os.path.normpath(absolute))
        real = p.resolve()
        if real != p and _protected_path(str(real)):
            return True
    except (OSError, ValueError, RuntimeError):
        return False
    if p.name.startswith(".env"):
        return True
    if p in _PROTECTED_FILES or any(p == d or d in p.parents for d in _PROTECTED_DIRS):
        return True
    if "/.git/hooks/" in str(p) or str(p).endswith("/.git/config"):
        return True
    if p == _BOT_ROOT or _BOT_ROOT in p.parents:
        drafts = _BOT_ROOT / "state" / "memory-drafts"
        return not (drafts in p.parents)
    return False


def _classify_path(raw: str, v: Verdict) -> None:
    if not raw:
        v.add(ORANGE, _("путь к файлу не указан"))
        return
    absolute = _absolute(raw)
    if absolute is None:
        v.add(ORANGE, _("путь относительный, не проверен"))
        return
    if _secret_path(absolute):
        v.secret = True
    if _protected_path(raw):
        v.add(RED, _("меняет ключи, настройки, память или код бота"))
        return
    v.add(ORANGE, _("пишет файл {os}").format(os=os.path.basename(raw)))
    resolved = Path(absolute)
    try:
        real = resolved.resolve()
    except OSError:
        real = resolved
    if real != Path(os.path.normpath(resolved)) and _protected_path(str(real)):
        v.add(RED, _("путь ведёт по ссылке к ключам, настройкам или коду бота"))


_MCP_RED_WORDS = frozenset({
    "send", "delete", "remove", "trash", "forward", "reply", "publish", "share", "post",
    "merge", "close", "archive", "cancel", "revoke", "transfer", "pay", "invite", "kick",
    "ban", "block", "leave", "mark", "spam", "respond",
})


def _classify_mcp(tool_name: str, v: Verdict) -> None:
    short = tool_name.split("__", 2)[-1].lower()
    words = set(re.split(r"[_\-]+", short))
    if words & _MCP_RED_WORDS:
        v.add(RED, _("действие во внешнем сервисе от твоего имени"))
    else:
        v.add(ORANGE, _("меняет данные во внешнем сервисе"))

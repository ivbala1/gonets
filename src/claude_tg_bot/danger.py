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

# Метки смысла для правил, а не для человека: текст причины переводится и
# меняется, метка нет. По ним разрешение на план оставляет карточку
# (проект шага 2, ревью Codex 29.10.2026).
DELETE = "delete"          # удаляет данные: файлы, тома, базы, ресурсы
HISTORY = "history"        # переписывает историю git или выбрасывает правки
PUBLISH = "publish"        # отправляет наружу: push, запрос с данными, пакет, GitHub
POWER = "power"            # перезагружает или выключает машину
DISK = "disk"              # пишет на диск напрямую: mkfs, dd
GUARD = "guard"            # меняет ключи, настройки, память или код бота
FETCH_EXEC = "fetch-exec"  # скачивает и сразу запускает код
UNPARSED = "unparsed"      # разбор сдался: что исполнится, неизвестно

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
    # Каждая программа звена с аргументами, как её увидел разбор, обёртки тоже,
    # и присваивания перед командой как ("=", [присваивание]): по этому списку
    # approval пускает без карточки только знакомое чтение.
    programs: list[tuple[str, list[str]]] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    # Куда команда копирует данные: хост rsync и scp, бакет s3. Отправка на
    # названное в одобренном плане идёт без карточки (approval._plan_card_tags).
    remotes: set[str] = field(default_factory=set)

    def add(self, level: str, reason: str, tag: str = "") -> None:
        if tag:
            self.tags.add(tag)
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
            v.secret = v.secret or (_mentions_secret(str(tool_input.get("command", "")))
                                    and not _metadata_only(v, str(tool_input.get("command", ""))))
        elif tool_name == "Monitor":
            # Monitor запускает shell-команду в фоне, как Bash (ревью 11.10.2026).
            command = tool_input.get("command")
            if isinstance(command, str) and command.strip():
                _classify_bash(command, v, depth=0)
                v.secret = v.secret or _mentions_secret(command)
            else:
                v.add(ORANGE, _("инструмент Monitor без команды, не разобран"), UNPARSED)
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
                v.add(ORANGE, _("heredoc не разобран"), UNPARSED)
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
        v.add(ORANGE, _("вложенная команда не разобрана"), UNPARSED)
        return
    text = _strip_heredocs(command, v, depth)
    if "`" in _quote_mask(text) or "`" in text:
        v.add(ORANGE, _("есть команда в обратных кавычках"))
        if re.search(r"\b(?:curl|wget)\b", text):
            # Подстановка исчезнет ниже, а её вывод уйдёт запросом (ревью Codex 29.10).
            v.add(RED, _("отправляет данные в сеть"), PUBLISH)
        text = text.replace("`", " ; ")
    if "$(" in text or "<(" in text or ">(" in text:
        v.add(ORANGE, _("есть вложенная команда $(…), <(…) или >(…)"))
    text = _pipe_to_interpreter(text, v)
    # Шаблон скрытых имён вне кавычек может подставить .env. Внутри
    # программы jq или awk это обычный текст (ревью 11.10.2026).
    masked = _quote_mask(text)
    masked_tokens = _tokenize(masked) or []
    if any(part.startswith(".") and glob.has_magic(part)
           for word in masked_tokens for part in word.split("/")):
        v.secret = True
        v.add(ORANGE, _("шаблон может подставить скрытые секретные файлы"))
    if _expansion_hides_flag(text, masked, masked_tokens):
        v.add(ORANGE, _("оболочка раскроет слово во флаг или команду, не разобрано"), UNPARSED)
    tokens = _tokenize(_newlines_to_separators(text))
    if tokens is None:
        v.add(ORANGE, _("команда не разобрана: незакрытая кавычка"), UNPARSED)
        return
    token = _CWD.set(_CWD.get())
    try:
        # Генератор читает следующий путь записи после обработки cd
        # предыдущего звена (ревью 11.10.2026).
        for seg in _segments(tokens, v):
            _classify_segment(seg, v, depth)
    finally:
        _CWD.reset(token)


_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_IFS_RE = re.compile(r"\$\{?IFS\b")


def _expansion_hides_flag(text: str, masked: str, masked_tokens: list[str]) -> bool:
    """Слово, которое bash раскроет уже после разбора во флаг или в несколько
    слов: `{rm,-rf,x}`, `find . {-delete,}`, `rm${IFS}-rf`, `$'-delete'`
    (аудит guard 31.10.2026, проверено bash 5.2). Обычные `f{,.bak}` и
    `src/{a,b}` не трогает."""
    if _IFS_RE.search(masked):
        return True
    for word in masked_tokens:
        for m in _BRACE_RE.finditer(word):
            body = m.group(1)
            if "," in body or ".." in body:
                alts = body.split(",") if "," in body else [body]
                if any((word[:m.start()] + alt).startswith("-") for alt in alts):
                    return True
    i = masked.find("$'")
    while i >= 0:
        j = masked.find("'", i + 2)
        if (i == 0 or masked[i - 1] != "\\") and j > 0:
            try:
                value = bytes(text[i + 2:j], "utf-8").decode("unicode_escape")
            except (UnicodeDecodeError, ValueError):
                return True
            if value.startswith("-"):
                return True
        i = masked.find("$'", i + 2)
    return False


# Скачанное идёт интерпретатору как программа: `| sh`, `| bash -s`, `| python3`
# или `| python3 -`. `| python3 -c '…'` и `| sh -c '…'` лишь читают ввод.
_PIPE_EXEC_RE = re.compile(
    r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:tee\b[^|;&\n]*\|\s*)*"
    # Обёртки перед интерпретатором: `| env bash`, `| timeout 30 sh` (ревью Codex 29.10).
    r"(?:(?:sudo|doas|env|nice|nohup|command|exec|stdbuf|ionice|time|timeout(?:\s+\S+)?)\s+"
    r"(?:(?:-\S+|\w+=\S*)\s+)*)*"
    r"(?:\S*/)?(?:(?:ba|z|da)?sh|python3?|perl|ruby|node)(?:\s+-s\b|\s+-(?=\s|$))?\s*(?:$|[;&|)\n])"
)


# Скачанное идёт оболочке подстановкой: `eval "$(curl …)"`, `source <(curl …)`,
# `bash -c "$(curl …)"` (ревью 29.10.2026).
_SUBST_EXEC_RE = re.compile(
    r"(?:\b(?:source|eval)|(?:^|[;&|(\s])\.|\b(?:(?:ba|z|da)?sh|python3?|perl|ruby|node)(?:\s+-\w+)*)"
    r"\s+[\"']?(?:<|\$)\(\s*(?:\S*/)?(?:curl|wget)\b"
)
_FETCH_PIPE_RE = re.compile(r"\b(?:curl|wget)\b[^|;&\n]*\|(?:\s*tee\b[^|;&\n]*\|)*")
_FETCH_WRAPPERS = frozenset({"sudo", "doas", "env", "nice", "nohup", "command", "exec", "stdbuf",
                             "ionice", "time", "timeout", "strace", "ltrace"})
_INTERPRETER_RE = re.compile(r"(?:ba|z|da)?sh|python3?(?:\.\d+)?|perl|ruby|node")


def _pipe_feeds_interpreter(masked: str) -> bool:
    """`… | bash -s -- флаги`, `| bash -x`, `| bash /dev/stdin`, `| python3 -`:
    интерпретатор читает программу со входа (аудит guard 31.10.2026).
    Код строкой (`-c`, `-e`, `-m`) или файл скрипта читают вход как данные."""
    for m in _FETCH_PIPE_RE.finditer(masked):
        words = re.split(r"[;&|)\n]", masked[m.end():], maxsplit=1)[0].split()
        i = 0
        while i < len(words) and (os.path.basename(words[i]) in _FETCH_WRAPPERS
                                  or words[i].startswith("-") or _ASSIGN_RE.match(words[i])
                                  or (i and words[i - 1] == "timeout")):
            i += 1
        if i >= len(words) or not _INTERPRETER_RE.fullmatch(os.path.basename(words[i])):
            continue
        shell = os.path.basename(words[i]).endswith("sh")
        code_flags = "c" if shell else "cme" if "python" in words[i] else "eEp"
        program_from_input = True
        for a in words[i + 1:]:
            if a in ("-", "--", "/dev/stdin") or (shell and a.startswith("-") and not a.startswith("--")
                                                  and "s" in a):
                break
            if a.startswith("-") and not a.startswith("--") and set(a[1:]) & set(code_flags):
                program_from_input = False
                break
            if not a.startswith("-"):
                program_from_input = False
                break
        if program_from_input:
            return True
    return False


def _pipe_to_interpreter(text: str, v: Verdict) -> str:
    masked = _quote_mask(text)
    if _PIPE_EXEC_RE.search(masked) or _SUBST_EXEC_RE.search(text) or _pipe_feeds_interpreter(masked):
        v.add(RED, _("скачивает и сразу запускает код"), FETCH_EXEC)
    return text


# Программы, у которых первое слово решает, что делать: оно из переменной
# не разобрано (`git $a`); у echo и awk это просто данные.
_SUBCOMMAND_PROGS = frozenset({
    "git", "docker", "podman", "docker-compose", "kubectl", "helm", "systemctl", "service",
    "terraform", "aws", "gcloud", "az", "doctl", "gh", "npm", "pnpm", "yarn", "apt", "apt-get",
    "rclone", "loginctl", "zfs", "zpool", "ansible", "redis-cli",
})


def _tagged_program(prog: str, args: list[str]) -> str:
    """Метка программы, которую разбор иначе считает незнакомой или
    безобидной: почта и сырые сетевые каналы, диски, rclone, loginctl
    (ревью 29.10.2026). Список не полон, как и весь разбор."""
    pos = _positionals(args)
    sub = pos[0] if pos else ""
    if prog in ("nc", "ncat", "netcat", "socat", "mail", "mailx", "sendmail", "msmtp", "mutt",
                "swaks", "telnet"):
        return PUBLISH
    if prog in ("twine", "flit", "poetry") and sub in ("upload", "publish"):
        return PUBLISH
    if prog == "rclone":
        if sub in ("delete", "deletefile", "purge", "rmdir", "rmdirs", "cleanup"):
            return DELETE
        if sub in ("copy", "copyto", "sync", "move", "moveto", "copyurl", "rcat", "bisync"):
            return PUBLISH
    if prog in ("mke2fs", "mkswap", "wipefs", "blkdiscard", "fdisk", "sfdisk", "gdisk", "sgdisk",
                "cfdisk", "parted", "swapoff") or (prog == "nvme" and sub in ("format", "sanitize")):
        return DISK
    if prog in ("shred", "srm", "wipe", "lvremove", "vgremove", "pvremove") or (
            prog == "cryptsetup" and sub in ("erase", "luksErase", "luksKillSlot")) or (
            prog in ("zfs", "zpool") and sub in ("destroy", "labelclear")):
        return DELETE
    if prog == "loginctl" and sub in ("reboot", "poweroff", "halt", "suspend", "hibernate", "kexec",
                                      "soft-reboot"):
        return POWER
    # `crontab файл` и `crontab -` заменяют весь crontab, прочие строки
    # теряются, как при `-r` (аудит guard 31.10.2026, задание guard2).
    if prog == "crontab" and ("-r" in _flags(args) or (
            not {"-l", "-e"} & _flags(args) and _operands(args, frozenset({"-u"})))):
        return DELETE
    # `tailscale funnel|serve <порт>` открывает службу в интернет или в
    # tailnet; показ, сброс и выключение не публикуют.
    if prog == "tailscale" and sub in ("funnel", "serve") and not {"status", "reset", "off"} & set(pos):
        return PUBLISH
    # Выключение Windows и всей WSL с ней из WSL (аудит guard 31.10.2026).
    low = prog.lower()
    text = " ".join(args).lower()
    if low == "shutdown.exe" or (low in ("wsl", "wsl.exe") and {"--shutdown", "--terminate", "-t"}
                                  & set(args)) or (
            low in ("powershell", "powershell.exe", "pwsh", "pwsh.exe")
            and re.search(r"\b(?:restart|stop)-computer\b", text)) or (
            low == "cmd.exe" and re.search(r"\bshutdown(?:\.exe)?\b", text)):
        return POWER
    return ""


# strace и ltrace запускают следующую команду. Без карточки только сводка и
# отбор вызовов; журнал в файл, подключение к процессу, смена окружения и
# пользователя карточкой, `-v` и `-e abbrev=` печатают переменные окружения.
# У ltrace `-x`, `-w`, `-e` и `-s` значат другое и берут значение.
_TRACER_PLAIN_RE = {"strace": re.compile(r"-(?:[fcCqtTyxrizZw]+|[es](?:trace=)?[%\w,!-]+)"),
                    "ltrace": re.compile(r"-[fcCrtTi]+")}
_TRACER_VALUE = frozenset({"-e", "-s"})
_TRACER_RISKY_VALUE = frozenset({"-o", "--output", "-p", "--attach", "-E", "--env", "-u", "--user",
                                 "-P", "-I", "-b", "-S", "-O", "-X", "-a", "-A", "-n", "-l", "-F", "-D",
                                 "-e", "-s", "-x", "-w"})


def _tracer_command(prog: str, args: list[str], v: Verdict) -> list[str]:
    """Команда под strace или ltrace; что нельзя считать чтением, красит v."""
    rest = list(args)
    while rest and rest[0].startswith("-"):
        flag = rest[0]
        plain = _TRACER_PLAIN_RE[prog]
        if flag in _TRACER_VALUE and plain.fullmatch(flag + "".join(rest[1:2])):
            rest = rest[2:]
        elif plain.fullmatch(flag):
            rest = rest[1:]
        else:
            v.add(ORANGE, _("{prog} с записью, чужим процессом или показом окружения").format(prog=prog))
            rest = rest[2:] if flag in _TRACER_RISKY_VALUE else rest[1:]
    if not rest:
        v.add(ORANGE, _("{prog} без команды").format(prog=prog))
    return rest


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
    # Запуск команды в другом сеансе, на других ядрах, с другим приоритетом
    # или в своих пространствах имён: метки внутренней команды не теряются
    # (аудит guard 31.10.2026, задание guard2). `busybox rm` это тот же rm.
    "busybox": (set(), 0), "setsid": (set(), 0),
    "taskset": (set(), 1), "chrt": ({"-T", "--sched-runtime", "-P", "--sched-period", "-D",
                                     "--sched-deadline"}, 1),
    "unshare": ({"-S", "--setuid", "-G", "--setgid", "-R", "--root", "-w", "--wd"}, 0),
    "nsenter": ({"-t", "--target", "-S", "--setuid", "-G", "--setgid"}, 0),
    # `flock [флаги] файл команда` или `файл -c 'строка'`; файл блокировки создаётся.
    "flock": ({"-w", "--timeout", "-E", "--conflict-exit-code"}, 1),
}
# Короткие флаги sudo со значением: в связке значение берёт последний (`-iu root`).
_SUDO_VALUE_SHORT = frozenset("ugCDhprtU")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SCRIPT_VALUE_SHORT = frozenset("cEIOBTm")


def _script_command(args: list[str]) -> str:
    """Строка команды `script`: `-c X`, `-qcX`, `--command X`, `--command=X`,
    сокращение `--comm=X`. Пусто: script запустит оболочку без команды."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if long_flag(name, "--command", 4):
                return val if eq else (args[i + 1] if i + 1 < len(args) else "")
        elif a.startswith("-") and len(a) > 1:
            for k, c in enumerate(a[1:], 1):
                if c in _SCRIPT_VALUE_SHORT:
                    val = a[k + 1:]
                    if not val:
                        i += 1
                        val = args[i] if i < len(args) else ""
                    if c == "c":
                        return val
                    break
        i += 1
    return ""
# Подкоманды rtk, которые запускают произвольную команду строкой.
_RTK_SHELL = frozenset({"test", "err", "summary", "run"})
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
    # Токены gcloud и бота telegram-send: `cat` шёл без карточки (корпус guard2).
    r"|gcloud/(?:application_default_credentials\.json|credentials\.db|access_tokens\.db"
    r"|legacy_credentials)|telegram-send\.conf"
    r"|(?:^|[/=:@])\.(?:zsh|bash|python|psql|mysql)_history(?:$|/)"
    r"|(?:^|[/=:@])\.[^/]*(?:token|secret|passw|credential)"
    r"|(?:^|/)(?:credentials?(?:\.[^/]*)?|token\.json|id_(?:rsa|dsa|ecdsa|ed25519)|shadow|gshadow)(?:/|$)"
    # Ключи, сертификаты с ключом, хранилища ключей Java и secrets.yaml
    # (решение владельца 14.10.2026).
    r"|(?:^|[/=:@])secrets\.(?:ya?ml|json)(?:$|/)"
    r"|\.(?:key|pem|p12|pfx|jks|keystore)(?:$|/)",
    re.IGNORECASE,
)


# `prod.env`, `secrets.env`, `.envrc`: значения те же, что в `.env`. Только для
# читаемых файлов (_secret_path): в словах команды это ещё и `process.env`.
_ENV_FILE_RE = re.compile(
    r"(?:^|/)(?!(?:example|sample)\.env(?:/|$))[^/]+\.env(?:/|$)|(?:^|/)\.envrc(?:/|$)", re.IGNORECASE)


def _secret_name(path: str) -> bool:
    return bool(_SECRET_WORD_RE.search(path) or _ENV_FILE_RE.search(path))


# Слова команды целиком, включая строки внутри кавычек и тела heredoc:
# программа на python или node, читающая `.env`, разбором оболочки не видна.
_MENTION_SPLIT_RE = re.compile(r"[\s'\"()\[\]{},;`<>|&]+")


def _metadata_only(v: Verdict, command: str) -> bool:
    """Команда ничего не пишет и не запускает незнакомого, а все её программы
    смотрят только сведения о файлах или печатают слова: имя секрета в
    тексте тут аргумент, а не скрытое чтение (`test -f .env && echo да`).
    С подстановкой нет: `test -f .env && echo $TOKEN` печатает значение,
    `for f in .env; do cat $f; done` читает файл (ревью 01.11.2026)."""
    return "$" not in re.sub(r"\$HOME\b", "", command) and v.level == YELLOW and bool(v.programs) and all(
        (prog in METADATA_ONLY and not any(long_flag(a, "--files0-from") for a in args))
        or prog in ("echo", "true", "false", "cd") for prog, args in v.programs)


def _mentions_secret(command: str) -> bool:
    """Секретное имя где угодно в тексте команды (признак Verdict.secret, цвет
    карточки не меняет). Грубо и с запасом: автоаппрув чата и выполнение
    задачи такую команду шлют карточкой (находка Codex 15.10.2026)."""
    return any(_SECRET_WORD_RE.search(word) for word in _MENTION_SPLIT_RE.split(command) if word)


def _secret_path(raw: str, cwd: str | None = None) -> bool:
    if _secret_name(raw):
        return True
    if not raw or raw.startswith("-") or any(c in raw for c in "\n\x00"):
        return False
    try:
        path = Path(raw).expanduser()
        # Ссылки среди совпадений не читают содержимое, но могут передать
        # секрет читалке (ревью 11.10.2026).
        if glob.has_magic(raw) and cwd and any(
                _secret_name(str(Path(cwd, p).resolve()))
                for p in glob.glob(str(path), root_dir=cwd)):
            return True
        if not path.is_absolute():
            if cwd is None:
                return False
            path = Path(cwd) / path
        return _secret_name(str(path.resolve()))
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


def _rg_lists_files(args: list[str]) -> bool:
    """`--files` как флаг: после `--` и за `-e` это слово, а не режим."""
    i = 0
    while i < len(args) and args[i] != "--":
        if args[i] == "--files":
            return True
        i += 2 if args[i] in _RG_VALUES else 1
    return False


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
        if args == ["--version"] or _rg_lists_files(args):
            # Только имена файлов, без чтения содержимого, как find
            # (аудит 31.10.2026). Скрытые и игнорируемые ловит _reads_secret.
            return False
        base = cwd or os.getcwd()
        return not all(os.path.isfile(os.path.join(base, os.path.expanduser(r)))
                       for r in _rg_roots(args))
    if cmd not in ("grep", "egrep", "fgrep"):
        return False
    short = [a for a in args if a.startswith("-") and not a.startswith("--")]
    longs = [a.split("=", 1)[0] for a in args if a.startswith("--")]
    return any("r" in a or "R" in a for a in short) \
        or any(long_flag(n, "--recursive") or long_flag(n, "--dereference-recursive")
               for n in longs) \
        or any(len(d) >= 3 and "recurse".startswith(d)
               for d in _option_values(args, "d", "--directories", _GREP_VALUES))


def _include_names_secret(pattern: str) -> bool:
    """Включающий шаблон (-g у rg, --include у grep) называет секрет или
    скрытые имена, как шаблон оболочки: `.env`, `*.key`, `.*`
    (решение владельца 14.10.2026)."""
    return bool(_SECRET_WORD_RE.search(pattern)) or any(
        part.startswith(".") and glob.has_magic(part) for part in pattern.split("/"))


# Только сведения о файле без содержимого: есть ли, размер, число строк, куда
# ведёт ссылка. `ls -la .env`, `test -f .env`, `ls ~/.ssh` значений не
# показывают (П1 из guard-2.md, задание guard2).
METADATA_ONLY = frozenset({"ls", "stat", "test", "[", "du", "wc", "realpath", "readlink"})
# Переменные без секретов: их значение агент видит и так (П3 из guard-2.md).
PLAIN_VARS = frozenset({"HOME", "USER", "PWD", "LOGNAME", "HOSTNAME", "PATH", "SHELL", "LANG", "TERM"})


def _reads_secret(parts: list[str], cwd: str | None = None) -> bool:
    if not parts:
        return False
    cmd, args = _program(parts[0]), parts[1:]
    # `wc --files0-from=.env` печатает строки файла в ошибках «нет такого файла».
    if cmd in METADATA_ONLY and not any(long_flag(a, "--files0-from") for a in args):
        return False
    if cmd == "printenv" and args and all(a in PLAIN_VARS for a in args):
        return False
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
        return any(long_flag(a, "--recursive", 5) for a in args) or any("r" in a for a in short)
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
    if cmd == "systemctl":
        return _systemctl_env(args)
    if cmd == "systemd-analyze":
        return "dump" in args
    # Имена читаемых файлов не в команде (аудит 31.10.2026, проверено):
    # `find … -print0 | sort --files0-from=-` печатает найденные `.env`,
    # `strings @список` читает файлы из списка. GNU принимает сокращение `--fil`.
    if cmd == "sort" and any(long_flag(a, "--files0-from", 5) for a in args):
        return True
    if cmd == "strings" and any(a.startswith("@") for a in args):
        return True
    # Без операндов печатают переменные со значениями, в том числе окружение.
    if cmd in ("export", "declare", "typeset") and all(a.startswith("-") for a in args):
        return True
    if cmd == "set" and not args:
        return True
    return cmd in ("env", "printenv")


# Значения флагов systemctl, как в `_h_systemctl`.
_SYSTEMCTL_VALUES = frozenset({"-H", "--host", "-M", "--machine", "-t", "--type", "-p",
                               "--property", "-P", "-n", "--lines", "-o", "--output", "--state",
                               "-s", "--signal", "--kill-whom", "--root", "--what",
                               "--job-mode"})


def systemctl_prop_secret(prop: str) -> bool:
    """Свойство `systemctl show`, в значении которого бывают секреты:
    окружение, учётные данные, командная строка запуска (ревью 29.10.2026)."""
    p = prop.lower()
    # Не только буквы и цифры: systemctl снимает экранирование в списке
    # свойств, `Environ\ment` это `Environment` (ревью Codex 29.10).
    return not p.isalnum() or "environment" in p or "credential" in p or (
        p.startswith("exec") and not p.startswith("execmain"))


def _systemctl_env(args: list[str]) -> bool:
    """`cat`, `dump`, `show-environment` и `show` без названных свойств или с
    секретным свойством печатают `Environment=` со значениями: там бывают
    токены службы (ревью 29.10.2026). Глагол ищется среди всех операндов:
    значение незнакомого флага (`--timestamp utc cat x`) сдвигает его."""
    pos = _operands(args, _SYSTEMCTL_VALUES)
    if any(a in ("cat", "dump", "show-environment") for a in pos):
        return True
    if "show" not in pos:
        return False
    # Сокращение `--prop` getopt примет за `--property` (ревью Codex 29.10).
    if any(long_flag(a, "--property", 5) and a.split("=", 1)[0] != "--property" for a in args):
        return True
    props = [p for v in _option_values(args, "p", "--property", _SYSTEMCTL_VALUES)
             + _option_values(args, "P", "", _SYSTEMCTL_VALUES) for p in v.split(",")]
    return not props or any(systemctl_prop_secret(p) for p in props)


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
    # getopt принимает сокращение: `--compil` это `--compile`.
    if "-C" in _flags(args) or any(long_flag(a, "--compile", 4) for a in args):
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
            v.programs.append(("=", [head]))
            parts = parts[1:]
            continue
        if "/" in head:
            _check_program_path(head, v)
        v.programs.append((prog, parts[1:]))
        if prog in ("sudo", "doas"):
            v.add(RED, _("с правами администратора"))
            rest = parts[1:]
            while rest and rest[0].startswith("-"):
                # Длинные формы тоже со значением: `sudo --user root rm` (ревью Codex 29.10).
                takes_value = rest[0] in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U",
                                          "--user", "--group", "--close-from", "--chdir", "--host",
                                          "--prompt", "--role", "--type", "--other-user",
                                          "--command-timeout") or (
                    # Связка: `sudo -iu root rm` (аудит guard 31.10.2026).
                    not rest[0].startswith("--") and len(rest[0]) > 2
                    and rest[0][-1] in _SUDO_VALUE_SHORT
                    and not set(rest[0][1:-1]) & _SUDO_VALUE_SHORT)
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
        if prog == "script":
            # `script -c 'строка' [журнал]` отдаёт строку оболочке и пишет
            # журнал сеанса (аудит guard 31.10.2026, задание guard2).
            v.add(ORANGE, _("script запускает команду и пишет журнал сеанса"))
            command = _script_command(parts[1:])
            if command:
                _classify_bash(command, v, depth + 1)
            return
        if prog in ("strace", "ltrace"):
            parts = _tracer_command(prog, parts[1:], v)
            continue
        if prog in ("taskset", "chrt") and _flags(parts[1:]) & {"-p", "--pid"}:
            v.add(ORANGE, _("меняет параметры запущенного процесса"))
            return
        if prog in _WRAPPERS:
            value_flags, skip = _WRAPPERS[prog]
            rest = parts[1:]
            # watch склеивает аргументы и отдаёт их `sh -c`, с -x запускает как есть.
            as_string = prog == "watch"
            while rest and (rest[0].startswith("-") or (prog == "env" and _ASSIGN_RE.match(rest[0]))):
                flag = rest[0]
                # Слитные формы тоже: `-S'rm x'`, `--split-string=…`, `-iS` (ревью Codex 29.10).
                if prog == "env" and (flag.startswith("-S") or long_flag(flag, "--split-string") or (
                        not flag.startswith("--") and "S" in flag[1:])):
                    v.add(ORANGE, _("env -S: команда из строки, не разобрана"), UNPARSED)
                    return
                if prog == "env" and _ASSIGN_RE.match(flag):
                    _check_assignment(flag, v)
                if prog == "watch" and flag in ("-x", "--exec"):
                    as_string = False
                rest = rest[2:] if flag in value_flags else rest[1:]
            if prog == "rtk" and rest[:1] and rest[0] in _RTK_SHELL:
                # `rtk test|err|summary|run` склеивают аргументы и отдают их
                # оболочке (проверено rtk 0.42.4, аудит guard 31.10.2026).
                v.add(ORANGE, _("rtk {sub} запускает команду оболочкой").format(sub=rest[0]))
                if rest[1:]:
                    _classify_bash(" ".join(rest[1:]), v, depth + 1)
                return
            if prog == "rtk" and rest[:1] == ["proxy"]:
                rest = rest[1:]
            elif prog == "rtk" and rest[:1] == ["grep"]:
                # `rtk grep` под капотом rg: его флаги (`--hidden`, `--pre`)
                # оцениваются как у rg, иначе секрет не виден (ревью 25.10.2026).
                rest = ["rg", *rest[1:]]
            if as_string:
                if rest:
                    _classify_bash(" ".join(rest), v, depth + 1)
                return
            if prog == "env" and not rest:
                # Голое `env` (и с -0, -u) печатает окружение: как обёртку его
                # нечем было проверить, признак секрета терялся (аудит 31.10.2026).
                v.secret = True
                v.add(ORANGE, _("может вывести секреты или окружение процессов"))
                return
            if prog == "flock":
                v.add(ORANGE, _("создаёт файл блокировки"))
                # `-c` флаг flock только до файла блокировки и сразу за ним, дальше
                # идут аргументы самой команды: `flock f git -c x=y push`.
                args, i = parts[1:], 0
                while i < len(args) and args[i].startswith("-"):
                    a = args[i]
                    if a.startswith("--command="):
                        _classify_bash(a.split("=", 1)[1], v, depth + 1)
                        return
                    if a == "--command" or (not a.startswith("--") and a.endswith("c")):
                        if args[i + 1:i + 2]:
                            _classify_bash(args[i + 1], v, depth + 1)
                        return
                    i += 2 if a in value_flags else 1
                if args[i + 1:i + 2] and (args[i + 1] in ("-c", "--command") or args[i + 1].startswith("--command=")):
                    command = args[i + 1].split("=", 1)[1] if "=" in args[i + 1] else " ".join(args[i + 2:i + 3])
                    if command:
                        _classify_bash(command, v, depth + 1)
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
        v.add(ORANGE, _("команда из переменной, не разобрана"), UNPARSED)
        return
    if parts[0] not in ("[", "[[") and any(c in parts[0] for c in "${}*?["):
        # `/???/r? -rf x`, `{rm,-rf,x}`: имя программы раскроет оболочка.
        v.add(ORANGE, _("имя команды раскрывается оболочкой, не разобрано"), UNPARSED)
        return
    first = next((a for a in args if not a.startswith("-")), "")
    if (first.startswith("$") or "{" in first) and prog in _SUBCOMMAND_PROGS:
        # `git $a`: подкоманда из переменной (ревью 29.10.2026).
        v.add(ORANGE, _("подкоманда из переменной, не разобрана"), UNPARSED)
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
        tagged = _tagged_program(prog, args)
        if tagged:
            v.add(RED, _("необратимое или наружу ({prog})").format(prog=prog), tagged)
        handler = _HANDLERS.get(prog)
        if handler is not None:
            handler(prog, args, v, depth)
            return
        if prog.startswith("mkfs"):
            v.add(RED, _("форматирует диск"), DISK)
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


def long_flag(arg: str, full: str, min_len: int = 3) -> bool:
    """Длинный флаг `full` или его сокращение: getopt_long и parse-options git
    принимают однозначное начало (`--in` это `--in-place`, `--ha` это
    `--hard`). Значение через `=` не мешает. min_len: длина сокращения с
    `--`, короче которой оно неоднозначно у этой программы."""
    name = arg.split("=", 1)[0]
    return name.startswith("--") and len(name) >= min_len and full.startswith(name)


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


def _raw_device(target: str) -> bool:
    return target.startswith("/dev/") and not target.startswith(
        ("/dev/null", "/dev/std", "/dev/tty", "/dev/fd/", "/dev/pts/", "/dev/shm/"))


def _write_target(target: str, v: Verdict, reason: str = N_("пишет вывод в файл")) -> None:
    if target in ("", "-", "/dev/null"):
        return
    if target.startswith("/proc/sysrq"):
        v.add(RED, _("перезагружает или выключает машину"), POWER)
        return
    if _raw_device(target):
        v.add(RED, _("пишет на диск или в файл напрямую (dd)"), DISK)
        return
    if _protected_path(target):
        v.add(RED, _("перезаписывает {target}").format(target=target), GUARD)
    else:
        v.add(ORANGE, _(reason))


def _h_delete(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    targets = _positionals(args)
    if prog == "rm" and targets and all(os.path.normpath(t).startswith("/tmp/") for t in targets):
        v.add(ORANGE, _("удаляет временные файлы в /tmp"))
        return
    if prog == "dd":
        v.add(RED, _("пишет на диск или в файл напрямую (dd)"), DISK)
        return
    v.add(RED, _("удаляет или затирает файлы"), DELETE)


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
        v.add(RED, _("меняет ключи, настройки или код бота"), GUARD)
    if any(_raw_device(t) for t in touched):
        v.add(RED, _("пишет на диск или в файл напрямую (dd)"), DISK)
    if prog == "rsync" and any(":" in t and not t.startswith("/") for t in targets):
        v.add(RED, _("копирует на удалённый сервер или с него"))
    if prog == "rsync":
        # Не только последний: значение флага после цели (`… host:/d --exclude x`).
        for target in targets[1:]:
            _note_remote(target, v)
    if "--delete" in _flags(args) or any(a.startswith("--delete") for a in args):
        v.add(RED, _("удаляет файлы в месте назначения (--delete)"), DELETE)
    v.add(ORANGE, _("копирует, перемещает или создаёт файлы"))


def _h_sed(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    flags = _flags(args)
    # getopt_long понимает сокращение: `--in`, `--i` это `--in-place`.
    if "-i" in flags or any(a.startswith("-i") for a in args) or any(
            long_flag(f, "--in-place") for f in flags):
        v.add(ORANGE, _("правит файлы на месте"))
    if _option_values(args, "f", "--file"):
        v.add(ORANGE, _("скрипт sed из файла, не разобран"))
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


def _sed_address(n: str) -> str:
    """Адрес sed: номер, `$`, `first~step`, `/re/` и `\\cREc` с флагами I и M
    (аудит guard 31.10.2026: `/x/Ie cmd` и `\\%x%e cmd` запускали команду)."""
    return (rf"(?:[0-9$~+]+|/(?:[^/\\\n]|\\.)*/[IM]*"
            rf"|\\(?P<{n}>\S)(?:(?!(?P={n})).)*(?P={n})[IM]*)")


_SED_EXEC_RE = re.compile(
    rf"(?:^|[;{{}}\n])\s*(?:{_sed_address('a1')}(?:\s*,\s*{_sed_address('a2')})?)?\s*!?\s*"
    r"(?:[wW]\s*\S|e(?:$|[\s;}])|s(?P<d>.).*?(?P=d).*?(?P=d)[gpiImM0-9]*(?:e|w\s*\S))"
)


def _h_tee(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # Как у перенаправления: устройства, sysrq, защищённые пути (ревью Codex 29.10).
    for t in _positionals(args):
        _write_target(t, v)


def _h_find(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if any(a in ("-delete",) for a in args):
        v.add(RED, _("удаляет найденные файлы"), DELETE)
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
    # getopt принимает однозначное сокращение: `--co=sh` это `--compress-program=sh`,
    # и при сбросе во временные файлы строки ввода исполняет sh (аудит 31.10.2026).
    if any(long_flag(a, "--compress-program", 4) for a in args):
        v.add(ORANGE, _("sort запускает внешнюю программу"))
    if prog == "tree" and {"-R", "-H"} <= _flags(args):
        # `tree -R -H …` пишет 00Tree.html в каждый подкаталог (аудит 31.10.2026).
        v.add(ORANGE, _("пишет вывод в файл"))
    # Имена читаемых файлов из файла или потока (`printf '\\056env\\0' |
    # sort --files0-from=-`): проверка имён их не видит (аудит guard 31.10.2026).
    if prog == "sort" and any(long_flag(a, "--files0-from", 5) for a in args):
        v.secret = True
        v.add(ORANGE, _("читает файлы по списку имён из файла или потока"))
    # Шаблон может раскрыться в имя вида `-ofile` и стать файлом вывода, как у
    # uniq ниже (ревью 11.10.2026; разрешение чтения по danger.py 29.10.2026).
    if any(glob.has_magic(a) for a in args):
        v.add(ORANGE, _("шаблон в аргументах может стать флагом записи"))


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
    if any(glob.has_magic(a) for a in operands):
        cwd = _CWD.get()
        if cwd and all(not glob.has_magic(a) or a.startswith("./") for a in operands):
            operands = [p for a in operands for p in (glob.glob(a, root_dir=cwd) or [a])]
        else:
            v.add(ORANGE, _("шаблон может подставить файл вывода"))
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
        v.add(RED, _("меняет ключи, настройки или код бота"), GUARD)
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
_GIT_TAG_LIST_BOOL = frozenset({"-l", "--list", "-i", "--ignore-case", "--column", "--no-column",
                                "--omit-empty", "--color", "--no-color"})
_GIT_TAG_LIST_VALUE = frozenset({"--sort", "--format", "--contains", "--no-contains", "--merged",
                                 "--no-merged", "--points-at"})


def git_tag_lists(rest: list[str]) -> bool:
    """`git tag` только показывает метки: без имени новой метки или с -l
    (тогда слова это шаблоны). Незнакомый флаг значит не список
    (аудит 31.10.2026)."""
    list_mode = positionals = False
    i = 0
    while i < len(rest):
        a = rest[i]
        name = a.split("=", 1)[0]
        if name in _GIT_TAG_LIST_VALUE:
            i += 1 if "=" in a else 2
            continue
        if a in ("-l", "--list"):
            list_mode = True
        elif a in _GIT_TAG_LIST_BOOL or re.fullmatch(r"-n\d*", a):
            pass
        elif a.startswith("-"):
            return False
        else:
            positionals = True
        i += 1
    return list_mode or not positionals


def git_listing(sub: str, rest: list[str]) -> bool:
    """Подкоманды git, которые с этими аргументами только показывают."""
    if sub == "tag":
        return git_tag_lists(rest)
    if sub == "stash":
        return rest[:1] in (["list"], ["show"])
    if sub == "worktree":
        return rest[:1] == ["list"] and all(a in ("--porcelain", "-v", "--verbose", "-z") for a in rest[1:])
    return False


# Флаги со значением у push и clean: `-o -n` это опция push «-n», а не
# --dry-run (аудит guard 31.10.2026, проверено git 2.43).
_GIT_DRY_VALUES = {
    "push": ("o", ("--repo", "--receive-pack", "--exec", "--push-option")),
    "clean": ("e", ("--exclude",)),
}


def _git_long(flags: set[str], *names: str) -> bool:
    """Длинный флаг git с учётом сокращения: parse-options принимает
    однозначное начало (`--h` это `--hard`, `--d` у branch это `--delete`)."""
    return any(long_flag(f, n) for f in flags for n in names)


def _git_dry(sub: str, rest: list[str]) -> bool:
    """Пробный прогон по последнему из -n, --dry-run и --no-dry-run; значение
    флага (`-o -n`, `-e -n`) не флаг."""
    short_values, long_values = _GIT_DRY_VALUES.get(sub, ("", ()))
    dry = False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if long_flag(name, "--no-dry-run", 6):
                dry = False
            elif long_flag(name, "--dry-run", 5):
                dry = True
            elif "=" not in a and _git_long({name}, *long_values):
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for k, c in enumerate(a[1:], 1):
                if c in short_values:
                    i += k == len(a) - 1
                    break
                dry = dry or c == "n"
        i += 1
    return dry


def _git_checkout_paths(rest: list[str]) -> bool:
    """`git checkout <путь>` и `git checkout <ревизия> <путь>` без `--`
    выбрасывают правки в файлах так же, как `git checkout -- путь`."""
    flags = _flags(rest)
    if {"-b", "-B"} & flags or _git_long(flags, "--orphan", "--detach"):
        return False
    if _git_long(flags, "--pathspec-from-file"):
        return True
    pos = _operands(rest, frozenset({"--conflict", "--pathspec-from-file"}))
    if len(pos) >= 2:
        return True
    return any((path := _absolute(p)) is not None and os.path.lexists(path) for p in pos)


def _h_git(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    sub, rest, has_config = _git_sub(args)
    flags = _flags(rest)
    dry = _git_dry(sub, rest)
    if has_config:
        v.add(ORANGE, _("git с разовыми настройками (-c)"))
    # --no-index обходит каталоги вместе со скрытыми файлами. Для обычного
    # сравнения двух названных файлов карточка не нужна (ревью 11.10.2026).
    if sub == "diff" and "--no-index" in rest and any(
            (absolute := _absolute(p)) and Path(absolute).is_dir() for p in _positionals(rest)):
        v.add(ORANGE, _("сравнивает каталоги вместе со скрытыми файлами"))
    if sub == "push" or (sub == "lfs" and rest[:1] == ["push"]):
        if not dry:
            v.add(RED, _("публикует в git"), PUBLISH)
        return
    # Отправка наружу и запуск программ (ревью 11.10.2026).
    if sub in ("send-email", "send-pack", "http-push", "daemon", "imap-send") or (
            sub == "svn" and rest[:1] == ["dcommit"]):
        v.add(RED, _("отправляет наружу (git {sub})").format(sub=sub), PUBLISH)
        return
    # git grep принимает сокращение: `--open-fi=cmd` запускает cmd оболочкой.
    if any(a.startswith(("--upload-pack", "--receive-pack", "--exec"))
           or long_flag(a, "--open-files-in-pager", 4)
           for a in rest) or (sub == "grep" and "-O" in flags):
        v.add(ORANGE, _("git запускает внешнюю программу"))
    if sub == "archive":
        for target in _option_values(rest, "o", "--output"):
            _write_target(target, v)
    if sub == "reset" and _git_long(flags, "--hard"):
        v.add(RED, _(_GIT_RED), HISTORY)
    elif sub == "checkout-index" and ({"-f", "--force"} & flags):
        v.add(RED, _(_GIT_RED), HISTORY)
    elif sub == "reset":
        v.add(ORANGE, _("меняет репозиторий (git reset)"))
    elif sub in ("read-tree", "update-index", "checkout-index", "format-patch") or (
            sub == "bundle" and rest[:1] == ["create"]) or (
            sub == "symbolic-ref" and (len(_positionals(rest)) >= 2 or {"-d", "--delete"} & flags)):
        v.add(ORANGE, _("меняет репозиторий или пишет файлы (git {sub})").format(sub=sub))
    elif sub == "clean" and not dry:
        v.add(RED, _("удаляет неотслеживаемые файлы (git clean)"), DELETE)
    elif (sub == "checkout" and "-B" in flags) or (sub == "switch" and ("-C" in flags or _git_long(flags, "--force-create"))):
        # Сбрасывает существующую ветку, как `git branch -f` (аудит 31.10.2026).
        v.add(RED, _("удаляет или переписывает ветки"), HISTORY)
    elif sub == "checkout" and ("-f" in flags or _git_long(flags, "--force") or "--" in rest
                                or "." in rest or _git_checkout_paths(rest)):
        v.add(RED, _(_GIT_RED), HISTORY)
    elif sub == "restore":
        v.add(RED, _(_GIT_RED), HISTORY)
    elif sub == "switch" and ("-f" in flags or _git_long(flags, "--discard-changes", "--force")):
        v.add(RED, _(_GIT_RED), HISTORY)
    elif sub == "branch" and ({"-D", "-d", "-M", "-m", "-f"} & flags
                              or _git_long(flags, "--delete", "--force", "--move")):
        v.add(RED, _("удаляет или переписывает ветки"), HISTORY)
    elif sub == "reflog" and rest and rest[0] in ("expire", "delete", "drop", "write"):
        v.add(RED, _(_GIT_RED), HISTORY)
    elif sub in ("rebase", "filter-branch", "filter-repo", "gc", "prune", "update-ref"):
        v.add(RED, _(_GIT_RED), HISTORY)
    elif sub == "commit" and _git_long(flags, "--amend"):
        v.add(RED, _("переписывает последний коммит"), HISTORY)
    elif sub == "stash" and rest and rest[0] in ("drop", "clear", "pop"):
        v.add(RED, _(_GIT_RED), HISTORY)
    elif sub == "worktree" and rest and rest[0] in ("remove", "prune"):
        v.add(RED, _("удаляет рабочие копии git"), DELETE)
    elif sub == "tag" and ({"-d", "--delete", "-f", "--force"} & flags):
        v.add(RED, _("удаляет или переписывает метки git"), HISTORY)
    elif sub == "remote" and rest and rest[0] in ("add", "remove", "rm", "set-url", "rename"):
        v.add(ORANGE, _("меняет настройки удалённых репозиториев"))
    elif sub == "config" and not ({"--get", "--list", "-l", "--get-all", "--get-regexp"} & flags) \
            and _positionals(rest)[:1] not in (["get"], ["list"]):
        if {"--global", "--system"} & flags:
            # ~/.gitconfig и /etc/gitconfig защищены (_PROTECTED_FILES), а путь
            # в команде не назван (аудит 31.10.2026).
            v.add(RED, _("меняет ключи, настройки или код бота"), GUARD)
        else:
            v.add(ORANGE, _("меняет настройки git"))
    elif sub in ("tag", "stash", "worktree") and git_listing(sub, rest):
        pass
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


# Действия systemctl над всей машиной, а не службой (ревью Codex 29.10).
_POWER_ACTIONS = frozenset({
    "reboot", "poweroff", "halt", "suspend", "hibernate", "isolate", "default", "rescue",
    "emergency", "exit", "soft-reboot", "suspend-then-hibernate", "hybrid-sleep", "sleep",
    "kexec", "switch-root",
})


def _h_systemctl(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if any(a.startswith(("--host", "--machine")) or a in ("-H", "-M") for a in args):
        v.add(RED, _("на другой машине (systemctl --host)"))
    # Без значений флагов: `systemctl -H web1 reboot` (ревью Codex 29.10).
    pos = _operands(args, frozenset({"-H", "--host", "-M", "--machine", "-t", "--type", "-p",
                                     "--property", "-n", "--lines", "-o", "--output", "--state",
                                     "-s", "--signal", "--kill-whom", "--root", "--what",
                                     "--job-mode"}))
    acts = [a for a in pos if a in _SVC_ACTIONS]
    # Действие первое слово: `systemctl status reboot` смотрит службу (ревью Codex 29.10).
    if (pos[:1] and pos[0] in _POWER_ACTIONS) or (acts and any(
            a.split(".")[0] in _POWER_ACTIONS and a.endswith(".target") for a in pos[1:])):
        v.add(RED, _("перезагружает или выключает машину"), POWER)
    if pos[:1] == ["clean"]:
        v.add(RED, _("удаляет данные службы (systemctl clean)"), DELETE)
    if acts:
        v.add(RED, _("управляет службами (systemctl {acts0})").format(acts0=acts[0]))


def _h_service(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    if len(pos) >= 2 and pos[1] not in ("status",):
        v.add(RED, _("управляет службами (service {pos1})").format(pos1=pos[1]))


def _h_power(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    v.add(RED, _("перезагружает или выключает машину"), POWER)


def _kill_targets(args: list[str]) -> list[str]:
    """Цели `kill` без сигнала: `-9`, `-KILL`, `-s 9`, `-n 9`, `--signal 9`
    первым словом это сигнал, а `-1` дальше или после `--` это все процессы."""
    rest = list(args)
    if rest and rest[0] in ("-s", "-n", "--signal"):
        rest = rest[2:]
    elif rest and rest[0].startswith("-") and rest[0] != "--":
        rest = rest[1:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return rest


def _h_kill(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # Список сигналов только отдельным флагом: в `-KILL` буква L это имя сигнала.
    if prog == "kill" and args[:1] and args[0] in ("-l", "-L", "--list", "--table"):
        return
    if prog == "kill" and "-1" in _kill_targets(args):
        # Все процессы пользователя, с ботом и сеансом (аудит guard 31.10.2026).
        v.add(RED, _("завершает все процессы пользователя"), POWER)
        return
    v.add(RED, _("завершает процессы"))


_JOURNAL_WRITE_OPTS = ("--vacuum-time", "--vacuum-size", "--vacuum-files", "--rotate", "--flush",
                       "--sync", "--relinquish-var", "--smart-relinquish-var", "--setup-keys",
                       "--update-catalog")


def _h_journalctl(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    # `--cursor-file=F` в конце пишет курсор в F, создаёт файл (аудит
    # 31.10.2026, проверено); `--cursor-` уже однозначное сокращение.
    for i, a in enumerate(args):
        name = a.split("=", 1)[0]
        if long_flag(name, "--cursor-file", len("--cursor-")):
            target = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            _write_target(target, v)
            v.add(ORANGE, _("пишет вывод в файл"))
    # getopt принимает однозначное сокращение: `--rot` это `--rotate`
    # (ревью 11.10.2026).
    for a in args:
        name = a.split("=", 1)[0]
        if a.startswith(("--vacuum", "--rotate", "--flush", "--sync", "--relinquish-var",
                         "--smart-relinquish-var", "--setup-keys", "--update-catalog")) or (
                any(long_flag(name, o, 5) for o in _JOURNAL_WRITE_OPTS)):
            vacuum = a.startswith("--vacuum") or any(
                long_flag(name, o, 5) for o in _JOURNAL_WRITE_OPTS[:3])
            v.add(RED, _("удаляет или переписывает системные журналы"), DELETE if vacuum else "")
            return


# Глобальные флаги docker со значением: `docker --context prod volume rm x`
# (ревью Codex 29.10).
_DOCKER_VALUES = frozenset({"--context", "-c", "-H", "--host", "--config", "-l", "--log-level",
                            "--tlscacert", "--tlscert", "--tlskey", "-f", "--file", "-p",
                            "--project-name", "--project-directory", "--env-file", "--profile",
                            "--ansi", "--progress", "--parallel"})


def _h_docker(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _operands(args, _DOCKER_VALUES)
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
        # Тома и данные в них: `down -v`, `rm -v`. Сами контейнеры
        # пересоздаются, их удаление обычный шаг обновления (ревью Codex 29.10).
        volumes = sub in ("down", "rm") and ({"-v", "--volumes"} & _flags(args))
        if sub:
            v.add(RED, _("управляет контейнерами (docker compose {sub})").format(sub=sub),
                  DELETE if volumes else PUBLISH if sub == "push" else "")
        return
    if pos[0] in ("volume", "network", "image", "container", "system", "builder", "buildx"):
        sub = pos[1] if len(pos) > 1 else ""
        if sub and sub not in {"ls", "list", "inspect", "logs", "ps", "top", "stats", "history",
                               "port", "diff", "df", "info"}:
            drops = (pos[0] in ("volume", "system", "container") and sub == "prune") or (
                pos[0] == "volume" and sub in ("rm", "remove")) or (
                pos[0] == "container" and sub in ("rm", "remove") and ({"-v", "--volumes"} & _flags(args)))
            v.add(RED, _("управляет ресурсами Docker ({pos0} {sub})").format(pos0=pos[0], sub=sub),
                  DELETE if drops else PUBLISH if (pos[0], sub) == ("image", "push") else "")
        return
    if (pos[0] == "rm" and ({"-v", "--volumes"} & _flags(args))) or pos[0] == "push":
        v.add(RED, _("управляет контейнерами (docker {pos0})").format(pos0=pos[0]),
              PUBLISH if pos[0] == "push" else DELETE)
    elif pos[0] not in read:
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
        v.add(RED, _("меняет кластер Kubernetes (kubectl {pos0})").format(pos0=pos[0]),
              DELETE if pos[0] == "delete" or (pos[0] == "replace" and "--force" in args) else "")


def _h_helm(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _operands(args, _KUBE_VALUES)
    read = {"list", "ls", "status", "get", "history", "show", "search", "template", "lint",
            "version", "env", "repo", "dependency", "verify"}
    if pos and pos[0] not in read:
        v.add(RED, _("меняет установку в кластере (helm {pos0})").format(pos0=pos[0]),
              DELETE if pos[0] in ("uninstall", "delete", "del", "un") else "")


def _h_terraform(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    pos = _positionals(args)
    # `terraform test` создаёт настоящие ресурсы, `workspace delete` удаляет
    # состояние (ревью 11.10.2026).
    if pos and (pos[0] in ("apply", "destroy", "import", "taint", "untaint", "state", "force-unlock",
                           "test")
                or pos[:2] == ["workspace", "delete"]):
        what = " ".join(pos[:2]) if pos[0] == "workspace" else pos[0]
        v.add(RED, _("меняет облачную инфраструктуру (terraform {what})").format(what=what),
              DELETE if pos[0] in ("destroy", "workspace") or pos[:2] in (["state", "rm"], ["state", "push"])
              or (pos[0] == "apply" and any(a in ("-destroy", "--destroy") for a in args)) else "")
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


_REMOTE_RE = re.compile(r"(?:\w+://)?(?:[^@/:]+@)?(\[[^\]]+\]|[^@/:\[]+)(?::|/|$)")


def _note_remote(target: str, v: Verdict) -> None:
    """Запомнить хост цели `host:путь`, `user@host::модуль`, `scp://host/…`,
    `[ipv6]:путь`. Цель из переменной не известна: запоминается как есть, такого
    имени в плане нет, и она идёт карточкой."""
    if "$" in target and not target.startswith(("$HOME", "${HOME}")):
        v.remotes.add(target.lower())
        return
    if target.startswith(("/", "./", "../", "~", "$")) or ":" not in target:
        return
    m = _REMOTE_RE.match(target)
    v.remotes.add(m.group(1).lower() if m else target)


_SCP_VALUES = frozenset({"-P", "-i", "-l", "-o", "-F", "-J", "-S", "-c", "-D", "-X"})
_SFTP_VALUES = _SCP_VALUES | {"-b", "-B", "-R", "-s"}


def _h_remote(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    if prog in ("scp", "sftp"):
        v.add(RED, _("копирует на удалённый сервер или с него"))
        operands = _operands(args, _SFTP_VALUES if prog == "sftp" else _SCP_VALUES)
        if prog == "sftp" and operands:
            # Интерактивная сессия: что уйдёт, не видно, цель сам сервер.
            _note_remote(operands[0] if ":" in operands[0] else operands[0] + ":", v)
        else:
            for target in operands[1:]:
                _note_remote(target, v)
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
    v.tags |= inner.tags
    v.remotes |= inner.remotes
    v.secret = v.secret or inner.secret
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


def _note_s3(args: list[str], high_level: bool, verb: str, v: Verdict) -> None:
    """Бакет и сервер, куда aws кладёт данные. Источник `-` и неизвестные флаги
    сдвигают позиции, поэтому цель ищется по самому адресу: перед ней есть
    операнд, значит это отправка, а не скачивание."""
    tail = args[args.index(verb) + 1:] if verb in args else []
    uris = [a for a in tail if a.startswith("s3://")]
    if high_level and verb in ("cp", "sync", "mv") and uris and any(
            a == "-" or not a.startswith("-") for a in tail[:tail.index(uris[-1])]):
        v.remotes.add(uris[-1][5:].split("/")[0].lower())
    elif verb in ("put-object", "upload-part", "copy-object"):
        v.remotes.update(b.lower() for b in _option_values(args, "", "--bucket"))
    if v.remotes:
        v.remotes.update(u.lower() for u in _option_values(args, "", "--endpoint-url"))


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
    # У aws глагол одно слово, файлы рядом не глаголы (`s3 cp delete s3://…`);
    # у az, gcloud, doctl глагол бывает глубже групп (ревью Codex 29.10).
    if prog == "aws":
        _note_s3(args, pos[:1] == ["s3"], verb, v)
    words = [verb] if prog == "aws" else pos
    drops = "--delete" in args or any(
        p in ("delete", "remove", "destroy", "terminate", "rm", "rb", "purge")
        or p.startswith(("delete-", "remove-", "terminate-")) for p in words)
    v.add(RED, _("меняет ресурсы в облаке ({prog} {args})").format(prog=prog, args=' '.join(pos[:2])),
          DELETE if drops else "")


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
        v.add(RED, _("отправляет запрос {method} в сеть").format(method=method), PUBLISH)
    if any("$(" in a or "`" in a for a in args):
        # Вывод команды в адресе или заголовке уходит и запросом GET (ревью 29.10.2026).
        v.add(RED, _("отправляет данные в сеть"), PUBLISH)
    if any(a.startswith(send_flags) for a in args if a.startswith("-")) or any(
            a.startswith("-") and not a.startswith("--") and set(a[1:]) & {"d", "F", "T"}
            for a in args) or (prog == "curl" and (
                # Файл настроек curl (`-K`, `--config`) задаёт и данные, и
                # загрузку: `data = "…"` это POST (аудит guard 31.10.2026).
                _option_values(args, "K", "--config", _CURL_VALUE_FLAGS)
                or any(a.startswith(("--expand-data", "--expand-json")) for a in args))):
        v.add(RED, _("отправляет данные в сеть"), PUBLISH)
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
    if prog in ("npm", "pnpm", "yarn") and pos[:1] == ["version"] and len(pos) > 1:
        v.add(ORANGE, _("меняет версию проекта и делает коммит ({prog} version)").format(prog=prog))
        return
    if pos and pos[0] in _PKG_PUBLISH and prog not in ("apt", "apt-get", "dpkg", "snap"):
        v.add(RED, _("публикует пакет или меняет доступ к нему ({prog} {pos0})").format(prog=prog, pos0=pos[0]),
              PUBLISH)
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
    if re.search(r"\b(DROP|DELETE|TRUNCATE|UPDATE|ALTER|INSERT|GRANT|REVOKE|CREATE|FLUSHALL|FLUSHDB|DEL|"
                 r"UNLINK|REPLACE|DELETEMANY|DELETEONE|DROPDATABASE|FINDONEANDDELETE|REMOVE)\b|\.DROP\(", text):
        # Строковые литералы SQL не команды: `SELECT 'DELETE'` (ревью Codex 29.10).
        drops = re.search(r"\b(DROP|DELETE|TRUNCATE|FLUSHALL|FLUSHDB|DEL|UNLINK|REPLACE|DELETEMANY|"
                          r"DELETEONE|DROPDATABASE|FINDONEANDDELETE|REMOVE)\b|\.DROP\(",
                          re.sub(r"'[^']*'", "''", text))
        v.add(RED, _("меняет базу данных"), DELETE if drops else "")
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
    if any(a == "--web" or a.startswith("--web=") for a in args):
        v.add(ORANGE, _("открывает браузер (gh --web)"))
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
            v.add(RED, _("действие на GitHub от твоего имени (gh api)"), PUBLISH)
        return
    generic_red = {"delete", "create", "edit", "close", "archive", "remove", "set", "add",
                   "merge", "transfer", "copy", "link", "unlink", "mark-template", "item-add",
                   "item-delete", "item-edit", "item-archive", "field-create", "field-delete"}
    if sub in _GH_RED.get(group, set()) or (group not in _GH_RED and sub in generic_red):
        v.add(RED, _("действие на GitHub от твоего имени (gh {group} {sub})").format(group=group, sub=sub),
              PUBLISH)
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
        v.add(RED, _("меняет общие правила всех сессий (память global)"), GUARD)
    else:
        v.add(ORANGE, _("пишет в память ({scope})").format(scope=scope or _("область не указана")))


def _h_ss(prog: str, args: list[str], v: Verdict, depth: int) -> None:
    flags = _flags(args)
    if "-K" in flags or "--kill" in flags or any(a.startswith("-") and not a.startswith("--") and "K" in a for a in args):
        v.add(RED, _("рвёт сетевые соединения (ss -K)"))


_HANDLERS = {
    "ss": _h_ss,
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
    # Аудит guard 31.10.2026: настройки git по XDG равны ~/.gitconfig, плагины
    # Claude Code несут хуки, агенты и команды это указания, как CLAUDE.md.
    _HOME / ".config" / "git", _HOME / ".claude" / "plugins", _HOME / ".claude" / "agents",
    _HOME / ".claude" / "commands",
)
_PROTECTED_FILES = (
    _HOME / ".bashrc", _HOME / ".profile", _HOME / ".bash_profile", _HOME / ".zshrc",
    _HOME / ".gitconfig", _HOME / ".claude" / "settings.json",
    _HOME / ".claude" / "settings.local.json", _HOME / ".claude" / "CLAUDE.md",
    _HOME / ".claude.json", _HOME / ".zshenv",  # решение владельца 12.10.2026
    # Оболочка читает их так же, как .bashrc и .zshrc (аудит guard 31.10.2026).
    _HOME / ".bash_aliases", _HOME / ".bash_login", _HOME / ".bash_logout",
    _HOME / ".zprofile", _HOME / ".zlogin", _HOME / ".zlogout",
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
    # Настройки Claude Code в проекте: хуки и MCP-сервер исполнятся при следующем
    # ране, в том числе по одобренному плану. Память `.claude/projects/` не они.
    if p.name == ".mcp.json" or any(
            part == ".claude" and Path(*p.parts[:i]) != _HOME and p.parts[i + 1:i + 2] != ("projects",)
            for i, part in enumerate(p.parts)):
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
        v.add(RED, _("меняет ключи, настройки, память или код бота"), GUARD)
        return
    v.add(ORANGE, _("пишет файл {os}").format(os=os.path.basename(raw)))
    resolved = Path(absolute)
    try:
        real = resolved.resolve()
    except OSError:
        real = resolved
    if real != Path(os.path.normpath(resolved)) and _protected_path(str(real)):
        v.add(RED, _("путь ведёт по ссылке к ключам, настройкам или коду бота"), GUARD)


_MCP_RED_WORDS = frozenset({
    "send", "delete", "remove", "trash", "forward", "reply", "publish", "share", "post",
    "merge", "close", "archive", "cancel", "revoke", "transfer", "pay", "invite", "kick",
    "ban", "block", "leave", "mark", "spam", "respond",
})


def _classify_mcp(tool_name: str, v: Verdict) -> None:
    short = tool_name.split("__", 2)[-1].lower()
    words = set(re.split(r"[_\-]+", short))
    if words & _MCP_RED_WORDS:
        v.add(RED, _("действие во внешнем сервисе от твоего имени"), PUBLISH)
    else:
        v.add(ORANGE, _("меняет данные во внешнем сервисе"))

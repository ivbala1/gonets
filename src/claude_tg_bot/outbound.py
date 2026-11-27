"""Отправки людям от имени Ивана: распознавание, вид карточки и решение хука.

approval.handle_approve зовёт одну функцию, decide: отправка себе идёт без
карточки, отправка, которая точно не уйдёт или не положена, получает отказ с
причиной агенту, остальное идёт карточкой мимо всех автоаппрувов (решения
Ивана 12.10, 13.10 и 03.11.2026, docs/design-2026-11-03-send-card.md).
Тап ✅ по карточке с адресатом, текстом и файлом и есть «ок» на точный текст.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import mailsend, otklik, paths
from .i18n import _, N_

log = logging.getLogger(__name__)


def _esc(s: str) -> str:
    """HTML-escape для Telegram parse_mode=HTML."""
    return html.escape(s, quote=False)


def _esc_truncate(s: str, limit: int) -> str:
    """Escape + обрезка по длине *escape-нутой* строки.

    Важно обрезать ПОСЛЕ escape: один символ `<` превращается в `&lt;` (4 char).
    Иначе обрезка до 2000 исходных символов может дать 8000+ escape-нутых
    и выйти за Telegram-лимит 4096.
    """
    escaped = _esc(s)
    if len(escaped) <= limit:
        return escaped
    cut = escaped[:limit]
    # Если обрезали посреди HTML-entity (`&lt;`, `&amp;` и т.п.), откатываемся
    # до последнего `&`, у которого нет закрывающего `;` в хвосте.
    last_amp = cut.rfind("&")
    if last_amp >= 0 and ";" not in cut[last_amp:]:
        cut = cut[:last_amp]
    return cut + " …"


# Отправка наружу от имени Ивана (Telegram его аккаунтом, письмо из его
# Gmail): всегда карточка, раньше любых автоаппрувов (чат Домового,
# фоновый ран, окно /auto). Тап ✅ по карточке с адресатом, текстом и файлом и
# есть «ок» на точный текст: правило держит бот, а не дисциплина агента.
# Совет GPT Sol и решение Ивана 12.10.2026. Ловим по серверу и глаголу, а
# не списком имён: у серверов есть send_voice, send_album, edit_message,
# send_scheduled_message, slack_send_message (ревью Codex 12.10.2026).
_SERVERS = frozenset({"telegram", "claude_ai_Gmail", "claude_ai_Slack"})
# Те же почта и Slack из плагинов (`plugin_engineering_gmail`), аудит guard
# 31.10.2026. Telegram плагина отвечает своим ботом в разрешённые чаты, а не
# аккаунтом Ивана: не сюда.
_PLUGIN_RE = re.compile(r"plugin_[\w.-]+_(?:gmail|slack)", re.IGNORECASE)
_VERB_RE = re.compile(r"(?:[a-z]+_)?(?:send|reply|forward|edit|post|schedule)[a-z_]*")
# Отправка, содержимое которой карточка показать не может: пересылка
# (переслать можно что угодно из чужого чата) и письмо по номеру черновика
# (сам черновик пишется без карточки). Такое бот отклоняет сразу.
_FORWARD_RE = re.compile(r"(?:[a-z]+_)?forward[a-z_]*")
# Отправка без глагола в имени: create_poll шлёт опрос в чат сообщением
# (SendMediaRequest в tools/messages.py сервера, финальное ревью 03.11.2026).
# vote_poll шлёт голос в опрос (SendVoteRequest), в открытом опросе люди видят
# выбор Ивана (решение Ивана 08.11.2026, ревью Codex). Остальные инструменты
# telegram-mcp, которые шлют сообщение, глагол имеют.
_SEND_NAMES = frozenset({"create_poll", "vote_poll"})
# Первое слово имени, с которым инструмент только читает или ждёт:
# get_send_as, get_scheduled_messages, wait_for_new_message. Без этого
# приставка _VERB_RE и слова serious.py ловили их как отправку: карточка, а в
# фоне отказ (08.11.2026). Тот же набор зовёт serious.py.
READ_WORDS = frozenset({"get", "list", "search", "read", "fetch", "find", "view", "wait"})
# Слово отправки в имени, но текста людям нет: «печатает…» (SetTyping), отмена
# своего отложенного сообщения (удаление, по модели 04.11.2026 без карточки),
# черновик Slack (сохраняется в «Drafts & Sent», отправляет сам Иван: запись
# памяти и каталог MintMCP, схема коннектора 08.11.2026 не проверена: Slack не
# подключён). Telegram сверен с telegram-mcp (tools/messages.py) 08.11.2026;
# serious.py их тоже пропускает. set_default_send_as сюда не входит: он
# меняет, от чьего имени уйдут следующие сообщения, карточку даёт serious.py.
NOT_SENDS = frozenset({"send_typing", "delete_scheduled_message", "slack_send_message_draft"})


def is_send(tool_name: str) -> bool:
    parts = tool_name.split("__", 2)
    return (len(parts) == 3 and parts[0] == "mcp"
            and (parts[1] in _SERVERS or bool(_PLUGIN_RE.fullmatch(parts[1])))
            and parts[2] not in NOT_SENDS and parts[2].split("_", 1)[0] not in READ_WORDS
            and (bool(_VERB_RE.fullmatch(parts[2])) or parts[2] in _SEND_NAMES))


def _refusal(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Причина отказа отправке, которую нельзя показать на карточке целиком."""
    short_tool = tool_name.split("__", 2)[-1]
    if _FORWARD_RE.fullmatch(short_tool):
        return _("пересылку агент не делает: отправь текст сам целиком")
    if tool_input.get("draftId"):
        return _("письмо по черновику не отправляется: передай адресатов, тему и текст целиком")
    return None


# Письмо модулем mailsend (Bash, вложения файлом): та же карточка отправки,
# что у Gmail MCP (решение Ивана 13.10.2026). Карточку строит бот из файлов в
# момент запроса: снимок письма кладётся во вход карточки под этим ключом, а
# по ✅ бот пишет билет с хешем снимка. mailsend отправляет только письмо с
# тем же хешем, поэтому правка текста или вложения после карточки и запуск
# мимо карточки дают отказ.
MAILSEND_CARD_KEY = "_mailsend"


# Признак исполнения mailsend, а не упоминания: запуск модулем, файл модуля
# скриптом интерпретатора, импорт в коде (python -c, heredoc). Ищется в
# executed_text, где слово с `'` shlex пишет как `'"'"'`.
_MAILSEND_RUN_RE = re.compile(
    r"-m\s*['\"]?claude_tg_bot\.mailsend\b"
    r"|\bpython[\d.]*['\"]?\s+(?:-\S+\s+)*(?:-m\s*['\"]?mailsend\b|['\"]?(?:\S*/)?mailsend\.py\b)"
    r"|\bimport\s+claude_tg_bot\.mailsend\b"
    r"|\bfrom\s+claude_tg_bot(?:\.mailsend\s+import\b|\s+import\b[^;\n]*\bmailsend\b)"
    r"|\b(?:import_module|run_module)\(\s*(?:'\"'\"'|['\"])claude_tg_bot\.mailsend\b"
)
# Команды, которые текст аргументов только читают или пишут: упоминание
# mailsend в них письма не отправит.
_TEXT_TOOLS = frozenset({"git", "grep", "egrep", "fgrep", "rg", "echo", "printf", "cat",
                         "sed", "head", "tail", "less", "wc", "diff", "ls"})
# Обёртки перед настоящей командой: `timeout 60 git ...` это git.
_CMD_WRAPPERS = frozenset({"env", "timeout", "nice", "nohup", "sudo", "exec", "command",
                           "time", "stdbuf", "setsid", "strace", "ltrace"})
_MAILSEND_MODULE = "claude_tg_bot.mailsend"
_PYTHON_RE = re.compile(r"(?:\S*/)?python3?(?:\.\d+)?")
_SHELL_OPS = frozenset("();<>|&")
_MAILSEND_FORM = "[cd DIR &&] .venv/bin/python -m claude_tg_bot.mailsend --to ... --body-file ..."


def _mailsend_argv(cmd: str, cwd: str | None) -> tuple[list[str], Path | None] | None:
    """Аргументы mailsend из простой команды `[cd DIR && или ;] PY -m
    claude_tg_bot.mailsend ...` и каталог для относительных путей. Подстановки,
    bash -c, heredoc, переменные и хвост не из читателей (_reader_tail) не
    разбираются: None."""
    # Перенос строки через `\` bash убирает до разбора; в одинарных кавычках
    # он остался бы, но такой путь или тема дадут другой хеш, то есть отказ.
    cmd = _join_lines(cmd)
    # В одинарных кавычках `$`, обратная кавычка и `\` обычные знаки: тема
    # `'Оклад $5000'` это простая форма (ревью Codex 11.11.2026).
    quoted = _single_quoted(cmd)
    if any(c in "\n\r" or (c in "$`\\" and not quoted[i]) for i, c in enumerate(cmd)):
        return None
    try:
        # Комментарий как в bash: `#` в начале слова вне кавычек, до конца
        # (перевода строки здесь нет). `#` внутри слова (`issue#12`) shlex
        # считает комментарием, bash нет: хвост за ним пропал бы из разбора
        # (ревью Opus 09.11.2026), а без комментариев вовсе `# don't` ломал
        # разбор и `# --help` делал отправку справкой (ревью Codex).
        lex = shlex.shlex(_strip_comments(cmd), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return None
    base = Path(cwd) if cwd and Path(cwd).is_absolute() else None
    if tokens[:1] == ["cd"]:
        # `;` наравне с `&&` (карточка на справку 10.11.2026): при неудачном
        # `cd` mailsend прочтёт относительные пути из другого каталога, хеш не
        # сойдётся с билетом, и письмо не уйдёт.
        if len(tokens) < 3 or tokens[2] not in ("&&", ";"):
            return None
        try:
            target = Path(tokens[1]).expanduser()
        except (RuntimeError, ValueError):  # `~нет_такого_пользователя`, нулевой байт
            return None
        if not target.is_absolute():
            if base is None:
                return None
            target = base / target
        base = target
        tokens = tokens[3:]
    if (len(tokens) < 3 or not _PYTHON_RE.fullmatch(tokens[0])
            or tokens[1:3] != ["-m", _MAILSEND_MODULE]):
        return None
    argv = tokens[3:]
    cut = next((i for i, t in enumerate(argv) if t and set(t) <= _SHELL_OPS), len(argv))
    argv, rest = argv[:cut], argv[cut:]
    # `2>&1` сразу за аргументами: вывод ошибок в тот же поток.
    if rest[:2] == [">&", "1"] and argv[-1:] == ["2"]:
        argv, rest = argv[:-1], rest[2:]
    if not _reader_tail(rest):
        return None
    return argv, base


# Хвост после mailsend, который письма не меняет и ничего не пишет: читатели
# вывода и файлов через `|`, `;`, `&&`, `||` (без перенаправлений). Справка и
# сухой прогон с `| head` шли карточкой «mention», настоящая отправка с
# `| tail` тоже, а по ней билет не пишется, и письмо не уходило: агент
# повторял команду второй карточкой (карточки 23.10, 27.10 и 09.11.2026,
# решение Ивана 09.11.2026). Письмо всё равно сверяется с билетом по хешу.
# Без `rg`: `--pre` запускает программу на каждом файле (ревью Opus).
_TAIL_OPS = frozenset({"|", ";", "&&", "||"})
_TAIL_READERS = frozenset({"head", "tail", "grep", "egrep", "fgrep", "wc", "cat",
                           "ls", "echo", "printf"})


def _reader_tail(rest: list[str]) -> bool:
    """Хвост из кусков `ОП читатель аргументы...`; пустой тоже годится."""
    i = 0
    while i < len(rest):
        if rest[i] not in _TAIL_OPS or i + 1 >= len(rest) or rest[i + 1] not in _TAIL_READERS:
            return False
        i += 2
        while i < len(rest) and not (rest[i] and set(rest[i]) <= _SHELL_OPS):
            i += 1
    return True


# Обёртки, которые запускают следующую команду: короткие буквы ключей со
# значением (в том числе последней буквой склейки: `sudo -iu root`, `xargs
# -0n 1`) и длинные ключи со значением следующим словом. Ключ вида `-n5`,
# `--signal=TERM` значение несёт сам.
WRAPPER_VALUE: dict[str, tuple[str, frozenset[str]]] = {
    "sudo": ("ugCDhprtUT", frozenset({"--user", "--group", "--chdir", "--host", "--prompt", "--role", "--type",
                                      "--other-user", "--close-from", "--command-timeout"})),
    "env": ("uCS", frozenset({"--unset", "--chdir", "--split-string"})),
    "timeout": ("sk", frozenset({"--signal", "--kill-after"})),
    "nice": ("n", frozenset({"--adjustment"})),
    "ionice": ("cnpPu", frozenset({"--class", "--classdata", "--pid", "--pgid", "--uid"})),
    "stdbuf": ("ioe", frozenset({"--input", "--output", "--error"})),
    "xargs": ("InPdLsEa", frozenset({"--max-args", "--max-procs", "--delimiter", "--max-lines", "--arg-file",
                                       "--replace", "--eof", "--max-chars", "--process-slot-var"})),
    "nohup": ("", frozenset()), "setsid": ("", frozenset()), "time": ("", frozenset()),
    "command": ("", frozenset()), "exec": ("a", frozenset()), "builtin": ("", frozenset()),
    "rtk": ("", frozenset()), "caffeinate": ("tw", frozenset()),
    "watch": ("nq", frozenset({"--interval", "--equexit"})),
    "doas": ("uC", frozenset()), "chronic": ("", frozenset()), "busybox": ("", frozenset()),
    "unshare": ("", frozenset({"--setgroups", "--map-user", "--map-group", "--wd", "--root"})),
    "nsenter": ("tSGwr", frozenset({"--target", "--setuid", "--setgid", "--wd", "--root"})),
    "strace": ("eopsEuIabXP", frozenset({"--output", "--signal", "--user", "--env"})),
    "ltrace": ("eopsEuabn", frozenset({"--output", "--user"})),
    "taskset": ("", frozenset()), "chrt": ("", frozenset()), "flock": ("wE", frozenset({"--timeout",
                                                                                           "--conflict-exit-code"})),
}


def skip_options(name: str, words: list[str],
                 spec: tuple[str, frozenset[str]] | None = None, start: int = 0) -> int:
    """Номер первого слова после ключей обёртки `name`, стоящей в words[start]
    (spec: свои буквы и длинные ключи со значением, для программы не из WRAPPER_VALUE: ssh)."""
    letters, long_values = spec or WRAPPER_VALUE[name]
    i = start + 1
    while i < len(words):
        w = words[i]
        if name == "env" and "=" in w and not w.startswith("-"):
            i += 1
        elif w.startswith("--"):
            i += 2 if w in long_values else 1
        elif w.startswith("-") and len(w) > 1:
            # Короткие склеенные: значение несёт хвост (`-n5`) или следующее
            # слово, если буква со значением последняя (`-iu root`).
            body = w[1:]
            k = next((n for n, ch in enumerate(body) if ch in letters), None)
            i += 2 if k is not None and k == len(body) - 1 else 1
        else:
            break
    return i


def _command_head(tokens: list[str]) -> str:
    """Имя команды простого куска: без присваиваний VAR=..., обёрток и их ключей
    (ключи со значением по WRAPPER_VALUE: `sudo -Hu git rm` это rm, а не git)."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        name = Path(t).name
        if re.fullmatch(r"\w+=.*", t, re.S) or t.startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", t):
            i += 1
        elif name in _CMD_WRAPPERS:
            end = skip_options(name, tokens, start=i) if name in WRAPPER_VALUE else i + 1
            if name == "env" and any(w.startswith(("-S", "--split-string")) for w in tokens[i + 1:end]):
                return name  # `env -S 'команда' …`: строка исполняется, кусок не текстовый
            i = end
        else:
            return name
    return ""


# Перед `#` комментария bash: начало строки, пробел, табуляция, перевод
# строки или оператор. Неразрывный пробел и `\v` слово не делят: `s\xa0#x; rm`
# в bash исполняет `rm` (перепроверка Opus 09.11.2026).
_COMMENT_AFTER = frozenset(" \t\n;&|()<>")


def _escaped(t: str, pos: int) -> bool:
    """Перед позицией нечётное число `\\`: знак на ней экранирован
    (`\\$(`, склейка `\\` с переводом строки); `\\\\` это одна `\\`."""
    n = 0
    while pos - n - 1 >= 0 and t[pos - n - 1] == "\\":
        n += 1
    return n % 2 == 1


# Знаков на поиск пар скобок в одном разборе: длинный ввод с незакрытыми
# `((` не делает разбор квадратичным (ревью Opus и Codex 10.11.2026).
ARITH_BUDGET = 2_000_000


def _close(t: str, j: int, opener: str, closer: str, budget: list[int] | None = None,
           slot: int = 0) -> int | None:
    """Пара скобки `t[j]` с учётом кавычек, как в bash: `'…'`, `"…"` (внутри
    `"…"` `$(` открывает свой уровень), `\\`. None: пары нет или кончился
    budget[slot] (общий счёт знаков на разбор)."""
    stack: list[str] = []
    k, n = j, len(t)
    while k < n:
        if budget is not None:
            budget[slot] -= 1
            if budget[slot] <= 0:
                return None
        c = t[k]
        top = stack[-1] if stack else ""
        if c == "\\":
            k += 2
            continue
        if top == '"':
            if c == '"':
                stack.pop()
            elif t.startswith("$(", k):
                stack.append("(")
                k += 2
                continue
        elif c == "'":
            e = t.find("'", k + 1)
            if e < 0:
                return None
            k = e + 1
            continue
        elif c == '"':
            stack.append('"')
        elif c in (opener, "("):
            stack.append(c)
        elif (c == closer and top == opener) or (c == ")" and top == "("):
            stack.pop()
            if not stack:
                return k
        k += 1
    return None


def heredoc_word(t: str, i: int) -> tuple[str, bool, int] | None:
    """Ограничитель heredoc с позиции после `<<` или `<<-`: (слово, в кавычках
    ли оно, конец). Пробелы перед словом пропускаются, слово идёт до пробела
    или знака оболочки; кавычки и `\\` в слове делают тело буквальным,
    `\\` с переводом строки склеивает слово, как в bash. None: слова нет
    (`<< ;`, ошибка bash) или кавычка не закрыта: тогда это не heredoc."""
    n = len(t)
    while i < n and t[i] in " \t":
        i += 1
    word: list[str] = []
    quoted = has = False
    while i < n and t[i] not in " \t\n;|&<>()`":
        c = t[i]
        if c == "$" and t.startswith(("$'", '$"'), i):
            # `<<$'EOF'` и `<<$"EOF"`: ограничитель EOF (ревью Codex 10.11.2026).
            i += 1
            continue
        if c == "'" and t[i - 1:i] == "$" and not _escaped(t, i - 1):
            e = i + 1
            while e < n and t[e] != "'":
                e += 2 if t[e] == "\\" else 1
            if e >= n:
                return None
            word.append(re.sub(r"\\(.)", r"\1", t[i + 1:e]))
            quoted = has = True
            i = e + 1
        elif c == "'":
            e = t.find(c, i + 1)
            if e < 0:
                return None
            word.append(t[i + 1:e])
            quoted = has = True
            i = e + 1
        elif c == '"':
            # В `"…"` `\\` снимается только перед `"`, `\\`, `$`, обратной кавычкой.
            e = i + 1
            while e < n and t[e] != '"':
                e += 2 if t[e] == "\\" else 1
            if e >= n:
                return None
            word.append(re.sub(r'\\([\\"$`])', r"\1", t[i + 1:e].replace("\\\n", "")))
            quoted = has = True
            i = e + 1
        elif c == "\\" and t.startswith("\\\n", i):
            i += 2
        elif c == "\\" and i + 1 < n:
            word.append(t[i + 1])
            quoted = has = True
            i += 2
        else:
            word.append(c)
            has = True
            i += 1
    return ("".join(word), quoted, i) if has else None


def heredoc_body(t: str, i: int, delim: str, strip: bool, quoted: bool,
                 nested: bool = False, stop: int | None = None) -> tuple[str, int]:
    """Тело heredoc с начала строки `i`: (тело, позиция за строкой
    ограничителя). Конец тела, как в bash 5: строка ровно из ограничителя (у
    `<<-` без табуляций в начале); без кавычек у ограничителя `\\` с переводом
    строки склеивает строки (нечётное число `\\`), сверяется и отдаётся
    склеенная. Нет такой строки: тело до конца текста. nested (внутри `$(...)`
    или `<(...)`): тело кончается и на строке, которая начинается с
    ограничителя и где дальше есть `)` (`E)`, `E x)`, `EE)` закрывают тело,
    `Ex foo` нет; у `<<''` любая строка с `)`; bash 5.3), позиция тогда сразу за ограничителем, хвост
    строки исполняется. stop: конец текста обратных кавычек, тело до него."""
    n = len(t) if stop is None else stop
    lines: list[str] = []
    while i < n:
        start, logical = i, []
        while True:
            e = t.find("\n", i, n)
            line, i = (t[i:n], n) if e < 0 else (t[i:e], e + 1)
            if nested and not logical:
                head = line.lstrip("\t") if strip else line
                if head.startswith(delim) and ")" in head[len(delim):]:
                    return "\n".join(lines), start + len(line) - len(head) + len(delim)
            if quoted or not _escaped(line, len(line)) or e < 0:
                logical.append(line)
                break
            logical.append(line[:-1])
        line = "".join(logical)
        if (line.lstrip("\t") if strip else line) == delim:
            return "\n".join(lines), i
        lines.append(line)
    return "\n".join(lines), n


def arith_end(t: str, i: int, budget: list[int] | None = None, slot: int = 0) -> int | None:
    """Конец арифметики `((...))` с первой скобки `i`: позиция за `))`. Как в
    bash, арифметика только если пара второй скобки (с учётом кавычек) стоит
    прямо перед `)`: `((cmd) )`, `((cmd); (cmd))`, `((echo "x))") )` это
    подоболочки, `$((1 << "2"))` арифметика (ревью Opus и Codex 10.11.2026).
    None: не арифметика."""
    if not t.startswith("((", i):
        return None
    m = _close(t, i + 1, "(", ")", budget, slot)
    return m + 2 if m is not None and t.startswith(")", m + 1) else None


def bracket_end(t: str, i: int, budget: list[int] | None = None, slot: int = 0) -> int | None:
    """Конец старой арифметики `$[...]` со скобки `i`: позиция за `]`."""
    m = _close(t, i, "[", "]", budget, slot)
    return None if m is None else m + 1


_CASE_WORD = re.compile(r"(case|esac|in)(?=[ \t\n;&|()]|$)")
_COMMAND_AFTER_WORDS = frozenset({"then", "do", "else", "{", "!", "time"})


def _command_position(t: str, pos: int) -> bool:
    """Слово на `pos` стоит на месте команды: после начала, перевода строки,
    `;&|()` или слова оболочки (`then`, `do`)."""
    j = pos - 1
    while j >= 0 and t[j] in " \t":
        j -= 1
    if j < 0 or t[j] in "\n;&|()":
        return True
    k = j
    while k >= 0 and t[k] not in " \t\n;&|()":
        k -= 1
    return t[k + 1:j + 1] in _COMMAND_AFTER_WORDS


@dataclass(frozen=True)
class _Heredoc:
    start: int       # `<<`
    word_end: int    # за ограничителем
    line_end: int    # перевод строки, за которым тело (или где тело оборвалось)
    body_start: int
    end: int         # за строкой ограничителя
    body: str
    quoted: bool


def unescape_backtick(text: str) -> str:
    """Текст обратных кавычек, как его исполняет bash: `\\``, `\\\\`, `\\$` без
    `\\`: вложенная `` `echo \\`cmd\\`` `` исполняет cmd (ревью Opus 10.11.2026)."""
    return re.sub(r"\\([`\\$])", r"\1", text)


def _backtick_end(t: str, i: int) -> int:
    """Закрывающая обратная кавычка с позиции `i` (`\\`` её не закрывает)."""
    while i < len(t):
        if t[i] == "\\":
            i += 2
        elif t[i] == "`":
            return i
        else:
            i += 1
    return len(t)


def _shell_scan(cmd: str) -> tuple[list[bool], list[tuple[int, int]], list[_Heredoc],
                                   list[tuple[int, int]]]:
    """Для каждого знака: внутри одинарных кавычек оболочки, где `$(...)` и
    обратные кавычки не исполняются (в двойных `'` обычный знак); участки
    комментариев bash (`#` в начале слова вне кавычек, до перевода строки);
    heredoc. Комментарий кавычек не открывает: апостроф в `# don't` не прячет
    подстановки ниже. Как в bash (ревью Opus и Codex 09.11.2026):
    экранированный знак (`a\\ #x`) и закрытая подстановка (`$(x)#tag`,
    `` `x`#tag ``) это часть слова; в `$'...'` кавычку закрывает только
    неэкранированная `'`; комментарий внутри обратных кавычек кончается на
    закрывающей, хвост за ней исполняется. `$(...)` и обратные кавычки
    внутри `"..."` открывают уровень без кавычек, как в bash: кавычки и
    heredoc внутри разбираются заново (ревью Opus 10.11.2026; раньше это был
    принятый риск guard3-decisions.md).

    Heredoc как в bash 5 (ревью Свода 09.11.2026): метка `<<` только вне
    кавычек, комментария, `${...}` и арифметики (`$((`, `((`, `$[`), не
    `<<<`; тело читается за переводом строки команды (heredoc_body), его знаки
    не кавычки и не комментарий; тело метки внутри обратных кавычек не
    выходит за закрывающую. Тело внутри `"$(cat <<'E' ...)"` вырезается тоже:
    непарные `"` и `'` в нём иначе сбивали кавычки, и хвост команды
    пропадал (ревью Opus 10.11.2026)."""
    n = len(cmd)
    out, single, double, esc, comment, prev = [], False, False, False, False, "\n"
    ansi = bq = comment_bq = False
    esc_prev = "\n"
    bq_double = False
    # Открытые скобки: (вид, глубина `${` снаружи); вид "" подоболочка, "w"
    # после закрывающей слово идёт дальше (`$(`, `<(`, `>(`, `arr=(`), "d" то
    # же и снова двойные кавычки. `${` считается на каждом уровне свой:
    # `${x:-$(cat <<E …)}` настоящий heredoc (ревью Codex 10.11.2026).
    parens: list[tuple[str, int, int]] = []
    # Тексты `$(...)` (начало, конец): границы по тем же кавычкам, `case` и
    # вложенности, что и heredoc (ревью Codex 10.11.2026: шаблон `x)` закрывал
    # подстановку в простом счёте скобок).
    subs: list[tuple[int, int]] = []
    budget = [ARITH_BUDGET]
    # `case`: глубина скобок у каждого открытого, ждём `in`, ждём `)` шаблона.
    cases: list[int] = []
    case_in = pattern = False
    # Метки из закрытой `$(...)`: bash читает их тела первыми, в порядке закрытия.
    closed: list[tuple[int, int, str, bool, bool, bool, bool, int]] = []
    brace = 0
    comments: list[tuple[int, int]] = []
    docs: list[_Heredoc] = []
    # Метка ждёт тела: (начало, конец слова, ограничитель, `<<-`, в кавычках,
    # в `...`, внутри `$(...)`: правило `E)` действует и за закрытой скобкой,
    # глубина скобок).
    pending: list[tuple[int, int, str, bool, bool, bool, bool, int]] = []

    def arith_subs(a: int, b: int) -> None:
        # `$(( $(cmd) ))`: подстановку внутри арифметики bash исполняет.
        if "$(" in cmd[a:b]:
            subs.extend((s + a, e + a) for s, e in _shell_scan(cmd[a:b])[3])

    def flush(at: int, keep_bq: bool = False) -> None:
        # Метки без тела (обрыв текста или закрытие обратных кавычек): тело пустое.
        nonlocal pending, closed
        rest = []
        for mark in closed + pending:
            if keep_bq and not mark[5]:
                rest.append(mark)
            else:
                docs.append(_Heredoc(mark[0], mark[1], at, at, at, "", mark[4]))
        pending, closed = rest, []

    def bodies(i: int) -> int:
        nonlocal pending, closed
        stop = _backtick_end(cmd, i) if bq else None
        pending, closed = closed + pending, []
        for k, (start, word_end, delim, strip, quoted, _in_bq, in_sub, _lvl) in enumerate(pending):
            body, end = heredoc_body(cmd, i, delim, strip, quoted,
                                     nested=in_sub or any(k for k, _b, _s in parens), stop=stop)
            docs.append(_Heredoc(start, word_end, i - 1, i, end, body, quoted))
            out.extend([False] * (end - i))
            i = end
            if end == n or end == stop or (end and cmd[end - 1] != "\n"):
                # Тело оборвалось посреди строки или на конце текста: у
                # остальных меток тела нет, хвост исполняется.
                pending = pending[k + 1:]
                flush(end)
                break
        pending = []
        return i

    i = 0
    while i < n:
        c = cmd[i]
        out.append((single and c != "'") or ansi)
        i += 1
        if esc:
            esc = False
            # `\\` с переводом строки bash убирает целиком: `cat \\⏎# c` это
            # `cat # c`, комментарий (сверка с bash 09.11.2026).
            prev = esc_prev if c == "\n" else "a"
            continue
        if ansi:
            if c == "\\":
                esc = True
            ansi = c != "'"
        elif single:
            single = c != "'"
        elif comment:
            if c == "\n" or (c == "`" and comment_bq):
                comment = False
                comments[-1] = (comments[-1][0], i - 1)
                if c == "`":
                    bq, double = False, bq_double
                    flush(i - 1, keep_bq=True)
                    prev = "a"
                    continue
        elif c == "\\":
            esc, esc_prev = True, prev
            continue
        elif c == '"':
            double = not double
        elif c == "`":
            bq = not bq
            if bq:
                bq_double, double = double, False
            else:
                double = bq_double
                flush(i - 1, keep_bq=True)
            # Внутри обратных кавычек новая команда; после закрывающей слово идёт дальше.
            prev = "\n" if bq else "a"
            continue
        elif double:
            if c == "(" and prev == "$":
                if (end := arith_end(cmd, i - 1, budget)) is not None:
                    arith_subs(i + 1, end - 2)
                    out.extend([False] * (end - i))
                    i, prev = end, "a"
                    continue
                parens.append(("d", brace, i))
                double, brace = False, 0
        elif c == "'":
            if prev == "$":
                ansi = True
            else:
                single = True
        elif c == "(" and cmd.startswith("(", i) and prev not in "<>" \
                and (end := arith_end(cmd, i - 1, budget)) is not None:
            # `$((1<<2))`, `((x<<=1))`, `for((`: сдвиг, а не heredoc. После
            # команды `((…))` `#` начинает комментарий, после `$((…))` нет.
            arith_subs(i + 1, end - 2)
            out.extend([False] * (end - i))
            i, prev = end, ("a" if prev == "$" else ")")
            continue
        elif c == "[" and prev == "$" and (end := bracket_end(cmd, i - 1, budget)) is not None:
            arith_subs(i, end - 1)
            out.extend([False] * (end - i))
            i, prev = end, "a"
            continue
        elif c == "{" and prev == "$":
            brace += 1
        elif c == "}" and brace:
            brace -= 1
        elif c == "<" and cmd.startswith("<<", i - 1) and not brace:
            if cmd.startswith("<", i + 1):
                # `<<<` here-string: слово, а не тело.
                out.extend([False, False])
                i, prev = i + 2, "<"
                continue
            strip = cmd.startswith("-", i + 1)
            word = heredoc_word(cmd, i + 1 + strip)
            if word is not None:
                delim, quoted, end = word
                pending.append((i - 1, end, delim, strip, quoted, bq, any(k for k, _b, _s in parens),
                                len(parens)))
                out.extend([False] * (end - i))
                i, prev = end, "a"
                continue
        elif c == ";" and cmd.startswith((";", "&"), i) and cases and cases[-1] == len(parens):
            pattern = True  # `;;`, `;&`, `;;&`: дальше шаблон case
        elif c in "cie" and prev in _COMMENT_AFTER and (kw := _CASE_WORD.match(cmd, i - 1)):
            word = kw.group(1)
            if word == "case" and _command_position(cmd, i - 1):
                cases.append(len(parens))
                case_in = True
            elif word == "in" and case_in:
                case_in, pattern = False, True
            elif word == "esac" and cases and cases[-1] == len(parens) and _command_position(cmd, i - 1):
                cases.pop()
                pattern = False
        elif c == "(" and pattern and cases[-1] == len(parens) and prev in _COMMENT_AFTER:
            pass  # `(x)` шаблона case: скобка шаблона, не подоболочка
        elif c == "(":
            # `$(`, `<(`, `>(`, `arr=(`: после закрывающей слово идёт дальше.
            parens.append(("w" if prev in "$<>=" else "", brace, i if prev == "$" else -1))
            brace = 0
        elif c == ")" and pattern and cases and cases[-1] == len(parens):
            pattern = False  # `x)` шаблона case не закрывает `$(...)` (ревью Codex 10.11.2026)
        elif c == ")" and parens:
            kind, brace, sub_start = parens.pop()
            if sub_start >= 0:
                subs.append((sub_start, i - 1))
            if kind:
                # Метки изнутри `$(...)` bash читает раньше внешних (ревью Opus 10.11.2026).
                closed += [p for p in pending if p[7] > len(parens)]
                pending = [p for p in pending if p[7] <= len(parens)]
                double = kind == "d"
                prev = "a"
                continue
        elif c == "#" and prev in _COMMENT_AFTER:
            comment, comment_bq = True, bq
            comments.append((i - 1, n))
        prev = c
        if c == "\n" and (pending or closed) and not (single or double or ansi or brace):
            i = bodies(i)
    flush(n)
    subs += [(start, n) for _k, _b, start in parens if start >= 0]
    return out, comments, docs, sorted(subs)


def _join_lines(cmd: str) -> str:
    """`\\` в конце строки склеивает строки, кроме как в комментарии: там
    это текст комментария, следующая строка исполняется (ревью Codex 09.11.2026)."""
    if "\\\n" not in cmd:
        return cmd
    comments = _shell_scan(cmd)[1]
    out, i = [], 0
    while i < len(cmd):
        # Нечётное число `\\` перед переводом строки: склейка; `\\\\` это
        # экранированная `\\`, строка не склеивается (ревью Opus 09.11.2026).
        if (cmd.startswith("\\\n", i) and not _escaped(cmd, i)
                and not any(s <= i < e for s, e in comments)):
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return "".join(out)


def _strip_comments(cmd: str) -> str:
    """Команда без комментариев bash (_shell_scan), переводы строк на месте.
    Разбирать дальше shlex с `commenters = ""`: у shlex `#` внутри слова
    (`issue#12`) тоже комментарий, и хвост строки пропадал из разбора."""
    out, pos = [], 0
    for start, end in _shell_scan(cmd)[1]:
        out.append(cmd[pos:start])
        pos = end
    out.append(cmd[pos:])
    return "".join(out)


def _single_quoted(cmd: str) -> list[bool]:
    """_shell_scan: только признаки одинарных кавычек. Текст комментария
    здесь считается исполняемым: лишняя карточка дешевле."""
    return _shell_scan(cmd)[0]


def _substitutions(cmd: str, heredoc: bool = False) -> list[str]:
    """Тексты подстановок $(...) и `...` вне одинарных кавычек (там они
    текст, финальное ревью 03.11.2026). Скобки внутри считаются без учёта
    кавычек, незакрытая подстановка идёт до конца строки."""
    # В теле heredoc апостроф обычный знак: `don't $(...)` исполняется
    # (ревью Opus 09.11.2026).
    scan = None if heredoc else _shell_scan(cmd)
    quoted = [False] * len(cmd) if scan is None else scan[0]

    out, i = [], cmd.find("`")
    while i >= 0:
        # `\\`` текст, `\\\\`` подстановка (ревью Opus 10.11.2026).
        if quoted[i] or _escaped(cmd, i):
            i = cmd.find("`", i + 1)
            continue
        end = _backtick_end(cmd, i + 1)
        out.append(unescape_backtick(cmd[i + 1:end]))
        i = cmd.find("`", end + 1)
    if scan is not None:
        # Границы `$(...)` от _shell_scan: кавычки, `case`, `\$(` текстом.
        return out + [cmd[start:end] for start, end in scan[3]]
    for m in re.finditer(r"\$\(", cmd):
        # `\$(...)` bash печатает как текст (ревью Codex 09.11.2026).
        if _escaped(cmd, m.start()):
            continue
        depth, j = 1, m.end()
        while j < len(cmd) and depth:
            depth += {"(": 1, ")": -1}.get(cmd[j], 0)
            j += 1
        out.append(cmd[m.end():j - 1 if depth == 0 else j])
    return out


def _mailsend_runs(cmd: str) -> bool:
    """Команда исполняет mailsend (_MAILSEND_RUN_RE), в том числе через bash -c,
    $(...), heredoc и обёртки: признак в исполняемой части (executed_text).
    Упоминание в аргументах _TEXT_TOOLS (сообщение коммита, в том числе за
    `rtk`, шаблон grep, текст echo) не в счёт. Что не разобралось, считается
    исполнением: лишняя карточка дешевле."""
    return bool(_MAILSEND_RUN_RE.search(cmd.replace("\\\n", ""))) and bool(
        _MAILSEND_RUN_RE.search(executed_text(cmd)))


def executed_text(cmd: str, depth: int = 0) -> str:
    """Части команды, которые исполняются: без аргументов _TEXT_TOOLS (шаблон
    grep, сообщение коммита, текст echo), с телами heredoc для интерпретатора
    и подстановками. Куски склеены переводом строки, слова в кавычках shlex.
    Что не разобралось, остаётся целиком: лишняя карточка дешевле."""
    # Heredoc ищется по исходному тексту: склейка `\\` с переводом строки
    # в теле с кавычками у ограничителя не делается (_shell_scan сам знает
    # переносы), команда склеивается уже без тел.
    bodies: list[tuple[bool, bool, str]] = []
    spans: list[tuple[int, int]] = []
    _flags, comments, docs, _subs = _shell_scan(cmd)
    # Начало команды перед меткой (после перевода строки без `\\`, `;|&(` или
    # обратной кавычки) и ближайший `|` за ней: один проход на весь текст, а
    # не на каждую метку (ревью Opus 10.11.2026, много меток в строке).
    bound, pipe = [-1] * (len(cmd) + 1), [len(cmd)] * (len(cmd) + 1)
    if docs:
        # `\\` в комментарии текст: перевод строки за ним граница команды (ревью Opus 10.11.2026).
        comment_ends = {end for _start, end in comments}
        for j, ch in enumerate(cmd):
            bound[j + 1] = j if ch in ";|&(`" or (ch == "\n" and (
                not _escaped(cmd, j) or j in comment_ends)) else bound[j]
        for j in range(len(cmd) - 1, -1, -1):
            pipe[j] = j if cmd[j] == "|" else pipe[j + 1]
    for doc in docs:
        opener = _join_lines(cmd[bound[doc.start] + 1:doc.start])
        # Тело исполняется и тогда, когда команда текстовая, но вывод идёт
        # дальше по конвейеру (`cat <<'EOF' | bash`, ревью Codex 09.11.2026).
        runs = (_text_head(opener.split(None, 64)[:64]) not in _TEXT_TOOLS
                or pipe[doc.word_end] < doc.line_end)
        bodies.append((runs, not doc.quoted, doc.body))
        spans += [(doc.start, doc.word_end), (doc.body_start, doc.end)]
    # Метки и тела вырезаются; у двух меток в строке участки перемежаются.
    pieces, pos = [], 0
    for start, end in sorted(spans):
        pieces.append(cmd[pos:start])
        pos = max(pos, end)
    pieces.append(cmd[pos:])
    shell = _join_lines("".join(pieces))
    # Исполняемое тело (bash, python) склеивает `\\` с переводом строки и
    # при кавычках у ограничителя: `-m \\` и `claude_tg_bot.mailsend` на
    # следующей строке это один вызов (ревью Opus 10.11.2026).
    out = [_join_lines(body) for runs, _open, body in bodies if runs]
    # Тело без кавычек у разделителя оболочка раскрывает: `$(...)` в нём
    # исполняется и у `cat <<EOF` (ревью Codex 09.11.2026).
    out += [inner if depth >= 3 else executed_text(inner, depth + 1)
            for runs, unquoted, body in bodies if unquoted and not runs
            for inner in _substitutions(body, heredoc=True)]
    out += [inner if depth >= 3 else executed_text(inner, depth + 1) for inner in _substitutions(shell)]
    try:
        # Комментарии bash вырезаются заранее (_strip_comments): у shlex `#`
        # внутри слова (`cat issue#12; curl ...`) съедал хвост строки, и
        # отправка за ним шла без карточки (решение Ивана 09.11.2026).
        # Подстановки выше ищутся по тексту с комментариями: лишняя карточка
        # дешевле. `;` после перевода строки: куски строк не склеиваются.
        lex = shlex.shlex(_strip_comments(shell).replace("\n", "\n;"), posix=True,
                          punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        # Склеенный текст: `-m \\` и `claude_tg_bot.mailsend` на следующей
        # строке это один вызов (ревью Opus 10.11.2026).
        return _join_lines(cmd)
    part: list[str] = []
    for t in tokens + [";"]:
        if t and set(t) <= _SHELL_OPS:
            if part and _text_head(part) not in _TEXT_TOOLS:
                out.append(shlex.join(part))
            part = []
        else:
            # `;` после перевода строки вставлен для разбора и попал бы в слово в кавычках
            # (тело heredoc в строке для ssh, SQL): из слова её убрать (перепроверка 13.11.2026).
            part.append(t.replace("\n;", "\n"))
    return "\n".join(out)


def _text_head(tokens: list[str]) -> str:
    """_command_head, и `rtk git …` (сжатие вывода хуком) это git."""
    if tokens and Path(tokens[0]).name == "rtk":
        tokens = tokens[2:] if tokens[1:2] == ["proxy"] else tokens[1:]
    return _command_head(tokens)


def mailsend_check(cmd: str, cwd: str | None) -> tuple[str, Any]:
    """Что делать с Bash-командой про mailsend: ("", None) не про него;
    ("dry", None) сухой прогон или справка, обычный путь; ("card", снимок)
    карточка письма; ("mention", None) запуск не простой формой, карточка
    команды мимо автоаппрувов; ("deny", причина) письмо всё равно не уйдёт."""
    if not _mailsend_runs(cmd):
        return "", None
    parsed = _mailsend_argv(cmd, cwd)
    if parsed is None:
        return "mention", None
    argv, base = parsed
    if "-h" in argv or "--help" in argv:
        return "dry", None
    try:
        args = mailsend.build_parser().parse_args(argv)
        if args.dry_run:
            return "dry", None
        letter = mailsend.load_letter(args, dirs=mailsend.attach_dirs(),
                                      limit=mailsend.max_bytes(), base=base)
    except (mailsend.MailError, argparse.ArgumentError, OSError, RuntimeError, ValueError) as e:
        # OSError: файл пропал или не читается; RuntimeError: `~нет_пользователя`
        # в пути; ValueError: нулевой байт в пути (ревью Codex 11.11.2026, круг 2).
        if not isinstance(e, mailsend.MailError):
            log.warning("mailsend_check: %s", type(e).__name__, exc_info=True)
        return "deny", f"mailsend: {e}"
    except SystemExit:
        # Справка сокращением (`--he`, `-hh`): argparse печатает её и выходит,
        # мимо проверки выше. Команда сделает то же, письма не будет; выход
        # наружу уронил бы бота (ревью Opus 11.11.2026).
        return "dry", None
    return "card", {
        "to": list(letter.to), "cc": list(letter.cc), "subject": letter.subject,
        "reply_to": letter.reply_to_id, "body": letter.body,
        "attachments": [{"name": a.name, "size": len(a.data), "path": a.path,
                         "sha256": hashlib.sha256(a.data).hexdigest()}
                        for a in letter.attachments],
        "digest": letter.digest(),
    }


def _human_size(size: int) -> str:
    if size < 1024:
        return _("{size} Б").format(size=size)
    if size < 1024 * 1024:
        return _("{size:.1f} КБ").format(size=size / 1024)
    return _("{size:.1f} МБ").format(size=size / 1024 / 1024)


# Адресаты на карточке отправки: строка до этого предела, длиннее с пометкой.
_RECIPIENTS_CAP = 300
# Вложений поимённо на карточке письма, остальные числом (все по 📄).
_MAIL_ATTACH_SHOWN = 5
RECIPIENTS_CLIPPED = N_("⚠️ <b>адресатов больше, чем видно: список целиком по 📄</b>")


def _plain(val: Any) -> str:
    """Список адресатов строкой через запятую, а не `['a@b.c']`."""
    return ", ".join(str(v) for v in val) if isinstance(val, list) else str(val)


def _recipients_clipped(*vals: str) -> bool:
    """Строка адресатов на карточке обрезана: Иван даёт «ок» на адресатов,
    поэтому обрезка идёт с ⚠️, а не молча (ревью 08.11.2026)."""
    return any(len(_esc(v)) > _RECIPIENTS_CAP for v in vals)


def format_mailsend(mail: dict[str, Any], text_cap: int) -> str:
    """Карточка письма mailsend: адресаты, тема, вложения с размером, текст
    до text_cap (длинная карточка ужимает текст, а не теряет письмо)."""
    if mail.get("warning"):
        return (_("⚠️ <b>команда запускает mailsend не простой формой</b>: письмо так не уйдёт, "
                  "без ✅ на карточке письма mailsend откажет. Форма для письма: "
                  "<code>{form}</code>").format(form=_esc(_MAILSEND_FORM)))
    to, cc = _plain(mail.get("to") or []), _plain(mail.get("cc") or [])
    lines = [_("📤 <b>письмо от твоего имени</b> (Gmail, mailsend)"),
             _("<b>кому:</b> <code>{to}</code>").format(to=_esc_truncate(to, _RECIPIENTS_CAP))]
    if cc:
        lines.append(_("копия: <code>{cc}</code>").format(cc=_esc_truncate(cc, _RECIPIENTS_CAP)))
    if _recipients_clipped(to, cc):
        lines.append(_(RECIPIENTS_CLIPPED))
    if mail.get("reply_to"):
        lines.append(_("ответ на письмо: <code>{reply_to}</code>").format(reply_to=_esc_truncate(str(mail['reply_to']), 300)))
    lines.append(_("тема письма: {subject}").format(subject=_esc_truncate(str(mail.get('subject') or ''), 300)))
    atts = mail.get("attachments") or []
    if atts:
        shown = ", ".join(
            f"<code>{_esc_truncate(str(a.get('name')), 200)}</code> ({_human_size(int(a.get('size') or 0))})"
            for a in atts[:_MAIL_ATTACH_SHOWN])
        if len(atts) > _MAIL_ATTACH_SHOWN:
            shown += _(" и ещё {n} (все по 📄)").format(n=len(atts) - _MAIL_ATTACH_SHOWN)
        lines.append(_("вложения: ") + shown)
    body = str(mail.get("body") or "")
    lines.append(_("текст письма:\n<pre>{body}</pre>").format(body=_esc_truncate(body, text_cap)))
    if len(_esc(body)) > text_cap:
        lines.append(_("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>"))
    return "\n".join(lines)


def mailsend_full_view(mail: dict[str, Any]) -> str:
    if mail.get("warning"):
        return _("--- mailsend ---\nкоманда не простой формой, письмо так не уйдёт.\nФорма: {form}").format(form=_MAILSEND_FORM)
    lines = [_("--- письмо (mailsend) ---"), _("Кому: {to}").format(to=_plain(mail.get('to') or []))]
    if mail.get("cc"):
        lines.append(_("Копия: {cc}").format(cc=_plain(mail['cc'])))
    if mail.get("reply_to"):
        lines.append(_("Ответ на: {reply_to}").format(reply_to=mail['reply_to']))
    lines.append(_("Тема: {subject}").format(subject=mail.get('subject')))
    for a in mail.get("attachments") or []:
        lines.append(_("Вложение: {name} ({size} байт) из {path}").format(
            name=a.get('name'), size=a.get('size'), path=a.get('path')))
    lines.append(_("sha256 письма: {digest}").format(digest=mail.get('digest')))
    lines += ["", _("--- текст письма ---"), str(mail.get("body") or "")]
    return "\n".join(lines)


TEXT_FIELDS = (("message", N_("текст")), ("text", N_("текст")), ("caption", N_("подпись")),
               ("body", N_("текст письма")), ("htmlBody", N_("HTML письма")),
               ("question", N_("вопрос опроса")), ("new_text", N_("новый текст")))


def format_card(tool_input: dict[str, Any], text_cap: int, tool_name: str = "") -> str:
    """Карточка отправки: кому, тема, файлы, все текстовые поля. Это то, на
    что Иван даёт «ок», поэтому поля называются словами, а не JSON, и ни одно
    отправляемое поле не прячется за другим (ревью Codex 12.10.2026)."""
    lines = [_("📤 <b>отправка от твоего имени</b>")]
    # Первое непустое поле адресата идёт в «кому», остальные заполненные
    # остаются в «прочем»: карточка не прячет ни одно поле (ревью 08.11.2026).
    to_key = next((k for k in ("to", "chat_id", "channel_id")
                   if tool_input.get(k) not in (None, "", [])), None)
    to = tool_input.get(to_key) if to_key else None
    reply_mail = tool_input.get("messageId")
    reply_all = tool_input.get("replyAll")
    if to not in (None, "", []):
        lines.append(_("<b>кому:</b> <code>{to}</code>").format(to=_esc_truncate(_plain(to), _RECIPIENTS_CAP)))
    elif reply_mail not in (None, ""):
        # Ответ Gmail без `to` уходит тем, кому Gmail отвечает сам (автор,
        # Reply-To, у своего письма прежние адресаты), с replyAll всем
        # адресатам (схема коннектора 08.11.2026). Адреса бот не знает,
        # поэтому называет письмо, а не человека.
        # Заданная копия заменяет прежнюю (схема reply): «всем адресатам» тогда
        # неверно, остаются строки «ответить всем» и «копия».
        all_of = reply_all is True and "cc" not in tool_input and "to" not in tool_input
        who = (_("<b>кому:</b> всем адресатам письма <code>{id}</code>") if all_of
               else _("<b>кому:</b> как ответит Gmail на письмо <code>{id}</code>"))
        lines.append(who.format(id=_esc_truncate(str(reply_mail), 300)))
        reply_mail = None
        if all_of:
            reply_all = None
        # Явно пустой список адресата у reply заменяет получателей пустым:
        # называем его, а не прячем как незаданный.
        empty = [k for k in ("to", "cc", "bcc") if tool_input.get(k, None) in ("", [])]
        if empty:
            lines.append(_("заданы пустыми") + f": <code>{_esc(', '.join(empty))}</code>")
    if reply_all not in (None, False):
        lines.append(_("ответить всем") + f": <code>{_esc_truncate(json.dumps(reply_all, ensure_ascii=False), 100)}</code>")
    short = tool_name.split("__", 2)[-1]
    target = (_("правка сообщения") if short.startswith("edit")
              else _("реакция на сообщение") if "reaction" in short
              else _("опрос в сообщении") if short == "vote_poll" else _("ответ на сообщение"))
    for key, label in (("cc", _("копия")), ("bcc", _("скрытая копия")),
                       ("message_id", target),
                       ("reply_to", _("ответ на сообщение или тема")),
                       ("replyToMessageId", _("ответ на письмо")),
                       ("replyThreadId", _("в переписке")), ("draftId", _("черновик")),
                       ("topic_id", _("тема"))):
        val = tool_input.get(key)
        if val not in (None, "", []):
            lines.append(f"{label}: <code>{_esc_truncate(_plain(val), 300)}</code>")
    if reply_mail not in (None, ""):
        lines.append(_("ответ на письмо") + f": <code>{_esc_truncate(str(reply_mail), 300)}</code>")
    if _recipients_clipped(*(_plain(tool_input.get(k) or "") for k in ("cc", "bcc")),
                           _plain(to) if to not in (None, "", []) else ""):
        lines.append(_(RECIPIENTS_CLIPPED))
    if tool_input.get("includeSignature"):
        lines.append(_("подпись Gmail: да"))
    votes = tool_input.get("option_indices")
    if votes not in (None, "", []):
        # Сервер считает варианты с нуля, Иван видит опрос сверху вниз с первого:
        # на карточке номер сверху с единицы (ревью Opus 08.11.2026). Не список
        # целых сервер отклонит, такое значение показывается как есть.
        # Порядок сервера это порядок создания опроса; в опросе с перемешанными
        # вариантами приложение показывает другой (перепроверка Opus 08.11.2026).
        # Сервер (FastMCP, pre_parse_json) разбирает строку поля не-строкового
        # типа как JSON: "[1, 3]" уходит голосом, как список (перепроверка Opus
        # 08.11.2026). Повторы сервер отклоняет.
        if isinstance(votes, str):
            try:
                votes = json.loads(votes)
            except ValueError:
                pass
        if (isinstance(votes, list) and all(type(v) is int and v >= 0 for v in votes)
                and len(set(votes)) == len(votes)):
            line = _("голос за варианты по порядку создания опроса (если варианты не "
                     "перемешаны, это порядок сверху): <code>{n}</code>, заменит прежний голос, если был")
            shown = ", ".join(str(v + 1) for v in votes)
        else:
            line = _("голос, номера как переданы (сервер такие отклонит): <code>{n}</code>")
            shown = _plain(votes)
        lines.append(line.format(n=_esc_truncate(shown, 100)))
    emoji = tool_input.get("emoji")
    if emoji not in (None, "", []):
        lines.append(_("реакция") + f": {_esc_truncate(str(tool_input['emoji']), 100)}")
    # Обрезка темы, имён файлов, текста и «прочего» даёт ⚠️ (ревью 08.11.2026).
    clipped = False
    subject = tool_input.get("subject")
    if subject:
        lines.append(_("тема письма: {subject}").format(subject=_esc_truncate(str(subject), 300)))
        clipped = len(_esc(str(subject))) > 300
    files = [n for k in ("file_path", "file_paths")
             for n in (tool_input.get(k) if isinstance(tool_input.get(k), list) else [tool_input.get(k)])
             if n not in (None, "")]
    if files:
        lines.append(_("файл: ") + ", ".join(f"<code>{_esc_truncate(str(n), 200)}</code>" for n in files))
        clipped = clipped or any(len(_esc(str(n))) > 200 for n in files)
    attachments = tool_input.get("attachments")
    if isinstance(attachments, list) and attachments:
        names = [str(a.get("filename") or _("без имени")) if isinstance(a, dict) else str(a)
                 for a in attachments]
        lines.append(_("вложения: ") + ", ".join(f"<code>{_esc_truncate(n, 200)}</code>" for n in names))
        clipped = clipped or any(len(_esc(n)) > 200 for n in names)
    for key in ("account", "parse_mode"):
        val = tool_input.get(key)
        if val not in (None, ""):
            lines.append(f"{key}: <code>{_esc_truncate(str(val), 100)}</code>")
    texts = [(_(label), str(tool_input[key])) for key, label in TEXT_FIELDS
             if tool_input.get(key) not in (None, "")]
    for label, text in texts:
        lines.append(f"{label}:\n<pre>{_esc_truncate(text, text_cap)}</pre>")
        clipped = clipped or len(_esc(text)) > text_cap
    if not texts and not files and not attachments and emoji in (None, "", []) and votes in (None, "", []):
        lines.append(_("<i>текста нет</i>"))
    # Вложения не списком карточка строкой не показывает: тогда они в «прочем».
    known = {to_key, "attachments" if isinstance(attachments, list) else None, "cc", "bcc", "message_id", "reply_to",
             "replyToMessageId", "replyThreadId", "messageId", "replyAll", "includeSignature",
             "draftId", "topic_id", "subject", "file_path", "file_paths", "emoji", "option_indices",
             "account", "parse_mode"} | {k for k, _unused in TEXT_FIELDS}
    rest = {k: v for k, v in tool_input.items() if k not in known and v not in (None, "", [])}
    if rest:
        dumped = json.dumps(rest, ensure_ascii=False)
        lines.append(_("<i>прочее:</i> <code>{rest}</code>").format(rest=_esc_truncate(dumped, 400)))
        clipped = clipped or len(_esc(dumped)) > 400
    if clipped:
        lines.append(_("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>"))
    return "\n".join(lines)


def otklik_match(cfg: Any, pending: Any, chat_id: int,
                 thread_id: int | None) -> dict[str, Any] | None:
    """Отклик, который этот чат взял на отправку, с тем же текстом, что в
    карточке: для пометки, когда одно касание (otklik.grant) не прошло:
    адресат, файл или поле вызова другие, либо в отклике адресат только
    свободным текстом, тогда Иван сверяет его сам с «Куда»."""
    mail = pending.tool_input.get(MAILSEND_CARD_KEY)
    if isinstance(mail, dict):
        texts = [str(mail.get("body") or "")]
    elif is_send(pending.tool_name):
        texts = [str(pending.tool_input[key]) for key, _label in TEXT_FIELDS
                 if pending.tool_input.get(key) not in (None, "")]
    else:
        return None
    key = pending.origin_key or (f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id))
    base = otklik.otklik_dir(cfg.state_dir)
    for body in texts:
        rec = otklik.outbound_match(base, key, body)
        if rec is not None:
            return rec
    return None


_SELF_CHATS = frozenset({"me", "self"})


def _to_self(tool_name: str, tool_input: dict[str, Any], cfg: Any) -> bool:
    """Отправка Telegram самому Ивану: «Избранное» (`me`) или его номер.
    Личка бота не «себе»: бот принял бы сообщение с аккаунта Ивана как его
    команду (`/auto 60`, «ок»), агент выдал бы разрешения сам себе (ревью
    03.11.2026). Почта и Slack себе этим не считаются."""
    if tool_name.split("__", 2)[1:2] != ["telegram"]:
        return False
    # Голос в опросе, пересланном в «Избранное», засчитывается в исходном
    # опросе, где его видят люди: «себе» не бывает (ревью Codex 08.11.2026).
    if tool_name.split("__", 2)[-1] == "vote_poll":
        return False
    chats = {str(tool_input[k]).strip().lower().removeprefix("@") for k in ("chat_id", "entity", "peer")
             if tool_input.get(k) not in (None, "")}
    own = {str(u) for u in getattr(cfg, "allowed_user_ids", ())}
    # Все поля адресата свои: `{"chat_id": "me", "entity": "@bob"}` не себе.
    return bool(chats) and chats <= (_SELF_CHATS | own)


SEND_FILE = "mcp__telegram__send_file"
# Сервер Telegram (telegram-mcp) при TELEGRAM_SERVER_ROOTS_ONLY=1 берёт файлы
# только из своих корней, аргументов после main.py в юните systemd. Вызов,
# который он точно отклонит, бот отклоняет сам до карточки, с подсказкой
# агенту: иначе Иван жал ✅, а файл не уходил (проект 03.11.2026, п. 2).
# Правила по серверу (telegram_mcp/runtime.py, DISALLOWED_PATH_PATTERNS и
# _resolve_readable_file_path, альбом от 2 до 10), кроме относительного пути:
# сервер считает его от первого корня, бот отклоняет (проект 03.11.2026).
_BAD_PATH_CHARS = ("*", "?", "[", "]", "{", "}", "~", "\x00")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
ALBUM_MIN, ALBUM_MAX = 2, 10
# Каталог для копии файла, который лежит вне корней (подсказка агенту).
_SEND_DIR_NAME = paths.TELEGRAM_SEND_DIR.name
# realpath на /mnt/c бывает медленным: не успел, значит проверки нет.
PATH_CHECK_SEC = 3.0
# Корни по юниту: путь -> (mtime_ns, корни или None).
_unit_cache: dict[str, tuple[int, tuple[str, ...] | None]] = {}


def _specifiers(word: str, home: str) -> str | None:
    """%h и %% из юнита systemd; другой спецификатор не разбираем: None."""
    unknown = False

    def one(m: re.Match[str]) -> str:
        nonlocal unknown
        if m.group(1) == "h":
            return home
        if m.group(1) == "%":
            return "%"
        unknown = True
        return ""

    out = re.sub(r"%(.?)", one, word, flags=re.S)
    return None if unknown else out


def parse_unit(text: str, home: str) -> tuple[str, ...] | None:
    """Корни сервера Telegram из текста юнита: аргументы ExecStart после
    main.py, только при TELEGRAM_SERVER_ROOTS_ONLY=1 в Environment. Чего не
    разобрать (флага нет, ключ вместо корня, относительный путь, окружение из
    EnvironmentFile= или UnsetEnvironment=), то None: проверки нет."""
    roots_only, exec_words, section = False, None, ""
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            section = line
            continue
        key, sep, value = line.partition("=")
        if section != "[Service]" or not sep:
            continue
        try:
            words = shlex.split(value)
        except ValueError:
            return None
        key = key.strip()
        if key in ("EnvironmentFile", "UnsetEnvironment"):
            # Флаг может прийти или пропасть мимо Environment=: не угадываем.
            return None
        if key == "Environment":
            # Пустое присваивание сбрасывает окружение юнита.
            if not words:
                roots_only = False
            for w in words:
                name, eq, val = w.partition("=")
                if eq and name == "TELEGRAM_SERVER_ROOTS_ONLY":
                    roots_only = val.strip().lower() in _TRUE_VALUES
        elif key == "ExecStart":
            exec_words = words or None
    if not roots_only or not exec_words:
        return None
    names = [Path(w).name for w in exec_words]
    if "main.py" not in names:
        return None
    roots: list[str] = []
    for w in exec_words[names.index("main.py") + 1:]:
        root = _specifiers(w, home)
        if root is None or w.startswith("-") or not os.path.isabs(root):
            return None
        roots.append(root)
    return tuple(roots) or None


def _unit_roots(unit: Path) -> tuple[str, ...] | None:
    """Корни из юнита с кэшем по mtime. Есть дополнения юнита (`.d/*.conf`):
    настоящий запуск может отличаться, проверки нет."""
    try:
        mtime = unit.stat().st_mtime_ns
        if any(unit.with_name(unit.name + ".d").glob("*.conf")):
            return None
    except OSError:
        return None
    cached = _unit_cache.get(str(unit))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        roots = parse_unit(unit.read_text(encoding="utf-8"), str(Path.home()))
    except (OSError, UnicodeDecodeError):
        return None
    _unit_cache[str(unit)] = (mtime, roots)
    return roots


def _within(real: str, roots: list[str]) -> bool:
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots)


def _bad_name(path: str) -> bool:
    return any(c in Path(path).name for c in _BAD_PATH_CHARS)


def _safe_name(path: str) -> str:
    """Имя файла без символов, которые сервер Telegram не принимает."""
    name = "".join(c for c in Path(path).name if c not in _BAD_PATH_CHARS).strip()
    return name if name.strip(".") else "file"


def path_refusal(file_path: Any, unit: Path | None) -> str | None:
    """Причина отказа send_file, который сервер Telegram точно не примет:
    путь не абсолютный, с символами шаблона или ~, с компонентом .., вне
    корней после realpath; альбом не от 2 до 10 путей. Несуществующий файл
    внутри корней проходит: его не найдёт сервер, а не бот."""
    roots = _unit_roots(unit) if unit is not None else None
    if not roots:
        return None
    paths = file_path if isinstance(file_path, list) else [file_path]
    if isinstance(file_path, list) and not ALBUM_MIN <= len(file_path) <= ALBUM_MAX:
        return _("send_file списком шлёт альбом от {lo} до {hi} файлов, а в вызове {n}: "
                 "пошли один путь строкой или от {lo} до {hi} списком").format(
            lo=ALBUM_MIN, hi=ALBUM_MAX, n=len(file_path))
    if not all(isinstance(p, str) for p in paths):
        return None
    real_roots = [os.path.realpath(r) for r in roots]
    bad = []
    for value in paths:
        # Сервер обрезает пробелы по краям (raw_path.strip() в
        # _resolve_readable_file_path), бот проверяет так же.
        value = value.strip()
        if (not os.path.isabs(value) or any(c in value for c in _BAD_PATH_CHARS)
                or ".." in Path(value).parts or not _within(os.path.realpath(value), real_roots)):
            bad.append(os.path.expanduser(value))
    if not bad:
        return None
    dest = next((r for r in roots if Path(r).name == _SEND_DIR_NAME), roots[0]).rstrip("/") + "/"
    # Имя с символами шаблона сервер не примет и в новом каталоге: копия
    # получает имя без них, каждая своей командой cp.
    # Два файла с одним именем: второй своей командой под именем с номером,
    # иначе cp -f затёр бы первый. `--`: имя вроде `-report.pdf` не ключ
    # (ревью Codex 09.11.2026).
    plain, single, seen = [], [], set()
    for p in bad:
        name = _safe_name(p) if _bad_name(p) else Path(p).name
        if _bad_name(p) or name in seen:
            n, base = 2, name
            while name in seen:
                name, n = f"{n}-{base}", n + 1
            single.append((p, name))
        else:
            plain.append(p)
        seen.add(name)
    cps = ["cp -f -- " + " ".join(shlex.quote(p) for p in plain) + " " + shlex.quote(dest)] if plain else []
    cps += ["cp -f -- " + shlex.quote(p) + " " + shlex.quote(dest + name) for p, name in single]
    # Папки отправки может не быть (новая машина): cp в неё упал бы (ревью Codex 09.11.2026).
    cp = " && ".join(["mkdir -p " + shlex.quote(dest), *cps])
    return _("сервер Telegram берёт файлы только по абсолютному пути из {roots}, без символов "
             "~ * ? [ ] {{ }} и без ..: скопируй {cp} и пошли новым абсолютным путём").format(
        roots=", ".join(roots), cp=cp)


async def _path_check(file_path: Any, unit: Path | None) -> str | None:
    try:
        return await asyncio.wait_for(asyncio.to_thread(path_refusal, file_path, unit), PATH_CHECK_SEC)
    except asyncio.TimeoutError:
        log.warning("send_file path check timed out, no check")
    except Exception:
        log.warning("send_file path check failed, no check", exc_info=True)
    return None


# Одно касание (otklik.grant): какие поля вызова Telegram допустимы и где
# текст. Любое другое заполненное поле (ответ, тема, аккаунт, разметка,
# отложенная отправка) меняет показанное: обычная карточка.
# send_message без parse_mode сервер шлёт как есть (parse_mode=None в Telethon,
# проверено 13.11.2026); у send_file без "plain" подпись читается как Markdown.
_TG_ONE_TOUCH = {"send_message": ("message", frozenset({"chat_id", "message"})),
                 "send_file": ("caption", frozenset({"chat_id", "file_path", "caption", "parse_mode",
                                                     "force_document"}))}


def otklik_send(tool_name: str, tool_input: dict[str, Any], mail: Any = None) -> dict[str, Any] | None:
    """Вызов отправки полями отклика (otklik.send_matches): канал, адресат,
    текст, тема, ответ на письмо, файл (имя и sha256). mail: снимок письма
    mailsend_check. None: форма, которую одно касание не сверяет.
    Файл Telegram читается здесь, а сервер читает его позже: подмена между
    чтениями не ловится (у mailsend хеш сверяет сама отправка)."""
    if isinstance(mail, dict):
        to, atts = mail.get("to") or [], mail.get("attachments") or []
        if len(to) != 1 or mail.get("cc") or len(atts) > 1:
            return None
        att = atts[0] if atts else {}
        return {"via": "mail", "to": to[0], "text": mail.get("body"), "subject": mail.get("subject"),
                "reply_to": mail.get("reply_to"), "attach_name": att.get("name"),
                "attach_sha": att.get("sha256")}
    parts = tool_name.split("__", 2)
    if len(parts) != 3 or parts[:2] != ["mcp", "telegram"] or parts[2] not in _TG_ONE_TOUCH:
        return None
    text_key, allowed = _TG_ONE_TOUCH[parts[2]]
    # Пустое строго: `0 in (False,)` в Python истинно, а `reply_to: 0` это значение.
    if any(not (v is None or v is False or v == "" or v == []) for k, v in tool_input.items()
           if k not in allowed):
        return None
    send = {"via": "telegram", "to": tool_input.get("chat_id"), "text": tool_input.get(text_key)}
    if parts[2] == "send_file":
        # Подпись без parse_mode сервер читает как Markdown: текст изменился бы.
        # Путь только абсолютный (относительный сервер считает от своих корней),
        # имя ссылки то же, что у файла: получатель видит имя, что на карточке.
        path = tool_input.get("file_path")
        if (tool_input.get("parse_mode") != "plain" or not isinstance(path, str)
                or not os.path.isabs(path) or path != path.strip()):
            return None
        try:
            real = Path(path).resolve(strict=True)
            if (real.name != Path(path).name or not real.is_file()
                    or real.stat().st_size > otklik.TG_FILE_MAX):
                return None
            sha = otklik.file_sha(real, otklik.TG_FILE_MAX)
        except (OSError, RuntimeError, ValueError):
            return None
        send.update(attach_name=real.name, attach_sha=sha)
    return send


UNATTENDED_HINT = N_(
    "фоновый ран: не отправляй, положи черновик в отчёт с адресатом, отправит живой ран "
    "после «ок» Ивана")


@dataclass(frozen=True)
class Verdict:
    """Решение по отправке: "allow" (себе), "deny" или "card"; audit: решение
    в журнал, reason: пояснение агенту."""
    decision: str
    audit: str = ""
    reason: str = ""


async def decide(tool_name: str, tool_input: dict[str, Any], *, cfg: Any,
                 unattended: bool, mail_kind: str = "", mail_info: Any = None,
                 chat_key: str = "") -> Verdict:
    """Одна точка решения по отправке от имени Ивана (MCP или mailsend с
    mail_kind из mailsend_check). Себе без карточки, в том числе из фона.
    Ровно показанное в отклике, который взял ран чата chat_key, без карточки
    один раз: ✅ на отклике и есть «ок» (решение Ивана 12.11.2026), повтор
    отказ. Человеку из фонового рана иначе сразу отказ без карточки: Ивана
    рядом нет, а карточка протухла бы за срок (решение Ивана 03.11.2026, п. 6).
    Путь send_file, который сервер Telegram не примет, отказ до карточки."""
    if mail_kind:
        why = mail_info if mail_kind == "deny" else None
    else:
        why = _refusal(tool_name, tool_input)
    if why:
        return Verdict("deny", "deny:outbound", why)
    if tool_name == SEND_FILE:
        why = await _path_check(tool_input.get("file_path"), getattr(cfg, "telegram_mcp_unit", None))
        if why:
            return Verdict("deny", "deny:outbound-path", why)
    to_self = not mail_kind and _to_self(tool_name, tool_input, cfg)
    if chat_key and not to_self:
        send = await asyncio.to_thread(otklik_send, tool_name, tool_input,
                                       mail_info if mail_kind == "card" else None)
        if send is not None:
            try:
                rec = await asyncio.to_thread(otklik.grant, otklik.otklik_dir(cfg.state_dir), chat_key, send)
            except otklik.Refused as e:
                return Verdict("deny", "deny:otklik-repeat", str(e))
            except Exception:
                # Испорченная запись (правка руками) не должна отказывать всем
                # отправкам всех чатов: хук при сбое отказывает (hook_approve).
                log.warning("otklik grant failed, card instead", exc_info=True)
                rec = None
            if rec is not None:
                return Verdict("allow", "allow:otklik", f"otklik:{rec['id']}")
    if unattended and not to_self:
        return Verdict("deny", "deny:outbound-unattended", _(UNATTENDED_HINT))
    if to_self:
        return Verdict("allow", "allow:self", "outbound:self")
    return Verdict("card")

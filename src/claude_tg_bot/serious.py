"""Причины карточки подтверждения: только отправка людям от имени Ивана.

С 04.11.2026 (решение Ивана «Да, только отправки»,
docs/design-2026-11-04-system-boundaries.md) решение бота зовёт send_reasons.
MCP-отправки Telegram, Gmail, Slack и письма mailsend решает outbound.decide;
здесь отправки мимо него: MCP внешнего сервиса, сервер Telegram на loopback,
публикация текста на GitHub, почта из Bash, Bot API Telegram и Slack, REST
Weeek и Jira. Удаление, push, серверы заказчика, вложенный агент, правка
защиты и секреты идут без карточки. Прежний разбор этих классов (модель
«только опасное» 03.11.2026, аудит guard3) удалён 04.11.2026, он есть в
истории git до коммита e97faa2.

Это эвристика, не граница: скрипт из файла, REST прочих сервисов и адрес,
склеенный в коде, разбор не видит; защита от ошибки агента, не от
намеренного обхода (docs/audit-2026-11/guard3-decisions.md).
"""
from __future__ import annotations

import ipaddress
import logging
import re
import shlex
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

from . import outbound, paths
from .i18n import _

log = logging.getLogger(__name__)

BOT_ROOT = paths.BOT_ROOT


# Отправка людям мимо карточки отправки (сервер Telegram на loopback, внешний
# сервис): как отправка, окно ⏩ её не пропускает, из фонового рана отказ.
class Outbound(str):
    pass


def sends(why: Iterable[str]) -> bool:
    """Среди причин отправка людям (Outbound)."""
    return any(isinstance(w, Outbound) for w in why)


def send_reasons(tool_name: str, tool_input: dict, cwd: str | None, *,
                 telegram_port: int = 8770) -> set[str]:
    """Почему действие идёт карточкой; пусто: без карточки.

    Outbound (окно ⏩ не пропускает, из фона отказ): MCP внешнего сервиса,
    который пишет людям; Bash и WebFetch к серверу Telegram на loopback.
    Обычная причина (окно ⏩ пропускает, как до 04.11.2026): MCP share,
    invite, publish; публикация текста на GitHub из Bash (`gh pr create`, `gh
    issue comment`, изменяющий `gh api`); почта из Bash (`mail`, `msmtp`,
    `sendmail`, curl по smtp); Bot API Telegram и Slack; запись в Weeek и
    комментарии Jira через REST."""
    try:
        if tool_name.startswith("mcp__"):
            return _mcp(tool_name)
        if tool_name in ("Bash", "Monitor"):
            return _bash(str(tool_input.get("command", "")), cwd, telegram_port)
        if tool_name == "WebFetch":
            url = str(tool_input.get("url", ""))
            found: set[str] = set()
            if _telegram_loopback_url(url, telegram_port):
                found.add(Outbound(_("отправка через сервер Telegram мимо проверки")))
            if _bot_api_send(url):
                found.add(_("отправка людям через Bot API Telegram или Slack"))
            return found
    except Exception:
        log.exception("serious: send check failed tool=%s", tool_name)
        return {_("проверка не удалась")}
    return set()


# --- MCP ---------------------------------------------------------------------

# Те же слова чтения и ожидания, что у outbound.is_send (wait_for_new_message, 08.11.2026).
_MCP_READ_WORDS = outbound.READ_WORDS
_MCP_SEND_WORDS = frozenset({"send", "reply", "post", "comment", "forward", "email", "respond"})
# Слово message само по себе не отправка: trash_message, label_message,
# update_message_labels у коннектора Gmail шли карточкой (ревью 04.11.2026);
# с глаголом записи (create_message, conversations_add_message) отправка.
_MCP_MESSAGE_VERBS = frozenset({"create", "add", "edit", "update", "schedule", "write", "compose", "new",
                                "broadcast", "chat"})


def _mcp(tool_name: str) -> set[str]:
    short = tool_name.split("__", 2)[-1]
    if short in outbound.NOT_SENDS:
        return set()
    words = set(re.split(r"[_\-]+", re.sub(r"(?<=[a-z])(?=[A-Z])", "_", short).lower()))
    out: set[str] = set()
    # Приглашение и выдача доступа людям: обычная карточка, как до 04.11.2026.
    if words & {"share", "invite", "publish"}:
        out.add(_("публикация или доступ другим людям"))
    # Отправка людям через MCP, которых нет в карточке отправки approval
    # (комментарий GitHub и Jira, письмо HubSpot, ответ Intercom). Свой Todoist
    # и календарь не в счёт (ревью Opus 03.11.2026).
    server = tool_name.split("__", 2)[1].lower() if tool_name.count("__") >= 2 else ""
    own = any(w in server for w in ("todoist", "calendar"))
    if not own and not words & _MCP_READ_WORDS and (
            words & _MCP_SEND_WORDS
            or ("message" in words and words & _MCP_MESSAGE_VERBS and not words & {"label", "labels"})
            or (words & {"create", "add"} and words & {"issue", "pull", "pr", "ticket", "conversation",
                                                    "conversations"})):
        out.add(Outbound(_("отправка людям через внешний сервис")))
    return out


# --- Bash --------------------------------------------------------------------

# Локальный адрес сервера Telegram: `127.0.0.1`, `127.1`, `localhost`, `localhost.`,
# `[::1]`, `[::ffff:127.0.0.1]`, `0.0.0.0` и `0` в месте адреса (`0:8770`,
# `/dev/tcp/0/8770`, `"0"`). Защита от ошибки агента, не от намеренного обхода
# (`0x7f000001`, переменные): решение Ивана 03.11.2026, README.
_LOOPBACK_RE = re.compile(
    r"(?<![\w.])(?:127(?:\.\d+){1,3}|(?:ip6-)?localhost\.?|0\.0\.0\.0|\[?::1\]?"
    r"|0(?=:\d|/\d)|(?<=['\"])0(?=['\"]))(?![\w.])", re.IGNORECASE)
_MAIL_PROGS = frozenset({"mail", "mailx", "sendmail", "msmtp", "mutt", "swaks"})
_SEND_PROG_RE = re.compile(r"\b(?:gh|mailx?|sendmail|msmtp|mutt|swaks)\b")
# Запрос к Bot API Telegram или Slack, который пишет людям (curl, python),
# письмо curl по SMTP, текст людям через REST Weeek и Jira: до 04.11.2026 шли
# карточкой через «секрет в сеть» или «загрузка файла». Адрес и метод ищутся
# отдельно: база адреса бывает в переменной (`B=…; curl "$B/sendMessage"`).
# Путь Bot API узнаётся и без облачного узла: свой сервер telegram-bot-api
# (TELEGRAM_BOT_API_URL, tg_api) стоит на другом адресе, путь `/bot<токен>/`
# тот же: токен литералом, переменной оболочки (`$T`, `${T}`, `$1`, `$(cat …)`,
# `"$T"`, `$(< файл)`) или вставкой в строку Python (`{tok}`, `" + tok`, `%s`).
# Модуль бота tg_api из Python (`tg_api.post(токен, "sendMessage", …)`, `from
# …tg_api import post`, метод в переменной): строка метода отправки где угодно
# в выполняемом тексте с tg_api.
_TG_BOT_PATH = r"/bot(?:\d{5,}:[\w-]+|\$[{(]?\s*[\w<]|[{\"'`]|%[sr(])"
_TG_MODULE_RE = re.compile(r"\btg_api\b")
_TG_METHOD_LITERAL_RE = re.compile(r"['\"](?:send|forward|copy|edit)\w*['\"]", re.IGNORECASE)
_API_HINT_RE = re.compile(r"api\.telegram\.org|slack\.com/api|smtps?://|weeek\.net|atlassian\.net|\btg_api\b|"
                          + _TG_BOT_PATH, re.IGNORECASE)
_TG_API_RE = re.compile(r"api\.telegram\.org/bot|" + _TG_BOT_PATH, re.IGNORECASE)
_TG_SEND_RE = re.compile(r"/(?:send|forward|copy|edit)\w*", re.IGNORECASE)
_SLACK_API_RE = re.compile(r"slack\.com/api", re.IGNORECASE)
_SLACK_SEND_RE = re.compile(r"\b(?:chat\.(?:postMessage|scheduleMessage|postEphemeral|update)"
                            r"|files\.(?:upload\w*|completeUploadExternal))\b")
_SMTP_RE = re.compile(r"smtps?://", re.IGNORECASE)
_REST_TEXT_HOST_RE = re.compile(r"api\.weeek\.net|atlassian\.net/rest/api/\S*comment", re.IGNORECASE)
_WRITE_REQUEST_RE = re.compile(r"(?:-X\s*|--request[=\s]+|--method[=\s]+)['\"]?(?:POST|PUT|PATCH)\b"
                               r"|(?:^|\s)(?:-[A-Za-z]*[dF]|--data(?:-\w+)?|--form|--json|--post-(?:data|file)"
                               r"|--body-(?:data|file))(?=[\s=@'\"]|$)")


def _bash(command: str, cwd: str | None, telegram_port: int) -> set[str]:
    out: set[str] = set()
    # Сервер Telegram на loopback принимает запросы без пароля: `curl` или
    # `python` отправили бы сообщение от имени Ивана мимо карточки. Аргументы
    # текстовых команд (шаблон grep, сообщение коммита) не в счёт.
    if telegram_port and _LOOPBACK_RE.search(command):
        executed = outbound.executed_text(command)
        if _LOOPBACK_RE.search(executed) and re.search(rf"(?<![\w.]){telegram_port}(?!\d)", executed):
            out.add(Outbound(_("отправка через сервер Telegram мимо проверки")))
    if _SEND_PROG_RE.search(command):
        for prog, args in _programs(command):
            if prog == "gh":
                out.update(_gh_send(args))
            elif prog in _MAIL_PROGS:
                out.add(_("письмо из Bash ({prog})").format(prog=prog))
    if _API_HINT_RE.search(command):
        executed = outbound.executed_text(command)
        if _bot_api_send(executed) or (_SMTP_RE.search(executed) and "--mail-rcpt" in executed):
            out.add(_("отправка людям через Bot API Telegram или Slack"))
        if _REST_TEXT_HOST_RE.search(executed) and _WRITE_REQUEST_RE.search(executed):
            # Комментарий или карточка в трекере задач (Weeek, Jira): текст
            # читают люди, на эту карточку опирается навык трекера (ревью 04.11.2026).
            out.add(_("текст людям через REST внешнего сервиса (Weeek, Jira)"))
    return out


# Обёртки, которые запускают следующую команду: короткие буквы ключей со
# значением (в том числе последней буквой склейки: `sudo -iu root`, `xargs
# -0n 1`) и длинные ключи со значением следующим словом. Ключ вида `-n5`,
# `--signal=TERM` значение несёт сам.
_WRAPPER_VALUE: dict[str, tuple[str, frozenset[str]]] = {
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
# Обёртки с позиционным словом перед командой: маска, приоритет, файл замка.
_WRAPPER_POSITIONAL = {"taskset": 1, "chrt": 1}
# Обёртки, которые исполняют строку из ключа: `su -c 'cmd'`, `flock f -c 'cmd'`,
# `script -c 'cmd' файл`.
_STRING_COMMAND = frozenset({"su", "script"})
# Подкоманды rtk, которые запускают команду следом (`rtk run gh …`).
_RTK_RUNNERS = frozenset({"proxy", "run", "test", "err", "summary"})
# Слова оболочки перед командой: `if true; then gh …`, `do gh …`, `! gh …`.
_SHELL_WORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "fi", "done"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_SHELL_VALUE = frozenset({"--rcfile", "--init-file"})
# Ключи find, после которых идёт команда до `;` или `+`.
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_LEX_BUDGET = 200_000  # знаков на весь разбор одной команды, с вложенными


class _Seg:
    """Звено команды: слова без кавычек, тела heredoc (текст, ограничитель в
    кавычках) и следующее звено конвейера (`cat <<EOF | bash`)."""
    __slots__ = ("words", "bodies", "next")

    def __init__(self) -> None:
        self.words: list[str] = []
        self.bodies: list[tuple[str, bool]] = []
        self.next: _Seg | None = None


class _Lexer:
    """Небольшой лексер оболочки для поиска программ (с 04.11.2026 вместо
    разбора danger, ревью Opus и Codex): кавычки, `$'…'`, перенос `\\` в конце
    строки, комментарий `#` в начале слова, вложенные `$(…)` и обратные
    кавычки (их звенья идут общим списком), `$((…))`, тела heredoc, подстановка
    процесса, цели перенаправлений с номером дескриптора и `<<<`. Не оболочка:
    расширений и алиасов не знает."""

    def __init__(self, text: str, budget: list[int]) -> None:
        self.t, self.n, self.budget = text, len(text), budget
        self.segs: list[_Seg] = []

    def lex(self) -> list[_Seg]:
        self._parse(0, False, 0)
        return self.segs

    def _spent(self) -> bool:
        self.budget[0] -= 1
        return self.budget[0] <= 0

    def _nested(self, text: str, depth: int) -> None:
        sub = _Lexer(text, self.budget)
        sub._parse(0, False, depth + 1)
        self.segs += sub.segs

    def _sub(self, i: int, depth: int) -> int:
        """`$(…)`, `<(…)`, `>(…)` с позиции после скобки; звенья в общий список."""
        if depth > 40:
            level = 1
            while i < self.n and level:
                level += {"(": 1, ")": -1}.get(self.t[i], 0)
                i += 1
            return i
        return self._parse(i, True, depth + 1)

    def _backtick(self, i: int, depth: int) -> int:
        j = i
        while j < self.n and self.t[j] != "`":
            j += 2 if self.t[j] == "\\" else 1
        self._nested(self.t[i:j], depth)
        return j + 1

    def _arith(self, i: int) -> int:
        level, j = 0, i + 1
        while j < self.n:
            level += {"(": 1, ")": -1}.get(self.t[j], 0)
            if level == 0:
                return j + 1
            j += 1
        return j

    def _dquote(self, i: int, word: list[str], depth: int) -> int:
        t = self.t
        while i < self.n and t[i] != '"' and not self._spent():
            if t[i] == "\\" and i + 1 < self.n:
                word.append(t[i + 1])
                i += 2
            elif t.startswith("$((", i):
                end = self._arith(i)
                word.append(t[i:end])
                i = end
            elif t.startswith("$(", i):
                i = self._sub(i + 2, depth)
            elif t[i] == "`":
                i = self._backtick(i + 1, depth)
            else:
                word.append(t[i])
                i += 1
        return i + 1

    def subs_only(self, depth: int) -> list[_Seg]:
        """Подстановки в тексте как в двойных кавычках (тело heredoc с
        ограничителем без кавычек: оболочка их исполнит и у cat)."""
        t, i = self.t, 0
        while i < self.n and not self._spent():
            if t[i] == "\\":
                i += 2
            elif t.startswith("$((", i):
                i = self._arith(i)
            elif t.startswith("$(", i):
                i = self._sub(i + 2, depth)
            elif t[i] == "`":
                i = self._backtick(i + 1, depth)
            else:
                i += 1
        return self.segs

    def _parse(self, i: int, closer: bool, depth: int) -> int:
        t, n = self.t, self.n
        seg, word, has_word, skip_next = _Seg(), [], False, False
        pending: list[tuple[_Seg, str, bool, bool]] = []
        pipe_src: list[_Seg | None] = [None]

        def end_word() -> None:
            nonlocal word, has_word, skip_next
            if has_word:
                if skip_next:
                    skip_next = False
                else:
                    seg.words.append("".join(word))
            word, has_word = [], False

        def end_seg(pipe: bool = False) -> None:
            nonlocal seg
            end_word()
            if seg.words or seg.bodies:
                self.segs.append(seg)
                if pipe_src[0] is not None:
                    pipe_src[0].next = seg
                pipe_src[0] = seg if pipe else None
            seg = _Seg()

        def fd_word() -> None:
            nonlocal word, has_word
            if has_word and "".join(word).isdigit():
                word, has_word = [], False

        while i < n and not self._spent():
            c = t[i]
            if c == "\\" and i + 1 < n:
                if t[i + 1] != "\n":
                    word.append(t[i + 1])
                    has_word = True
                i += 2
            elif c == "\n":
                end_word()
                i += 1
                for hseg, delim, strip, quoted in pending:
                    lines: list[str] = []
                    while i < n:
                        e = t.find("\n", i)
                        line, i = (t[i:], n) if e < 0 else (t[i:e], e + 1)
                        if (line.lstrip("\t") if strip else line) == delim:
                            break
                        lines.append(line)
                    hseg.bodies.append(("\n".join(lines), quoted))
                pending = []
                end_seg()
            elif c in " \t":
                end_word()
                i += 1
            elif not has_word and t.startswith("[[", i) and (i + 2 >= n or t[i + 2] in " \t"):
                e = t.find("]]", i + 2)
                e = n if e < 0 else e + 2
                word.append(t[i:e])
                has_word = True
                i = e
            elif c == "#" and not has_word:
                e = t.find("\n", i)
                i = n if e < 0 else e
            elif c == "'":
                e = t.find("'", i + 1)
                e = n if e < 0 else e
                word.append(t[i + 1:e])
                has_word = True
                i = e + 1
            elif t.startswith("$'", i):
                e = i + 2
                while e < n and t[e] != "'":
                    e += 2 if t[e] == "\\" else 1
                word.append(t[i + 2:e])
                has_word = True
                i = e + 1
            elif t.startswith("$((", i):
                e = self._arith(i)
                word.append(t[i:e])
                has_word = True
                i = e
            elif t.startswith("$(", i):
                i = self._sub(i + 2, depth)
                word.append("$(…)")
                has_word = True
            elif c == "`":
                i = self._backtick(i + 1, depth)
                word.append("`…`")
                has_word = True
            elif c == '"':
                i = self._dquote(i + 1, word, depth)
                has_word = True
            elif c in "<>" and t.startswith("(", i + 1):
                end_word()
                i = self._sub(i + 2, depth)
            elif t.startswith("<<<", i):
                fd_word()
                end_word()
                skip_next = True
                i += 3
            elif t.startswith("<<", i):
                fd_word()
                end_word()
                e = i + 2
                strip = e < n and t[e] == "-"
                e += strip
                while e < n and t[e] in " \t":
                    e += 1
                delim: list[str] = []
                quoted = False
                while e < n and t[e] not in " \t\n;|&<>()":
                    if t[e] in "'\"":
                        quoted = True
                        q = t.find(t[e], e + 1)
                        q = n if q < 0 else q
                        delim.append(t[e + 1:q])
                        e = q + 1
                    elif t[e] == "\\":
                        quoted = True
                        delim.append(t[e + 1:e + 2])
                        e += 2
                    else:
                        delim.append(t[e])
                        e += 1
                pending.append((seg, "".join(delim), strip, quoted))
                i = e
            elif c in "<>" or t.startswith("&>", i):
                # `> /tmp/mail`, `2>/dev/null gh`, `2>&1`: цель не программа.
                fd_word()
                end_word()
                e = i + 1
                while e < n and t[e] in "<>&|":
                    e += 1
                skip_next = True
                if e < n and (t[e].isdigit() or t[e] == "-") and t[e - 1] == "&":
                    e += 1
                    skip_next = False
                i = e
            elif c in ";&|":
                e = i
                while e < n and t[e] in ";&|":
                    e += 1
                end_seg(pipe=t[i:e] in ("|", "|&"))
                i = e
            elif c == "(":
                end_seg()
                i += 1
            elif c == ")":
                end_seg()
                i += 1
                if closer:
                    return i
            else:
                word.append(c)
                has_word = True
                i += 1
        end_seg()
        return i


def _skip_options(name: str, words: list[str]) -> int:
    """Номер первого слова после ключей обёртки `name`."""
    letters, long_values = _WRAPPER_VALUE[name]
    i = 1
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


def _runs_shell(words: list[str], depth: int) -> bool:
    """Звено исполняет оболочку (`bash`, `timeout 30 sh`), а не текстовую
    программу с аргументом bash (`grep bash`, ревью Codex 04.11.2026)."""
    progs = _programs(shlex.join(words), depth + 1) if words else []
    return bool(progs) and progs[0][0] in _SHELLS


def _programs(command: str, depth: int = 0) -> list[tuple[str, list[str]]]:
    """Программы, которые команда исполняет, с аргументами: звенья команды
    и её подстановок (_Lexer; цели перенаправлений не программы), тела
    heredoc оболочки и подстановки в теле без кавычек после присваиваний, слов
    оболочки (if, then, do) и обёрток (sudo, env, timeout, nice, nohup, xargs,
    `rtk`, `rtk run|proxy`); `bash -c '…'` и `env -S '…'` разбираются
    рекурсией. Имя программы без пути. Это разбор от ошибки агента, не от
    намеренного обхода (`$(which gh)`, программа из переменной)."""
    out: list[tuple[str, list[str]]] = []
    budget = [_LEX_BUDGET]
    segs = _Lexer(command, budget).lex()
    for seg in list(segs):
        for body, quoted in seg.bodies:
            if depth < 3 and (_runs_shell(seg.words, depth) or (seg.next is not None
                                                                and _runs_shell(seg.next.words, depth))):
                # Тело исполняет оболочка: `bash <<'EOF'`, `timeout 30 sh <<EOF`, `cat <<EOF | bash`.
                out += _programs(body, depth + 1)
            elif not quoted:
                segs += _Lexer(body, budget).subs_only(depth)
    for words in (seg.words for seg in segs):
        while words:
            head = words[0]
            if not head:
                words = words[1:]
                continue
            if any(c.isspace() for c in head) and depth < 3:
                # Строка команды словом целиком: `rtk run 'gh …'`, `watch -n 5 'gh …'`.
                out += _programs(head, depth + 1)
                break
            if head in _SHELL_WORDS:
                words = words[1:]
                continue
            if "=" in head and not head.startswith("-") and head.split("=", 1)[0].isidentifier():
                words = words[1:]
                continue
            name = head.rsplit("/", 1)[-1]
            if name in ("command", "type", "which", "hash") and words[1:2] and words[1] in ("-v", "-V", "-p", "-t", "-a"):
                if name != "command" or words[1] != "-p":
                    break
            if name == "rtk" and words[1:2] and words[1] in _RTK_RUNNERS:
                words = words[2:]
                continue
            if name == "env" and depth < 3:
                # `env -S 'gh pr' comment 1`: строка и остальные слова одна команда.
                # Ищется только среди ключей env, не в аргументах запущенной программы.
                split = None
                n = 0
                while n + 1 < len(words):
                    n += 1
                    w = words[n]
                    if w in ("-u", "--unset", "-C", "--chdir"):
                        n += 1
                        continue
                    if w in ("-S", "--split-string") and n + 1 < len(words):
                        split = (words[n + 1], words[n + 2:])
                    elif w.startswith("--split-string="):
                        split = (w.split("=", 1)[1], words[n + 1:])
                    elif w.startswith("-S") and len(w) > 2:
                        split = (w[2:], words[n + 1:])
                    elif not (w.startswith("-") or ("=" in w and w.split("=", 1)[0].isidentifier())):
                        break
                    if split is not None:
                        break
                if split is not None:
                    out += _programs(" ".join([split[0], shlex.join(split[1])]), depth + 1)
                    break
            if name in _STRING_COMMAND and depth < 3:
                # `su - x -c 'cmd'`, `script -qc 'cmd' файл`: строка из ключа -c.
                value = next((words[n + 1] for n, w in enumerate(words[:-1])
                              if w in ("-c", "--command") or (w[:1] == "-" and w[1:2] != "-" and w.endswith("c"))),
                             None)
                value = value or next((w.split("=", 1)[1] for w in words if w.startswith("--command=")), None)
                if value is not None:
                    out += _programs(value, depth + 1)
                    break
            if name == "flock":
                # `flock [ключи] файл команда` или `flock [ключи] файл -c 'cmd'`.
                i = _skip_options(name, words) + 1
                if i < len(words) and words[i] in ("-c", "--command") and i + 1 < len(words) and depth < 3:
                    out += _programs(words[i + 1], depth + 1)
                    break
                words = words[i:]
                continue
            if name in _WRAPPER_VALUE:
                i = _skip_options(name, words)
                if name == "timeout" and i < len(words):
                    i += 1  # срок
                i += _WRAPPER_POSITIONAL.get(name, 0)
                words = words[i:]
                continue
            if name == "eval" and depth < 3:
                out += _programs(" ".join(words[1:]), depth + 1)
                break
            if name in ("fd", "fdfind"):
                for n, w in enumerate(words):
                    if w in ("-x", "-X", "--exec", "--exec-batch") and n + 1 < len(words) and depth < 3:
                        out += _programs(shlex.join(words[n + 1:]), depth + 1)
                        break
            if name == "find":
                for n, w in enumerate(words):
                    if w in _FIND_EXEC and n + 1 < len(words):
                        inner = words[n + 1:]
                        end = next((k for k, x in enumerate(inner) if x in (";", "+", "\\;")), len(inner))
                        out += _programs(shlex.join(inner[:end]), depth + 1) if depth < 3 else []
            if name in _SHELLS and depth < 3:
                i, with_c = 1, False
                while i < len(words) and words[i] and words[i][0] in "-+":
                    w = words[i]
                    if w in _SHELL_VALUE or (not w.startswith("--") and w[-1] in "oO"):
                        # `-o pipefail`, склейка `-euo pipefail`: значение следующим словом.
                        with_c = with_c or (not w.startswith("--") and "c" in w[1:])
                        i += 2
                        continue
                    if not w.startswith("--") and "c" in w[1:]:
                        with_c = True
                    i += 1
                if with_c and i < len(words):
                    out += _programs(words[i], depth + 1)
                    break
            out.append((name, words[1:]))
            break
    return out


def _bot_api_send(text: str) -> bool:
    return bool((_TG_API_RE.search(text) and _TG_SEND_RE.search(text))
                or (_TG_MODULE_RE.search(text) and _TG_METHOD_LITERAL_RE.search(text))
                or (_SLACK_API_RE.search(text) and _SLACK_SEND_RE.search(text)))


# --- GitHub ------------------------------------------------------------------

# Подкоманды gh, которые публикуют текст от имени Ивана.
_GH_TEXT = frozenset({("pr", "create"), ("pr", "comment"), ("pr", "review"), ("pr", "edit"), ("issue", "create"),
                      ("issue", "comment"), ("issue", "edit"), ("release", "create"), ("release", "upload"),
                      ("gist", "create")})
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname", "-H", "--header", "-X", "--method", "-q", "--jq",
                             "-t", "--template", "--title", "-b", "--body", "-F", "-f", "--field",
                             "--raw-field", "--input", "-B", "--base", "-l", "--label"})


def _gh_positionals(args: list[str]) -> list[str]:
    pos: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in _GH_VALUE_FLAGS:
            i += 2
            continue
        if not args[i].startswith("-"):
            pos.append(args[i])
        i += 1
    return pos


def _gh_send(args: list[str]) -> set[str]:
    """Публикация текста на GitHub: подкоманды _GH_TEXT, закрытие с
    комментарием (`gh pr close -c`) и изменяющий `gh api`."""
    pos = _gh_positionals(args)
    if tuple(pos[:2]) in _GH_TEXT or (tuple(pos[:2]) in (("pr", "close"), ("issue", "close")) and any(
            a in ("-c", "--comment") or a.startswith("--comment=") for a in args)):
        return {_("публикация на GitHub (gh {a} {b})").format(a=pos[0], b=pos[1])}
    if pos[:2] == ["api", "graphql"]:
        if re.search(r"\bmutation\b", " ".join(args)) or "--input" in args:
            return {_("изменение через GitHub API")}
    elif pos[:1] == ["api"]:
        # Метод по значению опции; без неё gh с полями шлёт POST (ревью Codex 03.11.2026).
        method = ""
        for i, a in enumerate(args):
            if a in ("-X", "--method") and i + 1 < len(args):
                method = args[i + 1].upper()
            elif a.startswith("-X") and len(a) > 2:
                method = a[2:].upper()
            elif a.startswith("--method="):
                method = a.split("=", 1)[1].upper()
        fields = any(a in ("-f", "-F", "--field", "--raw-field", "--input") or
                     a.startswith(("--field=", "--raw-field=", "--input=")) for a in args)
        if method in ("POST", "PUT", "PATCH", "DELETE") or (not method and fields):
            return {_("изменение через GitHub API")}
    return set()


# --- WebFetch ----------------------------------------------------------------

# Сокращённая запись IPv4 (`127.1`, `2130706433`, `0x7f000001`): её разбирает
# inet_aton, как и сам клиент.
_SHORT_IPV4_RE = re.compile(r"(?:0x[0-9a-f]*|\d+)(?:\.(?:0x[0-9a-f]*|\d+)){0,3}")


def _telegram_loopback_url(url: str, telegram_port: int) -> bool:
    """WebFetch на loopback с портом сервера Telegram (guard3, Н8). Имя
    loopback только localhost, localhost.localdomain и *.localhost (RFC 6761);
    `localhost.example.com` обычный внешний адрес."""
    if not telegram_port:
        return False
    try:
        u = urlsplit(url.strip())
        host = (u.hostname or "").lower().rstrip(".")
        port = u.port
    except ValueError:
        return False
    if port != telegram_port:
        return False
    ip = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if _SHORT_IPV4_RE.fullmatch(host):
            try:
                ip = ipaddress.ip_address(socket.inet_aton(host))
            except (OSError, ValueError):
                ip = None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return host in ("localhost", "localhost.localdomain") or host.endswith(".localhost") or bool(
        ip and (ip.is_loopback or ip.is_unspecified))

"""Причины карточки подтверждения: только отправка людям от имени Ивана.

С 04.11.2026 (решение Ивана «Да, только отправки»,
docs/design-2026-11-04-system-boundaries.md) решение бота зовёт send_reasons.
MCP-отправки Telegram, Gmail, Slack и письма mailsend решает outbound.decide;
здесь отправки мимо него: MCP внешнего сервиса, сервер Telegram на loopback,
публикация текста на GitHub, почта из Bash, Bot API Telegram и Slack, REST
Weeek и Jira. С 11.11.2026 ещё необратимое на серверах заказчиков из
CLIENT_HOSTS (Irreversible). Прочее удаление, push, вложенный агент, правка
защиты и секреты идут без карточки. Прежний разбор этих классов (модель
«только опасное» 03.11.2026, аудит guard3) удалён 04.11.2026, он есть в
истории git до коммита e97faa2.

Это эвристика, не граница: скрипт из файла, REST прочих сервисов и адрес,
склеенный в коде, разбор не видит; защита от ошибки агента, не от
намеренного обхода (docs/audit-2026-11/guard3-decisions.md).
"""
from __future__ import annotations

import bisect
import ipaddress
import logging
import re
import shlex
import socket
from collections.abc import Iterable
from typing import Any
from functools import lru_cache
from urllib.parse import urlsplit

from . import outbound, paths
from .i18n import N_, _

log = logging.getLogger(__name__)

BOT_ROOT = paths.BOT_ROOT


# Отправка людям мимо карточки отправки (сервер Telegram на loopback, внешний
# сервис): как отправка, окно ⏩ её не пропускает, из фонового рана отказ.
class Outbound(str):
    pass


def sends(why: Iterable[str]) -> bool:
    """Среди причин отправка людям (Outbound)."""
    return any(isinstance(w, Outbound) for w in why)


# Необратимое на сервере заказчика (цель 2б, 11.11.2026): окно ⏩ пропускает
# (плановое обслуживание), из фонового рана отказ.
class Irreversible(str):
    pass


def irreversible(why: Iterable[str]) -> bool:
    return any(isinstance(w, Irreversible) for w in why)


IRREVERSIBLE_UNATTENDED_HINT = N_(
    "фоновый ран: необратимое на сервере заказчика не выполняй, положи точную команду в отчёт, "
    "выполнит живой ран после «ок» Ивана")

# Без разбора bash: слово в исполняемом тексте (outbound.executed_text: команда
# для ssh в кавычках и heredoc в `bash -s` да, сообщение коммита и echo нет).
# Пролёт до слова не длиннее 1000 знаков: иначе время квадратичное на длинной строке.
_IRREVERSIBLE_RE = re.compile(r"""
    (?<![\w-])rm\s+(?:-\S{0,40}\s+){0,10}-(?:-recursive\b|[a-z]*r) | \brmdir\b | \bdropdb\b
    | \bfind\b[^|;&\n]{0,1000}\s(?:-exec(?:dir)?\s+(?:\S*/)?rm\b|-delete\b) | \bxargs\s+(?:-\S+\s+){0,10}(?:\S*/)?rm\b
    | \bshred\b | \bwipefs\b | \b(?:lv|vg|pv)remove\b | \bz(?:fs|pool)\s+destroy\b
    | \bcrontab\b[^|;&\n]{0,100}\s-[a-z]*r | \bvirsh\b[^|;&\n]{0,1000}\b(?:undefine|destroy)\b
    | \b(?:vssadmin|wbadmin)\b[^|;&\n]{0,100}\bdelete\b
    | \btruncate\b | \bmkfs
    | \bdd\b[^|;&\n]{0,1000}\bof=
    | \bdocker(?:-compose)?(?:\s+--?(?!-)[\w-]+(?:[= ](?!-)\S+)?){0,10}?
      \s+(?:(?:compose|container|image|volume|network|system|builder|buildx|stack|service|secret|config
              |node|plugin|context|manifest)\s+(?:--?(?!-)[\w-]+(?:[= ](?!-)\S+)?\s+){0,10}?)?
      (?:rm|rmi|prune)\b
    | \bcompose\b[^|;&\n]{0,1000}\bdown\b | \bkubectl\b[^|;&\n]{0,1000}\bdelete\b
    | \bterraform\b(?:[^|;&\n]{0,1000}(?<![\w-])destroy\b|(?=[^|;&\n]{0,1000}\bapply\b)[^|;&\n]{0,1000}\s--?destroy\b)
    | (?<![\w/.-])(?<![^|]\|)(?:(?<=\s)(?:shutdown|reboot|poweroff|halt)(?![\w-])  # не путь, не `a|reboot|b` шаблона
       | (?:shutdown|reboot|poweroff|halt)(?![\w-]|\|[^|]))
    | \bsystemctl\b[^|;&\n]{0,1000}(?<![\w-])(?:stop|disable|mask)\b | \bservice\s+\S+\s+stop\b
    | \b(?:Remove-Item|Remove-VM\w*|Remove-AD\w*|Remove-Mailbox|Remove-Partition|Clear-Disk|Format-Volume
         |Restart-Computer|Stop-Computer|Stop-Service|Stop-VM)\b | \b(?:rd|del)\b[^|;&\n]{0,1000}\s/s\b
""", re.IGNORECASE | re.VERBOSE)


# SQL ищется по всему исполняемому тексту: тело heredoc для psql или mysql
# разбор делит на строки, а `WHERE` бывает строкой ниже (перепроверка Opus 13.11.2026).
# `WHERE` в значении или комментарии SQL: принятый риск.
_SQL_RE = re.compile(r"""
    \bdrop\s+(?:table|database|schema)\b | \bdelete\s+from\b
    | \bupdate\s+\S+\s+set\b(?![^;]{0,1000}\bwhere\b) | \balter\s+table\b[^;]{0,1000}\bdrop\b
""", re.IGNORECASE | re.VERBOSE)


_SQL_CLIENTS = r"(?<![\w.-])(?:psql|pgcli|mysql|mariadb|sqlite3|sqlcmd|clickhouse(?:-client)?|cqlsh)(?![\w-])"
_SQL_STDIN_RE = re.compile(r"\bdrop\s+(?:table|database|schema)\b|\bdelete\s+from\b|\btruncate\s+(?:table\s+)?\w",
                           re.IGNORECASE)
_HELP = frozenset({"--help", "--version"})
_SQL_HEADS = frozenset({"update", "delete", "drop", "alter", "insert", "select", "where", "set", "from"})



@lru_cache(maxsize=4)
def _hosts_re(hosts: tuple[str, ...]) -> re.Pattern[str] | None:
    """Хост словом: `ssh h`, `u@h`, `-ComputerName h`, `\\\\h\\share`; не часть пути
    `/srv/h` и `C:\\x\\h`, не `h.json`. Запись с точкой в начале: суффикс имени (`.corp.local`)."""
    parts = [r"[\w.-]*" + re.escape(h) if h.startswith(".") else re.escape(h) for h in hosts if h.strip(".")]
    if not parts:
        return None
    return re.compile(r"(?<![\w./-])(?<![^\\]\\)(?:" + "|".join(parts) + r")(?![\w-]|\.\w)", re.IGNORECASE)


def _client_irreversible(command: str, hosts: tuple[str, ...]) -> str:
    """Необратимое слово, если в исполняемом тексте ещё и сервер заказчика; иначе
    пусто. Имя сервера в сообщении коммита или echo не в счёт."""
    found = _hosts_re(hosts) if hosts else None
    if not found or not found.search(command):
        return ""
    executed = outbound.executed_text(command)
    return _irreversible_in(command, found, False, 0, executed) if found.search(executed) else ""


# Аргументы не исполняются, а шаблоны и вывод в них бывают со словами из списка
# (`grep -r "delete from"`, `last reboot`, `journalctl -g shutdown`).
_READ_PROGS = outbound._TEXT_TOOLS | {"last", "lastb", "journalctl", "dmesg"}
# Ключи ssh со значением (`-p 22`, `-vp 22`, `-oX=Y` несёт значение сам).
_SSH_OPTS = ("BbcDEeFIiJLlmOoPpQRSWw", frozenset())
# `git` командой, а не частью имени или пути (`find /srv/git … -exec rm -rf`, `git-server`).
_GIT_RM_RE = re.compile(r"(?<![\w./=-])(?<!-u\s)git(?:\s+(?:-[Cc]\s+\S+|-(?![Cc](?:\s|$))\S+))*\s+rm\b")
# Своё временное: агент убирает его за собой по правилам, это не карточка.
# После `/tmp/` имя, а не `$D` (пустая у себя в двойных кавычках: весь /tmp), `$(`, точка.
_TMP_PATH_RE = re.compile(r"(?:(?:/var|/private)?/tmp|~/tmp)/[^*?\s$`.]")


def _ssh_target(args: list[str]) -> tuple[str, list[str]]:
    """Адрес и команда для сервера из аргументов ssh; ключи и до, и после адреса."""
    words = ["ssh", *args]
    i = outbound.skip_options("ssh", words, _SSH_OPTS)
    if i >= len(words):
        return "", []
    tail = words[i:]
    rest = tail[outbound.skip_options("ssh", tail, _SSH_OPTS):]
    if not rest:
        # `-o RemoteCommand=…` (и `-oRemoteCommand=…`): команда в ключе.
        rest = [re.split(r"[= ]", w, maxsplit=1)[1] for w in args if re.match(r"(?:-o)?RemoteCommand[= ]", w, re.I)][:1]
    return tail[0], rest


def _irreversible_in(command: str, found: re.Pattern[str], remote: bool, depth: int,
                     executed: str | None = None) -> str:
    """Слово необратимого в программах команды (`_programs` по кускам исполняемого
    текста: тело heredoc для оболочки, `if … then`, обёртки, `su -c`); команда для
    ssh разбирается так же (ревью 12.11.2026). remote: команда уже идёт на сервер
    из списка. `rm` без `-r` в счёт только там (или с именем сервера в тексте) и не
    по своему временному: местная уборка не карточка."""
    executed = outbound.executed_text(command) if executed is None else executed
    if len(executed) > _LEX_BUDGET // 2:
        # Разбор остановился бы на бюджете и хвост не проверил: слово по всему тексту.
        m = _IRREVERSIBLE_RE.search(executed) or _SQL_RE.search((executed))
        return " ".join(m.group(0).split())[:60] if m else ""
    progs = _programs(executed)
    docs = outbound._shell_scan(command)[2] if "<<" in command else []
    line_cache: dict = {}
    openers = {doc: _heredoc_opener(command, doc, line_cache) for doc in docs}
    # `ssh h sudo bash -s <<EOF`: тело heredoc приходит отдельными командами, оно идёт на сервер.
    remote = remote or any(found.search(dest) and (not rest or _runs_shell(rest, depth))
                           for name, args in progs if name == "ssh" for dest, rest in [_ssh_target(args)])
    if re.search(_SQL_CLIENTS, " ".join(shlex.join([n, *a]) for n, a in progs if n not in _READ_PROGS)):
        # SQL на стандартный ввод клиента (`echo "DROP TABLE t" | psql`, `psql <<< …`): явные слова
        # в тексте echo и printf и в здесь-строке.
        # echo в исполняемом тексте отброшен: аргументы берутся из самой команды.
        fed = [shlex.join(a) for n, a in _programs(command) if n in ("echo", "printf")]
        fed += re.findall(r"<<<\s*(.{0,1000})", executed)
        m = _SQL_STDIN_RE.search("\n".join(fed))
        if m:
            return " ".join(m.group(0).split())[:60]
        # Тело heredoc для клиента SQL (`psql <<EOF`, и через ssh) или в файл .sql: целиком,
        # `WHERE` бывает строкой ниже. Тело для git, python, заметок не в счёт.
        for doc in docs:
            if re.search(_SQL_CLIENTS + r"|\.sql\b", openers[doc]):
                m = _SQL_RE.search(doc.body)
                if m:
                    return " ".join(m.group(0).split())[:60]
    if depth < 3:
        # Heredoc в строке `bash -c '…'` и в теле для оболочки: свои тела проверяются там.
        try:
            words = shlex.split(executed)
        except ValueError:
            words = []
        inner = [w for w in words if "<<" in w and "\n" in w and w != command]
        inner += [doc.body for doc in docs
                  if "<<" in doc.body and re.search(r"(?<![\w.-])(?:bash|sh|zsh|dash)(?![\w-])", openers[doc])]
        for text in inner:
            hit = _irreversible_in(text, found, remote, depth + 1)
            if hit:
                return hit
    # `rm -f "$tmp"` после `tmp=$(mktemp)`: своё временное, только эта переменная.
    temp_vars = {f"${v}" for v in re.findall(r"(?<!\w)(\w{1,64})=\$\((?:\S{0,256}/)?mktemp\b", command)}
    temp_vars |= {f"${{{v[1:]}}}" for v in temp_vars}
    has_xargs = bool(re.search(r"\bxargs\b", executed))
    for name, args in progs:
        # Строка тела heredoc для клиента SQL (`UPDATE t SET …`): тело проверено целиком выше.
        if name in _READ_PROGS or name.lower() in _SQL_HEADS:
            continue
        if name == "ssh":
            dest, rest = _ssh_target(args)  # второй раз после признака remote: дёшево, слов мало
            if rest and depth < 3:
                # ssh склеивает аргументы пробелом: так команду видит оболочка сервера.
                hit = _irreversible_in(" ".join(rest), found, remote or bool(found.search(dest)), depth + 1)
                if hit:
                    return hit
            continue
        inside = _container_command(name, args)
        if inside and depth < 3:
            # Команда в контейнере на сервере: как команда для ssh (база и настройки в томах).
            hit = _irreversible_in(shlex.join(inside), found, remote, depth + 1)
            if hit:
                return hit
            continue
        opts = args[:args.index("--")] if "--" in args else args
        if opts and (opts[0] in _HELP or (opts[-1] in _HELP and not opts[-2:-1][0:1] == ["-"]
                                          and not (len(opts) > 1 and opts[-2].startswith("-")))):
            continue  # справка: программа ничего не делает (`-name --help` это значение)
        text = shlex.join([name, *args])
        # SQL только у клиента SQL (`psql -c`, `mysql -e`): строки кода Python и тексты не в счёт.
        m = re.fullmatch(_SQL_CLIENTS, name) and _SQL_RE.search((text))
        if m:
            return " ".join(m.group(0).split())[:60]
        # `git rm` удаляет только восстановимое из git (`find … -exec git rm -r`).
        if name == "rm":
            # После `--` и `-old` это путь.
            paths = [a for a in opts if not a.startswith("-")] + args[len(opts) + 1:]
            # Своё временное и с -r не карточка; без путей (`xargs rm`) пути придут
            # со стандартного ввода, неизвестно какие. Без -r в счёт только на сервере.
            # `xargs rm /tmp/x` допишет пути со стандартного ввода.
            if (paths and not has_xargs
                    and all(".." not in a and (_TMP_PATH_RE.match(a) or a.split("/", 1)[0] in temp_vars)
                            for a in paths)):
                continue
            if _IRREVERSIBLE_RE.search(text) or remote or found.search(text):
                return text[:60]
            continue
        m = _IRREVERSIBLE_RE.search(_GIT_RM_RE.sub("git", text))
        if m:
            return " ".join(m.group(0).split())[:60]
    return ""


def _heredoc_opener(command: str, doc: Any, cache: dict) -> str:
    """Строка с меткой heredoc (переносы `\\` склеены), кусок между `;`, `&&`, `||`
    вне кавычек: `cat <<EOF | ssh h psql` и `ssh h 'psql db || true' <<EOF` это тело для psql."""
    if "nl" not in cache:
        # Неэкранированные переводы строк один раз на команду: перенос `\\` строку не кончает.
        cache["nl"] = [i for i, ch in enumerate(command) if ch == "\n" and not outbound._escaped(command, i)]
    nl = cache["nl"]
    k = bisect.bisect_left(nl, doc.start)
    start = nl[k - 1] + 1 if k else 0
    end = nl[k] if k < len(nl) else len(command)
    seps = cache.get((start, end))
    if seps is None:
        seps, quote = [], ""
        for i in range(start, end):
            ch = command[i]
            if quote:
                quote = "" if ch == quote and (quote == "'" or not outbound._escaped(command, i)) else quote
            elif ch in "'\"" and not outbound._escaped(command, i):
                quote = ch
            elif ch == ";" or command.startswith(("&&", "||"), i):
                seps.append(i)
        cache[(start, end)] = seps
    k = bisect.bisect_left(seps, doc.start)
    lo = seps[k - 1] + 1 if k else start
    hi = seps[k] if k < len(seps) else end
    return command[lo:hi]


# Ключи `docker exec` со значением.
_DOCKER_EXEC_OPTS = ("uwevplm", frozenset({
    "--user", "--workdir", "--env", "--env-file", "--detach-keys", "--volume", "--publish", "--label", "--name",
    "--entrypoint", "--index", "--pull", "--preserve-fds", "--network", "--platform", "--cpus", "--memory",
    "--mount", "--add-host", "--hostname", "--cap-add", "--cap-drop", "--device", "--dns", "--ulimit", "--gpus",
    "--volumes-from", "--log-driver", "--restart", "--pid", "--ipc", "--security-opt"}))


def _container_command(name: str, args: list[str]) -> list[str]:
    """Команда для `docker exec` и `docker compose exec|run` (контейнер, затем команда)."""
    if name not in ("docker", "podman", "docker-compose"):
        return []
    compose = name == "docker-compose" or "compose" in args
    # `docker --context x exec`, `docker container exec`, `docker compose -f x.yml exec|run web …`;
    # `docker run образ …` это новый контейнер из образа, не в счёт.
    at = next((n for n, w in enumerate(args) if w == "exec" or (compose and w == "run") or w == "run"), -1)
    if at >= 0 and (args[at] == "exec" or compose):
        rest = ["x", *args[at + 1:]]
        i = outbound.skip_options("x", rest, _DOCKER_EXEC_OPTS)
        return rest[i + 1:]  # без имени контейнера или службы
    return []


def send_reasons(tool_name: str, tool_input: dict, cwd: str | None, *,
                 telegram_port: int = 8770, client_hosts: tuple[str, ...] = ()) -> set[str]:
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
            found = _bash(str(tool_input.get("command", "")), cwd, telegram_port)
            try:  # сбой здесь не теряет найденную выше отправку
                word = _client_irreversible(str(tool_input.get("command", "")), client_hosts)
            except Exception:
                log.exception("serious: client check failed")
                word = _("проверка не удалась")
            if word:
                found.add(Irreversible(_("необратимое на сервере заказчика: {word}").format(word=word)))
            return found
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


_WRAPPER_VALUE = outbound.WRAPPER_VALUE
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
# Слово оболочки, после которого `((` без пробела арифметика: `for((`, `if((`.
_ARITH_WORDS = frozenset({"for", "if", "elif", "while", "until", "do", "then", "else", "!"})
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
        # Метки heredoc без тела, оставшиеся при закрытии `$(…)`: bash читает
        # их тела за следующим переводом строки снаружи (`$(cat <<E)⏎тело⏎E`).
        self.carry: list[tuple[_Seg, str, bool, bool]] = []

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
        """`$(…)`, `<(…)`, `>(…)` с позиции после скобки; звенья в общий список.
        Метки без тела при закрытии скобки в self.carry (берёт только _parse)."""
        self.carry = []
        if depth > 40:
            level = 1
            while i < self.n and level:
                level += {"(": 1, ")": -1}.get(self.t[i], 0)
                i += 1
            return i
        return self._parse(i, True, depth + 1)

    def _backtick(self, i: int, depth: int) -> int:
        j = outbound._backtick_end(self.t, i)
        self._nested(outbound.unescape_backtick(self.t[i:j]), depth)
        return j + 1

    def _arith(self, i: int, depth: int) -> int | None:
        """Арифметика `((…))` с первой скобки `i`: позиция за `))`; None, если
        это не она (outbound.arith_end). Подстановки внутри (`$(( $(cmd) ))`)
        bash исполняет: их звенья идут в общий список."""
        end = outbound.arith_end(self.t, i, self.budget, slot=1)
        if end is not None:
            self.segs += _Lexer(self.t[i + 2:end - 2], self.budget).subs_only(depth)
        return end

    def _bracket(self, i: int) -> int | None:
        """Конец `$[…]` со скобки `i` (outbound.bracket_end)."""
        return outbound.bracket_end(self.t, i, self.budget, slot=1)

    def _dquote(self, i: int, word: list[str], depth: int) -> int:
        t = self.t
        carry: list[tuple[_Seg, str, bool, bool]] = []
        while i < self.n and t[i] != '"' and not self._spent():
            if t.startswith("\\\n", i):
                # В двойных кавычках `\\` с переводом строки bash убирает: `"$\\⏎(…)"`
                # (а `"\\$\\⏎(…)"` текст).
                dollar = t[i - 1:i] == "$" and not outbound._escaped(t, i - 1)
                i += 2
                if dollar and t.startswith("(", i) and not t.startswith("((", i):
                    word.pop()
                    i = self._sub(i + 1, depth)
                    carry += self.carry
            elif t[i] == "\\" and i + 1 < self.n:
                word.append(t[i + 1])
                i += 2
            elif t.startswith("$((", i) and (end := self._arith(i + 1, depth)) is not None:
                word.append(t[i:end])
                i = end
            elif t.startswith("$(", i):
                i = self._sub(i + 2, depth)
                carry += self.carry
            elif t[i] == "`":
                i = self._backtick(i + 1, depth)
            else:
                word.append(t[i])
                i += 1
        # Метки из `"$(cat <<E)"`: тело за строкой читает _parse.
        self.carry = carry
        return i + 1

    def subs_only(self, depth: int) -> list[_Seg]:
        """Подстановки в тексте как в двойных кавычках (тело heredoc с
        ограничителем без кавычек: оболочка их исполнит и у cat)."""
        t, i = self.t, 0
        while i < self.n and not self._spent():
            if t[i] == "\\":
                i += 2
            elif t.startswith("$((", i) and (end := self._arith(i + 1, depth)) is not None:
                i = end
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
        brace = 0
        pending: list[tuple[_Seg, str, bool, bool]] = []
        # Сколько меток в начале pending перенесено из закрытых `$(…)`: bash
        # читает их тела первыми, в порядке закрытия (ревью Opus 10.11.2026).
        carried = 0
        # Подоболочки `( … )` внутри: их `)` не закрывает `$(…)` (ревью Codex 10.11.2026).
        sub = 0
        # `case`: глубина подоболочек у открытых, ждём `in`, ждём `)` шаблона.
        cases: list[int] = []
        case_in = pattern = False
        pipe_src: list[_Seg | None] = [None]

        def take_carry() -> None:
            nonlocal pending, carried
            pending[carried:carried] = self.carry
            carried += len(self.carry)
            self.carry = []

        def end_word() -> None:
            nonlocal word, has_word, skip_next, case_in, pattern
            if has_word:
                if skip_next:
                    skip_next = False
                else:
                    text = "".join(word)
                    at_command = not seg.words or seg.words[-1] in _SHELL_WORDS
                    if text == "case" and at_command:
                        cases.append(sub)
                        case_in = True
                    elif text == "in" and case_in:
                        case_in, pattern = False, True
                    elif text == "esac" and at_command and cases and cases[-1] == sub:
                        cases.pop()
                        pattern = False
                    seg.words.append(text)
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
                # Тело как в bash (outbound.heredoc_body): пустое, до конца
                # текста без ограничителя, `E)` закрывает его внутри `$(…)`.
                for k, (hseg, delim, strip, quoted) in enumerate(pending):
                    body, i = outbound.heredoc_body(t, i, delim, strip, quoted, nested=closer)
                    hseg.bodies.append((body, quoted))
                    if i == n or t[i - 1] != "\n":
                        # Оборвалось посреди строки: у остальных меток тела нет.
                        for rest, _d, _s, rest_quoted in pending[k + 1:]:
                            rest.bodies.append(("", rest_quoted))
                        break
                pending, carried = [], 0
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
            elif t.startswith("$((", i) and (e := self._arith(i + 1, depth)) is not None:
                word.append(t[i:e])
                has_word = True
                i = e
            elif t.startswith("((", i) and (not has_word or word[-1:] == ["$"]
                                             or "".join(word) in _ARITH_WORDS) \
                    and (e := self._arith(i, depth)) is not None:
                # Арифметическая команда `((x<<2))`, `for ((…))`, `for((…))` и
                # `$((`, разорванная переносом `\`: сдвиг, а не heredoc.
                if word[-1:] == ["$"]:
                    word.append(t[i:e])
                else:
                    end_word()
                i = e
            elif t.startswith("$[", i) and (e := self._bracket(i + 1)) is not None:
                self.segs += _Lexer(t[i + 2:e - 1], self.budget).subs_only(depth)
                word.append(t[i:e])
                has_word = True
                i = e
            elif t.startswith("${", i):
                # Внутри `${…}` `<<` не heredoc (`${x:-a<<b}`).
                brace += 1
                word.append("${")
                has_word = True
                i += 2
            elif t.startswith("$(", i):
                i = self._sub(i + 2, depth)
                take_carry()
                word.append("$(…)")
                has_word = True
            elif c == "`":
                i = self._backtick(i + 1, depth)
                word.append("`…`")
                has_word = True
            elif c == '"':
                i = self._dquote(i + 1, word, depth)
                take_carry()
                has_word = True
            elif c in "<>" and t.startswith("(", i + 1):
                end_word()
                i = self._sub(i + 2, depth)
                take_carry()
            elif t.startswith("<<<", i):
                fd_word()
                end_word()
                skip_next = True
                i += 3
            elif t.startswith("<<", i) and not brace and (
                    hw := outbound.heredoc_word(t, i + 2 + t.startswith("-", i + 2))) is not None:
                # Метка как в bash (outbound.heredoc_word); без слова это не heredoc.
                fd_word()
                end_word()
                delim, quoted, e = hw
                pending.append((seg, delim, t.startswith("-", i + 2), quoted))
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
                if t[i:e] in (";;", ";&", ";;&") and cases and cases[-1] == sub:
                    pattern = True
                end_seg(pipe=t[i:e] in ("|", "|&"))
                i = e
            elif c == "(" and word[-1:] == ["$"]:
                # `$` и `(`, разорванные переносом `\`: это `$(…)` (сверка с bash 10.11.2026).
                word[-1] = "$(…)"
                i = self._sub(i + 1, depth)
                take_carry()
            elif c == "(" and pattern and cases[-1] == sub and not has_word:
                i += 1  # `(x)` шаблона case
            elif c == "(":
                end_seg()
                sub += 1
                i += 1
            elif c == ")" and pattern and cases and cases[-1] == sub:
                # `x)` шаблона case: слова шаблона не программа, `$(…)` не закрыт.
                end_word()
                seg.words = []
                pattern = False
                i += 1
            elif c == ")":
                end_seg()
                i += 1
                if sub:
                    sub -= 1
                elif closer:
                    self.carry = pending
                    return i
            else:
                if c == "}" and brace:
                    brace -= 1
                word.append(c)
                has_word = True
                i += 1
        end_seg()
        return i


_skip_options = outbound.skip_options


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
    # Шаги лексера и знаки поиска пар скобок (outbound.ARITH_BUDGET).
    budget = [_LEX_BUDGET, outbound.ARITH_BUDGET]
    segs = _Lexer(command, budget).lex()
    for seg in list(segs):
        # Один раз на звено, а не на каждое тело (много меток в строке).
        shell = bool(seg.bodies) and depth < 3 and (
            _runs_shell(seg.words, depth) or (seg.next is not None and _runs_shell(seg.next.words, depth)))
        for body, quoted in seg.bodies:
            if shell:
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

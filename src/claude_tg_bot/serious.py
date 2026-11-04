"""Опасное действие: на что бот даёт карточку при модели «разрешено всё,
кроме списка» (решение Ивана 03.11.2026: «карточка на опасное действие, а
то, что я прямо попросил сделать, на это карточка не нужна»).

Пять классов, остальное без карточки, в том числе неразобранное:
1) серьёзное удаление, которое не вернуть: каталог целиком вне временных
   мест и git-репозиториев, диски, базы, затирание истории на сервере git и
   незакоммиченных правок, удаление насовсем во внешнем сервисе;
2) отправка другим людям (карточка на каждую, outbound.py), в том числе
   запросом к локальному серверу Telegram мимо неё, публикация, загрузка
   файла на сайт;
3) секрет в сеть;
4) оплата и покупка;
5) команды на серверах заказчиков (известное чтение без карточки, всё
   прочее карточкой изменения с пояснением агента мимо окна ⏩), правка
   защиты бота и вложенный агент без хука бота.

Решения Ивана по аудиту guard3 (docs/audit-2026-11/guard3-decisions.md):
чтение секрета в живом чате и публикация через MCP без слов send, share,
publish остаются без карточки (принятый риск); правила Н1, Н3, Н4, Н11 узкие
и шире не делаются, у каждого есть тест разрешённой стороны в
tests/test_guard3.py.

Это эвристика, не граница: что сделает скрипт изнутри (`python job.py`) или
клик в браузере, разбор не видит (ревью Codex 03.11.2026, принято Иваном).
"""
from __future__ import annotations

import fnmatch
import functools
import ipaddress
import logging
import os
import re
import shlex
import socket
import contextvars
import subprocess
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import danger, outbound
from .i18n import _

log = logging.getLogger(__name__)

_HOME = Path.home()
BOT_ROOT = Path(__file__).resolve().parents[2]
FREE_ROOTS = (Path("/tmp"), _HOME / "tmp", _HOME / ".cache",
              # Временные папки на стороне Windows (WSL): ~/tmp и Temp пользователя.
              Path("/mnt/c/Users") / _HOME.name / "tmp",
              Path("/mnt/c/Users") / _HOME.name / "AppData" / "Local" / "Temp")
# Модули, которые решают, нужна ли карточка, и как запускается агент; правка
# остального кода бота опасной не считается (Иван 03.11.2026: 11 карточек на
# правку bot.py в одной задаче это те самые лишние). Список узкий (решение
# Ивана по guard3, Н3): `hook_approve` и всё, что он импортирует (хук это
# новый процесс на каждый вызов, правка срабатывает сразу), `approval`,
# `serious`, `outbound` с их импортами и `tg_cards` (выбор на карточке
# плана). Полноту сверяет tests/test_guard3.py по sys.modules. bot.py,
# voice.py, tg_events.py и прочие модули бота правятся без карточки.
GUARD_MODULES = ("__init__.py", "hook_approve.py", "config.py", "i18n.py", "warm.py", "skills/__init__.py",
                 "skills/_oneshot.py", "approval.py", "audit.py", "auth.py", "burst.py", "claude_runner.py",
                 "danger.py", "events.py", "limits.py", "mailsend.py", "otklik.py", "outbound.py",
                 "reactions.py", "replies.py", "runs.py", "serious.py", "untrusted.py", "tg_cards.py")


def _with_real(paths: Iterable[Path]) -> tuple[Path, ...]:
    out: list[Path] = []
    for p in paths:
        out.append(p)
        try:
            real = p.resolve()
        except (OSError, RuntimeError):
            continue
        if real != p:
            out.append(real)
    return tuple(out)


# Защита бота: модули проверки, окружение, ключи и хук, настройки Claude
# Code, где хук прописан. Запуск модулей бота правкой не считается.
# Состояние, по которому бот решает: билеты писем, отложенные раны, метки
# сессий (ревью Opus 03.11.2026).
GUARD_STATE = ("sent_texts.jsonl", "audit.jsonl", "at_jobs.json", "sessions.json",
               "foreign_sessions.json", "untrusted_sessions.json", "deal_sessions.json", "scopes.json",
               "session_scopes.json")
GUARD_DIRS = _with_real((BOT_ROOT / ".venv", _HOME / ".claude" / "hooks", BOT_ROOT / "state" / "mailsend",
                         BOT_ROOT / "state" / "events", BOT_ROOT / "state" / "handoff",
                         # Глобальный контракт всех сессий: меняется только с подтверждения Ивана.
                         _HOME / ".agent-memory" / "global"))
GUARD_FILES = _with_real((BOT_ROOT / "claude-hook-settings.json", _HOME / ".claude" / "settings.json",
                          _HOME / ".claude" / "settings.local.json", _HOME / ".claude.json",
                          *(BOT_ROOT / "src" / "claude_tg_bot" / m for m in GUARD_MODULES),
                          *(BOT_ROOT / "state" / f for f in GUARD_STATE)))

# Программы, которые по названному пути только читают или запускают его.
_READERS = frozenset({
    "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "zgrep", "rg", "diff", "cmp",
    "wc", "ls", "stat", "file", "sha256sum", "sha1sum", "md5sum", "du", "tree", "realpath",
    "readlink", "basename", "dirname", "test", "[", "echo", "printf", "jq", "bat", "awk",
    "python", "python3", "pytest", "bash", "sh", "node", "timeout", "nice", "env", "cd", "pushd",
    "find", "xxd", "od", "strings", "column", "sort", "uniq", "cut", "nl", "tac", "rtk",
})
_GIT_READ = frozenset({"diff", "log", "show", "status", "blame", "grep", "ls-files", "rev-parse",
                       "describe", "shortlog", "ls-tree", "cat-file", "add", "commit", "fetch",
                       "branch", "tag", "stash", "worktree", "remote", "config", "check-ignore"})

_MCP_DELETE = frozenset({"delete", "remove", "purge", "destroy", "wipe", "empty"})
# Обратимое: событие календаря уходит в корзину, черновик и метка свои.
_MCP_DELETE_OK_RE = re.compile(r"(?:delete_event|delete_draft|delete_label|remove[-_]labels?|unlabel_\w+)",
                               re.IGNORECASE)
_MCP_MONEY = frozenset({"pay", "payment", "purchase", "checkout", "buy", "order", "transfer"})
_MCP_MONEY_DOC = frozenset({"refund", "refunds", "invoice", "invoices"})
_DISK_PROGS = frozenset({"mkfs", "wipefs", "fdisk", "sfdisk", "parted", "sgdisk", "shred", "blkdiscard"})
_GLOB_RE = re.compile(r"(?<!\\)[*?\[]")
_VAR_RE = re.compile(r"\$\{?(\w+)\}?")
# Локальный адрес сервера Telegram: `127.0.0.1`, `127.1`, `localhost`, `localhost.`,
# `[::1]`, `[::ffff:127.0.0.1]`, `0.0.0.0` и `0` в месте адреса (`0:8770`,
# `/dev/tcp/0/8770`, `"0"`). Защита от ошибки агента, не от намеренного обхода
# (`0x7f000001`, переменные): решение Ивана 03.11.2026, README.
_LOOPBACK_RE = re.compile(
    r"(?<![\w.])(?:127(?:\.\d+){1,3}|(?:ip6-)?localhost\.?|0\.0\.0\.0|\[?::1\]?"
    r"|0(?=:\d|/\d)|(?<=['\"])0(?=['\"]))(?![\w.])", re.IGNORECASE)
_SECRET_VAR_RE = re.compile(r"\$\{?\w*(?:TOKEN|SECRET|PASSW|API_?KEY|PRIVATE)\w*|\$\(\s*cat\s+\S*(?:token|secret|\.env|key)",
                            re.IGNORECASE)


# Сервер заказчика (проект 03.11.2026, п. 6): изменение идёт карточкой с
# пояснением агента, окно ⏩ его не пропускает; известное чтение без карточки
# и причины не даёт (решение Ивана 03.11.2026 вечером). Вид причины задаёт её
# тип: reasons() отдаёт те же строки.
class CustomerChange(str):
    pass


# Отправка людям мимо карточки отправки (сервер Telegram на loopback, внешний
# сервис): как отправка, окно ⏩ её не пропускает, из фонового рана отказ.
class Outbound(str):
    pass


def customer_kind(why: Iterable[str]) -> str:
    """Вид карточки сервера заказчика: «change» или пусто."""
    return "change" if any(isinstance(w, CustomerChange) for w in why) else ""


def sends(why: Iterable[str]) -> bool:
    """Среди причин отправка людям (Outbound)."""
    return any(isinstance(w, Outbound) for w in why)


def reasons(tool_name: str, tool_input: dict, cwd: str | None, *,
            customer_hosts: tuple[str, ...] = (),
            telegram_port: int = 8770) -> set[str]:
    """Почему действие идёт карточкой; пусто: без карточки. Отправка от имени
    Ивана (MCP send/reply) решается в approval, здесь её нет."""
    try:
        if tool_name.startswith("mcp__"):
            found = _mcp(tool_name)
            method = str(tool_input.get("method") or tool_input.get("http_method") or "").upper()
            operation = str(tool_input.get("operation") or tool_input.get("action") or "").lower()
            if method == "DELETE" or operation in ("delete", "remove", "purge", "destroy"):
                found.add(_("удаление во внешнем сервисе"))
            return found
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
            return {_("правка защиты бота")} if path and _guard(_abs(path, cwd)) else set()
        if tool_name in ("Bash", "Monitor"):
            return _bash(str(tool_input.get("command", "")), cwd, customer_hosts, telegram_port)
        if tool_name == "WebFetch":
            return _webfetch(str(tool_input.get("url", "")), telegram_port)
    except Exception:
        log.exception("serious: check failed tool=%s", tool_name)
        return {_("проверка не удалась")}
    return set()


def _mcp(tool_name: str) -> set[str]:
    short = tool_name.split("__", 2)[-1]
    words = set(re.split(r"[_\-]+", re.sub(r"(?<=[a-z])(?=[A-Z])", "_", short).lower()))
    out: set[str] = set()
    # Возврат денег и счёт (guard3, Н9): чтение их (`get_invoice`, `list_refunds`) не оплата.
    if words & _MCP_MONEY or (words & _MCP_MONEY_DOC and not words & _MCP_READ_WORDS):
        out.add(_("оплата или покупка"))
    if words & _MCP_DELETE and not _MCP_DELETE_OK_RE.fullmatch(short):
        out.add(_("удаление во внешнем сервисе"))
    if words & {"share", "invite", "publish"}:
        out.add(_("публикация или доступ другим людям"))
    # Отправка людям через MCP, которых нет в карточке отправки approval
    # (комментарий GitHub и Jira, письмо HubSpot, ответ Intercom). Свой Todoist
    # и календарь не в счёт (ревью Opus 03.11.2026).
    server = tool_name.split("__", 2)[1].lower() if tool_name.count("__") >= 2 else ""
    own = any(w in server for w in ("todoist", "calendar"))
    if not own and (words & {"send", "reply", "post", "comment", "forward", "email", "message", "respond"}
                    or (words & {"create", "add"} and words & {"issue", "pull", "pr", "ticket", "conversation"})):
        if not words & _MCP_READ_WORDS:
            out.add(Outbound(_("отправка людям через внешний сервис")))
    return out


_MCP_READ_WORDS = frozenset({"get", "list", "search", "read", "fetch", "find", "view"})
# Адрес WebFetch: облачные метаданные по имени, ключ известного вида. AKIA
# (идентификатор ключа AWS) не секрет: он стоит в подписанных ссылках S3.
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "metadata.azure.com", "instance-data",
                             "instance-data.ec2.internal"})
_KEY_SHAPE_RE = re.compile(
    r"\d{6,12}:[\w-]{30,}|\bsk-(?=[\w-]*\d)[\w-]{20,}|\bgh[pousr]_\w{30,}|\bgithub_pat_\w{30,}"
    r"|\bAIza[\w-]{30,}|\bxox[abprs]-[\w-]{10,}|\bglpat-[\w-]{20,}")
# Сокращённая запись IPv4 (`127.1`, `2130706433`, `0x7f000001`): её разбирает
# inet_aton, как и сам клиент.
_SHORT_IPV4_RE = re.compile(r"(?:0x[0-9a-f]*|\d+)(?:\.(?:0x[0-9a-f]*|\d+)){0,3}")


def _webfetch(url: str, telegram_port: int) -> set[str]:
    """WebFetch (guard3, Н8): loopback с портом сервера Telegram как отправка,
    прочий loopback, IP-литерал, link-local и метаданные облака причиной
    «запрос во внутреннюю сеть»; ключ известного вида в адресе: секрет в сеть.
    Длинная строка без вида ключа (fbclid, id Google Drive, курсор) не в счёт."""
    out: set[str] = set()
    try:
        u = urlsplit(url.strip())
        host = (u.hostname or "").lower().rstrip(".")
        port = u.port
    except ValueError:
        return {_("запрос во внутреннюю сеть")}
    ip = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if _SHORT_IPV4_RE.fullmatch(host):
            try:
                ip = ipaddress.ip_address(socket.inet_aton(host))
            except (OSError, ValueError):
                out.add(_("запрос во внутреннюю сеть"))
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # Имя loopback только localhost, localhost.localdomain и *.localhost
    # (RFC 6761); `localhost.example.com` обычный внешний адрес.
    loopback = host in ("localhost", "localhost.localdomain") or host.endswith(".localhost") or bool(
        ip and (ip.is_loopback or ip.is_unspecified))
    if loopback and telegram_port and port == telegram_port:
        out.add(Outbound(_("отправка через сервер Telegram мимо проверки")))
    elif loopback or ip is not None or host in _METADATA_HOSTS:
        out.add(_("запрос во внутреннюю сеть"))
    # Ключ известного вида в любом месте адреса: в пути, запросе, якоре,
    # имени и пароле, в том числе в %-записи.
    if _KEY_SHAPE_RE.search(url) or _KEY_SHAPE_RE.search(unquote(url)):
        out.add(_("секрет уходит в сеть"))
    return out


# --- Пути --------------------------------------------------------------------

def _abs(word: str, cwd: str | None) -> str | None:
    path = os.path.expanduser(danger._HOME_VAR_RE.sub(lambda _m: str(_HOME), word))
    if not path.startswith("/"):
        if not cwd:
            return None
        path = os.path.join(cwd, path)
    # Ссылки и `..` в каталоге раскрываются, сама последняя часть нет:
    # `rm ссылка` удаляет ссылку, а не цель. С `/` на конце (`rm -r ссылка/`)
    # удаляется цель: путь раскрывается целиком.
    if path.endswith("/"):
        try:
            return os.path.realpath(path)
        except OSError:
            return os.path.normpath(path)
    head, tail = os.path.split(os.path.normpath(path))
    try:
        head = os.path.realpath(head)
    except OSError:
        pass
    return os.path.join(head, tail) if tail else head


def _under(path: str, roots: Iterable[Path]) -> bool:
    p = Path(path)
    return any(p == r or r in p.parents for r in roots)


def _guard(path: str | None, n11: bool = True) -> bool:
    """Путь защиты. n11=False: без настроек Н11 (`.mcp.json`, `.codex`,
    `~/.ssh/config`, юниты): они защита только при записи (ревью PR 10)."""
    if path is None:
        return False
    p = Path(path)
    try:
        real = Path(os.path.realpath(path))
    except OSError:
        real = p
    return any(_guard_one(q) or (n11 and _n11(q)) for q in (p, real))


# Юниты systemd бота и telegram-mcp: по юниту telegram-mcp бот сверяет корни send_file.
_GUARD_UNITS_DIR = _HOME / ".config" / "systemd" / "user"


def _n11(p: Path) -> bool:
    """Настройки, которые исполнятся при следующем запуске мимо хука (guard3,
    Н11, узко): MCP-серверы и Codex проекта (сам каталог `.codex` тоже),
    ~/.ssh/config (по нему бот раскрывает хост заказчика), юниты бота и
    telegram-mcp с дополнениями `*.service.d`. Прочее из danger._PROTECTED_*
    (~/.bashrc, ~/.gitconfig, ~/.claude/CLAUDE.md, .git/hooks, сам ~/.codex)
    без карточки: решение Ивана, обычная работа."""
    if p.name == ".mcp.json" or p == _HOME / ".ssh" / "config" or (p.name == ".codex" and p.parent != _HOME) or any(
            part == ".codex" and Path(*p.parts[:i]) != _HOME for i, part in enumerate(p.parts[:-1])):
        return True
    if p.parent == _GUARD_UNITS_DIR and (fnmatch.fnmatch(p.name, "claude-tg-bot*.service")
                                         or p.name == "telegram-mcp.service"):
        return True
    # Дополнения юнитов и сам их каталог (`claude-tg-bot.service.d/x.conf`).
    return any(d.parent == _GUARD_UNITS_DIR and (fnmatch.fnmatch(d.name, "claude-tg-bot*.service.d")
                                                 or d.name == "telegram-mcp.service.d") for d in (p, p.parent))


def _guard_one(p: Path) -> bool:
    if p.parent == BOT_ROOT and p.name.startswith(".env"):
        return True
    # Настройки Claude Code любого проекта: `disableAllHooks` снял бы хук бота.
    if p.parent.name == ".claude" and p.name in ("settings.json", "settings.local.json"):
        return True
    # Копия пакета `claude_tg_bot` вне каталога бота защитой не считается:
    # подмену из каталога сессии закрывает `python -P` у хука, а правило давало
    # карточки на копии бота в ~/tmp (ревью PR 10).
    if _shadows_guard(p):
        return True
    return p in GUARD_FILES or _under(str(p), GUARD_DIRS)


def _shadows_guard(p: Path) -> bool:
    """Подмена модуля защиты пакетом-каталогом (`config/__init__.py` рядом с
    `config.py` грузится первым) или байткодом в `__pycache__` внутри пакета
    бота (ревью PR 10)."""
    for root in {BOT_ROOT, BOT_ROOT.resolve()}:
        pkg = root / "src" / "claude_tg_bot"
        if pkg not in p.parents:
            continue
        rel = p.relative_to(pkg).parts
        if "__pycache__" in rel[:-1]:
            return True
        for m in GUARD_MODULES:
            stem = Path(m).with_suffix("").parts
            if m != "__init__.py" and len(rel) > len(stem) and rel[:len(stem)] == stem:
                return True
    return False


def _holds_guard(path: str | None) -> bool:
    """Каталог, внутри которого лежит защита: `rm -rf src/claude_tg_bot`."""
    if path is None:
        return False
    p = Path(path)
    return any(p in g.parents for g in (*GUARD_FILES, *GUARD_DIRS))


def _in_repo(path: str) -> bool:
    """Путь внутри рабочей копии git, но не сама рабочая копия и не каталог,
    где лежит чужая `.git` (вложенный репозиторий удаляется целиком)."""
    p = Path(path)
    if (p / ".git").exists() or ".git" in p.parts:
        return False
    return any((parent / ".git").exists() for parent in p.parents)


# Кэши и окружения пересобираются: их удаление ничего не теряет (ревью Opus 03.11.2026).
_REBUILT = frozenset({".pytest_cache", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
                      ".ruff_cache", "build", "dist", ".tox", ".cache"})


def _repo_keeps(path: str) -> bool:
    """Всё под путём есть в git: нет неотслеживаемого, игнорируемого (данные,
    `.env`) и незакоммиченного. Не вышло проверить: не хранит."""
    p = Path(path)
    if p.name in _REBUILT:
        return True
    base = p if p.is_dir() else p.parent
    out = _git_safe(str(base), "status", "--porcelain", "--ignored", "--", str(p))
    if out is None:
        return False
    # Кэши и окружения внутри (`__pycache__`, `.venv`) не потеря.
    lost = [ln for ln in out.splitlines() if ln.strip()
            and not set(Path(ln[3:].strip().strip('"')).parts) & _REBUILT]
    return not lost


def _recoverable(path: str | None) -> bool:
    if path is None:
        return False
    return _under(path, FREE_ROOTS) or _in_repo(path)


# --- Bash --------------------------------------------------------------------

# Все значения каждой переменной команды: неизвестная переменная, хоть раз
# названная путём защиты, считается путём защиты (ревью Codex 03.11.2026).
_ALL_VALUES: contextvars.ContextVar[dict[str, list[str]]] = contextvars.ContextVar("serious_values", default={})
_WRAPPERS = frozenset({"timeout", "nice", "nohup", "ionice", "stdbuf", "exec", "command", "time", "sudo", "env"})


def _steps(verdict: danger.Verdict, cwd: str | None, nested: bool = False,
           values: dict[str, str] | None = None) -> Iterator[tuple[str, list[str], str | None]]:
    """Программы с их каталогом: cd и pushd меняют его следующим звеньям,
    `env -C` и `sudo -D` следующей программе. В подоболочке и `bash -c` (nested)
    после cd каталог не известен: разбор не видит, где кончается `( … )`
    (ревью Codex 03.11.2026)."""
    once: str | None = None
    prev: list[str] = []
    for prog, args in verdict.programs:
        # Внутренняя программа обёртки (`env -C d rtk proxy rm …`) идёт следом
        # с хвостом её аргументов: каталог `env -C` относится и к ней.
        inner = bool(prev) and [prog, *args] == prev[len(prev) - len(args) - 1:]
        if not inner and prog not in _WRAPPERS:
            once = None
        yield prog, args, once or cwd
        prev = list(args)
        if prog in ("cd", "pushd"):
            target = next((a for a in args if a == "-" or not a.startswith("-")), "~")
            target = _expand(target, values or {}) if "$" in target else target
            cwd = None if target in ("-", None) or nested else _abs(target, cwd)
        elif prog in ("env", "sudo"):
            flag = ("-C", "--chdir") if prog == "env" else ("-D", "--chdir")
            for i, a in enumerate(args):
                if a in flag and i + 1 < len(args):
                    once = _abs(args[i + 1], cwd)
                elif a.startswith(flag[1] + "="):
                    once = _abs(a.split("=", 1)[1], cwd)


def _assignments(verdict: danger.Verdict, command: str = "") -> dict[str, str]:
    """Значения простых присваиваний. Переменная, присвоенная дважды или
    подстановкой `$(…)` (её значение разбор не хранит), в словарь не попадает:
    она неизвестна. Исключение `t=$(mktemp …)`: временный файл в /tmp."""
    out: dict[str, str] = {}
    seen: set[str] = set()
    for prog, args in verdict.programs:
        pairs = [args[0]] if prog == "=" and args else [a for a in args if "=" in a and not a.startswith("-")] \
            if prog in ("export", "local", "declare", "readonly") else []
        for a in pairs:
            name, _eq, value = a.partition("=")
            if name in seen:
                out.pop(name, None)
                continue
            seen.add(name)
            out[name] = value
    # Переменная цикла `for X in …` принимает значения, которых разбор не видит.
    loops = re.findall(r"\bfor\s+(\w+)\s+in\s+([^;\n]*)", command)
    for name, _items in loops:
        out.pop(name, None)
        seen.add(name)
    every: dict[str, list[str]] = {}
    for prog, args in verdict.programs:
        pairs = [args[0]] if prog == "=" and args else [a for a in args if "=" in a and not a.startswith("-")] \
            if prog in ("export", "local", "declare", "readonly") else []
        for a in pairs:
            every.setdefault(a.partition("=")[0], []).append(a.partition("=")[2])
    for name, items in loops:
        every.setdefault(name, []).extend(os.path.expanduser(w) for w in items.split())
    _ALL_VALUES.set(every)
    temp = set(re.findall(r"\b(\w+)=\$\(\s*mktemp\b", command))
    return {k: "/tmp/mktemp" if k in temp else v for k, v in out.items()
            if k in temp or (v != "$" and not v.startswith("$("))}


def _expand(word: str, values: dict[str, str]) -> str | None:
    """Простые `$X` из присваиваний той же команды; неизвестная: None."""
    missing = False

    def sub(m: re.Match[str]) -> str:
        nonlocal missing
        if m.group(1) == "HOME":
            return str(_HOME)
        if m.group(1) in values:
            return values[m.group(1)]
        missing = True
        return ""

    out = _VAR_RE.sub(sub, word)
    return None if missing or "$(" in out or "`" in out else out


def _bash(command: str, cwd: str | None, customer_hosts: tuple[str, ...], telegram_port: int = 0) -> set[str]:
    # `$(which rm) -rf …`: программа из подстановки поиска это она сама (guard3, Н7).
    command = _SUBST_PROG_RE.sub(lambda m: m.group(1) or m.group(2), command)
    verdict = danger.classify("Bash", {"command": command}, cwd)
    values = _assignments(verdict, command)
    out: set[str] = set()
    masked = danger._quote_mask(command)
    nested = bool(re.search(r"(?<![$<>])\(|\b(?:ba|z|da)?sh\s+-\w*c\b|\beval\b", masked)) or bool(
        re.search(r"\$\([^)]*\bcd\b|`[^`]*\bcd\b", masked))
    _collect_moves(verdict, cwd, nested, values)
    ps_step = False
    for n, (prog, args, step_cwd) in enumerate(_steps(verdict, cwd, nested, values)):
        head = verdict.heads[n] if n < len(verdict.heads) else prog
        prog = _no_exe(prog)
        _delete(prog, args, step_cwd, values, out, n)
        if prog == "git":
            _git(args, step_cwd, command, out)
        elif prog in ("ssh", "autossh", "scp", "sftp", "rsync", "sshpass", "plink", "plink.exe"):
            rprog, rargs = prog, args
            if prog == "sshpass":
                # `sshpass -p x ssh host …`: дальше та же программа ssh.
                inner = next((i for i, a in enumerate(args)
                              if _no_exe(a) in ("ssh", "autossh", "scp", "sftp", "rsync")
                              and not (i and args[i - 1] in ("-p", "-f", "-d", "-P"))), None)
                rprog, rargs = (_no_exe(args[inner]), args[inner + 1:]) if inner is not None else ("", [])
            if rprog in ("ssh", "autossh"):
                _remote_delete(rargs, out)
            if rprog:
                _remote(rprog, [_VAR_RE.sub(lambda m: values.get(m.group(1), m.group(0)), a) for a in rargs],
                        customer_hosts, out, command, step_cwd)
        elif prog == "xargs" and customer_hosts and [_no_exe(a) for a in _first_pos(
                args, _WRAP_VALUE["xargs"])[:1]] in (["ssh"], ["autossh"], ["sshpass"], ["plink"]):
            # Хост из потока (`… | xargs ssh`): неизвестен, считается заказчика.
            out.add(CustomerChange(_("команда ssh по списку из потока")))
        elif prog.lower() in _PS_PROGS and customer_hosts:
            ps_step = _powershell(args, step_cwd, customer_hosts, out) or ps_step
        elif prog.lower() in ("cmd", "cmd.exe") and customer_hosts:
            # `cmd.exe /c ssh dc …`: разбор не раскрывает cmd, команда после `/c` разбирается отдельно.
            rest = next((args[j + 1:] for j, a in enumerate(args) if a.lower() in ("/c", "/k")), None)
            if rest:
                # Свой контекст: вложенный разбор не затирает переносы и
                # переменные внешней команды (ревью PR 10).
                out.update(w for w in contextvars.copy_context().run(_bash, " ".join(rest), step_cwd, customer_hosts)
                           if customer_kind([w]))
        elif prog in ("telnet", "ssh-copy-id") and customer_hosts:
            # У ssh-copy-id хост последним (`-i` бывает без файла), у telnet первым.
            pos = [a for a in args if not a.startswith("-")] if prog == "ssh-copy-id" else _first_pos(
                args, {"-l", "-b", "-e", "-n"})
            host = (pos[-1] if prog == "ssh-copy-id" else pos[0]) if pos else ""
            # Ключ ssh-copy-id дописывает на сервер; telnet с вводом (конвейер,
            # heredoc, файл) шлёт команды серверу. telnet без ввода: проверка порта.
            if host and _host_matches(host, customer_hosts) and (
                    prog == "ssh-copy-id" or _piped_into(command, host, "telnet")
                    or _stdin_text(command, step_cwd) != ""):
                out.add(_customer_reason(host))
        elif prog in ("ansible", "ansible-playbook"):
            # Хост заказчика в целях или `-i`: его класс в любом каталоге;
            # иначе прежнее правило по каталогу src-work.
            if not (customer_hosts and _ansible(prog, args, step_cwd, customer_hosts, out)) and not (
                    {"--check", "-C", "--list-hosts", "--list-tasks", "--syntax-check"} & set(args)) and (
                    any("src-work" in (_abs(a, step_cwd) or "") for a in args) or "src-work" in (step_cwd or "")):
                out.add(_("изменения на серверах заказчика (ansible)"))
        elif prog == "doctl" and danger._cloud_words(args) and not danger._cloud_read_verb(
                danger._cloud_words(args)[-1]):
            out.add(_("изменения в облаке заказчика (doctl)"))
        elif prog in ("curl", "wget"):
            # `V=$(cat .env); curl -d "$V"`: значение подстановки разбор не хранит,
            # секретом считается переменная из подстановки в команде, читающей секрет.
            secret_vars = {k for k, v in values.items() if v.startswith("$")} if verdict.secret else set()
            _upload([_VAR_RE.sub(lambda m: "$SECRET_" if m.group(1) in secret_vars
                                 else values.get(m.group(1), m.group(0)), a) for a in args], out,
                    secret_cmd=verdict.secret, raw=args)
            # Запрос по адресу хоста заказчика: исполняет его веб-сервер (REST RouterOS).
            # Адрес и ключом `--url=URL` (`--url URL` уже отдельное слово).
            urls = [a.split("=", 1)[1] if a.startswith("--url=") else a for a in args
                    if not a.startswith("-") or a.startswith("--url=")]
            host = next((h for a in urls if (h := _url_host(a)) and any(
                fnmatch.fnmatch(h.lower(), p.lower()) for p in customer_hosts)), "")
            if host and not _prog_read(prog, args):
                out.add(_customer_reason(host))
        elif prog == "gh":
            _gh(args, out)
        elif prog in ("claude", "codex") and _nested_agent(prog, args):
            out.add(_("вложенный агент без защиты бота"))
        elif prog == "mosh" and customer_hosts:
            _mosh(args, customer_hosts, out)
        if prog in ("curl", "wget") and _hidden_body_secret(prog, args, step_cwd):
            out.add(_("секрет уходит в сеть"))
        # Окружение бота: установка `.venv/bin/pip` и `uv pip` в каталоге бота,
        # в том числе после `cd` в той же команде (guard3, Н4).
        if _pip_install(prog, args) and _bot_env(prog, head, args, step_cwd, values):
            out.add(_("ставит пакеты в окружение бота"))
        _guard_step(prog, args, step_cwd, out, values)
    # Признак секрета в команде с сетевой программой (guard3, Н5, решение
    # Ивана: дешёвое правило без сверки «свой сервис для ключа»).
    if verdict.secret and _secret_to_net(command, verdict, cwd):
        out.add(_("секрет уходит в сеть"))
    # Глобальный контракт всех сессий и манифест памяти меняются только с
    # подтверждения Ивана (правило записи памяти).
    if re.search(r"\bmemory\b[^;&|]*\bremember\b", command) and re.search(
            r"--sc\w*[=\s]+[\"']?(?:global|\$)|--content\w*[=\s]+[\"']?(?:manifest|\$)", command):
        out.add(_("запись в глобальную память или манифест"))
    if danger.DISK in verdict.tags:
        out.add(_("пишет на диск напрямую"))
    # Сервер Telegram на loopback принимает запросы без пароля: `curl` или
    # `python` отправили бы сообщение от имени Ивана мимо карточки (проект
    # 03.11.2026, п. 5). Бот и скиллы туда из Bash не ходят. Аргументы
    # текстовых команд (шаблон grep, сообщение коммита) не в счёт, как у mailsend.
    # Исполняемая часть команды считается один раз.
    executed: str | None = None
    if telegram_port and _LOOPBACK_RE.search(command):
        executed = outbound.executed_text(command)
        if _LOOPBACK_RE.search(executed) and re.search(rf"(?<![\w.]){telegram_port}(?!\d)", executed):
            out.add(Outbound(_("отправка через сервер Telegram мимо проверки")))
    # Удалённое исполнение не программой ssh: UNC `\\host\share`, адрес
    # `ssh://host` (`docker -H`), удалённый PowerShell (`-ComputerName`,
    # `New-PSSession dc`), в том числе внутри подстановки. Хост в аргументах
    # локальной программы (nmap, ping, dig, скрипт python) на сервере ничего не
    # исполняет и заказчиком не считается; текстовые команды тоже (ревью 03.11.2026).
    # Чтение в другом шаге проверку не отменяет: `ssh h uptime; docker -H
    # ssh://h restart app` это изменение (финальное ревью 03.11.2026). Мост
    # PowerShell, который шаг уже разобрал со скриптом -FilePath, не повторяется.
    if customer_hosts and customer_kind(out) != "change":
        if executed is None:
            executed = outbound.executed_text(command)
        host = _customer_word(executed, customer_hosts) or (next(
            (h for h in _ps_hosts(_PS_PROBE_RE.sub("", executed)) if "$" not in h and _host_matches(h, customer_hosts)),
            "")
            if _PS_WORD_RE.search(executed) and not ps_step else "")
        if host and _customer_change(command):
            out.add(_customer_reason(host))
    if (danger.GUARD in verdict.tags and _names_guard(command, cwd, verdict)) or _redirect_to_guard(
            command, cwd, verdict, values):
        out.add(_("правка защиты бота"))
    return out


_PIP_RE = re.compile(r"pip(?:3(?:\.\d+)?)?")
_PYTHON_RE = re.compile(r"python(?:3(?:\.\d+)?)?")
_PIP_VALUE = frozenset({"--python", "--log", "--proxy", "--retries", "--timeout", "--exists-action", "--trusted-host",
                        "--cert", "--client-cert", "--cache-dir", "--src", "--use-feature", "--use-deprecated",
                        "--keyring-provider", "-C", "--config-settings", "--directory", "-p"})


def _pip_install(prog: str, args: list[str]) -> bool:
    """Сама установка (ревью PR 10): `pip install`, `python -m pip install`,
    `uv pip install`. Слово install в другом месте (`pytest -k install`) нет."""
    if _PIP_RE.fullmatch(prog):
        rest = args
    elif _PYTHON_RE.fullmatch(prog):
        i = 0
        while i < len(args) and args[i].startswith("-") and args[i] != "-m":
            i += 1
        if args[i:i + 2] not in (["-m", "pip"], ["-m", "pip3"]):
            return False
        rest = args[i + 2:]
    elif prog == "uv":
        pos = _first_pos(args, _PIP_VALUE)
        if pos[:1] != ["pip"]:
            return False
        rest = args[args.index("pip") + 1:]
    else:
        return False
    return _first_pos(rest, _PIP_VALUE)[:1] == ["install"]


def _bot_env(prog: str, head: str, args: list[str], cwd: str | None, values: dict[str, str]) -> bool:
    """Установка идёт в окружение бота: путь программы из её слова (в
    кавычках тоже) внутри `.venv` бота, `uv pip` в каталоге бота или с
    `--python` из окружения бота."""
    venvs = (BOT_ROOT / ".venv", BOT_ROOT.resolve() / ".venv")
    word = _expand(head, values) if "$" in head else head
    if word and "/" in word and (path := _abs(word, cwd)) and _under(path, venvs):
        return True
    if prog != "uv":
        return False
    if cwd and Path(cwd) in (BOT_ROOT, BOT_ROOT.resolve()):
        return True
    return any((path := _abs(v, cwd)) and _under(path, (BOT_ROOT, BOT_ROOT.resolve()))
               for v in _ansible_values(args, ("--python", "-p")))


_SUBST_PROG_RE = re.compile(r"\$\(\s*(?:which|command\s+-v|type\s+-[pP])\s+([\w.+-]+)\s*\)"
                            r"|`\s*(?:which|command\s+-v|type\s+-[pP])\s+([\w.+-]+)\s*`")

# Вложенный агент (guard3, Н1, узко): без карточки только `--version`,
# `--help`, служебные подкоманды чтения и запуск в режиме чтения. Хук бота у
# вложенного запуска не стоит: он задан только флагом `--settings` рана бота,
# а в ~/.claude/settings.json его нет и не будет (задел бы сессии на ПК).
_CLAUDE_SUBCOMMANDS = frozenset({"mcp", "config", "doctor", "update", "upgrade", "install", "setup-token",
                                 "plugin", "plugins", "migrate-installer", "auth", "login", "logout"})
_CLAUDE_VALUE = frozenset({
    "--model", "--max-turns", "--permission-mode", "--output-format", "--input-format", "--allowedTools",
    "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--mcp-config", "--settings",
    "--append-system-prompt", "--system-prompt", "--system-prompt-file", "--append-system-prompt-file",
    "--add-dir", "--agents", "--agent", "--session-id", "--fallback-model", "--setting-sources",
    "--plugin-dir", "--max-budget-usd", "--json-schema", "--betas", "--permission-prompt-tool", "--tools"})
_CLAUDE_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS"})
# Ключи claude, которые несут свои настройки, хуки, агентов, плагины и
# MCP-серверы: режим чтения с ними не режим чтения (ревью PR 10).
_CLAUDE_LOADS = frozenset({"--settings", "--setting-sources", "--agents", "--plugin-dir", "--mcp-config",
                           "--permission-prompt-tool"})
# Имя инструмента в `--tools`: с заглавной буквы, без пробелов, `Tool(...)`.
_TOOL_NAME_RE = re.compile(r"[A-Z]\w*(?:\([^()\s]*\))?")
_AGENT_HELP = frozenset({"--version", "-v", "-V", "--help", "-h"})
_CODEX_SUBCOMMANDS = frozenset({"login", "logout", "completion", "mcp", "features", "help", "apply", "a"})
_CODEX_VALUE = frozenset({"-s", "--sandbox", "-m", "--model", "-c", "--config", "-p", "--profile", "-C", "--cd",
                          "-a", "--ask-for-approval", "-i", "--image", "--add-dir", "--output-schema",
                          "-o", "--output-last-message", "--color", "--local-provider"})
# `codex exec -s read-only -c ключ=…`: без карточки только эти ключи; прочие
# (`mcp_servers.*`, `notify`, `shell*`, `hooks`) исполняют своё (ревью PR 10).
_CODEX_READ_CONFIG = frozenset({"model", "model_reasoning_effort", "web_search"})


def _agent_help(args: list[str], value: frozenset[str], subs: frozenset[str]) -> bool:
    """`--help` и `--version` до `--` и до текста запроса; служебная подкоманда
    (`codex exec --help`, `claude mcp --help`) не текст запроса."""
    sub = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return False
        if a in _AGENT_HELP:
            return True
        if a.startswith("-"):
            i += 2 if a in value else 1
            continue
        if not sub and a in subs:
            sub = True
            i += 1
            continue
        return False
    return False


def _claude_tools(args: list[str]) -> list[str] | None:
    """Инструменты из `--tools`. Ключ вариадический, но текст запроса после
    него не инструменты: следующие слова берутся, пока похожи на имена."""
    tools: list[str] | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("--tools="):
            tools = (tools or []) + [t for t in re.split(r"[,\s]+", a.split("=", 1)[1]) if t]
        elif a == "--tools":
            tools = tools or []
            j = i + 1
            while j < len(args) and not args[j].startswith("-"):
                parts = [t for t in re.split(r"[,\s]+", args[j]) if t]
                if j > i + 1 and not (parts and all(_TOOL_NAME_RE.fullmatch(t) for t in parts)):
                    break
                tools += parts
                j += 1
            i = j
            continue
        i += 1
    return tools


def _codex_config_ok(v: str) -> bool:
    key, _eq, val = v.partition("=")
    key = key.strip()
    return key in _CODEX_READ_CONFIG or (key == "sandbox_mode" and val.strip().strip("'\"") == "read-only")


def _nested_agent(prog: str, args: list[str]) -> bool:
    """`claude` или `codex` запускает агента не в режиме чтения."""
    value = _CLAUDE_VALUE if prog == "claude" else _CODEX_VALUE
    if _agent_help(args, value, _CLAUDE_SUBCOMMANDS if prog == "claude" else _CODEX_SUBCOMMANDS | {"exec", "e"}):
        return False
    pos = _first_pos([a for a in args if not a.startswith("--") or "=" not in a], value)
    if prog == "claude":
        # Служебные подкоманды без карточки только читающие (ревью PR 10):
        # `claude mcp add`, `plugin install`, `config set` ставят своё.
        if pos[:1] == ["doctor"] or pos[:2] in (
                ["mcp", "list"], ["mcp", "get"], ["config", "list"], ["config", "get"],
                ["plugin", "list"], ["plugins", "list"], ["plugin", "validate"],
                ["plugin", "marketplace"] if pos[2:3] in (["list"], []) else [None]):
            return False
        # Справка подкоманды (`claude mcp add --help`) ничего не ставит.
        if pos and pos[0] in _CLAUDE_SUBCOMMANDS and {"--help", "-h"} & set(args):
            return False
        if pos and pos[0] in _CLAUDE_SUBCOMMANDS:
            return True
        # `claude -p` с `--tools` из списка чтения и `--strict-mcp-config` без своих
        # серверов, настроек, агентов и плагинов.
        tools = _claude_tools(args)
        return not ({"-p", "--print"} & set(args) and "--strict-mcp-config" in args and tools is not None
                    and all(t in _CLAUDE_READ_TOOLS for t in tools)
                    and not any(a.split("=", 1)[0] in _CLAUDE_LOADS for a in args))
    if pos and pos[0] in _CODEX_SUBCOMMANDS:
        return False
    if not pos or pos[0] not in ("exec", "e"):
        return True
    # `codex exec -s read-only` без обхода песочницы и без `-c` вне короткого списка.
    sandbox = [v for v in _ansible_values(args, ("-s", "--sandbox"))]
    return not (sandbox and all(v == "read-only" for v in sandbox) and not {
        "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto"} & set(args) and all(
        _codex_config_ok(v) for v in _ansible_values(args, ("-c", "--config"))))


_MOSH_VALUE = frozenset({"-p", "--port", "--ssh", "--server", "--client", "--predict", "--family",
                         "--bind-server", "--experimental-remote-ip"})


def _mosh(args: list[str], customer: tuple[str, ...], out: set[str]) -> None:
    """`mosh host -- команда` (guard3, Н10): как ssh; без команды сеанс, его команд не видно."""
    i, host = 0, ""
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("-"):
            i += 2 if a in _MOSH_VALUE else 1
            continue
        host = a
        i += 1
        break
    rest = args[i:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if host and _host_matches(host, customer) and (not rest or _customer_change(" ".join(rest))):
        out.add(_customer_reason(host))


def _hidden_body_secret(prog: str, args: list[str], cwd: str | None) -> bool:
    """Тело запроса в файле (guard3, Н5): `curl -K настройки` и `wget
    --body-file`. Файл секрета, а у curl ещё настройки, которых не видно или
    которые ссылаются на секрет."""
    if prog == "curl":
        files = _curl_opt(args, "K", ("--config",))
    else:
        files = _ansible_values(args, ("--body-file", "--post-file"))
    for f in files:
        if danger._secret_path(f, cwd):
            return True
        if prog == "curl":
            text = _read_script(f, cwd)
            if text is None or _SECRET_VAR_RE.search(text) or any(
                    danger._secret_path(w, cwd) for w in re.split(r"[\s='\"@<]+", text) if w):
                return True
    return False


_NET_PROGS = frozenset({"curl", "wget", "ssh", "autossh", "sshpass", "nc", "ncat", "netcat", "socat", "dig",
                        "nslookup", "host", "drill"})
_LOCAL_URL_RE = re.compile(r"https?://(?:localhost|127\.\d+\.\d+\.\d+|\[::1\])(?:[:/?#]|$)")


# Ключ входа и файл настроек ssh на сервер не уходят (ревью PR 10).
_SSH_LOCAL_OPT_RE = re.compile(r"\s*(?:identityfile|certificatefile)\b", re.IGNORECASE)


# Значение `-o ProxyCommand` (`ProxyCommand=x`, `ProxyCommand x`).
_SSH_PROXY_OPT_RE = re.compile(r"\s*proxycommand(?:\s*=\s*|\s+)(.*)", re.IGNORECASE | re.DOTALL)


def _ssh_net_args(args: list[str], autossh: bool = False) -> list[str]:
    """Ключи ssh и хост без ключа входа, файла настроек `-F` и удалённой
    команды. Вложенный ssh в `-o ProxyCommand` разбирается так же: его ключ
    входа тоже не секрет в сеть (ревью PR 10). Значение с операторами
    оболочки (`<`, `|`, `$(…)`) остаётся целиком: в нём вход из файла."""
    opts, i = _ssh_opts(args, autossh)
    out: list[str] = []
    for c, v in opts:
        if c in "iF" or (c == "o" and _SSH_LOCAL_OPT_RE.match(v)):
            continue
        m = _SSH_PROXY_OPT_RE.match(v) if c == "o" else None
        if m and not re.search(r"[<>|;&$`]", m.group(1)):
            try:
                inner = shlex.split(m.group(1))
            except ValueError:
                inner = []
            prog = _no_exe(os.path.basename(inner[0])) if inner else ""
            if prog in ("ssh", "autossh"):
                v = "ProxyCommand=" + shlex.join([inner[0], *_ssh_net_args(inner[1:], prog == "autossh")])
        out.append(f"-{c}{v}")
    return out + args[i:i + 1]


_SSH_PUBLIC_RE = re.compile(r"(?:^|/)\.ssh/(?:config|known_hosts[\w.-]*)$|\.pub$")


def _secret_to_net(command: str, verdict: danger.Verdict, cwd: str | None) -> bool:
    """Команда с признаком секрета зовёт сетевую программу. Признак ищется по
    шагам: у ssh не в счёт ключ входа (`-i ключ` в любом месте и в склейке
    `-vi`, IdentityFile, в том числе у ssh внутри ProxyCommand), файл настроек
    `-F` и удалённая команда (её пути на сервере), у обёртки (timeout, env, sudo) её внутренняя программа, которая
    идёт своим шагом, и `ssh-add` (ключ остаётся на машине); вход `< файл` и
    подстановки `$(…)` в счёт. curl и wget только на loopback не в сеть."""
    net = [(p, a) for p, a in verdict.programs if _no_exe(p) in _NET_PROGS]
    if not net:
        return False
    if all(_no_exe(p) in ("curl", "wget") and (urls := _curl_urls(args))
           and all(_LOCAL_URL_RE.match(u) for u in urls) for p, args in net):
        return False
    progs = verdict.programs
    for k, (p, a) in enumerate(progs):
        name = _no_exe(p)
        if name == "ssh-add":
            continue
        if name in _WRAPPERS and k + 1 < len(progs):
            inner = [verdict.heads[k + 1] if k + 1 < len(verdict.heads) else progs[k + 1][0], *progs[k + 1][1]]
            if a[len(a) - len(inner):] == inner:
                # Свои ключи обёртки секрета не читают; `env X=…` без команды
                # (печать окружения) здесь не тот случай.
                continue
        if name in ("ssh", "autossh"):
            a = _ssh_net_args(a, name == "autossh")
        elif name == "mosh":
            a = a[:1]
        # Список хостов, известные хосты и открытые ключи не секрет (замер по
        # журналу 04.11.2026: `ssh host …; grep '^Host ' ~/.ssh/config`).
        a = [c for c in a if not _SSH_PUBLIC_RE.search(c)]
        try:
            if danger.classify("Bash", {"command": shlex.join([p, *a])}, cwd).secret:
                return True
        except Exception:
            return True
    masked = danger._quote_mask(command)
    if any(danger._secret_path(command[m.start(1):m.end(1)].strip("'\""), cwd)
           for m in _STDIN_FILE_RE.finditer(masked)):
        return True
    return any(danger.classify("Bash", {"command": s1 or s2}, cwd).secret
               for s1, s2 in re.findall(r"\$\(([^()]*)\)|`([^`]*)`", command))


# Программы удалённого исполнения, у которых `.exe` из WSL снимается.
_REMOTE_EXE = frozenset({"ssh", "autossh", "scp", "sftp", "rsync", "sshpass", "plink", "curl", "wget",
                         "telnet", "ssh-copy-id", "ansible", "ansible-playbook"})


def _no_exe(prog: str) -> str:
    """`ssh.exe`, `scp.exe` из WSL та же программа (финальное ревью 03.11.2026),
    в том числе вложенная: `sshpass -p x ssh.exe`, `xargs ssh.exe`."""
    low = prog.lower()
    return low[:-4] if low.endswith(".exe") and low[:-4] in _REMOTE_EXE else prog
# Ключи ansible и ansible-playbook со значением следующим словом.
_ANSIBLE_VALUE = frozenset({
    "-i", "--inventory", "--inventory-file", "-m", "--module-name", "-a", "--args", "-l", "--limit", "-u",
    "--user", "-e", "--extra-vars", "-f", "--forks", "-M", "--module-path", "-t", "--tree", "--tags",
    "--skip-tags", "-B", "--background", "-P", "--poll", "-c", "--connection", "-T", "--timeout",
    "--private-key", "--key-file", "--vault-password-file", "--vault-id", "--become-user", "--become-method",
    "--ssh-common-args", "--ssh-extra-args", "--scp-extra-args", "--sftp-extra-args", "--playbook-dir",
    "--start-at-task"})
# Модули ad hoc только чтения; `command` чтение по тексту `-a`. Прочие
# модули (shell, raw, script, copy, template, file, service, systemd, apt,
# yum, package, user, lineinfile и неизвестные) изменение.
_ANSIBLE_READ_MODULES = frozenset({"ping", "setup"})


def _ansible_values(args: list[str], names: tuple[str, ...]) -> list[str]:
    """Значения ключей names: `-i x`, `-ix`, `--inventory=x`."""
    out: list[str] = []
    for i, a in enumerate(args):
        if a in names:
            if i + 1 < len(args):
                out.append(args[i + 1])
        elif a.startswith("--") and a.split("=", 1)[0] in names and "=" in a:
            out.append(a.split("=", 1)[1])
        elif not a.startswith("--") and len(a) > 2 and a[:2] in names:
            out.append(a[2:])
    return out


# Цель или `-l` только на эту машину: хосты заказчика из файла инвентаря
# тогда не в счёт. Любой другой шаблон (группа, псевдоним, маска, динамический
# инвентарь) считается задевающим их: разбор групп и псевдонимов терял
# изменения (проверка 03.11.2026).
_ANSIBLE_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})


def _ansible(prog: str, args: list[str], cwd: str | None, customer: tuple[str, ...], out: set[str]) -> bool:
    """ansible и ansible-playbook с хостом заказчика в целях (шаблон ad hoc,
    `-l`) или в `-i` (список через запятую или файл инвентаря): причина
    заказчика по модулю, плейбук всегда изменение. True: хост заказчика найден
    (финальное ревью 03.11.2026)."""
    pats = [p.lower() for p in customer]
    limits = _ansible_values(args, ("-l", "--limit"))
    targets = _first_pos(args, _ANSIBLE_VALUE)[:1] if prog == "ansible" else []
    hosts = [h for v in limits + targets for h in re.split(r"[:,]", v)]
    texts: list[str] = []
    for v in _ansible_values(args, ("-i", "--inventory", "--inventory-file")):
        if "," in v:
            hosts += v.split(",")
            continue
        texts.append(_read_script(v, cwd) or "")
    local = [v for v in targets + limits if v]
    only_local = bool(local) and all(
        p.strip().lower() in _ANSIBLE_LOCAL for v in local for p in re.split(r"[:,]", v) if p.strip())
    for text in texts if not only_local else ():
        # Файл инвентаря: слова без `ssh -G` (их много), как _customer_word.
        # `ansible localhost -i inv.ini` на хост заказчика из файла не ходит.
        hosts += [w for w in re.split(r"[\s=:\[\],'\"]+", text)
                  if w and any(fnmatch.fnmatch(w.lower(), p) for p in pats)]
    host = next((h for h in (h.strip().lstrip("&!") for h in hosts)
                 if h and "/" not in h and _host_matches(h, customer)), "")
    if not host:
        return False
    flags = set(args)
    if {"--list-hosts", "--list-tasks", "--list-tags", "--syntax-check"} & flags:
        # На сервер не ходит. `--check` не в счёт: задачи с `check_mode: false`
        # выполняются по-настоящему (проверка 03.11.2026).
        return True
    if prog == "ansible-playbook":
        out.add(_customer_reason(host))
        return True
    module = (_ansible_values(args, ("-m", "--module-name")) or ["command"])[-1]
    module = module.removeprefix("ansible.builtin.").removeprefix("ansible.legacy.")
    text = " ".join(_ansible_values(args, ("-a", "--args")))
    read = module in _ANSIBLE_READ_MODULES or (module == "command" and not _customer_change(text, "sh"))
    if not read:
        out.add(_customer_reason(host))
    return True


def _delete(prog: str, args: list[str], cwd: str | None, values: dict[str, str], out: set[str],
            step: int | None = None) -> None:
    if prog in _DISK_PROGS or prog.startswith("mkfs."):
        out.add(_("затирает диск или файлы ({prog})").format(prog=prog))
        return
    if prog == "rm":
        flags = danger._flags(args)
        recursive = bool({"-r", "-R"} & flags) or any(
            danger.long_flag(a, "--recursive", 3) for a in args) or any(
            a.startswith("-") and not a.startswith("--") and set(a[1:]) & {"r", "R"} for a in args)
        targets = danger._positionals(args)
        if not targets and not ({"--help", "--version"} & flags):
            # Цели из потока (`… | xargs rm -rf`): что удалится, не видно.
            out.add(_("удаляет файлы по списку из потока"))
            return
        for raw in targets:
            word = _expand(raw, values)
            path = _abs(word, cwd) if word is not None else None
            single = not recursive and word is not None and not _GLOB_RE.search(word)
            if single:
                continue
            if not _recoverable(path) or (path and Path(path) in (_HOME, Path("/"))) or (
                    path and _moved_here(path, step)):
                out.add(_("удаляет каталог или много файлов вне git и ~/tmp"))
                return
            # Удаление байткода модуль не подменяет: `__pycache__` пересоберётся.
            if _holds_guard(path) or (_guard(path) and "__pycache__" not in Path(path).parts):
                out.add(_("правка защиты бота"))
                return
            if path and not _under(path, FREE_ROOTS) and not _repo_keeps(
                    path.split("*", 1)[0].rstrip("/") or path):
                out.add(_("удаляет из репозитория то, чего нет в git"))
                return
        return
    if prog == "truncate":
        # Обрезка как `rm` по пути: один файл без шаблона не в счёт (guard3, Н7).
        for raw in _first_pos(args, {"-s", "--size", "-r", "--reference"}):
            word = _expand(raw, values)
            if word is not None and not _GLOB_RE.search(word):
                continue
            path = _abs(word, cwd) if word is not None else None
            if not _recoverable(path):
                out.add(_("затирает файлы ({prog})").format(prog=prog))
                return
        return
    if prog in ("tar", "zip", "rsync") and _removes_sources(prog, args, cwd, values):
        out.add(_("удаляет исходные файлы после упаковки или копии ({prog})").format(prog=prog))
        return
    if prog == "gsutil" and _first_pos(args, {"-o", "-h", "-u", "-p"})[:1] in (["rm"], ["rb"]):
        out.add(_("удаляет данные ({prog})").format(prog=prog))
        return
    if prog == "find" and ("-delete" in args or any(
            a in ("-exec", "-execdir", "-ok") and i + 1 < len(args)
            and danger._program(args[i + 1]) in ("rm", "shred", "truncate")
            for i, a in enumerate(args))):
        rest = list(args)
        while rest and (rest[0] in ("-H", "-L", "-P") or rest[0].startswith("-O")):
            rest = rest[1:]
        if rest[:1] == ["-D"]:
            rest = rest[2:]
        roots = rest[:next((i for i, a in enumerate(rest) if a.startswith(("-", "(", "!"))), len(rest))]
        for raw in roots or ["."]:
            word = _expand(raw, values)
            path = _abs(word, cwd) if word is not None else None
            # Корень поиска сам может быть рабочей копией: удаляются найденные в ней.
            if not (_recoverable(path) or (path and (Path(path) / ".git").exists())):
                out.add(_("удаляет файлы поиском вне git и ~/tmp"))
                return
            # Имя после `!` или `-not` отрицание, а не фильтр (ревью Codex 03.11.2026).
            names = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-name", "-iname", "-path")
                     and not (i and args[i - 1] in ("!", "-not"))]
            if not names and path and not _under(path, FREE_ROOTS) and not _repo_keeps(path):
                out.add(_("удаляет из репозитория то, чего нет в git"))
                return
            if _guard(path) or (_holds_guard(path) and (not names or any(
                    fnmatch.fnmatch(g.name, n.rsplit("/", 1)[-1]) for n in names
                    for g in GUARD_FILES if path and Path(path) in g.parents))):
                out.add(_("правка защиты бота"))
                return
        return
    if prog == "rsync" and any(a.startswith("--del") for a in args):
        targets = _rsync_operands(args)
        word = _expand(targets[-1], values) if targets else None
        dest = _abs(word, cwd) if word is not None and ":" not in word else None
        if dest is None or not _recoverable(dest):
            out.add(_("rsync --delete вне git и ~/tmp"))
        elif _guard(dest) or _holds_guard(dest):
            out.add(_("правка защиты бота"))
        elif not _under(dest, FREE_ROOTS) and not _repo_keeps(dest):
            out.add(_("удаляет из репозитория то, чего нет в git"))
        return
    if prog == "docker":
        words = set(args)
        if ("volume" in words and words & {"rm", "prune"}) or (
                "system" in words and "prune" in words and "--volumes" in words) or (
                words & {"down", "rm"} and words & {"-v", "--volumes"}):
            out.add(_("удаляет тома docker с данными"))
        return
    if prog not in ("git", "rm", "rmdir", "unlink", "truncate", "find", "rsync", "dd", "cp", "mv", "install",
                    "ln", "tar", "unzip", "ssh", "autossh", "bash", "sh", "zsh", "dash", "eval", "sudo", "env",
                    "timeout", "nice", "nohup", "exec", "xargs", "=", "curl", "wget", "gh"):
        # Базы, kubectl delete, helm uninstall, terraform destroy, облака, rclone;
        # публикация пакета и почта (npm publish, twine, sendmail).
        try:
            sub = danger.classify("Bash", {"command": shlex.join([prog, *args])}, cwd)
        except Exception:
            return
        if danger.DELETE in sub.tags:
            out.add(_("удаляет данные ({prog})").format(prog=prog))
        if danger.PUBLISH in sub.tags and prog != "docker":
            out.add(_("публикация или отправка наружу ({prog})").format(prog=prog))


# Переносы и ссылки текущей команды: (номер шага, куда), для проверки `rm -r`
# во временном месте (guard3, Н7: `mv ~/Documents /tmp/d && rm -rf /tmp/d`).
_MOVES: contextvars.ContextVar[tuple[tuple[int, str], ...]] = contextvars.ContextVar("serious_moves", default=())


def _collect_moves(verdict: danger.Verdict, cwd: str | None, nested: bool, values: dict[str, str]) -> None:
    """Номера шагов совпадают с _bash. Цель, которая не раскрылась, не в счёт;
    один файл (не каталог) тоже: `rm` его не больше, чем `rm файл`.
    Происхождение идёт дальше по цепочке: `mv ~/Documents /tmp/a && mv
    /tmp/a /tmp/b && rm -rf /tmp/b` (ревью PR 10)."""
    moves: list[tuple[int, str]] = []
    for n, (prog, args, step_cwd) in enumerate(_steps(verdict, cwd, nested, values)):
        if prog not in ("mv", "ln"):
            continue
        target_dir = _ansible_values(args, ("-t", "--target-directory"))
        ops = danger._positionals(args)
        dest = target_dir[-1] if target_dir else (ops[-1] if len(ops) >= 2 else None)
        srcs = ops if target_dir else ops[:-1]
        if dest is None:
            continue
        d = _expand(dest, values)
        dpath = _abs(d, step_cwd) if d is not None else None
        if dpath is None:
            continue
        into_dir = bool(target_dir) or dest.endswith("/") or len(srcs) > 1 or os.path.isdir(dpath)
        for s in srcs:
            w = _expand(s, values)
            spath = _abs(w, step_cwd) if w is not None else None
            carried = spath is not None and _moved_here(spath, n, moves)
            # Перенос из временного места или из git ничего не теряет, один файл тоже.
            if spath is not None and not carried and (_recoverable(spath) or os.path.isfile(spath)):
                continue
            # В каталог: перенесённое ложится в него под своим именем.
            moves.append((n, os.path.join(dpath, os.path.basename(spath.rstrip("/"))) if (
                into_dir and spath) else dpath))
    _MOVES.set(tuple(moves))


def _moved_here(path: str, step: int | None = None, moves: Iterable[tuple[int, str]] | None = None) -> bool:
    """`rm` по пути, куда та же команда раньше этого шага перенесла или
    сослалась на то, чего не вернуть: путь внутри места переноса или место
    внутри пути. Перенос после `rm` на него не влияет."""
    p = Path(path)
    for n, dest in _MOVES.get() if moves is None else moves:
        if step is not None and n >= step:
            continue
        d = Path(dest)
        if p == d or d in p.parents or p in d.parents:
            return True
    return False


def _removes_sources(prog: str, args: list[str], cwd: str | None, values: dict[str, str]) -> bool:
    """`tar --remove-files`, `zip -m`, `rsync --remove-source-files`: источник
    вне git и временных мест удаляется после упаковки или копии."""
    if prog == "tar":
        if not any(danger.long_flag(a, "--remove-files", 5) for a in args):
            return False
        srcs = _tar_sources(args, cwd, values)
    elif prog == "zip":
        if not ("--move" in args or "m" in _short_letters([a for a in args if a.startswith("-")])):
            return False
        srcs = [(a, cwd) for a in _first_pos(args, {"-b", "-n", "-t", "-tt", "-x", "-i", "-P", "-Z", "-O",
                                                    "--out"})[1:]]
    else:
        if "--remove-source-files" not in args:
            return False
        srcs = [(a, cwd) for a in _rsync_operands(args)[:-1]]
    for raw, base in srcs:
        if ":" in raw and not raw.startswith("/"):
            continue
        word = _expand(raw, values)
        path = _abs(word, base) if word is not None else None
        if not _recoverable(path):
            return True
    return False


_TAR_VALUE_LONG = ("--file", "--directory", "--files-from", "--exclude-from", "--listed-incremental")


def _tar_sources(args: list[str], cwd: str | None, values: dict[str, str]) -> list[tuple[str, str | None]]:
    """Источники tar с каталогом, от которого они считаются. Ключи со
    значением: в связке значение следующим словом (`-czf архив`), а склеенное
    (`-cf/dev/null`) в том же слове; в старой форме без дефиса (`tar cfC архив
    каталог …`) значения идут словами по порядку. `-C каталог` меняет каталог
    для последующих источников (ревью PR 10)."""
    srcs: list[tuple[str, str | None]] = []
    base = cwd

    def chdir(v: str) -> None:
        nonlocal base
        d = _expand(v, values)
        base = _abs(d, base) if d is not None else None

    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in _TAR_VALUE_LONG:
                if not eq:
                    i += 1
                    val = args[i] if i < len(args) else ""
                if name == "--directory":
                    chdir(val)
        elif a.startswith("-") and len(a) > 1:
            for j, c in enumerate(a[1:], 1):
                if c in "fCTXgb":
                    v = a[j + 1:]
                    if not v:
                        i += 1
                        v = args[i] if i < len(args) else ""
                    if c == "C":
                        chdir(v)
                    break
        elif i == 0:
            for c in a:
                if c in "fCTXgb":
                    i += 1
                    if c == "C" and i < len(args):
                        chdir(args[i])
        else:
            srcs.append((a, base))
        i += 1
    return srcs


def _git_safe(dir_: str, *args: str) -> str | None:
    """Вывод git без чужого кода: fsmonitor, хуки и пейджер выключены, чистое
    окружение и таймаут (ревью Codex 03.11.2026). None: не вышло."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(_HOME), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        r = subprocess.run(["git", "-C", dir_, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                            "-c", "core.pager=cat", *args],
                           capture_output=True, text=True, timeout=3, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _git(args: list[str], cwd: str | None, command: str, out: set[str]) -> None:
    sub, rest, _has_config = danger._git_sub(args)
    where = cwd
    for i, a in enumerate(args[:len(args) - len(rest)]):
        if a == "-C" and i + 1 < len(args):
            where = _abs(args[i + 1], where)
        elif a.startswith("-C") and len(a) > 2:
            where = _abs(a[2:], where)
        elif a.startswith("--work-tree="):
            where = _abs(a.split("=", 1)[1], where)
        elif a in ("--work-tree", "--git-dir") and i + 1 < len(args):
            where = _abs(args[i + 1], where) if a == "--work-tree" else None
    flags = danger._flags(rest)
    if where and _under(where, GUARD_DIRS) and sub in (
            "commit", "push", "reset", "rebase", "merge", "am", "apply", "checkout", "restore", "rm", "mv",
            "pull", "cherry-pick", "revert", "stash"):
        out.add(_("правка защиты бота"))
        return
    if where and _under(where, (BOT_ROOT, BOT_ROOT.resolve())) and _bot_tree_foreign(sub, rest, where, command):
        out.add(_("правка защиты бота"))
        return
    if (sub == "stash" and rest[:1] in (["clear"], ["drop"])) or (
            sub == "worktree" and rest[:1] == ["remove"] and ({"-f", "--force"} & set(rest))):
        out.add(_("стирает незакоммиченные правки ({sub})").format(sub=f"{sub} {rest[0]}"))
        return
    if sub == "push":
        pos = danger._positionals(rest)
        if ({"-f", "--force", "--mirror", "--delete", "-d", "--prune"} & flags
                or any(a.startswith(("--force", "--mirror")) for a in rest)
                or any(p.startswith(("+", ":")) for p in pos[1:])):
            out.add(_("затирает историю на сервере git (force push или удаление ветки)"))
            return
        if re.search(r"\bgit\b[^;&|]*\b(?:remote\s+(?:set-url|add)|config)\b", command) or any(
                (a[2:] if a.startswith("-c") and len(a) > 2 else a if i and args[i - 1] == "-c" else ""
                 ).lower().startswith(("remote.", "url.", "core.sshcommand", "push.")) for i, a in enumerate(args)) or any(a.startswith("--config-env") for a in args):
            out.add(_("push после смены адреса репозитория"))
            return
        remote = pos[0] if pos else ((_git_safe(where, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                                                "@{push}") or "origin/").split("/", 1)[0].strip()
                                     if where else "origin")
        if not _own_push(where, remote or "origin"):
            out.add(_("push не в свой репозиторий"))
        return
    if sub in ("filter-branch", "filter-repo") or (sub == "reflog" and rest[:1] in (["expire"], ["delete"])) or (
            sub == "gc" and any(a.startswith("--prune") for a in rest)) or (
            sub == "update-ref" and "-d" in flags):
        out.add(_("затирает историю git ({sub})").format(sub=sub))
        return
    discards = (
        (sub == "reset" and danger._git_long(flags, "--hard"))
        or (sub == "checkout" and ("-f" in flags or "--force" in rest or "--" in rest or "." in rest
                                   or danger._git_checkout_paths(rest)
                                   # `git checkout файл` без `--`: путь от каталога команды.
                                   or (not ({"-b", "-B"} & flags) and any(
                                       (q := _abs(x, where)) and os.path.lexists(q)
                                       for x in danger._positionals(rest)))))
        or (sub == "restore" and (not ({"--staged", "-S"} & flags) or {"--worktree", "-W"} & flags))
        or (sub == "switch" and ("-f" in flags or danger._git_long(flags, "--discard-changes", "--force")))
    )
    if sub == "clean" and not danger._git_dry(sub, rest):
        if where and _under(where, FREE_ROOTS):
            return
        listed = _git_safe(where or ".", "clean", "-n", *[a for a in rest if a not in ("-f", "--force")])
        if listed is None or listed.strip():
            out.add(_("git clean удаляет неотслеживаемые файлы"))
        return
    if discards:
        if where and _under(where, FREE_ROOTS):
            return
        status = _git_safe(where or ".", "status", "--porcelain", "--untracked-files=no")
        if status is None or status.strip():
            out.add(_("стирает незакоммиченные правки ({sub})").format(sub=sub))


# Свой владелец на GitHub (APPROVE_GITHUB_OWNER): push в его репозитории и
# свой origin бота (guard3, Н4: `git pull`, `fetch`, `reset` к нему) без
# карточки. Пусто: своих репозиториев нет, всё это карточкой.
_GITHUB_URL = r"(?:(?:ssh|https)://(?:[\w.-]+@)?github\.com/|(?:[\w.-]+@)?github\.com:)"


def _github_owner() -> str:
    return os.environ.get("APPROVE_GITHUB_OWNER", "").strip()


def _own_github_url(url: str, repo: str | None = None) -> bool:
    """Адрес репозитория своего владельца: ровно одно имя после владельца,
    без `.` и `..` (git схлопывает `owner/../чужой/репо` в чужой путь, ревью
    04.11.2026); регистр только ASCII (`ı` не `i`)."""
    owner = _github_owner()
    m = owner and re.match(_GITHUB_URL + re.escape(owner) + r"/([\w.-]+?)(?:\.git)?/?$", url,
                           re.IGNORECASE | re.ASCII)
    if not m or m.group(1) in (".", ".."):
        return False
    return repo is None or m.group(1).lower() == repo


def _own_bot_url(url: str) -> bool:
    return _own_github_url(url, "claude-tg-bot")


_GIT_STOP = ("--abort", "--quit", "--continue", "--skip", "--show-current-patch")


# Ключи git fetch со значением следующим словом (`--depth 1`, `-j 4`).
_FETCH_VALUE = frozenset({"--depth", "--deepen", "--shallow-since", "--shallow-exclude", "-j", "--jobs",
                          "--negotiation-tip", "--refmap", "-o", "--server-option", "--upload-pack", "--filter",
                          "--recurse-submodules-default", "--submodule-prefix"})
_REMOTE_CHANGE_RE = re.compile(r"\bgit\b[^;&|\n]*?\bremote\s+(?:set-url|add)\b")


def _bot_origin_own(where: str) -> bool:
    urls = (_git_safe(where, "remote", "get-url", "--all", "origin") or "").split()
    return bool(urls) and all(_own_bot_url(u) for u in urls)


def _upstream_remote(where: str) -> str | None:
    """Удалённый, за которым идёт текущая ветка (`@{u}`); None: не задан."""
    got = (_git_safe(where, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "").strip()
    return got.split("/", 1)[0] if got else None


def _fetch_head_own(where: str, command: str) -> bool:
    """FETCH_HEAD от своего origin: fetch в той же команде только из origin
    (голый fetch из удалённого текущей ветки, без него из origin), а без
    fetch записи файла FETCH_HEAD из своего адреса."""
    fetches = [m.group(1) for m in re.finditer(r"\bgit\b[^;&|\n]*?\bfetch\b([^;&|\n]*)", command)]
    if fetches:
        for tail in fetches:
            try:
                words = shlex.split(tail)
            except ValueError:
                words = tail.split()
            if "--all" in words or "--multiple" in words:
                return False
            pos = _first_pos([w for w in words if not w.startswith("--") or "=" not in w], _FETCH_VALUE)
            if (pos[0] if pos else (_upstream_remote(where) or "origin")) != "origin":
                return False
        return True
    git_dir = (_git_safe(where, "rev-parse", "--git-dir") or "").strip()
    if not git_dir:
        return False
    try:
        text = (Path(where) / git_dir / "FETCH_HEAD").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    urls = [ln.rsplit(" of ", 1)[-1].strip() for ln in text.splitlines() if ln.strip()]
    return bool(urls) and all(_own_bot_url(u) for u in urls)


def _own_ref(where: str, ref: str, command: str) -> bool:
    """Ссылка из своего origin: `origin/…`, предок HEAD или коммит, который
    уже есть в ветках origin."""
    if ref in ("-", "HEAD", "@") or re.fullmatch(r"(?:HEAD|@)[~^]\d*(?:[~^]\d*)*", ref):
        return True
    if ref == "FETCH_HEAD":
        return _fetch_head_own(where, command)
    if ref in ("@{u}", "@{upstream}"):
        return _upstream_remote(where) == "origin"
    if ref == "origin" or ref.startswith(("origin/", "refs/remotes/origin/")):
        return True
    for name in (ref, f"origin/{ref}"):
        got = _git_safe(where, "for-each-ref", "--contains", name, "--format=%(refname)", "refs/remotes/origin")
        if got and got.strip():
            return True
    return False


def _bot_tree_foreign(sub: str, rest: list[str], where: str, command: str) -> bool:
    """git в каталоге бота меняет дерево из чужого источника (guard3, Н4,
    узко): `apply`, `am`, `cherry-pick`, `merge`, `rebase` не из origin,
    переход на ветку не из origin, `reset` к чужому FETCH_HEAD, `pull` не из
    origin. `pull`, `fetch` и `reset` к своему origin без карточки."""
    if sub not in ("apply", "am", "cherry-pick", "merge", "rebase", "checkout", "switch", "reset", "pull"):
        return False
    if any(a in _GIT_STOP for a in rest):
        return False
    if sub == "apply":
        # `--apply` применяет и рядом с `--stat`, `--check` (ревью PR 10).
        return "--apply" in rest or not {"--check", "--stat", "--numstat", "--summary"} & set(rest)
    if sub == "am":
        return True
    if "--" in rest and sub in ("checkout", "reset"):
        # Файлы, а не переход: стирание правок проверяет discards ниже.
        return False
    # `git remote set-url|add` в той же команде до этого шага или до fetch
    # меняет, что такое origin (ревью PR 10).
    changed = _REMOTE_CHANGE_RE.search(command)
    own = _bot_origin_own(where) and not (changed and re.search(
        rf"\bgit\b[^;&|\n]*?\b(?:{re.escape(sub)}|fetch)\b", command[changed.end():]))
    value = {"-m", "--message", "-s", "--strategy", "-X", "--strategy-option", "-F", "--file", "--onto",
             "--exec", "--cleanup", "--depth", "-j", "--jobs", "--upstream", "--conflict", "--orphan"}
    if sub == "rebase":
        value |= {"-x"}
    pos = _first_pos([a for a in rest if not a.startswith("--") or "=" not in a], value)
    if sub == "pull":
        # Голый pull идёт из удалённого текущей ветки: свой, только если это origin.
        return not own or (pos[0] != "origin" if pos else _upstream_remote(where) != "origin")
    if sub == "reset":
        if not pos:
            return False
        ref = pos[0]
        if _git_safe(where, "rev-parse", "--verify", "-q", ref + "^{commit}") is None and ref != "FETCH_HEAD":
            # Не ссылка, а путь: `git reset файл` убирает из индекса.
            return False
        return not (own and _own_ref(where, ref, command))
    if sub in ("checkout", "switch"):
        flags = danger._flags(rest)
        if {"-b", "-B", "-c", "-C"} & flags:
            # Новая ветка: от HEAD или от точки начала.
            start = pos[1:2]
            return bool(start) and not (own and _own_ref(where, start[0], command))
        if not pos:
            return False
        ref = pos[0]
        if os.path.lexists(_abs(ref, where) or "") and _git_safe(
                where, "rev-parse", "--verify", "-q", ref + "^{commit}") is None:
            return False
        return not (own and _own_ref(where, ref, command))
    # merge, rebase, cherry-pick: каждая ссылка из origin; без ссылок вверх по ветке.
    refs = [r for p in pos for r in p.split("..") if r] or ["@{u}"]
    refs += _ansible_values(rest, ("--onto",))
    if not own:
        return True
    return not all(_own_ref(where, r, command) for r in refs)


def _own_push(where: str | None, remote: str) -> bool:
    if where is None:
        return False
    if "://" in remote or "@" in remote or remote.startswith(("/", ".")):
        urls = [remote]
    else:
        urls = (_git_safe(where, "remote", "get-url", "--push", "--all", remote) or "").split()
    return bool(urls) and all(_own_github_url(u) for u in urls)


def _remote_delete(args: list[str], out: set[str]) -> None:
    """`ssh host 'rm -rf /srv/data'`: удаление на любом сервере. Каталог там
    неизвестен, git и ~/tmp сервера отсюда не видны: в счёт только /tmp."""
    i = 0
    while i < len(args):
        if args[i] in danger_ssh_value_flags:
            i += 2
            continue
        if args[i].startswith("-"):
            i += 1
            continue
        i += 1
        break
    remote = " ".join(args[i:])
    if not remote:
        return
    try:
        inner = danger.classify("Bash", {"command": remote}, None)
    except Exception:
        return
    found: set[str] = set()
    for p, a in inner.programs:
        _delete(p, a, None, _assignments(inner, remote), found)
    if found or danger.DISK in inner.tags:
        out.add(_("удаляет данные на сервере"))


def _host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    host = host.split("@")[-1].lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if "$" in host:
        # Хост из переменной или цикла не известен: считается чужим.
        return True
    names = {host}
    real = _ssh_hostname(host)
    if real:
        names.add(real)
    return any(fnmatch.fnmatch(n, p.lower()) for n in names for p in patterns)


@functools.lru_cache(maxsize=256)
def _ssh_hostname(alias: str) -> str:
    """Настоящий адрес псевдонима из ~/.ssh/config: `ssh -G` только читает
    настройки. Ответ запоминается до перезапуска бота: процесс на каждую
    проверку дорог, а правка ~/.ssh/config редка."""
    if not re.fullmatch(r"[\w.-]+", alias):
        return ""
    try:
        r = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True, timeout=2,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"^hostname (\S+)$", r.stdout, re.MULTILINE)
    return m.group(1).lower() if m else ""


def _customer_reason(host: str) -> str:
    return CustomerChange(_("изменения на сервере заказчика ({host})").format(host=host))


# Ключи rsync со значением следующим словом: значение не операнд
# (`rsync -av ./x/ h:/srv/ --exclude cache`, финальное ревью 03.11.2026).
_RSYNC_VALUE_LONG = frozenset({
    "--exclude", "--include", "--filter", "--rsh", "--exclude-from", "--include-from", "--files-from",
    "--password-file", "--log-file", "--log-file-format", "--partial-dir", "--backup-dir", "--temp-dir",
    "--compare-dest", "--copy-dest", "--link-dest", "--chmod", "--chown", "--usermap", "--groupmap",
    "--suffix", "--rsync-path", "--out-format", "--timeout", "--contimeout", "--port", "--sockopts",
    "--bwlimit", "--max-size", "--min-size", "--max-delete", "--block-size", "--modify-window", "--iconv",
    "--info", "--debug", "--skip-compress", "--checksum-choice", "--compress-choice", "--compress-level",
    "--read-batch", "--write-batch", "--only-write-batch", "--stop-after", "--stop-at", "--protocol",
    "--outbuf", "--remote-option", "--address", "--early-input", "--checksum-seed", "--config"})
# Короткие ключи со значением: `-e`, `-f` (--filter), `-T`, `-B`, `-M`, `-@`.
_RSYNC_VALUE_SHORT = frozenset("efTBM@")


def _rsync_operands(args: list[str]) -> list[str]:
    """Операнды rsync без значений ключей: `--exclude X`, `-e X`, склейка
    `-avze X`; формы с `=` и `-eX` значение несут в себе."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i + 1:])
            break
        if a.startswith("--"):
            i += 2 if a in _RSYNC_VALUE_LONG else 1
            continue
        if a.startswith("-") and len(a) > 1:
            letters = a[1:]
            j = next((k for k, c in enumerate(letters) if c in _RSYNC_VALUE_SHORT), -1)
            # Значение следующим словом, только если буква ключа последняя.
            i += 2 if j == len(letters) - 1 else 1
            continue
        out.append(a)
        i += 1
    return out


def _remote(prog: str, args: list[str], customer: tuple[str, ...], out: set[str],
            command: str = "", cwd: str | None = None) -> None:
    operands = _rsync_operands(args) if prog == "rsync" else danger._positionals(args)
    if prog in ("scp", "rsync", "sftp") and operands and ":" in operands[-1] and any(
            danger._secret_path(src) for src in operands[:-1]):
        out.add(_("копирует секрет на другой хост"))
    if not customer:
        return
    # Прыжок через бастион заказчика: `-J host`, `-o ProxyJump=host`, `-e 'ssh -J host'`.
    jumped = [h for m in re.finditer(r"(?:-J\s*|ProxyJump[=\s]+)([\w.@:,\[\]-]+)", " ".join(args))
              for h in m.group(1).split(",")]
    if prog not in ("ssh", "autossh", "plink", "plink.exe") and any(_host_matches(h, customer) for h in jumped):
        dest = operands[-1] if operands else ""
        # Скачать через бастион заказчика: чтение, без карточки.
        if not (prog in ("scp", "rsync") and not (":" in dest and not dest.startswith("/"))
                and "--remove-source-files" not in args):
            out.add(CustomerChange(_("копирует на сервер заказчика через его бастион")))
        return
    if prog in ("ssh", "autossh", "plink", "plink.exe"):
        i, host, jumps, forward = 0, "", [], False
        if prog.startswith("plink"):
            while i < len(args):
                a = args[i]
                if a in _PLINK_VALUE_FLAGS or a == "-nc":
                    forward = forward or a in ("-L", "-R", "-D", "-nc")
                    i += 2
                    continue
                if a.startswith("-"):
                    i += 1
                    continue
                host = a
                i += 1
                break
        else:
            opts, i = _ssh_opts(args, prog == "autossh")
            if any(c == "G" for c, _v in opts):
                # `ssh -G host` только печатает настройки, на сервер не ходит.
                return
            jumps = [h for c, v in opts if c == "J" for h in v.split(",")]
            forward = _ssh_forwards(opts)
            host = args[i] if i < len(args) else ""
            i += 1
            # `-F файл` и ProxyCommand (guard3, Н10): псевдоним из чужого файла
            # настроек `ssh -G` не раскроет, а ProxyCommand сам исполняется.
            pats = [p.lower() for p in customer]
            proxies: list[str] = []
            for c, v in opts:
                if c == "F":
                    text = _read_script(v, cwd)
                    words = [w for w in re.split(r"[\s=,'\"]+", text or "") if w]
                    if text is None:
                        jumps.append("$config")
                    jumps += [w for w in words if any(fnmatch.fnmatch(w.lower(), p) for p in pats)]
                    # Строки ProxyCommand файла как `-o ProxyCommand` (ревью PR 10).
                    proxies += [m.group(1) for m in _SSH_PROXY_LINE_RE.finditer(text or "")]
                elif c == "o" and re.match(r"\s*proxycommand[=\s]", v, re.IGNORECASE):
                    proxies.append(re.split(r"[=\s]+", v.strip(), maxsplit=1)[-1])
            for proxy in proxies:
                if proxy.strip().lower() == "none":
                    continue
                if re.search(r"-W\s*['\"]?%h:%p", proxy):
                    # Прыжок через хост, как `-J`.
                    jumps += [w for w in re.split(r"[\s'\"]+", proxy)
                              if w and any(fnmatch.fnmatch(w.split("@")[-1].lower(), p) for p in pats)]
                else:
                    inner = contextvars.copy_context().run(
                        _bash, proxy.replace("%h", host).replace("%p", "22"), cwd, customer)
                    out.update(w for w in inner if customer_kind([w]))
        remote_cmd = " ".join(args[i:])
        named = [a.split("=", 1)[1] for j, a in enumerate(args)
                 if (j and args[j - 1] == "-o" and a.lower().startswith("hostname="))
                 or a.lower().startswith("-ohostname=")]
        if any(_host_matches(h, customer) for h in [host, *jumps, *jumped, *named] if h):
            if forward:
                # Проброс портов открывает доступ к сети заказчика и через неё.
                out.add(CustomerChange(_("проброс портов через сервер заказчика ({host})").format(host=host)))
                return
            if _ssh_change(args, remote_cmd, host, command, cwd):
                out.add(_customer_reason(host))
        return
    dest = operands[-1] if operands else ""
    if ":" in dest and not dest.startswith("/"):
        host = dest.split(":", 1)[0]
        if _host_matches(host, customer):
            out.add(CustomerChange(_("копирует на сервер заказчика ({host})").format(host=host)))
            return
    elif prog == "sftp":
        hosts = [a for i, a in enumerate(args) if not a.startswith("-")
                 and not (i and args[i - 1] in danger_ssh_value_flags)]
        if hosts and _host_matches(hosts[0].split(":", 1)[0], customer):
            # В сессии бывают put и rm, команд разбор не видит.
            out.add(CustomerChange(_("сессия sftp на сервере заказчика")))
        return
    # Скачать с сервера заказчика: чтение там, без карточки. С
    # `--remove-source-files` rsync удаляет скачанное на сервере.
    for src in operands[:-1]:
        if ":" in src and not src.startswith("/") and _host_matches(src.split(":", 1)[0], customer):
            if "--remove-source-files" in args:
                out.add(_customer_reason(src.split(":", 1)[0]))
            return


# Строка ProxyCommand в файле настроек ssh (`ProxyCommand x`, `ProxyCommand=x`).
_SSH_PROXY_LINE_RE = re.compile(r"^[ \t]*proxycommand(?:[ \t]*=[ \t]*|[ \t]+)(.+?)[ \t]*$",
                                re.IGNORECASE | re.MULTILINE)
# Оболочка без своей команды читает stdin: `ssh host bash -s <<EOF`.
_BARE_SHELL_RE = re.compile(r"\s*(?:sudo\s+(?:-[a-zA-Z]+\s+)*)?(?:ba|da|z)?sh(?:\s+-[a-z]+)*\s*")
_PLINK_VALUE_FLAGS = frozenset({"-P", "-l", "-pw", "-i", "-L", "-R", "-D", "-m", "-hostkey", "-sercfg",
                                "-proxycmd", "-load", "-pwfile", "-sshlog", "-sshrawlog", "-logfile"})
# Буквы ключей ssh со значением (man ssh); у autossh ещё `-M порт`.
_SSH_VALUE_LETTERS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
# Проброс портов: `-L`, `-R`, `-D`, `-W`, туннель `-w` и то же в `-o`.
_SSH_FORWARD_LETTERS = frozenset("LRDWw")
_SSH_FORWARD_OPT_RE = re.compile(r"\s*(?:local|remote|dynamic)forward\b", re.IGNORECASE)


def _ssh_opts(args: list[str], autossh: bool = False) -> tuple[list[tuple[str, str]], int]:
    """Ключи ssh, как их разбирает getopt: склеенные (`-fNL 8080:x:80`,
    `-L8080:x:80`, `-oLocalForward=…`) и со значением следующим словом.
    Второе: индекс хоста в args."""
    letters = _SSH_VALUE_LETTERS | ({"M"} if autossh else set())
    opts: list[tuple[str, str]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return opts, i + 1
        if not a.startswith("-") or a == "-":
            break
        for j, c in enumerate(a[1:], 1):
            if c in letters:
                value = a[j + 1:]
                if not value and i + 1 < len(args):
                    i += 1
                    value = args[i]
                opts.append((c, value))
                break
            opts.append((c, ""))
        i += 1
    return opts, i


def _ssh_forwards(opts: list[tuple[str, str]]) -> bool:
    """Среди ключей ssh проброс портов."""
    return any(c in _SSH_FORWARD_LETTERS or (c == "o" and _SSH_FORWARD_OPT_RE.match(v)) for c, v in opts)


def _ssh_change(args: list[str], remote_cmd: str, host: str, command: str, cwd: str | None) -> bool:
    """Команда ssh на сервере заказчика меняет его: удалённая команда и то,
    что приходит на stdin (тело heredoc, файл после `<`, `plink -m`), не
    узнаны как чтение. Текст, которого разбор не видит, изменение: поток по
    конвейеру (`echo … | ssh host`), RemoteCommand из настроек, подстановка
    `$(…)` и обратные кавычки, файл, который не прочитался или обрезан."""
    if re.search(r"remotecommand", " ".join(args), re.IGNORECASE) or "$(" in remote_cmd or "`" in remote_cmd:
        return True
    if _piped_into(command, host):
        return True
    stdin = _stdin_text(command, cwd)
    if stdin is None:
        return True
    for j, a in enumerate(args[:-1]):
        if a == "-m":
            script = _read_script(args[j + 1], cwd)
            if script is None:
                return True
            stdin = "\n".join(t for t in (stdin, script) if t)
    if stdin and _BARE_SHELL_RE.fullmatch(remote_cmd):
        remote_cmd = ""
    return _customer_change("\n".join(t for t in (remote_cmd, stdin) if t))


def _piped_into(command: str, host: str, progs: str = "ssh|autossh|sshpass|plink") -> bool:
    """ssh (или progs) к этому хосту стоит после `|` конвейера: его вход не виден."""
    masked = danger._quote_mask(command)
    for m in re.finditer(r"(?<!\|)\|(?![|&])([^;&|\n]*)", masked):
        segment = command[m.start(1):m.end(1)]
        if re.search(rf"(?<![\w.-])(?:{progs})(?:\.exe)?(?![\w.-])", segment) and host in segment:
            return True
    return False


_PS_PROGS = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
# Хост удалённого PowerShell (WinRM): `-ComputerName a,b`, `-cn 'a'`, `-Computer a`,
# `-VMName a`, `-HostName a`, `-Server a`. PowerShell принимает любое
# однозначное начало имени параметра (ревью 03.11.2026).
_PS_PARAM_RE = re.compile(r"(?<![\w-])-([a-z]+)(?:\s*:\s*|\s+)(?:@\()?([^\s;|)}]+)", re.IGNORECASE)
_PS_HOST_PARAMS = (("computername", 3), ("cn", 2), ("vmname", 2), ("hostname", 2), ("server", 3))
# Сессия по хосту без имени параметра: `New-PSSession dc`, `Enter-PSSession dc`.
_PS_SESSION_RE = re.compile(r"(?<![\w-])(?:New-PSSession|Enter-PSSession|New-CimSession|nsn|etsn)\s+(?:@\()?"
                            r"([^\s;|)}\-][^\s;|)}]*)", re.IGNORECASE)
# Скрипт Invoke-Command: `-FilePath x.ps1` и начала имени от `-fi`.
_PS_FILEPATH_RE = re.compile(r"(?<![\w-])-fi(?:l(?:e(?:p(?:a(?:t(?:h)?)?)?)?)?)?\s+(['\"]?)(\S+?)\1(?=\s|$)",
                             re.IGNORECASE)
_SCRIPT_CAP = 256 * 1024


# Мост PowerShell в тексте команды: только тогда `-ComputerName` и `-Server` про WinRM.
_PS_WORD_RE = re.compile(r"(?<![\w-])(?:powershell|pwsh)(?:\.exe)?(?![\w-])", re.IGNORECASE)
# Проверка связи с хоста Ивана: `Test-NetConnection -ComputerName dc` на сервере ничего не исполняет.
_PS_PROBE_RE = re.compile(r"(?<![\w-])(?:Test-Connection|Test-NetConnection|tnc|Resolve-DnsName)(?![\w-])[^;|\n}]*",
                          re.IGNORECASE)
# Запрос по адресу из PowerShell: адрес хоста заказчика значит запрос к нему.
_PS_WEB_RE = re.compile(r"(?<![\w-])(?:Invoke-WebRequest|Invoke-RestMethod|iwr|irm|curl|wget)(?![\w-])",
                        re.IGNORECASE)


def _ps_hosts(text: str) -> list[str]:
    hosts = [h for m in _PS_PARAM_RE.finditer(text)
             if any(name.startswith(m.group(1).lower()) and len(m.group(1)) >= n for name, n in _PS_HOST_PARAMS)
             for h in m.group(2).split(",")]
    hosts += [m.group(1) for m in _PS_SESSION_RE.finditer(text)]
    return [h.strip("'\"") for h in hosts if h.strip("'\"")]


def _powershell(args: list[str], cwd: str | None, customer: tuple[str, ...], out: set[str]) -> bool:
    """Мост PowerShell из WSL (`powershell.exe -Command "Invoke-Command
    -ComputerName dc …"`): хост виден в команде, значит команда на сервере
    заказчика. Скрипт `-File` и `-FilePath` проверяется по содержимому; не
    прочитался или обрезан, значит изменение. True: хост заказчика найден."""
    kind, value = _ps_command(args)
    text: str | None = value if kind == "command" else _read_script(value, cwd) if kind == "file" else None
    seen = " ".join(args) + ("\n" + text if text else "")
    host = next((h for h in _ps_hosts(_PS_PROBE_RE.sub("", seen)) if _host_matches(h, customer)), "") \
        or _customer_word(seen, customer, web=bool(_PS_WEB_RE.search(seen)))
    if not host:
        return False
    if text is not None and re.search(r"(?<![\w-])(?:Invoke-Command|icm)(?![\w-])", text, re.IGNORECASE):
        for m in list(_PS_FILEPATH_RE.finditer(text)):
            body = _read_script(m.group(2), cwd)
            if body is None:
                text = None
                break
            text = text.replace(m.group(0), "-ScriptBlock {\n" + body + "\n}")
    if text is None or _customer_change(text, "ps"):
        out.add(_customer_reason(host))
    return True


def _ps_command(args: list[str]) -> tuple[str, str]:
    """Что исполнит powershell.exe: ("command", текст) после `-Command` или
    первым позиционным, ("file", путь) после `-File`; ("", "") для
    `-EncodedCommand` и непонятного."""
    i = 0
    while i < len(args):
        a = args[i].lower()
        if not a.startswith(("-", "/")):
            return "command", " ".join(args[i:])
        if a in ("-c", "/c") or (len(a) >= 3 and "-command".startswith(a)):
            return "command", " ".join(args[i + 1:])
        if a in ("-f", "/f") or (len(a) >= 3 and "-file".startswith(a)):
            return ("file", args[i + 1]) if i + 1 < len(args) else ("", "")
        if a.lstrip("-/") in _PS_EXE_VALUE:
            i += 2
            continue
        if a.startswith(("-e", "/e")) and not a.startswith(("-ex", "/ex")):
            return "", ""
        i += 1
    return "command", ""


_PS_EXE_VALUE = frozenset({"executionpolicy", "ep", "ex", "windowstyle", "w", "inputformat", "if", "outputformat",
                           "of", "version", "v", "configurationname", "workingdirectory", "wd", "settingsfile"})


def _read_script(word: str, cwd: str | None) -> str | None:
    """Текст файла скрипта; None: не обычный файл, не прочитался или длиннее
    предела (хвост не проверен)."""
    if word == "/dev/null":
        return ""
    path = _abs(word.replace("\\", "/"), cwd)
    # Только обычный файл: `< /dev/stdin` или FIFO повесили бы проверку.
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(_SCRIPT_CAP + 1)
    except OSError:
        return None
    return text if len(text) <= _SCRIPT_CAP else None


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)(?:\n[ \t]*\2[ \t]*(?:\n|$)|\Z)", re.DOTALL)
_HERESTRING_RE = re.compile(r"<<<\s*(\"[^\"]*\"|'[^']*'|\S+)")
_STDIN_FILE_RE = re.compile(r"(?<![<\d])<\s*(?![<(])([^\s;&|<>]+)")


def _stdin_text(command: str, cwd: str | None) -> str | None:
    """Что команда подаёт на stdin: тела heredoc, here-string и файл после `<`.
    None: вход не виден (`< <(…)`, файл не прочитался или обрезан)."""
    parts = [m.group(3) for m in _HEREDOC_RE.finditer(command)]
    parts += [m.group(1).strip("'\"") for m in _HERESTRING_RE.finditer(command)]
    masked = danger._quote_mask(command)
    if re.search(r"(?<![<\d])<\s*<\(", masked):
        return None
    for m in _STDIN_FILE_RE.finditer(masked):
        body = _read_script(command[m.start(1):m.end(1)].strip("'\""), cwd)
        if body is None:
            return None
        parts.append(body)
    return "\n".join(p for p in parts if p)


# Хост в слове команды: UNC `\\host\share` и `//host/share`, адрес
# `ssh://host`, с web=True любой адрес `https://host/…`.
_WORD_SPLIT_RE = re.compile(r"[\s'\"`(){};,|&<>=]+")
_URL_HOST_RE = re.compile(r"(?:([a-z][\w+.-]*)://)?(?:[^@/\s]*@)?(\[[^\]]+\]|[^/:?#\s\[\]@]+)", re.IGNORECASE)


def _url_host(word: str) -> str:
    """Хост адреса curl и wget, в том числе без схемы (`bastion:8080/x`)."""
    m = _URL_HOST_RE.match(word)
    return m.group(2).strip("[]") if m else ""


def _customer_word(text: str, customer: tuple[str, ...], web: bool = False) -> str:
    """Первый хост заказчика среди слов текста: UNC, адрес ssh, с web любой
    адрес. Без раскрытия псевдонимов ssh: слов много, а `ssh -G` на каждое
    медленно."""
    pats = [p.lower() for p in customer]
    for raw in _WORD_SPLIT_RE.split(text):
        m = re.match(r"([a-z][\w+.-]*)://(?:[^@/\s]*@)?(\[[^\]]+\]|[^/:?#\s]+)", raw, re.IGNORECASE)
        if m and (web or "ssh" in m.group(1).lower()):
            host = m.group(2).strip("[]")
        elif not m and raw.startswith(("\\\\", "//")):
            host = re.split(r"[\\/]", raw.lstrip("\\/"), maxsplit=1)[0]
        else:
            continue
        if re.fullmatch(r"[\w.:-]+", host) and any(fnmatch.fnmatch(host.lower(), p) for p in pats):
            return host
    return ""


# --- Сервер заказчика: известное чтение --------------------------------------
# Перевёрнутый список (ревью 03.11.2026, решение Ивана): без карточки только то,
# что разбор узнал как чтение RouterOS, Linux, PowerShell или Windows, в том
# числе конвейер из таких команд. Всё прочее, в том числе непонятное, изменение.

def _customer_change(text: str, lang: str = "") -> bool:
    """Удалённая команда меняет сервер заказчика: не узнана как чтение."""
    if not text.strip():
        return False
    try:
        return not _read_text(text, lang)
    except Exception:
        log.exception("serious: customer read check failed")
        return True


# Перенаправление вывода в файл: `>`, `>>`, `2>`, `&>`, в том числе без пробела
# перед ним (`echo x>/etc/y`); кроме /dev/null, $null, nul, дескриптора и числа
# (`where rx-byte > 1000` у RouterOS), стрелок `->` и `=>`.
_REDIRECT_RE = re.compile(
    r"(?<![-=<>])[\d&]?>>?\|?(?![=>])\s*"
    r"(?!&\d|/dev/null(?![\w/])|\$null\b|nul\b|\d+(?:\s|$|[\]);]))(?=\S)", re.IGNORECASE)
# Верхние меню RouterOS: по ним текст узнаётся как его команды.
_ROS_MENUS = frozenset({
    "ip", "ipv6", "interface", "system", "routing", "tool", "user", "queue", "ppp", "certificate", "file",
    "log", "snmp", "radius", "port", "disk", "caps-man", "capsman", "container", "dude", "mpls",
    "special-login", "export", "import", "ping", "traceroute", "password", "partitions", "console", "lcd",
    "app", "iot", "lora", "zerotier", "user-manager", "beep", "execute", "setup", "quit", "redo", "undo"})
_ROS_READ = frozenset({"print", "get", "monitor", "monitor-traffic", "export", "ping", "traceroute", "torch",
                       "find"})
# Глаголы изменения RouterOS: слово, с которого начинается любой из них
# (RouterOS понимает сокращения: `/ip addr rem 0`), тоже изменение.
_ROS_CHANGE = ("add", "set", "remove", "unset", "enable", "disable", "move", "import", "reset",
               "reset-configuration", "reset-counters", "reboot", "shutdown", "upgrade", "downgrade", "run",
               "fetch", "execute", "edit", "comment", "clear", "flush", "make-static", "release", "renew",
               "load", "save", "install", "uninstall", "apply", "cancel", "download", "send", "start", "stop")
# Скриптовые команды RouterOS без последствий для настроек.
_ROS_COLON_OK = frozenset({"put", "local", "global", "set", "foreach", "for", "if", "while", "do", "delay",
                           "len", "pick", "tostr", "tonum", "typeof", "time", "totime", "toarray", "tobool",
                           "toip", "toip6", "toid", "resolve", "return", "nothing", "find"})
# Глаголы и команды PowerShell только чтения; сессия, модуль, пауза и
# переход по каталогу ничего на сервере не меняют.
_PS_READ_VERBS = frozenset({"get", "test", "measure", "select", "where", "sort", "resolve", "show",
                            "convertto", "convertfrom", "compare", "group"})
_PS_READ_CMDLETS = frozenset({
    "format-table", "format-list", "format-wide", "format-custom", "out-string", "out-host", "out-null",
    "write-output", "write-host", "foreach-object", "invoke-command", "import-module", "start-sleep",
    "new-pssession", "enter-pssession", "exit-pssession", "remove-pssession", "new-cimsession",
    "remove-cimsession", "new-pssessionoption", "new-cimsessionoption", "set-location", "push-location",
    "pop-location", "set-strictmode", "new-timespan", "new-object", "set-variable", "new-variable"})
_PS_ALIASES = {
    "gci": "get-childitem", "ls": "get-childitem", "dir": "get-childitem", "gc": "get-content",
    "cat": "get-content", "type": "get-content", "gps": "get-process", "ps": "get-process",
    "gsv": "get-service", "select": "select-object", "where": "where-object", "?": "where-object",
    "sort": "sort-object", "ft": "format-table", "fl": "format-list", "fw": "format-wide",
    "measure": "measure-object", "gm": "get-member", "gi": "get-item", "gp": "get-itemproperty",
    "gcm": "get-command", "gal": "get-alias", "gwmi": "get-wmiobject", "gcim": "get-ciminstance",
    "sls": "select-string", "echo": "write-output", "write": "write-output", "%": "foreach-object",
    "foreach": "foreach-object", "compare": "compare-object", "diff": "compare-object", "gl": "get-location",
    "pwd": "get-location", "cd": "set-location", "sl": "set-location", "chdir": "set-location",
    "icm": "invoke-command", "h": "get-history", "history": "get-history", "gjb": "get-job",
    "gdr": "get-psdrive", "iwr": "invoke-webrequest", "irm": "invoke-restmethod", "group": "group-object",
    "nsn": "new-pssession", "etsn": "enter-pssession", "rsn": "remove-pssession", "sleep": "start-sleep",
    "ipmo": "import-module"}
_PS_KEYWORDS = frozenset({"if", "elseif", "else", "try", "catch", "finally", "while", "do", "until", "for",
                          "switch", "return", "break", "continue", "exit", "param", "begin", "process", "end",
                          "function", "filter", "throw", "trap"})
# Методы .NET без последствий: строки, даты, числа.
_PS_SAFE_METHODS = frozenset({
    "tostring", "split", "trim", "trimstart", "trimend", "replace", "substring", "contains", "startswith",
    "endswith", "tolower", "toupper", "gettype", "indexof", "padleft", "padright", "join", "format", "equals",
    "compareto", "round", "floor", "ceiling", "abs", "max", "min", "parse", "tryparse", "isnullorempty",
    "isnullorwhitespace", "adddays", "addhours", "addminutes", "addseconds", "addmonths", "addyears",
    "subtract", "tolocaltime", "touniversaltime", "toshortdatestring", "tolongdatestring", "getvalue",
    "gethostname", "gethostentry", "gethostaddresses", "exists", "where"})
# Linux и Windows: программы только чтения при любых аргументах.
_SH_READ = frozenset({
    "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "zgrep", "zcat", "zless", "bzcat",
    "xzcat", "ls", "stat", "df", "du", "free", "uptime", "w", "who", "whoami", "last", "lastb", "lastlog",
    "ps", "pgrep", "pidof", "netstat", "ping", "ping6", "traceroute", "traceroute6", "tracepath", "tracert",
    "pathping", "mtr", "dig", "nslookup", "host", "uname", "id", "groups", "lsblk", "blkid", "sensors",
    "lscpu", "lspci", "lsusb", "lsmod", "lsof", "vmstat", "iostat", "mpstat", "nproc", "getent", "wc", "cut",
    "tr", "column", "jq", "echo", "printf", "true", "false", "test", "[", "[[", "sleep", "pwd", "cd", "which",
    "whereis", "type", "file", "readlink", "realpath", "basename", "dirname", "md5sum", "sha1sum",
    "sha256sum", "sha512sum", "cksum", "base64", "tree", "diff", "cmp", "comm", "nl", "tac", "rev", "seq",
    "strings", "od", "hexdump", "xxd", "printenv", "locale", "exit", "set", "export", "local", "unset",
    "shopt", "wait", "lvs", "pvs", "vgs", "lvdisplay", "pvdisplay", "vgdisplay", "apt-cache", "dpkg-query",
    "iptables-save", "ip6tables-save", "systeminfo", "tasklist", "getmac", "quser", "qwinsta", "ver",
    "findstr", "where", "dir", "vol", "driverquery", "cls"})
_SYSTEMCTL_READ = frozenset({"status", "is-active", "is-enabled", "is-failed", "is-system-running",
                             "list-units", "list-unit-files", "list-timers", "list-sockets", "list-dependencies",
                             "list-jobs", "list-machines", "list-automounts", "list-paths", "show", "cat",
                             "show-environment", "get-default", "help"})
_SYSTEMCTL_VALUE = frozenset({"-n", "--lines", "-t", "--type", "-p", "--property", "-o", "--output", "-H",
                              "--host", "-M", "--machine", "--state"})
_DOCKER_READ = frozenset({"ps", "logs", "inspect", "images", "stats", "version", "info", "top", "port", "diff",
                          "history", "events"})
_DOCKER_GROUP_READ = frozenset({"ls", "list", "ps", "inspect", "logs", "top", "stats", "port", "diff",
                                "history", "df", "info", "events", "show"})
_COMPOSE_READ = frozenset({"ps", "logs", "config", "images", "ls", "top", "version", "port", "events"})
# Чтение пакетов: `apt list --upgradable`, `dpkg -l` перед обновлением.
_APT_READ = frozenset({"list", "show", "policy", "search", "depends", "rdepends", "changelog", "madison",
                       "showsrc"})
_DPKG_READ = frozenset({"-l", "--list", "-s", "--status", "-L", "--listfiles", "-S", "--search", "-p",
                        "--print-avail", "--get-selections", "--print-architecture", "--version", "-C", "--audit"})
_GIT_READ_REMOTE = frozenset({"status", "log", "diff", "show", "rev-parse", "describe", "ls-files", "ls-remote",
                              "blame", "shortlog", "cat-file", "ls-tree", "grep", "rev-list", "for-each-ref",
                              "show-ref", "count-objects", "help", "version", "whatchanged"})


def _read_text(text: str, lang: str = "", depth: int = 0) -> bool:
    """Весь текст из известного чтения. lang: «ros», «ps», «sh» или пусто
    (узнать по тексту)."""
    if depth > 8:
        return False
    text = text.replace("\\\n", " ")
    # Вывод в файл (`> f`, `>> f`) пишет на сервер, и внутри `sh -c '…'` тоже.
    if _REDIRECT_RE.search(danger._quote_mask(text)):
        return False
    lang = lang or _guess_lang(text)
    if lang == "ps":
        text = text.replace("`\n", " ")
    for stmt in _statements(text, lang):
        ok = _ros_read(stmt, depth) if lang == "ros" else _ps_read(stmt, depth) if lang == "ps" \
            else _sh_read(stmt, depth)
        if not ok:
            return False
    return True


_PS_VERB_NOUN_RE = re.compile(r"(?<![\w/.\\-])[A-Za-z]+-[A-Z][A-Za-z]+(?![\w-])")


def _guess_lang(text: str) -> str:
    s = text.lstrip()
    m = re.match(r"/([a-z][a-z0-9-]*)", s)
    if s.startswith(":") or (m and m.group(1) in _ROS_MENUS):
        return "ros"
    masked = danger._quote_mask(text)
    if any(m.group(0).split("-", 1)[0].lower() in _PS_VERBS for m in _PS_VERB_NOUN_RE.finditer(masked)) \
            or re.search(r"\$_\b|(?:^|[;\n{])\s*\$\w+\s*=[^=]", masked):
        return "ps"
    return "sh"


_PS_VERBS = frozenset({
    "add", "approve", "assert", "backup", "block", "checkpoint", "clear", "close", "compare", "complete",
    "compress", "confirm", "connect", "convert", "convertfrom", "convertto", "copy", "debug", "deny", "disable",
    "disconnect", "dismount", "edit", "enable", "enter", "exit", "expand", "export", "find", "format", "get",
    "grant", "group", "hide", "import", "initialize", "install", "invoke", "join", "limit", "lock", "measure",
    "merge", "mount", "move", "new", "open", "optimize", "out", "ping", "pop", "protect", "publish", "push",
    "read", "receive", "redo", "register", "remove", "rename", "repair", "request", "reset", "resize",
    "resolve", "restart", "restore", "resume", "revoke", "save", "search", "select", "send", "set", "show",
    "skip", "split", "start", "step", "stop", "submit", "suspend", "switch", "sync", "test", "trace", "unblock",
    "undo", "uninstall", "unlock", "unprotect", "unpublish", "unregister", "update", "use", "wait", "watch",
    "where", "write", "foreach", "sort", "tee"})


def _statements(text: str, lang: str) -> list[str]:
    """Простые команды: деление по `;`, переводу строки, `|` и `&` вне кавычек
    и скобок. `2>&1` не деление; у PowerShell `&` в начале команды это вызов."""
    out: list[str] = []
    cur: list[str] = []
    depth, quote, i = 0, "", 0
    while i < len(text):
        c = text[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and lang != "ps" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\" and lang != "ps" and i + 1 < len(text):
            cur += [c, text[i + 1]]
            i += 2
            continue
        if c in "'\"":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and (c in ";\n|" or (c == "&" and text[i - 1:i] not in (">", "<")
                                             and text[i + 1:i + 2] != ">" and "".join(cur).strip())):
            out.append("".join(cur))
            cur = []
            i += 2 if text[i:i + 2] == "&&" else 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def _groups(stmt: str, opens: str) -> tuple[str, list[str]] | None:
    """Содержимое скобок верхнего уровня из opens и текст, где они заменены
    на `\\x00`. None: скобки не закрыты."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    flat: list[str] = []
    groups: list[str] = []
    stack: list[str] = []
    start, quote = 0, ""
    for i, c in enumerate(stmt):
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c in pairs:
            if not stack and c in opens:
                start = i + 1
            stack.append(c)
        elif c in pairs.values() and stack:
            opener = stack.pop()
            if not stack and opener in opens:
                groups.append(stmt[start:i])
                flat.append("\x00")
                continue
        if not stack or stack[0] not in opens:
            flat.append(c)
    return None if stack or quote else ("".join(flat), groups)


def _ros_read(stmt: str, depth: int) -> bool:
    """RouterOS: `print`, `get`, `monitor`, `export` без `file=`, `/log print`,
    ping, traceroute, `/tool torch`; `[…]` и `{…}` внутри тоже чтение."""
    parsed = _groups(stmt, "[{")
    if parsed is None:
        return False
    flat, groups = parsed
    if not all(_read_text(g, "ros", depth + 1) for g in groups):
        return False
    s = flat.strip()
    if s.startswith(":"):
        m = re.match(r":([a-z]+)", s)
        return bool(m) and m.group(1) in _ROS_COLON_OK
    for w in re.split(r"[\s/]+", s.lstrip("/")):
        if not re.fullmatch(r"[a-z][a-z0-9-]*", w):
            break
        if w in _ROS_READ or (len(w) >= 2 and "print".startswith(w)) or (len(w) >= 3 and "export".startswith(w)):
            # `print file=x` и `export file=x` пишут файл на роутер.
            return not re.search(r"(?<![\w-])file=", danger._quote_mask(s))
        if len(w) >= 2 and any(v.startswith(w) for v in _ROS_CHANGE):
            return False
    return False


def _mask_single(text: str) -> str:
    """Содержимое строк в одинарных кавычках PowerShell заменено пробелами."""
    return re.sub(r"'[^']*'", lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", text)


def _subexprs(text: str) -> list[str] | None:
    """`$(…)` PowerShell, в том числе внутри строки в двойных кавычках."""
    out = []
    for m in re.finditer(r"\$\(", text):
        depth, j = 1, m.end()
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        if depth:
            return None
        out.append(text[m.end():j - 1])
    return out


def _ps_read(stmt: str, depth: int) -> bool:
    """PowerShell: Get-, Test-, Measure-, Select-, Where-, Sort-, вывод
    Format-Table и Format-List, Resolve-, Find- (кроме установки), Show-,
    ConvertTo-, ConvertFrom-, Compare-, Write-Output и Write-Host; блоки
    `{…}`, `(…)` и `$(…)` внутри тоже. Вызов `&`, точка, метод .NET вне
    короткого списка и `Invoke-Command -FilePath` (скрипт не виден): нет."""
    subs = _subexprs(_mask_single(stmt))
    if subs is None or not all(_read_text(x, "ps", depth + 1) for x in subs):
        return False
    masked = danger._quote_mask(stmt)
    if any(m.group(1).lower() not in _PS_SAFE_METHODS
           for m in re.finditer(r"(?:\.|::)\s*(\w+)\s*\(", masked)):
        return False
    parsed = _groups(stmt, "({")
    if parsed is None:
        return False
    flat, groups = parsed
    if not all(_read_text(g, "ps", depth + 1) for g in groups):
        return False
    s = flat.strip()
    if not s:
        return True
    if s[0] in "&.":
        return False
    first = s.split(None, 1)[0]
    rest = s[len(first):]
    if first[0] in "$'\"\x00@[" or re.match(r"-?\d", first):
        # Выражение; присваивание проверяется по правой части.
        m = re.match(r"\$[\w:.\[\]]+\s*[-+*/]?=(?!=)\s*(.+)", s, re.DOTALL)
        return _ps_read(m.group(1), depth + 1) if m else True
    name = first.lower()
    if name in _PS_KEYWORDS:
        return True
    name = _PS_ALIASES.get(name, name)
    if name in _PS_READ_CMDLETS:
        if name == "foreach-object" and any(not w.startswith(("-", "\x00")) for w in rest.split()):
            # `% Kill`: метод с этим именем у каждого объекта.
            return False
        return not (name == "invoke-command" and re.search(r"(?<![\w-])-f", rest, re.IGNORECASE))
    verb, _sep, noun = name.partition("-")
    if noun and re.fullmatch(r"[a-z]\w*", noun) and (
            verb in _PS_READ_VERBS or (verb == "find" and "install" not in noun)):
        # `Test-ComputerSecureChannel -Repair` меняет сервер (guard3, Н10).
        # `-Force` у Get-* показывает скрытое (`Get-ChildItem -Force`), это
        # чтение, как было до guard3 (ревью PR 10).
        flags = r"repair|fix" if verb == "get" else r"repair|force|fix"
        return not re.search(rf"(?<![\w-])-(?:{flags})(?![\w-])", rest, re.IGNORECASE)
    if name in ("invoke-webrequest", "invoke-restmethod", "curl", "wget"):
        return _iwr_read(rest)
    return _words_read(_split_words(s), depth)


def _iwr_read(rest: str) -> bool:
    """Invoke-WebRequest и Invoke-RestMethod: GET без тела и файла."""
    words = rest.split()
    for i, w in enumerate(words):
        lw = w.lower()
        if lw.startswith(("-b", "-inf", "-o", "-f", "-cu")):
            return False
        if lw.startswith("-me") and (i + 1 >= len(words) or words[i + 1].strip("'\"").lower() not in ("get", "head")):
            return False
    return True


def _split_words(s: str) -> list[str]:
    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def _sh_read(stmt: str, depth: int) -> bool:
    """Linux и Windows: подоболочка и группа по содержимому, подстановка
    `$(…)`, обратные кавычки и `<(…)` нет (текст не виден)."""
    if "$(" in stmt or "`" in stmt or "<(" in stmt or ">(" in stmt:
        return False
    s = stmt.strip()
    if s[:1] in "({" and s[-1:] in ")}":
        return _read_text(s[1:-1].strip(), "sh", depth + 1)
    # `ls>/dev/null`: вывод отделяется от слова, иначе имя программы `null`.
    # Запись в файл уже отсеяна _REDIRECT_RE.
    for m in reversed(list(re.finditer(r"(?<=[^\s\d&<>=-])>", danger._quote_mask(s)))):
        s = s[:m.start()] + " " + s[m.start():]
    try:
        words = shlex.split(s, posix=True)
    except ValueError:
        return False
    return _words_read(words, depth)


_WRAP_VALUE = {"nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-p", "-t", "--class", "--classdata"},
               "stdbuf": {"-i", "-o", "-e"}, "watch": {"-n", "--interval"}, "nohup": set(), "time": {"-f", "-o"},
               "command": set(), "exec": {"-a"},
               "xargs": {"-n", "-I", "-P", "-d", "-L", "-s", "-a", "-E", "--max-args", "--max-procs",
                         "--delimiter", "--arg-file", "--replace", "--max-lines"},
               "env": {"-u", "--unset", "-C", "--chdir"}, "timeout": {"-s", "-k", "--signal", "--kill-after"},
               "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "--user", "--group", "--host",
                        "--prompt", "--chdir"}}


def _unwrap(low: str, args: list[str]) -> list[str]:
    """Команда, которую запустит обёртка low: её аргументы без её ключей."""
    i = 0
    while i < len(args) and (args[i].startswith("-") or (low == "env" and "=" in args[i])):
        i += 2 if args[i] in _WRAP_VALUE[low] else 1
    if low == "timeout" and i < len(args):
        i += 1
    return args[i:]


def _xargs_read(rest: list[str]) -> bool:
    """Команда после xargs: поток дописывает ей аргументы, а с `-I` и саму
    команду. Чтение только программа из _SH_READ (читает при любых
    аргументах), в том числе за обёртками (`xargs sudo cat`); оболочка,
    интерпретатор (python, perl, ruby, node), ssh и всё прочее изменение."""
    if not rest:
        # xargs без команды зовёт echo.
        return True
    while True:
        name = Path(rest[0].replace("\\", "/")).name.lower().removesuffix(".exe")
        if name not in _WRAP_VALUE or name == "watch":
            return name in _SH_READ
        if name == "sudo" and {"-i", "-s", "-e", "--login", "--shell", "--edit"} & set(rest[1:]):
            return False
        rest = _unwrap(name, rest[1:])
        if not rest:
            # `xargs sudo`: команду даёт поток.
            return False


def _words_read(words: list[str], depth: int) -> bool:
    while words and words[0] in ("do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"):
        words = words[1:]
    if not words or words[0] in ("done", "fi", "esac", "for"):
        # `for x in a b`: значения видны, подстановка отсечена выше.
        return True
    while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0], re.DOTALL):
        words = words[1:]
    if not words:
        return True
    prog, args = Path(words[0].replace("\\", "/")).name, words[1:]
    low = prog.lower().removesuffix(".exe")
    if low == "command" and {"-v", "-V"} & set(args):
        return True
    if low in _WRAP_VALUE:
        if low == "sudo" and {"-i", "-s", "-e", "--login", "--shell", "--edit"} & set(args):
            return False
        rest = _unwrap(low, args)
        if low == "xargs":
            return _xargs_read(rest)
        if not rest:
            return low != "watch"
        return _read_text(" ".join(rest), "sh", depth + 1) if low == "watch" else _words_read(rest, depth + 1)
    if low in ("bash", "sh", "dash", "zsh"):
        flags = [a for a in args if a.startswith("-")]
        return (len(args) >= 2 and "-c" in flags and args.index("-c") + 1 < len(args)
                and _read_text(args[args.index("-c") + 1], "sh", depth + 1))
    if low in ("ssh", "autossh"):
        opts, i = _ssh_opts(args, low == "autossh")
        if _ssh_forwards(opts) or any(c == "o" and "remotecommand" in v.lower() for c, v in opts):
            return False
        return _read_text(" ".join(args[i + 1:]), "", depth + 1)
    if low == "cmd":
        rest = next((args[j + 1:] for j, a in enumerate(args) if a.lower() in ("/c", "/k")), None)
        return rest is not None and _read_text(" ".join(rest), "", depth + 1)
    if low in ("powershell", "pwsh"):
        kind, value = _ps_command(args)
        return kind == "command" and _read_text(value, "ps", depth + 1)
    return _prog_read(low, args)


def _short_letters(args: list[str]) -> set[str]:
    """Буквы коротких ключей: `-nvL` даёт n, v, L."""
    return {c for a in args if a.startswith("-") and not a.startswith("--") and not re.fullmatch(r"-\d+", a)
            for c in a[1:]}


def _prog_read(name: str, args: list[str]) -> bool:
    if name in _SH_READ:
        return True
    low = [a.lower() for a in args]
    pos = [a for a in args if not a.startswith("-")]
    check = _READ_CHECKS.get(name)
    try:
        return bool(check and check(args, low, pos))
    except (IndexError, ValueError):
        return False


def _first_pos(args: list[str], value_flags: Iterable[str]) -> list[str]:
    """Позиционные аргументы без значений ключей из value_flags."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in value_flags:
            i += 2
            continue
        if not a.startswith("-"):
            out.append(a)
        i += 1
    return out


def _systemctl_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    p = _first_pos(args, _SYSTEMCTL_VALUE)
    return not p or p[0] in _SYSTEMCTL_READ


def _docker_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    p = _first_pos(args, {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "-f", "--file",
                          "-p", "--project-name", "--env-file", "--profile"})
    if not p:
        return True
    if p[0] == "compose":
        return len(p) < 2 or p[1] in _COMPOSE_READ
    if p[0] in ("container", "image", "volume", "network", "system", "context", "node", "service", "stack"):
        return len(p) >= 2 and p[1] in _DOCKER_GROUP_READ
    return p[0] in _DOCKER_READ


def _apt_get_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    simulate = {"-s", "--simulate", "--dry-run", "--just-print", "--recon", "--no-act"}
    return bool(simulate & set(args)) or "s" in _short_letters(args) or pos[:1] == ["changelog"]


def _date_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    if {"-s", "--set"} & set(args) or any(a.startswith("--set=") for a in args):
        return False
    # Склейка `-s@0`, `-us…` (guard3, Н10): первая буква со значением решает.
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            letter = next((c for c in a[1:] if c in "dfIrRs"), "")
            if letter == "s":
                return False
    return all(p.startswith("+") for p in _first_pos(args, {"-d", "--date", "-r", "--reference", "-f", "--file"}))


def _ip_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    rest = list(args)
    while rest and rest[0].startswith("-"):
        if rest[0] in ("-b", "-batch", "-force"):
            return False
        rest = rest[2:] if rest[0] in ("-n", "-netns", "-f", "-family", "-rc", "-rcvbuf", "-l", "-loops") else rest[1:]
    return len(rest) < 2 or rest[1] in ("show", "sh", "list", "ls", "lst", "get", "monitor", "help")


def _smartctl_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    bad = {"-t", "--test", "-s", "--smart", "-o", "--offlineauto", "-S", "--saveauto", "-X", "--abort", "--set"}
    return not bad & set(args) and not any(a.startswith(("--test=", "--smart=", "--set=", "--offlineauto=",
                                                         "--saveauto=")) for a in args)


def _mdadm_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    read = {"--detail", "-D", "--examine", "-E", "--query", "-Q", "--detail-platform"}
    change = {"--create", "-C", "--assemble", "-A", "--manage", "--add", "-a", "--remove", "-r", "--fail", "-f",
              "--grow", "-G", "--stop", "-S", "--zero-superblock", "--run", "-R", "--readonly", "-o",
              "--readwrite", "-w", "--re-add", "--replace", "--monitor", "-F", "--build", "-B", "--update", "-U"}
    return bool(read & set(args)) and not change & set(args)


def _openssl_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    if not args or {"-out", "-sess_out", "-keylogfile", "-msgfile"} & set(args):
        return False
    if args[0] in ("version", "ciphers", "verify", "s_client"):
        return True
    return args[0] in ("x509", "req", "crl") and "-noout" in args


# Ключи curl, которые шлют данные или пишут файл: `-d`, `-F`, `-T`, `-o`, `-O`,
# `-K` (ключи из файла), `-c` (файл cookie), `-J`, `-Q` (команды FTP).
_CURL_BAD_SHORT = frozenset("dFTOoKcJQ")
_CURL_VALUE_SHORT = frozenset("XDHuAebmxwErYyzCPtUh")
_CURL_BAD_LONG = ("--data", "--form", "--upload-file", "--output", "--remote-name", "--json", "--config",
                  "--cookie-jar", "--trace", "--quote", "--libcurl", "--etag-save", "--hsts", "--alt-svc",
                  "--create-dirs", "--post")


def _curl_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    """curl только GET или HEAD, без данных, загрузки и записи файла."""
    i = 0
    while i < len(args):
        a, nxt = args[i], args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name.startswith(_CURL_BAD_LONG):
                return False
            if name in ("--request", "--dump-header", "--stderr"):
                v = val if eq else nxt
                if (name == "--request" and v.upper() not in ("GET", "HEAD")) or (name != "--request" and v != "-"):
                    return False
                i += 0 if eq else 1
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:]
            for j, ch in enumerate(letters):
                if ch in _CURL_BAD_SHORT:
                    return False
                if ch in _CURL_VALUE_SHORT:
                    v = letters[j + 1:] or nxt
                    if (ch == "X" and v.upper() not in ("GET", "HEAD")) or (ch == "D" and v != "-"):
                        return False
                    i += 0 if letters[j + 1:] else 1
                    break
        i += 1
    return True


def _wget_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    """wget только на stdout (`-O -`, `-qO-`) или `--spider`, без отправки данных."""
    to_stdout = False
    i = 0
    while i < len(args):
        a, nxt = args[i], args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            v = val if eq else nxt
            if name in ("--post-data", "--post-file", "--body-data", "--body-file", "--output-file",
                        "--append-output", "--recursive", "--mirror", "--page-requisites", "--directory-prefix",
                        "--execute", "--force-directories", "--convert-links"):
                return False
            if name == "--method" and v.upper() not in ("GET", "HEAD"):
                return False
            if name == "--spider" or (name == "--output-document" and v == "-"):
                to_stdout = True
            if name in ("--method", "--output-document") and not eq:
                i += 1
        elif a in ("-nv", "-nc", "-nd", "-nH", "-np"):
            pass
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:]
            for j, ch in enumerate(letters):
                if ch in "oarmpPexk":
                    return False
                if ch in "OTtUiBwQlDARIX":
                    v = letters[j + 1:] or nxt
                    if ch == "O" and v == "-":
                        to_stdout = True
                    i += 0 if letters[j + 1:] else 1
                    break
        i += 1
    return to_stdout


def _git_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i].startswith("-c"):
            # `-c core.pager=…` исполнил бы команду.
            return False
        i += 2 if args[i] in ("-C", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(args):
        return True
    sub, rest = args[i], args[i + 1:]
    if any(a.startswith("--output") for a in rest):
        return False
    rpos = [r for r in rest if not r.startswith("-")]
    if sub in _GIT_READ_REMOTE:
        return True
    if sub == "branch":
        return not rpos and not {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "-u",
                                 "--set-upstream-to", "--unset-upstream", "-f", "--force",
                                 "--edit-description"} & set(rest)
    if sub == "tag":
        return not rpos or bool({"-l", "--list"} & set(rest))
    if sub == "remote":
        return not rpos or rpos[0] in ("show", "get-url")
    if sub == "config":
        return bool({"--get", "--get-all", "--get-regexp", "-l", "--list"} & set(rest))
    if sub == "worktree":
        return rpos[:1] == ["list"]
    if sub == "stash":
        return rpos[:1] in (["list"], ["show"])
    if sub == "reflog":
        return rpos[:1] in ([], ["show"])
    return False


def _kubectl_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    p = _first_pos(args, {"-n", "--namespace", "--context", "--kubeconfig", "-o", "--output", "-l", "--selector",
                          "-c", "--container"})
    if not p:
        return True
    if p[0] == "config":
        return p[1:2] in (["view"], ["get-contexts"], ["current-context"])
    if p[0] == "auth":
        return p[1:2] == ["can-i"]
    return p[0] in ("get", "describe", "logs", "top", "version", "explain", "api-resources", "api-versions",
                    "cluster-info", "events")


def _awk_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    if {"-f", "--file", "-i", "--include", "-l", "--load", "-E", "--exec"} & set(args) or any(
            a.startswith(("--file=", "--include=", "--exec=", "--load=")) for a in args):
        return False
    # Программа gawk из `--source` и `-e` (guard3, Н10) проверяется как обычная.
    sources = _ansible_values(args, ("-e", "--source"))
    p = _first_pos(args, {"-F", "-v", "--field-separator", "--assign", "-e", "--source"})
    programs = sources or (p[:1] if p else [""])
    return not any(re.search(r"\bsystem\s*\(|\bgetline\b|\|", program) or re.search(r"\bprintf?\b[^;}]*>", program)
                   for program in programs)


def _sed_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    if any(a.startswith(("--in-place", "--file")) for a in args):
        return False
    # Скрипт из `-e X`, `--expression=X`, склейки `-eX` и `-ne X` (guard3, Н10);
    # `-f` берёт скрипт из файла, его не видно.
    scripts: list[str] = []
    named = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression"):
            named = True
            scripts += args[i + 1:i + 2]
            i += 2
            continue
        if a.startswith("--expression="):
            named = True
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for j, c in enumerate(a[1:], 1):
                if c == "i" or c == "f":
                    return False
                if c in "el":
                    if c == "e":
                        named = True
                        value = a[j + 1:]
                        if not value and i + 1 < len(args):
                            i += 1
                            value = args[i]
                        scripts.append(value)
                    elif not a[j + 1:]:
                        i += 1
                    break
        i += 1
    if not named:
        scripts = pos[:1]
    return not any(re.search(r"(?:^|[;{}\n0-9$/])\s*[weW](?:\s|$)", s) for s in scripts)


def _net_read(args: list[str], low: list[str], pos: list[str]) -> bool:
    if not low:
        return False
    sub, rest = low[0], low[1:]
    opts = [a for a in rest if a.startswith("/")]
    names = [a for a in rest if not a.startswith("/")]
    if sub in ("view", "statistics"):
        return True
    if sub == "config":
        return not opts
    if sub in ("user", "group", "localgroup", "share", "session"):
        return len(names) <= 1 and all(o == "/domain" for o in opts)
    if sub in ("use", "start", "accounts"):
        return not rest
    return False


_READ_CHECKS: dict[str, Callable[[list[str], list[str], list[str]], bool]] = {
    "find": lambda a, low, p: not {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
                                   "-fprintf", "-fls"} & set(a),
    "journalctl": lambda a, low, p: not any(x.startswith((
        "--vacuum", "--rotate", "--flush", "--sync", "--relinquish-var", "--smart-relinquish-var", "--setup-keys",
        "--update-catalog", "--cursor-file")) for x in a),
    "systemctl": _systemctl_read,
    "docker": _docker_read, "podman": _docker_read,
    "docker-compose": lambda a, low, p: not p or p[0] in _COMPOSE_READ,
    "apt": lambda a, low, p: bool(p) and p[0] in _APT_READ,
    "apt-get": _apt_get_read,
    "dpkg": lambda a, low, p: bool(a) and a[0] in _DPKG_READ,
    "date": _date_read,
    "hostname": lambda a, low, p: not p and not {"-F", "--file", "-b", "--boot"} & set(a),
    "mount": lambda a, low, p: a in ([], ["-l"]),
    "ip": _ip_read,
    "smartctl": _smartctl_read,
    "mdadm": _mdadm_read,
    "openssl": _openssl_read,
    "curl": _curl_read,
    "wget": _wget_read,
    "git": _git_read,
    "kubectl": _kubectl_read,
    "uniq": lambda a, low, p: len(_first_pos(a, {"-f", "-s", "-w"})) <= 1,
    "sort": lambda a, low, p: "o" not in _short_letters(a) and not any(x.startswith("--output") for x in a),
    "awk": _awk_read, "gawk": _awk_read, "mawk": _awk_read,
    "sed": _sed_read,
    "top": lambda a, low, p: "b" in _short_letters(a) or "--batch" in a,
    "dmesg": lambda a, low, p: not {"c", "C", "D", "E", "n"} & _short_letters(a) and not any(
        x.startswith(("--clear", "--read-clear", "--console")) for x in a),
    "ss": lambda a, low, p: "K" not in _short_letters(a) and "--kill" not in a,
    "arp": lambda a, low, p: not {"d", "s", "f"} & _short_letters(a) and not {"--delete", "--set", "--file"} & set(a),
    "route": lambda a, low, p: not p or low[-1:] == ["print"] and len(p) == 1,
    "iptables": lambda a, low, p: bool({"L", "S"} & _short_letters(a) or {"--list", "--list-rules"} & set(a))
    and not set("AIDRFZNXPE") & _short_letters(a) and not any(x in a for x in (
        "--append", "--insert", "--delete", "--replace", "--flush", "--zero", "--new-chain", "--delete-chain",
        "--policy", "--rename-chain")),
    "nft": lambda a, low, p: p[:1] == ["list"],
    "ufw": lambda a, low, p: p[:1] in (["status"], ["show"], ["version"]) or p[:2] == ["app", "list"],
    "fail2ban-client": lambda a, low, p: p[:1] in (["status"], ["ping"], ["version"], ["get"], ["banned"]),
    "crontab": lambda a, low, p: "-l" in a and not {"-r", "-e", "-i"} & set(a),
    "timedatectl": lambda a, low, p: not p or p[0] in ("status", "show", "list-timezones", "timesync-status",
                                                       "show-timesync"),
    "tcpdump": lambda a, low, p: not {"w", "z", "Z"} & _short_letters(a),
    "zpool": lambda a, low, p: p[:1] in (["status"], ["list"], ["iostat"], ["get"], ["history"]),
    "zfs": lambda a, low, p: p[:1] in (["list"], ["get"]),
    "ipconfig": lambda a, low, p: all(x in ("/all", "/displaydns", "/?") for x in low),
    "wevtutil": lambda a, low, p: low[:1] in (["qe"], ["query-events"], ["el"], ["enum-logs"], ["gl"],
                                              ["get-log"], ["gli"], ["get-log-info"], ["gp"], ["get-publisher"],
                                              ["ep"], ["enum-publishers"]),
    "repadmin": lambda a, low, p: bool(low) and (low[0].startswith("/show") or low[0] in (
        "/replsummary", "/queue", "/istg", "/bridgeheads", "/failcache", "/latency")),
    "dcdiag": lambda a, low, p: "/fix" not in low,
    "w32tm": lambda a, low, p: low[:1] in (["/query"], ["/stripchart"], ["/monitor"], ["/tz"]),
    "netsh": lambda a, low, p: "show" in low and not {"set", "add", "delete", "reset", "import", "export",
                                                       "install", "uninstall", "flush", "start", "stop", "exec",
                                                       "connect", "disconnect", "rename"} & set(low),
    "vssadmin": lambda a, low, p: low[:1] == ["list"],
    "wbadmin": lambda a, low, p: low[:1] == ["get"],
    "sc": lambda a, low, p: [x for x in low if not x.startswith("\\\\")][:1] in (
        ["query"], ["queryex"], ["qc"], ["qdescription"], ["qfailure"], ["getdisplayname"], ["getkeyname"],
        ["enumdepend"], ["sdshow"]),
    "reg": lambda a, low, p: low[:1] in (["query"], ["compare"]),
    "net": _net_read,
    "wmic": lambda a, low, p: bool({"get", "list"} & set(low)) and not {"call", "delete", "set", "create"} & set(low),
    "schtasks": lambda a, low, p: low[:1] == ["/query"],
    "gpresult": lambda a, low, p: not {"/h", "/x"} & set(low),
    "bcdedit": lambda a, low, p: low[:1] in ([], ["/enum"], ["/v"]),
    "query": lambda a, low, p: low[:1] in (["user"], ["session"], ["process"], ["termserver"]),
}
_READ_CHECKS["ip6tables"] = _READ_CHECKS["iptables"]
_READ_CHECKS["hostnamectl"] = _READ_CHECKS["timedatectl"]


danger_ssh_value_flags = frozenset({"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-W", "-b", "-c",
                                    "-E", "-e", "-I", "-m", "-O", "-Q", "-S", "-w", "-B", "-M", "-P", "-s"})


# Буквы curl со значением: `-sK файл` и `-sKфайл` это `-K файл` (ревью PR 10).
_CURL_VALUE_LETTERS = _CURL_VALUE_SHORT | frozenset("dFToKcQ")


def _curl_opt(args: list[str], letter: str, longs: tuple[str, ...]) -> list[str]:
    """Значения ключа curl: `-K x`, `-Kx`, склейка `-sK x`, `--config=x`, `--config x`."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in longs:
                if eq:
                    out.append(val)
                elif i + 1 < len(args):
                    out.append(args[i + 1])
                    i += 1
        elif a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], 1):
                if ch in _CURL_VALUE_LETTERS:
                    v = a[j + 1:]
                    if not v and i + 1 < len(args):
                        v = args[i + 1]
                        i += 1
                    if ch == letter:
                        out.append(v)
                    break
        i += 1
    return out


def _curl_urls(args: list[str]) -> list[str]:
    """Адреса запроса: слова со схемой и значения `--url` (`--url=x`, `--url x`)."""
    return list(dict.fromkeys([a for a in args if a.startswith(("http://", "https://"))]
                              + _curl_opt(args, "", ("--url",))))


def _upload(args: list[str], out: set[str], secret_cmd: bool = False, raw: list[str] | None = None) -> None:
    """Файл на сайт (`-F x=@f`, `-T f`, `-d @f`) и секрет в запросе. Данные
    текстом в API без карточки: так читаются GraphQL и поиск."""
    urls = _curl_urls(args)
    local = bool(urls) and all(re.match(r"https?://(?:localhost|127\.\d+\.\d+\.\d+|\[::1\])(?:[:/?#]|$)", u)
                               for u in urls)
    file_up = False
    for i, a in enumerate(args):
        prev = args[i - 1] if i else ""
        if a in ("-T", "--upload-file") or a.startswith(("--upload-file=", "--post-file", "-T")) and len(a) > 2:
            file_up = True
        # Значение формы: `-F f=@x`, `-Ff=@x`, `--form=f=<x`; данных: `-d @x`, `-d@x`, `--json @x`.
        form = a[2:] if a.startswith("-F") and len(a) > 2 else a.split("=", 1)[1] if a.startswith("--form=") \
            else a if prev in ("-F", "--form", "--form-string") else None
        if form is not None and re.search(r"=[@<]", form):
            file_up = True
        data = a[2:] if a.startswith("-d") and len(a) > 2 else a.split("=", 1)[1] if a.startswith(
            ("--data", "--json")) and "=" in a else a if prev in (
            "-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "--json") else None
        if data is not None and (data.startswith("@") or re.match(r"\w*@[^\s]", data) and prev == "--data-urlencode"):
            file_up = True
    if file_up and not local:
        out.add(_("загружает файл на сайт"))
    # Ключ в заголовке (`-H "Authorization: …"`) уходит своему сервису, это
    # обычный вход; ключ в адресе или теле уходит кому угодно.
    body = [a for i, a in enumerate(args) if not (i and args[i - 1] in ("-H", "--header"))
            and not a.startswith(("-H", "--header="))]
    raw_body = [a for i, a in enumerate(raw or []) if not (i and (raw or [])[i - 1] in ("-H", "--header"))
                and not a.startswith(("-H", "--header="))]
    if not local and any(_SECRET_VAR_RE.search(a) or (secret_cmd and "$" in a) for a in body + raw_body):
        out.add(_("секрет уходит в сеть"))


_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname", "-H", "--header", "-X", "--method", "-q", "--jq",
                             "-t", "--template", "--title", "-b", "--body", "-F", "-f", "--field",
                             "--raw-field", "--input", "-B", "--base", "-l", "--label"})


def _gh(args: list[str], out: set[str]) -> None:
    pos: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in _GH_VALUE_FLAGS:
            i += 2
            continue
        if not args[i].startswith("-"):
            pos.append(args[i])
        i += 1
    if pos[:2] == ["repo", "edit"] and any(a.startswith("--visibility") for a in args):
        out.add(_("меняет видимость репозитория на GitHub"))
    elif pos[:2] in (["release", "delete"], ["release", "delete-asset"]):
        out.add(_("удаление во внешнем сервисе"))
    elif pos[:2] in (["pr", "create"], ["pr", "comment"], ["pr", "merge"], ["pr", "review"], ["issue", "create"],
                   ["issue", "comment"], ["pr", "edit"], ["issue", "edit"], ["release", "create"], ["release", "upload"], ["repo", "delete"],
                   ["gist", "create"], ["repo", "create"]):
        out.add(_("публикация на GitHub (gh {a} {b})").format(a=pos[0], b=pos[1]))
    elif pos[:2] == ["api", "graphql"]:
        if re.search(r"\bmutation\b", " ".join(args)) or "--input" in args:
            out.add(_("изменение через GitHub API"))
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
            out.add(_("изменение через GitHub API"))


# Программы, которые пишут названный файл: для настроек Н11 только они
# (запись и редактор, перенос, удаление, права), и правка на месте `-i`.
_N11_WRITERS = frozenset({"mv", "rm", "rmdir", "unlink", "truncate", "touch", "tee", "sponge", "dd", "chmod", "chown",
                          "chgrp", "patch", "shred", "vi", "vim", "nvim", "nano", "emacs", "ed", "ex"})
_INPLACE_PROGS = frozenset({"yq", "perl", "ruby"})


def _guard_step(prog: str, args: list[str], cwd: str | None, out: set[str],
                values: dict[str, str] | None = None) -> None:
    """Не читающая программа называет путь защиты: `sed -i`, `cp`, `mv`,
    `patch`, `pip install` в окружение бота. Настройки Н11 считаются только
    там, где программа пишет названное (ревью PR 10): `yq . .mcp.json`,
    `systemd-analyze verify юнит`, `ssh -F ~/.ssh/config` их читают."""
    n11 = True
    if prog == "git":
        sub, rest, _c = danger._git_sub(args)
        if sub in _GIT_READ:
            return
        names = rest
        for i, a in enumerate(args[:len(args) - len(rest)]):
            if a == "-C" and i + 1 < len(args):
                cwd = _abs(args[i + 1], cwd)
            elif a.startswith("-C") and len(a) > 2:
                cwd = _abs(a[2:], cwd)
            elif a.startswith("--work-tree="):
                cwd = _abs(a.split("=", 1)[1], cwd)
            elif a == "--work-tree" and i + 1 < len(args):
                cwd = _abs(args[i + 1], cwd)
    elif prog == "sed":
        if not any(a == "-i" or a.startswith(("-i", "--in-place")) or
                   (a.startswith("-") and not a.startswith("--") and "i" in a[1:]) for a in args):
            return
        names = args
    elif prog in ("sort", "uniq") and (any(a.startswith(("-o", "--output")) for a in args) or prog == "uniq"):
        names = danger._positionals(args)[-1:] + danger._option_values(args, "o", "--output")
    elif prog in _READERS or prog == "=":
        # Присваивание `P=…/.venv/bin/python` путь только запоминает.
        return
    elif prog in ("cp", "install", "ln", "rsync"):
        # Источник только читается: в защиту пишет последний операнд.
        names = danger._positionals(args)[-1:]
    elif prog in ("tar", "unzip"):
        # Распаковка в каталог бота пишет в защиту, не называя файлов.
        names = [a for a in danger._option_values(args, "C", "--directory")
                 + danger._option_values(args, "d", "--exdir")]
        if any(BOT_ROOT == Path(p) or Path(p) in BOT_ROOT.parents or _guard(p)
               for p in filter(None, (_abs(n, cwd) for n in names))) or (
                not names and cwd and (Path(cwd) == BOT_ROOT or _guard(cwd))):
            if prog == "unzip" or {"-x", "--extract", "--get"} & danger._flags(args) or any(
                    a.startswith("-") and not a.startswith("--") and "x" in a for a in args) or (
                    args and not args[0].startswith("-") and "x" in args[0]):
                out.add(_("распаковка в каталог бота"))
        return
    else:
        names = args
        n11 = prog in _N11_WRITERS or (prog in _INPLACE_PROGS and any(
            a.startswith(("--in-place", "--inplace")) or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
            for a in args))
    for a in names:
        for word in (a, a.split("=", 1)[-1]):
            if not word or word.startswith("-"):
                continue
            expanded = _expand(word, values or {})
            options = [expanded] if expanded is not None else [word] + [
                _VAR_RE.sub(lambda m, v=v, n=n: v if m.group(1) == n else m.group(0), word)
                for n, vs in _ALL_VALUES.get().items() for v in vs if f"${n}" in word or f"${{{n}}}" in word]
            # Удаление байткода модуль не подменяет: `__pycache__` пересоберётся.
            if any((_guard(_abs(w, cwd), n11) and not (
                    prog in ("rm", "unlink") and "__pycache__" in Path(_abs(w, cwd) or "").parts))
                   or (prog in ("mv", "rm", "rmdir") and _holds_guard(_abs(w, cwd)))
                   for w in options):
                out.add(_("правка защиты бота"))
                return


def _redirect_to_guard(command: str, cwd: str | None, verdict: danger.Verdict, values: dict[str, str]) -> bool:
    """`> файл` и `>> файл` с путём из переменной: danger такую цель не
    раскрывает и защитой не помечает (ревью Opus 03.11.2026)."""
    tokens = danger._tokenize(danger._newlines_to_separators(command)) or []
    targets = [tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t in danger._REDIRECTS]
    dirs = {cwd, *(d for _p, _a, d in _steps(verdict, cwd))}
    options = []
    for t in targets:
        w = _expand(t, values)
        options += [w] if w is not None else [t] + [
            _VAR_RE.sub(lambda m, v=v, n=n: v if m.group(1) == n else m.group(0), t)
            for n, vs in _ALL_VALUES.get().items() for v in vs if f"${n}" in t or f"${{{n}}}" in t]
    return any(_guard(_abs(w, d)) for w in options for d in dirs if d or w.startswith(("/", "~")))


def _names_guard(command: str, cwd: str | None, verdict: danger.Verdict) -> bool:
    """Перенаправление `> файл` в защиту: danger видит запись в защищённое,
    здесь сверка с узким перечнем по словам команды."""
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        words = command.split()
    dirs = [cwd] + [d for _p, _a, d in _steps(verdict, cwd)]
    values = _assignments(verdict, command)
    # `F=~/.claude/settings.json; echo x > $F`: путь из переменной (ревью Opus 03.11.2026).
    words = [_expand(w, values) or w for w in words]
    # Настройки Н11 здесь не в счёт: запись в них ловят точные проверки
    # (_redirect_to_guard, _guard_step), а слово в команде бывает и чтением.
    return any(_guard(_abs(w.lstrip(">"), d), n11=False)
               for w in words if w.lstrip(">") for d in set(dirs) if d or w.startswith(("/", "~")))

"""HTTP-сервер внутри бота + реестр ожидающих approvals.

Схема:
    Claude Code --PreToolUse hook--> hook_approve.py
      └─ HTTP POST /approve -> handle_approve()
           │
           ├─ ApprovalRegistry.create() -> asyncio.Future в реестре
           ├─ отправить Inline Keyboard в Telegram
           └─ await future (с таймаутом)

    Пользователь жмёт кнопку -> CallbackQuery -> ApprovalRegistry.resolve()
      └─ future.set_result(decision) -> handle_approve возвращает JSON hook'у
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import glob
import hmac
import html
import ipaddress
import json
import logging
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from . import audit, danger, mailsend
from .burst import burst_target_key
from .config import LOOPBACK_HOSTS, Config
from .i18n import _, N_

log = logging.getLogger(__name__)

TRUNCATE_PAYLOAD = 2000
MULTIEDIT_PER_SIDE_CAP = 400
# Потолок входа для difflib (символы на сторону): защита событийного цикла.
DIFF_INPUT_CAP = 20000
AUDIT_LOG_NAME = "audit.jsonl"


# Tools, исключённые из burst-окна. По осознанному решению Ивана окно
# разрешает ЛЮБЫЕ действия (вкл. Bash/Write/Edit) на 15 мин / 1 ч — иначе на
# самый частый инструмент (Bash) приходился пучок ручных аппрувов. Размен
# удобства на безопасность: в открытом окне rm/push/curl проходят без карточки.
# Set оставлен пустым, чтобы при необходимости снова что-то исключить точечно.
_BURST_DENY_TOOLS: frozenset[str] = frozenset()

# Отправка наружу от имени Ивана (Telegram его аккаунтом, письмо из его
# Gmail): всегда карточка, раньше любых автоаппрувов (чат Домового,
# фоновый ран, окно /auto). Тап ✅ по карточке с адресатом, текстом и файлом и
# есть «ок» на точный текст: правило держит бот, а не дисциплина агента.
# Совет GPT Sol и решение Ивана 12.10.2026. Ловим по серверу и глаголу, а
# не списком имён: у серверов есть send_voice, send_album, edit_message,
# send_scheduled_message, slack_send_message (ревью Codex 12.10.2026).
_OUTBOUND_SERVERS = frozenset({"telegram", "claude_ai_Gmail", "claude_ai_Slack"})
_OUTBOUND_VERB_RE = re.compile(r"(?:[a-z]+_)?(?:send|reply|forward|edit|post|schedule)[a-z_]*")
# Отправка, содержимое которой карточка показать не может: пересылка
# (переслать можно что угодно из чужого чата) и письмо по номеру черновика
# (сам черновик пишется без карточки). Такое бот отклоняет сразу.
_OUTBOUND_FORWARD_RE = re.compile(r"(?:[a-z]+_)?forward[a-z_]*")


def _is_outbound_send(tool_name: str) -> bool:
    parts = tool_name.split("__", 2)
    return (len(parts) == 3 and parts[0] == "mcp" and parts[1] in _OUTBOUND_SERVERS
            and bool(_OUTBOUND_VERB_RE.fullmatch(parts[2])))


def _outbound_refusal(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Причина отказа отправке, которую нельзя показать на карточке целиком."""
    short_tool = tool_name.split("__", 2)[-1]
    if _OUTBOUND_FORWARD_RE.fullmatch(short_tool):
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
# скриптом интерпретатора, импорт в коде (python -c, heredoc).
_MAILSEND_RUN_RE = re.compile(
    r"-m\s*['\"]?claude_tg_bot\.mailsend\b"
    r"|\bpython[\d.]*['\"]?\s+(?:-\S+\s+)*(?:-m\s*['\"]?mailsend\b|['\"]?(?:\S*/)?mailsend\.py\b)"
    r"|\bimport\s+claude_tg_bot\.mailsend\b"
    r"|\bfrom\s+claude_tg_bot(?:\.mailsend\s+import\b|\s+import\b[^;\n]*\bmailsend\b)"
    r"|\b(?:import_module|run_module)\(\s*['\"]claude_tg_bot\.mailsend\b"
)
# Команды, которые текст аргументов только читают или пишут: упоминание
# mailsend в них письма не отправит.
_TEXT_TOOLS = frozenset({"git", "grep", "egrep", "fgrep", "rg", "echo", "printf", "cat",
                         "sed", "head", "tail", "less", "wc", "diff", "ls"})
# Обёртки перед настоящей командой: `timeout 60 git ...` это git.
_CMD_WRAPPERS = frozenset({"env", "timeout", "nice", "nohup", "sudo", "exec", "command",
                           "time", "stdbuf", "setsid"})
_HEREDOC_RE = re.compile(
    r"<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)(?:\n[ \t]*\2[ \t]*(?=\n|\Z)|\Z)", re.S)
_MAILSEND_MODULE = "claude_tg_bot.mailsend"
_PYTHON_RE = re.compile(r"(?:\S*/)?python3?(?:\.\d+)?")
_SHELL_OPS = frozenset("();<>|&")
_MAILSEND_FORM = "[cd DIR &&] .venv/bin/python -m claude_tg_bot.mailsend --to ... --body-file ..."


def _mailsend_argv(cmd: str, cwd: str | None) -> tuple[list[str], Path | None] | None:
    """Аргументы mailsend из простой команды `[cd DIR &&] PY -m
    claude_tg_bot.mailsend ...` и каталог для относительных путей. Подстановки,
    конвейеры, bash -c, heredoc и переменные не разбираются: None."""
    # Перенос строки через `\` bash убирает до разбора; в одинарных кавычках
    # он остался бы, но такой путь или тема дадут другой хеш, то есть отказ.
    cmd = cmd.replace("\\\n", "")
    if any(c in cmd for c in "$`\\\n\r"):
        return None
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    base = Path(cwd) if cwd and Path(cwd).is_absolute() else None
    if tokens[:1] == ["cd"]:
        if len(tokens) < 3 or tokens[2] != "&&":
            return None
        target = Path(tokens[1]).expanduser()
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
    if any(t and set(t) <= _SHELL_OPS for t in argv):
        return None
    return argv, base


def _command_head(tokens: list[str]) -> str:
    """Имя команды простого куска: без присваиваний VAR=..., обёрток и их ключей."""
    for t in tokens:
        if (re.fullmatch(r"\w+=.*", t, re.S) or t.startswith("-")
                or re.fullmatch(r"[\d.]+[smhd]?", t) or Path(t).name in _CMD_WRAPPERS):
            continue
        return Path(t).name
    return ""


def _substitutions(cmd: str) -> list[str]:
    """Тексты подстановок $(...) и `...`. Скобки считаются без учёта кавычек,
    незакрытая подстановка идёт до конца строки."""
    out = [m.group(1) for m in re.finditer(r"`([^`]*)`?", cmd)]
    for m in re.finditer(r"\$\(", cmd):
        depth, j = 1, m.end()
        while j < len(cmd) and depth:
            depth += {"(": 1, ")": -1}.get(cmd[j], 0)
            j += 1
        out.append(cmd[m.end():j - 1 if depth == 0 else j])
    return out


def _mailsend_runs(cmd: str, depth: int = 0) -> bool:
    """Команда исполняет mailsend (_MAILSEND_RUN_RE), в том числе через bash -c,
    $(...), heredoc и обёртки. Упоминание в аргументах _TEXT_TOOLS (сообщение
    коммита, шаблон grep, текст echo) не в счёт. Что не разобралось, считается
    исполнением: лишняя карточка дешевле."""
    cmd = cmd.replace("\\\n", "")
    if not _MAILSEND_RUN_RE.search(cmd):
        return False
    # Тело heredoc: код для интерпретатора и оболочки, просто текст для cat и git.
    bodies: list[tuple[str, str]] = []

    def cut(m: re.Match[str]) -> str:
        line = cmd[:m.start()].rsplit("\n", 1)[-1]
        bodies.append((re.split(r"\$\(|[;|&(`]", line)[-1], m.group(4)))
        return m.group(3)

    shell = _HEREDOC_RE.sub(cut, cmd)
    for opener, body in bodies:
        if _command_head(opener.split()) not in _TEXT_TOOLS and _MAILSEND_RUN_RE.search(body):
            return True
    # Подстановка исполняется и внутри аргумента любой команды. Глубокую
    # вложенность не разбираем: признак внутри уже считается исполнением.
    for inner in _substitutions(shell):
        if _MAILSEND_RUN_RE.search(inner) and (depth >= 3 or _mailsend_runs(inner, depth + 1)):
            return True
    try:
        # Перевод строки разделяет команды; внутри кавычек `;` остаётся текстом.
        lex = shlex.shlex(shell.replace("\n", ";"), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return True
    part: list[str] = []
    for t in tokens + [";"]:
        if t and set(t) <= _SHELL_OPS:
            if _command_head(part) not in _TEXT_TOOLS and _MAILSEND_RUN_RE.search(" ".join(part)):
                return True
            part = []
        else:
            part.append(t)
    return False


def _mailsend_check(cmd: str, cwd: str | None) -> tuple[str, Any]:
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
    except (mailsend.MailError, argparse.ArgumentError) as e:
        return "deny", f"mailsend: {e}"
    return "card", {
        "to": list(letter.to), "cc": list(letter.cc), "subject": letter.subject,
        "reply_to": letter.reply_to_id, "body": letter.body,
        "attachments": [{"name": a.name, "size": len(a.data), "path": a.path}
                        for a in letter.attachments],
        "digest": letter.digest(),
    }


def _human_size(size: int) -> str:
    if size < 1024:
        return _("{size} Б").format(size=size)
    if size < 1024 * 1024:
        return _("{size:.1f} КБ").format(size=size / 1024)
    return _("{size:.1f} МБ").format(size=size / 1024 / 1024)


def _format_mailsend(mail: dict[str, Any]) -> str:
    """Карточка письма mailsend: адресаты, тема, вложения с размером, весь текст."""
    if mail.get("warning"):
        return (_("⚠️ <b>команда запускает mailsend не простой формой</b>: письмо так не уйдёт, "
                  "без ✅ на карточке письма mailsend откажет. Форма для письма: "
                  "<code>{form}</code>").format(form=_esc(_MAILSEND_FORM)))
    lines = [_("📤 <b>письмо от твоего имени</b> (Gmail, mailsend)"),
             _("кому: <code>{to}</code>").format(to=_esc_truncate(', '.join(mail.get('to') or []), 300))]
    if mail.get("cc"):
        lines.append(_("копия: <code>{cc}</code>").format(cc=_esc_truncate(', '.join(mail['cc']), 300)))
    if mail.get("reply_to"):
        lines.append(_("ответ на письмо: <code>{reply_to}</code>").format(reply_to=_esc_truncate(str(mail['reply_to']), 300)))
    lines.append(_("тема письма: {subject}").format(subject=_esc_truncate(str(mail.get('subject') or ''), 300)))
    atts = mail.get("attachments") or []
    if atts:
        lines.append(_("вложения: ") + ", ".join(
            f"<code>{_esc_truncate(str(a.get('name')), 200)}</code> ({_human_size(int(a.get('size') or 0))})"
            for a in atts))
    body = str(mail.get("body") or "")
    lines.append(_("текст письма:\n<pre>{body}</pre>").format(body=_esc_truncate(body, TRUNCATE_PAYLOAD)))
    if len(_esc(body)) > TRUNCATE_PAYLOAD:
        lines.append(_("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>"))
    return "\n".join(lines)


def _mailsend_full_view(mail: dict[str, Any]) -> str:
    if mail.get("warning"):
        return _("--- mailsend ---\nкоманда не простой формой, письмо так не уйдёт.\nФорма: {form}").format(form=_MAILSEND_FORM)
    lines = [_("--- письмо (mailsend) ---"), _("Кому: {to}").format(to=', '.join(mail.get('to') or []))]
    if mail.get("cc"):
        lines.append(_("Копия: {cc}").format(cc=', '.join(mail['cc'])))
    if mail.get("reply_to"):
        lines.append(_("Ответ на: {reply_to}").format(reply_to=mail['reply_to']))
    lines.append(_("Тема: {subject}").format(subject=mail.get('subject')))
    for a in mail.get("attachments") or []:
        lines.append(_("Вложение: {name} ({size} байт) из {path}").format(
            name=a.get('name'), size=a.get('size'), path=a.get('path')))
    lines.append(_("sha256 письма: {digest}").format(digest=mail.get('digest')))
    lines += ["", _("--- текст письма ---"), str(mail.get("body") or "")]
    return "\n".join(lines)


@dataclass
class Pending:
    approval_id: str
    tool_name: str
    tool_input: dict[str, Any]
    future: asyncio.Future
    telegram_message_id: int | None = None
    session_id: str | None = None
    # Куда отправлять approve-карточку. Берётся из session_targets по session_id;
    # если sid не зарегистрирован (race / неизвестный hook), fall-back на
    # cfg.primary_chat_id с thread=None.
    target_chat_id: int | None = None
    target_thread_id: int | None = None
    # Рабочий каталог агента из входа хука: относительный путь в команде
    # оценивается от него (ревью 11.10.2026).
    cwd: str | None = None
    # Карточка обдумывания темы задачи: без кнопок ⏩ (окно там не действует).
    no_burst: bool = False


class ApprovalRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Pending] = {}

    def create(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session_id: str | None = None,
        target_chat_id: int | None = None,
        target_thread_id: int | None = None,
        cwd: str | None = None,
    ) -> Pending:
        aid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        p = Pending(
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            future=fut,
            session_id=session_id,
            target_chat_id=target_chat_id,
            target_thread_id=target_thread_id,
            cwd=cwd,
        )
        self._items[aid] = p
        return p

    def get(self, approval_id: str) -> Pending | None:
        return self._items.get(approval_id)

    def resolve(self, approval_id: str, decision: str) -> Pending | None:
        p = self._items.pop(approval_id, None)
        if p is None:
            return None
        if not p.future.done():
            p.future.set_result(decision)
        return p

    def expire(self, approval_id: str) -> Pending | None:
        """Убрать из реестра без выставления результата (for timeout cleanup)."""
        return self._items.pop(approval_id, None)

    def resolve_by_session(self, session_id: str, decision: str) -> list[Pending]:
        """Resolve все pending approvals, принадлежащие данной session_id.

        Используется /cancel: когда убиваем claude-процесс, оставшиеся approve-кнопки
        уже бессмысленны (их нажатие не дойдёт до мёртвого hook'а).
        """
        to_resolve = [aid for aid, p in self._items.items() if p.session_id == session_id]
        out: list[Pending] = []
        for aid in to_resolve:
            p = self._items.pop(aid)
            if not p.future.done():
                p.future.set_result(decision)
            out.append(p)
        return out

    @property
    def pending_count(self) -> int:
        return len(self._items)


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
    # Если обрезали посреди HTML-entity (`&lt;`, `&amp;` и т.п.) — откатываемся
    # до последнего `&`, у которого нет закрывающего `;` в хвосте.
    last_amp = cut.rfind("&")
    if last_amp >= 0 and ";" not in cut[last_amp:]:
        cut = cut[:last_amp]
    return cut + " …"


_DANGER_REASONS_CAP = 400
_DESCRIPTION_CAP = 200
FULL_VIEW_THRESHOLD = 1500


def _danger_line(tool_name: str, tool_input: Any, cwd: str | None = None) -> str:
    """Первая строка карточки: оценка бота по ПОЛНОМУ входу инструмента.
    Подсказка, а не решение: жёлтый значит «опасного не нашёл», не «безопасно»."""
    try:
        verdict = danger.classify(tool_name, tool_input if isinstance(tool_input, dict) else {},
                                  cwd=cwd)
    except Exception:
        log.exception("danger classify failed")
        return _("⚪ <b>оценка опасности не удалась</b>, читай команду целиком")
    if verdict.red:
        reasons = "; ".join(verdict.red)
        return _("🔴 <b>ОПАСНО:</b> {reasons}").format(reasons=_esc_truncate(reasons, _DANGER_REASONS_CAP))
    if verdict.orange:
        reasons = "; ".join(verdict.orange)
        return _("🟠 <b>Меняет:</b> {reasons}").format(reasons=_esc_truncate(reasons, _DANGER_REASONS_CAP))
    return _("🟡 опасного не нашёл (это не значит «безопасно»)")


def needs_full_view(tool_input: Any, tool_name: str | None = None) -> bool:
    """Вход длиннее, чем влезает в карточку: показать кнопку «📄 Полностью».
    Длина считается после HTML-экранирования (`<` это `&lt;`), а с именем
    инструмента кнопка есть всегда, когда тело карточки что-то обрезало:
    хвост `; rm …` за 600 знаками `<` иначе пропадал без кнопки
    (ревью 11.10.2026)."""
    try:
        dumped = json.dumps(tool_input, ensure_ascii=False)
    except (TypeError, ValueError):
        dumped = str(tool_input)
    if len(_esc(dumped)) > FULL_VIEW_THRESHOLD:
        return True
    if tool_name is None:
        return False
    # Все обрезки тела ставят «…» (`_esc_truncate`, сравнение Edit,
    # «ещё N edit(s)» у MultiEdit); «…» в самом входе даёт лишнюю кнопку, не вред.
    return "…" in _format_tool_body(tool_name, tool_input, None)


def full_view_text(tool_name: str, tool_input: Any) -> str:
    """Полный вход инструмента для просмотра файлом до решения по карточке.
    Поля, которых нет в основном тексте (replace_all, timeout, фон), идут
    хвостом: это полный вход, а не его пересказ."""
    main = _full_view_main(tool_name, tool_input)
    if isinstance(tool_input, dict):
        shown = {"Bash": {"command", "description", MAILSEND_CARD_KEY},
                 "Write": {"file_path", "content"},
                 "Edit": {"file_path", "old_string", "new_string"}}.get(tool_name)
        if shown is not None:
            rest = {k: v for k, v in tool_input.items() if k not in shown}
            if rest:
                main += _("\n\n--- остальные поля ---\n") + json.dumps(rest, ensure_ascii=False, indent=2)
    return main


def _full_view_main(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, dict) and _is_outbound_send(tool_name):
        # Вложения именем и размером: base64 в просмотре не нужен.
        shown = dict(tool_input)
        if isinstance(shown.get("attachments"), list):
            shown["attachments"] = [
                {"filename": a.get("filename"), "mimeType": a.get("mimeType"),
                 "base64_chars": len(str(a.get("content") or ""))}
                if isinstance(a, dict) else "?" for a in shown["attachments"]]
        texts = [_("--- {label} ---\n{value}").format(label=_(label), value=shown.pop(key))
                 for key, label in _OUTBOUND_TEXT_FIELDS
                 if shown.get(key) not in (None, "")]
        return (json.dumps(shown, ensure_ascii=False, indent=2) + "\n\n" + "\n\n".join(texts)).rstrip()
    if isinstance(tool_input, dict):
        if tool_name == "Bash":
            desc = str(tool_input.get("description", ""))
            head = _("# Пояснение агента: {desc}\n\n").format(desc=desc) if desc else ""
            mail = tool_input.get(MAILSEND_CARD_KEY)
            tail = f"\n\n{_mailsend_full_view(mail)}" if isinstance(mail, dict) else ""
            return head + str(tool_input.get("command", "")) + tail
        if tool_name == "Write":
            return f"{tool_input.get('file_path')}\n\n{tool_input.get('content', '')}"
        if tool_name == "Edit":
            return (_("{path}\n\n--- было\n{old}\n\n+++ стало\n{new}").format(
                path=tool_input.get('file_path'), old=tool_input.get('old_string', ''),
                new=tool_input.get('new_string', '')))
    return json.dumps(tool_input, ensure_ascii=False, indent=2)


CARD_TEXT_CAP = 3800


def _format_tool_call(tool_name: str, tool_input: dict[str, Any], session_id: str | None,
                      cwd: str | None = None) -> str:
    """Карточка: оценка опасности, затем описание вызова (HTML, escape-нуто).
    Если вышло длиннее лимита Telegram (длинный путь, много правок), тело
    заменяется обрезанным полным текстом: целиком он доступен по «📄»."""
    head = _danger_line(tool_name, tool_input, cwd)
    card = f"{head}\n{_format_tool_body(tool_name, tool_input, session_id)}"
    if len(card) <= CARD_TEXT_CAP:
        return card
    raw = full_view_text(tool_name, tool_input)
    return (f"{head}\n🛠 <b>{_esc_truncate(str(tool_name), 100)}</b>  "
            + _("<i>карточка длинная, целиком по кнопке 📄</i>") + "\n"
            f"<pre>{_esc_truncate(raw, 2500)}</pre>")


def _format_tool_body(tool_name: str, tool_input: dict[str, Any], session_id: str | None) -> str:
    """Короткое человекочитаемое описание tool call для Telegram (HTML, всё escape-нуто)."""
    name = _esc(tool_name)
    header = f"🛠 <b>{name}</b>"
    if session_id:
        header += f"  <i>sid={_esc(session_id[:8])}</i>"

    if not isinstance(tool_input, dict):
        return f"{header}\n<pre>{_esc_truncate(str(tool_input), TRUNCATE_PAYLOAD)}</pre>"

    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        desc = str(tool_input.get("description", "")).strip()
        # Пояснение пишет агент, а его могли обмануть: оно подписано как его
        # слова и стоит после оценки бота, а не вместо неё.
        note = (_("💬 <i>пояснение агента:</i> {desc}").format(desc=_esc_truncate(desc, _DESCRIPTION_CAP))
                if desc else _("💬 <i>пояснения агента нет</i>"))
        mail = tool_input.get(MAILSEND_CARD_KEY)
        if isinstance(mail, dict):
            # Письмо целиком важнее команды: команда коротко, полностью по 📄.
            return (f"{header}\n{_format_mailsend(mail)}\n{note}\n"
                    f"<pre>{_esc_truncate(cmd, 400)}</pre>")
        body = _esc_truncate(cmd, TRUNCATE_PAYLOAD)
        return f"{header}\n{note}\n<pre>{body}</pre>"
    if tool_name == "Write":
        path = str(tool_input.get("file_path") or "?")
        content = str(tool_input.get("content", ""))
        body = (
            f"<code>{_esc(path)}</code>\n"
            f"<pre>{_esc_truncate(content, TRUNCATE_PAYLOAD)}</pre>"
        )
        return f"{header}\n{body}"
    if tool_name == "Edit":
        path = str(tool_input.get("file_path") or "?")
        old_s = str(tool_input.get("old_string", ""))
        new_s = str(tool_input.get("new_string", ""))
        ra = " (replace_all)" if tool_input.get("replace_all") else ""
        # Unified diff вместо слепых −/+ блоков: видно, ЧТО меняется, а не два
        # похожих куска, которые надо сличать глазами с телефона.
        # keepends: иначе правка, меняющая только перевод строки, даёт ПУСТОЙ
        # diff и карточка показывает «изменений нет».
        # Обрезаем ДО difflib: строки приходят от модели, на мегабайтном входе
        # сравнение съело бы событийный цикл (в нём же живёт approve-сервер).
        cap = DIFF_INPUT_CAP
        diff_lines = list(difflib.unified_diff(
            old_s[:cap].splitlines(keepends=True), new_s[:cap].splitlines(keepends=True),
            lineterm="", n=2,
        ))[2:]  # без заголовков --- / +++
        diff = "\n".join(line.replace("\n", "␊") for line in diff_lines)
        clipped = len(old_s) > cap or len(new_s) > cap
        if not diff:
            diff = (
                _("(изменения за пределами первых {cap} символов — "
                  "было {old_len}, стало {new_len})").format(
                    cap=cap, old_len=len(old_s), new_len=len(new_s))
                if clipped else _("(нет текстовых изменений)")
            )
        elif clipped:
            diff += _("\n… (сравнение обрезано по {cap} символов)").format(cap=cap)
        body = (
            f"<code>{_esc(path)}</code>{ra}\n"
            f"<pre>{_esc_truncate(diff, TRUNCATE_PAYLOAD)}</pre>"
        )
        return f"{header}\n{body}"
    if tool_name == "MultiEdit":
        path = str(tool_input.get("file_path") or "?")
        edits = tool_input.get("edits") or []
        if not isinstance(edits, list):
            edits = []
        per_side_cap = MULTIEDIT_PER_SIDE_CAP
        total_budget = TRUNCATE_PAYLOAD
        blocks: list[str] = []
        used = 0
        shown = 0
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict):
                continue
            o = _esc_truncate(str(e.get("old_string", "")), per_side_cap)
            n = _esc_truncate(str(e.get("new_string", "")), per_side_cap)
            ra = " (replace_all)" if e.get("replace_all") else ""
            block = f"<b>[{i}]</b>{ra}\n− <pre>{o}</pre>\n+ <pre>{n}</pre>"
            if used + len(block) > total_budget and shown > 0:
                blocks.append(_("<i>… ещё {count} edit(s) не показаны</i>").format(count=len(edits) - shown))
                break
            blocks.append(block)
            used += len(block)
            shown += 1
        body = f"<code>{_esc(path)}</code> · {len(edits)} edit(s)\n" + "\n".join(blocks)
        return f"{header}\n{body}"
    if tool_name == "NotebookEdit":
        path = str(tool_input.get("notebook_path") or tool_input.get("file_path") or "?")
        cell_id = str(tool_input.get("cell_id", ""))
        cell_type = str(tool_input.get("cell_type", ""))
        edit_mode = str(tool_input.get("edit_mode", ""))
        new_source = str(tool_input.get("new_source", ""))
        meta = " · ".join(x for x in [cell_type, edit_mode, cell_id] if x)
        body = (
            f"<code>{_esc(path)}</code>"
            + (f"\n<i>{_esc(meta)}</i>" if meta else "")
            + f"\n<pre>{_esc_truncate(new_source, TRUNCATE_PAYLOAD)}</pre>"
        )
        return f"{header}\n{body}"
    if tool_name == "WebFetch":
        url = str(tool_input.get("url", "?"))
        prompt = str(tool_input.get("prompt", ""))
        body = url + (f"\n\n{prompt}" if prompt else "")
        return f"{header}\n<pre>{_esc_truncate(body, TRUNCATE_PAYLOAD)}</pre>"
    if _is_outbound_send(tool_name):
        return f"{header}\n{_format_outbound(tool_input)}"
    # generic fallback
    dumped = json.dumps(tool_input, ensure_ascii=False, indent=2)
    return f"{header}\n<pre>{_esc_truncate(dumped, TRUNCATE_PAYLOAD)}</pre>"


_OUTBOUND_TEXT_FIELDS = (("message", N_("текст")), ("text", N_("текст")), ("caption", N_("подпись")),
                         ("body", N_("текст письма")), ("htmlBody", N_("HTML письма")))


def _format_outbound(tool_input: dict[str, Any]) -> str:
    """Карточка отправки: кому, тема, файлы, все текстовые поля. Это то, на
    что Иван даёт «ок», поэтому поля называются словами, а не JSON, и ни одно
    отправляемое поле не прячется за другим (ревью Codex 12.10.2026)."""
    lines = [_("📤 <b>отправка от твоего имени</b>")]
    to = tool_input.get("to") or tool_input.get("chat_id")
    if to not in (None, ""):
        lines.append(_("кому: <code>{to}</code>").format(to=_esc_truncate(str(to), 300)))
    for key, label in (("cc", _("копия")), ("bcc", _("скрытая копия")),
                       ("message_id", _("ответ на сообщение")),
                       ("replyToMessageId", _("ответ на письмо")),
                       ("replyThreadId", _("в переписке")), ("draftId", _("черновик")),
                       ("topic_id", _("тема"))):
        val = tool_input.get(key)
        if val not in (None, "", []):
            lines.append(f"{label}: <code>{_esc_truncate(str(val), 300)}</code>")
    subject = tool_input.get("subject")
    if subject:
        lines.append(_("тема письма: {subject}").format(subject=_esc_truncate(str(subject), 300)))
    files = tool_input.get("file_path")
    if files:
        names = files if isinstance(files, list) else [files]
        lines.append(_("файл: ") + ", ".join(f"<code>{_esc_truncate(str(n), 200)}</code>" for n in names))
    attachments = tool_input.get("attachments")
    if isinstance(attachments, list) and attachments:
        names = [str(a.get("filename") or _("без имени")) if isinstance(a, dict) else "?"
                 for a in attachments]
        lines.append(_("вложения: ") + ", ".join(f"<code>{_esc_truncate(n, 200)}</code>" for n in names))
    for key in ("account", "parse_mode"):
        val = tool_input.get(key)
        if val not in (None, ""):
            lines.append(f"{key}: <code>{_esc_truncate(str(val), 100)}</code>")
    texts = [(_(label), str(tool_input[key])) for key, label in _OUTBOUND_TEXT_FIELDS
             if tool_input.get(key) not in (None, "")]
    clipped = False
    for label, text in texts:
        lines.append(f"{label}:\n<pre>{_esc_truncate(text, TRUNCATE_PAYLOAD)}</pre>")
        clipped = clipped or len(_esc(text)) > TRUNCATE_PAYLOAD
    if not texts and not files and not attachments:
        lines.append(_("<i>текста нет</i>"))
    if clipped:
        lines.append(_("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>"))
    known = {"to", "chat_id", "cc", "bcc", "message_id", "replyToMessageId", "replyThreadId",
             "draftId", "topic_id", "subject", "file_path", "attachments", "account",
             "parse_mode"} | {k for k, _unused in _OUTBOUND_TEXT_FIELDS}
    rest = {k: v for k, v in tool_input.items() if k not in known and v not in (None, "", [])}
    if rest:
        lines.append(_("<i>прочее:</i> <code>{rest}</code>").format(
            rest=_esc_truncate(json.dumps(rest, ensure_ascii=False), 400)))
    return "\n".join(lines)


_FILE_TOOLS_FOR_WHITELIST = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# Безобидные redirection-токены, которые не должны валить bash-safe-проверку.
# `2>/dev/null` особенно частотный — в `ls /missing 2>/dev/null` побочного эффекта нет.
# Справа допустим и разделитель (`ls x 2>/dev/null; ls y`): то, что после
# него, всё равно проверяется отдельным сегментом.
_BASH_SAFE_REDIRECT_RE = re.compile(
    r"(?<!\S)(?:2>/dev/null|2>&1|&>/dev/null|>/dev/null|1>/dev/null)(?=\s|$|[;|&])"
)
# Сегментные разделители: pipe / and / or / background (&) / sequence (;) /
# newline. ВСЕ способы, которыми bash разделяет команды, обязаны быть здесь:
# иначе сегмент с неучтённым разделителем (`a<newline>rm -rf`, `a & rm`)
# проверяется как одна команда, а `.match` валидирует лишь её префикс —
# хвост проскакивает в auto-allow. Поэтому ниже используем `fullmatch`.
# Порядок важен: более длинный токен (`||`, `&&`) пробуется первым.
# `2>&1`/`&>/dev/null` стрипаются как safe-redirections ВЫШЕ, поэтому их `&`
# сюда не доходит.
_BASH_SEPARATORS = ("||", "&&", "|", "&", ";", "\n", "\r")

# Флаги, превращающие читающую команду в исполняющую или мутирующую.
# Allowlist-regex смотрит только на имя команды и пропустил бы `find . -delete`,
# поэтому денилист проверяется отдельно и привязан к имени: `-i` у sed это
# in-place, а у grep — ignore-case, общим списком их не различить.
# Ключ для `git grep` — "git": имя команды берётся из первого токена.
_BASH_DANGEROUS_ARGS: dict[str, frozenset[str]] = {
    "find": frozenset({
        "-delete", "-exec", "-execdir", "-ok", "-okdir",
        "-fls", "-fprint", "-fprint0", "-fprintf",
    }),
    "fd": frozenset({"-x", "--exec", "-X", "--exec-batch"}),
    "fdfind": frozenset({"-x", "--exec", "-X", "--exec-batch"}),
    # rg --pre/--hostname-bin запускают внешнюю программу на каждый файл.
    "rg": frozenset({"--pre", "--pre-glob", "--hostname-bin"}),
    "ripgrep": frozenset({"--pre", "--pre-glob", "--hostname-bin"}),
    # git grep -O/--open-files-in-pager отдаёт совпадения в pager, а pager
    # исполняется shell'ом; --textconv/--ext-diff зовут настроенные фильтры.
    "git": frozenset({
        "-O", "--open-files-in-pager", "--textconv", "--ext-diff", "-c", "--config-env",
        # git diff/log/show --output=<файл> пишет вывод в файл.
        "--output",
        # --upload-pack/--exec у fetch и ls-remote запускают программу,
        # --filters у cat-file гонит содержимое через настроенные фильтры.
        "--upload-pack", "--exec", "--filters",
        # git grep --no-index ищет и по файлам из .gitignore, то есть по .env.
        "--no-exclude-standard",
    }),
    # sort -o/--output перезаписывает файл, --compress-program запускает бинарь.
    "sort": frozenset({"-o", "--output", "--compress-program", "--files0-from"}),
    "tree": frozenset({"-o"}),
    # file -C компилирует базу magic и пишет magic.mgc (ревью 11.10.2026).
    "file": frozenset({"-C", "--compile"}),
    # journalctl умеет удалять и переписывать журналы, а не только читать.
    "journalctl": frozenset({
        "--vacuum-time", "--vacuum-size", "--vacuum-files", "--rotate", "--flush",
        "--sync", "--relinquish-var", "--smart-relinquish-var", "--setup-keys",
        "--update-catalog",
    }),
}

# Короткие флаги из денилиста, которые могут прийти в связке (`sort -uo out`).
_BASH_DANGEROUS_SHORT: dict[str, frozenset[str]] = {
    cmd: frozenset(f[1] for f in flags if len(f) == 2 and f[0] == "-")
    for cmd, flags in _BASH_DANGEROUS_ARGS.items()
}


def _scan_bash_segments(command: str) -> list[str] | None:
    """Разбить команду на сегменты по shell-разделителям, уважая кавычки.

    Возвращает None, если решать по команде нельзя: command substitution
    (`$(`, backtick) вне одинарных кавычек, любое `$`-раскрытие вне одинарных
    кавычек, скобки `(`/`)` вне кавычек, redirection (`>`, `<`) вне кавычек
    либо незакрытая кавычка.

    Кавычки учитываются намеренно: `grep -E 'a|b'` и `find . -iname "*a|b*"` —
    обычный поиск, а разбиение по голому `|` резало их на сегменты вроде `b'`,
    которые не матчатся ни под какой allowlist, и поиск уходил в карточку.
    Внутри кавычек `>` и `|` для bash обычные символы, а `$(` и backtick в
    двойных кавычках всё ещё раскрываются — поэтому там они запрещены.

    Скобки запрещены, потому что allowlist смотрит только на первое слово:
    `ls () (rm -rf x); ls` — это объявление функции `ls` с телом `rm` и её
    вызов, а под regex сегмент выглядит как безобидный `ls …`.
    `$VAR` запрещён по той же причине: word splitting подставляет значение
    отдельным словом, поэтому `find . $F` при `F=-delete` становится
    `find . -delete` уже ПОСЛЕ нашей проверки аргументов.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        # Экранированный символ переносим парой: `\|` и `\;` не разделители.
        # В одинарных кавычках backslash буквальный, escape там не работает.
        if ch == "\\" and quote != "'" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if quote is not None:
            if quote == '"' and (ch == "$" or ch == "`"):
                return None
            if ch == quote:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in ("<", ">", "`", "$", "(", ")"):
            return None
        for sep in _BASH_SEPARATORS:
            if command.startswith(sep, i):
                segments.append("".join(buf))
                buf = []
                i += len(sep)
                break
        else:
            buf.append(ch)
            i += 1
    if quote is not None:
        return None
    segments.append("".join(buf))
    return segments


_GLOB_CHARS = frozenset("*?[{")


def _has_unquoted_glob(segment: str) -> bool:
    """True, если в сегменте есть `*`, `?`, `[` или `{` вне кавычек и без `\\`.
    Bash раскрывает их уже ПОСЛЕ проверки: `rg x *` при файле `--pre=sh` в
    каталоге получает флаг запуска программы, `sed -n 1p *` при файле `-i`
    переписывает файлы, `{-i,a}` даёт то же скобками."""
    quote: str | None = None
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "\\" and quote != "'":
            i += 2
            continue
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in _GLOB_CHARS:
            return True
        i += 1
    return False


# sed как фильтр и печать: флаги только списком (без `-i`, `-e`, `-f`; GNU sed
# принимает флаги и после имён файлов, поэтому проверяется каждый токен),
# ровно один скрипт, разобранный `_sed_script_ok`.
_SED_FLAGS = frozenset({
    "-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended", "-s", "--separate",
    "-u", "--unbuffered", "-z", "--null-data",
})
_SED_S_FLAGS = frozenset("gpiImM0123456789")


def _sed_regex_end(script: str, i: int, delim: str, brackets: bool) -> int:
    """Индекс после закрывающего `delim`, иначе -1. В регулярке (`brackets`)
    скобочное выражение `[...]` пропускается целиком, и разделитель внутри
    него запрещён: sed и этот разбор могли бы иначе по-разному понять, где
    кончается шаблон, и за границей спрятался бы флаг `w` или `e`."""
    n = len(script)
    while i < n:
        c = script[i]
        if c == "\n":
            return -1
        if c == "\\":
            i += 2
            continue
        if brackets and c == "[":
            j = i + 1
            if j < n and script[j] == "^":
                j += 1
            if j < n and script[j] == "]":
                j += 1
            while j < n and script[j] != "]":
                if script[j] in (delim, "\n"):
                    return -1
                if script[j] == "[" and j + 1 < n and script[j + 1] in ":.=":
                    close = script.find(script[j + 1] + "]", j + 2)
                    if close < 0 or delim in script[j:close] or "\n" in script[j:close]:
                        return -1
                    j = close + 2
                    continue
                j += 1
            if j >= n:
                return -1
            i = j + 1
            continue
        if c == delim:
            return i + 1
        i += 1
    return -1


def _sed_address_end(script: str, i: int) -> int:
    """Конец адреса (`N`, `first~step`, `$`, `/re/I`), `i` если адреса нет,
    -1 если он сломан."""
    n = len(script)
    if i < n and script[i].isdigit():
        while i < n and (script[i].isdigit() or script[i] == "~"):
            i += 1
        return i
    if i < n and script[i] == "$":
        return i + 1
    if i < n and script[i] == "/":
        j = _sed_regex_end(script, i + 1, "/", True)
        if j < 0:
            return -1
        while j < n and script[j] in "IM":
            j += 1
        return j
    return i


def _sed_script_ok(script: str) -> bool:
    """Скрипт из команд `p`, `d`, `=`, `q`/`Q` и `s///` с флагами без `w`
    и `e`, с адресами и `!`, через `;` или перевод строки. Всё остальное
    (`w`, `r`, `e`, `{}`, `y`, адрес `\\%re%`) значит карточка."""
    n = len(script)
    i = 0
    while True:
        while i < n and script[i] in " \t;\n":
            i += 1
        if i >= n:
            return True
        j = _sed_address_end(script, i)
        if j < 0:
            return False
        if j > i and j < n and script[j] == ",":
            k = j + 1
            if k < n and script[k] in "+~":
                k += 1
                if k >= n or not script[k].isdigit():
                    return False
            j2 = _sed_address_end(script, k)
            if j2 <= k:
                return False
            j = j2
        i = j
        while i < n and script[i] in " \t":
            i += 1
        if i < n and script[i] == "!":
            i += 1
            while i < n and script[i] in " \t":
                i += 1
        if i >= n:
            return False
        c = script[i]
        if c in "pd=":
            i += 1
        elif c in "qQ":
            i += 1
            while i < n and script[i].isdigit():
                i += 1
        elif c == "s":
            if i + 1 >= n:
                return False
            d = script[i + 1]
            if d.isalnum() or d.isspace() or d == "\\":
                return False
            j = _sed_regex_end(script, i + 2, d, True)
            if j < 0:
                return False
            j = _sed_regex_end(script, j, d, False)
            if j < 0:
                return False
            while j < n and script[j] in _SED_S_FLAGS:
                j += 1
            i = j
        else:
            return False
        while i < n and script[i] in " \t":
            i += 1
        if i < n and script[i] not in ";\n":
            return False


def _sed_is_safe(parts: list[str], segment: str) -> bool:
    """`sed [флаги] 'скрипт' [файлы]`. Проверка идёт по токенам после снятия
    кавычек: `'-i'`, `\\-i`, `'-e' 'w файл'` для bash те же флаги."""
    if _has_unquoted_glob(segment):
        return False
    script = None
    for p in parts[1:]:
        if p.startswith("-") and p != "-":
            if p in _SED_FLAGS:
                continue
            if not p.startswith("--") and all("-" + c in _SED_FLAGS for c in p[1:]):
                continue
            return False
        if script is None:
            script = p
    return script is not None and _sed_script_ok(script)


# systemctl только для просмотра и только по списку флагов: `--host`/`-H`/
# `-M` уводят команду на другую машину по ssh, а глагол-действие (restart,
# stop, edit) меняет службы. Неизвестный флаг значит карточка.
_SYSTEMCTL_READ_VERBS = frozenset({
    "status", "show", "list-units", "list-timers", "list-unit-files", "is-active",
    "is-failed", "is-enabled", "cat",
})
_SYSTEMCTL_FLAGS = frozenset({
    "--user", "--no-pager", "--all", "-a", "--full", "-l", "--value", "--plain",
    "--no-legend", "-q", "--quiet",
})
_SYSTEMCTL_VALUE_FLAGS = frozenset({"--property", "-p", "--type", "-t", "--state", "--lines", "-n"})


def _systemctl_read_ok(parts: list[str], segment: str) -> bool:
    if _has_unquoted_glob(segment):
        return False
    verbs = []
    i = 1
    while i < len(parts):
        a = parts[i]
        name = a.split("=", 1)[0]
        if a in _SYSTEMCTL_FLAGS:
            i += 1
        elif name in _SYSTEMCTL_VALUE_FLAGS and "=" in a:
            i += 1
        elif a in _SYSTEMCTL_VALUE_FLAGS:
            i += 2
        elif a.startswith("-"):
            return False
        else:
            verbs.append(a)
            i += 1
    return bool(verbs) and verbs[0] in _SYSTEMCTL_READ_VERBS


# gh только для чтения: пары «группа подкоманда» списком. Без `--web`/`-w`
# (браузер; у `pr checks -w` это ожидание, тоже не нужно) и без
# `--log`/`--log-failed` у `run view` (скачивает журналы в кэш). `gh api`,
# расширения и алиасы не пропускаются: алиас может быть командой shell.
# `gh api` разбирается отдельно в `_gh_api_read_ok`.
_GH_READ = {
    "pr": frozenset({"list", "view", "status", "diff", "checks"}),
    "issue": frozenset({"list", "view", "status"}),
    "run": frozenset({"list", "view"}),
    "release": frozenset({"list", "view"}),
    "repo": frozenset({"view"}),
}
_GH_BANNED_FLAGS = frozenset({"--web", "-w", "--log", "--log-failed"})


def _gh_read_ok(parts: list[str], segment: str) -> bool:
    if len(parts) < 3 or _has_unquoted_glob(segment):
        return False
    if parts[2] not in _GH_READ.get(parts[1], frozenset()):
        return False
    args = parts[3:]
    for i, a in enumerate(args):
        if a.split("=", 1)[0] in _GH_BANNED_FLAGS:
            return False
        if a.startswith("-") and not a.startswith("--") and "w" in a[1:]:
            return False
        # Репозиторий только `владелец/имя`: форма `сервер/владелец/имя` уводит
        # запрос на произвольный сервер.
        repo = None
        if a in ("-R", "--repo") and i + 1 < len(args):
            repo = args[i + 1]
        elif a.startswith("--repo="):
            repo = a.split("=", 1)[1]
        elif a.startswith("-R") and len(a) > 2:
            repo = a[2:]
        if repo is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            return False
    return True


# journalctl по списку флагов: getopt_long принимает сокращения (`--rot` это
# `--rotate`, `--vacuum-t` это `--vacuum-time`), поэтому запрет по имени
# не работает. `-f` и `-e` не пропускаем: бесконечное ожидание и пейджер.
_JOURNAL_FLAGS = frozenset({
    "--user", "--system", "--no-pager", "-r", "--reverse", "-q", "--quiet", "-a", "--all",
    "-x", "--catalog", "-k", "--dmesg", "-m", "--merge", "--utc", "--no-hostname",
    "--no-full", "--list-boots", "--disk-usage", "--case-sensitive",
})
_JOURNAL_VALUE_FLAGS = frozenset({
    "-u", "--unit", "--user-unit", "-n", "--lines", "-S", "--since", "-U", "--until", "-p",
    "--priority", "-o", "--output", "-b", "--boot", "-g", "--grep", "-t", "--identifier",
    "--output-fields", "-T", "--exclude-identifier", "--facility",
})
_JOURNAL_SHORT_BOOL = frozenset("rqaxkm")


def _journalctl_read_ok(parts: list[str], segment: str) -> bool:
    if _has_unquoted_glob(segment):
        return False
    i = 1
    while i < len(parts):
        a = parts[i]
        name = a.split("=", 1)[0]
        if a in _JOURNAL_FLAGS:
            i += 1
        elif name in _JOURNAL_VALUE_FLAGS and "=" in a:
            i += 1
        elif a in _JOURNAL_VALUE_FLAGS:
            i += 2
        elif a.startswith("-") and not a.startswith("--") and len(a) > 2 and a[1] == "n" and a[2:].isdigit():
            i += 1
        elif a.startswith("-") and not a.startswith("--") and set(a[1:]) <= _JOURNAL_SHORT_BOOL:
            i += 1
        elif a.startswith("-"):
            return False
        elif re.fullmatch(r"_?[A-Z_]+=\S*", a):
            i += 1
        else:
            return False
    return True


def _gh_api_read_ok(parts: list[str], segment: str) -> bool:
    """`gh api <путь>` только GET: без `-X`, `--input` и полей (`-f`/`-F`
    превращают запрос в POST), без `--hostname` и полного URL. GraphQL
    пропускается с единственным полем `-f query=...` без слова mutation:
    изменение в GraphQL без этого слова не записать."""
    if _has_unquoted_glob(segment):
        return False
    endpoint = query = None
    i = 2
    while i < len(parts):
        a = parts[i]
        name = a.split("=", 1)[0]
        if a in ("--paginate", "--slurp", "-i", "--include", "--silent"):
            i += 1
        elif a in ("-q", "--jq", "-t", "--template"):
            if i + 1 >= len(parts):
                return False
            i += 2
        elif name in ("--jq", "--template") and "=" in a:
            i += 1
        elif a in ("-f", "--raw-field") and query is None and i + 1 < len(parts) \
                and parts[i + 1].startswith("query="):
            query = parts[i + 1][len("query="):]
            i += 2
        elif a.startswith("-") or endpoint is not None:
            return False
        else:
            endpoint = a
            i += 1
    if endpoint is None or "://" in endpoint:
        return False
    if endpoint == "graphql":
        return query is not None and not re.search(r"mutation", query, re.IGNORECASE)
    return query is None


# awk без выхода наружу: без system(), getline, конвейеров (`|`, `|&`),
# перенаправления вывода print/printf (`> файл`), директив gawk `@include`,
# `@load` и косвенного вызова `@f()`. Флаги только -F и -v: `-f` берёт
# программу из файла, `-e`/`-i`/`-l` у gawk подключают код.
# Общий разбор сравнения и записи используется также в danger
# (ревью 11.10.2026).
_AWK_BANNED_RE = re.compile(r"system|getline|@|(?<!\|)\|(?!\|)")
_AWK_INPUT_RE = re.compile(r"\b(?:ARGV|ARGC|ENVIRON|PROCINFO)\b")


def _awk_program_ok(program: str) -> bool:
    return not (_AWK_BANNED_RE.search(program) or _AWK_INPUT_RE.search(program)
                or danger._awk_writes(program))


def _awk_read_ok(parts: list[str], segment: str) -> bool:
    if _has_unquoted_glob(segment):
        return False
    i = 1
    while i < len(parts):
        a = parts[i]
        if a in ("-F", "-v"):
            i += 2
        elif a.startswith(("-F", "-v")) and len(a) > 2:
            i += 1
        elif a.startswith("-"):
            return False
        else:
            break
    if i >= len(parts):
        return False
    if not _awk_program_ok(parts[i]):
        return False
    return not any(p.startswith("-") for p in parts[i + 1:])


# curl только на чтение внешнего адреса, по списку флагов: без записи файлов
# (`-o` только /dev/null, без `-O`, `-c`, `--output-dir`), без чтения файлов
# (`-K`, `-T`, `-F`, `@файл` в данных и заголовках, формат `-w @файл`), без
# прокси, `--resolve` и `--unix-socket`. Только GET: данные (`-d`,
# `--data-urlencode`) лишь вместе с `-G`, тогда они уходят в строку запроса.
# Адрес только http(s) с доменным именем: IP-литералы, localhost (и с точкой
# на конце), имена со встроенным IP и сервисы вида nip.io закрыты, чтобы не
# стучаться в локальные службы. Публичное имя, которое само указывает в
# домашнюю сеть, этим не отсечь; поэтому и только GET: approval-сервер на
# GET отдаёт лишь /health. Без `-L`/`--location`: редирект с публичного
# адреса на localhost или в домашнюю сеть обходил бы проверку адреса. WebFetch
# без карточки проходит по той же проверке адреса (`_curl_url_ok`).
_CURL_BOOL = frozenset({
    "-s", "--silent", "-S", "--show-error", "-f", "--fail",
    "-I", "--head", "-i", "--include", "--compressed", "-G", "--get", "-v", "--verbose",
    "-g", "--globoff",
})
_CURL_SHORT_BOOL = frozenset("sSfIiGvg")
_CURL_VALUE = frozenset({
    "-m", "--max-time", "--connect-timeout", "-A", "--user-agent", "-H", "--header",
    "-w", "--write-out", "-o", "--output", "-d", "--data", "--data-urlencode",
    "--retry", "-e", "--referer",
})


# Публичные DNS-сервисы, которые отдают адрес из самого имени
# (127.0.0.1.nip.io, 7f000001.nip.io, lvh.me в 127.0.0.1).
_CURL_DNS_REBIND = (
    ".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me", ".traefik.me",
    ".lacolhost.com", ".vcap.me", ".localhost.run",
)
_CURL_DNS_REBIND_BARE = frozenset(d.lstrip(".") for d in _CURL_DNS_REBIND)


def _curl_url_ok(url: str) -> bool:
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    host = host.rstrip(".")
    if u.scheme not in ("http", "https") or "." not in host:
        return False
    if host.endswith((".localhost", ".local", ".internal", ".lan", ".home")) or host == "localhost":
        return False
    if host.endswith(_CURL_DNS_REBIND) or host in _CURL_DNS_REBIND_BARE:
        return False
    if re.search(r"(?:^|[.-])\d{1,3}[.-]\d{1,3}[.-]\d{1,3}[.-]\d{1,3}(?:[.-]|$)", host):
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    return not re.fullmatch(r"[0-9.]+|0x[0-9a-f.x]+", host)


def _curl_read_ok(parts: list[str], segment: str) -> bool:
    if _has_unquoted_glob(segment):
        return False
    urls = 0
    has_data = False
    # `-G` считается только флагом, а не значением другого флага:
    # `--user-agent -G --data x=1` это POST (ревью 11.10.2026).
    get = False
    i = 1
    while i < len(parts):
        a = parts[i]
        if a in _CURL_BOOL or (
            a.startswith("-") and not a.startswith("--") and len(a) > 1
            and set(a[1:]) <= _CURL_SHORT_BOOL
        ):
            get = get or a == "--get" or (not a.startswith("--") and "G" in a[1:])
            i += 1
            continue
        if a.startswith("--") and "=" in a:
            name, val = a.split("=", 1)
        elif a in _CURL_VALUE:
            if i + 1 >= len(parts):
                return False
            name, val = a, parts[i + 1]
            i += 1
        elif a.startswith("-") and not a.startswith("--") and a[:2] in _CURL_VALUE and len(a) > 2:
            name, val = a[:2], a[2:]
        elif a.startswith("-"):
            return False
        else:
            # curl сам раскрывает `{a,b}` и `[1-2]` в адресе, и кавычки shell
            # этому не мешают: `{example.org,localhost}` дошёл бы до localhost
            # (ревью 11.10.2026).
            if any(c in a for c in "{}[]") or not _curl_url_ok(a):
                return False
            urls += 1
            i += 1
            continue
        if name not in _CURL_VALUE:
            return False
        if name in ("-o", "--output") and val != "/dev/null":
            return False
        if name in ("-d", "--data", "--data-urlencode"):
            if "@" in val.split("=", 1)[0]:
                return False
            has_data = True
        if name in ("-H", "--header", "-w", "--write-out") and (val.startswith("@") or "%output{" in val):
            return False
        i += 1
    return urls > 0 and (get or not has_data)


# Обёртки, которые только запускают следующую команду: `rtk` и `rtk proxy`
# (сжатие вывода, их подставляет хук rtk), `RTK_DISABLED=1`, `TZ=<зона>`,
# `timeout <срок>`. Снимаются с начала сегмента как буквальный текст, дальше
# проверяется то, что они запускают. Кавычки и двойные пробелы не снимаются:
# такой сегмент ни с чем не совпадёт и уйдёт в карточку. `rtk run` (это
# `sh -c`) после снятия становится `run ...` и тоже уходит в карточку.
_WRAPPER_RE = re.compile(
    r"(?:rtk(?: proxy)?|RTK_DISABLED=1|TZ=[A-Za-z0-9_/+-]+|timeout \d+(?:\.\d+)?[smh]?) "
)
# `git -C <каталог>` меняет только каталог: `git -C x log` проверяется как
# `git log`. Опции перед -C (`git -c ...`) сюда не попадают.
_GIT_C_RE = re.compile(r"git -C (?:'[^']*'|\"[^\"\\]*\"|[^\s'\"\\]+) ")
# xargs допишет аргументы со stdin, которые здесь не видны, поэтому внутри
# только команды, которым никакой аргумент не даёт писать или запускать.
# Печатающих содержимое (cat, head, tail) нет: имя файла со stdin не
# проверить на секрет. grep тоже читает содержимое неизвестных файлов
# со stdin, поэтому требует карточку (ревью 11.10.2026).
_XARGS_INNER = frozenset({
    "ls", "wc", "basename", "dirname", "stat",
    "realpath", "readlink", "echo", "du",
})
_XARGS_BOOL = frozenset({"-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose", "-x"})
# Без `-a`/`--arg-file`: xargs читает строки из файла и отдаёт их команде,
# а имя файла после снятия xargs уже не проверить на секрет (ревью 11.10.2026).
_XARGS_VALUE = frozenset({"-I", "-n", "-L", "-P", "-d", "-s", "-E"})


def _unwrap_segment(segment: str) -> str | None:
    """Сегмент без обёрток из `_WRAPPER_RE`, без `git -C <каталог>` и без
    `xargs [флаги]`. None, если обёртку не разобрать. `rtk grep` под капотом
    это rg, поэтому дальше он проверяется как rg (флаг `--pre` запрещён)."""
    s = segment
    while m := _WRAPPER_RE.match(s):
        rest = s[m.end():]
        if m.group(0).startswith("rtk") and rest.startswith("grep "):
            rest = "rg " + rest[len("grep "):]
        s = rest
    if s == "xargs" or s.startswith("xargs "):
        try:
            parts = shlex.split(s)
        except ValueError:
            return None
        i = 1
        while i < len(parts):
            a = parts[i]
            if a in _XARGS_BOOL:
                i += 1
            elif a in _XARGS_VALUE:
                i += 2
            elif a[:2] in _XARGS_VALUE and len(a) > 2:
                i += 1
            elif a.startswith("-"):
                return None
            else:
                break
        inner = parts[i:]
        if not inner or inner[0] not in _XARGS_INNER:
            return None
        s = shlex.join(inner)
    if m := _GIT_C_RE.match(s):
        s = "git " + s[m.end():]
    return s


_SYSTEM_READ_RE = re.compile(r"systemctl(?: .*)?|journalctl(?: .*)?|gh (?:pr|issue|run|release|repo|api) .*")

# Встроенный список чтения поверх APPROVE_BASH_AUTO_ALLOW_REGEX. sed, awk и
# curl здесь любые: их пропускает только разбор аргументов в
# `_segment_args_are_safe`. Остальное читает и ничего не меняет.
_EXTRA_READ_RE = re.compile(
    r"(?:sed|awk|gawk|mawk|curl) .+"
    r"|git (?:rev-parse|blame|shortlog|rev-list|merge-base|show-ref|for-each-ref|cat-file"
    r"|config --get(?:-all|-regexp)?)(?: .*)?"
    r"|sleep \d+(?:\.\d+)?[smh]?"
    r"|(?:pgrep|dig|host|diff|cmp|comm|nl|paste|rev|tac|seq|md5sum|sha1sum|sha256sum"
    r"|id|groups|nproc|lsblk|ps|strings|od|hexdump|printf|test|getent)(?: .*)?"
    r"|ping -c ?\d+(?: .*)?"
    r"|command -v \S+(?: \S+)*"
    r"|ip (?:-(?:4|6|br|brief|c|color|j|json|p|pretty|s|stats|d|details|o|oneline) )*"
    r"(?:a|addr|address|r|route|l|link|neigh)(?: show(?: .*)?)?"
    r"|python3 -m json\.tool(?: --[a-z-]+)*(?: [^\s-]\S*)?"
)


# uniq пишет во второй операнд: `uniq вход выход` перезаписывает файл, в том
# числе модуль скилла, который потом идёт без карточки (ревью 11.10.2026).
_UNIQ_VALUE = frozenset({"-f", "-s", "-w"})
_UNIQ_LONG_VALUE = ("--skip-fields", "--skip-chars", "--check-chars")


def _uniq_read_ok(parts: list[str], segment: str, cwd: str | None = None) -> bool:
    if _has_unquoted_glob(segment):
        # ./ не даст подставить флаг, но два совпадения дадут файл вывода
        # (ревью 11.10.2026).
        words = _bash_words(segment)
        if words is None or cwd is None:
            return False
        expanded: list[str] = []
        for word in words:
            raw = "".join(ch for ch, _unused in word)
            if any(ch in _GLOB_CHARS and not quoted for ch, quoted in word):
                if not raw.startswith("./") or "{" in raw:
                    return False
                matches = glob.glob(raw, root_dir=cwd)
                expanded.extend(matches or [raw])
            else:
                expanded.append(raw)
        parts = expanded
    operands = 0
    i = 1
    while i < len(parts):
        a = parts[i]
        if a == "--":
            operands += len(parts) - i - 1
            break
        if a.startswith("--") and "=" not in a:
            if any(o.startswith(a) for o in _UNIQ_LONG_VALUE):
                i += 1
        elif a.startswith("-") and len(a) > 1 and not a.startswith("--"):
            # Значение слитно (`-f2`, `-cf2`) или следующим словом (`-cf 2`).
            k = next((j for j, c in enumerate(a[1:], 1) if f"-{c}" in _UNIQ_VALUE), None)
            if k is not None and k == len(a) - 1:
                i += 1
        elif not a.startswith("--"):
            operands += 1
        i += 1
    return operands <= 1


# `git branch` без карточки только просмотром: `-D`, `-f`, `-m`, `-c`,
# `--set-upstream-to` и имя без режима списка меняют ветки (ревью 11.10.2026).
_GIT_BRANCH_BOOL = frozenset({
    "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "-l", "--list",
    "--show-current", "-i", "--ignore-case", "--no-color", "--color", "--no-column",
    "--column", "--omit-empty", "-q", "--quiet", "--no-abbrev", "--abbrev",
})
_GIT_BRANCH_VALUE = frozenset({"--sort", "--format"})
_GIT_BRANCH_FILTER = frozenset({"--contains", "--no-contains", "--merged", "--no-merged",
                                "--points-at"})
_GIT_BRANCH_SHORT = frozenset("arvliq")


def _git_branch_read_ok(parts: list[str], segment: str) -> bool:
    if _has_unquoted_glob(segment):
        return False
    list_mode = False
    positionals = False
    i = 2
    while i < len(parts):
        a = parts[i]
        name = a.split("=", 1)[0]
        if name in _GIT_BRANCH_VALUE:
            i += 1 if "=" in a else 2
            continue
        if name in _GIT_BRANCH_FILTER:
            list_mode = True
        elif a in ("-l", "--list") or (a.startswith("-") and not a.startswith("--")
                                         and "l" in a[1:] and set(a[1:]) <= _GIT_BRANCH_SHORT):
            list_mode = True
        elif name in _GIT_BRANCH_BOOL and (a == name or name in ("--color", "--column", "--abbrev")):
            pass
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1 \
                and set(a[1:]) <= _GIT_BRANCH_SHORT:
            pass
        elif a.startswith("-"):
            return False
        else:
            positionals = True
        i += 1
    return list_mode or not positionals


def _segment_args_are_safe(segment: str, cwd: str | None = None) -> bool:
    """False, если сегмент зовёт команду из `_BASH_DANGEROUS_ARGS` с её
    исполняющим или мутирующим флагом (`find -delete`, `fd --exec`).

    Токены снимаются `shlex`, а не `str.split`: bash видит `find . '-delete'`,
    `find . -de"lete"` и `find . -de\\lete` как один и тот же флаг `-delete`,
    поэтому сырое разбиение по пробелам денилист обходило.
    Проверяются три формы записи флага: точная (`-o`), `--flag=value`
    (`--compress-program=/tmp/x`) и связка коротких (`sort -uo out`).
    Шаблоны имён (`*`, `?`, `[`, `{`) вне кавычек у этих команд запрещены:
    раскрытие идёт после проверки и может подставить файл с именем флага.
    """
    try:
        parts = shlex.split(segment)
    except ValueError:
        return False  # незакрытая кавычка: решать нельзя, значит карточка
    if not parts:
        return True
    if parts[0] == "sed":
        return _sed_is_safe(parts, segment)
    if parts[0] == "printf" and any(p == "-v" or (p.startswith("-") and not p.startswith("--") and "v" in p)
                                    for p in parts[1:2]):
        # `printf -v VAR` пишет переменную оболочки, в том числе таблицу
        # команд BASH_CMDS: `ls` следующего сегмента станет `rm` (ревью Codex 13.10).
        return False
    if parts[0] in ("awk", "gawk", "mawk"):
        return _awk_read_ok(parts, segment)
    if parts[0] == "curl":
        return _curl_read_ok(parts, segment)
    if parts[0] in ("mkdir", "date", "hostname"):
        return parts[0] != "mkdir" and danger._system_read_ok(parts[0], parts[1:])
    if parts[:2] == ["git", "fetch"]:
        return False
    if parts[:2] == ["git", "diff"] and "--no-index" in parts:
        # Ровно два названных файла сравнения, остальные флаги проверяются
        # общим запретом ниже (ревью 11.10.2026).
        operands = [a for a in parts[2:] if not a.startswith("-")]
        if len(operands) != 2 or _has_unquoted_glob(segment):
            return False
        if any(Path(cwd or Path.cwd(), Path(p).expanduser()).is_dir() for p in operands):
            return False
    elif parts[0] == "git" and any(a.startswith("--no-index") for a in parts):
        return False
    if _is_remember_call(parts):
        if parts[-2:] == ["remember", "--help"]:
            return True
        return _remember_args(parts, segment, None) is not None
    if parts[0] == "systemctl":
        return _systemctl_read_ok(parts, segment)
    if parts[0] == "journalctl":
        return _journalctl_read_ok(parts, segment)
    if parts[:2] == ["gh", "api"]:
        return _gh_api_read_ok(parts, segment)
    if parts[0] == "gh":
        return _gh_read_ok(parts, segment)
    if parts[0] == "uniq":
        return _uniq_read_ok(parts, segment, cwd)
    if parts[:2] == ["git", "branch"]:
        return _git_branch_read_ok(parts, segment)
    if parts[:2] == ["git", "remote"]:
        return all(p in ("-v", "--verbose") for p in parts[2:])
    banned = _BASH_DANGEROUS_ARGS.get(parts[0])
    if banned is None:
        return True
    if _has_unquoted_glob(segment):
        return False
    banned_short = _BASH_DANGEROUS_SHORT.get(parts[0], frozenset())
    for p in parts[1:]:
        if p in banned:
            return False
        if p.split("=", 1)[0] in banned:
            return False
        # getopt_long и git parse-options принимают однозначное сокращение
        # длинного флага: `sort --out=f` это `--output=f`, `git grep --open=x`
        # это `--open-files-in-pager=x`. Поэтому запрещён и любой префикс.
        name = p.split("=", 1)[0]
        if name.startswith("--") and len(name) > 2 and any(
            b.startswith(name) for b in banned if b.startswith("--")
        ):
            return False
        if banned_short and p.startswith("-") and not p.startswith("--"):
            if any(c in banned_short for c in p[1:]):
                return False
    return True


# Секреты, которые Bash без карточки не читает (ревью 07.10.2026): разрешённые
# читалки (`cat`, `strings`, `od`, `sed -n p`, `diff` и т.п.) иначе выводили
# бы их агенту, а `curl` GET без карточки уносил бы наружу. Слово команды
# сверяется после снятия кавычек (`.en''v` это `.env`). `.env.example` не
# секрет и остаётся читаемым.

def _bash_words(segment: str) -> list[list[tuple[str, bool]]] | None:
    """Слова сегмента посимвольно с пометкой «символ в кавычках или
    экранирован». Шаблоны и скобки раскрывает только то, что вне кавычек."""
    words: list[list[tuple[str, bool]]] = []
    cur: list[tuple[str, bool]] = []
    started = False
    quote: str | None = None
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if quote is None:
            if ch == "\\" and i + 1 < n:
                cur.append((segment[i + 1], True))
                started = True
                i += 2
            elif ch in ("'", '"'):
                quote = ch
                started = True
                i += 1
            elif ch.isspace():
                if started:
                    words.append(cur)
                    cur, started = [], False
                i += 1
            else:
                cur.append((ch, False))
                started = True
                i += 1
            continue
        if ch == quote:
            quote = None
        elif quote == '"' and ch == "\\" and i + 1 < n and segment[i + 1] in '"\\$`':
            cur.append((segment[i + 1], True))
            i += 1
        else:
            cur.append((ch, True))
        i += 1
    if quote is not None:
        return None
    if started:
        words.append(cur)
    return words


def _word_expands_to_secret(word: list[tuple[str, bool]]) -> bool:
    """Скобки `{a,b}` и `{1..3}` вне кавычек собирают любое имя (`{.e,}nv`),
    поэтому всегда карточка. Шаблон `*?[` опасен, если компонент пути
    начинается с точки (`.en*`, `.e?v`, `.en[v]`: bash подставит `.env`, а
    `*` скрытые файлы не берёт) или путь идёт в /proc (`/proc/*/environ`)."""
    for k, (ch, q) in enumerate(word):
        if ch == "{" and not q:
            depth_text = ""
            for ch2, q2 in word[k + 1:]:
                if ch2 == "}" and not q2:
                    if "," in depth_text or ".." in depth_text:
                        return True
                    break
                depth_text += ch2 if not q2 else "\0"
    if not any(ch in "*?[" and not q for ch, q in word):
        return False
    if "".join(ch for ch, _unused in word).startswith("/proc"):
        return True
    comp: list[tuple[str, bool]] = []
    for ch, q in [*word, ("/", True)]:
        if ch != "/":
            comp.append((ch, q))
            continue
        if comp and comp[0][0] == "." and any(c in "*?[" and not cq for c, cq in comp):
            return True
        comp = []
    return False


def _segment_touches_secret(segment: str, cwd: str | None = None) -> bool:
    """Секретные источники и цели ссылок проверяются без чтения содержимого
    от каталога агента, как в danger (ревью 11.10.2026). grep с рекурсией и
    rg по каталогу всегда идут на карточку (решение владельца 14.10.2026)."""
    words = _bash_words(segment)
    if words is None or any(_word_expands_to_secret(w) for w in words):
        return True
    try:
        parts = shlex.split(segment)
    except ValueError:
        return True
    for word in words:
        raw = "".join(ch for ch, _unused in word)
        if cwd and any(ch in "*?[" and not q for ch, q in word):
            matches = glob.glob(raw, root_dir=cwd)
            # Имя файла, похожее на ключ (`-r`), после раскрытия шаблона
            # меняет смысл команды: `grep x *` становится рекурсивным
            # (решение владельца 14.10.2026).
            if any(m.startswith("-") for m in matches):
                return True
            if any(danger._secret_path(p, cwd) for p in matches):
                return True
    return danger._reads_secret(parts, cwd) or danger._searches_tree(parts, cwd)


def _bash_command_is_safe(command: str, *patterns: re.Pattern[str],
                          cwd: str | None = None) -> bool:
    """True если каждый сегмент команды fullmatch-ит хотя бы один из `patterns`,
    не содержит опасных конструкций (substitution, redirection в файл), не
    зовёт разрешённую команду с мутирующим флагом и не читает секрет
    (`_segment_touches_secret`).

    Безобидные standalone `2>/dev/null` / `2>&1` / `>/dev/null` стрипуются
    до скана — они часто встречаются в диагностических командах.
    `>/dev/null.evil` не стрипуется и остаётся запрещённым.
    """
    cmd = command.strip()
    if not cmd:
        return False
    sanitized = _BASH_SAFE_REDIRECT_RE.sub("", cmd)
    segments = _scan_bash_segments(sanitized)
    if segments is None:
        return False
    current = cwd or str(Path.cwd())
    for seg in segments:
        if not _segment_ok(seg, patterns, current):
            return False
        try:
            parts = shlex.split(_unwrap_segment(seg.strip()) or "")
        except ValueError:
            return False
        if parts[:1] == ["cd"]:
            if len(parts) != 2 or parts[1].startswith("-"):
                return False
            current = str(Path(current, Path(parts[1]).expanduser()).resolve())
    return True


def _segment_ok(segment: str, patterns: tuple[re.Pattern[str], ...] | list[re.Pattern[str]],
                cwd: str | None = None) -> bool:
    """Сегмент без обёрток совпадает с одним из `patterns`, проходит разбор
    аргументов и не трогает секретов."""
    s = _unwrap_segment(segment.strip())
    if not s:
        return False
    # После git -C пути чтения относятся к каталогу git, а не бота. Каталог
    # берётся только у самого git после обёрток, а не у любого слова git
    # в аргументах (ревью 11.10.2026).
    head = segment.strip()
    while m := _WRAPPER_RE.match(head):
        head = head[m.end():]
    if m := _GIT_C_RE.match(head):
        try:
            git_dir = shlex.split(m.group(0))[2]
        except (ValueError, IndexError):
            return False
        cwd = str(Path(cwd or Path.cwd(), Path(git_dir).expanduser()).resolve())
    return (
        any(p.fullmatch(s) for p in patterns)
        and _segment_args_are_safe(s, cwd)
        and not _segment_touches_secret(s, cwd)
    )


# Вызов скилла бота из его же venv: `<repo>/.venv/bin/python -m
# claude_tg_bot.skills.<имя> [аргументы]`. Скиллы действуют только внутри
# (темы группы бота, Dropbox), поэтому идут без карточки. Фоновые
# раны разрешены целиком раньше, plan-режим ждёт ✅. Аргументы режет тот же
# сегментный разбор (`;`, `>`, `$(`, `$VAR`).
# Скилл с внешним действием (письмо клиенту, сообщение от имени Ивана) в
# claude_tg_bot.skills не класть: он прошёл бы без карточки.
_SKILL_PYTHON = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
_SKILL_CALL_RE = re.compile(
    re.escape(str(_SKILL_PYTHON)) + r" -m claude_tg_bot\.skills\.[a-z_]+(?: .*)?"
)


# Чтение общей памяти командой Свода. Без `remember` (пишет и публикует).
# У `status` флаги только списком: `--nudge` ставит месячную метку подсказки
# старта, а запрет по имени обходится сокращением argparse (`--nu`) и
# кавычками (`--nud""ge`). Префикс MEMORY_CONFIG_DIR и его `export`
# пропускаются только со штатным каталогом.
_HOME_RE = rf"(?:~|{re.escape(str(Path.home()))})"
_MEMORY_CONFIG_RE = rf"{_HOME_RE}/\.agent-memory-config/?"
_MEMORY_READ_RE = re.compile(
    rf"(?:MEMORY_CONFIG_DIR={_MEMORY_CONFIG_RE} )?{_HOME_RE}/src/svod/bin/memory"
    r" (?:(?:recall|search|why|index|explain)(?: .*)?|status(?: --(?:fetch|json))*"
    r"|(?:[a-z]+ )?--help)"
    rf"|export MEMORY_CONFIG_DIR={_MEMORY_CONFIG_RE}"
)

# Второе мнение Codex скиллом /2mind (решение Ивана 14.10.2026): остаток
# лимита, имя модели и вызов обёрткой ask.py. Обёртка сама ставит Codex
# песочницу `--sandbox read-only` и других его флагов не принимает, поэтому
# прямой `codex exec` (там любой `--sandbox` и `-c`) остаётся на карточке.
# Скилл лежит в ~/.claude/skills, его правка идёт через карточку (danger).
# Запрос к Codex только файлом прямо в state/2mind/ (туда Write без карточки,
# `_whitelist_target_ok`): ответ и лог ask.py кладёт рядом с запросом, и
# любой другой путь давал бы запись вне каталога (ревью Codex 14.10.2026).
TWO_MIND_DIRNAME = "2mind"
_TWO_MIND_RE = re.compile(
    rf"python3 {_HOME_RE}/\.claude/skills/2mind/"
    r"(?:codex_limits\.py|latest_sol\.py(?: --all)?"
    rf"|ask\.py {re.escape(str(_SKILL_PYTHON.parents[2] / 'state' / TWO_MIND_DIRNAME))}/[A-Za-z0-9_-][A-Za-z0-9._-]*\.txt"
    r"(?: --effort (?:medium|high|xhigh)| --fallback| --cd [^\s;&|<>`$]+)*)"
)

# Сетевая разведка без записи: состояние Tailscale и слушающие порты.
_NET_READ_RE = re.compile(r"tailscale (?:status|ip|version)(?: .*)?|ss(?: -[ltunpaeHmio]+)*")


def _bash_auto_allowed(command: str, bash_re: re.Pattern[str] | None,
                       cwd: str | None = None) -> bool:
    """Bash без карточки, пока человек в чате: сегменты из вызовов скиллов,
    чтения памяти, встроенного списка чтения (`_EXTRA_READ_RE`) и команд
    APPROVE_BASH_AUTO_ALLOW_REGEX (`bash_re`, если задан)."""
    patterns = [_SKILL_CALL_RE, _MEMORY_READ_RE, _SYSTEM_READ_RE, _EXTRA_READ_RE, _NET_READ_RE,
                _TWO_MIND_RE]
    if bash_re is not None:
        patterns.append(bash_re)
    return _bash_command_is_safe(command, *patterns, cwd=cwd)


# Чтение этапа плана: свой неизменный список, без APPROVE_BASH_AUTO_ALLOW_REGEX
# (настройка может разрешать и запись) и без вызовов скиллов бота (скилл
# действует, а не читает). Ревью Codex 13.10.2026.
_PLAN_READ_RE = re.compile(
    r"(?:ls|pwd|cd|find|fd|fdfind|tree|realpath|readlink|basename|dirname|whoami|date|hostname"
    r"|uname|uptime|head|tail|wc|cat|grep|rg|jq|which|type|echo|file|stat|du|df|free|cut|sort"
    r"|uniq|column|tr|true|false|journalctl)(?: .*)?"
    r"|systemctl --user (?:status|is-active|is-failed|list-units|list-timers|cat)(?: .*)?"
    r"|git (?:status|diff|log|show|branch|remote -v|ls-files|describe|stash list|grep)(?: .*)?"
    r"|tailscale (?:status|ip|version)(?: .*)?|ss(?: -[ltunpaeHmio]+)*"
    # Версия установленной программы: имя без пути, только `--version`
    # (`npm version` и `-V` у части программ значат другое).
    r"|[a-z][a-z0-9._+-]* --version"
)


# Читающий глагол первым словом (после имени сервиса): `mark_read` это
# действие, `download_*` пишет файл, `export_chat_invite` создаёт ссылку.
_PLAN_MCP_READ_RE = re.compile(
    r"(?:(?:slack|github|grafana|gmail|notion|linear|jira|confluence|google)_)?"
    r"(?:get|list|search|read|query|fetch|describe|find|view|show|count)(?:_[a-z0-9_]*)?"
)


def _plan_read_allowed(command: str, cwd: str | None = None, *, two_mind: bool = False) -> bool:
    """Bash этапа плана: только чтение по `_PLAN_READ_RE` и чтению памяти.
    Секреты и опасные аргументы (`find -delete`, `sort -o`) режет тот же разбор.
    two_mind: ещё /2mind (`_TWO_MIND_RE`), у обдумывания темы задачи."""
    patterns = [_PLAN_READ_RE, _MEMORY_READ_RE, _EXTRA_READ_RE] + ([_TWO_MIND_RE] if two_mind else [])
    return _bash_command_is_safe(command, *patterns, cwd=cwd)


# Отказ обдумывания темы задачи: запись файлов и отправка наружу до «Делай».
_TASK_THINK_DENY = N_(
    "обдумывание задачи: до «Делай» файлы не меняются и ничего не отправляется. Опиши "
    "это действие в проекте решения, выполнение после «Делай» пойдёт в новой сессии по проекту."
)

# Запись файлов на этапе плана запрещена целиком, включая файл плана Claude
# Code в ~/.claude/plans: план нужен ответом, а не файлом.
_PLAN_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


# Запись памяти Свода из рана с человеком в чате. Тело записи бот кладёт
# файлом в state/memory-drafts/ (запись туда идёт по whitelist путей), а
# здесь пропускается только `memory remember --file <черновик>`: тело через
# `<<'EOF'` и `<(...)` разбор сегментов не принимает, и это к лучшему.
# Область global (контракт всех сессий) остаётся на карточке: внедрённый
# чужим текстом «закон» иначе молча попал бы в каждую будущую сессию.
_MEMORY_WRITE_RE = re.compile(
    rf"(?:MEMORY_CONFIG_DIR={_MEMORY_CONFIG_RE} )?{_HOME_RE}/src/svod/bin/memory remember(?: .*)?"
)
_REMEMBER_VALUE_FLAGS = frozenset({
    "--scope", "--id", "--file", "--content-type", "--source", "--session",
    "--record", "--section", "--line", "--base",
})
_REMEMBER_BOOL_FLAGS = frozenset({"--dry-run", "--json"})
_REMEMBER_SCOPE_RE = re.compile(r"personal|clients/[a-z0-9][a-z0-9_-]*")
MEMORY_DRAFTS_DIRNAME = "memory-drafts"


def _is_remember_call(parts: list[str]) -> bool:
    if parts and parts[0].startswith("MEMORY_CONFIG_DIR="):
        parts = parts[1:]
    return len(parts) >= 2 and parts[0].endswith("/src/svod/bin/memory") and parts[1] == "remember"


def _remember_args(parts: list[str], segment: str, drafts: Path | None) -> dict[str, str] | None:
    """Разбор `memory remember` по токенам после снятия кавычек. None, если
    вызов нельзя пропустить без карточки: незнакомый или сокращённый флаг
    (argparse понял бы `--sc global` как `--scope`), повтор флага, область не
    personal и не clients/<имя>, манифест вместо записи, нет `--file` (тогда
    тело читается из stdin, то есть из чего угодно по конвейеру), путь не
    абсолютный, ссылка или файл не внутри черновиков, шаблон имени вне кавычек. `drafts` None значит «черновики неизвестны»: такой
    вызов не проходит, но сегмент можно отличить от прочих."""
    if parts and parts[0].startswith("MEMORY_CONFIG_DIR="):
        parts = parts[1:]
    if drafts is None or _has_unquoted_glob(segment):
        return None
    values: dict[str, str] = {}
    flags: set[str] = set()
    tokens = parts[2:]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        name, eq, inline = tok.partition("=")
        if eq and name in _REMEMBER_VALUE_FLAGS:
            value = inline
            i += 1
        elif tok in _REMEMBER_VALUE_FLAGS:
            if i + 1 >= len(tokens):
                return None
            name, value = tok, tokens[i + 1]
            i += 2
        elif tok in _REMEMBER_BOOL_FLAGS and tok not in flags:
            flags.add(tok)
            i += 1
            continue
        else:
            return None
        if name in values:
            return None
        values[name] = value
    if not _REMEMBER_SCOPE_RE.fullmatch(values.get("--scope", "")):
        return None
    # Манифест несёт пути к файлам-источникам внутри себя (`put` с `file`) и
    # удаления: черновик стал бы окном к любому файлу машины. Только запись.
    if values.get("--content-type", "markdown") != "markdown":
        return None
    # Только абсолютный путь: `~` в кавычках или в `--file=~/...` bash не
    # раскрывает, и CLI прочёл бы другой файл, чем проверен здесь.
    raw_file = values.get("--file", "")
    if not raw_file.startswith("/"):
        return None
    try:
        draft = Path(raw_file)
        if draft.is_symlink():
            return None
        target = draft.resolve()
        root = drafts.resolve()
    except OSError:
        return None
    if target == root or root not in target.parents:
        return None
    return {
        "scope": values["--scope"],
        "name": values.get("--record") or values.get("--id", ""),
        "dry_run": "1" if "--dry-run" in flags else "",
    }


def memory_write_auto_allowed(
    command: str, bash_re: re.Pattern[str] | None, drafts: Path,
) -> list[dict[str, str]] | None:
    """Команда с `memory remember`, которую можно пропустить без карточки:
    каждый вызов remember подаёт черновик из `drafts` в personal или
    clients/<имя>, а остальные сегменты проходят те же правила, что у
    `_bash_auto_allowed`. Возвращает разобранные вызовы для уведомления,
    иначе None."""
    cmd = command.strip()
    if not cmd:
        return None
    segments = _scan_bash_segments(_BASH_SAFE_REDIRECT_RE.sub("", cmd))
    if segments is None:
        return None
    patterns = [_SKILL_CALL_RE, _MEMORY_READ_RE, _SYSTEM_READ_RE, _EXTRA_READ_RE]
    if bash_re is not None:
        patterns.append(bash_re)
    calls: list[dict[str, str]] = []
    for seg in segments:
        s = seg.strip()
        if not s:
            return None
        if _MEMORY_WRITE_RE.fullmatch(s):
            try:
                parts = shlex.split(s)
            except ValueError:
                return None
            info = _remember_args(parts, s, drafts) if _is_remember_call(parts) else None
            if info is None:
                return None
            calls.append(info)
            continue
        if not _segment_ok(s, patterns):
            return None
    return calls or None


# MCP-инструменты, где чтение от записи отличает аргумент, а не имя: Grafana
# `alerting_manage_*` (операции list, get, versions, get_*) и
# `grafana_api_request` (метод GET). Незнакомая операция значит карточка.
def _mcp_args_readonly(short_tool: str, tool_input: dict[str, Any]) -> bool:
    if short_tool in ("alerting_manage_rules", "alerting_manage_routing"):
        op = str(tool_input.get("operation", ""))
        return op in ("list", "get", "versions") or op.startswith("get_")
    if short_tool == "grafana_api_request":
        return str(tool_input.get("method") or "GET").upper() == "GET"
    return False


def _untrusted_tool_allowed(tool_name: str, tool_input: dict[str, Any], mcp_re: re.Pattern | None) -> bool:
    """Ран по чужому тексту (/event с untrusted и wake): разрешены только MCP
    на чтение. Это первый замок; второй держит сам CLI: режим dontAsk и
    правила ask (claude_runner.UNTRUSTED_ASK_TOOLS) отклоняют Bash, Write,
    Edit, WebFetch и пишущие MCP, даже если здесь ответить allow."""
    if not tool_name.startswith("mcp__"):
        return False
    short_tool = tool_name.split("__", 2)[-1]
    return bool(mcp_re is not None and mcp_re.fullmatch(short_tool)) or _mcp_args_readonly(short_tool, tool_input)


def _unattended_auto_allowed(is_unattended: bool, permission_mode: str) -> bool:
    """Фоновый ран (/at, /event с wake, скилл по таймеру) решается без
    карточки: её никто не ждёт, и ран упирается в свой таймаут (правило Ивана
    14.09.2026). Plan-режим сюда не входит: /plan запускает человек у
    телефона, и до ✅ действия идут через карточку, а плановому скиллу plan
    служит песочницей, и неразрешённое ему отклоняется в конце handle_approve.
    Пустой permission_mode (CLI его не прислал) не даёт отличить plan."""
    return is_unattended and permission_mode not in ("", "plan")


def _target_path(tool_name: str, tool_input: dict[str, Any]) -> Path | None:
    """Target file path для Write/Edit/MultiEdit/NotebookEdit, если подходит для whitelist-проверки.

    Возвращает абсолютный resolved Path только если raw уже абсолютный.

    Почему НЕ резолвим relative: бот живёт в WorkingDirectory systemd
    (`~/src/claude-tg-bot/`), а Claude — в CLAUDE_CWD (`~/src/`).
    `Path("state/foo.txt").resolve()` всегда возвращает абсолютный путь
    относительно `os.getcwd()` бота, и whitelist-решение принимается для
    `<bot-cwd>/state/foo.txt`, а Claude фактически правит
    `<claude-cwd>/state/foo.txt`. Несовпадение → можно auto-approve файл,
    которого whitelist не покрывает.

    Решение: relative = uncertain → fall through to manual approve. Это
    consistent fail-closed — пользователь увидит карточку и решит сам.
    """
    if tool_name not in _FILE_TOOLS_FOR_WHITELIST:
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return None
    # Reject relative до expanduser/resolve. expanduser нужен для `~/...` —
    # после него путь становится абсолютным. resolve() тоже допустим, он
    # только нормализует символические ссылки и `..` в уже абсолютном пути.
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return None
    try:
        return expanded.resolve()
    except OSError:
        return None


def _chat_auto_allowed(
    target: tuple[int, int | None] | None, target_key: str, keys: frozenset[str]
) -> bool:
    """Чат из APPROVE_AUTO_CHAT_KEYS: любой tool без карточки, и attended, и
    unattended. Только для зарегистрированной сессии (target известен): чужая
    или незарегистрированная сессия резолвится в primary-чат, и ей чужой
    автоаппрув доставаться не должен."""
    return target is not None and target_key in keys


def _touches_secret(tool_name: str, tool_input: dict, cwd: str | None) -> bool:
    """Команда или правка может прочитать или записать секрет (danger.Verdict.secret).
    Такое автоаппрув чата и выполнение задачи не пропускают: идёт карточкой
    (находка Codex 15.10.2026: chat-auto стоял раньше проверок секретов)."""
    if tool_name not in ("Bash", "Monitor", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return False
    return danger.classify(tool_name, tool_input, cwd).secret


_BOT_ROOT = Path(__file__).resolve().parents[2]


def _whitelist_target_ok(target: Path, state_dir: Path) -> bool:
    """Внутри репозитория бота без карточки пишутся только черновики памяти
    и запросы /2mind, что бы ни стояло в APPROVE_WHITELIST_PATHS. Правка кода бота (модуль
    скилла) плюс вызов скилла, который идёт без карточки, давали запуск
    любого кода без ведома Ивана; правка `telegram-persona.md` и файлов
    state/ (задания `/at` исполняются фоном без карточек) это закрепление
    чужих указаний. Ревью Codex 04.10.2026, решение Ивана 05.10.2026.

    Каталоги `.git` и `.obsidian` закрыты в любом разрешённом пути: git-хуки
    и плагины Obsidian (JavaScript, его исполняет приложение) это запуск
    кода, а не правка заметок. С 06.10.2026 в списке всё хранилище Obsidian.
    Так же `.codex`: настройки и хуки проекта Codex запускают команды, а
    /2mind зовёт Codex без карточки (ревью Codex 14.10.2026)."""
    if any(part in (".git", ".obsidian", ".codex") for part in target.parts):
        return False
    if target != _BOT_ROOT and _BOT_ROOT not in target.parents:
        return True
    if (state_dir / MEMORY_DRAFTS_DIRNAME).resolve() in target.parents:
        return True
    return _two_mind_request(target, state_dir)


def _two_mind_request(target: Path, state_dir: Path) -> bool:
    """Запрос к Codex для /2mind (решение Ивана 14.10.2026): только файл
    `.txt` прямо в state/2mind/, без вложенных каталогов и скрытых имён."""
    return (target.parent == (state_dir / TWO_MIND_DIRNAME).resolve()
            and target.suffix == ".txt" and not target.name.startswith("."))


def _is_under_any(target: Path, allowed: frozenset[Path]) -> bool:
    for a in allowed:
        try:
            target.relative_to(a)
            return True
        except ValueError:
            continue
    return False


async def send_approval_request(
    application: Application, cfg: Config, pending: Pending
) -> None:
    # Если run_claude зарегистрировал target — шлём туда (тот же чат/тема, где
    # пользователь начал диалог). Иначе fallback на primary — нужен на случай,
    # если hook прилетел до того, как run_claude записал session_targets, или
    # session_id в hook payload не соответствует ни одной из активных сессий.
    chat_id = pending.target_chat_id if pending.target_chat_id is not None else cfg.primary_chat_id
    thread_id = pending.target_thread_id
    text = _format_tool_call(pending.tool_name, pending.tool_input, pending.session_id, pending.cwd)
    rows = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"appr:{pending.approval_id}:allow"),
        InlineKeyboardButton("❌ Deny", callback_data=f"appr:{pending.approval_id}:deny"),
    ]]
    # ⏩ burst-кнопки показываем для всех инструментов (см. _BURST_DENY_TOOLS):
    # окно auto-approve по решению Ивана покрывает любые действия.
    # Отправке от имени Ивана окно не положено: каждая идёт своей карточкой.
    if (pending.tool_name not in _BURST_DENY_TOOLS and not _is_outbound_send(pending.tool_name)
            and MAILSEND_CARD_KEY not in pending.tool_input and not pending.no_burst):
        rows.append([
            InlineKeyboardButton("⏩ 15m", callback_data=f"appr:{pending.approval_id}:auto15"),
            InlineKeyboardButton("⏩ 1h", callback_data=f"appr:{pending.approval_id}:auto60"),
        ])
    if needs_full_view(pending.tool_input, pending.tool_name):
        rows.append([InlineKeyboardButton(
            _("📄 Полностью"), callback_data=f"apprfull:{pending.approval_id}")])
    kb = InlineKeyboardMarkup(rows)
    # PTB сам опускает message_thread_id=None — танец с kwargs не нужен.
    msg = await application.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=kb,
        parse_mode="HTML",
        message_thread_id=thread_id,
    )
    pending.telegram_message_id = msg.message_id


def warn_if_no_shared_secret(cfg: Config) -> bool:
    """Предупреждение в журнал при пустом APPROVE_SHARED_SECRET. Без секрета
    /approve и /event (с wake, то есть запуском агента) принимают запрос от
    любого локального процесса. Будить без секрета не перестаём: Домовой шлёт
    свои пробуждения без токена, пока секрет не задан с обеих сторон."""
    if cfg.approve_shared_secret:
        return False
    log.warning(
        "APPROVE_SHARED_SECRET пуст: /approve и /event (wake) принимают запросы "
        "любого локального процесса без X-Approve-Token"
    )
    return True


def _check_shared_secret(request: web.Request, cfg: Config) -> bool:
    if not cfg.approve_shared_secret:
        return True
    got = request.headers.get("X-Approve-Token", "")
    return hmac.compare_digest(got, cfg.approve_shared_secret)


# Остановка бота из его же рана (restart.py). Мягкая граница от случайности,
# не защита: обойти можно скриптом или python (ревью Codex 13.10.2026).
# claude-tg-bot-down и прочие юниты с тем же началом имени не задеваются.
_BOT_UNIT = r"claude[-_]tg[-_]bot(?:\.service)?(?![-\w])"
_SELF_RESTART_RES = (
    re.compile(r"\bsystemctl\b[^;&|\n]*\b(?:restart|try-restart|reload-or-restart|"
               r"try-reload-or-restart|condrestart|stop|kill|disable|mask)\b[^;&|\n]*\b" + _BOT_UNIT),
    re.compile(r"\b(?:pkill|killall)\b[^;&|\n]*claude[-_]tg[-_]bot"),
    re.compile(r"\bkill\b[^;&|\n]*(?:claude[-_]tg[-_]bot|MainPID)"),
)
SELF_RESTART_HINT = N_(
    "бот нельзя перезапускать или останавливать из его же рана: это оборвёт этот "
    "ран и чужие. Закоммить правку и подай заявку: "
    ".venv/bin/python -m claude_tg_bot.restart_request --reason \"что поменялось\" "
    "(из ~/src/claude-tg-bot); бот перезапустится сам, когда закончится вся работа."
)


def _self_restart_refusal(tool_name: str, tool_input: dict) -> str | None:
    if tool_name not in ("Bash", "Monitor"):
        return None
    command = str(tool_input.get("command") or "")
    if any(r.search(command) for r in _SELF_RESTART_RES):
        return _(SELF_RESTART_HINT)
    return None


async def handle_approve(request: web.Request) -> web.Response:
    """HTTP endpoint для hook_approve.py."""
    cfg: Config = request.app["cfg"]
    if not _check_shared_secret(request, cfg):
        log.warning("approve rejected: bad/missing X-Approve-Token from %s", request.remote)
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or None
    permission_mode = payload.get("permission_mode") or ""
    scheduled = payload.get("scheduled") is True
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    if not tool_name:
        return web.json_response({"error": "tool_name missing"}, status=400)
    if not isinstance(tool_input, dict):
        tool_input = {"_raw": tool_input}

    application: Application = request.app["application"]
    registry: ApprovalRegistry = request.app["registry"]
    session_targets: dict[str, tuple[int, int | None]] = request.app["session_targets"]
    audit_path = cfg.state_dir / AUDIT_LOG_NAME

    # Резолвим destination по session_id: куда отправлять approve-карточку
    # и/или whitelist-уведомление. Если sid не найден (race / unknown hook),
    # fallback на primary_chat_id (single-user старое поведение).
    target = session_targets.get(session_id) if session_id else None
    target_chat_id = target[0] if target else cfg.primary_chat_id
    target_thread_id = target[1] if target else None

    # Burst auto-approve: пользователь явно открыл окно через /auto или нажал
    # ⏩ Auto на approve-карточке. Окно scoped по Telegram target (чат + topic),
    # чтобы auto в одной ветке не разрешал tools из другой. Видимость через audit
    # decision="allow:burst" + одно уведомление при истечении окна.
    target_key = burst_target_key(target_chat_id, target_thread_id)
    burst_windows: dict[str, float] = application.bot_data.get("burst_until_by_target") or {}
    burst_until = float(burst_windows.get(target_key, 0.0))
    # burst НЕ применяем при неизвестном session_id (target резолвился на primary
    # fallback) — иначе чужая/незарегистрированная сессия попала бы в burst-окно,
    # открытое в primary-чате. Инструменты больше не фильтруем (_BURST_DENY_TOOLS
    # пуст): окно по решению Ивана разрешает любые действия.
    # Unattended-раны (/at, /event wake, plan-режим) не наследуют окно /auto:
    # человека рядом нет, а текст мог прийти из внешнего события.
    unattended_sids: set[str] = application.bot_data.get("unattended_sids") or set()
    is_unattended = scheduled or (bool(session_id) and session_id in unattended_sids)
    # Ран по чужому тексту (/event untrusted): отказ всему, кроме MCP-чтения,
    # раньше автоаппрува чата: чат Домового стоит в APPROVE_AUTO_CHAT_KEYS.
    untrusted_sids: set[str] = application.bot_data.get("untrusted_sids") or set()
    if (
        session_id and session_id in untrusted_sids
        and not _untrusted_tool_allowed(tool_name, tool_input, cfg.mcp_auto_allow_re)
    ):
        aid = uuid.uuid4().hex[:12]
        log.info("approve deny aid=%s tool=%s target=%s (untrusted)", aid, tool_name, target_key)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:untrusted",
        )
        return web.json_response({
            "decision": "deny",
            "reason": _("ран по внешнему событию (untrusted): разрешено только чтение"),
        })
    # Перезапуск или остановка самого бота из его рана обрывает этот ран и
    # чужие (restart.py): отказ до всех автоаппрувов, с подсказкой про заявку.
    self_stop = _self_restart_refusal(tool_name, tool_input)
    if self_stop:
        aid = uuid.uuid4().hex[:12]
        log.info("approve deny aid=%s tool=%s target=%s (self-restart)", aid, tool_name, target_key)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:self-restart",
        )
        return web.json_response({"decision": "deny", "reason": self_stop})
    # Отправка от имени Ивана мимо всех автоаппрувов ниже, сразу на карточку
    # (_OUTBOUND_SEND_RE).
    outbound = _is_outbound_send(tool_name)
    refusal = _outbound_refusal(tool_name, tool_input) if outbound else None
    if refusal:
        aid = uuid.uuid4().hex[:12]
        log.info("approve deny aid=%s tool=%s (outbound: %s)", aid, tool_name, refusal)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:outbound",
        )
        return web.json_response({"decision": "deny", "reason": refusal})
    # Этап плана (/plan и задачи до ✅): только чтение. Признак берётся и из
    # режима, присланного хуком, и из метки бота на сессии (plan_stage_sids):
    # режим CLI может прийти пустым. Читающее разрешается здесь, остальное
    # отказом, без карточки и мимо всех автоаппрувов ниже: в сессии плана
    # бывает чужой текст источника (ревью Codex 13.10.2026).
    plan_sids: set[str] = application.bot_data.get("plan_stage_sids") or set()
    plan_stage = permission_mode == "plan" or (bool(session_id) and session_id in plan_sids)
    # Обдумывание темы задачи с Иваном у карточек (переделка 18.10.2026): то
    # же чтение, плюс /2mind; прочее, кроме записи файлов и отправки наружу,
    # идёт карточкой мимо всех автоаппрувов ниже (force_card), а не отказом.
    # В сессии бывает чужой текст: whitelist путей, вызов скиллов, окно ⏩ и
    # regex Bash его обходили бы (ревью Codex 18.10).
    think_sids: set[str] = application.bot_data.get("task_think_sids") or set()
    task_think = (plan_stage and bool(session_id) and session_id in think_sids
                  and not is_unattended and target is not None)
    force_card = False
    if plan_stage:
        if tool_name == "Bash":
            command = str(tool_input.get("command", ""))
            ok = await asyncio.to_thread(_plan_read_allowed, command, cwd)
            if not ok and task_think:
                ok = await asyncio.to_thread(_plan_read_allowed, command, cwd, two_mind=True)
        elif tool_name == "Write" and task_think:
            # Только новый файл запроса: Edit по нему уже не /2mind (ревью Codex 18.10).
            target_path = _target_path(tool_name, tool_input)
            ok = target_path is not None and _two_mind_request(target_path, cfg.state_dir)
        elif tool_name == "WebFetch":
            ok = _curl_url_ok(str(tool_input.get("url", "")))
        elif tool_name.startswith("mcp__") and not outbound:
            # Свой список читающих глаголов: настройка MCP_AUTO_ALLOW_REGEX
            # может разрешать и запись (ревью Codex 13.10).
            short_tool = tool_name.split("__", 2)[-1]
            ok = bool(_PLAN_MCP_READ_RE.fullmatch(short_tool)) or _mcp_args_readonly(short_tool, tool_input)
        else:
            ok = False
        force_card = not ok and task_think and tool_name not in _PLAN_WRITE_TOOLS and not outbound
    if plan_stage and not force_card:
        aid = uuid.uuid4().hex[:12]
        log.info("approve %s aid=%s tool=%s (plan-stage)", "allow" if ok else "deny", aid, tool_name)
        await asyncio.to_thread(
            audit.write_event, audit_path, approval_id=aid, tool_name=tool_name,
            tool_input=tool_input, decision="allow:plan-read" if ok else "deny:plan-stage",
        )
        if ok:
            return web.json_response({"decision": "allow", "reason": "plan:read"})
        if task_think:
            return web.json_response({"decision": "deny", "reason": _(_TASK_THINK_DENY)})
        return web.json_response({
            "decision": "deny",
            "reason": _("этап плана: до одобрения плана только чтение. Файлы не меняются, "
                        "команды с записью не выполняются; план пиши в ответ. Если для плана "
                        "нужна эта команда, назови её владельцу словами."),
        })
    # Письмо модулем mailsend: карточка отправки, как у Gmail MCP
    # (MAILSEND_CARD_KEY). Снимок письма считает только бот, поле из входа
    # агента отбрасывается.
    mail_kind, mail_info = "", None
    if tool_name == "Bash":
        tool_input = {k: v for k, v in tool_input.items() if k != MAILSEND_CARD_KEY}
        mail_kind, mail_info = await asyncio.to_thread(
            _mailsend_check, str(tool_input.get("command", "")), cwd)
        if mail_kind == "deny":
            aid = uuid.uuid4().hex[:12]
            log.info("approve deny aid=%s tool=Bash (mailsend: %s)", aid, mail_info)
            await asyncio.to_thread(
                audit.write_event,
                audit_path,
                approval_id=aid,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="deny:outbound",
            )
            return web.json_response({"decision": "deny", "reason": mail_info})
        if mail_kind in ("card", "mention"):
            outbound = True
            if force_card:
                # Письмо распознаётся только здесь: до «Делай» отправки нет и
                # карточкой (ревью Codex 18.10).
                return web.json_response({"decision": "deny", "reason": _(_TASK_THINK_DENY)})
    # Чат с автоаппрувом по умолчанию (APPROVE_AUTO_CHAT_KEYS): без карточек
    # вообще, включая unattended-раны. Только для зарегистрированной сессии
    # (target известен): primary-fallback чужой сессии сюда не попадает.
    # Чтение и запись секретов идут дальше, на карточку, и у фонового рана
    # ниже тоже (находка Codex 15.10).
    secret = await asyncio.to_thread(_touches_secret, tool_name, tool_input, cwd)
    if not force_card and not outbound and not secret and _chat_auto_allowed(target, target_key, cfg.approve_auto_chat_keys):
        aid = uuid.uuid4().hex[:12]
        log.info("approve auto-allow aid=%s tool=%s target=%s (chat-auto)", aid, tool_name, target_key)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="allow:chat-auto",
        )
        return web.json_response({"decision": "allow", "reason": "chat:auto"})
    # Ран выполнения задачи после «Делай» Ивана: права выдаются этому рану, а
    # не теме (bot.py ставит и снимает метку по sid). Секреты, отправка от
    # имени Ивана и перезапуск бота идут своими путями выше и ниже (ревью
    # Codex 15.10: опасный шаг получает свою карточку, остальное идёт).
    exec_sids: set[str] = application.bot_data.get("task_exec_sids") or set()
    if not outbound and not secret and target is not None and session_id and session_id in exec_sids:
        aid = uuid.uuid4().hex[:12]
        log.info("approve auto-allow aid=%s tool=%s target=%s (task-exec)", aid, tool_name, target_key)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="allow:task-exec",
        )
        return web.json_response({"decision": "allow", "reason": "task:exec"})

    # Фоновый ран (/at, /event с wake, скилл по таймеру): без карточки, см.
    # _unattended_auto_allowed. Источники свои: /at пишет сам Иван, /event
    # принимается только с loopback и только JSON (_reject_browser_forgery), с
    # X-Approve-Token при секрете, скилл запускает код бота (SCHEDULED_ENV).
    if not outbound and not secret and _unattended_auto_allowed(is_unattended, permission_mode):
        aid = uuid.uuid4().hex[:12]
        log.info("approve auto-allow aid=%s tool=%s target=%s (unattended)", aid, tool_name, target_key)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="allow:unattended",
        )
        return web.json_response({"decision": "allow", "reason": "unattended"})

    burst_eligible = (
        target is not None and not is_unattended and tool_name not in _BURST_DENY_TOOLS
        and not outbound and not secret and not force_card
    )
    if burst_eligible and time.time() < burst_until:
        aid = uuid.uuid4().hex[:12]
        log.info(
            "approve auto-allow aid=%s tool=%s target=%s (burst until=%.0f)",
            aid, tool_name, target_key, burst_until,
        )
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="allow:burst",
        )
        return web.json_response({"decision": "allow", "reason": "burst"})

    # Path-whitelist auto-approve: Write/Edit/MultiEdit/NotebookEdit внутри разрешённых
    # каталогов — без Telegram-карточки, с пост-фактум уведомлением. Operator visibility
    # сохраняется через уведомление + audit decision="allow:whitelist".
    # Для unattended-ранов, дошедших сюда (plan-режим), авто-разрешения
    # выключены (кроме MCP-чтения, см. ниже): whitelist путей
    # включает рабочие каталоги (в т.ч. репозиторий самого бота), а bash-regex
    # допускает запись аргументами внешне безобидных команд (`sort -o`,
    # `git branch -D`). Пока человек в чате — это осознанный размен на удобство;
    # для /at, /event и plan-ранов размена нет, там всё через карточку.
    whitelist = () if is_unattended or force_card else cfg.approve_whitelist_paths
    if whitelist:
        target_path = _target_path(tool_name, tool_input)
        if (
            target_path is not None
            and _is_under_any(target_path, whitelist)
            and _whitelist_target_ok(target_path, cfg.state_dir)
        ):
            aid = uuid.uuid4().hex[:12]
            log.info("approve auto-allow aid=%s tool=%s path=%s (whitelist)", aid, tool_name, target_path)
            await asyncio.to_thread(
                audit.write_event,
                audit_path,
                approval_id=aid,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="allow:whitelist",
            )
            try:
                short = str(target_path)
                if len(short) > 100:
                    short = "…" + short[-100:]
                await application.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"✅ auto (whitelist) <b>{_esc(tool_name)}</b> <code>{_esc(short)}</code>",
                    parse_mode="HTML",
                    message_thread_id=target_thread_id,
                )
            except Exception:
                log.exception("failed to send whitelist auto-approve notification")
            return web.json_response({"decision": "allow", "reason": "whitelist"})

    # Bash auto-allow по regex: пользователь явно разрешил readonly-команды,
    # чтобы не получать approve-карточки на каждый ls/grep/journalctl. Pattern
    # настраивается через APPROVE_BASH_AUTO_ALLOW_REGEX. Mutating-команды
    # (restart/push/rm и т.п.) специально не покрываются — они продолжают идти
    # через approve. Notification в чат намеренно НЕ шлём: цель именно убрать
    # шум; видимость сохраняется через audit decision="allow:bash-safe".
    # Сюда же вызовы скиллов бота (_SKILL_CALL_RE). Для plan-рана ничего из этого.
    if tool_name == "Bash" and not is_unattended and not outbound and not force_card:
        cmd = str(tool_input.get("command", ""))
        # Проверка раскрывает шаблоны и смотрит файлы: в отдельном потоке,
        # чтобы медленная файловая система не держала остальные запросы бота.
        if await asyncio.to_thread(_bash_auto_allowed, cmd, cfg.bash_auto_allow_re, cwd=cwd):
            aid = uuid.uuid4().hex[:12]
            log.info("approve auto-allow aid=%s tool=Bash cmd=%s (regex)", aid, cmd[:80])
            await asyncio.to_thread(
                audit.write_event,
                audit_path,
                approval_id=aid,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="allow:bash-safe",
            )
            return web.json_response({"decision": "allow", "reason": "bash:safe"})
        calls = memory_write_auto_allowed(
            cmd, cfg.bash_auto_allow_re, cfg.state_dir / MEMORY_DRAFTS_DIRNAME)
        if calls:
            aid = uuid.uuid4().hex[:12]
            log.info("approve auto-allow aid=%s tool=Bash cmd=%s (memory-write)", aid, cmd[:80])
            await asyncio.to_thread(
                audit.write_event,
                audit_path,
                approval_id=aid,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="allow:memory-write",
            )
            saved = [c for c in calls if not c["dry_run"]]
            if saved:
                try:
                    names = ", ".join(f"{c['scope']} · {c['name'] or '?'}" for c in saved)
                    await application.bot.send_message(
                        chat_id=target_chat_id,
                        text=_("💾 подаю в память: <code>{names}</code>").format(names=_esc(names)),
                        parse_mode="HTML",
                        message_thread_id=target_thread_id,
                    )
                except Exception:
                    log.exception("failed to send memory-write notification")
            return web.json_response({"decision": "allow", "reason": "memory:write"})

    # MCP auto-allow: read-only инструменты (get/list/search/read/query...)
    # проходят без карточки — иначе mcp__* в hook-matcher душил бы карточками
    # и чтение из чата, и unattended-скиллы (Gmail/Slack-чекеры по таймеру).
    # Мутирующие (send/create/delete/update...) не матчатся и идут через
    # approve. Notification не шлём (как у bash-safe): видимость через audit.
    # MCP-чтение оставляем и для unattended: разбор внешнего события без
    # чтения бесполезен, а мутирующие MCP под дефолтный regex не подпадают.
    mcp_re = cfg.mcp_auto_allow_re
    if tool_name.startswith("mcp__") and mcp_re is not None and not outbound and not force_card:
        # Имя без префикса mcp__<server>__: сервер-часть содержит UUID/дефисы
        # и в regex ей делать нечего.
        short_tool = tool_name.split("__", 2)[-1]
        if mcp_re.fullmatch(short_tool) or _mcp_args_readonly(short_tool, tool_input):
            aid = uuid.uuid4().hex[:12]
            log.info("approve auto-allow aid=%s tool=%s (mcp-readonly)", aid, tool_name)
            await asyncio.to_thread(
                audit.write_event,
                audit_path,
                approval_id=aid,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="allow:mcp-readonly",
            )
            return web.json_response({"decision": "allow", "reason": "mcp:readonly"})

    # WebFetch auto-allow: пользователь явно разрешил поиск в интернете глобально.
    # Read-only по природе (никаких записей/команд). Notification в чат намеренно
    # НЕ шлём: при многошаговом research'е (3-5 fetch'ей подряд) уведомления
    # спамят. Видимость через audit decision="allow:webfetch". Адрес тот же,
    # что у curl без карточки (`_curl_url_ok`): публичное доменное имя по
    # http(s); localhost, IP-литералы и имена вида nip.io идут на карточку.
    if tool_name == "WebFetch" and _curl_url_ok(str(tool_input.get("url", ""))):
        aid = uuid.uuid4().hex[:12]
        url = str(tool_input.get("url", "?"))
        log.info("approve auto-allow aid=%s tool=WebFetch url=%s (always)", aid, url)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="allow:webfetch",
        )
        return web.json_response({"decision": "allow", "reason": "webfetch:auto"})

    # Плановый ран скилла карточку не получает никогда: его сессию бот не знает
    # и ответа никто не ждёт. Сюда он доходит только в plan-режиме (или без
    # permission_mode), где действие заранее не разрешено: отказ сразу.
    if scheduled:
        aid = uuid.uuid4().hex[:12]
        log.info("approve auto-deny aid=%s tool=%s (scheduled, mode=%s)", aid, tool_name, permission_mode or "?")
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:scheduled",
        )
        return web.json_response({"decision": "deny", "reason": "scheduled run: tool not pre-allowed"})

    card_input = tool_input
    if mail_kind == "card":
        card_input = {**tool_input, MAILSEND_CARD_KEY: mail_info}
    elif mail_kind == "mention":
        card_input = {**tool_input, MAILSEND_CARD_KEY: {"warning": True}}
    pending = registry.create(
        tool_name,
        card_input,
        session_id=session_id,
        target_chat_id=target_chat_id,
        target_thread_id=target_thread_id,
        cwd=cwd,
    )
    # Окно ⏩ обдумывание задачи не берёт (force_card): кнопки только сбивали бы.
    pending.no_burst = force_card
    log.info("approve request aid=%s tool=%s sid=%s", pending.approval_id, tool_name, (session_id or "?")[:8])
    # 2-3 попытки: секундный лаг Telegram/502 не должен молча превращаться в
    # deny — Claude обойдёт «отклонённый» шаг, а пользователь этого не увидит.
    # Fail-closed сохраняется: deny только после исчерпания попыток.
    send_ok = False
    for attempt in range(3):
        try:
            await send_approval_request(application, cfg, pending)
            send_ok = True
            break
        except Exception:
            log.exception(
                "failed to send approval request (attempt %d/3) aid=%s",
                attempt + 1, pending.approval_id,
            )
            if attempt < 2:
                await asyncio.sleep(3.0)
    if not send_ok:
        # future никем не await-ится — просто убираем из registry.
        registry.expire(pending.approval_id)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=pending.approval_id,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:telegram-send-failed",
        )
        return web.json_response({"decision": "deny", "reason": "telegram send failed"})

    try:
        decision = await asyncio.wait_for(pending.future, timeout=cfg.approve_timeout_sec)
    except asyncio.TimeoutError:
        registry.expire(pending.approval_id)
        await asyncio.to_thread(
            audit.write_event,
            audit_path,
            approval_id=pending.approval_id,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:timeout",
        )
        if pending.telegram_message_id is not None:
            try:
                await application.bot.edit_message_reply_markup(
                    chat_id=target_chat_id,
                    message_id=pending.telegram_message_id,
                    reply_markup=None,
                )
                await application.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"⌛ timeout approval aid={pending.approval_id} — deny",
                    message_thread_id=target_thread_id,
                )
            except Exception:
                log.exception("failed to mark timeout message")
        return web.json_response({"decision": "deny", "reason": "user timeout"})

    # ✅ на карточке письма: билет на ровно это письмо (хеш снимка), его
    # гасит mailsend. Билет не записался, значит и разрешать нечего.
    reason = ""
    if decision == "allow" and mail_kind == "card":
        try:
            await asyncio.to_thread(mailsend.write_ticket, cfg.state_dir, mail_info["digest"],
                                    cfg.approve_shared_secret, pending.approval_id)
        except OSError:
            log.exception("mailsend ticket write failed aid=%s", pending.approval_id)
            decision, reason = "deny", _("бот не записал одобрение письма")
    await asyncio.to_thread(
        audit.write_event,
        audit_path,
        approval_id=pending.approval_id,
        tool_name=tool_name,
        tool_input=tool_input,
        decision=decision,
    )
    log.info("approve resolved aid=%s decision=%s", pending.approval_id, decision)
    if reason:
        return web.json_response({"decision": decision, "reason": reason})
    return web.json_response({"decision": decision})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_event(request: web.Request) -> web.Response:
    """Внешнее событие (крон, алерт, скрипт) → уведомление в Telegram, при
    wake=true — ещё и запуск claude на первичный разбор.

    POST /event {"source": "...", "title": "...", "text": "...", "wake": bool,
                 "chat_id": int?, "thread_id": int?, "prompt": str?, "fresh": bool?,
                 "untrusted": bool?, "silent": bool?, "notice": bool?}
    fresh=true вместе с wake: сессия чата сбрасывается перед запуском, как при
    idle-TTL. Нужно таймерам, чьё состояние живёт в файлах, а не в сессии
    (тренер: журнал), иначе контекст растёт без предела.
    untrusted=true вместе с wake: в тексте чужие данные (письма, сообщения),
    ран идёт только на чтение: без Bash, правки файлов, WebFetch, WebSearch и
    мутирующих MCP. chat_id обязан быть среди разрешённых чатов бота.
    silent=true: уведомление уходит без звука, а вместе с wake без звука и всё,
    что бот отправит в рамках этого рана (ответ, его куски, файлы, вопрос
    кнопками, служебные сообщения), см. _SILENT_RUN в bot.py. Утреннее
    пробуждение Домового: не будить человека. Другие раны не затрагиваются.
    notice=false (только вместе с wake): уведомления «🔔» нет, в чате только
    статус рана (он удаляется) и ответ. Утреннее пробуждение Домового: заголовок «Утро» без
    текста давал пустую карточку перед планом. Если будить нечем (фабрика не
    установлена), карточка уходит всё равно, иначе событие не оставит следа.
    Auth: как /approve — X-Approve-Token при заданном secret; сервер и так
    слушает loopback, внешним источникам нужен туннель/прокси.
    """
    cfg: Config = request.app["cfg"]
    if not _check_shared_secret(request, cfg):
        return web.json_response({"error": "bad token"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    source = str(payload.get("source") or "event").strip()[:60]
    title = str(payload.get("title") or "").strip()[:200]
    text = str(payload.get("text") or "").strip()[:3000]
    if not title and not text:
        return web.json_response({"error": "title or text required"}, status=400)
    chat_id = payload.get("chat_id") or cfg.primary_chat_id
    thread_id = payload.get("thread_id")
    if not isinstance(chat_id, int) or (thread_id is not None and not isinstance(thread_id, int)):
        return web.json_response({"error": "chat_id/thread_id must be int"}, status=400)
    if chat_id not in cfg.allowed_chat_ids and chat_id not in cfg.allowed_user_ids:
        # Те же чаты, где бот и так работает: группы из TELEGRAM_ALLOWED_CHAT_IDS
        # и личные чаты разрешённых пользователей (chat_id = user_id).
        log.warning("event rejected: chat_id=%s not allowed", chat_id)
        return web.json_response({"error": "chat_id not allowed"}, status=403)
    fresh = payload.get("fresh", False)
    if not isinstance(fresh, bool):
        # Строка "false" прошла бы как истина и молча оборвала бы разговор.
        return web.json_response({"error": "fresh must be a JSON boolean"}, status=400)
    untrusted = payload.get("untrusted", False)
    if not isinstance(untrusted, bool):
        # Строка "true" не должна молча давать полные права, а "false" урезать
        # их: тип только JSON boolean, иначе отказ, и отправитель увидит ошибку.
        return web.json_response({"error": "untrusted must be a JSON boolean"}, status=400)
    silent = payload.get("silent", False)
    if not isinstance(silent, bool):
        return web.json_response({"error": "silent must be a JSON boolean"}, status=400)
    with_notice = payload.get("notice", True)
    if not isinstance(with_notice, bool):
        return web.json_response({"error": "notice must be a JSON boolean"}, status=400)
    if not with_notice and not payload.get("wake"):
        # Без карточки и без пробуждения событие не оставило бы в чате ничего.
        return web.json_response({"error": "notice=false requires wake=true"}, status=400)

    application: Application = request.app["application"]
    if application.bot_data.get("draining") or application.bot_data.get("stopping"):
        # Бот ждёт конца работы перед перезапуском (restart.py): событие не
        # принято, отправитель повторит позже. До уведомления, чтобы повтор
        # не дал второе (ревью Codex 13.10.2026).
        return web.json_response({"error": "restarting, retry later"}, status=503)
    # С этого места событие считается принятым: перезапуск ждёт его, а
    # пробуждение идёт даже после начала ожидания (ревью Codex 13.10.2026).
    # Без await между проверкой выше и счётом.
    application.bot_data["admitted_events"] = application.bot_data.get("admitted_events", 0) + 1
    handed_off = False
    try:
        response, handed_off = await _handle_event_admitted(
            application, payload, source, title, text, chat_id, thread_id, fresh, untrusted, silent,
            with_notice)
        return response
    finally:
        if not handed_off:
            application.bot_data["admitted_events"] -= 1


async def _handle_event_admitted(
    application: Application, payload: dict, source: str, title: str, text: str,
    chat_id: int, thread_id: int | None, fresh: bool, untrusted: bool, silent: bool,
    with_notice: bool = True,
) -> tuple[web.Response, bool]:
    """Уведомление и пробуждение принятого события. Второе значение: учёт
    принятого события передан задаче пробуждения (она снимет его сама)."""
    notice = f"🔔 <b>{_esc(source)}</b>" + (f": {_esc(title)}" if title else "")
    if text:
        notice += f"\n{_esc_truncate(text, TRUNCATE_PAYLOAD)}"

    sent = None
    # Без карточки только когда пробуждение точно будет: при недоступной
    # фабрике ниже ответ 202 и рана нет, карточка остаётся единственным следом.
    if with_notice or application.bot_data.get("event_wake") is None:
        try:
            sent = await application.bot.send_message(
                chat_id=chat_id, text=notice, parse_mode="HTML", message_thread_id=thread_id,
                disable_notification=silent,
            )
        except Exception:
            log.exception("event notice failed source=%s", source)
            return web.json_response({"error": "telegram send failed"}, status=502), False

    if payload.get("wake"):
        # Ленивая привязка: bot.py кладёт сюда coroutine-фабрику при старте,
        # чтобы approval.py не импортировал bot (цикл).
        wake = application.bot_data.get("event_wake")
        if wake is None:
            log.error("event wake requested, но фабрика не установлена")
            return web.json_response(
                {"ok": True, "wake": False, "reason": "wake unavailable"}, status=202
            ), False
        prompt = str(payload.get("prompt") or "").strip() or (
            _("Внешнее событие от «{source}»: {title}\n\n{text}\n\n"
              "Сделай первичный разбор: что это, серьёзно ли, что проверить. "
              "Ничего не мутируй без approve.").format(source=source, title=title, text=text)
        )
        # Трекаем задачу: голый create_task не дожидается shutdown и теряет
        # ссылку (сборщик может убить её на середине).
        tasks: set[asyncio.Task] = application.bot_data.setdefault("event_wake_tasks", set())
        task = asyncio.create_task(
            wake(chat_id, thread_id, prompt, fresh, untrusted=untrusted, silent=silent))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

        def _release(_: asyncio.Task) -> None:
            application.bot_data["admitted_events"] -= 1

        task.add_done_callback(_release)
        handed_off = True
    else:
        handed_off = False

    # message_id отдаётся наружу затем, что отправитель может связать по нему
    # будущий ответ человека. Старым вызывающим это ничего не ломает: они
    # смотрят на код ответа.
    out: dict = {"ok": True, "message_id": getattr(sent, "message_id", None)}
    return web.json_response(out), handed_off


async def handle_restart(request: web.Request) -> web.Response:
    """Заявка на перезапуск бота (restart_request.py, restart.py).

    POST /restart {"reason": str, "commit": str, "chat_key": str?}
    chat_key: тема, из рана которой подана заявка; туда придут сообщения о
    ходе перезапуска. Пусто = личный чат владельца. 202 = принята, бот
    перезапустится после конца всей текущей работы. Auth как у /approve."""
    cfg: Config = request.app["cfg"]
    if not _check_shared_secret(request, cfg):
        return web.json_response({"error": "bad token"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "bad json"}, status=400)
    reason = str(payload.get("reason") or "").strip()[:300]
    commit = str(payload.get("commit") or "").strip()
    chat_key = str(payload.get("chat_key") or "").strip() or str(cfg.primary_chat_id)
    try:
        chat_id = int(chat_key.split(":", 1)[0])
    except ValueError:
        return web.json_response({"error": "bad chat_key"}, status=400)
    if chat_id not in cfg.allowed_chat_ids and chat_id not in cfg.allowed_user_ids:
        return web.json_response({"error": "chat_id not allowed"}, status=403)
    application: Application = request.app["application"]
    begin = application.bot_data.get("restart_begin")
    if begin is None:
        return web.json_response({"error": "restart unavailable"}, status=503)
    status, body = await begin(chat_key, reason, commit)
    return web.json_response(body, status=status)


TASK_WORDS_MAX = 3000


async def handle_task(request: web.Request) -> web.Response:
    """Задача по словам Ивана из другого входа (Домовой, скилл task_new):
    POST /task {"words": "...", "context": "...", "key": "...", "chat_key":
    "...", "ask_id": N}. words: исходные слова Ивана, а не пересказ (ревью
    Codex 15.10); context: пояснение Домового, справка; key: id запроса,
    повтор с тем же key второй темы не заведёт; chat_key и ask_id: тема рана,
    позвавшего task_new, и сообщение Ивана, на которое ответит строка итога
    после закрытия (19.10.2026); to: id открытой задачи, тогда слова не
    заводят новую, а добавляются в её тему (решение Ивана 19.10.2026). Ответ {"text": "Задача «N имя»: ссылка"} или причина
    отказа. Auth как у /event: X-Approve-Token при заданном секрете, loopback."""
    cfg: Config = request.app["cfg"]
    if not _check_shared_secret(request, cfg):
        return web.json_response({"error": "bad token"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "bad json"}, status=400)
    words = str(payload.get("words") or "").strip()[:TASK_WORDS_MAX]
    context = str(payload.get("context") or "").strip()[:TASK_WORDS_MAX]
    key = re.sub(r"[^A-Za-z0-9_.-]", "", str(payload.get("key") or ""))[:64]
    chat_key = str(payload.get("chat_key") or "")
    if not re.fullmatch(r"-?\d{1,20}(:\d{1,12})?", chat_key):
        chat_key = ""
    ask = payload.get("ask_id")
    ask_id = ask if isinstance(ask, int) and not isinstance(ask, bool) and 0 < ask < 2**53 else None
    to = str(payload.get("to") or "")
    if to and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", to):
        return web.json_response({"error": "bad task id"}, status=400)
    if not words:
        return web.json_response({"error": "words required"}, status=400)
    application: Application = request.app["application"]
    factory = application.bot_data.get("task_new")
    if factory is None:
        return web.json_response({"error": "tasks unavailable"}, status=503)
    text = await factory(words, context, key, chat_key=chat_key, ask_id=ask_id, to=to)
    return web.json_response({"text": text})


def _browser_forgery_reason(method: str, content_type: str, host_header: str, bind_host: str) -> str:
    """Почему запрос похож на подделку из браузера (пусто = пропустить).

    Loopback-порт в WSL2 доступен и программам Windows, в том числе браузеру:
    страница может вслепую отправить POST на 127.0.0.1, а /event с wake будит
    агента, у которого фоновые раны идут без карточек. Браузер не отправит на
    чужой адрес `Content-Type: application/json` без CORS-preflight, который
    здесь не обслуживается, а подмену адреса через DNS (rebinding) выдаёт
    заголовок Host. Свои клиенты (хук, тренер, Домовой) шлют JSON на loopback.
    """
    if method != "POST":
        return ""
    if content_type != "application/json":
        return "application/json required"
    if bind_host in LOOPBACK_HOSTS:
        hostname = urlsplit("//" + host_header).hostname or ""
        if hostname not in LOOPBACK_HOSTS:
            return "loopback Host required"
    return ""


@web.middleware
async def _reject_browser_forgery(request: web.Request, handler):
    cfg: Config = request.app["cfg"]
    reason = _browser_forgery_reason(
        request.method, request.content_type, request.host, cfg.approve_http_host,
    )
    if reason:
        log.warning("http rejected %s %s: %s (Host=%r)", request.method, request.path, reason, request.host)
        return web.json_response({"error": reason}, status=403)
    return await handler(request)


def build_aiohttp_app(
    application: Application,
    cfg: Config,
    registry: ApprovalRegistry,
    session_targets: dict[str, tuple[int, int | None]],
) -> web.Application:
    app = web.Application(middlewares=[_reject_browser_forgery])
    app["application"] = application
    app["cfg"] = cfg
    app["registry"] = registry
    app["session_targets"] = session_targets
    app.router.add_post("/approve", handle_approve)
    app.router.add_post("/event", handle_event)
    app.router.add_post("/restart", handle_restart)
    app.router.add_post("/task", handle_task)
    app.router.add_get("/health", handle_health)
    return app

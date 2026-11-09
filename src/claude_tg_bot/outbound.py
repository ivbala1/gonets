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
    # Если обрезали посреди HTML-entity (`&lt;`, `&amp;` и т.п.) — откатываемся
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


def _single_quoted(cmd: str) -> list[bool]:
    """Для каждого знака: внутри одинарных кавычек оболочки, где `$(...)` и
    обратные кавычки не исполняются. В двойных кавычках `'` обычный знак.
    Комментарий (`#` в начале слова вне кавычек, до конца строки) кавычек не
    открывает: апостроф в `# don't` не прячет подстановки ниже. Сам текст
    комментария считается исполняемым: лишняя карточка дешевле."""
    out, single, double, esc, comment, prev = [], False, False, False, False, "\n"
    for c in cmd:
        out.append(single and c != "'")
        if esc:
            esc = False
        elif single:
            single = c != "'"
        elif comment:
            comment = c != "\n"
        elif c == "\\":
            esc = True
        elif c == '"':
            double = not double
        elif double:
            pass
        elif c == "'":
            single = True
        elif c == "#" and (prev.isspace() or prev in ";&|()<>"):
            comment = True
        prev = c
    return out


def _substitutions(cmd: str) -> list[str]:
    """Тексты подстановок $(...) и `...` вне одинарных кавычек (там они
    текст, финальное ревью 03.11.2026). Скобки внутри считаются без учёта
    кавычек, незакрытая подстановка идёт до конца строки."""
    quoted = _single_quoted(cmd)
    out = [m.group(1) for m in re.finditer(r"`([^`]*)`?", cmd) if not quoted[m.start()]]
    for m in re.finditer(r"\$\(", cmd):
        if quoted[m.start()]:
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
    cmd = cmd.replace("\\\n", "")
    bodies: list[tuple[str, str]] = []

    def cut(m: re.Match[str]) -> str:
        line = cmd[:m.start()].rsplit("\n", 1)[-1]
        bodies.append((re.split(r"\$\(|[;|&(`]", line)[-1], m.group(4)))
        return m.group(3)

    shell = _HEREDOC_RE.sub(cut, cmd)
    out = [body for opener, body in bodies if _text_head(opener.split()) not in _TEXT_TOOLS]
    out += [inner if depth >= 3 else executed_text(inner, depth + 1) for inner in _substitutions(shell)]
    try:
        # `;` после перевода строки, а не вместо: комментарий shlex читает до
        # конца строки, и `# x` не съедает команды на следующих строках.
        lex = shlex.shlex(shell.replace("\n", "\n;"), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return cmd
    part: list[str] = []
    for t in tokens + [";"]:
        if t and set(t) <= _SHELL_OPS:
            if part and _text_head(part) not in _TEXT_TOOLS:
                out.append(shlex.join(part))
            part = []
        else:
            part.append(t)
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
    карточке: для пометки (решение Ивана 22.10.2026: два нажатия остаются,
    сверять проще). Адресата бот не сверяет: в отклике он записан свободным
    текстом, поэтому Иван сверяет его сам с «Куда»."""
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
    plain = [p for p in bad if not _bad_name(p)]
    cps = ["cp -f " + " ".join(shlex.quote(p) for p in plain) + " " + shlex.quote(dest)] if plain else []
    cps += ["cp -f " + shlex.quote(p) + " " + shlex.quote(dest + _safe_name(p)) for p in bad if _bad_name(p)]
    cp = " && ".join(cps)
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
                 unattended: bool, mail_kind: str = "", mail_info: Any = None) -> Verdict:
    """Одна точка решения по отправке от имени Ивана (MCP или mailsend с
    mail_kind из mailsend_check). Себе без карточки, в том числе из фона.
    Человеку из фонового рана сразу отказ без карточки: Ивана рядом нет, а
    карточка протухла бы за срок (решение Ивана 03.11.2026, п. 6). Путь
    send_file, который сервер Telegram не примет, отказ до карточки."""
    if mail_kind:
        why = mail_info if mail_kind == "deny" else None
    else:
        why = _refusal(tool_name, tool_input)
    if why:
        return Verdict("deny", "deny:outbound", why)
    to_self = not mail_kind and _to_self(tool_name, tool_input, cfg)
    if unattended and not to_self:
        return Verdict("deny", "deny:outbound-unattended", _(UNATTENDED_HINT))
    if tool_name == SEND_FILE:
        why = await _path_check(tool_input.get("file_path"), getattr(cfg, "telegram_mcp_unit", None))
        if why:
            return Verdict("deny", "deny:outbound-path", why)
    if to_self:
        return Verdict("allow", "allow:self", "outbound:self")
    return Verdict("card")

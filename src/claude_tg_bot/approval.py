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

import asyncio
import contextlib
import difflib
import functools
import glob
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import shlex
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from telegram.error import NetworkError
from telegram.ext import Application

from . import audit, buttons, events, mailsend, outbound, paths, runs, serious
from .burst import burst_target_key
from .config import LOOPBACK_HOSTS, Config
from .i18n import _, N_
from .limits import clock
from .outbound import _esc, _esc_truncate
from .replies import is_unconfirmed

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
# С 28.10.2026 (решение Ивана «Пусть окно пускает») окно пускает и команды,
# которые читают или пишут секрет: кнопки ⏩ на такой карточке были, а
# следующая команда с токеном снова шла карточкой (до 04.11.2026 в audit
# decision="allow:burst:secret"). Отправка наружу и фоновые раны окно
# по-прежнему не берёт. С 29.10.2026 (решение Ивана «⏩ и дубли») окно берёт и
# обдумывание задачи: там без него каждая проверка шла карточкой.
# Set оставлен пустым, чтобы при необходимости снова что-то исключить точечно.
_BURST_DENY_TOOLS: frozenset[str] = frozenset()

# Карточка плана (шаг 2 карточек, решение Ивана 29.10.2026): текст плана во
# входе карточки под этим ключом. Агент просит её командой скилла plan; права
# выдаёт только нажатие «✅ Делай план», и только запросившему запуску (runs.py).
PLAN_CARD_KEY = "_plan"
# План целиком на одной карточке: Иван одобряет то, что прочёл.
PLAN_TEXT_MAX = 2500


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
    # Чат рана, если карточка показана не там (мастерская показывает в личке).
    origin_key: str = ""
    # Рабочий каталог агента из входа хука: относительный путь в команде
    # оценивается от него (ревью 11.10.2026).
    cwd: str | None = None
    # Запуск claude, который ждёт ответа (runs.py): разрешение на план выдаётся ему.
    run_id: str = ""
    # Срок нажатия (time.time()): его показывает карточка (ux2, 01.11.2026).
    deadline: float = 0.0
    # HTML отправленной карточки: итог срока дописывается в неё правкой.
    card_html: str = ""
    # Текст карточки, как его показывает Telegram: на ✅ сверяется с текстом
    # сообщения в нажатии. Агент знает ключ бота и мог переписать карточку
    # через Bot API (проект 03.11.2026, п. 4).
    card_text: str = ""
    # Отправка людям мимо карточки отправки (serious.Outbound): окно ⏩ тоже
    # не положено.
    sends: bool = False
    # Клавиатура карточки, как её нарисовал бот (keyboard_rows): на ✅
    # сверяется с клавиатурой сообщения в нажатии, подмена подписи кнопки
    # даёт отказ (ревью 03.11.2026).
    card_keyboard: tuple = ()
    # Сущности текста из ответа Telegram (entity_rows); None: ответа не было.
    card_entities: tuple | None = None


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
        origin_key: str = "",
        run_id: str = "",
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
            origin_key=origin_key,
            run_id=run_id,
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


_DESCRIPTION_CAP = 200
FULL_VIEW_THRESHOLD = 1500


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
        shown = {"Bash": {"command", "description", outbound.MAILSEND_CARD_KEY},
                 "Write": {"file_path", "content"},
                 "Edit": {"file_path", "old_string", "new_string"}}.get(tool_name)
        if shown is not None:
            rest = {k: v for k, v in tool_input.items() if k not in shown}
            if rest:
                main += _("\n\n--- остальные поля ---\n") + json.dumps(rest, ensure_ascii=False, indent=2)
    return main


def _full_view_main(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, dict) and outbound.is_send(tool_name):
        # Вложения именем и размером: base64 в просмотре не нужен.
        shown = dict(tool_input)
        if isinstance(shown.get("attachments"), list):
            shown["attachments"] = [
                {"filename": a.get("filename"), "mimeType": a.get("mimeType"),
                 "base64_chars": len(str(a.get("content") or ""))}
                if isinstance(a, dict) else str(a) for a in shown["attachments"]]
        texts = [_("--- {label} ---\n{value}").format(label=_(label), value=shown.pop(key))
                 for key, label in outbound.TEXT_FIELDS
                 if shown.get(key) not in (None, "")]
        return (json.dumps(shown, ensure_ascii=False, indent=2) + "\n\n" + "\n\n".join(texts)).rstrip()
    if isinstance(tool_input, dict):
        if tool_name == "Bash":
            desc = str(tool_input.get("description", ""))
            head = _("# Пояснение агента: {desc}\n\n").format(desc=desc) if desc else ""
            mail = tool_input.get(outbound.MAILSEND_CARD_KEY)
            tail = f"\n\n{outbound.mailsend_full_view(mail)}" if isinstance(mail, dict) else ""
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
    """Карточка: заголовок, затем описание вызова (HTML, escape-нуто). С
    04.11.2026 карточка только на отправку людям, оценки опасности по разбору
    команды нет. Если вышло длиннее лимита Telegram, тело заменяется
    обрезанным полным текстом: целиком он доступен по «📄»."""
    head = _("📤 <b>отправка людям от твоего имени</b>: проверь адресата и текст")
    card = f"{head}\n{_format_tool_body(tool_name, tool_input, session_id)}"
    if len(card) <= CARD_TEXT_CAP:
        return card
    # Отправка наружу: сырой текст вместо карточки терял заголовок «от твоего
    # имени», текст письма и адресата в конце длинной команды (аудит outbound
    # 31.10.2026). Ужимается текст сообщения, адресаты и тема остаются.
    mail = tool_input.get(outbound.MAILSEND_CARD_KEY) if tool_name == "Bash" and isinstance(tool_input, dict) else None
    if (isinstance(mail, dict) and not mail.get("warning")) or (
            outbound.is_send(tool_name) and isinstance(tool_input, dict)):
        for cap in (1000, 400, 100):
            card = f"{head}\n{_format_tool_body(tool_name, tool_input, session_id, text_cap=cap)}"
            if len(card) <= CARD_TEXT_CAP:
                return card
    raw = full_view_text(tool_name, tool_input)
    return (f"{head}\n🛠 <b>{_esc_truncate(str(tool_name), 100)}</b>  "
            + _("<i>карточка длинная, целиком по кнопке 📄</i>") + "\n"
            f"<pre>{_esc_truncate(raw, 2500)}</pre>")


def _format_tool_body(tool_name: str, tool_input: dict[str, Any], session_id: str | None,
                      text_cap: int = TRUNCATE_PAYLOAD) -> str:
    """Короткое человекочитаемое описание tool call для Telegram (HTML, всё escape-нуто).
    text_cap: предел текста письма и сообщения на карточке отправки."""
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
        mail = tool_input.get(outbound.MAILSEND_CARD_KEY)
        if isinstance(mail, dict):
            # Письмо целиком важнее команды: команда коротко, полностью по 📄.
            return (f"{header}\n{outbound.format_mailsend(mail, text_cap)}\n{note}\n"
                    f"<pre>{_esc_truncate(cmd, 400 if text_cap >= TRUNCATE_PAYLOAD else 150)}</pre>")
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
    if outbound.is_send(tool_name):
        return f"{header}\n{outbound.format_card(tool_input, text_cap, tool_name)}"
    # generic fallback
    dumped = json.dumps(tool_input, ensure_ascii=False, indent=2)
    return f"{header}\n<pre>{_esc_truncate(dumped, TRUNCATE_PAYLOAD)}</pre>"


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


# Секреты, которые Bash без карточки не читает (ревью 07.10.2026): разрешённые
# читалки (`cat`, `strings`, `od`, `sed -n p`, `diff` и т.п.) иначе выводили
# бы их агенту, а `curl` GET без карточки уносил бы наружу. Слово команды
# сверяется после снятия кавычек (`.en''v` это `.env`). `.env.example` не
# секрет и остаётся читаемым.


# Вызов скилла бота из его же venv: `<repo>/.venv/bin/python -m
# claude_tg_bot.skills.<имя> [аргументы]`. Скиллы действуют только внутри
# (темы группы бота, Dropbox), поэтому идут без карточки. Фоновые
# раны разрешены целиком раньше, plan-режим ждёт ✅. Аргументы режет тот же
# сегментный разбор (`;`, `>`, `$(`, `$VAR`).
# Скилл с внешним действием (письмо клиенту, сообщение от имени Ивана) в
# claude_tg_bot.skills не класть: он прошёл бы без карточки.
_SKILL_PYTHON = paths.VENV_PYTHON


# Чтение общей памяти командой Свода. Без `remember` (пишет и публикует).
# У `status` флаги только списком: `--nudge` ставит месячную метку подсказки
# старта, а запрет по имени обходится сокращением argparse (`--nu`) и
# кавычками (`--nud""ge`). Префикс MEMORY_CONFIG_DIR и его `export`
# пропускаются только со штатным каталогом.
_HOME_RE = rf"(?:~|{re.escape(str(Path.home()))})"
_MEMORY_CONFIG_RE = rf"{_HOME_RE}/\.agent-memory-config/?"

# Второе мнение Codex скиллом /2mind (решение Ивана 14.10.2026): остаток
# лимита, имя модели и вызов обёрткой ask.py. Обёртка сама ставит Codex
# песочницу `--sandbox read-only` и других его флагов не принимает. С
# 04.11.2026 прочие запуски Codex и правка скилла идут без карточки (карточка
# только на отправку людям); список нужен этапу плана и обдумыванию задачи.
# Запрос к Codex только файлом прямо в state/2mind/ (туда Write без карточки,
# `_whitelist_target_ok`): ответ и лог ask.py кладёт рядом с запросом, и
# любой другой путь давал бы запись вне каталога (ревью Codex 14.10.2026).
TWO_MIND_DIRNAME = "2mind"


# Заявка на перезапуск бота из обдумывания темы задачи без карточки (решение
# Ивана 04.11.2026). Сверка строки целиком, без разбора по сегментам и
# срезания хвостов (совет Codex 04.11): `;`, `$(…)`, переменные впереди и
# лишние флаги не проходят. Причина в двойных кавычках без `$`, обратной
# кавычки, `\` и `"` либо в одинарных. Относительный `.venv/bin/python` только
# после `cd` в репозиторий или из него. Полный путь ещё из каталога, где
# python нечего импортировать: `python -m` ставит каталог запуска первым в
# sys.path, и свой `httpx.py` или пакет `claude_tg_bot` рядом запустился бы
# вместо бота (ревью Codex и /code-review 04.11). Путь к репозиторию
# пишется полностью или через `~`, как в подсказке SELF_RESTART_HINT.
_RESTART_ROOT = paths.BOT_ROOT


def _home_forms(path: Path) -> str:
    """Регулярка пути: полностью или через `~`, если путь в домашнем каталоге."""
    forms = [re.escape(str(path))]
    with contextlib.suppress(ValueError):
        forms.append(re.escape("~/" + str(path.relative_to(Path.home()))))
    return "(?:" + "|".join(forms) + ")"


# Причина отдельным словом или через `=`, в конце допускается ` 2>&1`;
# `./.venv/bin/python` то же, что `.venv/bin/python` (ревью Opus 04.11.2026).
_RESTART_TAIL = (r" -m claude_tg_bot\.restart_request --reason[ =]"
                 r"""(?:"[^"$`\\\r\n]*"|'[^'\r\n]*')(?: 2>&1)?""")
_RESTART_CD_RE = re.compile(
    rf"cd {_home_forms(_RESTART_ROOT)}/? && "
    rf"(?:(?:\./)?\.venv/bin/python|{_home_forms(_SKILL_PYTHON)}){_RESTART_TAIL}")
_RESTART_REL_RE = re.compile(rf"(?:\./)?\.venv/bin/python{_RESTART_TAIL}")
_RESTART_ABS_RE = re.compile(rf"{_home_forms(_SKILL_PYTHON)}{_RESTART_TAIL}")
_IMPORTABLE_SUFFIXES = (".py", ".pyc", ".pyw", ".so", ".pyd")


@functools.lru_cache(maxsize=1)
def _shadowable_names() -> frozenset[str]:
    """Имена верхнего уровня, которые заявка может импортировать: стандартная
    библиотека, пакеты venv бота и сам `claude_tg_bot`. Свой `tests/` или
    скрипт в чужом репозитории их не перекрывает и карточки не даёт (ревью Opus 04.11)."""
    # Плюс то, что httpx и httpcore пробуют импортировать по желанию (ревью Opus 04.11).
    names = set(sys.stdlib_module_names) | {"claude_tg_bot", "brotli", "brotlicffi", "zstandard",
                                            "h2", "socksio", "trio", "click"}
    for site in (_RESTART_ROOT / ".venv" / "lib").glob("python*/site-packages"):
        with contextlib.suppress(OSError):
            names.update(re.split(r"[.-]", p.name, maxsplit=1)[0] for p in site.iterdir())
    return frozenset(names)


def _nothing_to_import(here: Path) -> bool:
    """В каталоге нет модуля или пакета, который `python -m` взял бы первым
    вместо нужного заявке (sys.path начинается с каталога запуска)."""
    shadowable = _shadowable_names()
    with os.scandir(here) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith("claude_tg_bot"):
                return False
            if name.endswith(_IMPORTABLE_SUFFIXES):
                module = name.split(".", 1)[0]
            elif entry.is_dir() and any(init.endswith(_IMPORTABLE_SUFFIXES) for init in
                                        glob.glob(glob.escape(entry.path) + "/__init__.*")):
                module = name
            else:
                continue
            if module in shadowable:
                return False
    return True


def _restart_request_ok(command: str, cwd: str | None) -> bool:
    """Команда целиком это заявка на перезапуск бота в допустимой форме."""
    if "\r" in command or "\n" in command:
        return False
    if _RESTART_CD_RE.fullmatch(command):
        return True
    rel = bool(_RESTART_REL_RE.fullmatch(command))
    if not cwd or not (rel or _RESTART_ABS_RE.fullmatch(command)):
        return False
    try:
        here = Path(cwd).resolve()
        if here == _RESTART_ROOT.resolve():
            return True
        return not rel and _nothing_to_import(here)
    except (OSError, ValueError):
        return False


# Запись памяти Свода из рана с человеком в чате. Тело записи бот кладёт
# файлом в state/memory-drafts/ (запись туда идёт по whitelist путей), а
# здесь пропускается только `memory remember --file <черновик>`: тело через
# `<<'EOF'` и `<(...)` разбор сегментов не принимает, и это к лучшему.
# Область global и манифест заметки не получают и идут обычным allow:default
# (с 04.11.2026 карточка только на отправку людям).
_MEMORY_WRITE_RE = re.compile(
    rf"(?:MEMORY_CONFIG_DIR={_MEMORY_CONFIG_RE} )?{_HOME_RE}/src/svod/engine/bin/memory remember(?: .*)?"
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
    return len(parts) >= 2 and parts[0].endswith("/src/svod/engine/bin/memory") and parts[1] == "remember"


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


def memory_write_auto_allowed(command: str, drafts: Path) -> list[dict[str, str]] | None:
    """Команда с `memory remember`, о которой бот пишет «💾 подаю в память»:
    каждый вызов remember подаёт черновик из `drafts` в personal или
    clients/<имя>. Прочие сегменты команды с 04.11.2026 не проверяются: без
    карточки идёт всё, кроме отправки людям. Возвращает разобранные вызовы
    для уведомления, иначе None (тогда обычный allow:default)."""
    cmd = command.strip()
    if not cmd:
        return None
    segments = _scan_bash_segments(_BASH_SAFE_REDIRECT_RE.sub("", cmd))
    if segments is None:
        return None
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


AGENT_MODELS = frozenset({"haiku", "sonnet", "opus"})
AGENT_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def origin_chat_key(chat_id: int, thread_id: int | None) -> str:
    return f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)


def _agent_role_refusal(cfg: Config, chat_key: str, tool_input: dict[str, Any]) -> str | None:
    """В чатах AGENT_ROLE_CHAT_KEYS (Домовой) субагент только с явной ролью
    цены: именованный профиль (scout, worker: модель и усилие в его файле)
    или general-purpose с допустимыми model и effort. Ответвление сессии
    (fork) отказ: оно копирует всю сессию и стоило 74% поиска (разбор
    22.10.2026); словами правило не работало, 29 из 47 вызовов без модели."""
    keys = getattr(cfg, "agent_role_chat_keys", frozenset())
    if chat_key not in keys:
        return None
    kind = str(tool_input.get("subagent_type") or "general-purpose").strip()
    hint = _("scout для чтения открытых страниц и файлов, worker для дела по готовому плану, "
             "general-purpose только с model (haiku, sonnet, opus) и effort (low, medium, high, xhigh, max)")
    if kind == "fork":
        return _("ответвление сессии (fork) здесь не зовётся: оно копирует всю сессию. Возьми {hint}").format(hint=hint)
    if kind != "general-purpose":
        return None
    model = str(tool_input.get("model") or "").strip().lower()
    effort = str(tool_input.get("effort") or "").strip().lower()
    if model not in AGENT_MODELS or effort not in AGENT_EFFORTS:
        return _("general-purpose без явных model и effort наследует модель и усилие главного рана "
                 "(дорого). Возьми {hint}").format(hint=hint)
    return None


def _guarded_data_refusal(cfg: Config, tool_name: str, tool_input: dict[str, Any],
                          cwd: str | None = None) -> str | None:
    """Отказ для Write и Edit по общим данным (APPROVE_GUARDED_DATA_PATHS).
    Относительный путь берётся от рабочего каталога агента из хука; без него
    сверить нельзя, и вызов идёт дальше обычным порядком. Граница: запись
    через Bash (>>, sed -i) здесь не ловится, это правило промта."""
    guarded = getattr(cfg, "approve_guarded_data_paths", frozenset())
    if not guarded or tool_name not in _FILE_TOOLS_FOR_WHITELIST:
        return None
    target = _target_path(tool_name, tool_input)
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if target is None and isinstance(raw, str) and raw and cwd and Path(cwd).is_absolute():
        target = (Path(cwd) / raw).resolve()
    if target is None or not any(target == g or g in target.parents for g in guarded):
        return None
    hint = getattr(cfg, "approve_guarded_data_hint", "")
    return _("общие данные: этот файл меняется не инструментом {tool}, а командой под замком "
             "синхронизации{hint}").format(tool=tool_name, hint=f": {hint}" if hint else "")


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


_BOT_ROOT = paths.BOT_ROOT


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
    /2mind зовёт Codex без карточки (ревью Codex 14.10.2026).

    Так же `.claude` (настройки с хуками, агенты, команды, плагины проекта
    исполняются при следующем ране) и `.mcp.json` (запуск MCP-сервера), кроме
    `.claude/projects/`: там память, её пишут без карточки (задание guard2,
    11.2026)."""
    parts = target.parts
    if any(part in (".git", ".obsidian", ".codex") for part in parts) or ".mcp.json" in parts:
        return False
    if any(part == ".claude" and parts[i + 1:i + 2] != ("projects",) for i, part in enumerate(parts)):
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


_PLAN_SKILL = "claude_tg_bot.skills.plan"
# Ровно одна форма: путь питона, модуль и текст в одинарных кавычках до конца
# команды. Внутри одинарных кавычек оболочка ничего не раскрывает, перевод
# строки и прочие связки вне кавычек не проходят (ревью Codex 29.10: shlex
# принимал `скилл\nпрограмма` за четыре слова).
_PLAN_CALL_RE = re.compile(
    re.escape(str(_SKILL_PYTHON)) + " -m " + re.escape(_PLAN_SKILL) + r" '([^']*)'"
)


def _plan_request(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str] | None:
    """Запрос карточки плана: ("ok", текст плана) или ("bad", причина отказа).
    None: команда скилл плана не запускает.

    Текст на карточке тот же, что получит скилл, и ничего, кроме скилла, не
    исполняется (ревью Codex 29.10)."""
    if tool_name != "Bash":
        return None
    command = str(tool_input.get("command", ""))
    try:
        words = shlex.split(command, comments=False)
    except ValueError:
        words = []
    # Запуск модуля после снятия кавычек (`skills.pl""an` тоже он); упоминание
    # в поиске или пути файла не запрос.
    runs_module = any(w == "-m" + _PLAN_SKILL or (w == _PLAN_SKILL and i and words[i - 1] == "-m")
                      for i, w in enumerate(words))
    if not runs_module and not (not words and f"-m {_PLAN_SKILL}" in command):
        return None
    m = _PLAN_CALL_RE.fullmatch(command.strip())
    if m is None:
        return ("bad", _("карточку плана проси одной отдельной командой: {python} -m {skill} "
                         "'<текст плана>', текст в одинарных кавычках без одинарной кавычки "
                         "внутри, без других команд").format(python=_SKILL_PYTHON, skill=_PLAN_SKILL))
    text = m.group(1).strip()
    if not text:
        return ("bad", _("план пуст: напиши, что сделаешь, где и чем проверишь"))
    # Карточка не длиннее предела Telegram после экранирования HTML.
    if len(text) > PLAN_TEXT_MAX or len(_esc(text)) > PLAN_TEXT_MAX + 1000:
        return ("bad", _("план длиннее {n} знаков: сократи до главного (что, где, чем проверишь)")
                .format(n=PLAN_TEXT_MAX))
    return ("ok", text)


def _whitelist_hit(tool_name: str, tool_input: dict[str, Any], cfg: Config) -> bool:
    target_path = _target_path(tool_name, tool_input)
    return (target_path is not None and _is_under_any(target_path, cfg.approve_whitelist_paths)
            and _whitelist_target_ok(target_path, cfg.state_dir))


CARD_SEND_TIMEOUT = 30


async def send_approval_request(
    application: Application, cfg: Config, pending: Pending
) -> None:
    # Если run_claude зарегистрировал target — шлём туда (тот же чат/тема, где
    # пользователь начал диалог). Иначе fallback на primary — нужен на случай,
    # если hook прилетел до того, как run_claude записал session_targets, или
    # session_id в hook payload не соответствует ни одной из активных сессий.
    chat_id = pending.target_chat_id if pending.target_chat_id is not None else cfg.primary_chat_id
    thread_id = pending.target_thread_id
    plan = pending.tool_input.get(PLAN_CARD_KEY)
    if isinstance(plan, dict):
        text = (_("📋 <b>План</b>") + "\n\n" + _esc(str(plan.get("text") or "")) + "\n\n<i>"
                + _("✅: остальные шаги этого запуска без карточек. Карточкой остаётся только "
                    "отправка людям.") + "</i>")
    else:
        text = _format_tool_call(pending.tool_name, pending.tool_input, pending.session_id, pending.cwd)
    if pending.origin_key:
        text = _("🛠 <b>Мастерская</b>") + "\n" + text
    try:
        match = outbound.otklik_match(cfg, pending, chat_id, thread_id)
    except Exception:
        log.warning("otklik match for card failed", exc_info=True)
        match = None
    if match is not None:
        note = _("✅ текст совпадает с одобренным откликом {id} «{title}»\n"
                 "Куда в отклике: {channel}\n<b>сверь адресата выше с «Куда»</b>").format(
            id=match["id"], title=_esc_truncate(str(match.get("title")), 100),
            channel=_esc_truncate(str(match.get("channel")), 200))
        if len(text) + len(note) > CARD_TEXT_CAP + 150:
            # Карточка у предела Telegram 4096: пометка короче, номер остаётся.
            note = _("✅ совпадает с одобренным откликом {id}; <b>сверь адресата с «Куда»</b>").format(
                id=match["id"])
        text += "\n\n" + note
    if pending.deadline:
        # Сколько ждать и что будет по истечении: без этого Иван не знал, жив
        # ли ещё вопрос, и узнавал о сроке новым сообщением (ux.md 10).
        mins = max(1, round((pending.deadline - time.time()) / 60))
        text += "\n\n" + _("⌛ жду до {until} ({mins} мин), потом отказ").format(
            until=clock(pending.deadline), mins=mins)
    rows = [[
        {"text": "✅ Approve", "data": f"appr:{pending.approval_id}:allow"},
        {"text": "❌ Deny", "data": f"appr:{pending.approval_id}:deny"},
    ]]
    if isinstance(plan, dict):
        rows = [[
            {"text": _("✅ Делай план"), "data": f"appr:{pending.approval_id}:allow"},
            {"text": _("❌ Не надо"), "data": f"appr:{pending.approval_id}:deny"},
        ]]
    # ⏩ burst-кнопки показываем для всех инструментов (см. _BURST_DENY_TOOLS):
    # окно auto-approve по решению Ивана покрывает любые действия.
    # Отправке от имени Ивана окно не положено: каждая идёт своей карточкой.
    if (pending.tool_name not in _BURST_DENY_TOOLS and not outbound.is_send(pending.tool_name)
            and outbound.MAILSEND_CARD_KEY not in pending.tool_input and not isinstance(plan, dict)
            and not pending.sends):
        rows.append([
            {"text": "⏩ 15m", "data": f"appr:{pending.approval_id}:auto15"},
            {"text": "⏩ 1h", "data": f"appr:{pending.approval_id}:auto60"},
        ])
    if needs_full_view(pending.tool_input, pending.tool_name):
        rows.append([{"text": _("📄 Полностью"), "data": f"apprfull:{pending.approval_id}"}])
    kb = buttons.markup(rows)
    pending.card_keyboard = keyboard_rows(kb)
    # Текст и разметка до ответа Telegram: если ответа нет (таймаут чтения),
    # на ✅ сверка идёт с ними, сущности из этой разметки допустимы
    # (финальное ревью 03.11.2026). Ответ Telegram текст заменяет ниже.
    pending.card_text = card_plain_text(text)
    pending.card_html = text
    pending.card_entities = None
    # PTB сам опускает message_thread_id=None — танец с kwargs не нужен.
    msg = await application.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=kb,
        parse_mode="HTML",
        message_thread_id=thread_id,
        # Умолчание PTB 5 с: карточка доходила, а бот считал отправку неудачной.
        read_timeout=CARD_SEND_TIMEOUT,
        write_timeout=CARD_SEND_TIMEOUT,
    )
    pending.telegram_message_id = msg.message_id
    if isinstance(getattr(msg, "text", None), str) and msg.text:
        pending.card_text = msg.text
        pending.card_entities = entity_rows(getattr(msg, "entities", None))


def card_plain_text(card_html: str) -> str:
    """Текст карточки без разметки, как его покажет Telegram."""
    return html.unescape(re.sub(r"<[^>]+>", "", card_html)).strip()


_BUTTON_KINDS = ("login_url", "switch_inline_query", "switch_inline_query_current_chat",
                 "switch_inline_query_chosen_chat", "callback_game", "pay", "web_app", "copy_text")


def keyboard_rows(markup: Any) -> tuple:
    """Клавиатура для сверки: подпись, данные и адрес каждой кнопки."""
    rows = getattr(markup, "inline_keyboard", None) or ()
    return tuple(tuple((getattr(b, "text", None), getattr(b, "callback_data", None), getattr(b, "url", None),
                        tuple(k for k in _BUTTON_KINDS if getattr(b, k, None) is not None)) for b in row)
                 for row in rows)


def entity_rows(entities: Any) -> tuple:
    """Сущности текста для сверки: вид, место, адрес ссылки и язык кода."""
    return tuple((str(getattr(e.type, "value", e.type)), e.offset, e.length, getattr(e, "url", None),
                  getattr(e, "language", None), getattr(e, "custom_emoji_id", None),
                  getattr(getattr(e, "user", None), "id", None)) for e in (entities or ()))


# Сущности, которые Telegram сам находит в тексте без разметки.
AUTO_ENTITIES = frozenset({"url", "mention", "hashtag", "cashtag", "bot_command", "email", "phone_number"})
_TAG_ENTITIES = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic", "code": "code", "pre": "pre",
                 "a": "text_link", "u": "underline", "ins": "underline", "s": "strikethrough",
                 "strike": "strikethrough", "del": "strikethrough", "blockquote": "blockquote"}


def _norm_space(text: str) -> str:
    return " ".join(text.split())


def card_matches(pending: Pending, message: Any) -> bool:
    """Сообщение из нажатия показывает то, что бот сам отправил: текст (без
    различий в пробелах и переводах строк), клавиатура и сущности текста.
    Ответа Telegram при отправке не было: сущности только те, что дала бы
    разметка карточки, и найденные Telegram сам (спойлер на адресате нет)."""
    shown = getattr(message, "text", None)
    if not pending.card_text or not isinstance(shown, str) or _norm_space(shown) != _norm_space(pending.card_text):
        return False
    if not pending.card_keyboard or keyboard_rows(getattr(message, "reply_markup", None)) != pending.card_keyboard:
        return False
    got = entity_rows(getattr(message, "entities", None))
    if pending.card_entities is not None:
        return got == pending.card_entities
    allowed = AUTO_ENTITIES | {_TAG_ENTITIES[t] for t in re.findall(r"<([a-z]+)", pending.card_html or "")
                               if t in _TAG_ENTITIES}
    return all(e[0] in allowed for e in got)


# bot_data[RUN_NOTES][ключ темы]: строки футера идущего хода темы (ux2, 01.11.2026).
RUN_NOTES = "run_notes"


def add_run_note(application: Application, target_key: str, kind: str, item: str) -> bool:
    """Отметка «после факта» (запись без карточки, подача в память) строкой
    футера хода вместо отдельного сообщения со звуком посреди рана (ux.md 11).
    Ход темы бот открывает в bot_data[RUN_NOTES] на время рана; нет его (ран
    не из чата, мастерская, сессия не узнана), False: зовущий шлёт сообщение."""
    notes = application.bot_data.get(RUN_NOTES)
    bucket = notes.get(target_key) if isinstance(notes, dict) else None
    if bucket is None:
        return False
    bucket.append((kind, item))
    return True


async def _mark_card_timeout(application: Application, pending: Pending, chat_id: int,
                             thread_id: int | None, wait_sec: float) -> None:
    """Срок карточки истёк: итог дописывается в неё той же правкой, что снимает
    кнопки, как решение по нажатию (ux.md 10). Не вышло: кнопки снимаются, итог
    тихим сообщением ответом на карточку."""
    line = _("⌛ без ответа {mins} мин: отказ").format(mins=max(1, round(wait_sec / 60)))
    mid = pending.telegram_message_id
    if mid is not None and await buttons.note_on_card(
            application.bot, pending.card_html, line, f"timeout aid={pending.approval_id}", chat_id, mid):
        return
    try:
        await application.bot.send_message(
            chat_id=chat_id, text=line, message_thread_id=thread_id,
            disable_notification=True,
            reply_to_message_id=mid,
            allow_sending_without_reply=True,
        )
    except Exception:
        log.exception("failed to mark timeout message")


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
# Причина агенту на ❌ и на срок любой карточки хука (проект 03.11.2026, п. 4).
CARD_DENIED_HINT = N_("Иван отказал сам на карточке: не повторяй этот вызов и ✅ не проси, "
                      "спроси, что не так")
CARD_TIMEOUT_HINT = N_("Иван не ответил на карточку за {mins} мин: повтори, только если он попросит")
# Решение нажатия, когда карточка в Telegram (текст, кнопки) не та, что отправил бот.
CARD_CHANGED = "deny:card-changed"
CARD_CHANGED_HINT = N_("карточка в Telegram изменена не ботом (текст или кнопки), ✅ не принят: не правь "
                       "сообщения бота, повтори вызов, если он нужен")


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
    # Вид карточки плана задаёт только бот: поле из входа агента отбрасывается
    # (ревью Codex 29.10, как у outbound.MAILSEND_CARD_KEY).
    tool_input = {k: v for k, v in tool_input.items() if k != PLAN_CARD_KEY}

    application: Application = request.app["application"]
    registry: ApprovalRegistry = request.app["registry"]
    session_targets: dict[str, tuple[int, int | None]] = request.app["session_targets"]
    audit_path = cfg.state_dir / AUDIT_LOG_NAME
    # Сессия и запуск в каждой строке журнала: по ним меряются план и судья (шаг 3).
    run_id = runs.current(session_id)
    _audit = functools.partial(audit.write_event, session_id=session_id or "", run_id=run_id)

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
    # Мастерская: права и окно ⏩ считаются по её теме, а показ (карточки,
    # уведомления, таймаут) в личке, тема заглушена (проект Домового 22.10.2026).
    origin_key = (f"{target_chat_id}:{target_thread_id}" if target_thread_id is not None
                  else str(target_chat_id))
    if getattr(cfg, "workshop_chat_key", "") and origin_key == cfg.workshop_chat_key:
        target_chat_id, target_thread_id = cfg.primary_chat_id, None
    else:
        origin_key = ""
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
            _audit,
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
    # Субагенты (Task, Agent): решение здесь, до логики этапа плана, где
    # неизвестный инструмент получил бы отказ, а бот сам разрешает там
    # субагентов. После проверки чужого текста: его ран их не зовёт (проект
    # Домового 22.10.2026, добавка А; ревью Codex п. 7).
    if tool_name in ("Task", "Agent"):
        # Чат рана: у мастерской адрес показа уже личка, её тема в origin_key.
        role_refusal = _agent_role_refusal(cfg, origin_key or origin_chat_key(target_chat_id, target_thread_id),
                                           tool_input)
        aid = uuid.uuid4().hex[:12]
        log.info("approve %s aid=%s tool=%s target=%s (agent role)",
                 "deny" if role_refusal else "allow", aid, tool_name, target_key)
        await asyncio.to_thread(
            _audit, audit_path, approval_id=aid, tool_name=tool_name, tool_input=tool_input,
            decision="deny:agent-role" if role_refusal else "allow:agent",
        )
        if role_refusal:
            return web.json_response({"decision": "deny", "reason": role_refusal})
        return web.json_response({"decision": "allow", "reason": "agent"})
    # Перезапуск или остановка самого бота из его рана обрывает этот ран и
    # чужие (restart.py): отказ до всех автоаппрувов, с подсказкой про заявку.
    # Запрос карточки плана только показывает текст: слова в нём не команды, и
    # отказы по виду команды к нему не относятся (_plan_request проверил форму).
    plan_req = _plan_request(tool_name, tool_input)
    plan_call = plan_req is not None and plan_req[0] == "ok"
    # Заявка в допустимой форме ничего, кроме себя, не запускает: слова
    # `systemctl restart claude-tg-bot` в её причине не повод для отказа (ревью Codex 04.11).
    self_stop = None if plan_call else _self_restart_refusal(tool_name, tool_input)
    if self_stop and tool_name == "Bash" and await asyncio.to_thread(
            _restart_request_ok, str(tool_input.get("command", "")), cwd):
        self_stop = None
    if self_stop:
        aid = uuid.uuid4().hex[:12]
        log.info("approve deny aid=%s tool=%s target=%s (self-restart)", aid, tool_name, target_key)
        await asyncio.to_thread(
            _audit,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:self-restart",
        )
        return web.json_response({"decision": "deny", "reason": self_stop})
    # Общие данные (журнал и таблицы Домового) пишет только команда под
    # замком синхронизации: Write и Edit по ним отказ до всех автоаппрувов,
    # иначе правка пропадала при rebase или перетирала соседнюю сессию.
    guarded = None if plan_call else _guarded_data_refusal(cfg, tool_name, tool_input, cwd)
    if guarded:
        aid = uuid.uuid4().hex[:12]
        log.info("approve deny aid=%s tool=%s target=%s (guarded data)", aid, tool_name, target_key)
        await asyncio.to_thread(
            _audit, audit_path, approval_id=aid, tool_name=tool_name,
            tool_input=tool_input, decision="deny:guarded-data",
        )
        return web.json_response({"decision": "deny", "reason": guarded})
    # Отправка от имени Ивана мимо всех автоаппрувов ниже: решает outbound.decide.
    sending = outbound.is_send(tool_name)
    exec_sids: set[str] = application.bot_data.get("task_exec_sids") or set()
    # Запрос карточки плана: раньше всех автоаппрувов, иначе вызов
    # скилла прошёл бы без карточки (_SKILL_CALL_RE, окно ⏩). Сам вызов прав
    # не даёт; где план не положен, отказ с пояснением (ревью Codex 29.10).
    plan_text = ""
    if plan_req is not None:
        why_not = ""
        if plan_req[0] == "bad":
            why_not = plan_req[1]
        elif not cfg.plan_cards:
            why_not = _("план не нужен: шаги идут без карточек, карточка только на отправку "
                        "людям. Делай, что просил Иван")
        elif not run_id or target is None or is_unattended:
            why_not = _("карточка плана здесь не выдаётся (фоновый ран, этап плана или режим "
                        "выключен): действуй по обычным правилам, карточка только на отправку людям")
        elif (session_id in exec_sids
              or _chat_auto_allowed(target, target_key, cfg.approve_auto_chat_keys)):
            why_not = _("план не нужен: в этом чате действия уже разрешены")
        if why_not:
            aid = uuid.uuid4().hex[:12]
            log.info("approve deny aid=%s tool=Bash (plan request: %s)", aid, why_not[:80])
            await asyncio.to_thread(
                _audit, audit_path, approval_id=aid, tool_name=tool_name,
                tool_input=tool_input, decision="deny:plan-request",
            )
            return web.json_response({"decision": "deny", "reason": why_not})
        plan_text = plan_req[1]
    # Письмо модулем mailsend: карточка отправки, как у Gmail MCP
    # (outbound.MAILSEND_CARD_KEY). Снимок письма считает только бот, поле из входа
    # агента отбрасывается.
    mail_kind, mail_info = "", None
    if tool_name == "Bash":
        tool_input = {k: v for k, v in tool_input.items() if k != outbound.MAILSEND_CARD_KEY}
        if not plan_call:
            mail_kind, mail_info = await asyncio.to_thread(
                outbound.mailsend_check, str(tool_input.get("command", "")), cwd)
        if mail_kind in ("card", "mention", "deny"):
            sending = True
    # Карточка только на отправку людям (решение Ивана 04.11.2026,
    # docs/design-2026-11-04-system-boundaries.md): удаление, push, сервер
    # заказчика, вложенный агент, правка защиты и секрет без карточки; причина
    # отправки идёт карточкой мимо всех автоаппрувов ниже. Чужой текст,
    # перезапуск бота и общие данные решены выше и сюда не доходят. С
    # 04.11.2026 замка «только чтение» нет нигде: ни в обдумывании задачи, ни в
    # /plan (Write и Edit там не выполняет сам CLI), ни в проверке «позже».
    danger_why: set[str] = set()
    if not sending and not plan_text:
        danger_why = await asyncio.to_thread(
            serious.send_reasons, tool_name, tool_input, cwd,
            telegram_port=getattr(cfg, "telegram_mcp_port", 8770))
    # Отправка людям мимо карточки отправки (сервер Telegram на loopback,
    # внешний сервис) из фонового рана: отказ сразу, карточка ждала бы без
    # человека весь срок (решение Ивана 03.11.2026).
    outward = serious.sends(danger_why)
    if is_unattended and outward:
        aid = uuid.uuid4().hex[:12]
        log.info("approve deny aid=%s tool=%s target=%s (deny:outbound-unattended)", aid, tool_name, target_key)
        await asyncio.to_thread(_audit, audit_path, approval_id=aid, tool_name=tool_name,
                                tool_input=tool_input, decision="deny:outbound-unattended",
                                why=sorted(danger_why))
        return web.json_response({"decision": "deny", "reason": _(outbound.UNATTENDED_HINT)})
    # Отправка от имени Ивана: себе без карточки, отказ с причиной (пересылка,
    # письмо не уйдёт, фоновый ран, путь файла) или карточка мимо всех
    # автоаппрувов ниже (outbound.decide).
    if sending:
        verdict = await outbound.decide(tool_name, tool_input, cfg=cfg, unattended=is_unattended,
                                        mail_kind=mail_kind, mail_info=mail_info)
        if verdict.decision != "card":
            aid = uuid.uuid4().hex[:12]
            log.info("approve %s aid=%s tool=%s target=%s (%s)",
                     verdict.decision, aid, tool_name, target_key, verdict.audit)
            await asyncio.to_thread(_audit, audit_path, approval_id=aid, tool_name=tool_name,
                                    tool_input=tool_input, decision=verdict.audit)
            return web.json_response({"decision": verdict.decision, "reason": verdict.reason})
    with_human = target is not None and not is_unattended
    if (not sending and not plan_text and not danger_why
            # Запись в whitelist идёт своей веткой ниже, с заметкой «✍️» в футере хода.
            and not (with_human and _whitelist_hit(tool_name, tool_input, cfg))
            # Запись в память идёт своей веткой ниже, с уведомлением «💾 подаю в память».
            and not (tool_name == "Bash" and not is_unattended and memory_write_auto_allowed(
                str(tool_input.get("command", "")), cfg.state_dir / MEMORY_DRAFTS_DIRNAME))):
        aid = uuid.uuid4().hex[:12]
        log.info("approve auto-allow aid=%s tool=%s target=%s (default)", aid, tool_name, target_key)
        await asyncio.to_thread(_audit, audit_path, approval_id=aid, tool_name=tool_name,
                                tool_input=tool_input, decision="allow:default")
        return web.json_response({"decision": "allow", "reason": "default"})
    straight = bool(danger_why)
    # Чат с автоаппрувом по умолчанию (APPROVE_AUTO_CHAT_KEYS): без карточек
    # вообще, включая unattended-раны. Только для зарегистрированной сессии
    # (target известен): primary-fallback чужой сессии сюда не попадает.
    if not sending and not straight and _chat_auto_allowed(
            target, target_key, cfg.approve_auto_chat_keys):
        aid = uuid.uuid4().hex[:12]
        log.info("approve auto-allow aid=%s tool=%s target=%s (chat-auto)", aid, tool_name, target_key)
        await asyncio.to_thread(
            _audit,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="allow:chat-auto",
        )
        return web.json_response({"decision": "allow", "reason": "chat:auto"})
    # Ран выполнения задачи после «Делай» Ивана: права выдаются этому рану, а
    # не теме (bot.py ставит и снимает метку по sid). Отправка от
    # имени Ивана и перезапуск бота идут своими путями выше и ниже (ревью
    # Codex 15.10: опасный шаг получает свою карточку, остальное идёт).
    if not sending and not straight and target is not None and session_id and session_id in exec_sids:
        aid = uuid.uuid4().hex[:12]
        log.info("approve auto-allow aid=%s tool=%s target=%s (task-exec)", aid, tool_name, target_key)
        await asyncio.to_thread(
            _audit,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="allow:task-exec",
        )
        return web.json_response({"decision": "allow", "reason": "task:exec"})

    burst_eligible = (
        target is not None and not is_unattended and tool_name not in _BURST_DENY_TOOLS
        and not sending
        and not plan_text
        # Отправку людям окно не пропускает.
        and not outward
    )
    if burst_eligible and time.time() < burst_until:
        aid = uuid.uuid4().hex[:12]
        log.info(
            "approve auto-allow aid=%s tool=%s target=%s (burst until=%.0f)",
            aid, tool_name, target_key, burst_until,
        )
        await asyncio.to_thread(
            _audit,
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
    whitelist = () if is_unattended or straight else cfg.approve_whitelist_paths
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
                _audit,
                audit_path,
                approval_id=aid,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="allow:whitelist",
            )
            short = str(target_path)
            if (target is not None and not origin_key and add_run_note(
                    application, target_key, "auto",
                    f"{tool_name} {short if len(short) <= 60 else '…' + short[-60:]}")):
                return web.json_response({"decision": "allow", "reason": "whitelist"})
            try:
                if len(short) > 100:
                    short = "…" + short[-100:]
                await application.bot.send_message(
                    chat_id=target_chat_id,
                    text=_("✍️ без карточки: <b>{tool}</b> <code>{path}</code>").format(
                        tool=_esc(tool_name), path=_esc(short)),
                    parse_mode="HTML",
                    message_thread_id=target_thread_id,
                )
            except Exception:
                log.exception("failed to send whitelist auto-approve notification")
            return web.json_response({"decision": "allow", "reason": "whitelist"})

    # Запись в память: без карточки, с уведомлением «💾 подаю в память».
    if tool_name == "Bash" and not is_unattended and not sending and not plan_text and not straight:
        cmd = str(tool_input.get("command", ""))
        calls = memory_write_auto_allowed(cmd, cfg.state_dir / MEMORY_DRAFTS_DIRNAME)
        if calls:
            aid = uuid.uuid4().hex[:12]
            log.info("approve auto-allow aid=%s tool=Bash cmd=%s (memory-write)", aid, cmd[:80])
            await asyncio.to_thread(
                _audit,
                audit_path,
                approval_id=aid,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="allow:memory-write",
            )
            saved = [c for c in calls if not c["dry_run"]]
            if saved and target is not None and not origin_key:
                # Строкой футера хода, если он идёт (ux.md 11).
                pending_names = [f"{c['scope']} · {c['name'] or '?'}" for c in saved]
                if add_run_note(application, target_key, "memory", pending_names[0]):
                    for name in pending_names[1:]:
                        add_run_note(application, target_key, "memory", name)
                    saved = []
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

    # Плановый ран скилла карточку не получает никогда: его сессию бот не знает
    # и ответа никто не ждёт. Сюда он доходит только с причиной карточки
    # (публикация, письмо): отказ сразу.
    if scheduled:
        aid = uuid.uuid4().hex[:12]
        log.info("approve auto-deny aid=%s tool=%s (scheduled, mode=%s)", aid, tool_name, permission_mode or "?")
        await asyncio.to_thread(
            _audit,
            audit_path,
            approval_id=aid,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:scheduled",
        )
        return web.json_response({"decision": "deny", "reason": "scheduled run: tool not pre-allowed"})

    card_input = tool_input
    if mail_kind == "card":
        card_input = {**tool_input, outbound.MAILSEND_CARD_KEY: mail_info}
    elif mail_kind == "mention":
        card_input = {**tool_input, outbound.MAILSEND_CARD_KEY: {"warning": True}}
    if plan_text:
        card_input = {**tool_input, PLAN_CARD_KEY: {"text": plan_text}}
    pending = registry.create(
        tool_name,
        card_input,
        session_id=session_id,
        target_chat_id=target_chat_id,
        target_thread_id=target_thread_id,
        cwd=cwd,
        origin_key=origin_key,
        run_id=run_id,
    )
    pending.sends = outward
    log.info("approve request aid=%s tool=%s sid=%s", pending.approval_id, tool_name, (session_id or "?")[:8])
    # 2-3 попытки: секундный лаг Telegram/502 не должен молча превращаться в
    # deny — Claude обойдёт «отклонённый» шаг, а пользователь этого не увидит.
    # Fail-closed сохраняется: deny только после исчерпания попыток.
    # Таймаут чтения значит, что запрос ушёл, а ответ Telegram не дождались:
    # карточка обычно дошла. Повтор давал дубль, три таймаута подряд давали
    # отказ по карточке, которую Иван видит (лог 22-29.10.2026: все TimedOut
    # такие). Поэтому ждём нажатия, как после удачной отправки. Карточке
    # отправки от имени Ивана нужен номер её сообщения (нажатие сверяется с
    # ним), ей прежний повтор.
    needs_message_id = (outbound.is_send(tool_name) or outbound.MAILSEND_CARD_KEY in card_input
                        or PLAN_CARD_KEY in card_input)
    send_started = time.monotonic()
    # getattr: в тестах cfg без срока, карточка тогда его не называет.
    wait_sec = getattr(cfg, "approve_timeout_sec", 0)
    if wait_sec:
        pending.deadline = time.time() + wait_sec
    send_ok = False
    for attempt in range(3):
        try:
            await send_approval_request(application, cfg, pending)
            send_ok = True
            break
        except NetworkError as e:
            # PTB зовёт TimedOut и таймауты соединения и пула, где запрос не
            # ушёл: им повтор (ревью Codex 29.10).
            if not needs_message_id and is_unconfirmed(e):
                log.warning("approval card send timed out, waiting for a press anyway aid=%s",
                            pending.approval_id)
                send_ok = True
                break
            log.exception(
                "failed to send approval request (attempt %d/3) aid=%s",
                attempt + 1, pending.approval_id,
            )
            if attempt < 2:
                await asyncio.sleep(3.0)
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
            _audit,
            audit_path,
            approval_id=pending.approval_id,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:telegram-send-failed",
        )
        return web.json_response({"decision": "deny", "reason": "telegram send failed"})

    try:
        # Срок общий с отправкой: хук ждёт весь запрос approve_timeout_sec + 30
        # (hook_approve), а отправка с повторами занимает до полутора минут.
        left = cfg.approve_timeout_sec - (time.monotonic() - send_started)
        decision = await asyncio.wait_for(pending.future, timeout=max(left, 1.0))
    except asyncio.TimeoutError:
        registry.expire(pending.approval_id)
        await asyncio.to_thread(
            _audit,
            audit_path,
            approval_id=pending.approval_id,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="deny:timeout",
        )
        log.info("approve timeout aid=%s", pending.approval_id)
        await _mark_card_timeout(application, pending, target_chat_id, target_thread_id,
                                 cfg.approve_timeout_sec)
        # Причина агенту словами: когда повторять (проект 03.11.2026, п. 4).
        return web.json_response({"decision": "deny", "reason": _(CARD_TIMEOUT_HINT).format(
            mins=max(1, round(cfg.approve_timeout_sec / 60)))})

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
    # ✅ на карточке плана: разрешение запуску, который её ждал, если он ещё идёт.
    plan_granted = ""
    if decision == "allow" and plan_text:
        if runs.grant_plan(session_id, pending.run_id, pending.approval_id, plan_text):
            plan_granted = pending.approval_id
        else:
            decision, reason = "deny", _("запуск, просивший план, уже закончился")
    elif plan_text:
        runs.revoke_plan(pending.run_id)
    await asyncio.to_thread(
        _audit,
        audit_path,
        approval_id=pending.approval_id,
        tool_name=tool_name,
        tool_input=tool_input,
        decision="allow:plan-card" if plan_granted else decision,
        plan=plan_granted,
        why=sorted(danger_why),
    )
    log.info("approve resolved aid=%s decision=%s", pending.approval_id, decision)
    if decision == CARD_CHANGED:
        decision, reason = "deny", _(CARD_CHANGED_HINT)
    if decision == "deny" and not reason:
        # ❌ Ивана на карточке: агенту не повторять вызов. Снятие карточки
        # /cancel и концом рана приходит как deny:<причина> и сюда не попадает.
        reason = _(CARD_DENIED_HINT)
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
    # Вопросы рана становятся делами (опыт В1, docs/design-2026-10-26-v1.md):
    # только явное "deal", иначе обычные карточки.
    asks = payload.get("asks")
    if asks is not None and asks not in ("deal", "card"):
        return web.json_response({"error": "asks must be \"deal\" or \"card\""}, status=400)
    with_notice = payload.get("notice", True)
    if not isinstance(with_notice, bool):
        return web.json_response({"error": "notice must be a JSON boolean"}, status=400)
    if not with_notice and not payload.get("wake"):
        # Без карточки и без пробуждения событие не оставило бы в чате ничего.
        return web.json_response({"error": "notice=false requires wake=true"}, status=400)
    key = payload.get("key")
    if key is not None and not (isinstance(key, str) and events.KEY_RE.fullmatch(key)):
        return web.json_response({"error": "key must be 8-128 chars of [A-Za-z0-9._:-]"}, status=400)
    ttl = payload.get("expires_in")
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int)
                            or not events.MIN_TTL_SEC <= ttl <= events.MAX_TTL_SEC):
        return web.json_response({"error": "expires_in must be int seconds 60..86400"}, status=400)
    # Абсолютный срок (Unix-время): отправитель знает конец своего окна, а
    # повторы доставки сдвигали бы относительный (ревью Codex 22.10.2026).
    expires_at = payload.get("expires_at")
    if expires_at is not None:
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) \
                or expires_at > time.time() + events.MAX_TTL_SEC:
            return web.json_response({"error": "expires_at must be unix time within 24h"}, status=400)
        if expires_at <= time.time():
            return web.json_response({"error": "event expired before delivery"}, status=410)

    application: Application = request.app["application"]
    target_key = f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)
    if (application.bot_data.get("draining") or application.bot_data.get("stopping")
            or target_key in (application.bot_data.get("draining_keys") or ())):
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
            with_notice, key, ttl, events.event_dir(cfg.state_dir), expires_at)
        return response
    finally:
        if not handed_off:
            application.bot_data["admitted_events"] -= 1


async def _handle_event_admitted(
    application: Application, payload: dict, source: str, title: str, text: str,
    chat_id: int, thread_id: int | None, fresh: bool, untrusted: bool, silent: bool,
    with_notice: bool = True, key: str | None = None, ttl: int | None = None,
    store: Path | None = None, expires_at: float | None = None,
) -> tuple[web.Response, bool]:
    """Уведомление и пробуждение принятого события. Второе значение: учёт
    принятого события передан задаче пробуждения (она снимет его сама).
    Пробуждение до ответа ложится на диск (events.py): рестарт между ответом
    и стартом рана его не теряет, повтор с тем же ключом не будит второй раз."""
    notice = f"🔔 <b>{_esc(source)}</b>" + (f": {_esc(title)}" if title else "")
    if text:
        notice += f"\n{_esc_truncate(text, TRUNCATE_PAYLOAD)}"
    wake = application.bot_data.get("event_wake") if payload.get("wake") else None
    # Проверено в handle_event; чужой текст делами не становится.
    deal_asks = payload.get("asks") == "deal" and not untrusted
    prompt = str(payload.get("prompt") or "").strip() or (
        _("Внешнее событие от «{source}»: {title}\n\n{text}\n\n"
          "Сделай первичный разбор: что это, серьёзно ли, что проверить. "
          "Ничего не мутируй без approve.").format(source=source, title=title, text=text)
    )
    event_id = None
    sends_notice = with_notice or wake is None
    if wake is not None and store is not None:
        rec = events.new_record(key=key, chat_id=chat_id, thread_id=thread_id, prompt=prompt, fresh=fresh,
                                untrusted=untrusted, silent=silent, source=source, title=title, ttl=ttl,
                                deal_asks=deal_asks,
                                expires_at=expires_at, receiving=sends_notice,
                                boot=application.bot_data.get("boot_id"))
        try:
            reserved = events.reserve(store, rec)
            old = None if reserved else events.load(store, rec["id"])
        except OSError:
            # Без файла 200 обещал бы то, чего рестарт не переживёт: пусть
            # отправитель повторит (ревью Codex 22.10.2026).
            log.exception("event store failed source=%s", source)
            return web.json_response({"error": "event store failed, retry later"}, status=503), False
        if not reserved:
            if old is not None and old.get("fp") != rec["fp"]:
                log.warning("event key reused for another event: source=%s", source)
                return web.json_response({"error": "key already used for another event"}, status=409), False
            if old is not None and old.get("status") == "receiving":
                # Первый такой же запрос ещё ждёт Telegram: ответ ему решит,
                # принято ли событие; повтор пусть придёт позже.
                return web.json_response({"error": "same event is being accepted, retry later"},
                                         status=503), False
            log.info("event duplicate by key, not woken again: source=%s", source)
            return web.json_response({"ok": True, "duplicate": True}), False
        event_id = rec["id"]

    sent = None
    # Без карточки только когда пробуждение точно будет: при недоступной
    # фабрике ниже ответ 202 и рана нет, карточка остаётся единственным следом.
    if sends_notice:
        try:
            sent = await application.bot.send_message(
                chat_id=chat_id, text=notice, parse_mode="HTML", message_thread_id=thread_id,
                disable_notification=silent,
            )
        except asyncio.CancelledError:
            # Клиент оборвал запрос: ответа 200 не было, ключ свободен.
            if event_id is not None:
                with contextlib.suppress(OSError):
                    events.drop(store, event_id)
            raise
        except Exception as e:
            if not is_unconfirmed(e):
                log.exception("event notice failed source=%s", source)
                if event_id is not None:
                    # Не принято: повтор с тем же ключом должен пройти.
                    with contextlib.suppress(OSError):
                        events.drop(store, event_id)
                return web.json_response({"error": "telegram send failed"}, status=502), False
            # Уведомление обычно дошло: 502 дал бы повтор отправителя и второе
            # уведомление (ревью Codex 05.11.2026).
            log.warning("event notice unconfirmed by Telegram, accepted source=%s", source)
        if event_id is not None:
            try:
                if events.update(store, event_id, status="accepted") is None:
                    raise OSError("event record unreadable")
            except OSError:
                # Запись осталась бы «принимается» и держала повторы на 503.
                log.exception("event store failed source=%s", source)
                with contextlib.suppress(OSError):
                    events.drop(store, event_id)
                return web.json_response({"error": "event store failed, retry later"}, status=503), False

    if payload.get("wake"):
        # Ленивая привязка: bot.py кладёт сюда coroutine-фабрику при старте,
        # чтобы approval.py не импортировал bot (цикл).
        if wake is None:
            log.error("event wake requested, но фабрика не установлена")
            return web.json_response(
                {"ok": True, "wake": False, "reason": "wake unavailable"}, status=202
            ), False
        # Трекаем задачу: голый create_task не дожидается shutdown и теряет
        # ссылку (сборщик может убить её на середине).
        tasks: set[asyncio.Task] = application.bot_data.setdefault("event_wake_tasks", set())
        task = asyncio.create_task(
            wake(chat_id, thread_id, prompt, fresh, untrusted=untrusted, silent=silent, event_id=event_id,
                 deal_asks=deal_asks))
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

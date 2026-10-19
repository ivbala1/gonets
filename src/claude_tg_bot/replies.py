"""Доставка ответа Claude в Telegram: чанкование, длинный текст файлом,
SEND_FILE-маркеры, cost-футер.

Выделено из bot.py: чистые функции без завязки на состояние обработчиков —
модуль «как доставить ответ», bot.py остаётся про роутинг и жизненный цикл.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from telegram import MessageEntity, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import ContextTypes

from .claude_runner import ClaudeResult
from .config import Config
from .i18n import _

log = logging.getLogger(__name__)


# Повтор отправки ответа при сбое соединения с Telegram. Только ошибки, при
# которых запрос точно не ушёл (соединение или TLS не установлены, пул занят),
# поэтому дубля не будет. Тайм-аут чтения не повторяем: сообщение могло дойти.
# Случай 13.10.2026: ConnectError на TLS, готовый ответ Claude потерян.
_SEND_RETRY_DELAYS = (2.0, 5.0)
_NOT_SENT_CAUSES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# 429 (RetryAfter): Telegram запрос не выполнил, пережидаем его паузу и шлём
# снова, дубля нет. Случай 14.10.2026: две темы задач завелись подряд,
# группа упёрлась в лимит, итоговый план с кнопками потерялся.
_FLOOD_RETRIES = 2
_FLOOD_WAIT_MAX = 60.0


async def _send_retrying(send: Callable[[], Awaitable[Any]]) -> Any:
    delays = iter(_SEND_RETRY_DELAYS)
    floods = 0
    while True:
        try:
            return await send()
        except RetryAfter as e:
            ra = e.retry_after
            wait = (ra.total_seconds() if isinstance(ra, timedelta) else float(ra or 0)) + 1
            floods += 1
            if floods > _FLOOD_RETRIES or wait > _FLOOD_WAIT_MAX:
                raise
            log.warning("reply send hit flood control, retry in %.0fs", wait)
            await asyncio.sleep(wait)
        except Exception as e:
            delay = next(delays, None) if isinstance(e.__cause__, _NOT_SENT_CAUSES) else None
            if delay is None:
                raise
            log.warning("reply send failed before connect (%s), retry in %.0fs", e, delay)
            await asyncio.sleep(delay)


async def send_reply_text(cfg: Config, update: Update, text: str) -> None:
    """Длинный текст — как файл, короткий — чанками. Splitter по \\n, не посреди слова."""
    msg = update.message
    if not text.strip():
        return
    if len(text) >= cfg.large_reply_as_file_threshold:
        data = text.encode("utf-8")

        def doc() -> Awaitable[Any]:
            buf = io.BytesIO(data)
            buf.name = "claude-reply.txt"
            return msg.reply_document(document=buf, filename="claude-reply.txt", caption=_("{count} chars").format(count=len(text)))

        await _send_retrying(doc)
        return
    for chunk in split_for_telegram(text, cfg.telegram_text_limit):
        plain, entities = render_markup(chunk)
        if not entities:
            await _send_retrying(lambda chunk=chunk: msg.reply_text(chunk))
            continue
        try:
            await _send_retrying(lambda plain=plain, entities=entities: msg.reply_text(plain, entities=entities))
        except BadRequest:
            # Сущности не приняты: ответ важнее шрифтов.
            log.exception("markup entities rejected, sending raw")
            await _send_retrying(lambda chunk=chunk: msg.reply_text(chunk))


async def send_voice_caption(msg, audio: bytes, caption: str) -> None:
    """Голосовой ответ с подписью; разметка в подписи та же, что в тексте."""
    plain, entities = render_markup(caption)
    if not entities:
        await msg.reply_voice(voice=audio, caption=caption)
        return
    try:
        await msg.reply_voice(voice=audio, caption=plain, caption_entities=entities)
    except BadRequest:
        log.exception("markup entities rejected in caption, sending raw")
        await msg.reply_voice(voice=audio, caption=caption)


# Ссылки в голос не идут: «эйч-ти-ти-пи двоеточие слэш слэш» слушать незачем.
# Блок источников (строка «Источники:», «Ссылки», «Sources» и следующие за ней
# пункты и строки со ссылками) вырезается целиком, адреса в тексте убираются,
# у [текст](адрес) остаётся текст.
_URL_RE = re.compile(r"\(?(?:https?://|www\.)[^\s)]+\)?")
_SOURCES_HEAD_RE = re.compile(
    r"^(?:источник|ссылк|литератур|что почитать|sources?\b|references?\b|links?\b)", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•–—]|\d+[.)])\s")


def _sources_head(line: str) -> bool:
    # Заголовок, а не фраза «Источник пишет, что…»: короткая строка с двоеточием
    # или в два-три слова, либо сразу со ссылками.
    bare = re.sub(r"^[\W_]+", "", line).strip()
    if not _SOURCES_HEAD_RE.match(bare):
        return False
    if _URL_RE.search(bare):
        return True
    return len(bare) <= 40 and (bare.rstrip("*_ ").endswith(":") or len(re.findall(r"\w+", bare)) <= 3)


def voice_text(reply: str) -> tuple[str, bool]:
    """Текст для синтеза голоса: без блока источников и адресов. Второе
    значение True, если что-то вырезано: тогда ссылки нужно прислать текстом."""
    kept: list[str] = []
    dropped = False
    in_sources = False
    for line in reply.split("\n"):
        if _sources_head(line):
            in_sources = dropped = True
            continue
        if in_sources:
            if not line.strip() or _LIST_ITEM_RE.match(line) or _URL_RE.search(line):
                continue
            in_sources = False
        if _URL_RE.search(line):
            # Адреса внутри [текст](адрес) разметка уберёт сама, отдельно их не трогаем.
            bare = _INLINE_RE.sub(lambda m: m.group("ltext") if m.group("url") else m.group(0), line)
            stripped = _URL_RE.sub("", bare)
            if stripped != bare:
                dropped = True
                line = re.sub(r"[ \t]{2,}", " ", re.sub(r"\s+([,.;:!?])", r"\1", stripped)).rstrip()
                if not re.sub(r"[\W_]+", "", line):
                    continue
            elif bare != line:
                dropped = True
        kept.append(line)
    return render_markup("\n".join(kept).strip())[0], dropped


# Разметка ответа в шрифты Telegram. Задаётся сущностями, а не parse_mode:
# Telegram не разбирает текст сам, поэтому непарная звёздочка или угловая
# скобка не роняет отправку, а просто остаётся в тексте как есть.
#   ```lang\n...```  блок кода          `x`        моноширинный, копируется тапом
#   **x** жирный     *x* или _x_ курсив  __x__ подчёркнутый  ~~x~~ зачёркнутый
#   ||x|| спойлер    [текст](https://…) ссылка
#   строки с «> » цитата; «# Заголовок» жирным
# Курсив только на границе слова: *111#, snake_case и 2*3*4 остаются текстом.
_FENCE_BLOCK_RE = re.compile(r"```(?:([\w+#.-]+)?\n)?(.*?)```", re.DOTALL)
_QUOTE_LINE_RE = re.compile(r"^>(?: |$)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(\S.*)$")


def _span(open_: str, close: str, name: str, stop: str) -> str:
    # Содержимое не пустое, без перевода строки, не начинается и не кончается пробелом.
    return rf"{open_}(?P<{name}>[^\s{stop}](?:[^\n]*?[^\s{stop}])?){close}"


_INLINE_RE = re.compile("|".join([
    r"`(?P<code>[^`\n]*[^`\s][^`\n]*)`",
    r"\[(?P<ltext>[^\]\n]+)\]\((?P<url>(?:https?|tg)://[^\s)]+)\)",
    _span(r"\*\*", r"\*\*", "bold", r"*"),
    _span(r"(?<!\w)__", r"__(?!\w)", "under", r"_"),
    _span(r"~~", r"~~", "strike", r"~"),
    _span(r"\|\|", r"\|\|", "spoiler", r"|"),
    r"(?<![\w*])\*(?P<ital>[^\s*](?:[^\n*]*?[^\s*])?)\*(?![\w*])",
    r"(?<![\w_])_(?P<ital2>[^\s_](?:[^\n_]*?[^\s_])?)_(?![\w_])",
]))

_STYLE_GROUPS = (
    ("bold", MessageEntity.BOLD),
    ("under", MessageEntity.UNDERLINE),
    ("strike", MessageEntity.STRIKETHROUGH),
    ("spoiler", MessageEntity.SPOILER),
    ("ital", MessageEntity.ITALIC),
    ("ital2", MessageEntity.ITALIC),
)


def _utf16_len(s: str) -> int:
    # Смещения сущностей Telegram считает в UTF-16 (эмодзи занимают две единицы).
    return len(s.encode("utf-16-le")) // 2


class _Out:
    """Собирает чистый текст и сущности со смещениями в UTF-16."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.entities: list[MessageEntity] = []
        self.offset = 0

    def text(self, s: str) -> None:
        self.parts.append(s)
        self.offset += _utf16_len(s)

    def wrap(self, kind: str, fill, **kw) -> None:
        start = self.offset
        fill()
        if self.offset > start:
            self.entities.append(MessageEntity(type=kind, offset=start, length=self.offset - start, **kw))


def _inline(s: str, out: _Out, nested: bool = False) -> None:
    # Внутри жирного и прочих стилей Telegram не допускает code: там `x`
    # остаётся текстом без кавычек.
    pos = 0
    for m in _INLINE_RE.finditer(s):
        out.text(s[pos:m.start()])
        pos = m.end()
        if m.group("code") is not None:
            if nested:
                out.text(m.group("code"))
            else:
                out.wrap(MessageEntity.CODE, lambda m=m: out.text(m.group("code")))
            continue
        if m.group("url") is not None:
            out.wrap(MessageEntity.TEXT_LINK, lambda m=m: _inline(m.group("ltext"), out, True), url=m.group("url"))
            continue
        for group, kind in _STYLE_GROUPS:
            if m.group(group) is not None:
                out.wrap(kind, lambda m=m, g=group: _inline(m.group(g), out, True))
                break
    out.text(s[pos:])


def _blocks(seg: str, out: _Out) -> None:
    lines = seg.split("\n")
    i = 0
    while i < len(lines):
        if i:
            out.text("\n")
        if _QUOTE_LINE_RE.match(lines[i]):
            j = i
            while j < len(lines) and _QUOTE_LINE_RE.match(lines[j]):
                j += 1

            def fill(quoted=lines[i:j]) -> None:
                for k, line in enumerate(quoted):
                    if k:
                        out.text("\n")
                    _inline(_QUOTE_LINE_RE.sub("", line, count=1), out)

            out.wrap(MessageEntity.BLOCKQUOTE, fill)
            i = j
            continue
        h = _HEADING_RE.match(lines[i])
        if h:
            out.wrap(MessageEntity.BOLD, lambda h=h: _inline(h.group(1), out, True))
        else:
            _inline(lines[i], out)
        i += 1


def render_markup(text: str) -> tuple[str, list[MessageEntity]]:
    """Разметка ответа → (чистый текст, сущности Telegram). Без разметки текст не меняется."""
    out = _Out()
    pos = 0
    for m in _FENCE_BLOCK_RE.finditer(text):
        _blocks(text[pos:m.start()], out)
        body = m.group(2).strip("\n")
        out.wrap(MessageEntity.PRE, lambda body=body: out.text(body), language=m.group(1) or None)
        pos = m.end()
    _blocks(text[pos:], out)
    out.entities.sort(key=lambda e: e.offset)
    return "".join(out.parts), out.entities


# SEND_FILE — маркер, который Claude может вставить в ответ, чтобы бот
# отправил локальный файл пользователю как Telegram-документ. Пример:
#   <<SEND_FILE:/tmp/track.gpx>>
# Маркер вырезается из текста перед отправкой. Безопасность: путь должен
# резолвиться внутри одного из whitelist root'ов (см. send_file_whitelist),
# файл существует, не директория, ≤50 МБ (лимит bot API).
_SEND_FILE_MAX_BYTES = 50 * 1024 * 1024
_SEND_FILE_RE = re.compile(r"<<SEND_FILE:([^>\n]+)>>")


def send_file_whitelist(cfg: Config) -> list[Path]:
    raw = os.environ.get("SEND_FILE_WHITELIST_PATHS", "")
    if raw.strip():
        return [Path(p).expanduser().resolve() for p in raw.split(":") if p.strip()]
    return [Path("/tmp").resolve(), (cfg.state_dir / "outbox").resolve()]


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


# Файлы, которые Claude запрещено читать (permissions.deny в
# claude-hook-settings.json), SEND_FILE тоже не отправляет: маркер шёл мимо
# запрета Read, файл не читается инструментом (ревью 12.10.2026).
_SECRET_DIRS = (".ssh", ".aws")
_SECRET_FILES = (
    Path("~/.claude/.credentials.json"), Path("~/.codex/auth.json"),
    Path("~/.config/gh/hosts.yml"), Path("~/.docker/config.json"),
)


def _is_secret_file(p: Path) -> bool:
    name = p.name
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return True
    if name == ".credentials.json" or any(part in _SECRET_DIRS for part in p.parts):
        return True
    # Ключи и учётные данные вне ~/.ssh: /tmp/id_rsa, backup.key, server.pem,
    # credentials (ревью 11.10.2026).
    low = name.lower()
    if low.startswith(("id_", "credentials")) or low.endswith((".key", ".pem")):
        return True
    return any(p == f.expanduser().resolve() for f in _SECRET_FILES)


def extract_send_file_markers(
    text: str, whitelist: list[Path], refuse_reason: str = "",
) -> tuple[list[Path], str]:
    """Парсит <<SEND_FILE:path>> маркеры. Возвращает (валидные пути, текст без маркеров).

    Невалидные маркеры заменяются на короткое уведомление, в логе пишется причина.
    refuse_reason непустой: ни один файл не отправляется, каждый маркер
    заменяется уведомлением с этой причиной (ран по чужому тексту).
    """
    files: list[Path] = []

    def _validate(raw: str) -> tuple[Path | None, str | None]:
        if refuse_reason:
            return None, refuse_reason
        try:
            p = Path(raw.strip()).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as e:
            return None, f"path unresolvable: {e}"
        if not p.is_file():
            return None, "not a regular file"
        if not any(_is_under(p, root) for root in whitelist):
            return None, "outside SEND_FILE whitelist"
        if _is_secret_file(p):
            return None, _("secret file (Read запрещён в claude-hook-settings.json)")
        try:
            size = p.stat().st_size
        except OSError as e:
            return None, f"stat failed: {e}"
        if size == 0:
            return None, "empty file"
        if size > _SEND_FILE_MAX_BYTES:
            return None, f"too large ({size} bytes, max {_SEND_FILE_MAX_BYTES})"
        return p, None

    def _sub(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        p, err = _validate(raw)
        if p is not None:
            files.append(p)
            return ""
        log.warning("SEND_FILE skipped %s: %s", raw, err)
        return _("⚠️ SEND_FILE({raw}) пропущен: {error}").format(raw=raw, error=err)

    cleaned = _SEND_FILE_RE.sub(_sub, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return files, cleaned


# ASK — маркер уточняющего вопроса с вариантами ответа. Claude вставляет его
# в ответ, бот рисует кнопки, выбор уходит следующим сообщением в ту же
# сессию. Формат:  <<ASK:Вопрос?|Вариант 1|Вариант 2>>
# Почему так, а не инструмент AskUserQuestion: в headless-режиме (`claude -p`,
# на котором работает бот) этого инструмента в наборе НЕТ — проверено по
# system-init событию stream-json, поэтому перехватывать его хуком бесполезно.
_ASK_RE = re.compile(r"<<ASK:([^>\n]+)>>")
_ASK_MAX_OPTIONS = 4
# Вариант целиком уходит следующим ходом в сессию и целиком показывается на
# кнопке. Ограничение нужно, чтобы длинный «хвост» не оказался инструкцией,
# которую пользователь на кнопке не прочитал.
_ASK_MAX_OPTION_LEN = 100
_ASK_MAX_QUESTION_LEN = 300
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def extract_ask_marker(text: str) -> tuple[tuple[str, list[str]] | None, str]:
    """Первый валидный <<ASK:...>> → ((вопрос, варианты), текст без маркера).

    Маркеры внутри ``` ``` ``` игнорируются: Claude может цитировать файл,
    README или свой же формат, и это не должно превращаться в кнопки.
    Невалидные маркеры (нет вариантов, слишком длинные) и второй маркер
    остаются в тексте как есть — пользователь хотя бы прочитает вопрос.
    """
    # Диапазоны кодовых блоков: маркеры внутри них не трогаем.
    fences = [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]

    def _in_fence(pos: int) -> bool:
        return any(start <= pos < end for start, end in fences)

    found: list[tuple[str, list[str]]] = []

    def _sub(m: re.Match[str]) -> str:
        if found or _in_fence(m.start()):
            return m.group(0)
        parts = [p.strip() for p in m.group(1).split("|")]
        question = parts[0] if parts else ""
        options = [p for p in parts[1:] if p]
        if (
            not question
            or len(options) < 2
            or len(question) > _ASK_MAX_QUESTION_LEN
            or any(len(o) > _ASK_MAX_OPTION_LEN for o in options)
        ):
            return m.group(0)
        found.append((question, options[:_ASK_MAX_OPTIONS]))
        return ""

    cleaned = _ASK_RE.sub(_sub, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return (found[0] if found else None), cleaned


async def send_cost_footer(cfg: Config, update: Update, result: ClaudeResult, model_note: str = "") -> None:
    """Короткий футер: цена ответа, сессии и контекст. model_note это
    напоминание о недефолтной модели ветки («🤖 opus ⏳»), пусто на дефолте;
    в теме задачи к нему дописан расход задачи («задача ≈$1.35»).

    Цена ответа это разница накопительной суммы сессии с прошлым ответом
    (claude_runner._answer_cost), контекст это вход последнего запроса
    основного диалога, а не сумма по запросам рана, как до 13.10.2026: по ней
    предупреждение срабатывало почти всегда (ревью Codex). Шлём только при
    расходе по сессии: $0-ошибки, cancel, 404 без футера."""
    if not cfg.show_cost_footer or result.session_cost_usd <= 0:
        return
    if result.cost_usd is not None:
        cost = _("💸 ${cost:.2f} (сессия ${session:.2f})").format(
            cost=result.cost_usd, session=result.session_cost_usd)
    else:
        cost = _("💸 сессия ${session:.2f}").format(session=result.session_cost_usd)
    if result.context_tokens:
        ctx = _("🧠 контекст {tokens}k").format(tokens=result.context_tokens // 1000)
        if result.context_max > result.context_tokens * 1.1:
            ctx += _(" (макс {max}k)").format(max=result.context_max // 1000)
        if result.compacted:
            ctx += _(", сжатий {count}").format(count=result.compacted)
    elif result.compacted:
        ctx = _("🧠 контекст сжат") + (
            _(" (макс {max}k)").format(max=result.context_max // 1000) if result.context_max else "")
    else:
        ctx = ""
    footer = cost + (f" · {ctx}" if ctx else "")
    if model_note:
        footer += f" · {model_note}"
    if cfg.context_warn_tokens > 0 and result.context_tokens >= cfg.context_warn_tokens:
        footer += _("\n⚠️ контекст большой: /reset удешевит и ускорит следующие ответы")
    try:
        await update.message.reply_text(footer)
    except Exception:
        log.exception("cost footer send failed")


async def send_files(update: Update, files: list[Path]) -> None:
    msg = update.message
    for fpath in files:
        try:
            with fpath.open("rb") as fh:
                await msg.reply_document(document=fh, filename=fpath.name)
            log.info("SEND_FILE sent: %s (%d bytes)", fpath, fpath.stat().st_size)
        except Exception:
            log.exception("SEND_FILE failed for %s", fpath)
            try:
                await msg.reply_text(_("⚠️ не удалось отправить файл {name}").format(name=fpath.name))
            except Exception:
                pass


def split_for_telegram(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:  # мало что выиграли — режем жёстко
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out

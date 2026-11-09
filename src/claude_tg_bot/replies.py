"""Доставка ответа Claude в Telegram: чанкование, длинный текст файлом,
SEND_FILE-маркеры, cost-футер.

Выделено из bot.py: чистые функции без завязки на состояние обработчиков —
модуль «как доставить ответ», bot.py остаётся про роутинг и жизненный цикл.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import MessageEntity, ReplyParameters, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from . import jsonstore, paths, reactions, tg_api
from .claude_runner import ClaudeResult
from .config import Config
from .i18n import _

log = logging.getLogger(__name__)


# Повтор отправки ответа при сбое соединения с Telegram. Только ошибки, при
# которых запрос точно не ушёл (соединение или TLS не установлены, пул занят),
# поэтому дубля не будет. Тайм-аут чтения не повторяем: сообщение могло дойти.
# Случай 13.10.2026: ConnectError на TLS, готовый ответ Claude потерян.
# Признаки и паузы общие с прямыми вызовами Bot API (tg_api).
_SEND_RETRY_DELAYS = tg_api.RETRY_DELAYS
_NOT_SENT_CAUSES = tg_api.NOT_SENT
# 429 (RetryAfter): Telegram запрос не выполнил, пережидаем его паузу и шлём
# снова, дубля нет. Случай 14.10.2026: две темы задач завелись подряд,
# группа упёрлась в лимит, итоговый план с кнопками потерялся.
_FLOOD_RETRIES = 2
_FLOOD_WAIT_MAX = 60.0
# Дольше этого отправка попадает в журнал: по нему видно, что медленно,
# Telegram или сеть (голосовые стали упираться в таймаут с 23.10.2026).
_SLOW_SEND_SEC = 3.0


def is_unconfirmed(e: BaseException) -> bool:
    """Запрос ушёл, ответа Telegram не дождались: сообщение обычно доходит,
    иногда минуты спустя (случаи 30-31.10.2026: 1, 5 и 125 с после таймаута).
    Это не отказ, и слать то же самое заново значит слать дубль. ReadError сюда
    не входит: httpcore глушит сбой записи запроса и отдаёт его как ReadError,
    то есть запрос мог не дойти (ревью 05.11.2026)."""
    return isinstance(e, TimedOut) and e.__cause__ is not None and tg_api.is_unconfirmed(e.__cause__)


async def _send_retrying(send: Callable[[], Awaitable[Any]]) -> Any:
    delays = iter(_SEND_RETRY_DELAYS)
    floods = 0
    while True:
        started = time.monotonic()
        try:
            try:
                return await send()
            finally:
                took = time.monotonic() - started
                if took > _SLOW_SEND_SEC:
                    log.info("reply send took %.1fs", took)
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


async def _send_once(send: Callable[[], Awaitable[Any]]) -> Any:
    """Отправка куска ответа: неподтверждённый кусок не обрывает ответ, хвост
    уходит дальше, а вызывающие не шлют уже ушедшее заново."""
    try:
        return await _send_retrying(send)
    except NetworkError as e:
        if not is_unconfirmed(e):
            raise
        log.warning("reply send unconfirmed by Telegram, not repeated")
        return None


async def send_reply_text(cfg: Config, update: Update, text: str, *, reply_markup=None) -> None:
    """Длинный текст — как файл, короткий — чанками. Splitter по \\n, не посреди слова."""
    await send_text_to(cfg, update.message, text, reply_markup=reply_markup)


async def send_text_to(cfg: Config, msg, text: str, head: str = "", *, reply_markup=None) -> None:
    """То же, что send_reply_text, ответом на любое сообщение (кнопка под голосовым).
    head: первая строка каждого куска и начало подписи файла (ярлык дела,
    по нему свайп на любой кусок находит дело, опыт В1). reply_markup: кнопки
    под последним куском."""
    if not text.strip():
        return
    if len(text) >= cfg.large_reply_as_file_threshold:
        data = text.encode("utf-8")

        def doc() -> Awaitable[Any]:
            buf = io.BytesIO(data)
            buf.name = "claude-reply.txt"
            caption = _("{count} chars").format(count=len(text))
            return msg.reply_document(document=buf, filename="claude-reply.txt",
                                      caption=f"{head}\n{caption}" if head else caption,
                                      **({"reply_markup": reply_markup} if reply_markup else {}))

        await _send_once(doc)
        return
    # Первый кусок цитирует настоящее сообщение Ивана (проект Домового 22.10.2026,
    # 1.8): в личке видно, на что ответ. Сообщение удалено: ответ всё равно уходит
    # (allow_sending_without_reply), отказ цитаты: повтор без неё. У пробуждений
    # без сообщения (номер 0) цитаты нет.
    quote = _quote_of(msg)
    # +2: перевод строки и второе слово UTF-16 у 🧵 (Telegram считает так).
    limit = cfg.telegram_text_limit - (len(head) + 2 if head else 0)
    chunks = list(split_for_telegram(text, limit))
    for i, chunk in enumerate(chunks):
        if head:
            chunk = f"{head}\n{chunk}"
        plain, entities = render_markup(chunk)
        markup = {"reply_markup": reply_markup} if reply_markup and i == len(chunks) - 1 else {}
        extra = {"reply_parameters": quote} if quote is not None else {}
        quote = None
        try:
            if not entities:
                _noted(plain, await _send_once(
                    lambda chunk=chunk, extra=extra: msg.reply_text(chunk, **extra, **markup)))
                continue
            try:
                _noted(plain, await _send_once(lambda plain=plain, entities=entities, extra=extra:
                                               msg.reply_text(plain, entities=entities, **extra, **markup)))
            except BadRequest:
                if extra:
                    raise
                # Сущности не приняты: ответ важнее шрифтов.
                log.exception("markup entities rejected, sending raw")
                _noted(plain, await _send_once(lambda chunk=chunk: msg.reply_text(chunk, **markup)))
        except BadRequest:
            if not extra:
                raise
            log.warning("quoted reply rejected, sending without quote", exc_info=True)
            _noted(plain, await _send_once(lambda chunk=chunk: msg.reply_text(chunk, **markup)))


def _noted(text: str, sent: Any) -> None:
    """Ответ модели в журнал для реакций. Только здесь и в notify, а не во всех
    отправках бота: служебные сообщения несут коды входа (auth.py)."""
    reactions.remember(getattr(sent, "chat_id", None), getattr(sent, "message_id", None), text)


def _quote_of(msg) -> ReplyParameters | None:
    mid = getattr(msg, "message_id", 0)
    if not isinstance(mid, int) or mid <= 0 or getattr(getattr(msg, "chat", None), "type", "") != "private":
        return None
    return ReplyParameters(message_id=mid, allow_sending_without_reply=True)


async def send_voice_caption(msg, audio: bytes, caption: str, reply_markup=None) -> Any:
    """Голосовой ответ с подписью; разметка в подписи та же, что в тексте."""
    log.info("voice send bytes=%d", len(audio))
    # Случаи 07.11.2026: ответ Telegram на голосовое 100-450 КБ не приходил за
    # общие 30 с, при живой сети; ждём, как файл (_SEND_FILE_TIMEOUT).
    wait = {"read_timeout": _SEND_FILE_TIMEOUT, "write_timeout": _SEND_FILE_TIMEOUT}
    plain, entities = render_markup(caption)
    if not entities:
        return await _send_retrying(lambda: msg.reply_voice(
            voice=audio, caption=caption, reply_markup=reply_markup, **wait))
    try:
        return await _send_retrying(lambda: msg.reply_voice(
            voice=audio, caption=plain, caption_entities=entities, reply_markup=reply_markup, **wait))
    except BadRequest:
        log.exception("markup entities rejected in caption, sending raw")
        return await _send_retrying(lambda: msg.reply_voice(
            voice=audio, caption=caption, reply_markup=reply_markup, **wait))


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


# Длинный голосовой ответ идёт серией голосовых, у каждого полная подпись
# со своим куском текста (подпись у бота до 1024 знаков, Premium не поднимает).
# Telegram сам проигрывает голосовые подряд. Блоки кода в подпись и голос не
# идут: они приходят текстом после серии. Решение Ивана 21.10.2026.
_COUNTER_RESERVE = 12  # «\n\n10/10» и запас на подсчёт знаков
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")


def _rendered_len(text: str) -> int:
    return utf16_len(render_markup(text)[0])


def _pack(pieces: list[str], sep: str, budget: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for p in pieces:
        cand = f"{cur}{sep}{p}" if cur else p
        if cur and _rendered_len(cand) > budget:
            out.append(cur)
            cur = p
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def _split_outside_markup(text: str, sep: re.Pattern) -> list[str]:
    # Делить только вне [текст](адрес), **жирного** и прочей разметки:
    # иначе ссылка распадается на сырые куски (ревью Codex 21.10.2026).
    spans = [m.span() for m in _INLINE_RE.finditer(text)]
    pieces: list[str] = []
    pos = 0
    for m in sep.finditer(text):
        if any(a < m.start() < b for a, b in spans):
            continue
        pieces.append(text[pos:m.start()])
        pos = m.end()
    pieces.append(text[pos:])
    return pieces


def _hard_cut(text: str, budget: int) -> list[str]:
    # Последний случай, кусок без пробелов: режем по длине в UTF-16, как
    # считает Telegram (эмодзи по две единицы).
    # Счёт по исходному тексту: разметка при отрисовке знаки только убирает.
    parts: list[str] = []
    cur, used = "", 0
    for ch in text:
        size = utf16_len(ch)
        if cur and used + size > budget:
            parts.append(cur)
            cur, used = "", 0
        cur += ch
        used += size
    if cur:
        parts.append(cur)
    return parts if _rendered_len(text) > budget else [text]


def _split_long_line(line: str, budget: int) -> list[str]:
    # Строка длиннее подписи: по предложениям, затем по словам, в крайнем
    # случае жёстко.
    parts: list[str] = []
    for sentence in _pack(_split_outside_markup(line, _SENTENCE_END_RE), " ", budget):
        if _rendered_len(sentence) <= budget:
            parts.append(sentence)
            continue
        for words in _pack(_split_outside_markup(sentence, re.compile(" ")), " ", budget):
            parts.extend(_hard_cut(words, budget))
    return parts


def plan_voice_series(reply: str, caption_limit: int) -> tuple[list[str], list[str]]:
    """Ответ → (куски для подписей голосовых, блоки кода текстом после серии).
    Режется по строкам: разметка у бота внутри строки, перенос её не рвёт.
    Кусок заканчивается на пустой строке, если она есть во второй половине."""
    fences = [m.group(0) for m in _FENCE_BLOCK_RE.finditer(reply)]
    body = re.sub(r"\n{3,}", "\n\n", _FENCE_BLOCK_RE.sub("", reply)).strip()
    # Нижняя граница: при крошечном TELEGRAM_CAPTION_LIMIT бюджет не ноль.
    budget = max(caption_limit - _COUNTER_RESERVE, 1)
    lines: list[str] = []
    for line in body.split("\n"):
        lines.extend([line] if _rendered_len(line) <= budget else _split_long_line(line, budget))
    chunks: list[str] = []
    cur: list[str] = []
    for line in lines:
        while cur and _rendered_len("\n".join(cur + [line])) > budget:
            cut = len(cur)
            blanks = [i for i, x in enumerate(cur) if not x.strip() and i > 0 and i >= len(cur) // 2]
            if blanks:
                cut = blanks[-1]
            chunks.append("\n".join(cur[:cut]))
            cur = cur[cut:]
            while cur and not cur[0].strip():
                cur.pop(0)
        cur.append(line)
    chunks.append("\n".join(cur))
    return [c.strip("\n") for c in chunks if c.strip()], fences


def with_counter(chunk: str, index: int, total: int) -> str:
    return f"{chunk}\n\n{index}/{total}" if total > 1 else chunk


# Кнопка «📄 Полный текст» под последним голосовым серии: весь ответ одним
# сообщением по нажатию, без автоматического дубля. Текст лежит на диске,
# чтобы кнопка работала и после перезапуска бота; старше 14 дней удаляется.
_FULL_TEXT_DIR = "voice-texts"
_FULL_TEXT_TTL_SEC = 14 * 86400
_FULL_TEXT_TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")


def save_full_text(state_dir: Path, text: str, chat_id: int) -> str:
    folder = state_dir / _FULL_TEXT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in folder.glob("*.txt"):
        with contextlib.suppress(OSError):
            if now - old.stat().st_mtime > _FULL_TEXT_TTL_SEC:
                old.unlink()
    token = secrets.token_hex(8)
    jsonstore.write_text(folder / f"{token}.txt", f"{chat_id}\n{text}")  # 0600: текст ответа
    return token


def load_full_text(state_dir: Path, token: str, chat_id: int) -> str | None:
    """Текст по кнопке; None, если его нет или кнопка нажата в другом чате."""
    if not _FULL_TEXT_TOKEN_RE.match(token):
        return None
    try:
        head, _sep, text = (state_dir / _FULL_TEXT_DIR / f"{token}.txt").read_text(
            encoding="utf-8").partition("\n")
    except OSError:
        return None
    return text if head == str(chat_id) else None


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


def utf16_len(s: str) -> int:
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
        self.offset += utf16_len(s)

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
# файл существует, не директория, не больше предела отправки: 50 МБ у
# облачного Bot API, 2000 МБ у своего сервера (send_file_max_bytes).
_SEND_FILE_MAX_BYTES = 50 * 1024 * 1024
_SEND_FILE_MAX_LOCAL = 2000 * 1024 * 1024


def send_file_max_bytes(cfg: Config) -> int:
    return _SEND_FILE_MAX_LOCAL if tg_api.on_local_server(cfg) else _SEND_FILE_MAX_BYTES


_SEND_FILE_RE = re.compile(r"<<SEND_FILE:([^>\n]+)>>")


def send_file_whitelist(cfg: Config) -> list[Path]:
    """Корни маркера: `SEND_FILE_WHITELIST_PATHS` (через `:`) или по умолчанию
    `/tmp`, `state/outbox` и папка отправки `telegram-send`. Её агент берёт по
    правилу персоны для send_file, и маркер с ней отклонялся (09.11.2026)."""
    raw = os.environ.get("SEND_FILE_WHITELIST_PATHS", "")
    if raw.strip():
        return [Path(p.strip()).expanduser().resolve() for p in raw.split(":") if p.strip()]
    return [Path("/tmp").resolve(), (cfg.state_dir / "outbox").resolve(),
            paths.TELEGRAM_SEND_DIR.resolve()]


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
    max_bytes: int = _SEND_FILE_MAX_BYTES,
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
        if size > max_bytes:
            return None, f"too large ({size} bytes, max {max_bytes})"
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

    Маркеры в коде (блок ```, одиночный `код`, незакрытый блок) игнорируются:
    Claude может цитировать файл, README или свой же формат, и это не должно
    превращаться в кнопки (одиночный код с 08.11.2026).
    Невалидные маркеры (нет вариантов, слишком длинные) и второй маркер
    остаются в тексте как есть — пользователь хотя бы прочитает вопрос.
    """
    # Диапазоны кода: маркеры внутри них не трогаем.
    fences = _code_spans(text)

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


_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Участки кода, где метки не действуют: блоки ```, одиночные `кода` и
    незакрытый блок до конца текста."""
    spans = [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]
    # Одиночные кавычки ищутся вне блоков: иначе ``` склеился бы с соседней.
    outside = _FENCE_RE.sub(lambda f: " " * len(f.group(0)), text)
    spans += [(m.start(), m.end()) for m in _INLINE_CODE_RE.finditer(outside)]
    opened = re.search(r"(?m)^[ \t]*```", outside)
    if opened:
        # Незакрытый блок (оборванный ответ) идёт до конца текста (ревью Codex
        # 04.11.2026); ``` посреди строки текста блоком не считается (ревью Opus).
        spans.append((opened.start(), len(text)))
    return spans


# Метка звука: ран без звука (/event с silent и wake) зовёт владельца со звуком
# одним итоговым ответом, когда уверен, что нужна его реакция (решение Ивана
# 26.10.2026: «пробуждение слушателя не равно пробуждению меня»). Вырезается
# из любого ответа; внутри блока ``` остаётся как есть, как и ASK.
_LOUD_RE = re.compile(r"[ \t]*<<LOUD>>")


def _extract_flag_marker(rx: re.Pattern[str], text: str) -> tuple[bool, str]:
    """Метка-флаг вне кода (блоков ``` и `одиночного`) → (да или нет, текст без
    меток). Метка, упомянутая в `коде` (рассказ о самом боте), не срабатывает."""
    fences = _code_spans(text)
    found = False

    def _sub(m: re.Match[str]) -> str:
        nonlocal found
        if any(start <= m.start() < end for start, end in fences):
            return m.group(0)
        found = True
        return ""

    cleaned = rx.sub(_sub, text)
    if not found:
        return False, text
    return True, cleaned.strip()


def extract_loud_marker(text: str) -> tuple[bool, str]:
    """Есть ли <<LOUD>> вне кодовых блоков → (да или нет, текст без меток)."""
    return _extract_flag_marker(_LOUD_RE, text)


# Метка голоса: модель сама решает, что Иван просит ответ голосом в любой
# форме («ответь голосовым», «скажи вслух», «перешли то же голосом», «voice
# please»), и ставит <<VOICE>>; бот озвучивает этот ответ. Жёсткий список слов
# до 08.11.2026 ловил «почему голосом?» и пропускал «давай аудио». Вырезается
# из любого ответа; внутри блока ``` остаётся как есть, как и LOUD.
_VOICE_RE = re.compile(r"[ \t]*<<VOICE>>")


def extract_voice_marker(text: str) -> tuple[bool, str]:
    """Есть ли <<VOICE>> вне кодовых блоков → (да или нет, текст без меток)."""
    return _extract_flag_marker(_VOICE_RE, text)


# Метка тишины: ран сторожа или слушателя, где от Ивана ничего не нужно, не шлёт
# ничего, строка остаётся в журнале и всплывает утром или вечером (решение Ивана
# 01.11.2026: «сторож молчит, значит нового нет»).
_QUIET_RE = re.compile(r"[ \t]*<<QUIET>>")


def extract_quiet_marker(text: str) -> tuple[bool, str]:
    """Есть ли <<QUIET>> вне кодовых блоков → (да или нет, текст без меток)."""
    return _extract_flag_marker(_QUIET_RE, text)


# Повторная проверка, которую агент назначает себе сам (идея agent-autoloop,
# решение Ивана 04.11.2026): <<LATER:30m|что проверить>>. Срок разбирает
# tg_at.schedule_later, здесь только форма. Устроено как ASK, но «>» в тексте
# допустим, а цитата формата в `коде` маркером не считается (ревью 04.11.2026).
_LATER_RE = re.compile(r"<<LATER:([^\n]+?)>>")
LATER_MAX_ABOUT_LEN = 200


def extract_later_marker(text: str) -> tuple[tuple[str, str] | None, str]:
    """<<LATER:срок|текст>> в самом конце ответа → ((срок, текст), текст без маркера).

    Считается только последний маркер вне ``` ``` ``` и `кода`, и только если
    после него одни пробелы, а строка с ним не цитата «>»: агент ставит свой
    в конце ответа, маркер из процитированного письма или страницы посреди
    ответа не срабатывает (ревью 04.11.2026). Без «|», с пустой частью или
    длинным текстом маркер остаётся в тексте как есть, другие тоже.
    """
    fences = _code_spans(text)
    marks = [m for m in _LATER_RE.finditer(text)
             if not any(start <= m.start() < end for start, end in fences)]
    if not marks:
        return None, text
    m = marks[-1]
    line_head = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
    if text[m.end():].strip() or line_head.lstrip().startswith(">"):
        return None, text
    spec, sep, about = m.group(1).partition("|")
    spec, about = spec.strip(), about.strip()
    if not sep or not spec or not about or len(about) > LATER_MAX_ABOUT_LEN:
        return None, text
    cleaned = text[:m.start()] + text[m.end():]
    return (spec, about), re.sub(r"\n{3,}", "\n\n", cleaned).strip()


# Сколько отметок одного вида называть в футере; остальные числом (журнал audit полный).
_RUN_NOTES_SHOWN = 8


def format_run_notes(items: list[tuple[str, str]]) -> list[str]:
    """Отметки хода (approval.add_run_note) строками футера: по строке на
    вид, повторы один раз, в порядке появления."""
    groups: dict[str, list[str]] = {}
    for kind, item in items:
        seen = groups.setdefault(kind, [])
        if item not in seen:
            seen.append(item)
    labels = {"auto": _("✍️ без карточки: {items}"), "memory": _("💾 в память: {items}")}
    lines = []
    for kind, seen in groups.items():
        text = ", ".join(seen[:_RUN_NOTES_SHOWN])
        if len(seen) > _RUN_NOTES_SHOWN:
            text += _(" и ещё {n}").format(n=len(seen) - _RUN_NOTES_SHOWN)
        lines.append(labels.get(kind, kind + ": {items}").format(items=text))
    return lines


async def send_cost_footer(
    cfg: Config, update: Update, result: ClaudeResult, model_note: str = "", *, warn_reset: bool = True,
    notes: list[str] | tuple[str, ...] = (),
) -> bool:
    """Короткий футер: цена ответа, сессии и контекст. model_note это
    напоминание о недефолтной модели ветки («🤖 opus ⏳»), пусто на дефолте;
    в теме задачи к нему дописан расход задачи («задача ≈$1.35»).
    warn_reset=False убирает подсказку про /reset: в теме с автосжатием
    разговор сжимает сам CLI (решение Ивана 21.10.2026).

    Цена ответа это разница накопительной суммы сессии с прошлым ответом
    (claude_runner._answer_cost), контекст это вход последнего запроса
    основного диалога, а не сумма по запросам рана, как до 13.10.2026: по ней
    предупреждение срабатывало почти всегда (ревью Codex). Шлём только при
    расходе по сессии: $0-ошибки, cancel, 404 без футера.

    notes: служебные строки хода (новая сессия, запись без карточки, подача в
    память), идут последними строками футера и шлют его и без расхода (ux2,
    01.11.2026). True: футер отправлен, notes доставлены."""
    footer = _cost_line(cfg, result, model_note, warn_reset)
    text = "\n".join(([footer] if footer else []) + list(notes))
    if not text:
        return False
    try:
        # Без звука: футер идёт сразу за ответом, второй звонок на один ход
        # лишний (аудит удобства 31.10.2026).
        await update.message.reply_text(text, disable_notification=True)
        return True
    except Exception:
        log.exception("cost footer send failed")
        return False


def _cost_line(cfg: Config, result: ClaudeResult, model_note: str, warn_reset: bool) -> str:
    """Строка расхода футера; пусто, если сказать нечего."""
    if not cfg.show_cost_footer or result.session_cost_usd <= 0:
        return ""
    warn = warn_reset and cfg.context_warn_tokens > 0 and result.context_tokens >= cfg.context_warn_tokens
    # Дешёвый ответ без строки (решение Ивана 22.10.2026); строка остаётся,
    # если есть что сказать: большой контекст, недефолтная модель, расход задачи.
    if (result.cost_usd is not None and result.cost_usd < getattr(cfg, "cost_footer_min_usd", 0.0)
            and not warn and not model_note):
        return ""
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
    if warn:
        footer += _("\n⚠️ контекст большой: /reset удешевит и ускорит следующие ответы")
    return footer


# Случай 25.10.2026: GPX 2 МБ загружен, но ответ Telegram не пришёл за 5 с
# (умолчание read_timeout), бот написал «не удалось», а файл дошёл.
_SEND_FILE_TIMEOUT = 120
_SEND_FILE_TIMEOUT_MAX = 3600


def _send_file_timeout(size: int) -> int:
    """Свой сервер отвечает на sendDocument, когда сам загрузил файл в
    Telegram: срок растёт с размером (около 0,25 МБ/с, оценка, не замер)."""
    return min(_SEND_FILE_TIMEOUT_MAX, max(_SEND_FILE_TIMEOUT, 60 + size // (256 * 1024)))


async def send_files(update: Update, files: list[Path], *, local: bool = False,
                     max_bytes: int = _SEND_FILE_MAX_BYTES) -> None:
    """local: свой сервер Bot API: файл уходит ему путём (local_mode PTB шлёт
    file://, сервер читает с диска сам), без загрузки байтов по HTTP; в облако
    открытым файлом. Размер проверяется ещё раз: файл мог вырасти после
    разбора маркера."""
    msg = update.message
    for fpath in files:
        try:
            size = fpath.stat().st_size
            if size > max_bytes:
                log.warning("SEND_FILE skipped %s: grew to %d bytes", fpath, size)
                await msg.reply_text(_("⚠️ SEND_FILE({raw}) пропущен: {error}").format(
                    raw=fpath, error=f"too large ({size} bytes, max {max_bytes})"))
                continue
            # Облако отвечает, когда приняло байты по HTTP: прежние 120 с.
            wait = _send_file_timeout(size) if local else _SEND_FILE_TIMEOUT
            if local:
                await msg.reply_document(document=fpath, filename=fpath.name,
                                         read_timeout=wait, write_timeout=wait)
            else:
                with fpath.open("rb") as fh:
                    await msg.reply_document(document=fh, filename=fpath.name,
                                             read_timeout=wait, write_timeout=wait)
            log.info("SEND_FILE sent: %s (%d bytes)", fpath, size)
        except Exception as e:
            log.exception("SEND_FILE failed for %s", fpath)
            # Тайм-аут чтения: файл обычно загружен, не пришло подтверждение (но не всегда).
            if is_unconfirmed(e):
                text = _("⚠️ Telegram не подтвердил отправку файла {name}: он мог дойти, проверь чат выше")
            else:
                text = _("⚠️ не удалось отправить файл {name}")
            try:
                await msg.reply_text(text.format(name=fpath.name))
            except Exception:
                pass


def _fit_utf16(text: str, limit: int) -> int:
    """Сколько первых знаков text умещается в limit единиц UTF-16."""
    used = 0
    for i, ch in enumerate(text):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > limit:
            return i
    return len(text)


def split_for_telegram(text: str, limit: int) -> list[str]:
    # Лимит Telegram в единицах UTF-16, эмодзи вне BMP по две: кусок по
    # числу знаков отвергался целиком (аудит очереди 31.10.2026).
    if utf16_len(text) <= limit:
        return [text]
    out: list[str] = []
    remaining = text
    while (fit := max(_fit_utf16(remaining, limit), 1)) < len(remaining):
        cut = remaining.rfind("\n", 0, fit)
        if cut < fit // 2:  # мало что выиграли — режем жёстко
            cut = fit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out

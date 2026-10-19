"""Чистая часть скилла chat_digest: ключи Telethon, разбор списка чатов,
формат непрочитанного для claude и разрезание сводки на сообщения.

Почему по темам: у форума Telegram счётчик непрочитанного живёт в каждой теме
отдельно, а общий хвост ленты (get_history) старые непрочитанные темы просто
не показывает. Границу прочтения даёт `read_inbox_max_id` темы (у обычной
группы то же поле у диалога), сообщения после неё и есть непрочитанное.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from ...i18n import N_, _

SECTION_SEP = "-----"
PLAIN_TOPIC_ID = 0  # чат без тем: одна условная тема на весь диалог
PLAIN_TOPIC_TITLE = N_("Обсуждение")
_SEP_RE = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)


@dataclass(frozen=True)
class TelegramCreds:
    api_id: int
    api_hash: str = field(repr=False)
    session: str = field(repr=False)


def load_telegram_creds(path: Path) -> TelegramCreds:
    """Ключи и строка сессии из .env telegram-mcp: тот же личный аккаунт, что у
    MCP-сервера, вторую авторизацию заводить не нужно. Метка PERSONAL в
    приоритете, как у самого сервера."""
    if not path.is_file():
        raise RuntimeError(_("файл ключей Telethon не найден: {path}").format(path=path))
    values = dotenv_values(path)
    api_id = (values.get("TELEGRAM_API_ID") or "").strip()
    api_hash = (values.get("TELEGRAM_API_HASH") or "").strip()
    session = (
        values.get("TELEGRAM_SESSION_STRING_PERSONAL")
        or values.get("TELEGRAM_SESSION_STRING")
        or ""
    ).strip()
    missing = [
        name for name, value in (
            ("TELEGRAM_API_ID", api_id),
            ("TELEGRAM_API_HASH", api_hash),
            ("TELEGRAM_SESSION_STRING_PERSONAL или TELEGRAM_SESSION_STRING", session),
        ) if not value
    ]
    if missing:
        raise RuntimeError(_("в {path} нет: {missing}").format(path=path, missing=', '.join(missing)))
    return TelegramCreds(api_id=int(api_id), api_hash=api_hash, session=session)


def parse_chat_ids(raw: str) -> list[int]:
    """CHAT_DIGEST_SOURCE_CHAT_IDS: id через запятую, порядок сохраняется,
    повторы схлопываются. Не число: ошибка словами, а не трассировка."""
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as e:
            raise RuntimeError(
                _("CHAT_DIGEST_SOURCE_CHAT_IDS: «{item}» не похоже на id чата").format(item=item)) from e
        if value not in out:
            out.append(value)
    return out


def parse_chat_spec(value: str) -> int | str:
    """Значение --chat: целое это id чата, всё остальное подстрока названия."""
    item = value.strip()
    if not item:
        raise RuntimeError(_("--chat: пустое значение"))
    return int(item) if re.fullmatch(r"-?\d+", item) else item


def match_dialogs(query: str, dialogs: list[tuple[int, str]]) -> tuple[int, str]:
    """Один диалог по имени: точное совпадение названия важнее подстроки,
    регистр не важен. Несколько кандидатов или ни одного это ошибка словами,
    а не «первый попавшийся»: сводка отмечает чат прочитанным."""
    needle = query.strip().casefold()
    exact = [d for d in dialogs if d[1].casefold() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = exact or [d for d in dialogs if needle in d[1].casefold()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise RuntimeError(_("чат «{query}» не найден среди диалогов").format(query=query))
    names = "; ".join(f"{title} ({chat_id})" for chat_id, title in partial[:8])
    raise RuntimeError(
        _("«{query}» подходит к нескольким чатам, уточни имя или дай id: {names}").format(
            query=query, names=names))


@dataclass(frozen=True)
class Message:
    id: int
    date: str  # ISO до минут, UTC
    sender: str
    text: str
    media: str | None = None
    reply_to: int | None = None
    forwarded_from: str | None = None


@dataclass
class TopicUnread:
    topic_id: int
    title: str
    unread_count: int
    read_inbox_max_id: int
    messages: list[Message] = field(default_factory=list)

    @property
    def max_id(self) -> int | None:
        """Последний собранный id: до него и отмечаем прочитанным. Всё, что
        пришло позже сбора или не влезло в лимит, остаётся непрочитанным."""
        return max((m.id for m in self.messages), default=None)


@dataclass
class ChatUnread:
    chat_id: int
    title: str
    forum: bool
    topics: list[TopicUnread] = field(default_factory=list)
    peer: object = None  # InputPeer с access_hash из сбора: отметка не разрешает id заново

    @property
    def unread(self) -> list[TopicUnread]:
        return [t for t in self.topics if t.messages]

    @property
    def total(self) -> int:
        return sum(len(t.messages) for t in self.topics)


def format_message(topic: TopicUnread, m: Message) -> str:
    """Одна строка на сообщение, текст целиком: обрезанный хвост не попал бы
    в сводку, а отметка прошла бы по всему сообщению."""
    extra = []
    if m.media:
        extra.append(f"[{m.media}]")
    if m.forwarded_from:
        extra.append(_("[пересылка: {sender}]").format(sender=m.forwarded_from))
    if m.reply_to and m.reply_to != topic.topic_id:
        extra.append(_("(ответ на {id})").format(id=m.reply_to))
    text = m.text.replace("\n", " ⏎ ")
    return f"{m.id} {m.date} {m.sender}: {' '.join(extra)} {text}".rstrip()


def format_topic(topic: TopicUnread) -> str:
    """Компактная текстовая лента темы для claude."""
    lines = [_("# Тема «{title}», непрочитанных {count}").format(
        title=topic.title, count=len(topic.messages))]
    lines.extend(format_message(topic, m) for m in topic.messages)
    return "\n".join(lines)


def fit_budget(chat: ChatUnread, max_chars: int) -> int:
    """Уложить чат в общий бюджет промпта целыми сообщениями. Темы идут по
    порядку: тема, на которой бюджет кончился, режется по хвосту, темы после
    неё выпадают из прогона целиком. Всё выпавшее остаётся непрочитанным,
    потому что отметка идёт по максимальному id из оставшегося. Хотя бы одно
    сообщение проходит всегда, иначе гигантское сообщение стопорило бы чат
    навсегда. Возвращает число отложенных сообщений."""
    spent = 0
    dropped = 0
    kept_topics: list[TopicUnread] = []
    exhausted = False
    for topic in chat.topics:
        if exhausted:
            dropped += len(topic.messages)
            continue
        kept: list[Message] = []
        for m in topic.messages:
            size = len(format_message(topic, m)) + 1
            if spent + size > max_chars and (kept or kept_topics):
                exhausted = True
                break
            spent += size
            kept.append(m)
        dropped += len(topic.messages) - len(kept)
        topic.messages = kept
        if kept:
            kept_topics.append(topic)
    chat.topics = kept_topics
    return dropped


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", text.casefold())


def validate_digest(sections: list[str], chat: ChatUnread) -> list[str]:
    """Сводка полна, когда первый блок «Лично тебе» на месте и у каждой темы
    с непрочитанным есть блок «Тема: …». Иначе отметка прошла бы по темам,
    которых в сводке нет (отказ модели, пропуск, чужая инструкция в тексте).
    Возвращает список претензий словами, пусто значит порядок."""
    problems: list[str] = []
    if not sections or not sections[0].casefold().startswith(_("Лично тебе").casefold()):
        problems.append(_("нет блока «Лично тебе» в начале"))
    heads = [_norm(sec.splitlines()[0]) for sec in sections[1:] if sec.strip()]
    topic_label = _norm(_("Тема"))
    heads = [h[len(topic_label):] if h.startswith(topic_label) else h for h in heads]
    for topic in chat.unread:
        if topic.topic_id == PLAIN_TOPIC_ID:
            # Одна лента без тем: модель сама делит её на сюжеты, значит
            # достаточно хотя бы одного содержательного блока после «Лично тебе».
            if not heads:
                problems.append(_("нет ни одного блока с содержанием"))
            continue
        want = _norm(topic.title)
        if not any(h and (want in h or h in want) for h in heads):
            problems.append(_("нет блока темы «{title}»").format(title=topic.title))
    return problems


PROMPT = N_("""Ниже непрочитанные сообщения из чата «{chat_title}» в Telegram{forum_note}. Владелец аккаунта: {owner}. Собери для него сводку.

Правила:
1. Русский язык, готовый текст для Telegram: без markdown, без заголовков с решёткой, без длинного тире.
2. Первый блок начинается строкой «Лично тебе». В нём всё, что адресовано владельцу (обращения, вопросы, предложения без его ответа), и события, требующие реакции: дни рождения, просьбы о помощи, встречи с датами. Если такого нет, блок состоит из одной строки «Лично тебе: ничего».
3. Дальше по одному блоку на тему. Первая строка блока: «Тема: <название>». Затем 2–8 коротких абзацев с сутью обсуждений: имена участников как в чате, даты, итоги, решения, новости из жизни людей. Не пересказывай каждое сообщение подряд.
4. Фото и видео тебе не видны: не додумывай их содержание, только отмечай, что они были.
5. Блоки разделяй строкой ровно из пяти дефисов: {sep}
6. Ничего не выдумывай, без советов и оценок от себя.

{body}""")


def build_prompt(chat: ChatUnread, owner: str) -> str:
    forum_note = (_(", форум с темами") if chat.forum
                  else _(" (одна лента без тем: раздели её на блоки по сюжетам, каждый начинай строкой «Тема: <сюжет>»)"))
    body = "\n\n".join(format_topic(t) for t in chat.unread)
    return _(PROMPT).format(chat_title=chat.title, forum_note=forum_note, owner=owner, sep=SECTION_SEP, body=body)


def split_sections(digest: str) -> list[str]:
    """Сводка → отдельные сообщения Telegram (по одному на блок)."""
    return [part.strip() for part in _SEP_RE.split(digest) if part.strip()]


def period_label(chat: ChatUnread) -> str:
    dates = sorted(m.date[:10] for t in chat.topics for m in t.messages)
    if not dates:
        return ""
    first, last = dates[0], dates[-1]
    return first if first == last else f"{first} … {last}"


def header_line(chat: ChatUnread) -> str:
    """Первое сообщение сводки: какой чат, за какой период, сколько и где."""
    where = ", ".join(f"{t.title} ({len(t.messages)})" for t in chat.unread)
    return _("Сводка «{title}», {period}: {total} непрочитанных. Темы: {topics}").format(
        title=chat.title, period=period_label(chat), total=chat.total, topics=where)

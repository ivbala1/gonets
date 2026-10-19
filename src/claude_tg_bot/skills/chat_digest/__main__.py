"""Сводка непрочитанного в чатах Telegram → тема «Сводки чатов» в группе бота, затем отметка прочитанным.

Зачем: в чатах друзей пишут больше, чем Иван успевает читать. Скилл берёт
чаты из CHAT_DIGEST_SOURCE_CHAT_IDS или из --chat, собирает непрочитанное (у
форума ПО ТЕМАМ: счётчики живут в темах, общий хвост ленты старые темы не
показывает; у обычной группы и лички по диалогу), отдаёт ленту headless
claude за сводкой, постит её в тему CHAT_DIGEST_TG_CHAT_ID /
CHAT_DIGEST_TG_THREAD_ID и отмечает прочитанным ровно до последнего
сообщения, попавшего в сводку. Всё, что пришло позже, остаётся
непрочитанным. Тема одна на все чаты, каждая сводка начинается с названия
чата.

Порядок и гарантии:
  1. Telethon (сессия из .env telegram-mcp): авторизация проверяется явно,
     без интерактивного входа. Сущности чатов берутся с access_hash при
     сборе и переиспользуются при отметке. Клиент отключается ДО claude.
  2. По каждому чату: лента укладывается в бюджет CHAT_DIGEST_MAX_CHARS
     целыми сообщениями (остаток ждёт следующего прогона); claude -p без
     единого инструмента и без MCP пишет сводку; сводка проверяется на
     полноту (блок «Лично тебе» и блок каждой темы), иначе чат пропускается.
  3. Сводка уходит в тему блоками; отметка прочитанным СРАЗУ после того,
     как ушли все блоки этого чата. Упала сводка, доставка или отметка
     одного чата, остальные чаты идут своим чередом, а упавший повторится
     в следующем прогоне (повтор сводки в теме допустим, потеря нет).
  4. Замок на прогон: таймер и ручной запуск не пересекаются.
  --dry-run печатает сводки и ничего не постит и не отмечает.
  --no-mark постит, но не отмечает.
  --chat <id или имя> (можно несколько): разовая сводка любого чата, не
     только из списка; имя ищется по названию диалога, неоднозначность это
     ошибка словами, а не первый попавшийся чат.
Нет непрочитанного: выход 0 молча.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from ...i18n import _
from ...notify import send_message
from .._oneshot import resolve_claude_bin, run_claude_oneshot
from .core import (
    PLAIN_TOPIC_ID,
    PLAIN_TOPIC_TITLE,
    SECTION_SEP,
    ChatUnread,
    Message,
    TelegramCreds,
    TopicUnread,
    build_prompt,
    fit_budget,
    header_line,
    load_telegram_creds,
    match_dialogs,
    parse_chat_ids,
    parse_chat_spec,
    split_sections,
    validate_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
HOOK_SETTINGS = REPO_ROOT / "claude-hook-settings.json"
LOCK_PATH = REPO_ROOT / "state" / "chat_digest.lock"
DEFAULT_TELEGRAM_ENV = "~/.local/share/telegram-mcp/.env"
DEFAULT_TIMEOUT_SEC = 480  # один чат вместе со сбором и доставкой укладывается в 10 минут Bash у бота
DEFAULT_LIMIT_PER_TOPIC = 600
DEFAULT_MAX_CHARS = 200_000
TOPICS_PAGE = 100


@contextlib.asynccontextmanager
async def _session(creds: TelegramCreds):
    """Подключение без start(): у отозванной сессии start() спросил бы телефон
    со stdin, под systemd это трассировка, в терминале вечное ожидание."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(creds.session), creds.api_id, creds.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                _("сессия Telethon не авторизована: перевыпусти строку сессии telegram-mcp"))
        yield client
    finally:
        await client.disconnect()


def _sender_name(m) -> str:
    s = m.sender
    if s is None:
        return str(m.sender_id)
    name = " ".join(x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x)
    return name or getattr(s, "title", None) or getattr(s, "username", None) or str(m.sender_id)


async def _entities(cl, specs: list[int | str]) -> list:
    """Спецификации → сущности Telethon. Имя ищется по диалогам; числовой id
    сначала через кэш и запрос по id, а если Telegram не отдал сущность без
    access_hash, тоже через диалоги. Порядок сохраняется, повторы схлопываются."""
    dialogs = None

    async def load_dialogs():
        nonlocal dialogs
        if dialogs is None:
            dialogs = [d async for d in cl.iter_dialogs()]
        return dialogs

    out: list = []
    seen: set[int] = set()
    for spec in specs:
        if isinstance(spec, int):
            try:
                entity = await cl.get_entity(spec)
            except ValueError:
                found = [d for d in await load_dialogs() if d.id == spec]
                if not found:
                    raise RuntimeError(_("чат {spec} не найден среди диалогов").format(spec=spec)) from None
                entity = found[0].entity
        else:
            listing = [(d.id, d.name or "") for d in await load_dialogs()]
            chat_id = match_dialogs(spec, listing)[0]
            entity = next(d.entity for d in dialogs if d.id == chat_id)
        from telethon import utils

        marked = utils.get_peer_id(entity)
        if marked not in seen:
            seen.add(marked)
            out.append(entity)
    return out


async def _forum_topics(cl, chat, request_cls, page: int = TOPICS_PAGE) -> list:
    """Все темы форума постранично. Смещение по последней теме страницы:
    offset_date это дата её верхнего сообщения (оно приходит в res.messages),
    offset_id его id, offset_topic id темы. Конец, когда набрано res.count,
    страница пуста или смещение не сдвинулось. Повторы отсеиваются по id."""
    topics: list = []
    seen: set[int] = set()
    offset_date, offset_id, offset_topic = None, 0, 0
    while True:
        res = await cl(request_cls(
            peer=chat, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=page,
        ))
        fresh = [t for t in res.topics if getattr(t, "id", None) not in seen]
        if not fresh:
            return topics
        for t in fresh:
            seen.add(t.id)
            topics.append(t)
        if len(topics) >= getattr(res, "count", 0):
            return topics
        last = fresh[-1]
        top_id = getattr(last, "top_message", 0)
        top = next((m for m in getattr(res, "messages", []) if m.id == top_id), None)
        offset_date = getattr(top, "date", None) or getattr(last, "date", None)
        offset_id, offset_topic = top_id, last.id


async def _fill(cl, chat, topic: TopicUnread, limit: int) -> None:
    kwargs = {"min_id": topic.read_inbox_max_id, "reverse": True, "limit": limit}
    if topic.topic_id != PLAIN_TOPIC_ID:
        kwargs["reply_to"] = topic.topic_id
    async for m in cl.iter_messages(chat, **kwargs):
        fwd = None
        if m.fwd_from is not None:
            fwd = getattr(m.fwd_from, "from_name", None) or str(getattr(m.fwd_from, "from_id", "") or "")
        topic.messages.append(Message(
            id=m.id,
            date=m.date.isoformat(timespec="minutes"),
            sender=_sender_name(m),
            text=m.message or "",
            media=type(m.media).__name__.replace("MessageMedia", "") if m.media else None,
            reply_to=m.reply_to.reply_to_msg_id if m.reply_to else None,
            forwarded_from=fwd,
        ))


async def collect(creds: TelegramCreds, specs: list[int | str], limit: int) -> tuple[str, list[ChatUnread]]:
    """Подпись владельца и непрочитанное по каждому чату за одно подключение."""
    from telethon import utils
    from telethon.tl.functions.messages import GetForumTopicsRequest, GetPeerDialogsRequest
    from telethon.tl.types import InputDialogPeer

    async with _session(creds) as cl:
        me = await cl.get_me()
        owner = " ".join(x for x in (me.first_name, me.last_name) if x)
        if me.username:
            owner += f" (@{me.username})"
        chats: list[ChatUnread] = []
        for chat in await _entities(cl, specs):
            item = ChatUnread(
                chat_id=utils.get_peer_id(chat), title=utils.get_display_name(chat) or str(utils.get_peer_id(chat)),
                forum=bool(getattr(chat, "forum", False)), peer=utils.get_input_peer(chat),
            )
            if item.forum:
                for t in await _forum_topics(cl, chat, GetForumTopicsRequest):
                    if getattr(t, "unread_count", 0):
                        item.topics.append(TopicUnread(
                            topic_id=t.id, title=t.title,
                            unread_count=t.unread_count, read_inbox_max_id=t.read_inbox_max_id,
                        ))
            else:
                res = await cl(GetPeerDialogsRequest(peers=[InputDialogPeer(item.peer)]))
                for d in res.dialogs:
                    if getattr(d, "unread_count", 0):
                        item.topics.append(TopicUnread(
                            topic_id=PLAIN_TOPIC_ID, title=_(PLAIN_TOPIC_TITLE),
                            unread_count=d.unread_count, read_inbox_max_id=d.read_inbox_max_id,
                        ))
            for topic in item.topics:
                await _fill(cl, chat, topic, limit)
            chats.append(item)
        return owner, chats


async def mark_read(creds: TelegramCreds, chat: ChatUnread) -> list[str]:
    """Отметить темы одного чата до последнего id сводки. Ошибка одной темы не
    мешает остальным; возвращает список претензий словами."""
    from telethon.tl.functions.messages import ReadDiscussionRequest

    problems: list[str] = []
    async with _session(creds) as cl:
        for topic in chat.unread:
            try:
                if topic.topic_id == PLAIN_TOPIC_ID:
                    await cl.send_read_acknowledge(chat.peer, max_id=topic.max_id)
                else:
                    await cl(ReadDiscussionRequest(peer=chat.peer, msg_id=topic.topic_id, read_max_id=topic.max_id))
            except Exception as e:  # noqa: BLE001 - одна тема не должна ронять отметку остальных
                problems.append(_("{title}: {error}").format(title=topic.title, error=type(e).__name__))
    return problems


def run_digest_claude(prompt: str, timeout: int) -> str:
    """Ран сводки без единого инструмента: claude здесь только читает промпт
    и пишет текст. В промпте чужие сообщения, поэтому встроенные инструменты
    выключены целиком (--tools ""), MCP-серверы не поднимаются, режим plan
    как второй замок, транскрипт с чужой перепиской на диск не пишется."""
    args = [
        resolve_claude_bin(),
        "-p",
        "--output-format", "json",
        "--permission-mode", "plan",
        "--tools", "",
        "--no-session-persistence",
        "--settings", str(HOOK_SETTINGS),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    ]
    try:
        proc = run_claude_oneshot(args, input=prompt.encode("utf-8"), timeout=timeout, cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        raise RuntimeError(_("claude не уложился в {seconds} с").format(seconds=timeout)) from None
    except OSError as e:
        raise RuntimeError(_("claude не запустился: {error}").format(error=type(e).__name__)) from None
    if proc.returncode != 0:
        # Сырой вывод дочернего процесса наружу не идёт: в журнал и боту
        # уходит только код и класс причины.
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        kind = (_("лимит или доступ") if any(k in stderr for k in ("429", "401", "rate", "limit"))
                else _("ошибка запуска"))
        raise RuntimeError(_("claude exit {rc}: {kind}").format(rc=proc.returncode, kind=kind))
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(_("claude stdout не JSON: {error}").format(error=e)) from e
    if payload.get("is_error"):
        raise RuntimeError(_("claude вернул is_error=true"))
    return (payload.get("result") or "").strip()


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else None


def _positive(value: int, name: str) -> int:
    if value <= 0:
        raise RuntimeError(_("{name} должен быть больше нуля, получено {value}").format(name=name, value=value))
    return value


def digest_messages(chat: ChatUnread, owner: str, timeout: int) -> list[str]:
    """Заголовок плюс блоки сводки одного чата. RuntimeError, если claude упал
    или сводка неполна: такой чат не постим и не отмечаем."""
    sections = split_sections(run_digest_claude(build_prompt(chat, owner), timeout))
    problems = validate_digest(sections, chat)
    if problems:
        raise RuntimeError(_("сводка неполна: {problems}").format(problems="; ".join(problems)))
    return [header_line(chat), *sections]


def _deliver(messages: list[str], chat_id: int, thread_id: int) -> int:
    """Число ушедших блоков; `--` отделяет текст от флагов notify."""
    sent = 0
    for text in messages:
        rc, _unused = send_message(["--chat-id", str(chat_id), "--thread-id", str(thread_id), "--", text])
        if rc != 0:
            break
        sent += 1
    return sent


def _lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_tg_bot.skills.chat_digest")
    parser.add_argument("--dry-run", action="store_true", help="напечатать сводки, не постить и не отмечать")
    parser.add_argument("--no-mark", action="store_true", help="запостить, но не отмечать прочитанным")
    parser.add_argument("--chat", action="append", default=None, metavar="ID_ИЛИ_ИМЯ",
                        help="разовая сводка этого чата по id или названию (можно повторять); "
                             "без флага берётся список CHAT_DIGEST_SOURCE_CHAT_IDS")
    parser.add_argument("--limit", type=int, default=None, help="максимум сообщений на тему за прогон")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    load_dotenv(REPO_ROOT / ".env")
    chat = _int_env("CHAT_DIGEST_TG_CHAT_ID")
    thread = _int_env("CHAT_DIGEST_TG_THREAD_ID")
    if chat is None or thread is None:
        print(_("chat_digest: нужны CHAT_DIGEST_TG_CHAT_ID и CHAT_DIGEST_TG_THREAD_ID в .env (пара)"),
              file=sys.stderr)
        return 1
    env_path = Path(os.environ.get("CHAT_DIGEST_TELEGRAM_ENV", "").strip() or DEFAULT_TELEGRAM_ENV).expanduser()

    lock = _lock()
    if lock is None:
        print(_("chat_digest: другой прогон ещё идёт, этот пропущен"))
        return 0
    try:
        try:
            timeout = _positive(_int_env("CHAT_DIGEST_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC, "CHAT_DIGEST_TIMEOUT_SEC")
            limit = _positive(args.limit if args.limit is not None else
                              (_int_env("CHAT_DIGEST_MAX_PER_TOPIC") or DEFAULT_LIMIT_PER_TOPIC), _("лимит на тему"))
            max_chars = _positive(_int_env("CHAT_DIGEST_MAX_CHARS") or DEFAULT_MAX_CHARS, "CHAT_DIGEST_MAX_CHARS")
            sources: list[int | str]
            if args.chat:
                sources = [parse_chat_spec(c) for c in args.chat]
            else:
                sources = list(parse_chat_ids(os.environ.get("CHAT_DIGEST_SOURCE_CHAT_IDS", "")))
                if not sources:
                    raise RuntimeError(_("CHAT_DIGEST_SOURCE_CHAT_IDS пуст и --chat не задан: нечего сводить"))
            creds = load_telegram_creds(env_path)
            owner, chats = asyncio.run(collect(creds, sources, limit))
        except RuntimeError as e:
            print(f"chat_digest: {e}", file=sys.stderr)
            return 1

        pending = [c for c in chats if c.total]
        if not pending:
            print(_("chat_digest: непрочитанного нет: {chats}").format(
                chats=", ".join(c.title for c in chats)))
            return 0

        failed: list[str] = []
        for item in pending:
            deferred = fit_budget(item, max_chars)
            note = _(", {count} отложено до следующего прогона").format(count=deferred) if deferred else ""
            try:
                messages = digest_messages(item, owner, timeout)
            except RuntimeError as e:
                print(_("chat_digest: «{title}»: {error}").format(title=item.title, error=e), file=sys.stderr)
                failed.append(item.title)
                continue
            if args.dry_run:
                print(("\n\n" + SECTION_SEP + "\n\n").join(messages))
                print(_("\nchat_digest: dry-run, «{title}»: {count} сообщений не отправлены{note}\n").format(
                    title=item.title, count=len(messages), note=note))
                continue
            sent = _deliver(messages, chat, thread)
            if sent != len(messages):
                print(_("chat_digest: «{title}»: ушло {sent} из {count} сообщений, "
                        "отметки нет, следующий прогон соберёт заново").format(
                            title=item.title, sent=sent, count=len(messages)), file=sys.stderr)
                failed.append(item.title)
                continue
            if args.no_mark:
                print(_("chat_digest: «{title}»: {count} сообщений в теме, --no-mark{note}").format(
                    title=item.title, count=len(messages), note=note))
                continue
            try:
                problems = asyncio.run(mark_read(creds, item))
            except RuntimeError as e:
                problems = [str(e)]
            if problems:
                print(_("chat_digest: «{title}»: сводка в теме, но отметка не прошла: {problems}").format(
                    title=item.title, problems="; ".join(problems)), file=sys.stderr)
                failed.append(item.title)
                continue
            marked = ", ".join(_("{title} до {id}").format(title=t.title, id=t.max_id) for t in item.unread)
            print(_("chat_digest: «{title}»: {count} сообщений в теме, отмечено: {marked}{note}").format(
                title=item.title, count=len(messages), marked=marked, note=note))
        return 1 if failed else 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())

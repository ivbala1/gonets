"""Вложения больше 20 МБ: скачивание через MTProto под токеном бота (Telethon).

Зачем: облачный Bot API отдаёт getFile только до 20 МБ, и запись созвона
27,3 МБ 19.10.2026 не дошла. Решение Ивана 19.10: большие файлы качать
MTProto-клиентом, вошедшим ботом (api_id и api_hash приложения с
my.telegram.org плюс токен бота), а не поднимать свой сервер Bot API.

Устройство: один клиент на процесс, создаётся при первом большом файле,
сессия в state/telethon-bot.session (своя, ботовая; пользовательская сессия
telegram-mcp здесь не используется). Клиент не подписан на обновления
(receive_updates=False): Bot API продолжает получать их через getUpdates.
Бот достаёт своё же сообщение по номеру (в личке messages.getMessages, в
супергруппе channels.getMessages; access_hash ботам не нужен) и качает
медиа целиком сообщением: так Telethon сам обновляет устаревшую ссылку на
файл. Проверено пробой на ПК 19.10.2026: личка и тема группы, 27,3 МБ.

Запись идёт в `<имя>.part` рядом с целью, после проверки размера файл
переименовывается. Одновременно качается один большой файл: проверка места
на диске тогда не делит запас между двумя загрузками.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .i18n import _

log = logging.getLogger(__name__)

# Предел getFile облачного Bot API.
BOT_API_MAX_BYTES = 20 * 1024 * 1024
PART_SUFFIX = ".part"


class FetchError(Exception):
    """Сбой скачивания; текст исключения показывается Ивану как есть."""


def mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def needs_mtproto(size: int | None) -> bool:
    """Путь по размеру: неизвестный размер идёт через Bot API (при отказе
    «File is too big» бот переходит на MTProto сам)."""
    return size is not None and size > BOT_API_MAX_BYTES


def too_big_for_bot_api(exc: BaseException) -> bool:
    """Ответ Bot API на getFile файла больше 20 МБ."""
    return "file is too big" in str(exc).lower()


def limit_text(size: int, max_bytes: int) -> str:
    return _("⚠️ файл слишком большой ({got:.1f} MB), лимит {max:.1f} MB").format(
        got=size / (1024 * 1024), max=max_bytes / (1024 * 1024))


def check_disk(directory: Path, size: int, min_free: int) -> None:
    """Места должно хватить на файл и остаться запас min_free."""
    free = shutil.disk_usage(directory).free
    if free < size + min_free:
        raise FetchError(_(
            "⚠️ не хватает места на диске: файлу нужно {need}, свободно {free}, "
            "запас {reserve} оставляю системе"
        ).format(need=mb(size), free=mb(free), reserve=mb(min_free)))


def clean_parts(root: Path) -> int:
    """Недокачанные `.part`, оставшиеся после аварии бота: finally их не убрал."""
    n = 0
    if not root.is_dir():
        return 0
    for p in root.rglob("*" + PART_SUFFIX):
        with contextlib.suppress(OSError):
            p.unlink()
            n += 1
    return n


@dataclass
class Progress:
    """Ход загрузки для сообщения «качаю»: пишет колбэк Telethon, читает бот."""
    done: int = 0
    total: int = 0
    started: float = field(default_factory=time.monotonic)

    def update(self, done: int, total: int) -> None:
        self.done = done
        if total:
            self.total = total

    @property
    def percent(self) -> int:
        return int(self.done * 100 / self.total) if self.total else 0


def _telethon_client(session: str, api_id: int, api_hash: str) -> Any:
    from telethon import TelegramClient
    return TelegramClient(session, api_id, api_hash, receive_updates=False)


class BotMTProto:
    """Один MTProto-клиент бота на процесс."""

    def __init__(self, *, session_path: Path, api_id: int, api_hash: str,
                 bot_token: str, timeout_sec: float,
                 client_factory: Callable[[str, int, str], Any] = _telethon_client) -> None:
        self.session_path = session_path
        self._api_id = api_id
        self._api_hash = api_hash
        self._bot_token = bot_token
        self.bot_id = int(bot_token.split(":", 1)[0])
        self.timeout_sec = timeout_sec
        self._factory = client_factory
        self._client: Any = None
        self._login_lock = asyncio.Lock()
        self._queue = asyncio.Semaphore(1)

    def __repr__(self) -> str:  # ключи и токен в лог не попадают
        return f"BotMTProto(session={self.session_path})"

    @property
    def busy(self) -> bool:
        """Идёт другая большая загрузка: эта встанет в очередь."""
        return self._queue.locked()

    async def _connected(self) -> Any:
        async with self._login_lock:
            if self._client is not None:
                if not self._client.is_connected():
                    await self._client.connect()
                return self._client
            # Права 600 с момента создания: в файле ключ авторизации бота.
            if not self.session_path.exists():
                os.close(os.open(self.session_path, os.O_CREAT | os.O_WRONLY, 0o600))
            client = self._factory(str(self.session_path), self._api_id, self._api_hash)
            try:
                await client.start(bot_token=self._bot_token)
                me = await client.get_me()
            except BaseException:
                with contextlib.suppress(Exception):
                    await client.disconnect()
                raise
            # Сессия, авторизованная другим аккаунтом, Telethon лишь
            # предупреждает и работает дальше под ним: проверяем сами.
            if not getattr(me, "bot", False) or getattr(me, "id", None) != self.bot_id:
                with contextlib.suppress(Exception):
                    await client.disconnect()
                log.error("mtproto session %s is not this bot (id=%s)", self.session_path,
                          getattr(me, "id", None))
                raise FetchError(_(
                    "⚠️ сессия MTProto принадлежит не этому боту: удали {path}, "
                    "бот войдёт заново"
                ).format(path=self.session_path))
            log.info("mtproto client logged in as bot id=%s", self.bot_id)
            self._client = client
            return client

    async def download(self, chat_id: int, message_id: int, target: Path, *,
                       max_bytes: int, min_free: int, progress: Progress) -> int:
        """Скачать файл сообщения в target. Возвращает размер в байтах.
        Общий срок timeout_sec покрывает вход, повтор и скачивание, но не
        ожидание очереди."""
        async with self._queue:
            progress.started = time.monotonic()
            try:
                return await asyncio.wait_for(
                    self._download(chat_id, message_id, target, max_bytes=max_bytes,
                                   min_free=min_free, progress=progress),
                    self.timeout_sec)
            except TimeoutError:
                raise FetchError(_(
                    "⚠️ загрузка не уложилась в {min} мин (скачано {pct}%), пришли файл ещё раз"
                ).format(min=int(self.timeout_sec // 60), pct=progress.percent)) from None

    async def _download(self, chat_id: int, message_id: int, target: Path, *,
                        max_bytes: int, min_free: int, progress: Progress) -> int:
        from telethon.errors import FloodWaitError

        part = target.with_name(target.name + PART_SUFFIX)
        for attempt in (1, 2):
            try:
                client = await self._connected()
                msg = await client.get_messages(chat_id, ids=message_id)
                f = getattr(msg, "file", None) if msg is not None else None
                size = getattr(f, "size", None) if f is not None else None
                if not size:
                    raise FetchError(_("⚠️ сообщение с файлом удалено или изменено, пока шла загрузка"))
                # Размер из самого документа: сообщение могли отредактировать
                # после того, как бот проверил лимит по обновлению.
                if max_bytes > 0 and size > max_bytes:
                    raise FetchError(limit_text(size, max_bytes))
                check_disk(target.parent, size, min_free)
                progress.update(0, size)
                got = await client.download_media(msg, file=str(part),
                                                  progress_callback=progress.update)
                written = part.stat().st_size if part.is_file() else 0
                if got is None or Path(got) != part or written != size:
                    raise FetchError(_(
                        "⚠️ файл скачался не целиком ({got} из {total}), пришли его ещё раз"
                    ).format(got=mb(written), total=mb(size)))
                os.replace(part, target)
                return size
            except FetchError:
                raise
            except FloodWaitError as e:
                raise FetchError(_("⚠️ Telegram просит подождать {sec} с, пришли файл позже")
                                 .format(sec=e.seconds)) from None
            except ConnectionError:
                if attempt == 2:
                    raise FetchError(_(
                        "⚠️ связь с Telegram оборвалась на {pct}%, пришли файл ещё раз"
                    ).format(pct=progress.percent)) from None
                log.info("mtproto download %s/%s: connection dropped, retry",
                         chat_id, message_id)
            finally:
                with contextlib.suppress(OSError):
                    part.unlink()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

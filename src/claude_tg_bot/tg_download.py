"""Скачивание вложений Telegram (перенесено из bot.py, шаг 8 разбиения 2)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram.error import BadRequest, NetworkError
from telegram.ext import ContextTypes

from . import bigfile
from .config import Config
from .i18n import _

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


@dataclass
class DownloadJob:
    """Идущая большая загрузка: /cancel, /compose cancel и остановка бота
    отменяют её задачу, общий MTProto-клиент при этом остаётся."""
    chat_key: str
    generation: int | None  # поколение compose-буфера, если кусок в нём
    task: asyncio.Future | None = None
    cancelled: bool = False


def _downloads_cancel(app: Any, chat_key: str | None = None, *, generation: int | None = None) -> int:
    """Отменить большие загрузки темы (или все). Сколько отменено."""
    jobs: dict[int, DownloadJob] = app.bot_data.get("downloads") or {}
    n = 0
    for job in list(jobs.values()):
        if chat_key is not None and job.chat_key != chat_key:
            continue
        if generation is not None and job.generation != generation:
            continue
        if job.cancelled:
            continue
        job.cancelled = True
        if job.task is not None:
            job.task.cancel()
        n += 1
    return n


_PROGRESS_EVERY_SEC = 5.0


async def _quiet_edit(status: Any, text: str) -> None:
    """Сбой сообщения о ходе загрузки (удалено, RetryAfter) загрузку не роняет."""
    if status is None:
        return
    try:
        await status.edit_text(text)
    except Exception:
        log.debug("download status edit failed (ignored)", exc_info=True)


async def _progress_ticker(status: Any, progress: bigfile.Progress, label: str) -> None:
    last = ""
    while True:
        await asyncio.sleep(_b._PROGRESS_EVERY_SEC)
        if not progress.total:
            continue
        text = _("⏳ качаю «{name}», {size}: {pct}%").format(
            name=label, size=bigfile.mb(progress.total), pct=progress.percent)
        if text != last:
            await _quiet_edit(status, text)
            last = text


def _mtproto(app: Any, cfg: Config) -> bigfile.BotMTProto:
    client: bigfile.BotMTProto | None = app.bot_data.get("mtproto")
    if client is None:
        client = bigfile.BotMTProto(
            session_path=cfg.state_dir / "telethon-bot.session",
            api_id=cfg.telegram_api_id, api_hash=cfg.telegram_api_hash,
            bot_token=cfg.telegram_bot_token, timeout_sec=cfg.big_download_timeout_sec,
        )
        app.bot_data["mtproto"] = client
    return client


# Повтор скачивания через Bot API при сетевом сбое. Скачивание только читает,
# дубля не бывает, поэтому повторяем и тайм-аут. BadRequest (в том числе
# «File is too big») наследует NetworkError, но повтором не лечится.
# Случай 23.10.2026: getFile голосового упал по TimedOut, голосовое потеряно.
# Случай 29.10.2026: два голосовых подряд, все три попытки по 5 с (умолчание
# HTTPXRequest) упали по TimedOut, через минуту сеть уже отвечала за 0,3 с.
# Поэтому попытке даём 20 с на чтение и ждём заминку Telegram дольше.
_DOWNLOAD_RETRY_DELAYS = (2.0, 5.0, 15.0)
_DOWNLOAD_READ_TIMEOUT = 20.0


def _slot_holders(
    ctx: ContextTypes.DEFAULT_TYPE, chat_key: str,
    gslot: _b.GatherSlot | None, slot: _b.Reservation | None,
) -> tuple[_b.GatherBatch | _b.ComposeBuffer | None, ...]:
    """Пачка склейки и compose-буфер, в которых занят слот под это скачивание."""
    return (gslot[0] if gslot is not None else None,
            _b._compose_current(ctx, chat_key, slot) if slot is not None else None)


async def _download_retrying(
    file_obj: Any, target: Path,
    holders: tuple[_b.GatherBatch | _b.ComposeBuffer | None, ...] = (),
) -> None:
    # Скачивание с повторами (и даже одна попытка: getFile и файл по 20 с)
    # может идти дольше 30 с, которые пачка и /go ждут неготовый слот: помечаем
    # их slow, как большую загрузку, иначе кусок уедет отдельным ходом после
    # следующих сообщений (ревью 29.10.2026). Срок задают сами попытки.
    delays = iter(_DOWNLOAD_RETRY_DELAYS)
    marked = tuple(h for h in holders if h is not None)
    for h in marked:
        h.slow += 1
    try:
        while True:
            try:
                tg_file = await file_obj.get_file(read_timeout=_DOWNLOAD_READ_TIMEOUT)
                await tg_file.download_to_drive(
                    custom_path=str(target), read_timeout=_DOWNLOAD_READ_TIMEOUT)
                return
            except NetworkError as e:
                delay = None if isinstance(e, BadRequest) else next(delays, None)
                if delay is None:
                    raise
                log.warning("download failed (%s), retry in %.0fs", e, delay)
                await asyncio.sleep(delay)
    finally:
        for h in marked:
            h.slow -= 1


async def _fetch_attachment(
    update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, *, file_obj: Any, size: int | None,
    target: Path, label: str, chat_key: str, gslot: _b.GatherSlot | None,
    slot: _b.Reservation | None, hold: bool,
) -> str | None:
    """Скачать вложение в target. До 20 МБ через Bot API (getFile), больше
    через MTProto под токеном бота. Неизвестный размер идёт через Bot API,
    отказ «File is too big» переводит на MTProto.

    "small"/"big" при успехе, None при сбое (Ивану уже сказано словами).
    "big" с hold=True: вызывающий держит единицу active_work (перезапуск ждёт
    файл до Claude) и обязан её вернуть или передать пачке склейки.
    """
    msg = update.message
    if not bigfile.needs_mtproto(size):
        try:
            await _b._download_retrying(file_obj, target, _slot_holders(ctx, chat_key, gslot, slot))
            return "small"
        except Exception as e:
            if not bigfile.too_big_for_bot_api(e):
                log.exception("attachment download failed chat_key=%s", chat_key)
                await msg.reply_text(_("⚠️ не удалось скачать вложение"))
                return None
            log.info("bot api refused file as too big chat_key=%s, using mtproto", chat_key)
    return await _b._fetch_big(update, ctx, size=size, target=target, label=label,
                            chat_key=chat_key, gslot=gslot, slot=slot, hold=hold)


async def _fetch_big(
    update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, *, size: int | None, target: Path,
    label: str, chat_key: str, gslot: _b.GatherSlot | None, slot: _b.Reservation | None,
    hold: bool,
) -> str | None:
    cfg: Config = ctx.application.bot_data["cfg"]
    app = ctx.application
    bd = app.bot_data
    msg = update.message
    size_text = bigfile.mb(size) if size else _("размер неизвестен")
    if bd.get("draining") or bd.get("stopping"):
        await msg.reply_text(_("♻️ бот готовится к перезапуску: большой файл не скачиваю, "
                               "пришли его после рестарта"))
        return None
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await msg.reply_text(_(
            "⚠️ файл «{name}» ({size}) больше 20 МБ, а скачивание больших файлов не настроено: "
            "нет TELEGRAM_API_ID и TELEGRAM_API_HASH в .env бота"
        ).format(name=label, size=size_text))
        return None
    client = _mtproto(app, cfg)
    # Загрузка это работа для перезапуска: проверка draining выше и счёт без
    # await между ними.
    bd["active_work"] = bd.get("active_work", 0) + 1
    keep = False
    batch = gslot[0] if gslot is not None else None
    buf = _b._compose_current(ctx, chat_key, slot) if slot is not None else None
    for holder in (batch, buf):
        if holder is not None:
            holder.slow += 1
    job = DownloadJob(chat_key=chat_key, generation=slot[0] if slot is not None else None)
    jobs: dict[int, DownloadJob] = bd.setdefault("downloads", {})
    jobs[id(job)] = job
    progress = bigfile.Progress()
    status = None
    ticker: asyncio.Task | None = None
    try:
        try:
            status = await msg.reply_text(
                _("⏳ «{name}», {size}: жду, пока докачается другой файл").format(name=label, size=size_text)
                if client.busy else
                _("⏳ качаю «{name}», {size}").format(name=label, size=size_text)
            )
        except Exception:
            log.debug("download status send failed (ignored)", exc_info=True)
        ticker = asyncio.ensure_future(_progress_ticker(status, progress, label))
        dl = asyncio.ensure_future(client.download(
            msg.chat_id, msg.message_id, target, max_bytes=cfg.document_max_size_bytes,
            min_free=cfg.download_min_free_bytes, progress=progress))
        job.task = dl
        if job.cancelled:  # /cancel пришёл, пока отправлялось сообщение «качаю»
            dl.cancel()
        try:
            got = await dl
        except asyncio.CancelledError:
            if not job.cancelled:
                raise
            log.info("big download cancelled chat_key=%s", chat_key)
            await _quiet_edit(status, _("🗑 загрузка «{name}» отменена").format(name=label))
            return None
        except bigfile.FetchError as e:
            log.warning("big download failed chat_key=%s: %s", chat_key, e)
            text = str(e)
        except Exception as e:
            log.exception("big download failed chat_key=%s", chat_key)
            text = _("⚠️ не удалось скачать «{name}»: {err}").format(name=label, err=type(e).__name__)
        else:
            elapsed = time.monotonic() - progress.started
            log.info("big download done chat_key=%s size=%d sec=%.1f", chat_key, got, elapsed)
            await _quiet_edit(status, _("✅ скачал «{name}», {size} за {sec} с").format(
                name=label, size=bigfile.mb(got), sec=int(elapsed)))
            keep = hold
            return "big"
        if status is not None:
            await _quiet_edit(status, text)
        else:
            await msg.reply_text(text)
        return None
    finally:
        if ticker is not None:
            ticker.cancel()
        jobs.pop(id(job), None)
        for holder in (batch, buf):
            if holder is not None:
                holder.slow -= 1
        if not keep:
            bd["active_work"] -= 1


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

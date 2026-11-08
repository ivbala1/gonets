"""Скачивание вложений Telegram (перенесено из bot.py, шаг 8 разбиения 2)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram.error import BadRequest, NetworkError, TimedOut
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
    msg_key: tuple[int, int] | None = None  # (chat_id, message_id) файла: повтор свайпом


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
    timeout_to_mtproto: Callable[[], bool] | None = None,
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
                # При сбое 06.11.2026 повторы getFile по TimedOut не помогали, а
                # MTProto качал за 1-3 с: TimedOut уходит туда, как только
                # timeout_to_mtproto() разрешит (спрашиваем на каждом тайм-ауте,
                # MTProto мог освободиться). Остальные сбои сети повторяем.
                delay = (None if isinstance(e, BadRequest)
                         or (isinstance(e, TimedOut) and timeout_to_mtproto is not None
                             and timeout_to_mtproto())
                         else next(delays, None))
                if delay is None:
                    raise
                log.warning("download failed (%s), retry in %.0fs", e, delay)
                await asyncio.sleep(delay)
    finally:
        for h in marked:
            h.slow -= 1


def _media_key(msg: Any) -> tuple[int, int]:
    return (msg.chat_id, msg.message_id)


def _media_busy(app: Any, msg: Any) -> bool:
    """Файл этого сообщения ещё качается: повтор свайпом запустил бы вторую
    загрузку в тот же файл и второй ход. Уже скачанное повтор качает заново
    намеренно: отметка «скачан» врала бы после сбоя распознавания или отмены."""
    key = _media_key(msg)
    if key in (app.bot_data.get("media_busy") or set()):  # голосовое распознаётся
        return True
    jobs: dict[int, DownloadJob] = app.bot_data.get("downloads") or {}
    return any(j.msg_key == key and not j.cancelled for j in jobs.values())


async def _fetch_attachment(
    update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, *, file_obj: Any, size: int | None,
    target: Path, label: str, chat_key: str, gslot: _b.GatherSlot | None,
    slot: _b.Reservation | None, hold: bool, fail_text: str | None = None,
) -> str | None:
    """Скачать вложение или голосовое в target. До 20 МБ через Bot API
    (getFile), больше через MTProto под токеном бота. Неизвестный размер идёт
    через Bot API, отказ «File is too big» переводит на MTProto, тайм-аут
    Bot API тоже (если MTProto настроен), а сбой MTProto после тайм-аута
    ещё раз пробует Bot API.

    "small"/"big" при успехе, None при сбое или отмене (Ивану уже сказано словами).
    "big" с hold=True: вызывающий держит единицу active_work (перезапуск ждёт
    файл до Claude) и обязан её вернуть или передать пачке склейки.
    """
    msg = update.message
    if bigfile.needs_mtproto(size):
        return await _b._fetch_big(update, ctx, size=size, target=target, label=label,
                                   chat_key=chat_key, gslot=gslot, slot=slot, hold=hold)
    holders = _slot_holders(ctx, chat_key, gslot, slot)

    def bot_api(to_mtproto: Callable[[], bool] | None) -> Awaitable[None]:
        return _b._download_retrying(file_obj, target, holders, timeout_to_mtproto=to_mtproto)

    # Загрузка видна /cancel с первой попытки Bot API, а не только в MTProto
    # (код-ревью 07.11.2026: отменённое голосовое иначе распознавалось).
    job = DownloadJob(chat_key=chat_key, generation=slot[0] if slot is not None else None,
                      msg_key=_media_key(msg))
    jobs: dict[int, DownloadJob] = ctx.application.bot_data.setdefault("downloads", {})
    jobs[id(job)] = job
    retry: Callable[[], Awaitable[None]] | None = None
    try:
        try:
            await _cancellable(job, bot_api(lambda: _timeouts_to_mtproto(ctx)))
            if not job.cancelled:
                return "small"
            # /cancel мог успеть, пока задача уже кончилась: отменённое не отдаём.
            await _end_cancelled(ctx, msg, None, label)
            return None
        except asyncio.CancelledError:
            if not job.cancelled:
                raise
            log.info("download cancelled chat_key=%s", chat_key)
            await _end_cancelled(ctx, msg, None, label)
            return None
        except Exception as e:
            if job.cancelled:
                await _end_cancelled(ctx, msg, None, label)
                return None
            if bigfile.too_big_for_bot_api(e):
                log.info("bot api refused file as too big chat_key=%s, using mtproto", chat_key)
            elif isinstance(e, TimedOut) and _mtproto_configured(ctx):
                # Случай 06.11.2026: getFile фото часами висел по TimedOut, хотя
                # остальной Bot API отвечал за 0,3 с; MTProto идёт мимо getFile.
                log.warning("bot api timed out chat_key=%s, using mtproto", chat_key)
                retry = lambda: bot_api(None)  # noqa: E731
            else:
                log.exception("attachment download failed chat_key=%s", chat_key)
                await _say(msg, fail_text or _("⚠️ не удалось скачать вложение"))
                return None
        return await _b._fetch_big(update, ctx, size=size, target=target, label=label,
                                   chat_key=chat_key, gslot=gslot, slot=slot, hold=hold,
                                   job=job, retry=retry)
    finally:
        jobs.pop(id(job), None)


async def _cancellable(job: DownloadJob, coro: Awaitable[Any]) -> Any:
    """Выполнить coro задачей, которую /cancel отменит через job."""
    task = asyncio.ensure_future(coro)
    job.task = task
    if job.cancelled:  # /cancel пришёл раньше
        task.cancel()
    try:
        return await task
    finally:
        job.task = None


def _timeouts_to_mtproto(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """TimedOut getFile сразу в MTProto, без повторов Bot API. Пока MTProto
    качает другой файл, повторяем: иначе маленький встал бы за большим."""
    busy = getattr(ctx.application.bot_data.get("mtproto"), "busy", False)
    return _mtproto_configured(ctx) and not busy


def _mtproto_configured(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    cfg: Config = ctx.application.bot_data["cfg"]
    return bool(cfg.telegram_api_id and cfg.telegram_api_hash)


async def _fetch_big(
    update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, *, size: int | None, target: Path,
    label: str, chat_key: str, gslot: _b.GatherSlot | None, slot: _b.Reservation | None,
    hold: bool, job: DownloadJob | None = None,
    retry: Callable[[], Awaitable[None]] | None = None,
) -> str | None:
    """retry: файл меньше 20 МБ после тайм-аута Bot API; вызывается вместо
    отказа при перезапуске и после сбоя MTProto (код-ревью 07.11.2026)."""
    cfg: Config = ctx.application.bot_data["cfg"]
    app = ctx.application
    bd = app.bot_data
    msg = update.message
    size_text = bigfile.mb(size) if size else _("размер неизвестен")
    if job is not None and job.cancelled:  # отмена между Bot API и этим входом
        await _end_cancelled(ctx, msg, None, label)
        return None
    if retry is not None and bd.get("draining") and not bd.get("stopping"):
        # Маленький файл перезапуск не держит: Bot API, как без тайм-аута.
        got = await _retry_bot_api(retry, msg, None, label, target, chat_key, job=job,
                                   say_failure=True)
        if job is not None and job.cancelled:
            await _end_cancelled(ctx, msg, None, label)
            return None
        return got
    if bd.get("draining") or bd.get("stopping"):
        await _say(msg, _("♻️ бот готовится к перезапуску: большой файл не скачиваю, "
                               "пришли его после рестарта"))
        return None
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await _say(msg, _(
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
    job = job or DownloadJob(chat_key=chat_key, generation=slot[0] if slot is not None else None,
                             msg_key=_media_key(msg))
    jobs: dict[int, DownloadJob] = bd.setdefault("downloads", {})
    jobs[id(job)] = job
    progress = bigfile.Progress()
    status = None
    ticker: asyncio.Task | None = None
    try:
        # Маленький файл после тайм-аута Bot API качается секунды: без «⏳»
        # и «✅», иначе альбом дал бы по сообщению на фото (код-ревью 07.11.2026).
        try:
            status = None if retry is not None and not client.busy else await msg.reply_text(
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
            await _end_cancelled(ctx, msg, status, label)
            return None
        except bigfile.FetchError as e:
            log.warning("big download failed chat_key=%s: %s", chat_key, e)
            text = str(e)
        except Exception as e:
            log.exception("big download failed chat_key=%s", chat_key)
            text = _("⚠️ не удалось скачать «{name}»: {err}").format(name=label, err=type(e).__name__)
        else:
            if job.cancelled:  # /cancel успел, пока dl уже кончилась
                await _end_cancelled(ctx, msg, status, label)
                return None
            elapsed = time.monotonic() - progress.started
            log.info("big download done chat_key=%s size=%d sec=%.1f", chat_key, got, elapsed)
            await _quiet_edit(status, _("✅ скачал «{name}», {size} за {sec} с").format(
                name=label, size=bigfile.mb(got), sec=int(elapsed)))
            keep = hold
            return "big"
        if job.cancelled:
            await _end_cancelled(ctx, msg, status, label)
            return None
        if retry is not None and not bd.get("stopping"):
            got_retry = await _retry_bot_api(retry, msg, status, label, target, chat_key, job=job)
            if job.cancelled:
                await _end_cancelled(ctx, msg, status, label)
                return None
            if got_retry is not None:
                return got_retry
        if status is not None:
            await _quiet_edit(status, text)
        else:
            await _say(msg, text)
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


async def _end_cancelled(ctx: ContextTypes.DEFAULT_TYPE, msg: Any, status: Any, label: str) -> None:
    """Единый выход отменённой загрузки. /cancel отвечает сам, здесь только
    статус «отменена»; остановка бота просит прислать файл заново (раньше
    это говорил _dispatch_to_claude, ревью 07.11.2026)."""
    if ctx.application.bot_data.get("stopping"):
        text = _("⚠️ бот перезапускается, сообщение не ушло в Claude: повтори после рестарта")
        if status is not None:
            await _quiet_edit(status, text)
        else:
            await _say(msg, text)
    else:
        await _quiet_edit(status, _("🗑 загрузка «{name}» отменена").format(name=label))


async def _say(msg: Any, text: str) -> None:
    """Ответ о загрузке: сбой отправки не роняет обработчик, иначе места в
    compose и склейке висели бы до 30 с (код-ревью 07.11.2026)."""
    try:
        await msg.reply_text(text)
    except Exception:
        log.warning("download reply failed (ignored)", exc_info=True)


async def _retry_bot_api(
    retry: Callable[[], Awaitable[None]], msg: Any, status: Any, label: str, target: Path,
    chat_key: str, job: DownloadJob | None = None, say_failure: bool = False,
) -> str | None:
    """Ещё раз Bot API с обычными повторами. "small" при успехе; None при
    сбое или отмене. say_failure: сказать о сбое самому (иначе скажет
    вызывающий своим текстом)."""
    log.info("retrying bot api chat_key=%s", chat_key)
    try:
        if job is not None:
            await _cancellable(job, retry())
        else:
            await retry()
    except asyncio.CancelledError:
        if job is None or not job.cancelled:
            raise
        return None
    except Exception:
        log.exception("bot api retry failed chat_key=%s", chat_key)
        if say_failure and not (job is not None and job.cancelled):
            await _say(msg, _("⚠️ не удалось скачать «{name}»").format(name=label))
        return None
    if job is not None and job.cancelled:  # выход по отмене делает вызывающий
        return None
    await _quiet_edit(status, _("✅ скачал «{name}», {size}").format(
        name=label, size=bigfile.mb(target.stat().st_size)))
    return "small"


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

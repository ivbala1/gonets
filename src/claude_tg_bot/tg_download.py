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

from . import bigfile, bot_api_server, tg_api
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


async def _progress_ticker(status: Any, progress: bigfile.Progress, label: str,
                           size_text: str | None = None) -> None:
    """Ход большой загрузки раз в _PROGRESS_EVERY_SEC. MTProto: проценты.
    Свой сервер (size_text задан): пока сервер качает, хода нет (длина его
    недокачанного файла не равна скачанному), только секунды; копия с его
    диска в процентах."""
    last = ""
    while True:
        await asyncio.sleep(_b._PROGRESS_EVERY_SEC)
        if progress.stopped:
            return
        if progress.phase == "copy" and progress.total:
            text = _("⏳ копирую «{name}» с диска сервера: {pct}%").format(
                name=label, pct=progress.percent)
        elif progress.total and progress.phase != "server":
            text = _("⏳ качаю «{name}», {size}: {pct}%").format(
                name=label, size=bigfile.mb(progress.total), pct=progress.percent)
        elif size_text is not None:
            text = _("⏳ качаю «{name}», {size}: {sec} с").format(
                name=label, size=size_text, sec=int(time.monotonic() - progress.started))
        else:
            continue
        if text != last:
            progress.editing = True
            try:
                await _quiet_edit(status, text)
            finally:
                progress.editing = False
            last = text
            if progress.stopped:
                return


_TICKER_EDIT_WAIT = 15.0


async def _stop_ticker(ticker: asyncio.Task | None, progress: bigfile.Progress) -> None:
    """Остановить ход до итоговой строки. Спящий тикер отменяется сразу;
    правку, уже ушедшую в сеть, отмена не отзывает (сервер её выполнит и она
    ляжет после «✅»), поэтому её ждём до ответа, тикер выходит по флагу
    (перепроверка ревью 09.11.2026). Отмена нас самих проходит наружу."""
    if ticker is None:
        return
    progress.stopped = True
    if progress.editing:
        await asyncio.wait({ticker}, timeout=_TICKER_EDIT_WAIT)
    ticker.cancel()
    await asyncio.wait({ticker})


def _first_status(label: str, size_text: str, busy: bool) -> str:
    return (_("⏳ «{name}», {size}: жду, пока докачается другой файл").format(name=label, size=size_text)
            if busy else _("⏳ качаю «{name}», {size}").format(name=label, size=size_text))


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
    # Отменённая загрузка тоже занята, пока не вышла: поток копии своего
    # сервера ещё пишет .part той же цели (второе мнение 08.11.2026).
    jobs: dict[int, DownloadJob] = app.bot_data.get("downloads") or {}
    return any(j.msg_key == key for j in jobs.values())


async def _fetch_attachment(
    update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, *, file_obj: Any, size: int | None,
    target: Path, label: str, chat_key: str, gslot: _b.GatherSlot | None,
    slot: _b.Reservation | None, hold: bool, fail_text: str | None = None,
    max_bytes: int | None = None,
) -> str | None:
    """Скачать вложение или голосовое в target.

    Свой сервер Bot API (TELEGRAM_BOT_API_URL на loopback): любой размер через
    getFile, см. _fetch_local. Облако: до 20 МБ через Bot API (getFile),
    больше через MTProto под токеном бота. Неизвестный размер идёт через
    Bot API, отказ «File is too big» переводит на MTProto, тайм-аут Bot API
    тоже (если MTProto настроен), а сбой MTProto после тайм-аута ещё раз
    пробует Bot API. max_bytes: предел вида вложения (по умолчанию
    DOCUMENT_MAX_SIZE_BYTES), повторно проверяется по ответу сервера.

    "small"/"big" при успехе, None при сбое или отмене (Ивану уже сказано словами).
    "big" с hold=True: вызывающий держит единицу active_work (перезапуск ждёт
    файл до Claude) и обязан её вернуть или передать пачке склейки.
    """
    msg = update.message
    if _on_local_server(ctx):
        cfg: Config = ctx.application.bot_data["cfg"]
        return await _fetch_local(
            update, ctx, file_obj=file_obj, size=size, target=target, label=label,
            chat_key=chat_key, gslot=gslot, slot=slot, hold=hold, fail_text=fail_text,
            max_bytes=cfg.document_max_size_bytes if max_bytes is None else max_bytes)
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
    retry: Callable[[], Awaitable[None]] | None = None, status: Any = None,
    quiet: bool = False,
) -> str | None:
    """retry: файл меньше 20 МБ после тайм-аута Bot API; вызывается вместо
    отказа при перезапуске и после сбоя MTProto (код-ревью 07.11.2026).
    status: сообщение о загрузке, уже показанное своим сервером: его правим,
    второе не шлём. quiet: мелкий файл, который не отдал свой сервер: без
    «⏳» и «✅», как после тайм-аута в облаке."""
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
        await _say_or_edit(msg, status, _drain_text())
        return None
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await _say_or_edit(msg, status, _(
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
    ticker: asyncio.Task | None = None
    try:
        first = _first_status(label, size_text, client.busy)
        if status is not None:
            await _quiet_edit(status, first)
        else:
            # Маленький файл после тайм-аута Bot API качается секунды: без «⏳»
            # и «✅», иначе альбом дал бы по сообщению на фото (код-ревью 07.11.2026).
            try:
                status = (None if (retry is not None or quiet) and not client.busy
                          else await msg.reply_text(first))
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
            try:
                got = await dl
            finally:
                await _stop_ticker(ticker, progress)
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
        await _say_or_edit(msg, status, text)
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
        await _say_or_edit(msg, status, _(
            "⚠️ бот перезапускается, сообщение не ушло в Claude: повтори после рестарта"))
    else:
        await _quiet_edit(status, _("🗑 загрузка «{name}» отменена").format(name=label))


def _drain_text() -> str:
    return _("♻️ бот готовится к перезапуску: большой файл не скачиваю, "
             "пришли его после рестарта")


async def _say_or_edit(msg: Any, status: Any, text: str) -> None:
    if status is not None:
        await _quiet_edit(status, text)
    else:
        await _say(msg, text)


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


# ---- свой сервер Bot API (--local) ----
# getFile там отвечает, когда сервер сам скачал файл из Telegram (любой
# размер), и отдаёт путь на диске; копию бот делает сам (bigfile.copy_local),
# не PTB: тот копирует синхронно и на сотнях МБ остановил бы весь бот.
# Тайм-аут getFile почти всегда значит «сервер ещё качает», поэтому один
# общий срок на всю операцию, короткие повторы только на сбой связи.
_LOCAL_SMALL_DEADLINE = 300.0
_LOCAL_RETRY_DELAYS = _DOWNLOAD_RETRY_DELAYS


def _on_local_server(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    return tg_api.on_local_server(ctx.application.bot_data["cfg"])


async def _local_get(file_obj: Any, target: Path, *, size: int | None, big: bool,
                     max_bytes: int, min_free: int, read_timeout: float,
                     progress: bigfile.Progress) -> int:
    """getFile своего сервера и копия в target. FetchError: сказать словами,
    MTProto не поможет (предел, место). Прочее: сервер не отдал файл."""
    if big and size:
        # Сервер кладёт свою копию на тот же диск: место под обе.
        bigfile.check_disk(target.parent, 2 * size, min_free)
    delays = iter(_LOCAL_RETRY_DELAYS)
    while True:
        try:
            tg_file = await file_obj.get_file(read_timeout=read_timeout)
            break
        except BadRequest:
            raise
        except NetworkError as e:
            delay = next(delays, None)
            if delay is None:
                raise
            log.warning("local getFile failed (%s), retry in %.0fs", type(e).__name__, delay)
            await asyncio.sleep(delay)
    reported = getattr(tg_file, "file_size", None)
    if max_bytes > 0 and reported and reported > max_bytes:
        raise bigfile.FetchError(bigfile.limit_text(reported, max_bytes))
    src = bigfile.server_file(getattr(tg_file, "file_path", None), bot_api_server.WORK_DIR, reported)
    real = src.stat().st_size
    if max_bytes > 0 and real > max_bytes:
        raise bigfile.FetchError(bigfile.limit_text(real, max_bytes))
    bigfile.check_disk(target.parent, real, min_free if real > bigfile.BOT_API_MAX_BYTES else 0)
    progress.phase = "copy"
    return await bigfile.copy_local(src, target, real, progress)


async def _fetch_local(
    update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, *, file_obj: Any, size: int | None,
    target: Path, label: str, chat_key: str, gslot: _b.GatherSlot | None,
    slot: _b.Reservation | None, hold: bool, fail_text: str | None, max_bytes: int,
) -> str | None:
    """Вложение любого размера через свой сервер. Больше 20 МБ и неизвестный
    размер («большой»): сообщение о ходе, единица active_work (перезапуск
    ждёт; с hold=True вызывающий её держит дальше, как у _fetch_big), запас
    диска, очередь по одному. Мелкий идёт молча. Сервер не отдал файл
    (срок, сбой связи, отказ, путь не годится): MTProto, если настроен."""
    cfg: Config = ctx.application.bot_data["cfg"]
    bd = ctx.application.bot_data
    msg = update.message
    big = size is None or size > bigfile.BOT_API_MAX_BYTES
    size_text = bigfile.mb(size) if size else _("размер неизвестен")
    if big and (bd.get("draining") or bd.get("stopping")):
        await _say(msg, _drain_text())
        return None
    # Проверка draining выше и счёт работы без await между ними.
    if big:
        bd["active_work"] = bd.get("active_work", 0) + 1
    job = DownloadJob(chat_key=chat_key, generation=slot[0] if slot is not None else None,
                      msg_key=_media_key(msg))
    jobs: dict[int, DownloadJob] = bd.setdefault("downloads", {})
    jobs[id(job)] = job
    # Склейка и /go ждут файл всё время: очередь, getFile, повторы, копия.
    holders = tuple(h for h in _slot_holders(ctx, chat_key, gslot, slot) if h is not None)
    for h in holders:
        h.slow += 1
    keep = False
    status = None
    tickers: list[asyncio.Task] = []
    progress = bigfile.Progress()
    queue: asyncio.Semaphore = bd.setdefault("local_big_queue", asyncio.Semaphore(1))
    deadline = float(cfg.big_download_timeout_sec) if big else _LOCAL_SMALL_DEADLINE
    fallback: Exception | None = None

    async def run() -> int:
        progress.started = time.monotonic()
        progress.phase = "server"
        if big and status is not None:
            tickers.append(asyncio.ensure_future(_progress_ticker(status, progress, label, size_text)))
        return await asyncio.wait_for(
            _local_get(file_obj, target, size=size, big=big, max_bytes=max_bytes,
                       min_free=cfg.download_min_free_bytes, read_timeout=deadline,
                       progress=progress), deadline)

    async def queued() -> int:
        if not big:
            return await run()
        async with queue:
            if status is not None and queue_was_busy:
                await _quiet_edit(status, _first_status(label, size_text, False))
            return await run()

    try:
        queue_was_busy = big and queue.locked()
        if big:
            try:
                status = await msg.reply_text(_first_status(label, size_text, queue_was_busy))
            except Exception:
                log.debug("download status send failed (ignored)", exc_info=True)
        try:
            try:
                got = await _cancellable(job, queued())
            finally:
                for t in tickers:
                    await _stop_ticker(t, progress)
        except asyncio.CancelledError:
            if not job.cancelled:
                raise
            log.info("download cancelled chat_key=%s", chat_key)
            await _end_cancelled(ctx, msg, status, label)
            return None
        except bigfile.FetchError as e:
            if job.cancelled:
                await _end_cancelled(ctx, msg, status, label)
                return None
            log.warning("local download refused chat_key=%s: %s", chat_key, e)
            await _say_or_edit(msg, status, str(e))
            return None
        except Exception as e:  # noqa: BLE001 любой отказ сервера: запасной путь ниже
            if job.cancelled:
                await _end_cancelled(ctx, msg, status, label)
                return None
            fallback = e
        else:
            if job.cancelled:  # /cancel успел, пока задача уже кончилась
                await _end_cancelled(ctx, msg, status, label)
                return None
            if not big:
                return "small"
            elapsed = time.monotonic() - progress.started
            log.info("local download done chat_key=%s size=%d sec=%.1f", chat_key, got, elapsed)
            await _quiet_edit(status, _("✅ скачал «{name}», {size} за {sec} с").format(
                name=label, size=bigfile.mb(got), sec=int(elapsed)))
            keep = hold
            return "big"
    finally:
        for t in tickers:
            t.cancel()
        jobs.pop(id(job), None)
        for h in holders:
            h.slow -= 1
        if big and not keep:
            bd["active_work"] -= 1
    # Сервер не отдал файл: MTProto идёт к Telegram своим путём. Слоты и
    # работа выше уже отданы, _fetch_big берёт их заново.
    reason = "timeout" if isinstance(fallback, TimeoutError) else type(fallback).__name__
    if _mtproto_configured(ctx) and not bd.get("stopping") and (big or not bd.get("draining")):
        log.warning("local bot api did not give the file chat_key=%s (%s), using mtproto",
                    chat_key, reason)
        return await _b._fetch_big(update, ctx, size=size, target=target, label=label,
                                   chat_key=chat_key, gslot=gslot, slot=slot, hold=hold,
                                   status=status, quiet=not big)
    log.warning("local bot api did not give the file chat_key=%s (%s)", chat_key, reason,
                exc_info=fallback)
    if isinstance(fallback, TimeoutError):
        text = _("⚠️ «{name}» не скачался за {min} мин, пришли его ещё раз").format(
            name=label, min=max(1, int(deadline // 60)))
    else:
        text = fail_text or _("⚠️ не удалось скачать «{name}»: {err}").format(name=label, err=reason)
    await _say_or_edit(msg, status, text)
    return None


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

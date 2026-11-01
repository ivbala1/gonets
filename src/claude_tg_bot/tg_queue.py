"""Очередь сообщений: склейка кусков и пачка реплик (перенесено из bot.py, шаг 13 разбиения 2)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from telegram.ext import ContextTypes

from .config import Config
from .i18n import _, N_

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


# Склейка: пересылка с комментарием приходит от Telegram отдельными сообщениями
# почти разом (сначала комментарий, потом пересланное), альбом приходит по
# сообщению на фото. Без склейки комментарий уезжал отдельным раном без самого
# пересланного, а пересланное получало «claude ещё работает над прошлым запросом».
# Цена: каждый запрос стартует на GATHER_WINDOW_SEC позже. 0 = выключено.
# 3 с с 20.10.2026: при 1,5 с вложение большой пересылки, которое телефон
# догружает после текста, приходило отдельным запросом (жалоба Ивана).
GATHER_WINDOW_SEC = 3.0
# Новая пачка темы, открытая не позже стольких секунд после прошлого куска,
# пишется в журнал с разрывом: по этим строкам окно подбирается по фактам.
GATHER_LATE_LOG_SEC = 30.0
# Дольше пачку не держим: ни ради скачиваемого или распознаваемого куска, ни
# при непрерывном потоке сообщений.
GATHER_MAX_WAIT_SEC = 30.0


@dataclass(eq=False)  # сравнение и хеш по объекту: пачки лежат в множествах ожидания
class GatherBatch:
    """Сообщения одной темы, пришедшие почти разом; уходят одним промптом.

    update первого сообщения: на него ответ и 👀, остальным 👀 ставит флаш.
    sealed: пачку забрали (флаш перед отправкой или «!»-поправка); кусок, не
    успевший к этому, обработчик шлёт сам, а новые сообщения открывают
    следующую пачку. cancelled: пачку отменил /cancel, опоздавший кусок
    выбрасывается, иначе отменённый запрос воскрес бы после скачивания.
    slow: идущие большие загрузки (MTProto). Пачка с ними ждёт их до конца,
    а не GATHER_MAX_WAIT_SEC; после тишины её состав закрывается (closed),
    и новые сообщения темы открывают следующую пачку, которая уйдёт после
    этой (prev, done). work: сколько единиц active_work пачка держит за
    докачанные файлы (перезапуск их ждёт); admitted: такой файл принят до
    ожидания перезапуска и доводится до Claude.
    """
    update: _b.Update
    ctx: Any
    items: list[str | None] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)
    voice: bool = False
    last_arrival: float = 0.0
    arrived: float = field(default_factory=time.time)  # порядок среди отложенных
    sealed: bool = False
    cancelled: bool = False
    slow: int = 0
    closed: bool = False
    work: int = 0
    admitted: bool = False
    prev: "GatherBatch | None" = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def ready(self) -> bool:
        return all(x is not None for x in self.items)

    @property
    def pieces(self) -> list[str]:
        return [x for x in self.items if x]


GatherSlot = tuple[GatherBatch, int]


def _gather_reserve(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> GatherSlot | None:
    """Занять место в пачке ДО скачивания и распознавания (как _compose_reserve).

    Первое сообщение открывает пачку и запускает её отправку по тишине.
    None = склейка выключена, обработчик работает обычным путём.
    """
    # Во время stop() новая пачка не нужна: задачу, созданную после начала
    # остановки, PTB уже не ждёт. Обработчик шлёт кусок сам, как до склейки.
    if _b.GATHER_WINDOW_SEC <= 0 or not ctx.application.running:
        return None
    batches: dict[str, GatherBatch] = ctx.application.bot_data.setdefault("gather_batches", {})
    batch = batches.get(chat_key)
    if batch is None or batch.sealed:
        tails: dict[str, GatherBatch] = ctx.application.bot_data.setdefault("gather_tails", {})
        prev = tails.get(chat_key)
        if prev is not None and prev.last_arrival:
            gap = time.monotonic() - prev.last_arrival
            if gap <= GATHER_LATE_LOG_SEC:
                log.info("gather: new batch %.1fs after previous piece (window %.1fs) chat=%s",
                         gap, _b.GATHER_WINDOW_SEC, chat_key)
        batch = GatherBatch(update=update, ctx=ctx,
                            prev=prev if prev is not None and not prev.done.is_set() else None)
        batches[chat_key] = batch
        tails[chat_key] = batch
        # create_task приложения, а не asyncio: исключение флаша дойдёт до
        # _on_error с этим update (пользователь увидит ошибку), а stop() при
        # рестарте дождётся задачи вместо того, чтобы бросить её на полпути.
        ctx.application.create_task(
            _gather_flush(chat_key, batch), update=update, name=f"gather:{chat_key}",
        )
    else:
        batch.messages.append(update.message)
    batch.items.append(None)
    batch.last_arrival = time.monotonic()
    return (batch, len(batch.items) - 1)


def _gather_fill(slot: GatherSlot, piece: str, *, voice: bool = False, work: bool = False) -> bool:
    """Положить кусок в занятое место. False: пачку уже забрали без него,
    обработчик шлёт кусок обычным путём, не теряя его. work: кусок держит
    единицу active_work (докачанный большой файл), она переходит к пачке."""
    batch, idx = slot
    if batch.cancelled:
        return True  # /cancel: кусок выбрасывается вместе с пачкой
    if batch.sealed:
        return False
    batch.items[idx] = piece
    batch.voice = batch.voice or voice
    if work:
        batch.work += 1
        batch.admitted = True
    return True


def _gather_release(slot: GatherSlot | None) -> None:
    """Освободить место, если кусок не доехал (сбой скачивания, STT, лимит)."""
    if slot is None:
        return
    batch, idx = slot
    if not batch.sealed and batch.items[idx] is None:
        batch.items[idx] = ""  # пустое = «места нет», при сборке отфильтруется


def _gather_pending(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> bool:
    """В теме собирается пачка сообщений Ивана (склейка или /compose), ещё не
    ушедшая в Claude."""
    bd = ctx.application.bot_data
    batch = bd.get("gather_batches", {}).get(chat_key)
    buf = bd.get("compose_buffers", {}).get(chat_key)
    # Пустое место ("") это кусок, который не доехал: отменять в нём нечего.
    return (batch is not None and not batch.sealed and not batch.cancelled
            and any(x != "" for x in batch.items)) or (
        buf is not None and not buf.sealed and any(x != "" for x in buf.items))


def _gather_take(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> GatherBatch | None:
    """Забрать ожидающую пачку темы сейчас; её флаш после этого ничего не шлёт."""
    batches: dict[str, GatherBatch] = ctx.application.bot_data.setdefault("gather_batches", {})
    batch = batches.pop(chat_key, None)
    if batch is not None:
        batch.sealed = True
    return batch


def _gather_cancel(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> int:
    """/cancel до отправки: выбросить ожидающую пачку и пачки, закрытые в
    ожидании большой загрузки. Сколько сообщений в них было."""
    n = 0
    batch = _gather_take(ctx, chat_key)
    if batch is not None:
        batch.cancelled = True
        n += len(batch.items)
    waiting: dict[str, set[GatherBatch]] = ctx.application.bot_data.get("gather_waiting") or {}
    for b in waiting.pop(chat_key, set()):
        b.cancelled = True
        b.sealed = True
        n += len(b.items)
    return n


def _gather_close(app: Any, chat_key: str, batch: GatherBatch) -> None:
    """Закрыть состав пачки, ждущей большую загрузку: новые сообщения темы
    идут следующей пачкой, /cancel находит эту в gather_waiting."""
    batch.closed = True
    batches: dict[str, GatherBatch] = app.bot_data.setdefault("gather_batches", {})
    if batches.get(chat_key) is batch:
        batches.pop(chat_key, None)
    app.bot_data.setdefault("gather_waiting", {}).setdefault(chat_key, set()).add(batch)


async def _gather_react(messages: list[Any]) -> None:
    for m in messages:
        try:
            await m.set_reaction("👀")
        except Exception:
            log.debug("set_reaction failed (ignored)", exc_info=True)


async def _busy_notice(msg: Any) -> Any:
    try:
        return await msg.reply_text(
            _("⏳ ещё отвечаю на прошлое: новое уйдёт одним следующим сообщением. "
              "«стоп» остановит, «!текст» прервёт и сразу отправит поправку")
        )
    except Exception:
        log.exception("busy notice failed")
        return None


async def _drop_busy_notice(sent: Any) -> None:
    """Очередь дошла до запроса: уведомление «встанет в очередь» больше не нужно."""
    if sent is None:
        return
    try:
        await sent.delete()
    except Exception:
        log.debug("busy notice delete failed (ignored)", exc_info=True)


# Пачка хода (этап 2 очереди, решение Ивана 29.10.2026, вариант Б): текст
# Ивана и ответы кнопкой, пришедшие, пока тема занята, уходят одним следующим
# ходом с рамкой, а не каждый своим ходом с пересказом сказанного. Везёт пачку
# первый такой запрос (он ждёт замка темы), остальные дописываются к ней и
# ждут конца её хода: так их учёт активной работы и отметка «ответ в очереди»
# живут до ответа.
PACK_NOTE = N_(
    "[Написано, пока ты работал над прошлым запросом: уточнения и ответы к нему. "
    "Не пересказывай то, что уже сказал, ответь одним сообщением на всё ниже.]\n\n")


@dataclass
class PackItem:
    text: str
    msg: Any
    voice: bool = False
    # Ответ кнопкой: вариант вопроса и эпоха сессии, под которую он сочинён.
    ask_ref: dict | None = None
    expect_epoch: int | None = None
    # Работа принята до ожидания перезапуска (докачанный файл склейки).
    admitted: bool = False
    # Запись отложенного, которую везёт это сообщение (_drained_claim).
    carried: str | None = None
    # Записи state/inflight.json этого сообщения: отложенное их называет.
    rids: list[str | None] = field(default_factory=list)


@dataclass(eq=False)
class TurnPack:
    items: list[PackItem] = field(default_factory=list)
    # taken: везущий запрос дошёл до замка и забрал пачку, новые сообщения
    # открывают следующую. cancelled: пачку снял /cancel или «стоп», и после
    # взятия, пока процесса ещё нет (ревью Codex 29.10). started: процесс
    # claude хода пачки создан, дальше отмена идёт обычным прерыванием.
    taken: bool = False
    cancelled: bool = False
    started: bool = False
    # Принятые в ход ответы кнопкой: им пометка о снятом проекте, если ход не начался.
    picks: list[dict] = field(default_factory=list)
    # Ответы кнопкой, получившие свой отказ, и пачка, перенесённая лимитом в
    # отложенный запуск: им «не ушло» не говорить.
    refused: list[PackItem] = field(default_factory=list)
    deferred: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


def _turn_packs(ctx: ContextTypes.DEFAULT_TYPE) -> dict[str, TurnPack]:
    return ctx.application.bot_data.setdefault("turn_packs", {})


@dataclass(eq=False)
class RunPrestart:
    """Ран без пачки под замком темы, процесса claude ещё нет. cancelled
    ставит /cancel: процесс гасится сразу после создания, до промпта."""
    cancelled: bool = False


def _prestart_cancel(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> int:
    """Отметить отмену рана, чей процесс ещё не создан. 1, если такой есть."""
    entry = (ctx.application.bot_data.get("run_prestart") or {}).get(chat_key)
    if entry is None or entry.cancelled:
        return 0
    entry.cancelled = True
    _b._mark_user_stop(ctx, chat_key)  # ответ снятого рана второй раз не нужен
    return 1


def _pack_cancel(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> int:
    """Снять ждущую пачку темы или взятую, чей процесс ещё не создан.
    Возвращает число снятых сообщений."""
    count = 0
    pack = _turn_packs(ctx).pop(chat_key, None)
    if pack is not None and not pack.taken:
        pack.cancelled = True
        pack.done.set()
        count += len(pack.items)
        for item in pack.items:
            _b._pending_text_taken(ctx, chat_key, item.text)  # правка снятого не ищет двойника
    taken = ctx.application.bot_data.get("turn_packs_taken", {}).pop(chat_key, None)
    if taken is not None and not taken.started and not taken.cancelled:
        # Присоединённые ждут конца везущего: done ставит его finally.
        taken.cancelled = True
        count += len(taken.items)
    return count


async def _pack_prompt(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, chat_key: str,
                       pack: TurnPack) -> tuple[str, bool, list[dict]] | None:
    """Промпт пачки под замком темы: (текст с рамкой, голосом ли ответ,
    варианты принятых ответов кнопкой). Ответ кнопкой прежней сессии
    получает отказ и в пачку не идёт. None: идти нечему. Эпоха сверки та же,
    что везущий ставит в expect_epoch: смену после неё ловит поздняя сверка."""
    # Сверка всех без await между ними, отказы после (ревью Codex 29.10).
    epoch = _b._session_epoch(ctx, chat_key)
    kept: list[PackItem] = []
    refused: list[PackItem] = []
    for item in pack.items:
        if item.ask_ref is not None and (
                item.expect_epoch != epoch or _b._session_will_change(ctx, cfg, chat_key, item.expect_epoch)):
            refused.append(item)
        else:
            kept.append(item)
    pack.picks = [i.ask_ref for i in kept if i.ask_ref is not None]
    pack.refused = refused
    for item in refused:
        with contextlib.suppress(Exception):
            await item.msg.reply_text(_b._ask_pick_refusal(item.ask_ref))
    if not kept:
        return None
    if len(pack.items) > 1:
        log.info("turn pack %d messages into one request chat=%s", len(kept), chat_key)
    text = _(PACK_NOTE) + "\n\n---\n\n".join(i.text for i in kept)
    return text, any(i.voice for i in kept), list(pack.picks)


async def _gather_flush(chat_key: str, batch: GatherBatch) -> None:
    """Ждёт GATHER_WINDOW_SEC тишины и готовности всех кусков, шлёт пачку одним
    ходом. Не дольше GATHER_MAX_WAIT_SEC, даже если сообщения всё идут; пачку
    с большой загрузкой ждёт до её конца (срок у самой загрузки)."""
    app = batch.ctx.application
    try:
        await _gather_flush_impl(chat_key, batch)
    finally:
        batch.done.set()
        waiting: dict[str, set[GatherBatch]] = app.bot_data.get("gather_waiting") or {}
        waiting.get(chat_key, set()).discard(batch)
        if not waiting.get(chat_key, True):
            waiting.pop(chat_key, None)
        tails: dict[str, GatherBatch] = app.bot_data.get("gather_tails") or {}
        if tails.get(chat_key) is batch:
            tails.pop(chat_key, None)
        if batch.work:
            app.bot_data["active_work"] = app.bot_data.get("active_work", 0) - batch.work
            batch.work = 0


async def _gather_stop_defer(chat_key: str, batch: GatherBatch) -> None:
    """Остановка застала пачку в склейке: готовые куски ложатся в отложенные и
    уйдут после старта, а куски, ещё не докачанные и не распознанные, просят
    повторить. Отложить нельзя (нечего, диск): отказ, как раньше."""
    refusal = _("⚠️ бот перезапускается, сообщение не ушло в Claude: повтори после рестарта")
    first = batch.update.message
    messages = [first, *batch.messages]
    replies = [(first, refusal)]
    if batch.pieces and batch.ctx.application.bot_data.get("at_jobs") is not None:
        try:
            text = _b._drain_defer(batch.ctx, batch.update, _b._DRAIN_JOINER.join(batch.pieces),
                                voice=batch.voice, arrived=batch.arrived)
        except OSError:
            log.exception("gathered request not deferred on shutdown chat=%s", chat_key)
        else:
            ready = next((m for m, piece in zip(messages, batch.items) if piece), first)
            replies = [(ready, text), *((m, refusal) for m, piece in zip(messages, batch.items)
                                        if piece is None)]
    log.warning("gathered request stopped by shutdown chat=%s deferred=%s", chat_key,
                replies[0][1] is not refusal)
    for target, line in replies:
        with contextlib.suppress(Exception):
            await target.reply_text(line)


async def _gather_flush_impl(chat_key: str, batch: GatherBatch) -> None:
    ctx = batch.ctx
    app = ctx.application
    deadline = time.monotonic() + _b.GATHER_MAX_WAIT_SEC
    while not batch.sealed and app.running:
        now = time.monotonic()
        wait = batch.last_arrival + _b.GATHER_WINDOW_SEC - now
        if wait <= 0 and batch.ready:
            break
        if now >= deadline and not batch.slow:
            break
        if batch.slow and not batch.closed and (wait <= 0 or now >= deadline):
            _gather_close(app, chat_key, batch)
        if batch.closed:
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(max(min(max(wait, 0.2), deadline - now), 0.05))
    if batch.sealed:
        return  # пачку забрала «!»-поправка или выбросил /cancel
    batch.sealed = True
    batches: dict[str, GatherBatch] = app.bot_data.setdefault("gather_batches", {})
    if batches.get(chat_key) is batch:
        batches.pop(chat_key, None)
    if not app.running or app.bot_data.get("stopping"):
        # Бот останавливается: новый ран всё равно оборвал бы рестарт.
        # cancelled, чтобы и докачавшийся позже кусок не ушёл в Claude сам;
        # уведомление нужно, даже если ни один кусок ещё не готов.
        batch.cancelled = True
        await _gather_stop_defer(chat_key, batch)
        return
    # Пачка, закрытая раньше в ожидании большой загрузки, уходит первой:
    # порядок сообщений темы сохраняется.
    prev = batch.prev
    batch.prev = None
    if prev is not None and not prev.done.is_set():
        # Пока ждёт, видна /cancel в gather_waiting: иначе отмена снимала
        # только прежнюю, а эта уходила следом (аудит очереди 31.10.2026).
        waiting_now: dict[str, set[GatherBatch]] = app.bot_data.setdefault("gather_waiting", {})
        waiting_now.setdefault(chat_key, set()).add(batch)
        await prev.done.wait()
    if batch.cancelled:
        return
    pieces = batch.pieces
    if not pieces:
        return
    if batch.messages:
        # Реакции фоном: пока они ждут сеть, опоздавший кусок успел бы
        # отправиться сам и обогнать пачку.
        app.create_task(_gather_react(batch.messages))
    if len(pieces) > 1:
        log.info("gathered %d messages into one request chat=%s", len(pieces), chat_key)
    # Следующая пачка темы ждёт эту только до очереди замка, а не до конца её
    # рана: до входа в замок в _dispatch_to_claude нет ни одного await, порядок
    # дальше держит FIFO замка. Иначе сообщение, написанное во время долгого
    # рана, вставало за кнопками и событиями, пришедшими позже (разбор 22.10.2026).
    # Из gather_waiting пачка уходит тогда же: /cancel её уже не снимет.
    batch.done.set()
    waiting: dict[str, set[GatherBatch]] = app.bot_data.get("gather_waiting") or {}
    waiting.get(chat_key, set()).discard(batch)
    await _b._dispatch_to_claude(
        batch.update, ctx, "\n\n---\n\n".join(pieces), send_voice_reply=batch.voice,
        **({"admitted": True} if batch.admitted else {}),
    )


def _gather_send_now(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> None:
    """Отправить ожидающую пачку сразу, не дожидаясь тишины. Перед /compose:
    иначе текст, пришедший до /compose, ушёл бы позже содержимого буфера."""
    batch = _gather_take(ctx, chat_key)
    if batch is None or not batch.pieces:
        return
    app = ctx.application
    if batch.messages:
        app.create_task(_gather_react(batch.messages))
    app.create_task(
        _b._dispatch_to_claude(
            batch.update, batch.ctx, "\n\n---\n\n".join(batch.pieces),
            send_voice_reply=batch.voice,
        ),
        update=batch.update,
    )


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

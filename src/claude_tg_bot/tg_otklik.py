"""Кнопки откликов (otklik.py) в Telegram (перенесено из bot.py, шаг 3 разбиения)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, ReplyParameters, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from . import buttons, otklik, tg_api
from .approval import AUTO_ENTITIES, entity_rows, keyboard_rows
from .config import Config
from .i18n import _

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


def _otklik_markup(rec: dict) -> InlineKeyboardMarkup | None:
    return buttons.markup(otklik.buttons(rec))


_OTKLIK_SHOW_LOCKS: dict[str, asyncio.Lock] = {}


def _otklik_matches(rec: dict, msg: Any) -> bool:
    """Сообщение отклика такое, каким его показал бот: текст как в ответе
    Telegram при показе (иначе по записи) без различий в пробелах, те же
    кнопки, сущности только те, что Telegram находит сам (без разметки)."""
    shown = getattr(msg, "text", None)
    if not isinstance(shown, str) or " ".join(shown.split()) != " ".join(otklik.shown_text(rec).split()):
        return False
    if keyboard_rows(getattr(msg, "reply_markup", None)) != keyboard_rows(_otklik_markup(rec)):
        return False
    return all(e[0] in AUTO_ENTITIES for e in entity_rows(getattr(msg, "entities", None)))


async def _otklik_edit(bot: Any, rec: dict) -> str | None:
    """Правка сообщения по записи. None: не прошла; иначе текст сообщения из
    ответа Telegram (эталон сверки на ✅) или пусто, если его нет."""
    try:
        msg = await bot.edit_message_text(
            otklik.render(rec), chat_id=rec["chat_id"], message_id=rec["message_id"],
            reply_markup=_otklik_markup(rec), link_preview_options=LinkPreviewOptions(is_disabled=True))
    except BadRequest as e:
        if not tg_api.not_modified_text(str(e)):
            log.warning("otklik edit failed aid=%s: %s", rec.get("id"), e)
            return None
        return ""
    except Exception:
        log.warning("otklik edit failed aid=%s", rec.get("id"), exc_info=True)
        return None
    text = getattr(msg, "text", None)
    return text if isinstance(text, str) else ""


async def _otklik_show(bot: Any, rec: dict, base: Path | None = None) -> None:
    """Сообщение отклика по записи: текст со статусом и кнопки статуса.
    С base показ идёт по свежему чтению записи, по одному на запись, и
    отмечается в ней; запись сменилась во время правки: показать ещё раз.
    Правка не прошла: синхронизация после рана перерисует (ревью Codex 22.10.2026)."""
    if not rec.get("message_id") or rec.get("chat_id") is None:
        return
    if base is None:
        await _otklik_edit(bot, rec)
        return
    lock = _OTKLIK_SHOW_LOCKS.setdefault(rec["id"], asyncio.Lock())
    async with lock:
        for _try in range(3):
            cur = await asyncio.to_thread(otklik.load, base, rec["id"]) or rec
            shown = await _otklik_edit(bot, cur)
            if shown is None:
                return
            try:
                if await asyncio.to_thread(otklik.mark_shown, base, cur["id"], cur["status"],
                                           otklik.gen_of(cur), shown or None):
                    return
            except Exception:
                log.warning("otklik mark shown failed aid=%s", cur.get("id"), exc_info=True)
                return


async def _otklik_sync(bot: Any, base: Path) -> None:
    """Старт бота: сообщения откликов, отставшие от записи, перерисовать.
    С паузой: при первом старте их может быть несколько десятков."""
    try:
        targets = await asyncio.to_thread(otklik.sync_targets, base)
    except Exception:
        log.exception("otklik: sync scan failed")
        return
    for rec in targets:
        await _otklik_show(bot, rec, base)
        await asyncio.sleep(0.5)


async def _otklik_unknown_notice(bot: Any, rec: dict, base: Path) -> None:
    """Неясный исход отправки: перерисовать сообщение и сказать отдельной
    строкой со звуком, правка сообщения звука не даёт (ревью 22.10.2026)."""
    await _otklik_show(bot, rec, base)
    await _otklik_loud(bot, rec, _("❓ Отклик «{title}»: ран кончился без итога отправки, результат неизвестен. "
                                   "Проверь канал; если не ушло, ↩️ на сообщении вернёт кнопки.").format(
        title=rec.get("title")))


async def _otklik_close_attempts(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, chat_key: str) -> None:
    """Конец рана чата, под его замком: взятые им отклики без итога получают
    «результат неизвестен» (otklik.close_attempts). Сообщения правятся фоном,
    заодно отставшие от записи (правка из команды агента не прошла)."""
    try:
        base = otklik.otklik_dir(cfg.state_dir)
        if not base.is_dir():
            return
        changed = await asyncio.to_thread(otklik.close_attempts, base, chat_key)
    except Exception:
        log.exception("otklik: closing attempts failed chat=%s", chat_key)
        return
    for rec in changed:
        log.warning("otklik %s: run ended without result, outcome unknown", rec["id"])
        ctx.application.create_task(_otklik_unknown_notice(ctx.bot, rec, base))
    ctx.application.create_task(_otklik_sync(ctx.bot, base))


def _otklik_target(ctx: ContextTypes.DEFAULT_TYPE, rec: dict, update: Update) -> Update:
    """Кого будит ✅: тема откликов, без неё чат, где отклик готовили
    (otklik.sender_of). Сообщение с кнопкой лежит в личке; если отправляет
    она же, ран отвечает на само сообщение."""
    sender = otklik.sender_of(rec)
    msg = update.callback_query.message
    if sender == str(msg.chat_id):
        return _b.Update(update_id=update.update_id, message=msg)
    return _b._synthetic_update(ctx.bot, *_b._parse_chat_key(sender))


def _otklik_queued(ctx: ContextTypes.DEFAULT_TYPE, what: str, sender: str) -> None:
    """Журнал очереди отправки: сколько ранов этого чата впереди (идёт и ждут,
    счёт _counted_lock). Только чтение: строка журнала отправку не срывает."""
    log.info("otklik queued %s chat=%s ahead=%d", what, sender, _b._chat_load(ctx.application.bot_data, sender))


async def on_otklik_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки отклика (otklik.py): ✅ будит агента отправить ровно этот текст,
    ✏️ просит правку ответом, ⏭ только отмечает пропуск, ✋ отменяет одобрение
    до захвата, ↩️ возвращает кнопки после проверки канала. Кнопка привязана к
    хешу текста, поколению и своему сообщению: старая или чужая не действует.
    Переходы под замком записей (otklik.pick), вне цикла событий: его держит
    и команда агента."""
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    if user is None or user.id not in cfg.allowed_user_ids:
        return
    base = otklik.otklik_dir(cfg.state_dir)
    parsed = otklik.parse_callback(query.data or "")
    msg = query.message
    try:
        if parsed is None or msg is None:
            raise otklik.Refused("bad callback")
        oid, sha, gen, action = parsed
        if action == "o":
            # ✅ только по сообщению, которое нарисовал бот: текст, кнопки и
            # сущности. Агент знает ключ бота и мог переписать сообщение или
            # подписи кнопок (проект 03.11.2026, п. 4). Старая кнопка сверку не
            # проходит: её отклонит pick как устаревшую.
            cur = await asyncio.to_thread(otklik.load, base, oid)
            if otklik.pick_ok(cur, sha, gen, action, msg.chat_id, msg.message_id) and not _otklik_matches(cur, msg):
                log.warning("otklik pick aid=%s: сообщение не то, что нарисовал бот", oid)
                await _b._answer_quietly(query, _("Сообщение не совпадает с откликом (текст или кнопки): ✅ не "
                                                  "принят. Перерисовал, проверь и нажми ещё раз"), show_alert=True)
                await _otklik_show(ctx.bot, cur, base)
                return
        rec = await asyncio.to_thread(otklik.pick, base, oid, sha, gen, action,
                                      msg.chat_id, msg.message_id, getattr(cfg, "otklik_chat_key", ""))
    except otklik.Refused as e:
        await _b._answer_quietly(query, str(e) if isinstance(e, otklik.Late)
                                 else _("Кнопка устарела: этот отклик уже решён или заменён"), show_alert=True)
        # Кнопки по нынешнему состоянию, а не снять все: на том же сообщении
        # могут стоять кнопки нового поколения.
        cur = otklik.load(base, parsed[0]) if parsed else None
        if cur is not None and msg is not None and msg.message_id == cur.get("message_id"):
            await _otklik_show(ctx.bot, cur, base)
        else:
            await buttons.strip_buttons(query)
        return
    except (OSError, ValueError) as e:
        log.warning("otklik save failed: %s", e)
        await _b._answer_quietly(query, _("Не записалось, см. журнал бота"), show_alert=True)
        return
    # Время нажатия для разбора недели: ожидание кнопки до старта рана, доли ✏️ и ⏭.
    log.info("otklik pick aid=%s action=%s source=%s sender=%s", rec["id"], action, otklik.source_of(rec),
             otklik.sender_of(rec))
    if action != "o":
        await _b._answer_quietly(query, {"e": _("Жду правку"), "s": _("Пропускаю"),
                                      "c": _("Отменил, кнопки вернулись"),
                                      "r": _("Кнопки вернулись")}[action])
        # ⏭ только пишет статус: пропуск в воронку переносят утро, вечер и
        # поиск по `otklik list --status skipped`, отдельный ран не нужен
        # (разбор 22.10.2026).
        if rec.get("at"):
            # ✋ снял одобрение в срок: его задание из очереди /at тоже.
            _drop_otklik_jobs(ctx.application, rec["id"])
        await _otklik_show(ctx.bot, rec, base)
        return
    ts = otklik.at_ts(rec)
    if ts is not None and ts > time.time():
        # Отправка в срок: ✅ это «ок» на текст, адресата и время; ран будит очередь /at.
        await _otklik_schedule(ctx, query, rec, base, ts)
        return

    async def prologue() -> None:
        await _b._answer_quietly(query, _("Отправляю"))
        await _otklik_show(ctx.bot, rec, base)
    await _otklik_run(ctx, cfg, base, rec, lambda: _otklik_target(ctx, rec, update), prologue)


async def _otklik_run(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, base: Path, rec: dict,
                      target: Callable[[], Update], prologue: Any = None, *, keep_if_rejected: bool = False,
                      **extra: Any) -> bool | None:
    """Ран отправки одобренного отклика (по ✅ или в его срок). Одобренное, которое
    ран не взял, снова с кнопками, а прошедшее окно срока пропущенным со строкой
    Ивану; keep_if_rejected: отказ из-за перезапуска оставляет его одобренным
    (запуск в срок лежит в очереди /at и уйдёт после рестарта). target зовётся
    внутри try: сбой разбора чата тоже возвращает кнопки. Возвращает ответ
    _dispatch_to_claude: True отказ до старта."""
    oid = rec["id"]
    gen = otklik.gen_of(rec)

    def still_approved() -> bool:
        # Ран по ✅ по правилу промта Домового доотправляет все одобренные:
        # нажатие, ждавшее замка за ним, иначе стало бы холостым раном «уже
        # ушло». Запись не прочиталась: ран идёт, `otklik take` откажет сам,
        # а молча потерять отправку хуже лишнего рана (ревью 22.10.2026).
        # Окно срока прошло, пока ран ждал тему: агента не будить (finally ниже).
        cur = otklik.load(base, oid)
        return cur is None or (cur.get("status") == "approved" and not _at_late(cur))

    reason = _("агент не взял текст")
    rejected: bool | None = None
    # В фоновой теме (отклики, мастерская) ✅ идёт свежей сессией: задание по
    # кнопке самодостаточно, а продолжение сессии поиска перечитывало бы
    # 100-230 тысяч токенов чужого контекста (разбор 22.10.2026; выбор /model
    # до конца сессии свежая сессия снимает). Исключение: отложенный после
    # лимита повтор этой темы продолжает её сессию. Проверка идёт под замком
    # темы: пока кнопка ждала, повтор мог появиться (ревью Codex 23.10.2026).
    sender = otklik.sender_of(rec)

    def fresh() -> bool:
        return _b._is_background(cfg, sender) and not _b._limit_retry_pending(ctx, sender)
    back = None
    try:
        if prologue is not None:
            await prologue()
        _otklik_queued(ctx, f"aid={oid}", sender)
        rejected = await _b._dispatch_to_claude(
            target(), ctx, otklik.approved_prompt(rec, base),
            send_voice_reply=False, from_callback=True, still_wanted=still_approved, fresh=fresh, **extra,
        )
        if rejected:
            reason = _("бот перезапускается")
    except Exception:
        log.exception("otklik dispatch failed aid=%s", oid)
        reason = _("не удалось запустить агента")
    finally:
        # Решение не должно пропасть молча: одобренное, которое ран не взял
        # (отказ, лимит, рестарт, сбой), снова с кнопками. Взятое без итога
        # бот уже пометил «неизвестно» в конце рана, отправленное не трогается.
        # В finally: отмена задачи не оставит запись одобренной до рестарта.
        stopping = keep_if_rejected and ctx.application.bot_data.get("stopping")
        expired = False
        if _at_late(rec):
            # Окно срока кончилось, пока ран ждал (замок темы): ✅ на возвращённых
            # кнопках ответила бы только «срок прошёл», поэтому сразу failed.
            expired = await _otklik_expire(ctx, base, rec)
        elif not (keep_if_rejected and (rejected is True or stopping)):
            # Запуск в срок, отменённый остановкой бота (ждал тему): запись и
            # задание остаются, после рестарта он уйдёт, как у прочих /at.
            try:
                back = otklik.revert_untaken(base, oid, gen, reason)
            except Exception:
                log.exception("otklik revert failed aid=%s", oid)
    if back is not None:
        log.info("otklik %s: approved but not taken, buttons back (%s)", oid, reason)
        await _otklik_show(ctx.bot, back, base)
        if keep_if_rejected:
            # Отправка в срок идёт без Ивана: правка карточки звука не даёт.
            # Причина отказа до старта (вход, лимит) ушла в тему откликов.
            await _otklik_loud(ctx.bot, back, _("⚠️ Отклик «{title}» в срок {when} не ушёл: {reason}, подробности "
                                                "в теме откликов. Кнопки вернулись.").format(
                title=back.get("title"), when=otklik.at_label(back), reason=reason))
    elif keep_if_rejected and not expired and rejected is not True and not stopping:
        cur = await asyncio.to_thread(otklik.load, base, oid)
        if cur is not None and otklik.gen_of(cur) == gen and cur.get("status") == "failed":
            # Агент взял текст и отметил сбой (канал, окно кончилось до grant).
            await _otklik_loud(ctx.bot, cur, _("⚠️ Отклик «{title}» в срок {when} не ушёл: агент отметил "
                                               "сбой отправки.").format(title=cur.get("title"),
                                                                       when=otklik.at_label(cur)))
    return rejected


async def _otklik_loud(bot: Any, rec: dict, text: str, quiet: bool = False) -> None:
    """Строка ответом на карточку отклика, со звуком (quiet: без), правка карточки звука не даёт."""
    if not rec.get("message_id") or rec.get("chat_id") is None:
        return
    try:
        await bot.send_message(rec["chat_id"], text, disable_notification=quiet, reply_parameters=ReplyParameters(
            message_id=rec["message_id"], allow_sending_without_reply=True))
    except Exception:
        log.exception("otklik %s: loud line failed", rec.get("id"))


def _at_late(rec: dict) -> bool:
    """У отклика есть срок, и его окно уже закрыто (или срок не читается)."""
    if not rec.get("at"):
        return False
    ts = otklik.at_ts(rec)
    return otklik.at_refusal(rec) is not None and (ts is None or ts <= time.time())


async def _otklik_expire(ctx: ContextTypes.DEFAULT_TYPE, base: Path, rec: dict) -> bool:
    """Одобренное в срок, чьё окно прошло без отправки: failed, строка Ивану
    со звуком ответом на карточку. Взятое раном (sending) не трогается.
    True: запись помечена здесь."""
    oid = rec["id"]
    note = _("не ушло: срок {when} прошёл").format(when=otklik.at_label(rec))
    try:
        back = await asyncio.to_thread(otklik.expire_scheduled, base, oid, otklik.gen_of(rec), note)
    except Exception:
        log.exception("otklik %s: expire failed", oid)
        return False
    if back is None:
        return False
    log.warning("otklik %s: scheduled send expired (%s)", oid, otklik.at_label(rec))
    await _otklik_show(ctx.bot, back, base)
    await _otklik_loud(ctx.bot, back, _(
        "⌛ Отклик «{title}» не ушёл: срок {when} прошёл (бот, ПК или очередь темы не успели). "
        "Нужно, поставь заново с новым временем.").format(title=back.get("title"), when=otklik.at_label(back)),
        # Окно уже закрыто, сделать до утра нечего: ночью без звука.
        quiet=otklik.quiet_now())
    return True


async def _otklik_schedule(ctx: ContextTypes.DEFAULT_TYPE, query: Any, rec: dict, base: Path, ts: float) -> None:
    """✅ на отклике со временем до срока: запуск в очередь /at (at_jobs.json,
    переживает перезапуск), ран не будится. Запись остаётся одобренной с ✋."""
    oid, gen, sender = rec["id"], otklik.gen_of(rec), otklik.sender_of(rec)
    _drop_otklik_jobs(ctx.application, oid)
    try:
        chat_id, thread_id = _b._parse_chat_key(sender)
        jid = _b.at_put(ctx.application, {
            "chat_key": sender, "chat_id": chat_id, "thread_id": thread_id, "fire_at": ts,
            "prompt": _("📨 отклик {id} «{title}»: отправка в срок по ✅").format(
                id=oid, title=str(rec.get("title"))[:60]),
            "otklik": oid, "otklik_gen": gen})
    except Exception:
        log.exception("otklik %s: scheduling failed", oid)
        back = None
        try:
            back = await asyncio.to_thread(otklik.revert_untaken, base, oid, gen,
                                           _("не поставилось в очередь, см. журнал бота"))
        except Exception:
            log.exception("otklik %s: revert failed", oid)
        await _b._answer_quietly(query, _("Не поставилось в очередь, см. журнал бота"), show_alert=True)
        if back is not None:
            await _otklik_show(ctx.bot, back, base)
        return
    log.info("otklik %s: scheduled jid=%s at=%s sender=%s", oid, jid, otklik.at_label(rec), sender)
    await _b._answer_quietly(query, _("Уйдёт {when}").format(when=otklik.at_label(rec)))
    await _otklik_show(ctx.bot, rec, base)


def _drop_otklik_jobs(app: Any, oid: str) -> None:
    """Снять из очереди /at прежние запуски в срок этого отклика (после ✋ и
    перед новым ✅): иначе `/at` темы показывал бы по одному на каждое одобрение."""
    store = app.bot_data.get("at_jobs")
    if store is None:
        return
    for jid, entry in list(store.data.items()):
        try:
            mine = json.loads(entry["v"]).get("otklik") == oid
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if mine and jid not in app.bot_data.get("at_running", ()):
            for j in app.job_queue.get_jobs_by_name(f"at:{jid}"):
                j.schedule_removal()
            store.remove(jid)


async def otklik_at_fire(ctx: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    """Срок отклика, одобренного заранее: ран отправки, как по ✅. Запись
    сменилась (✋, новое ✅, правка): запуск снимается молча. Окно прошло (бот
    или ПК не работали): отклик пропущен, строка Ивану со звуком."""
    app = ctx.application
    cfg: Config = app.bot_data["cfg"]
    store = app.bot_data["at_jobs"]
    jid, oid = data["jid"], str(data.get("otklik") or "")
    gen = int(data.get("otklik_gen") or 0)
    base = otklik.otklik_dir(cfg.state_dir)
    rec = await asyncio.to_thread(otklik.load, base, oid)
    if rec is None or rec.get("status") != "approved" or otklik.gen_of(rec) != gen or not rec.get("at"):
        log.info("otklik %s: scheduled send dropped, record changed (jid=%s)", oid, jid)
        store.remove(jid)
        return
    ts = otklik.at_ts(rec)
    if ts is not None and ts > time.time():
        # Очередь сработала раньше срока (часы): дождаться его.
        _b._schedule_at_job(app, jid, {**{k: v for k, v in data.items() if k != "jid"}, "fire_at": ts})
        return
    if _at_late(rec):
        store.remove(jid)
        await _otklik_expire(ctx, base, rec)
        return
    log.info("otklik %s: scheduled send fires (jid=%s)", oid, jid)
    rejected = None
    try:
        rejected = await _otklik_run(
            ctx, cfg, base, rec, lambda: _b._synthetic_update(ctx.bot, *_b._parse_chat_key(otklik.sender_of(rec))),
            keep_if_rejected=True,
                                     on_run_start=lambda: store.remove(jid), deferred=True)
    finally:
        bd = app.bot_data
        if bd.get("stopping"):
            pass  # остановка оборвала ожидание темы: задание на диске уйдёт после рестарта
        elif rejected is True and jid in store.data:
            # Отказ из-за перезапуска: запись в очереди и одобренная, уйдёт после
            # рестарта; заявку отменили, пока шёл отказ: вернуть в очередь сейчас.
            if not bd.get("stopping") and not bd.get("draining"):
                _b._schedule_at_job(app, jid, {k: v for k, v in data.items() if k != "jid"})
        else:
            store.remove(jid)


async def otklik_at_cancelled(ctx: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    """/at cancel снял отправку отклика в срок: это ✋, одобрение снимается,
    карточка снова с кнопками (иначе обещала бы «уйдёт», а не ушло бы)."""
    try:
        data = json.loads(raw)
        oid, gen = str(data.get("otklik") or ""), int(data.get("otklik_gen") or 0)
    except (ValueError, TypeError, AttributeError):
        return
    if not oid:
        return
    base = otklik.otklik_dir(ctx.application.bot_data["cfg"].state_dir)
    try:
        # Без пометки: отмена намеренная, а пометка рисует «не ушло, нажми ✅ ещё раз».
        back = await asyncio.to_thread(otklik.revert_untaken, base, oid, gen, "")
    except Exception:
        log.exception("otklik %s: revert after /at cancel failed", oid)
        return
    if back is not None:
        log.info("otklik %s: scheduled send cancelled by /at cancel", oid)
        await _otklik_show(ctx.bot, back, base)


async def on_otklik_all_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """«✅ Все N» под пачкой откликов: каждая запись проходит ту же сверку
    хеша и поколения, что её ✅, один ран отправляет всё одобренное и
    отчитывается одним итогом. Решённое отдельно пропускается, текст ни одного
    отклика кнопка не несёт (решение Ивана 01.11.2026)."""
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    if user is None or user.id not in cfg.allowed_user_ids:
        return
    base = otklik.otklik_dir(cfg.state_dir)
    bid = otklik.parse_batch_callback(query.data or "")
    msg = query.message
    try:
        if bid is None or msg is None:
            raise otklik.Refused("bad callback")
        picked = await asyncio.to_thread(otklik.pick_all, base, bid, msg.chat_id, msg.message_id,
                                         getattr(cfg, "otklik_chat_key", ""))
    except (otklik.Refused, OSError, ValueError) as e:
        log.warning("otklik batch refused: %s", e)
        await _b._answer_quietly(query, _("Кнопка устарела: эта пачка уже решена"), show_alert=True)
        await buttons.strip_buttons(query)
        return
    log.info("otklik batch %s picked=%d", bid, len(picked))
    if not picked:
        await _b._answer_quietly(query, _("Кнопка устарела: эта пачка уже решена"), show_alert=True)
        await buttons.strip_buttons(query)
        return
    gens = {rec["id"]: otklik.gen_of(rec) for rec in picked}
    first, sender = picked[0], otklik.sender_of(picked[0])

    def still_approved() -> bool:
        return any((cur := otklik.load(base, oid)) is None or cur.get("status") == "approved" for oid in gens)

    def fresh() -> bool:
        return _b._is_background(cfg, sender) and not _b._limit_retry_pending(ctx, sender)

    reason = _("агент не взял текст")
    try:
        await _b._answer_quietly(query, _("Отправляю: {count}").format(count=len(picked)))
        await buttons.note_on_card(query, getattr(msg, "text", None) or "", _("✅ одобрено {count}, отправляю").format(count=len(picked)),
                                   "otklik batch", parse_mode=None)
        for rec in picked:
            await _otklik_show(ctx.bot, rec, base)
        _otklik_queued(ctx, f"batch={bid} n={len(picked)}", sender)
        if await _b._dispatch_to_claude(
            _otklik_target(ctx, first, update), ctx, otklik.approved_prompt(first, base),
            send_voice_reply=False, from_callback=True, still_wanted=still_approved, fresh=fresh,
        ):
            reason = _("бот перезапускается")
    except Exception:
        log.exception("otklik batch dispatch failed batch=%s", bid)
        reason = _("не удалось запустить агента")
    finally:
        back = []
        for oid, gen in gens.items():
            try:
                back.append(otklik.revert_untaken(base, oid, gen, reason))
            except Exception:
                log.exception("otklik revert failed aid=%s", oid)
    for rec in filter(None, back):
        log.info("otklik %s: approved but not taken, buttons back (%s)", rec["id"], reason)
        await _otklik_show(ctx.bot, rec, base)


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

"""Кнопки откликов (otklik.py) в Telegram (перенесено из bot.py, шаг 3 разбиения)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
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
    if not rec.get("message_id") or rec.get("chat_id") is None:
        return
    with contextlib.suppress(Exception):
        await bot.send_message(
            rec["chat_id"],
            _("❓ Отклик «{title}»: ран кончился без итога отправки, результат неизвестен. "
              "Проверь канал; если не ушло, ↩️ на сообщении вернёт кнопки.").format(title=rec.get("title")),
            reply_parameters=ReplyParameters(message_id=rec["message_id"], allow_sending_without_reply=True))


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
    """Кого будит ✅: чат, где отклик готовили. Сообщение с кнопкой лежит в
    личке; если источник она же, ран отвечает на само сообщение."""
    source = otklik.source_of(rec)
    msg = update.callback_query.message
    if source == str(msg.chat_id):
        return _b.Update(update_id=update.update_id, message=msg)
    chat, _sep, thread = source.partition(":")
    return _b._synthetic_update(ctx.bot, int(chat), int(thread) if thread else None)


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
                                      msg.chat_id, msg.message_id)
    except otklik.Refused:
        await _b._answer_quietly(query, _("Кнопка устарела: этот отклик уже решён или заменён"),
                              show_alert=True)
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
    log.info("otklik pick aid=%s action=%s source=%s", rec["id"], action, otklik.source_of(rec))
    if action != "o":
        await _b._answer_quietly(query, {"e": _("Жду правку"), "s": _("Пропускаю"),
                                      "c": _("Отменил, кнопки вернулись"),
                                      "r": _("Кнопки вернулись")}[action])
        # ⏭ только пишет статус: пропуск в воронку переносят утро, вечер и
        # поиск по `otklik list --status skipped`, отдельный ран не нужен
        # (разбор 22.10.2026).
        await _otklik_show(ctx.bot, rec, base)
        return
    gen = otklik.gen_of(rec)

    def still_approved() -> bool:
        # Ран по ✅ по правилу промта Домового доотправляет все одобренные:
        # нажатие, ждавшее замка за ним, иначе стало бы холостым раном «уже
        # ушло». Запись не прочиталась: ран идёт, `otklik take` откажет сам,
        # а молча потерять отправку хуже лишнего рана (ревью 22.10.2026).
        cur = otklik.load(base, oid)
        return cur is None or cur.get("status") == "approved"

    reason = _("агент не взял текст")
    # В мастерской ✅ идёт свежей сессией: задание по кнопке самодостаточно,
    # а продолжение сессии поиска перечитывало бы 100-230 тысяч токенов
    # чужого контекста (разбор 22.10.2026, решение Ивана: модель мастерской
    # по умолчанию, Opus; выбор /model до конца сессии свежая сессия снимает).
    # Исключение: отложенный после лимита повтор этой темы продолжает её
    # сессию. Проверка идёт под замком темы: пока кнопка ждала, повтор мог
    # появиться (ревью Codex 23.10.2026).
    source = otklik.source_of(rec)

    def fresh() -> bool:
        return _b._is_workshop(cfg, source) and not _b._limit_retry_pending(ctx, source)
    try:
        await _b._answer_quietly(query, _("Отправляю"))
        await _otklik_show(ctx.bot, rec, base)
        if await _b._dispatch_to_claude(
            _otklik_target(ctx, rec, update), ctx, otklik.approved_prompt(rec, base),
            send_voice_reply=False, from_callback=True, still_wanted=still_approved, fresh=fresh,
        ):
            reason = _("бот перезапускается")
    except Exception:
        log.exception("otklik dispatch failed aid=%s", oid)
        reason = _("не удалось запустить агента")
    finally:
        # Решение не должно пропасть молча: одобренное, которое ран не взял
        # (отказ, лимит, рестарт, сбой), снова с кнопками. Взятое без итога
        # бот уже пометил «неизвестно» в конце рана, отправленное не трогается.
        # В finally: отмена задачи не оставит запись одобренной до рестарта.
        try:
            back = otklik.revert_untaken(base, oid, gen, reason)
        except Exception:
            log.exception("otklik revert failed aid=%s", oid)
            back = None
    if back is not None:
        log.info("otklik %s: approved but not taken, buttons back (%s)", oid, reason)
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
        picked = await asyncio.to_thread(otklik.pick_all, base, bid, msg.chat_id, msg.message_id)
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
    first, source = picked[0], otklik.source_of(picked[0])

    def still_approved() -> bool:
        return any((cur := otklik.load(base, oid)) is None or cur.get("status") == "approved" for oid in gens)

    def fresh() -> bool:
        return _b._is_workshop(cfg, source) and not _b._limit_retry_pending(ctx, source)

    reason = _("агент не взял текст")
    try:
        await _b._answer_quietly(query, _("Отправляю: {count}").format(count=len(picked)))
        await buttons.note_on_card(query, getattr(msg, "text", None) or "", _("✅ одобрено {count}, отправляю").format(count=len(picked)),
                                   "otklik batch", parse_mode=None)
        for rec in picked:
            await _otklik_show(ctx.bot, rec, base)
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

"""Карточки вопросов и плана (перенесено из bot.py, шаг 14 разбиения 2)."""
from __future__ import annotations

import contextlib
import logging
import time
import uuid
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from .claude_runner import SessionStore
from .config import Config
from .i18n import _

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


_ASK_BUTTON_CAP = 28


def _ask_button_label(n: int, option: str) -> str:
    """«1 · начало варианта…», не длиннее _ASK_BUTTON_CAP символов."""
    label = f"{n} · {option}"
    return label if len(label) <= _ASK_BUTTON_CAP else label[: _ASK_BUTTON_CAP - 1].rstrip() + "…"


# Тот же вопрос с теми же вариантами, пока прежняя карточка ждёт ответа,
# заново не шлётся (разбор недели W43, предложение 3: 38 повторов за 6 часов
# на 173 вопроса; решение Ивана 25.10.2026).
ASK_REPEAT_HOLD_SEC = 6 * 3600


def _ask_waiting(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, ask: tuple[str, list[str]], *,
                 permission_mode: str, untrusted: bool, epoch: int) -> bool:
    """Карточка этого вопроса с теми же вариантами в этом чате доставлена и
    ждёт ответа: не нажата, та же сессия и тот же режим рана, моложе
    ASK_REPEAT_HOLD_SEC. Нажатая карточка из реестра уходит, после рестарта
    реестр пуст: тогда вопрос шлётся как обычно. Ответ словами карточку не
    гасит: она по-прежнему нажимается, а повторы шли и после слов Ивана
    (решение по предложению 3 разбора W43). epoch: эпоха сессии рана с
    вопросом, снятая под замком темы."""
    question, options = ask
    pending: dict[str, dict] = ctx.application.bot_data.get("ask_options", {})
    cards: dict[str, list[dict]] = {}
    for v in pending.values():
        if (v.get("chat_key") == chat_key and v.get("question") == question and v.get("msg")
                and v.get("epoch") == epoch and v.get("permission_mode") == permission_mode
                and bool(v.get("untrusted")) == untrusted):
            cards.setdefault(v.get("card"), []).append(v)
    now = time.time()
    return any([v.get("answer") for v in vs] == list(options)
               and float(vs[0].get("at") or 0) > now - ASK_REPEAT_HOLD_SEC for vs in cards.values())


def _ask_picks_queued(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> set[str]:
    """Принятые ответы кнопкой этого чата, чей ход ещё не закончился. Ран,
    закончившийся при непустом наборе (кроме своего ответа), задал вопрос, не
    зная ответа Ивана."""
    return ctx.application.bot_data.setdefault("ask_picks_queued", {}).setdefault(chat_key, set())


def _ask_cards_changed(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> int:
    """Поколение карточек чата: растёт при каждой замене и принятом ответе.
    Сбой отправки возвращает прежние карточки, только если поколение то же:
    пустой реестр не значит, что между ними не было другой карточки."""
    gens: dict[str, int] = ctx.application.bot_data.setdefault("ask_card_gen", {})
    gens[chat_key] = gens.get(chat_key, 0) + 1
    return gens[chat_key]


def _take_ask_cards(pending: dict[str, dict], keep: Callable[[dict], bool]) -> list[dict]:
    """Снимает из реестра все варианты карточек, у которых keep(вариант)
    истинно, без единого await: нажатие, пришедшее следом, их уже не найдёт.
    Возвращает по записи на карточку: сообщение, вопрос и варианты по порядку."""
    taken = {t: v for t, v in pending.items() if keep(v)}
    for t in taken:
        pending.pop(t, None)
    return _ask_cards_of(taken)


def _ask_cards_of(options: dict[str, dict]) -> list[dict]:
    """Варианты реестра по карточкам: сообщение, вопрос, варианты по порядку."""
    cards: dict[Any, dict] = {}
    for v in options.values():
        c = cards.setdefault(v.get("card"), {"msg": v.get("msg"), "question": v.get("question", ""),
                                             "options": []})
        c["options"].append(v.get("answer", ""))
    return list(cards.values())


@contextlib.contextmanager
def _ask_answer_queued(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, ref: dict):
    """Ответ на вопрос ждёт замка темы и идёт своим ходом: пока ход не
    кончился, вопрос другого рана сочинён без этого ответа и кнопками не
    уходит (_send_ask_card; случай 29.10.2026: Иван нажал и его, второй ход
    повторил первый). Снимается в конце хода, а не под замком: ран до него
    шлёт карточку уже после замка, и ход ответа успел бы его взять."""
    queued = uuid.uuid4().hex[:8]
    ref["queued"] = queued
    _ask_picks_queued(ctx, chat_key).add(queued)
    try:
        yield
    finally:
        _ask_picks_queued(ctx, chat_key).discard(queued)


async def _retire_ask_cards(ctx: ContextTypes.DEFAULT_TYPE, cards: list[dict], note: str) -> None:
    """Снятые карточки правятся на месте: кнопки убраны, внизу пометка. Без
    отдельного сообщения на каждую замену (ревью Codex 29.10.2026)."""
    for c in cards:
        where = c.get("msg")
        if not where:
            continue
        listing = "\n".join(f"{i}. {opt}" for i, opt in enumerate(c["options"], 1))
        with contextlib.suppress(Exception):
            await ctx.bot.edit_message_text(f"❓ {c['question']}\n\n{listing}\n\n{note}",
                                            chat_id=where[0], message_id=where[1], reply_markup=None)


async def _send_ask_card(
    ctx: ContextTypes.DEFAULT_TYPE, update: _b.Update, chat_key: str,
    ask: tuple[str, list[str]], *, permission_mode: str, epoch: int,
    untrusted: bool = False, own_queued: str | set[str] | None = None,
) -> None:
    """Уточняющий вопрос Claude кнопками. Выбор уходит следующим сообщением
    в ту же сессию — за рулём это тап вместо печати.

    Вместе с ответом запоминаем режим рана и поколение сессии: иначе вопрос,
    заданный внутри /plan, продолжался бы уже в обычном режиме (мутации без
    решения по плану), а кнопка, нажатая после /reset, отвечала бы в чужую
    сессию. Вопрос из рана по чужому тексту (untrusted) продолжается тоже
    только на чтение: вариант ответа сочинён по этому тексту. Фоновость рана
    не запоминается: нажатие делает Иван (on_ask_pick). Эпоха снята под
    замком темы в конце рана: карточка уходит после сетевых ожиданий, и
    /reset за это время дал бы ей эпоху новой сессии (ревью /2mind 25.10.2026).
    """
    question, options = ask
    pending: dict[str, dict] = ctx.application.bot_data.setdefault("ask_options", {})
    # Telegram обрезает подпись кнопки по ширине экрана, и вариант читался
    # наполовину. Полный текст вариантов идёт в самом сообщении с номерами,
    # кнопка несёт номер и начало: выбор виден целиком до нажатия.
    listing = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, 1))
    # Вопрос, сочинённый без ответа Ивана из очереди или в прежней сессии,
    # уходит без кнопок и ничьих карточек не гасит: карточка идёт уже после
    # замка темы, и за это время мог прийти ответ или /reset (ревью 29.10.2026).
    own = {own_queued} if isinstance(own_queued, str) else set(own_queued or ())
    if _ask_picks_queued(ctx, chat_key) - own:
        stale = _("без кнопок: ты уже ответил на прежний вопрос, ответ в очереди")
    elif epoch != _b._session_epoch(ctx, chat_key):
        stale = _("без кнопок: сессия темы сменилась")
    else:
        stale = ""
    if stale:
        log.info("ask without buttons chat=%s", chat_key)
        with contextlib.suppress(Exception):
            await update.message.reply_text(f"❓ {question}\n\n{listing}\n\n({stale})")
        return
    # Один текущий вопрос на чат (проект Домового 22.10.2026, 1.5; случай
    # 29.10.2026: тот же вопрос другими словами дал две живые карточки, Иван
    # нажал обе): любая прежняя карточка этого чата гаснет, а не только с тем
    # же текстом, в том числе от фонового рана: варианты остаются в тексте,
    # ответить можно словами. Вопросы дел живут в своём реестре и не задеваются.
    # Снимаются до отправки, без await: иначе старую нажимали, пока шла новая,
    # и обе давали ход. Сбой отправки возвращает их.
    old = {t: v for t, v in pending.items() if v.get("chat_key") == chat_key}
    for t in old:
        pending.pop(t, None)
    gen = _ask_cards_changed(ctx, chat_key)
    # Токен вместо текста: callback_data ограничена 64 байтами.
    rows = []
    card_id = uuid.uuid4().hex[:8]
    for opt in options:
        token = uuid.uuid4().hex[:8]
        pending[token] = {
            "chat_key": chat_key,
            "answer": opt,
            "question": question,
            "permission_mode": permission_mode,
            "untrusted": untrusted,
            "epoch": epoch,
            "card": card_id,
            "at": time.time(),
        }
        rows.append([InlineKeyboardButton(_ask_button_label(len(rows) + 1, opt),
                                          callback_data=f"ask:{token}")])
    try:
        sent = await update.message.reply_text(
            f"❓ {question}\n\n{listing}", reply_markup=InlineKeyboardMarkup(rows),
        )
        if isinstance(getattr(sent, "message_id", None), int):
            where = (sent.chat_id, sent.message_id, getattr(sent, "message_thread_id", None))
            mine = [v for v in pending.values() if v.get("card") == card_id]
            for v in mine:
                v["msg"] = where
            if not mine:
                # Пока карточка отправлялась, её уже сменила следующая (отправка
                # идёт после замка темы): без сообщения та её не поправила.
                await _retire_ask_cards(ctx, [{"msg": where, "question": question, "options": list(options)}],
                                        _("↓ вопрос заменён новым ниже"))
    except Exception:
        log.exception("ask card failed")
        for t, v in list(pending.items()):
            if v.get("card") == card_id:
                pending.pop(t, None)
        if ctx.application.bot_data.get("ask_card_gen", {}).get(chat_key) == gen:
            pending.update(old)
            return
    await _retire_ask_cards(ctx, _ask_cards_of(old), _("↓ вопрос заменён новым ниже"))


ASK_TTL_SEC = 12 * 3600


def _card_msg_matches(picked: dict, message: Any) -> bool:
    """Нажатие пришло под тем сообщением, куда ушла карточка. Токен живёт в
    callback_data, и подставленный под другое сообщение разрешённого чата он
    иначе сработал бы там (проект шага 1, 2.4). Карточка без запомненного
    сообщения (не доставилась с message_id) не сверяется."""
    where = picked.get("msg")
    if not where:
        return True
    # chat.id, а не chat_id: у InaccessibleMessage (старше 48 ч) есть только chat.
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id is None:
        chat_id = getattr(message, "chat_id", None)
    return message is not None and chat_id == where[0] and getattr(message, "message_id", None) == where[1]


def _ask_pick_refusal(picked: dict) -> str:
    """Отказ называет вопрос и ответ: «сессия сменилась» без них не давало
    понять, какая кнопка не сработала (задача Ивана 23.10.2026)."""
    text = _("⚠️ сессия темы сменилась, ответ «{answer}» на вопрос «{question}» не отправлен: "
             "если он ещё нужен, напиши его текстом").format(
        answer=picked.get("answer", ""), question=picked.get("question", ""))
    if picked.get("dropped_plan"):
        # Нажатие сняло проект сразу, как текст Ивана (on_ask_pick).
        text += "\n" + _("Проект решения снят, как после твоего сообщения; ответь текстом.")
    return text


async def _dropped_plan_note(msg: Any, ask_ref: dict | None) -> None:
    """Нажатие уже сняло проект темы задачи (on_ask_pick), а ход отвергнут
    лимитом или входом: сказать, что проекта больше нет."""
    if ask_ref is not None and ask_ref.get("dropped_plan"):
        with contextlib.suppress(Exception):
            await msg.reply_text(_("Проект решения снят, как после твоего сообщения; ответь текстом."))


async def on_ask_pick(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None:
        return
    # Чат проверяется, как у текста: кнопка из группы вне разрешённых не
    # должна отвечать в сессию (проект шага 1, 2.4).
    if not _b._is_allowed(cfg, update):
        return
    token = (query.data or "").split(":", 1)[-1]
    pending: dict[str, dict] = ctx.application.bot_data.setdefault("ask_options", {})
    if token in pending and not _card_msg_matches(pending[token], query.message):
        # Чужое сообщение: вариант остаётся у своей карточки.
        await _b._answer_quietly(query, _("Ответ не принят: кнопка не от этого вопроса."), show_alert=True)
        return
    picked = pending.pop(token, None)
    if picked is not None:
        # Соседние варианты гаснут вместе с выбранным, до первого await:
        # иначе второй вариант, нажатый следом, проходил проверку и запускал
        # второй ход (ревью Codex 29.10.2026).
        _take_ask_cards(pending, lambda v: v.get("card") == picked.get("card"))
    if picked is not None and picked.get("at") is not None and time.time() - float(picked["at"]) > ASK_TTL_SEC:
        # Карточка пережила полдня (и сжатие сессии, эпоха при нём та же):
        # ответ на неё уже не о том (проект 22.10.2026, 1.5).
        picked = None
    # Сверка эпохи одна на всё нажатие: /reset во время await ниже не должен
    # дать снять проект, а потом отказать (ревью 25.10.2026).
    same_session = picked is not None and picked.get("epoch") == _b._session_epoch(
        ctx, str(picked.get("chat_key", "")))
    if picked is not None and same_session:
        # Ответ кнопкой в теме задачи снимает проект сразу, до первого await:
        # иначе «Делай» успел бы выполнить снимаемое (ревью Codex 16.10).
        # Кнопка сменившейся сессии ниже получит отказ и проект не трогает
        # (ревью /2mind 25.10.2026).
        ask_chat, _sep, ask_thread = str(picked.get("chat_key", "")).partition(":")
        with contextlib.suppress(ValueError):
            act, _was, _snap = _b._task_touch_now(ctx, int(ask_chat), int(ask_thread) if ask_thread else None, None)
            if act in ("dropped", "cancelled"):
                picked["dropped_plan"] = True
    if picked is None:
        # Реестр вариантов живёт в памяти: после рестарта бота кнопка молча
        # теряла ответ. Ответ на нажатие один, поэтому причина в нём, а не
        # после пустого query.answer() (ревью 12.10.2026).
        await _b._answer_quietly(
            query, _("Ответ не принят: вопрос устарел (старше 12 часов, заменён новым, бот "
            "перезапускался) или на него уже ответили. Если нужно, напиши ответ текстом."), show_alert=True,
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if not same_session:
        await _ask_pick_finish(update, ctx, query, picked, same_session)
        return
    # Ответ встаёт в очередь чата до первого await: иначе вопрос другого рана,
    # ушедший в это окно, получал кнопки, и его нажатие давало второй ход
    # (ревью Codex 29.10.2026).
    _ask_cards_changed(ctx, str(picked["chat_key"]))
    with _ask_answer_queued(ctx, str(picked["chat_key"]), picked):
        await _ask_pick_finish(update, ctx, query, picked, same_session)


async def _ask_pick_finish(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, query: Any, picked: dict,
                           same_session: bool) -> None:
    try:
        await query.answer()
    except Exception:
        log.debug("callback answer failed (ignored)", exc_info=True)
    chat_key = picked["chat_key"]
    answer = picked["answer"]
    card = query.message
    if not same_session:
        # Между вопросом и тапом был /reset, /resume или idle-сброс: отвечать
        # в другую сессию бессмысленно и вводит в заблуждение. Новая сессия
        # контекста вопроса не знает, а по его тексту роутер памяти мог бы
        # закрепить её за другим заказчиком (ревью /2mind 25.10.2026).
        try:
            await query.edit_message_text(f"🗳 {card.text}\n\n{_ask_pick_refusal(picked)}")
        except Exception:
            pass
        return
    try:
        await query.edit_message_text(f"🗳 {card.text}\n\n→ {answer}")
    except Exception:
        log.debug("ask card finalize failed (ignored)", exc_info=True)
    # Время принятого нажатия для разбора недели (ожидание кнопки до старта
    # рана); отказ по сроку и по смене сессии сюда не доходит.
    log.info("ask pick chat=%s", chat_key)
    # Ответ уходит вместе с вопросом: без него в сессии, где ран уже завершён,
    # видно только голое «prod» без контекста.
    prompt = _("[Ответ на вопрос «{question}»]: {answer}").format(question=picked.get('question', ''), answer=answer)
    untrusted = bool(picked.get("untrusted"))
    if untrusted:
        prompt = _(_b.UNTRUSTED_PROMPT_NOTE) + prompt
    await _dispatch_ask_answer(update, ctx, card, prompt, picked, untrusted)


async def _dispatch_ask_answer(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE, card: Any, prompt: str,
                               picked: dict, untrusted: bool) -> None:
    # update у callback-апдейта не имеет .message — заворачиваем сообщение
    # карточки, у него реальный message_id и та же тема.
    await _b._dispatch_to_claude(
        _b.Update(update_id=update.update_id, message=card), ctx, prompt,
        send_voice_reply=False,
        # Режим наследуется от рана, задавшего вопрос: уточнение внутри /plan
        # обязано остаться в plan-режиме, а не начать мутации.
        permission_mode=picked.get("permission_mode", "bypassPermissions"),
        # Нажатие делает Иван, поэтому права как у его сообщения, даже если
        # вопрос задал фоновый ран: сессия, где побывал чужой текст, не
        # сбрасывается, как и после его текста (решение Ивана 25.10.2026;
        # 23.10 сброс после сторожа выбросил ответ утреннему плану). Ответ на
        # вопрос из рана по чужому тексту остаётся только на чтение.
        unattended=False,
        from_callback=True,
        untrusted=untrusted,
        # Сессия могла смениться, пока ответ ждал замка темы.
        expect_epoch=picked.get("epoch"),
        ask_ref=picked,
    )


def _plan_generation(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> int:
    """Номер последнего plan-рана темы: растёт под замком темы при старте
    каждого /plan. Одобрение относится к плану своего поколения: старая
    карточка, нажатая во время нового /plan или в очереди за ним, иначе
    выполняла бы новый план (ревью 11.10.2026)."""
    gens: dict[str, int] = ctx.application.bot_data.setdefault("plan_generations", {})
    return gens.get(chat_key, 0)


def _bump_plan_generation(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> None:
    gens: dict[str, int] = ctx.application.bot_data.setdefault("plan_generations", {})
    gens[chat_key] = gens.get(chat_key, 0) + 1


def _plan_snapshot(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> dict:
    """Эпоха, session_id и поколение плана темы на сейчас: для карточки
    плана снимаются под замком темы в конце plan-рана (ревью 11.10.2026)."""
    store: SessionStore | None = ctx.application.bot_data.get("sessions")
    return {
        "epoch": _b._session_epoch(ctx, chat_key),
        "sid": store.get(chat_key) if store is not None else None,
        "plan": _plan_generation(ctx, chat_key),
    }


async def _send_plan_card(
    ctx: ContextTypes.DEFAULT_TYPE, update: _b.Update, chat_key: str,
    snapshot: dict | None = None,
) -> None:
    """Кнопки решения по плану после /plan-рана (он read-only).

    С токеном запоминаются эпоха и session_id: «Выполнять» старой карточки
    после /reset, /resume или смены скоупа уходил в чужую сессию. Новая
    карточка темы гасит прежние: одобрение относится к последнему плану
    (ревью 12.10.2026).
    snapshot: снимок _plan_snapshot под замком в конце рана. Карточка
    отправляется уже после замка, и за задержку отправки ответа новый /plan
    успевал пройти целиком: снятые здесь значения достались бы его плану.
    Устаревший снимок карточку не публикует (ревью 11.10.2026).
    """
    if snapshot is None:
        snapshot = _plan_snapshot(ctx, chat_key)
    if snapshot["plan"] != _plan_generation(ctx, chat_key):
        log.info("plan card skipped chat=%s: newer /plan started", chat_key)
        try:
            await update.message.reply_text(
                _("⚠️ в теме уже начат новый /plan, кнопок у этого плана нет")
            )
        except Exception:
            log.debug("stale plan note failed (ignored)", exc_info=True)
        return
    token = uuid.uuid4().hex[:8]
    pending: dict[str, dict] = ctx.application.bot_data.setdefault("plan_options", {})
    for t, v in list(pending.items()):
        if v.get("chat_key") == chat_key:
            pending.pop(t, None)
    pending[token] = {"chat_key": chat_key, **snapshot}
    rows = [
        [InlineKeyboardButton(_("✅ Выполнять"), callback_data=f"plan:{token}:ok")],
        [
            InlineKeyboardButton(_("✏️ Доработать"), callback_data=f"plan:{token}:more"),
            InlineKeyboardButton(_("❌ Отмена"), callback_data=f"plan:{token}:no"),
        ],
    ]
    try:
        sent = await update.message.reply_text(
            _("План выше. Выполнять?"), reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        log.exception("plan card failed")
        return
    if isinstance(getattr(sent, "message_id", None), int) and token in pending:
        pending[token]["msg"] = (sent.chat_id, sent.message_id)


async def on_plan_pick(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None:
        return
    if not _b._is_allowed(cfg, update):
        return
    parts = (query.data or "").split(":")
    token, decision = (parts[1], parts[2]) if len(parts) >= 3 else ("", "")
    pending: dict[str, dict] = ctx.application.bot_data.setdefault("plan_options", {})
    if token in pending and not _card_msg_matches(pending[token], query.message):
        await _b._answer_quietly(query, _("Решение не принято: кнопка не от этого плана."), show_alert=True)
        return
    try:
        await query.answer()
    except Exception:
        log.debug("callback answer failed (ignored)", exc_info=True)
    picked = pending.pop(token, None)
    if picked is None:
        try:
            await query.edit_message_text(_("⚠️ решение по плану устарело — повтори /plan"))
        except Exception:
            pass
        return
    store: SessionStore | None = ctx.application.bot_data.get("sessions")
    if picked["epoch"] != _b._session_epoch(ctx, picked["chat_key"]) or (
        store is not None and store.get(picked["chat_key"]) != picked["sid"]
    ) or picked.get("plan") != _plan_generation(ctx, picked["chat_key"]):
        try:
            await query.edit_message_text(
                _("⚠️ сессия темы сменилась после этого плана, решение не отправлено: повтори /plan")
            )
        except Exception:
            pass
        return
    labels = {"ok": _("✅ выполняю план"), "more": _("✏️ дорабатываю план"), "no": _("❌ план отменён")}
    card = query.message
    try:
        await query.edit_message_text(labels.get(decision, decision))
    except Exception:
        log.debug("plan card finalize failed (ignored)", exc_info=True)
    if decision == "no":
        return
    upd = _b.Update(update_id=update.update_id, message=card)
    if decision == "more":
        await _b._dispatch_to_claude(
            upd, ctx,
            _("Доработай план: уточни то, что осталось неясным, и предложи его снова. "
              "Ничего пока не выполняй."),
            send_voice_reply=False, permission_mode="plan", unattended=True,
            expect_epoch=picked["epoch"], expect_plan=picked.get("plan"),
        )
        return
    # Выполнение: обычный режим, мутации идут через approve-карточки.
    await _b._dispatch_to_claude(
        upd, ctx, _("План одобрен — выполняй его."), send_voice_reply=False,
        expect_epoch=picked["epoch"], expect_plan=picked.get("plan"),
        # Это нажатие, а не текст: лимит не переносит его в /at, где пропали
        # бы сверки эпохи и поколения плана (ревью Codex 26.10.2026).
        from_callback=True,
    )


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

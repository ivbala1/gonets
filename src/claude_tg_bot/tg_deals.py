"""Дела утреннего плана и кнопки подборки новостей в Telegram (перенесено из bot.py, шаг 4 разбиения)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.ext import Application, ContextTypes

from . import deals
from .claude_runner import ClaudeResult
from .config import Config
from .i18n import N_, _
from .skills.news_digest import backlog as news_backlog, core as news

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


# ---- Дела: вопросы утреннего плана (опыт В1, docs/design-2026-10-26-v1.md) ----

DEAL_FIRST_NOTE = N_(
    "[Это дело: ответвление утреннего плана с момента вопроса ниже. Главный разговор "
    "лички идёт отдельно и получит строку итога. Данные Домового могли измениться "
    "после вопроса: перед правкой перечитай их.]\n\n")


def _deals_base(app: Any) -> Path:
    return deals.deals_dir(app.bot_data["cfg"].state_dir)


def _deal_update(app: Any, deal_id: str, fn: Callable[[dict], Any]) -> Any:
    """fn(запись дела) под замком реестра. None: дела нет или реестр не
    читается (это в журнал: ответ тогда не принимается, а не теряется молча)."""
    try:
        with deals.locked(_deals_base(app)) as data:
            deal = data.get(deal_id)
            return None if deal is None else fn(deal)
    except (OSError, ValueError):
        log.exception("deal registry failed id=%s", deal_id)
        return None


async def _send_deal_card(ctx: ContextTypes.DEFAULT_TYPE, msg: Any, deal: dict) -> bool:
    """Карточка вопроса дела: ярлык первой строкой, варианты из реестра на
    диске (кнопка живёт и после рестарта). Прежняя карточка дела гаснет."""
    rows = [[InlineKeyboardButton(_b._ask_button_label(i, opt), callback_data=f"deal:{deal['id']}:{i}")]
            for i, opt in enumerate(deal["options"], 1)]
    listing = "\n".join(f"{i}. {opt}" for i, opt in enumerate(deal["options"], 1))
    try:
        sent = await msg.reply_text(f"{deals.head(deal)}\n❓ {deal['question']}\n\n{listing}",
                                    reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        log.exception("deal card failed id=%s", deal["id"])
        return False
    mid = getattr(sent, "message_id", None)

    def remember(d: dict) -> Any:
        old = d.get("card_msg")
        d["card_msg"] = mid
        return old

    old = _deal_update(ctx.application, deal["id"], remember)
    if old and old != mid:
        with contextlib.suppress(Exception):
            await ctx.bot.edit_message_reply_markup(chat_id=deal["chat_id"], message_id=old, reply_markup=None)
    log.info("deal card id=%s chat=%s", deal["id"], deal["chat_id"])
    return True


async def _deal_open(ctx: ContextTypes.DEFAULT_TYPE, msg: Any, chat_id: int, thread_id: int | None,
                     ask: tuple[str, list[str]], *, qsid: str, quuid: str, qepoch: int) -> bool:
    """Вопрос рана с флагом утра становится делом. False: дел уже DEAL_CAP,
    реестр не пишется или карточка не ушла; тогда вопрос идёт обычной карточкой."""
    try:
        with deals.locked(_deals_base(ctx.application)) as data:
            # Вчерашние дела не занимают места новому утру (ревью 26.10).
            deals.close_stale(data, chat_id, thread_id)
            if any(d.get("chat_id") == chat_id and d.get("thread_id") == thread_id and d.get("state") == "asked"
                   and d.get("question") == ask[0] and deals.is_open(d) for d in data.values()):
                # Тот же вопрос уже ждёт в деле: второе дело не заводится (ревью 26.10).
                log.info("deal skipped why=same chat=%s", chat_id)
                return True
            if deals.open_count(data, chat_id, thread_id) >= deals.DEAL_CAP:
                log.info("deal skipped why=cap chat=%s", chat_id)
                return False
            deal = deals.new_deal(chat_id=chat_id, thread_id=thread_id, qsid=qsid, quuid=quuid,
                                  question=ask[0], options=ask[1])
            # Эпоха сессии вопроса: запасной путь, если ответвиться не вышло.
            deal["qepoch"] = qepoch
            data[deal["id"]] = deal
    except (OSError, ValueError):
        log.exception("deal registry failed, ordinary card chat=%s", chat_id)
        return False
    if await _send_deal_card(ctx, msg, deal):
        return True
    _deal_update(ctx.application, deal["id"], lambda d: d.update(state="closed", error="card"))
    return False


def _deal_finish(app: Any, deal: dict, result: ClaudeResult, reply: str,
                 ask: tuple[str, list[str]] | None) -> dict | None:
    """Итог хода дела в реестр: idle или failed, строка итога для главного
    разговора, новый вопрос того же дела. Запись дела для карточки или None."""
    def finish(d: dict) -> dict | None:
        if d.get("state") != "running":
            return None
        now = time.time()
        log.info("deal done id=%s ok=%d sec=%.0f", d["id"], 0 if result.is_error else 1,
                 now - float(d.get("answered_at") or now))
        if result.is_error:
            d.update(state="failed", error=(result.text or "")[:200], last_at=now)
            return None
        deals.add_note(d, deals.note_text(d, str(d.get("answer") or ""), reply), now)
        if ask is None:
            d.update(state="idle", last_at=now)
            return None
        deals.ask_again(d, ask[0], ask[1], now)
        return dict(d)

    return _deal_update(app, deal["id"], finish)


def _deal_reply_target(ctx: ContextTypes.DEFAULT_TYPE, msg: Any) -> str | None:
    """Дело, на сообщение которого ответили свайпом: сообщение самого бота с
    ярлыком «🧵 <id> · …» в первой строке, дело этого чата. Пересланное это
    чужой текст, а не ответ Ивана."""
    reply = getattr(msg, "reply_to_message", None)
    if reply is None or getattr(msg, "forward_origin", None) is not None:
        return None
    sender = getattr(reply, "from_user", None)
    if sender is None or not getattr(sender, "is_bot", False):
        return None
    try:
        if sender.id != ctx.bot.id:
            return None
    except Exception:
        return None
    did = deals.id_from_text(getattr(reply, "text", None) or getattr(reply, "caption", None))
    if did is None:
        return None
    deal = deals.load(_deals_base(ctx.application)).get(did)
    # Закрытое дело: обычный путь со склейкой и цитатой (ревью 26.10).
    return did if (deal is not None and deal.get("chat_id") == getattr(msg, "chat_id", None)
                   and deals.is_open(deal)) else None


async def _deal_swipe(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deal_id: str, text: str) -> bool:
    """Свайп-ответ в дело. False: дело закрыто, сообщение идёт обычным путём."""
    msg = update.message
    text = text.strip()
    if text.startswith("!"):
        # Поправка в деле это обычный ход дела: ран лички она не прерывает.
        text = text[1:].strip()
    if not text:
        return True
    seen: dict = {}

    def take(d: dict) -> bool:
        seen.update(state=d.get("state"), label=d.get("label"), card=d.get("card_msg"), chat=d.get("chat_id"))
        return deals.accept(d, text, states=deals.SWIPE_STATES)

    if not _deal_update(ctx.application, deal_id, take):
        if seen.get("state") in ("accepted", "running"):
            await msg.reply_text(_("🧵 дело «{label}» ещё отвечает: дождись ответа и ответь на него").format(
                label=seen.get("label", "")))
            return True
        log.info("deal refused id=%s why=closed", deal_id)
        await msg.reply_text(_("🧵 дело закрыто, пишу в главный разговор"))
        return False
    if seen.get("state") == "asked" and seen.get("card"):
        # Ответ словами на вопрос дела: кнопки его карточки гаснут.
        with contextlib.suppress(Exception):
            await ctx.bot.edit_message_reply_markup(chat_id=seen["chat"], message_id=seen["card"], reply_markup=None)
    log.info("deal pick id=%s via=swipe", deal_id)
    await _deal_turn(update, ctx, deal_id, from_callback=False)
    return True


async def _deal_turn(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deal_id: str, *,
                     from_callback: bool) -> None:
    """Ход дела по принятому ответу (accepted). Первый ход идёт в ответвление
    сессии вопроса с точки его ответа, следующие в сессию дела. Исход хода
    всегда видим: ответ, отказ с причиной или «уйдёт после рестарта»."""
    app = ctx.application
    deal = deals.load(_deals_base(app)).get(deal_id)
    if deal is None or deal.get("state") != "accepted":
        return
    answer = str(deal.get("answer") or "")
    # Метка хода: тот же текст следующим свайпом это уже другой ход (ревью 26.10).
    turn_at = deal.get("answered_at")
    first = not deal.get("sid")
    prompt = (_("[Ответ на вопрос «{question}»]: {answer}").format(question=deal.get("question", ""), answer=answer)
              if deal.get("answering") else answer)
    if first:
        prompt = _(DEAL_FIRST_NOTE) + prompt
    ref = {"id": deal_id, "sid": deal.get("sid"), "head": deals.head(deal),
           "fork_at": (deal["qsid"], deal["quuid"]) if first else None, "fork_failed": False,
           "started": False}
    kind = "fork" if first else "resume" if from_callback else "reply"

    def started() -> None:
        # Процесс создан, промпт не подан: ошибка записи гасит его (run_claude).
        if not _deal_update(app, deal_id, lambda d: d.get("answered_at") == turn_at and deals.start(d, answer)):
            raise RuntimeError(f"deal {deal_id} changed before start")
        ref["started"] = True
        log.info("deal turn id=%s kind=%s", deal_id, kind)

    refused: bool | None = None
    crashed = True
    try:
        refused = await _b._dispatch_to_claude(
            update, ctx, prompt, send_voice_reply=False, from_callback=from_callback, deal=ref,
            on_run_start=started, allow_codex_fallback=False, deferred=True)
        crashed = False
    finally:
        # Исход и при исключении или отмене посреди обработки: дело не
        # остаётся «отвечает» до следующего рестарта (ревью 26.10).
        if not ref["fork_failed"]:
            await _deal_settle(app, update, deal_id, turn_at, answer, ref["head"],
                               waits=not crashed and (refused is True or bool(app.bot_data.get("stopping"))))
    if ref["fork_failed"]:
        # Точки вопроса нет (сессию сжали или её файла нет): ответ на вопрос
        # уходит по-старому, в главный разговор со сверкой эпохи вопроса.
        _deal_update(app, deal_id, lambda d: d.update(state="closed", error="fork", last_at=time.time()))
        log.info("deal refused id=%s why=fork", deal_id)
        if deal.get("answering"):
            ask_ref = {"question": deal.get("question", ""), "answer": answer}
            with _b._ask_answer_queued(ctx, _b._chat_key(update)[0], ask_ref):
                await _b._dispatch_to_claude(
                    update, ctx, _("[Ответ на вопрос «{question}»]: {answer}").format(
                        question=deal.get("question", ""), answer=answer),
                    # Запреты дела остаются: без GPT и переноса в /at (ревью 26.10).
                    send_voice_reply=False, from_callback=True, allow_codex_fallback=False,
                    expect_epoch=deal.get("qepoch"), ask_ref=ask_ref)
        else:
            with contextlib.suppress(Exception):
                await update.message.reply_text(ref["head"] + "\n" + _(
                    "⚠️ дело закрыто: его разговор не открылся (сессию сжали или её нет), ответ «{answer}» "
                    "не выполнен; напиши его в главный разговор").format(answer=answer))


async def _deal_settle(app: Any, update: Update, deal_id: str, turn_at: Any, answer: str, head: str, *,
                       waits: bool) -> None:
    """Ход кончился без итога в реестре: не начинался (лимит, вход, отказ) или
    оборвался (исключение, отмена). Ожидание перезапуска оставляет accepted."""
    def settle(d: dict) -> str | None:
        if d.get("answered_at") != turn_at:
            return None
        if d.get("state") == "accepted":
            if waits:
                return "kept"
            d.update(state="failed", error="not started", last_at=time.time())
            return "not started"
        if d.get("state") == "running":
            d.update(state="failed", error="run aborted", last_at=time.time())
            return "aborted"
        return None

    outcome = _deal_update(app, deal_id, settle)
    if outcome in ("not started", "aborted"):
        log.info("deal done id=%s ok=0 why=%s", deal_id, outcome.replace(" ", "-"))
        with contextlib.suppress(Exception):
            await update.message.reply_text(
                head + "\n" + (
                    _("⚠️ ответ «{answer}» не выполнен (лимит, вход или сбой до старта); ответь здесь "
                      "свайпом, когда будет можно") if outcome == "not started" else
                    _("⚠️ ход по ответу «{answer}» оборвался, исход неизвестен; если нужно, ответь здесь "
                      "свайпом")).format(answer=answer))


async def on_deal_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка вопроса дела: вариант из реестра на диске, принимается один раз."""
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None or not _b._is_allowed(cfg, update):
        return
    parts = (query.data or "").split(":")
    try:
        deal_id, n = parts[1], int(parts[2])
    except (IndexError, ValueError):
        await _b._answer_quietly(query, _("Кнопка не разобрана."), show_alert=True)
        return
    card = query.message
    chat_id = getattr(getattr(card, "chat", None), "id", None)
    mid = getattr(card, "message_id", None)
    seen: dict = {}

    def take(d: dict) -> bool:
        opts = d.get("options") or []
        seen.update(state=d.get("state"), asked_at=float(d.get("asked_at") or 0))
        if d.get("chat_id") != chat_id or d.get("card_msg") != mid or not 1 <= n <= len(opts):
            seen["why"] = "card"
            return False
        if time.time() - seen["asked_at"] > _b.ASK_TTL_SEC:
            seen["why"] = "old"
            return False
        seen["answer"] = opts[n - 1]
        if not deals.accept(d, opts[n - 1]):
            seen["why"] = "taken"
            return False
        return True

    if not _deal_update(ctx.application, deal_id, take):
        why = seen.get("why")
        log.info("deal refused id=%s why=%s", deal_id, why or "missing")
        await _b._answer_quietly(query, {
            "card": _("Ответ не принят: кнопка не от этого вопроса."),
            "old": _("Ответ не принят: вопросу больше 12 часов. Если нужно, ответь на него свайпом."),
            "taken": _("На этот вопрос уже ответили или дело ещё отвечает."),
        }.get(why or "", _("Ответ не принят: дело не найдено.")), show_alert=True)
        if why in ("old", "taken", None):
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
        return
    with contextlib.suppress(Exception):
        await query.answer()
    with contextlib.suppress(Exception):
        # Ярлык остаётся первой строкой: свайп на карточку тоже идёт в дело.
        await query.edit_message_text(f"{card.text}\n\n🗳 → {seen['answer']}")
    log.info("deal pick id=%s via=button wait=%.0fs", deal_id, time.time() - seen["asked_at"])
    await _deal_turn(_b.Update(update_id=update.update_id, message=card), ctx, deal_id, from_callback=True)


async def _deals_resume(app: Application) -> None:
    """После рестарта: принятый и не начатый ответ уходит, оборванный ход
    называется в чате (без повтора: он мог успеть что-то сделать)."""
    try:
        with deals.locked(_deals_base(app)) as data:
            wake, cut = deals.startup(data)
    except (OSError, ValueError):
        log.exception("deal registry scan failed")
        return
    for d in cut:
        log.info("deal done id=%s ok=0 why=restart", d["id"])
        with contextlib.suppress(Exception):
            await app.bot.send_message(
                d["chat_id"], deals.head(d) + "\n" + (_(
                    "⚠️ ответ «{answer}» не выполнен: бот не работал больше суток; если нужно, ответь "
                    "здесь свайпом") if d.get("error") == "expired" else _(
                    "⚠️ ответ «{answer}» прерван перезапуском бота, исход неизвестен; если нужно, ответь "
                    "здесь свайпом")).format(answer=d.get("answer", "")),
                message_thread_id=d.get("thread_id"))
    tasks_set: set[asyncio.Task] = app.bot_data.setdefault("deal_tasks", set())
    for d in wake:
        ctx = ContextTypes.DEFAULT_TYPE(application=app)
        task = asyncio.create_task(_deal_turn(_b._synthetic_update(app.bot, d["chat_id"], d.get("thread_id")),
                                              ctx, d["id"], from_callback=True))
        tasks_set.add(task)
        task.add_done_callback(tasks_set.discard)


# ---- Утренняя подборка новостей (skills/news_digest) ----

async def on_news_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки пункта подборки: «📌 В список» (строка в бэклог проекта, проект
    выбирает модель, см. news_digest/backlog.py), «✖ Мимо» (пункт
    сворачивается, отзыв для следующих отборов), «↩ Вернуть» (отмена «мимо»). Пункт берётся из выпуска на диске: подборку
    шлёт таймер, а кнопку жмут и после рестарта бота. Ставить что-то по кнопке
    нельзя (решение Ивана 08.10.2026): только ответом на пункт, через агента темы."""
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    if user is None or user.id not in cfg.allowed_user_ids:
        return
    base = news.news_dir(cfg.state_dir)
    parsed = news.parse_callback(query.data or "")
    item = news.load_item(base, parsed[0], parsed[1]) if parsed else None
    if parsed is None or item is None:
        await _b._answer_quietly(query, _("Кнопка от старого выпуска, она больше не работает"))
        return
    digest_id, _unused, action = parsed
    now = datetime.now().astimezone()
    if action == "p":
        await _news_file_to_backlog(query, cfg, ctx, digest_id, item, now)
        return
    if action == "w":
        await _b._task_start(query, cfg, ctx, digest_id, item, now)
        return
    if action == "x":
        view, toast = "skipped", _("Учту: такое не нужно")
    else:
        view, toast = "open", _("Вернул")
    try:
        news.append_feedback(base, digest_id, item, action, now)
    except OSError as e:
        log.warning("news feedback write failed: %s", e)
    await _b._answer_quietly(query, toast)
    await _news_edit(query, item, digest_id, view)


async def _news_edit(query, item, digest_id: str, view: str, where: str = "") -> None:
    rows = news.item_buttons(item, digest_id, view)
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(b["text"], callback_data=b["data"]) for b in row] for row in rows]
    ) if rows else None
    try:
        await query.edit_message_text(
            news.render_item(item, view, where), parse_mode="HTML", reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        log.debug("news item edit failed (ignored)", exc_info=True)


async def _news_file_to_backlog(query, cfg: Config, ctx: ContextTypes.DEFAULT_TYPE,
                                digest_id: str, item, now: datetime) -> None:
    """«📌 В список»: модель выбирает проект, пункт уходит в его BACKLOG.md.
    Пока идёт выбор, кнопок нет; повторное нажатие того же пункта не запускает
    второй выбор. Не нашёлся проект или не записалось: кнопки возвращаются,
    причина отдельным сообщением ответом на пункт."""
    busy: set = ctx.application.bot_data.setdefault("news_filing", set())
    key = (digest_id, item.n)
    if key in busy:
        await _b._answer_quietly(query, _("Уже выбираю бэклог"))
        return
    busy.add(key)
    try:
        await _b._answer_quietly(query, _("Выбираю бэклог"))
        await _news_edit(query, item, digest_id, "busy")
        base = news.news_dir(cfg.state_dir)
        filed = await asyncio.to_thread(news_backlog.file_item, item, now.strftime("%Y-%m-%d"), base)
        if filed.status in ("added", "dup"):
            if filed.note:
                log.warning("news backlog %s: %s", filed.name, filed.note)
            where = filed.name + (f" ({filed.note})" if filed.note else "")
            try:
                news.append_feedback(base, digest_id, item, "p", now)
            except OSError as e:
                log.warning("news feedback write failed: %s", e)
            await _news_edit(query, item, digest_id, "saved", where)
            return
        await _news_edit(query, item, digest_id, "open")
        if filed.status == "none":
            text = (_("Не нашёл проект для этого пункта: {note}. Ответь на пункт, куда его положить, или заведём новый проект.").format(note=filed.note)
                    if filed.note else
                    _("Не нашёл проект для этого пункта. Ответь на пункт, куда его положить, или заведём новый проект."))
        else:
            log.warning("news backlog failed: %s %s", filed.name, filed.note)
            text = _("Не записалось в бэклог: {note}. Нажми ещё раз.").format(note=filed.note or _("см. журнал бота"))
        msg = query.message
        if msg is not None:
            try:
                await msg.reply_text(text, disable_notification=True)
            except Exception:
                log.debug("news backlog note failed (ignored)", exc_info=True)
    finally:
        busy.discard(key)


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

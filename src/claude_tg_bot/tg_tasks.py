"""Темы задач в Telegram: карточки, создание тем, исполнение, уборка (перенесено из bot.py, шаг 5 разбиения)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from telegram import (ForceReply, LinkPreviewOptions,
                      ReplyParameters, Update)
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from . import tasks
from .buttons import markup as _markup, strip_buttons
from .claude_runner import ClaudeResult, SessionStore
from .config import Config, now_local
from .i18n import _
from .replies import (_send_retrying, extract_later_marker, extract_loud_marker, extract_quiet_marker,
                      is_unconfirmed, split_for_telegram)
from .skills.news_digest import core as news

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


# ---- Темы задач (tasks.py, переделка 16.10.2026) ----

def _task_for(cfg: Config, chat_id: int, thread_id: int | None,
              ctx: ContextTypes.DEFAULT_TYPE | None = None) -> dict | None:
    """Задача темы. Реестр не читается, а тема помечена как тема задачи:
    задача без номера в обдумывании (ревью Codex 13.10)."""
    if thread_id is None:
        return None
    try:
        return tasks.by_thread(tasks.load(tasks.registry_dir(cfg.state_dir), strict=True),
                               chat_id, thread_id)
    except (OSError, ValueError) as e:
        log.error("news tasks registry unreadable: %s", e)
        scopes = ctx.application.bot_data.get("scopes") if ctx is not None else None
        if scopes is not None and (scopes.get(f"{chat_id}:{thread_id}") or "").startswith(_(tasks.SCOPE_PREFIX)):
            return {"id": "", "status": "thinking", "chat_id": chat_id, "thread_id": thread_id}
        return None


def _task_update(cfg: Config, tid: str, expect: tuple[str, ...] | None = None,
                 expect_sha: str | None = None, expect_exec: dict | None = None,
                 **fields) -> dict | None:
    """Правка задачи под замком реестра. expect: только из этих статусов;
    expect_sha: только при этом отпечатке проекта; expect_exec: только пока
    идёт это выполнение (tasks.exec_ref). Иначе None и ничего не меняется
    (переходы без гонок, ревью Codex 13.10)."""
    with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
        task = data.get(tid)
        if task is None or (expect is not None and task.get("status") not in expect):
            return None
        if expect_sha is not None and task.get("plan_sha") != expect_sha:
            return None
        if expect_exec is not None and not tasks.exec_current(task, expect_exec):
            return None
        task.update(fields)
        return dict(task)


def _task_add_cost(cfg: Config, task: dict | None,
                   result: ClaudeResult) -> tuple[dict, float, float] | None:
    """Расход рана в задачу темы сразу после рана, под замком темы (проект
    шага 3, 1.6): cost[сессия] = максимум прежнего и суммы сессии. (задача,
    сумма до, после) или None вне темы задачи и у ответа без расхода (GPT,
    отмена до ответа)."""
    if not task or not task.get("id") or not result.session_id or result.session_cost_usd <= 0:
        return None
    try:
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            current = data.get(task["id"])
            if current is None:
                return None
            before, after = tasks.add_cost(current, result.session_id, result.session_cost_usd)
            return dict(current), before, after
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable, cost of %s not counted: %s", task["id"], e)
        return None


async def _task_cost_seen(ctx: ContextTypes.DEFAULT_TYPE, task: dict, before: float, after: float) -> None:
    """Строка расхода на карточке, когда сдвинулась показанная сумма.
    Предупреждение о пороге $5 убрано (решение Ивана 19.10.2026)."""
    cfg: Config = ctx.application.bot_data["cfg"]
    # Зовётся после отправки ответа, вне замка темы: задачу могли закрыть,
    # снимок рана затёр бы её карточку. Поэтому свежая запись, закрытая не
    # трогается (ревью 14.10). До правки карточки await нет.
    try:
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            current = data.get(task["id"])
            if current is None or current.get("status") not in tasks.OPEN_STATUSES:
                return
            task = dict(current)
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable, cost of %s not shown: %s", task["id"], e)
        return
    if tasks.cost_moved(before, after):
        await _b._task_card(ctx, task)


async def _task_card(ctx: ContextTypes.DEFAULT_TYPE, task: dict, status_line: str | None = None,
                     buttons: bool = True) -> bool:
    """Карточка задачи (первое сообщение темы): строка этапа и кнопка
    «Закрыть». False, если правку стоит повторить (_edit_settled)."""
    if not task.get("card_id"):
        return True
    status = task.get("status", "")
    cfg: Config = ctx.application.bot_data["cfg"]
    registry = tasks.load(tasks.registry_dir(cfg.state_dir))
    line = _(status_line) if status_line else status_line
    if not line:
        line = tasks.stage_line(task)
    # Расход из реестра, а не из переданной записи: её часто взяли до рана.
    cost = tasks.cost_total(registry.get(task.get("id")) or task)
    seen = (registry.get(task.get("id")) or {}).get("status")
    attempts = 0

    async def edit():
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            # Повтор после паузы 429: за неё задачу могли сдвинуть, устаревшая
            # правка вернула бы прежнюю строку (ревью Codex 14.10).
            try:
                fresh = tasks.load(tasks.registry_dir(cfg.state_dir), strict=True).get(task.get("id"))
            except (OSError, ValueError):
                fresh = None
            if seen is not None and fresh is not None and fresh.get("status") != seen:
                raise _StaleCard
        return await ctx.bot.edit_message_text(
            tasks.card_text(tasks.item_of(task), line, tasks.source_of(task), cost),
            chat_id=task["chat_id"], message_id=task["card_id"],
            reply_markup=_markup(tasks.card_buttons(task["id"], status)) if buttons else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    try:
        # 429 пережидается (живая проверка 14.10.2026). Правка идемпотентна.
        await _send_retrying(edit)
    except _StaleCard:
        return True
    except Exception as e:
        log.debug("news task card edit failed (ignored)", exc_info=True)
        return _edit_settled(e)
    return True


class _StaleCard(Exception):
    """Правка карточки устарела за паузу 429: новее уже сделала своя."""


def _edit_settled(e: Exception) -> bool:
    """Правку повторять незачем: Telegram отказал явно (сообщения нет, текст
    тот же, нет прав). Таймаут, сбой сети и 429 повторяются (ревью Codex 14.10)."""
    return isinstance(e, (BadRequest, Forbidden))


async def _task_news_item(ctx: ContextTypes.DEFAULT_TYPE, task: dict, where: str) -> bool:
    """Пункт в теме «Новости»: ссылка на тему задачи или её итог. False, если
    правку стоит повторить."""
    mid = task.get("news_message_id")
    if not mid:
        return True
    try:
        await ctx.bot.edit_message_text(
            news.render_item(tasks.item_of(task), "work", where), chat_id=task["chat_id"],
            message_id=mid, parse_mode="HTML", reply_markup=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as e:
        log.debug("news item edit failed (ignored)", exc_info=True)
        return _edit_settled(e)
    return True


def _task_text(update: Update, text: str | None) -> str | None:
    """Слова Ивана для разбора «делай»: пересланное это чужой текст."""
    msg = update.message
    if msg is not None and getattr(msg, "forward_origin", None) is not None:
        return None
    return text


async def _task_touch(ctx: ContextTypes.DEFAULT_TYPE, update: Update, text: str | None = None,
                      seen_sha: str | None = None) -> bool:
    """Сообщение Ивана в теме задачи, сразу при получении (до склейки и
    очереди темы), у голосового после расшифровки. «Делай» под проектом
    одобряет его и в Claude не идёт: тогда True. Иное снимает проект:
    обдумывание продолжается (ревью Codex 15.10: поправка перед
    стартом не даёт выполнить старое одобрение). Во время выполнения
    сообщение ждёт его конца; получение подтверждает общее «claude ещё
    работает» очереди темы."""
    _unused, chat_id, thread_id = _b._chat_key(update)
    act, was, task = _task_touch_now(ctx, chat_id, thread_id, _task_text(update, text), seen_sha)
    msg = update.message
    if act == "go":
        await _task_do(ctx, task["id"], task.get("plan_sha", ""), reply_to=msg)
        return True
    # Дальше сообщение идёт в Claude: подсказки и карточка фоном, без await
    # до очереди темы, иначе следующее сообщение обогнало бы это (ревью Codex 16.10).
    if act == "cancelled":
        ctx.application.create_task(_reply_quietly(msg, _(
            "Выполнение ещё не началось, одобрение снял: обдумываем дальше, "
            "выполнение после нового «Делай».")))
    if act in ("cancelled", "dropped") or (act == "thinking" and was in ("done", "failed")):
        ctx.application.create_task(_b._task_card(ctx, task))
    return False


def _task_seen_sha(ctx: ContextTypes.DEFAULT_TYPE, update: Update) -> str:
    """Отпечаток проекта под «Делай» в теме задачи сейчас, пусто: проекта нет."""
    _unused, chat_id, thread_id = _b._chat_key(update)
    if thread_id is None:
        return ""
    cfg: Config = ctx.application.bot_data["cfg"]
    task = tasks.by_thread(tasks.load(tasks.registry_dir(cfg.state_dir)), chat_id, thread_id)
    return task.get("plan_sha", "") if task is not None and task.get("status") == "proposed" else ""


def _task_touch_now(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, thread_id: int | None,
                    text: str | None, seen_sha: str | None = None) -> tuple[str, str | None, dict | None]:
    """tasks.touch под замком реестра, без await: (что случилось, прежний
    статус, снимок задачи). Кнопка вопроса зовёт до первого await, чтобы
    снимаемый проект не успели выполнить (ревью Codex 16.10)."""
    if thread_id is None:
        return "", None, None
    cfg: Config = ctx.application.bot_data["cfg"]
    try:
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            task = tasks.by_thread(data, chat_id, thread_id)
            if task is None:
                return "", None, None
            was = task.get("status")
            act = tasks.touch(task, text, seen_sha)
            return act, was, dict(task)
    except (OSError, ValueError) as e:
        log.error("news tasks registry unreadable: %s", e)
        return "", None, None


async def _reply_quietly(msg: Any, text: str) -> None:
    with contextlib.suppress(Exception):
        await msg.reply_text(text, disable_notification=True)


def _task_turn(cfg: Config, task: dict) -> tuple[int | None, dict | None]:
    """tasks.turn под замком реестра; нечитаемый реестр: без проекта. Второе:
    снимок, если ход сменил этап (карточку надо поправить: сообщение или
    добавка дождались конца выполнения, ревью Codex 19.10)."""
    try:
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            current = data.get(task["id"])
            if current is None:
                return None, None
            was = current.get("status")
            rev = tasks.turn(current)
            return rev, dict(current) if current.get("status") != was else None
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable, turn of %s not counted: %s", task["id"], e)
        return None, None


def _task_forget(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> None:
    for store_name in ("sessions", "scopes", "topic_names", "scope_fingerprints"):
        store = ctx.application.bot_data.get(store_name)
        if store is not None:
            store.remove(chat_key)


async def _task_start(query, cfg: Config, ctx: ContextTypes.DEFAULT_TYPE,
                      digest_id: str, item, now: datetime) -> None:
    """«🛠 В работу» под новостью: тема на задачу и сразу обдумывание. Место
    в реестре занимается до запроса к Telegram: двойное нажатие и два пункта
    разом не создадут лишних тем."""
    msg = query.message
    if msg is None:
        return
    bd = ctx.application.bot_data
    if bd.get("draining") or bd.get("stopping"):
        await _b._answer_quietly(query, _("♻️ бот готовится к перезапуску, задача не заведена: "
                              "повтори через пару минут"), show_alert=True)
        return
    # Заведение темы это работа для перезапуска (ревью 14.10). Без await после проверки.
    bd["active_work"] = bd.get("active_work", 0) + 1
    try:
        await _task_start_admitted(query, cfg, ctx, digest_id, item, now)
    finally:
        bd["active_work"] -= 1


async def _task_start_admitted(query, cfg: Config, ctx: ContextTypes.DEFAULT_TYPE,
                               digest_id: str, item, now: datetime) -> None:
    msg = query.message
    base = tasks.registry_dir(cfg.state_dir)
    chat_id = msg.chat_id
    try:
        with tasks.locked(base) as data:
            kind, task = tasks.reserve(data, tasks.task_id(digest_id, item.n), item, chat_id, now,
                                       digest=digest_id)
            if kind == "new":
                task["news_message_id"] = msg.message_id
            task = dict(task) if task else None
            opened = [dict(t) for t in tasks.taken(data)]
    except (OSError, ValueError) as e:
        log.error("news tasks registry unreadable: %s", e)
        await _b._answer_quietly(query, _("Реестр задач не читается, см. журнал бота"), show_alert=True)
        return
    if kind == "exists":
        if task.get("status") in tasks.OPEN_STATUSES and task.get("thread_id"):
            await _b._answer_quietly(query, _("Задача по этому пункту уже в работе"))
            await _b._news_edit(query, item, digest_id, "work",
                             tasks.topic_link(chat_id, task["thread_id"]))
        elif task.get("status") == "creating":
            await _b._answer_quietly(query, _("Тема задачи по этому пункту уже заводится"))
        else:
            await _b._answer_quietly(query, _("Задача по этому пункту уже была"))
        return
    if kind == "full":
        await _b._answer_quietly(query, _("Открыто {count} задач: сначала закрой одну").format(count=tasks.MAX_OPEN),
                              show_alert=True)
        # Длинные заголовки: список режется, как ответы claude.
        with contextlib.suppress(Exception):
            for chunk in split_for_telegram(_("Открытые задачи:\n{lines}").format(lines=tasks.open_list(opened)),
                                            cfg.telegram_text_limit):
                await msg.reply_text(chunk, disable_notification=True,
                                     link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    await _b._answer_quietly(query, _("Завожу тему задачи"))
    try:
        thread_id, _unused = await _task_create(ctx, cfg, task, tasks.intro_prompt(item))
    except Exception as e:
        _task_unborn(cfg, task["id"])
        log.warning("news task topic create failed: %s", e)
        with contextlib.suppress(Exception):
            await msg.reply_text(
                _("Тема задачи не создалась ({error}). Если тема всё же появилась, "
                  "удали её и нажми «В работу» ещё раз.").format(error=type(e).__name__), disable_notification=True)
        return
    try:
        news.append_feedback(news.news_dir(cfg.state_dir), digest_id, item, "w", now)
    except OSError as e:
        log.warning("news feedback write failed: %s", e)
    await _b._news_edit(query, item, digest_id, "work", tasks.topic_link(chat_id, thread_id))


async def _task_base_name(item) -> str:
    """Имя темы до цифры. Слова Ивана называет дешёвая модель в потоке (бот
    не стоит, замок реестра не держится); сбой, таймаут или мусорный ответ:
    первые слова кодом. У новостей имя от модели отбора (решение Ивана 14.10.2026)."""
    fallback = tasks.fallback_name(item)
    if item.kind != "task":
        return fallback
    try:
        return await asyncio.to_thread(tasks.model_name, item.text) or fallback
    except Exception as e:
        log.warning("task name by model failed, taking first words: %s", e)
        return fallback


def _task_assign_name(cfg: Config, tid: str, base_name: str) -> str:
    """Номер и имя в запись задачи: выбор и запись одним блоком под замком
    реестра, две одновременные задачи не получат один номер."""
    with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
        return tasks.assign_name(data, tid, base_name)


async def _task_topic_icon(ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    """custom_emoji_id значка темы задачи среди разрешённых Telegram значков
    тем (tasks.pick_icon), None если подходящего нет: тогда значок идёт в имени.
    Набор спрашивается один раз за работу бота. Сбой запроса не запоминается:
    следующая тема спросит снова."""
    bd = ctx.application.bot_data
    if "task_topic_icon" in bd:
        return bd["task_topic_icon"]
    try:
        icon = tasks.pick_icon(await ctx.bot.get_forum_topic_icon_stickers())
    except Exception as e:
        log.warning("forum topic icons not loaded, icon goes in the name: %s", e)
        return None
    bd["task_topic_icon"] = icon
    return icon


async def _task_create(ctx: ContextTypes.DEFAULT_TYPE, cfg: Config, task: dict, prompt: str,
                       note: str | None = None) -> tuple[int, str]:
    """Тема по занятой записи реестра (статус creating): имя, тема, сразу номер
    темы в реестр, пометка темы, обдумывание, карточка с закреплением, ран
    обдумывания фоном. Вернёт номер темы и её имя. Тема не создалась: запись
    снимается, исключение уходит вызывающему, он и объясняет Ивану."""
    base = tasks.registry_dir(cfg.state_dir)
    tid, chat_id = task["id"], task["chat_id"]
    item = tasks.item_of(task)
    short = _task_assign_name(cfg, tid, await _task_base_name(item))
    # Значок темы настоящий, если Telegram такой разрешает; иначе цвет, а в
    # имени значка нет: номер впереди важнее (решение Ивана 14.10.2026).
    icon = await _task_topic_icon(ctx)
    try:
        if icon:
            topic = await ctx.bot.create_forum_topic(chat_id, short, icon_custom_emoji_id=icon)
        else:
            topic = await ctx.bot.create_forum_topic(chat_id, short, icon_color=tasks.TOPIC_ICON_COLOR)
    except Exception:
        # Ответ мог потеряться, а тема создаться: повторять вслепую нельзя.
        with tasks.locked(base) as data:
            data.pop(tid, None)
        raise
    thread_id = topic.message_thread_id
    # Номер темы в реестр первым делом: упади бот дальше, уборка найдёт и удалит её.
    task = _task_update(cfg, tid, thread_id=thread_id) or task
    chat_key = f"{chat_id}:{thread_id}"
    # Имя пишем сами: событие создания темы ботом в кэш не попадает. Пометка
    # темы без имён проектов: память закрепит первое сообщение рана (у слов
    # Ивана к ней дописана исходная тема, решение Ивана 13.10.2026).
    if note is None:
        note = tasks.scope_note()
    for store_name, value in (("topic_names", short), ("scopes", note)):
        store: SessionStore | None = ctx.application.bot_data.get(store_name)
        if store is not None:
            store.set(chat_key, value)
    task = _task_update(cfg, tid, expect=("creating",), status="thinking") or task
    task = await _task_new_card(ctx, task, tasks.STAGE_LINES["creating"], "thinking")
    # Обдумывание до его конца это работа для перезапуска по заявке (ревью
    # 14.10). Счёт до create_task: вызывающий держит свой, снимает _task_intro.
    bd = ctx.application.bot_data
    bd["active_work"] = bd.get("active_work", 0) + 1
    try:
        ctx.application.create_task(_task_intro(ctx, task, prompt))
    except BaseException:
        bd["active_work"] -= 1
        raise
    return thread_id, short


async def _task_new_card(ctx: ContextTypes.DEFAULT_TYPE, task: dict, line: str, status: str) -> dict:
    """Карточка задачи новым сообщением с закреплением: при заведении темы и
    при старте бота у живой задачи, чья карточка тогда не ушла (ревью Codex
    14.10). Вернёт запись с номером карточки или прежнюю, если не ушла."""
    cfg: Config = ctx.application.bot_data["cfg"]
    tid, chat_id = task["id"], task["chat_id"]
    source = tasks.source_of(task)
    try:
        card = await _send_retrying(lambda: ctx.bot.send_message(
            chat_id, tasks.card_text(tasks.item_of(task), _(line), source, tasks.cost_total(task)),
            message_thread_id=task["thread_id"],
            reply_markup=_markup(tasks.card_buttons(tid, status)), disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        ))
        task = _task_update(cfg, tid, card_id=card.message_id) or task
        with contextlib.suppress(Exception):
            await ctx.bot.pin_chat_message(chat_id, card.message_id, disable_notification=True)
    except Exception as e:
        log.warning("news task card failed", exc_info=True)
        if is_unconfirmed(e):
            # Карточка, возможно, ушла: старт её заново не шлёт (card_unsure).
            task = _task_update(cfg, tid, card_unsure=True) or task
    return task


def _task_unborn(cfg: Config, tid: str) -> None:
    """Сбой до запроса темы: запись снимается, как при сбое
    create_forum_topic (ревью 14.10). Запись с номером темы остаётся: тему
    найдёт и удалит уборка."""
    try:
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            task = data.get(tid)
            if task is not None and task.get("status") == "creating" and not task.get("thread_id"):
                data.pop(tid)
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable, %s not freed: %s", tid, e)


def _tasks_chat_id(cfg: Config) -> int | None:
    """Группа тем задач: та же, что у «Новостей» (проект шага 3, 1.4)."""
    return cfg.news_tg_chat_id


def _is_task_ask_reply(ctx: ContextTypes.DEFAULT_TYPE, msg: Any) -> bool:
    """Ответ на подсказку пустой /task. Состояние не хранится: узнаём по
    сообщению бота с началом tasks.ASK, на которое ответили (проект шага 3,
    1.4). Пересланное это чужой текст, а не ответ Ивана."""
    reply = getattr(msg, "reply_to_message", None)
    if reply is None or getattr(msg, "forward_origin", None) is not None:
        return False
    sender = getattr(reply, "from_user", None)
    if sender is None or not getattr(sender, "is_bot", False):
        return False
    try:
        if sender.id != ctx.bot.id:
            return False
    except Exception:
        # Бот без get_me не знает своего id: подсказку не узнать.
        return False
    return (getattr(reply, "text", None) or "").startswith(_(tasks.ASK))


async def on_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/task <что сделать>: тема задачи в группе «Новостей» и сразу обдумывание."""
    cfg: Config = ctx.application.bot_data["cfg"]
    if not _b._is_allowed(cfg, update):
        return
    msg = update.message
    parts = (msg.text or "").split(maxsplit=1)
    words = parts[1].strip() if len(parts) > 1 else ""
    if not words:
        # На телефоне команда из меню уходит сразу, без текста: подсказка с
        # принудительным ответом, Telegram сам откроет поле ответа.
        await msg.reply_text(_(tasks.ASK), reply_markup=ForceReply(selective=True,
                                                                 input_field_placeholder=_("что сделать")))
        return
    await _task_command(update, ctx, words)


async def _task_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE, words: str) -> None:
    """Задача по словам Ивана: из /task или из ответа на её подсказку. Ответ
    в теме, где набрали, правится в ссылку на новую тему."""
    msg = update.message
    chat_key, chat_id, thread_id = _b._chat_key(update)
    no_preview = LinkPreviewOptions(is_disabled=True)
    origin = None
    with contextlib.suppress(Exception):
        origin = await msg.reply_text(_("Завожу тему задачи…"), disable_notification=True)
    # ask_id: на это сообщение Ивана ответит строка итога после закрытия,
    # если задача из лички (решение Ивана 19.10.2026).
    text = await _task_new(ctx, words, tasks.command_id(chat_id, msg.message_id), origin_key=chat_key,
                           origin={"chat_id": chat_id, "thread_id": thread_id, "ask_id": msg.message_id,
                                   "message_id": origin.message_id if origin is not None else None})
    with contextlib.suppress(Exception):
        if origin is not None:
            await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=origin.message_id,
                                            link_preview_options=no_preview)
        else:
            await msg.reply_text(text, link_preview_options=no_preview)


async def _task_new(ctx: ContextTypes.DEFAULT_TYPE, words: str, tid: str, origin_key: str | None = None,
                    origin: dict | None = None, context: str = "") -> str:
    """Задача по словам Ивана из любого входа (/task, Домовой через POST
    /task). Вернёт строку для ответа: ссылку на тему или причину отказа.
    tid: повторная доставка того же входа найдёт занятую запись, второй темы
    не будет. origin_key: тема, чья память переходит в задачу (решение Ивана
    13.10.2026, вопрос 3а)."""
    cfg: Config = ctx.application.bot_data["cfg"]
    group = _tasks_chat_id(cfg)
    if group is None:
        return _("Темы задач заводятся в группе с «Новостями», а она не задана "
                 "(NEWS_TG_CHAT_ID в .env бота).")
    bd = ctx.application.bot_data
    if bd.get("draining") or bd.get("stopping"):
        return _("♻️ бот готовится к перезапуску, задача не заведена: повтори через пару минут")
    # Заведение темы это работа для перезапуска (ревью 14.10). Без await после проверки.
    bd["active_work"] = bd.get("active_work", 0) + 1
    try:
        item = tasks.command_item(words)
        try:
            with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
                kind, task = tasks.reserve(data, tid, item, group, now_local(),
                                           source="command", trusted=True, origin=origin,
                                           context=context.strip()[:tasks.COMMAND_TEXT_MAX])
                task = dict(task) if task else None
                opened = [dict(t) for t in tasks.taken(data)]
        except (OSError, ValueError) as e:
            log.error("tasks registry unreadable: %s", e)
            return _("Реестр задач не читается, см. журнал бота")
        if kind == "exists":
            if task.get("status") == "creating":
                return _("Тема этой задачи уже заводится")
            if task.get("status") in tasks.OPEN_STATUSES and task.get("thread_id"):
                return _("Задача уже заведена: {link}").format(link=tasks.topic_link(task['chat_id'], task['thread_id']))
            return _("Эта задача уже была и закрыта")
        if kind == "full":
            return (_("Открыто {count} задач, сначала закрой одну:\n").format(count=tasks.MAX_OPEN)
                    + tasks.open_list(opened))
        note = tasks.scope_note(_b._memory_scope_hint(ctx, cfg, origin_key) if origin_key else "")
        try:
            new_thread, short = await _task_create(
                ctx, cfg, task, tasks.command_intro_prompt(item.text, context), note)
        except Exception as e:
            _task_unborn(cfg, tid)
            log.warning("task topic create failed %s: %s", tid, e)
            return (_("Тема задачи не создалась ({error}). Если тема всё же появилась, "
                      "удали её и повтори.").format(error=type(e).__name__))
        return _("Задача «{short}»: {link}").format(short=short, link=tasks.topic_link(group, new_thread))
    finally:
        bd["active_work"] -= 1


async def _task_from_http(ctx: ContextTypes.DEFAULT_TYPE, words: str, context: str, key: str,
                          chat_key: str = "", ask_id: int | None = None, to: str = "") -> str:
    """Вход POST /task (Домовой, скилл task_new): задача как из /task в
    личном чате, память этого чата переходит в задачу. chat_key и ask_id из
    окружения позвавшего рана: откуда звали и на какое сообщение Ивана
    ответит строка итога после закрытия (решение Ивана 19.10.2026). Без темы
    рана (вызов из терминала): личка, без ответа. to: слова добавляются в
    открытую задачу (_task_add)."""
    cfg: Config = ctx.application.bot_data["cfg"]
    if to:
        if chat_key != str(cfg.primary_chat_id):
            # Добавлять в задачу из лички, то есть Домовому; от случайного
            # вызова агентом другой темы, не от умысла (ревью Codex 19.10).
            return _("Добавить в задачу можно только из личного чата с ботом")
        return await _task_add(ctx, to, words, context, key)
    tid = f"h{key}" if key else f"h{int(time.time() * 1000)}"
    chat, _sep, thread = (chat_key or str(cfg.primary_chat_id)).partition(":")
    origin = {"chat_id": int(chat), "thread_id": int(thread) if thread else None, "ask_id": ask_id}
    return await _task_new(ctx, words, tid, origin_key=str(cfg.primary_chat_id), origin=origin,
                           context=context)


async def _task_add(ctx: ContextTypes.DEFAULT_TYPE, tid: str, words: str, context: str = "",
                    key: str = "") -> str:
    """Слова Ивана через Домового в открытую задачу (решение Ивана 19.10.2026):
    видны в теме и идут ходом обдумывания, как его сообщение там. «Делай» так
    не одобряется (касание без текста), закрыть нельзя: это только в теме.
    Идущее выполнение добавка ждёт, как сообщение в теме. key: повтор того
    же запроса (Домовой не дождался ответа) второй раз не добавляет. Вернёт
    строку для ответа: что добавлено и куда, или причину отказа.

    Добавки к одной задаче идут по очереди (замок задачи), проект снимается
    до первого ожидания сети, отметка «обдумывание идёт» и учёт работы
    ставятся сразу: авария после приёма видна в теме при старте (ревью Codex
    19.10)."""
    cfg: Config = ctx.application.bot_data["cfg"]
    bd = ctx.application.bot_data
    locks: dict[str, asyncio.Lock] = bd.setdefault("task_add_locks", {})
    async with locks.setdefault(tid, asyncio.Lock()):
        if bd.get("draining") or bd.get("stopping"):
            return _("♻️ бот готовится к перезапуску, в задачу не добавлено: повтори через пару минут")
        try:
            task = tasks.load(tasks.registry_dir(cfg.state_dir), strict=True).get(tid)
        except (OSError, ValueError) as e:
            log.error("tasks registry unreadable: %s", e)
            return _("Реестр задач не читается, см. журнал бота")
        if task is None:
            return _("Задачи {tid} нет: открытые показывает task_new --open").format(tid=tid)
        if task.get("status") == "creating" or not task.get("thread_id"):
            return _("Тема задачи ещё заводится: повтори через минуту")
        if task.get("status") not in tasks.OPEN_STATUSES:
            return _("Задача «{name}» закрыта: заведи новую и в пояснении сошлись на неё").format(
                name=tasks.short_name(task))
        link = tasks.topic_link(task["chat_id"], task["thread_id"])
        if key and key in (task.get("add_keys") or ()):
            return _("Уже добавлено в задачу «{name}»: {link}").format(name=tasks.short_name(task), link=link)
        # До первого await: касание без текста снимает проект и не начатое
        # одобрение («делай» в словах одобрением не считается), отметка и
        # учёт работы: перезапуск по заявке ждёт этот ход.
        act, was, snap = _task_touch_now(ctx, task["chat_id"], task["thread_id"], None)
        with contextlib.suppress(OSError, ValueError):
            _task_update(cfg, tid, expect=tasks.OPEN_STATUSES, think_open=ADD_PENDING)
        bd["active_work"] = bd.get("active_work", 0) + 1
        try:
            if snap is not None and (act in ("cancelled", "dropped")
                                     or (act == "thinking" and was in ("done", "failed"))):
                ctx.application.create_task(_b._task_card(ctx, snap))
            try:
                await _send_retrying(lambda: ctx.bot.send_message(
                    task["chat_id"], tasks.addition_echo(words), message_thread_id=task["thread_id"],
                    link_preview_options=LinkPreviewOptions(is_disabled=True)))
            except Exception as e:
                if not is_unconfirmed(e):
                    log.warning("task addition echo failed %s", tid, exc_info=True)
                    _task_add_settle(cfg, tid)
                    bd["active_work"] -= 1
                    return _("В тему задачи не ушло ({error}), не добавлено").format(error=type(e).__name__)
                # Эхо обычно в теме видно: «не добавлено» вело к повтору и второму эху.
                log.warning("task addition echo unconfirmed by Telegram, added anyway %s", tid)
            ctx.application.create_task(_b._task_add_run(ctx, task, tasks.addition_prompt(words, context)))
        except BaseException:
            bd["active_work"] -= 1
            raise
        if key:
            with contextlib.suppress(OSError, ValueError):
                with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
                    if tid in data:
                        tasks.remember_add_key(data[tid], key)
    if act == "busy":
        return _("Добавил в задачу «{name}», агент ответит после идущего выполнения: {link}").format(
            name=tasks.short_name(task), link=link)
    return _("Добавил в задачу «{name}»: {link}").format(name=tasks.short_name(task), link=link)


# Отметка think_open от приёма добавки до старта её рана: ран ставит свой
# номер хода, а оставшаяся после аварии видна при старте (_tasks_recover).
ADD_PENDING = "add"


def _task_add_settle(cfg: Config, tid: str) -> None:
    """Снять отметку приёма добавки, если её не сменил ран."""
    with contextlib.suppress(OSError, ValueError):
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            if (data.get(tid) or {}).get("think_open") == ADD_PENDING:
                data[tid]["think_open"] = None


async def _task_add_run(ctx: ContextTypes.DEFAULT_TYPE, task: dict, prompt: str) -> None:
    """Ход обдумывания по словам из _task_add; учёт работы снимается здесь."""
    try:
        upd = _b._synthetic_update(ctx.bot, task["chat_id"], task["thread_id"])
        await _b._dispatch_to_claude(upd, ctx, prompt, send_voice_reply=False, admitted=True)
    except Exception:
        log.exception("task addition run failed %s", task["id"])
    finally:
        _task_add_settle(ctx.application.bot_data["cfg"], task["id"])
        ctx.application.bot_data["active_work"] -= 1


async def _task_intro(ctx: ContextTypes.DEFAULT_TYPE, task: dict, prompt: str) -> None:
    """Первое обдумывание темы задачи. Учёт работы взял _task_create, здесь он
    снимается. Работу приняли до заявки на перезапуск, поэтому admitted: её
    доводят, а не отклоняют (ревью 14.10)."""
    try:
        upd = _b._synthetic_update(ctx.bot, task["chat_id"], task["thread_id"])
        try:
            # Обычный режим, как следующие ходы обдумывания (решение Ивана
            # 04.11.2026: замка «только чтение» нет, правило в промте).
            await _b._dispatch_to_claude(upd, ctx, prompt, send_voice_reply=False, admitted=True)
        except Exception:
            log.exception("news task intro failed %s", task["id"])
        # Задачу могли закрыть, пока уходил ответ: снимок с заведения темы затёр
        # бы её карточку. Свежая запись, закрытая не трогается (ревью 14.10).
        try:
            current = tasks.load(tasks.registry_dir(ctx.application.bot_data["cfg"].state_dir),
                                 strict=True).get(task["id"])
        except (OSError, ValueError) as e:
            log.error("tasks registry unreadable, intro card of %s by snapshot: %s", task["id"], e)
            current = task
        if current is not None and current.get("status") in tasks.OPEN_STATUSES:
            await _b._task_card(ctx, current)
    finally:
        ctx.application.bot_data["active_work"] -= 1


async def _task_thought(ctx: ContextTypes.DEFAULT_TYPE, update: Update, task: dict, rev: int,
                        reply: str, skill: str) -> None:
    """Ответ обдумывания с маркером <<DO>>: проект решения под «▶️ Делай».
    Без нажатия не выполняется ничего: самостоятельные скиллы убраны
    (решение Ивана 19.10.2026)."""
    cfg: Config = ctx.application.bot_data["cfg"]
    chat_key = f"{task['chat_id']}:{task['thread_id']}"
    if _b._queued_count(ctx, chat_key) or _b._gather_pending(ctx, chat_key):
        # Следом ждут сообщения Ивана (в очереди темы или ещё в склейке): этот
        # ответ уже не про его последнее слово, проект даст следующий ход
        # (ревью Codex 16.10 и 19.10).
        return
    try:
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            current = data.get(task["id"])
            if current is None or not tasks.propose(current, rev, reply, skill):
                return
            proposed = dict(current)
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable, proposal of %s lost: %s", task["id"], e)
        return
    with contextlib.suppress(Exception):
        # Скилл идёт в выполнение отдельной строкой: Иван видит его до нажатия
        # (ревью Codex 18.10).
        how = _(" (по скиллу «{skill}»)").format(skill=skill) if skill else ""
        await _send_retrying(lambda: update.message.reply_text(
            _("Делать по проекту выше{how}? Кнопка или слово «делай»; другое сообщение "
              "продолжит обдумывание.").format(how=how),
            reply_markup=_markup(tasks.do_buttons(proposed["id"], proposed["plan_sha"])),
            disable_notification=True))
    await _b._task_card(ctx, proposed)


async def _task_do(ctx: ContextTypes.DEFAULT_TYPE, tid: str, sha: str, press: str | None = None,
                   query=None, reply_to: Any = None) -> None:
    """«▶️ Делай» кнопкой или словом. Одобрение привязано к отпечатку
    проекта: устаревшая кнопка не выполнит новый текст. Выполнение свободно:
    сразу; занято другой задачей: ответ «занято», проект ждёт дальше и кнопка
    остаётся рабочей (очередь убрана, разбор 19.10.2026)."""
    bd = ctx.application.bot_data
    cfg: Config = bd["cfg"]

    async def say(text: str, alert: bool = False) -> None:
        if query is not None:
            await _b._answer_quietly(query, text, show_alert=alert)
        elif reply_to is not None:
            with contextlib.suppress(Exception):
                await reply_to.reply_text(text, disable_notification=True)

    if bd.get("draining") or bd.get("stopping"):
        await say(_("♻️ бот готовится к перезапуску: скажи «делай» после рестарта"), alert=True)
        return
    # Проверка ожидания, переход и учёт работы без await между ними: рестарт
    # не начнётся между одобрением и стартом выполнения.
    try:
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            kind, task = tasks.approve(data, tid, sha, _task_live(bd), press=press)
            task = dict(task) if task else None
            holder = tasks.running(data)
            holder = dict(holder) if holder else None
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable: %s", e)
        await say(_("Реестр задач не читается, см. журнал"), alert=True)
        return
    if kind == "running":
        _task_exec_admit(ctx, task)
    if kind == "dup":
        await say(_("Это нажатие уже принято"))
        return
    if kind == "busy":
        who = _("«{name}»").format(name=tasks.short_name(holder)) if holder else _("другая задача")
        await say(_("⏳ Сейчас выполняется {who}. Когда закончится, нажми «Делай» ещё раз или напиши «делай».").format(who=who),
                  alert=True)
        return
    if kind == "stale":
        if task is not None and task.get("plan_sha") == sha and task.get("status") == "running":
            await say(_("Этот проект уже одобрен"))
            return
        await say(_("Этот проект уже не действует: под последним проектом своя кнопка"), alert=True)
        if query is not None:
            await strip_buttons(query)
        return
    if query is not None:
        await strip_buttons(query)
    await say(_("▶️ Выполняю"))
    ctx.application.create_task(_task_exec(ctx, task))


def _task_exec_finish(cfg: Config, ref: dict, result: ClaudeResult, stopping: bool) -> None:
    """Ран выполнения кончился: done или failed с итогом, пока замок темы
    ещё у выполнения. Итог доставит _task_exec по result_gen, даже если Иван
    уже снова пишет в теме. Итог это сводка «✅/❌» из любого ответа рана;
    без неё выполнение не считается удачным (разбор задач 19.10.2026)."""
    # Сводка, написанная до сбоя рана (таймаут или отмена на ответе по
    # уведомлению), тоже итог: работа к ней уже сделана (ревью Codex 19.10).
    summary = tasks.exec_summary(result.texts or (() if result.is_error else (result.text,)))
    ok = summary is not None and not tasks.exec_failed(summary)
    text = summary or (result.text if result.is_error else _(tasks.EXEC_UNCONFIRMED))
    # Итог хранится до доставки и может уйти отдельно: метка звука в нём лишняя.
    text = extract_later_marker(extract_quiet_marker(extract_loud_marker(text)[1])[1])[1]
    try:
        with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
            current = data.get(ref["id"])
            if not tasks.exec_current(current, ref) or not current.get("exec_started"):
                return
            current.update(status="done" if ok else "failed",
                           interrupted=stopping and result.is_error and summary is None,
                           # Снимается, когда ответ рана ушёл в тему; авария
                           # раньше: итог пришлёт старт (ревью Codex 19.10).
                           result_unsent=True,
                           result_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            tasks.push_result(current, text, ok)
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable, exec end of %s not saved: %s", ref["id"], e)


def _task_exec_admit(ctx: ContextTypes.DEFAULT_TYPE, task: dict) -> None:
    """Учёт выполнения от перехода в running до выхода процесса: работа для
    перезапуска и занятое выполнение. Без await: зовут сразу после
    перехода, снимает _task_exec_release."""
    bd = ctx.application.bot_data
    bd["active_work"] = bd.get("active_work", 0) + 1
    bd.setdefault("task_exec_live", set()).add(task["id"])


def _task_exec_release(ctx: ContextTypes.DEFAULT_TYPE, task: dict) -> None:
    bd = ctx.application.bot_data
    bd["active_work"] -= 1
    bd.setdefault("task_exec_live", set()).discard(task["id"])


def _task_live(bd: dict) -> set[str]:
    """Выполнения по памяти бота, чей процесс ещё не вышел (tasks.approve)."""
    return set(bd.get("task_exec_live") or ())


def _task_release_after_exit(ctx: ContextTypes.DEFAULT_TYPE, procs: list,
                             release: Callable[[], Awaitable[None]]) -> bool:
    """Процесс рана не вышел и после SIGKILL (unexited, run_claude):
    выполнение занято до подтверждённого выхода, release (снятие учёта)
    зовётся фоном после него. False: живых нет, снимать сразу (ревью
    Codex 14.10). Не через PTB create_task: остановка бота ждала бы выхода,
    которого может и не быть."""
    alive = [p for p in procs if p.returncode is None]
    if not alive:
        return False
    pids = ", ".join(str(p.pid) for p in alive)
    log.warning("task exec kept busy until pid %s exits", pids)

    async def wait() -> None:
        for p in alive:
            await p.wait()
        log.info("pid %s exited, task exec released", pids)
        await release()

    waits: set[asyncio.Task] = ctx.application.bot_data.setdefault("task_exit_waits", set())
    task = asyncio.create_task(wait())
    waits.add(task)
    task.add_done_callback(waits.discard)
    return True


async def _task_exec(ctx: ContextTypes.DEFAULT_TYPE, task: dict) -> None:
    """Выполнение одобренного проекта: ран с отметкой старта и правами рана
    выполнения (allow:task-exec), затем done или failed с итогом в теме. Ран
    не стартовал: проект снова ждёт «Делай». Учёт взял вызывающий
    (_task_exec_admit), здесь он снимается."""
    cfg: Config = ctx.application.bot_data["cfg"]
    tid = task["id"]
    # Своё одобрение: проект могли поправить и одобрить заново, пока этот
    # обработчик ждал Telegram (ревью Codex 14.10).
    ref = tasks.exec_ref(task)
    unexited: list = []
    sink: list = []
    sent: list = []
    try:
        try:
            await _b._task_card(ctx, task)
            upd = _b._synthetic_update(ctx.bot, task["chat_id"], task["thread_id"])

            def started() -> None:
                # Признак реального старта: диспетчер возвращается и без рана
                # (ожидание перезапуска, восстановление входа). Зовёт run_claude,
                # когда процесс создан, до подачи промпта (ревью Codex 14.10).
                # Ошибка записи не глотается: без отметки процесс гасится до
                # промпта (ревью 14.10).
                if _task_update(cfg, tid, expect_exec=ref,
                                exec_started=datetime.now().astimezone().isoformat(timespec="seconds")) is None:
                    raise RuntimeError(f"task {tid} is not running")

            # Сессия всегда новая, мостом служит одобренный проект: в
            # обдумывании мог быть чужой текст (страница, чат, лог сервера), а
            # распознать его по инструментам надёжно нельзя (переделка
            # 18.10.2026, ревью Codex). Без запасного пути через GPT: там нет
            # карточек, а песочница с записью (проект шага 3, 1.8).
            await _b._dispatch_to_claude(upd, ctx, tasks.exec_prompt(task), send_voice_reply=False,
                                      fresh=True, on_run_start=started,
                                      allow_codex_fallback=False, exec_task=ref, unexited=unexited,
                                      reply_sink=sink, reply_sent=sent)
        except Exception:
            log.exception("task exec failed %s", tid)
        reply = sink[0][0] if sink else ""
        bd = ctx.application.bot_data
        restarting = bool(bd.get("stopping") or bd.get("draining"))
        try:
            with tasks.locked(tasks.registry_dir(cfg.state_dir)) as data:
                current = data.get(tid)
                done, not_started = None, False
                if tasks.exec_current(current, ref):
                    # Конец рана не записал диспетчер: ран не стартовал или
                    # упал до итога (_task_exec_finish).
                    status = tasks.after_exec(current, False)
                    current["status"] = status
                    not_started = status == "proposed"
                    if not_started:
                        # Тот же проект снова ждёт «Делай»: повтор после того же
                        # нажатия (dup) не сработает. Кнопку ещё надо прислать:
                        # отметка в том же переходе (ревью Codex 19.10).
                        current.update(accepted_press=None, offer_due=True)
                    else:
                        tasks.push_result(current, reply, False)
                    done = dict(current)
                elif current is not None and current.get("result_gen") == ref["exec_gen"]:
                    done = dict(current)
        except (OSError, ValueError) as e:
            log.error("tasks registry unreadable after exec %s: %s", tid, e)
            done = None
        if done is None:
            return
        if not_started:
            await _task_offer_do(ctx, done, _("Выполнение не запустилось из-за рестарта: после него "
                                              "скажи «делай» ещё раз.") if restarting else
                                 _("Выполнение не запустилось: скажи «делай» ещё раз."))
            return
        if done.get("status") in ("done", "failed"):
            # Иван уже снова пишет в теме: карточку ведёт его ход.
            interrupted = bool(done.get("interrupted") or ctx.application.bot_data.get("stopping"))
            await _b._task_card(ctx, done, tasks.EXEC_INTERRUPTED
                             if interrupted and done.get("status") == "failed" else None)
        if done.get("result_gen") == ref["exec_gen"] and done.get("result_unsent"):
            if sent:
                with contextlib.suppress(OSError, ValueError):
                    _task_update(cfg, tid, result_unsent=None)
            else:
                # Ответ рана в тему не ушёл (сеть): итог живёт только в теме,
                # его шлёт выполнение, не вышло, повторит уборка (ревью Codex 19.10).
                await _task_post_result(ctx, done)
    finally:
        async def release() -> None:
            _task_exec_release(ctx, task)

        # Процесс выполнения не вышел и после SIGKILL: занято до выхода.
        if not _task_release_after_exit(ctx, unexited, release):
            await release()


async def _task_note(ctx: ContextTypes.DEFAULT_TYPE, task: dict) -> bool:
    """Строка итога в личку после «🗑 Закрыть» у задачи из лички, ответом на
    исходное сообщение Ивана (решение Ивана 19.10.2026: итог живёт в теме).
    Отправитель захватывает строку под замком реестра, второй (уборка,
    повтор) её не шлёт (ревью Codex 19.10). Не ушла: повторит уборка, после
    tasks.NOTE_TRIES неудач строка снимается. True, если строки больше не ждут."""
    cfg: Config = ctx.application.bot_data["cfg"]
    base = tasks.registry_dir(cfg.state_dir)
    target = tasks.note_target(task, cfg.primary_chat_id)
    try:
        with tasks.locked(base) as data:
            fresh = data.get(task["id"])
            if fresh is None or not fresh.get("note_due"):
                return True
            if target is None:
                fresh["note_due"] = None
                return True
            if not tasks.claim(fresh, "note_sending", now_local()):
                return False
            fresh = dict(fresh)
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable, close note of %s not sent: %s", task["id"], e)
        return False
    chat_id, ask_id = target
    reply = ReplyParameters(message_id=ask_id, allow_sending_without_reply=True) if ask_id else None
    try:
        await _send_retrying(lambda: ctx.bot.send_message(
            chat_id, tasks.close_note(fresh), reply_parameters=reply,
            link_preview_options=LinkPreviewOptions(is_disabled=True)))
    except Exception as e:
        log.warning("task close note failed %s", task["id"], exc_info=True)
        if not is_unconfirmed(e):
            with contextlib.suppress(OSError, ValueError):
                with tasks.locked(base) as data:
                    current = data.get(task["id"])
                    if current is not None:
                        tasks.release_note(current)
            return False
        # Неподтверждённая отправка: строка, скорее всего, дошла, повтор дал бы дубль.
    with contextlib.suppress(OSError, ValueError):
        _task_update(cfg, task["id"], note_due=None, note_sending=None,
                     noted=datetime.now().astimezone().isoformat(timespec="seconds"))
    return True


async def _task_offer_do(ctx: ContextTypes.DEFAULT_TYPE, task: dict, line: str) -> None:
    """Выполнение не началось: проект снова ждёт «Делай», строка на карточке
    и новая кнопка в теме (прежнюю сняло нажатие). Кнопка не ушла: отметка
    offer_due, кнопку пришлёт следующий старт (ревью Codex 19.10); слово
    «делай» работает и без неё."""
    await _b._task_card(ctx, task, line)
    cfg: Config = ctx.application.bot_data["cfg"]
    try:
        await ctx.bot.send_message(
            task["chat_id"], _("Выполнение не запустилось. Делать по проекту выше?"),
            message_thread_id=task["thread_id"], disable_notification=True,
            reply_markup=_markup(tasks.do_buttons(task["id"], task["plan_sha"])))
        offer_due = None
    except Exception as e:
        log.warning("task do offer failed %s", task["id"], exc_info=True)
        # Неподтверждённая отправка: кнопка, скорее всего, ушла, повтор дал бы дубль.
        offer_due = None if is_unconfirmed(e) else True
    with contextlib.suppress(OSError, ValueError):
        _task_update(cfg, task["id"], expect=("proposed",), expect_sha=task.get("plan_sha"), offer_due=offer_due)


async def _task_post_result(ctx: ContextTypes.DEFAULT_TYPE, task: dict) -> bool:
    """Итог выполнения из реестра в тему, когда ответ рана туда не ушёл
    (отметка result_unsent). Один отправитель за раз (захват result_posting);
    не вышло, отметка остаётся, повторит уборка."""
    cfg: Config = ctx.application.bot_data["cfg"]
    base = tasks.registry_dir(cfg.state_dir)
    try:
        with tasks.locked(base) as data:
            fresh = data.get(task["id"])
            if fresh is None or not fresh.get("result_unsent") or not fresh.get("thread_id"):
                return True
            if not tasks.claim(fresh, "result_posting", now_local()):
                return False
            fresh = dict(fresh)
    except (OSError, ValueError) as e:
        log.error("tasks registry unreadable, result of %s not posted: %s", task["id"], e)
        return False
    try:
        await _send_retrying(lambda: ctx.bot.send_message(
            fresh["chat_id"], tasks.unsent_result(fresh), message_thread_id=fresh["thread_id"],
            link_preview_options=LinkPreviewOptions(is_disabled=True)))
        done = True
    except Exception as e:
        log.warning("task result post failed %s", task["id"], exc_info=True)
        # Неподтверждённая отправка: итог, скорее всего, дошёл, повтор дал бы дубль.
        done = is_unconfirmed(e)
    with contextlib.suppress(OSError, ValueError):
        if done:
            _task_update(cfg, task["id"], result_unsent=None, result_posting=None)
        else:
            _task_update(cfg, task["id"], result_posting=None)
    return done


async def on_task_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки задачи: «▶️ Делай» под проектом, «🗑 Закрыть» на карточке,
    «Оставить» в напоминании. Переходы статуса под замком реестра."""
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    if user is None or user.id not in cfg.allowed_user_ids:
        return
    parsed = tasks.parse_callback(query.data or "")
    try:
        task = tasks.load(tasks.registry_dir(cfg.state_dir), strict=True).get(parsed[0]) if parsed else None
    except (OSError, ValueError) as e:
        # Нечитаемый реестр не значит «задача закрыта»: кнопки остаются
        # (проект шага 3, 1.8).
        log.error("tasks registry unreadable: %s", e)
        await _b._answer_quietly(query, _("Реестр задач не читается, см. журнал"), show_alert=True)
        return
    if parsed is None or task is None or task.get("status") not in tasks.OPEN_STATUSES:
        await _b._answer_quietly(query, _("Кнопка устарела или задача уже закрыта"))
        await strip_buttons(query)
        return
    tid, action, sha = parsed
    pressed_in = getattr(getattr(query.message, "chat", None), "id", None) or getattr(query.message, "chat_id", None)
    if pressed_in is not None and pressed_in != task.get("chat_id"):
        # Кнопки задачи действуют только в её группе (ревью Codex 19.10).
        await _b._answer_quietly(query, _("Кнопка не из темы этой задачи"), show_alert=True)
        return
    if action == "keep":
        await _b._answer_quietly(query, _("Оставляю"))
        await strip_buttons(query)
        return
    if action == "do":
        await _task_do(ctx, tid, sha, press=query.id, query=query)
        return
    # Закрытие: не посреди рана, иначе тема закроется, а агент продолжит
    # (ревью Codex 13.10). Замок темы берётся без ожидания: запрос, вставший
    # в очередь после, увидит закрытую задачу и не уйдёт в Claude.
    lock = _b._chat_lock(ctx, f"{task['chat_id']}:{task['thread_id']}")
    if lock.locked():
        await _b._answer_quietly(query, _("Агент в теме работает: дождись конца или /cancel, потом закрой"),
                              show_alert=True)
        return
    async with lock:
        closed = _task_update(cfg, tid, expect=tasks.CLOSABLE, status="closed",
                              closed=datetime.now().astimezone().isoformat(timespec="seconds"),
                              note_due=tasks.note_target(task, cfg.primary_chat_id) is not None or None)
    if closed is None:
        await _b._answer_quietly(query, _("Сейчас закрыть нельзя: идёт выполнение"), show_alert=True)
        return
    await _b._answer_quietly(query, _("Закрываю задачу"))
    with contextlib.suppress(Exception):
        await ctx.bot.close_forum_topic(task["chat_id"], task["thread_id"])
    await _b._task_card(ctx, closed, _("Задача закрыта. Тема удалится завтра утром."), buttons=False)
    await _task_news_item(ctx, closed, _("задача закрыта"))
    if closed.get("note_due"):
        await _b._task_note(ctx, closed)
    if query.message is not None and query.message.message_id != closed.get("card_id"):
        await strip_buttons(query)


async def _tasks_housekeeping(ctx: ContextTypes.DEFAULT_TYPE, startup: bool = False) -> None:
    """Раз в день и при старте: строку итога, не дошедшую до лички, ещё раз;
    удалить темы, закрытые до сегодня; напомнить о задачах, где неделю тихо. Ошибка «темы нет» снимает задачу, другие ошибки (сеть, лимиты)
    оставляют её до следующего раза (ревью Codex 13.10)."""
    cfg: Config = ctx.application.bot_data["cfg"]
    base = tasks.registry_dir(cfg.state_dir)
    sessions: SessionStore | None = ctx.application.bot_data.get("sessions")
    now = now_local()
    try:
        registry = tasks.load(base, strict=True)
    except (OSError, ValueError) as e:
        # Пустым нечитаемый реестр не считается: уборка ждёт починки (проект шага 3, 1.8).
        log.error("tasks registry unreadable, housekeeping skipped: %s", e)
        return
    for task in list(registry.values()):
        tid, thread_id = task["id"], task.get("thread_id")
        chat_key = f"{task['chat_id']}:{thread_id}"
        if task.get("note_due"):
            await _b._task_note(ctx, task)
        if task.get("result_unsent") and thread_id and task.get("status") in tasks.OPEN_STATUSES and (
                startup or not task.get("result_at")
                or now - datetime.fromisoformat(task["result_at"]) > tasks.NOTE_CLAIM):
            # На ходу только старый итог: свежий ещё может уходить ответом рана.
            await _task_post_result(ctx, task)
        if tasks.gone_expired(task, now) and not task.get("deleting"):
            with tasks.locked(base) as data:
                data.pop(tid, None)
            continue
        if task.get("status") == "gone" and task.get("deleting") and thread_id:
            # Рестарт между записью gone и удалением темы: удаление повторяется
            # (ревью Codex 16.10).
            try:
                await ctx.bot.delete_forum_topic(task["chat_id"], thread_id)
                done = True
            except BadRequest as e:
                done = "not found" in str(e).lower() or "topic_id_invalid" in str(e).lower()
            except TelegramError as e:
                log.warning("news task topic delete retry failed %s: %s", tid, e)
                done = False
            if done:
                _task_forget(ctx, chat_key)
                _task_update(cfg, tid, expect=("gone",), deleting=None)
            else:
                # Тема осталась: задача снова прежняя, обычная уборка повторит
                # удаление и не держит тему недоступной (ревью Codex 16.10).
                _task_update(cfg, tid, expect=("gone",), status=task["deleting"], gone=None, deleting=None)
            continue
        stale_creating = task.get("status") == "creating" and (
            now - datetime.fromisoformat(task["created"])).total_seconds() > 3600
        gone = False
        if stale_creating and not thread_id:
            gone = True
        elif thread_id and (stale_creating or tasks.delete_due(task, now)) \
                and not _b._chat_lock(ctx, chat_key).locked():
            # Тему, где идёт ран (/event, /at), не трогать: уборка повторит.
            # Запись уходит в gone до запроса к Telegram и по свежему статусу:
            # сообщение, пришедшее пока тема удаляется, уже не задача и не
            # пропадает молча вместе с темой (ревью Codex 16.10).
            was = task.get("status")
            with tasks.locked(base) as data:
                fresh = data.get(tid)
                due = fresh is not None and fresh.get("status") == was and (
                    stale_creating or tasks.delete_due(fresh, now))
                if due:
                    fresh.update(status="gone", gone=now.isoformat(timespec="seconds"), deleting=was)
            if not due:
                continue
            try:
                await ctx.bot.delete_forum_topic(task["chat_id"], thread_id)
                gone = True
            except BadRequest as e:
                gone = "not found" in str(e).lower() or "topic_id_invalid" in str(e).lower()
                if not gone:
                    log.warning("news task topic delete failed %s: %s", tid, e)
            except TelegramError as e:
                log.warning("news task topic delete failed %s: %s", tid, e)
            if not gone:
                # Тема осталась: задача снова прежняя, уборка повторит.
                _task_update(cfg, tid, expect=("gone",), status=was, gone=None, deleting=None)
                continue
        elif thread_id and sessions is not None and tasks.reminder_due(
                task, sessions.last_used(chat_key), now) and not _b._chat_lock(ctx, chat_key).locked():
            rows = tasks.card_buttons(tid, task.get("status", "")) + [
                [{"text": _("▶ Оставить"), "data": f"{tasks.CALLBACK_PREFIX}:{tid}:keep"}]]
            try:
                await ctx.bot.send_message(
                    task["chat_id"], tasks.reminder_text(task), message_thread_id=thread_id,
                    reply_markup=_markup(rows))
                _task_update(cfg, tid, reminded=now.isoformat(timespec="seconds"))
            except BadRequest as e:
                gone = "thread not found" in str(e).lower()
                if not gone:
                    log.warning("news task reminder failed %s: %s", tid, e)
            except TelegramError as e:
                log.warning("news task reminder failed %s: %s", tid, e)
                if is_unconfirmed(e):
                    # Напоминание, скорее всего, дошло: дату ставим, повтор дал бы дубль.
                    _task_update(cfg, tid, reminded=now.isoformat(timespec="seconds"))
        if not gone:
            continue
        if thread_id:
            _task_forget(ctx, chat_key)
        # Запись остаётся отметкой «пункт уже был в работе»: старая кнопка
        # в выпуске не заведёт задачу заново (ревью Codex 13.10).
        _task_update(cfg, tid, status="gone", gone=now.isoformat(timespec="seconds"), deleting=None)
        log.info("news task gone %s (was %s)", tid, task.get("status"))


async def _tasks_recover(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """При старте бота ранов нет. Выполнение, которое правда стартовало,
    становится failed с пометкой «прервал рестарт»: что успело сделаться,
    Иван проверит сам (ревью Codex 15.10: после аварии не повторять вслепую).
    Не стартовавшее (и запись из очереди до 19.10.2026) снова ждёт «Делай»."""
    cfg: Config = ctx.application.bot_data["cfg"]
    base = tasks.registry_dir(cfg.state_dir)
    moved: list[tuple[dict, str | None]] = []
    # Строго: нечитаемый реестр это ошибка в журнал у вызывающего, а не пустой.
    with tasks.locked(base) as data:
        for task in data.values():
            status = task.get("status")
            if status == "running" and task.get("exec_started"):
                task.update(status="failed", interrupted=True)
                tasks.push_result(task, _(tasks.EXEC_INTERRUPTED), False)
                moved.append((dict(task), tasks.EXEC_INTERRUPTED))
            elif status in ("running", "queued"):
                task.update(status="proposed", accepted_press=None, offer_due=True)
                task.pop("queued_at", None)
                moved.append((dict(task), None))
            elif status == "proposed" and task.get("offer_due"):
                # Кнопка после прошлого рестарта не ушла (_task_offer_do).
                moved.append((dict(task), None))
            else:
                continue
            log.warning("news task %s recovered: %s -> %s", task["id"], status, task["status"])
    # Обдумывание, оборванное аварией бота (OOM, kill): ответа не будет, тема
    # узнаёт об этом сама (разбор 19.10.2026).
    # Отметка снимается после отправки: сбой сети оставит её до следующего
    # старта (ревью Codex 19.10); темы нет, снимается тоже.
    cut = [dict(t) for t in tasks.load(base, strict=True).values() if t.get("think_open")]
    for task in cut:
        said = not (task.get("status") in tasks.OPEN_STATUSES and task.get("thread_id"))
        if not said:
            log.warning("news task %s: thinking cut by bot restart", task["id"])
            try:
                await ctx.bot.send_message(task["chat_id"], _(tasks.THINK_INTERRUPTED),
                                           message_thread_id=task["thread_id"])
                said = True
            except BadRequest:
                said = True
            except Exception as e:
                log.warning("cut thinking notice failed %s", task["id"], exc_info=True)
                # Неподтверждённая отправка: сообщение, скорее всего, дошло, повтор дал бы дубль.
                said = is_unconfirmed(e)
        if said:
            with contextlib.suppress(OSError, ValueError):
                _task_update(cfg, task["id"], think_open=None)
    for task, line in moved:
        if task["status"] == "proposed":
            await _task_offer_do(ctx, task, _("Выполнение не запустилось из-за рестарта: скажи «делай» "
                                              "ещё раз."))
        else:
            await _b._task_card(ctx, task, line)
    # Карточка живой задачи, не ушедшая при заведении темы: заново (ревью Codex 14.10).
    registry = tasks.load(base, strict=True)
    for task in list(registry.values()):
        status = task.get("status")
        if (status in tasks.OPEN_STATUSES and status != "creating" and task.get("thread_id")
                and not task.get("card_id") and not task.get("card_unsure")):
            log.warning("news task %s without card, sending it", task["id"])
            await _task_new_card(ctx, task, tasks.stage_line(task), status)


async def _tasks_startup(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Через полминуты после старта: права бота в группе новостей и уборка."""
    chat_id = _tasks_chat_id(ctx.application.bot_data["cfg"])
    if chat_id is not None:
        try:
            me = await ctx.bot.get_me()
            member = await ctx.bot.get_chat_member(chat_id, me.id)
            missing = [label for attr, label in (("can_manage_topics", "управление темами"),
                                                 ("can_delete_messages", "удаление сообщений"))
                       if not getattr(member, attr, False)]
            if missing:
                log.warning("news tasks: у бота нет прав: %s", ", ".join(missing))
        except TelegramError as e:
            log.warning("news tasks rights check failed: %s", e)
    try:
        await _tasks_housekeeping(ctx, startup=True)
    except (OSError, ValueError) as e:
        log.error("news tasks registry unreadable: %s", e)


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

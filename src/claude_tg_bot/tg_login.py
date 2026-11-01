"""Вход в Claude и Codex из Telegram (перенесено из bot.py, шаг 9 разбиения 2)."""
from __future__ import annotations

import contextlib
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop, ContextTypes

from .auth import looks_like_auth_code
from .i18n import _

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


def _login_keyboard(ctx, engines=("claude", "codex"), *, private: bool = True):
    rows = []
    for engine in engines:
        label = _("Восстановить вход {engine}").format(engine='Claude' if engine == 'claude' else 'Codex')
        action = {"callback_data": f"login:{engine}"} if private else {
            "url": f"https://t.me/{ctx.bot.username}?start=login_{engine}"
        }
        rows.append([InlineKeyboardButton(label, **action)])
    return InlineKeyboardMarkup(rows)


async def _offer_login(update, ctx, engine):
    cfg = ctx.application.bot_data.get("cfg")
    if cfg is not None and _b._is_workshop(cfg, _b._chat_key(update)[0]):
        # Мастерская заглушена: кнопка входа в личку, туда же ведёт и вход.
        with contextlib.suppress(Exception):
            await ctx.bot.send_message(
                cfg.primary_chat_id,
                _("🛠 Мастерская: нужно восстановить вход {engine}.").format(
                    engine='Claude' if engine == 'claude' else 'Codex'),
                reply_markup=_login_keyboard(ctx, (engine,), private=True))
        return
    try:
        await update.effective_message.reply_text(
            _("Нужно восстановить вход {engine}. "
              "Кнопка открывает вход в личном чате с ботом.").format(engine='Claude' if engine == 'claude' else 'Codex'),
            reply_markup=_login_keyboard(ctx, (engine,), private=update.effective_chat.type == "private"),
        )
    except Exception:
        log.warning("login button delivery failed for %s", engine)


async def _start_login(update, ctx, engine: str):
    cfg = ctx.application.bot_data["cfg"]
    if not _b._is_allowed(cfg, update):
        return
    msg = update.effective_message
    if update.effective_chat.type != "private":
        await msg.reply_text(_("Восстановление входа доступно в личном чате с ботом."),
                             reply_markup=_login_keyboard(ctx, private=False))
        return
    manager = ctx.application.bot_data["login_manager"]
    if engine == "cancel":
        cancelled = await manager.cancel(update.effective_user.id, update.effective_chat.id)
        await msg.reply_text(_("Вход отменён.") if cancelled else _("Нет активного входа."))
        return
    if engine not in ("claude", "codex"):
        await msg.reply_text(_("Выбери, какой вход восстановить. Отмена: /login cancel"),
                             reply_markup=_login_keyboard(ctx))
        return
    if not manager.binaries[engine]:
        await msg.reply_text(_("Codex не настроен: отсутствует CODEX_BIN."))
        return
    if any(lock.locked() for lock in ctx.application.bot_data.get("chat_locks", {}).values()):
        await msg.reply_text(_("Сначала дождись завершения запросов бота или отмени их через /cancel в соответствующем чате."))
        return
    if not manager.start(engine, update.effective_user.id, update.effective_chat.id):
        await msg.reply_text(_("Уже идёт восстановление входа. Дождись результата или используй /login cancel."))


async def on_login_callback(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _b._is_allowed(ctx.application.bot_data["cfg"], update):
        await update.callback_query.answer()
        return
    await update.callback_query.answer()
    await _b._start_login(update, ctx, update.callback_query.data.split(":", 1)[1])


async def on_auth_update(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or update.callback_query is not None:
        return
    text = msg.text or msg.caption or ""
    parts = text.split()
    command = parts[0].split("@", 1)[0].lower() if parts and text.startswith("/") else ""
    manager = ctx.application.bot_data["login_manager"]
    session = manager.session
    user = update.effective_user
    chat = update.effective_chat
    owner = session is not None and user is not None and chat is not None and (
        session.user_id, session.chat_id
    ) == (user.id, chat.id)
    reply = getattr(msg, "reply_to_message", None)
    quoted = (reply.text or reply.caption or "") if reply is not None else ""
    login_active = session is not None
    secret = command == "/authcode" or looks_like_auth_code(text, login_active=login_active)
    quoted_secret = quoted.lower().startswith("/authcode") or looks_like_auth_code(
        quoted, login_active=login_active
    )
    # Пересланное это чужой текст, а не команда: пересланное «/login claude»
    # запускало вход и на 10 минут отклоняло все запросы к моделям. Сообщения,
    # похожие на код входа, удаляются по-прежнему (ревью 12.10.2026).
    forwarded = getattr(msg, "forward_origin", None) is not None
    login_command = not forwarded and (command == "/login" or (
        command == "/start" and len(parts) == 2 and parts[1] in ("login_claude", "login_codex")
    ))
    if not (secret or quoted_secret or owner or login_command):
        return
    if not _b._is_allowed(ctx.application.bot_data["cfg"], update):
        raise ApplicationHandlerStop
    if secret:
        with contextlib.suppress(Exception):
            await msg.delete()
    if update.edited_message is not None or update.edited_channel_post is not None:
        raise ApplicationHandlerStop
    if secret:
        if chat.type != "private":
            answer = _("Код входа принимается только в личном чате с ботом.")
        else:
            code = text[len(parts[0]):].strip() if command == "/authcode" else text.strip()
            answer = await manager.submit(user.id, chat.id, code)
        # Do not quote the deleted message containing the code.
        await ctx.bot.send_message(chat_id=chat.id, text=answer, message_thread_id=msg.message_thread_id)
    elif login_command:
        engine = parts[1] if len(parts) == 2 else ""
        if command == "/start":
            engine = engine.removeprefix("login_")
        await _b._start_login(update, ctx, engine)
    elif owner and command == "/cancel" and not forwarded:
        await _b._start_login(update, ctx, "cancel")
    elif quoted_secret:
        await ctx.bot.send_message(chat_id=chat.id, text=_("Сообщения с цитатой кода входа не передаются модели."))
    else:
        await msg.reply_text(_("Идёт восстановление входа. Дождись результата или используй /login cancel. Код Claude: /authcode КОД."))
    raise ApplicationHandlerStop


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

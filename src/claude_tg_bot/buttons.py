"""Кнопки под сообщениями: сборка, снятие, строка итога в карточке.

Кнопка задаётся словарём `{"text": подпись, "data": данные}` или
`{"text": подпись, "url": адрес}`, клавиатура списком рядов. Из него
`keyboard` собирает сырой JSON для Bot API (notify, handoff, otklik без PTB),
`markup` разметку PTB. Данные кнопки Telegram принимает до 64 байт: длиннее,
и сообщение отвергается целиком, поэтому проверка здесь, до отправки
(`notify --buttons` берёт данные от агента).

PTB импортируется внутри функций: notify, handoff и otklik запускаются без него.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Сообщение удалено или устарело для правки: обычное дело, не сбой бота.
_GONE = ("message to edit not found", "message can't be edited")


def _gone(e: BaseException) -> bool:
    text = str(e).lower()
    return any(g in text for g in _GONE)

CALLBACK_DATA_LIMIT = 64


def _button(b: dict[str, Any]) -> dict[str, str]:
    """Одна кнопка в виде Bot API. Нет подписи или действия: KeyError или
    TypeError, данные длиннее 64 байт: ValueError."""
    text = b["text"]
    if not isinstance(text, str):
        raise TypeError("button text must be a string")
    if "url" in b:
        url = b["url"]
        if not isinstance(url, str):
            raise TypeError("button url must be a string")
        return {"text": text, "url": url}
    data = b["data"]
    if not isinstance(data, str):
        raise TypeError("button data must be a string")
    size = len(data.encode("utf-8"))
    if not 1 <= size <= CALLBACK_DATA_LIMIT:
        raise ValueError(f"button data must be 1-{CALLBACK_DATA_LIMIT} bytes, got {size}")
    return {"text": text, "callback_data": data}


def keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Сырой `reply_markup` для Bot API. Пустой список: клавиатура без кнопок
    (правка ею кнопки снимает)."""
    return {"inline_keyboard": [[_button(b) for b in row] for row in rows]}


def markup(rows: list[list[dict[str, Any]]]) -> Any:
    """Разметка PTB (`InlineKeyboardMarkup`); пустой список: None."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not rows:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b["text"], url=b["url"]) if "url" in b
         else InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
        for row in keyboard(rows)["inline_keyboard"]])


async def strip_buttons(target: Any, chat_id: int | None = None, message_id: int | None = None, *,
                        message: bool = False) -> bool | None:
    """Снять кнопки: `strip_buttons(query)`, `strip_buttons(msg, message=True)`
    или `strip_buttons(bot, chat_id, message_id)`. True: сняты. None: их уже
    не было («message is not modified»), тихо. False: другой сбой, он в
    журнале, наружу не выходит. Отмена задачи проходит насквозь."""
    from telegram.error import BadRequest

    from . import tg_api

    try:
        if message_id is not None:
            await target.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        elif message:
            await target.edit_reply_markup(reply_markup=None)
        else:  # CallbackQuery
            await target.edit_message_reply_markup(reply_markup=None)
    except BadRequest as e:
        if tg_api.not_modified_text(str(e)):
            return None
        (log.info if _gone(e) else log.warning)("strip buttons failed: %s", e)
        return False
    except Exception as e:
        log.warning("strip buttons failed: %s: %s", type(e).__name__, e)
        return False
    return True


def fits_card(card_html: str, line: str) -> bool:
    """Карточка со строкой итога влезет в одно сообщение. Длина HTML не
    меньше длины показанного текста, так что проверка с запасом."""
    from . import tg_api
    from .replies import utf16_len

    return bool(card_html) and utf16_len(card_html) + utf16_len(line) + 2 <= tg_api.TEXT_LIMIT


async def note_on_card(target: Any, card_html: str, line: str, what: str,
                       chat_id: int | None = None, message_id: int | None = None, *,
                       parse_mode: str | None = "HTML") -> bool:
    """Дописать строку итога в карточку той же правкой, что снимает кнопки:
    `note_on_card(query, ...)` или `note_on_card(bot, ..., chat_id, message_id)`.
    True: строка в карточке (уже была там: «not modified»; или правка ушла без
    подтверждения: обычно прошла, отдельная строка была бы вторым показом,
    ревью Codex 05.11.2026). False: не вышло (не влезает, сообщения нет, сбой),
    кнопки сняты, насколько удалось; зовущий
    пишет строку отдельным сообщением. `what` метка для журнала; карточка
    простым текстом (`card.text`): `parse_mode=None`."""
    from telegram.error import BadRequest

    from . import tg_api
    from .replies import is_unconfirmed

    if fits_card(card_html, line):
        text = f"{card_html}\n\n{line}"
        try:
            if message_id is not None:
                await target.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                               parse_mode=parse_mode, reply_markup=None)
            else:
                await target.edit_message_text(text, parse_mode=parse_mode, reply_markup=None)
            return True
        except BadRequest as e:
            if tg_api.not_modified_text(str(e)):
                return True  # строка уже в карточке: второй показ не нужен
            if _gone(e):
                log.info("card edit failed (%s): %s", what, e)
                return False  # снимать кнопки не с чего
            log.warning("card edit failed (%s), stripping buttons: %s", what, e)
        except Exception as e:
            if is_unconfirmed(e):
                log.warning("card edit unconfirmed (%s), no reply", what)
                return True
            log.warning("card edit failed (%s), stripping buttons", what, exc_info=True)
    await strip_buttons(target, chat_id, message_id)
    return False

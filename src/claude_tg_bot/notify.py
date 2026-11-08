"""Отправка уведомления через нашего бота.

Usage:
    python -m claude_tg_bot.notify "текст"                    # в личку (primary_chat_id)
    echo "длинный текст" | python -m claude_tg_bot.notify     # то же, из stdin
    python -m claude_tg_bot.notify --chat-id -1001 --thread-id 42 "текст"
    python -m claude_tg_bot.notify --html --no-preview --silent ... "<b>текст</b>"
    python -m claude_tg_bot.notify --silent --pin "текст"     # закрепить, последней строкой message_id: N
    python -m claude_tg_bot.notify --unpin N                  # открепить (в чат/тему тот же --chat-id)

Закреп текста, который начинается с «Вопрос дня», один на дату и чат: второй
получает отказ и номер первого (разбор недели помощника W43, предложение 7,
решение Ивана 25.10.2026). Учёт в state/daily_pins.json; неудачная отправка
не записывается, и повтор после сбоя проходит.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from .config import load_config, local_zone
from .i18n import _
from . import jsonstore, reactions, tg_api
from .buttons import keyboard


def main(argv: list[str] | None = None) -> int:
    """CLI-обёртка: код возврата 0/1 (2 от argparse). message_id не нужен,
    см. send_message. Из командной строки (так зовёт агент) `--chat-id` только
    в чаты бота. Негодные настройки: код 1 и строка, а не трейсбек."""
    try:
        rc, _unused = _run(argv, any_chat=False)
    except RuntimeError as e:
        print(f"notify: {e}", file=sys.stderr)
        return 1
    return rc


def send_message(argv: list[str]) -> tuple[int, int | None]:
    """То же, что main, но отдаёт ещё и message_id ПЕРВОГО отправленного чанка
    (якорь, по которому отправитель может узнать ответ человека). Скиллы
    шлют в свои чаты из .env (NEWS_TG_CHAT_ID и др.), их не сверяем.
    Ошибка разбора аргументов (argparse бросает SystemExit) и негодные
    настройки (load_config бросает RuntimeError) отдаются кодом: иначе
    вызывающий скилл выходил мимо своего rc, не сохранив состояние (ревизия
    07.11.2026)."""
    try:
        return _run(argv)
    except SystemExit as e:
        return (e.code if isinstance(e.code, int) and e.code else 2), None
    except RuntimeError as e:
        print(f"notify: {e}", file=sys.stderr)
        return 1, None


def _run(argv: list[str] | None = None, *, any_chat: bool = True) -> tuple[int, int | None]:
    parser = argparse.ArgumentParser(prog="claude_tg_bot.notify", add_help=True)
    parser.add_argument("text", nargs="*", help="текст; пусто = читается stdin")
    parser.add_argument("--chat-id", type=int, default=None, help="целевой чат (default: личка)")
    parser.add_argument(
        "--thread-id", type=int, default=None, help="forum-тема; требует --chat-id"
    )
    parser.add_argument(
        "--buttons",
        default="",
        help=(
            'inline-кнопки JSON: [[{"text":"...","data":"..."}],...] (ряды). '
            "Прикрепляются только к ПОСЛЕДНЕМУ чанку. Невалидный JSON — ошибка до отправки."
        ),
    )
    parser.add_argument(
        "--html", action="store_true",
        help="parse_mode HTML; такой текст не режется на части (тег разорвался бы), длиннее лимита = ошибка",
    )
    parser.add_argument("--no-preview", action="store_true", help="без карточки-превью ссылок")
    parser.add_argument("--silent", action="store_true", help="без звука уведомления")
    parser.add_argument(
        "--pin", action="store_true",
        help="закрепить первое сообщение без звука и вывести его message_id (для --unpin)",
    )
    parser.add_argument(
        "--unpin", type=int, default=None, metavar="MESSAGE_ID",
        help="открепить сообщение по id (текст не отправляется)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.unpin is not None and args.pin:
        print(_("notify: --pin и --unpin вместе нельзя"), file=sys.stderr)
        return 1, None

    text = ""
    if args.unpin is None:
        text = " ".join(args.text) if args.text else sys.stdin.read()
        text = text.strip()
        if not text:
            print("usage: python -m claude_tg_bot.notify <text>  |  stdin", file=sys.stderr)
            return 1, None

    reply_markup: dict | None = None
    if args.buttons:
        # Валидируем ДО отправки: сообщение без кнопок, на которое рассчитан
        # callback-обработчик, хуже честной ошибки.
        try:
            rows = json.loads(args.buttons)
            # Только кнопки с данными, как раньше: ссылки агент шлёт текстом.
            reply_markup = keyboard([[{"text": b["text"], "data": b["data"]} for b in row] for row in rows])
        except (ValueError, KeyError, TypeError) as e:
            print(_("notify: --buttons невалиден: {error}").format(error=e), file=sys.stderr)
            return 1, None

    if args.thread_id is not None and args.chat_id is None:
        print(_("notify: --thread-id требует --chat-id"), file=sys.stderr)
        return 1, None

    # skip_hook_validation: notify — диагностический канал, ему нужны только
    # Telegram-настройки. Не должен зависеть от целостности hook-settings —
    # иначе если hook сломан, нельзя даже пинг "бот упал" отправить.
    cfg = load_config(skip_hook_validation=True)

    thread_id: int | None
    if args.chat_id is not None:
        # Только чаты бота, как POST /event: личка разрешённого пользователя
        # или группа из TELEGRAM_ALLOWED_CHAT_IDS. Агент без карточки иначе
        # писал бы ботом в любую группу, где бот состоит (задание guard2).
        if not any_chat and args.chat_id not in cfg.allowed_chat_ids \
                and args.chat_id not in cfg.allowed_user_ids:
            print(_("notify: чат {chat} не из разрешённых (TELEGRAM_ALLOWED_CHAT_IDS)").format(
                chat=args.chat_id), file=sys.stderr)
            return 1, None
        chat_id, thread_id = args.chat_id, args.thread_id
    else:
        chat_id, thread_id = cfg.primary_chat_id, None

    if args.unpin is not None:
        return _unpin(cfg.telegram_bot_token, chat_id, args.unpin), None

    daily = _daily_slot(cfg, text, chat_id, thread_id) if args.pin else None
    if daily is None:
        return _deliver(cfg, args, text, chat_id, thread_id, reply_markup, None)
    # Проверка, отправка и запись под одним замком: два параллельных вызова
    # не отправят по вопросу и не затрут учёт друг друга.
    with _daily_lock(daily[0]):
        if (prev := _daily_sent(*daily)) is not None:
            if prev:
                print(_("notify: «{prefix}» за {day} уже отправлен и закреплён, второй не шлю").format(
                    prefix=DAILY_PREFIX, day=daily[1].split("|")[-1]), file=sys.stderr)
                print(f"message_id: {prev}")
            else:
                # Отметка 0: отправка не подтверждена, номера и закрепа нет.
                print(f"notify: «{DAILY_PREFIX}» за {daily[1].split('|')[-1]} уже отправлен без "
                      "подтверждения, номера нет, второй не шлю", file=sys.stderr)
            return 1, prev
        return _deliver(cfg, args, text, chat_id, thread_id, reply_markup, daily)


def _deliver(cfg, args: argparse.Namespace, text: str, chat_id: int, thread_id: int | None,
             reply_markup: dict | None, daily: tuple[Path, str] | None) -> tuple[int, int | None]:
    """Отправка частями, закреп и вывод message_id (тело прежнего _run)."""
    limit = cfg.telegram_text_limit
    # Защита и для вызовов с подставной конфигурацией (ревью 12.10.2026).
    if not 1 <= limit <= tg_api.TEXT_LIMIT:
        print(_("notify: недопустимый лимит текста"), file=sys.stderr)
        return 1, None

    # Резка общая с ответами бота: по строкам, длина в UTF-16, как считает
    # Telegram (ревизия повторов 07.11.2026, п. 4).
    from .replies import split_for_telegram
    chunks = split_for_telegram(text, limit)
    if args.html and len(chunks) > 1:
        print(_("notify: --html текст длиннее {limit}, резать разметку нельзя").format(limit=limit),
              file=sys.stderr)
        return 1, None
    first_message_id: int | None = None
    for idx, ch in enumerate(chunks):
        payload: dict = {"chat_id": chat_id, "text": ch}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if reply_markup is not None and idx == len(chunks) - 1:  # кнопки под последней, как в боте
            payload["reply_markup"] = reply_markup
        if args.html:
            payload["parse_mode"] = "HTML"
        if args.no_preview:
            payload["link_preview_options"] = {"is_disabled": True}
        if args.silent:
            payload["disable_notification"] = True
        # Без raise_for_status: его HTTPStatusError несёт полный URL с
        # bot-токеном и через traceback утёк бы в journald вызывающих юнитов.
        try:
            # Неушедшее и 429 повторяются сами: дубля нет, а отказ части
            # вёл к повтору всего сообщения вызывающим.
            r = tg_api.post(cfg.telegram_bot_token, "sendMessage", json=payload, timeout=10.0,
                            retry_not_sent=True)
        except httpx.ReadTimeout:
            # Запрос ушёл, ответа нет: часть обычно доходит (случаи 30-31.10.2026,
            # replies.is_unconfirmed). Код 1 давал повтор вызывающим (сводка
            # чатов слала себя ещё раз) и обрывал остаток (аудит outbound).
            print(_("notify: Telegram не подтвердил часть {n}, не повторяю: она могла дойти").format(
                n=idx + 1), file=sys.stderr)
            if idx == 0 and daily is not None:
                # Номера нет, но вопрос дня ушёл: второй за дату не шлём (0 = id неизвестен).
                # Код при --pin прежний (1 без message_id): так его читает промт Домового.
                _daily_mark(*daily, 0)
            continue
        except httpx.HTTPError as e:
            print(f"notify: HTTP error: {type(e).__name__}", file=sys.stderr)
            _print_id_on_failure(args.pin, first_message_id)
            return 1, first_message_id
        if r.status_code != 200:
            print(f"notify: sendMessage failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
            _print_id_on_failure(args.pin, first_message_id)
            return 1, first_message_id
        with contextlib.suppress(AttributeError, ValueError):
            reactions.remember_sent(r.json(), ch, cfg.state_dir)
        if idx == 0:  # id только первой части: ответ без id не должен подменяться id второй
            try:
                first_message_id = int(r.json()["result"]["message_id"])
            except (AttributeError, ValueError, KeyError, TypeError):
                pass  # доставку это не ломает, просто не будет якоря для reply
            if daily is not None and first_message_id is not None:
                # Сразу после доставки: сбой следующей части или закрепа
                # не должен открыть дорогу второму вопросу.
                _daily_mark(*daily, first_message_id)
    dest = f"chat_id={chat_id}" + (f" thread_id={thread_id}" if thread_id is not None else "")
    print(f"sent {len(chunks)} message(s) to {dest}")
    if args.pin:
        # Сообщение уже ушло, поэтому id печатаем всегда, а сбой закрепа даёт
        # код 1: вызывающий не должен слать заново (дубль), а записать id.
        if first_message_id is None:
            print("notify: pin невозможен, message_id не получен", file=sys.stderr)
            return 1, None
        print(f"message_id: {first_message_id}")
        if not _pin_call(cfg.telegram_bot_token, "pinChatMessage", chat_id, first_message_id,
                         silent=True):
            return 1, first_message_id
    return 0, first_message_id


DAILY_PREFIX = "Вопрос дня"
DAILY_KEEP_SEC = 40 * 24 * 3600


def _daily_slot(cfg, text: str, chat_id: int, thread_id: int | None) -> tuple[Path, str] | None:
    """(файл учёта, ключ «чат|дата») для закрепа «Вопрос дня», иначе None.
    Без state_dir в конфиге учёта нет."""
    state_dir = getattr(cfg, "state_dir", None)
    if state_dir is None or not text.startswith(DAILY_PREFIX):
        return None
    where = f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)
    return Path(state_dir) / "daily_pins.json", f"{where}|{datetime.now(local_zone()).date().isoformat()}"


@contextlib.contextmanager
def _daily_lock(path: Path):
    """Замок учёта на время проверки, отправки и записи. Замок не взялся
    (каталога нет, права): идём без него, доставка важнее."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path.with_suffix(".lock"), "a")
    except OSError:
        yield
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()


def _daily_load(path: Path) -> dict:
    return jsonstore.read_dict(path)


def _daily_sent(path: Path, key: str) -> int | None:
    entry = _daily_load(path).get(key)
    try:
        return int(entry["message_id"])
    except (TypeError, KeyError, ValueError):
        return None


def _daily_mark(path: Path, key: str, message_id: int) -> None:
    """Запись атомарно (временный файл и замена). Сбой записи доставку не
    ломает, лишь не защитит от второго вопроса. Дата берётся до отправки:
    отправка, пересёкшая полночь, числится за прошлым днём."""
    now = time.time()

    def fresh(v: object) -> bool:
        try:
            return isinstance(v, dict) and now - float(v.get("at") or 0) < DAILY_KEEP_SEC
        except (TypeError, ValueError):
            return False
    data = {k: v for k, v in _daily_load(path).items() if fresh(v)}
    data[key] = {"message_id": message_id, "at": now}
    try:
        jsonstore.write_json(path, data)
    except OSError as e:
        print(f"notify: daily_pins не записан: {type(e).__name__}", file=sys.stderr)


def _print_id_on_failure(pin: bool, message_id: int | None) -> None:
    """Сбой на второй и дальше части: первая уже доставлена, id нужен вызывающему,
    иначе он пошлёт всё заново (дубль)."""
    if pin and message_id is not None:
        print(f"message_id: {message_id}")


def _pin_call(token: str, method: str, chat_id: int, message_id: int, *, silent: bool = False) -> bool:
    """pinChatMessage / unpinChatMessage. Токен в логи не попадает (см. sendMessage)."""
    payload: dict = {"chat_id": chat_id, "message_id": message_id}
    if silent:
        payload["disable_notification"] = True
    try:
        r = tg_api.post(token, method, json=payload, timeout=10.0, retry_any=True)
    except httpx.HTTPError as e:
        print(f"notify: {method} HTTP error: {type(e).__name__}", file=sys.stderr)
        return False
    if tg_api.not_modified(r):  # повтор уже прошедшего закрепа
        return True
    if r.status_code != 200:
        print(f"notify: {method} failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return True


def _unpin(token: str, chat_id: int, message_id: int) -> int:
    try:
        r = tg_api.post(token, "unpinChatMessage",
                        json={"chat_id": chat_id, "message_id": message_id}, timeout=10.0, retry_any=True)
    except httpx.HTTPError as e:
        print(f"notify: unpinChatMessage HTTP error: {type(e).__name__}", file=sys.stderr)
        return 1
    # Уже откреплено или сообщения нет: цель достигнута. «chat not found» и
    # прочее остаётся ошибкой.
    if r.status_code == 400 and "message to unpin not found" in r.text.lower():
        print(f"unpinned message_id={message_id} (already not pinned)")
        return 0
    if r.status_code != 200:
        print(f"notify: unpinChatMessage failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return 1
    print(f"unpinned message_id={message_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

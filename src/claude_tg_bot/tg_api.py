"""Связь с Bot API Telegram: адрес сервера, токен в адресе, тайм-ауты, повторы.

Адрес сервера один на всех: `TELEGRAM_BOT_API_URL` в .env, по умолчанию
облако `https://api.telegram.org`. Свой сервер `telegram-bot-api` на ПК
(BACKLOG, намерение 07.11.2026) включается одной строкой
`TELEGRAM_BOT_API_URL=http://127.0.0.1:8081`: на него идут бот (PTB),
notify, handoff, otklik, счёт, уведомление об остановке и bot-down-notify.py
(последний без пакета, читает ту же строку .env сам). После `logOut` с облака
запрос токеном в облако вернул бы бота туда, поэтому адрес нигде больше
не пишется руками.

Повторы. Повторять можно только то, что точно не ушло: соединение или TLS не
установлены, пул занят (`NOT_SENT`). Тайм-аут чтения (`is_unconfirmed`) значит,
что запрос ушёл и ответа нет: сообщение обычно доходит (случаи 30-31.10.2026),
повтор дал бы дубль. ReadError сюда не входит ни в одну сторону: httpcore
отдаёт сбой записи запроса как ReadError, запрос мог и дойти, и нет (ревью
05.11.2026), поэтому он не повторяется и подтверждением не считается.
Ответ 429 значит, что Telegram запрос не выполнил: его тоже можно повторить
после паузы. Правки (edit*, pin*) повторять можно при любом сбое связи.
Что делает каждое место при каждом сбое: docs/tg-send-failures-2026-11-07.md.

Токен. Он есть только в адресе запроса, и адрес собирается только здесь.
Исключения httpx наружу выходят пересобранными: того же класса (вызывающие
ловят ReadTimeout и HTTPError как раньше), без запроса и без исходной цепочки,
токен в тексте заменён. Ответ (httpx.Response) отдаётся как есть: его repr
адреса не несёт, а адрес запроса в нём вызывающие не печатают.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

ENV = "TELEGRAM_BOT_API_URL"
DEFAULT_URL = "https://api.telegram.org"
# Предел текста одного сообщения Telegram, в единицах UTF-16.
TEXT_LIMIT = 4096
# Запрос точно не ушёл: повтор не даст дубля.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Паузы повторов при NOT_SENT (общие с replies._send_retrying).
RETRY_DELAYS = (2.0, 5.0)
# 429: Telegram запрос не выполнил, пауза retry_after плюс секунда; как у
# replies, не больше двух раз и не дольше минуты.
FLOOD_RETRIES = 2
FLOOD_WAIT_MAX = 60.0


def _loopback(host: str) -> bool:
    if host.lower() in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_url(raw: str | None) -> str:
    """Адрес сервера Bot API без хвостовой косой черты; пусто: облако.
    Только схема, узел и порт. http только на loopback: по сети токен шёл бы
    открытым текстом. Текст ошибки адреса не повторяет."""
    if not (raw or "").strip():
        return DEFAULT_URL
    value = raw.strip().rstrip("/")
    try:
        u = urlsplit(value)
        port_ok = u.port is None or u.port > 0
    except ValueError:
        port_ok = False
        u = None
    # Пустые `?` и `#` urlsplit не видит, а путь с токеном ушёл бы в запрос
    # или фрагмент; пробел, управляющие символы (urlsplit молча выкидывает
    # перевод строки) и пустой порт тоже опечатка.
    bad_char = any(c in "?#@" or ord(c) < 33 for c in value) or value.endswith(":")
    if (u is None or bad_char or not port_ok or u.scheme not in ("http", "https") or not u.hostname
            or u.path or u.query or u.fragment or u.username or u.password):
        raise ValueError(f"{ENV}: нужен адрес вида https://узел[:порт] без пути")
    if u.scheme == "http" and not _loopback(u.hostname):
        raise ValueError(f"{ENV}: http только для своего сервера на 127.0.0.1")
    return value


def api_url(env: Mapping[str, str] | None = None) -> str:
    """Адрес из окружения (load_config уже положил туда .env)."""
    return parse_url((os.environ if env is None else env).get(ENV))


def is_local(url: str) -> bool:
    """Свой сервер на loopback: прокси из окружения ему не нужен, а http-прокси
    увидел бы токен открытым текстом."""
    return _loopback(urlsplit(url).hostname or "")


def on_local_server(cfg: Any) -> bool:
    """Бот работает через свой сервер Bot API (--local): вложения любого
    размера через getFile, отправка файлов путём до 2000 МБ."""
    return is_local(getattr(cfg, "telegram_bot_api_url", "") or "")


def ptb_urls(url: str) -> tuple[str, str]:
    """base_url и base_file_url для PTB Bot: токен PTB дописывает сам."""
    return f"{url}/bot", f"{url}/file/bot"


def is_not_sent(e: BaseException) -> bool:
    return isinstance(e, NOT_SENT)


def is_unconfirmed(e: BaseException) -> bool:
    """Запрос ушёл, ответа не дождались: не повторять, это не отказ."""
    return isinstance(e, httpx.ReadTimeout)


def flood_wait(r: httpx.Response) -> float | None:
    """Пауза перед повтором по ответу 429, иначе None."""
    if r.status_code != 429:
        return None
    try:
        ra = float(r.json()["parameters"]["retry_after"])
    except (AttributeError, ValueError, KeyError, TypeError):
        ra = 1.0
    return max(ra, 0.0) + 1.0


def not_modified_text(text: str | None) -> bool:
    """Текст ошибки Telegram «message is not modified» (ответ или BadRequest PTB)."""
    return "message is not modified" in (text or "").lower()


def not_modified(r: httpx.Response) -> bool:
    """Правка уже применена (повтор edit*): 400 «message is not modified»."""
    return r.status_code == 400 and not_modified_text(r.text)


class TokenLogFilter(logging.Filter):
    """Токен бота в записи журнала заменяется на <token>: на своём сервере он
    стоит в пути к файлам (~/.local/share/telegram-bot-api/<токен>/), и ошибка
    копирования вложения печатала бы его с трассировкой. Ставится на обработчики."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.token:
            return True
        # Handler.handle зовёт фильтр вне своей защиты: исключение отсюда
        # уронило бы место вызова журнала. Сообщение, которое не собирается,
        # заменяем целиком: handleError напечатал бы аргументы как есть.
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            msg = record.getMessage()
            if self.token in msg:
                record.msg, record.args = scrub(msg, self.token), None
            if record.exc_text:
                record.exc_text = scrub(record.exc_text, self.token)
            if record.stack_info:
                record.stack_info = scrub(record.stack_info, self.token)
        except Exception:  # noqa: BLE001
            record.msg, record.args = "<сообщение журнала не собрано>", None
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def scrub(text: str, *tokens: str) -> str:
    """Токены в тексте на <token> (пустые пропускаются)."""
    for token in tokens:
        if token:
            text = text.replace(token, "<token>")
    return text


def _clean(e: Exception, token: str) -> Exception:
    text = scrub(str(e), token)
    if isinstance(e, httpx.InvalidURL):
        return httpx.RequestError(text)
    try:
        return type(e)(text)
    except TypeError:
        return httpx.RequestError(text)


def post(token: str, method: str, *, json: Any = None, data: Any = None, files: Any = None,
         timeout: float = 10.0, retry_not_sent: bool = False, retry_any: bool = False) -> httpx.Response:
    """POST к методу Bot API. Одна попытка. С retry_not_sent ещё до двух, если
    запрос точно не ушёл, и до двух после паузы 429 (Telegram не выполнил):
    дубля нет. retry_any только для правок (edit*, pin*): повтор при любом
    сбое связи и ответе 5xx, второй раз правка ничего не меняет (`not_modified`). С
    файлами при повторах нужны байты, не открытый файл. Исключения httpx
    пересобраны без токена."""
    base = api_url()
    target = f"{base}/bot{token}/{method}"
    kwargs: dict[str, Any] = {k: v for k, v in (("json", json), ("data", data), ("files", files))
                              if v is not None}
    if is_local(base):
        kwargs["trust_env"] = False
    retry = retry_not_sent or retry_any
    delays = iter(RETRY_DELAYS if retry else ())
    floods = 0
    while True:
        try:
            r = httpx.post(target, **kwargs, timeout=timeout)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            err: Exception = e
        else:
            wait = flood_wait(r) if retry else None
            if wait is not None:
                if floods >= FLOOD_RETRIES or wait > FLOOD_WAIT_MAX:
                    return r
                floods += 1
                time.sleep(wait)
                continue
            # 5xx у правки: исход неизвестен, но второй раз она ничего не меняет.
            delay = next(delays, None) if retry_any and r.status_code >= 500 else None
            if delay is None:
                return r
            time.sleep(delay)
            continue
        again = is_not_sent(err) or (retry_any and isinstance(err, httpx.TransportError))
        delay = next(delays, None) if again else None
        if delay is None:
            # Вне except: у пересобранного нет __context__ с адресом.
            raise _clean(err, token)
        time.sleep(delay)


async def apost(token: str, method: str, *, json: Any = None, timeout: float = 10.0) -> httpx.Response:
    """Асинхронный POST, одна попытка; timeout ограничивает и всё время запроса.
    Сбой: пересобранное исключение httpx или TimeoutError."""
    base = api_url()
    target = f"{base}/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=not is_local(base)) as client:
            return await asyncio.wait_for(client.post(target, json=json), timeout)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        err: Exception = e
    raise _clean(err, token)

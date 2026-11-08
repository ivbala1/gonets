#!/usr/bin/python3
"""Сообщение в Telegram, что бот упал и systemd его больше не поднимает.

Запускается юнитом claude-tg-bot-down.service из OnFailure= основного юнита.
Нарочно без пакета claude_tg_bot и без его venv: упасть мог как раз новый код
или окружение. Только стандартная библиотека системного Python. Токен и чат
берутся из .env рядом; в сообщение не идёт ничего из журнала. Шлёт ботом
тревог (ALERT_BOT_TOKEN, ALERT_CHAT_ID, ALERT_THREAD_ID: тот же бот и тема,
что у сторожа ПК на роутере) прямо в облако; его нет или он не отправил:
основным ботом в личку по TELEGRAM_BOT_API_URL.
"""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
# Тот же адрес сервера Bot API, что у бота (claude_tg_bot.tg_api, строка
# TELEGRAM_BOT_API_URL в .env): после logOut с облака запрос туда вернул бы
# бота в облако. Пакет не импортируется (см. выше), поэтому проверка адреса
# здесь копией tg_api.parse_url; тест сверяет их на одних и тех же адресах.
API_URL_ENV = "TELEGRAM_BOT_API_URL"
DEFAULT_API_URL = "https://api.telegram.org"


def _loopback(host: str) -> bool:
    if host.lower() in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def api_url(env: dict[str, str]) -> str | None:
    """Адрес из .env или облако; None: адрес не годится (бот с ним тоже не стартует)."""
    if not env.get(API_URL_ENV, "").strip():
        return DEFAULT_API_URL
    value = env[API_URL_ENV].strip().rstrip("/")
    try:
        u = urllib.parse.urlsplit(value)
        port_ok = u.port is None or u.port > 0
    except ValueError:
        return None
    if (not port_ok or any(c in "?#@" or ord(c) < 33 for c in value) or value.endswith(":")
            or u.scheme not in ("http", "https") or not u.hostname
            or u.path or u.query or u.fragment or u.username or u.password):
        return None
    if u.scheme == "http" and not _loopback(u.hostname):
        return None
    return value


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        value = raw.strip()
        if value[:1] in ("\"", "'") and value.find(value[0], 1) > 0:
            # В кавычках; хвост после закрывающей (комментарий) отбрасывается.
            value = value[1:value.find(value[0], 1)]
        elif value.startswith("#") and raw[:1].isspace():
            value = ""  # `KEY= # комментарий`: пусто; `KEY=#abc` значение, как у python-dotenv
        else:
            # Комментарий в конце строки после пробела или таба, как у python-dotenv бота.
            value = re.split(r"\s#", value, maxsplit=1)[0].rstrip()
        env[re.sub(r"^export\s+", "", key.strip())] = value
    return env


def unit_failed() -> bool:
    """Юнит правда в failed: страховка от вызова при штатном перезапуске.
    Состояние не прочиталось (нет шины, сбой systemctl): считаем упавшим,
    лишняя тревога лучше потерянной."""
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "--value",
                            "claude-tg-bot.service"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    state = r.stdout.strip()
    return r.returncode != 0 or state in ("", "failed")


def main() -> int:
    if "--test" not in sys.argv and not unit_failed():
        print("bot-down-notify: юнит не в failed, сообщение не нужно", file=sys.stderr)
        return 0
    # Нет .env или он не читается: одна строка в журнал вместо трейсбека.
    try:
        env = read_env(REPO / ".env")
    except (OSError, ValueError) as e:
        print(f"bot-down-notify: .env не прочитан: {e}", file=sys.stderr)
        return 2
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    try:
        users = [int(x) for x in env.get("TELEGRAM_ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x]
    except ValueError:
        # Битый список не должен закрыть независимый путь бота тревог.
        print("bot-down-notify: TELEGRAM_ALLOWED_USER_IDS в .env не читается", file=sys.stderr)
        users = []
    alert = alert_target(env)
    if alert is None and (not token or not users):
        print("bot-down-notify: нет токена или пользователей в .env", file=sys.stderr)
        return 2
    base = api_url(env)
    bad_url = base is None
    if bad_url:
        # Бот с таким адресом сам не стартует, и молчать тут значило бы
        # потерять тревогу о падении от одной опечатки. Облако: до перехода на
        # свой сервер это и есть адрес бота; после перехода тревогу несёт бот
        # тревог, сюда доходит только запасной путь.
        print(f"bot-down-notify: {API_URL_ENV} в .env не годится, основной бот пойдёт в облако"
              + (" только без бота тревог" if env.get("ALERT_BOT_TOKEN", "").strip() else ""), file=sys.stderr)
        base = DEFAULT_API_URL
    try:
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    if env.get("CLAUDE_TG_BOT_LANG", "").strip().lower() == "ru":
        text = (
            ("Проверка сообщения о падении, бот на самом деле работает.\n" if "--test" in sys.argv else "")
            + "🔴 Бот claude-tg-bot упал и systemd его больше не поднимает"
            + (f" (версия {head})" if head else "") + ".\n"
            "Из Telegram его не починить: по ssh смотреть "
            "journalctl --user -u claude-tg-bot -n 50, затем systemctl --user restart claude-tg-bot."
            + (f"\nВ .env негодный {API_URL_ENV}: бот с ним не стартует." if bad_url else "")
        )
    else:
        text = (
            ("Test of the crash message, the bot is actually running.\n" if "--test" in sys.argv else "")
            + "🔴 The bot is down and systemd no longer restarts it"
            + (f" (version {head})" if head else "") + ".\n"
            "It cannot be fixed from Telegram: over ssh check "
            "journalctl --user -u claude-tg-bot -n 50, then systemctl --user restart claude-tg-bot."
            + (f"\nBad {API_URL_ENV} in .env: the bot does not start with it." if bad_url else "")
        )
    if alert is not None and send(DEFAULT_API_URL, *alert, text):
        return 0
    if not token or not users:
        return 1
    if bad_url and env.get("ALERT_BOT_TOKEN", "").strip():
        # Запас основным ботом в облако по негодному адресу после перехода на
        # свой сервер вернул бы бота в облако; бот тревог задан (пусть и негоден).
        print("bot-down-notify: запас основным ботом в облако не шлю: задан бот тревог", file=sys.stderr)
        return 1
    if alert is not None:
        print("bot-down-notify: бот тревог не отправил, шлю основным ботом в личку", file=sys.stderr)
    return 0 if send(base, token, min(users), None, text) else 1


def alert_target(env: dict[str, str]) -> tuple[str, int | str, int | None] | None:
    """Бот тревог: (токен, чат, тема) или None. Тот же бот, что у сторожа ПК
    на роутере: шлёт в облако мимо основного бота, поэтому тревога не зависит
    от его сервера Bot API и от logOut (решение Ивана 07.11.2026). Чат: номер
    или @имя. Негодная тема не гасит бота: сообщение уйдёт в чат без темы."""
    token = env.get("ALERT_BOT_TOKEN", "").strip()
    raw_chat = env.get("ALERT_CHAT_ID", "").strip()
    raw_thread = env.get("ALERT_THREAD_ID", "").strip()
    if not token:
        if raw_chat or raw_thread:
            print("bot-down-notify: ALERT_CHAT_ID или ALERT_THREAD_ID без ALERT_BOT_TOKEN, бота тревог нет",
                  file=sys.stderr)
        return None
    chat: int | str
    if raw_chat.startswith("@") and len(raw_chat) > 1:
        chat = raw_chat
    elif re.fullmatch(r"-?[0-9]+", raw_chat) and int(raw_chat) != 0:
        chat = int(raw_chat)
    else:
        print("bot-down-notify: ALERT_CHAT_ID в .env пуст или негоден, бота тревог нет", file=sys.stderr)
        return None
    thread = int(raw_thread) if re.fullmatch(r"[0-9]+", raw_thread) and int(raw_thread) > 0 else None
    if raw_thread and thread is None:
        print("bot-down-notify: ALERT_THREAD_ID в .env негоден, шлю без темы", file=sys.stderr)
    return token, chat, thread


# Паузы между попытками. Это единственная тревога о том, что бот лежит:
# потерять её дороже, чем прислать дважды (решение Ивана 07.11.2026), поэтому
# повтор при любом сбое, кроме отказа Telegram (4xx, кроме 429). Юнит oneshot,
# срока запуска у него нет.
RETRY_DELAYS = (5.0, 15.0)


def send(base: str, token: str, chat: int | str, thread: int | None, text: str, sleep=None) -> bool:
    """sendMessage со звуком (тревога), до трёх попыток."""
    payload: dict[str, object] = {"chat_id": chat, "text": text}
    if thread is not None:
        payload["message_thread_id"] = thread
    req = urllib.request.Request(
        f"{base}/bot{token}/sendMessage", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    delays = iter(RETRY_DELAYS)
    while True:
        flood = 0.0
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if json.load(r).get("ok") is True:
                    return True
                what = "ok=false"
        except urllib.error.HTTPError as e:
            what = f"HTTP {e.code}"
            with e:
                if 400 <= e.code < 500 and e.code != 429:
                    print(f"bot-down-notify: Telegram отказал: {what}", file=sys.stderr)
                    return False
                if e.code == 429:
                    # Пауза, которую назвал Telegram, если длиннее своей. Потолок
                    # 10 минут: раньше срока повтор снова 429, и тревога терялась
                    # (ревью Codex 08.11.2026); юнит oneshot без предела запуска.
                    try:
                        flood = min(float(json.load(e)["parameters"]["retry_after"]) + 1, 600.0)
                    except Exception:
                        pass
        except Exception as e:
            # Текст исключения может содержать URL с токеном: печатаем только тип.
            what = type(e).__name__
        delay = next(delays, None)
        if delay is None:
            print(f"bot-down-notify: отправка не удалась: {what}", file=sys.stderr)
            return False
        delay = max(delay, flood)
        print(f"bot-down-notify: сбой ({what}), повтор через {delay:.0f} с", file=sys.stderr)
        (sleep or time.sleep)(delay)


if __name__ == "__main__":
    sys.exit(main())

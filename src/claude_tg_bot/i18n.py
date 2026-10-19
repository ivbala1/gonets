"""Язык сообщений бота: исходник русский, перевод из каталога.

Строки, которые видит человек в Telegram или читает модель в задании, пишутся
в коде по-русски и оборачиваются в `_()`: msgid это сам русский текст. При
CLAUDE_TG_BOT_LANG=ru текст возвращается как есть, иначе берётся перевод из
`locales/<язык>.json`; нет перевода, остаётся русский текст. По умолчанию
английский: открытая копия бота для всех, у владельца в .env стоит ru.
Переменной нет в окружении процесса (скилл по ssh, утилита до загрузки
.env), язык берётся из .env бота.

Правила для кода:
- подстановки только именованные и только через `.format()` после `_()`:
  `_("Работаю {n} мин").format(n=n)`; f-строку в `_()` не класть, иначе msgid
  каждый раз новый и перевода нет;
- на уровне модуля `_()` не звать: язык читается из окружения, а .env
  загружается позже импорта. Константу помечать `N_()` (для сборщика
  каталога), переводить `_()` там, где её показывают;
- журнал, исключения для журнала, регулярные выражения, callback_data и имена
  файлов не переводятся.

Полноту каталога проверяет tests/test_i18n.py: каждый msgid из `_()` и `N_()`
должен иметь перевод с теми же подстановками.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

LANG_ENV = "CLAUDE_TG_BOT_LANG"
DEFAULT_LANG = "en"
SOURCE_LANG = "ru"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Явный язык поверх окружения (тесты, разовые утилиты). None значит окружение.
_override: str | None = None


@lru_cache(maxsize=1)
def _env_file_language() -> str:
    """CLAUDE_TG_BOT_LANG из .env бота; нет файла или ключа, пусто."""
    try:
        from dotenv import dotenv_values
        return dotenv_values(ENV_FILE).get(LANG_ENV) or ""
    except (ImportError, OSError, ValueError):
        return ""


def language() -> str:
    """Текущий язык: явный, иначе CLAUDE_TG_BOT_LANG из окружения (если
    переменная задана, даже пустой), иначе из .env бота, иначе английский."""
    if _override:
        return _override
    value = os.environ.get(LANG_ENV)
    if value is None:
        value = _env_file_language()
    return value.strip().lower() or DEFAULT_LANG


def set_language(lang: str | None) -> None:
    """Задать язык явно; None возвращает выбор окружению."""
    global _override
    _override = lang.strip().lower() if lang and lang.strip() else None


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    """Каталог перевода языка; нет файла или он битый, каталог пустой."""
    path = LOCALES_DIR / f"{lang}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v}


def _(msgid: str) -> str:
    """Перевод строки на текущий язык; без перевода исходный русский текст."""
    lang = language()
    if lang == SOURCE_LANG:
        return msgid
    return catalog(lang).get(msgid, msgid)


def N_(msgid: str) -> str:
    """Пометка строки для каталога без перевода (константы уровня модуля)."""
    return msgid


def plural(n: int, ru: tuple[str, str, str], en: tuple[str, str]) -> str:
    """Форма слова при числе n: русские три формы, английские две."""
    if language() != SOURCE_LANG:
        return en[0] if abs(n) == 1 else en[1]
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return ru[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return ru[1]
    return ru[2]

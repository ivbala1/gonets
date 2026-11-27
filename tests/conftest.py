"""Общая настройка тестов: сообщения бота по-русски, как в исходнике.

Тексты бота тесты сверяют через `_()` (tests/texts.py), поэтому набор
проходит на любом языке. CLAUDE_TG_BOT_TEST_LANG=en гоняет его по-английски:
ловятся непереведённые строки и исключения от перевода (подстановки, разбор
меток). Английский по умолчанию проверяет tests/test_i18n.py, задавая язык явно.
"""
import os
import sys
from pathlib import Path

# Код из этой копии, а не из установки venv: venv бота на ПК ставит пакет из
# ~/src/claude-tg-bot (editable), и тесты в копии под ~/tmp иначе молча
# проверяли живой код (04.11.2026).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Настоящий .env бота тестам не виден: load_config (load_dotenv) клал бы весь
# его в os.environ процесса, и любой ключ оттуда (адрес Bot API, язык, пути)
# молча менял бы тесты на ПК (ревью 08.11.2026). Модули берут путь из paths при
# импорте (config.BOT_ENV_FILE, i18n.ENV_FILE), поэтому подмена до них. Тест со
# своим .env ставит путь сам.
from claude_tg_bot import paths  # noqa: E402

paths.ENV_FILE = Path(__file__).resolve().parent / ".env-absent-in-tests"
assert not paths.ENV_FILE.exists(), paths.ENV_FILE

from claude_tg_bot import config, i18n  # noqa: E402

assert config.BOT_ENV_FILE == i18n.ENV_FILE == paths.ENV_FILE, "модуль импортирован раньше conftest"

i18n.set_language(os.environ.get("CLAUDE_TG_BOT_TEST_LANG", "ru"))

# Адрес Bot API из окружения процесса (агент, запущенный ботом, наследует его):
# с ним подменённые httpx.post получали trust_env. Тест ставит свой адрес сам.
os.environ["TELEGRAM_BOT_API_URL"] = ""

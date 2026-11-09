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

from claude_tg_bot import i18n  # noqa: E402

i18n.set_language(os.environ.get("CLAUDE_TG_BOT_TEST_LANG", "ru"))

# Настоящий .env бота (load_config зовёт load_dotenv) не должен задавать тестам
# адрес Bot API: с 08.11.2026 там свой сервер, и подменённые httpx.post получали
# trust_env. load_dotenv заданное не перезаписывает; тест ставит свой адрес сам.
os.environ["TELEGRAM_BOT_API_URL"] = ""

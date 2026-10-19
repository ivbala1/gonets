"""Язык сообщений: переключение, формы слов и полнота английского каталога."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_tg_bot import i18n

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "i18n_catalog.py"
_spec = importlib.util.spec_from_file_location("i18n_catalog", _TOOL)
catalog_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog_tool)


class LanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = i18n._override

    def tearDown(self) -> None:
        i18n.set_language(self._saved)

    def test_default_is_english(self) -> None:
        i18n.set_language(None)
        with patch.dict(os.environ, {i18n.LANG_ENV: ""}):
            self.assertEqual(i18n.language(), "en")
        with patch.dict(os.environ, {i18n.LANG_ENV: "RU"}):
            self.assertEqual(i18n.language(), "ru")

    def test_absent_variable_falls_back_to_bot_env_file(self) -> None:
        import tempfile
        i18n.set_language(None)
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_text(f"{i18n.LANG_ENV}=ru\n")
            env = {k: v for k, v in os.environ.items() if k != i18n.LANG_ENV}
            with patch.object(i18n, "ENV_FILE", env_file), patch.dict(os.environ, env, clear=True):
                i18n._env_file_language.cache_clear()
                try:
                    self.assertEqual(i18n.language(), "ru")
                finally:
                    i18n._env_file_language.cache_clear()

    def test_source_language_returns_msgid(self) -> None:
        i18n.set_language("ru")
        self.assertEqual(i18n._("Нет такого текста в каталоге"), "Нет такого текста в каталоге")

    def test_missing_translation_falls_back_to_source(self) -> None:
        i18n.set_language("en")
        self.assertEqual(i18n._("Строка без перевода 12345"), "Строка без перевода 12345")

    def test_unknown_language_falls_back_to_source(self) -> None:
        i18n.set_language("xx")
        self.assertEqual(i18n._("Строка"), "Строка")

    def test_plural(self) -> None:
        forms_ru, forms_en = ("минута", "минуты", "минут"), ("minute", "minutes")
        i18n.set_language("ru")
        self.assertEqual([i18n.plural(n, forms_ru, forms_en) for n in (1, 2, 5, 11, 21, 22, 25)],
                         ["минута", "минуты", "минут", "минут", "минута", "минуты", "минут"])
        i18n.set_language("en")
        self.assertEqual([i18n.plural(n, forms_ru, forms_en) for n in (1, 2, 21)],
                         ["minute", "minutes", "minutes"])


class CatalogTests(unittest.TestCase):
    def test_english_catalog_complete(self) -> None:
        res = catalog_tool.problems("en")
        self.assertEqual(res["missing"], [], "нет перевода: tools/i18n_catalog.py missing")
        self.assertEqual(res["mismatch"], [], "подстановки перевода расходятся с msgid")

    def test_catalog_has_no_stale_entries(self) -> None:
        self.assertEqual(catalog_tool.problems("en")["stale"], [])

    def test_translations_are_english(self) -> None:
        cyr = [v for v in catalog_tool.load("en").values()
               if any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in v)]
        self.assertEqual(cyr, [], "в английском переводе кириллица")


if __name__ == "__main__":
    unittest.main()

"""Живой список моделей для подписей кнопок /model: новейшая модель по
семейству, кэш на сутки, устойчивость к ошибкам API и битому кэшу."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import models  # noqa: E402

LISTING = [
    {"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1", "created_at": "2026-08-28T00:00:00Z"},
    {"id": "claude-opus-5", "display_name": "Claude Opus 5", "created_at": "2026-07-24T00:00:00Z"},
    {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "created_at": "2026-06-29T00:00:00Z"},
    {"id": "claude-fable-5", "display_name": "Claude Fable 5", "created_at": "2026-06-07T00:00:00Z"},
    {"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8", "created_at": "2026-05-28T00:00:00Z"},
    {"id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5", "created_at": "2025-10-15T00:00:00Z"},
    {"id": "claude-experimental-x", "display_name": "X", "created_at": "2027-01-01T00:00:00Z"},
]


class NewestPerFamilyTests(unittest.TestCase):
    def test_family_is_parsed_from_id(self) -> None:
        self.assertEqual(models.family("claude-opus-4-8"), "opus")
        self.assertEqual(models.family("claude-haiku-4-5-20251001"), "haiku")
        self.assertIsNone(models.family("gpt-6-astra"))

    def test_newest_of_each_known_family_in_fixed_order(self) -> None:
        self.assertEqual(list(models.newest_by_family(LISTING).items()),
                         [("fable", "claude-fable-5-1"), ("opus", "claude-opus-5"),
                          ("sonnet", "claude-sonnet-5"), ("haiku", "claude-haiku-4-5-20251001")])

    def test_newer_model_appears_without_code_changes(self) -> None:
        listing = LISTING + [{"id": "claude-opus-6", "display_name": "Claude Opus 6",
                              "created_at": "2026-12-01T00:00:00Z"}]
        self.assertEqual(models.newest_by_family(listing)["opus"], "claude-opus-6")


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.calls = 0

    def fetch_ok(self) -> list[dict]:
        self.calls += 1
        return LISTING

    def fetch_fail(self) -> list[dict]:
        self.calls += 1
        raise OSError("сети нет")

    def test_fetch_is_cached_for_a_day(self) -> None:
        clock = [1000.0]
        first = models.cached_models(self.state, fetch=self.fetch_ok, now=lambda: clock[0])
        clock[0] += 3600
        second = models.cached_models(self.state, fetch=self.fetch_ok, now=lambda: clock[0])
        self.assertEqual(first, second)
        self.assertEqual(self.calls, 1)
        clock[0] += models.CACHE_TTL_SEC
        models.cached_models(self.state, fetch=self.fetch_ok, now=lambda: clock[0])
        self.assertEqual(self.calls, 2)
        self.assertTrue((self.state / models.CACHE_NAME).is_file())

    def test_failed_fetch_keeps_stale_cache(self) -> None:
        (self.state / models.CACHE_NAME).write_text(json.dumps({"fetched_at": 0, "models": LISTING[:2]}))
        result = models.cached_models(self.state, fetch=self.fetch_fail, now=lambda: 10 ** 9)
        self.assertEqual([m["id"] for m in result], ["claude-fable-5-1", "claude-opus-5"])

    def test_failed_fetch_without_cache_gives_no_labels(self) -> None:
        self.assertEqual(models.cached_models(self.state, fetch=self.fetch_fail), [])
        self.assertEqual(models.newest_by_family([]), {})

    def test_broken_cache_or_reply_does_not_raise(self) -> None:
        """Кэш с валидным JSON не той формы и оборванный ответ API не должны
        ронять /model: остаётся пустой список, кнопки идут без подписи."""
        import http.client

        def fetch_cut() -> list[dict]:
            raise http.client.IncompleteRead(b"")

        for body in ("[]", "null", '"x"'):
            (self.state / models.CACHE_NAME).write_text(body)
            self.assertEqual(models.cached_models(self.state, fetch=fetch_cut), [])
        (self.state / models.CACHE_NAME).write_text(
            json.dumps({"fetched_at": 0, "models": [{"display_name": "без id"}, "мусор", LISTING[1]]}))
        result = models.cached_models(self.state, fetch=fetch_cut, now=lambda: 10 ** 9)
        self.assertEqual(models.newest_by_family(result), {"opus": "claude-opus-5"})

    def test_missing_token_is_an_error_not_a_crash(self) -> None:
        original = models._access_token
        try:
            models._access_token = lambda path=None: None
            with self.assertRaises(OSError):
                models.fetch_models()
        finally:
            models._access_token = original


if __name__ == "__main__":
    unittest.main()

"""PersistentDict: переживает «рестарт», пишет атомарно, битый файл не роняет."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.overrides import PersistentDict  # noqa: E402


class PersistentDictTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "model_overrides.json"

    def test_survives_restart(self):
        d = PersistentDict(self.path)
        d["123456789"] = "claude-fable-5-1"
        again = PersistentDict(self.path)
        self.assertEqual(again.get("123456789"), "claude-fable-5-1")
        self.assertIn("123456789", again)

    def test_pop_and_del_persist(self):
        d = PersistentDict(self.path)
        d["a"] = "x"
        d["b"] = "y"
        self.assertEqual(d.pop("a", None), "x")
        del d["b"]
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {})
        self.assertIsNone(d.pop("missing", None))

    def test_missing_file_is_empty(self):
        self.assertEqual(dict(PersistentDict(self.path)), {})

    def test_corrupt_file_starts_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        d = PersistentDict(self.path)
        self.assertEqual(dict(d), {})
        d["k"] = "v"
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"k": "v"})

    def test_non_string_values_ignored_on_load(self):
        self.path.write_text(json.dumps({"a": "x", "b": 1, "c": ""}), encoding="utf-8")
        self.assertEqual(dict(PersistentDict(self.path)), {"a": "x"})


if __name__ == "__main__":
    unittest.main()

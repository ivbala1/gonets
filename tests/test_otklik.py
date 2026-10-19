"""Кнопки откликов: запись, кнопки, сверка текста и сообщения, задание агенту."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import bot, otklik  # noqa: E402


class OtklikRecordTests(unittest.TestCase):
    def test_buttons_only_while_pending_and_bound_to_text(self) -> None:
        rec = otklik.new_record("Acme", "почта", "Hello")
        rows = otklik.buttons(rec)
        self.assertEqual([b["text"] for b in rows[0]], ["✅ Ок", "✏️ Поправить", "⏭ Пропустить"])
        for b in rows[0]:
            parsed = otklik.parse_callback(b["data"])
            self.assertEqual(parsed[:2], (rec["id"], otklik.text_sha("Hello", "почта")))
            self.assertLessEqual(len(b["data"].encode()), 64)
        rec["status"] = "approved"
        self.assertEqual(otklik.buttons(rec), [])

    def test_render_shows_full_text_and_status(self) -> None:
        rec = otklik.new_record("Acme", "Telegram @x", "Приветствую!\n\nРезюме прикладываю.")
        self.assertIn("Резюме прикладываю.", otklik.render(rec))
        rec["status"], rec["note"] = "failed", "сайт просит оплату"
        self.assertIn("не ушло: сайт просит оплату", otklik.render(rec))

    def test_show_only_approved_and_untouched(self) -> None:
        import argparse
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта hr@example.com", "Hello")
            otklik.save(base, rec)
            show = lambda: otklik._cmd_show(argparse.Namespace(id=rec["id"]), base)  # noqa: E731
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(show(), 1)  # ещё не одобрен
                rec["status"] = "approved"
                otklik.save(base, rec)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(show(), 0)
                self.assertIn("Hello", out.getvalue())
                for field, value in (("text", "Hello!"), ("channel", "почта evil@x.y")):
                    bad = dict(rec, **{field: value})
                    otklik.save(base, bad)
                    self.assertEqual(show(), 1, field)

    def test_bad_callback_and_id(self) -> None:
        self.assertIsNone(otklik.parse_callback("otk:zz:yy:o"))
        with self.assertRaises(ValueError):
            otklik.record_path(Path("/tmp"), "../etc")


class OtklikPickTests(unittest.TestCase):
    def _press(self, action: str, *, sha: str | None = None, message_id: int = 10,
               tamper_text: bool = False, twice: bool = False, fail: bool = False):
        with tempfile.TemporaryDirectory() as d:
            base = otklik.otklik_dir(Path(d))
            rec = otklik.new_record("Acme", "почта hr@example.com", "Hello")
            rec["chat_id"], rec["message_id"] = 1, 10
            if tamper_text:
                rec["text"] = "Hello, changed"
            otklik.save(base, rec)
            cfg = SimpleNamespace(allowed_user_ids={7}, state_dir=Path(d))
            dispatch = AsyncMock(side_effect=RuntimeError("boom") if fail else None)

            async def go() -> None:
                for _ in range(2 if twice else 1):
                    msg = SimpleNamespace(message_id=message_id, chat_id=1)
                    query = SimpleNamespace(
                        data=f"otk:{rec['id']}:{sha or rec['sha']}:{action}", message=msg,
                        answer=AsyncMock(), edit_message_text=AsyncMock(),
                        edit_message_reply_markup=AsyncMock())
                    update = SimpleNamespace(callback_query=query, update_id=1,
                                             effective_user=SimpleNamespace(id=7))
                    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}))
                    with patch.object(bot, "_dispatch_to_claude", dispatch), \
                            patch.object(bot, "Update", lambda **kw: kw):
                        await bot.on_otklik_pick(update, ctx)
            asyncio.run(go())
            return otklik.load(base, rec["id"]), dispatch

    def test_ok_wakes_agent_with_exact_record(self) -> None:
        rec, dispatch = self._press("o")
        self.assertEqual(rec["status"], "approved")
        prompt = dispatch.call_args.args[2]
        self.assertIn(rec["id"], prompt)
        self.assertIn(rec["sha"], prompt)
        self.assertIn("без изменений", prompt)

    def test_dispatch_failure_returns_buttons(self) -> None:
        rec, dispatch = self._press("o", fail=True)
        self.assertEqual(rec["status"], "pending")

    def test_second_press_ignored(self) -> None:
        rec, dispatch = self._press("o", twice=True)
        self.assertEqual(dispatch.await_count, 1)

    def test_edit_waits_for_reply_without_agent(self) -> None:
        rec, dispatch = self._press("e")
        self.assertEqual(rec["status"], "edit")
        dispatch.assert_not_awaited()

    def test_skip_wakes_agent_to_record(self) -> None:
        rec, dispatch = self._press("s")
        self.assertEqual(rec["status"], "skipped")
        self.assertIn("Ничего не отправляй", dispatch.call_args.args[2])

    def test_stale_hash_other_message_or_changed_text_ignored(self) -> None:
        for kw in ({"sha": "0" * 12}, {"message_id": 11}, {"tamper_text": True}):
            with self.subTest(**kw):
                rec, dispatch = self._press("o", **kw)
                self.assertEqual(rec["status"], "pending")
                dispatch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

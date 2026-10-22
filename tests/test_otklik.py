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
        self.assertEqual([b["text"] for b in otklik.buttons(rec)[0]], ["✋ Отменить"])
        rec["status"] = "unknown"
        self.assertEqual([b["text"] for b in otklik.buttons(rec)[0]], ["↩️ Не ушло, вернуть кнопки"])
        rec["status"] = "edit"
        self.assertEqual([b["text"] for b in otklik.buttons(rec)[0]], ["↩️ Вернуть кнопки"])
        for status in ("sending", "sent", "skipped"):
            rec["status"] = status
            self.assertEqual(otklik.buttons(rec), [], status)

    def test_render_shows_full_text_and_status(self) -> None:
        rec = otklik.new_record("Acme", "Telegram @x", "Приветствую!\n\nРезюме прикладываю.")
        self.assertIn("Резюме прикладываю.", otklik.render(rec))
        rec["status"], rec["note"] = "failed", "сайт просит оплату"
        self.assertIn("не ушло, по словам агента: сайт просит оплату", otklik.render(rec))

    def test_take_only_approved_untouched_and_own_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта hr@example.com", "Hello")
            rec["chat_id"], rec["source"] = 1, "1"
            otklik.save(base, rec)
            with self.assertRaises(otklik.Refused):
                otklik.take(base, rec["id"], "1")  # ещё не одобрен
            rec["status"] = "approved"
            otklik.save(base, rec)
            for field, value in (("text", "Hello!"), ("channel", "почта evil@x.y")):
                otklik.save(base, dict(rec, **{field: value}))
                with self.assertRaises(otklik.Refused, msg=field):
                    otklik.take(base, rec["id"], "1")
            otklik.save(base, rec)
            with self.assertRaises(otklik.Refused):
                otklik.take(base, rec["id"], "")  # не ран бота
            with self.assertRaises(otklik.Refused):
                otklik.take(base, rec["id"], "1:55")  # другой чат
            got = otklik.take(base, rec["id"], "1")
            self.assertEqual(got["status"], "sending")
            self.assertEqual(got["attempt"]["chat"], "1")
            # Ревью Codex 22.10: второй take отказывает и тому же чату (два
            # помощника одного рана), перечитать можно только своей попыткой.
            with self.assertRaises(otklik.Refused):
                otklik.take(base, rec["id"], "1")
            self.assertTrue(otklik.own_attempt(otklik.load(base, rec["id"]), got["attempt"]["id"], "1"))
            self.assertFalse(otklik.own_attempt(otklik.load(base, rec["id"]), got["attempt"]["id"], "1:9"))

    def test_two_takes_one_wins(self) -> None:
        """Ревью Codex 22.10: два захвата одной записи невозможны: после конца
        рана взятое уходит в unknown, новый ран его уже не возьмёт."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта", "Hello")
            rec.update(chat_id=1, status="approved")
            otklik.save(base, rec)
            otklik.take(base, rec["id"], "1")
            self.assertEqual([r["id"] for r in otklik.close_attempts(base, "1")], [rec["id"]])
            self.assertEqual(otklik.load(base, rec["id"])["status"], "unknown")
            with self.assertRaises(otklik.Refused):
                otklik.take(base, rec["id"], "1")

    def test_done_needs_own_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта", "Hello")
            rec.update(chat_id=1, status="approved")
            otklik.save(base, rec)
            with self.assertRaises(otklik.Refused):
                otklik.finish(base, rec["id"], "sent", "", "", "1")  # не взят
            att = otklik.take(base, rec["id"], "1")["attempt"]["id"]
            for bad, chat in (("", "1"), ("deadbeef", "1"), (att, "1:9"), (att, "")):
                with self.assertRaises(otklik.Refused, msg=(bad, chat)):
                    otklik.finish(base, rec["id"], "sent", "", bad, chat)
            with self.assertRaises(otklik.Refused):
                otklik.finish(base, rec["id"], "skipped", "в воронке")  # уходит: не пропуск
            with self.assertRaises(otklik.Refused):
                otklik.finish(base, rec["id"], "sent", "", "", "1", checked=True)  # не неясный исход
            self.assertEqual(otklik.finish(base, rec["id"], "sent", "ok", att, "1")["status"], "sent")
            with self.assertRaises(otklik.Refused):
                otklik.finish(base, rec["id"], "failed", "", att, "1")  # итог уже стоит

    def test_unknown_can_be_confirmed_sent_but_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта", "Hello")
            rec.update(chat_id=1, status="unknown")
            otklik.save(base, rec)
            for status in ("skipped", "failed", "sent"):
                with self.assertRaises(otklik.Refused, msg=status):
                    otklik.finish(base, rec["id"], status, "", "", "1")
            with self.assertRaises(otklik.Refused):
                otklik.finish(base, rec["id"], "sent", "нашёл в канале", checked=True)  # не ран бота
            self.assertEqual(otklik.finish(base, rec["id"], "sent", "нашёл в канале", chat_key="1",
                                           checked=True)["status"], "sent")

    def test_failed_not_skipped_or_replaced(self) -> None:
        """Ревью Codex 22.10: failed это слова агента, письмо могло дойти."""
        self.assertNotIn("failed", otklik.SKIP_FROM)
        self.assertNotIn("failed", otklik.REPLACE_FROM)
        rec = otklik.new_record("Acme", "почта", "Hello")
        rec["status"] = "failed"
        self.assertIn("Проверь канал", otklik.render(rec))

    def test_skip_from_not_sent_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for status in otklik.SKIP_FROM:
                rec = otklik.new_record("Acme", "почта", "Hello")
                rec.update(chat_id=1, status=status)
                otklik.save(base, rec)
                self.assertEqual(otklik.finish(base, rec["id"], "skipped", "в воронке")["status"], "skipped")

    def test_recover_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            recs = {}
            for status in ("approved", "sending", "pending", "sent", "old"):
                rec = otklik.new_record("Acme", "почта", status)
                rec.update(chat_id=1, status=status)
                if status == "old":
                    # Одобрено до 22.10.2026: полей нового порядка нет.
                    for field in ("gen", "source", "attempt"):
                        rec.pop(field)
                    rec["status"] = "approved"
                otklik.save(base, rec)
                recs[status] = rec["id"]
            changed = {r["id"] for r in otklik.recover(base)}
            self.assertEqual(changed, {recs["approved"], recs["sending"], recs["old"]})
            self.assertEqual(otklik.load(base, recs["old"])["status"], "unknown")
            back = otklik.load(base, recs["approved"])
            self.assertEqual((back["status"], back["gen"]), ("pending", 1))
            self.assertIn("перезапуск", otklik.render(back))
            self.assertEqual(otklik.load(base, recs["sending"])["status"], "unknown")
            self.assertEqual(otklik.load(base, recs["pending"])["gen"], 0)

    def test_close_attempts_only_own_chat(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            mine = otklik.new_record("A", "почта", "a")
            mine.update(chat_id=1, status="approved")
            other = otklik.new_record("B", "почта", "b")
            other.update(chat_id=1, source="1:9", status="approved")
            otklik.save(base, mine)
            otklik.save(base, other)
            otklik.take(base, mine["id"], "1")
            otklik.take(base, other["id"], "1:9")
            self.assertEqual([r["id"] for r in otklik.close_attempts(base, "1")], [mine["id"]])
            self.assertEqual(otklik.load(base, other["id"])["status"], "sending")

    def test_sync_targets_only_unshown_live_records(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            made = {}
            for name, status, shown in (("synced", "pending", True), ("lagging", "pending", False),
                                        ("old_final", "sent", None), ("old_live", "failed", None)):
                rec = otklik.new_record("Acme", "почта", name)
                rec.update(chat_id=1, message_id=10, status=status)
                if shown is True:
                    rec["shown"] = [status, 0]
                elif shown is False:
                    rec["shown"] = ["approved", 0]
                otklik.save(base, rec)
                made[name] = rec["id"]
            self.assertEqual({r["id"] for r in otklik.sync_targets(base)}, {made["lagging"], made["old_live"]})
            otklik.mark_shown(base, made["lagging"], "pending", 0)
            otklik.mark_shown(base, made["old_live"], "pending", 0)  # показано не то: не отмечается
            self.assertEqual({r["id"] for r in otklik.sync_targets(base)}, {made["old_live"]})

    def test_crash_mid_replace_recovered(self) -> None:
        """Ревью Codex 22.10: обрыв процесса между закрытием старой и отправкой новой."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            old = otklik.new_record("Acme", "почта", "Hello")
            old.update(chat_id=1, message_id=10, status="edit")
            new = otklik.new_record("Acme", "почта", "Hello v2")
            new.update(status="delivering", replaces={"id": old["id"], "status": "edit", "note": ""})
            old.update(status="skipped", note=f"заменён {new['id']}")
            otklik.save(base, old)
            otklik.save(base, new)
            otklik.recover(base)
            self.assertEqual(otklik.load(base, old["id"])["status"], "edit")
            self.assertIsNone(otklik.load(base, new["id"]))

    def test_stale_show_drops_mark(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта", "Hello")
            rec.update(chat_id=1, message_id=10, status="pending", shown=["pending", 0])
            otklik.save(base, rec)
            self.assertFalse(otklik.mark_shown(base, rec["id"], "approved", 0))
            self.assertIsNone(otklik.load(base, rec["id"])["shown"])
            self.assertEqual([r["id"] for r in otklik.sync_targets(base)], [rec["id"]])

    def test_old_callback_without_generation(self) -> None:
        self.assertEqual(otklik.parse_callback("otk:" + "a" * 12 + ":" + "b" * 12 + ":o"),
                         ("a" * 12, "b" * 12, 0, "o"))
        self.assertIsNone(otklik.parse_callback("otk:" + "a" * 12 + ":" + "b" * 12 + ":1:x"))

    def test_outbound_match_only_taken_by_this_chat(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта", "Hello\nworld")
            rec.update(chat_id=1, status="approved")
            otklik.save(base, rec)
            self.assertIsNone(otklik.outbound_match(base, "1", "Hello\nworld"))  # не взят
            otklik.take(base, rec["id"], "1")
            self.assertEqual(otklik.outbound_match(base, "1", "Hello\r\nworld\n")["id"], rec["id"])
            self.assertIsNone(otklik.outbound_match(base, "1", "Hello"))
            self.assertIsNone(otklik.outbound_match(base, "1:9", "Hello\nworld"))

    def test_bad_callback_and_id(self) -> None:
        self.assertIsNone(otklik.parse_callback("otk:zz:yy:o"))
        with self.assertRaises(ValueError):
            otklik.record_path(Path("/tmp"), "../etc")


class OtklikNewTests(unittest.TestCase):
    def _new(self, base: Path, replaces: str = "", chat_key: str = "1") -> int:
        import argparse
        import contextlib
        import io
        text = base / "t.txt"
        text.write_text("Hello v2", encoding="utf-8")
        api = lambda method, payload: {"result": {"message_id": 77}}  # noqa: E731
        args = argparse.Namespace(title="Acme", channel="почта", file=str(text), replaces=replaces)
        with patch.object(otklik, "_bot_api", api), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return otklik._cmd_new(args, base / "o", 1, chat_key, frozenset({"1", "1:9"}))

    def test_replaces_closes_old_and_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            old = otklik.new_record("Acme", "почта", "Hello")
            old.update(chat_id=1, message_id=10, source="1:9", status="edit")
            otklik.save(base / "o", old)
            self.assertEqual(self._new(base, replaces=old["id"]), 0)
            closed = otklik.load(base / "o", old["id"])
            self.assertEqual(closed["status"], "skipped")
            new_id = closed["note"].split()[-1]
            fresh = otklik.load(base / "o", new_id)
            self.assertEqual((fresh["status"], fresh["source"], fresh["message_id"]), ("pending", "1:9", 77))

    def test_replaces_refuses_approved_or_sending(self) -> None:
        for status in ("approved", "sending", "unknown", "sent"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                old = otklik.new_record("Acme", "почта", "Hello")
                old.update(chat_id=1, message_id=10, status=status)
                otklik.save(base / "o", old)
                self.assertEqual(self._new(base, replaces=old["id"]), 1)
                self.assertEqual(otklik.load(base / "o", old["id"])["status"], status)

    def test_send_failure_restores_replaced(self) -> None:
        import argparse
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            old = otklik.new_record("Acme", "почта", "Hello")
            old.update(chat_id=1, message_id=10, status="edit")
            otklik.save(base / "o", old)
            text = base / "t.txt"
            text.write_text("Hello v2", encoding="utf-8")
            api = lambda method, payload: None if method == "sendMessage" else {"result": {}}  # noqa: E731
            args = argparse.Namespace(title="Acme", channel="почта", file=str(text), replaces=old["id"])
            with patch.object(otklik, "_bot_api", api), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(otklik._cmd_new(args, base / "o", 1, "1", frozenset({"1"})), 1)
            self.assertEqual(otklik.load(base / "o", old["id"])["status"], "edit")
            self.assertEqual([p.stem for p in (base / "o").glob("*.json")], [old["id"]])

    def test_done_ok_even_if_message_not_updated(self) -> None:
        import argparse
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта", "Hello")
            rec.update(chat_id=1, message_id=10, status="approved")
            otklik.save(base, rec)
            att = otklik.take(base, rec["id"], "1")["attempt"]["id"]
            args = argparse.Namespace(id=rec["id"], status="sent", note="", take=att, checked=False)
            with patch.object(otklik, "_bot_api", lambda *a: None), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(otklik._cmd_done(args, base, "1"), 0)
            self.assertIn(": sent", out.getvalue())

    def test_show_rereads_only_own_attempt(self) -> None:
        import argparse
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = otklik.new_record("Acme", "почта", "Hello")
            rec.update(chat_id=1, message_id=10, status="approved")
            otklik.save(base, rec)
            att = otklik.take(base, rec["id"], "1")["attempt"]["id"]
            show = lambda take, chat: otklik._cmd_show(argparse.Namespace(id=rec["id"], take=take), base, chat)  # noqa: E731
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(show(att, "1"), 0)
                self.assertEqual(show("deadbeef", "1"), 1)
                self.assertEqual(show(att, "1:9"), 1)
            self.assertIn("Hello", out.getvalue())

    def test_source_only_assistant_chats(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._new(Path(d), chat_key="-100:5"), 1)
            self.assertEqual(self._new(Path(d), chat_key=""), 0)  # без рана: личка


class OtklikCardTests(unittest.TestCase):
    """Решение Ивана 22.10.2026: два нажатия остаются, но красная карточка
    говорит, что текст тот же, что в одобренном отклике, и просит сверить адресата."""

    def _card(self, tool: str, tool_input: dict, *, taken_by: str = "1", thread: int | None = None) -> str:
        from claude_tg_bot import approval
        with tempfile.TemporaryDirectory() as d:
            base = otklik.otklik_dir(Path(d))
            rec = otklik.new_record("Acme, DevOps", "Telegram @hr_acme", "Добрый день!\nРезюме ниже.")
            rec.update(chat_id=1, source=taken_by, status="approved")
            otklik.save(base, rec)
            otklik.take(base, rec["id"], taken_by)

            async def go() -> str:
                registry = approval.ApprovalRegistry()
                pending = registry.create(tool, tool_input, session_id="sid", target_chat_id=1,
                                          target_thread_id=thread)
                bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
                await approval.send_approval_request(
                    SimpleNamespace(bot=bot), SimpleNamespace(primary_chat_id=1, state_dir=Path(d)), pending)
                return bot.send_message.call_args.kwargs["text"]
            return asyncio.run(go())

    def test_same_text_marked(self) -> None:
        text = self._card("mcp__telegram__send_message",
                          {"chat_id": "@hr_acme", "message": "Добрый день!\nРезюме ниже."})
        self.assertIn("совпадает с одобренным откликом", text)
        self.assertIn("Telegram @hr_acme", text)
        self.assertIn("сверь адресата", text)
        self.assertIn("<b>кому:</b>", text)

    def test_other_text_or_chat_not_marked(self) -> None:
        for tool_input, taken_by in (({"chat_id": "@hr_acme", "message": "Другой текст"}, "1"),
                                     ({"chat_id": "@hr_acme", "message": "Добрый день!\nРезюме ниже."}, "1:9")):
            with self.subTest(taken_by=taken_by):
                text = self._card("mcp__telegram__send_message", tool_input, taken_by=taken_by)
                self.assertNotIn("совпадает", text)

    def test_not_outbound_not_marked(self) -> None:
        self.assertNotIn("совпадает", self._card("Write", {"file_path": "/tmp/x",
                                                            "content": "Добрый день!\nРезюме ниже."}))


class OtklikPickTests(unittest.TestCase):
    def _press(self, action: str, *, sha: str | None = None, message_id: int = 10,
               tamper_text: bool = False, twice: bool = False, fail: bool = False,
               status: str = "pending", gen: int | None = None, data: str | None = None,
               dispatch_effect=None):
        with tempfile.TemporaryDirectory() as d:
            base = otklik.otklik_dir(Path(d))
            rec = otklik.new_record("Acme", "почта hr@example.com", "Hello")
            rec["chat_id"], rec["message_id"], rec["status"] = 1, 10, status
            if tamper_text:
                rec["text"] = "Hello, changed"
            otklik.save(base, rec)
            cfg = SimpleNamespace(allowed_user_ids={7}, state_dir=Path(d))
            if dispatch_effect is None and fail:
                dispatch_effect = RuntimeError("boom")
            dispatch = AsyncMock(side_effect=dispatch_effect, return_value=None)
            self.bot = SimpleNamespace(edit_message_text=AsyncMock())

            async def go() -> None:
                for _ in range(2 if twice else 1):
                    msg = SimpleNamespace(message_id=message_id, chat_id=1)
                    g = otklik.gen_of(rec) if gen is None else gen
                    head = f"otk:{rec['id']}:{sha or rec['sha']}"
                    # old: кнопка до 22.10.2026, без поколения.
                    query = SimpleNamespace(
                        data=f"{head}:{action}" if data == "old" else f"{head}:{g}:{action}", message=msg,
                        answer=AsyncMock(), edit_message_text=AsyncMock(),
                        edit_message_reply_markup=AsyncMock())
                    update = SimpleNamespace(callback_query=query, update_id=1,
                                             effective_user=SimpleNamespace(id=7))
                    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}), bot=self.bot)
                    with patch.object(bot, "_dispatch_to_claude", dispatch), \
                            patch.object(bot, "Update", lambda **kw: kw):
                        await bot.on_otklik_pick(update, ctx)
            asyncio.run(go())
            self.base = base
            return otklik.load(base, rec["id"]), dispatch

    def test_ok_wakes_agent_with_exact_record(self) -> None:
        rec, dispatch = self._press("o")
        prompt = dispatch.call_args.args[2]
        self.assertIn("take --id", prompt)
        self.assertIn(rec["id"], prompt)
        self.assertIn(rec["sha"], prompt)
        self.assertIn("без изменений", prompt)

    def test_dispatch_failure_returns_buttons(self) -> None:
        rec, dispatch = self._press("o", fail=True)
        self.assertEqual((rec["status"], rec["gen"]), ("pending", 1))
        self.assertIn("не удалось запустить агента", otklik.render(rec))
        markup = self.bot.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertTrue(markup.inline_keyboard[0][0].callback_data.endswith(":1:o"))

    def test_untaken_approval_returns_buttons(self) -> None:
        """Ран кончился, отказан рестартом или задержан лимитом, а текст не
        взят: одобренное снова с кнопками нового поколения."""
        for effect, reason in ((None, "агент не взял текст"), ([True], "бот перезапускается")):
            with self.subTest(reason=reason):
                rec, _d = self._press("o", dispatch_effect=effect)
                self.assertEqual((rec["status"], rec["gen"]), ("pending", 1))
                self.assertIn(reason, otklik.render(rec))

    def test_taken_then_crash_keeps_no_buttons(self) -> None:
        """Ран взял текст и упал: обработчик нажатия кнопки не возвращает (в
        unknown запись переводит конец рана, test_run_end_marks_taken_otklik_unknown)."""
        async def take_and_crash(*_a, **_kw):
            oid = next(p.stem for p in self.base.glob("*.json") if not p.stem.startswith("."))
            otklik.take(self.base, oid, "1")
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as d:
            self.base = base = otklik.otklik_dir(Path(d))
            rec = otklik.new_record("Acme", "почта", "Hello")
            rec.update(chat_id=1, message_id=10)
            otklik.save(base, rec)
            cfg = SimpleNamespace(allowed_user_ids={7}, state_dir=Path(d))
            query = SimpleNamespace(
                data=f"otk:{rec['id']}:{rec['sha']}:0:o", message=SimpleNamespace(message_id=10, chat_id=1),
                answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
            update = SimpleNamespace(callback_query=query, update_id=1, effective_user=SimpleNamespace(id=7))
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}),
                                  bot=SimpleNamespace(edit_message_text=AsyncMock()))
            with patch.object(bot, "_dispatch_to_claude", AsyncMock(side_effect=take_and_crash)), \
                    patch.object(bot, "Update", lambda **kw: kw):
                asyncio.run(bot.on_otklik_pick(update, ctx))
            got = otklik.load(base, rec["id"])
        self.assertEqual(got["status"], "sending")
        self.assertEqual(otklik.buttons(got), [])

    def test_stale_generation_refused(self) -> None:
        # Кнопка прошлого поколения после возврата кнопок не действует.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            r = otklik.new_record("Acme", "почта", "Hello")
            r.update(chat_id=1, message_id=10, gen=1)
            otklik.save(base, r)
            with self.assertRaises(otklik.Refused):
                otklik.pick(base, r["id"], r["sha"], 0, "o", 1, 10)
            self.assertEqual(otklik.pick(base, r["id"], r["sha"], 1, "o", 1, 10)["status"], "approved")

    def test_old_buttons_without_generation_still_work(self) -> None:
        rec, dispatch = self._press("o", data="old")
        dispatch.assert_awaited_once()

    def test_cancel_before_take(self) -> None:
        rec, dispatch = self._press("c", status="approved")
        self.assertEqual((rec["status"], rec["gen"]), ("pending", 1))
        dispatch.assert_not_awaited()

    def test_edit_can_return_buttons(self) -> None:
        rec, dispatch = self._press("r", status="edit")
        self.assertEqual((rec["status"], rec["gen"]), ("pending", 1))
        dispatch.assert_not_awaited()

    def test_note_cleared_on_new_decision(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            r = otklik.new_record("Acme", "почта", "Hello")
            r.update(chat_id=1, message_id=10, note="бот перезапускался")
            otklik.save(base, r)
            got = otklik.pick(base, r["id"], r["sha"], 0, "o", 1, 10)
            self.assertNotIn("перезапуск", otklik.render(got))

    def test_reopen_after_unknown(self) -> None:
        rec, dispatch = self._press("r", status="unknown")
        self.assertEqual((rec["status"], rec["gen"]), ("pending", 1))
        dispatch.assert_not_awaited()
        rec, dispatch = self._press("r", status="sending")
        self.assertEqual(rec["status"], "sending")  # живая попытка: ↩️ не действует

    def test_second_press_ignored(self) -> None:
        rec, dispatch = self._press("o", twice=True)
        self.assertEqual(dispatch.await_count, 1)

    def test_edit_waits_for_reply_without_agent(self) -> None:
        rec, dispatch = self._press("e")
        self.assertEqual(rec["status"], "edit")
        dispatch.assert_not_awaited()

    def test_skip_records_without_agent(self) -> None:
        # Разбор 22.10.2026: ⏭ будил полный ран; воронку теперь правят утро, вечер и поиск.
        rec, dispatch = self._press("s")
        self.assertEqual(rec["status"], "skipped")
        dispatch.assert_not_awaited()

    def test_ok_run_skipped_once_record_is_sent(self) -> None:
        """Разбор 22.10.2026: нажатие, ждавшее замка за раном, который уже
        отправил все одобренные, давало холостой ран «уже ушло»."""
        with tempfile.TemporaryDirectory() as d:
            base = otklik.otklik_dir(Path(d))
            rec = otklik.new_record("Acme", "почта hr@example.com", "Hello")
            rec["chat_id"], rec["message_id"] = 1, 10
            otklik.save(base, rec)
            cfg = SimpleNamespace(allowed_user_ids={7}, state_dir=Path(d))
            seen = []

            async def other_run_sends(*_a, still_wanted, **_kw):
                seen.append(still_wanted())
                cur = otklik.load(base, rec["id"])
                cur["status"] = "sent"
                otklik.save(base, cur)
                seen.append(still_wanted())

            query = SimpleNamespace(
                data=f"otk:{rec['id']}:{rec['sha']}:0:o",
                message=SimpleNamespace(message_id=10, chat_id=1),
                answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
            update = SimpleNamespace(callback_query=query, update_id=1, effective_user=SimpleNamespace(id=7))
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}),
                                  bot=SimpleNamespace(edit_message_text=AsyncMock()))
            with patch.object(bot, "_dispatch_to_claude", AsyncMock(side_effect=other_run_sends)), \
                    patch.object(bot, "Update", lambda **kw: kw):
                asyncio.run(bot.on_otklik_pick(update, ctx))
            self.assertEqual(seen, [True, False])
            self.assertEqual(otklik.load(base, rec["id"])["status"], "sent")  # отправленное не трогается

    def test_stale_hash_other_message_or_changed_text_ignored(self) -> None:
        for kw in ({"sha": "0" * 12}, {"message_id": 11}, {"tamper_text": True}):
            with self.subTest(**kw):
                rec, dispatch = self._press("o", **kw)
                self.assertEqual(rec["status"], "pending")
                dispatch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

"""Принятые события на диске: ключ повтора, восстановление после рестарта (ревью Codex 22.10.2026)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import events  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402


def _rec(key=None, **kw):
    base = dict(key=key, chat_id=1, thread_id=None, prompt="Утро", fresh=False, untrusted=False,
                silent=False, source="домовой", title="Утро", ttl=None, now=1000.0)
    base.update(kw)
    return events.new_record(**base)


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "events"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_key_reserved_once(self) -> None:
        self.assertTrue(events.reserve(self.base, _rec("listen-abc12345")))
        self.assertFalse(events.reserve(self.base, _rec("listen-abc12345")))
        self.assertTrue(events.reserve(self.base, _rec("listen-other123")))
        self.assertEqual(len(list(self.base.glob("*.json"))), 2)
        self.assertEqual(list(self.base.glob(".tmp-*")), [])

    def test_without_key_always_new(self) -> None:
        self.assertTrue(events.reserve(self.base, _rec()))
        self.assertTrue(events.reserve(self.base, _rec()))

    def test_drop_frees_key(self) -> None:
        rec = _rec("watch-abc12345")
        events.reserve(self.base, rec)
        events.drop(self.base, rec["id"])
        self.assertTrue(events.reserve(self.base, rec))

    def test_startup_sorts_records(self) -> None:
        accepted = _rec("k-accepted1")
        started = _rec("k-started01")
        stale = _rec("k-stale0001", ttl=60)
        done_old = _rec("k-doneold01")
        done_new = _rec("k-donenew01")
        for rec in (accepted, started, stale, done_old, done_new):
            events.reserve(self.base, rec)
        events.update(self.base, started["id"], status="started")
        events.update(self.base, done_old["id"], status="done", done_at=1000.0)
        events.update(self.base, done_new["id"], status="done", done_at=1000.0 + events.KEEP_DONE_SEC)
        (self.base / ("0" * 24 + ".json")).write_text("не json", encoding="utf-8")
        wake, interrupted, expired = events.startup(self.base, now=1100.0)
        self.assertEqual([r["id"] for r in wake], [accepted["id"]])
        self.assertEqual([r["id"] for r in interrupted], [started["id"]])
        self.assertEqual([r["id"] for r in expired], [stale["id"]])
        self.assertFalse((self.base / ("0" * 24 + ".json")).exists())

    def test_old_done_pruned(self) -> None:
        rec = _rec("k-doneold01")
        events.reserve(self.base, rec)
        events.update(self.base, rec["id"], status="done", done_at=1000.0)
        events.startup(self.base, now=1000.0 + events.KEEP_DONE_SEC + 1)
        self.assertIsNone(events.load(self.base, rec["id"]))

    def test_resume_flags_keep_untrusted_and_drop_old_fresh(self) -> None:
        rec = _rec(untrusted=True, fresh=True, silent=True)
        self.assertEqual(events.resume_flags(rec, now=1000.0 + 60), (True, True, True))
        self.assertEqual(events.resume_flags(rec, now=1000.0 + events.FRESH_MAX_AGE_SEC + 1),
                         (False, True, True))
        rec["untrusted"] = "false"
        self.assertTrue(events.resume_flags(rec, now=1001.0)[1])

    def test_expired(self) -> None:
        rec = _rec(ttl=120)
        self.assertFalse(events.expired(rec, now=1100.0))
        self.assertTrue(events.expired(rec, now=1121.0))
        self.assertTrue(events.expired({}, now=0))


if __name__ == "__main__":
    unittest.main()


class EventHttpTests(unittest.TestCase):
    """POST /event: принятое пробуждение на диске до ответа, повтор по ключу не будит."""

    def _post(self, payloads, *, send_fails=False):
        import asyncio
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from claude_tg_bot import approval
        state = Path(tempfile.mkdtemp())
        cfg = SimpleNamespace(approve_shared_secret="", primary_chat_id=1, state_dir=state,
                              allowed_chat_ids=frozenset(), allowed_user_ids=frozenset({1}))
        wake = AsyncMock()
        send = AsyncMock(side_effect=OSError) if send_fails else AsyncMock(
            return_value=SimpleNamespace(message_id=5))
        application = SimpleNamespace(bot=SimpleNamespace(send_message=send), bot_data={"event_wake": wake})
        out = []

        async def go():
            for payload in payloads:
                request = SimpleNamespace(app={"cfg": cfg, "application": application}, headers={},
                                          json=AsyncMock(return_value=payload), remote="127.0.0.1")
                resp = await approval.handle_event(request)
                await asyncio.sleep(0)
                out.append((resp.status, json.loads(resp.text)))
        asyncio.run(go())
        return out, wake, events.event_dir(state)

    def test_repeat_with_key_wakes_once(self) -> None:
        payload = {"title": "поиск работы", "wake": True, "untrusted": True, "key": "listen-abcdef12"}
        out, wake, store = self._post([payload, dict(payload)])
        self.assertEqual([s for s, _ in out], [200, 200])
        self.assertTrue(out[1][1].get("duplicate"))
        self.assertEqual(wake.await_count, 1)
        rec = events.load(store, wake.call_args.kwargs["event_id"])
        self.assertEqual((rec["untrusted"], rec["status"]), (True, "accepted"))

    def test_bad_key_or_ttl_rejected(self) -> None:
        out, wake, _ = self._post([{"title": "x", "wake": True, "key": "a b"},
                                   {"title": "x", "wake": True, "expires_in": "60"},
                                   {"title": "x", "wake": True, "expires_in": 5}])
        self.assertEqual([s for s, _ in out], [400, 400, 400])
        wake.assert_not_called()

    def test_notice_failure_frees_key(self) -> None:
        out, wake, store = self._post([{"title": "x", "wake": True, "key": "watch-abcdef12"}], send_fails=True)
        self.assertEqual(out[0][0], 502)
        self.assertEqual(list(store.glob("*.json")), [])

    def test_repeat_while_receiving_waits(self) -> None:
        import asyncio
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from claude_tg_bot import approval
        state = Path(tempfile.mkdtemp())
        cfg = SimpleNamespace(approve_shared_secret="", primary_chat_id=1, state_dir=state,
                              allowed_chat_ids=frozenset(), allowed_user_ids=frozenset({1}))
        gate = asyncio.Event()

        async def slow_then_fail(**_kw):
            await gate.wait()
            raise OSError("telegram down")

        application = SimpleNamespace(bot=SimpleNamespace(send_message=slow_then_fail),
                                      bot_data={"event_wake": AsyncMock()})
        payload = {"title": "x", "wake": True, "key": "woke-abcdef12"}  # gitleaks:allow (ключ события, не секрет)

        def request():
            return SimpleNamespace(app={"cfg": cfg, "application": application}, headers={},
                                   json=AsyncMock(return_value=dict(payload)), remote="127.0.0.1")

        async def go():
            first = asyncio.create_task(approval.handle_event(request()))
            await asyncio.sleep(0)
            second = await approval.handle_event(request())
            gate.set()
            return (await first).status, second.status
        self.assertEqual(asyncio.run(go()), (502, 503))
        self.assertEqual(list(events.event_dir(state).glob("*.json")), [])

    def test_key_reused_for_other_event_refused(self) -> None:
        out, wake, _ = self._post([{"title": "x", "wake": True, "key": "woke-abcdef12", "prompt": "A"},  # gitleaks:allow
                                   {"title": "x", "wake": True, "key": "woke-abcdef12", "prompt": "A2"},  # gitleaks:allow
                                   {"title": "x", "wake": True, "key": "woke-abcdef12", "untrusted": True}])  # gitleaks:allow
        # Другой текст того же события (время в промпте) повтор; другое доверие отказ.
        self.assertEqual([s for s, _ in out], [200, 200, 409])
        self.assertEqual(wake.await_count, 1)

    def test_store_failure_is_503(self) -> None:
        from unittest.mock import patch
        with patch.object(events, "reserve", side_effect=OSError("disk")):
            out, wake, _ = self._post([{"title": "x", "wake": True}])
        self.assertEqual(out[0][0], 503)
        wake.assert_not_called()

    def test_expired_on_arrival_is_410(self) -> None:
        out, wake, _ = self._post([{"title": "x", "wake": True, "expires_at": 1.0}])
        self.assertEqual(out[0][0], 410)
        wake.assert_not_called()

    def test_ttl_passed(self) -> None:
        out, wake, store = self._post([{"title": "Поиск", "wake": True, "notice": False, "expires_in": 600}])
        rec = events.load(store, wake.call_args.kwargs["event_id"])
        self.assertAlmostEqual(rec["expires_at"] - rec["accepted_at"], 600, delta=1)


class EventWakeTests(unittest.IsolatedAsyncioTestCase):
    """Фабрика пробуждения бота: пометки started и done, отказ остановкой, срок."""

    async def asyncSetUp(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from claude_tg_bot import bot as botmod
        self.botmod = botmod
        self.state = Path(tempfile.mkdtemp())
        self.store = events.event_dir(self.state)
        self.cfg = SimpleNamespace(state_dir=self.state)
        self.app = SimpleNamespace(bot_data={"cfg": self.cfg}, bot=SimpleNamespace(send_message=AsyncMock()))
        self.dispatch = AsyncMock(return_value=None)
        for name, value in {"_dispatch_to_claude": self.dispatch,
                            "_synthetic_update": MagicMock(), "ContextTypes": MagicMock()}.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def _wake(self, rec, run=True, refused=None):
        events.reserve(self.store, rec)

        async def fake(*_a, on_run_start=None, still_wanted=None, **_kw):
            if still_wanted is not None and not still_wanted():
                return None
            if run and on_run_start is not None:
                on_run_start()
            return refused
        self.dispatch.side_effect = fake
        wake = self.botmod._make_event_wake(self.app)
        await wake(rec["chat_id"], None, rec["prompt"], untrusted=rec["untrusted"], event_id=rec["id"])
        return events.load(self.store, rec["id"])

    async def test_started_run_marked_done(self) -> None:
        got = await self._wake(_rec("k-ran00001", now=None))
        self.assertEqual((got["status"], got["outcome"]), ("done", "ran"))
        self.assertIn("started_at", got)

    async def test_untrusted_prompt_is_fenced(self) -> None:
        # Свежая своя сессия рана по чужому тексту: имя заказчика из письма не
        # должно закрепить её память, роутер ограждённое не считает (шаг 1, 2.5).
        rec = _rec("k-fence001", now=None)
        rec["untrusted"], rec["prompt"] = True, "Сторож: письмо от заказчика"
        await self._wake(rec)
        prompt = self.dispatch.await_args.args[2]
        self.assertTrue(prompt.startswith(self.botmod._(self.botmod.UNTRUSTED_PROMPT_NOTE)))
        self.assertIn("```\nСторож: письмо от заказчика\n```", prompt)

    async def test_refused_by_stop_stays_accepted(self) -> None:
        got = await self._wake(_rec("k-stop0001", now=None), run=False, refused=True)
        self.assertEqual(got["status"], "accepted")

    async def test_stop_before_process_stays_accepted(self) -> None:
        self.app.bot_data["stopping"] = True
        got = await self._wake(_rec("k-stop0002", now=None), run=False, refused=None)
        self.assertEqual(got["status"], "accepted")

    async def test_start_mark_failure_keeps_key(self) -> None:
        from unittest.mock import patch
        with patch.object(events, "update", side_effect=OSError("disk")):
            got = await self._wake(_rec("k-mark0001", now=None))
        self.assertIsNotNone(got)  # файл держит ключ: повтор отправителя не разбудит второй раз

    async def test_resume_skips_this_boot(self) -> None:
        from unittest.mock import AsyncMock
        rec = _rec("k-boot0001", now=None)
        rec["boot"] = "b1"
        events.reserve(self.store, rec)
        wake = AsyncMock()
        self.app.bot_data.update(event_wake=wake, boot_id="b1")
        await self.botmod._events_resume(self.app)
        wake.assert_not_called()

    async def test_expired_under_lock_not_started(self) -> None:
        rec = _rec("k-late0001", ttl=60, now=1.0)
        got = await self._wake(rec)
        self.assertEqual((got["status"], got["outcome"]), ("done", "expired"))

    async def test_resume_skips_deferred_and_broken(self) -> None:
        import json
        import time
        from unittest.mock import AsyncMock
        now = time.time()
        deferred = _rec("k-defer001", now=now - 60)
        events.reserve(self.store, deferred)
        broken = _rec("k-broken01", now=now - 60)
        broken["accepted_at"] = "вчера"
        events.reserve(self.store, broken)
        at_store = SimpleNamespace(data={"j1": {"v": json.dumps({"event_id": deferred["id"]})}})
        wake = AsyncMock()
        self.app.bot_data.update(event_wake=wake, at_jobs=at_store)
        await self.botmod._events_resume(self.app)
        wake.assert_not_called()
        self.assertEqual(events.load(self.store, deferred["id"])["outcome"], "deferred")
        self.assertIsNone(events.load(self.store, broken["id"]))

    async def test_resume_after_restart(self) -> None:
        import time
        now = time.time()
        waiting = _rec("k-wait0001", untrusted=True, fresh=True, now=now - 1200)
        started = _rec("k-midrun01", now=now - 300)
        for rec in (waiting, started):
            events.reserve(self.store, rec)
        events.update(self.store, started["id"], status="started")
        wake = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
        self.app.bot_data["event_wake"] = wake
        await self.botmod._events_resume(self.app)
        await __import__("asyncio").sleep(0)
        args, kwargs = wake.call_args
        from datetime import datetime
        when = datetime.fromtimestamp(float(waiting["accepted_at"])).strftime("%H:%M")
        self.assertIn(_("(Событие получено в {when}, ждало {minutes} мин: бот перезапускался.)\n\n").format(
            when=when, minutes=20), args[2])
        self.assertEqual((args[3], kwargs["untrusted"], kwargs["event_id"]), (False, True, waiting["id"]))
        self.assertEqual(events.load(self.store, started["id"])["outcome"], "interrupted")
        cut_at = datetime.fromtimestamp(float(started["accepted_at"])).strftime("%H:%M")
        self.assertIn(_("⚠️ Пробуждение «{source}: {title}» (получено в {when}) оборвал "
                        "перезапуск бота посреди рана. Повторять не стал: что успело "
                        "сделаться, видно по журналу и чату.").format(source="домовой", title="Утро", when=cut_at),
                      self.app.bot.send_message.await_args.args[1])


class GuardedDataTests(unittest.TestCase):
    """Журнал и таблицы Домового пишет только команда под замком (ревью Codex 22.10.2026)."""

    def test_write_and_edit_refused_under_guarded(self) -> None:
        from claude_tg_bot import approval
        root = Path(tempfile.mkdtemp()).resolve()
        (root / "data").mkdir()
        cfg = SimpleNamespace(approve_guarded_data_paths=frozenset({root / "data", root / "prompt.md"}),
                              approve_guarded_data_hint="python -m domovoy.data")
        for tool, inp in (("Edit", {"file_path": str(root / "data" / "voronka.md")}),
                          ("Write", {"file_path": str(root / "prompt.md")}),
                          ("MultiEdit", {"file_path": str(root / "data" / "sub" / "x.md")})):
            with self.subTest(tool=tool):
                reason = approval._guarded_data_refusal(cfg, tool, inp)
                self.assertIn("domovoy.data", reason)
        for tool, inp in (("Edit", {"file_path": str(root / "README.md")}),
                          ("Read", {"file_path": str(root / "data" / "voronka.md")}),
                          ("Edit", {"file_path": "data/voronka.md"})):  # без cwd не сверить
            with self.subTest(tool=tool, path=inp["file_path"]):
                self.assertIsNone(approval._guarded_data_refusal(cfg, tool, inp))
        self.assertIsNone(approval._guarded_data_refusal(SimpleNamespace(), "Edit",
                                                         {"file_path": str(root / "data" / "x.md")}))
        self.assertIn("domovoy.data", approval._guarded_data_refusal(cfg, "Edit", {"file_path": "data/voronka.md"},
                                                                     str(root)))

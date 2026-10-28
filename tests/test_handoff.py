"""Передача экрана: запись и токен, кнопки, лимит, ожидание итога, служба экрана."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from claude_tg_bot import bot, handoff, screen_server  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402


_REAL_PAGE_STATE = screen_server.page_state


def _open_record(base: Path, *, chat_key: str = "1", port: int = 1) -> tuple[dict, str]:
    rec, token = handoff.new_record("капча", "hh.ru", chat_key, "s1", port)
    rec["status"], rec["chat_id"], rec["message_id"] = handoff.OPEN, 1, 10
    handoff.save(base, rec)
    return rec, token


class RecordTests(unittest.TestCase):
    def test_token_stored_only_as_hash(self) -> None:
        rec, token = handoff.new_record("капча", "hh.ru", "1", "s1", 9240)
        self.assertNotIn(token, json.dumps(rec))
        self.assertTrue(handoff.token_ok(rec, token))
        self.assertFalse(handoff.token_ok(rec, token + "x"))
        self.assertFalse(handoff.token_ok(rec, ""))
        self.assertAlmostEqual(rec["expires"] - rec["created_ts"], 600)

    def test_buttons_only_while_open(self) -> None:
        rec, token = handoff.new_record("капча", "hh.ru", "1", "s1", 9240)
        self.assertEqual(handoff.buttons(rec), [])
        rec["status"] = handoff.OPEN
        url = handoff.screen_url("http://host.ts.net:8765/", rec["id"], token)
        self.assertEqual(url, f"http://host.ts.net:8765/s/{rec['id']}#{token}")
        rows = handoff.buttons(rec, url)
        self.assertEqual(rows[0][0]["url"], url)
        self.assertEqual([b["text"] for b in rows[1]], [_("✅ Готово"), _("🙅 Не могу")])
        for b in rows[1]:
            self.assertEqual(handoff.parse_callback(b["data"])[0], rec["id"])
            self.assertLessEqual(len(b["data"].encode()), 64)
        markup = handoff.markup(rows)["inline_keyboard"]
        self.assertEqual(markup[0][0], {"text": _("🖥 Открыть экран"), "url": url})
        self.assertIn("callback_data", markup[1][0])

    def test_render_mentions_tailscale_and_status(self) -> None:
        rec, _token = handoff.new_record("капча после откликов", "hh.ru", "1", "s1", 9240)
        rec["status"] = handoff.OPEN
        text = handoff.render(rec)
        self.assertIn("капча после откликов", text)
        self.assertIn("Tailscale", text)
        handoff.set_status(rec, "cant", "button")
        self.assertIn(_("🙅 не получилось, агент пропустит"), handoff.render(rec))

    def test_region_parse(self) -> None:
        self.assertEqual(handoff.parse_region("10,20.4,300,80"), [10, 20, 300, 80])
        for bad in ("", "1,2,3", "a,b,c,d", "-1,0,100,100", "0,0,5,100", "0,0,100,99999"):
            self.assertIsNone(handoff.parse_region(bad), bad)

    def test_bad_callback_id_and_chat_key(self) -> None:
        self.assertIsNone(handoff.parse_callback("ho:zz:d"))
        self.assertIsNone(handoff.parse_callback(f"ho:{'a' * 12}:x"))
        with self.assertRaises(ValueError):
            handoff.record_path(Path("/tmp"), "../etc")
        self.assertEqual(handoff.parse_chat_key("-100123:45"), (-100123, 45))
        self.assertEqual(handoff.parse_chat_key("77"), (77, None))
        self.assertIsNone(handoff.parse_chat_key("abc"))

    def test_rate_limit_counts_last_hour_per_chat(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            now = time.time()
            for i, (key, age) in enumerate((("1", 10), ("1", 3000), ("1", 4000), ("2", 10))):
                rec, _t = handoff.new_record("r", "", key, "s", 1, now=now - age)
                rec["id"] = f"{i:012x}"
                handoff.save(base, rec)
            self.assertEqual(handoff.recent_count(base, "1", now), 2)
            self.assertEqual(handoff.recent_count(base, "2", now), 1)
            old, _t = handoff.new_record("r", "", "1", "s", 1, now=now - 8 * 86400)
            handoff.save(base, old)
            handoff.prune(base, now)
            self.assertIsNone(handoff.load(base, old["id"]))

    def test_ask_refuses_over_limit_and_without_session(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            args = argparse.Namespace(reason="капча", site="hh.ru", quiet=False, image="", region="")
            env = {"SCREEN_PUBLIC_BASE": "http://h:8765", "CLAUDE_TG_CHAT_KEY": "5"}
            err = io.StringIO()
            with patch.dict("os.environ", env), contextlib.redirect_stderr(err):
                with patch.dict("os.environ", {"AGENT_BROWSER_SESSION": ""}):
                    self.assertEqual(handoff._cmd_ask(args, base, 1), 1)
                for _i in range(3):
                    _open_record(base, chat_key="5")
                with patch.dict("os.environ", {"AGENT_BROWSER_SESSION": "s2"}), \
                        patch.object(handoff, "stream_port", return_value=1), \
                        patch.object(handoff, "_bot_api") as api:
                    self.assertEqual(handoff._cmd_ask(args, base, 1), 1)
                    api.assert_not_called()
            self.assertIn(_("handoff: за час уже {n} передачи в этот чат, больше нельзя: остановись и скажи Ивану").format(
                n=3), err.getvalue())

    def test_one_open_handoff_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            _open_record(base, chat_key="9")
            args = argparse.Namespace(reason="капча", site="hh.ru", quiet=False, image="", region="")
            env = {"SCREEN_PUBLIC_BASE": "https://h", "CLAUDE_TG_CHAT_KEY": "5",
                   "AGENT_BROWSER_SESSION": "s1"}
            err = io.StringIO()
            with patch.dict("os.environ", env), contextlib.redirect_stderr(err), \
                    patch.object(handoff, "stream_port", return_value=1), \
                    patch.object(handoff, "_bot_api") as api:
                self.assertEqual(handoff._cmd_ask(args, base, 1), 1)
                api.assert_not_called()
            self.assertIn(_("handoff: у этой сессии уже открыта передача, дождись её итога"), err.getvalue())
            self.assertFalse(handoff.session_busy(base, "s2"))

    def test_stream_port_sets_frame_sized_viewport_first(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_ab(session: str, *args: str) -> SimpleNamespace:
            calls.append(args)
            data = {"connected": True, "enabled": True, "port": 4242}
            return SimpleNamespace(stdout=json.dumps({"data": data}), returncode=0)

        with patch.object(handoff, "_ab", side_effect=fake_ab):
            self.assertEqual(handoff.stream_port("s1"), 4242)
        self.assertEqual(calls[0], ("set", "viewport", "1280", "720"))

    def test_final_status_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec, _token = _open_record(base)
            self.assertIsNotNone(handoff.finish(base, rec["id"], "done", "button"))
            # wait по сроку и страница после кнопки ничего не меняют.
            self.assertIsNone(handoff.finish(base, rec["id"], "expired", "timeout", require_live=False))
            self.assertIsNone(handoff.finish(base, rec["id"], "cant", "page"))
            self.assertEqual(handoff.load(base, rec["id"])["status"], "done")
            late, _t = _open_record(base)
            late["expires"] = time.time() - 1
            handoff.save(base, late)
            self.assertIsNone(handoff.finish(base, late["id"], "done", "button"))
            self.assertIsNotNone(handoff.finish(base, late["id"], "expired", "timeout",
                                                require_live=False))


class WaitTests(unittest.TestCase):
    def _wait(self, mutate, timeout: float = 5.0) -> tuple[int, dict, list]:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec, _token = _open_record(base)
            mutate(base, rec)
            calls: list = []
            with patch.object(handoff, "stream_disable", lambda s: calls.append(("disable", s))), \
                    patch.object(handoff, "_bot_api", lambda m, p: calls.append((m, p)) or {}), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = handoff._cmd_wait(argparse.Namespace(id=rec["id"], timeout=timeout), base)
            return code, handoff.load(base, rec["id"]), calls

    def test_done_and_cant_codes(self) -> None:
        for status, want in (("done", 0), ("cant", 2)):
            with self.subTest(status=status):
                def mutate(base, rec, status=status):
                    handoff.set_status(rec, status, "button")
                    handoff.save(base, rec)
                code, rec, calls = self._wait(mutate)
                self.assertEqual(code, want)
                self.assertIn(("disable", "s1"), calls)
                self.assertTrue(any(c[0] == "editMessageText" for c in calls))

    def test_expiry_gives_3_and_closes_record(self) -> None:
        def mutate(base, rec):
            rec["expires"] = time.time() - 1
            handoff.save(base, rec)
        code, rec, calls = self._wait(mutate)
        self.assertEqual(code, 3)
        self.assertEqual(rec["status"], "expired")
        self.assertIn(("disable", "s1"), calls)
        self.assertIn(_("⌛ экран закрыт по времени"), calls[-1][1]["text"])

    def test_short_timeout_expires_too(self) -> None:
        code, rec, _calls = self._wait(lambda base, rec: None, timeout=1.0)
        self.assertEqual(code, 3)
        self.assertFalse(handoff.is_live(rec))

    def test_message_already_edited_by_button_not_edited_again(self) -> None:
        def mutate(base, rec):
            handoff.set_status(rec, "done", "button")
            rec["notified"] = True
            handoff.save(base, rec)
        code, _rec, calls = self._wait(mutate)
        self.assertEqual(code, 0)
        self.assertFalse(any(c[0] == "editMessageText" for c in calls))


class PickTests(unittest.TestCase):
    def _press(self, action: str, *, message_id: int = 10, expired: bool = False,
               user: int = 7) -> tuple[dict, SimpleNamespace]:
        with tempfile.TemporaryDirectory() as d:
            base = handoff.handoff_dir(Path(d))
            rec, _token = _open_record(base)
            if expired:
                rec["expires"] = time.time() - 1
                handoff.save(base, rec)
            cfg = SimpleNamespace(allowed_user_ids={7}, state_dir=Path(d))
            msg = SimpleNamespace(message_id=message_id, chat_id=1)
            query = SimpleNamespace(data=f"ho:{rec['id']}:{action}", message=msg, answer=AsyncMock(),
                                    edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
            update = SimpleNamespace(callback_query=query, update_id=1,
                                     effective_user=SimpleNamespace(id=user))
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}))
            dispatch = AsyncMock()
            with patch.object(bot, "_dispatch_to_claude", dispatch):
                asyncio.run(bot.on_handoff_pick(update, ctx))
            dispatch.assert_not_awaited()
            return handoff.load(base, rec["id"]), query

    def test_done_and_cant_write_status_without_new_run(self) -> None:
        rec, query = self._press("d")
        self.assertEqual(rec["status"], "done")
        self.assertTrue(rec["notified"])
        query.edit_message_text.assert_awaited()
        rec, _query = self._press("c")
        self.assertEqual(rec["status"], "cant")

    def test_stale_other_message_or_stranger_ignored(self) -> None:
        for kw in ({"message_id": 11}, {"expired": True}, {"user": 8}):
            with self.subTest(**kw):
                rec, _query = self._press("d", **kw)
                self.assertEqual(rec["status"], handoff.OPEN)


class TextAnswerTests(unittest.TestCase):
    """Капча с буквами: ответ на фото в Telegram, без экрана и без нового рана."""

    def _photo_record(self, base: Path) -> dict:
        rec, _token = _open_record(base)
        rec["photo"] = True
        handoff.save(base, rec)
        return rec

    def test_answer_only_for_live_photo_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            plain, _t = _open_record(base)
            self.assertIsNone(handoff.answer(base, plain["id"], "abc"))
            rec = self._photo_record(base)
            self.assertEqual(handoff.find_by_message(base, 1, 10), rec["id"])
            self.assertIsNone(handoff.find_by_message(base, 2, 10))
            for speech in ("   ", "не вижу буквы", "z" * 21):
                self.assertIsNone(handoff.answer(base, rec["id"], speech), speech)
            done = handoff.answer(base, rec["id"], "  x7K2\n")
            self.assertEqual((done["status"], done["answer"]), ("answered", "x7K2"))
            self.assertIsNone(handoff.answer(base, rec["id"], "again"))
            self.assertIn(_("✍️ ответ передан агенту"), handoff.render(done))

    def test_unclaimed_answer_scrubbed_after_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = self._photo_record(base)
            handoff.answer(base, rec["id"], "x7K2")
            handoff.scrub(base)
            self.assertIn("answer", handoff.load(base, rec["id"]))
            handoff.scrub(base, now=time.time() + handoff.TTL_SECONDS + 1)
            self.assertNotIn("answer", handoff.load(base, rec["id"]))

    def test_wait_prints_answer_once_and_returns_4(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            rec = self._photo_record(base)
            handoff.answer(base, rec["id"], "x7K2")
            calls: list = []
            out = io.StringIO()
            with patch.object(handoff, "stream_disable", lambda s: None), \
                    patch.object(handoff, "_bot_api", lambda m, p, *a: calls.append(m) or {}), \
                    contextlib.redirect_stdout(out):
                code = handoff._cmd_wait(argparse.Namespace(id=rec["id"], timeout=5), base)
            self.assertEqual(code, 4)
            self.assertIn("answer: x7K2", out.getvalue())
            self.assertEqual(calls, ["editMessageCaption"])
            self.assertNotIn("answer", handoff.load(base, rec["id"]))

    def test_bot_reply_goes_to_record_not_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = handoff.handoff_dir(Path(d))
            rec = self._photo_record(base)
            cfg = SimpleNamespace(allowed_user_ids={7}, state_dir=Path(d))
            bot_api = SimpleNamespace(edit_message_caption=AsyncMock())
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}), bot=bot_api)

            def msg(reply_id: int, text: str = "x7K2"):
                return SimpleNamespace(text=text, chat_id=1, reply_text=AsyncMock(),
                                       reply_to_message=SimpleNamespace(message_id=reply_id))
            other = msg(99)
            self.assertFalse(asyncio.run(bot._handoff_reply(ctx, other)))
            # Речь, а не буквы: уходит в Claude, передача остаётся открытой.
            self.assertFalse(asyncio.run(bot._handoff_reply(ctx, msg(10, "не вижу, открой крупнее"))))
            self.assertEqual(handoff.load(base, rec["id"])["status"], handoff.OPEN)
            m = msg(10)
            self.assertTrue(asyncio.run(bot._handoff_reply(ctx, m)))
            self.assertEqual(handoff.load(base, rec["id"])["answer"], "x7K2")
            bot_api.edit_message_caption.assert_awaited()
            # Буквы следующим сообщением, без ответа на фото.
            rec2 = self._photo_record(base)
            plain = SimpleNamespace(text="Ab3d", chat_id=1, reply_text=AsyncMock(), reply_to_message=None)
            self.assertFalse(asyncio.run(bot._handoff_reply(
                ctx, SimpleNamespace(text="ок", chat_id=1, reply_text=AsyncMock(), reply_to_message=None))))
            self.assertTrue(asyncio.run(bot._handoff_reply(ctx, plain)))
            self.assertEqual(handoff.load(base, rec2["id"])["answer"], "Ab3d")
            late = msg(10, "again")
            self.assertTrue(asyncio.run(bot._handoff_reply(ctx, late)))
            self.assertEqual(_("Экран уже закрыт, ответ агенту не передан"), late.reply_text.call_args.args[0])


class SanitizeTests(unittest.TestCase):
    def test_only_mouse_keys_and_ack_pass(self) -> None:
        s = screen_server.sanitize
        self.assertIsNone(s({"type": "config", "maxFps": 120}))
        self.assertIsNone(s({"type": "input_touch", "eventType": "touchStart", "touchPoints": []}))
        self.assertIsNone(s({"type": "screencast_start"}))
        self.assertIsNone(s("x"))
        self.assertEqual(s({"type": "ack", "seq": 4}), {"type": "ack", "seq": 4})
        self.assertIsNone(s({"type": "ack", "seq": "4"}))

    def test_mouse_whitelisted_and_modifiers_except_shift_dropped(self) -> None:
        out = screen_server.sanitize({"type": "input_mouse", "eventType": "mousePressed", "x": 5,
                                      "y": 6, "button": "left", "clickCount": 1,
                                      "modifiers": 2 | 4 | 8, "evil": 1})
        self.assertEqual(out, {"type": "input_mouse", "eventType": "mousePressed", "x": 5.0, "y": 6.0,
                               "modifiers": 8, "button": "left", "clickCount": 1})
        self.assertIsNone(screen_server.sanitize({"type": "input_mouse", "eventType": "drag",
                                                  "x": 1, "y": 1}))
        self.assertIsNone(screen_server.sanitize({"type": "input_mouse", "eventType": "mouseMoved",
                                                  "x": -1, "y": 1}))

    def test_keys_single_char_or_named(self) -> None:
        k = lambda key, **kw: screen_server.sanitize(  # noqa: E731
            {"type": "input_keyboard", "eventType": "keyDown", "key": key, **kw})
        self.assertEqual(k("я", modifiers=2)["modifiers"], 0)
        self.assertEqual(k("я", text="x")["text"], "я")
        bs = k("Backspace", code="KeyA", windowsVirtualKeyCode=65)
        self.assertEqual((bs["code"], bs["windowsVirtualKeyCode"]), ("Backspace", 8))
        self.assertEqual(k("Enter")["text"], "\r")
        self.assertIsNone(k("F12"))
        self.assertIsNone(k("abc"))
        self.assertIsNone(k("\x00"))
        self.assertIsNone(screen_server.sanitize({"type": "input_keyboard", "eventType": "char",
                                                  "key": "a"}))

    def test_only_frames_reach_phone(self) -> None:
        self.assertIsNotNone(screen_server.from_browser('{"type":"frame","seq":1,"data":"x"}'))
        for kind in ("console", "url", "tabs", "status"):
            self.assertIsNone(screen_server.from_browser(json.dumps({"type": kind})))

    def test_listens_only_on_loopback(self) -> None:
        for host, ok in (("", True), ("127.0.0.1", True), ("::1", True), ("0.0.0.0", False),
                         ("100.64.0.1", False), ("example.com", False)):
            with self.subTest(host=host), patch.dict("os.environ", {"SCREEN_BIND_HOST": host}):
                if ok:
                    self.assertIn(screen_server.bind_host(), ("127.0.0.1", "::1"))
                else:
                    with self.assertRaises(RuntimeError):
                        screen_server.bind_host()


class ServerTests(unittest.TestCase):
    """Сквозной прогон: поддельная трансляция agent-browser и телефон."""

    PAGE = {"host": "hh.ru", "title": "Кабинет", "pwd": False}

    def setUp(self) -> None:
        # Настоящий agent-browser в тестах не зовём: что стало со страницей,
        # отвечает подмена.
        self.page_state = AsyncMock(return_value={})
        p = patch.object(screen_server, "page_state", self.page_state)
        p.start()
        self.addCleanup(p.stop)

    def test_proxy_filters_and_done_from_page(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        got: list[dict] = []
        headers: dict = {}

        async def fake_stream(request: web.Request) -> web.WebSocketResponse:
            headers.update(origin=request.headers.get("Origin"), query=dict(request.query))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "console", "text": "secret"})
            await ws.send_json({"type": "frame", "seq": 1, "data": "AA==", "metadata": {}})
            async for m in ws:
                got.append(json.loads(m.data))
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_get("/", fake_stream)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            async with TestServer(upstream_app) as upstream:
                rec, token = _open_record(base, port=upstream.port)
                rec["region"] = [5, 6, 300, 80]
                handoff.save(base, rec)
                async with TestClient(TestServer(screen_server.make_app(base))) as client:
                    page = await client.get(f"/s/{rec['id']}")
                    self.assertEqual(page.status, 200)
                    self.assertEqual(page.headers["Cache-Control"], "no-store")
                    self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
                    # Адрес публичный: чужой или несуществующий id не отвечает.
                    self.assertEqual((await client.get("/s/" + "0" * 12)).status, 404)
                    self.assertEqual((await client.get("/")).status, 404)

                    bad = await client.ws_connect(f"/ws/{rec['id']}")
                    await bad.send_json({"token": "nope"})
                    self.assertEqual((await bad.receive_json())["reason"], "denied")
                    await bad.close()

                    ws = await client.ws_connect(f"/ws/{rec['id']}")
                    await ws.send_json({"token": token})
                    info = await ws.receive_json()
                    self.assertEqual((info["type"], info["region"]), ("info", [5, 6, 300, 80]))
                    self.assertEqual((await ws.receive_json())["type"], "frame")
                    await ws.send_json({"type": "config", "maxFps": 120})
                    await ws.send_json({"type": "input_mouse", "eventType": "mousePressed",
                                        "x": 3, "y": 4, "button": "left", "clickCount": 1,
                                        "modifiers": 2})
                    await ws.send_json({"type": "ack", "seq": 1})
                    seen: list = []

                    async def snap(session: str) -> dict:
                        # Снимок до записи итога: агент ещё ждёт и не ушёл со страницы.
                        seen.append(handoff.load(base, rec["id"])["status"])
                        return self.PAGE
                    self.page_state.side_effect = snap
                    await ws.send_json({"type": "done"})
                    # Итог с тем, что стало со страницей: на ПК видно, удался ли вход.
                    self.assertEqual(await ws.receive_json(),
                                     {"type": "closed", "reason": "done", "page": self.PAGE})
                    self.page_state.assert_awaited_once_with("s1")
                    self.assertEqual(seen, [handoff.OPEN])
                    await ws.close()
                    # После итога страница тоже 404.
                    self.assertEqual((await client.get(f"/s/{rec['id']}")).status, 404)
            self.assertEqual(handoff.load(base, rec["id"])["status"], "done")
        self.assertEqual(headers["origin"], "http://127.0.0.1")
        self.assertEqual(headers["query"].get("maxFps"), str(screen_server.MAX_FPS))
        self.assertEqual(headers["query"].get("pacing"), "ack")
        self._check_got(got)

    def test_outside_result_lockout_headers_and_rate(self) -> None:
        asyncio.run(self._run_limits())

    async def _run_limits(self) -> None:
        async def fake_stream(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for _m in ws:
                pass
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_get("/", fake_stream)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            async with TestServer(upstream_app) as upstream:
                rec, token = _open_record(base, port=upstream.port)
                async with TestClient(TestServer(screen_server.make_app(base))) as client:
                    miss = await client.get("/s/" + "0" * 12)
                    self.assertNotIn("Server", miss.headers)
                    self.assertEqual(miss.headers["Cache-Control"], "no-store")

                    # Итог по кнопке бота во время показа: экран закрывается сам.
                    ws = await client.ws_connect(f"/ws/{rec['id']}")
                    await ws.send_json({"token": token})
                    self.assertEqual((await ws.receive_json())["type"], "info")
                    handoff.finish(base, rec["id"], "done", "button")
                    closed = await asyncio.wait_for(ws.receive_json(), 3)
                    self.assertEqual(closed, {"type": "closed", "reason": "done"})
                    # Итог кнопкой бота: агент уже продолжает, страницу не снимаем.
                    self.page_state.assert_not_awaited()
                    await ws.close()

                    # Десять неверных токенов: вход по этой передаче закрыт и с верным.
                    rec2, token2 = _open_record(base, port=upstream.port)
                    rec2["session"] = "s2"
                    handoff.save(base, rec2)
                    for _ in range(screen_server.MAX_BAD_TOKENS):
                        bad = await client.ws_connect(f"/ws/{rec2['id']}")
                        await bad.send_json({"token": "nope"})
                        await bad.receive_json()
                        await bad.close()
                    self.assertEqual((await client.get(f"/s/{rec2['id']}")).status, 404)

                    with patch.object(screen_server, "RATE_MAX", 0):
                        self.assertEqual((await client.get(f"/s/{rec2['id']}")).status, 429)

    def test_focus_probe_and_typed_ack(self) -> None:
        asyncio.run(self._run_probe())

    async def _run_probe(self) -> None:
        got: list[dict] = []

        async def fake_stream(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for m in ws:
                got.append(json.loads(m.data))
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_get("/", fake_stream)
        kind = AsyncMock(return_value="password")
        with tempfile.TemporaryDirectory() as d, patch.object(screen_server, "focus_kind", kind), \
                patch.object(screen_server, "PROBE_DELAY", 0.05):
            base = Path(d)
            async with TestServer(upstream_app) as upstream:
                rec, token = _open_record(base, port=upstream.port)
                async with TestClient(TestServer(screen_server.make_app(base))) as client:
                    ws = await client.ws_connect(f"/ws/{rec['id']}")
                    await ws.send_json({"token": token})
                    self.assertEqual((await ws.receive_json())["type"], "info")
                    # Два касания подряд: проверка одна, ответ на последнее.
                    await ws.send_json({"type": "probe", "id": 1})
                    await ws.send_json({"type": "probe", "id": 2})
                    await ws.send_json({"type": "input_keyboard", "eventType": "keyDown", "key": "a"})
                    await ws.send_json({"type": "typed", "id": 7})
                    answers = [await asyncio.wait_for(ws.receive_json(), 3) for _ in range(2)]
                    self.assertCountEqual(answers, [{"type": "typed", "id": 7},
                                                    {"type": "focus", "id": 2, "kind": "password"}])
                    await ws.send_json({"type": "probe", "id": "x"})
                    await ws.send_json({"type": "done"})
                    self.assertEqual((await ws.receive_json())["reason"], "done")
                    await ws.close()
        kind.assert_awaited_once_with(rec["session"])
        # Служебные метки в трансляцию не уходят.
        self.assertEqual([m["type"] for m in got], ["input_keyboard"])

    def test_focus_kind_only_known_words(self) -> None:
        async def run(out: bytes) -> str:
            proc = AsyncMock()
            proc.communicate.return_value = (out, b"")
            with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
                return await screen_server.focus_kind("s1")
        self.assertEqual(asyncio.run(run(b'"password"\n')), "password")
        self.assertEqual(asyncio.run(run(b'"hunter2"\n')), "unknown")
        self.assertEqual(asyncio.run(run(b"not json")), "unknown")
        self.assertEqual(asyncio.run(screen_server.focus_kind("")), "unknown")
        # Экран закрыт во время проверки: процесс убит, а не брошен.
        async def cancelled() -> object:
            proc = AsyncMock()
            proc.returncode = None
            proc.kill = Mock()
            async def hang() -> None:
                await asyncio.sleep(10)
            proc.communicate.side_effect = hang
            with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
                task = asyncio.create_task(screen_server.focus_kind("s1"))
                await asyncio.sleep(0.05)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return proc
        hung = asyncio.run(cancelled())
        hung.kill.assert_called_once()
        hung.wait.assert_awaited_once()
        missing = AsyncMock(side_effect=FileNotFoundError)
        with patch.object(asyncio, "create_subprocess_exec", missing):
            self.assertEqual(asyncio.run(screen_server.focus_kind("s1")), "unknown")

    def test_page_state_only_site_title_and_password(self) -> None:
        async def run(out: bytes) -> dict:
            proc = AsyncMock()
            proc.communicate.return_value = (out, b"")
            with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
                return await _REAL_PAGE_STATE("s1")
        ok = json.dumps({"host": "eservices.rs.ge", "title": "  Кабинет\n" + "x" * 200,
                         "pwd": True, "value": "hunter2"}).encode()
        got = asyncio.run(run(ok))
        self.assertEqual(set(got), {"host", "title", "pwd"})
        self.assertEqual((got["host"], got["pwd"]), ("eservices.rs.ge", True))
        self.assertTrue(got["title"].startswith("Кабинет x"))
        self.assertEqual(len(got["title"]), screen_server.TITLE_CAP)
        self.assertEqual(asyncio.run(run(b'{"host": "evil<b>", "title": "t", "pwd": false}')), {})
        self.assertEqual(asyncio.run(run(b'"text"')), {})
        self.assertEqual(asyncio.run(run(b"not json")), {})
        self.assertEqual(asyncio.run(_REAL_PAGE_STATE("")), {})

    def _check_got(self, got: list) -> None:
        self.assertEqual([m["type"] for m in got], ["input_mouse", "ack"])
        self.assertEqual(got[0]["modifiers"], 0)


if __name__ == "__main__":
    unittest.main()

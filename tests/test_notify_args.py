"""Тесты аргументов notify: адресация в тему и чат по умолчанию."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import notify  # noqa: E402


def _fake_cfg():
    return SimpleNamespace(
        primary_chat_id=111,
        telegram_bot_token="TOKEN",
        telegram_text_limit=4096,
    )


def _ok_response(message_id: int = 4242):
    return SimpleNamespace(
        status_code=200,
        text="ok",
        json=lambda: {"ok": True, "result": {"message_id": message_id}},
    )


class NotifyArgsTests(unittest.TestCase):
    def test_thread_without_chat_is_error(self):
        rc = notify.main(["--thread-id", "42", "текст"])
        self.assertEqual(rc, 1)

    def test_default_goes_to_primary_without_thread(self):
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response()) as post:
                rc = notify.main(["привет"])
        self.assertEqual(rc, 0)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], 111)
        self.assertNotIn("message_thread_id", payload)

    def test_explicit_chat_and_thread(self):
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response()) as post:
                rc = notify.main(["--chat-id", "-1", "--thread-id", "9", "т"])
        self.assertEqual(rc, 0)
        payload = post.call_args.kwargs["json"]
        self.assertEqual((payload["chat_id"], payload["message_thread_id"]), (-1, 9))

    def test_dash_leading_text_after_separator(self):
        # Текст, начинающийся с «-», не должен парситься как опция (находка 17).
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response()) as post:
                rc = notify.main(["--chat-id", "-100500", "--thread-id", "77", "--", "-первый пункт дня?"])
        self.assertEqual(rc, 0)
        self.assertEqual(post.call_args.kwargs["json"]["text"], "-первый пункт дня?")

    def test_send_message_returns_message_id(self):
        # Якорь для reply: по нему отправитель узнаёт ответ человека.
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response(555)):
                rc, mid = notify.send_message(["--chat-id", "-100500", "--", "сводка"])
        self.assertEqual((rc, mid), (0, 555))

    def test_send_message_survives_response_without_json(self):
        cfg = _fake_cfg()
        bare = SimpleNamespace(status_code=200, text="ok")
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=bare):
                rc, mid = notify.send_message(["текст"])
        self.assertEqual((rc, mid), (0, None))

    def test_html_silent_no_preview_flags(self):
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response()) as post:
                rc = notify.main(["--chat-id", "-1", "--html", "--silent", "--no-preview", "--", "<b>т</b>"])
        self.assertEqual(rc, 0)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(payload["disable_notification"])
        self.assertEqual(payload["link_preview_options"], {"is_disabled": True})

    def test_plain_has_no_extra_fields(self):
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response()) as post:
                notify.main(["текст"])
        payload = post.call_args.kwargs["json"]
        for key in ("parse_mode", "disable_notification", "link_preview_options"):
            self.assertNotIn(key, payload)

    def test_html_longer_than_limit_is_error_not_split(self):
        # Разрезанный HTML ломает теги: Telegram отверг бы вторую часть.
        cfg = SimpleNamespace(primary_chat_id=111, telegram_bot_token="TOKEN", telegram_text_limit=10)
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response()) as post:
                rc = notify.main(["--html", "--", "<b>" + "x" * 20 + "</b>"])
        self.assertEqual(rc, 1)
        post.assert_not_called()

    def test_pin_sends_then_pins_silently_and_prints_id(self):
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response(777)) as post:
                with patch("builtins.print") as pr:
                    rc = notify.main(["--pin", "--silent", "Вопрос дня: ?"])
        self.assertEqual(rc, 0)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args_list[1].args[0].endswith("/pinChatMessage"))
        self.assertEqual(
            post.call_args_list[1].kwargs["json"],
            {"chat_id": 111, "message_id": 777, "disable_notification": True},
        )
        self.assertEqual(pr.call_args_list[-1].args[0], "message_id: 777")

    def _run_capture(self, argv, post_effect):
        cfg = _fake_cfg()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", side_effect=post_effect) as post:
                with redirect_stdout(out), redirect_stderr(err):
                    rc = notify.main(argv)
        return rc, out.getvalue(), err.getvalue(), post

    def test_pin_failure_returns_1_but_prints_message_id(self):
        # Сообщение ушло: id нужен вызывающему, чтобы не слать заново (дубль).
        bad = SimpleNamespace(status_code=400, text="no rights")
        rc, out, err, _post = self._run_capture(["--pin", "текст"], [_ok_response(5), bad])
        self.assertEqual(rc, 1)
        self.assertIn("message_id: 5\n", out)
        self.assertIn("pinChatMessage failed: 400", err)

    def test_pin_network_error_returns_1_but_prints_message_id(self):
        rc, out, err, _post = self._run_capture(
            ["--pin", "текст"], [_ok_response(5), notify.httpx.ConnectError("x")]
        )
        self.assertEqual(rc, 1)
        self.assertIn("message_id: 5\n", out)
        self.assertIn("pinChatMessage HTTP error: ConnectError", err)

    def test_pin_without_message_id_returns_1(self):
        bare = SimpleNamespace(status_code=200, text="ok")
        rc, out, err, post = self._run_capture(["--pin", "текст"], [bare])
        self.assertEqual(rc, 1)
        self.assertNotIn("message_id", out)
        self.assertEqual(post.call_count, 1)

    def _run_chunked(self, argv, post_effect):
        cfg = SimpleNamespace(primary_chat_id=111, telegram_bot_token="TOKEN", telegram_text_limit=5)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", side_effect=post_effect) as post:
                with redirect_stdout(out), redirect_stderr(err):
                    rc = notify.main(argv)
        return rc, out.getvalue(), err.getvalue(), post

    def test_pin_second_chunk_failure_still_prints_first_id(self):
        # Первая часть доставлена, вторая упала по таймауту: id нужен вызывающему,
        # иначе он перешлёт всё заново; закрепа нет.
        rc, out, err, post = self._run_chunked(
            ["--pin", "0123456789"], [_ok_response(7), notify.httpx.ReadTimeout("x")]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "message_id: 7\n")
        self.assertIn("HTTP error: ReadTimeout", err)
        self.assertEqual(post.call_count, 2)

    def test_pin_second_chunk_http_status_failure_still_prints_first_id(self):
        bad = SimpleNamespace(status_code=429, text="Too Many Requests")
        rc, out, err, _post = self._run_chunked(["--pin", "0123456789"], [_ok_response(7), bad])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "message_id: 7\n")
        self.assertIn("sendMessage failed: 429", err)

    def test_pin_first_chunk_without_id_does_not_take_second_id(self):
        bare = SimpleNamespace(status_code=200, text="ok")
        rc, out, err, post = self._run_chunked(["--pin", "0123456789"], [bare, _ok_response(8)])
        self.assertEqual(rc, 1)
        self.assertNotIn("message_id", out)
        self.assertEqual(post.call_count, 2)  # без закрепа вообще

    def test_plain_stdout_is_unchanged(self):
        rc, out, err, post = self._run_capture(["текст"], [_ok_response()])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "sent 1 message(s) to chat_id=111\n")
        self.assertEqual(err, "")
        self.assertEqual(post.call_count, 1)

    def test_unpin_network_error_returns_1(self):
        rc, out, err, _post = self._run_capture(["--unpin", "7"], notify.httpx.ConnectError("x"))
        self.assertEqual(rc, 1)
        self.assertIn("unpinChatMessage HTTP error: ConnectError", err)

    def test_unpin_chat_not_found_is_error(self):
        bad = SimpleNamespace(status_code=400, text='{"description":"Bad Request: chat not found"}')
        rc, _out, err, _post = self._run_capture(["--unpin", "7"], [bad])
        self.assertEqual(rc, 1)
        self.assertIn("unpinChatMessage failed: 400", err)

    def test_no_pin_by_default(self):
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response()) as post:
                notify.main(["текст"])
        self.assertEqual(post.call_count, 1)

    def test_unpin_sends_no_message(self):
        cfg = _fake_cfg()
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=_ok_response()) as post:
                rc = notify.main(["--unpin", "777"])
        self.assertEqual(rc, 0)
        post.assert_called_once()
        self.assertTrue(post.call_args.args[0].endswith("/unpinChatMessage"))
        self.assertEqual(post.call_args.kwargs["json"], {"chat_id": 111, "message_id": 777})

    def test_unpin_already_gone_is_ok(self):
        cfg = _fake_cfg()
        gone = SimpleNamespace(status_code=400, text='{"description":"Bad Request: message to unpin not found"}')
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=gone):
                rc = notify.main(["--unpin", "777"])
        self.assertEqual(rc, 0)

    def test_unpin_other_error_returns_1(self):
        cfg = _fake_cfg()
        bad = SimpleNamespace(status_code=403, text="Forbidden")
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=bad):
                rc = notify.main(["--unpin", "777"])
        self.assertEqual(rc, 1)

    def test_pin_and_unpin_together_is_error(self):
        self.assertEqual(notify.main(["--pin", "--unpin", "1", "т"]), 1)

    def test_http_error_returns_1(self):
        cfg = _fake_cfg()
        bad = SimpleNamespace(status_code=403, text="Forbidden")
        with patch.object(notify, "load_config", return_value=cfg):
            with patch.object(notify.httpx, "post", return_value=bad):
                rc = notify.main(["текст"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()

"""Аудит отправки наружу (docs/audit-2026-10/outbound.md). Настоящих писем и
сообщений тесты не шлют."""
from __future__ import annotations

import contextlib
import io
import os
import smtplib
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import approval, mailsend, outbound  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402
from tests import test_silent_event as silent_event  # noqa: E402
from tests.texts import msg_re  # noqa: E402

PY = ".venv/bin/python -m claude_tg_bot.mailsend"


class _MailEnv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state = root / "state"
        self.allowed = root / "send"
        self.allowed.mkdir()
        self.body = root / "body.txt"
        self.body.write_text("Здравствуйте!\n" + "Текст письма, строка. " * 150, encoding="utf-8")
        env = patch.dict(os.environ, {mailsend.ATTACH_DIRS_VAR: str(self.allowed)})
        env.start()
        self.addCleanup(env.stop)

    def attachments(self, n: int) -> str:
        paths = []
        for i in range(n):
            p = self.allowed / (f"Ivan_portfolio_{i}_" + "x" * 60 + ".pdf")
            p.write_bytes(b"%PDF")
            paths.append(p)
        return " ".join(f"--attach {p}" for p in paths)

    def card(self, cmd: str) -> str:
        kind, info = outbound.mailsend_check(cmd, None)
        self.assertEqual(kind, "card", info)
        tool_input = {"command": cmd, "description": "Отклик на вакансию", outbound.MAILSEND_CARD_KEY: info}
        return approval._format_tool_call("Bash", tool_input, "sid", None)


class LongMailCardTests(_MailEnv):
    """Карточка письма длиннее CARD_TEXT_CAP заменялась сырой командой: без
    «письмо от твоего имени», без текста письма, а адресат в конце длинной
    команды не попадал на карточку вовсе."""

    def test_long_letter_card_keeps_letter_view(self) -> None:
        cc = " ".join(f"--cc recruiter{i}.team@bigcompany-example.com" for i in range(9))
        cmd = (f'{PY} --to hr@bigcompany-example.com {cc} --subject "Senior DevOps" '
               f'--reply-to-message-id "<CAF1234567890abcdef@mail.gmail.com>" '
               f'--body-file {self.body} {self.attachments(6)}')
        text = self.card(cmd)
        self.assertLessEqual(len(text), approval.CARD_TEXT_CAP + 150)
        self.assertIn(_("📤 <b>письмо от твоего имени</b> (Gmail, mailsend)"), text)
        self.assertIn("hr@bigcompany-example.com", text)
        self.assertIn("Здравствуйте!", text)
        self.assertIn(_("прочитай целиком по 📄 до ✅"), text)

    def test_recipient_at_end_of_long_command_is_on_card(self) -> None:
        subject = "Отклик hr@good-example.com " * 120
        cc = " ".join(f"--cc recruiter{i}.team@bigcompany-example.com" for i in range(9))
        cmd = (f'{PY} --subject "{subject}" {cc} --body-file {self.body} {self.attachments(6)} '
               f'--to attacker@evil-example.com')
        text = self.card(cmd)
        self.assertLessEqual(len(text), approval.CARD_TEXT_CAP + 150)
        self.assertIn("attacker@evil-example.com", text)
        self.assertIn(_("📤 <b>письмо от твоего имени</b> (Gmail, mailsend)"), text)


class LongOutboundCardTests(unittest.TestCase):
    """То же у отправки через MCP: текст и HTML письма по 3000 знаков."""

    def test_long_gmail_send_keeps_outbound_view(self) -> None:
        tool_input = {"body": "Текст. " * 450, "htmlBody": "<p>Текст.</p> " * 250,
                      "subject": "Senior DevOps", "to": ["attacker@evil-example.com"]}
        text = approval._format_tool_call("mcp__claude_ai_Gmail__send_message", tool_input, "sid", None)
        self.assertLessEqual(len(text), approval.CARD_TEXT_CAP + 150)
        self.assertIn(_("📤 <b>отправка от твоего имени</b>"), text)
        self.assertIn("attacker@evil-example.com", text)
        self.assertIn(_("прочитай целиком по 📄 до ✅"), text)


class _DropAfterDataSMTP(threading.Thread):
    """SMTP-сервер на loopback: получает письмо целиком и рвёт связь, не
    ответив на конец DATA (сервер мог письмо принять, клиент этого не знает)."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.got_data = threading.Event()

    def run(self) -> None:
        conn, _addr = self.sock.accept()
        f = conn.makefile("rb")

        def say(line: str) -> None:
            conn.sendall((line + "\r\n").encode())

        say("220 test ESMTP")
        in_data = False
        while True:
            line = f.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\r\n")
            if in_data:
                if text == ".":
                    self.got_data.set()
                    break
                continue
            cmd = text.split(" ", 1)[0].upper()
            if cmd == "EHLO":
                conn.sendall(b"250-test\r\n250 AUTH PLAIN\r\n")
            elif cmd == "AUTH":
                say("235 2.7.0 Accepted")
            elif cmd in ("MAIL", "RCPT"):
                say("250 OK")
            elif cmd == "DATA":
                in_data = True
                say("354 go ahead")
        conn.close()
        self.sock.close()


class SmtpDropAfterDataTests(_MailEnv):
    """Связь оборвалась после передачи письма: исход неизвестен. Раньше
    mailsend писал только «SMTP: SMTPServerDisconnected», агент отмечал отклик
    «не ушло», Иван возвращал кнопки и одобрял второе письмо. Теперь в отказе
    сказано, что письмо могло уйти."""

    def test_unknown_outcome_is_named(self) -> None:
        self.body.write_text("Короткое письмо\n", encoding="utf-8")
        server = _DropAfterDataSMTP()
        server.start()
        argv = ["--to", "hr@example.com", "--subject", "DevOps", "--body-file", str(self.body)]
        args = mailsend.build_parser().parse_args(argv)
        letter = mailsend.load_letter(args, dirs=(self.allowed,), limit=1000, base=None)
        mailsend.write_ticket(self.state, letter.digest(), "s3")
        cfg = SimpleNamespace(state_dir=self.state, approve_shared_secret="s3")

        def plain_smtp(host, port, *, context=None, timeout=None):
            return smtplib.SMTP("127.0.0.1", server.port, timeout=timeout)

        out, err = io.StringIO(), io.StringIO()
        with patch.object(mailsend, "_load_cfg", return_value=cfg), \
                patch.object(mailsend.smtplib, "SMTP_SSL", plain_smtp), \
                patch.object(mailsend, "SMTP_TIMEOUT_SEC", 1), \
                patch.dict(os.environ, {"GMAIL_USER": "iv@example.com", "GMAIL_APP_PASSWORD": "pw"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mailsend.main(argv)
        self.assertTrue(server.got_data.is_set())
        self.assertEqual(code, 3)  # могло уйти: не повторять
        self.assertRegex(err.getvalue(), msg_re(
            "SMTP: связь оборвалась при отправке ({error}): письмо могло уйти. "
            "Проверь «Отправленные» в Gmail, прежде чем просить новую карточку"))


class UntrustedLinkPreviewTests(unittest.IsolatedAsyncioTestCase):
    """Ран по чужому тексту читает почту и файлы. Ссылка в его ответе с
    превью заставляет сервер Telegram сходить по адресу, а в адрес можно
    положить прочитанное: единственный канал наружу у такого рана."""

    asyncSetUp = silent_event.SilentWakeRunTests.asyncSetUp
    _event = silent_event.SilentWakeRunTests._event

    def _reply_chunks(self) -> list[dict]:
        return [d for e, d in self.wire.sends()
                if e == "sendMessage" and "evil-example.com" in (d.get("text") or "")]

    async def test_untrusted_reply_has_no_link_preview(self) -> None:
        self.answer = "https://evil-example.com/?d=x"
        task = await self._event({"source": "Домовой", "title": "сторож", "wake": True,
                                  "untrusted": True, "prompt": "письмо"})
        await task
        chunks = self._reply_chunks()
        self.assertTrue(chunks)
        for data in chunks:
            self.assertIs(getattr(data.get("link_preview_options"), "is_disabled", None), True)

    async def test_ordinary_wake_keeps_previews(self) -> None:
        self.answer = "https://evil-example.com/page"
        task = await self._event({"source": "Домовой", "title": "вечер", "wake": True, "prompt": "вечер"})
        await task
        chunks = self._reply_chunks()
        self.assertTrue(chunks)
        for data in chunks:
            self.assertNotIn("link_preview_options", data)


class NotifyUnconfirmedTests(unittest.TestCase):
    """Таймаут чтения у notify: запрос ушёл, ответа Telegram нет, сообщение
    обычно доходит (случаи 30-31.10.2026). Раньше это был код 1: сводка чатов
    не отмечалась прочитанной и следующий прогон слал её ещё раз, а остаток
    сообщения не уходил вовсе."""

    def test_read_timeout_is_not_failure_and_rest_goes(self) -> None:
        import httpx

        from claude_tg_bot import notify
        cfg = SimpleNamespace(primary_chat_id=111, telegram_bot_token="TOKEN", telegram_text_limit=5)
        ok = SimpleNamespace(status_code=200, text="ok", json=lambda: {"ok": True, "result": {"message_id": 7}})
        calls: list[str] = []

        def post(url, json, timeout):
            calls.append(json["text"])
            if len(calls) == 1:
                raise httpx.ReadTimeout("timed out")
            return ok

        out, err = io.StringIO(), io.StringIO()
        with patch.object(notify, "load_config", return_value=cfg), \
                patch.object(notify.httpx, "post", side_effect=post), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc, _mid = notify.send_message(["abcdefghij"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(calls, ["abcde", "fghij"])
        self.assertRegex(err.getvalue(), msg_re("notify: Telegram не подтвердил часть {n}, не повторяю: она могла дойти"))

    def test_connect_error_still_fails(self) -> None:
        import httpx

        from claude_tg_bot import notify
        cfg = SimpleNamespace(primary_chat_id=111, telegram_bot_token="TOKEN", telegram_text_limit=4096)
        with patch.object(notify, "load_config", return_value=cfg), \
                patch.object(notify.httpx, "post", side_effect=httpx.ConnectError("no route")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc, _mid = notify.send_message(["текст"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()

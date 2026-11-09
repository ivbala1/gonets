"""Письмо модулем mailsend: Bash-команда идёт карточкой отправки, как Gmail
MCP, а модуль отправляет только письмо с хешем карточки (решение Ивана
13.10.2026). Настоящих писем тесты не шлют: SMTP подменён."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import approval, mailsend, outbound  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402
from tests.texts import msg_re  # noqa: E402

PY = ".venv/bin/python -m claude_tg_bot.mailsend"


class _Env(unittest.TestCase):
    """Временные каталоги: состояние бота, разрешённые вложения, текст письма."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.state = root / "state"
        self.allowed = root / "resume"
        self.allowed.mkdir()
        self.outside = root / "other"
        self.outside.mkdir()
        self.body = root / "body.txt"
        self.body.write_text("Здравствуйте!\nРезюме <во> вложении.\n", encoding="utf-8")
        self.pdf = self.allowed / "cv.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 test")
        (self.outside / "secret.pdf").write_bytes(b"x")
        env = patch.dict(os.environ, {mailsend.ATTACH_DIRS_VAR: str(self.allowed)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def cmd(self, extra: str = "") -> str:
        return (f'{PY} --to hr@example.com --subject "DevOps (Senior)" --body-file {self.body} '
                f'--attach {self.pdf}{extra}')


class CommandClassifyTests(_Env):
    def test_simple_forms_give_card(self) -> None:
        for cmd in (self.cmd(),
                    f"cd /home/user/src/claude-tg-bot && {self.cmd()}",
                    f"cd {self.body.parent} && python3 -m claude_tg_bot.mailsend --to a@b.c "
                    "--subject S --body-file body.txt",
                    self.cmd().replace(" --body-file", " \\\n  --body-file")):
            with self.subTest(cmd=cmd):
                kind, info = outbound.mailsend_check(cmd, "/home/user/src")
                self.assertEqual(kind, "card")
                self.assertIn("Резюме", info["body"])

    def test_dry_run_and_help_skip_card(self) -> None:
        self.assertEqual(outbound.mailsend_check(self.cmd(" --dry-run"), None)[0], "dry")
        self.assertEqual(outbound.mailsend_check(f"{PY} --help", None)[0], "dry")

    def test_tricky_forms_give_command_card(self) -> None:
        for cmd in (f'bash -c "{self.cmd()}"', f"x=$({self.cmd()})", f"`{self.cmd()}`",
                    f"{self.cmd()} | tee /tmp/log", f"{self.cmd()}; rm -f /tmp/x",
                    f"{self.cmd()} > /tmp/log", f"{self.cmd()} 2> /tmp/err", f"{self.cmd()} &",
                    f"{self.cmd()} |& head", f"{self.cmd()} | sed -i s/a/b/ {self.body}",
                    f"{self.cmd()} | head; git push", f"{self.cmd()} |", f"{self.cmd()} ;",
                    f"{self.cmd()} | grep issue#12; rm -rf /tmp/x", f"{self.cmd()} < /dev/null",
                    f"{self.cmd()} ;; cat x", f"{self.cmd()} | /usr/bin/head", f"{self.cmd()} | rg --pre=sh x",
                    f"{PY} --to a@b.c 2>&1 --subject s --body-file {self.body}",
                    f"{self.cmd()}\xa0#x; rm -rf /tmp/zz", f"{self.cmd()}\x0b#x; rm -rf /tmp/zz",
                    f"bash <<'EOF'\n{self.cmd()}\nEOF", f"FOO=1 {self.cmd()}",
                    f"timeout 60 {self.cmd()}", "python -c 'import claude_tg_bot.mailsend'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(outbound.mailsend_check(cmd, "/x")[0], "mention")

    def test_reader_tail_keeps_kind(self) -> None:
        # Карточки 09.11.2026: справка и сухой прогон с `| head`, `; cat` шли
        # карточкой команды; настоящая отправка с `| tail` давала карточку без
        # билета, и письмо не уходило.
        for extra in (" --dry-run 2>&1 | tail -8", " --dry-run | head -30; cat /tmp/x",
                      " --dry-run && echo ok || echo fail"):
            with self.subTest(extra=extra):
                self.assertEqual(outbound.mailsend_check(self.cmd(extra), None)[0], "dry")
        help_cmd = f'{PY} --help 2>&1 | head -30; cat "{self.body}"'
        self.assertEqual(outbound.mailsend_check(help_cmd, None)[0], "dry")
        # Комментарий bash (`#` в начале слова) не аргумент (ревью Codex 09.11.2026).
        self.assertEqual(outbound.mailsend_check(f"{PY} --help | head -30 # don't print more", None)[0], "dry")
        for extra in (" # send once", " | tail -8 # don't print more", " # --help",
                      " --subject 'a # b'"):
            with self.subTest(extra=extra):
                kind, info = outbound.mailsend_check(self.cmd(extra), None)
                self.assertEqual(kind, "card")
        plain = outbound.mailsend_check(self.cmd(), None)
        # `-h` у читателя в хвосте не делает отправку справкой.
        for extra in (" 2>&1 | tail -8", "; echo ok", " | grep -i sent | wc -l", " | grep -h sent"):
            with self.subTest(extra=extra):
                kind, info = outbound.mailsend_check(self.cmd(extra), None)
                self.assertEqual(kind, "card")
                self.assertEqual(info["digest"], plain[1]["digest"])

    def test_file_names_are_not_mentions(self) -> None:
        for cmd in ("cat src/claude_tg_bot/mailsend.py", ".venv/bin/python -m pytest tests/test_mailsend.py"):
            with self.subTest(cmd=cmd):
                self.assertEqual(outbound.mailsend_check(cmd, "/x")[0], "")

    def test_run_in_other_forms_gives_command_card(self) -> None:
        for cmd in (f"python3 src/claude_tg_bot/mailsend.py --to a@b.c",
                    f'echo "$({self.cmd()})"', f"git status\n{self.cmd()}",
                    "cd src/claude_tg_bot && python -m mailsend --to a@b.c",
                    "python3 - <<'EOF'\nfrom claude_tg_bot import mailsend\nmailsend.main()\nEOF",
                    f"env A=1 {self.cmd()}", f"sh -c 'git status; {self.cmd()}'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(outbound.mailsend_check(cmd, "/x")[0], "mention")

    def test_mentions_in_other_commands_are_not_mailsend(self) -> None:
        # Сообщение коммита, шаблон поиска, текст: письмо так не уходит.
        for cmd in ('git commit -m "fix mailsend approval"',
                    f'git commit -m "пример: {self.cmd()}"',
                    "git commit -m \"$(cat <<'EOF'\nкарточка для python -m claude_tg_bot.mailsend (вложения)\nEOF\n)\"",
                    f'grep -rn "{PY}" README.md', "rg -n mailsend src",
                    'echo "python -m claude_tg_bot.mailsend"', "timeout 60 git log -S mailsend",
                    "sed -n 1,20p src/claude_tg_bot/mailsend.py",
                    "cat > /tmp/x.py <<'EOF'\nfrom claude_tg_bot import mailsend\nEOF",
                    ".venv/bin/python -m pytest -k mailsend"):
            with self.subTest(cmd=cmd):
                self.assertEqual(outbound.mailsend_check(cmd, "/x"), ("", None))

    def test_rtk_prefix_and_quoted_import(self) -> None:
        # Разбор по outbound.executed_text (третье ревью 03.11.2026): `rtk git
        # commit` это git, его сообщение не исполняется; `rtk python` исполняет.
        for cmd in ("rtk git commit -m 'fix mailsend: python -m claude_tg_bot.mailsend'",
                    "rtk proxy git commit -m 'python -m claude_tg_bot.mailsend'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(outbound.mailsend_check(cmd, "/x"), ("", None))
        for cmd in (f"rtk {self.cmd()}", "rtk python -m claude_tg_bot.mailsend --to a@b.c",
                    "python3 -c \"import importlib; importlib.import_module('claude_tg_bot.mailsend').main()\""):
            with self.subTest(cmd=cmd):
                self.assertEqual(outbound.mailsend_check(cmd, "/x")[0], "mention")

    def test_bad_letter_denied(self) -> None:
        for cmd in (f"{PY} --to hr@example.com --subject S --body-file {self.body} "
                    f"--attach {self.outside / 'secret.pdf'}",
                    f"{PY} --to hr@example.com --subject S --body-file /nonexistent/body.txt",
                    f"{PY} --to 'a@b.c, d@e.f' --subject S --body-file {self.body}",
                    f"{PY} --to hr@example.com --subject S"):
            with self.subTest(cmd=cmd):
                kind, reason = outbound.mailsend_check(cmd, "/x")
                self.assertEqual(kind, "deny")
                self.assertTrue(reason.startswith("mailsend:"))

    def test_symlink_out_of_allowed_dir_denied(self) -> None:
        link = self.allowed / "link.pdf"
        link.symlink_to(self.outside / "secret.pdf")
        kind, _ = outbound.mailsend_check(
            f"{PY} --to a@b.c --subject S --body-file {self.body} --attach {link}", None)
        self.assertEqual(kind, "deny")


class HandleApproveTests(_Env):
    def _decide(self, command: str, *, press: str | None = None, extra: dict | None = None,
                unattended: bool = False) -> tuple[dict, AsyncMock]:
        cfg = SimpleNamespace(
            approve_shared_secret="s3", primary_chat_id=1, state_dir=self.state, approve_timeout_sec=5,
            approve_auto_chat_keys=frozenset({"1"}), approve_whitelist_paths=(),
            mcp_auto_allow_re=None, bash_auto_allow_re=re.compile(r".*"),
        )
        bot_data = {"unattended_sids": {"sid"} if unattended else set(),
                    "untrusted_sids": set(), "burst_until_by_target": {"1": time.time() + 600}}
        tool_input = {"command": command, "description": "письмо", **(extra or {})}
        request = SimpleNamespace(
            app={"cfg": cfg, "application": SimpleNamespace(bot_data=bot_data),
                 "registry": approval.ApprovalRegistry(), "session_targets": {"sid": (1, None)}},
            headers={"X-Approve-Token": "s3"}, remote="127.0.0.1",
            json=AsyncMock(return_value={
                "tool_name": "Bash", "tool_input": tool_input, "session_id": "sid",
                "permission_mode": "bypassPermissions", "cwd": "/home/user/src",
            }),
        )

        async def fake_card(application, cfg_, pending) -> None:
            if press is None:
                raise OSError
            pending.future.set_result(press)

        card = AsyncMock(side_effect=fake_card)
        with patch.object(approval, "send_approval_request", card), \
                patch.object(approval.asyncio, "sleep", AsyncMock()):
            resp = asyncio.run(approval.handle_approve(request))
        return json.loads(resp.text), card

    def test_send_goes_to_card_in_auto_chat_and_burst(self) -> None:
        out, card = self._decide(self.cmd())
        self.assertEqual(out["decision"], "deny")
        card.assert_called()
        shown = card.call_args.args[2].tool_input[outbound.MAILSEND_CARD_KEY]
        self.assertEqual(shown["to"], ["hr@example.com"])
        self.assertIn("Резюме <во> вложении.", shown["body"])

    def test_unattended_send_refused_without_card(self) -> None:
        # Фоновый ран письмо человеку не шлёт: черновик в отчёт (решение Ивана 03.11.2026).
        for cmd in (self.cmd(), f'bash -c "{self.cmd()}"'):
            with self.subTest(cmd=cmd[:40]):
                out, card = self._decide(cmd, unattended=True)
                self.assertEqual(out["decision"], "deny")
                self.assertIn("черновик в отчёт", out["reason"])
                card.assert_not_called()

    def test_dry_run_passes_without_card(self) -> None:
        out, card = self._decide(self.cmd(" --dry-run"))
        self.assertEqual(out["decision"], "allow")
        card.assert_not_called()

    def test_tricky_form_gets_card_not_auto(self) -> None:
        out, card = self._decide(f'bash -c "{self.cmd()}"')
        self.assertEqual(out["decision"], "deny")
        card.assert_called()
        self.assertTrue(card.call_args.args[2].tool_input[outbound.MAILSEND_CARD_KEY]["warning"])

    def test_commit_mentioning_mailsend_keeps_auto_approve(self) -> None:
        out, card = self._decide('git commit -m "fix mailsend approval"')
        self.assertEqual(out["decision"], "allow")
        card.assert_not_called()

    def test_bad_attachment_denied_without_card(self) -> None:
        out, card = self._decide(
            f"{PY} --to a@b.c --subject S --body-file {self.body} --attach {self.outside / 'secret.pdf'}")
        self.assertEqual(out["decision"], "deny")
        self.assertRegex(out["reason"], msg_re("вложение {path} не из разрешённых каталогов ({dirs})"))
        card.assert_not_called()

    def test_agent_cannot_supply_snapshot(self) -> None:
        fake = {outbound.MAILSEND_CARD_KEY: {"to": ["x@y.z"], "body": "подмена", "digest": "0" * 64}}
        out, card = self._decide("ls", extra=fake)
        self.assertEqual(out["decision"], "allow")  # обычная команда, чат с автоаппрувом
        out, card = self._decide(self.cmd(), extra=fake)
        shown = card.call_args.args[2].tool_input[outbound.MAILSEND_CARD_KEY]
        self.assertNotIn("подмена", shown["body"])

    def test_approve_writes_ticket_deny_does_not(self) -> None:
        out, _ = self._decide(self.cmd(), press="deny")
        self.assertEqual(out["decision"], "deny")
        self.assertEqual(list(mailsend.ticket_dir(self.state).glob("*.json")), [])
        out, card = self._decide(self.cmd(), press="allow")
        self.assertEqual(out["decision"], "allow")
        digest = card.call_args.args[2].tool_input[outbound.MAILSEND_CARD_KEY]["digest"]
        self.assertTrue((mailsend.ticket_dir(self.state) / f"{digest}.json").is_file())


class SendAfterCardTests(_Env):
    """Билет карточки и отправка: то же письмо уходит один раз, изменённое нет."""

    def _approve(self) -> None:
        kind, info = outbound.mailsend_check(self.cmd(), None)
        self.assertEqual(kind, "card")
        mailsend.write_ticket(self.state, info["digest"], "s3")

    def _run(self, refused: dict | None = None) -> tuple[int, MagicMock, str]:
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.send_message.return_value = refused or {}
        cfg = SimpleNamespace(state_dir=self.state, approve_shared_secret="s3")
        out, err = io.StringIO(), io.StringIO()
        argv = ["--to", "hr@example.com", "--subject", "DevOps (Senior)", "--body-file", str(self.body),
                "--attach", str(self.pdf)]
        with patch.object(mailsend, "_load_cfg", return_value=cfg), \
                patch.object(mailsend.smtplib, "SMTP_SSL", return_value=smtp) as ssl_cls, \
                patch.dict(os.environ, {"GMAIL_USER": "iv@example.com",
                                        "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mailsend.main(argv)
        self.assertEqual(ssl_cls.call_count, smtp.send_message.call_count)
        return code, smtp, out.getvalue() + err.getvalue()

    def test_no_card_no_send(self) -> None:
        code, smtp, text = self._run()
        self.assertEqual(code, 1)
        smtp.send_message.assert_not_called()
        self.assertIn(_("нет одобрения карточкой на это письмо: запускай без обходов, чтобы бот показал "
                        "карточку, и не меняй текст и вложения после неё"), text)

    def test_approved_letter_sent_once(self) -> None:
        self._approve()
        code, smtp, text = self._run()
        self.assertEqual(code, 0, text)
        smtp.login.assert_called_once_with("iv@example.com", "abcdefghijklmnop")
        msg = smtp.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "hr@example.com")
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["cv.pdf"])
        self.assertIn("Message-ID: <", text)
        self.assertNotIn("abcd", text)
        code, smtp, _ = self._run()
        self.assertEqual(code, 1)
        smtp.send_message.assert_not_called()

    def test_partly_refused_recipients_is_rc_3(self) -> None:
        # Части адресатов письмо ушло: код 3, повтор отправил бы его им ещё раз.
        self._approve()
        code, smtp, text = self._run(refused={"cc@example.com": (550, b"no")})
        self.assertEqual(code, 3, text)
        smtp.send_message.assert_called_once()
        self.assertIn("cc@example.com", text)

    def test_changed_body_or_attachment_after_card_refused(self) -> None:
        for change in (lambda: self.body.write_text("Другой текст", encoding="utf-8"),
                       lambda: self.pdf.write_bytes(b"%PDF other")):
            with self.subTest(change=change):
                self.body.write_text("Здравствуйте!\nРезюме <во> вложении.\n", encoding="utf-8")
                self.pdf.write_bytes(b"%PDF-1.4 test")
                self._approve()
                change()
                code, smtp, _ = self._run()
                self.assertEqual(code, 1)
                smtp.send_message.assert_not_called()

    def test_forged_or_stale_ticket_refused(self) -> None:
        kind, info = outbound.mailsend_check(self.cmd(), None)
        mailsend.write_ticket(self.state, info["digest"], "другой секрет")
        self.assertEqual(_("билет одобрения не от бота"), mailsend.take_ticket(self.state, info["digest"], "s3"))
        mailsend.write_ticket(self.state, info["digest"], "s3")
        with patch.object(mailsend.time, "time", return_value=time.time() + mailsend.TICKET_TTL_SEC + 5):
            self.assertEqual(_("одобрение карточкой устарело, нужна новая карточка"),
                             mailsend.take_ticket(self.state, info["digest"], "s3"))

    def test_reply_headers(self) -> None:
        args = mailsend.build_parser().parse_args(
            ["--to", "a@b.c", "--subject", "Re: x", "--body-file", str(self.body),
             "--reply-to-message-id", "<abc@mail.gmail.com>"])
        letter = mailsend.load_letter(args, dirs=(self.allowed,), limit=1000, base=None)
        msg = mailsend.build_message(letter, "iv@example.com")
        self.assertEqual(msg["In-Reply-To"], "<abc@mail.gmail.com>")
        self.assertEqual(msg["References"], "<abc@mail.gmail.com>")


class MailCardTextTests(_Env):
    def _input(self, body: str | None = None) -> dict:
        if body is not None:
            self.body.write_text(body, encoding="utf-8")
        kind, info = outbound.mailsend_check(self.cmd(), None)
        self.assertEqual(kind, "card")
        return {"command": self.cmd(), "description": "", outbound.MAILSEND_CARD_KEY: info}

    def test_card_shows_recipient_subject_body_and_sizes(self) -> None:
        text = approval._format_tool_call("Bash", self._input(), "sid")
        self.assertIn(_("📤 <b>письмо от твоего имени</b> (Gmail, mailsend)"), text)
        self.assertIn("hr@example.com", text)
        self.assertIn("DevOps (Senior)", text)
        self.assertIn("Резюме &lt;во&gt; вложении.", text)
        self.assertIn("cv.pdf", text)
        self.assertIn(_("{size} Б").format(size=13), text)
        self.assertNotIn(_("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>"), text)

    def test_long_body_full_view_and_no_burst_buttons(self) -> None:
        tool_input = self._input("абв " * 1500 + "КОНЕЦ")
        text = approval._format_tool_call("Bash", tool_input, "sid")
        self.assertIn(_("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>"), text)
        self.assertTrue(approval.needs_full_view(tool_input, "Bash"))
        full = approval.full_view_text("Bash", tool_input)
        self.assertIn("КОНЕЦ", full)
        self.assertNotIn(_("\n\n--- остальные поля ---\n"), full)

        async def go() -> list:
            registry = approval.ApprovalRegistry()
            pending = registry.create("Bash", tool_input, session_id="sid", target_chat_id=1)
            bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
            await approval.send_approval_request(SimpleNamespace(bot=bot),
                                                 SimpleNamespace(primary_chat_id=1), pending)
            kb = bot.send_message.call_args.kwargs["reply_markup"]
            return [b.callback_data for row in kb.inline_keyboard for b in row]
        data = asyncio.run(go())
        self.assertFalse(any(d.endswith(("auto15", "auto60")) for d in data))
        self.assertTrue(any(d.startswith("apprfull:") for d in data))


if __name__ == "__main__":
    unittest.main()

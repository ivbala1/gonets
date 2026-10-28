from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Update
from telegram.ext import ApplicationHandlerStop

from claude_tg_bot.auth import LoginManager, LoginSession, is_auth_error, login_details
from claude_tg_bot.bot import (
    _is_claude_unavailable, _login_keyboard, _offer_login, _start_login, on_auth_update,
)
from claude_tg_bot.claude_runner import ClaudeResult, run_claude
from claude_tg_bot.codex_runner import run_codex
from claude_tg_bot.i18n import _

ERROR = "Failed to authenticate: OAuth session expired and could not be refreshed"
CODE = "a" * 43 + "#" + "b" * 43
URL = "https://claude.com/cai/oauth/authorize?state=test&code_challenge=test"


class AuthClassificationTests(unittest.TestCase):
    def test_auth_failure_without_http_activates_fallback(self):
        self.assertTrue(is_auth_error(ERROR))
        self.assertTrue(_is_claude_unavailable(ClaudeResult(text=ERROR, is_error=True, auth_error="claude")))

    def test_regular_error_or_answer_does_not_trigger_fallback(self):
        for text in (ERROR, "refusal", "cancelled", "timeout", "model not found"):
            self.assertFalse(_is_claude_unavailable(ClaudeResult(text=text)))
        self.assertFalse(_is_claude_unavailable(ClaudeResult(text="", auth_error="codex")))
        self.assertFalse(is_auth_error("Rate limit exceeded"))

    def test_old_http_policy_remains(self):
        for status in (401, 429, 500, 503, 529):
            self.assertTrue(_is_claude_unavailable(ClaudeResult(text="", api_error_status=status)))
        for status in (None, 400, 403, 404):
            self.assertFalse(_is_claude_unavailable(ClaudeResult(text="", api_error_status=status)))

    def test_login_output_requires_complete_trusted_url(self):
        self.assertIsNone(login_details("claude", URL))
        self.assertEqual(login_details("claude", URL + "\nPaste code here > "), (URL, ""))
        for url in (
            "https://claude.com.evil.test/cai/oauth/authorize",
            "https://claude.com@evil.test/cai/oauth/authorize",
            "https://claude.com:bad/cai/oauth/authorize",
            "https://claude.com/other",
        ):
            self.assertIsNone(login_details("claude", url + "\n"))

    def test_codex_device_code_after_ansi_removal(self):
        output = "\x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\x1b[94mABCD-12345\x1b[0m\n"
        self.assertEqual(login_details("codex", output), ("https://auth.openai.com/codex/device", "ABCD-12345"))
        self.assertIsNone(login_details("codex", "https://auth.openai.com/codex/device\n"))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def claude_result(self, result, *, rc=1, is_error=True, stderr=b"", assistant=None):
        proc = MagicMock(returncode=rc, pid=123456)
        proc.stdin.drain = AsyncMock()
        proc.wait = AsyncMock(return_value=rc)
        proc.stdout = asyncio.StreamReader()
        if assistant:
            proc.stdout.feed_data((json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": assistant}]}}) + "\n").encode())
        proc.stdout.feed_data((json.dumps({"type": "result", "result": result, "is_error": is_error}) + "\n").encode())
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()
        proc.stderr.feed_data(stderr)
        proc.stderr.feed_eof()
        store = MagicMock()
        store.get.return_value = None
        with (
            patch("claude_tg_bot.claude_runner._ensure_hook_still_valid"),
            patch("claude_tg_bot.claude_runner.ensure_oauth_fresh", new=AsyncMock()),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            return await run_claude("claude", "1", 1, None, "hello", store,
                                    cwd=Path("/tmp"), hook_settings=Path("/tmp/test-settings"))

    async def test_claude_auth_error_with_and_without_nonzero_exit(self):
        for rc in (0, 1):
            result = await self.claude_result(ERROR, rc=rc)
            self.assertEqual(result.auth_error, "claude")
            self.assertTrue(result.is_error)
            self.assertTrue(_is_claude_unavailable(result))

    async def test_claude_answer_and_tool_output_do_not_control_auth(self):
        result = await self.claude_result(ERROR, rc=0, is_error=False)
        self.assertFalse(result.auth_error)
        result = await self.claude_result("model not found", assistant=ERROR)
        self.assertFalse(result.auth_error)
        self.assertFalse(_is_claude_unavailable(result))

    async def test_cancel_never_falls_back(self):
        result = await self.claude_result(ERROR, rc=-15)
        self.assertFalse(result.auth_error)
        self.assertFalse(_is_claude_unavailable(result))

    async def test_oauth_warmup_runs_without_tools(self):
        # Прогрев входа идёт без хука бота: ни инструментов, ни MCP, режим
        # dontAsk, без сессии на диске (ревью Codex 19.10.2026).
        from claude_tg_bot import claude_runner
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"{}", b""))
        expiring = MagicMock(side_effect=[0.0, 0.0, 0.0])
        with (
            patch.object(claude_runner, "_oauth_expires_at", expiring),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn,
        ):
            await claude_runner.ensure_oauth_fresh("claude")
        args = list(spawn.call_args.args)
        tools = args.index("--tools")
        self.assertEqual(args[tools + 1], "")
        self.assertTrue(args[tools + 2].startswith("--"))
        for flag in ("--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[-1], "ok")

    async def codex_result(self, events, *, rc=1, stderr=b""):
        raw = "\n".join(json.dumps(e) for e in events).encode()
        proc = MagicMock(returncode=rc)
        proc.communicate = AsyncMock(return_value=(raw, stderr))
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
            result = await run_codex("codex", "prompt", cwd=Path("/tmp"))
        self.assertIn("--json", spawn.call_args.args)
        return result

    async def test_codex_auth_diagnostics(self):
        for message in ("Your refresh token could not be refreshed", "refresh_token_reused", "Please log in again"):
            result = await self.codex_result([{"type": "turn.failed", "error": {"message": message}}])
            self.assertTrue(result.auth_error)
        result = await self.codex_result([], stderr=b"Token refresh failed: invalid_grant")
        self.assertTrue(result.auth_error)

    async def test_codex_items_and_successful_retry_do_not_trigger_auth(self):
        for item_type in ("agent_message", "command_execution"):
            result = await self.codex_result([{"type": "item.completed", "item": {"type": item_type, "text": ERROR}}])
            self.assertFalse(result.auth_error)
        result = await self.codex_result([{"type": "error", "message": ERROR}, {"type": "turn.completed"}], rc=0)
        self.assertFalse(result.auth_error)
        self.assertFalse(result.is_error)


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent = []

        self.code_asked = asyncio.Event()

        async def send(**kw):
            self.sent.append(kw)
            if kw.get("protect_content"):
                self.code_asked.set()
            return SimpleNamespace(delete=AsyncMock())

        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        self.script = Path(self.tmp.name) / "fake-cli"
        # Срок входа по умолчанию: эти тесты проверяют ход входа, а не часы.
        # С ttl=3 медленный запуск fake CLI (на маке до секунд) обрывал вход
        # по сроку, тест срока задаёт свой ttl.
        self.manager = LoginManager(self.bot, str(self.script), str(self.script), Path(self.tmp.name))
        self.addAsyncCleanup(self.manager.close)

    def script_body(self, body):
        self.script.write_text(f"#!{sys.executable}\nimport sys, time, json\n" + body)
        self.script.chmod(0o700)

    async def wait_for_code(self):
        # Ждём событие, а не опрос с таймаутом: ссылка с запросом кода ушла
        # в чат или вход уже завершился (тогда кода не ждут и тест падает).
        session = self.manager.session
        asked = asyncio.ensure_future(self.code_asked.wait())
        await asyncio.wait({asked, session.task}, return_when=asyncio.FIRST_COMPLETED)
        asked.cancel()
        self.assertTrue(session.accepts_code)

    def claude_script(self, *, status=True):
        self.script_body(
            "if sys.argv[1:] == ['auth', 'status']:\n"
            f" print(json.dumps({{'loggedIn': {status!r}}}))\n"
            "else:\n"
            f" print({URL!r}, flush=True)\n"
            " print('Paste code here > ', end='', flush=True)\n"
            f" assert sys.stdin.readline().strip() == {CODE!r}\n"
        )

    async def test_claude_login_code_via_stdin_then_status(self):
        self.claude_script()
        self.assertTrue(self.manager.start("claude", 1, 1))
        session = self.manager.session
        self.assertFalse(self.manager.start("codex", 2, 2))
        await self.wait_for_code()
        self.assertEqual(_("Нет ожидающего входа. Начни с /login claude."), await self.manager.submit(2, 2, CODE))
        self.assertEqual(_("Нужен одноразовый код с сайта Claude целиком, без ссылки и пробелов."),
                         await self.manager.submit(1, 1, "https://example.test"))
        self.assertEqual(_("Код принят. Проверяю вход."), await self.manager.submit(1, 1, CODE))
        self.assertEqual(_("Код уже отправлен. Жду результат входа."), await self.manager.submit(1, 1, CODE))
        await session.task
        self.assertIsNone(self.manager.session)
        self.assertIn(_("Вход {name} восстановлен. Можно повторить запрос боту.").format(name="Claude"),
                      self.sent[-1]["text"])
        self.assertNotIn(CODE, str(self.sent))
        for message in session.messages:
            message.delete.assert_awaited_once()

    async def test_success_exit_requires_positive_status(self):
        self.claude_script(status=False)
        self.manager.start("claude", 1, 1)
        session = self.manager.session
        await self.wait_for_code()
        await self.manager.submit(1, 1, CODE)
        await session.task
        self.assertIn(_("Не удалось подтвердить вход {name}. Повтори /login {engine}.").format(
            name="Claude", engine="claude"), self.sent[-1]["text"])

    async def test_codex_flow_never_needs_code_in_chat(self):
        self.script_body(
            "if sys.argv[1:] == ['login', '--device-auth']:\n"
            " print('https://auth.openai.com/codex/device\\nABCD-12345', flush=True)\n"
            "else:\n print('Logged in using ChatGPT')\n"
        )
        self.manager.start("codex", 1, 1)
        await self.manager.session.task
        self.assertTrue(any("ABCD-12345" in s["text"] and s["protect_content"] for s in self.sent))
        self.assertIn(_("Вход {name} восстановлен. Можно повторить запрос боту.").format(name="Codex"),
                      self.sent[-1]["text"])

    async def test_timeout_kills_login_process_and_clears_state(self):
        self.script_body("print('waiting', flush=True)\ntime.sleep(30)\n")
        self.manager.ttl = 0.15
        self.manager.start("claude", 1, 1)
        session = self.manager.session
        await session.task
        self.assertIsNotNone(session.proc.returncode)
        self.assertIsNone(self.manager.session)
        self.assertIn(_("Время ожидания входа истекло. Повтори /login {engine}.").format(engine="claude"),
                      self.sent[-1]["text"])

    async def test_cancel_and_shutdown_clean_process(self):
        self.claude_script()
        self.manager.start("claude", 1, 1)
        session = self.manager.session
        await self.wait_for_code()
        self.assertFalse(await self.manager.cancel(2, 2))
        self.assertTrue(await self.manager.cancel(1, 1))
        self.assertIsNotNone(session.proc.returncode)
        self.assertIsNone(self.manager.session)
        self.manager.start("codex", 1, 1)
        await self.manager.close()
        self.assertIsNone(self.manager.session)

    async def test_cli_failure_does_not_echo_output(self):
        self.script_body(f"print({CODE!r})\nsys.exit(1)\n")
        self.manager.start("codex", 1, 1)
        await self.manager.session.task
        self.assertNotIn(CODE, str(self.sent))
        self.assertIn(_(
            "Вход Codex не завершён. Если вход по коду устройства выключен, "
            "включи его в настройках безопасности ChatGPT и повтори /login codex. "
            "Другой способ: codex login в терминале WSL."
        ), self.sent[-1]["text"])


class TelegramAuthTests(unittest.IsolatedAsyncioTestCase):
    def setup_context(self, *, session=None):
        manager = MagicMock(session=session, binaries={"claude": "claude", "codex": "codex"})
        manager.submit = AsyncMock(return_value="Код принят")
        manager.cancel = AsyncMock(return_value=True)
        bot = SimpleNamespace(username="example_test_bot", send_message=AsyncMock())
        return SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={
            "cfg": SimpleNamespace(allowed_user_ids={1}, allowed_chat_ids={-1}),
            "login_manager": manager, "chat_locks": {},
        }))

    def update(self, text, *, user_id=1, chat_id=1, edited=False, quote=None):
        payload = {"message_id": 1, "date": 1, "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                   "from": {"id": user_id, "is_bot": False, "first_name": "Test"}, "text": text}
        if quote:
            payload["reply_to_message"] = {**payload, "message_id": 2, "text": quote}
        return Update.de_json({"update_id": 1, "edited_message" if edited else "message": payload}, None)

    async def guard(self, update, ctx):
        with patch("telegram.Message.delete", new=AsyncMock()) as delete, patch("telegram.Message.reply_text", new=AsyncMock()):
            with self.assertRaises(ApplicationHandlerStop):
                await on_auth_update(update, ctx)
        return delete

    async def test_explicit_and_bare_codes_are_consumed_before_model(self):
        for text in (f"/authcode {CODE}", CODE):
            ctx = self.setup_context(session=LoginSession("claude", 1, 1, accepts_code=True))
            delete = await self.guard(self.update(text), ctx)
            delete.assert_awaited_once()
            ctx.application.bot_data["login_manager"].submit.assert_awaited_once_with(1, 1, CODE)
            self.assertNotIn(CODE, str(ctx.bot.send_message.call_args_list))

    async def test_stale_edited_group_and_foreign_codes_never_enter_model(self):
        for update in (self.update(CODE), self.update(CODE, edited=True), self.update(CODE, chat_id=-1), self.update(CODE, user_id=2)):
            ctx = self.setup_context()
            await self.guard(update, ctx)
            if update.edited_message or update.effective_chat.id < 0 or update.effective_user.id != 1:
                ctx.application.bot_data["login_manager"].submit.assert_not_awaited()

    async def test_quoted_code_and_other_commands_during_login_are_blocked(self):
        ctx = self.setup_context(session=LoginSession("claude", 1, 1))
        for text in ("hello", "/compose hello", "/at 5m hello"):
            await self.guard(self.update(text), ctx)
        await self.guard(self.update("hello", quote=f"/authcode {CODE}"), self.setup_context())

    async def test_hex_hash_without_login_reaches_model(self):
        # Хеш коммита (40) и sha256 (64) вне входа уходят модели, во время
        # входа удаляются как код (решение владельца 12.10.2026).
        for text in ("0123456789abcdef" * 2 + "01234567", "ABCDEF0123456789" * 4):
            ctx = self.setup_context()
            await on_auth_update(self.update(text), ctx)
            ctx = self.setup_context()
            await on_auth_update(self.update("hello", quote=text), ctx)
            ctx = self.setup_context(session=LoginSession("claude", 1, 1, accepts_code=True))
            delete = await self.guard(self.update(text), ctx)
            delete.assert_awaited_once()
        for text in ("a" * 41, "0123456789abcdef" * 4 + "0", CODE):
            await self.guard(self.update(text), self.setup_context())

    async def test_login_requires_private_and_allowed_user(self):
        for update in (self.update("/login claude", chat_id=-1), self.update("/login claude", user_id=2)):
            ctx = self.setup_context()
            with patch("telegram.Message.reply_text", new=AsyncMock()):
                await _start_login(update, ctx, "claude")
            ctx.application.bot_data["login_manager"].start.assert_not_called()

    async def test_start_deeplink_and_cancel(self):
        ctx = self.setup_context()
        await self.guard(self.update("/start login_codex"), ctx)
        ctx.application.bot_data["login_manager"].start.assert_called_once_with("codex", 1, 1)
        ctx = self.setup_context(session=LoginSession("claude", 1, 1))
        await self.guard(self.update("/cancel"), ctx)
        ctx.application.bot_data["login_manager"].cancel.assert_awaited_once_with(1, 1)

    async def test_group_button_contains_no_login_secret(self):
        ctx = self.setup_context()
        keyboard = _login_keyboard(ctx, ("claude",), private=False)
        self.assertEqual(keyboard.inline_keyboard[0][0].url, "https://t.me/example_test_bot?start=login_claude")

    async def test_notice_delivery_failure_does_not_break_fallback(self):
        with patch("telegram.Message.reply_text", new=AsyncMock(side_effect=RuntimeError("network"))):
            await _offer_login(self.update("test"), self.setup_context(), "claude")


if __name__ == "__main__":
    unittest.main()

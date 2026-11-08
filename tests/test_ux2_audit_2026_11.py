"""Второй круг удобства (ux2, 01.11.2026): видно, что идёт, и меньше шума.

/status первой строкой отвечает, что идёт, сколько и как остановить; срок
карточки виден в самой карточке, а по истечении дописывается в неё же;
служебные сообщения хода («✅ auto», «💾», «🆕 новая сессия», «не удалось
закрепить session_id») уходят строкой футера, а не отдельными сообщениями.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import approval, bot, limits, replies  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult  # noqa: E402
from tests.test_review_bot_main import GROUP, _Base, _msg  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402

CHAT = -100
THREAD = 5
KEY = f"{CHAT}:{THREAD}"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _status_ctx(*, run_watch: dict | None = None, procs: dict | None = None,
                waiting: int = 0) -> SimpleNamespace:
    registry = SimpleNamespace(pending_count=0, _items={})
    cfg = SimpleNamespace(projects=None)
    bot_data = {
        "cfg": cfg, "sessions": SimpleNamespace(get=lambda k: None), "procs": procs or {},
        "active_sessions": {}, "started_at": 0.0, "registry": registry,
        "run_watch": run_watch or {}, "lock_waiting": {KEY: waiting},
    }
    return SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))


def _run_status(ctx: SimpleNamespace, clock: _Clock) -> bot._RunStatus:
    st = bot._RunStatus(SimpleNamespace(), CHAT, THREAD, chat_key=KEY,
                        registry=ctx.application.bot_data["run_watch"], clock=clock)
    ctx.application.bot_data["run_watch"][st.run_id] = st
    return st


async def _call_status(ctx: SimpleNamespace, limit: str = "") -> str:
    msg = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=msg)
    with patch.object(bot, "_is_allowed", return_value=True), \
            patch.object(bot, "_chat_key", return_value=(KEY, CHAT, THREAD)), \
            patch.object(bot, "_current_sid", return_value="abcdef123456"), \
            patch.object(bot, "_memory_status_line", AsyncMock(return_value="ok")), \
            patch.object(bot, "_scope_note", return_value=""), \
            patch.object(bot, "_model_label", return_value="sonnet"), \
            patch.object(bot, "_effort_for", return_value="high"), \
            patch.object(bot, "burst_until_ts", return_value=0.0), \
            patch.object(bot, "_workshop_status", return_value=""), \
            patch.object(bot, "_cost_day_line", return_value=""), \
            patch.object(bot.limits, "summary", return_value=limit):
        await bot.on_status(update, ctx)
    return msg.reply_text.await_args.args[0]


class StatusHeadlineTests(unittest.TestCase):
    """Первая строка /status: что идёт, сколько идёт, как остановить (ux.md 13)."""

    def test_running_turn_first_line(self) -> None:
        clock = _Clock()
        ctx = _status_ctx(procs={KEY: SimpleNamespace(returncode=None, pid=42)})
        st = _run_status(ctx, clock)

        async def go() -> str:
            await st.on_tool("Bash: pytest -q")
            clock.t += 245
            return await _call_status(ctx)
        with patch.object(st, "_show", AsyncMock()):
            text = asyncio.run(go())
        first = text.splitlines()[0]
        self.assertTrue(first.startswith("▶️"), text)
        self.assertIn(_("4м05с"), first)
        self.assertIn("Bash: pytest -q", first)
        self.assertIn(_("шаг {steps}").format(steps=1), first)
        self.assertIn("«стоп»", first)

    def test_idle_first_line(self) -> None:
        text = asyncio.run(_call_status(_status_ctx()))
        self.assertEqual(text.splitlines()[0], _("💤 сейчас ничего не идёт"))

    def test_process_without_status(self) -> None:
        # Ран без живого статуса (codex): идёт, а шагов не видно.
        ctx = _status_ctx(procs={KEY: SimpleNamespace(returncode=None, pid=42)}, waiting=2)
        first = asyncio.run(_call_status(ctx)).splitlines()[0]
        self.assertTrue(first.startswith("▶️"), first)
        self.assertIn("«стоп»", first)
        self.assertIn(_("в очереди {n}").format(n=2), first)

    def test_uptime_in_hours_and_limit(self) -> None:
        ctx = _status_ctx()
        ctx.application.bot_data["started_at"] = 0.0
        with patch.object(bot.time, "time", return_value=3 * 3600 + 25 * 60):
            text = asyncio.run(_call_status(ctx, limit="5 ч 62%, неделя 40%"))
        self.assertNotIn("uptime: 1", text)
        self.assertIn("5 ч 62%, неделя 40%", text.splitlines()[1])
        self.assertIn(_("{h} ч {m} мин").format(h=3, m=25), text)


class _CardBot:
    """Telegram без сети для карточек: копит отправки и правки."""

    def __init__(self, *, edit_fails: bool = False) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.markups: list[dict] = []
        self._edit_fails = edit_fails

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=10)

    async def edit_message_text(self, *args, **kw):
        if self._edit_fails:
            raise RuntimeError("edit failed")
        self.edits.append({"text": args[0], **kw} if args else kw)

    async def edit_message_reply_markup(self, **kw):
        self.markups.append(kw)


def _approve_request(cfg: SimpleNamespace, tg: _CardBot, tool_name: str, tool_input: dict,
                     bot_data: dict | None = None):
    data = {"unattended_sids": set(), "untrusted_sids": set(), "burst_until_by_target": {}}
    data.update(bot_data or {})
    return SimpleNamespace(
        app={"cfg": cfg, "application": SimpleNamespace(bot_data=data, bot=tg),
             "registry": approval.ApprovalRegistry(),
             "session_targets": {"sid": (CHAT, THREAD)}},
        headers={}, remote="127.0.0.1",
        json=AsyncMock(return_value={
            "tool_name": tool_name, "tool_input": tool_input,
            "session_id": "sid", "permission_mode": "default",
        }),
    )


def _card_cfg(timeout: float, **kw) -> SimpleNamespace:
    base = dict(
        approve_shared_secret="", primary_chat_id=1, state_dir=Path(tempfile.gettempdir()),
        approve_auto_chat_keys=frozenset(), approve_whitelist_paths=(),
        mcp_auto_allow_re=None, approve_timeout_sec=timeout,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class CardDeadlineTests(unittest.TestCase):
    """Срок карточки в самой карточке, итог срока дописывается в неё (ux.md 10)."""

    def _send_card(self, timeout: int = 900) -> tuple[str, approval.Pending]:
        tg = _CardBot()

        async def go() -> approval.Pending:
            pending = approval.ApprovalRegistry().create(
                "Bash", {"command": "rm -rf build"}, session_id="sid",
                target_chat_id=CHAT, target_thread_id=THREAD)
            pending.deadline = time.time() + timeout
            await approval.send_approval_request(SimpleNamespace(bot=tg, bot_data={}),
                                                 _card_cfg(timeout), pending)
            return pending
        pending = asyncio.run(go())
        return tg.sent[0]["text"], pending

    def test_card_names_deadline_and_outcome(self) -> None:
        with patch.object(approval.time, "time", return_value=1_700_000_000.0):
            text, pending = self._send_card(900)
        until = limits.clock(1_700_000_000.0 + 900)
        self.assertIn(_("⌛ жду до {until} ({mins} мин), потом отказ").format(until=until, mins=15), text)
        self.assertEqual(pending.card_html, text)

    def test_timeout_edits_card_without_new_message(self) -> None:
        tg = _CardBot()
        request = _approve_request(_card_cfg(0.01), tg, "Bash", {"command": "rm -rf build"})

        async def send(_app, _cfg, pending):
            pending.telegram_message_id = 10
            pending.card_html = "🔴 <b>rm</b>"

        with patch.object(approval, "send_approval_request", AsyncMock(side_effect=send)), \
                patch.object(approval.serious, "send_reasons", return_value={"тестовая причина"}), \
                patch.object(approval.audit, "write_event"):
            resp = asyncio.run(approval.handle_approve(request))
        self.assertIn("не ответил на карточку", json.loads(resp.text)["reason"])
        self.assertEqual(tg.sent, [])
        self.assertEqual(len(tg.edits), 1)
        edit = tg.edits[0]
        self.assertTrue(edit["text"].startswith("🔴 <b>rm</b>\n\n⌛"), edit["text"])
        self.assertEqual(edit.get("parse_mode"), "HTML")
        self.assertIsNone(edit.get("reply_markup"))

    def test_timeout_edit_failure_quiet_fallback(self) -> None:
        tg = _CardBot(edit_fails=True)
        request = _approve_request(_card_cfg(0.01), tg, "Bash", {"command": "rm -rf build"})

        async def send(_app, _cfg, pending):
            pending.telegram_message_id = 10
            pending.card_html = "🔴 <b>rm</b>"

        with patch.object(approval, "send_approval_request", AsyncMock(side_effect=send)), \
                patch.object(approval.serious, "send_reasons", return_value={"тестовая причина"}), \
                patch.object(approval.audit, "write_event"):
            asyncio.run(approval.handle_approve(request))
        self.assertEqual(len(tg.markups), 1)
        self.assertEqual(len(tg.sent), 1)
        self.assertIs(tg.sent[0].get("disable_notification"), True)
        self.assertNotIn("aid=", tg.sent[0]["text"])


class ToolNotesToFooterTests(unittest.TestCase):
    """«✅ auto (whitelist)» и «💾 подаю в память» во время рана темы копятся
    для футера, а не идут отдельным сообщением со звуком (ux.md 11)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve() / "work"  # /var на маке ссылка
        self.work.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def _whitelist(self, bot_data: dict) -> tuple[dict, _CardBot]:
        tg = _CardBot()
        cfg = _card_cfg(5, approve_whitelist_paths=(self.work,), state_dir=self.state)
        request = _approve_request(cfg, tg, "Write", {"file_path": str(self.work / "notes.md"),
                                                       "content": "x"}, bot_data)
        with patch.object(approval.audit, "write_event"):
            resp = asyncio.run(approval.handle_approve(request))
        self.assertEqual(json.loads(resp.text)["decision"], "allow")
        return request.app["application"].bot_data, tg

    def test_whitelist_write_goes_to_footer_during_run(self) -> None:
        data, tg = self._whitelist({"run_notes": {KEY: []}})
        self.assertEqual(tg.sent, [])
        self.assertEqual(len(data["run_notes"][KEY]), 1)
        kind, item = data["run_notes"][KEY][0]
        self.assertEqual(kind, "auto")
        self.assertIn("notes.md", item)

    def test_whitelist_without_run_still_notifies(self) -> None:
        # Нет хода темы, который соберёт футер: прежнее сообщение.
        _data, tg = self._whitelist({})
        self.assertEqual(len(tg.sent), 1)

    def test_memory_write_goes_to_footer(self) -> None:
        tg = _CardBot()
        cfg = _card_cfg(5, state_dir=self.state)
        request = _approve_request(cfg, tg, "Bash", {"command": "memory add"}, {"run_notes": {KEY: []}})
        saved = [{"dry_run": False, "scope": "global", "name": "кофе"}]
        with patch.object(approval, "memory_write_auto_allowed", return_value=saved), \
                patch.object(approval.audit, "write_event"):
            asyncio.run(approval.handle_approve(request))
        self.assertEqual(tg.sent, [])
        self.assertEqual(request.app["application"].bot_data["run_notes"][KEY],
                         [("memory", "global · кофе")])


class FooterNotesTests(unittest.TestCase):
    def _footer(self, result: ClaudeResult, notes: list[str], min_usd: float = 0.0):
        cfg = SimpleNamespace(show_cost_footer=True, context_warn_tokens=0, cost_footer_min_usd=min_usd)
        msg = SimpleNamespace(reply_text=AsyncMock())
        sent = asyncio.run(replies.send_cost_footer(cfg, SimpleNamespace(message=msg), result, notes=notes))
        return sent, msg.reply_text

    def test_notes_appended_to_cost_line(self) -> None:
        sent, reply = self._footer(ClaudeResult(text="ok", cost_usd=0.5, session_cost_usd=1.0),
                                   ["✍️ x"])
        self.assertTrue(sent)
        text = reply.await_args.args[0]
        self.assertTrue(text.startswith("💸"), text)
        self.assertTrue(text.endswith("\n✍️ x"), text)
        self.assertIs(reply.await_args.kwargs.get("disable_notification"), True)

    def test_notes_sent_for_cheap_answer(self) -> None:
        sent, reply = self._footer(ClaudeResult(text="ok", cost_usd=0.01, session_cost_usd=0.0),
                                   ["✍️ x"], min_usd=0.5)
        self.assertTrue(sent)
        self.assertEqual(reply.await_args.args[0], "✍️ x")

    def test_no_notes_cheap_answer_silent(self) -> None:
        sent, reply = self._footer(ClaudeResult(text="ok", cost_usd=0.01, session_cost_usd=1.0),
                                   [], min_usd=0.5)
        self.assertFalse(sent)
        reply.assert_not_awaited()

    def test_format_groups_and_caps(self) -> None:
        items = [("auto", f"Write f{i}.md") for i in range(10)] + [("auto", "Write f0.md"),
                                                                   ("memory", "global · a")]
        lines = replies.format_run_notes(items)
        self.assertEqual(len(lines), 2)
        self.assertIn("f7.md", lines[0])
        self.assertNotIn("f8.md", lines[0])
        self.assertIn(_(" и ещё {n}").format(n=2), lines[0])
        self.assertIn("global · a", lines[1])


class TurnNotesTests(_Base):
    """Ход темы собирает строки для футера; не дошло до футера, одно тихое
    сообщение в конце, ничего не теряется."""

    def _note_in_run(self) -> None:
        async def hook(run):
            approval.add_run_note(self.ctx.application, str(GROUP), "auto", "Write notes.md")
        self.run_hook = hook

    async def test_note_reaches_footer(self) -> None:
        self._note_in_run()
        seen: list[list[str]] = []

        async def footer(*_a, notes=(), **_kw) -> bool:
            seen.append(list(notes))
            return True
        self.send_cost_footer.side_effect = footer
        msg = await self.dispatch("сделай")
        notes = seen[0]
        self.assertTrue(any("notes.md" in n for n in notes), notes)
        msg.reply_text.assert_not_awaited()
        self.assertNotIn(str(GROUP), self.bot_data.get("run_notes", {}))

    async def test_note_survives_quiet_stop(self) -> None:
        async def hook(run):
            approval.add_run_note(self.ctx.application, str(GROUP), "auto", "Write notes.md")
            bot._mark_user_stop(self.ctx, str(GROUP))
        self.run_hook = hook
        self.result_error = True
        msg = await self.dispatch("сделай")
        self.send_cost_footer.assert_not_awaited()
        msg.reply_text.assert_awaited_once()
        self.assertIn("notes.md", msg.reply_text.await_args.args[0])
        self.assertIs(msg.reply_text.await_args.kwargs.get("disable_notification"), True)


class TurnServiceNotesTests(_Base):
    """Ещё служебные сообщения хода строкой футера (ux.md 12, 15)."""

    def _capture_footer(self) -> list[list[str]]:
        seen: list[list[str]] = []

        async def footer(*_a, notes=(), **_kw) -> bool:
            seen.append(list(notes))
            return True
        self.send_cost_footer.side_effect = footer
        return seen

    async def test_idle_new_session_note_in_footer(self) -> None:
        self.cfg.session_idle_ttl_sec = 60
        self.store.set(str(GROUP), "old-sid")
        self.store.data[str(GROUP)]["ts"] = time.time() - 3600
        seen = self._capture_footer()
        msg = await self.dispatch("привет")
        self.assertIsNone(self.runs[0]["resumed"])
        msg.reply_text.assert_not_awaited()
        self.assertTrue(any(n.startswith("🆕") for n in seen[0]), seen)

    async def test_session_lost_note_in_footer(self) -> None:
        seen = self._capture_footer()

        async def lost_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            return ClaudeResult(text="ok", session_lost=True)
        with patch.object(bot, "run_claude", lost_run):
            msg = await self.dispatch("привет")
        msg.reply_text.assert_not_awaited()
        self.assertTrue(any("session_id" in n for n in seen[0]), seen)


class AbandonedCardInPlaceTests(unittest.TestCase):
    """Карточка, снятая концом рана, получает пометку правкой себя же, без
    нового сообщения (как решение по нажатию)."""

    def _cleanup(self, *, card_html: str, reason: str = "deny:claude-exited",
                 edit_fails: bool = False):
        async def go():
            registry = approval.ApprovalRegistry()
            p = registry.create("Bash", {"command": "rm x"}, session_id="sid",
                                target_chat_id=CHAT, target_thread_id=THREAD)
            p.telegram_message_id = 10
            p.card_html = card_html
            tg = _CardBot(edit_fails=edit_fails)
            ctx = SimpleNamespace(bot=tg, application=SimpleNamespace(
                bot_data={"cfg": SimpleNamespace(primary_chat_id=1), "registry": registry}))
            n = await bot._cleanup_abandoned_approvals(ctx, "sid", reason, "🚫 снята")
            return n, tg
        return asyncio.run(go())

    def test_card_edited_no_message(self) -> None:
        n, tg = self._cleanup(card_html="🔴 <b>rm</b>")
        self.assertEqual(n, 1)
        self.assertEqual(tg.sent, [])
        self.assertEqual(tg.edits[0]["text"], "🔴 <b>rm</b>\n\n🚫 снята")
        self.assertIsNone(tg.edits[0].get("reply_markup"))

    def test_cancel_marks_card_too(self) -> None:
        n, tg = self._cleanup(card_html="🔴 <b>rm</b>", reason="deny:cancelled-by-user")
        self.assertEqual(tg.sent, [])
        self.assertEqual(len(tg.edits), 1)

    def test_edit_failure_falls_back_to_quiet_reply(self) -> None:
        n, tg = self._cleanup(card_html="🔴 <b>rm</b>", edit_fails=True)
        self.assertEqual(len(tg.markups), 1)
        self.assertEqual(len(tg.sent), 1)
        self.assertIs(tg.sent[0].get("disable_notification"), True)


if __name__ == "__main__":
    unittest.main()

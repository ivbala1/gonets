"""Тесты chat_digest: ключи Telethon, список чатов, формат ленты, разрезание
сводки, и главное: отметка прочитанным только после полной доставки."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.i18n import _  # noqa: E402
from claude_tg_bot.skills.chat_digest import __main__ as skill  # noqa: E402
from claude_tg_bot.skills.chat_digest.core import (  # noqa: E402
    PLAIN_TOPIC_ID,
    ChatUnread,
    Message,
    TopicUnread,
    TelegramCreds,
    build_prompt,
    fit_budget,
    format_topic,
    header_line,
    load_telegram_creds,
    match_dialogs,
    parse_chat_ids,
    parse_chat_spec,
    split_sections,
    validate_digest,
)


def _msg(i: int, text: str = "привет", **kw) -> Message:
    return Message(id=i, date=f"2026-09-{10 + i % 3:02d}T10:00+00:00", sender="Маша", text=text, **kw)


def _digest(*blocks: str) -> list[str]:
    """Блоки сводки, как их пишет модель на языке бота: «Лично тебе», затем темы."""
    return [f"{_('Лично тебе')}: ничего"] + [f"{_('Тема')}: {b}" for b in blocks]


def _chat(*, forum=True) -> ChatUnread:
    topic = TopicUnread(topic_id=31566 if forum else PLAIN_TOPIC_ID, title="Девочки" if forum else "Обсуждение",
                        unread_count=2, read_inbox_max_id=100, messages=[_msg(101), _msg(102, "пока")])
    return ChatUnread(chat_id=-100, title="Наши люди", forum=forum, topics=[topic])


class CredsTest(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(RuntimeError):
            load_telegram_creds(Path("/nonexistent/.env"))

    def test_personal_label_preferred(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            self.addCleanup(Path(f.name).unlink)
            f.write("TELEGRAM_API_ID=123\nTELEGRAM_API_HASH=abc\n"
                    "TELEGRAM_SESSION_STRING=old\nTELEGRAM_SESSION_STRING_PERSONAL=new\n")
        creds = load_telegram_creds(Path(f.name))
        self.assertEqual((creds.api_id, creds.api_hash, creds.session), (123, "abc", "new"))

    def test_repr_hides_secrets(self):
        text = repr(TelegramCreds(api_id=1, api_hash="HASH-SECRET", session="SESSION-SECRET"))
        self.assertNotIn("SECRET", text)

    def test_missing_keys_named(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            self.addCleanup(Path(f.name).unlink)
            f.write("TELEGRAM_API_ID=123\n")
        with self.assertRaises(RuntimeError) as cm:
            load_telegram_creds(Path(f.name))
        self.assertIn("TELEGRAM_API_HASH", str(cm.exception))


class ParseTest(unittest.TestCase):
    def test_list_dedup_and_order(self):
        self.assertEqual(parse_chat_ids(" -1001, -1002;-1001,,"), [-1001, -1002])

    def test_garbage_is_error(self):
        with self.assertRaises(RuntimeError):
            parse_chat_ids("-1001,наши люди")


class ChatSpecTest(unittest.TestCase):
    def test_spec_int_or_name(self):
        self.assertEqual(parse_chat_spec(" -1001 "), -1001)
        self.assertEqual(parse_chat_spec("Наши люди"), "Наши люди")
        with self.assertRaises(RuntimeError):
            parse_chat_spec("  ")

    def test_exact_title_beats_substring(self):
        dialogs = [(-1, "Наши люди"), (-2, "Наши люди (архив)")]
        self.assertEqual(match_dialogs("наши люди", dialogs), (-1, "Наши люди"))

    def test_unique_substring(self):
        dialogs = [(-1, "Наши люди"), (-2, "Компания друзей в Берлине")]
        self.assertEqual(match_dialogs("компания", dialogs), (-2, "Компания друзей в Берлине"))

    def test_ambiguous_lists_candidates(self):
        dialogs = [(-1, "Барахолка Берлин"), (-2, "Берлин барахолка")]
        with self.assertRaises(RuntimeError) as cm:
            match_dialogs("барахолка", dialogs)
        self.assertIn("(-1)", str(cm.exception))
        self.assertIn("(-2)", str(cm.exception))

    def test_not_found(self):
        with self.assertRaises(RuntimeError):
            match_dialogs("нет такого", [(-1, "Наши люди")])


class FormatTest(unittest.TestCase):
    def test_format_topic_lines(self):
        topic = _chat().topics[0]
        topic.messages.append(_msg(103, "фото", media="Photo", reply_to=101, forwarded_from="Канал"))
        text = format_topic(topic)
        self.assertIn(_("# Тема «{title}», непрочитанных {count}").format(title="Девочки", count=3), text)
        self.assertIn("103 2026-09-11T10:00+00:00 Маша: [Photo] "
                      + _("[пересылка: {sender}]").format(sender="Канал") + " "
                      + _("(ответ на {id})").format(id=101) + " фото", text)

    def test_reply_to_topic_root_is_not_marked(self):
        topic = _chat().topics[0]
        topic.messages.append(_msg(104, "в корень", reply_to=31566))
        self.assertNotIn(_("(ответ на {id})").format(id=31566), format_topic(topic))

    def test_prompt_and_header(self):
        chat = _chat()
        prompt = build_prompt(chat, "Мария Иванова (@mivanova)")
        self.assertIn("@mivanova", prompt)
        self.assertIn(_(", форум с темами"), prompt)
        self.assertIn("-----", prompt)
        self.assertEqual(header_line(chat), _("Сводка «{title}», {period}: {total} непрочитанных. Темы: {topics}").format(
            title="Наши люди", period="2026-09-10 … 2026-09-12", total=2, topics="Девочки (2)"))

    def test_split_sections(self):
        digest = "Лично тебе: ничего\n-----\nТема: Девочки\nтекст\n\n------\n\nТема: Юмор\nмемы\n"
        self.assertEqual(split_sections(digest), ["Лично тебе: ничего", "Тема: Девочки\nтекст", "Тема: Юмор\nмемы"])

    def test_max_id_and_totals(self):
        chat = _chat()
        self.assertEqual(chat.topics[0].max_id, 102)
        self.assertEqual(chat.total, 2)

    def test_long_text_is_not_truncated(self):
        topic = _chat().topics[0]
        topic.messages.append(_msg(103, "x" * 5000))
        self.assertIn("x" * 5000, format_topic(topic))


class BudgetTest(unittest.TestCase):
    def test_budget_cuts_tail_and_later_topics(self):
        chat = _chat()
        second = TopicUnread(topic_id=2, title="Юмор", unread_count=1, read_inbox_max_id=0,
                             messages=[_msg(201, "мем")])
        chat.topics.append(second)
        line = len("101 2026-09-12T10:00+00:00 Маша: привет") + 1
        dropped = fit_budget(chat, line + 5)
        self.assertEqual(dropped, 2)
        self.assertEqual([t.title for t in chat.topics], ["Девочки"])
        self.assertEqual([m.id for m in chat.topics[0].messages], [101])
        self.assertEqual(chat.topics[0].max_id, 101)

    def test_budget_keeps_at_least_one_message(self):
        chat = _chat()
        chat.topics[0].messages = [_msg(101, "x" * 1000)]
        self.assertEqual(fit_budget(chat, 10), 0)
        self.assertEqual(chat.total, 1)

    def test_budget_untouched_when_it_fits(self):
        chat = _chat()
        self.assertEqual(fit_budget(chat, 10_000), 0)
        self.assertEqual(chat.total, 2)


class ValidateTest(unittest.TestCase):
    def test_complete(self):
        chat = _chat()
        self.assertEqual(validate_digest(_digest("девочки\nтекст"), chat), [])

    def test_missing_topic_and_personal_block(self):
        chat = _chat()
        problems = validate_digest(["Не могу выполнить запрос"], chat)
        self.assertEqual(len(problems), 2)
        self.assertIn("Девочки", problems[1])

    def test_plain_chat_accepts_any_story_blocks(self):
        chat = _chat(forum=False)
        self.assertEqual(validate_digest(_digest("Выборы\n..."), chat), [])
        self.assertEqual(len(validate_digest(_digest(), chat)), 1)

    def test_title_with_emoji_matches_trimmed_heading(self):
        chat = _chat()
        chat.topics[0].title = "Здоровье и спорт 💉🤸"
        self.assertEqual(validate_digest(_digest("Здоровье и спорт\n..."), chat), [])


class MainFlowTest(unittest.TestCase):
    """Отметка только после полной доставки; dry-run и --no-mark без побочек."""

    @property
    def DIGEST(self) -> str:
        return "\n-----\n".join(_digest("Девочки\nвсё хорошо"))

    def _run(self, argv, *, send_results, chats=None, digest=None, mark_problems=None):
        env = {
            "CHAT_DIGEST_SOURCE_CHAT_IDS": "-100",
            "CHAT_DIGEST_TG_CHAT_ID": "-200",
            "CHAT_DIGEST_TG_THREAD_ID": "42",
        }
        calls = {"sent": [], "sent_args": [], "marked": [], "digests": 0}

        def fake_send(args):
            calls["sent"].append(args[-1])
            calls["sent_args"].append(args)
            return (send_results.pop(0) if send_results else 0), 1

        async def fake_collect(creds, specs, limit):
            calls["specs"] = specs
            return "Мария (@mivanova)", chats if chats is not None else [_chat()]

        async def fake_mark(creds, chat):
            calls["marked"].append({chat.chat_id: {t.topic_id: t.max_id for t in chat.unread}})
            return list(mark_problems or [])

        def fake_digest(prompt, timeout):
            calls["digests"] += 1
            result = digest(prompt) if callable(digest) else (digest or self.DIGEST)
            if isinstance(result, Exception):
                raise result
            return result

        class Lock:
            def close(self):
                pass

        with patch.dict("os.environ", env, clear=False), \
             patch.object(skill, "load_dotenv", lambda *_: None), \
             patch.object(skill, "load_telegram_creds", lambda p: object()), \
             patch.object(skill, "_lock", lambda: Lock()), \
             patch.object(skill, "collect", fake_collect), \
             patch.object(skill, "mark_read", fake_mark), \
             patch.object(skill, "run_digest_claude", fake_digest), \
             patch.object(skill, "send_message", fake_send):
            rc = skill.main(argv)
        return rc, calls

    def test_full_delivery_then_mark(self):
        rc, calls = self._run([], send_results=[0, 0, 0])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["sent"]), 3)
        self.assertTrue(calls["sent"][0].startswith(header_line(_chat())))
        self.assertEqual(calls["marked"], [{-100: {31566: 102}}])

    def test_text_is_separated_from_flags(self):
        _, calls = self._run([], send_results=[0, 0, 0])
        self.assertEqual(calls["sent_args"][0][-2], "--")

    def test_incomplete_digest_is_not_posted(self):
        rc, calls = self._run([], send_results=[], digest="Не могу выполнить запрос")
        self.assertEqual(rc, 1)
        self.assertEqual(calls["sent"], [])
        self.assertEqual(calls["marked"], [])

    def test_failed_chat_does_not_block_others(self):
        other = _chat()
        other.chat_id, other.title = -101, "Второй"

        def digest(prompt):
            if "Второй" in prompt:
                return RuntimeError("claude не уложился в 480 с")
            return self.DIGEST

        rc, calls = self._run([], send_results=[0, 0, 0], chats=[_chat(), other], digest=digest)
        self.assertEqual(rc, 1)
        self.assertEqual(calls["marked"], [{-100: {31566: 102}}])
        self.assertEqual(calls["digests"], 2)

    def test_mark_problem_reported_as_failure(self):
        rc, calls = self._run([], send_results=[0, 0, 0], mark_problems=["Девочки: FloodWaitError"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls["sent"]), 3)

    def test_second_run_is_skipped_by_lock(self):
        with patch.object(skill, "_lock", lambda: None), \
             patch.object(skill, "load_dotenv", lambda *_: None), \
             patch.dict("os.environ", {"CHAT_DIGEST_TG_CHAT_ID": "-200", "CHAT_DIGEST_TG_THREAD_ID": "42"}):
            self.assertEqual(skill.main([]), 0)


    def test_failed_delivery_no_mark(self):
        rc, calls = self._run([], send_results=[0, 1])
        self.assertEqual(rc, 1)
        self.assertEqual(calls["marked"], [])

    def test_dry_run_no_side_effects(self):
        rc, calls = self._run(["--dry-run"], send_results=[])
        self.assertEqual(rc, 0)
        self.assertEqual(calls["sent"], [])
        self.assertEqual(calls["marked"], [])

    def test_no_mark_flag(self):
        rc, calls = self._run(["--no-mark"], send_results=[0, 0, 0])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls["sent"]), 3)
        self.assertEqual(calls["marked"], [])

    def test_nothing_unread_is_quiet_success(self):
        empty = ChatUnread(chat_id=-100, title="Наши люди", forum=True, topics=[])
        rc, calls = self._run([], send_results=[], chats=[empty])
        self.assertEqual(rc, 0)
        self.assertEqual(calls["sent"], [])

    def test_chat_flag_by_name_bypasses_env_list(self):
        rc, calls = self._run(["--chat", "Наши люди", "--chat", "-777"], send_results=[0, 0, 0])
        self.assertEqual(rc, 0)
        self.assertEqual(calls["specs"], ["Наши люди", -777])

    def test_env_list_used_without_flag(self):
        rc, calls = self._run([], send_results=[0, 0, 0])
        self.assertEqual(rc, 0)
        self.assertEqual(calls["specs"], [-100])


class ClaudeRunTest(unittest.TestCase):
    def test_timeout_becomes_runtime_error(self):
        import subprocess

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        with patch.object(skill, "run_claude_oneshot", boom), patch.object(skill, "resolve_claude_bin", lambda: "claude"):
            with self.assertRaises(RuntimeError) as cm:
                skill.run_digest_claude("x", 1)
        self.assertEqual(str(cm.exception), _("claude не уложился в {seconds} с").format(seconds=1))

    def test_child_output_is_not_echoed_on_failure(self):
        from subprocess import CompletedProcess

        def failing(*a, **kw):
            return CompletedProcess(a, 1, b"", b"very-private-detail 429 rate limit")

        with patch.object(skill, "run_claude_oneshot", failing), patch.object(skill, "resolve_claude_bin", lambda: "claude"):
            with self.assertRaises(RuntimeError) as cm:
                skill.run_digest_claude("x", 1)
        self.assertNotIn("very-private-detail", str(cm.exception))
        self.assertEqual(str(cm.exception), _("claude exit {rc}: {kind}").format(rc=1, kind=_("лимит или доступ")))


if __name__ == "__main__":
    unittest.main()

"""Карточка отправки 08.11.2026: новый текст правки виден текстом, у ответа
Gmail без `to` назван адресат, не-отправки (набор текста, черновик Slack,
чтения с send/schedule в имени) идут без карточки и не получают отказ в фоне.
Имена и поля инструментов сверены с telegram-mcp (tools/messages.py) и
схемой коннектора Gmail 08.11.2026."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot import approval, outbound, serious  # noqa: E402
from claude_tg_bot.i18n import _  # noqa: E402

CLIP = _("⚠️ <b>текст длиннее карточки: прочитай целиком по 📄 до ✅</b>")


def card(tool: str, inp: dict) -> str:
    return approval._format_tool_body(tool, inp, None)


class EditMessageCardTests(unittest.TestCase):
    def test_new_text_shown_as_text_not_json(self) -> None:
        body = card("mcp__telegram__edit_message",
                    {"chat_id": "@x", "message_id": 5, "new_text": "Новый <текст>"})
        self.assertIn(_("новый текст") + ":\n<pre>Новый &lt;текст&gt;</pre>", body)
        self.assertNotIn(_("<i>прочее:</i>"), body)
        self.assertNotIn(_("<i>текста нет</i>"), body)

    def test_edit_is_not_called_reply(self) -> None:
        body = card("mcp__telegram__edit_message",
                    {"chat_id": "@x", "message_id": 5, "new_text": "t"})
        self.assertIn(_("правка сообщения") + ": <code>5</code>", body)
        self.assertNotIn(_("ответ на сообщение"), body)

    def test_long_new_text_warns(self) -> None:
        body = card("mcp__telegram__edit_message",
                    {"chat_id": "@x", "message_id": 5, "new_text": "a" * 3000})
        self.assertIn(CLIP, body)

    def test_full_view_has_new_text(self) -> None:
        text = approval.full_view_text("mcp__telegram__edit_message",
                                       {"chat_id": "@x", "message_id": 5, "new_text": "весь текст"})
        self.assertIn(_("--- {label} ---\n{value}").format(label=_("новый текст"), value="весь текст"),
                      text)

    def test_send_message_reply_to_named(self) -> None:
        # reply_to у telegram-mcp ещё и номер темы форума.
        body = card("mcp__telegram__send_message", {"chat_id": "@x", "message": "t", "reply_to": 9})
        self.assertIn(_("ответ на сообщение или тема") + ": <code>9</code>", body)

    def test_reaction_named(self) -> None:
        body = card("mcp__telegram__send_reaction", {"chat_id": "@x", "message_id": 4, "emoji": "👍"})
        self.assertIn(_("реакция на сообщение") + ": <code>4</code>", body)
        self.assertIn(_("реакция") + ": 👍", body)
        self.assertNotIn(_("ответ на сообщение"), body)
        self.assertNotIn(_("<i>текста нет</i>"), body)
        self.assertNotIn(_("<i>прочее:</i>"), body)

    def test_vote_poll_is_card_with_choice(self) -> None:
        self.assertTrue(outbound.is_send("mcp__telegram__vote_poll"))
        body = card("mcp__telegram__vote_poll", {"chat_id": "@team", "message_id": 42, "option_indices": [1, 3]})
        self.assertIn("<b>кому:</b> <code>@team</code>", body)
        self.assertIn(_("опрос в сообщении") + ": <code>42</code>", body)
        self.assertIn(_("голос за варианты по порядку создания опроса (если варианты не "
                        "перемешаны, это порядок сверху): <code>{n}</code>, заменит прежний голос, если был").format(
            n="2, 4"), body)
        self.assertNotIn(_("<i>текста нет</i>"), body)
        self.assertNotIn(_("<i>прочее:</i>"), body)
        as_json = card("mcp__telegram__vote_poll", {"chat_id": "@team", "message_id": 42, "option_indices": "[1, 3]"})
        self.assertIn("<code>2, 4</code>", as_json)
        for odd, shown in (("1,3", "1,3"), ([-1], "-1"), ([True], "True"), ([0, 0], "0, 0"), ("[true]", "True")):
            with self.subTest(odd=odd):
                raw = card("mcp__telegram__vote_poll", {"chat_id": "@team", "message_id": 42, "option_indices": odd})
                self.assertIn(_("голос, номера как переданы (сервер такие отклонит): <code>{n}</code>").format(
                    n=shown), raw)

    def test_vote_in_saved_messages_is_not_self(self) -> None:
        import asyncio
        from types import SimpleNamespace
        cfg = SimpleNamespace(allowed_user_ids={7}, telegram_mcp_unit=None)
        for chat in ("me", "self", "7"):
            for unattended, want in ((False, "card"), (True, "deny")):
                with self.subTest(chat=chat, unattended=unattended):
                    v = asyncio.run(outbound.decide(
                        "mcp__telegram__vote_poll", {"chat_id": chat, "message_id": 1, "option_indices": [0]},
                        cfg=cfg, unattended=unattended))
                    self.assertEqual(v.decision, want)
        v = asyncio.run(outbound.decide("mcp__telegram__send_message", {"chat_id": "me", "message": "x"},
                                        cfg=cfg, unattended=True))
        self.assertEqual(v.decision, "allow")

    def test_album_files_shown(self) -> None:
        body = card("mcp__telegram__send_album", {"chat_id": "@x", "file_paths": ["/a/1.jpg", "/a/2.jpg"]})
        self.assertIn("<code>/a/1.jpg</code>, <code>/a/2.jpg</code>", body)
        self.assertNotIn(_("<i>текста нет</i>"), body)

    def test_both_file_fields_and_recipient_fields_visible(self) -> None:
        body = card("mcp__telegram__send_album",
                    {"chat_id": "@x", "file_path": "/b.jpg", "file_paths": ["/a.jpg"]})
        self.assertIn("<code>/b.jpg</code>, <code>/a.jpg</code>", body)
        body = card("mcp__claude_ai_Slack__slack_send_message",
                    {"chat_id": "@x", "channel_id": "C1", "message": "t"})
        self.assertIn("<b>кому:</b> <code>@x</code>", body)
        self.assertIn("C1", body.split(_("<i>прочее:</i>"))[1])

    def test_empty_emoji_not_shown(self) -> None:
        body = card("mcp__telegram__send_reaction", {"chat_id": "@x", "message_id": 4, "emoji": []})
        self.assertNotIn(_("реакция") + ":", body)
        self.assertIn(_("<i>текста нет</i>"), body)

    def test_long_rest_warns(self) -> None:
        body = card("mcp__telegram__create_poll",
                    {"chat_id": "@x", "question": "q", "options": ["o" * 50] * 20})
        self.assertIn(CLIP, body)

    def test_long_recipients_warn(self) -> None:
        many = [f"user{i}@example.com" for i in range(40)]
        for inp in ({"to": many, "body": "b"}, {"to": ["a@b.c"], "bcc": many, "body": "b"}):
            with self.subTest(keys=sorted(inp)):
                body = card("mcp__claude_ai_Gmail__send_message", inp)
                self.assertIn(_(outbound.RECIPIENTS_CLIPPED), body)
        short = card("mcp__claude_ai_Gmail__send_message", {"to": ["a@b.c"], "body": "b"})
        self.assertNotIn(_(outbound.RECIPIENTS_CLIPPED), short)


class GmailReplyCardTests(unittest.TestCase):
    def test_reply_without_to_names_sender(self) -> None:
        body = card("mcp__claude_ai_Gmail__reply", {"messageId": "m1", "body": "ok"})
        self.assertIn(_("<b>кому:</b> как ответит Gmail на письмо <code>{id}</code>").format(id="m1"), body)
        self.assertNotIn(_("<i>прочее:</i>"), body)

    def test_reply_all_without_to(self) -> None:
        body = card("mcp__claude_ai_Gmail__reply", {"messageId": "m1", "body": "ok", "replyAll": True})
        self.assertIn(_("<b>кому:</b> всем адресатам письма <code>{id}</code>").format(id="m1"), body)
        self.assertNotIn(_("ответить всем") + ":", body)

    def test_reply_all_with_cc_not_called_all(self) -> None:
        # Заданная копия у reply заменяет прежнюю (схема коннектора).
        body = card("mcp__claude_ai_Gmail__reply",
                    {"messageId": "m1", "body": "ok", "replyAll": True, "cc": ["x@y.z"]})
        self.assertNotIn(_("всем адресатам письма"), body)
        self.assertIn(_("ответить всем") + ": <code>true</code>", body)
        self.assertIn("x@y.z", body)

    def test_reply_all_with_empty_cc(self) -> None:
        body = card("mcp__claude_ai_Gmail__reply",
                    {"messageId": "m1", "body": "ok", "replyAll": True, "cc": [], "to": []})
        self.assertNotIn(_("всем адресатам письма"), body)
        self.assertIn(_("заданы пустыми") + ": <code>to, cc</code>", body)

    def test_reply_all_with_empty_to_not_called_all(self) -> None:
        body = card("mcp__claude_ai_Gmail__reply", {"messageId": "m1", "body": "ok", "replyAll": True, "to": []})
        self.assertNotIn(_("всем адресатам письма"), body)

    def test_long_subject_and_odd_attachments(self) -> None:
        body = card("mcp__claude_ai_Gmail__send_message", {"to": ["a@b.c"], "subject": "s" * 400, "body": "b"})
        self.assertIn(CLIP, body)
        body = card("mcp__claude_ai_Gmail__send_message",
                    {"to": ["a@b.c"], "body": "b", "attachments": {"filename": "x.pdf"}})
        self.assertIn("x.pdf", body)
        body = card("mcp__claude_ai_Gmail__send_message",
                    {"to": ["a@b.c"], "body": "b", "attachments": ["/h/y.pdf"]})
        self.assertIn("<code>/h/y.pdf</code>", body)
        text = approval.full_view_text("mcp__claude_ai_Gmail__send_message",
                                       {"to": ["a@b.c"], "body": "b", "attachments": ["/h/y.pdf"]})
        self.assertIn("/h/y.pdf", text)

    def test_long_file_name_warns(self) -> None:
        body = card("mcp__telegram__send_album",
                    {"chat_id": "@x", "file_paths": ["/r/" + "a" * 205 + ".jpg"]})
        self.assertIn(CLIP, body)
        body = card("mcp__claude_ai_Gmail__send_message",
                    {"to": ["a@b.c"], "body": "b", "attachments": [{"filename": "f" * 250, "content": "QQ=="}]})
        self.assertIn(CLIP, body)

    def test_reply_all_not_true_shown_raw(self) -> None:
        body = card("mcp__claude_ai_Gmail__reply", {"messageId": "m1", "body": "ok", "replyAll": "false"})
        self.assertIn(_("<b>кому:</b> как ответит Gmail на письмо <code>{id}</code>").format(id="m1"), body)
        self.assertIn(_("ответить всем") + ': <code>"false"</code>', body)
        body = card("mcp__claude_ai_Gmail__reply", {"body": "ok", "replyAll": True})
        self.assertIn(_("ответить всем") + ": <code>true</code>", body)

    def test_reply_with_to_shows_list_plainly(self) -> None:
        body = card("mcp__claude_ai_Gmail__reply",
                    {"messageId": "m1", "body": "ok", "to": ["a@b.c", "d@e.f"]})
        self.assertIn("<b>кому:</b> <code>a@b.c, d@e.f</code>", body)
        self.assertIn(_("ответ на письмо") + ": <code>m1</code>", body)
        self.assertNotIn(_(outbound.RECIPIENTS_CLIPPED), body)

    def test_signature_named(self) -> None:
        body = card("mcp__claude_ai_Gmail__reply",
                    {"messageId": "m1", "body": "ok", "includeSignature": True})
        self.assertIn(_("подпись Gmail: да"), body)
        self.assertNotIn(_("<i>прочее:</i>"), body)


class NotASendTests(unittest.TestCase):
    NOT_SENDS = ("mcp__telegram__send_typing", "mcp__telegram__get_send_as",
                 "mcp__telegram__get_scheduled_messages", "mcp__telegram__delete_scheduled_message",
                 "mcp__telegram__wait_for_new_message",
                 "mcp__claude_ai_Slack__slack_send_message_draft",
                 "mcp__plugin_operations_slack__slack_send_message_draft")

    def test_not_send(self) -> None:
        for tool in self.NOT_SENDS:
            with self.subTest(tool=tool):
                self.assertFalse(outbound.is_send(tool))
                self.assertEqual(serious.send_reasons(tool, {}, None), set())

    def test_send_as_change_still_card(self) -> None:
        # Меняет, от чьего имени уйдут следующие сообщения: карточка остаётся.
        self.assertTrue(serious.sends(serious.send_reasons("mcp__telegram__set_default_send_as", {}, None)))

    def test_real_sends_still_cards(self) -> None:
        for tool in ("mcp__telegram__send_message", "mcp__telegram__edit_message",
                     "mcp__telegram__send_scheduled_message", "mcp__telegram__send_reaction",
                     "mcp__telegram__create_poll", "mcp__claude_ai_Gmail__send_draft",
                     "mcp__claude_ai_Slack__slack_send_message",
                     "mcp__plugin_engineering_slack__slack_send_message"):
            with self.subTest(tool=tool):
                self.assertTrue(outbound.is_send(tool))
                if not tool.endswith("create_poll"):  # опрос ловит только is_send
                    self.assertTrue(serious.sends(serious.send_reasons(tool, {}, None)))


if __name__ == "__main__":
    unittest.main()

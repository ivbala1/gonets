"""Кнопки через buttons (ревизия повторов 07.11.2026, раздел «Можно»): сборка
с проверкой 64 байт, снятие с журналом настоящих ошибок, строка итога в
карточке; преграда для ручной сборки и снятия кнопок вне модуля."""
from __future__ import annotations

import ast
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_tg_bot import buttons, notify, reactions  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ---- сборка ----

def test_keyboard_raw_json() -> None:
    rows = [[{"text": "a", "data": "x:1"}, {"text": "b", "url": "https://t.me/x"}], [{"text": "c", "data": "y"}]]
    assert buttons.keyboard(rows) == {"inline_keyboard": [
        [{"text": "a", "callback_data": "x:1"}, {"text": "b", "url": "https://t.me/x"}],
        [{"text": "c", "callback_data": "y"}]]}
    assert buttons.keyboard([]) == {"inline_keyboard": []}


def test_markup_ptb() -> None:
    kb = buttons.markup([[{"text": "a", "data": "x:1"}, {"text": "b", "url": "https://t.me/x"}]])
    assert isinstance(kb, InlineKeyboardMarkup)
    (a, b), = kb.inline_keyboard
    assert (a.text, a.callback_data, a.url) == ("a", "x:1", None)
    assert (b.text, b.callback_data, b.url) == ("b", None, "https://t.me/x")
    assert buttons.markup([]) is None


@pytest.mark.parametrize("data", ["", "x" * 65, "я" * 33])
def test_data_limit(data) -> None:
    with pytest.raises(ValueError):
        buttons.keyboard([[{"text": "a", "data": data}]])
    with pytest.raises(ValueError):
        buttons.markup([[{"text": "a", "data": data}]])


def test_data_limit_edge() -> None:
    assert buttons.keyboard([[{"text": "a", "data": "я" * 32}]])["inline_keyboard"][0][0]["callback_data"] == "я" * 32


@pytest.mark.parametrize("b", [{"data": "x"}, {"text": "a"}, {"text": 1, "data": "x"}, {"text": "a", "data": 5},
                               {"text": "a", "url": None}])
def test_bad_button(b) -> None:
    with pytest.raises((KeyError, TypeError)):
        buttons.keyboard([[b]])


def test_notify_rejects_long_data(capsys) -> None:
    rc, _mid = notify._run(["--buttons", '[[{"text": "a", "data": "' + "x" * 65 + '"}]]', "hi"])
    assert rc == 1
    assert "--buttons" in capsys.readouterr().err


# ---- снятие ----

def test_strip_query_message_bot() -> None:
    q = SimpleNamespace(edit_message_reply_markup=AsyncMock())
    assert run(buttons.strip_buttons(q))
    q.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    m = SimpleNamespace(edit_reply_markup=AsyncMock())
    assert run(buttons.strip_buttons(m, message=True))
    m.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock())
    assert run(buttons.strip_buttons(bot, 5, 7))
    bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=5, message_id=7, reply_markup=None)


def test_strip_not_modified_quiet(caplog) -> None:
    q = SimpleNamespace(edit_message_reply_markup=AsyncMock(side_effect=BadRequest(
        "Message is not modified: specified new message content and reply markup are exactly the same")))
    with caplog.at_level(logging.DEBUG, logger="claude_tg_bot.buttons"):
        assert run(buttons.strip_buttons(q)) is None
    assert not caplog.records


@pytest.mark.parametrize("err", [BadRequest("Chat not found"), TimedOut(), RuntimeError("boom")])
def test_strip_real_error_logged(caplog, err) -> None:
    q = SimpleNamespace(edit_message_reply_markup=AsyncMock(side_effect=err))
    with caplog.at_level(logging.WARNING, logger="claude_tg_bot.buttons"):
        assert run(buttons.strip_buttons(q)) is False
    assert any("strip buttons failed" in r.getMessage() for r in caplog.records)


def test_strip_cancel_passes() -> None:
    q = SimpleNamespace(edit_message_reply_markup=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        run(buttons.strip_buttons(q))


# ---- строка итога в карточке ----

def test_note_on_card_query() -> None:
    q = SimpleNamespace(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    assert run(buttons.note_on_card(q, "<b>card</b>", "✅", "t"))
    q.edit_message_text.assert_awaited_once_with("<b>card</b>\n\n✅", parse_mode="HTML", reply_markup=None)
    q.edit_message_reply_markup.assert_not_awaited()


def test_note_on_card_bot() -> None:
    bot = SimpleNamespace(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    assert run(buttons.note_on_card(bot, "card", "⌛", "t", 5, 7))
    bot.edit_message_text.assert_awaited_once_with("card\n\n⌛", chat_id=5, message_id=7,
                                                   parse_mode="HTML", reply_markup=None)


def test_note_on_card_too_long_strips() -> None:
    q = SimpleNamespace(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    # 2047 эмодзи вне BMP это 4094 единицы UTF-16: по len() влезло бы.
    assert not run(buttons.note_on_card(q, "😀" * 2047, "✅", "t"))
    q.edit_message_text.assert_not_awaited()
    q.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    assert not run(buttons.note_on_card(q, "", "✅", "t"))


def test_note_on_card_fail_strips() -> None:
    q = SimpleNamespace(edit_message_text=AsyncMock(side_effect=BadRequest("can't parse entities")),
                        edit_message_reply_markup=AsyncMock())
    assert not run(buttons.note_on_card(q, "card", "✅", "t"))
    q.edit_message_reply_markup.assert_awaited_once()


def test_note_on_card_unconfirmed_no_reply() -> None:
    err = TimedOut()
    err.__cause__ = httpx.ReadTimeout("slow")
    q = SimpleNamespace(edit_message_text=AsyncMock(side_effect=err), edit_message_reply_markup=AsyncMock())
    assert run(buttons.note_on_card(q, "card", "✅", "t"))
    q.edit_message_reply_markup.assert_not_awaited()


# ---- мелкие ошибки ----

def test_login_callback_without_colon() -> None:
    from claude_tg_bot import tg_login
    from claude_tg_bot import bot as bot_mod
    for data in (None, "login", "login:codex"):
        update = SimpleNamespace(callback_query=SimpleNamespace(data=data, answer=AsyncMock()))
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": object()}))
        with patch.object(bot_mod, "_is_allowed", return_value=True), \
                patch.object(bot_mod, "_start_login", AsyncMock()) as start:
            run(tg_login.on_login_callback(update, ctx))
        assert start.await_args.args[2] == ("codex" if data == "login:codex" else "")


def test_remember_sent(tmp_path) -> None:
    reactions.remember_sent({"ok": True, "result": {"message_id": 9, "chat": {"id": 42}}}, "привет", tmp_path)
    assert reactions.lookup(tmp_path, 42, 9) == "привет"
    for bad in (None, {"result": True}, {"result": {"message_id": 1}}, {"result": {"chat": {"id": -5}, "message_id": 2}}):
        reactions.remember_sent(bad, "x", tmp_path)
    assert reactions.lookup(tmp_path, -5, 2) == ""


def test_otklik_send_remembered(tmp_path, monkeypatch) -> None:
    from claude_tg_bot import otklik, tg_api
    cfg = SimpleNamespace(telegram_bot_token="t", state_dir=tmp_path)
    resp = SimpleNamespace(status_code=200, text="ok",
                           json=lambda: {"ok": True, "result": {"message_id": 3, "chat": {"id": 11}}})
    monkeypatch.setattr("claude_tg_bot.config.load_config", lambda **_k: cfg)
    with patch.object(tg_api, "post", return_value=resp):
        assert otklik._bot_api("sendMessage", {"chat_id": 11, "text": "отклик"})
        otklik._bot_api("editMessageReplyMarkup", {"chat_id": 11, "message_id": 3})
    assert reactions.lookup(tmp_path, 11, 3) == "отклик"


# ---- преграда ----

SKIP = {"src/claude_tg_bot/buttons.py"}


def _sources() -> list[Path]:
    files = [*(ROOT / "src" / "claude_tg_bot").rglob("*.py"), *(ROOT / "tools").rglob("*.py"),
             ROOT / "bot-down-notify.py"]
    return [p for p in files if str(p.relative_to(ROOT)) not in SKIP]


def _hand_buttons(tree: ast.AST) -> list[ast.AST]:
    bad: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if name in ("InlineKeyboardMarkup", "InlineKeyboardButton", "edit_message_reply_markup",
                        "edit_reply_markup"):
                bad.append(node)
            elif any(k.arg in ("inline_keyboard", "callback_data") for k in node.keywords):
                bad.append(node)  # dict(inline_keyboard=...) (ревью 08.11.2026)
        elif isinstance(node, ast.Dict):
            if any(isinstance(k, ast.Constant) and k.value in ("inline_keyboard", "callback_data")
                   for k in node.keys):
                bad.append(node)
    return bad


def test_no_hand_built_buttons() -> None:
    """Кнопки собираются `buttons.keyboard` / `buttons.markup` (проверка 64
    байт), снимаются `buttons.strip_buttons` (тихое «not modified», журнал
    остального), итог в карточку `buttons.note_on_card`."""
    found = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += [f"{path.relative_to(ROOT)}:{n.lineno}" for n in _hand_buttons(tree)]
    assert not found, "кнопки руками, звать buttons: " + ", ".join(found)


def test_barrier_catches() -> None:
    code = ("InlineKeyboardMarkup([[InlineKeyboardButton('a', callback_data='x')]])\n"
            "m = {'inline_keyboard': []}\nawait_q = q.edit_message_reply_markup(reply_markup=None)\n"
            "m2 = dict(inline_keyboard=[])\n")
    assert len(_hand_buttons(ast.parse(code))) == 5


def test_open_tasks_list_is_split(tmp_path) -> None:
    """Пять открытых задач с длинными заголовками: список кусками в пределе
    Telegram, а не одним сообщением, которое Telegram отвергнет."""
    from claude_tg_bot import tasks, tg_tasks
    from claude_tg_bot import bot as bot_mod
    opened = [{"id": f"t{i}", "status": "running", "chat_id": -100, "thread_id": 70 + i,
               "item": {"title": "заголовок " * 30}} for i in range(5)]
    msg = SimpleNamespace(chat_id=-100, message_id=1, reply_text=AsyncMock())
    query = SimpleNamespace(message=msg)
    cfg = SimpleNamespace(state_dir=tmp_path, telegram_text_limit=1000)

    class _Locked:
        def __enter__(self):
            return {}

        def __exit__(self, *a):
            return False
    with patch.object(tasks, "locked", return_value=_Locked()), \
            patch.object(tasks, "reserve", return_value=("full", None)), \
            patch.object(tasks, "taken", return_value=opened), \
            patch.object(bot_mod, "_answer_quietly", AsyncMock()):
        run(tg_tasks._task_start_admitted(query, cfg, MagicMock(), "d1", SimpleNamespace(n=1), None))
    chunks = [c.args[0] for c in msg.reply_text.await_args_list]
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)
    assert sum(c.count("заголовок заголовок") > 0 for c in chunks) == len(chunks)


def test_note_on_card_plain_text() -> None:
    q = SimpleNamespace(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    assert run(buttons.note_on_card(q, "a < b", "→ да", "t", parse_mode=None))
    q.edit_message_text.assert_awaited_once_with("a < b\n\n→ да", parse_mode=None, reply_markup=None)


def test_notify_rejects_url_button(capsys) -> None:
    """notify как раньше: только кнопки с данными."""
    rc, _mid = notify._run(["--buttons", '[[{"text": "a", "url": "https://x"}]]', "hi"])
    assert rc == 1 and "--buttons" in capsys.readouterr().err


def _approve_press(strip_effect):
    """Нажатие ✅ на карточку, которой в реестре уже нет (второе нажатие)."""
    from claude_tg_bot import approval
    from claude_tg_bot import bot as bot_mod
    registry = approval.ApprovalRegistry()
    cfg = SimpleNamespace(allowed_user_ids={7}, primary_chat_id=1)
    msg = SimpleNamespace(message_id=10, chat_id=1, is_topic_message=False, reply_text=AsyncMock(),
                          text="card", text_html="card", reply_markup=None)
    query = SimpleNamespace(data="appr:0123456789ab:allow", message=msg, answer=AsyncMock(),
                            edit_message_reply_markup=AsyncMock(side_effect=strip_effect),
                            edit_message_text=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                             effective_chat=SimpleNamespace(id=1))
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg, "registry": registry}), bot_data={})
    run(bot_mod.on_callback(update, ctx))
    return query, msg


def test_second_press_on_closed_card_is_quiet() -> None:
    query, msg = _approve_press(BadRequest("Message is not modified"))
    query.edit_message_reply_markup.assert_awaited_once()
    msg.reply_text.assert_not_awaited()


def test_press_on_closed_card_with_buttons_says_so() -> None:
    query, msg = _approve_press(None)
    msg.reply_text.assert_awaited_once()


def test_handoff_send_remembered(tmp_path, monkeypatch) -> None:
    from claude_tg_bot import handoff, tg_api
    cfg = SimpleNamespace(telegram_bot_token="t", state_dir=tmp_path)
    resp = SimpleNamespace(status_code=200, text="ok",
                           json=lambda: {"ok": True, "result": {"message_id": 4, "chat": {"id": 12}}})
    monkeypatch.setattr("claude_tg_bot.config.load_config", lambda **_k: cfg)
    with patch.object(tg_api, "post", return_value=resp):
        assert handoff._bot_api("sendMessage", {"chat_id": 12, "text": "экран"})
    assert reactions.lookup(tmp_path, 12, 4) == "экран"


def test_note_on_card_already_there() -> None:
    """Строка уже в карточке (повтор): успех, без снятия и отдельного ответа."""
    q = SimpleNamespace(edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")),
                        edit_message_reply_markup=AsyncMock())
    assert run(buttons.note_on_card(q, "card", "✅", "t"))
    q.edit_message_reply_markup.assert_not_awaited()


def test_gone_message_is_info(caplog) -> None:
    """Удалённое сообщение: в журнал уровнем info, кнопки не снимаются."""
    q = SimpleNamespace(edit_message_text=AsyncMock(side_effect=BadRequest("Message to edit not found")),
                        edit_message_reply_markup=AsyncMock(side_effect=BadRequest("Message to edit not found")))
    with caplog.at_level(logging.INFO, logger="claude_tg_bot.buttons"):
        assert not run(buttons.note_on_card(q, "card", "✅", "t"))
        assert run(buttons.strip_buttons(q)) is False
    q.edit_message_reply_markup.assert_awaited_once()
    assert caplog.records and all(r.levelno == logging.INFO for r in caplog.records)

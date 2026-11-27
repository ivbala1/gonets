"""Реакции, геометка, кнопка проверки Кодексом (решение Ивана 01.11.2026)."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import ReactionTypeEmoji

from claude_tg_bot import bot, inflight, reactions, tg_quick
from claude_tg_bot.i18n import _
from claude_tg_bot.overrides import PersistentDict
from claude_tg_bot.replies import send_text_to
from claude_tg_bot.claude_runner import ClaudeResult
from tests.test_review_bot_main import _Base


def _ctx(tmp_path):
    data = {
        "reaction_notes": PersistentDict(tmp_path / "notes.json"),
        "codex_checks": PersistentDict(tmp_path / "checks.json"),
        "procs": {},
    }
    app = MagicMock()
    app.bot_data = data
    ctx = MagicMock()
    ctx.application = app
    ctx.bot.set_message_reaction = AsyncMock()
    data["cfg"] = NS(state_dir=tmp_path, allowed_user_ids={7}, allowed_chat_ids=set(), codex_bin="codex",
                     codex_fallback_sandbox="read-only", telegram_text_limit=4000,
                     large_reply_as_file_threshold=100000)
    return ctx


def _reaction_update(old, new, chat_id=7, chat_type="private", user_id=7, mid=5):
    chat = NS(id=chat_id, type=chat_type)
    event = NS(chat=chat, message_id=mid, old_reaction=tuple(ReactionTypeEmoji(e) for e in old),
               new_reaction=tuple(ReactionTypeEmoji(e) for e in new))
    return NS(message_reaction=event, effective_user=NS(id=user_id), effective_chat=chat)


def test_sent_log_keeps_private_only_and_latest(tmp_path):
    reactions.remember(7, 5, "первый", tmp_path)
    reactions.remember(7, 5, "правка", tmp_path)
    reactions.remember(-100, 6, "группа", tmp_path)
    assert reactions.lookup(tmp_path, 7, 5) == "правка"
    assert reactions.lookup(tmp_path, -100, 6) == ""
    assert reactions.lookup(tmp_path, 7, 99) == ""


def test_sent_log_trim_keeps_tail(tmp_path):
    for i in range(30):
        reactions.remember(7, i, "ы" * 400, tmp_path)
    path = tmp_path / reactions.SENT_FILE
    with patch.object(reactions, "_MAX_BYTES", 2000):
        reactions._trim(path)
    assert path.stat().st_size <= 2000
    assert reactions.lookup(tmp_path, 7, 29)
    assert not reactions.lookup(tmp_path, 7, 0)
    assert all(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())


def test_reaction_makes_note_and_ack(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "Вызвать мастера", tmp_path)
    with patch.object(bot, "_dispatch_to_claude", AsyncMock()) as dispatch:
        asyncio.run(tg_quick.on_reaction(_reaction_update([], ["👎"]), ctx))
    dispatch.assert_not_awaited()
    assert tg_quick.reaction_notes(ctx, 7) == {"7:5": ("👎", "Вызвать мастера")}
    ctx.bot.set_message_reaction.assert_awaited_once_with(7, 5, tg_quick.REACTION_ACK)
    assert "👎 Вызвать мастера" in tg_quick.reaction_note_text(tg_quick.reaction_notes(ctx, 7))


def test_sent_log_keeps_head_and_tail_of_long_message(tmp_path):
    long = "Утро. " + "план " * 300 + "Поставить бассейн на среду?"
    reactions.remember(7, 5, long, tmp_path)
    kept = reactions.lookup(tmp_path, 7, 5)
    assert kept.startswith("Утро. ") and kept.endswith("Поставить бассейн на среду?")
    assert len(kept) == reactions.TEXT_LIMIT + len("\n[…]\n")
    short = "ы" * reactions.TEXT_LIMIT
    assert reactions.clip(short) == short


def _like(ctx, dispatch, update=None):
    """👍 на сообщение 5 лички 7 с подменённым запуском хода."""
    with patch.object(bot, "_dispatch_to_claude", dispatch):
        asyncio.run(tg_quick.on_reaction(update or _reaction_update([], ["👍"]), ctx))


def _acks(ctx):
    return [c.args for c in ctx.bot.set_message_reaction.await_args_list]


ACK_ON, ACK_OFF = (7, 5, tg_quick.REACTION_ACK), (7, 5, None)


def test_like_starts_turn_as_ok_on_quoted_message(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "Поставить бассейн на среду?", tmp_path)
    seen = {}

    async def dispatch(update, _ctx, prompt, **kwargs):
        seen.update(kwargs, prompt=prompt, chat=update.message.chat.id, thread=update.message.message_thread_id,
                    wanted=kwargs["still_wanted"]())
        kwargs["on_run_start"]()

    _like(ctx, dispatch)
    assert seen["chat"] == 7 and seen["thread"] is None
    assert seen["prompt"].startswith(_(tg_quick.REACTION_OK).split("{quote}")[0])
    assert seen["prompt"].endswith("```\nПоставить бассейн на среду?\n```")
    # Признаки как у кнопки отклика: текст хода это данные, ждёт молча.
    assert seen["from_callback"] is True and seen["send_voice_reply"] is False
    assert set(seen) - {"prompt", "chat", "thread", "wanted"} == {
        "from_callback", "send_voice_reply", "still_wanted", "on_run_start", "prestart_sink", "inflight_ref"}
    assert seen["wanted"] is True
    assert seen["inflight_ref"] == {"message_id": 5, "prompt": "👍 Поставить бассейн на среду?", "kind": "reaction"}
    assert not ctx.application.bot_data["reaction_notes"] and not ctx.application.bot_data["reaction_likes"]
    assert _acks(ctx) == [ACK_ON]


def test_like_taken_off_before_start_cancels_turn(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "Поставить бассейн на среду?", tmp_path)
    seen = []

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        # Тема занята: пока ход ждёт замка, Иван снял 👍.
        await tg_quick.on_reaction(_reaction_update(["👍"], []), ctx)
        seen.append(kwargs["still_wanted"]())

    _like(ctx, dispatch)
    assert seen == [False]
    # 👌 снята самим снятием 👍, конец отменённого хода её больше не трогает.
    assert _acks(ctx) == [ACK_ON, ACK_OFF]
    assert not ctx.application.bot_data["reaction_likes"]


def test_like_taken_off_after_start_keeps_ack(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "Поставить бассейн на среду?", tmp_path)

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        assert kwargs["still_wanted"]()
        kwargs["on_run_start"]()
        await tg_quick.on_reaction(_reaction_update(["👍"], []), ctx)

    _like(ctx, dispatch)
    assert _acks(ctx) == [ACK_ON]


def test_like_set_again_runs_its_own_turn_only(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "Поставить бассейн на среду?", tmp_path)
    wanted = []

    async def second(_update, _ctx, _prompt, **kwargs):
        wanted.append(("second", kwargs["still_wanted"]()))
        kwargs["on_run_start"]()

    async def first(_update, _ctx, _prompt, **kwargs):
        await tg_quick.on_reaction(_reaction_update(["👍"], []), ctx)
        with patch.object(bot, "_dispatch_to_claude", second):
            await tg_quick.on_reaction(_reaction_update([], ["👍"]), ctx)
        wanted.append(("first", kwargs["still_wanted"]()))

    _like(ctx, first)
    assert wanted == [("second", True), ("first", False)]
    # Отменённый первый ход не снимает 👌 второго.
    assert _acks(ctx) == [ACK_ON, ACK_OFF, ACK_ON]


def test_like_over_dislike_drops_its_note_and_dislike_over_like_cancels(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    asyncio.run(tg_quick.on_reaction(_reaction_update([], ["👎"]), ctx))
    seen = []

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        assert tg_quick.reaction_notes(ctx, 7) == {}
        await tg_quick.on_reaction(_reaction_update(["👍"], ["👎"]), ctx)
        seen.append(kwargs["still_wanted"]())

    _like(ctx, dispatch, _reaction_update(["👎"], ["👍"]))
    assert seen == [False]
    assert tg_quick.reaction_notes(ctx, 7) == {"7:5": ("👎", "текст")}
    assert _acks(ctx)[-1] == ACK_ON


def test_like_and_dislike_in_one_event_are_both_taken(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    seen = []

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        # Заметка 👎 записана до хода 👍: он же её и увезёт.
        seen.append(tg_quick.reaction_notes(ctx, 7))

    _like(ctx, dispatch, _reaction_update([], ["👎", "👍"]))
    assert seen == [{"7:5": ("👎", "текст")}]


def test_like_taken_off_keeps_ack_of_dislike_whose_note_is_gone(tmp_path):
    """👍 и 👎 на сообщении, заметку 👎 уже увёз прошлый ход: снятый 👍
    отменяет свой ход, а 👌 остаётся за стоящей 👎."""
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    seen = []

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        await tg_quick.on_reaction(_reaction_update(["👍", "👎"], ["👎"]), ctx)
        seen.append(kwargs["still_wanted"]())

    _like(ctx, dispatch, _reaction_update(["👎"], ["👍", "👎"]))
    assert seen == [False]
    assert _acks(ctx) == [ACK_ON]


def test_old_like_note_from_file_goes_with_its_like(tmp_path):
    """Заметка 👍, записанная до 12.11.2026: снятый 👍 убирает её и 👌."""
    ctx = _ctx(tmp_path)
    notes = ctx.application.bot_data["reaction_notes"]
    notes["7:5"] = json.dumps({"emoji": "👍", "text": "Вызвать мастера"}, ensure_ascii=False)
    assert "- 👍 Вызвать мастера" in tg_quick.reaction_note_text(tg_quick.reaction_notes(ctx, 7))
    asyncio.run(tg_quick.on_reaction(_reaction_update(["👍"], []), ctx))
    assert tg_quick.reaction_notes(ctx, 7) == {}
    assert _acks(ctx) == [ACK_OFF]


def test_dislike_changed_to_like_on_message_out_of_log_takes_ack_off(tmp_path):
    """Сообщение выпало из журнала отправок, заметка 👎 на нём жива: смена на
    👍 заметку снимает, хода нет (текста нет), 👌 не остаётся."""
    ctx = _ctx(tmp_path)
    notes = ctx.application.bot_data["reaction_notes"]
    notes["7:99"] = json.dumps({"emoji": "👎", "text": "старое"}, ensure_ascii=False)
    dispatch = AsyncMock()
    _like(ctx, dispatch, _reaction_update(["👎"], ["👍"], mid=99))
    dispatch.assert_not_awaited()
    assert tg_quick.reaction_notes(ctx, 7) == {}
    assert [c.args for c in ctx.bot.set_message_reaction.await_args_list] == [(7, 99, None)]


def test_like_taken_off_next_to_dislike_keeps_its_note_and_ack(tmp_path):
    """Две реакции на одном сообщении: сняли 👍, пока его ход ждал; заметка
    👎 и 👌 остаются за 👎."""
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    seen = []

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        await tg_quick.on_reaction(_reaction_update(["👍"], ["👍", "👎"]), ctx)
        await tg_quick.on_reaction(_reaction_update(["👍", "👎"], ["👎"]), ctx)
        seen.append(kwargs["still_wanted"]())

    _like(ctx, dispatch)
    assert seen == [False]
    assert tg_quick.reaction_notes(ctx, 7) == {"7:5": ("👎", "текст")}
    assert ACK_OFF not in _acks(ctx)


def test_like_refused_by_restart_takes_ack_off(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    _like(ctx, AsyncMock(return_value=True))
    assert _acks(ctx) == [ACK_ON, ACK_OFF]
    assert not ctx.application.bot_data["reaction_likes"]


def test_like_held_before_start_takes_ack_off(tmp_path):
    """Лимит подписки или вход: проверка «ещё нужно» прошла, а ран не начался."""
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        assert kwargs["still_wanted"]()

    _like(ctx, dispatch)
    assert _acks(ctx) == [ACK_ON, ACK_OFF]


def _prestart(ctx, kwargs, update):
    """Как ран в bot.py: завести отметку «до старта» и отдать её зовущему."""
    mark = bot.RunPrestart()
    ctx.application.bot_data.setdefault("run_prestart", {})[bot._chat_key(update)[0]] = mark
    kwargs["prestart_sink"].append(mark)
    return mark


def test_like_taken_off_before_process_cancels_own_run_like_stop(tmp_path):
    """Ход взял тему, процесс рана ещё не создан (освежение входа): снятый 👍
    гасит свой ран до промпта готовой отменой «стоп», 👌 снимается один раз."""
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    marks = []

    async def dispatch(update, _ctx, _prompt, **kwargs):
        assert kwargs["still_wanted"]()
        marks.append(_prestart(ctx, kwargs, update))
        with patch.object(bot, "_mark_user_stop", MagicMock()) as stop:
            await tg_quick.on_reaction(_reaction_update(["👍"], []), ctx)
        stop.assert_called_once_with(ctx, "7")
        kwargs["on_run_start"]()  # процесс создан, обёртка рана гасит его до промпта

    _like(ctx, dispatch)
    assert marks[0].cancelled
    assert _acks(ctx) == [ACK_ON, ACK_OFF]
    assert not ctx.application.bot_data["reaction_likes"]


def test_like_taken_off_never_cancels_next_run_of_the_chat(tmp_path):
    """Ход 👍 не создал процесса и отправляет ответ, тему уже взял следующий
    ран: снятый 👍 его не гасит и ответ не глушит."""
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    marks = []

    async def dispatch(update, _ctx, _prompt, **kwargs):
        assert kwargs["still_wanted"]()
        marks.append(_prestart(ctx, kwargs, update))
        marks.append(bot.RunPrestart())
        ctx.application.bot_data["run_prestart"]["7"] = marks[1]  # следующий ран темы
        with patch.object(bot, "_mark_user_stop", MagicMock()) as stop:
            await tg_quick.on_reaction(_reaction_update(["👍"], []), ctx)
        stop.assert_not_called()

    _like(ctx, dispatch)
    assert not marks[0].cancelled and not marks[1].cancelled
    # Ран не начался: 👌 снимает конец хода, один раз.
    assert _acks(ctx) == [ACK_ON, ACK_OFF]
    assert not ctx.application.bot_data["reaction_likes"]


def test_stop_word_before_process_takes_ack_off(tmp_path):
    """«Стоп» гасит ран 👍 до промпта: 👌 «ран пошёл» не остаётся."""
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)

    async def dispatch(update, _ctx, _prompt, **kwargs):
        assert kwargs["still_wanted"]()
        _prestart(ctx, kwargs, update).cancelled = True
        kwargs["on_run_start"]()

    _like(ctx, dispatch)
    assert _acks(ctx) == [ACK_ON, ACK_OFF]


def test_like_set_again_while_first_is_held_keeps_new_ack(tmp_path):
    """Первый ход прошёл проверку «ещё нужно» и встал на лимите (рана нет);
    👍 сняли и поставили заново: первый уже не отменить, а его конец не
    снимает 👌 второго."""
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    calls = []

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        calls.append(kwargs["still_wanted"]())
        if len(calls) == 1:
            await tg_quick.on_reaction(_reaction_update(["👍"], []), ctx)
            await tg_quick.on_reaction(_reaction_update([], ["👍"]), ctx)
            return  # первый ход так и не начался
        kwargs["on_run_start"]()

    _like(ctx, dispatch)
    assert calls == [True, True]
    assert _acks(ctx) == [ACK_ON, ACK_ON]
    assert not ctx.application.bot_data["reaction_likes"]


def test_bot_marks_go_one_at_a_time(tmp_path):
    """Снятие 👌 прежнего хода не обгоняется постановкой 👌 нового: второй
    запрос к Telegram уходит только после ответа на первый."""
    ctx = _ctx(tmp_path)
    order, gate = [], None

    async def slow(_chat, _mid, mark):
        order.append(("start", mark))
        if mark is None:
            await gate.wait()
        order.append(("end", mark))

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        ctx.bot.set_message_reaction = slow
        off = asyncio.ensure_future(tg_quick._ack_reaction(ctx, 7, 5, False))
        await asyncio.sleep(0)
        on = asyncio.ensure_future(tg_quick._ack_reaction(ctx, 7, 5, True))
        for _i in range(3):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(off, on)

    asyncio.run(scenario())
    assert order == [("start", None), ("end", None), ("start", tg_quick.REACTION_ACK), ("end", tg_quick.REACTION_ACK)]


def test_dislike_taken_off_next_to_like_keeps_ack(tmp_path):
    """Две реакции на одном сообщении: сняли только 👎, 👌 лайка остаётся."""
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)

    async def dispatch(_update, _ctx, _prompt, **kwargs):
        assert kwargs["still_wanted"]()
        kwargs["on_run_start"]()

    _like(ctx, dispatch)
    asyncio.run(tg_quick.on_reaction(_reaction_update(["👍"], ["👍", "👎"]), ctx))
    assert tg_quick.reaction_notes(ctx, 7) == {"7:5": ("👎", "текст")}
    asyncio.run(tg_quick.on_reaction(_reaction_update(["👍", "👎"], ["👍"]), ctx))
    assert tg_quick.reaction_notes(ctx, 7) == {}
    assert ACK_OFF not in _acks(ctx)


def test_like_cancelled_during_first_ack_leaves_no_mark(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    ctx.bot.set_message_reaction = AsyncMock(side_effect=[asyncio.CancelledError(), None])
    dispatch = AsyncMock()
    try:
        _like(ctx, dispatch)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("отмена должна выйти наружу")
    dispatch.assert_not_awaited()
    assert not ctx.application.bot_data["reaction_likes"]
    assert _acks(ctx) == [ACK_ON, ACK_OFF]


def test_like_turn_failure_takes_ack_off_and_leaves_no_mark(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    try:
        _like(ctx, AsyncMock(side_effect=RuntimeError("сбой")))
    except RuntimeError:
        pass
    else:
        raise AssertionError("сбой запуска должен выйти наружу")
    assert _acks(ctx) == [ACK_ON, ACK_OFF]
    assert not ctx.application.bot_data["reaction_likes"]


def test_reaction_removed_drops_note(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    asyncio.run(tg_quick.on_reaction(_reaction_update([], ["👎"]), ctx))
    asyncio.run(tg_quick.on_reaction(_reaction_update(["👎"], []), ctx))
    assert tg_quick.reaction_notes(ctx, 7) == {}
    ctx.bot.set_message_reaction.assert_awaited_with(7, 5, None)


def test_reaction_ignored_for_other_emoji_unknown_message_group_and_stranger(tmp_path):
    ctx = _ctx(tmp_path)
    reactions.remember(7, 5, "текст", tmp_path)
    for update in (_reaction_update([], ["🔥"]), _reaction_update([], ["👍"], mid=99),
                   _reaction_update([], ["👍"], chat_type="supergroup"),
                   _reaction_update([], ["👍"], user_id=8)):
        with patch.object(bot, "_dispatch_to_claude", AsyncMock()) as dispatch:
            asyncio.run(tg_quick.on_reaction(update, ctx))
        dispatch.assert_not_awaited()
    assert not ctx.application.bot_data["reaction_notes"]
    ctx.bot.set_message_reaction.assert_not_awaited()


def _location_update(venue=None, forward=None):
    msg = MagicMock()
    msg.location = NS(latitude=48.8584, longitude=2.2945)
    msg.venue = venue
    msg.reply_text = AsyncMock()
    return NS(message=msg, effective_user=NS(id=7), effective_chat=NS(id=7, type="private"))


def test_location_goes_as_text(tmp_path):
    ctx = _ctx(tmp_path)
    update = _location_update(NS(title="Кафе  Зима", address="Улица 1"))
    with patch.object(bot, "_task_touch", AsyncMock()), patch.object(bot, "_compose_add", return_value=None), \
            patch.object(bot, "_gather_reserve", return_value=None), \
            patch.object(bot, "_forward_note", return_value=""), \
            patch.object(bot, "_text_in_context", side_effect=lambda cfg, msg, t: t), \
            patch.object(bot, "_dispatch_to_claude", AsyncMock()) as dispatch:
        asyncio.run(tg_quick.on_location(update, ctx))
    text = dispatch.await_args.args[2]
    assert "48.85840, 2.29450" in text and "```\nКафе Зима, Улица 1\n```" in text
    assert "mlat=48.85840&mlon=2.29450" in text
    # Без send_voice_reply _dispatch_to_claude_impl падал TypeError (обязательный аргумент).
    assert dispatch.await_args.kwargs["send_voice_reply"] is False


def test_location_denied_for_stranger(tmp_path):
    ctx = _ctx(tmp_path)
    update = _location_update()
    update.effective_user = NS(id=8)
    with patch.object(bot, "_dispatch_to_claude", AsyncMock()) as dispatch:
        asyncio.run(tg_quick.on_location(update, ctx))
    dispatch.assert_not_awaited()


def test_check_button_only_under_long_answer_and_keeps_last(tmp_path):
    ctx = _ctx(tmp_path)
    assert tg_quick.codex_check_markup(ctx, "в", "о" * 10) is None
    for _i in range(tg_quick.CHECK_KEEP + 3):
        markup = tg_quick.codex_check_markup(ctx, "в", "о" * tg_quick.CHECK_MIN_CHARS)
    checks = ctx.application.bot_data["codex_checks"]
    assert len(checks) == tg_quick.CHECK_KEEP
    assert markup.inline_keyboard[0][0].callback_data.split(":")[1] in checks


def _query(data):
    message = MagicMock()
    query = MagicMock(data=data, message=message)
    query.edit_message_reply_markup = AsyncMock()
    return NS(callback_query=query, effective_user=NS(id=7), effective_chat=NS(id=7, type="private")), message


def _real_tasks(ctx):
    """create_task приложения: настоящие задачи, у них done-колбэк снимает резерв."""
    tasks = []

    def create(coro):
        tasks.append(asyncio.get_running_loop().create_task(coro))
        return tasks[-1]
    ctx.application.create_task = create
    return tasks


def test_check_runs_read_only_once(tmp_path):
    ctx = _ctx(tmp_path)
    tasks = _real_tasks(ctx)
    markup = tg_quick.codex_check_markup(ctx, "вопрос", "ответ" * 200)
    data = markup.inline_keyboard[0][0].callback_data
    update, message = _query(data)
    message.edit_reply_markup = AsyncMock()
    result = NS(text="🔎 Codex\n\nвсё верно", is_error=False)
    typing = MagicMock()
    typing.return_value.__aenter__ = AsyncMock()
    typing.return_value.__aexit__ = AsyncMock(return_value=False)

    async def run():
        with patch.object(bot, "_answer_quietly", AsyncMock()) as toast, \
                patch.object(bot, "_typing_indicator", typing), \
                patch.object(bot, "_codex_as_claude_result", AsyncMock(return_value=result)) as codex, \
                patch.object(tg_quick, "send_text_to", AsyncMock()) as send:
            await tg_quick.on_codex_check(update, ctx)
            await tg_quick.on_codex_check(update, ctx)
            assert toast.await_args_list[-1].args[1] == _("Кодекс уже проверяет")
            assert len(tasks) == 1
            await tasks[0]
            await asyncio.sleep(0)
            await tg_quick.on_codex_check(update, ctx)
            assert toast.await_args_list[-1].args[1] == _("Кнопка устарела")
        assert codex.await_args.kwargs["sandbox"] == "read-only"
        assert ctx.application.bot_data["codex_checks_running"] == 0
        assert not ctx.application.bot_data["codex_checks_inflight"]
        assert codex.await_args.kwargs["chat_key"].startswith("codexcheck:")
        assert "вопрос" in codex.await_args.args[1]
        send.assert_awaited_once()

    asyncio.run(run())
    message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_markup_only_on_last_chunk(tmp_path):
    cfg = _ctx(tmp_path).application.bot_data["cfg"]
    cfg.telegram_text_limit = 50
    msg = MagicMock()
    msg.message_id = 0
    msg.reply_text = AsyncMock()
    asyncio.run(send_text_to(cfg, msg, "\n".join(["строка длиной сорок знаков почти вот"] * 3), reply_markup="M"))
    calls = msg.reply_text.await_args_list
    assert len(calls) > 1
    assert "reply_markup" not in calls[0].kwargs
    assert calls[-1].kwargs["reply_markup"] == "M"


def test_changed_reaction_survives_drop(tmp_path):
    ctx = _ctx(tmp_path)
    notes = ctx.application.bot_data["reaction_notes"]
    notes["7:5"] = json.dumps({"emoji": "👎", "text": "т"}, ensure_ascii=False)
    notes["7:6"] = json.dumps({"emoji": "👍", "text": "т"}, ensure_ascii=False)
    tg_quick.drop_reaction_notes(ctx, {"7:5": ("👍", "т"), "7:6": ("👍", "т")})
    assert list(notes) == ["7:5"]


def test_check_busy_keeps_button(tmp_path):
    ctx = _ctx(tmp_path)
    markup = tg_quick.codex_check_markup(ctx, "в", "о" * tg_quick.CHECK_MIN_CHARS)
    ctx.application.bot_data["codex_checks_running"] = tg_quick.CHECK_PARALLEL
    update, _message = _query(markup.inline_keyboard[0][0].callback_data)
    with patch.object(bot, "_answer_quietly", AsyncMock()):
        asyncio.run(tg_quick.on_codex_check(update, ctx))
    assert len(ctx.application.bot_data["codex_checks"]) == 1


def test_model_reply_is_noted_for_reactions(tmp_path):
    reactions.configure(tmp_path)
    cfg = _ctx(tmp_path).application.bot_data["cfg"]
    msg = MagicMock()
    msg.message_id = 0
    msg.reply_text = AsyncMock(return_value=NS(chat_id=7, message_id=11))
    asyncio.run(send_text_to(cfg, msg, "ответ"))
    assert reactions.lookup(tmp_path, 7, 11) == "ответ"


def _typing():
    typing = MagicMock()
    typing.return_value.__aenter__ = AsyncMock()
    typing.return_value.__aexit__ = AsyncMock(return_value=False)
    return typing


def _press_once(ctx, update, codex, send=None):
    async def run():
        tasks = _real_tasks(ctx)
        with patch.object(bot, "_answer_quietly", AsyncMock()), \
                patch.object(bot, "_typing_indicator", _typing()), \
                patch.object(bot, "_codex_as_claude_result", codex), \
                patch.object(tg_quick, "send_text_to", send or AsyncMock()):
            await tg_quick.on_codex_check(update, ctx)
            for task in tasks:
                await task
            await asyncio.sleep(0)

    asyncio.run(run())


def test_failed_check_keeps_record_and_button(tmp_path):
    """Сбой Кодекса, ошибка отправки ответа или остановка бота: запись на диске
    и кнопка под ответом остаются, нажать можно снова и после перезапуска (ревью quick 02.11)."""
    cases = ((AsyncMock(return_value=NS(text="⛔ отменено: бот останавливается", is_error=True)), None),
             (AsyncMock(side_effect=RuntimeError("boom")), None),
             (AsyncMock(return_value=NS(text="всё верно", is_error=False)), AsyncMock(side_effect=RuntimeError("400"))))
    for codex, send in cases:
        ctx = _ctx(tmp_path)
        markup = tg_quick.codex_check_markup(ctx, "вопрос", "ответ" * 200)
        data = markup.inline_keyboard[0][0].callback_data
        update, message = _query(data)
        message.edit_reply_markup = AsyncMock()
        _press_once(ctx, update, codex, send)
        assert data.split(":")[1] in ctx.application.bot_data["codex_checks"]
        assert ctx.application.bot_data["codex_checks_running"] == 0
        assert not ctx.application.bot_data["codex_checks_inflight"]
        message.edit_reply_markup.assert_not_awaited()
        update.callback_query.edit_message_reply_markup.assert_not_awaited()
        ctx.application.bot_data["codex_checks"].clear()


def test_check_cancelled_before_start_frees_slot(tmp_path):
    """Отмена задачи до её первого шага (остановка бота) не держит место и
    отметку «проверяет»: finally корутины тут не выполнился бы."""
    ctx = _ctx(tmp_path)
    markup = tg_quick.codex_check_markup(ctx, "вопрос", "ответ" * 200)
    update, _message = _query(markup.inline_keyboard[0][0].callback_data)

    async def run():
        tasks = _real_tasks(ctx)
        with patch.object(bot, "_answer_quietly", AsyncMock()):
            await tg_quick.on_codex_check(update, ctx)
            tasks[0].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

    asyncio.run(run())
    assert ctx.application.bot_data["codex_checks_running"] == 0
    assert not ctx.application.bot_data["codex_checks_inflight"]
    assert len(ctx.application.bot_data["codex_checks"]) == 1


def test_good_check_takes_button_and_record_off(tmp_path):
    ctx = _ctx(tmp_path)
    markup = tg_quick.codex_check_markup(ctx, "вопрос", "ответ" * 200)
    update, message = _query(markup.inline_keyboard[0][0].callback_data)
    message.edit_reply_markup = AsyncMock()
    _press_once(ctx, update, AsyncMock(return_value=NS(text="всё верно", is_error=False)))
    assert not ctx.application.bot_data["codex_checks"]
    message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_check_on_inaccessible_message_keeps_record(tmp_path):
    """Сообщение под кнопкой боту недоступно: ответить некуда, запись не снимается."""
    from telegram import Chat, InaccessibleMessage
    ctx = _ctx(tmp_path)
    markup = tg_quick.codex_check_markup(ctx, "вопрос", "ответ" * 200)
    update, _message = _query(markup.inline_keyboard[0][0].callback_data)
    update.callback_query.message = InaccessibleMessage(Chat(7, "private"), 5)
    codex = AsyncMock()
    _press_once(ctx, update, codex)
    codex.assert_not_awaited()
    assert len(ctx.application.bot_data["codex_checks"]) == 1


def test_forwarded_location_is_not_ivans(tmp_path):
    """Пересланная чужая геометка не подписывается «от Ивана»: иначе модель
    считает, что Иван сейчас там."""
    ctx = _ctx(tmp_path)
    with patch.object(bot, "_task_touch", AsyncMock()), patch.object(bot, "_compose_add", return_value=None), \
            patch.object(bot, "_gather_reserve", return_value=None), \
            patch.object(bot, "_forward_note", return_value="[Переслано от: Аня]\n"), \
            patch.object(bot, "_text_in_context", side_effect=lambda cfg, msg, t: t), \
            patch.object(bot, "_dispatch_to_claude", AsyncMock()) as dispatch:
        asyncio.run(tg_quick.on_location(_location_update(), ctx))
    text = dispatch.await_args.args[2]
    assert text.startswith("[Переслано от: Аня]\n") and "Ивана" not in text
    assert "48.85840, 2.29450" in text


def test_rotation_keeps_running_check(tmp_path):
    """Ротация последних кнопок не снимает запись идущей проверки (ревью quick, круг 2)."""
    ctx = _ctx(tmp_path)
    first = tg_quick.codex_check_markup(ctx, "в", "о" * tg_quick.CHECK_MIN_CHARS)
    first_id = first.inline_keyboard[0][0].callback_data.split(":")[1]
    ctx.application.bot_data["codex_checks_inflight"] = {first_id}
    for _i in range(tg_quick.CHECK_KEEP + 2):
        tg_quick.codex_check_markup(ctx, "в", "о" * tg_quick.CHECK_MIN_CHARS)
    assert first_id in ctx.application.bot_data["codex_checks"]


def test_failed_check_moves_record_to_end(tmp_path):
    """Сбой проверки ставит запись в конец: следующая кнопка её не вытеснит (круг 3)."""
    ctx = _ctx(tmp_path)
    markup = tg_quick.codex_check_markup(ctx, "вопрос", "ответ" * 200)
    for _i in range(tg_quick.CHECK_KEEP - 1):
        tg_quick.codex_check_markup(ctx, "в", "о" * tg_quick.CHECK_MIN_CHARS)
    update, message = _query(markup.inline_keyboard[0][0].callback_data)
    message.edit_reply_markup = AsyncMock()
    _press_once(ctx, update, AsyncMock(side_effect=RuntimeError("boom")))
    tg_quick.codex_check_markup(ctx, "в", "о" * tg_quick.CHECK_MIN_CHARS)
    assert markup.inline_keyboard[0][0].callback_data.split(":")[1] in ctx.application.bot_data["codex_checks"]


class LikeTurnTests(_Base):
    """👍 через настоящий запуск хода (обвязка без сети из test_review_bot_main)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot_data["reaction_notes"] = PersistentDict(self.state / "reaction_notes.json")
        self.ctx.bot.set_message_reaction = AsyncMock()
        reactions.remember(7, 5, "Поставить бассейн на среду?", self.state)
        reactions.remember(7, 6, "Сводка новостей", self.state)

    def acks(self) -> list:
        """Отметки бота на сообщении 5: 👀 хода идёт на сообщение 0 и сюда не входит."""
        calls = [c.args or (c.kwargs.get("chat_id"), c.kwargs.get("message_id"), c.kwargs.get("reaction"))
                 for c in self.ctx.bot.set_message_reaction.await_args_list]
        return [c for c in calls if c[1] == 5]

    async def test_like_runs_turn_with_pending_dislike_note_and_crash_record(self) -> None:
        await tg_quick.on_reaction(_reaction_update([], ["👎"], mid=6), self.ctx)
        seen = {}

        async def in_run(_run) -> None:
            seen.update(inflight.load(self.state))
        self.run_hook = in_run
        await tg_quick.on_reaction(_reaction_update([], ["👍"]), self.ctx)
        [run] = self.runs
        note, ok = _(tg_quick.REACTIONS_NOTE).split("{lines}")[0], _(tg_quick.REACTION_OK).split("{quote}")[0]
        self.assertTrue(run["prompt"].startswith(note))
        self.assertIn("- 👎 Сводка новостей", run["prompt"])
        self.assertLess(run["prompt"].index(note), run["prompt"].index(ok))
        self.assertTrue(run["prompt"].endswith("```\nПоставить бассейн на среду?\n```"))
        # Заметка ушла этим ходом; запись на случай аварии жила до конца хода.
        self.assertEqual(tg_quick.reaction_notes(self.ctx, 7), {})
        [rec] = seen.values()
        self.assertEqual((rec["chat_id"], rec["message_id"], rec["head"], rec["kind"], rec.get("sid")),
                         (7, 5, "👍 Поставить бассейн на среду?", "reaction", run["sid"]))
        self.assertEqual(inflight.load(self.state), {})
        self.assertEqual(self.acks(), [ACK_ON])
        self.send_reply_text.assert_awaited()

    async def test_like_waits_busy_chat_silently_and_taking_it_off_cancels(self) -> None:
        lock = bot._chat_lock(self.ctx, "7")
        await lock.acquire()
        turn = asyncio.ensure_future(tg_quick.on_reaction(_reaction_update([], ["👍"]), self.ctx))
        for _i in range(5):
            await asyncio.sleep(0)
        # Ни уведомления «ещё отвечаю», ни пачки хода: ждёт замка сам.
        self.ctx.bot.send_message.assert_not_awaited()
        self.assertFalse(bot._turn_packs(self.ctx))
        await tg_quick.on_reaction(_reaction_update(["👍"], []), self.ctx)
        lock.release()
        await turn
        self.assertEqual(self.runs, [])
        self.assertEqual(self.acks(), [ACK_ON, ACK_OFF])
        self.assertEqual(inflight.load(self.state), {})

    async def test_like_taken_off_while_login_refreshes_never_reaches_claude(self) -> None:
        """Процесс рана ещё не создан: снятый 👍 помечает отмену до старта,
        обёртка рана видит её при создании процесса."""
        await tg_quick.on_reaction(_reaction_update([], ["👎"], mid=6), self.ctx)
        gate, entered = asyncio.Event(), asyncio.Event()
        seen = {}

        async def slow_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            entered.set()
            await gate.wait()  # освежение входа до создания процесса
            seen["prestart"] = dict(self.bot_data["run_prestart"])[chat_key]
            kw["on_run_start"]()
            return ClaudeResult(text="⛔ отменено", session_id="sid-x", is_error=True, signalled=True)

        with patch.object(bot, "run_claude", slow_run):
            turn = asyncio.ensure_future(tg_quick.on_reaction(_reaction_update([], ["👍"]), self.ctx))
            await entered.wait()
            await tg_quick.on_reaction(_reaction_update(["👍"], []), self.ctx)
            gate.set()
            await turn
        self.assertTrue(seen["prestart"].cancelled)
        # Заметка 👎, подмешанная в погашенный ход, ждёт следующего.
        self.assertEqual(tg_quick.reaction_notes(self.ctx, 7), {"7:6": ("👎", "Сводка новостей")})
        self.assertEqual(self.acks(), [ACK_ON, ACK_OFF])
        self.assertFalse(self.bot_data["reaction_likes"])
        self.send_reply_text.assert_not_awaited()

    async def test_like_held_by_limit_takes_ack_off(self) -> None:
        with patch.object(bot, "_limit_hold", AsyncMock(return_value=True)):
            await tg_quick.on_reaction(_reaction_update([], ["👍"]), self.ctx)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.acks(), [ACK_ON, ACK_OFF])

    async def test_like_refused_while_bot_waits_restart(self) -> None:
        self.bot_data["draining"] = True
        await tg_quick.on_reaction(_reaction_update([], ["👍"]), self.ctx)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.acks(), [ACK_ON, ACK_OFF])
        self.assertEqual(inflight.load(self.state), {})
        self.ctx.bot.send_message.assert_awaited_once()

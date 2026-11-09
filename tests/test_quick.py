"""Реакции, геометка, кнопка проверки Кодексом (решение Ивана 01.11.2026)."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import ReactionTypeEmoji

from claude_tg_bot import bot, reactions, tg_quick
from claude_tg_bot.overrides import PersistentDict
from claude_tg_bot.replies import send_text_to


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
    asyncio.run(tg_quick.on_reaction(_reaction_update([], ["👍"]), ctx))
    assert tg_quick.reaction_notes(ctx, 7) == {"7:5": ("👍", "Вызвать мастера")}
    ctx.bot.set_message_reaction.assert_awaited_once_with(7, 5, tg_quick.REACTION_ACK)
    assert "👍 Вызвать мастера" in tg_quick.reaction_note_text(tg_quick.reaction_notes(ctx, 7))


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
        asyncio.run(tg_quick.on_reaction(update, ctx))
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
            assert toast.await_args_list[-1].args[1] == "Кодекс уже проверяет"
            assert len(tasks) == 1
            await tasks[0]
            await asyncio.sleep(0)
            await tg_quick.on_codex_check(update, ctx)
            assert toast.await_args_list[-1].args[1] == "Кнопка устарела"
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

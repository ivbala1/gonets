"""Тесты выбора модели: парсинг CLAUDE_MODEL_CHOICES в load_config и кнопки
/model (реестр токенов в bot_data, установка и сброс override по клику)."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from telegram import CallbackQuery, Chat, Message, Update, User  # noqa: E402

from claude_tg_bot.bot import (  # noqa: E402
    _activate_pending_model, _bump_session_epoch, _model_footer_note, _model_for,
    _model_label, on_model, on_model_pick, on_model_pin,
)
from claude_tg_bot.config import DEFAULT_MODEL_CHOICES, load_config  # noqa: E402


class ModelChoicesConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("CLAUDE_MODEL_CHOICES")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("CLAUDE_MODEL_CHOICES", None)
        else:
            os.environ["CLAUDE_MODEL_CHOICES"] = self._saved

    def test_empty_env_falls_back_to_default(self) -> None:
        os.environ["CLAUDE_MODEL_CHOICES"] = ""
        cfg = load_config(skip_hook_validation=True)
        self.assertEqual(cfg.model_choices, DEFAULT_MODEL_CHOICES)

    def test_env_overrides_and_trims(self) -> None:
        os.environ["CLAUDE_MODEL_CHOICES"] = " m1 , m2 ,,"
        cfg = load_config(skip_hook_validation=True)
        self.assertEqual(cfg.model_choices, ("m1", "m2"))


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []

    async def send_message(self, *args, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=1)

    async def answer_callback_query(self, *args, **kw):
        return True

    async def edit_message_text(self, *args, **kw):
        self.edited.append(kw)
        return True


def _user() -> User:
    return User(id=1, first_name="u", is_bot=False)


def _cmd_update(
    bot: FakeBot, text: str = "/model", chat_id: int = 555, thread_id: int | None = None,
) -> Update:
    msg = Message(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=Chat.PRIVATE if thread_id is None else Chat.SUPERGROUP),
        from_user=_user(),
        text=text,
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
    )
    msg.set_bot(bot)
    return Update(update_id=1, message=msg)


def _cb_update(bot: FakeBot, data: str, user_id: int = 1) -> Update:
    who = User(id=user_id, first_name="u", is_bot=False)
    msg = Message(
        message_id=11,
        date=datetime.now(timezone.utc),
        chat=Chat(id=555, type=Chat.PRIVATE),
        from_user=who,
    )
    msg.set_bot(bot)
    cq = CallbackQuery(
        id="cb1", from_user=who, chat_instance="ci", data=data, message=msg,
    )
    cq.set_bot(bot)
    return Update(update_id=2, callback_query=cq)


def _cfg(**over) -> SimpleNamespace:
    base = dict(
        allowed_user_ids=frozenset({1}),
        allowed_chat_ids=frozenset({-100777}),
        model="claude-sonnet-5",
        model_choices=("m-alpha", "m-beta"),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(cfg: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}))


class ModelButtonsTests(unittest.TestCase):
    def test_dynamic_buttons_are_aliases_labelled_with_newest_model(self) -> None:
        from claude_tg_bot import models as live
        bot = FakeBot()
        ctx = _ctx(_cfg(model="sonnet", model_choices=("opus", "sonnet"),
                        model_choices_dynamic=True, state_dir=Path("/nonexistent-state")))
        original = live.live_newest_by_family
        try:
            live.live_newest_by_family = lambda state_dir: {"opus": "claude-opus-6"}
            asyncio.run(on_model(_cmd_update(bot), ctx))
        finally:
            live.live_newest_by_family = original
        markup = bot.sent[0]["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(labels[:2], ["opus (claude-opus-6)", "✅ sonnet"])
        self.assertIn("алиасы семейств", bot.sent[0]["text"])
        # Кнопка сохраняет алиас, а не id из подписи: иначе выбор устареет.
        picked = sorted(v[1] for v in ctx.application.bot_data["model_options"].values())
        self.assertEqual(picked, ["", "opus", "sonnet"])

    def test_no_arg_shows_buttons_and_registers_tokens(self) -> None:
        bot = FakeBot()
        ctx = _ctx(_cfg())
        asyncio.run(on_model(_cmd_update(bot), ctx))
        self.assertEqual(len(bot.sent), 1)
        markup = bot.sent[0]["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(labels), 3)  # 2 модели + кнопка сброса
        self.assertIn("m-alpha", labels[0])
        options: dict[str, tuple[str, str]] = ctx.application.bot_data["model_options"]
        self.assertEqual(sorted(v[1] for v in options.values()), ["", "m-alpha", "m-beta"])
        for target, _model in options.values():
            self.assertEqual(target, "555")

    def test_pick_sets_override_then_reset_clears(self) -> None:
        bot = FakeBot()
        ctx = _ctx(_cfg())
        asyncio.run(on_model(_cmd_update(bot), ctx))
        options = ctx.application.bot_data["model_options"]
        tok_beta = next(t for t, v in options.items() if v[1] == "m-beta")
        asyncio.run(on_model_pick(_cb_update(bot, f"model:{tok_beta}"), ctx))
        # Кнопка ставит модель до конца сессии, а не закрепляет за темой.
        self.assertEqual(_model_for(ctx, ctx.application.bot_data["cfg"], "555"), "m-beta")
        self.assertFalse(ctx.application.bot_data.get("model_by_target"))
        self.assertIn("m-beta", bot.edited[-1]["text"])
        self.assertIn("до конца сессии", bot.edited[-1]["text"])
        # Повторный /model: у текущей модели галочка, клик по сбросу снимает override.
        asyncio.run(on_model(_cmd_update(bot), ctx))
        options = ctx.application.bot_data["model_options"]
        markup = bot.sent[-1]["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("✅ m-beta", labels)
        tok_reset = next(t for t, v in options.items() if v[1] == "")
        asyncio.run(on_model_pick(_cb_update(bot, f"model:{tok_reset}"), ctx))
        self.assertEqual(_model_for(ctx, ctx.application.bot_data["cfg"], "555"), "claude-sonnet-5")

    def test_stale_token_reports_and_does_not_touch_overrides(self) -> None:
        bot = FakeBot()
        ctx = _ctx(_cfg())
        asyncio.run(on_model_pick(_cb_update(bot, "model:deadbeef"), ctx))
        self.assertIn("устарел", bot.edited[-1]["text"])
        self.assertFalse(ctx.application.bot_data.get("model_by_target"))

    def test_second_click_on_same_button_is_stale(self) -> None:
        """Токен одноразовый: повторный клик не должен молча переприменять выбор."""
        bot = FakeBot()
        ctx = _ctx(_cfg())
        asyncio.run(on_model(_cmd_update(bot), ctx))
        tok = next(t for t, v in ctx.application.bot_data["model_options"].items() if v[1] == "m-beta")
        asyncio.run(on_model_pick(_cb_update(bot, f"model:{tok}"), ctx))
        ctx.application.bot_data["model_by_target"]["555"] = "manual"
        asyncio.run(on_model_pick(_cb_update(bot, f"model:{tok}"), ctx))
        self.assertIn("устарел", bot.edited[-1]["text"])
        self.assertEqual(ctx.application.bot_data["model_by_target"]["555"], "manual")

    def test_keyboard_from_other_topic_is_dropped_not_misapplied(self) -> None:
        """Реестр перезаписывается каждым /model, поэтому клавиатура прошлой темы
        устаревает. Проверяем именно это: не «применилось к чужой ветке», а «устарел»."""
        bot = FakeBot()
        ctx = _ctx(_cfg())
        asyncio.run(on_model(_cmd_update(bot, chat_id=-100777, thread_id=42), ctx))
        tok_topic = next(
            t for t, v in ctx.application.bot_data["model_options"].items() if v[1] == "m-alpha"
        )
        asyncio.run(on_model(_cmd_update(bot, chat_id=555), ctx))
        asyncio.run(on_model_pick(_cb_update(bot, f"model:{tok_topic}"), ctx))
        self.assertIn("устарел", bot.edited[-1]["text"])
        self.assertFalse(ctx.application.bot_data.get("model_by_target"))

    def test_override_is_per_thread(self) -> None:
        bot = FakeBot()
        ctx = _ctx(_cfg())
        asyncio.run(on_model(_cmd_update(bot, chat_id=-100777, thread_id=42), ctx))
        tok = next(t for t, v in ctx.application.bot_data["model_options"].items() if v[1] == "m-alpha")
        asyncio.run(on_model_pick(_cb_update(bot, f"model:{tok}"), ctx))
        self.assertEqual(ctx.application.bot_data["model_pending"], {"-100777:42": "m-alpha"})

    def test_click_by_foreign_user_ignored(self) -> None:
        bot = FakeBot()
        ctx = _ctx(_cfg())
        asyncio.run(on_model(_cmd_update(bot), ctx))
        tok = next(t for t, v in ctx.application.bot_data["model_options"].items() if v[1] == "m-beta")
        asyncio.run(on_model_pick(_cb_update(bot, f"model:{tok}", user_id=999), ctx))
        self.assertFalse(ctx.application.bot_data.get("model_by_target"))
        self.assertFalse(bot.edited)

    def test_callback_data_within_telegram_limit(self) -> None:
        """callback_data у Telegram ограничена 64 байтами: длинные id моделей
        поэтому и не кладутся в кнопку напрямую."""
        bot = FakeBot()
        ctx = _ctx(_cfg(model_choices=("claude-haiku-4-5-20251001",) * 3))
        asyncio.run(on_model(_cmd_update(bot), ctx))
        markup = bot.sent[-1]["reply_markup"]
        for row in markup.inline_keyboard:
            for b in row:
                self.assertLessEqual(len(b.callback_data.encode()), 64)


class SessionModelTests(unittest.TestCase):
    """Модель «до конца сессии»: ставится на время сложной задачи и сама
    уходит со сменой сессии, чтобы не забыть вернуть дефолт."""

    def _pick(self, bot: FakeBot, ctx: SimpleNamespace, model: str) -> None:
        asyncio.run(on_model(_cmd_update(bot), ctx))
        tok = next(t for t, v in ctx.application.bot_data["model_options"].items() if v[1] == model)
        asyncio.run(on_model_pick(_cb_update(bot, f"model:{tok}"), ctx))

    def test_session_model_ends_with_the_session_it_ran_in(self) -> None:
        bot, cfg = FakeBot(), _cfg()
        ctx = _ctx(cfg)
        self._pick(bot, ctx, "m-beta")
        _activate_pending_model(ctx, "555")  # первый ран сессии
        self.assertEqual(_model_for(ctx, cfg, "555"), "m-beta")
        _bump_session_epoch(ctx, "555")  # /reset, простой, /resume
        self.assertEqual(_model_for(ctx, cfg, "555"), "claude-sonnet-5")

    def test_choice_made_before_session_change_goes_to_the_new_session(self) -> None:
        """/model перед /reset или после долгого простоя темы: сброс сессии
        случается до первого рана и не должен съесть свежий выбор."""
        bot, cfg = FakeBot(), _cfg()
        ctx = _ctx(cfg)
        self._pick(bot, ctx, "m-beta")
        _bump_session_epoch(ctx, "555")
        _activate_pending_model(ctx, "555")
        self.assertEqual(_model_for(ctx, cfg, "555"), "m-beta")
        _bump_session_epoch(ctx, "555")
        self.assertEqual(_model_for(ctx, cfg, "555"), "claude-sonnet-5")

    def test_session_model_falls_back_to_pinned_not_default(self) -> None:
        bot, cfg = FakeBot(), _cfg()
        ctx = _ctx(cfg)
        ctx.application.bot_data["model_by_target"] = {"555": "m-alpha"}
        self._pick(bot, ctx, "m-beta")
        _activate_pending_model(ctx, "555")
        self.assertEqual(_model_label(ctx, cfg, "555"), "m-beta ⏳ до конца сессии")
        _bump_session_epoch(ctx, "555")
        self.assertEqual(_model_label(ctx, cfg, "555"), "m-alpha 📌 закреплена за темой")

    def test_pin_button_after_pick_makes_choice_permanent(self) -> None:
        bot, cfg = FakeBot(), _cfg()
        ctx = _ctx(cfg)
        self._pick(bot, ctx, "m-beta")
        pin = bot.edited[-1]["reply_markup"].inline_keyboard[0][0]
        self.assertTrue(pin.callback_data.startswith("modelpin:"))
        asyncio.run(on_model_pin(_cb_update(bot, pin.callback_data), ctx))
        self.assertEqual(ctx.application.bot_data["model_by_target"], {"555": "m-beta"})
        _activate_pending_model(ctx, "555")
        _bump_session_epoch(ctx, "555")
        self.assertEqual(_model_for(ctx, cfg, "555"), "m-beta")
        # Токен одноразовый: повторный клик ничего не меняет.
        asyncio.run(on_model_pin(_cb_update(bot, pin.callback_data), ctx))
        self.assertIn("устарел", bot.edited[-1]["text"])

    def test_text_commands_session_pin_default(self) -> None:
        bot, cfg = FakeBot(), _cfg()
        ctx = _ctx(cfg)
        asyncio.run(on_model(_cmd_update(bot, "/model opus"), ctx))
        self.assertEqual(_model_label(ctx, cfg, "555"), "opus ⏳ до конца сессии")
        self.assertFalse(ctx.application.bot_data.get("model_by_target"))
        asyncio.run(on_model(_cmd_update(bot, "/model pin"), ctx))  # закрепить текущую
        self.assertEqual(_model_label(ctx, cfg, "555"), "opus 📌 закреплена за темой")
        asyncio.run(on_model(_cmd_update(bot, "/model pin fable"), ctx))
        self.assertEqual(ctx.application.bot_data["model_by_target"], {"555": "fable"})
        asyncio.run(on_model(_cmd_update(bot, "/model haiku"), ctx))
        asyncio.run(on_model(_cmd_update(bot, "/model default"), ctx))
        self.assertEqual(_model_label(ctx, cfg, "555"), "claude-sonnet-5 — дефолт")

    def test_menu_offers_to_pin_the_session_model(self) -> None:
        bot, cfg = FakeBot(), _cfg()
        ctx = _ctx(cfg)
        self._pick(bot, ctx, "m-beta")
        asyncio.run(on_model(_cmd_update(bot), ctx))
        labels = [b.text for row in bot.sent[-1]["reply_markup"].inline_keyboard for b in row]
        self.assertIn("📌 закрепить m-beta за темой", labels)
        self.assertIn("m-beta ⏳ до конца сессии", bot.sent[-1]["text"])

    def test_footer_note_only_for_non_default_model(self) -> None:
        bot, cfg = FakeBot(), _cfg()
        ctx = _ctx(cfg)
        self.assertEqual(_model_footer_note(ctx, cfg, "555"), "")
        self._pick(bot, ctx, "m-beta")
        self.assertEqual(_model_footer_note(ctx, cfg, "555"), "🤖 m-beta ⏳")
        _activate_pending_model(ctx, "555")
        _bump_session_epoch(ctx, "555")
        ctx.application.bot_data["model_by_target"] = {"555": "m-alpha", "777": "claude-sonnet-5"}
        self.assertEqual(_model_footer_note(ctx, cfg, "555"), "🤖 m-alpha 📌")
        # Закреплена та же модель, что и дефолт: напоминать не о чем.
        self.assertEqual(_model_footer_note(ctx, cfg, "777"), "")

    def test_run_activates_pending_choice_before_picking_model(self) -> None:
        """Порядок в месте запуска: сначала сброс сессии по простою, потом
        активация выбора, потом run_claude. Иначе выбор либо сгорит, либо
        переживёт свою сессию."""
        src = (Path(__file__).resolve().parents[1] / "src/claude_tg_bot/bot.py").read_text()
        idle = src.index("idle-reset session chat=")
        activate = src.index("_activate_pending_model(ctx, chat_key)\n")
        run = src.index("model=_model_for(ctx, cfg, chat_key)")
        self.assertLess(idle, activate)
        self.assertLess(activate, run)


class ModelPipelineTests(unittest.TestCase):
    """Override должен доезжать до запуска claude, а не только лежать в bot_data:
    run_claude получает модель из `_model_for`, не из cfg.model напрямую."""

    def test_model_for_prefers_override_then_falls_back(self) -> None:
        cfg = _cfg()
        ctx = _ctx(cfg)
        self.assertEqual(_model_for(ctx, cfg, "555"), "claude-sonnet-5")
        ctx.application.bot_data["model_by_target"] = {"555": "m-beta"}
        self.assertEqual(_model_for(ctx, cfg, "555"), "m-beta")
        self.assertEqual(_model_for(ctx, cfg, "999"), "claude-sonnet-5")
        ctx.application.bot_data["model_by_target"].pop("555")
        self.assertEqual(_model_for(ctx, cfg, "555"), "claude-sonnet-5")

    def test_empty_default_means_cli_default(self) -> None:
        cfg = _cfg(model="")
        ctx = _ctx(cfg)
        self.assertEqual(_model_for(ctx, cfg, "555"), "")

    def test_run_call_site_uses_model_for(self) -> None:
        """Регрессия на «кнопки работают, а ран берёт cfg.model»: место вызова
        run_claude обязано передавать _model_for, иначе override не доедет."""
        src = (Path(__file__).resolve().parents[1] / "src/claude_tg_bot/bot.py").read_text()
        self.assertIn("model=_model_for(ctx, cfg, chat_key)", src)
        self.assertNotIn("model=cfg.model", src)


if __name__ == "__main__":
    unittest.main()

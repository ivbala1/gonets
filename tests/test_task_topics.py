"""Темы задач в боте (переделка 16.10.2026, итог второго мнения Codex 15.10
в BACKLOG.md): задача начинается с обдумывания только чтением; проект с
маркером <<DO>> получает «▶️ Делай», то же слово «делай» текстом и голосом;
голое «да» и поправка продолжают обдумывание; старая кнопка не выполняет
новый проект; выполнение одно на весь бот, остальные ждут общей очереди и
стартуют сами; права у рана выполнения, а не у темы; итог в личку; авария
посреди выполнения оставляет тему с ошибкой. Фейки на SimpleNamespace по
образцу test_review_bot_main.py, без сети."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")

from claude_tg_bot import approval  # noqa: E402
from claude_tg_bot import bot as botmod  # noqa: E402
from claude_tg_bot import claude_runner  # noqa: E402
from claude_tg_bot import tasks  # noqa: E402
from claude_tg_bot.claude_runner import ClaudeResult, SessionStore  # noqa: E402
from claude_tg_bot.skills.news_digest.core import Item  # noqa: E402
from telegram import Chat, Message, MessageEntity, MessageOriginUser, Update, User  # noqa: E402
from telegram.error import BadRequest, NetworkError, RetryAfter  # noqa: E402
from telegram.ext import CommandHandler  # noqa: E402

# Настоящий роутер Свода и разметка ограждений: помощники из test_tasks.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tasks import _fenced, _resolve_pin  # noqa: E402

USER = 7
GROUP = -100
THREAD = 77
TID = "202610131008-1"
PLAN = "Понял так: прочитать README; проверю: сводка в ответе\n1. прочитать README\n2. ответить сводкой"
SHA = tasks.plan_sha(PLAN)
DO = f"nt:{TID}:do:{SHA}"
ITEM = Item(1, "try", "web-edit", "суть", "у тебя", "https://e.x/1", "hn", message_id=101)
BOT_ID = 4242
NEW_THREAD = 88


class _Status:
    def __init__(self, *_a, **_kw) -> None:
        pass

    def start(self) -> None:
        pass

    def on_event(self) -> None:
        pass

    async def on_tool(self, _desc: str) -> None:
        pass

    async def finish(self) -> None:
        pass


def _msg(thread: int | None = THREAD) -> SimpleNamespace:
    return SimpleNamespace(
        text="", caption=None, chat_id=GROUP, message_id=11,
        message_thread_id=thread, is_topic_message=thread is not None,
        forward_origin=None, reply_to_message=None,
        set_reaction=AsyncMock(), reply_text=AsyncMock(), delete=AsyncMock(),
    )


def _update(msg: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        update_id=1, message=msg, effective_message=msg, callback_query=None,
        effective_user=SimpleNamespace(id=USER),
        effective_chat=SimpleNamespace(id=GROUP, type="supergroup"),
    )


class _TaskBase(unittest.IsolatedAsyncioTestCase):
    """Бот без сети: run_claude подменён, реестр задач и хранилища во
    временном каталоге, тема задачи GROUP:THREAD. Ответы агента по очереди
    из self.replies, дальше «ok»."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.store = SessionStore(self.state / "sessions.json")
        self.runs: list[dict] = []
        self.replies: list[str] = []
        self.result: ClaudeResult | None = None
        self.cfg = SimpleNamespace(
            codex_trigger_prefix="gpt:", codex_bin="", codex_fallback=False,
            codex_fallback_sandbox="read-only",
            session_idle_ttl_sec=0, claude_bin="claude", claude_cwd=Path("/"),
            hook_settings_path=Path("/x.json"), max_claude_runtime_sec=0,
            allowed_user_ids={USER}, allowed_chat_ids={GROUP}, projects=(),
            persona_text="", telegram_text_limit=4096, telegram_caption_limit=1024,
            voice_reply_max_chars=4000, voice_max_duration_sec=180, state_dir=self.state,
            primary_chat_id=USER,
        )
        self.bot_data = {
            "cfg": self.cfg, "sessions": self.store, "procs": {}, "active_sessions": {},
            "session_targets": {}, "chat_locks": {},
            "untrusted_sessions": SessionStore(self.state / "untrusted_sessions.json"),
            "session_scopes": SessionStore(self.state / "session_scopes.json"),
            "scope_fingerprints": SessionStore(self.state / "scope_fingerprints.json"),
            "topic_names": SessionStore(self.state / "topic_names.json"),
            "scopes": SessionStore(self.state / "scopes.json"),
        }
        self.bg: list[asyncio.Task] = []
        # Ран выполнения ждёт, пока тест не откроет ворота (None: не ждёт).
        self.gate: asyncio.Event | None = None
        # Метки прав рана выполнения в момент рана (approval: allow:task-exec).
        self.exec_sids_seen: list[set] = []
        self.topics: dict[int, SimpleNamespace] = {}

        def synthetic(bot, chat_id, thread_id):
            # Своя тема на каждый номер: выполнение идёт в теме своей задачи.
            if thread_id == self.topic.message_thread_id:
                return _update(self.topic)
            return _update(self.topics.setdefault(thread_id, _msg(thread_id)))

        def create_task(coro, update=None, *, name=None):
            t = asyncio.ensure_future(coro)
            self.bg.append(t)
            return t

        self.bot = SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock(),
                                   edit_message_text=AsyncMock(), close_forum_topic=AsyncMock(),
                                   delete_forum_topic=AsyncMock())
        self.ctx = SimpleNamespace(
            application=SimpleNamespace(bot_data=self.bot_data, running=False, create_task=create_task),
            bot=self.bot, args=[],
        )
        self.topic = _msg()

        async def fake_run(_bin, chat_key, _cid, _tid, prompt, store, **kw):
            existing = store.get(chat_key)
            sid = existing or f"sid-{len(self.runs)}"
            if kw.get("on_session_start") is not None:
                kw["on_session_start"](sid)
            if kw.get("on_run_start") is not None:
                # Как настоящий run_claude: процесс создан, промпт ещё не подан.
                kw["on_run_start"]()
            self.exec_sids_seen.append(set(self.bot_data.get("task_exec_sids") or ()))
            self.runs.append({"resumed": existing, "prompt": prompt, "sid": sid, "kw": kw,
                              "chat_key": chat_key})
            if self.gate is not None:
                await self.gate.wait()
            if self.result is not None:
                return self.result
            store.set(chat_key, sid)
            # Выполнение без своего ответа отвечает сводкой: без неё итог не
            # подтверждён (разбор задач 19.10.2026).
            default = "✅ Готово" if kw.get("on_run_start") is not None else "ok"
            text = self.replies.pop(0) if self.replies else default
            return ClaudeResult(text=text, session_id=sid)

        self.codex = AsyncMock(return_value=ClaudeResult(text="gpt"))
        for name, value in {
            "run_claude": fake_run,
            "_codex_as_claude_result": self.codex,
            "_synthetic_update": synthetic,
            "_persona_for": lambda *a: "",
            "_memory_scope_hint": lambda *a: "",
            "_effort_for": lambda *a: "",
            "_model_for": lambda *a: "",
            "_activate_pending_model": lambda *a: None,
            "_model_footer_note": lambda *a: "",
            "_RunStatus": _Status,
            "_cleanup_abandoned_approvals": AsyncMock(),
            "send_reply_text": AsyncMock(),
            "send_files": AsyncMock(),
            "send_cost_footer": AsyncMock(),
        }.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)
        # Имя задачи от модели: настоящий claude в тесте не зовётся. Пустой
        # ответ значит «модель не дала имени», имя берётся из первых слов.
        self.named: list[str] = []
        self.model_answer: str | BaseException = ""

        def model_name(words):
            self.named.append(words)
            if isinstance(self.model_answer, BaseException):
                raise self.model_answer
            return self.model_answer

        p = patch.object(tasks, "model_name", model_name)
        p.start()
        self.addCleanup(p.stop)

    async def asyncTearDown(self) -> None:
        for t in self.bg:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self.bg, return_exceptions=True)

    def put_task(self, tid: str = TID, thread: int = THREAD, title: str = ITEM.title, **fields) -> None:
        """Задача из новости, по умолчанию с проектом под «Делай»."""
        digest, n = tid.rsplit("-", 1)
        with tasks.locked(tasks.registry_dir(self.state)) as data:
            _, task = tasks.reserve(data, tid, replace(ITEM, n=int(n), title=title), GROUP,
                                    datetime.now().astimezone(), digest=digest)
            task.update(thread_id=thread, card_id=500, plan=PLAN, plan_sha=SHA, status="proposed")
            task.update(fields)

    def put_command(self, tid: str = "t7-5", thread: int = THREAD, words: str = "обнови README", **fields) -> None:
        """Задача по словам Ивана (trusted, из /task), по умолчанию в обдумывании."""
        with tasks.locked(tasks.registry_dir(self.state)) as data:
            _, task = tasks.reserve(data, tid, tasks.command_item(words), GROUP,
                                    datetime.now().astimezone(), source="command", trusted=True)
            task.update(thread_id=thread, card_id=500, status="thinking")
            task.update(fields)

    def task(self, tid: str = TID) -> dict:
        return tasks.load(tasks.registry_dir(self.state), strict=True)[tid]

    async def press(self, data: str, press_id: str | None = None, settle: bool = True) -> SimpleNamespace:
        # Каждое нажатие со своим id, как у Telegram; press_id: то же нажатие заново.
        self.presses = getattr(self, "presses", 0) + 1
        query = SimpleNamespace(id=press_id or f"q{self.presses}", data=data, answer=AsyncMock(), message=_msg(),
                                edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER), update_id=2)
        await botmod.on_task_pick(update, self.ctx)
        if settle and self.gate is None or (self.gate is not None and self.gate.is_set()):
            await self.settle()
        return query

    async def say(self, text: str, thread: int = THREAD) -> SimpleNamespace:
        """Сообщение Ивана в тему: как on_text, с касанием задачи."""
        msg = self.topic if thread == self.topic.message_thread_id else self.topics.setdefault(thread, _msg(thread))
        msg.text = text
        await botmod.on_text(_update(msg), self.ctx)
        await self.settle()
        return msg

    async def settle(self) -> None:
        """Дождаться фоновых задач, в том числе выполнений, запущенных насосом."""
        while not all(t.done() for t in self.bg):
            await asyncio.gather(*self.bg, return_exceptions=True)

    def topic_texts(self, msg: SimpleNamespace | None = None) -> list[str]:
        return [c.args[0] for c in (msg or self.topic).reply_text.call_args_list]

    def card_texts(self) -> list[str]:
        return [c.args[0] for c in self.bot.edit_message_text.call_args_list]

    def personal(self) -> list[str]:
        """Сообщения в личку (итог задачи)."""
        return [c.args[1] for c in self.bot.send_message.call_args_list if c.args and c.args[0] == USER]

    def do_markups(self, msg: SimpleNamespace | None = None) -> list[list[str]]:
        return [_buttons(c.kwargs["reply_markup"]) for c in (msg or self.topic).reply_text.call_args_list
                if c.kwargs.get("reply_markup") is not None]


def _buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class ThinkTests(_TaskBase):
    """Обдумывание: только чтение, проект под «Делай» лишь у ответа с
    маркером и только если Иван не писал, пока ответ писался."""

    async def test_reply_with_marker_gets_do_button(self) -> None:
        self.put_command()
        self.replies = [f"{PLAN}\n<<DO>>"]
        msg = await self.say("а проверь ещё CHANGELOG")
        run = self.runs[-1]
        self.assertEqual(run["kw"]["permission_mode"], "plan")
        # правила обдумывания напоминаются в каждом ходе
        self.assertIn(tasks.think_note(), run["prompt"])
        task = self.task("t7-5")
        self.assertEqual((task["status"], task["plan"], task["plan_sha"]), ("proposed", PLAN, SHA))
        # Иван маркера не видит, под ответом одна кнопка «Делай»
        sent = botmod.send_reply_text.await_args.args[2]
        self.assertNotIn("<<DO", sent)
        self.assertEqual(self.do_markups(msg), [[f"nt:t7-5:do:{SHA}"]])
        self.assertIn("Проект решения готов", self.card_texts()[-1])

    async def test_marker_only_from_last_answer(self) -> None:
        # Проект с маркером, потом ответ по уведомлению без маркера: проект
        # мог быть отменён, кнопки нет, оба текста видны (ревью Codex 19.10)
        self.put_command()
        self.result = ClaudeResult(text="Проект отменён, нужно уточнение", session_id="s1",
                                   texts=(f"{PLAN}\n<<DO>>", "Проект отменён, нужно уточнение"))
        msg = await self.say("что с README?")
        self.assertEqual(self.task("t7-5")["status"], "thinking")
        self.assertEqual(self.do_markups(msg), [])
        shown = botmod.send_reply_text.await_args.args[2]
        self.assertIn("прочитать README", shown)
        self.assertNotIn("<<DO", shown)
        # маркер в последнем ответе: проект из всего показанного
        self.result = ClaudeResult(text=f"Уточнил.\n{PLAN}\n<<DO>>", session_id="s1",
                                   texts=("Смотрю версии", f"Уточнил.\n{PLAN}\n<<DO>>"))
        msg = await self.say("а так?")
        self.assertEqual(self.task("t7-5")["status"], "proposed")
        self.assertEqual(len(self.do_markups(msg)), 1)

    async def test_questions_without_marker_have_no_button(self) -> None:
        self.put_command()
        self.replies = ["Понял так: X. Вопрос: на маке или на ПК?"]
        msg = await self.say("сделай X")
        self.assertEqual(self.task("t7-5")["status"], "thinking")
        self.assertEqual(self.do_markups(msg), [])

    async def test_message_during_answer_makes_proposal_stale(self) -> None:
        # Иван дописал, пока агент писал проект: проект уже не про последнее
        # слово, кнопки нет, следующий ход даст новый
        self.put_command()
        self.replies = [f"{PLAN}\n<<DO>>"]
        self.gate = asyncio.Event()
        first = asyncio.ensure_future(self.say("сделай X"))
        while not self.runs:
            await asyncio.sleep(0)
        self.assertFalse(await botmod._task_touch(self.ctx, _update(_msg()), "и ещё Y"))
        self.gate.set()
        await first
        self.assertEqual(self.task("t7-5")["status"], "thinking")
        self.assertEqual(self.do_markups(), [])

    async def test_gpt_answer_never_proposes(self) -> None:
        self.cfg.codex_bin, self.cfg.codex_fallback = "codex", True
        self.put_command()
        self.result = ClaudeResult(text="⚠️ API 529", session_id="s1", is_error=True, api_error_status=529)
        self.codex.return_value = ClaudeResult(text=f"{PLAN}\n<<DO>>")
        await self.say("сделай X")
        self.codex.assert_awaited_once()
        self.assertEqual(self.codex.await_args.kwargs["sandbox"], "read-only")
        self.assertEqual(self.task("t7-5")["status"], "thinking")
        self.assertNotIn("<<DO", botmod.send_reply_text.await_args.args[2])

    async def test_ordinary_topic_untouched(self) -> None:
        # обычная тема против задачи: там режим по умолчанию и маркер не разбирается
        self.put_command()
        self.replies = ["готово\n<<DO>>"]
        await self.say("обнови README", thread=78)
        self.assertEqual(self.runs[-1]["kw"]["permission_mode"], "bypassPermissions")
        self.assertNotIn(tasks.think_note(), self.runs[-1]["prompt"])
        self.assertEqual(self.task("t7-5")["status"], "thinking")

    async def test_background_run_in_task_reads_only(self) -> None:
        self.put_task(status="done")
        self.store.set(f"{GROUP}:{THREAD}", "sid-exec")
        await botmod._dispatch_to_claude(_update(_msg()), self.ctx, "событие", send_voice_reply=False,
                                         unattended=True, fresh=True, admitted=True)
        run = self.runs[-1]
        self.assertEqual((run["kw"]["permission_mode"], run["resumed"]), ("plan", "sid-exec"))


class DoTests(_TaskBase):
    """«▶️ Делай» кнопкой и словом, права рана выполнения, итог в личку."""

    async def test_press_runs_exec_and_delivers_result(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.store.set(f"{GROUP}:{THREAD}", "sid-think")
        self.replies = ["✅ Готово\nобновил README, проверил рендер на GitHub"]
        await self.press(f"nt:t7-5:do:{SHA}")
        self.assertEqual(len(self.runs), 1)
        run = self.runs[0]
        self.assertEqual(run["kw"]["permission_mode"], "bypassPermissions")
        # сессия всегда новая, мост только проект (переделка 18.10.2026)
        self.assertIsNone(run["resumed"])
        self.assertIn("прочитать README", run["prompt"])
        self.assertIn("обнови README", run["prompt"])
        # права у рана выполнения, после рана сняты
        self.assertEqual(self.exec_sids_seen, [{"sid-0"}])
        self.assertEqual(self.bot_data["task_exec_sids"], set())
        task = self.task("t7-5")
        self.assertEqual((task["status"], task["result_ok"]), ("done", True))
        # итог в теме, в личку ничего до закрытия (решение Ивана 19.10.2026)
        self.assertIn("проверил рендер", botmod.send_reply_text.await_args.args[2])
        self.assertEqual(self.personal(), [])
        self.assertIn(tasks.STAGE_LINES["done"], self.card_texts()[-1])

    async def test_summary_kept_when_agent_answers_notification_after_it(self) -> None:
        # Разбор задач 19.10.2026: после сводки пришло уведомление фоновой
        # задачи; итог берётся из сводки, в теме видны оба ответа, маркер нет.
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.result = ClaudeResult(text="Фоновая проверка закончилась", session_id="sid-x",
                                   texts=("✅ Готово\nобновил README\n<<DO>>", "Фоновая проверка закончилась"))
        await self.press(f"nt:t7-5:do:{SHA}")
        task = self.task("t7-5")
        self.assertEqual((task["status"], task["result_ok"]), ("done", True))
        self.assertEqual(task["result"], "✅ Готово\nобновил README")
        shown = botmod.send_reply_text.await_args.args[2]
        self.assertIn("обновил README", shown)
        self.assertIn("Фоновая проверка закончилась", shown)
        self.assertNotIn("<<DO", shown)

    async def test_exec_without_summary_is_not_success(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.replies = ["сделал что-то"]
        await self.press(f"nt:t7-5:do:{SHA}")
        task = self.task("t7-5")
        self.assertEqual((task["status"], task["result_ok"]), ("failed", False))
        self.assertEqual(task["result"], tasks.EXEC_UNCONFIRMED)

    async def test_news_exec_in_fresh_session(self) -> None:
        self.put_task()
        self.store.set(f"{GROUP}:{THREAD}", "sid-think")
        await self.press(DO)
        run = self.runs[0]
        self.assertIsNone(run["resumed"])  # в обдумывании был чужой текст
        self.assertIn("https://e.x/1", run["prompt"])
        self.assertNotIn("суть", run["prompt"])

    async def test_word_do_approves_and_does_not_reach_claude(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        msg = await self.say("Да, делай")
        self.assertEqual(len(self.runs), 1)
        self.assertNotIn("Да, делай", self.runs[0]["prompt"])
        self.assertIn("прочитать README", self.runs[0]["prompt"])
        self.assertEqual(self.task("t7-5")["status"], "done")
        self.assertIn("▶️ Выполняю", self.topic_texts(msg))

    async def test_bare_yes_is_not_approval(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        await self.say("да")
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.runs[0]["kw"]["permission_mode"], "plan")
        self.assertEqual(self.task("t7-5")["status"], "thinking")
        # старая кнопка после этого не выполняет
        query = await self.press(f"nt:t7-5:do:{SHA}")
        self.assertIn("уже не действует", query.answer.call_args.args[0])
        self.assertEqual(len(self.runs), 1)

    async def test_correction_then_old_button_stale_new_button_runs(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        new_plan = "Понял так: README и CHANGELOG\n1. оба файла"
        self.replies = [f"{new_plan}\n<<DO>>"]
        await self.say("и CHANGELOG тоже")
        new_sha = tasks.plan_sha(new_plan)
        self.assertEqual(self.task("t7-5")["plan_sha"], new_sha)
        query = await self.press(f"nt:t7-5:do:{SHA}")
        self.assertIn("уже не действует", query.answer.call_args.args[0])
        self.assertEqual(len(self.runs), 1)
        await self.press(f"nt:t7-5:do:{new_sha}")
        self.assertIn("оба файла", self.runs[-1]["prompt"])

    async def test_repeated_do_runs_once(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.gate = asyncio.Event()
        first = asyncio.ensure_future(self.press(f"nt:t7-5:do:{SHA}", press_id="q-1"))
        while not self.runs:
            await asyncio.sleep(0)
        again = await self.press(f"nt:t7-5:do:{SHA}", press_id="q-1")
        self.assertIn("уже принято", again.answer.call_args.args[0])
        other = await self.press(f"nt:t7-5:do:{SHA}", press_id="q-2")
        self.assertIn("уже одобрен", other.answer.call_args.args[0])
        msg = _msg()
        msg.text = "делай"
        self.assertTrue(await botmod._task_touch(self.ctx, _update(msg), "делай") is False)
        self.gate.set()
        await first
        await self.settle()
        self.assertEqual(len(self.runs), 1)

    async def test_failed_exec_keeps_topic_and_says_so(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.replies = ["❌ Не вышло: сервис не поднялся, откатил"]
        await self.press(f"nt:t7-5:do:{SHA}")
        task = self.task("t7-5")
        self.assertEqual((task["status"], task["result_ok"]), ("failed", False))
        self.assertEqual(self.personal(), [])
        later = datetime.now().astimezone() + timedelta(days=2)
        self.assertFalse(tasks.delete_due(task, later))

    async def test_result_kept_when_topic_send_fails(self) -> None:
        # Ответ выполнения не ушёл в тему (сеть): итог в реестре и отдельным
        # сообщением в теме, его больше нигде нет (ревью Codex 16.10 и 19.10)
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.replies = ["✅ Готово\nобновил README"]
        botmod.send_reply_text.side_effect = NetworkError("сеть")
        with self.assertLogs(botmod.log, "ERROR"):
            await self.press(f"nt:t7-5:do:{SHA}")
        task = self.task("t7-5")
        self.assertEqual((task["status"], task["result_ok"]), ("done", True))
        self.assertIn("обновил README", task["result"])
        posts = [c for c in self.bot.send_message.await_args_list
                 if c.kwargs.get("message_thread_id") == THREAD and "обновил README" in c.args[1]]
        self.assertEqual(len(posts), 1)
        self.assertIsNone(task.get("result_unsent"))

    async def test_unsent_result_retried_by_housekeeping(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.replies = ["✅ Готово\nобновил README"]
        botmod.send_reply_text.side_effect = NetworkError("сеть")
        self.bot.send_message.side_effect = NetworkError("сеть")
        with self.assertLogs(botmod.log, "WARNING"):
            await self.press(f"nt:t7-5:do:{SHA}")
        self.assertTrue(self.task("t7-5")["result_unsent"])
        self.bot.send_message.side_effect = None
        # на ходу свежий итог не трогается: он может ещё уходить ответом рана
        await botmod._tasks_housekeeping(self.ctx)
        self.assertTrue(self.task("t7-5")["result_unsent"])
        await botmod._tasks_housekeeping(self.ctx, startup=True)
        self.assertIsNone(self.task("t7-5")["result_unsent"])
        self.assertIn("обновил README", self.bot.send_message.await_args.args[1])

    async def test_summary_before_run_error_is_the_result(self) -> None:
        # Сводка написана, потом ран упал на ответе по уведомлению: работа
        # сделана, итог из сводки, в теме и сводка, и ошибка (ревью Codex 19.10)
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.result = ClaudeResult(text="⛔ claude убит по таймауту (3600s)", session_id="s1", is_error=True,
                                   texts=("✅ Готово\nобновил README",))
        await self.press(f"nt:t7-5:do:{SHA}")
        task = self.task("t7-5")
        self.assertEqual((task["status"], task["result"], task["interrupted"]),
                         ("done", "✅ Готово\nобновил README", False))
        shown = botmod.send_reply_text.await_args.args[2]
        self.assertIn("обновил README", shown)
        self.assertIn("таймауту", shown)

    async def test_exec_timeout_is_failure(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.result = ClaudeResult(text="⛔ claude убит по таймауту (3600s)", session_id="s1", is_error=True)
        await self.press(f"nt:t7-5:do:{SHA}")
        self.assertEqual(self.task("t7-5")["status"], "failed")

    async def test_exec_5xx_does_not_call_gpt(self) -> None:
        self.cfg.codex_bin, self.cfg.codex_fallback = "codex", True
        self.result = ClaudeResult(text="⚠️ API 529", session_id="s1", is_error=True, api_error_status=529)
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        await self.press(f"nt:t7-5:do:{SHA}")
        self.codex.assert_not_awaited()
        self.assertTrue(any("в GPT не передаю" in t for t in self.topic_texts()))
        self.assertEqual(self.task("t7-5")["status"], "failed")

    async def test_message_after_done_thinks_again(self) -> None:
        self.put_command(status="done", delivered="2026-10-15T10:00:00+04:00")
        self.replies = [f"{PLAN}\n<<DO>>"]
        await self.say("а теперь ещё и CHANGELOG")
        self.assertEqual(self.runs[-1]["kw"]["permission_mode"], "plan")
        self.assertEqual(self.task("t7-5")["status"], "proposed")
        # тема, где снова обдумывают, утром не удаляется
        later = datetime.now().astimezone() + timedelta(days=2)
        self.assertFalse(tasks.delete_due(self.task("t7-5"), later))


class SkillMarkerTests(_TaskBase):
    """Скилл в маркере называется под проектом, но без нажатия не выполняется
    ничего: самостоятельные скиллы убраны (решение Ивана 19.10.2026)."""

    async def test_skill_named_and_waits_for_press(self) -> None:
        self.put_command(words="обнови Immich")
        self.replies = [f"{PLAN}\n<<DO:immich-upgrade>>"]
        msg = await self.say("обнови Immich")
        self.assertEqual(len(self.runs), 1)
        task = self.task("t7-5")
        self.assertEqual((task["status"], task["skill"]), ("proposed", "immich-upgrade"))
        self.assertEqual(len(self.do_markups(msg)), 1)
        self.assertTrue(any("по скиллу «immich-upgrade»" in t for t in self.topic_texts(msg)))


class QueueTests(_TaskBase):
    """Одно выполнение на весь бот, очереди нет: «Делай» другой задачи
    отвечает «занято», проект ждёт, кнопка рабочая (разбор 19.10.2026)."""

    async def test_second_is_busy_and_button_works_after(self) -> None:
        self.put_command("t7-5", thread=THREAD, status="proposed", plan=PLAN, plan_sha=SHA, name="1 README")
        self.put_command("t7-6", thread=79, status="proposed", plan=PLAN, plan_sha=SHA, name="2 Immich")
        self.gate = asyncio.Event()
        first = asyncio.ensure_future(self.press(f"nt:t7-5:do:{SHA}"))
        while not self.runs:
            await asyncio.sleep(0)
        query = await self.press(f"nt:t7-6:do:{SHA}", press_id="q-busy")
        self.assertIn("Сейчас выполняется «1 README»", query.answer.call_args.args[0])
        self.assertEqual(self.task("t7-6")["status"], "proposed")
        query.edit_message_reply_markup.assert_not_awaited()  # кнопка остаётся
        self.gate.set()
        await first
        await self.settle()
        self.assertEqual(len(self.runs), 1)  # само ничего не стартует
        # то же нажатие теперь выполняет
        await self.press(f"nt:t7-6:do:{SHA}", press_id="q-busy")
        self.assertEqual([r["chat_key"] for r in self.runs], [f"{GROUP}:{THREAD}", f"{GROUP}:79"])
        self.assertEqual((self.task("t7-5")["status"], self.task("t7-6")["status"]), ("done", "done"))

    async def test_word_do_while_busy_answers_in_topic(self) -> None:
        self.put_command("t7-5", thread=THREAD, status="running", plan=PLAN, plan_sha=SHA, exec_gen=1,
                         exec_started="2026-10-19T10:00:00+04:00", name="1 README")
        self.put_command("t7-6", thread=79, status="proposed", plan=PLAN, plan_sha=SHA)
        msg = await self.say("делай", thread=79)
        self.assertTrue(any("Сейчас выполняется «1 README»" in t for t in self.topic_texts(msg)))
        self.assertEqual(self.task("t7-6")["status"], "proposed")
        self.assertEqual(self.runs, [])

    async def test_message_during_exec_waits(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.gate = asyncio.Event()
        first = asyncio.ensure_future(self.press(f"nt:t7-5:do:{SHA}"))
        while not self.runs:
            await asyncio.sleep(0)
        msg = _msg()
        msg.text = "стоп, не трогай CHANGELOG"
        later = asyncio.ensure_future(botmod.on_text(_update(msg), self.ctx))
        await asyncio.sleep(0.05)
        self.assertTrue(any("ещё работает" in t for t in self.topic_texts(msg)))
        self.gate.set()
        await first
        await later
        await self.settle()
        self.assertEqual([r["kw"]["permission_mode"] for r in self.runs], ["bypassPermissions", "plan"])
        self.assertIn("не трогай CHANGELOG", self.runs[1]["prompt"])

    async def test_message_after_exec_can_propose(self) -> None:
        # Сообщение, дождавшееся конца выполнения, снова обдумывание: его
        # проект получает «Делай», тема не удаляется по прошлому успеху (ревью Codex 16.10)
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.gate = asyncio.Event()
        self.replies = ["✅ Готово", "Понял так: ещё и CHANGELOG\n1. правка\n<<DO>>"]
        first = asyncio.ensure_future(self.press(f"nt:t7-5:do:{SHA}"))
        while not self.runs:
            await asyncio.sleep(0)
        msg = _msg()
        msg.text = "а ещё CHANGELOG"
        later = asyncio.ensure_future(botmod.on_text(_update(msg), self.ctx))
        await asyncio.sleep(0.05)
        self.gate.set()
        await first
        await later
        await self.settle()
        task = self.task("t7-5")
        self.assertEqual(task["status"], "proposed")
        self.assertIn("CHANGELOG", task["plan"])
        self.assertEqual(len(self.do_markups(msg)), 1)

    async def test_correction_before_exec_start_cancels_it(self) -> None:
        # «Делай» нажат, выполнение ждёт замка темы; поправка до старта
        # отменяет одобрение, старый проект не запускается (ревью Codex 16.10)
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        lock = botmod._chat_lock(self.ctx, f"{GROUP}:{THREAD}")
        await lock.acquire()
        await self.press(f"nt:t7-5:do:{SHA}", settle=False)
        self.assertEqual(self.task("t7-5")["status"], "running")
        msg = _msg()
        msg.text = "стоп, не выполняй, сначала покажи diff"
        later = asyncio.ensure_future(botmod.on_text(_update(msg), self.ctx))
        await asyncio.sleep(0.05)
        self.assertTrue(any("одобрение снял" in t for t in self.topic_texts(msg)))
        lock.release()
        await later
        await self.settle()
        self.assertEqual([r["kw"]["permission_mode"] for r in self.runs], ["plan"])
        self.assertEqual(self.task("t7-5")["status"], "thinking")

    async def test_two_waiting_messages_last_one_proposes(self) -> None:
        # Два уточнения ждут замка темы: проект даёт ответ на последнее, а не
        # первый обработчик с чужим номером хода (ревью Codex 16.10)
        self.put_command()
        lock = botmod._chat_lock(self.ctx, f"{GROUP}:{THREAD}")
        await lock.acquire()
        self.replies = ["Понял так: только README\n<<DO>>", "Понял так: README и CHANGELOG\n<<DO>>"]
        one, two = _msg(), _msg()
        one.text, two.text = "только README", "и CHANGELOG тоже"
        first = asyncio.ensure_future(botmod.on_text(_update(one), self.ctx))
        await asyncio.sleep(0.05)
        second = asyncio.ensure_future(botmod.on_text(_update(two), self.ctx))
        await asyncio.sleep(0.05)
        lock.release()
        await first
        await second
        await self.settle()
        task = self.task("t7-5")
        self.assertEqual(task["status"], "proposed")
        self.assertIn("CHANGELOG", task["plan"])
        self.assertEqual(self.do_markups(one), [])
        self.assertEqual(len(self.do_markups(two)), 1)

    async def test_ask_button_answer_drops_proposal(self) -> None:
        # Ответ кнопкой вопроса идёт мимо касания: проект всё равно снимается (ревью Codex 16.10)
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        await botmod._dispatch_to_claude(_update(_msg()), self.ctx, "[Ответ на вопрос «Где?»: на ПК]",
                                         send_voice_reply=False, from_callback=True)
        self.assertEqual(self.task("t7-5")["status"], "thinking")
        query = await self.press(f"nt:t7-5:do:{SHA}")
        self.assertIn("уже не действует", query.answer.call_args.args[0])

    async def test_not_started_waits_for_do_again(self) -> None:
        # Восстановление входа: ран не стартовал, проект снова ждёт «Делай»,
        # насос его по кругу не гоняет; то же нажатие повторно не сработает
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.bot_data["login_manager"] = SimpleNamespace(session=object())
        await self.press(f"nt:t7-5:do:{SHA}", press_id="q-login")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.task("t7-5")["status"], "proposed")
        self.assertTrue(any("не запустилось" in c.args[1] for c in self.bot.send_message.await_args_list))
        self.bot_data["login_manager"] = None
        await self.press(f"nt:t7-5:do:{SHA}")
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.task("t7-5")["status"], "done")

    async def test_not_started_by_restart_waits_for_do_again(self) -> None:
        # Ожидание перезапуска началось между одобрением и раном: проект снова
        # ждёт «Делай», после рестарта само ничего не выполняется
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        real = botmod._task_card

        async def card(ctx, task, *a, **kw):
            if task.get("status") == "running":
                self.bot_data["draining"] = True
            return await real(ctx, task, *a, **kw)

        with patch.object(botmod, "_task_card", card):
            await self.press(f"nt:t7-5:do:{SHA}")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.task("t7-5")["status"], "proposed")
        self.assertTrue(any("из-за рестарта" in t for t in self.card_texts()))
        self.bot_data["draining"] = False
        with patch.dict(os.environ, {"NEWS_TG_CHAT_ID": ""}):
            await botmod._tasks_startup(self.ctx)
        await self.settle()
        self.assertEqual(self.runs, [])
        await self.press(f"nt:t7-5:do:{SHA}")
        self.assertEqual(self.task("t7-5")["status"], "done")

    async def test_draining_refuses_do(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.bot_data["draining"] = True
        query = await self.press(f"nt:t7-5:do:{SHA}")
        self.assertIn("после рестарта", query.answer.call_args.args[0])
        self.assertEqual(self.task("t7-5")["status"], "proposed")


class RaceTests(_TaskBase):
    """Гонки из повторного ревью Codex 16.10."""

    async def test_plan_command_drops_proposal(self) -> None:
        # /plan идёт с unattended, но это ход Ивана: проект снимается
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        await botmod._dispatch_to_claude(_update(_msg()), self.ctx, "/plan другой подход",
                                         send_voice_reply=False, permission_mode="plan", unattended=True)
        self.assertEqual(self.task("t7-5")["status"], "thinking")
        await self.press(f"nt:t7-5:do:{SHA}")
        self.assertEqual([r["kw"]["permission_mode"] for r in self.runs], ["plan"])

    async def test_message_during_topic_delete_is_not_a_task(self) -> None:
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        self.put_command("t7-2", thread=72, status="closed", closed=yesterday, result="y", result_ok=True)
        seen: list = []

        async def delete(chat_id, thread_id):
            # Иван пишет, пока Telegram удаляет тему: это уже не задача
            msg = _msg(72)
            seen.append(await botmod._task_touch(self.ctx, _update(msg), "а ещё X"))
            seen.append(self.task("t7-2")["status"])

        self.bot.delete_forum_topic = AsyncMock(side_effect=delete)
        await botmod._tasks_housekeeping(self.ctx)
        self.assertEqual(seen, [False, "gone"])
        self.assertEqual(self.task("t7-2")["status"], "gone")

    async def test_failed_delete_restores_task(self) -> None:
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        self.put_command("t7-2", thread=72, status="closed", closed=yesterday, result="y", result_ok=True)
        self.bot.delete_forum_topic = AsyncMock(side_effect=NetworkError("сеть"))
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_housekeeping(self.ctx)
        self.assertEqual(self.task("t7-2")["status"], "closed")

    async def test_slow_card_does_not_reorder_messages(self) -> None:
        # Карточка первого уточнения задержалась: второе всё равно идёт вторым,
        # проект по последнему слову
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        real = botmod._task_card

        async def slow(ctx, task, *a, **kw):
            await asyncio.sleep(0.1)
            return await real(ctx, task, *a, **kw)

        self.replies = ["Понял так: только README\n<<DO>>", "Понял так: README и CHANGELOG\n<<DO>>"]
        one, two = _msg(), _msg()
        one.text, two.text = "только README", "и CHANGELOG тоже"
        with patch.object(botmod, "_task_card", slow):
            first = asyncio.ensure_future(botmod.on_text(_update(one), self.ctx))
            second = asyncio.ensure_future(botmod.on_text(_update(two), self.ctx))
            await first
            await second
            await self.settle()
        self.assertEqual([r["prompt"].split("\n")[0] for r in self.runs], ["только README", "и CHANGELOG тоже"])
        task = self.task("t7-5")
        self.assertEqual(task["status"], "proposed")
        self.assertIn("CHANGELOG", task["plan"])

    async def test_ask_button_drops_proposal_before_any_await(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.bot_data["ask_options"] = {"tok": {"chat_key": f"{GROUP}:{THREAD}", "answer": "на ПК",
                                                "question": "Где?", "card": 1,
                                                "epoch": botmod._session_epoch(self.ctx, f"{GROUP}:{THREAD}"),
                                                "permission_mode": "plan"}}
        statuses: list = []

        async def answer(*_a, **_kw):
            statuses.append(self.task("t7-5")["status"])
            await botmod._task_do(self.ctx, "t7-5", SHA)  # «Делай» в это окно не выполняет снятое

        card = _msg()
        card.text = "Где?"
        card.chat = SimpleNamespace(id=GROUP, type="supergroup")
        query = SimpleNamespace(data="ask:tok", answer=AsyncMock(side_effect=answer), message=card,
                                edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
        await botmod.on_ask_pick(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER),
                                                 update_id=3), self.ctx)
        await self.settle()
        self.assertEqual(statuses, ["thinking"])
        self.assertEqual([r["kw"]["permission_mode"] for r in self.runs], ["plan"])

    async def test_message_while_exec_result_goes_out(self) -> None:
        # Замок темы уже свободен, итог выполнения ещё отправляется: новое
        # сообщение видит done и даёт проект с кнопкой
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.replies = ["✅ Готово", "Понял так: ещё CHANGELOG\n<<DO>>"]
        msg = _msg()
        msg.text = "а теперь CHANGELOG"
        sent: list = []

        async def send(cfg, update, text, *a, **kw):
            sent.append(text)
            if text == "✅ Готово":
                await botmod.on_text(_update(msg), self.ctx)

        botmod.send_reply_text.side_effect = send
        await self.press(f"nt:t7-5:do:{SHA}")
        task = self.task("t7-5")
        self.assertEqual(task["status"], "proposed")
        self.assertIn("CHANGELOG", task["plan"])
        self.assertEqual(len(self.do_markups(msg)), 1)
        self.assertEqual(task["result"], "✅ Готово")


    async def test_close_note_replies_to_ivan_once(self) -> None:
        # Задача из лички после «Закрыть»: одна строка итога ответом на слова
        # Ивана; уборка сразу после закрытия её не повторяет (решение Ивана 19.10.2026)
        self.put_command("t7-3", thread=73, status="done", result="✅ Готово\nобновил README", result_ok=True,
                         exec_started="2026-10-19T10:00:00+04:00", name="1 README",
                         origin={"chat_id": USER, "thread_id": None, "ask_id": 41})
        await self.press("nt:t7-3:close")
        await botmod._tasks_housekeeping(self.ctx)
        notes = [c for c in self.bot.send_message.await_args_list if c.args and c.args[0] == USER]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].args[1], "✅ Задача «1 README»: обновил README")
        self.assertEqual(notes[0].kwargs["reply_parameters"].message_id, 41)
        task = self.task("t7-3")
        self.assertEqual(task["status"], "closed")
        self.assertIsNone(task["note_due"])
        self.assertTrue(task["noted"])

    async def test_close_note_only_for_private_chat_tasks(self) -> None:
        self.put_task(status="done", result="✅ Готово", result_ok=True)
        self.put_command("t7-4", thread=74, status="done", result="✅ Готово", result_ok=True,
                         origin={"chat_id": GROUP, "thread_id": 5, "ask_id": 41})
        await self.press(f"nt:{TID}:close")
        await self.press("nt:t7-4:close")
        self.assertEqual(self.personal(), [])
        self.assertIsNone(self.task("t7-4")["note_due"])

    async def test_close_note_retried_by_housekeeping(self) -> None:
        self.put_command("t7-3", thread=73, status="done", result="❌ Не вышло: упал", result_ok=False,
                         exec_started="2026-10-19T10:00:00+04:00", name="1 README",
                         origin={"chat_id": USER, "thread_id": None, "ask_id": 41})
        fails = [NetworkError("сеть")]

        async def send(chat_id, text, **kw):
            if chat_id == USER and fails:
                raise fails.pop()

        self.bot.send_message.side_effect = send
        with self.assertLogs(botmod.log, "WARNING"):
            await self.press("nt:t7-3:close")
        self.assertTrue(self.task("t7-3")["note_due"])
        # через 10 минут уборка повторяет; до утра тема ещё не удалена
        with tasks.locked(tasks.registry_dir(self.state)) as data:
            data["t7-3"]["closed"] = (datetime.now().astimezone() - timedelta(minutes=11)).isoformat()
        await botmod._tasks_housekeeping(self.ctx)
        self.assertIsNone(self.task("t7-3")["note_due"])
        sent = [c.args[1] for c in self.bot.send_message.await_args_list if c.args and c.args[0] == USER]
        self.assertEqual(sent[-1], "❌ Задача «1 README»: упал")

    async def test_close_note_claimed_by_one_sender(self) -> None:
        # Уборка, пока строка уже отправляется кнопкой, её второй раз не шлёт
        self.put_command("t7-3", thread=73, status="done", result="✅ Готово", result_ok=True,
                         exec_started="2026-10-19T10:00:00+04:00", name="1 README",
                         origin={"chat_id": USER, "thread_id": None, "ask_id": 41})
        gate = asyncio.Event()

        async def send(chat_id, text, **kw):
            if chat_id == USER:
                await gate.wait()

        self.bot.send_message.side_effect = send
        closing = asyncio.ensure_future(self.press("nt:t7-3:close"))
        for _wait in range(1000):
            if self.task("t7-3").get("note_sending"):
                break
            await asyncio.sleep(0)
        await botmod._tasks_housekeeping(self.ctx)
        gate.set()
        await closing
        notes = [c for c in self.bot.send_message.await_args_list if c.args and c.args[0] == USER]
        self.assertEqual(len(notes), 1)
        self.assertIsNone(self.task("t7-3")["note_due"])

    async def test_close_note_given_up_after_tries(self) -> None:
        self.put_command("t7-3", thread=73, status="closed", closed="2026-10-19T10:00:00+04:00",
                         note_due=True, origin={"chat_id": USER, "thread_id": None, "ask_id": 41})
        self.bot.send_message.side_effect = NetworkError("сеть")
        with self.assertLogs(botmod.log, "WARNING"):
            for _try in range(tasks.NOTE_TRIES):
                await botmod._task_note(self.ctx, self.task("t7-3"))
        task = self.task("t7-3")
        self.assertEqual((task["note_due"], task["note_tries"], task["note_sending"]), (None, tasks.NOTE_TRIES, None))

    async def test_restart_mid_delete_retries(self) -> None:
        self.put_command("t7-2", thread=72, status="gone", gone="2026-10-16T08:45:00+04:00", deleting="done")
        await botmod._tasks_housekeeping(self.ctx)
        self.bot.delete_forum_topic.assert_awaited_once_with(GROUP, 72)
        self.assertIsNone(self.task("t7-2").get("deleting"))

    async def test_failed_delete_retry_restores_status(self) -> None:
        self.put_command("t7-2", thread=72, status="gone", gone="2026-10-16T08:45:00+04:00", deleting="done")
        self.bot.delete_forum_topic = AsyncMock(side_effect=NetworkError("сеть"))
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_housekeeping(self.ctx)
        task = self.task("t7-2")
        self.assertEqual((task["status"], task.get("deleting")), ("done", None))

    async def test_busy_topic_not_deleted(self) -> None:
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        self.put_command("t7-2", thread=72, status="closed", closed=yesterday)
        lock = botmod._chat_lock(self.ctx, f"{GROUP}:72")
        async with lock:
            await botmod._tasks_housekeeping(self.ctx)
        self.bot.delete_forum_topic.assert_not_awaited()
        self.assertEqual(self.task("t7-2")["status"], "closed")


class RecoverTests(_TaskBase):
    async def test_crash_mid_exec_leaves_failed_topic(self) -> None:
        self.put_command("t7-5", thread=THREAD, status="running", exec_gen=1,
                         exec_started="2026-10-16T10:00:00+04:00")
        self.put_command("t7-6", thread=79, status="running", exec_gen=1)
        await botmod._tasks_recover(self.ctx)
        a, b = self.task("t7-5"), self.task("t7-6")
        self.assertEqual((a["status"], a["result_ok"]), ("failed", False))
        self.assertIn("прервал рестарт", a["result"])
        self.assertEqual(self.personal(), [])
        # не стартовавшее снова ждёт «Делай» с новой кнопкой, повтора вслепую
        # нет (ревью Codex 15.10)
        self.assertEqual(b["status"], "proposed")
        self.assertEqual(self.runs, [])
        self.assertTrue(any("не запустилось" in c.args[1] for c in self.bot.send_message.await_args_list))

    async def test_thinking_cut_by_crash_is_reported(self) -> None:
        # Разбор 19.10.2026: OOM оборвал обдумывание, тема молчала 7,5 часа
        self.put_command("t7-5", thread=THREAD, status="thinking", think_open=True)
        self.put_command("t7-6", thread=79, status="thinking")
        await botmod._tasks_recover(self.ctx)
        said = [(c.kwargs.get("message_thread_id"), c.args[1]) for c in self.bot.send_message.await_args_list]
        self.assertIn((THREAD, tasks.THINK_INTERRUPTED), said)
        self.assertNotIn((79, tasks.THINK_INTERRUPTED), said)
        self.assertIsNone(self.task("t7-5").get("think_open"))

    async def test_cut_thinking_notice_kept_until_sent(self) -> None:
        # Сеть упала на предупреждении: отметка остаётся до следующего старта
        self.put_command("t7-5", thread=THREAD, status="thinking", think_open=True)
        self.bot.send_message.side_effect = NetworkError("сеть")
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_recover(self.ctx)
        self.assertTrue(self.task("t7-5")["think_open"])

    async def test_lost_do_button_comes_back_on_next_start(self) -> None:
        # Рестарт посреди одобрения: проект снова ждёт «Делай»; кнопка не
        # ушла (сеть), её пришлёт следующий старт (ревью Codex 19.10)
        self.put_command("t7-5", thread=THREAD, status="running", plan=PLAN, plan_sha=SHA, exec_gen=1)
        self.bot.send_message.side_effect = NetworkError("сеть")
        with self.assertLogs(botmod.log, "WARNING"):
            await botmod._tasks_recover(self.ctx)
        self.assertEqual((self.task("t7-5")["status"], self.task("t7-5")["offer_due"]), ("proposed", True))
        self.bot.send_message.side_effect = None
        await botmod._tasks_recover(self.ctx)
        self.assertIsNone(self.task("t7-5")["offer_due"])
        self.assertEqual(_buttons(self.bot.send_message.await_args.kwargs["reply_markup"]), [f"nt:t7-5:do:{SHA}"])

    async def test_thinking_mark_lives_only_during_run(self) -> None:
        self.put_command()
        seen: list = []
        real = botmod.run_claude

        async def run(*a, **kw):
            seen.append(self.task("t7-5").get("think_open"))
            return await real(*a, **kw)

        with patch.object(botmod, "run_claude", run):
            await self.say("что там с README?")
        self.assertEqual(seen, [self.task("t7-5")["rev"]])
        self.assertIsNone(self.task("t7-5").get("think_open"))

    async def test_next_turn_mark_survives_previous_cleanup(self) -> None:
        # Первый ход снимает только свою отметку: следующий мог уже поставить
        # свою, и её авария должна остаться видна (ревью Codex 19.10)
        self.put_command()
        real = botmod.run_claude

        async def run(*a, **kw):
            with tasks.locked(tasks.registry_dir(self.state)) as data:
                data["t7-5"]["think_open"] = 99  # отметка следующего хода
            return await real(*a, **kw)

        with patch.object(botmod, "run_claude", run):
            await self.say("что там с README?")
        self.assertEqual(self.task("t7-5")["think_open"], 99)

    async def test_crash_before_reply_posts_result_on_start(self) -> None:
        # Авария после записи итога, до ответа в тему: старт присылает итог
        self.put_command("t7-5", thread=THREAD, status="done", result="✅ Готово\nобновил README",
                         result_ok=True, result_unsent=True, result_at="2026-10-19T10:00:00+04:00")
        with patch.dict(os.environ, {"NEWS_TG_CHAT_ID": ""}):
            await botmod._tasks_startup(self.ctx)
        self.assertIn("обновил README", self.bot.send_message.await_args.args[1])
        self.assertIsNone(self.task("t7-5")["result_unsent"])
class HousekeepingTests(_TaskBase):
    async def test_delete_only_closed_next_morning(self) -> None:
        # выполненная и ошибка сами не удаляются, закрытая вчера уходит
        # (решение Ивана 19.10.2026)
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        self.put_command("t7-1", thread=71, status="closed", closed=yesterday, result="ok", result_ok=True)
        self.put_command("t7-2", thread=72, status="failed", result="x", result_ok=False)
        self.put_command("t7-3", thread=73, status="done", result="✅ Готово", result_ok=True)
        await botmod._tasks_housekeeping(self.ctx)
        deleted = [c.args[1] for c in self.bot.delete_forum_topic.await_args_list]
        self.assertEqual(deleted, [71])
        self.assertEqual(self.task("t7-1")["status"], "gone")
        self.assertEqual(self.task("t7-2")["status"], "failed")
        self.assertEqual(self.task("t7-3")["status"], "done")
        self.assertEqual(self.personal(), [])

    async def test_topic_written_during_housekeeping_kept(self) -> None:
        # Пока уборка повторяла строку другой задачи, Иван вернул вчерашнюю
        # закрытую тему в работу: она не удаляется (ревью Codex 16.10)
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        self.put_command("t7-1", thread=71, status="closed", closed=yesterday, note_due=True,
                         origin={"chat_id": USER, "thread_id": None, "ask_id": 41})
        self.put_command("t7-2", thread=72, status="closed", closed=yesterday, result="y", result_ok=True)
        real = botmod._task_note

        async def note(ctx, task):
            if task["id"] == "t7-1":
                with tasks.locked(tasks.registry_dir(self.state)) as data:
                    data["t7-2"]["status"] = "thinking"
            return await real(ctx, task)

        with patch.object(botmod, "_task_note", note):
            await botmod._tasks_housekeeping(self.ctx)
        deleted = [c.args[1] for c in self.bot.delete_forum_topic.await_args_list]
        self.assertEqual(deleted, [71])
        self.assertEqual(self.task("t7-2")["status"], "thinking")

    async def test_close_button(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        await self.press("nt:t7-5:close")
        self.assertEqual(self.task("t7-5")["status"], "closed")
        self.bot.close_forum_topic.assert_awaited_once_with(GROUP, THREAD)
        self.assertIn("Задача закрыта", self.card_texts()[-1])
        query = await self.press(f"nt:t7-5:do:{SHA}")
        self.assertIn("устарела", query.answer.call_args.args[0])
        self.assertEqual(self.runs, [])

    async def test_old_buttons_say_outdated(self) -> None:
        self.put_task(status="thinking")
        for data in (f"nt:{TID}:ok:{SHA}:bot", f"nt:{TID}:plan", f"nt:{TID}:done"):
            query = await self.press(data)
            self.assertIn("устарела", query.answer.call_args.args[0])
        self.assertEqual(self.task()["status"], "thinking")

    async def test_unreadable_registry_keeps_buttons(self) -> None:
        self.put_task()
        tasks.tasks_path(tasks.registry_dir(self.state)).write_text("{битый", encoding="utf-8")
        query = await self.press(DO)
        self.assertIn("Реестр задач не читается", query.answer.call_args.args[0])
        query.edit_message_reply_markup.assert_not_awaited()
        with self.assertLogs(botmod.log, "ERROR"):
            await botmod._tasks_housekeeping(self.ctx)
        with self.assertRaises(ValueError):
            await botmod._tasks_recover(self.ctx)


class ExecRightsTests(unittest.TestCase):
    """Хук: ран выполнения без карточек, кроме секретов и отправки наружу;
    тот же sid после рана снова на карточках (ревью Codex 15.10)."""

    def decide(self, tool: str, inp: dict, exec_sids=frozenset({"sid"})) -> tuple[dict, AsyncMock]:
        import re
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(
                approve_shared_secret="", primary_chat_id=1, state_dir=Path(d),
                approve_auto_chat_keys=frozenset(), approve_whitelist_paths=(),
                mcp_auto_allow_re=re.compile(r"get[A-Za-z_]*"), bash_auto_allow_re=re.compile(r"ls"),
            )
            bot_data = {"unattended_sids": set(), "untrusted_sids": set(), "burst_until_by_target": {},
                        "task_exec_sids": set(exec_sids)}
            request = SimpleNamespace(
                app={"cfg": cfg, "application": SimpleNamespace(bot_data=bot_data),
                     "registry": approval.ApprovalRegistry(), "session_targets": {"sid": (GROUP, THREAD)}},
                headers={}, remote="127.0.0.1",
                json=AsyncMock(return_value={"tool_name": tool, "tool_input": inp, "session_id": "sid",
                                             "permission_mode": "bypassPermissions", "cwd": d}),
            )
            card = AsyncMock(side_effect=OSError)
            with patch.object(approval, "send_approval_request", card), \
                    patch.object(approval.asyncio, "sleep", AsyncMock()):
                resp = asyncio.run(approval.handle_approve(request))
            return json.loads(resp.text), card

    def test_exec_run_goes_without_cards(self) -> None:
        for tool, inp in (("Bash", {"command": "docker compose pull && docker compose up -d"}),
                          ("Bash", {"command": "ssh immich 'docker ps'"}),
                          ("Edit", {"file_path": "/home/u/src/x/README.md", "old_string": "a",
                                    "new_string": "b"})):
            out, card = self.decide(tool, inp)
            self.assertEqual(out["decision"], "allow", inp)
            card.assert_not_called()

    def test_secret_and_send_still_card(self) -> None:
        for tool, inp in (("Bash", {"command": "cat ~/.env"}),
                          ("mcp__telegram__send_message", {"chat_id": "@x", "message": "hi"})):
            out, card = self.decide(tool, inp)
            self.assertEqual(out["decision"], "deny", tool)
            card.assert_called()

    def test_self_restart_refused(self) -> None:
        out, card = self.decide("Bash", {"command": "systemctl --user restart claude-tg-bot"})
        self.assertEqual(out["decision"], "deny")
        card.assert_not_called()

    def test_same_session_outside_exec_cards(self) -> None:
        out, card = self.decide("Bash", {"command": "docker compose up -d"}, exec_sids=frozenset())
        self.assertEqual(out["decision"], "deny")
        card.assert_called()


class CommandTests(_TaskBase):
    """Входы: /task, ответ на подсказку, Домовой через POST /task."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        env = patch.dict(os.environ, {"NEWS_TG_CHAT_ID": str(GROUP)})
        env.start()
        self.addCleanup(env.stop)
        self.bot.id = BOT_ID
        self.bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=NEW_THREAD))
        # Значков тем у Telegram в тесте нет: цвет, в имени только номер.
        self.bot.get_forum_topic_icon_stickers = AsyncMock(return_value=())
        self.bot.pin_chat_message = AsyncMock()
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=500))
        # обдумывание идёт в новую тему
        self.topic = _msg(NEW_THREAD)
        self.ask = SimpleNamespace(from_user=SimpleNamespace(id=BOT_ID, is_bot=True), text=tasks.ASK)

    def command(self, text: str, *, chat_id: int = GROUP, thread: int | None = 55, message_id: int = 31,
                reply_to=None, forward=None) -> SimpleNamespace:
        msg = _msg(thread)
        msg.text, msg.chat_id, msg.message_id = text, chat_id, message_id
        msg.reply_to_message, msg.forward_origin = reply_to, forward
        msg.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=900))
        upd = _update(msg)
        upd.effective_chat = SimpleNamespace(id=chat_id, type="private" if chat_id > 0 else "supergroup")
        return upd

    def registry(self) -> dict:
        return tasks.load(tasks.registry_dir(self.state), strict=True)

    async def test_run_gets_ivan_message_for_task_new(self) -> None:
        # Ран в личке получает в окружение сообщение Ивана, на которое
        # ответит строка итога задачи из task_new; у пробуждения и ответа
        # кнопкой своего сообщения Ивана нет (ревью Codex 19.10)
        upd = self.command("обнови Immich", chat_id=USER, thread=None, message_id=77)
        await botmod._dispatch_to_claude(upd, self.ctx, "обнови Immich", send_voice_reply=False)
        await botmod._dispatch_to_claude(upd, self.ctx, "пробуждение", send_voice_reply=False, unattended=True)
        await botmod._dispatch_to_claude(upd, self.ctx, "[Ответ на вопрос]", send_voice_reply=False,
                                         from_callback=True)
        self.assertEqual([r["kw"]["ask_id"] for r in self.runs], [77, None, None])

    async def test_task_opens_topic_and_thinks_without_buttons(self) -> None:
        self.replies = ["Понял так: поправлю README; проверю рендер. Вопрос: какой раздел?"]
        upd = self.command("/task поправь README бота\nподробности", chat_id=USER, thread=None)
        await botmod.on_task(upd, self.ctx)
        await self.settle()
        self.assertEqual(self.bot.create_forum_topic.await_args.args, (GROUP, "1 поправь README бота"))
        task = self.task(f"t{USER}-31")
        self.assertEqual((task["source"], task["trusted"], task["thread_id"], task["status"]),
                         ("command", True, NEW_THREAD, "thinking"))
        self.assertEqual(task["origin"], {"chat_id": USER, "thread_id": None, "ask_id": 31, "message_id": 900})
        key = f"{GROUP}:{NEW_THREAD}"
        self.assertEqual(self.bot_data["scopes"].get(key), tasks.SCOPE_NOTE)
        edit = next(c for c in self.bot.edit_message_text.await_args_list if c.kwargs.get("message_id") == 900)
        self.assertEqual(edit.args[0], f"Задача «1 поправь README бота»: {tasks.topic_link(GROUP, NEW_THREAD)}")
        # обдумывание сразу, только чтение, ни одной кнопки кроме «Закрыть»
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.runs[0]["kw"]["permission_mode"], "plan")
        self.assertTrue(self.runs[0]["prompt"].startswith("Владелец поставил задачу. Его слова:\nпоправь README бота"))
        card = self.bot.send_message.await_args_list[0]
        self.assertEqual(_buttons(card.kwargs["reply_markup"]), [f"nt:t{USER}-31:close"])
        self.assertEqual(self.do_markups(), [])

    async def test_intro_with_marker_proposes(self) -> None:
        self.replies = [f"{PLAN}\n<<DO>>"]
        await botmod.on_task(self.command("/task обнови README"), self.ctx)
        await self.settle()
        task = self.task(f"t{abs(GROUP)}-31")
        self.assertEqual(task["status"], "proposed")
        self.assertEqual(self.do_markups(), [[f"nt:t{abs(GROUP)}-31:do:{SHA}"]])

    async def test_double_delivery_gives_one_topic(self) -> None:
        upd = self.command("/task обнови зависимости")
        await botmod.on_task(upd, self.ctx)
        await botmod.on_task(upd, self.ctx)
        await self.settle()
        self.bot.create_forum_topic.assert_awaited_once()
        self.assertEqual(list(self.registry()), [f"t{abs(GROUP)}-31"])

    async def test_limit_lists_open_tasks_with_stages(self) -> None:
        for n, status in enumerate(("thinking", "proposed", "running", "creating", "failed"), 1):
            self.put_task(f"202610131008-{n}", thread=70 + n, status=status)
        upd = self.command("/task ещё одна")
        await botmod.on_task(upd, self.ctx)
        self.bot.create_forum_topic.assert_not_awaited()
        text = self.bot.edit_message_text.await_args.args[0]
        self.assertIn(f"Открыто {tasks.MAX_OPEN} задач", text)
        for stage in ("обдумывание", "ждёт «Делай»", "выполняется", "тема заводится", "не вышло"):
            self.assertIn(stage, text)

    async def test_empty_task_asks_and_text_answer_creates(self) -> None:
        upd = self.command("/task")
        await botmod.on_task(upd, self.ctx)
        self.assertEqual(upd.message.reply_text.await_args.args[0], tasks.ASK)
        answer = self.command("почисти логи", reply_to=self.ask)
        await botmod.on_text(answer, self.ctx)
        await self.settle()
        self.bot.create_forum_topic.assert_awaited_once()

    async def test_domovoy_http_entry(self) -> None:
        # Домовой передаёт исходные слова и пояснение; повтор с тем же ключом
        # второй темы не заводит (ревью Codex 15.10)
        app = SimpleNamespace(bot_data=self.bot_data, create_task=self.ctx.application.create_task,
                              running=False, bot=self.bot)

        self.bot_data["task_new"] = lambda *a, **kw: botmod._task_from_http(self.ctx, *a, **kw)
        request = SimpleNamespace(
            app={"cfg": SimpleNamespace(approve_shared_secret=""), "application": app},
            headers={}, remote="127.0.0.1",
            json=AsyncMock(return_value={"words": "обнови Immich", "context": "Иван за рулём", "key": "k1",
                                         "chat_key": str(USER), "ask_id": 77}))
        resp = await approval.handle_task(request)
        await self.settle()
        body = json.loads(resp.text)
        self.assertEqual(body["text"], f"Задача «1 обнови Immich»: {tasks.topic_link(GROUP, NEW_THREAD)}")
        task = self.task("hk1")
        self.assertEqual((task["source"], task["trusted"], task["context"]), ("command", True, "Иван за рулём"))
        # строка итога после закрытия ответит на сообщение Ивана 77 в личке
        self.assertEqual(task["origin"], {"chat_id": USER, "thread_id": None, "ask_id": 77})
        self.assertEqual(tasks.note_target(task, USER), (USER, 77))
        prompt = self.runs[0]["prompt"]
        self.assertTrue(prompt.startswith("Владелец поставил задачу. Его слова:\nобнови Immich"))
        self.assertIn("Иван за рулём", prompt)
        again = json.loads((await approval.handle_task(request)).text)
        self.assertIn("уже заведена", again["text"])
        self.bot.create_forum_topic.assert_awaited_once()
        request.json = AsyncMock(return_value={"words": "  "})
        self.assertEqual((await approval.handle_task(request)).status, 400)
        # чужие chat_key и ask_id не проходят: личка без ответа
        request.json = AsyncMock(return_value={"words": "почисти логи", "key": "k2",
                                               "chat_key": "1; rm", "ask_id": True})
        await approval.handle_task(request)
        await self.settle()
        self.assertEqual(self.task("hk2")["origin"], {"chat_id": USER, "thread_id": None, "ask_id": None})


class VoiceTests(_TaskBase):
    async def test_voice_do_approves(self) -> None:
        self.put_command(status="proposed", plan=PLAN, plan_sha=SHA)
        self.bot_data["openai"] = object()
        msg = _msg()
        msg.voice = SimpleNamespace(duration=2, get_file=AsyncMock(return_value=SimpleNamespace(
            download_to_drive=AsyncMock())))
        with patch.object(botmod, "transcribe", AsyncMock(return_value="Да, делай.")):
            await botmod.on_voice(_update(msg), self.ctx)
        await self.settle()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.runs[0]["kw"]["permission_mode"], "bypassPermissions")
        self.assertEqual(self.task("t7-5")["status"], "done")


class CostTests(_TaskBase):
    """Расход задачи (проект шага 3, 1.6): максимум по сессии, сумма по
    сессиям, строка карточки и футера; одно сообщение со звуком за порогом,
    работа не останавливается (решение Ивана 13.10.2026, вопрос 2б)."""

    async def talk(self, sid: str, cost: float, text: str = "вопрос", thread: int = THREAD, **kw) -> None:
        self.result = ClaudeResult(text="ok", session_id=sid, session_cost_usd=cost, **kw)
        msg = self.topic if thread == THREAD else _msg(thread)
        await botmod._dispatch_to_claude(_update(msg), self.ctx, text, send_voice_reply=False)

    @staticmethod
    def footer_notes() -> list[str]:
        return [c.args[3] for c in botmod.send_cost_footer.await_args_list]

    async def test_session_max_and_exec_counted(self) -> None:
        self.put_task(status="thinking", plan="", plan_sha="")
        await self.talk("sid-talk", 0.5)
        await self.talk("sid-talk", 0.8)
        self.assertEqual(self.task()["cost"], {"sid-talk": 0.8})
        self.assertEqual(self.footer_notes()[-1], "задача ≈$0.80")
        self.assertTrue(self.card_texts()[-1].endswith("💸 расход около $0.80"))
        edits = len(self.card_texts())
        await self.talk("sid-talk", 0.8)
        self.assertEqual((self.task()["cost"], len(self.card_texts())), ({"sid-talk": 0.8}, edits))
        # выполнение новости в свежей сессии считается отдельно
        with tasks.locked(tasks.registry_dir(self.state)) as data:
            data[TID].update(status="proposed", plan=PLAN, plan_sha=SHA)
        self.result = ClaudeResult(text="✅ Готово", session_id="sid-exec", session_cost_usd=0.3)
        await self.press(DO)
        task = self.task()
        self.assertEqual((task["status"], task["cost"]), ("done", {"sid-talk": 0.8, "sid-exec": 0.3}))
        self.assertEqual(self.footer_notes()[-1], "задача ≈$1.10")
        # вне темы задачи расход никуда не пишется
        await self.talk("sid-other", 2.0, thread=78)
        self.assertEqual(self.task()["cost"], {"sid-talk": 0.8, "sid-exec": 0.3})
        self.assertEqual(self.footer_notes()[-1], "")

    async def test_no_threshold_warning(self) -> None:
        # Предупреждение $5 убрано, строка расхода осталась (решение Ивана 19.10.2026)
        self.put_task(status="thinking")
        await self.talk("sid-1", 5.2)
        self.assertEqual([c for c in self.bot.send_message.await_args_list if "$5" in str(c.args)], [])
        self.assertIn("около $5.20", self.card_texts()[-1])

class CardTests(_TaskBase):
    async def test_card_edit_waits_out_flood_control(self) -> None:
        self.put_task(status="thinking")
        self.bot.edit_message_text.side_effect = [RetryAfter(3), None]
        with patch("claude_tg_bot.replies.asyncio.sleep", AsyncMock()) as sleep:
            ok = await botmod._task_card(self.ctx, self.task())
        self.assertTrue(ok)
        self.assertEqual(self.bot.edit_message_text.await_count, 2)
        sleep.assert_awaited_once_with(4.0)

    async def test_stale_card_edit_not_retried_after_close(self) -> None:
        self.put_task(status="done")
        task = self.task()

        async def edit(*_a, **_kw):
            if self.bot.edit_message_text.await_count == 1:
                with tasks.locked(tasks.registry_dir(self.state)) as data:
                    data[TID]["status"] = "closed"
                raise RetryAfter(3)

        self.bot.edit_message_text.side_effect = edit
        with patch("claude_tg_bot.replies.asyncio.sleep", AsyncMock()):
            ok = await botmod._task_card(self.ctx, task)
        self.assertTrue(ok)
        self.assertEqual(self.bot.edit_message_text.await_count, 1)

    async def test_creating_answers_already_creating(self) -> None:
        self.put_task(status="creating", thread=None)
        query = SimpleNamespace(answer=AsyncMock(), message=_msg(thread=None))
        await botmod._task_start(query, self.cfg, self.ctx, "202610131008", ITEM, datetime.now().astimezone())
        self.assertIn("уже заводится", query.answer.call_args.args[0])


class _RunnerStdin:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def write(self, _data: bytes) -> None:
        self.order.append("prompt")

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _RunnerStdout:
    def __init__(self, *results: str) -> None:
        self.lines = [json.dumps({"type": "result", "result": r, "session_id": "s"}).encode() + b"\n"
                      for r in results or ("ok",)]

    async def readuntil(self, _sep: bytes = b"\n") -> bytes:
        if not self.lines:
            raise asyncio.IncompleteReadError(b"", None)
        return self.lines.pop(0)


class RunStartMarkTests(unittest.IsolatedAsyncioTestCase):
    """Отметка старта рана в run_claude: после создания процесса и до подачи
    промпта, один раз; её ошибка гасит процесс до промпта (ревью Codex 14.10)."""

    async def run_with(self, mark, results: tuple[str, ...] = ()) -> ClaudeResult:
        self.order: list[str] = []
        self.procs: dict = {}

        async def create(*_a, **_kw):
            self.order.append("process")
            return SimpleNamespace(pid=1, returncode=0, stdin=_RunnerStdin(self.order),
                                   stdout=_RunnerStdout(*results),
                                   stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
                                   wait=AsyncMock(return_value=0))

        store = SimpleNamespace(get=lambda k: None, set=lambda k, v: None, remove=lambda k: None)
        with patch.object(claude_runner.asyncio, "create_subprocess_exec", create), \
                patch.object(claude_runner, "_ensure_hook_still_valid", lambda *_: None), \
                patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()), \
                patch.object(claude_runner, "kill_process_tree", lambda proc, sig: self.order.append("kill")), \
                patch.object(claude_runner, "_reap_proc", AsyncMock()):
            return await claude_runner.run_claude("claude", "k", 1, None, "x", store, cwd=Path("/tmp"),
                                                  hook_settings=Path("/tmp/x.json"), process_registry=self.procs,
                                                  on_run_start=mark)

    async def test_every_result_text_kept(self) -> None:
        # Ответ на уведомление фоновой задачи после главного: text последний,
        # texts все по порядку (разбор задач 19.10.2026).
        result = await self.run_with(None, ("✅ Готово", "  ", "Фон кончился"))
        self.assertEqual(result.text, "Фон кончился")
        self.assertEqual(result.texts, ("✅ Готово", "Фон кончился"))

    async def test_mark_after_process_before_prompt(self) -> None:
        await self.run_with(lambda: self.order.append("mark"))
        self.assertEqual(self.order, ["process", "mark", "prompt"])

    async def test_mark_failure_kills_process_before_prompt(self) -> None:
        def mark() -> None:
            self.order.append("mark")
            raise OSError(28, "No space left on device")

        with self.assertRaises(OSError):
            await self.run_with(mark)
        self.assertEqual(self.order, ["process", "mark", "kill"])
        self.assertEqual(self.procs, {})


class _BrokenStdout:
    async def readuntil(self, _sep: bytes = b"\n") -> bytes:
        raise RuntimeError("поток оборвался")


class UnexitedRunTests(unittest.IsolatedAsyncioTestCase):
    """Процесс рана не вышел и после SIGKILL: запись в журнал, вызывающий
    получает его в unexited и держит проект до выхода (ревью Codex 14.10)."""

    @staticmethod
    def stuck_proc(**fields) -> SimpleNamespace:
        async def wait() -> int:
            raise asyncio.TimeoutError  # не вышел за время ожидания после SIGKILL

        return SimpleNamespace(pid=4321, returncode=None, wait=wait,
                               stderr=SimpleNamespace(read=AsyncMock(return_value=b"")), **fields)

    async def test_claude_stream_error_reports_process(self) -> None:
        proc = self.stuck_proc(stdin=_RunnerStdin([]), stdout=_BrokenStdout())
        procs: dict = {}
        unexited: list = []
        store = SimpleNamespace(get=lambda k: None, set=lambda k, v: None, remove=lambda k: None)
        with patch.object(claude_runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
                patch.object(claude_runner, "_ensure_hook_still_valid", lambda *_: None), \
                patch.object(claude_runner, "ensure_oauth_fresh", AsyncMock()), \
                patch.object(claude_runner, "kill_process_tree", lambda proc, sig: None), \
                self.assertLogs(claude_runner.log, "WARNING") as logs, self.assertRaises(RuntimeError):
            await claude_runner.run_claude("claude", "k", 1, None, "x", store, cwd=Path("/tmp"),
                                           hook_settings=Path("/tmp/x.json"), process_registry=procs,
                                           unexited=unexited)
        self.assertEqual((unexited, procs), ([proc], {}))
        self.assertTrue(any("4321" in line for line in logs.output))

    async def test_codex_timeout_reports_process(self) -> None:
        from claude_tg_bot import codex_runner

        async def communicate(input=None):
            raise asyncio.TimeoutError  # таймаут codex

        proc = self.stuck_proc(communicate=communicate)
        unexited: list = []
        with patch.object(codex_runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
                patch.object(codex_runner, "kill_process_tree", lambda proc, sig: None):
            res = await codex_runner.run_codex("codex", "x", cwd=Path("/"), timeout_sec=1, chat_key="k",
                                               process_registry={}, unexited=unexited)
        self.assertTrue(res.is_error)
        self.assertEqual(unexited, [proc])



class QuoteTests(_TaskBase):
    async def test_quote_fenced_and_does_not_pin_client_memory(self) -> None:
        # Нейтральный ответ на клиентский план в личной теме после сброса:
        # имя заказчика в цитате не закрепляет его память за новой сессией
        # (ревью Codex 14.10, №6)
        plan = "1. Проверить бэкап Гамма\n2. Ответить сводкой"
        msg = _msg(55)
        msg.text = "поправь второй шаг"
        msg.reply_to_message = SimpleNamespace(text=plan, caption=None, message_id=5)
        await botmod.on_text(_update(msg), self.ctx)
        prompt = self.runs[-1]["prompt"]
        self.assertIn(tasks.fence(plan), prompt)
        lines, mask = prompt.splitlines(), _fenced(prompt)
        for i, line in enumerate(lines):
            if "Гамма" in line:
                self.assertTrue(mask[i], line)
            if "поправь второй шаг" in line or "отвечает на сообщение" in line:
                self.assertFalse(mask[i], line)
        probe = _resolve_pin(plan, "")
        if probe is None or probe[0] == "personal":
            self.skipTest("Свода нет или алиас заказчика не распознаётся")
        self.assertEqual(_resolve_pin(prompt, "")[0], "personal")


def _command_update(forwarded: bool) -> Update:
    now = datetime.now(timezone.utc)
    origin = MessageOriginUser(now, User(id=8, first_name="Петя", is_bot=False)) if forwarded else None
    msg = Message(message_id=5, date=now, chat=Chat(id=USER, type=Chat.PRIVATE),
                  from_user=User(id=USER, first_name="Иван", is_bot=False), text="/task сделай X",
                  entities=(MessageEntity(MessageEntity.BOT_COMMAND, 0, 5),), forward_origin=origin)
    return Update(update_id=1, message=msg)


class CommandRoutingTests(unittest.TestCase):
    def test_forwarded_task_is_not_a_command(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(telegram_bot_token="1:test", claude_bin="claude", codex_bin="",
                                  claude_cwd=Path(d), state_dir=Path(d))
            app = botmod.build_app(cfg)
        handlers = app.handlers[0]
        task_h = next(h for h in handlers if isinstance(h, CommandHandler) and "task" in h.commands)
        text_h = next(h for h in handlers if getattr(h, "callback", None) is botmod.on_text)
        self.assertIs(task_h.callback, botmod.on_task)
        own, forwarded = _command_update(False), _command_update(True)
        self.assertTrue(task_h.filters.check_update(own))
        self.assertFalse(task_h.filters.check_update(forwarded))
        self.assertFalse(text_h.filters.check_update(own))
        self.assertTrue(text_h.filters.check_update(forwarded))


if __name__ == "__main__":
    unittest.main()

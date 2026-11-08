"""Запуск и остановка бота: post_init, остановка, перезапуск, ошибки (перенесено из bot.py, шаг 12 разбиения 2)."""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
import uuid
from datetime import time as dtime
from typing import Any

from aiohttp import web
from telegram import BotCommand, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

from . import bigfile, buttons, login_expiry, otklik, restart, tg_api
from . import procs as procs_mod
from .approval import ApprovalRegistry, warn_if_no_shared_secret
from .claude_runner import SessionStore
from .config import Config, local_zone
from .i18n import _
from .voice import log_local_stt_status, make_client

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


async def _on_post_init(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    # Метка запуска: события, принятые этим процессом до восстановления, оно
    # не трогает (ревью Codex 22.10.2026).
    app.bot_data["boot_id"] = uuid.uuid4().hex[:12]
    # Недокачанные большие файлы прошлого процесса (авария, SIGKILL).
    parts = bigfile.clean_parts(cfg.state_dir / "attachments")
    if parts:
        log.warning("removed %d stale .part downloads", parts)
    # Раны прошлого процесса мертвы: одобренные отклики снова с кнопками,
    # взятые без итога «результат неизвестен». До сервера событий и опроса
    # Telegram, чтобы ни один ран и ни одно нажатие не застали старое.
    otklik_base = otklik.otklik_dir(cfg.state_dir)
    try:
        recovered = await asyncio.to_thread(otklik.recover, otklik_base)
    except Exception:
        log.exception("otklik: startup recovery failed")
        recovered = []
    async def _otklik_after_restart() -> None:
        for rec in recovered:
            log.warning("otklik %s: %s after restart", rec["id"], rec["status"])
            if rec["status"] == "unknown":
                await _b._otklik_unknown_notice(app.bot, rec, otklik_base)
        # Остальное перерисует синхронизация: восстановленные в pending и
        # отставшие с прошлых сбоев.
        await _b._otklik_sync(app.bot, otklik_base)
    app.bot_data["otklik_sync"] = asyncio.get_running_loop().create_task(_otklik_after_restart())
    registry: ApprovalRegistry = app.bot_data["registry"]
    session_targets: dict[str, tuple[int, int | None]] = app.bot_data["session_targets"]
    # Wake-фабрика для POST /event {"wake": true}: approval.py не может
    # импортировать bot (цикл), поэтому отдаём замыкание через bot_data.
    # Ставим ДО старта сервера: иначе событие, пришедшее в первые миллисекунды,
    # получило бы ok=true и осталось только уведомлением.
    app.bot_data["event_wake"] = _b._make_event_wake(app)

    app.bot_data["task_new"] = lambda *a, **kw: _b._task_from_http(
        ContextTypes.DEFAULT_TYPE(application=app), *a, **kw)
    app.bot_data["restart_begin"] = lambda chat_key, reason, commit: _restart_begin(
        app, chat_key, reason, commit)

    aio_app = _b.build_aiohttp_app(app, cfg, registry, session_targets)
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.approve_http_host, cfg.approve_http_port)
    await site.start()
    app.bot_data["approval_runner"] = runner
    warn_if_no_shared_secret(cfg)
    app.bot_data["started_at"] = time.time()
    log.info("approval HTTP server on %s:%s", cfg.approve_http_host, cfg.approve_http_port)

    openai_client = make_client(cfg)
    app.bot_data["openai"] = openai_client
    log.info("voice features: %s", "enabled" if openai_client else "DISABLED (no OPENAI_API_KEY)")
    log_local_stt_status(cfg)

    # Меню команд при вводе «/» в клиенте. Заодно перетирает стейл-меню,
    # оставшееся на токене от прошлых ботов.
    try:
        await app.bot.set_my_commands([
            BotCommand("status", _("что сейчас происходит")),
            BotCommand("login", _("восстановить вход Claude или Codex")),
            BotCommand("cancel", _("прервать текущий claude-процесс")),
            BotCommand("reset", _("сбросить сессию (force = прибить и сбросить)")),
            BotCommand("keys", _("кнопки под полем ввода (off = убрать)")),
            BotCommand("plan", _("сначала план с кнопками, выполнение после ✅")),
            BotCommand("task", _("завести тему задачи: /task что сделать")),
            BotCommand("auto", _("окно auto-approve: 15m|1h|off")),
            BotCommand("compose", _("копить сообщения, /go отправит одним промптом")),
            BotCommand("go", _("отправить compose-буфер")),
            BotCommand("at", _("отложенный промпт: 45m|14:30 <текст>")),
            BotCommand("resume", _("подцепить одну из последних сессий")),
            BotCommand("export", _("сессия темы → Markdown-файл")),
            BotCommand("handback", _("команда для продолжения в терминале")),
            BotCommand("usage", _("расход текущего 5-часового окна")),
            BotCommand("effort", _("глубина рассуждений в этой ветке")),
            BotCommand("model", _("модель claude для этой ветки")),
            BotCommand("scope", _("пометка темы (проект/контекст)")),
            BotCommand("help", _("справка по командам")),
        ])
    except Exception:
        log.exception("set_my_commands failed (continuing)")

    # TTL cleanup для state/attachments/. Запускаем как фоновую таску, чтобы
    # она была отменяема и не зависала между рестартами через apscheduler.
    app.bot_data["cleanup_task"] = asyncio.create_task(_b._attachment_cleanup_loop(cfg))
    # Задачи из новостей: застрявшие посреди рана возвращаются до приёма
    # обновлений (позже это сломало бы уже новый ран, ревью Codex 13.10);
    # уборка тем утром до подборки и раз при старте с проверкой прав.
    try:
        await _b._tasks_recover(ContextTypes.DEFAULT_TYPE(application=app))
    except (OSError, ValueError) as e:
        log.error("news tasks recovery failed: %s", e)
    app.job_queue.run_daily(_b._tasks_housekeeping, name="news-tasks",
                            time=dtime(8, 45, tzinfo=local_zone()))
    app.job_queue.run_once(_b._tasks_startup, when=30, name="news-tasks-start")
    # Конец сохранённого входа claude.ai: напоминание владельцу заранее.
    app.job_queue.run_daily(login_expiry.check_login_expiry, name="login-expiry",
                            time=dtime(9, 30, tzinfo=local_zone()))
    app.job_queue.run_once(login_expiry.check_login_expiry, when=60, name="login-expiry-start")
    # Сообщение о перезапуске по заявке: после старта приёма обновлений.
    app.job_queue.run_once(_restart_announce, when=3, name="restart-announce")
    # Работа, оборванная аварией прошлого процесса: забрать до приёма обновлений.
    _b._inflight_recover(app)
    app.job_queue.run_once(_b._inflight_announce, when=3, name="inflight-announce")

    # Восстановить отложенные /at-промпты после рестарта. Просроченные (бот
    # лежал в момент срабатывания) уходят сразу (delay клампится к 1с).
    at_store: SessionStore = app.bot_data["at_jobs"]
    restored = 0
    for jid, entry in list(at_store.data.items()):
        try:
            payload = json.loads(entry["v"])
            _b._schedule_at_job(app, jid, payload)
            restored += 1
        except Exception:
            log.exception("at-job %s повреждён — удаляю", jid)
            at_store.remove(jid)
    if restored:
        log.info("restored %d scheduled /at job(s)", restored)

    # Сигналы остановки PTB заменяются своими: те же SIGINT, SIGTERM и
    # SIGABRT, но флаг остановки ставится до выхода из цикла (_on_stop_signal).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
        try:
            loop.add_signal_handler(sig, _on_stop_signal, app)
        except (NotImplementedError, RuntimeError, ValueError):
            log.warning("stop signal handler not set for %s", sig)

    # Принятые до рестарта пробуждения: после восстановления задач, /at и
    # сигналов остановки, иначе ран прочёл бы старый статус задачи, а
    # остановка в эту минуту не выставила бы флаг (ревью 22.10.2026).
    try:
        await _b._events_resume(app)
    except Exception:
        log.exception("event resume failed")
    # Принятые и не начатые ответы дел уходят, оборванные называются (проект В1, 2.8).
    try:
        await _b._deals_resume(app)
    except Exception:
        log.exception("deal resume failed")


def _parse_chat_key(chat_key: str) -> tuple[int, int | None]:
    """chat_key → (chat_id, thread_id). Format: '<chat_id>' или '<chat_id>:<thread_id>'."""
    if ":" in chat_key:
        chat_str, thread_str = chat_key.split(":", 1)
        return int(chat_str), (int(thread_str) if thread_str else None)
    return int(chat_key), None


async def _send_shutdown_notice_via_api(
    bot_token: str, chat_key: str, text: str, timeout_sec: float = 5.0
) -> None:
    """Прямой POST к Telegram bot API. Bot client уже shut down в post_shutdown,
    поэтому app.bot.send_message не работает: используем raw HTTPS
    (tg_api: тот же адрес сервера, одна попытка, timeout_sec на всё)."""
    try:
        chat_id, thread_id = _parse_chat_key(chat_key)
    except Exception:
        log.warning("cannot parse chat_key=%s for shutdown notice", chat_key)
        return
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    try:
        resp = await tg_api.apost(bot_token, "sendMessage", json=payload, timeout=timeout_sec)
    except Exception as e:
        # Без traceback: цепочка исключения раньше могла нести адрес с токеном;
        # текст ошибок httpx tg_api уже очистил от токена.
        log.warning("shutdown notice POST failed for chat_key=%s: %s: %s", chat_key, type(e).__name__, e)
        return
    if resp.status_code != 200:
        log.warning("shutdown notice POST returned %s for chat_key=%s", resp.status_code, chat_key)


_STOP_GRACE_SEC = 30.0


def _on_stop_signal(app: Application) -> None:
    """SIGINT, SIGTERM или SIGABRT: остановка бота (ревью 12.10.2026).

    PTB в Application.stop() ждёт задачи обработчиков и задания /at, а
    уведомление и SIGTERM ранам в _on_post_shutdown идут только после этого:
    к тому времени раны уже кончились. Под systemd claude получал SIGTERM сам
    и Иван видел «отменено (signal -15)»; из терминала (Ctrl+C) claude в своей
    сессии сигнала не получал, и остановка ждала конца рана. Поэтому здесь,
    до ожидания: флаг stopping (новые раны не стартуют, прерванный ран
    отвечает «бот перезапускается», пока клиент Telegram жив), SIGTERM
    группам идущих ранов и SIGKILL оставшимся через _STOP_GRACE_SEC. Дальше
    как у PTB: SystemExit выводит run_polling на штатную остановку.
    """
    _terminate_runs(app)
    raise SystemExit


def _terminate_runs(app: Application) -> None:
    """Флаг stopping, SIGTERM группам идущих ранов, SIGKILL оставшимся через
    _STOP_GRACE_SEC. Общее для сигнала остановки и перезапуска по заявке."""
    app.bot_data["stopping"] = True
    # Большая загрузка может идти до получаса, а stop() ждёт обработчики.
    _b._downloads_cancel(app)
    _b._local_stt_cancel(app)
    procs: dict[str, asyncio.subprocess.Process] = app.bot_data.get("procs") or {}
    # В реестре только идущие раны. Группу бьём и тогда, когда сам claude уже
    # вышел, а его внук держит вывод (procs.kill_group); пустая группа: no-op.
    alive = list(procs.items())
    for ck, p in alive:
        log.warning("stop signal: SIGTERM to claude proc pid=%s (chat_key=%s)", p.pid, ck)
        _b.kill_process_tree(p, signal.SIGTERM)

    def _kill_left() -> None:
        for ck, p in alive:
            if procs_mod.running(p):
                log.warning("claude proc pid=%s (chat_key=%s) ignored SIGTERM, SIGKILL", p.pid, ck)
                _b.kill_process_tree(p, signal.SIGKILL)

    if alive:
        asyncio.get_running_loop().call_later(_STOP_GRACE_SEC, _kill_left)


# --- перезапуск по заявке (restart.py) -----------------------------------------

def _workshop_busy(app: Application) -> bool:
    cfg: Config = app.bot_data["cfg"]
    key = getattr(cfg, "workshop_chat_key", "")
    lock = app.bot_data.get("chat_locks", {}).get(key) if key else None
    waiting = app.bot_data.get("lock_waiting", {}).get(key, 0) if key else 0
    return bool(lock is not None and lock.locked()) or waiting > 0


async def _restart_wait_workshop(app: Application, req: dict, drain: Any) -> None:
    """Первый шаг перезапуска: дождаться конца работы мастерской. Личка и её
    события тем временем работают; «/cancel мастерская» ускорит."""
    run = app.bot_data.get("workshop_run") or {}
    since = _b._workshop_since(run)
    await _restart_notify(app, req, _("♻️ Перезапуск ждёт конца работы мастерской «{what}» с {since}; "
                                      "личка работает как обычно. «/cancel мастерская» ускорит.").format(
        what=run.get("what") or "?", since=since), True)
    while _workshop_busy(app) and not drain.cancelled and not drain.now_event.is_set():
        await asyncio.sleep(5)


def _restart_active(app: Application) -> int:
    """Сколько работы ещё идёт: обработки сообщений (_dispatch_to_claude) и
    принятые события, которые ещё не дошли до неё."""
    return (app.bot_data.get("active_work", 0) + app.bot_data.get("admitted_events", 0)
            + len(app.bot_data.get("event_wake_tasks") or ()))


def _restart_keyboard(rid: str) -> InlineKeyboardMarkup:
    return buttons.markup([[
        {"text": _("Перезапустить сейчас"), "data": f"rst:now:{rid}"},
        {"text": _("Ждать"), "data": f"rst:wait:{rid}"},
        {"text": _("Отменить"), "data": f"rst:cancel:{rid}"},
    ]])


async def _restart_notify(app: Application, req: dict, text: str, ask: bool = False) -> None:
    try:
        chat_id, thread_id = _parse_chat_key(req["chat_key"])
        await app.bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id, text=text,
            reply_markup=_restart_keyboard(req["id"]) if ask else None,
        )
    except Exception:
        log.exception("restart notice failed (ignored)")


def _restart_reschedule_at(app: Application) -> None:
    """/at, отклонённые на время ожидания, остались на диске без задания:
    при отмене заявки вернуть их в очередь (иначе ждали бы до рестарта)."""
    at_store: SessionStore = app.bot_data["at_jobs"]
    running: set[str] = app.bot_data.get("at_running") or set()
    for jid, entry in list(at_store.data.items()):
        if jid in running or app.job_queue.get_jobs_by_name(f"at:{jid}"):
            continue
        try:
            _b._schedule_at_job(app, jid, json.loads(entry["v"]))
        except Exception:
            log.exception("at-job %s не восстановлен после отмены перезапуска", jid)


async def _restart_abort(app: Application, req: dict, text: str) -> None:
    cfg: Config = app.bot_data["cfg"]
    drain = app.bot_data.get("restart_drain")
    if drain is not None:
        drain.cancelled = True
    app.bot_data["restart_drain"] = None
    app.bot_data["draining"] = False
    app.bot_data.pop("draining_keys", None)
    restart.clear_request(cfg.state_dir)
    _restart_reschedule_at(app)
    log.warning("restart request %s aborted: %s", req.get("id"), text.splitlines()[0])
    await _restart_notify(app, req, text)


async def _restart_exit(app: Application, req: dict) -> None:
    """Код проверен: заявка в фазе restarting на диск, штатный выход с
    EXIT_RESTART, systemd поднимет бота снова (Restart=on-failure)."""
    cfg: Config = app.bot_data["cfg"]
    req = {**req, "phase": restart.PHASE_RESTARTING}
    restart.save_request(cfg.state_dir, req)
    log.warning("restart request %s: exiting with %s", req["id"], restart.EXIT_RESTART)
    app.bot_data["exit_code"] = restart.EXIT_RESTART
    # Обычно работы уже нет; после «Перезапустить сейчас» идущие раны получают
    # SIGTERM и отвечают «бот перезапускается, ран прерван».
    _terminate_runs(app)
    app.stop_running()


async def _restart_begin(app: Application, chat_key: str, reason: str, commit: str) -> tuple[int, dict]:
    """Приём заявки из /restart (approval.handle_restart)."""
    # Под замком: проверка «заявки нет» и установка новой через await
    # to_thread иначе пропускали бы вторую заявку (ревью Codex 13.10.2026).
    lock: asyncio.Lock = app.bot_data.setdefault("restart_lock", asyncio.Lock())
    async with lock:
        return await _restart_begin_locked(app, chat_key, reason, commit)


async def _restart_begin_locked(app: Application, chat_key: str, reason: str,
                                commit: str) -> tuple[int, dict]:
    cfg: Config = app.bot_data["cfg"]
    if app.bot_data.get("stopping"):
        return 503, {"error": "bot is stopping"}
    drain = app.bot_data.get("restart_drain")
    if drain is not None:
        return 202, {"ok": True, "id": drain.req["id"], "duplicate": True}
    problem = await asyncio.to_thread(restart.code_problem, commit)
    if problem:
        return 409, {"error": problem}
    if _b._is_workshop(cfg, chat_key):
        # Заявка из мастерской: ход перезапуска и кнопки в личке, тема заглушена.
        chat_key = str(cfg.primary_chat_id)
    req = restart.new_request(chat_key, reason, commit)
    restart.save_request(cfg.state_dir, req)
    # Ожидание в два шага (проект Домового 22.10.2026): пока идёт наряд
    # мастерской, новую работу не берёт только она, личка работает; общее
    # ожидание начинается, когда мастерская свободна (_restart_run).
    workshop_busy = _workshop_busy(app)
    if workshop_busy:
        app.bot_data["draining_keys"] = {cfg.workshop_chat_key}
    else:
        app.bot_data["draining"] = True

    async def notify(text: str, ask: bool) -> None:
        await _restart_notify(app, req, text, ask)

    async def do_restart() -> None:
        await _restart_exit(app, req)

    async def abort(text: str) -> None:
        await _restart_abort(app, req, text)

    drain = restart.Drain(
        req=req, active=lambda: _restart_active(app), notify=notify,
        validate=restart.validate_code, code_problem=restart.code_problem,
        restart=do_restart, abort=abort,
    )
    app.bot_data["restart_drain"] = drain

    async def _run() -> None:
        try:
            if workshop_busy:
                await _restart_wait_workshop(app, req, drain)
                if drain.cancelled:
                    return
                app.bot_data.pop("draining_keys", None)
                app.bot_data["draining"] = True
            await drain.run()
        except Exception as e:
            log.exception("restart drain failed")
            if not drain.cancelled:
                await _restart_abort(app, req, _("❌ Перезапуск отменён из-за ошибки: {error_type}: {error}").format(error_type=type(e).__name__, error=e))

    app.bot_data["restart_task"] = asyncio.create_task(_run())
    log.warning("restart request %s accepted: chat=%s reason=%r", req["id"], chat_key, reason)
    return 202, {"ok": True, "id": req["id"]}


async def on_restart_pick(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки долгого ожидания: «Перезапустить сейчас», «Ждать», «Отменить»."""
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    if user is None or user.id not in cfg.allowed_user_ids:
        return
    parts = (query.data or "").split(":")
    action, rid = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
    drain = ctx.application.bot_data.get("restart_drain")
    if drain is None or drain.req.get("id") != rid:
        await _b._answer_quietly(query, _("Эта заявка уже не действует"))
        return
    await buttons.strip_buttons(query)
    if action == "now":
        await _b._answer_quietly(query, _("Проверяю код и перезапускаю"))
        drain.now_event.set()
    elif action == "cancel":
        await _b._answer_quietly(query, _("Заявка отменена"))
        await _restart_abort(ctx.application, drain.req, _("Заявка на перезапуск отменена, бот берёт работу как обычно."))
    else:
        await _b._answer_quietly(query, _("Жду дальше"))


async def _restart_announce(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """После старта: сообщить о перезапуске по заявке и закрыть её.

    Заявка в фазе pending значит, что бот остановили до перезапуска (сигнал,
    сбой): она потеряна, повторять её сам бот не будет."""
    app = ctx.application
    cfg: Config = app.bot_data["cfg"]
    req = restart.load_request(cfg.state_dir)
    if req is None:
        return
    restart.clear_request(cfg.state_dir)
    if req.get("phase") != restart.PHASE_RESTARTING:
        await _restart_notify(app, req, _("⚠️ Бот остановился раньше, чем дошло до перезапуска по заявке. "
                                        "Если правка ещё нужна, подай заявку снова."))
        return
    head, _unused = await asyncio.to_thread(restart.git_state)
    version = head[:7] or _("неизвестна")
    note = "" if not req.get("commit") or head == req["commit"] else \
        _(" (в заявке был {commit})").format(commit=req['commit'][:7])
    await _restart_notify(app, req, _("✅ Бот перезапущен по заявке: {reason}. "
                                    "Версия {version}{note}.").format(
        reason=req.get('reason') or _("без причины"), version=version, note=note))


async def _compose_lost_notice(app: Application) -> None:
    """Буфер /compose в памяти рестарт не переживает: сказать, что он сброшен.
    Иначе после рестарта куски шли каждый своим раном, а /go отвечал «не
    включён» (аудит очереди 31.10.2026). Клиент Telegram здесь ещё жив."""
    buffers: dict[str, _b.ComposeBuffer] = app.bot_data.get("compose_buffers") or {}
    for chat_key, buf in list(buffers.items()):
        buffers.pop(chat_key, None)
        if buf.cancelled:
            continue
        try:
            chat_id, thread_id = _parse_chat_key(chat_key)
            await app.bot.send_message(
                chat_id=chat_id, message_thread_id=thread_id,
                text=_("♻️ бот перезапускается: буфер /compose ({count} шт.) не отправлен и сброшен. "
                       "После рестарта /compose заново").format(count=buf.filled),
            )
        except Exception:
            log.warning("compose lost notice failed chat=%s", chat_key, exc_info=True)


async def _on_post_stop(app: Application) -> None:
    await _compose_lost_notice(app)
    manager = app.bot_data.get("login_manager")
    if manager is not None:
        await manager.close()


async def _on_post_shutdown(app: Application) -> None:
    cleanup_task: asyncio.Task | None = app.bot_data.get("cleanup_task")
    if cleanup_task is not None:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("cleanup task raised on shutdown")
    runner: web.AppRunner | None = app.bot_data.get("approval_runner")
    if runner is not None:
        await runner.cleanup()
        log.info("approval HTTP server stopped")
    openai_client = app.bot_data.get("openai")
    if openai_client is not None:
        try:
            await openai_client.close()
        except Exception:
            log.exception("openai client close failed")
    mtproto: bigfile.BotMTProto | None = app.bot_data.get("mtproto")
    if mtproto is not None:
        await mtproto.close()
    cancel_tasks: set[asyncio.Task[None]] = app.bot_data.get("cancel_tasks") or set()
    if cancel_tasks:
        await asyncio.gather(*list(cancel_tasks), return_exceptions=True)
    wake_tasks: set[asyncio.Task] = app.bot_data.get("event_wake_tasks") or set()
    if wake_tasks:
        for task in list(wake_tasks):
            task.cancel()
        await asyncio.gather(*list(wake_tasks), return_exceptions=True)
    # Ожидания выхода процессов, не вышедших после SIGKILL: удержание проекта
    # в памяти бота рестарт не переживает (ревью Codex 14.10).
    exit_waits: set[asyncio.Task] = app.bot_data.get("task_exit_waits") or set()
    for task in list(exit_waits):
        task.cancel()
    if exit_waits:
        await asyncio.gather(*list(exit_waits), return_exceptions=True)
    burst_tasks: dict[str, asyncio.Task] = app.bot_data.get("burst_expiry_tasks") or {}
    for task in list(burst_tasks.values()):
        if not task.done():
            task.cancel()
    if burst_tasks:
        await asyncio.gather(*list(burst_tasks.values()), return_exceptions=True)
    procs: dict[str, asyncio.subprocess.Process] = app.bot_data.get("procs") or {}
    # Гасим и раны, где claude уже вышел, а внук держит вывод (procs.running);
    # весть о перезапуске, как и раньше, только темам с живым claude.
    active = [(ck, p) for ck, p in list(procs.items()) if procs_mod.running(p)]
    notify = [ck for ck, p in active if p.returncode is None]
    if active:
        # Уведомляем юзеров (через прямой Telegram API — bot client уже мёртв
        # в post_shutdown). Без этого юзер видит загадочное "отменено
        # (signal 143)" и не понимает что бот рестартуется.
        # Обычно сюда уже никто не доходит: Application.stop() ждёт раны до
        # post_shutdown, их гасит _on_stop_signal, а «бот перезапускается»
        # пишет сам _dispatch_to_claude. Ветка запасная, для остановки без
        # сигнала (ревью 12.10.2026).
        cfg: Config | None = app.bot_data.get("cfg")
        if cfg is not None:
            await asyncio.gather(
                *(
                    _send_shutdown_notice_via_api(
                        cfg.telegram_bot_token,
                        ck,
                        _("♻️ Бот перезапускается, эта сессия будет прервана. "
                          "Дождись пары секунд и повтори запрос."),
                    )
                    for ck in notify
                ),
                return_exceptions=True,
            )
        # Расширенный grace (30 сек) — даёт claude-процессам время дописать
        # последний ответ в Telegram и завершиться сами. /cancel оставляет
        # default 5 сек: там юзер явно нажал и не ждёт долго.
        await asyncio.gather(
            *(_b._terminate_and_wait(ck, p, grace_sec=30.0) for ck, p in active),
            return_exceptions=True,
        )
    # Последним: авария посреди остановки ещё даст весть после старта (inflight.py).
    _b._inflight_stopped(app)


async def _terminate_and_wait(chat_key: str, proc: asyncio.subprocess.Process, *, grace_sec: float = 5.0) -> None:
    """SIGTERM всей pgroup, подождать grace_sec, SIGKILL если живы. Гарантирует,
    что к концу shutdown ни claude, ни его дети не остаются зомбями."""
    log.warning("terminating claude proc pid=%s (chat_key=%s) on shutdown", proc.pid, chat_key)
    # Сам claude мог уже выйти, а его потомки нет: судим по группе, не по
    # returncode лидера (procs.kill_group).
    if not procs_mod.group_alive(proc):
        return
    _b.kill_process_tree(proc, signal.SIGTERM)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_sec
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_sec)
    except asyncio.TimeoutError:
        pass
    while procs_mod.group_alive(proc) and loop.time() < deadline:
        await asyncio.sleep(0.1)
    if not procs_mod.group_alive(proc):
        return
    log.warning("claude proc pid=%s group alive after %ss; sending SIGKILL to pgroup", proc.pid, grace_sec)
    _b.kill_process_tree(proc, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        log.error("claude proc pid=%s did not die after SIGKILL", proc.pid)


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный error handler PTB. Без него необработанное исключение
    обработчика (сетевой флап Telegram, баг) видно только в логе, а
    пользователь смотрит в тишину и не знает, дошёл ли запрос."""
    log.error("unhandled error in handler", exc_info=ctx.error)
    if isinstance(update, _b.Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                _("⚠️ внутренняя ошибка при обработке — смотри логи бота, повтори запрос")
            )
        except Exception:
            log.debug("error notice failed (ignored)", exc_info=True)


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

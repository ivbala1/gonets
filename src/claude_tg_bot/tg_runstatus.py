"""Строка состояния рана (_RunStatus), кнопка наблюдения, индикатор набора (перенесено из bot.py, шаг 15 разбиения 2)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import timedelta
from typing import Any, Callable

from telegram import InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from . import buttons
from .config import Config
from .i18n import _, plural
from .replies import is_unconfirmed

# Тот же журнал, что у bot.py: имена в журнале не меняются.
log = logging.getLogger("claude_tg_bot.bot")


class _RunStatus:
    """Одно статусное сообщение «что сейчас делает claude» на весь ран.

    Обновляется по событиям tool_use из stream-json (см. claude_runner),
    редактированием того же сообщения — без спама новыми. Троттлинг 3 с,
    по завершении рана сообщение удаляется. Любой сбой Telegram глотается:
    статус — косметика, ломать ран он не должен.

    Фоновый тик (start) раз в _TICK_SEC перерисовывает статус и без событий,
    чтобы росла «тишина», и сторожит ран: нет ни одного события stream-json
    silence_alert_sec, и в чат уходит тревога со звуком и кнопками «Ждать» и
    «Прервать» (on_run_watch_pick), одна на эпизод тишины; после
    progress_note_sec работы один раз приходит весточка «Работаю N минут».
    Codex-раны идут мимо: событий по ходу у run_codex нет, сторож давал бы
    ложные тревоги.
    """

    _THROTTLE_SEC = 3.0
    # Перерисовка без событий: тишина растёт на глазах, а правок в разы меньше
    # лимита Telegram на редактирование.
    _TICK_SEC = 25.0
    # Тишину в статусе показываем только заметную: паузы по 5-10 с между
    # событиями обычное дело.
    _SILENCE_SHOW_SEC = 30.0
    # Попыток громкой отправки на эпизод: тревогу или весточку, которая не
    # уходит, не повторяем со звуком до конца рана.
    _LOUD_TRIES = 4
    # finish() убирает статус и тревоги не дольше этого, даже после 429.
    _FINISH_BUDGET_SEC = 30.0
    _DELETE_TRIES = 3
    # BadRequest правки, после которых это сообщение уже не поправить.
    _EDIT_GONE = ("message to edit not found", "message can't be edited", "message_id_invalid")

    def __init__(
        self, bot: Any, chat_id: int, thread_id: int | None, *,
        chat_key: str = "",
        silence_alert_sec: int = 0,
        progress_note_sec: int = 0,
        registry: dict[str, _b._RunStatus] | None = None,
        waiting: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self.chat_key = chat_key
        self._silence_alert_sec = silence_alert_sec
        self._progress_note_sec = progress_note_sec
        # run_id -> статус живых ранов: по нему кнопки тревоги находят свой ран.
        self._registry = registry
        # True, пока ран ждёт решения по карточке approve: эта тишина за
        # человеком, а не за раном.
        self._waiting = waiting
        self._clock = clock
        self.run_id = uuid.uuid4().hex[:12]
        self._message_id: int | None = None
        # Первая отправка статуса не подтверждена: больше его в этом ране не шлём.
        self._send_unknown = False
        self._started = clock()
        self._last_edit = 0.0
        # Начало тишины для статуса и текста тревоги: последнее событие, а
        # ожидание карточки approve сдвигает его, как и _quiet_since.
        self._last_event = self._started
        # Отсчёт до тревоги: от последнего события или от нажатия «Ждать».
        self._quiet_since = self._started
        self._alerted = False
        # Сколько ещё попыток у тревоги этого эпизода и у весточки.
        self._alert_left = self._LOUD_TRIES
        self._progress_left = self._LOUD_TRIES
        self._alert_ids: list[int] = []
        self._progress_sent = False
        self._steps = 0
        self._desc = ""
        self._closed = False
        # До этого момента Telegram не трогаем: 429 на retry_after.
        self._pause_until = 0.0
        # Пауза одного статуса после его сбоя: громкие отправки она не держит.
        self._status_pause_until = 0.0
        self._ticker: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _dur(sec: float) -> str:
        m, s = divmod(int(sec), 60)
        return _("{m}м{s:02d}с").format(m=m, s=s) if m else _("{s}с").format(s=s)

    def _fmt(self, now: float) -> str:
        parts = [f"🔧 {self._desc}", _("шаг {steps}").format(steps=self._steps), _("всего {dur}").format(dur=self._dur(now - self._started))]
        silence = now - self._last_event
        if silence >= self._SILENCE_SHOW_SEC:
            parts.append(_("тишина {dur}").format(dur=self._dur(silence)))
        return " · ".join(parts)

    def headline(self) -> str:
        """Первая строка /status: что идёт, сколько и как остановить (ux2, 01.11.2026)."""
        dur = self._dur(self._clock() - self._started)
        if self._desc:
            line = _("▶️ отвечаю {dur}, шаг {steps}: {desc}").format(
                dur=dur, steps=self._steps, desc=self._desc[:80])
        else:
            line = _("▶️ отвечаю {dur}").format(dur=dur)
        if self._waiting is not None and self._waiting():
            line += _(", ждёт твоей карточки")
        return line

    def start(self) -> None:
        """Регистрация для кнопок и фоновый тик. Задача копирует контекст рана,
        поэтому в ране без звука (_SILENT_RUN) тревога тоже уходит без звука."""
        if self._registry is not None:
            self._registry[self.run_id] = self
        self._ticker = asyncio.create_task(self._tick_loop(), name=f"run-status:{self.run_id}")

    def on_event(self) -> None:
        """Любое событие stream-json (activity_cb): тишина и эпизод тревоги сначала."""
        now = self._clock()
        self._last_event = now
        self._quiet_since = now
        self._alerted = False
        self._alert_left = self._LOUD_TRIES

    async def on_tool(self, desc: str) -> None:
        self._steps += 1
        self._desc = desc
        self.on_event()
        if self._lock.locked():
            # Тик шлёт статус, тревогу или весточку: шаг уже учтён, тик его
            # покажет. Ожидание здесь держало бы чтение потока claude (аудит 10.2026).
            return
        async with self._lock:
            if self._closed:
                return
            now = self._clock()
            if self._message_id is not None and now - self._last_edit < self._THROTTLE_SEC:
                return
            await self._show(now)

    def _back_off(self, exc: RetryAfter) -> None:
        """Общая пауза всех запросов рана на retry_after из 429."""
        ra = exc.retry_after
        sec = ra.total_seconds() if isinstance(ra, timedelta) else float(ra or 0)
        self._pause_until = max(self._pause_until, self._clock() + sec)

    async def _show(self, now: float) -> None:
        """Отправить или перерисовать статус. Звать под self._lock."""
        if now < max(self._pause_until, self._status_pause_until) or self._send_unknown:
            return
        try:
            if self._message_id is None:
                m = await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=self._fmt(now),
                    message_thread_id=self._thread_id,
                    disable_notification=True,
                )
                self._message_id = m.message_id
            else:
                await self._bot.edit_message_text(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    text=self._fmt(now),
                )
            # Окно троттлинга от конца запроса: при медленном Telegram каждое
            # событие иначе снова ждало правку, а с ней и чтение потока claude
            # (аудит 10.2026).
            self._last_edit = max(now, self._clock())
        except Exception as e:
            if self._message_id is None and is_unconfirmed(e):
                # Статус мог дойти, номера нет: новый при каждом показе плодил бы копии.
                self._send_unknown = True
                log.warning("run status send unconfirmed by Telegram, no more status in this run")
                return
            if isinstance(e, RetryAfter):
                self._back_off(e)
            elif (
                self._message_id is not None
                and isinstance(e, BadRequest)
                and any(s in str(e).lower() for s in self._EDIT_GONE)
            ):
                # Статус удалили руками: править нечего, следующий показ
                # пришлёт новый. Тревоги и весточку это не задерживает.
                self._message_id = None
                log.info("run status message gone, will send a new one: %s", e)
                return
            # Сбой тоже тратит окно троттлинга статуса: иначе следующее
            # событие повторило бы запрос сразу.
            self._status_pause_until = max(self._status_pause_until, now + self._THROTTLE_SEC)
            log.debug("run status update failed (ignored)", exc_info=True)

    # Куда тревога тишины, если тема рана заглушена (мастерская): (чат, тема).
    alarm_target: tuple[int, int | None] | None = None

    async def _send_loud(
        self, text: str, left: int, markup: InlineKeyboardMarkup | None = None,
        alarm: bool = False,
    ) -> tuple[int | None, int]:
        """Отдельное сообщение со звуком: статус беззвучный и разбудить не может.
        Возвращает id (None, если не доставлено: тик повторит позже) и остаток
        попыток эпизода из left. Явный отказ Telegram (429, BadRequest) тратит
        одну попытку. Таймаут и сбой сети неопределённы: Telegram мог доставить,
        поэтому после них не больше одного повтора, иначе дубли со звуком."""
        if self._clock() < self._pause_until:
            return None, left
        chat_id, thread_id = (self.alarm_target if alarm and self.alarm_target else (self._chat_id, self._thread_id))
        if alarm and self.alarm_target:
            text = _("🛠 Мастерская: ") + text
        try:
            m = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=thread_id,
                disable_notification=False,
                reply_markup=markup,
            )
            return m.message_id, left
        except Exception as e:
            if is_unconfirmed(e):
                # Сообщение обычно доходит: считаем отправленным, эпизод закрыт (left 0), повтора нет.
                log.warning("run watch message unconfirmed by Telegram, not repeated")
                return None, 0
            if isinstance(e, RetryAfter):
                self._back_off(e)
            unsure = not isinstance(e, TelegramError) or (
                isinstance(e, NetworkError) and not isinstance(e, BadRequest)
            )
            left = min(left - 1, 1) if unsure else left - 1
            log.warning(
                "run watch message failed (%s, tries left %d)",
                "maybe delivered" if unsure else "not delivered", left, exc_info=True,
            )
            return None, left

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._TICK_SEC)
            try:
                await self._tick()
            except Exception:
                log.debug("run status tick failed (ignored)", exc_info=True)

    async def _tick(self) -> None:
        async with self._lock:
            if self._closed:
                return
            now = self._clock()
            if self._waiting is not None and self._waiting():
                self._quiet_since = now
                self._last_event = now
            if self._desc and (self._message_id is None or now - self._last_edit >= self._THROTTLE_SEC):
                await self._show(now)
            # Пока шёл запрос, finish() мог закрыть ран: тогда ни звука.
            if self._closed:
                return
            last = self._desc or _("ещё не было")
            if (
                self._silence_alert_sec > 0
                and not self._alerted
                and now - self._quiet_since >= self._silence_alert_sec
            ):
                quiet = self._quiet_since
                mins = int(now - self._last_event) // 60
                markup = buttons.markup([[
                    {"text": _("Ждать"), "data": f"runwatch:wait:{self.run_id}"},
                    {"text": _("Прервать"), "data": f"runwatch:stop:{self.run_id}"},
                ]])
                mid, left = await self._send_loud(
                    _("⏳ {mins} {unit} тишины: "
                      "последнее действие {last}. Похоже, завис или долгая команда.").format(
                        mins=mins, unit=plural(mins, ('минута', 'минуты', 'минут'), ('minute', 'minutes')), last=last),
                    self._alert_left,
                    markup,
                    alarm=True,
                )
                if mid is not None:
                    self._alert_ids.append(mid)
                # Эпизод закрывает доставленная тревога или кончившиеся
                # попытки. Событие, пришедшее во время отправки, уже начало
                # новый эпизод со своими попытками.
                if self._quiet_since == quiet:
                    self._alert_left = left
                    if mid is not None or left <= 0:
                        self._alerted = True
                if self._closed:
                    return
            if (
                self._progress_note_sec > 0
                and not self._progress_sent
                and now - self._started >= self._progress_note_sec
            ):
                mins = int(now - self._started) // 60
                mid, self._progress_left = await self._send_loud(
                    _("Работаю {mins} {unit}, "
                      "шагов {steps}, сейчас: {last}. Прервать: «стоп»").format(
                        mins=mins, unit=plural(mins, ('минуту', 'минуты', 'минут'), ('minute', 'minutes')),
                        steps=self._steps, last=last),
                    self._progress_left,
                )
                if mid is not None or self._progress_left <= 0:
                    self._progress_sent = True

    def snooze(self, alert_id: int | None) -> None:
        """«Ждать»: следующая тревога не раньше ещё одного полного срока тишины."""
        self._quiet_since = self._clock()
        self._alerted = False
        self._alert_left = self._LOUD_TRIES
        self.forget_alert(alert_id)

    def forget_alert(self, alert_id: int | None) -> None:
        """На тревогу ответили: по завершении рана её не удалять."""
        if alert_id in self._alert_ids:
            self._alert_ids.remove(alert_id)

    async def finish(self) -> None:
        self._closed = True
        if self._registry is not None:
            self._registry.pop(self.run_id, None)
        # Под замком: дождаться отправки, начатой тиком, иначе созданное ею
        # сообщение осталось бы в чате навсегда.
        async with self._lock:
            mid, self._message_id = self._message_id, None
            alerts, self._alert_ids = self._alert_ids, []
        if self._ticker is not None:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
            self._ticker = None
        # Тревогу без ответа убираем вместе со статусом: ответ рана уже идёт
        # следом, кнопки при нём только сбивали бы с толку.
        # После 429 ждём retry_after, но не дольше _FINISH_BUDGET_SEC на всё:
        # ответ рана ждёт finish().
        deadline = self._clock() + self._FINISH_BUDGET_SEC
        alarm_chat = self.alarm_target[0] if self.alarm_target else self._chat_id
        for m, chat in ([(mid, self._chat_id)] if mid is not None else []) + [(a, alarm_chat) for a in alerts]:
            done = False
            for _unused in range(self._DELETE_TRIES):
                wait = self._pause_until - self._clock()
                if wait > 0:
                    if self._clock() + wait > deadline:
                        break
                    await asyncio.sleep(wait)
                try:
                    # Тревога мастерской лежит в личке (alarm_target), статус в теме.
                    await self._bot.delete_message(chat_id=chat, message_id=m)
                    done = True
                except RetryAfter as e:
                    self._back_off(e)
                except Exception:
                    # Не 429 (уже удалено, нет прав): повтор не поможет.
                    done = True
                    log.debug("run status delete failed (ignored)", exc_info=True)
                if done:
                    break
            if not done:
                log.warning("run status message %s left in chat: delete kept hitting 429", m)


def _run_waits_approval(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> bool:
    """Ран темы ждёт решения по карточке approve. Публичного «есть ли карточка
    у сессии» у ApprovalRegistry нет, поэтому реестр читается напрямую."""
    sid = ctx.application.bot_data.get("active_sessions", {}).get(chat_key)
    registry = ctx.application.bot_data.get("registry")
    items = getattr(registry, "_items", None) or {}
    return bool(sid) and any(getattr(p, "session_id", None) == sid for p in items.values())


async def on_run_watch_pick(update: _b.Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки тревоги тишины рана (_RunStatus): «Ждать» и «Прервать»."""
    cfg: Config = ctx.application.bot_data["cfg"]
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    if user is None or user.id not in cfg.allowed_user_ids:
        return
    parts = (query.data or "").split(":")
    action, run_id = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
    status: _b._RunStatus | None = ctx.application.bot_data.get("run_watch", {}).get(run_id)
    card = query.message
    if status is None:
        await _b._answer_quietly(query, _("Ран уже закончился"))
        await buttons.strip_buttons(query)
        return
    alert_id = getattr(card, "message_id", None)
    if action == "wait":
        status.snooze(alert_id)
        await _b._answer_quietly(query, _("Жду дальше"))
        await buttons.strip_buttons(query)
        return
    if action != "stop":
        await _b._answer_quietly(query, _("Неизвестная кнопка"))
        return
    status.forget_alert(alert_id)
    procs: dict[str, asyncio.subprocess.Process] = ctx.application.bot_data["procs"]
    proc = procs.get(status.chat_key)
    if proc is None or proc.returncode is not None:
        note = "🛑 " + _("процесса уже нет")
    else:
        note = await _b._cancel_running(ctx, status.chat_key, proc)
    await _b._answer_quietly(query, _("Прерываю"))
    text = getattr(card, "text", None) or ""
    if text:
        await buttons.note_on_card(query, text, note, "run watch stop", parse_mode=None)
        return
    try:
        await query.edit_message_text(note)
    except Exception:
        log.debug("run watch card edit failed (ignored)", exc_info=True)


async def _typing_loop(bot: Any, chat_id: int) -> None:
    """Держит индикатор «печатает…» живым: Telegram гасит его через ~5 с,
    а claude работает минуты. Сбой индикатора не должен ломать основной путь."""
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, read_timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("chat action failed (ignored)", exc_info=True)
        await asyncio.sleep(5.0)


@contextlib.asynccontextmanager
async def _typing_indicator(bot: Any, chat_id: int):
    task = asyncio.create_task(_typing_loop(bot, chat_id))
    try:
        yield
    finally:
        task.cancel()


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402

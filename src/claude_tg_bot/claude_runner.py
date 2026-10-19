from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .auth import is_auth_error
from .config import agent_env
from .i18n import _
# Ран по чужому тексту: списки замков и проверка в модуле untrusted.
from .untrusted import (  # noqa: F401  (UNTRUSTED_READ_TOOLS: для тестов и справки)
    UNTRUSTED_ALLOWED_TOOLS,
    UNTRUSTED_ASK_TOOLS,
    UNTRUSTED_READ_TOOLS,
    unknown_tools as untrusted_unknown_tools,
)

log = logging.getLogger(__name__)

# Лимит StreamReader для построчного чтения stream-json: события assistant
# несут весь текст сообщения одной строкой, дефолтные 64 KiB не хватает.
_STREAM_LINE_LIMIT = 10 * 1024 * 1024


def kill_process_tree(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Послать сигнал всему process group, в которой живёт claude.

    Claude запускается с `start_new_session=True`, поэтому он становится
    лидером своей process group, и все его дети (Bash из Bash-tool, sub-процессы
    от MCP-серверов и т.п.) наследуют ту же pgid. `proc.terminate()` бьёт
    только по верхнему PID, оставляя детей зомбями. `os.killpg(pgid, sig)`
    кладёт всё дерево.

    pgid берём через os.getpgid(proc.pid) — у нас он равен pid после
    start_new_session, но на случай гонок safer использовать getpgid.
    Если процесс уже мёртв (returncode != None) — no-op.
    """
    if proc.pid is None or proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        # Уже умер или нет такого PID — нечего убивать.
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except OSError as e:
        # На POSIX это редкий случай; fall-back на прямой kill самого процесса.
        log.warning("killpg(%s, %s) failed: %s — falling back to proc.terminate/kill", pgid, sig, e)
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            pass

# F1: mtime-cached revalidation hook settings.
# load_config валидирует hook-файл при старте бота, но админ может изменить
# файл в runtime (git pull, редактор, миграция). Claude читает актуальный файл
# на КАЖДОМ запуске, и если matcher сломан — bypassPermissions превращается
# в полный bypass (fail-open). Перепроверяем перед каждым run, дёшево через mtime.
_hook_validation_cache: dict[str, float] = {}


def _ensure_hook_still_valid(path: Path) -> None:
    """Повторно валидировать hook settings, если mtime изменился с прошлого раза.

    Raises RuntimeError, если файл стал невалидным или недоступным —
    run_claude должен вернуть пользователю ошибку вместо того, чтобы запустить
    claude с бесконтрольным bypassPermissions.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        raise RuntimeError(f"hook settings unreadable: {path}: {e}") from e
    key = str(path)
    if _hook_validation_cache.get(key) == mtime:
        return  # файл не менялся — ок
    # Lazy import — избегаем циклов (config тянет re, json; безопасен).
    from .config import _validate_hook_settings
    _validate_hook_settings(path)
    _hook_validation_cache[key] = mtime


def new_session_id() -> str:
    return str(uuid.uuid4())


def _atomic_write_text(path: Path, data: str) -> None:
    """Пишем через tmp-файл в той же директории + os.replace — atomic на одном FS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class SessionStore:
    """chat_key -> {session_id, last-used ts}, persisted to JSON (atomic).

    chat_key — это `f"{chat_id}"` для private/обычного group-чата или
    `f"{chat_id}:{thread_id}"` для forum-thread в супергруппе. Это позволяет
    держать отдельную Claude-сессию на каждую тему в одной группе.

    На диске значение = {"v": <строка>, "ts": <unix-time>}. ts нужен для
    idle-TTL (бот стартует новую сессию, если тему давно не трогали, чтобы не
    переигрывать всю историю в холодный prompt-cache). Старый формат (голая
    строка) читается с миграцией: ts стартового времени = «сейчас», т.е.
    существующим сессиям даётся свежее окно, без пере-сброса сразу после деплоя.

    Стор generic: используется и для session_id, и для имён forum-тем
    (topic_names.json); для имён ts просто не читается.

    NOTE: read-modify-write через self.data + _flush() намеренно sync (без
    await/to_thread). На single-threaded asyncio это даёт сериализацию через
    event loop. **Не оборачивать `set`/`remove` в `to_thread`** — появится
    race между потоками (last-write-wins по os.replace).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text() or "{}")
            except json.JSONDecodeError:
                log.warning("sessions file %s is corrupt, starting fresh", path)
                raw = {}
            now = time.time()
            for k, v in (raw.items() if isinstance(raw, dict) else []):
                if isinstance(v, dict):
                    val = str(v.get("v", ""))
                    if val:
                        self.data[k] = {"v": val, "ts": float(v.get("ts", 0.0) or 0.0)}
                elif isinstance(v, str) and v:
                    # legacy голая строка — мигрируем, ts=now (grace-окно).
                    self.data[k] = {"v": v, "ts": now}

    def _flush(self) -> None:
        _atomic_write_text(self.path, json.dumps(self.data, indent=2))

    def get(self, chat_key: str) -> str | None:
        entry = self.data.get(chat_key)
        return entry["v"] if entry else None

    def last_used(self, chat_key: str) -> float:
        """Unix-время последнего set() для ключа; 0.0 — если ключа нет."""
        entry = self.data.get(chat_key)
        return entry["ts"] if entry else 0.0

    def set(self, chat_key: str, session_id: str) -> None:
        self.data[chat_key] = {"v": session_id, "ts": time.time()}
        self._flush()

    def remove(self, chat_key: str) -> str | None:
        prev = self.data.pop(chat_key, None)
        if prev is not None:
            self._flush()
        return prev["v"] if prev else None


@dataclass
class ClaudeResult:
    text: str
    session_lost: bool = False  # session_id не пришёл от claude — следующий запрос начнёт новую
    session_id: str = ""  # sid, с которым run был запущен (для post-hoc cleanup abandoned pendings)
    # Цена этого ответа: разница накопительного total_cost_usd сессии с прошлым
    # ответом. None: прошлая сумма неизвестна (сессия продолжена до учёта).
    cost_usd: float | None = 0.0
    session_cost_usd: float = 0.0  # накопительно по сессии (total_cost_usd)
    # Контекст последнего запроса основного диалога (вход с кэшем), а не сумма
    # по всем запросам рана, как было до 13.10.2026. 0 = неизвестен (после
    # сжатия новых запросов не было или событий нет).
    context_tokens: int = 0
    context_max: int = 0  # максимум за ран
    compacted: int = 0  # сколько раз контекст сжимался за ран
    is_error: bool = False  # это сообщение об ошибке/отказе, а не нормальный ответ
    # HTTP-статус ошибки Anthropic API (из stdout-JSON .api_error_status), если был.
    # Нужен bot.py, чтобы решить про codex-фоллбэк (401/429/5xx = claude недоступен).
    api_error_status: int | None = None
    auth_error: str = ""
    # Тексты всех удачных событий result рана по порядку. Их несколько, когда
    # после ответа пришло уведомление фоновой задачи и агент ответил ещё раз:
    # text тогда последний ответ, а главный остаётся здесь. У ошибки рана
    # (таймаут, отмена, сбой) здесь то, что агент успел ответить до неё
    # (разбор задач 19.10.2026).
    texts: tuple[str, ...] = ()


@dataclass
class _Attempt:
    """Результат одного запуска claude-подпроцесса (одна попытка run_claude)."""
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    active_sid: str
    resumed: bool  # True если запускались через --resume (а не свежий --session-id)
    # Последнее событие type=result из stream-json (аналог прежнего JSON-ответа
    # при --output-format json). None, если стрим оборвался до финала.
    final_event: dict | None = None
    ctx_last: int | None = None  # вход последнего запроса основного диалога
    ctx_max: int = 0
    compacted: int = 0
    result_texts: list[str] = field(default_factory=list)  # тексты всех result по порядку
    # Ран по чужому тексту остановлен по init: инструменты без замка.
    blocked_tools: list[str] = field(default_factory=list)


def _describe_tool_use(evt: dict) -> str | None:
    """Короткая строка статуса из stream-json события assistant с tool_use.

    None, если событие не про вызов инструмента.
    """
    if evt.get("type") != "assistant":
        return None
    message = evt.get("message") or {}
    desc: str | None = None
    for block in (message.get("content") or []):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "?")
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        detail = ""
        if name == "Bash":
            detail = str(inp.get("command", "")).split("\n", 1)[0][:80]
        elif name in ("Read", "Write", "Edit", "MultiEdit"):
            detail = str(inp.get("file_path", "")).rsplit("/", 1)[-1]
        elif name == "NotebookEdit":
            detail = str(inp.get("notebook_path", "")).rsplit("/", 1)[-1]
        elif name in ("Grep", "Glob"):
            detail = str(inp.get("pattern", ""))[:60]
        elif name == "WebFetch":
            detail = str(inp.get("url", ""))[:80]
        elif name == "WebSearch":
            detail = str(inp.get("query", ""))[:60]
        elif name.startswith("mcp__"):
            name = name.split("__", 2)[-1]
        desc = f"{name}: {detail}" if detail else name
    return desc


def _is_stale_session_error(stderr: str) -> bool:
    """True, если --resume упал из-за того, что сессия не найдена.

    claude печатает "No conversation found with session ID: <uuid>" в stderr и
    выходит rc=1, когда session_id из sessions.json протух/удалён из
    ~/.claude/projects (чистка, смена CLAUDE_CWD, ротация). Без обработки КАЖДОЕ
    сообщение в чате падало бы rc=1, пока пользователь вручную не сделает /reset.
    run_claude ловит это и авто-ретраит со свежей сессией.
    """
    return "no conversation found" in stderr.lower()


def _request_context(evt: dict) -> int | None:
    """Вход одного запроса к модели из события assistant основного диалога:
    свежий вход, чтение и создание кэша. None для подагентов и прочих событий.
    Несколько событий одного message.id несут одинаковый usage, поэтому
    берётся последнее, без сложения (ревью Codex 13.10.2026)."""
    if evt.get("type") != "assistant" or evt.get("parent_tool_use_id"):
        return None
    u = (evt.get("message") or {}).get("usage")
    if not isinstance(u, dict):
        return None
    return int((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
               + (u.get("cache_creation_input_tokens") or 0)) or None


# Сколько сессий помнить для цены ответа: давние после /reset и TTL не нужны,
# для вытесненной продолженной сессии цена ответа будет неизвестна.
COST_TOTALS_KEEP = 300


def _answer_cost(cost_totals: MutableMapping[str, str] | None, sid: str,
                 total: float, resumed: bool) -> float | None:
    """Цена ответа: total_cost_usd накопительный по сессии и при --resume
    продолжает прежнюю сумму (проверено 13.10.2026), поэтому вычитается сумма
    прошлого ответа той же сессии. Новая сессия: вся сумма. Продолженная без
    учёта (до 13.10 или учёт потерян): None. Сумма запоминается."""
    prev_raw = cost_totals.get(sid) if cost_totals is not None and sid else None
    try:
        prev = float(prev_raw) if prev_raw is not None else None
    except ValueError:
        prev = None
    if cost_totals is not None and sid and total > 0:
        try:
            # Убрать и вставить заново: свежие сессии в конце, обрезка по
            # COST_TOTALS_KEEP снимает давние.
            cost_totals.pop(sid, None)
            cost_totals[sid] = f"{total:.6f}"
            while len(cost_totals) > COST_TOTALS_KEEP:
                del cost_totals[next(iter(cost_totals))]
        except Exception:
            # Учёт цены не должен ронять отправку готового ответа (ревью Codex).
            log.exception("session cost not saved sid=%s", sid[:8])
    if prev is not None and total >= prev:
        return total - prev
    return None if resumed else total


def _usage_stats(out: dict) -> tuple[float, int]:
    """Из JSON-ответа claude вытащить (стоимость turn, размер контекста).

    context = все входные токены, обработанные в этом turn (свежий input +
    чтение кэша + создание кэша) — это и есть «вес» текущей сессии, по которому
    видно раздувшийся диалог.
    """
    cost = float(out.get("total_cost_usd") or 0.0)
    u = out.get("usage") or {}
    ctx = int(
        (u.get("input_tokens") or 0)
        + (u.get("cache_read_input_tokens") or 0)
        + (u.get("cache_creation_input_tokens") or 0)
    )
    return cost, ctx


def _format_error_text(returncode: int, out: dict | None, stderr_txt: str, raw_stdout: str) -> str:
    """Человекочитаемый текст ошибки вместо сырого JSON-блоба.

    claude кладёт структурные ошибки (refusal, недоступная модель, лимиты) в
    stdout-JSON с полем .result — показываем его, а для частых случаев добавляем
    подсказку, что делать.
    """
    # Подтипы error_* содержат errors вместо result (ревью 12.10.2026).
    if out and (out.get("result") or out.get("errors")):
        errors = out.get("errors") or []
        msg = str(out.get("result") or "\n".join(map(str, errors))).strip()
        stop = out.get("stop_reason")
        api_status = out.get("api_error_status")
        low = msg.lower()
        if stop == "refusal":
            return _(
                "⚠️ Claude отклонил запрос (refusal по Usage Policy — часто ложное "
                "срабатывание).\n\n{msg}\n\n"
                "Можно переформулировать, /reset и повторить, или сменить модель: /model <имя>."
            ).format(msg=msg)
        if api_status == 404 or "selected model" in low or ("model" in low and "access" in low):
            return _(
                "⚠️ {msg}\n\n"
                "Это могло быть временно — попробуй повторить. Если повторяется — "
                "смени модель: /model <имя> (например sonnet или opus), "
                "либо задай CLAUDE_MODEL в .env."
            ).format(msg=msg)
        return _("⚠️ claude error (rc={rc}):\n{msg}").format(rc=returncode, msg=msg)
    if stderr_txt:
        return _("⚠️ claude error rc={rc}\n{details}").format(rc=returncode, details=stderr_txt[:1500])
    return _("⚠️ claude error rc={rc}\n{details}").format(
        rc=returncode, details=raw_stdout.strip()[:1500] or _('(нет вывода)'))


# Путь к OAuth-credentials Claude CLI. Бот не задаёт CLAUDE_CONFIG_DIR, поэтому
# дефолтный ~/.claude/.credentials.json.
_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# Single-flight для warmup-refresh: per-chat lock в bot.py сериализует только
# один чат, а .credentials.json — общий. Без этого два чата после простоя
# запустили бы два конкурентных refresh, гоняющихся за запись токена (с
# ротацией refresh_token второй мог бы записать уже погашенный → ложный 401).
_oauth_refresh_lock = asyncio.Lock()


async def _reap_proc(proc: asyncio.subprocess.Process) -> None:
    """Снять зомби после SIGKILL, не виснув навсегда (uninterruptible sleep).
    Не вышел и тогда: запись в журнал, returncode остаётся None, по нему
    вызывающий держит проект задачи до выхода (unexited, ревью Codex 14.10)."""
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    if proc.returncode is None:
        log.warning("pid=%s не вышел за 5 с после SIGKILL", proc.pid)


def _oauth_expires_at() -> float | None:
    """expiresAt (секунды, UTC) OAuth-токена из .credentials.json, либо None,
    если файла/поля нет или это не OAuth-логин (например, ANTHROPIC_API_KEY)."""
    try:
        data = json.loads(_CREDENTIALS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    exp = oauth.get("expiresAt")
    # expiresAt хранится в миллисекундах.
    return exp / 1000 if isinstance(exp, (int, float)) else None


async def ensure_oauth_fresh(claude_bin: str, *, buffer_sec: int = 120) -> None:
    """Pre-flight: если OAuth access-token протух (или протухнет в ближайшие
    buffer_sec), сделать дешёвый warmup-вызов claude, чтобы CLI зарефрешил токен
    из refresh_token ДО основного запроса.

    Зачем: при долгом простое access-token живёт ~8ч и протухает. Новый CLI
    обычно рефрешит сам на старте, но warmup делает это детерминированно и
    логируемо — страховка от гонок/сетевых сбоев (инцидент 2026-06-20: старый
    CLI 2.1.118 не зарефрешил протухший токен и отдал 401). Если refresh_token
    мёртв — warmup тоже не поможет, но молча: основной запрос вернёт осмысленную
    ошибку (и сработает codex-fallback)."""
    exp = _oauth_expires_at()
    if exp is None:
        return  # не OAuth или нет файла — рефрешить нечего
    if exp - time.time() > buffer_sec:
        log.debug("oauth token fresh (%.0fs left) — skip warmup", exp - time.time())
        return
    async with _oauth_refresh_lock:
        # Double-check под локом: другой чат мог зарефрешить, пока мы ждали.
        exp = _oauth_expires_at()
        if exp is None or exp - time.time() > buffer_sec:
            log.debug("oauth refreshed by concurrent run — skip warmup")
            return
        log.info("oauth token expiring (%.0fs left) — warmup-refresh via claude", exp - time.time())
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                claude_bin, "-p", "--output-format", "json",
                "--model", "claude-haiku-4-5-20251001", "ok",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=agent_env(),
                start_new_session=True,
            )
            await asyncio.wait_for(proc.communicate(), timeout=45)
        except asyncio.TimeoutError:
            log.warning("oauth warmup timed out — killing pgroup")
            kill_process_tree(proc, signal.SIGKILL)
            await _reap_proc(proc)
            return
        except asyncio.CancelledError:
            if proc is not None:
                kill_process_tree(proc, signal.SIGKILL)
                await _reap_proc(proc)
            raise
        except OSError as e:
            log.warning("oauth warmup failed: %s — proceeding, main call may refresh or error", e)
            return
        new_exp = _oauth_expires_at()
        if new_exp and new_exp > exp:
            log.info("oauth token refreshed (now %.0fs left)", new_exp - time.time())
        else:
            log.warning("oauth warmup did not advance expiry — refresh_token may be dead")


def resume_target(
    store: "SessionStore",
    scope_store: "SessionStore | None",
    chat_key: str,
    memory_scope_hint: str,
) -> str | None:
    """Какую сессию можно возобновить, и можно ли вообще.

    Роутер памяти закрепляет проект за сессией по первому сообщению и внутри
    сессии его НЕ меняет. Значит возобновлять можно только пока эффективный
    scope темы тот же: иначе бот подаёт новую рамку, а память остаётся от
    прежнего проекта.

    Сверка живёт ЗДЕСЬ, перед каждым --resume, а не только в обработчиках
    /scope и переименования: те могут не сработать (занятый чат, рестарт
    бота), и тогда старая сессия жила бы вечно.
    """
    existing = store.get(chat_key)
    if scope_store is None:
        return existing
    current = memory_scope_hint or ""
    previous = scope_store.get(chat_key) or ""
    if existing and previous != current:
        log.info(
            "scope changed for %s (%r -> %r), starting a fresh claude session",
            chat_key, previous, current,
        )
        store.remove(chat_key)
        existing = None
    scope_store.set(chat_key, current)
    return existing


# Этап плана: инструменты мимо хука, которые действуют, а не читают
# (субагенты, выход из плана, планировщики, сообщения другим сессиям).
# Хук закрывает остальное (approval, plan_stage; ревью Codex 13.10.2026).
PLAN_DISALLOWED_TOOLS = (
    "Task", "Agent", "ExitPlanMode", "Artifact", "ScheduleWakeup",
    "CronCreate", "CronDelete", "RemoteTrigger", "PushNotification", "SendMessage",
)
# Обдумывание темы задачи с Иваном у карточек: субагенты разрешены. Их Bash и
# запись идут через тот же хук с sid родителя и режимом plan (проверено
# 18.10.2026 пробным раном), то есть под теми же правилами этапа плана.
PLAN_AGENT_TOOLS = ("Task", "Agent")

_untrusted_dir: Path | None = None


def untrusted_settings_path(hook_settings: Path) -> Path:
    """Настройки рана по чужому тексту (модуль untrusted): файл хука бота,
    правила permissions.ask (UNTRUSTED_ASK_TOOLS) и независимая проверка на
    все инструменты. Файл назван по содержимому в своём каталоге процесса и
    не удаляется, пока бот жив: соседний ран не останется без настроек."""
    global _untrusted_dir
    data = json.loads(hook_settings.read_text(encoding="utf-8"))
    perms = data.setdefault("permissions", {})
    perms["ask"] = list(dict.fromkeys([*(perms.get("ask") or []), *UNTRUSTED_ASK_TOOLS]))
    # Сбой самой проверки (не запустилась, упала до ответа) это код 2, то
    # есть отказ, а не пропуск.
    guard = f"{shlex.quote(sys.executable)} -m claude_tg_bot.untrusted || exit 2"
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    pre.append({"matcher": "*", "hooks": [{"type": "command", "command": guard, "timeout": 30}]})
    # Таймаут или сбой хука без onFailure пропускает действие (проверено на
    # 2.1.303 19.10.2026), с block отклоняет: здесь это для обоих хуков.
    for entry in pre:
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict):
                hook["onFailure"] = "block"
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
    if _untrusted_dir is None or not _untrusted_dir.is_dir():
        _untrusted_dir = Path(tempfile.mkdtemp(prefix="claude-tg-untrusted-"))
        atexit.register(shutil.rmtree, _untrusted_dir, True)
    path = _untrusted_dir / f"settings-{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
    if not path.exists():
        _atomic_write_text(path, text)
    return path


async def run_claude(
    claude_bin: str,
    chat_key: str,
    target_chat_id: int,
    target_thread_id: int | None,
    prompt: str,
    store: SessionStore,
    *,
    cwd: Path,
    hook_settings: Path,
    persona: str = "",
    memory_scope_hint: str = "",
    effort: str = "",
    model: str = "",
    max_runtime_sec: int = 0,
    process_registry: dict[str, asyncio.subprocess.Process] | None = None,
    session_registry: dict[str, str] | None = None,
    session_targets: dict[str, tuple[int, int | None]] | None = None,
    scope_store: SessionStore | None = None,
    progress_cb: Callable[[str], Awaitable[None]] | None = None,
    activity_cb: Callable[[], None] | None = None,
    permission_mode: str = "bypassPermissions",
    plan_agents: bool = False,
    unattended_sids: set[str] | None = None,
    untrusted_sids: set[str] | None = None,
    on_session_start: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    cost_totals: MutableMapping[str, str] | None = None,
    autocompact: str = "",
    on_run_start: Callable[[], None] | None = None,
    unexited: list[asyncio.subprocess.Process] | None = None,
    ask_id: int | None = None,
) -> ClaudeResult:
    """Запустить claude -p для конкретного чата/темы.

    cost_totals: накопительный total_cost_usd по session_id с прошлого ответа
    (переживает рестарт), из него цена этого ответа (_answer_cost).

    chat_key — идентификатор разговора (str), используется как ключ для
        SessionStore, process_registry и session_registry. Для частного чата
        это `f"{chat_id}"`; для forum-thread — `f"{chat_id}:{thread_id}"`.
    target_chat_id, target_thread_id — куда отправлять approve-карточки и
        системные уведомления. Регистрируются в session_targets[session_id]
        чтобы handle_approve по hook-payload (где есть только session_id) мог
        достать правильный destination, а не падать на cfg.primary_chat_id.
    max_runtime_sec — жёсткий потолок выполнения; 0 = без таймаута.
    memory_scope_hint - сохранённый scope Telegram-темы для read-only hook
        общей памяти. Передаётся только в окружении дочернего Claude-процесса.
    plan_agents - в режиме plan не запрещать субагентов (PLAN_AGENT_TOOLS):
        обдумывание темы задачи, где Иван у карточек.
    untrusted_sids - не None для рана по чужому тексту: набор инструментов
        прежний, режим dontAsk с правилами ask (UNTRUSTED_ASK_TOOLS), sid
        регистрируется на время рана, и хук отклоняет всё, кроме MCP на чтение.
    on_session_start - зовётся с session_id каждой попытки до запуска
        процесса: бот пишет по нему постоянные метки раньше, чем промпт
        попадёт в транскрипт (ревью 12.10.2026).
    on_run_start - зовётся один раз за ран, когда процесс создан, до подачи
        промпта: отказ до процесса (settings, ошибка запуска, остановка) его
        не вызовет. Исключение из него гасит процесс до промпта и уходит
        вызывающему (ревью Codex 14.10).
    activity_cb - зовётся на каждое событие stream-json (не только tool_use):
        по нему бот считает тишину рана для сторожа зависаний.
    should_stop - True, когда бот останавливается: сверяется перед каждой
        попыткой и сразу после создания процесса. Сигнал остановки, пришедший
        во время подготовки OAuth, гасит только уже известные процессы, и
        запущенный после него claude обрывал бы рестарт (ревью 11.10.2026).
    unexited - сюда процесс попытки, не вышедший и после SIGKILL (_reap_proc):
        регистрацию в process_registry ран снимает, а бот держит проект
        задачи до его выхода (ревью Codex 14.10).
    """
    # F1: пере-валидация hook settings если файл менялся.
    # Fail-closed: если hook стал невалиден, отказываемся запускать Claude
    # вместо того, чтобы дать bypassPermissions без работающих approve-кнопок.
    try:
        _ensure_hook_still_valid(hook_settings)
    except RuntimeError as e:
        log.error("run_claude refusing: hook settings invalid: %s", e)
        return ClaudeResult(text=_("⚠️ hook settings невалидны, claude не запущен:\n{error}").format(error=e))
    settings_path = hook_settings
    if untrusted_sids is not None:
        # Без правил ask ран по чужому тексту держал бы один замок: отказ.
        try:
            settings_path = untrusted_settings_path(hook_settings)
        except (OSError, ValueError) as e:
            log.error("run_claude refusing: untrusted settings: %s", e)
            return ClaudeResult(
                text=_("⚠️ настройки рана по чужому тексту не собраны, claude не запущен:\n{error}").format(error=e),
                is_error=True,
            )

    # Pre-flight: освежить OAuth-токен, если он на грани протухания. Иначе после
    # долгого простоя первый запрос мог упасть с 401 (инцидент 2026-06-20).
    await ensure_oauth_fresh(claude_bin)

    # bypassPermissions нужен, чтобы headless не виснул на approve для безопасных tools.
    # Для destructive tools PreToolUse-hook перехватит и спросит через Telegram.
    # Prompt передаётся через stdin (НЕ argv), чтобы не светиться в ps/cmdline
    # и не упираться в ARG_MAX.
    common = [
        "-p",
        # stream-json: те же данные, что и json (финальное событие type=result),
        # плюс события tool_use по ходу рана — из них бот строит живой статус.
        # --verbose обязателен для stream-json в print-режиме.
        "--output-format", "stream-json",
        "--verbose",
        # bypassPermissions по умолчанию; /plan передаёт "plan" — тогда Claude
        # сначала предлагает план через ExitPlanMode (карточка с кнопками), а
        # после одобрения мутации идут через обычный approve-hook.
        # Ран по чужому тексту в dontAsk: правило ask там отказ без вопроса.
        "--permission-mode", "dontAsk" if untrusted_sids is not None else permission_mode,
        "--settings", str(settings_path),
        # Запрет multi-agent роя. При bypassPermissions approve-hook ловит только
        # Bash|Write|Edit|... — Workflow в matcher НЕ входит и выполнялся без тормозов.
        # Инцидент: deep-research развернул ~110 субагентов / ~14M токенов за ~13 мин
        # и выжег месячный лимит. Variadic <tools...> завершается следующим флагом
        # (--effort/--model/--resume), поэтому одного значения достаточно.
        "--disallowedTools", "Workflow",
        *(tuple(t for t in PLAN_DISALLOWED_TOOLS if not (plan_agents and t in PLAN_AGENT_TOOLS))
          if permission_mode == "plan" else ()),
    ]
    if untrusted_sids is not None:
        # Не --tools: набор инструментов тот же, что у обычного рана (см.
        # UNTRUSTED_ALLOWED_TOOLS). Новые встроенные инструменты ловит
        # untrusted_unknown_tools по событию init.
        common.extend(["--allowedTools", *UNTRUSTED_ALLOWED_TOOLS])
    if effort:
        # Передаём всегда, а не полагаемся на settings.json/env: флаг (CLI-arg)
        # детерминированно бьёт обе нижние ступени precedence, поэтому effort
        # не «уплывёт» от inherited effortLevel пользователя.
        common.extend(["--effort", effort])
    if model:
        # Явная модель: дефолт CLI может указывать на недоступную аккаунту модель
        # (напр. fable-5 -> 404). Флаг детерминированно перекрывает дефолт.
        common.extend(["--model", model])
    if autocompact:
        # Окно автосжатия: сжатие и посреди рана, как только контекст перевалил
        # за окно (проверено на 2.1.296 13.10.2026: два сжатия за ран в -p).
        common.extend(["--autocompact", autocompact])
    if persona:
        # Telegram-persona: добавляется к системному промпту claude.
        # Пусто = не добавляем (вариант для обычного терминального cwd).
        common.extend(["--append-system-prompt", persona])

    # Без секретов бота: хук подтверждения читает .env сам (config.agent_env).
    child_env = agent_env()
    # Тема рана: заявка на перезапуск (restart_request.py) шлёт ход
    # перезапуска туда, откуда её подали.
    child_env["CLAUDE_TG_CHAT_KEY"] = chat_key
    # Сообщение Ивана, на которое отвечает ран: задача, заведённая из рана
    # (task_new), ответит на него строкой итога после закрытия (19.10.2026).
    if ask_id:
        child_env["CLAUDE_TG_ASK_ID"] = str(ask_id)
    else:
        child_env.pop("CLAUDE_TG_ASK_ID", None)
    if memory_scope_hint:
        child_env["AGENT_MEMORY_SCOPE_HINT"] = memory_scope_hint
    else:
        child_env.pop("AGENT_MEMORY_SCOPE_HINT", None)

    async def _attempt(resume_sid: str | None) -> _Attempt | None:
        """Один запуск claude-подпроцесса. resume_sid=None => свежая сессия.

        None: бот останавливается, процесс не запущен или сразу погашен."""
        nonlocal on_run_start
        if should_stop is not None and should_stop():
            return None
        if resume_sid:
            args = [claude_bin, *common, "--resume", resume_sid]
            active_sid = resume_sid
            sid_short = resume_sid[:8]
        else:
            new_sid = new_session_id()
            args = [claude_bin, *common, "--session-id", new_sid]
            active_sid = new_sid
            sid_short = new_sid[:8] + "(new)"
        if on_session_start is not None:
            # До запуска, а не после рана: отмена, таймаут или аварийный
            # рестарт бота посреди рана не оставят транскрипт без метки.
            on_session_start(active_sid)

        log.info(
            "claude start chat=%s sid=%s cwd=%s effort=%s",
            chat_key, sid_short, cwd, effort or "(default)",
        )
        # start_new_session=True: claude становится session+pgroup leader, его дети
        # (Bash-tool, MCP-серверы и т.п.) попадают в ту же pgroup. Это позволяет
        # /cancel и timeout убивать ВСЁ поддерево через os.killpg, а не только верх.
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=child_env,
            start_new_session=True,
            limit=_STREAM_LINE_LIMIT,
        )
        if should_stop is not None and should_stop():
            # Сигнал пришёл, пока процесс создавался: обработчик остановки
            # его ещё не видел (ревью 11.10.2026).
            log.warning("claude start chat=%s cancelled: bot stopping", chat_key)
            kill_process_tree(proc, signal.SIGKILL)
            await _reap_proc(proc)
            return None
        if process_registry is not None:
            process_registry[chat_key] = proc
        # Регистрируем active session ДО await-а: /cancel пришедший прямо сейчас
        # должен видеть sid даже для свежей сессии (которую store узнает только
        # после успешного JSON-ответа от claude).
        if session_registry is not None:
            session_registry[chat_key] = active_sid
        if session_targets is not None:
            # Обратная мапа: session_id -> destination (chat_id, thread_id).
            # Нужна handle_approve, который из hook payload получает только sid.
            session_targets[active_sid] = (target_chat_id, target_thread_id)
        if unattended_sids is not None:
            # Пометка «человека за клавиатурой нет»: handle_approve не применит
            # к этой сессии открытое окно /auto.
            unattended_sids.add(active_sid)
        if untrusted_sids is not None:
            untrusted_sids.add(active_sid)
        timed_out = False
        final_event: dict | None = None
        result_texts: list[str] = []
        ctx_last: int | None = None
        ctx_max = 0
        compacted = 0
        blocked_tools: list[str] = []
        init_checked = False
        # Повтор свежей сессией после протухшего --resume не новый старт рана.
        run_start, on_run_start = on_run_start, None

        async def _drive() -> tuple[bytes, bytes]:
            """Скормить prompt в stdin и читать stream-json построчно.

            Замена communicate(): события tool_use по ходу рана уходят в
            progress_cb, финальное type=result сохраняется в final_event.
            stderr читается конкурентной таской — иначе заполненный pipe
            (64 KiB) взаимно заблокировал бы нас и claude.
            """
            nonlocal final_event, ctx_last, ctx_max, compacted, init_checked
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

            async def _readline() -> bytes:
                # Строку длиннее _STREAM_LINE_LIMIT (Read большого PDF или
                # картинки) пропускаем целиком с предупреждением, а не роняем
                # ран: следующий result всё равно дойдёт (ревью 11.10.2026).
                skipped = 0
                while True:
                    try:
                        line = await proc.stdout.readuntil(b"\n")
                    except asyncio.IncompleteReadError as e:
                        line = e.partial
                    except asyncio.LimitOverrunError as e:
                        skipped += len(await proc.stdout.read(e.consumed))
                        continue
                    if not skipped:
                        return line
                    log.warning(
                        "stream-json: строка длиннее %d байт пропущена (%d байт)",
                        _STREAM_LINE_LIMIT, skipped + len(line),
                    )
                    if not line:
                        return line
                    skipped = 0

            stderr_task = asyncio.create_task(proc.stderr.read())
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
                lines: list[bytes] = []
                while True:
                    line = await _readline()
                    if not line:
                        break
                    lines.append(line)
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(evt, dict):
                        continue
                    if activity_cb is not None:
                        try:
                            activity_cb()
                        except Exception:
                            log.debug("activity_cb failed (ignored)", exc_info=True)
                    if untrusted_sids is not None and not init_checked:
                        # Сигнал, а не граница: claude не ждёт бота, граница это
                        # замки (модуль untrusted). Без init до ответа модели
                        # или с неверным списком ран тоже останавливается.
                        if evt.get("type") == "system" and evt.get("subtype") == "init":
                            init_checked = True
                            blocked_tools.extend(untrusted_unknown_tools(evt.get("tools")))
                        elif evt.get("type") == "assistant" or (
                                evt.get("type") == "result" and evt.get("is_error") is not True):
                            blocked_tools.append(_("(событие init не пришло)"))
                        if blocked_tools:
                            log.error("untrusted run stopped chat=%s: no lock for tools %s",
                                      chat_key, ", ".join(blocked_tools))
                            kill_process_tree(proc, signal.SIGKILL)
                            stderr_task.cancel()
                            await asyncio.gather(stderr_task, return_exceptions=True)
                            await _reap_proc(proc)
                            return b"".join(lines), b""
                    if evt.get("type") == "result":
                        final_event = evt
                        said = evt.get("result")
                        if isinstance(said, str) and said.strip() and evt.get("is_error") is not True:
                            result_texts.append(said)
                        continue
                    size = _request_context(evt)
                    if size is not None:
                        ctx_last = size
                        ctx_max = max(ctx_max, size)
                    elif evt.get("type") == "system" and evt.get("subtype") == "compact_boundary":
                        # Прежний размер устарел, новый узнаем на следующем запросе.
                        ctx_last = None
                        compacted += 1
                    if progress_cb is None:
                        continue
                    desc = _describe_tool_use(evt)
                    if desc:
                        try:
                            await progress_cb(desc)
                        except Exception:
                            log.debug("progress_cb failed (ignored)", exc_info=True)
                stdout = b"".join(lines)
                stderr = await stderr_task
            except BaseException:
                stderr_task.cancel()
                # Завершаем читателя перед очисткой процесса (ревью 12.10.2026).
                await asyncio.gather(stderr_task, return_exceptions=True)
                raise
            await proc.wait()
            return stdout, stderr

        try:
            if run_start is not None:
                # Процесс есть, промпта он ещё не получил: ошибка отметки ниже
                # гасит его до промпта (ревью Codex 14.10).
                run_start()
            # max_runtime_sec=0 => без таймаута (старое поведение). Иначе wrap в
            # wait_for: при срабатывании таймаута kill-аем, чтобы proc не висел
            # зомби, потом возвращаем понятную ошибку.
            if max_runtime_sec and max_runtime_sec > 0:
                try:
                    stdout, stderr = await asyncio.wait_for(_drive(), timeout=max_runtime_sec)
                except asyncio.TimeoutError:
                    timed_out = True
                    log.warning(
                        "claude timeout chat=%s sid=%s after %ds — killing pgroup",
                        chat_key, sid_short, max_runtime_sec,
                    )
                    # killpg(SIGKILL) кладёт claude и всех его детей. SIGKILL
                    # сразу, без вежливого SIGTERM — таймаут уже истёк, дальше
                    # ждать нет смысла, и SIGKILL гарантированно завершит.
                    # _reap_proc, не голый wait(): если claude завис в
                    # uninterruptible sleep (D-state, на WSL реально при I/O на
                    # drvfs/9p), безлимитный wait() держал бы chat_lock навечно.
                    kill_process_tree(proc, signal.SIGKILL)
                    await _reap_proc(proc)
                    stdout, stderr = b"", b""
            else:
                stdout, stderr = await _drive()
        except asyncio.CancelledError:
            # Task отменяется (shutdown, gather cancel и т.п.). Без kill claude
            # и его дети (Bash и пр.) останутся зомбями: finally только убирает
            # из registry, а shutdown-cleanup уже не увидит этот proc.
            kill_process_tree(proc, signal.SIGKILL)
            # _reap_proc: shield от повторного cancel + таймаут против D-state.
            await _reap_proc(proc)
            raise
        except Exception:
            # Ошибка потока тоже завершает группу до снятия регистрации (ревью 12.10.2026).
            kill_process_tree(proc, signal.SIGKILL)
            await _reap_proc(proc)
            raise
        finally:
            if process_registry is not None and process_registry.get(chat_key) is proc:
                process_registry.pop(chat_key, None)
            if unexited is not None and proc.returncode is None:
                unexited.append(proc)
            if session_registry is not None and session_registry.get(chat_key) == active_sid:
                session_registry.pop(chat_key, None)
            if session_targets is not None:
                session_targets.pop(active_sid, None)
            if unattended_sids is not None:
                unattended_sids.discard(active_sid)
            if untrusted_sids is not None:
                untrusted_sids.discard(active_sid)
            # F2: abandoned approvals cleanup делает bot.py после run_claude —
            # там доступен ctx.bot для edit_message_reply_markup (в run_claude
            # его нет, а async edit в finally рискует при CancelledError).
            # sid возвращается через ClaudeResult.session_id.

        return _Attempt(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            active_sid=active_sid,
            resumed=bool(resume_sid),
            final_event=final_event,
            result_texts=result_texts,
            ctx_last=ctx_last,
            ctx_max=ctx_max,
            compacted=compacted,
            blocked_tools=blocked_tools,
        )

    # Роутер памяти закрепляет проект за сессией по первому сообщению и внутри
    # сессии его НЕ меняет. Значит возобновлять сессию можно только пока
    # эффективный scope темы тот же: иначе бот подаёт новую рамку, а память
    # остаётся от прежнего проекта. Сверка идёт ЗДЕСЬ, перед каждым --resume,
    # а не в обработчике переименования: тот может не сработать (занятый чат,
    # рестарт), и тогда старая сессия жила бы вечно.
    existing = resume_target(store, scope_store, chat_key, memory_scope_hint)

    stopped = ClaudeResult(text=_("⛔ отменено: бот останавливается"), is_error=True)
    att = await _attempt(existing)
    if att is None:
        return stopped

    # Авто-восстановление протухшей --resume сессии: если claude не нашёл
    # session_id из sessions.json (он мёртв в ~/.claude/projects), без этого
    # КАЖДОЕ сообщение в чате падало бы rc=1 до ручного /reset. Сбрасываем
    # мёртвый sid и один раз ретраим со свежей сессией (контекст всё равно
    # потерян — старой сессии не существует).
    if (
        att.resumed
        and not att.timed_out
        and att.returncode not in (0, -15, -9, 143, 137)
        and _is_stale_session_error(att.stderr.decode(errors="replace"))
    ):
        log.warning(
            "claude resume failed (stale session %s) — dropping и ретраю свежей сессией",
            (existing or "")[:8],
        )
        store.remove(chat_key)
        att = await _attempt(None)
        if att is None:
            return stopped

    if att.blocked_tools:
        return ClaudeResult(
            text=_("⛔ ран по чужому тексту остановлен: нет замка у инструментов {tools} "
                   "(claude_runner.UNTRUSTED_ASK_TOOLS)").format(tools=", ".join(att.blocked_tools)),
            session_id=att.active_sid, is_error=True,
        )

    if att.timed_out:
        # Ошибка рана, как у codex: выполнение задачи по таймауту не «готово»,
        # и ответ таймаута не становится планом (ревью Codex 16.10).
        return ClaudeResult(
            text=_("⛔ claude убит по таймауту ({timeout}s)").format(timeout=max_runtime_sec),
            session_id=att.active_sid, is_error=True, texts=tuple(att.result_texts),
        )

    if att.returncode != 0 or (att.final_event and att.final_event.get("is_error") is True):
        # -15 = SIGTERM (наш /cancel), -9 = SIGKILL
        if att.returncode in (-15, -9, 143, 137):
            return ClaudeResult(
                text=_("⛔ отменено (signal {signal})").format(signal=att.returncode),
                session_id=att.active_sid, is_error=True, texts=tuple(att.result_texts),
            )
        # Часть фатальных ошибок claude (refusal, недоступная модель, лимиты, API)
        # уходит в stdout, а не в stderr — в stream-json это финальное событие
        # type=result (собрано в _drive). Показываем читаемый .result +
        # подсказку, а не сырой блоб/голый rc=N.
        raw_out = att.stdout.decode(errors="replace")
        out = att.final_event
        stderr_txt = att.stderr.decode(errors="replace").strip()
        total, _unused = _usage_stats(out) if out else (0.0, 0)
        err_sid = (out.get("session_id") if out else None) or att.active_sid
        cost = _answer_cost(cost_totals, err_sid, total, att.resumed) if total > 0 else 0.0
        text = _format_error_text(att.returncode, out, stderr_txt, raw_out)
        log.error(
            "claude rc=%s stop=%s api_status=%s msg=%r",
            att.returncode,
            out.get("stop_reason") if out else None,
            out.get("api_error_status") if out else None,
            str((out.get("result") or out.get("errors") or stderr_txt) if out else stderr_txt)[:200],
        )
        api_status = out.get("api_error_status") if out else None
        diagnostic = stderr_txt + "\n" + str(out.get("result", "") if out else "")
        return ClaudeResult(
            text=text, session_id=att.active_sid,
            cost_usd=cost, session_cost_usd=total,
            context_tokens=att.ctx_last or 0, context_max=att.ctx_max, compacted=att.compacted,
            is_error=True,
            api_error_status=api_status if isinstance(api_status, int) else None,
            auth_error="claude" if api_status == 401 or is_auth_error(diagnostic) else "",
            texts=tuple(att.result_texts),
        )

    out = att.final_event
    if out is None:
        raw = att.stdout.decode(errors="replace")
        log.warning("claude stream без result-события; first 200 chars: %r", raw[:200])
        # session_id не узнаем от claude — возвращаем тот, с которым запустились.
        # raw — NDJSON-события, пользователю целиком не показываем.
        return ClaudeResult(
            text=raw[:1000] or _("[пустой ответ]"),
            session_lost=not att.resumed,
            session_id=att.active_sid,
        )

    result_text = out.get("result") or out.get("response") or _("[пустой ответ]")
    sid = out.get("session_id") or att.active_sid
    total, turn_input = _usage_stats(out)
    cost = _answer_cost(cost_totals, sid, total, att.resumed)
    if out.get("session_id"):
        store.set(chat_key, sid)
        session_lost = False
    else:
        session_lost = not att.resumed
    # ctx это вход последнего запроса, max за ран, input_sum прежняя сумма по
    # всем запросам рана (то, что до 13.10.2026 писалось как ctx).
    log.info("claude done chat=%s sid=%s result_len=%d cost=%s session=$%.4f ctx=%s max=%d input_sum=%d",
             chat_key, sid[:8], len(result_text),
             f"${cost:.4f}" if cost is not None else "?", total,
             att.ctx_last if att.ctx_last is not None else "?", att.ctx_max, turn_input)
    return ClaudeResult(
        text=result_text, session_lost=session_lost, session_id=sid,
        cost_usd=cost, session_cost_usd=total,
        context_tokens=att.ctx_last or 0, context_max=att.ctx_max, compacted=att.compacted,
        texts=tuple(att.result_texts),
    )

"""Перезапуск бота по заявке (план 13.10.2026, разбор Codex).

Агент внутри рана бота не перезапускает бота сам: `systemctl restart` убивал
его собственный ран и чужие раны других тем. Он подаёт заявку
(`python -m claude_tg_bot.restart_request`), бот записывает её на диск,
перестаёт брать новую работу, ждёт конца всех текущих обработок, проверяет
новый код отдельным процессом и выходит с кодом EXIT_RESTART. systemd
(`Restart=on-failure`) поднимает его снова, после старта бот сообщает версию.

Здесь логика без Telegram: заявка на диске, состояние git, проверка кода и
цикл ожидания с внедрёнными функциями, чтобы его можно было проверить тестом.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .i18n import _, plural

log = logging.getLogger(__name__)

# Не 0 и не 1: systemd с Restart=on-failure перезапускает при любом ненулевом
# коде, а 75 (EX_TEMPFAIL) в журнале отличим от падения. В SuccessExitStatus
# и RestartPreventExitStatus юнита его быть не должно.
EXIT_RESTART = 75

REPO = Path(__file__).resolve().parents[2]
REQUEST_NAME = "restart_request.json"
# Пути, изменение которых меняет работающий код. state/ и прочее не в счёт.
CODE_PATHS = ("src", "tests", "pyproject.toml")

PHASE_PENDING = "pending"
PHASE_RESTARTING = "restarting"

# Через сколько секунд ожидания спросить человека кнопками.
ASK_AFTER_SEC = 15 * 60
VALIDATE_TIMEOUT_SEC = 300


# --- заявка на диске ---------------------------------------------------------

def request_path(state_dir: Path) -> Path:
    return state_dir / REQUEST_NAME


def load_request(state_dir: Path) -> dict | None:
    try:
        data = json.loads(request_path(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.exception("restart request unreadable, ignoring")
        return None
    return data if isinstance(data, dict) and data.get("id") else None


def save_request(state_dir: Path, req: dict) -> None:
    """Атомарно: временный файл рядом и os.replace."""
    path = request_path(state_dir)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_request(state_dir: Path) -> None:
    try:
        request_path(state_dir).unlink()
    except FileNotFoundError:
        pass


def new_request(chat_key: str, reason: str, commit: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "chat_key": chat_key,
        "reason": reason,
        "commit": commit,
        "phase": PHASE_PENDING,
        "created": int(time.time()),
    }


# --- git ---------------------------------------------------------------------

def git_state(repo: Path = REPO) -> tuple[str, list[str]]:
    """(HEAD, изменённые и неотслеживаемые пути кода). HEAD пустой при ошибке."""
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=normal",
             "--", *CODE_PATHS],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        log.exception("git state failed")
        return "", []
    dirty = [line[3:] for line in status.splitlines() if line.strip()]
    return head, dirty


def code_problem(commit: str, repo: Path = REPO) -> str:
    """Почему код не тот, что в заявке (пусто, если тот)."""
    head, dirty = git_state(repo)
    if not head:
        return _("не удалось прочитать состояние git")
    if dirty:
        shown = ", ".join(dirty[:5]) + (_(" и др.") if len(dirty) > 5 else "")
        return _("в коде есть незакоммиченные правки: {shown}").format(shown=shown)
    if commit and head != commit:
        return _("коммит сменился после заявки: было {old}, стало {new}").format(
            old=commit[:7], new=head[:7])
    return ""


# --- проверка нового кода ------------------------------------------------------

_IMPORT_CHECK = (
    "import importlib, pkgutil, claude_tg_bot\n"
    "for m in pkgutil.walk_packages(claude_tg_bot.__path__, 'claude_tg_bot.'):\n"
    "    if m.name.endswith('__main__'): continue\n"
    "    importlib.import_module(m.name)\n"
)


def fresh_env() -> dict[str, str]:
    """Окружение, как у свежего запуска сервиса: снимок до load_dotenv
    (config.ENV_BEFORE_DOTENV). Иначе load_dotenv в проверке не перезаписал
    бы значения старого .env, а удалённые из файла ключи остались бы
    (ревью Codex 13.10.2026). Без снимка (процесс не грузил конфиг) текущее."""
    from . import config
    return dict(config.ENV_BEFORE_DOTENV if config.ENV_BEFORE_DOTENV is not None else os.environ)


async def _run(args: list[str], timeout: float, cwd: Path,
               env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _unused = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        # Вся группа: дочерние процессы тестов держали бы вывод открытым.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return -1, _("не уложилось в {seconds} с").format(seconds=int(timeout))
    return proc.returncode or 0, out.decode(errors="replace")


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


async def validate_code(repo: Path = REPO, python: str = sys.executable,
                        timeout: float = VALIDATE_TIMEOUT_SEC) -> str:
    """Отдельными процессами: импорт всех модулей, --check, тесты.

    Тесты через pytest: он берёт и unittest-классы, и pytest-функции, а
    unittest discover последние пропускал (ревью Codex 13.10.2026).
    Пусто, если всё прошло, иначе короткая причина с хвостом вывода."""
    steps = [
        (_("импорт модулей"), [python, "-c", _IMPORT_CHECK]),
        (_("проверка конфигурации"), [python, "-m", "claude_tg_bot", "--check"]),
        (_("тесты"), [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]),
    ]
    env = fresh_env()
    for name, args in steps:
        rc, out = await _run(args, timeout, repo, env)
        if rc != 0:
            return _("{name}: код {rc}\n{tail}").format(name=name, rc=rc, tail=_tail(out))
    return ""


# --- ожидание и перезапуск ----------------------------------------------------

@dataclass
class Drain:
    """Цикл ожидания по одной заявке. Telegram и бот снаружи, через функции.

    active(): сколько обработок ещё идёт (0 = можно перезапускаться).
    notify(text, ask): сообщение в тему заявки; ask=True с кнопками.
    validate(): проверка кода, пусто = годен.
    restart(): штатный выход с EXIT_RESTART.
    abort(text): отмена заявки с причиной.
    """
    req: dict
    active: Callable[[], int]
    notify: Callable[[str, bool], Awaitable[None]]
    validate: Callable[[], Awaitable[str]]
    code_problem: Callable[[str], str]
    restart: Callable[[], Awaitable[None]]
    abort: Callable[[str], Awaitable[None]]
    poll_sec: float = 2.0
    ask_after_sec: float = ASK_AFTER_SEC
    clock: Callable[[], float] = time.monotonic
    now_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False

    async def run(self) -> None:
        started = self.clock()
        asked = False
        n = self.active()
        await self.notify(
            _("♻️ Заявка на перезапуск принята: {reason}.\n").format(
                reason=self.req.get('reason') or _('без причины'))
            + (_("Жду окончания работы: {count}. Новые сообщения пока не беру.").format(count=n)
               if n else _("Активной работы нет, проверяю код.")),
            False,
        )
        while self.active() > 0 and not self.now_event.is_set():
            if self.cancelled:
                return
            if not asked and self.clock() - started >= self.ask_after_sec:
                asked = True
                minutes = int(self.ask_after_sec // 60)
                await self.notify(
                    _("⏳ Жду уже {minutes} {unit}, работы ещё {count}. "
                      "Перезапустить сейчас (идущие раны прервутся), ждать дальше или отменить заявку?").format(
                        minutes=minutes, unit=plural(minutes, ("минуту", "минуты", "минут"), ("minute", "minutes")),
                        count=self.active()),
                    True,
                )
            try:
                await asyncio.wait_for(self.now_event.wait(), self.poll_sec)
            except asyncio.TimeoutError:
                pass
        if self.cancelled:
            return
        problem = await asyncio.to_thread(self.code_problem, self.req.get("commit", ""))
        if not problem:
            problem = await self.validate()
        if not problem:
            # Ещё раз: пока шла проверка, код мог поменяться (ревью Codex).
            problem = await asyncio.to_thread(self.code_problem, self.req.get("commit", ""))
        if self.cancelled:
            return
        if problem:
            await self.abort(_("❌ Перезапуск отменён, бот работает на прежнем коде.\n{problem}").format(
                problem=problem))
            return
        await self.restart()

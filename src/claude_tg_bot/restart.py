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
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import jsonstore, paths
from . import procs
from .config import agent_env
from .i18n import _, plural

log = logging.getLogger(__name__)

# Не 0 и не 1: systemd с Restart=on-failure перезапускает при любом ненулевом
# коде, а 75 (EX_TEMPFAIL) в журнале отличим от падения. В SuccessExitStatus
# и RestartPreventExitStatus юнита его быть не должно.
EXIT_RESTART = 75

REPO = paths.BOT_ROOT
REQUEST_NAME = "restart_request.json"
# Неотслеживаемые файлы в счёт только здесь: новый модуль меняет код, а
# черновик в корне или state/ нет. Правка отслеживаемого файла в счёт везде,
# кроме документов: бот читает и файлы корня (ревью Opus 04.11.2026).
CODE_PATHS = ("src", "tests", "pyproject.toml")
# Коммит после заявки только в эти файлы заявку не отменяет: бот их не читает.
# Список разрешённых, а не запрещённых путей: бот читает и файлы корня
# (telegram-persona.md, claude-hook-settings.json), их коммит меняет работу
# так же, как код (ревью Codex и решение Ивана 04.11.2026).
DOC_FILES = ("BACKLOG.md", "README.md")
DOC_DIRS = ("docs/",)
_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")

PHASE_PENDING = "pending"
PHASE_RESTARTING = "restarting"

# Через сколько секунд ожидания спросить человека кнопками.
ASK_AFTER_SEC = 15 * 60
VALIDATE_TIMEOUT_SEC = 300


# --- заявка на диске ---------------------------------------------------------

def request_path(state_dir: Path) -> Path:
    return state_dir / REQUEST_NAME


def load_request(state_dir: Path) -> dict | None:
    """Битая заявка не читается и откладывается рядом (jsonstore, в журнал)."""
    data = jsonstore.read_dict(request_path(state_dir))
    return data if data.get("id") else None


def save_request(state_dir: Path, req: dict) -> None:
    jsonstore.write_json(request_path(state_dir), req, indent=2)


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
    """(HEAD, незакоммиченные пути, которые меняют работу). HEAD пустой при ошибке.

    Отслеживаемые файлы везде, кроме документов (как у `code_problem` для
    коммитов), неотслеживаемые только в CODE_PATHS."""
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=True, env=agent_env(),
        ).stdout.strip()
        # Байты: имя постороннего файла не в UTF-8 не должно ронять проверку.
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "-z", "--no-renames",
             "--untracked-files=normal"],
            capture_output=True, timeout=30, check=True, env=agent_env(),
        ).stdout.decode("utf-8", "surrogateescape")
    except (OSError, subprocess.SubprocessError, ValueError):
        log.exception("git state failed")
        return "", []
    dirty = []
    for entry in status.split("\0"):
        if len(entry) < 4:
            continue
        # Имя не в UTF-8 уйдёт в текст отмены: без одиночных суррогатов.
        code = entry[:2]
        path = entry[3:].encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        if code == "??":
            if path.startswith(tuple(p + "/" for p in CODE_PATHS)) or path in CODE_PATHS:
                dirty.append(path)
        elif not _doc_only(path):
            dirty.append(path)
    return head, dirty


def changed_since(commit: str, head: str, repo: Path = REPO) -> list[str] | None:
    """Файлы, изменённые между коммитом заявки и HEAD; None при ошибке git.

    Сравнение деревьев, а не история: rebase и amend ему не мешают.
    --no-renames: при переименовании из корня в docs/ виден и старый путь,
    иначе git показал бы только новый, в docs/ (ревью Opus 04.11)."""
    if not _COMMIT_RE.fullmatch(commit):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "diff", "-z", "--name-only", "--no-renames",
             "--no-ext-diff", commit, head, "--"],
            capture_output=True, text=True, timeout=30, check=True, env=agent_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # ValueError: имя файла не в UTF-8 (UnicodeDecodeError). Пропавший
        # коммит заявки (rebase, gc) обычен: traceback в журнале не нужен.
        log.warning("git diff since request failed: %s", type(exc).__name__)
        return None
    return [p for p in out.split("\0") if p]


def _doc_only(path: str) -> bool:
    return path in DOC_FILES or path.startswith(DOC_DIRS)


def code_problem(commit: str, repo: Path = REPO) -> str:
    """Почему код не тот, что в заявке (пусто, если тот).

    Новый коммит только в документацию (DOC_FILES, DOC_DIRS) заявку не
    отменяет, перезапуск идёт на нём: заявку 051edfa5 04.11.2026 отменил
    коммит одного бэклога."""
    head, dirty = git_state(repo)
    if not head:
        return _("не удалось прочитать состояние git")
    if dirty:
        shown = ", ".join(dirty[:5]) + (_(" и др.") if len(dirty) > 5 else "")
        return _("в коде есть незакоммиченные правки: {shown}").format(shown=shown)
    if commit and head != commit:
        changed = changed_since(commit, head, repo)
        if changed is None:
            return _("коммит сменился после заявки: было {old}, стало {new}, "
                     "сравнить их не удалось").format(old=commit[:7], new=head[:7])
        foreign = [p for p in changed if not _doc_only(p)]
        if foreign:
            shown = ", ".join(foreign[:5]) + (_(" и др.") if len(foreign) > 5 else "")
            return _("коммит сменился после заявки: было {old}, стало {new}, "
                     "изменены не только документы: {shown}").format(
                old=commit[:7], new=head[:7], shown=shown)
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
    # Своя группа: на таймауте и отмене гаснут и дочерние процессы тестов,
    # которые держали бы вывод открытым (procs.run_bounded).
    try:
        rc, out, _unused = await procs.run_bounded(args, timeout=timeout, env=env, cwd=cwd,
                                                   stderr=asyncio.subprocess.STDOUT)
    except TimeoutError:
        return -1, _("не уложилось в {seconds} с").format(seconds=int(timeout))
    return rc or 0, out.decode(errors="replace")


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

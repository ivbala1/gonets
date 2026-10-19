"""Общий запуск headless `claude` для oneshot-скиллов (сводки чатов и новостей).

Зачем не `subprocess.run(timeout=...)`: на TimeoutExpired он шлёт SIGKILL
только верхнему процессу (`claude`), а его дети — MCP-серверы (Gmail/Slack),
Bash-tool и т.п. — осиротеют и продолжат жить (в т.ч. с доступом к почте/Slack).
Тот же класс бага в интерактивном боте закрыт через start_new_session=True +
kill_process_tree (см. claude_runner.run_claude); здесь — sync-аналог:
claude становится лидером новой process group, и по таймауту мы убиваем ВСЮ
группу через os.killpg.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess

from dotenv import load_dotenv

from ..config import agent_env
from ..i18n import _


def resolve_claude_bin() -> str:
    """Абсолютный путь к `claude` для oneshot-скиллов.

    Скиллы не поднимают полный config, поэтому берут тот же приоритет, что и он:
    CLAUDE_BIN из .env или окружения, иначе PATH. Хардкодить путь нельзя: он
    меняется при смене способа установки Claude Code (npm/nvm → нативный билд).
    """
    load_dotenv()
    configured = os.environ.get("CLAUDE_BIN", "").strip()
    if configured:
        return configured
    resolved = shutil.which("claude")
    if resolved is None:
        raise RuntimeError(
            _("claude не найден: задай CLAUDE_BIN в .env или добавь его в PATH")
        )
    return resolved


# Канонические области памяти для плановых задач: через запятую в .env бота.
MEMORY_SCOPES_ENV = "CLAUDE_TG_BOT_MEMORY_SCOPES"


def known_memory_scopes() -> tuple[str, ...]:
    """Допустимые scope памяти плановой задачи из CLAUDE_TG_BOT_MEMORY_SCOPES."""
    load_dotenv()
    raw = os.environ.get(MEMORY_SCOPES_ENV, "")
    return tuple(s.strip().casefold() for s in raw.split(",") if s.strip())

# Метка планового рана для аппрув-хука: хук наследует окружение claude и
# передаёт метку боту, а бот решает такой ран сразу, без карточки. Сессий
# oneshot-ранов бот не знает, и без метки хук слал бы карточку в основной чат,
# которую никто не ждёт (сбой утреннего разбора дневника 14.09.2026).
SCHEDULED_ENV = "CLAUDE_TG_BOT_SCHEDULED"


def require_memory_scope(scope: str) -> str:
    """Канонический scope проекта для планового запуска.

    Роутер памяти закрепляет проект за сессией по ПЕРВОМУ сообщению. У
    планового запуска человека рядом нет, переспросить некого, и без явного
    scope задача молча отработает в личном режиме, то есть без проектной
    памяти вообще. Падать надо ДО запуска claude, а не выяснять это по
    странному ответу.
    """
    value = (scope or "").strip().casefold()
    known = known_memory_scopes()
    if value not in known:
        raise SystemExit(
            _("плановой задаче нужен канонический scope памяти, получено {scope!r}; "
              "допустимые ({env}): {known}").format(
                scope=scope, env=MEMORY_SCOPES_ENV, known=', '.join(known) or _('не заданы'))
        )
    return value


def run_claude_oneshot(
    args: list[str], *, input: bytes, timeout: int, cwd: str,
    memory_scope: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Drop-in замена для `subprocess.run(args, input=..., capture_output=True,
    timeout=..., cwd=...)`, но с изоляцией в отдельной process group и
    групповым SIGKILL при таймауте. Возвращает CompletedProcess (как run);
    при таймауте пробрасывает subprocess.TimeoutExpired (как run).

    `memory_scope` обязателен для проектных задач: он закрепляет сессию на
    нужном заказчике. Без него роутер отдаст личный режим.
    """
    env = agent_env()
    env[SCHEDULED_ENV] = "1"
    if memory_scope is not None:
        env["AGENT_MEMORY_SCOPE_HINT"] = require_memory_scope(memory_scope)
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Убиваем всю группу (claude + дети), затем reap'аем зомби. getpgid
        # может бросить ProcessLookupError, если процесс уже умер сам — это ок.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)

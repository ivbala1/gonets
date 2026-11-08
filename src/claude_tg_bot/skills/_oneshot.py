"""Общий запуск headless `claude` для oneshot-скиллов (сводки чатов и новостей).

Зачем не `subprocess.run(timeout=...)`: на TimeoutExpired он шлёт SIGKILL
только верхнему процессу (`claude`), а его дети — MCP-серверы (Gmail/Slack),
Bash-tool и т.п. — осиротеют и продолжат жить (в т.ч. с доступом к почте/Slack).
Запуск идёт через procs.run_bounded_sync: claude становится лидером своей
группы, и по таймауту SIGKILL получает вся группа.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from dotenv import load_dotenv

from .. import paths, procs
from ..config import agent_env
from ..dashes import soften_dashes
from ..i18n import _


def resolve_claude_bin() -> str:
    """Абсолютный путь к `claude` для oneshot-скиллов.

    Скиллы не поднимают полный config, поэтому берут тот же приоритет, что и он:
    CLAUDE_BIN из .env или окружения, иначе PATH. Хардкодить путь нельзя: он
    меняется при смене способа установки Claude Code (npm/nvm → нативный билд).
    """
    load_dotenv(paths.ENV_FILE)
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
    load_dotenv(paths.ENV_FILE)
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
    return procs.run_bounded_sync(args, input=input, timeout=timeout, cwd=cwd, env=env)


def run_claude_json(prompt: str, *, timeout: int, model: str | None = None) -> str:
    """Ран без единого инструмента и MCP для сводок (чатов, новостей) и
    коротких вопросов: во входе чужой текст, читать и писать claude здесь
    нечего. Встроенные инструменты выключены целиком (--tools ""), MCP не
    поднимаются, режим plan как второй замок, транскрипт на диск не пишется.
    Текст ответа; сбой: RuntimeError без сырого вывода дочернего процесса."""
    args = [
        resolve_claude_bin(), "-p",
        "--output-format", "json",
        "--permission-mode", "plan",
        "--tools", "",
        "--no-session-persistence",
        "--settings", str(paths.hook_settings()),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    ]
    if model:
        args += ["--model", model]
    try:
        proc = run_claude_oneshot(args, input=prompt.encode("utf-8"), timeout=timeout, cwd=str(paths.BOT_ROOT))
    except subprocess.TimeoutExpired:
        raise RuntimeError(_("claude не уложился в {seconds} с").format(seconds=timeout)) from None
    except OSError as e:
        raise RuntimeError(_("claude не запустился: {error}").format(error=type(e).__name__)) from None
    if proc.returncode != 0:
        # Сырой вывод дочернего процесса наружу не идёт: в журнал и боту
        # уходит только код и класс причины.
        stderr = proc.stderr.decode("utf-8", errors="replace")
        kind = (_("лимит или доступ") if any(k in stderr for k in ("429", "401", "rate", "limit"))
                else _("ошибка запуска"))
        raise RuntimeError(_("claude exit {rc}: {kind}").format(rc=proc.returncode, kind=kind))
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(_("claude stdout не JSON: {error}").format(error=e)) from e
    if payload.get("is_error"):
        raise RuntimeError(_("claude вернул is_error=true"))
    # Тире на дефис здесь: хук против тире в плановых ранах бота молчит, его
    # строка «Исправлено» подменила бы сводку (решение Ивана 22.10.2026).
    return soften_dashes((payload.get("result") or "").strip())

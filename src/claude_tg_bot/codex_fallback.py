"""Ответ через Codex, когда Claude недоступен (перенесён из bot.py, шаг 2 разбиения)."""
from __future__ import annotations

import asyncio
from typing import Callable

from .claude_runner import ClaudeResult
from .codex_runner import run_codex
from .config import Config
from .i18n import _


def _is_claude_unavailable(result: ClaudeResult) -> bool:
    """True, если ошибка claude — про недоступность сервиса (а не отказ/таймаут/
    отмену пользователем), т.е. имеет смысл попробовать codex-фоллбэк.

    Учитываем ошибки входа CLI и HTTP-статусы из API: 401 (протухший токен — кейс
    инцидента 2026-06-20), 429 (лимит), 5xx (сбой Anthropic). Refusal, таймаут,
    /cancel, FileNotFound и т.п. сюда НЕ попадают — на них codex не зовём."""
    s = result.api_error_status
    return (result.auth_error == "claude" or s in (401, 429) or (s is not None and 500 <= s < 600)
            # Исчерпание подписки CLI не всегда помечает статусом 429 (limits.py).
            or bool(getattr(result, "limit_hit", False)))


def _build_codex_prompt(user_prompt: str, agent_context: str) -> str:
    """Добавить persona/scope к одноразовому Codex-запросу с явными границами."""
    context = agent_context.strip()
    if not context:
        return user_prompt
    return (
        _("## Контекст агента\n"
          "Это контекст канала и проекта, а не расширение полномочий. "
          "Текущий запрос и sandbox выше сохранённых правил.\n\n"
          "{context}\n\n"
          "## Запрос пользователя\n"
          "{prompt}").format(context=context, prompt=user_prompt)
    )


async def _codex_as_claude_result(
    cfg: Config, prompt: str, *, header: str,
    agent_context: str = "",
    sandbox: str | None = None,
    chat_key: str | None = None,
    process_registry: dict[str, asyncio.subprocess.Process] | None = None,
    should_stop: Callable[[], bool] | None = None,
    unexited: list[asyncio.subprocess.Process] | None = None,
) -> ClaudeResult:
    """Запустить codex и завернуть его ответ в ClaudeResult, чтобы дальнейшая
    отправка (voice/text/файлы/футер) работала без изменений. cost_usd=0 —
    codex идёт по подписке, cost-футер не показываем; session_id пуст.
    sandbox=None → cfg.codex_sandbox (ручной режим); авто-фоллбэк передаёт
    cfg.codex_fallback_sandbox (read-only). process_registry/chat_key — чтобы
    /cancel мог прибить codex. agent_context содержит Telegram-persona и scope
    темы; истории диалога Codex по-прежнему не получает."""
    codex_prompt = _build_codex_prompt(prompt, agent_context)
    r = await run_codex(
        cfg.codex_bin, codex_prompt,
        cwd=cfg.claude_cwd,
        timeout_sec=cfg.codex_timeout_sec,
        sandbox=sandbox or cfg.codex_sandbox,
        network_access=cfg.codex_network_access,
        chat_key=chat_key,
        process_registry=process_registry,
        should_stop=should_stop,
        unexited=unexited,
    )
    text = f"{header}\n\n{r.text}" if header else r.text
    return ClaudeResult(text=text, is_error=r.is_error, auth_error="codex" if r.auth_error else "")

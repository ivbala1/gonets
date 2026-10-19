r"""Запуск нативного Codex CLI (OpenAI) как фоллбэк/альтернатива claude.

Зачем: когда claude недоступен (протухший токен/лимит/сбой API) или Иван явно
просит ответ GPT (префикс-триггер), бот переключается на Codex под подпиской
ChatGPT (OAuth, без доплат за токены — расходуется лимит подписки).

Ограничения относительно claude (осознанно — это деградированный режим):
- нет claude-сессий: каждый вызов одноразовый (--resume не поддерживаем),
  истории диалога codex не получает;
- свой набор инструментов и своя sandbox-модель (не hook-approve бота);
- сеть в sandbox закрыта, пока не передан network_access (см. run_codex): ни
  read-only, ни workspace-write сами по себе её не открывают;
- persona и scope темы добавляет `bot._build_codex_prompt`; истории диалога всё равно нет.

WSL-нюанс: `codex` из PATH — это Windows-npm-шим, который из Linux-cwd падает
на UNC-путь (\\wsl.localhost\...). Поэтому зовём нативный Linux-бинарь по
абсолютному пути (CODEX_BIN). Финальный ответ берём из --output-last-message —
чистый текст, без эха промпта / reasoning / служебного вывода.

Prompt передаётся через stdin (`codex exec ... -`, читает до EOF), а НЕ через
argv: иначе промпт, начинающийся с `--`, разобрался бы как опция codex, светился
бы в `ps` и упирался в ARG_MAX. EOF (закрытие stdin в communicate) одновременно
снимает риск зависания на «Reading additional input from stdin...».
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .claude_runner import _reap_proc, kill_process_tree
from .auth import is_auth_error
from .config import agent_env
from .i18n import _

log = logging.getLogger(__name__)

# Переменные, которые codex ставит СВОИМ детям как признак «ты уже внутри
# песочницы». Имя CODEX_SANDBOX бот занял под собственную настройку (.env), и
# через os.environ она протекала обратно в codex: тот считал себя вложенным
# запуском и резал возможности. Замер 21.08.2026 на codex-cli 0.149.0, один и
# тот же argv: с CODEX_SANDBOX в env curl из sandbox отдаёт 000, без неё 200
# (а часть команд просто «не запустилась»). Поэтому маркеры снимаем всегда:
# они принадлежат codex, а не боту.
CODEX_OWN_SANDBOX_MARKERS = ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


@dataclass
class CodexResult:
    text: str
    is_error: bool = False
    auth_error: bool = False


def _read_and_unlink(path: Path) -> str:
    """Прочитать last-message файл и удалить его. Пусто, если файла/контента нет."""
    try:
        text = path.read_text().strip()
    except OSError:
        text = ""
    finally:
        path.unlink(missing_ok=True)
    return text


async def run_codex(
    codex_bin: str,
    prompt: str,
    *,
    cwd: Path,
    timeout_sec: int = 600,
    sandbox: str = "workspace-write",
    network_access: bool = False,
    chat_key: str | None = None,
    process_registry: dict[str, asyncio.subprocess.Process] | None = None,
    should_stop: Callable[[], bool] | None = None,
    unexited: list[asyncio.subprocess.Process] | None = None,
) -> CodexResult:
    """Один headless-запуск `codex exec`.

    process_registry/chat_key: регистрируем proc так же, как run_claude, чтобы
    /cancel мог прибить долгий codex (до timeout_sec). start_new_session=True:
    codex и его дети попадают в одну pgroup, чтобы kill снёс всё поддерево.
    should_stop: True, когда бот останавливается; сверяется перед запуском и
    сразу после создания процесса, как в run_claude (ревью 11.10.2026).
    unexited: сюда процесс, не вышедший и после SIGKILL, как в run_claude
    (ревью Codex 14.10).
    """
    stopped = CodexResult(text=_("⛔ отменено: бот останавливается"), is_error=True)
    if should_stop is not None and should_stop():
        return stopped
    fd, last_path = tempfile.mkstemp(suffix=".txt", prefix="codex_last_")
    os.close(fd)
    last_msg = Path(last_path)
    args = [
        codex_bin, "exec", "--json",
        "--skip-git-repo-check",
        "--sandbox", sandbox,
    ]
    # Значение передаём ВСЕГДА, а не только при True: дефолт сети у codex
    # менялся между версиями, и «просто не передавать ключ» перестало означать
    # «сети нет». Замеры curl-ом на https://example.com из sandbox:
    #   0.144.6  workspace-write без ключа 000, с ключом=true 200
    #   0.149.0  workspace-write без ключа 200 (сеть по умолчанию!), =false 000
    #   обе версии read-only 000 при любом ключе — там сеть закрыта всегда.
    # Явный флаг делает тумблер двусторонним и не зависящим от версии codex.
    args += ["-c", f"sandbox_workspace_write.network_access={str(network_access).lower()}"]
    args += [
        "-o", str(last_msg),
        "-",  # читать prompt со stdin (не argv)
    ]
    child_env = {k: v for k, v in agent_env().items() if k not in CODEX_OWN_SANDBOX_MARKERS}
    log.info("codex start cwd=%s sandbox=%s network=%s prompt_len=%d",
             cwd, sandbox, network_access, len(prompt))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=child_env,
            start_new_session=True,
        )
    except OSError as e:
        log.error("codex spawn failed: %s", e)
        last_msg.unlink(missing_ok=True)
        return CodexResult(text=_("⚠️ codex не запустился: {error}").format(error=e), is_error=True)
    if should_stop is not None and should_stop():
        # Сигнал пришёл, пока процесс создавался: обработчик остановки
        # его ещё не видел (ревью 11.10.2026).
        log.warning("codex start cancelled: bot stopping")
        kill_process_tree(proc, signal.SIGKILL)
        await _reap_proc(proc)
        last_msg.unlink(missing_ok=True)
        return stopped

    if process_registry is not None and chat_key is not None:
        process_registry[chat_key] = proc

    timed_out = False
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            timed_out = True
            log.warning("codex timeout after %ds — killing pgroup", timeout_sec)
            kill_process_tree(proc, signal.SIGKILL)
            await _reap_proc(proc)
            stdout = b""
            stderr = b""
        except asyncio.CancelledError:
            kill_process_tree(proc, signal.SIGKILL)
            await _reap_proc(proc)
            last_msg.unlink(missing_ok=True)
            raise
    finally:
        if (
            process_registry is not None
            and chat_key is not None
            and process_registry.get(chat_key) is proc
        ):
            process_registry.pop(chat_key, None)
        if unexited is not None and proc.returncode is None:
            unexited.append(proc)

    raw = stdout.decode(errors="replace") if stdout else ""
    errors = []
    turn_failed = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in ("error", "turn.failed"):
            turn_failed = turn_failed or event.get("type") == "turn.failed"
            error = event.get("error") or event.get("message", "")
            errors.append(str(error.get("message", "") if isinstance(error, dict) else error))
    diagnostic = "\n".join(errors) + "\n" + (stderr.decode(errors="replace") if stderr else "")
    # Чистый финальный ответ из файла; на таймауте codex мог успеть записать
    # частичный ответ — читаем его прежде чем сдаться. Fallback — хвост stdout.
    answer = _read_and_unlink(last_msg)

    if timed_out:
        if answer:
            return CodexResult(
                text=_("⛔ codex прерван по таймауту ({timeout}s), частичный ответ:\n\n{answer}").format(
                    timeout=timeout_sec, answer=answer),
                is_error=True,
            )
        return CodexResult(text=_("⛔ codex убит по таймауту ({timeout}s)").format(timeout=timeout_sec), is_error=True)

    if not answer:
        answer = diagnostic.strip()[-3000:] or _("[codex: пустой ответ]")

    if proc.returncode != 0 or turn_failed:
        auth_error = is_auth_error(diagnostic)
        log.error("codex rc=%s auth_error=%s", proc.returncode, auth_error)
        if auth_error:
            answer = _("Codex потерял авторизацию. Восстанови вход через /login codex в личном чате с ботом.")
        return CodexResult(text=answer, is_error=True, auth_error=auth_error)
    log.info("codex done rc=0 answer_len=%d", len(answer))
    return CodexResult(text=answer)

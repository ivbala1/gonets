"""Общие средства для дочерних процессов бота: убить группу целиком.

Дочерние программы бота запускаются с `start_new_session=True`: процесс
становится лидером своей группы, и его потомки (Bash из инструмента, MCP,
ffmpeg воркера) живут в ней же. Убивать надо группу, а не лидера.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence

log = logging.getLogger(__name__)

# Сколько ждать выхода после SIGKILL: процесс в D-state (WSL, I/O на drvfs/9p)
# не выходит и после него, и голый wait() держал бы вызывающего вечно.
REAP_TIMEOUT_SEC = 5.0
# Сколько символов stderr отдаёт run_bounded: на причину сбоя хватает.
ERR_TAIL_CHARS = 2000


def kill_group(proc, sig: int) -> None:
    """Послать сигнал всей группе процесса `proc` (pgid равен его pid).

    Без проверки `returncode`: типичный таймаут, когда сам процесс уже вышел,
    а внук держит stdout. Номер группы при этом жив, пока в ней есть хоть один
    процесс, и Linux не отдаёт его новому процессу, так что `killpg(proc.pid)`
    бьёт ровно в наших потомков. Пустая группа: тихо ничего.
    """
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        os.killpg(pid, sig)
        return
    except ProcessLookupError as e:
        # Группы нет. Лидер ещё жив: значит, его запустили без своей группы,
        # бьём хотя бы его самого. Иначе группа просто опустела.
        if getattr(proc, "returncode", None) is not None:
            return
        log.warning("killpg(%s, %s): no such group, the process was started without "
                    "start_new_session; signalling it alone", pid, sig)
        err: OSError = e
    except OSError as e:
        log.warning("killpg(%s, %s) failed: %s, falling back to the process itself", pid, sig, e)
        if getattr(proc, "returncode", None) is not None:
            return
        err = e
    try:
        if sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except OSError:
        log.warning("signal %s to pid=%s failed after %s", sig, pid, type(err).__name__)


def group_alive(proc) -> bool:
    """Есть ли ещё процессы в группе `proc` (лидер или его потомки)."""
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running(proc) -> bool:
    """Идёт ли ещё ран этого процесса: жив лидер или его потомки (внук держит
    вывод, а сам claude уже вышел). Для решений «есть что отменять»."""
    if proc is None:
        return False
    return getattr(proc, "returncode", None) is None or group_alive(proc)


async def _drain(stream: asyncio.StreamReader) -> None:
    # Кусками и в никуда: потомок вне группы, пишущий в унаследованный
    # канал, не копит вывод в памяти бота (ревью Codex 08.11.2026).
    try:
        while await stream.read(65536):
            pass
    except (RuntimeError, OSError, ValueError):
        pass  # читает другая задача или канал уже сломан: wait() ниже как раньше


async def _drain_wait(proc) -> None:
    # Недочитанный канал (читатель снят отменой, буфер полон, транспорт на
    # паузе) держал wait() до конца таймаута: asyncio ждёт закрытия каналов,
    # а их конца никто не читал (проверено 08.11.2026, Python 3.14: 5 с на
    # `yes` после SIGKILL). Остаток дочитывается в никуда.
    pipes = [s for s in (getattr(proc, "stdout", None), getattr(proc, "stderr", None))
             if isinstance(s, asyncio.StreamReader) and not s.at_eof()]
    if pipes:
        await asyncio.gather(*(_drain(s) for s in pipes))
    await proc.wait()


async def reap(proc, timeout: float = REAP_TIMEOUT_SEC) -> None:
    """Снять процесс после SIGKILL, не виснув навсегда (D-state на WSL).
    Звать без живого читателя каналов: остаток вывода дочитывается в никуда.
    Не вышел и тогда: запись в журнал, returncode остаётся None, по нему
    вызывающий держит проект задачи до выхода (unexited, ревью Codex 14.10)."""
    try:
        await asyncio.wait_for(asyncio.shield(_drain_wait(proc)), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    if proc.returncode is None:
        log.warning("pid=%s не вышел за %s с после SIGKILL", proc.pid, timeout)


def _tail(data: bytes | None, limit: int | None) -> str:
    if not data:
        return ""
    if limit is None:
        return data.decode("utf-8", "replace")
    return data.decode("utf-8", "replace").strip()[-limit:]


async def spawn(argv: Sequence[str], **kw) -> asyncio.subprocess.Process:
    """create_subprocess_exec в своей группе, переживающий отмену: если задачу
    сняли посреди запуска, процесс уже мог родиться, и его надо добить, а не
    бросить (тот же приём, что в auth._spawn)."""
    kw["start_new_session"] = True
    task = asyncio.ensure_future(asyncio.create_subprocess_exec(*argv, **kw))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        proc = await born(task)
        if proc is not None:
            kill_group(proc, signal.SIGKILL)
            await reap(proc)
        raise


async def born(task: asyncio.Future):
    """Процесс из задачи запуска после отмены вызывающего: дождаться её под
    shield и при повторной отмене (второй cancel пробивал голый `await task`,
    процесс оставался без хозяина, ревью Codex 08.11.2026). None: не запустился."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if task.cancelled() or task.exception() is not None:
        return None
    return task.result()


async def run_bounded(
    argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None,
    cwd: str | os.PathLike[str] | None = None, input: bytes | None = None,
    stderr: int = asyncio.subprocess.PIPE, err_tail_chars: int | None = ERR_TAIL_CHARS,
) -> tuple[int, bytes, str]:
    """Запустить программу в своей группе и дождаться не дольше `timeout`.

    Возвращает (код выхода, stdout, хвост stderr текстом). На таймауте,
    отмене и любой другой ошибке вся группа получает SIGKILL и снимается
    через reap; исключение (TimeoutError, CancelledError, OSError) уходит
    вызывающему. `stderr`: PIPE, STDOUT (в stdout) или DEVNULL, как у
    create_subprocess_exec; хвост тогда пуст. `err_tail_chars=None`: весь
    stderr как есть.
    """
    proc = await spawn(
        argv,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except BaseException:
        kill_group(proc, signal.SIGKILL)
        await reap(proc)
        raise
    return proc.returncode, out or b"", _tail(err, err_tail_chars)


def run_bounded_sync(
    argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None,
    cwd: str | os.PathLike[str] | None = None, input: bytes | str | None = None,
    text: bool = False,
) -> subprocess.CompletedProcess:
    """Синхронный двойник run_bounded для скиллов и вспомогательных модулей:
    как `subprocess.run(argv, input=, capture_output=True, timeout=, text=)`,
    но в своей группе, и на таймауте SIGKILL получает вся группа, а не только
    верхний процесс (у `subprocess.run` внуки остались бы жить). На таймауте
    пробрасывает subprocess.TimeoutExpired, как run."""
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        start_new_session=True, text=text,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except BaseException:
        kill_group(proc, signal.SIGKILL)
        try:
            proc.communicate(timeout=REAP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            log.warning("pid=%s не вышел за %s с после SIGKILL", proc.pid, REAP_TIMEOUT_SEC)
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .config import agent_env
from .i18n import _

log = logging.getLogger(__name__)

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_AUTH_ERROR = re.compile(
    r"Failed to authenticate:|OAuth (?:session|token|access token) (?:has )?expired|"
    r"OAuth token (?:has been )?revoked|\bNot logged in\b|"
    r"Please (?:run /login|log in again)|"
    r"Your (?:access|refresh) token (?:has expired|could not be refreshed)|"
    r"refresh_token_(?:reused|expired|invalidated)|"
    r"Token refresh failed:|invalid_grant|"
    r"Missing bearer or basic authentication|"
    r"invalid authentication credentials",
    re.IGNORECASE,
)
_CLAUDE_CODE = re.compile(r"[A-Za-z0-9_-]{8,512}(?:#[A-Za-z0-9_-]{8,512})?")


def is_auth_error(diagnostic: str) -> bool:
    return bool(_AUTH_ERROR.search(diagnostic))


_HEX_HASH = re.compile(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}")


def looks_like_auth_code(text: str, *, login_active: bool = True) -> bool:
    text = text.strip()
    # Вне входа хеш коммита (40 hex) или sha256 (64 hex) это не код: иначе
    # вставленный хеш удалялся и не доходил до модели. Во время входа всё
    # по-прежнему (решение владельца 12.10.2026).
    if not login_active and _HEX_HASH.fullmatch(text):
        return False
    return bool(_CLAUDE_CODE.fullmatch(text) and ("#" in text or len(text) >= 40))


def login_details(engine: str, output: str) -> tuple[str, str] | None:
    output = _ANSI.sub("", output)
    # A delimiter is required: a pipe read may end halfway through the URL.
    for match in re.finditer(r"https://[^\s<>\"']+(?=\s)", output):
        url = match.group()
        parsed = urlsplit(url)
        if parsed.username or parsed.password or ":" in parsed.netloc:
            continue
        if engine == "claude" and (parsed.hostname, parsed.path) in {
            ("claude.com", "/cai/oauth/authorize"),
            ("claude.ai", "/oauth/authorize"),
        }:
            return url, ""
        if engine == "codex" and (parsed.hostname, parsed.path) == (
            "auth.openai.com", "/codex/device"
        ):
            code = re.search(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,5}\b", output[match.end():])
            if code:
                return url, code.group()
    return None


@dataclass
class LoginSession:
    engine: str
    user_id: int
    chat_id: int
    task: asyncio.Task | None = field(default=None, repr=False)
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    accepts_code: bool = False
    submitted: bool = False
    messages: list = field(default_factory=list, repr=False)


class LoginManager:
    def __init__(self, bot, claude_bin: str, codex_bin: str, cwd: Path, *, ttl: float = 600):
        self.bot = bot
        self.binaries = {"claude": claude_bin, "codex": codex_bin}
        self.cwd = cwd
        self.ttl = ttl
        self.session: LoginSession | None = None

    def start(self, engine: str, user_id: int, chat_id: int) -> bool:
        # No await between checking and reserving: concurrent updates cannot race.
        if self.session is not None:
            return False
        session = LoginSession(engine, user_id, chat_id)
        self.session = session
        session.task = asyncio.create_task(self._run(session), name=f"login:{engine}")
        return True

    async def _send(self, session: LoginSession, text: str, *, sensitive: bool = False):
        message = await self.bot.send_message(
            chat_id=session.chat_id, text=text, disable_web_page_preview=True,
            protect_content=sensitive,
        )
        if sensitive:
            session.messages.append(message)

    async def _spawn(self, session: LoginSession, args: list[str]):
        env = {k: v for k, v in agent_env().items()
               if k not in ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")}
        env.update(BROWSER="/bin/true", NO_COLOR="1")
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            self.binaries[session.engine], *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            cwd=str(self.cwd), env=env,
        ))
        try:
            session.proc = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            session.proc = await spawning
            raise
        return session.proc

    async def _flow(self, session: LoginSession) -> str:
        args = ["auth", "login", "--claudeai"] if session.engine == "claude" else ["login", "--device-auth"]
        proc = await self._spawn(session, args)
        output = ""
        sent = False
        while chunk := await proc.stdout.read(4096):
            output = (output + chunk.decode(errors="replace"))[-32768:]
            if not sent and (details := login_details(session.engine, output)):
                url, code = details
                if session.engine == "claude":
                    text = _(
                        "Открой ссылку и подтверди вход Claude:\n{url}\n\n"
                        "Верни выданный сайтом одноразовый код сюда:\n/authcode КОД\n"
                        "Код обрабатывает бот, в модель он не передаётся. "
                        "Отмена: /login cancel"
                    ).format(url=url)
                    session.accepts_code = True
                else:
                    text = _(
                        "Открой ссылку и войди в ChatGPT:\n{url}\n\n"
                        "Введи на сайте одноразовый код: {code}\n"
                        "Бот сам дождётся завершения входа Codex. Отмена: /login cancel"
                    ).format(url=url, code=code)
                await self._send(session, text, sensitive=True)
                sent = True
        await proc.wait()
        session.accepts_code = False
        if proc.returncode != 0:
            if session.engine == "codex":
                return _(
                    "Вход Codex не завершён. Если вход по коду устройства выключен, "
                    "включи его в настройках безопасности ChatGPT и повтори /login codex. "
                    "Другой способ: codex login в терминале WSL."
                )
            return _("Вход Claude не завершён. Повтори /login claude и используй новый код.")
        args = ["auth", "status"] if session.engine == "claude" else ["login", "status"]
        proc = await self._spawn(session, args)
        stdout, _unused = await asyncio.wait_for(proc.communicate(), timeout=15)
        ok = proc.returncode == 0
        if ok and session.engine == "claude":
            try:
                ok = json.loads(stdout).get("loggedIn") is True
            except (ValueError, AttributeError):
                ok = False
        name = "Claude" if session.engine == "claude" else "Codex"
        if ok:
            return _("Вход {name} восстановлен. Можно повторить запрос боту.").format(name=name)
        return _("Не удалось подтвердить вход {name}. Повтори /login {engine}.").format(
            name=name, engine=session.engine)

    async def _run(self, session: LoginSession):
        try:
            async with asyncio.timeout(self.ttl):
                await self._send(session, _("Запускаю вход {name}…").format(
                    name='Claude' if session.engine == 'claude' else 'Codex'))
                result = await self._flow(session)
            await self._send(session, result)
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self._send(session, _("Время ожидания входа истекло. Повтори /login {engine}.").format(
                    engine=session.engine))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # CLI output and Telegram exception bodies may contain login secrets.
            log.warning("login %s failed (%s)", session.engine, type(exc).__name__)
            with contextlib.suppress(Exception):
                await self._send(session, _("Не удалось завершить вход. Повтори /login {engine}.").format(
                    engine=session.engine))
        finally:
            session.accepts_code = False
            proc = session.proc
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5)
            for message in session.messages:
                with contextlib.suppress(Exception):
                    await message.delete()
            if self.session is session:
                self.session = None

    async def submit(self, user_id: int, chat_id: int, code: str) -> str:
        session = self.session
        if session is None or (session.user_id, session.chat_id) != (user_id, chat_id):
            return _("Нет ожидающего входа. Начни с /login claude.")
        if session.engine != "claude":
            return _("Код Codex нужно ввести на сайте из сообщения бота.")
        if session.submitted:
            return _("Код уже отправлен. Жду результат входа.")
        if not session.accepts_code or session.proc is None or session.proc.returncode is not None:
            return _("Сейчас код не ожидается. Дождись ссылки или повтори /login claude.")
        if not _CLAUDE_CODE.fullmatch(code):
            return _("Нужен одноразовый код с сайта Claude целиком, без ссылки и пробелов.")
        session.submitted = True
        session.accepts_code = False
        try:
            session.proc.stdin.write((code + "\n").encode())
            await session.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return _("Процесс входа завершился. Повтори /login claude.")
        return _("Код принят. Проверяю вход.")

    async def cancel(self, user_id: int, chat_id: int) -> bool:
        session = self.session
        if session is None or (session.user_id, session.chat_id) != (user_id, chat_id):
            return False
        await self.close()
        return True

    async def close(self):
        session = self.session
        if session is not None and session.task is not None:
            session.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.task
            # The task can be cancelled before its first instruction/finally.
            if self.session is session:
                self.session = None

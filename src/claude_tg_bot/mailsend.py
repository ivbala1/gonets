"""Письмо от владельца из его Gmail через SMTP, с локальными вложениями.

Коннектор Gmail MCP принимает вложения только base64 и с файлом резюме не
справляется, поэтому письмо с PDF уходит этим модулем (решение Ивана
13.10.2026). Вызов агента всегда идёт через карточку отправки бота
(approval.py): на ней адресаты, тема, весь текст из --body-file, имена и
размеры вложений. Тап ✅ кладёт билет: хеш ровно того письма, что было на
карточке. Здесь письмо собирается из файлов один раз в память, хеш
сверяется с билетом, билет гасится, и уходят те же байты. Поменяли текст или
вложение после карточки, звали модуль мимо карточки: билета нет, отказ.

Usage (агент, из корня репозитория бота):
    .venv/bin/python -m claude_tg_bot.mailsend --to hr@example.com --subject "DevOps" \\
        --body-file /tmp/claude/letter.txt \\
        --attach ~/.local/share/telegram-send/resume.pdf
    ... --dry-run   # показать собранное письмо, ничего не отправляя и без карточки

Логин GMAIL_USER и GMAIL_APP_PASSWORD (пароль приложения) из окружения, иначе
из env-файла: путь в MAILSEND_ENV, по умолчанию .env бота.
Значения не печатаются.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import tempfile
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

from .i18n import _

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_TIMEOUT_SEC = 60
ENV_FILE_VAR = "MAILSEND_ENV"
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
# Текст письма целиком идёт на карточку: большой файл там не прочесть.
BODY_MAX_BYTES = 100_000
TICKET_DIRNAME = "mailsend"
# Между тапом ✅ и запуском команды секунды; запас на медленный старт.
TICKET_TTL_SEC = 600

# Вложения только из этих каталогов (по realpath), список через `:` в
# MAILSEND_ATTACH_DIRS (.env бота). По умолчанию каталог отправки Telegram MCP
# (send_file). Лимит вместе: Gmail режет письмо на 25 МБ,
# base64 раздувает вложения на треть.
ATTACH_DIRS_VAR = "MAILSEND_ATTACH_DIRS"
DEFAULT_ATTACH_DIRS = (
    Path.home() / ".local" / "share" / "telegram-send",
)
MAX_BYTES_VAR = "MAILSEND_MAX_BYTES"
DEFAULT_MAX_BYTES = 18 * 1024 * 1024

_ADDR_RE = re.compile(r"[^@\s,;<>\"'()]+@[^@\s,;<>\"'()]+\.[^@\s,;<>\"'()]+")
_MSGID_RE = re.compile(r"<[^<>\s]+@[^<>\s]+>")


class MailError(Exception):
    """Письмо не собрать или не отправить; текст для человека."""


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes
    path: str


@dataclass(frozen=True)
class Letter:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    reply_to_id: str
    body: str
    attachments: tuple[Attachment, ...]

    def digest(self) -> str:
        """Хеш всего, что уходит адресату: адресаты, тема, ответ на письмо,
        текст и байты вложений с их именами. Им карточка и отправка сверяются."""
        meta = {
            "to": list(self.to), "cc": list(self.cc), "subject": self.subject,
            "reply_to": self.reply_to_id,
            "body": hashlib.sha256(self.body.encode("utf-8")).hexdigest(),
            "attachments": [[a.name, hashlib.sha256(a.data).hexdigest()] for a in self.attachments],
        }
        raw = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def attach_dirs() -> tuple[Path, ...]:
    raw = os.environ.get(ATTACH_DIRS_VAR, "").strip()
    dirs = tuple(Path(d.strip()).expanduser() for d in raw.split(":") if d.strip()) if raw \
        else DEFAULT_ATTACH_DIRS
    return tuple(d for d in dirs if d.is_absolute())


def max_bytes() -> int:
    try:
        return int(os.environ.get(MAX_BYTES_VAR, "") or DEFAULT_MAX_BYTES)
    except ValueError:
        return DEFAULT_MAX_BYTES


class _Parser(argparse.ArgumentParser):
    # Ошибка аргументов исключением, а не выходом с печатью в stderr: тот же
    # разбор идёт внутри бота, при построении карточки.
    def error(self, message: str):  # type: ignore[override]
        raise MailError(_("аргументы: {message}").format(message=message))


def build_parser() -> argparse.ArgumentParser:
    """Один разбор аргументов и для отправки, и для карточки бота: что карточка
    поняла из команды, то модуль и отправит."""
    p = _Parser(prog="claude_tg_bot.mailsend", exit_on_error=False)
    p.add_argument("--to", action="append", required=True, help="адресат, можно несколько раз")
    p.add_argument("--cc", action="append", default=[], help="копия, можно несколько раз")
    p.add_argument("--subject", required=True)
    p.add_argument("--body-file", required=True, help="файл с текстом письма, UTF-8")
    p.add_argument("--attach", action="append", default=[], help="вложение, можно несколько раз")
    p.add_argument("--reply-to-message-id", default="", help="Message-ID письма, на которое ответ")
    p.add_argument("--dry-run", action="store_true", help="показать письмо, не отправлять")
    return p


def _resolve(raw: str, base: Path | None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if base is None:
            raise MailError(_("относительный путь без рабочего каталога: {path}").format(path=raw))
        path = base / path
    return path


def load_letter(args: argparse.Namespace, *, dirs: tuple[Path, ...], limit: int,
                base: Path | None) -> Letter:
    """Письмо из аргументов и файлов. Все проверки здесь: карточка зовёт ту же
    функцию и отказывает раньше Ивана, если отправка всё равно не пройдёт."""
    to = tuple(a.strip() for a in args.to)
    cc = tuple(a.strip() for a in args.cc)
    for addr in to + cc:
        if not _ADDR_RE.fullmatch(addr):
            raise MailError(_("не адрес: {addr!r} (по одному адресу на --to и --cc)").format(addr=addr))
    subject = args.subject.strip()
    if not subject or "\r" in subject or "\n" in subject:
        raise MailError(_("тема пустая или с переводом строки"))
    reply_to = args.reply_to_message_id.strip()
    if reply_to and not _MSGID_RE.fullmatch(reply_to):
        raise MailError(_("--reply-to-message-id нужен в виде <...@...>"))
    body_path = _resolve(args.body_file, base)
    try:
        raw = body_path.read_bytes()
    except OSError as e:
        raise MailError(_("не прочитать текст письма {path}: {error}").format(
            path=body_path, error=type(e).__name__)) from e
    if len(raw) > BODY_MAX_BYTES:
        raise MailError(_("текст письма больше {limit} байт").format(limit=BODY_MAX_BYTES))
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise MailError(_("текст письма не в UTF-8")) from e
    if not body.strip():
        raise MailError(_("текст письма пустой"))
    allowed = [Path(d).resolve() for d in dirs]
    attachments: list[Attachment] = []
    total = 0
    for item in args.attach:
        try:
            real = _resolve(item, base).resolve(strict=True)
        except OSError as e:
            raise MailError(_("нет вложения {item}: {error}").format(
                item=item, error=type(e).__name__)) from e
        if not any(real.is_relative_to(d) for d in allowed):
            dirs = ", ".join(str(d) for d in allowed) or _("список пуст")
            raise MailError(_("вложение {path} не из разрешённых каталогов ({dirs})").format(
                path=real, dirs=dirs))
        if not real.is_file():
            raise MailError(_("вложение {path} не обычный файл").format(path=real))
        data = real.read_bytes()
        total += len(data)
        if total > limit:
            raise MailError(_("вложения больше {limit} байт вместе").format(limit=limit))
        attachments.append(Attachment(name=real.name, data=data, path=str(real)))
    return Letter(to=to, cc=cc, subject=subject, reply_to_id=reply_to, body=body,
                  attachments=tuple(attachments))


# ---- билет одобрения (кладёт бот по ✅ карточки, гасит отправка) ----

def ticket_dir(state_dir: Path) -> Path:
    return state_dir / TICKET_DIRNAME


def _ticket_mac(digest: str, created: float, secret: str) -> str:
    # Ключ: секрет /approve бота. Не крепость против агента, читающего .env
    # бота, а защита от случайного билета, написанного руками или скриптом.
    msg = f"{digest}:{created:.3f}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def write_ticket(state_dir: Path, digest: str, secret: str, approval_id: str = "") -> Path:
    base = ticket_dir(state_dir)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time()
    # Попутно убрать протухшие билеты: они уже ничего не разрешают.
    for old in base.glob("*.json"):
        try:
            if now - old.stat().st_mtime > TICKET_TTL_SEC:
                old.unlink()
        except OSError:
            pass
    rec = {"created": round(now, 3), "approval_id": approval_id,
           "mac": _ticket_mac(digest, round(now, 3), secret)}
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        path = base / f"{digest}.json"
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def take_ticket(state_dir: Path, digest: str, secret: str) -> str | None:
    """Погасить билет на это письмо. None значит можно отправлять, иначе причина."""
    path = ticket_dir(state_dir) / f"{digest}.json"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        # Гасим до отправки: второй запуск той же команды билета уже не найдёт.
        path.unlink()
    except (OSError, ValueError):
        return _("нет одобрения карточкой на это письмо: запускай без обходов, чтобы бот показал "
                 "карточку, и не меняй текст и вложения после неё")
    try:
        created = float(rec.get("created"))
        mac = str(rec.get("mac"))
    except (TypeError, ValueError):
        return _("билет одобрения испорчен")
    if not hmac.compare_digest(mac, _ticket_mac(digest, created, secret)):
        return _("билет одобрения не от бота")
    if time.time() - created > TICKET_TTL_SEC:
        return _("одобрение карточкой устарело, нужна новая карточка")
    return None


# ---- сборка и отправка ----

def load_credentials(env_file: Path) -> tuple[str, str]:
    """GMAIL_USER и пароль приложения: окружение, затем env-файл Домового.
    Пароль Google показывает группами через пробел, пробелы убираем."""
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (user and password) and env_file.is_file():
        from dotenv import dotenv_values
        values = dotenv_values(env_file)
        user = user or (values.get("GMAIL_USER") or "").strip()
        password = password or (values.get("GMAIL_APP_PASSWORD") or "")
    password = "".join(password.split())
    if not user or not password:
        raise MailError(_("нет GMAIL_USER или GMAIL_APP_PASSWORD (окружение и {env_file})").format(
            env_file=env_file))
    return user, password


def build_message(letter: Letter, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(letter.to)
    if letter.cc:
        msg["Cc"] = ", ".join(letter.cc)
    msg["Subject"] = letter.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] or None)
    if letter.reply_to_id:
        msg["In-Reply-To"] = letter.reply_to_id
        msg["References"] = letter.reply_to_id
    msg.set_content(letter.body)
    for att in letter.attachments:
        ctype = mimetypes.guess_type(att.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(att.data, maintype=maintype, subtype=subtype, filename=att.name)
    return msg


def preview(letter: Letter, sender: str) -> str:
    lines = [f"From: {sender}", f"To: {', '.join(letter.to)}"]
    if letter.cc:
        lines.append(f"Cc: {', '.join(letter.cc)}")
    lines.append(f"Subject: {letter.subject}")
    if letter.reply_to_id:
        lines.append(f"In-Reply-To: {letter.reply_to_id}")
    for att in letter.attachments:
        lines.append(_("Вложение: {name} ({size} байт) из {path}").format(
            name=att.name, size=len(att.data), path=att.path))
    lines += ["", letter.body.rstrip("\n"), "", _("sha256 письма: {digest}").format(digest=letter.digest())]
    return "\n".join(lines)


def send(letter: Letter, user: str, password: str) -> tuple[str, dict[str, Any]]:
    """Отправка через smtp.gmail.com:465 (SSL). Копию в «Отправленных» Gmail
    кладёт сам по отправке через свой SMTP (по отзывам пользователей, в
    документации Google не нашли; проверить на первом письме)."""
    msg = build_message(letter, user)
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=SMTP_TIMEOUT_SEC) as smtp:
            smtp.login(user, password)
            refused = smtp.send_message(msg, from_addr=user, to_addrs=list(letter.to + letter.cc))
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(_("Gmail не принял логин: код {code}").format(code=e.smtp_code)) from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"SMTP: {type(e).__name__}: {e}") from e
    return str(msg["Message-ID"]), dict(refused)


def _load_cfg() -> Any:
    from .config import load_config
    return load_config(skip_hook_validation=True)


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    except (MailError, argparse.ArgumentError) as e:
        print(f"mailsend: {e}", file=sys.stderr)
        return 2
    cfg = _load_cfg()
    env_file = Path(os.environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE).expanduser()
    try:
        letter = load_letter(args, dirs=attach_dirs(), limit=max_bytes(), base=Path.cwd())
        if args.dry_run:
            try:
                sender = load_credentials(env_file)[0]
            except MailError:
                sender = _("(GMAIL_USER не найден)")
            print(preview(letter, sender))
            print(_("\n(сухой режим: ничего не отправлено)"))
            return 0
        refusal = take_ticket(cfg.state_dir, letter.digest(), cfg.approve_shared_secret)
        if refusal:
            print(f"mailsend: {refusal}", file=sys.stderr)
            return 1
        user, password = load_credentials(env_file)
        message_id, refused = send(letter, user, password)
    except MailError as e:
        print(f"mailsend: {e}", file=sys.stderr)
        return 1
    print(f"Message-ID: {message_id}")
    rcpts = list(letter.to + letter.cc)
    ok = [r for r in rcpts if r not in refused]
    names = ", ".join(a.name for a in letter.attachments) or _("без вложений")
    print(_("отправлено от {user}: {recipients}; {names}").format(
        user=user, recipients=', '.join(ok), names=names))
    if refused:
        print(_("mailsend: сервер отклонил адресатов: {recipients}").format(
            recipients=', '.join(refused)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

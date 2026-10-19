"""Отклики на вакансии с кнопками ✅ Ок, ✏️ Поправить, ⏭ Пропустить.

Агент кладёт отклик записью и шлёт его Ивану сообщением с кнопками; бот по
нажатию сверяет, что текст тот же, и будит агента с заданием (решение Ивана
12.10.2026: кнопка вместо ответа «ок» на сообщение). Запись неизменяема по
тексту: правка это новая запись, у старой кнопки гаснут. Нажатие ✅ и есть «ок»
на точный текст; отправка в Telegram и Gmail всё равно идёт карточкой бота.

Usage (агент, из корня репозитория бота):
    .venv/bin/python -m claude_tg_bot.otklik new --title "Acme, Senior IT Ops" \\
        --channel "письмо на hr@example.com, PDF резюме EN" --file текст.txt
    .venv/bin/python -m claude_tg_bot.otklik show --id <id>      # одобренный текст, со сверкой хеша
    .venv/bin/python -m claude_tg_bot.otklik done --id <id> --status sent --note "ссылка"
    .venv/bin/python -m claude_tg_bot.otklik list --status approved  # одобренные, но не закрытые
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .i18n import N_, _

OTKLIK_DIRNAME = "otkliki"
CALLBACK_PREFIX = "otk"
_CALLBACK_RE = re.compile(r"otk:([0-9a-f]{12}):([0-9a-f]{12}):([oes])")
_ID_RE = re.compile(r"[0-9a-f]{12}")
# Итоги, которые ставит агент командой done; остальные ставит бот по кнопке.
DONE_STATUSES = ("sent", "failed", "skipped")
NOTE_CAP = 200
# Запас на строку статуса: сообщение потом правится, а лимит Telegram 4096.
NEW_TEXT_CAP = 4096 - 300
STATUS_LINES = {
    "pending": "",
    "approved": N_("✅ одобрено, отправляю…"),
    "edit": N_("✏️ напиши, что поправить, ответом на это сообщение"),
    "skipped": N_("⏭ пропущено"),
    "sent": N_("📤 отправлено"),
    "failed": N_("⚠️ не ушло"),
}


def otklik_dir(state_dir: Path) -> Path:
    return state_dir / OTKLIK_DIRNAME


def text_sha(text: str, channel: str = "") -> str:
    """Хеш одобряемого: текст и куда он уйдёт (адресат, вложение) вместе."""
    return hashlib.sha256(f"{channel}\0{text}".encode("utf-8")).hexdigest()[:12]


def record_sha(rec: dict[str, Any]) -> str:
    return text_sha(rec.get("text", ""), rec.get("channel", ""))


def record_path(base: Path, oid: str) -> Path:
    if not _ID_RE.fullmatch(oid):
        raise ValueError("bad otklik id")
    return base / f"{oid}.json"


def load(base: Path, oid: str) -> dict[str, Any] | None:
    try:
        return json.loads(record_path(base, oid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(base: Path, rec: dict[str, Any]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    path = record_path(base, rec["id"])
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def new_record(title: str, channel: str, text: str) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "id": uuid.uuid4().hex[:12], "title": title.strip(), "channel": channel.strip(),
        "text": text, "sha": text_sha(text, channel.strip()), "status": "pending", "note": "",
        "chat_id": None, "message_id": None, "created": now, "updated": now,
    }


def parse_callback(data: str) -> tuple[str, str, str] | None:
    m = _CALLBACK_RE.fullmatch(data or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


def buttons(rec: dict[str, Any]) -> list[list[dict[str, str]]]:
    """Кнопки только у ждущего отклика; у остальных сообщение без кнопок."""
    if rec.get("status") != "pending":
        return []
    head = f"{CALLBACK_PREFIX}:{rec['id']}:{rec['sha']}"
    return [[{"text": _("✅ Ок"), "data": f"{head}:o"},
             {"text": _("✏️ Поправить"), "data": f"{head}:e"},
             {"text": _("⏭ Пропустить"), "data": f"{head}:s"}]]


def render(rec: dict[str, Any]) -> str:
    """Текст сообщения: заголовок, канал, отклик целиком, строка статуса."""
    # Номер в первой строке: ответ на сообщение цитирует только его начало.
    parts = [_("📨 {title} (отклик {id})").format(title=rec['title'], id=rec['id'])]
    if rec.get("channel"):
        parts.append(_("Куда: {channel}").format(channel=rec['channel']))
    parts.append("")
    parts.append(rec["text"])
    line = _(STATUS_LINES.get(rec.get("status", ""), ""))
    if line:
        note = rec.get("note") or ""
        parts += ["", line + (f": {note}" if note else "")]
    return "\n".join(parts)


def approved_prompt(rec: dict[str, Any], base: Path) -> str:
    return (
        _("[Кнопка отклика] ✅ ок: отклик {id} «{title}». Куда: {channel}. "
          "Текст бери только командой `.venv/bin/python -m claude_tg_bot.otklik show --id {id}` "
          "из ~/src/claude-tg-bot (она сверяет хеш {sha}; отказ значит не отправлять) "
          "и отправь его этим каналом без изменений. Затем отметь итог командой "
          "`.venv/bin/python -m claude_tg_bot.otklik done --id {id} --status sent|failed "
          "--note \"ссылка или причина\"` из ~/src/claude-tg-bot, обнови воронку и черновики.").format(
              id=rec['id'], title=rec['title'], channel=rec['channel'], sha=rec['sha'])
    )


def skipped_prompt(rec: dict[str, Any]) -> str:
    return (
        _("[Кнопка отклика] ⏭ пропустить: отклик {id} «{title}». "
          "Ничего не отправляй, отметь пропуск в воронке и черновиках.").format(
              id=rec['id'], title=rec['title'])
    )


# ---- CLI для агента ----

def _bot_api(method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    import httpx

    from .config import load_config
    cfg = load_config(skip_hook_validation=True)
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/{method}"
    try:
        r = httpx.post(url, json=payload, timeout=10.0)
    except httpx.HTTPError as e:
        print(f"otklik: HTTP error: {type(e).__name__}", file=sys.stderr)
        return None
    if r.status_code != 200:
        # Без URL: в нём токен бота.
        print(f"otklik: {method} failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()


def _markup(rec: dict[str, Any]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": b["text"], "callback_data": b["data"]} for b in row]
                                for row in buttons(rec)]}


def _cmd_new(args: argparse.Namespace, base: Path, primary_chat_id: int) -> int:
    text = Path(args.file).read_text(encoding="utf-8").strip()
    if not text:
        print(_("otklik: пустой текст"), file=sys.stderr)
        return 1
    rec = new_record(args.title, args.channel, text)
    body = render(rec)
    if len(body) > NEW_TEXT_CAP:
        print(_("otklik: сообщение длиннее {limit} знаков, сократи текст").format(limit=NEW_TEXT_CAP),
              file=sys.stderr)
        return 1
    # Сначала сообщение без кнопок и запись с его номером, кнопки последними:
    # иначе быстрый тап застал бы запись без message_id и погасил кнопки.
    rec["status"] = "delivering"
    save(base, rec)
    res = _bot_api("sendMessage", {
        "chat_id": primary_chat_id, "text": body, "link_preview_options": {"is_disabled": True},
    })
    if res is None:
        return 1
    rec["chat_id"] = primary_chat_id
    rec["message_id"] = int(res["result"]["message_id"])
    rec["status"] = "pending"
    save(base, rec)
    if _bot_api("editMessageReplyMarkup", {
        "chat_id": rec["chat_id"], "message_id": rec["message_id"], "reply_markup": _markup(rec),
    }) is None:
        print(_("otklik {id}: сообщение ушло, кнопки не прикрепились").format(id=rec['id']),
              file=sys.stderr)
        return 1
    print(f"otklik {rec['id']} sent, message_id={rec['message_id']}")
    return 0


def _cmd_show(args: argparse.Namespace, base: Path) -> int:
    """Одобренный текст для отправки: только у approved и только при целом хеше."""
    rec = load(base, args.id)
    if rec is None or rec.get("status") != "approved" or record_sha(rec) != rec.get("sha"):
        print(_("otklik: отклик не одобрен или текст изменился после одобрения, не отправлять"),
              file=sys.stderr)
        return 1
    print(_("Куда: {channel}\n---\n{text}").format(channel=rec['channel'], text=rec['text']))
    return 0


def _cmd_list(args: argparse.Namespace, base: Path) -> int:
    for path in sorted(base.glob("*.json")):
        rec = load(base, path.stem)
        if rec and (not args.status or rec.get("status") == args.status):
            print(f"{rec['id']} {rec.get('status')} {rec.get('updated')} {rec.get('title')}")
    return 0


def _cmd_done(args: argparse.Namespace, base: Path) -> int:
    rec = load(base, args.id)
    if rec is None:
        print(_("otklik: нет такой записи"), file=sys.stderr)
        return 1
    rec["status"] = args.status
    rec["note"] = (args.note or "").strip()[:NOTE_CAP]
    rec["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    save(base, rec)
    print(f"otklik {rec['id']}: {rec['status']}")
    if rec.get("message_id") and rec.get("chat_id") is not None:
        if _bot_api("editMessageText", {
            "chat_id": rec["chat_id"], "message_id": rec["message_id"], "text": render(rec),
            "link_preview_options": {"is_disabled": True},
        }) is None:
            print(_("otklik: итог записан, но сообщение в чате не обновилось"), file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_tg_bot.otklik")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new", help="положить отклик и прислать Ивану с кнопками")
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--channel", required=True, help="куда уйдёт и что приложено")
    p_new.add_argument("--file", required=True, help="файл с точным текстом отклика")
    p_done = sub.add_parser("done", help="отметить итог отправки")
    p_done.add_argument("--id", required=True)
    p_done.add_argument("--status", required=True, choices=DONE_STATUSES)
    p_done.add_argument("--note", default="")
    p_show = sub.add_parser("show", help="одобренный текст со сверкой хеша")
    p_show.add_argument("--id", required=True)
    p_list = sub.add_parser("list", help="записи, по умолчанию все")
    p_list.add_argument("--status", default="")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    from .config import load_config
    cfg = load_config(skip_hook_validation=True)
    base = otklik_dir(cfg.state_dir)
    if args.cmd == "new":
        return _cmd_new(args, base, cfg.primary_chat_id)
    if args.cmd == "show":
        return _cmd_show(args, base)
    if args.cmd == "list":
        return _cmd_list(args, base)
    return _cmd_done(args, base)


if __name__ == "__main__":
    sys.exit(main())

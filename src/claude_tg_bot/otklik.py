"""Отклики на вакансии с кнопками ✅ Ок, ✏️ Поправить, ⏭ Пропустить.

Агент кладёт отклик записью и шлёт его Ивану сообщением с кнопками; бот по
нажатию сверяет, что текст тот же, и будит агента с заданием (решение Ивана
12.10.2026: кнопка вместо ответа «ок» на сообщение). Запись неизменяема по
тексту: правка это новая запись, у старой кнопки гаснут. Нажатие ✅ и есть «ок»
на точный текст; отправка в Telegram и Gmail всё равно идёт карточкой бота.

Отправка (ревью Codex 22.10.2026). Все переходы состояния идут под одним
постоянным файлом замка: запись меняется через os.replace, замок на ней самой
ненадёжен. Текст для отправки агент берёт только командой `take`: она переводит
одобренную запись в sending с попыткой (номер и чат рана), второй захват
получает отказ, итог sent или failed ставит только эта попытка. Отправляет
только чат, где отклик готовили (источник). Одобренное, которое ран так и не
взял, бот возвращает в pending с новым поколением кнопок (старая кнопка не
оживает). Взятое, но не закрытое итогом к концу рана, становится unknown:
результат неизвестен, кнопки вернёт только Иван (↩️) после проверки канала.
Так исключены два захвата одной записи; окно неизвестного исхода самой
внешней отправки остаётся, его закрывает проверка канала.

Usage (агент, из корня репозитория бота):
    .venv/bin/python -m claude_tg_bot.otklik new --title "Acme, Senior IT Ops" \\
        --channel "письмо на hr@example.com, PDF резюме EN" --file текст.txt [--replaces <id>]
    .venv/bin/python -m claude_tg_bot.otklik take --id <id>     # текст и номер попытки, один раз
    .venv/bin/python -m claude_tg_bot.otklik show --id <id> --take <попытка>  # перечитать
    .venv/bin/python -m claude_tg_bot.otklik done --id <id> --take <попытка> --status sent --note "ссылка"
    .venv/bin/python -m claude_tg_bot.otklik done --id <id> --status sent --checked --note "нашёл в канале"
    .venv/bin/python -m claude_tg_bot.otklik done --id <id> --status skipped --note "в воронке"
    .venv/bin/python -m claude_tg_bot.otklik list --status approved  # одобренные, но не взятые
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from .i18n import N_, _

OTKLIK_DIRNAME = "otkliki"
LOCK_NAME = ".lock"
CALLBACK_PREFIX = "otk"
# Поколение кнопок между хешем и действием; кнопки до 22.10.2026 без него (0).
_CALLBACK_RE = re.compile(r"otk:([0-9a-f]{12}):([0-9a-f]{12}):(?:(\d{1,6}):)?([oescr])")
_ID_RE = re.compile(r"[0-9a-f]{12}")
# Итоги, которые ставит агент командой done; остальные ставит бот по кнопке.
DONE_STATUSES = ("sent", "failed", "skipped")
# Пропуск и замена только того, что точно не уходило: failed это слова агента,
# а не доказательство (письмо могло дойти), как и unknown (ревью Codex 22.10).
SKIP_FROM = ("pending", "approved", "edit", "skipped")
REPLACE_FROM = ("pending", "edit", "skipped")
# Из каких статусов действует кнопка: ✋ отменяет одобренное до захвата, ↩️
# возвращает кнопки после проверки канала.
PICK_FROM = {"o": ("pending",), "e": ("pending",), "s": ("pending",),
             "c": ("approved",), "r": ("failed", "unknown", "edit")}
NOTE_CAP = 200
# Запас на строку статуса: сообщение потом правится, а лимит Telegram 4096.
NEW_TEXT_CAP = 4096 - 300
STATUS_LINES = {
    "pending": "",
    "approved": N_("✅ одобрено, отправляю…"),
    "sending": N_("📤 отправляю…"),
    "edit": N_("✏️ напиши, что поправить, ответом на это сообщение"),
    "skipped": N_("⏭ пропущено"),
    "sent": N_("📤 отправлено"),
    "failed": N_("⚠️ не ушло, по словам агента"),
    "unknown": N_("❓ результат неизвестен"),
}
# Статусы, по которым сообщение в чате обязано совпадать с записью: итоговые
# старых записей бот не перерисовывает (sync_targets).
LIVE_STATUSES = ("pending", "approved", "edit", "sending", "failed", "unknown")
SYNC_DAYS = 14


class Refused(Exception):
    """Переход не разрешён: запись не в том статусе, не та попытка или кнопка."""


def otklik_dir(state_dir: Path) -> Path:
    return state_dir / OTKLIK_DIRNAME


def text_sha(text: str, channel: str = "") -> str:
    """Хеш одобряемого: текст и куда он уйдёт (адресат, вложение) вместе."""
    return hashlib.sha256(f"{channel}\0{text}".encode("utf-8")).hexdigest()[:12]


def record_sha(rec: dict[str, Any]) -> str:
    return text_sha(rec.get("text", ""), rec.get("channel", ""))


def gen_of(rec: dict[str, Any]) -> int:
    return int(rec.get("gen") or 0)


def source_of(rec: dict[str, Any]) -> str:
    """Чат, где отклик готовили: только его ран отправляет. У записей до
    22.10.2026 источника нет, их готовила личка, куда пришло сообщение."""
    return str(rec.get("source") or rec.get("chat_id"))


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


@contextlib.contextmanager
def locked(base: Path) -> Iterator[None]:
    """Замок всех переходов: бот и команды агента (ревью Codex 22.10.2026).
    Держать только на чтение и запись файлов, без сети."""
    base.mkdir(parents=True, exist_ok=True)
    with open(base / LOCK_NAME, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


def _records(base: Path) -> Iterator[dict[str, Any]]:
    # Временные `.tmp-*.json` при записи не записи: имя не номер.
    for path in sorted(base.glob("*.json")):
        if _ID_RE.fullmatch(path.stem):
            rec = load(base, path.stem)
            if rec:
                yield rec


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_record(title: str, channel: str, text: str) -> dict[str, Any]:
    now = _now()
    return {
        "id": uuid.uuid4().hex[:12], "title": title.strip(), "channel": channel.strip(),
        "text": text, "sha": text_sha(text, channel.strip()), "status": "pending", "note": "",
        "chat_id": None, "message_id": None, "created": now, "updated": now,
        "gen": 0, "source": None, "attempt": None,
    }


def parse_callback(data: str) -> tuple[str, str, int, str] | None:
    m = _CALLBACK_RE.fullmatch(data or "")
    return (m.group(1), m.group(2), int(m.group(3) or 0), m.group(4)) if m else None


def buttons(rec: dict[str, Any]) -> list[list[dict[str, str]]]:
    """Кнопки по статусу: у ждущего ✅ ✏️ ⏭, у одобренного до захвата ✋, у
    неясного, неудачного и ждущего правки ↩️; у остальных сообщение без кнопок."""
    head = f"{CALLBACK_PREFIX}:{rec['id']}:{rec['sha']}:{gen_of(rec)}"
    status = rec.get("status")
    if status == "pending":
        return [[{"text": _("✅ Ок"), "data": f"{head}:o"},
                 {"text": _("✏️ Поправить"), "data": f"{head}:e"},
                 {"text": _("⏭ Пропустить"), "data": f"{head}:s"}]]
    if status == "approved":
        return [[{"text": _("✋ Отменить"), "data": f"{head}:c"}]]
    if status in ("failed", "unknown"):
        return [[{"text": _("↩️ Не ушло, вернуть кнопки"), "data": f"{head}:r"}]]
    if status == "edit":
        # Передумал править: без кнопки запись застряла бы до новой (ревью 22.10).
        return [[{"text": _("↩️ Вернуть кнопки"), "data": f"{head}:r"}]]
    return []


def render(rec: dict[str, Any]) -> str:
    """Текст сообщения: заголовок, канал, отклик целиком, строка статуса."""
    # Номер в первой строке: ответ на сообщение цитирует только его начало.
    parts = [_("📨 {title} (отклик {id})").format(title=rec['title'], id=rec['id'])]
    if rec.get("channel"):
        parts.append(_("Куда: {channel}").format(channel=rec['channel']))
    parts.append("")
    parts.append(rec["text"])
    note = rec.get("note") or ""
    if rec.get("status") == "pending" and note:
        # Кнопки вернул бот: одобренное не ушло, причина в пометке.
        line = _("⚠️ не ушло, нажми ✅ ещё раз")
    else:
        line = _(STATUS_LINES.get(rec.get("status", ""), ""))
    if line:
        parts += ["", line + (f": {note}" if note else "")]
    if rec.get("status") in ("failed", "unknown"):
        parts.append(_("Проверь канал: если не ушло, ↩️ вернёт кнопки, повтор только новым ✅"))
    return "\n".join(parts)


def approved_prompt(rec: dict[str, Any], base: Path) -> str:
    return (
        _("[Кнопка отклика] ✅ ок: отклик {id} «{title}». Куда: {channel}. "
          "Текст бери только командой `.venv/bin/python -m claude_tg_bot.otklik take --id {id}` "
          "из ~/src/claude-tg-bot (она сверяет хеш {sha} и выдаёт номер попытки; отказ значит "
          "не отправлять; take один раз на отклик, перечитать текст: `show --id {id} --take "
          "<попытка>`) и отправь его этим каналом без изменений. Затем отметь итог командой "
          "`.venv/bin/python -m claude_tg_bot.otklik done --id {id} --take <попытка> --status sent|failed "
          "--note \"ссылка или причина\"` из ~/src/claude-tg-bot, обнови воронку и черновики. "
          "Отправить нельзя (путь закрыт, платно, нужен ответ Ивана): текст не бери, скажи "
          "одной строкой почему, кнопки вернутся сами. Остальные одобренные "
          "(`otklik list --status approved`) отправь так же, каждый через take.").format(
              id=rec['id'], title=rec['title'], channel=rec['channel'], sha=rec['sha'])
    )


# ---- Переходы состояния: только под locked() ----

def pick(base: Path, oid: str, sha: str, gen: int, action: str,
         chat_id: int, message_id: int) -> dict[str, Any]:
    """Нажатие кнопки: сверка хеша, поколения, сообщения и статуса, затем переход."""
    with locked(base):
        rec = load(base, oid)
        if (rec is None or rec.get("sha") != sha or rec.get("sha") != record_sha(rec)
                or gen_of(rec) != gen or rec.get("status") not in PICK_FROM[action]
                or rec.get("message_id") != message_id or rec.get("chat_id") != chat_id):
            raise Refused(_("Кнопка устарела: этот отклик уже решён или заменён"))
        if action in ("c", "r"):
            rec.update(status="pending", gen=gen_of(rec) + 1, attempt=None)
        else:
            rec["status"] = {"o": "approved", "e": "edit", "s": "skipped"}[action]
            # Поле attempt отличает одобренное по новому порядку от одобренного
            # до 22.10.2026 (recover): у записей старого кода его нет.
            rec.setdefault("attempt", None)
        # Пометка прошлого возврата («бот перезапускался») к новому решению не относится.
        rec.update(note="", updated=_now())
        save(base, rec)
        return rec


def take(base: Path, oid: str, chat_key: str) -> dict[str, Any]:
    """Захват одобренного для отправки: sending с попыткой этого чата."""
    with locked(base):
        rec = load(base, oid)
        if rec is None:
            raise Refused(_("otklik: нет такой записи"))
        if not chat_key:
            raise Refused(_("otklik: текст выдаётся только рану бота (нет CLAUDE_TG_CHAT_KEY), не отправлять"))
        if source_of(rec) != chat_key:
            raise Refused(_("otklik: отклик готовил другой чат ({source}), отправляет только он").format(
                source=source_of(rec)))
        if rec.get("sha") != record_sha(rec):
            raise Refused(_("otklik: текст изменился после одобрения, не отправлять"))
        # Повторный take отказывает и тому же чату: в одном ране два помощника
        # иначе оба получили бы право отправить (ревью Codex 22.10.2026).
        # Перечитать текст своей попытки: show --take.
        if rec.get("status") != "approved":
            raise Refused(_("otklik: отклик не одобрен или уже взят (статус {status}), не отправлять").format(
                status=rec.get("status")))
        rec.update(status="sending", note="", updated=_now(),
                   attempt={"id": uuid.uuid4().hex[:8], "chat": chat_key, "at": _now()})
        save(base, rec)
        return rec


def own_attempt(rec: dict[str, Any], attempt_id: str, chat_key: str) -> bool:
    """Запись взята именно этой попыткой и этим чатом."""
    attempt = rec.get("attempt") or {}
    return (rec.get("status") == "sending" and bool(attempt_id) and bool(chat_key)
            and attempt.get("id") == attempt_id and attempt.get("chat") == chat_key)


def finish(base: Path, oid: str, status: str, note: str, attempt_id: str = "",
           chat_key: str = "", checked: bool = False) -> dict[str, Any]:
    """Итог агента. sent и failed ставит только попытка, взявшая текст, из
    своего чата. Неясный исход (unknown) закрывается в sent только отдельной
    проверкой канала (checked), не итогом попытки."""
    with locked(base):
        rec = load(base, oid)
        if rec is None:
            raise Refused(_("otklik: нет такой записи"))
        current = rec.get("status")
        if checked:
            if status != "sent" or current != "unknown" or not chat_key:
                raise Refused(_("otklik: --checked только для sent у неясного исхода и из рана бота "
                                "(статус {current})").format(
                    current=current))
        elif status in ("sent", "failed"):
            if not own_attempt(rec, attempt_id, chat_key):
                raise Refused(_("otklik: итог {status} ставит только попытка, взявшая текст "
                                "командой take, из своего чата (статус {current})").format(
                                    status=status, current=current))
        elif current not in SKIP_FROM:
            raise Refused(_("otklik: пропуск только неотправленного (статус {current})").format(current=current))
        rec.update(status=status, note=(note or "").strip()[:NOTE_CAP], updated=_now())
        save(base, rec)
        return rec


def revert_untaken(base: Path, oid: str, gen: int, note: str) -> dict[str, Any] | None:
    """Одобренное, которое ран так и не взял: снова кнопки с новым поколением.
    Поколение то же, что при одобрении: отмена и новое ✅ его не перехватят."""
    with locked(base):
        rec = load(base, oid)
        if rec is None or rec.get("status") != "approved" or gen_of(rec) != gen:
            return None
        rec.update(status="pending", gen=gen + 1, note=(note or "").strip()[:NOTE_CAP], updated=_now())
        save(base, rec)
        return rec


def close_attempts(base: Path, chat_key: str) -> list[dict[str, Any]]:
    """Ран чата кончился: его попытки без итога получают «результат неизвестен».
    Зовётся под замком темы, второго рана этого чата в это время нет."""
    changed = []
    with locked(base):
        for rec in _records(base):
            if rec.get("status") == "sending" and (rec.get("attempt") or {}).get("chat") == chat_key:
                rec.update(status="unknown", updated=_now())
                save(base, rec)
                changed.append(rec)
    return changed


def _undo_replace(base: Path, rec: dict[str, Any]) -> None:
    """Замена не дошла до Ивана: заменённой записи прежний статус, если её с
    тех пор не трогали. Под locked()."""
    was = rec.get("replaces") or {}
    old = load(base, str(was.get("id") or "")) if _ID_RE.fullmatch(str(was.get("id") or "")) else None
    if (old is not None and old.get("status") == "skipped"
            and old.get("note") == _("заменён {id}").format(id=rec["id"])):
        old.update(status=was.get("status") or "pending", note=was.get("note") or "", updated=_now())
        save(base, old)


def recover(base: Path) -> list[dict[str, Any]]:
    """Старт бота: раны прошлого процесса мертвы. Одобренное снова с кнопками,
    взятое получает «результат неизвестен». Одобренное до 22.10.2026 (без поля
    attempt) тоже неизвестно: прежний порядок отправлял прямо из approved, и
    письмо могло уйти до сбоя итога (ревью Codex 22.10.2026)."""
    changed = []
    with locked(base):
        for rec in _records(base):
            if rec.get("status") == "approved" and "attempt" in rec:
                rec.update(status="pending", gen=gen_of(rec) + 1, note=_("бот перезапускался"))
            elif rec.get("status") in ("approved", "sending"):
                rec["status"] = "unknown"
            elif rec.get("status") == "delivering" and not rec.get("message_id"):
                # Обрыв посреди new: сообщения нет, записи тоже не будет, а
                # заменённая получает прежний статус.
                _undo_replace(base, rec)
                record_path(base, rec["id"]).unlink(missing_ok=True)
                continue
            else:
                continue
            rec["updated"] = _now()
            save(base, rec)
            changed.append(rec)
    return changed


def mark_shown(base: Path, oid: str, status: str, gen: int) -> bool:
    """Сообщение в чате показывает это состояние: синхронизация его не тронет.
    False: запись успела смениться, показанное уже устарело, отметка снята."""
    with locked(base):
        rec = load(base, oid)
        if rec is None:
            return True
        current = rec.get("status") == status and gen_of(rec) == gen
        rec["shown"] = [status, gen] if current else None
        save(base, rec)
        return current


def sync_targets(base: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    """Записи, чьё сообщение могло отстать от состояния (правка не прошла):
    живые статусы за SYNC_DAYS дней, у которых показано не то. Итоговые
    записи до 22.10.2026 (без поля shown) не трогаются."""
    now = now or datetime.now().astimezone()
    out = []
    oldest = now.timestamp() - (SYNC_DAYS + 1) * 86400
    for path in sorted(base.glob("*.json")):
        # Старые файлы без чтения: их правили давно, синхронизировать нечего.
        try:
            if path.stat().st_mtime < oldest or not _ID_RE.fullmatch(path.stem):
                continue
        except OSError:
            continue
        rec = load(base, path.stem)
        if not rec:
            continue
        if not rec.get("message_id") or rec.get("status") == "delivering":
            continue
        if rec.get("shown") == [rec.get("status"), gen_of(rec)]:
            continue
        if "shown" not in rec and rec.get("status") not in LIVE_STATUSES:
            continue
        try:
            age = (now - datetime.fromisoformat(str(rec.get("updated")))).days
        except (TypeError, ValueError):
            continue
        if age <= SYNC_DAYS:
            out.append(rec)
    return out


def outbound_match(base: Path, chat_key: str, text: str) -> dict[str, Any] | None:
    """Взятый этим чатом отклик с тем же текстом: для пометки на карточке
    отправки (решение Ивана 22.10.2026, второе подтверждение остаётся)."""
    want = (text or "").replace("\r\n", "\n").strip()
    if not want:
        return None
    for rec in _records(base):
        if (rec.get("status") == "sending" and (rec.get("attempt") or {}).get("chat") == chat_key
                and rec.get("text", "").replace("\r\n", "\n").strip() == want):
            return rec
    return None


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


def _show_in_chat(rec: dict[str, Any], base: Path | None = None) -> bool:
    """Сообщение отклика по записи: текст со статусом и кнопки статуса.
    Показанное отмечается в записи; не прошло, бот перерисует при старте."""
    if not rec.get("message_id") or rec.get("chat_id") is None:
        return True
    ok = _bot_api("editMessageText", {
        "chat_id": rec["chat_id"], "message_id": rec["message_id"], "text": render(rec),
        "reply_markup": _markup(rec), "link_preview_options": {"is_disabled": True},
    }) is not None
    if ok and base is not None:
        mark_shown(base, rec["id"], rec["status"], gen_of(rec))
    return ok


def _cmd_new(args: argparse.Namespace, base: Path, primary_chat_id: int,
             chat_key: str, sources: frozenset[str]) -> int:
    text = Path(args.file).read_text(encoding="utf-8").strip()
    if not text:
        print(_("otklik: пустой текст"), file=sys.stderr)
        return 1
    rec = new_record(args.title, args.channel, text)
    rec["source"] = chat_key or str(primary_chat_id)
    if rec["source"] not in sources:
        print(_("otklik: отклики готовят только чаты Домового, а не {key}").format(key=rec["source"]),
              file=sys.stderr)
        return 1
    body = render(rec)
    if len(body) > NEW_TEXT_CAP:
        print(_("otklik: сообщение длиннее {limit} знаков, сократи текст").format(limit=NEW_TEXT_CAP),
              file=sys.stderr)
        return 1
    # Сначала сообщение без кнопок и запись с его номером, кнопки последними:
    # иначе быстрый тап застал бы запись без message_id и погасил кнопки.
    rec["status"] = "delivering"
    old = None
    with locked(base):
        if args.replaces:
            old = load(base, args.replaces)
            if old is None or old.get("status") not in REPLACE_FROM:
                print(_("otklik: заменить можно только неодобренный и неотправленный отклик"),
                      file=sys.stderr)
                return 1
            # Правка по ✏️ уходит туда же, где отклик готовили. Прежнее
            # состояние старой в новой записи: обрыв до отправки откатит замену.
            rec["source"] = source_of(old)
            rec["replaces"] = {"id": old["id"], "status": old.get("status"), "note": old.get("note") or ""}
            # Новая с данными отката раньше старой: обрыв между записями не
            # оставит заменённую без пути назад (ревью Codex 22.10.2026).
            save(base, rec)
            old.update(status="skipped", note=_("заменён {id}").format(id=rec["id"]), updated=_now())
            save(base, old)
        else:
            save(base, rec)
    if old is not None and not _show_in_chat(old, base):
        print(_("otklik: старый отклик закрыт, но его сообщение не обновилось"), file=sys.stderr)
    res = _bot_api("sendMessage", {
        "chat_id": primary_chat_id, "text": body, "link_preview_options": {"is_disabled": True},
    })
    if res is None:
        # Иван новой записи не видел: её нет, заменённая снова как была.
        with locked(base):
            _undo_replace(base, rec)
            record_path(base, rec["id"]).unlink(missing_ok=True)
            old = load(base, old["id"]) if old is not None else None
        if old is not None:
            _show_in_chat(old, base)
        return 1
    with locked(base):
        rec["chat_id"] = primary_chat_id
        rec["message_id"] = int(res["result"]["message_id"])
        rec["status"] = "pending"
        save(base, rec)
    payload = {"chat_id": rec["chat_id"], "message_id": rec["message_id"], "reply_markup": _markup(rec)}
    if _bot_api("editMessageReplyMarkup", payload) is None and _bot_api("editMessageReplyMarkup", payload) is None:
        # Код 0: запись есть, повтор `new` дал бы второе сообщение. Кнопки
        # дорисует бот после рана (синхронизация по отметке показа).
        print(_("otklik {id}: сообщение ушло, кнопки не прикрепились, бот вернёт их после рана").format(
            id=rec['id']), file=sys.stderr)
    else:
        mark_shown(base, rec["id"], "pending", gen_of(rec))
    print(f"otklik {rec['id']} sent, message_id={rec['message_id']}")
    return 0


def _print_text(rec: dict[str, Any]) -> None:
    print(_("Куда: {channel}\nПопытка: {attempt}\n---\n{text}").format(
        channel=rec['channel'], attempt=(rec.get("attempt") or {}).get("id"), text=rec['text']))


def _cmd_take(args: argparse.Namespace, base: Path, chat_key: str) -> int:
    try:
        rec = take(base, args.id, chat_key)
    except Refused as e:
        print(e, file=sys.stderr)
        return 1
    _print_text(rec)
    # Кнопка ✋ больше не действует: сообщение без неё. Сбой правки не мешает отправке.
    if not _show_in_chat(rec, base):
        print(_("otklik: текст выдан, но сообщение в чате не обновилось"), file=sys.stderr)
    return 0


def _cmd_show(args: argparse.Namespace, base: Path, chat_key: str) -> int:
    """Текст своей попытки, перечитать; одобренный выдаёт только take."""
    rec = load(base, args.id)
    if rec is None or not own_attempt(rec, args.take, chat_key) or record_sha(rec) != rec.get("sha"):
        print(_("otklik: текст для отправки выдаёт только `take --id` (он же сверяет хеш); "
                "без него не отправлять"), file=sys.stderr)
        return 1
    _print_text(rec)
    return 0


def _cmd_list(args: argparse.Namespace, base: Path) -> int:
    for rec in _records(base):
        if not args.status or rec.get("status") == args.status:
            note = f" | {' '.join(rec['note'].split())}" if rec.get("note") else ""
            print(f"{rec['id']} {rec.get('status')} {rec.get('updated')} {rec.get('title')}{note}")
    return 0


def _cmd_done(args: argparse.Namespace, base: Path, chat_key: str) -> int:
    try:
        rec = finish(base, args.id, args.status, args.note, args.take, chat_key, args.checked)
    except Refused as e:
        print(e, file=sys.stderr)
        return 1
    print(f"otklik {rec['id']}: {rec['status']}")
    if not _show_in_chat(rec, base):
        # Код 0: итог записан, а ошибка подтолкнула бы агента отправить ещё раз.
        print(_("otklik: итог записан, сообщение в чате обновит бот после рана"), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_tg_bot.otklik")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new", help="положить отклик и прислать Ивану с кнопками")
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--channel", required=True, help="куда уйдёт и что приложено")
    p_new.add_argument("--file", required=True, help="файл с точным текстом отклика")
    p_new.add_argument("--replaces", default="", help="отклик, который этот заменяет (правка по ✏️)")
    p_take = sub.add_parser("take", help="взять одобренный текст для отправки")
    p_take.add_argument("--id", required=True)
    p_done = sub.add_parser("done", help="отметить итог отправки")
    p_done.add_argument("--id", required=True)
    p_done.add_argument("--status", required=True, choices=DONE_STATUSES)
    p_done.add_argument("--take", default="", help="номер попытки из take (для sent и failed)")
    p_done.add_argument("--checked", action="store_true",
                        help="неясный исход: канал проверен, письмо там есть (только sent)")
    p_done.add_argument("--note", default="")
    p_show = sub.add_parser("show", help="перечитать текст своей попытки")
    p_show.add_argument("--id", required=True)
    p_show.add_argument("--take", required=True, help="номер попытки из take")
    p_list = sub.add_parser("list", help="записи, по умолчанию все")
    p_list.add_argument("--status", default="")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    from .config import load_config
    cfg = load_config(skip_hook_validation=True)
    base = otklik_dir(cfg.state_dir)
    chat_key = os.environ.get("CLAUDE_TG_CHAT_KEY", "").strip()
    try:
        if args.cmd == "new":
            # Личка, мастерская (её поиски готовят отклики) и чаты с автоаппрувом.
            sources = (frozenset({str(cfg.primary_chat_id), getattr(cfg, "workshop_chat_key", "")} - {""})
                       | cfg.approve_auto_chat_keys)
            return _cmd_new(args, base, cfg.primary_chat_id, chat_key, sources)
        if args.cmd == "take":
            return _cmd_take(args, base, chat_key)
        if args.cmd == "show":
            return _cmd_show(args, base, chat_key)
        if args.cmd == "list":
            return _cmd_list(args, base)
        return _cmd_done(args, base, chat_key)
    except ValueError:
        print(_("otklik: неверный номер отклика"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

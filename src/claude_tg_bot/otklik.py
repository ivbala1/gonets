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

Одно касание (решение Ивана 12.11.2026). Запись с точным адресатом (`--tg`
или `--mail` с темой и ответом на письмо) и файлом (`--attach`: имя и sha256)
держит их в хеше кнопки. Ран, взявший её командой `take`, отправляет ровно её
без второй карточки: бот сверяет вызов отправки с записью (`grant`) и пускает
один раз на попытку. Другой текст, адресат или файл идут обычной карточкой;
то же тому же после отправки получает отказ. Карточки до 08:30 приходят без
звука: их кладут поиск и подготовка, когда Иван может спать.

Отправка в срок (решение Ивана 27.11.2026, проект Свода «поручения на время»).
Запись одного касания с `--at` держит время отправки в хеше и на карточке; ✅
на ней до срока не будит ран, а ставит запуск в очередь `/at` бота (переживает
перезапуск, recover такую запись не сбрасывает). `take` и `grant` отказывают
раньше срока и позже AT_WINDOW_SEC после него; пачка «✅ Все N» такие записи
не берёт, правка по ✏️ без своего `--at` наследует время заменённой.

Usage (агент, из корня репозитория бота):
    .venv/bin/python -m claude_tg_bot.otklik new --title "Acme, Senior IT Ops" \\
        --channel "письмо на hr@example.com, PDF резюме EN" --file текст.txt [--replaces <id>]
    ... new ... --tg @hr_example [--attach резюме.pdf]       # одно касание, Telegram
    ... new ... --mail hr@example.com --subject "DevOps" [--reply-to <Message-ID>] [--attach резюме.pdf]
    ... new ... --mail hr@example.com --subject "DevOps" --at "2026-11-28 09:30"  # уйдёт в срок по ✅
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
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import jsonstore, tg_api
from .buttons import keyboard
from .i18n import N_, _

OTKLIK_DIRNAME = "otkliki"
LOCK_NAME = ".lock"
CALLBACK_PREFIX = "otk"
# Поколение кнопок между хешем и действием; кнопки до 22.10.2026 без него (0).
BATCH_PREFIX = "otka"
BATCH_FRESH_HOURS = 2           # старые ждущие в пачку не попадают
BATCH_MAX = 20                  # все названы в сообщении с кнопкой
_BATCH_RE = re.compile(r"otka:([0-9a-f]{12})")
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
# Поля отправки одного касания: входят в хеш кнопки, если заданы (at: время отправки).
SEND_FIELDS = ("via", "to", "subject", "reply_to", "attach", "at")
# Отправка в срок: окно после времени, позже не уходит; дальше AT_MAX_SEC не ставится.
AT_WINDOW_SEC = 2 * 3600
AT_MAX_SEC = 14 * 86400
# Карточки раньше утреннего сообщения Домового (08:30) без звука.
QUIET_UNTIL = (8, 30)
# Запас на строку статуса: сообщение потом правится, а лимит Telegram 4096.
NEW_TEXT_CAP = tg_api.TEXT_LIMIT - 300
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
# Нерешённая пачка «✅ Все N» живёт неделю (уборка бота, prune).
BATCH_KEEP_SEC = 7 * 86400


class Refused(Exception):
    """Переход не разрешён: запись не в том статусе, не та попытка или кнопка."""


class Late(Refused):
    """✅ на записи, чей срок отправки прошёл: текст для Ивана в самом исключении."""


def otklik_dir(state_dir: Path) -> Path:
    return state_dir / OTKLIK_DIRNAME


def text_sha(text: str, channel: str = "") -> str:
    """Хеш одобряемого: текст и куда он уйдёт (адресат, вложение) вместе."""
    return hashlib.sha256(f"{channel}\0{text}".encode("utf-8")).hexdigest()[:12]


def record_sha(rec: dict[str, Any]) -> str:
    """Хеш записи. Поля отправки (адресат, тема, файл) дописываются к каналу,
    только если заданы: хеш записей до 12.11.2026 прежний."""
    extra = {k: rec[k] for k in SEND_FIELDS if rec.get(k)}
    channel = rec.get("channel", "")
    if extra:
        channel += "\0" + json.dumps(extra, ensure_ascii=False, sort_keys=True)
    return text_sha(rec.get("text", ""), channel)


def gen_of(rec: dict[str, Any]) -> int:
    return int(rec.get("gen") or 0)


def source_of(rec: dict[str, Any]) -> str:
    """Чат, где отклик готовили: только его ран отправляет. У записей до
    22.10.2026 источника нет, их готовила личка, куда пришло сообщение."""
    return str(rec.get("source") or rec.get("chat_id"))


def sender_of(rec: dict[str, Any]) -> str:
    """Чат, чей ран отправляет одобренное: тема откликов, которую бот ставит
    по ✅ и «✅ Все N» (pick с route, шаг 1 решения 11.11.2026), иначе источник.
    Источник не меняется: правка по ✏️ и сборка пачки (`otklik all`) идут по нему."""
    return str(rec.get("sender") or source_of(rec))


def record_path(base: Path, oid: str) -> Path:
    if not _ID_RE.fullmatch(oid):
        raise ValueError("bad otklik id")
    return base / f"{oid}.json"


def load(base: Path, oid: str) -> dict[str, Any] | None:
    try:
        path = record_path(base, oid)
    except ValueError:  # чужой id (из кнопки или аргумента): записи нет
        return None
    return jsonstore.read_dict(path, keep_corrupt=False) or None


def save(base: Path, rec: dict[str, Any]) -> None:
    jsonstore.write_json(record_path(base, rec["id"]), rec, indent=2)


@contextlib.contextmanager
def locked(base: Path) -> Iterator[None]:
    """Замок всех переходов: бот и команды агента (ревью Codex 22.10.2026).
    Держать только на чтение и запись файлов, без сети."""
    with jsonstore.file_lock(base / LOCK_NAME):
        yield


def prune(base: Path, now: float, keep_sec: float, *, dry_run: bool = False) -> int:
    """Удалить итоговые записи (DONE_STATUSES) старше keep_sec от updated
    (суточная уборка бота, retention.OTKLIK_DONE_DAYS). Живые не трогает;
    запись без читаемого времени хранится. Под замком переходов."""
    if base.is_symlink() or not base.is_dir():
        return 0
    removed = 0
    with (contextlib.nullcontext() if dry_run else locked(base)):
        for path in sorted(base.glob("*.json")):
            if not _ID_RE.fullmatch(path.stem) or path.is_symlink():
                continue
            rec = load(base, path.stem)
            if not rec or rec.get("status") not in DONE_STATUSES:
                continue
            # Без своего updated возраст последнего изменения неизвестен:
            # запись хранится (ревью Codex 09.11.2026), created не подставляется.
            raw = rec.get("updated")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                updated = datetime.fromisoformat(raw).timestamp()
            except ValueError:
                continue
            if now - updated > keep_sec:
                if not dry_run:
                    path.unlink(missing_ok=True)
                removed += 1
        # Пачки «✅ Все N», которые так и не нажали: кнопка потом ответит
        # «пачка уже решена» (ревью 09.11.2026).
        bdir = base / "batches"
        if not bdir.is_symlink() and bdir.is_dir():
            for path in sorted(bdir.glob("*.json")):
                try:
                    st = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode) and now - st.st_mtime > BATCH_KEEP_SEC:
                    if not dry_run:
                        path.unlink(missing_ok=True)
                    removed += 1
    return removed


def _records(base: Path) -> Iterator[dict[str, Any]]:
    # Временные файлы записи (`.<имя>.*.tmp`, раньше `.tmp-*.json`) не записи.
    for path in sorted(base.glob("*.json")):
        if _ID_RE.fullmatch(path.stem):
            rec = load(base, path.stem)
            if rec:
                yield rec


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_record(title: str, channel: str, text: str, send: dict[str, Any] | None = None) -> dict[str, Any]:
    """send: поля одного касания (SEND_FIELDS), пустые не пишутся."""
    now = _now()
    rec = {
        "id": uuid.uuid4().hex[:12], "title": title.strip(), "channel": channel.strip(),
        "text": text, "status": "pending", "note": "",
        "chat_id": None, "message_id": None, "created": now, "updated": now,
        "gen": 0, "source": None, "attempt": None,
    }
    rec.update({k: v for k, v in (send or {}).items() if k in SEND_FIELDS and v})
    rec["sha"] = record_sha(rec)
    return rec


def _send_lines(rec: dict[str, Any]) -> list[str]:
    """Адресат, тема и файл одного касания строками сообщения."""
    out = []
    if rec.get("via") == "telegram":
        out.append(_("Кому: Telegram {to}").format(to=rec.get("to")))
    elif rec.get("via") == "mail":
        out.append(_("Кому: письмо {to}").format(to=rec.get("to")))
        out.append(_("Тема: {subject}").format(subject=rec.get("subject") or ""))
        if rec.get("reply_to"):
            out.append(_("Ответ на письмо: {id}").format(id=rec["reply_to"]))
    att = rec.get("attach") or {}
    if att:
        out.append(_("Файл: {name}, {size} байт, sha256 {sha}").format(
            name=att.get("name"), size=att.get("size"), sha=str(att.get("sha256"))[:16]))
    if rec.get("at") and not at_expired(rec):  # у просроченного срок в строке ⌛
        out.append(_("Уйдёт {when}, по ✅ без второго вопроса").format(when=at_label(rec)))
    return out


def at_ts(rec: dict[str, Any]) -> float | None:
    """Время отправки записи (секунды) или None: без него или не читается."""
    raw = rec.get("at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        t = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return t.timestamp() if t.tzinfo else None


def at_label(rec: dict[str, Any]) -> str:
    """Время отправки для Ивана: ДД.ММ ЧЧ:ММ в поясе бота."""
    from .config import local_zone
    ts = at_ts(rec)
    return datetime.fromtimestamp(ts, local_zone()).strftime("%d.%m %H:%M") if ts is not None else "?"


def at_refusal(rec: dict[str, Any], now: float | None = None) -> str | None:
    """Почему запись со временем сейчас не отправляется: рано или окно прошло.
    None: времени нет или сейчас его окно. Время не читается: отказ."""
    if not rec.get("at"):
        return None
    ts = at_ts(rec)
    now = time.time() if now is None else now
    if ts is None:
        return _("otklik: время отправки отклика {id} не читается, не отправлять").format(id=rec.get("id"))
    if now < ts:
        return _("otklik: отклик {id} уходит {when}, не раньше: его отправит запуск в срок, "
                 "сейчас не отправлять").format(id=rec.get("id"), when=at_label(rec))
    if now > ts + AT_WINDOW_SEC:
        return _("otklik: срок отправки отклика {id} ({when}) прошёл больше двух часов назад, "
                 "не отправлять").format(id=rec.get("id"), when=at_label(rec))
    return None


def parse_at(spec: str, now: float | None = None) -> str:
    """`--at`: «ГГГГ-ММ-ДД ЧЧ:ММ» в поясе бота (или ISO со смещением) → ISO со
    смещением. ValueError: не понято, уже прошло или дальше AT_MAX_SEC."""
    from .config import local_zone
    try:
        t = datetime.fromisoformat(spec.strip())
    except ValueError:
        raise ValueError(_("--at {spec}: нужно «ГГГГ-ММ-ДД ЧЧ:ММ» (время бота)").format(spec=spec)) from None
    if t.tzinfo is None:
        t = t.replace(tzinfo=local_zone())
    t = t.astimezone(local_zone()).replace(second=0, microsecond=0)
    now = time.time() if now is None else now
    if t.timestamp() <= now:
        raise ValueError(_("--at {spec}: время уже прошло").format(spec=spec))
    if t.timestamp() - now > AT_MAX_SEC:
        raise ValueError(_("--at {spec}: дальше 14 суток не ставится").format(spec=spec))
    return t.isoformat(timespec="minutes")


def _norm_text(text: Any) -> str:
    return str(text or "").replace("\r\n", "\n").strip()


def _norm_to(via: str, to: Any) -> str:
    """Адресат для сверки: Telegram без регистра, но с @ (строку без него
    сервер может понять как имя контакта); у почты без регистра только домен,
    часть до @ может различать регистр (ревью 12.11.2026)."""
    s = str(to or "").strip()
    if via == "telegram":
        return s.lower()
    local, at, domain = s.rpartition("@")
    return f"{local}@{domain.lower()}" if at else s


def send_matches(rec: dict[str, Any], send: dict[str, Any]) -> bool:
    """Вызов отправки ровно тот, что показан в записи: канал, адресат, текст,
    тема, ответ на письмо, файл (имя и sha256) или его отсутствие."""
    via = rec.get("via")
    att = rec.get("attach") or {}
    return (bool(via) and via == send.get("via") and bool(rec.get("to"))
            and _norm_to(via, rec.get("to")) == _norm_to(via, send.get("to"))
            and _norm_text(rec.get("text")) == _norm_text(send.get("text"))
            and (rec.get("subject") or "") == (send.get("subject") or "")
            and (rec.get("reply_to") or "") == (send.get("reply_to") or "")
            and (att.get("name"), att.get("sha256")) == (send.get("attach_name"), send.get("attach_sha")))


def grant(base: Path, chat_key: str, send: dict[str, Any]) -> dict[str, Any] | None:
    """Одно касание: вызов отправки рана chat_key совпал с откликом, который
    этот чат взял (sending) и ещё не отправлял: разрешение и отметка в попытке.
    Refused: то же тому же уже отправлено или разрешено и ждёт итога (повтор,
    в том числе второй записью с тем же текстом). None: не совпало, обычная
    карточка; так же у неудачного и неясного (failed, unknown) и у другой
    записи с тем же, пока такая есть: прежняя отправка могла дойти, решает
    Иван. Одобренное пачкой («✅ Все N») тоже карточкой: пачка не сверяет
    сообщения откликов с записями, как сверяет их ✅ (tg_otklik)."""
    if not chat_key or base.is_symlink() or not base.is_dir():
        return None
    with locked(base):
        recs = [r for r in _records(base) if r.get("via") and send_matches(r, send)
                and r.get("sha") == record_sha(r)]
        unclear = False
        for rec in recs:
            granted = (rec.get("attempt") or {}).get("granted")
            if rec.get("status") == "sent":
                raise Refused(_("otklik: этот текст этому адресату уже отправлен (отклик {id}), "
                                "повтор не отправляется").format(id=rec["id"]))
            if rec.get("status") == "sending" and granted:
                # Разрешение ставится до отправки: она могла и не уйти.
                raise Refused(_("otklik: по отклику {id} уже была отправка или карточка отправки, второй "
                                "вызов сам не уходит. Если первый не ушёл, отметь `done --status failed` "
                                "(не sent), кнопки вернёт Иван ↩️").format(id=rec["id"]))
            unclear = unclear or (rec.get("status") in ("failed", "unknown") and bool(granted))
        if unclear:
            return None
        for rec in recs:
            att = rec.get("attempt") or {}
            if rec.get("status") == "sending" and att.get("chat") == chat_key and not rec.get("batch"):
                if (why := at_refusal(rec)) is not None:
                    raise Refused(why)
                att["granted"] = _now()
                rec.update(attempt=att, updated=_now())
                save(base, rec)
                return rec
    return None


# Взятое чатом: попытка жива или её итог есть (неясный тоже мог дойти).
_TAKEN = ("sending", "sent", "failed", "unknown")


def _taken_with(rec: dict[str, Any], chat_key: str, want: str) -> bool:
    return (bool(want) and rec.get("status") in _TAKEN and (rec.get("attempt") or {}).get("chat") == chat_key
            and _norm_text(rec.get("text")) == want)


def hold(base: Path, chat_key: str, text: str) -> bool:
    """Отправку текста взятого отклика показали карточкой (одно касание не
    прошло): все живые попытки чата с этим текстом своё касание израсходовали,
    тот же вызов потом не уйдёт сам мимо Ивана (ревью 12.11.2026). True:
    до этого по такому тексту уже была отправка или карточка (already_sent),
    проверено тем же проходом под замком, что и отметка: параллельный grant
    или вторая карточка не проскочат между ними."""
    want = _norm_text(text)
    repeat = False
    with locked(base):
        for rec in _records(base):
            if not _taken_with(rec, chat_key, want):
                continue
            repeat = repeat or already_sent(rec)
            att = rec.get("attempt") or {}
            if rec.get("status") == "sending" and not att.get("granted"):
                att["granted"] = _now()
                rec.update(attempt=att, updated=_now())
                save(base, rec)
    return repeat


def already_sent(rec: dict[str, Any]) -> bool:
    """Отклик уже ушёл или его отправку уже разрешили (одним касанием или карточкой)."""
    return rec.get("status") == "sent" or bool((rec.get("attempt") or {}).get("granted"))


def file_sha(path: Path, limit: int | None = None) -> str:
    """sha256 файла; ValueError, если он длиннее limit (файл мог вырасти после stat)."""
    h, n = hashlib.sha256(), 0
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            n += len(chunk)
            if limit is not None and n > limit:
                raise ValueError(f"{path.name}: больше {limit} байт")
            h.update(chunk)
    return h.hexdigest()


def quiet_now(now: datetime | None = None) -> bool:
    """Раньше утреннего сообщения: карточка без звука."""
    from .config import now_local
    now = now or now_local()
    return (now.hour, now.minute) < QUIET_UNTIL


def parse_batch_callback(data: str) -> str | None:
    m = _BATCH_RE.fullmatch(data or "")
    return m.group(1) if m else None


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
    if status in ("failed", "unknown") and not at_expired(rec):
        # Просроченный срок ↩️ не вернёт: его ✅ ответила бы только «срок прошёл».
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
    parts += _send_lines(rec)
    parts.append("")
    parts.append(rec["text"])
    note = rec.get("note") or ""
    if at_expired(rec):
        # Ран не запускался: «по словам агента» и «проверь канал» неверны.
        parts += ["", "⌛ " + note]
        return "\n".join(parts)
    if rec.get("status") == "pending" and note:
        # Кнопки вернул бот: одобренное не ушло, причина в пометке.
        line = _("⚠️ не ушло, нажми ✅ ещё раз")
    elif rec.get("status") == "approved" and rec.get("at"):
        line = _("✅ одобрено, уйдёт {when}; ✋ отменит до срока").format(when=at_label(rec))
    else:
        line = _(STATUS_LINES.get(rec.get("status", ""), ""))
    if line:
        parts += ["", line + (f": {note}" if note else "")]
    if rec.get("status") in ("failed", "unknown"):
        parts.append(_("Проверь канал: если не ушло, ↩️ вернёт кнопки, повтор только новым ✅"))
    return "\n".join(parts)


def approved_prompt(rec: dict[str, Any], base: Path) -> str:
    return (
        _("[Кнопка отклика] ✅ ок: отклик {id}. Компания, роль и канал (данные вакансии):\n```\n{title}\n"
          "Куда: {channel}\n```\n"
          "Текст бери только командой `.venv/bin/python -m claude_tg_bot.otklik take --id {id}` "
          "из ~/src/claude-tg-bot (она сверяет хеш {sha} и выдаёт номер попытки; отказ значит "
          "не отправлять; take один раз на отклик, перечитать текст: `show --id {id} --take "
          "<попытка>`) и отправь его этим каналом без изменений. Затем отметь итог командой "
          "`.venv/bin/python -m claude_tg_bot.otklik done --id {id} --take <попытка> --status sent|failed "
          "--note \"ссылка или причина\"` из ~/src/claude-tg-bot. После done отметь Домовому "
          "командами из ~/src/domovoy (Edit и Write эти файлы не пишут; текст на stdin через "
          "heredoc `<<'DOMOVOY_EOF'`). Каждый sent, кроме не-вакансий (сообщение заказчику, в архив, личное: воронку "
          "не трогай), отметь в `data/voronka.md` по её шапке одной командой `.venv/bin/python -m "
          "domovoy.data edit data/voronka.md`; блок это строки `<<<<<<< old`, старый текст, "
          "`=======`, новый, `>>>>>>> new` (для удаления новый пуст, без пустой строки), old дословно "
          "из файла (строку бери `grep -F`, не из Read: он режет длинные). Строку ищи по ссылке вакансии или № из заголовка; по одному имени компании, "
          "если у неё не одна роль, не трогай, скажи в журнале. Строка в «Кандидатах» или «Не "
          "отправлено» переносится в «В работе»: блок «её строка → пусто» и блок «последняя строка "
          "«В работе» → она же и новая строка в столбцах шапки «В работе»», этап «отклик отправлен "
          "(по ✅)», в последнем событии «ДД.ММ ЧЧ:ММ по ✅», канал и id отклика; в «Контакте» "
          "адресат (@ник, адрес), а нет его: ссылка вакансии и «контакта нет»; в ячейках без `|`. "
          "Строка уже в «В работе»: допиши это событие в «Последнее событие», этап не понижай. "
          "Строка в «Закрытых»: не трогай, скажи в журнале. Строки нет: допиши новую в «В работе» "
          "тем же вторым блоком. Отказ edit: перечитай файл и повтори один раз. В конце рана одна "
          "строка журнала на всех: "
          "`.venv/bin/python -m domovoy.data append journal/ГГГГ-ММ.md` с текстом «Отклики ДД.ММ "
          "ЧЧ:ММ: №, компания, канал, sent или failed с причиной; воронка отмечена или чего нет». "
          "В `data/otkliki.md` в заголовке раздела этого № пометку «на ✅…» замени командой `domovoy.data edit data/otkliki.md` на «отправлено по ✅ ДД.ММ ЧЧ:ММ». "
          "Отправить нельзя (путь закрыт, платно, нужен ответ Ивана): текст не бери, скажи "
          "одной строкой почему, кнопки вернутся сами. Остальные одобренные "
          "(`otklik list --status approved`) отправь так же, каждый через take.").format(
              id=rec['id'], title=rec['title'], channel=rec['channel'], sha=rec['sha'])
        + "\n" + _(SEND_HINT)
    )


SEND_HINT = N_(
    "Если take показал «Кому», отправь ровно туда и ровно так, тогда второй карточки не будет (кроме пачки «✅ Все N»: там она будет): "
    "Telegram без файла `send_message` (chat_id из «Кому», message это текст, других полей нет); "
    "Telegram с файлом `send_file` (chat_id, file_path это путь файла из take, caption это текст, "
    "parse_mode \"plain\"); письмо `.venv/bin/python -m claude_tg_bot.mailsend --to <Кому> "
    "--subject <Тема> --body-file <файл с текстом> [--reply-to-message-id <Ответ на>] "
    "[--attach <путь файла>]`. Иное уйдёт Ивану обычной карточкой. Второй вызов того же бот "
    "отклонит: если первый не ушёл, отметь `done --status failed`, не sent.")


# ---- Переходы состояния: только под locked() ----

def pick_ok(rec: dict[str, Any] | None, sha: str, gen: int, action: str, chat_id: int, message_id: int) -> bool:
    """Кнопка действует: хеш, поколение, сообщение и статус записи совпадают."""
    return not (rec is None or rec.get("sha") != sha or rec.get("sha") != record_sha(rec)
                or gen_of(rec) != gen or rec.get("status") not in PICK_FROM[action]
                or (action == "r" and at_expired(rec))
                or rec.get("message_id") != message_id or rec.get("chat_id") != chat_id)


def shown_text(rec: dict[str, Any]) -> str:
    """Текст сообщения отклика, как его вернул Telegram при показе этого
    состояния; не записан, значит текст по записи (render)."""
    text = rec.get("shown_text")
    if isinstance(text, str) and text and rec.get("shown") == [rec.get("status"), gen_of(rec)]:
        return text
    return render(rec)


def pick(base: Path, oid: str, sha: str, gen: int, action: str,
         chat_id: int, message_id: int, route: str = "", batch: bool = False) -> dict[str, Any]:
    """Нажатие кнопки: сверка хеша, поколения, сообщения и статуса, затем переход.
    route: тема откликов; ✅ отдаёт отправку ей (sender), без неё источнику.
    batch: ✅ пачкой, отправка тогда карточкой (grant)."""
    with locked(base):
        rec = load(base, oid)
        if not pick_ok(rec, sha, gen, action, chat_id, message_id):
            raise Refused(_("Кнопка устарела: этот отклик уже решён или заменён"))
        if action == "o" and rec.get("at"):
            if batch:
                # Пачка такие не берёт (_cmd_all): срок одобряется на своей карточке.
                raise Refused(_("Кнопка устарела: этот отклик уже решён или заменён"))
            ts = at_ts(rec)
            if ts is None or time.time() > ts + AT_WINDOW_SEC:
                raise Late(_("Срок отправки {when} прошёл: попроси поставить отклик заново").format(
                    when=at_label(rec)))
        if action in ("c", "r"):
            rec.update(status="pending", gen=gen_of(rec) + 1, attempt=None)
        else:
            rec["status"] = {"o": "approved", "e": "edit", "s": "skipped"}[action]
            if action == "o":
                # Тем же переходом, что одобрение: взять текст сможет только ран
                # этого чата, и никакой другой в промежутке (take).
                if route:
                    rec["sender"] = route
                else:
                    rec.pop("sender", None)
                if batch:
                    rec["batch"] = True
                else:
                    rec.pop("batch", None)
            # Поле attempt отличает одобренное по новому порядку от одобренного
            # до 22.10.2026 (recover): у записей старого кода его нет.
            rec.setdefault("attempt", None)
        # Пометка прошлого возврата («бот перезапускался») к новому решению не относится.
        rec.update(note="", updated=_now())
        save(base, rec)
        return rec


def _batch_path(base: Path, bid: str) -> Path:
    return base / "batches" / f"{bid}.json"


def pick_all(base: Path, bid: str, chat_id: int, message_id: int, route: str = "") -> list[dict[str, Any]]:
    """«✅ Все N»: файл пачки держит хеш и поколение каждого показанного
    отклика, запись одобряет та же `pick`. Устаревшую запись (решена отдельно,
    заменена) пачка пропускает, а не одобряет. Файл снимается сразу: второе
    нажатие ничего не делает."""
    path = _batch_path(base, bid)
    with locked(base):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Refused(_("Кнопка устарела: эта пачка уже решена")) from e
        if data.get("chat_id") != chat_id or data.get("message_id") != message_id:
            raise Refused(_("Кнопка устарела: эта пачка уже решена"))
        path.unlink(missing_ok=True)
    picked = []
    for item in data.get("items") or []:
        try:
            picked.append(pick(base, item["id"], item["sha"], int(item["gen"]), "o",
                               int(item["chat_id"]), int(item["message_id"]), route, batch=True))
        except (Refused, KeyError, TypeError, ValueError, OSError):
            continue  # не записалось: отклик остаётся с кнопками, остальные идут
    return picked


def take(base: Path, oid: str, chat_key: str) -> dict[str, Any]:
    """Захват одобренного для отправки: sending с попыткой этого чата."""
    with locked(base):
        rec = load(base, oid)
        if rec is None:
            raise Refused(_("otklik: нет такой записи"))
        if not chat_key:
            raise Refused(_("otklik: текст выдаётся только рану бота (нет CLAUDE_TG_CHAT_KEY), не отправлять"))
        if sender_of(rec) != chat_key:
            raise Refused(_("otklik: этот отклик отправляет другой чат ({source}), не этот; не отправлять").format(
                source=sender_of(rec)))
        if rec.get("sha") != record_sha(rec):
            raise Refused(_("otklik: текст изменился после одобрения, не отправлять"))
        # Повторный take отказывает и тому же чату: в одном ране два помощника
        # иначе оба получили бы право отправить (ревью Codex 22.10.2026).
        # Перечитать текст своей попытки: show --take.
        if rec.get("status") != "approved":
            raise Refused(_("otklik: отклик не одобрен или уже взят (статус {status}), не отправлять").format(
                status=rec.get("status")))
        if (why := at_refusal(rec)) is not None:
            raise Refused(why)
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


def at_expired(rec: dict[str, Any]) -> bool:
    """Отклик в срок, который бот пометил failed по прошедшему окну: ран его не
    брал (attempt пуст), значит точно не уходил; заменять его можно."""
    return rec.get("status") == "failed" and bool(rec.get("at")) and not rec.get("attempt")


def expire_scheduled(base: Path, oid: str, gen: int, note: str) -> dict[str, Any] | None:
    """Одобренное в срок, чьё окно прошло без отправки (бот или ПК не работали,
    ран ждал тему): failed с причиной. Не skipped: пропуск (⏭) утро и поиск
    переносят в воронку как решение Ивана."""
    with locked(base):
        rec = load(base, oid)
        if rec is None or rec.get("status") != "approved" or gen_of(rec) != gen:
            return None
        rec.update(status="failed", note=(note or "").strip()[:NOTE_CAP], updated=_now())
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


def recover(base: Path, scheduled: frozenset[tuple[str, int]] = frozenset()) -> list[dict[str, Any]]:
    """Старт бота: раны прошлого процесса мертвы. Одобренное снова с кнопками,
    взятое получает «результат неизвестен». Одобренное до 22.10.2026 (без поля
    attempt) тоже неизвестно: прежний порядок отправлял прямо из approved, и
    письмо могло уйти до сбоя итога (ревью Codex 22.10.2026). scheduled: пары
    (id, поколение) записей, чья отправка в срок лежит в очереди /at: они ждут
    её одобренными."""
    changed = []
    with locked(base):
        for rec in _records(base):
            if rec.get("status") == "approved" and rec.get("at") and (rec.get("id"), gen_of(rec)) in scheduled:
                continue
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


def mark_shown(base: Path, oid: str, status: str, gen: int, text: str | None = None) -> bool:
    """Сообщение в чате показывает это состояние: синхронизация его не тронет.
    text: текст сообщения из ответа Telegram, эталон сверки на ✅ (shown_text);
    без него прежний текст остаётся, только если состояние то же.
    False: запись успела смениться, показанное уже устарело, отметка снята."""
    with locked(base):
        rec = load(base, oid)
        if rec is None:
            return True
        current = rec.get("status") == status and gen_of(rec) == gen
        if text and current:
            rec["shown_text"] = text
        elif not current or rec.get("shown") != [status, gen]:
            rec.pop("shown_text", None)
        rec["shown"] = [status, gen] if current else None
        save(base, rec)
        return current


def sync_targets(base: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    """Записи, чьё сообщение могло отстать от состояния (правка не прошла):
    живые статусы за SYNC_DAYS дней, у которых показано не то. Итоговые
    записи до 22.10.2026 (без поля shown) не трогаются."""
    now = now or datetime.now(timezone.utc)
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
    отправки, когда одно касание не прошло (grant: другой адресат, файл или
    поле вызова, отклик без адресата). Уже ушедший или разрешённый первым
    (already_sent): пометка тогда предупреждает о повторе."""
    want = _norm_text(text)
    found = [rec for rec in _records(base) if _taken_with(rec, chat_key, want)]
    found.sort(key=lambda r: not already_sent(r))
    return found[0] if found else None


# ---- CLI для агента ----

def _bot_api(method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    return tg_api.cli_call("otklik", method, payload)


def _result_text(res: Any) -> str | None:
    """Текст сообщения из ответа Bot API (`result.text`)."""
    result = res.get("result") if isinstance(res, dict) else None
    text = result.get("text") if isinstance(result, dict) else None
    return text if isinstance(text, str) and text else None


def _markup(rec: dict[str, Any]) -> dict[str, Any]:
    return keyboard(buttons(rec))


def _show_in_chat(rec: dict[str, Any], base: Path | None = None) -> bool:
    """Сообщение отклика по записи: текст со статусом и кнопки статуса.
    Показанное отмечается в записи; не прошло, бот перерисует при старте."""
    if not rec.get("message_id") or rec.get("chat_id") is None:
        return True
    res = _bot_api("editMessageText", {
        "chat_id": rec["chat_id"], "message_id": rec["message_id"], "text": render(rec),
        "reply_markup": _markup(rec), "link_preview_options": {"is_disabled": True},
    })
    if res is not None and base is not None:
        mark_shown(base, rec["id"], rec["status"], gen_of(rec), _result_text(res))
    return res is not None


# Предел подписи к файлу в Telegram, в единицах UTF-16.
CAPTION_LIMIT = 1024
# Файл больше одно касание Telegram не сверяет (outbound.otklik_send): хеш на каждый вызов.
TG_FILE_MAX = 50 * 1024 * 1024
# Адресат Telegram однозначно: @имя, номер чата или телефон, не имя контакта.
_TG_TO_RE = re.compile(r"@[A-Za-z0-9_]{4,32}|-?\d{1,20}|\+\d{7,15}")


def _send_args(args: argparse.Namespace, text: str = "") -> dict[str, Any]:
    """Поля одного касания из аргументов `new`; ValueError: неполные, лишние,
    адрес или подпись, которых не примет отправка. Каталоги вложений
    mailsend и корни сервера Telegram здесь не сверяются: их отказ придёт
    при отправке."""
    from .mailsend import _ADDR_RE, _MSGID_RE
    # getattr: `new` без этих ключей (вызовы до 12.11.2026, тесты) это отклик без одного касания.
    tg, mail, attach = (getattr(args, k, "") or "" for k in ("tg", "mail", "attach"))
    via, to = ("telegram", tg) if tg else ("mail", mail) if mail else ("", "")
    to = to.strip()
    subject, reply_to = ((getattr(args, k, "") or "").strip() for k in ("subject", "reply_to"))
    if via != "mail" and (subject or reply_to):
        raise ValueError(_("--subject и --reply-to только с --mail"))
    if via == "mail" and (not subject or "\n" in subject or "\r" in subject):
        raise ValueError(_("письму нужна тема в одну строку (--subject)"))
    if via and (not to or any(c.isspace() for c in to)):
        raise ValueError(_("адресат пустой или с пробелом: --tg @имя или --mail адрес"))
    if via == "telegram" and not _TG_TO_RE.fullmatch(to):
        raise ValueError(_("--tg {to}: нужен @имя, номер чата или +телефон").format(to=to))
    if via == "mail" and not _ADDR_RE.fullmatch(to):
        raise ValueError(_("--mail {to}: не адрес почты").format(to=to))
    if reply_to and not _MSGID_RE.fullmatch(reply_to):
        raise ValueError(_("--reply-to нужен в виде <...@...>"))
    send: dict[str, Any] = {"via": via, "to": to, "subject": subject, "reply_to": reply_to}
    at = (getattr(args, "at", "") or "").strip()
    if at:
        if not via:
            raise ValueError(_("--at только с адресатом (--tg или --mail): в срок уходит без второй карточки"))
        send["at"] = parse_at(at)
    if attach:
        real = Path(attach).expanduser().resolve(strict=True)
        if not real.is_file():
            raise ValueError(_("--attach {path} не обычный файл").format(path=real))
        send["attach"] = {"name": real.name, "size": real.stat().st_size, "sha256": file_sha(real),
                          "path": str(real)}
        if via == "telegram":
            from .replies import utf16_len
            if utf16_len(text) > CAPTION_LIMIT:
                raise ValueError(_("Telegram с файлом: текст идёт подписью, а она до {n} знаков; "
                                   "сократи или шли файл без --attach").format(n=CAPTION_LIMIT))
            if send["attach"]["size"] > TG_FILE_MAX:
                raise ValueError(_("Telegram с файлом: файл больше {mb} МБ одно касание не сверяет").format(
                    mb=TG_FILE_MAX // (1024 * 1024)))
    return send


def _cmd_new(args: argparse.Namespace, base: Path, primary_chat_id: int,
             chat_key: str, sources: frozenset[str]) -> int:
    text = Path(args.file).read_text(encoding="utf-8").strip()
    if not text:
        print(_("otklik: пустой текст"), file=sys.stderr)
        return 1
    try:
        send = _send_args(args, text)
        prev = load(base, args.replaces) if args.replaces and not send.get("at") else None
        if prev is not None and prev.get("at"):
            # Правка по ✏️ без своего времени: срок заменённой, а не немедленная отправка.
            if not send.get("via"):
                raise ValueError(_("заменяемый отклик уходит в срок ({when}): нужен адресат --tg или --mail "
                                   "(и --at, если время другое)").format(when=at_label(prev)))
            if at_refusal(prev) is not None and (at_ts(prev) or 0) <= time.time():
                raise ValueError(_("срок заменяемого отклика ({when}) прошёл: задай новое время --at").format(
                    when=at_label(prev)))
            send["at"] = prev["at"]
    except (OSError, ValueError) as e:
        print(f"otklik: {e}", file=sys.stderr)
        return 1
    rec = new_record(args.title, args.channel, text, send)
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
            if old is None or (old.get("status") not in REPLACE_FROM and not at_expired(old)):
                if old is not None and old.get("status") == "approved" and old.get("at"):
                    # Одобренный в срок стоит в очереди /at: новая карточка без ✋ его не снимет
                    # (ревью Codex 27.11.2026, перенос времени).
                    print(_("otklik: отклик {id} одобрен и уйдёт {when}; без ✋ Ивана на его карточке он остаётся в очереди "
                            "на этот срок, и новая карточка его не снимет. Перенести или поправить: ✋ "
                            "(кнопки вернутся), потом new --replaces {id}").format(
                                id=old["id"], when=at_label(old)), file=sys.stderr)
                    return 1
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
        "disable_notification": quiet_now(),
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
    shown = _bot_api("editMessageReplyMarkup", payload)
    if shown is None:
        # Код 0: запись есть, повтор `new` дал бы второе сообщение. Кнопки
        # дорисует бот после рана (синхронизация по отметке показа).
        print(_("otklik {id}: сообщение ушло, кнопки не прикрепились, бот вернёт их после рана").format(
            id=rec['id']), file=sys.stderr)
    else:
        mark_shown(base, rec["id"], "pending", gen_of(rec), _result_text(shown) or _result_text(res))
    print(f"otklik {rec['id']} sent, message_id={rec['message_id']}")
    return 0


def _created_at(rec: dict) -> datetime | None:
    """Время создания записи как aware datetime. Строки сравнивать нельзя: в
    записи смещение пояса, а пояс у пишущего и читающего может разниться.
    Строка без смещения считается в поясе системы, как её писал _now()."""
    try:
        t = datetime.fromisoformat(str(rec.get("created") or ""))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def _cmd_all(base: Path, primary_chat_id: int, chat_key: str) -> int:
    """Одно сообщение с кнопкой «✅ Все N» под показанной пачкой: только свежие
    карточки этого чата, каждая названа в списке. Ждущих меньше двух: ничего,
    хватает кнопки на карточке. Кнопка последней, как у карточки: сообщение,
    потом файл пачки, потом кнопка."""
    source = chat_key or str(primary_chat_id)
    fresh_from = datetime.now(timezone.utc) - timedelta(hours=BATCH_FRESH_HOURS)
    with locked(base):
        recs = [r for r in _records(base) if r.get("status") == "pending" and r.get("message_id")
                and not r.get("at") and r.get("chat_id") is not None and source_of(r) == source
                and (t := _created_at(r)) is not None and t > fresh_from][-BATCH_MAX:]
    if len(recs) < 2:
        print(_("otklik: ждущих меньше двух, кнопка не нужна"))
        return 0
    lines = [_("📨 Ждут «ок»: {count}").format(count=len(recs))]
    lines += [f"{i}. {r['title'][:150]}" for i, r in enumerate(recs, 1)]
    bid = uuid.uuid4().hex[:12]
    res = _bot_api("sendMessage", {"chat_id": primary_chat_id, "text": "\n".join(lines),
                                   "disable_notification": quiet_now()})
    if res is None:
        return 1
    path = _batch_path(base, bid)
    with locked(base):
        jsonstore.write_json(path, {
            "chat_id": primary_chat_id, "message_id": int(res["result"]["message_id"]),
            "items": [{"id": r["id"], "sha": r["sha"], "gen": gen_of(r), "chat_id": r["chat_id"],
                       "message_id": r["message_id"]} for r in recs]})
    if _bot_api("editMessageReplyMarkup", {
        "chat_id": primary_chat_id, "message_id": int(res["result"]["message_id"]),
        "reply_markup": keyboard([[{"text": _("✅ Все {count}").format(count=len(recs)),
                                    "data": f"{BATCH_PREFIX}:{bid}"}]]),
    }) is None:
        # Файл пачки остаётся: правка могла пройти без ответа (повторы при
        # сбое связи), и видимая кнопка без файла отказала бы. Кнопки нет:
        # файл никто не тронет (ревью Codex 08.11.2026).
        return 1
    print(f"otklik batch {bid}: {len(recs)}")
    return 0


def _print_text(rec: dict[str, Any]) -> None:
    lines = _send_lines(rec)
    if rec.get("attach"):
        lines.append(_("Путь файла: {path}").format(path=rec["attach"].get("path")))
    print(_("Куда: {channel}\n{send}Попытка: {attempt}\n---\n{text}").format(
        channel=rec['channel'], send="".join(f"{line}\n" for line in lines),
        attempt=(rec.get("attempt") or {}).get("id"), text=rec['text']))


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
            if rec.get("at"):
                note += " | " + _("уйдёт {when}").format(when=at_label(rec))
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
    p_new.add_argument("--replaces", default="", help="отклик, который этот заменяет (правка по ✏️; одобренный в срок сначала снимает ✋ Ивана)")
    to = p_new.add_mutually_exclusive_group()
    to.add_argument("--tg", default="", help="одно касание: адресат Telegram (@имя или номер чата)")
    to.add_argument("--mail", default="", help="одно касание: адрес письма (mailsend)")
    p_new.add_argument("--subject", default="", help="тема письма (с --mail)")
    p_new.add_argument("--reply-to", default="", help="Message-ID письма, на которое ответ (с --mail)")
    p_new.add_argument("--attach", default="", help="файл к отправке: имя и sha256 на карточке")
    p_new.add_argument("--at", default="",
                       help="отправить в срок: \"ГГГГ-ММ-ДД ЧЧ:ММ\" по времени бота, только с --tg или --mail")
    sub.add_parser("all", help="прислать кнопку «✅ Все N» под пачкой ждущих откликов")
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
        if args.cmd == "all":
            return _cmd_all(base, cfg.primary_chat_id, chat_key)
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

"""Дела: вопросы утреннего плана, ответ на которые продолжает разговор с
момента вопроса (опыт В1, docs/design-2026-10-26-v1.md).

Дело заводится вопросом рана с флагом утра: точка вопроса (sid и uuid
последнего ответа модели) ложится на диск вместе с вариантами, кнопка
работает и после рестарта. Ответ уходит в ответвление сессии с этой точки
(`--resume-session-at --fork-session`), дальше дело живёт своей сессией.

Реестр один файл state/deals/deals.json под замком файла, писатель один:
процесс бота. Состояния хода: asked (вопрос ждёт ответа), accepted (ответ
принят, ход не начинался), running (ход начался), idle (ход кончился),
failed, interrupted (рестарт посреди хода, исход неизвестен), closed.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

DEALS_SUBDIR = "deals"
# Дело без ходов дольше суток закрывается само.
DEAL_IDLE_SEC = 24 * 3600
# Не больше двух открытых дел в чате: третий вопрос идёт обычной карточкой.
DEAL_CAP = 2
LABEL_CAP = 24
# Закрытые дела держатся неделю с запасом: по ним разбор недели.
KEEP_CLOSED_SEC = 8 * 24 * 3600
NOTE_CAP = 300

OPEN_STATES = ("asked", "accepted", "running", "idle", "failed", "interrupted")

# Первая строка сообщения дела: «🧵 <id> · <ярлык>».
MARK = "🧵"
_HEAD_RE = re.compile(r"^🧵 ([0-9a-f]{8}) · ")
_ID_RE = re.compile(r"[0-9a-f]{8}")


def deals_dir(state_dir: Path) -> Path:
    return state_dir / DEALS_SUBDIR


def _path(base: Path) -> Path:
    return base / "deals.json"


@contextlib.contextmanager
def locked(base: Path):
    """Реестр под замком файла: нажатие, свайп и конец хода приходят
    параллельно, а ответ принимается ровно один раз. Нечитаемый реестр это
    отказ, а не пустой: запись затёрла бы все дела."""
    base.mkdir(parents=True, exist_ok=True)
    with open(base / "deals.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = _path(base)
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: не словарь")
        before = json.dumps(data, ensure_ascii=False, indent=1)
        yield data
        after = json.dumps(data, ensure_ascii=False, indent=1)
        if after == before:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(after, encoding="utf-8")
        os.replace(tmp, path)


def load(base: Path) -> dict[str, dict]:
    """Реестр на чтение; нечитаемый пустой (только для показа и маршрута)."""
    try:
        data = json.loads(_path(base).read_text(encoding="utf-8")) if _path(base).is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def make_label(question: str) -> str:
    words = " ".join(question.split())
    return words if len(words) <= LABEL_CAP else words[: LABEL_CAP - 1].rstrip() + "…"


def head(deal: dict) -> str:
    """Первая строка сообщений дела: по ней свайп находит дело."""
    return f"{MARK} {deal['id']} · {deal.get('label', '')}"


def id_from_text(text: str | None) -> str | None:
    m = _HEAD_RE.match(text or "")
    return m.group(1) if m else None


def is_open(deal: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return deal.get("state") in OPEN_STATES and float(deal.get("last_at") or 0) > now - DEAL_IDLE_SEC


def open_count(data: dict, chat_id: int, thread_id: int | None, now: float | None = None) -> int:
    return sum(1 for d in data.values()
               if d.get("chat_id") == chat_id and d.get("thread_id") == thread_id and is_open(d, now))


# Дело старше этого, не занятое ходом, новое утро закрывает: место новым вопросам.
STALE_FOR_NEW_SEC = 12 * 3600


def close_stale(data: dict, chat_id: int, thread_id: int | None, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for d in data.values():
        if (d.get("chat_id") == chat_id and d.get("thread_id") == thread_id
                and d.get("state") in ("asked", "idle", "failed", "interrupted")
                and float(d.get("created_at") or 0) < now - STALE_FOR_NEW_SEC):
            d.update(state="closed")


def new_deal(*, chat_id: int, thread_id: int | None, qsid: str, quuid: str, question: str,
             options: list[str], now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    return {
        "id": uuid.uuid4().hex[:8], "chat_id": chat_id, "thread_id": thread_id,
        "label": make_label(question), "qsid": qsid, "quuid": quuid, "sid": None,
        "question": question, "options": list(options), "card_msg": None, "asked_at": now,
        "answer": None, "answered_at": None, "state": "asked", "created_at": now,
        "last_at": now, "turns": 0, "notes": [], "error": None,
    }


def ask_again(deal: dict, question: str, options: list[str], now: float | None = None) -> None:
    """Новый вопрос внутри дела: то же дело, новая карточка."""
    now = time.time() if now is None else now
    deal.update(question=question, options=list(options), card_msg=None, asked_at=now,
                answer=None, answered_at=None, state="asked", last_at=now)


# Откуда свайп начинает ход: дело ждёт вопроса или прошлый ход кончился.
SWIPE_STATES = ("asked", "idle", "failed", "interrupted")


def accept(deal: dict, answer: str, now: float | None = None, *, states: tuple[str, ...] = ("asked",)) -> bool:
    """→ accepted с ответом. Кнопка принимается только у asked, свайп и у
    кончившегося хода. False: дело занято, закрыто или уже отвечено."""
    now = time.time() if now is None else now
    if deal.get("state") not in states or not is_open(deal, now):
        return False
    # Ответ на вопрос (asked) идёт в сессию вместе с вопросом, следующий ход дела как есть.
    deal.update(answer=answer, answered_at=now, state="accepted", last_at=now, error=None,
                answering=deal.get("state") == "asked")
    return True


def start(deal: dict, answer: str) -> bool:
    """accepted → running при старте процесса, только если это тот же ответ."""
    if deal.get("state") != "accepted" or deal.get("answer") != answer:
        return False
    deal.update(state="running", turns=int(deal.get("turns") or 0) + 1, last_at=time.time())
    return True


def note_text(deal: dict, said: str, reply: str) -> str:
    """Строка итога для главного разговора лички."""
    reply = " ".join(reply.split())
    if len(reply) > NOTE_CAP:
        reply = reply[: NOTE_CAP - 1].rstrip() + "…"
    return f"дело «{deal.get('label', '')}»: Иван ответил «{said}»; итог: {reply}"


def add_note(deal: dict, text: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    deal.setdefault("notes", []).append({"at": now, "text": text})


def pending_notes(data: dict, chat_id: int, thread_id: int | None) -> list[tuple[str, float, str]]:
    """Строки итога дел этого чата для главной сессии: (id дела, метка, текст)
    по времени. Читаются без снятия: снимает drop_notes при старте рана."""
    out = []
    for d in data.values():
        if d.get("chat_id") == chat_id and d.get("thread_id") == thread_id:
            out += [(d["id"], float(n.get("at") or 0), str(n.get("text") or "")) for n in d.get("notes") or []]
    return sorted(out, key=lambda n: n[1])


def drop_notes(data: dict, taken: list[tuple[str, float, str]]) -> None:
    """Снять ровно поданные строки; пришедшие после чтения остаются."""
    for did, at, text in taken:
        d = data.get(did)
        if d is not None:
            d["notes"] = [n for n in d.get("notes") or []
                          if not (float(n.get("at") or 0) == at and n.get("text") == text)]


def startup(data: dict, now: float | None = None) -> tuple[list[dict], list[dict]]:
    """После рестарта: accepted будятся заново (ход не начинался), running
    становятся interrupted (исход неизвестен, повтора нет). Принятый ответ
    старше суток не запускается, а называется (error=expired): обязательство
    не теряется молча. Закрытые и просроченные старше KEEP_CLOSED_SEC
    выбрасываются, кроме ждущих исхода."""
    now = time.time() if now is None else now
    wake, cut = [], []
    for did, d in list(data.items()):
        if not isinstance(d, dict) or not _ID_RE.fullmatch(str(did)):
            data.pop(did, None)
            continue
        if d.get("state") == "running":
            d.update(state="interrupted", last_at=now)
            cut.append(d)
            continue
        if d.get("state") == "accepted":
            if float(d.get("answered_at") or 0) < now - DEAL_IDLE_SEC:
                d.update(state="failed", error="expired", last_at=now)
                cut.append(d)
            else:
                wake.append(d)
            continue
        if float(d.get("last_at") or 0) < now - KEEP_CLOSED_SEC:
            data.pop(did, None)
            continue
        if not is_open(d, now) and d.get("state") != "closed":
            d["state"] = "closed"
    return wake, cut

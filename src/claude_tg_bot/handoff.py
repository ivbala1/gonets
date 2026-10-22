"""Передача экрана: агент в браузере упёрся в капчу или вход, Иван проходит сам.

Агент кладёт запись и шлёт Ивану сообщение с кнопками «🖥 Открыть экран»
(ссылка на страницу службы screen_server.py, наружу через Tailscale Funnel),
«✅ Готово» и «🙅 Не могу», затем ждёт итога командой wait. Кнопки бота и
страница только меняют статус записи; нового рана не запускают: тот же ран
агента ждёт и продолжает в той же вкладке (решение Ивана 20.10.2026). Капчи
и входы агент сам не решает никогда.

Экран живёт до 10 минут. В записи только хеш токена, сам токен уходит в
ссылке после «#»: во фрагменте он не попадает в журнал сервера и в Referer.
Запись меняют три процесса (бот, wait, служба экрана): проверка и запись
итога идут под общей блокировкой (ревью Codex 20.10.2026), итог не
перезаписывается.

Капча с символами (решение Ивана 21.10.2026): `ask --image <картинка>` шлёт
фото капчи с теми же кнопками; Иван может ответить на него буквами, бот
кладёт ответ в запись, нового рана нет, wait печатает «answer: …» и выходит
с кодом 4. Ответ попадает в переписку и журнал рана, поэтому так передаются
только буквы капчи; пароли и коды из СМС только экраном.

Usage (агент, из корня репозитория бота, в своей сессии AGENT_BROWSER_SESSION):
    .venv/bin/python -m claude_tg_bot.handoff ask --reason "капча после откликов" --site hh.ru \\
        [--region x,y,ширина,высота] [--image капча.png] [--quiet]
    .venv/bin/python -m claude_tg_bot.handoff wait --id <id> --timeout 580
        # коды: 0 готово, 2 не может, 3 время вышло, 4 ответ буквами (строка answer:);
        # вызывать с таймаутом Bash 600000
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .i18n import N_, _

HANDOFF_DIRNAME = "handoff"
CALLBACK_PREFIX = "ho"
_CALLBACK_RE = re.compile(r"ho:([0-9a-f]{12}):([dc])")
_ID_RE = re.compile(r"[0-9a-f]{12}")
CHAT_KEY_ENV = "CLAUDE_TG_CHAT_KEY"
SHOW_CHAT_KEY_ENV = "CLAUDE_TG_SHOW_CHAT_KEY"
SESSION_ENV = "AGENT_BROWSER_SESSION"
TTL_SECONDS = 600
DEFAULT_WAIT = 580
RATE_LIMIT = 3          # передач на чат
RATE_WINDOW = 3600      # за столько секунд
KEEP_DAYS = 7
OPEN = "open"
DELIVERING = "delivering"
FINAL = ("done", "cant", "expired", "answered")
EXIT_CODES = {"done": 0, "cant": 2, "expired": 3, "answered": 4}
ANSWER_CAP = 20  # буквы капчи: короткие и без пробелов, иначе это не ответ, а речь
IMAGE_CAP = 10 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
REASON_CAP = 200
STATUS_LINES = {
    "open": "",
    "done": N_("✅ пройдено, агент продолжает"),
    "cant": N_("🙅 не получилось, агент пропустит"),
    "expired": N_("⌛ экран закрыт по времени"),
    "answered": N_("✍️ ответ передан агенту"),
}


def handoff_dir(state_dir: Path) -> Path:
    return state_dir / HANDOFF_DIRNAME


def record_path(base: Path, hid: str) -> Path:
    if not _ID_RE.fullmatch(hid or ""):
        raise ValueError("bad handoff id")
    return base / f"{hid}.json"


def load(base: Path, hid: str) -> dict[str, Any] | None:
    try:
        return json.loads(record_path(base, hid).read_text(encoding="utf-8"))
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


@contextmanager
def locked(base: Path):
    """Общая блокировка записей передачи между процессами."""
    base.mkdir(parents=True, exist_ok=True)
    with open(base / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def finish(base: Path, hid: str, status: str, by: str, *, require_live: bool = True
           ) -> dict[str, Any] | None:
    """Записать итог, если его ещё нет. require_live: срок не вышел и запись
    открыта (кнопки, страница); без него закрывается и просроченная (wait).
    None: итог уже записан кем-то другим или записи нет."""
    with locked(base):
        rec = load(base, hid)
        if rec is None or rec.get("status") in FINAL:
            return None
        if require_live and not is_live(rec):
            return None
        set_status(rec, status, by)
        save(base, rec)
        return rec


def find_by_message(base: Path, chat_id: int, message_id: int) -> str | None:
    """Передача с фото капчи, на сообщение которой ответили."""
    for path in base.glob("*.json"):
        rec = load(base, path.stem)
        if (rec and rec.get("photo") and rec.get("chat_id") == chat_id
                and rec.get("message_id") == message_id):
            return rec["id"]
    return None


def looks_like_answer(text: str) -> bool:
    """Ответ на капчу: одно короткое слово. «Не вижу буквы, открой крупнее»
    это обычное сообщение, его в запись не класть (ревью Codex 21.10.2026)."""
    text = (text or "").strip()
    return 0 < len(text) <= ANSWER_CAP and not any(ch.isspace() for ch in text)


# Короткие слова-реплики: без ответа на фото это речь, а не буквы капчи.
NOT_ANSWERS = {"да", "нет", "ок", "ok", "окей", "ага", "угу", "хорошо", "спасибо", "готово",
               "стоп", "отмена", "привет", "делай", "ищи", "/cancel", "/go"}


def find_live_photo(base: Path, chat_id: int) -> str | None:
    """Единственная живая передача с фото в чате: буквы, присланные не ответом,
    а следующим сообщением (живой прогон 21.10.2026), относятся к ней."""
    found = [rec["id"] for path in base.glob("*.json")
             if (rec := load(base, path.stem)) and rec.get("photo") and rec.get("chat_id") == chat_id
             and is_live(rec)]
    return found[0] if len(found) == 1 else None


def answer(base: Path, hid: str, text: str) -> dict[str, Any] | None:
    """Ответ буквами на фото капчи: только у живой передачи с фото."""
    text = (text or "").strip()
    if not looks_like_answer(text):
        return None
    with locked(base):
        rec = load(base, hid)
        if rec is None or not rec.get("photo") or not is_live(rec) or not text:
            return None
        set_status(rec, "answered", "reply")
        rec["answer"] = text
        save(base, rec)
        return rec


def scrub(base: Path, now: float | None = None) -> None:
    """Стереть ответы, которые никто не забрал: wait не дошёл или ран оборвался."""
    now = now or time.time()
    with locked(base):
        for path in base.glob("*.json"):
            rec = load(base, path.stem)
            if rec and "answer" in rec and now >= float(rec.get("expires", 0)):
                rec.pop("answer")
                save(base, rec)


def take_answer(base: Path, hid: str) -> str:
    """Отдать ответ агенту один раз и стереть его из записи."""
    with locked(base):
        rec = load(base, hid)
        if rec is None:
            return ""
        text = rec.pop("answer", "")
        save(base, rec)
        return text


def mark_notified(base: Path, hid: str) -> None:
    with locked(base):
        rec = load(base, hid)
        if rec is not None:
            rec["notified"] = True
            save(base, rec)


def token_sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_ok(rec: dict[str, Any], token: str) -> bool:
    return bool(token) and hmac.compare_digest(token_sha(token), rec.get("token_sha", ""))


def is_live(rec: dict[str, Any], now: float | None = None) -> bool:
    """Экран можно смотреть и решать: запись открыта и срок не вышел."""
    return rec.get("status") == OPEN and (now or time.time()) < float(rec.get("expires", 0))


def parse_chat_key(key: str) -> tuple[int, int | None] | None:
    """«chat_id» или «chat_id:thread_id», как ключ чата в bot.py."""
    m = re.fullmatch(r"(-?\d+)(?::(\d+))?", (key or "").strip())
    if not m:
        return None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def new_record(reason: str, site: str, chat_key: str, session: str, stream_port: int,
               now: float | None = None) -> tuple[dict[str, Any], str]:
    """Запись и токен; токен в запись не кладётся, только его хеш."""
    now = now or time.time()
    token = secrets.token_urlsafe(32)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    rec = {
        "id": uuid.uuid4().hex[:12], "reason": reason.strip()[:REASON_CAP],
        "site": site.strip()[:REASON_CAP], "chat_key": chat_key, "session": session,
        "stream_port": int(stream_port), "token_sha": token_sha(token), "status": DELIVERING,
        "created_ts": now, "expires": now + TTL_SECONDS, "chat_id": None, "message_id": None,
        "created": stamp, "updated": stamp,
    }
    return rec, token


def set_status(rec: dict[str, Any], status: str, by: str) -> None:
    rec["status"] = status
    rec["closed_by"] = by
    rec["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")


def recent_count(base: Path, chat_key: str, now: float | None = None) -> int:
    """Передачи в этот чат за последний час, включая неудачные отправки."""
    now = now or time.time()
    n = 0
    for path in base.glob("*.json"):
        rec = load(base, path.stem)
        if rec and rec.get("chat_key") == chat_key and now - float(rec.get("created_ts", 0)) < RATE_WINDOW:
            n += 1
    return n


def session_busy(base: Path, session: str, now: float | None = None) -> bool:
    """У сессии уже есть незакрытая передача: вторая управляла бы той же вкладкой."""
    now = now or time.time()
    for path in base.glob("*.json"):
        rec = load(base, path.stem)
        if (rec and rec.get("session") == session and rec.get("status") in (DELIVERING, OPEN)
                and now < float(rec.get("expires", 0))):
            return True
    return False


def prune(base: Path, now: float | None = None) -> None:
    now = now or time.time()
    for path in base.glob("*.json"):
        rec = load(base, path.stem)
        if rec and now - float(rec.get("created_ts", now)) > KEEP_DAYS * 86400:
            path.unlink(missing_ok=True)


def parse_region(raw: str) -> list[int] | None:
    """«x,y,ширина,высота» в точках вкладки: что показать крупно на телефоне."""
    try:
        parts = [int(round(float(v))) for v in (raw or "").split(",")]
    except ValueError:
        return None
    if len(parts) != 4 or min(parts) < 0 or max(parts) > 20000 or parts[2] < 20 or parts[3] < 20:
        return None
    return parts


def crop_image(data: bytes, region: list[int], pad: int = 16) -> bytes:
    """Вырезать область капчи из снимка окна (ffmpeg: Pillow на ПК нет);
    не вышло, отдать снимок целиком. Снимать всё окно (`agent-browser
    screenshot <файл>`), не элемент: снимок элемента на фоновой вкладке не
    отвечал и оставлял вкладку со сдвинутой отрисовкой (21.10.2026), а кадр
    трансляции на неподвижной странице бывает устаревшим."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return data
    x, y = max(0, region[0] - pad), max(0, region[1] - pad)
    w, h = region[2] + 2 * pad, region[3] + 2 * pad
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "in.png", Path(d) / "out.png"
        src.write_bytes(data)
        try:
            res = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(src), "-vf",
                                  f"crop=min({w}\\,iw-{x}):min({h}\\,ih-{y}):{x}:{y}", str(dst)],
                                 capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return data
        return dst.read_bytes() if res.returncode == 0 and dst.is_file() else data


def parse_callback(data: str) -> tuple[str, str] | None:
    m = _CALLBACK_RE.fullmatch(data or "")
    return (m.group(1), m.group(2)) if m else None


def screen_url(public_base: str, hid: str, token: str) -> str:
    return f"{public_base.rstrip('/')}/s/{hid}#{token}"


def buttons(rec: dict[str, Any], url: str = "") -> list[list[dict[str, str]]]:
    """Кнопки только у открытой передачи. url пуст: без ссылки (ссылку знает
    только ask, токена в записи нет)."""
    if rec.get("status") != OPEN:
        return []
    rows: list[list[dict[str, str]]] = []
    if url:
        rows.append([{"text": _("🖥 Открыть экран"), "url": url}])
    head = f"{CALLBACK_PREFIX}:{rec['id']}"
    rows.append([{"text": _("✅ Готово"), "data": f"{head}:d"},
                 {"text": _("🙅 Не могу"), "data": f"{head}:c"}])
    return rows


def render(rec: dict[str, Any]) -> str:
    until = datetime.fromtimestamp(float(rec["expires"])).astimezone().strftime("%H:%M")
    parts = [_("🖥 Нужна твоя помощь в браузере: {reason}").format(reason=rec["reason"])]
    if rec.get("site"):
        parts.append(_("Сайт: {site}").format(site=rec["site"]))
    parts += ["", _("Открой экран, пройди сам и нажми «Готово». Экран живёт до {until}.").format(
        until=until),
              _("Не открывается: попробуй через v2rayNG или Tailscale.")]
    if rec.get("photo"):
        parts.append(_("Капча с буквами: можно просто ответить на это сообщение буквами с картинки."))
    line = _(STATUS_LINES.get(rec.get("status", ""), ""))
    if line:
        parts += ["", line]
    return "\n".join(parts)


# ---- agent-browser ----

def _agent_browser() -> str:
    found = shutil.which("agent-browser")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "agent-browser"
    return str(fallback)


def _ab(session: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, **{SESSION_ENV: session})
    return subprocess.run([_agent_browser(), *args], env=env, capture_output=True, text=True,
                          timeout=30)


# Кадр трансляции не шире 1280x720: в большом окне (1920x1031 у развёрнутого
# «Агента») он приходит уменьшенным, а касания страница считает по кадру и
# промахивалась мимо полей (вход AliExpress, 21.10.2026). Вкладка размера
# кадра даёт один к одному.
STREAM_VIEWPORT = ("1280", "720")


def stream_port(session: str) -> int | None:
    """Порт трансляции сессии: демон держит её включённой на случайном порту;
    выключена (после прошлой передачи) включаем заново. Вкладка сначала
    получает размер кадра."""
    try:
        _ab(session, "set", "viewport", *STREAM_VIEWPORT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for attempt in range(2):
        try:
            res = _ab(session, "stream", "status", "--json")
            data = json.loads(res.stdout or "{}").get("data") or {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
        if not data.get("connected"):
            return None
        if data.get("enabled") and data.get("port"):
            return int(data["port"])
        if attempt == 0:
            try:
                _ab(session, "stream", "enable")
            except (OSError, subprocess.TimeoutExpired):
                return None
    return None


def stream_disable(session: str) -> None:
    try:
        ok = _ab(session, "stream", "disable").returncode == 0
        if not ok:
            # Уже выключена (прошлый wait): не ошибка, но только при явном
            # enabled=false; непрочитанное состояние остаётся ошибкой.
            data = json.loads(_ab(session, "stream", "status", "--json").stdout or "{}").get("data") or {}
            ok = data.get("enabled") is False
    except (OSError, ValueError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        print(_("handoff: трансляцию выключить не удалось"), file=sys.stderr)


# ---- CLI для агента ----

def _bot_api(method: str, payload: dict[str, Any],
             photo: tuple[str, bytes, str] | None = None) -> dict[str, Any] | None:
    import httpx

    from .config import load_config
    cfg = load_config(skip_hook_validation=True)
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/{method}"
    try:
        if photo is None:
            r = httpx.post(url, json=payload, timeout=10.0)
        else:
            form = {k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items()}
            r = httpx.post(url, data=form, files={"photo": photo}, timeout=30.0)
    except httpx.HTTPError as e:
        print(f"handoff: HTTP error: {type(e).__name__}", file=sys.stderr)
        return None
    if r.status_code != 200:
        # Без URL: в нём токен бота.
        print(f"handoff: {method} failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()


def markup(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": b["text"], **({"url": b["url"]} if "url" in b
                                                       else {"callback_data": b["data"]})}
                                 for b in row] for row in rows]}


def _cmd_ask(args: argparse.Namespace, base: Path, primary_chat_id: int) -> int:
    session = os.environ.get(SESSION_ENV, "").strip()
    if not session:
        print(_("handoff: нет AGENT_BROWSER_SESSION: передавать можно только вкладку своей сессии"),
              file=sys.stderr)
        return 1
    public_base = os.environ.get("SCREEN_PUBLIC_BASE", "").strip()
    if not public_base.startswith(("http://", "https://")):
        print(_("handoff: SCREEN_PUBLIC_BASE в .env бота не задан (https://узел)"), file=sys.stderr)
        return 1
    # Ран мастерской показывает передачу экрана в личке (CLAUDE_TG_SHOW_CHAT_KEY).
    chat_key = (os.environ.get(SHOW_CHAT_KEY_ENV, "").strip() or os.environ.get(CHAT_KEY_ENV, "").strip()
                or str(primary_chat_id))
    target = parse_chat_key(chat_key)
    if target is None:
        print(_("handoff: непонятный CLAUDE_TG_CHAT_KEY"), file=sys.stderr)
        return 1
    region = None
    if args.region:
        region = parse_region(args.region)
        if region is None:
            print(_("handoff: --region нужен как x,y,ширина,высота в точках вкладки"), file=sys.stderr)
            return 1
    photo = None
    if args.image:
        img = Path(args.image)
        kind = IMAGE_TYPES.get(img.suffix.lower())
        try:
            data = img.read_bytes() if kind else b""
        except OSError:
            data = b""
        if not data or len(data) > IMAGE_CAP:
            print(_("handoff: картинка капчи не читается (нужен PNG или JPEG до 10 МБ)"), file=sys.stderr)
            return 1
        if region:
            data = crop_image(data, region)
            img, kind = img.with_suffix(".png"), "image/png"
        photo = (img.name, data, kind)
    port = stream_port(session)
    if port is None:
        print(_("handoff: у сессии нет открытого браузера или трансляции, сначала открой страницу"),
              file=sys.stderr)
        return 1
    with locked(base):
        prune(base)
        if recent_count(base, chat_key) >= RATE_LIMIT:
            print(_("handoff: за час уже {n} передачи в этот чат, больше нельзя: остановись и скажи Ивану").format(
                n=RATE_LIMIT), file=sys.stderr)
            return 1
        if session_busy(base, session):
            print(_("handoff: у этой сессии уже открыта передача, дождись её итога"), file=sys.stderr)
            return 1
        rec, token = new_record(args.reason, args.site, chat_key, session, port)
        rec["photo"] = photo is not None
        if region:
            rec["region"] = region
        # Как в otklik: сначала сообщение без кнопок и запись с его номером,
        # кнопки последними, иначе быстрый тап застал бы запись без message_id.
        save(base, rec)
    chat_id, thread_id = target
    body = render(dict(rec, status=OPEN))
    payload: dict[str, Any] = {"chat_id": chat_id}
    if photo is None:
        payload.update(text=body, link_preview_options={"is_disabled": True})
    else:
        payload["caption"] = body
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if args.quiet:
        payload["disable_notification"] = True
    res = _bot_api("sendPhoto" if photo else "sendMessage", payload, photo)
    if res is None:
        finish(base, rec["id"], "expired", "send-failed", require_live=False)
        return 1
    with locked(base):
        rec["chat_id"] = chat_id
        rec["message_id"] = int(res["result"]["message_id"])
        rec["status"] = OPEN
        save(base, rec)
    url = screen_url(public_base, rec["id"], token)
    if _bot_api("editMessageReplyMarkup", {
        "chat_id": chat_id, "message_id": rec["message_id"], "reply_markup": markup(buttons(rec, url)),
    }) is None:
        finish(base, rec["id"], "expired", "send-failed", require_live=False)
        print(_("handoff {id}: сообщение ушло, кнопки не прикрепились").format(id=rec["id"]),
              file=sys.stderr)
        return 1
    print(f"handoff {rec['id']} sent, wait: .venv/bin/python -m claude_tg_bot.handoff wait "
          f"--id {rec['id']} --timeout {DEFAULT_WAIT}")
    return 0


def wait_status(base: Path, hid: str, timeout: float, poll: float = 1.0) -> str:
    """Ждать итога; по сроку или таймауту закрыть передачу как expired."""
    deadline = time.time() + timeout
    while True:
        rec = load(base, hid)
        if rec is None:
            return "expired"
        status = rec.get("status")
        if status in FINAL:
            return status
        now = time.time()
        if now >= float(rec.get("expires", 0)) or now >= deadline:
            # Под блокировкой: нажатие, пришедшее в эту секунду, не затирается.
            if finish(base, hid, "expired", "timeout", require_live=False) is not None:
                return "expired"
            rec = load(base, hid) or {}
            return rec["status"] if rec.get("status") in FINAL else "expired"
        time.sleep(poll)


def _cmd_wait(args: argparse.Namespace, base: Path) -> int:
    rec = load(base, args.id)
    if rec is None:
        print(_("handoff: нет такой записи"), file=sys.stderr)
        return 1
    try:
        status = wait_status(base, args.id, max(1.0, min(args.timeout, TTL_SECONDS)))
    finally:
        # Трансляция выключается до возврата: ввод с телефона больше не дойдёт
        # до вкладки, когда агент продолжит.
        rec = load(base, args.id) or rec
        if rec.get("session"):
            stream_disable(rec["session"])
    if not rec.get("notified") and rec.get("message_id") and rec.get("chat_id") is not None:
        target = {"chat_id": rec["chat_id"], "message_id": rec["message_id"]}
        if rec.get("photo"):
            ok = _bot_api("editMessageCaption", {**target, "caption": render(rec)})
        else:
            ok = _bot_api("editMessageText", {**target, "text": render(rec),
                                              "link_preview_options": {"is_disabled": True}})
        if ok is not None:
            mark_notified(base, args.id)
    if status == "answered":
        print(f"answer: {take_answer(base, args.id)}")
    print(f"handoff {args.id}: {status}")
    return EXIT_CODES[status]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_tg_bot.handoff")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ask = sub.add_parser("ask", help="передать экран Ивану: сообщение с кнопками")
    p_ask.add_argument("--reason", required=True, help="что случилось, одной строкой")
    p_ask.add_argument("--site", default="")
    p_ask.add_argument("--quiet", action="store_true", help="без звука (ночной ран)")
    p_ask.add_argument("--image", default="", help="картинка капчи с буквами: можно ответить текстом")
    p_ask.add_argument("--region", default="", help="x,y,ширина,высота капчи: на телефоне крупно только она")
    p_wait = sub.add_parser("wait", help="ждать итога: 0 готово, 2 не может, 3 время вышло, 4 ответ")
    p_wait.add_argument("--id", required=True)
    p_wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    from .config import load_config
    cfg = load_config(skip_hook_validation=True)
    base = handoff_dir(cfg.state_dir)
    if args.cmd == "ask":
        return _cmd_ask(args, base, cfg.primary_chat_id)
    return _cmd_wait(args, base)


if __name__ == "__main__":
    sys.exit(main())

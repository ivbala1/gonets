"""Служба экрана: живой экран вкладки агента на телефон Ивана.

Отдельный процесс (юнит claude-tg-screen.service), чтобы рестарт бота не рвал
экран. Слушает только 127.0.0.1; наружу её отдаёт Tailscale Funnel
(`tailscale funnel --bg 8765`): HTTPS с сертификатом ts.net, TLS кончается
на этом ПК, телефону VPN не нужен (решение Ивана 20.10.2026). Адрес публичный
и постоянный, поэтому страница и WebSocket отвечают только на живую
передачу, остальное 404. Страница /s/<id> берёт токен из «#» ссылки и шлёт
его первым сообщением WebSocket /ws/<id>; служба сверяет хеш с записью
handoff.py и проксирует трансляцию agent-browser (ws://127.0.0.1:<порт>).
С телефона к браузеру идут только мышь, клавиши и ack, без модификаторов,
кроме Shift; кадры и ввод нигде не пишутся, в журнал только id и счётчики.

    .venv/bin/python -m claude_tg_bot.screen_server
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, WSMsgType, web

from . import handoff

log = logging.getLogger("claude_tg_bot.screen")

DEFAULT_PORT = 8765
AUTH_TIMEOUT = 10
CHECK_EVERY = 0.5
MAX_BAD_TOKENS = 10  # неверных токенов на передачу, дальше она не открывается
# Адрес публичный (ревью Codex 20.10.2026, второй круг): общий предел запросов
# до чтения записи, предел ждущих токена соединений и сообщений с телефона.
RATE_WINDOW = 10.0
RATE_MAX = 60
MAX_PENDING = 8
MSG_RATE_MAX = 400  # сообщений с телефона за секунду; протяжка шлёт около 30
MAX_FPS = 8
MAX_MSG = 4096
SHIFT = 8  # бит Shift в modifiers CDP; Alt 1, Ctrl 2, Meta 4 обнуляются
MOUSE_EVENTS = {"mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"}
MOUSE_BUTTONS = {"none", "left", "middle", "right"}
KEY_EVENTS = {"keyDown", "keyUp"}
# Служебные поля клавиш считает служба, с телефона берётся только имя
# (ревью Codex 20.10.2026: произвольный код клавиши при разрешённом имени).
NAMED_KEYS = {
    "Backspace": {"code": "Backspace", "windowsVirtualKeyCode": 8},
    "Enter": {"code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "Tab": {"code": "Tab", "windowsVirtualKeyCode": 9},
    "Escape": {"code": "Escape", "windowsVirtualKeyCode": 27},
    "Delete": {"code": "Delete", "windowsVirtualKeyCode": 46},
    "ArrowLeft": {"code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "ArrowUp": {"code": "ArrowUp", "windowsVirtualKeyCode": 38},
    "ArrowRight": {"code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "ArrowDown": {"code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "Home": {"code": "Home", "windowsVirtualKeyCode": 36},
    "End": {"code": "End", "windowsVirtualKeyCode": 35},
}
COORD_CAP = 20000
WHEEL_CAP = 5000
# Куда на сайте ушёл фокус после касания: строка ввода на телефоне сама встаёт
# в фокус, для пароля прячет буквы (просьба Ивана 22.10.2026, обсуждено с
# Codex). Скрипт постоянный, наружу только слово из FOCUS_KINDS, без value
# и подписей; не больше одной проверки разом, около двух в секунду.
FOCUS_KINDS = {"text", "password", "email", "tel", "number", "search", "url", "none", "unknown"}
FOCUS_JS = (
    "(()=>{let e=document.activeElement,d=0;while(e&&d<5){"
    "if(e.shadowRoot&&e.shadowRoot.activeElement){e=e.shadowRoot.activeElement;d++;continue}"
    "if(e.tagName==='IFRAME'||e.tagName==='FRAME'){try{const x=e.contentDocument;"
    "if(!x)return 'unknown';e=x.activeElement;d++;continue}catch(_){return 'unknown'}}break}"
    "if(!e||e===document.body||e===document.documentElement)return 'none';"
    "if(e.disabled||e.readOnly)return 'none';"
    "if(e.tagName==='TEXTAREA'||e.isContentEditable)return 'text';"
    "if(e.tagName==='INPUT'){const t=(e.type||'text').toLowerCase();"
    "if(t==='password')return 'password';"
    "if(['email','tel','number','search','url'].includes(t))return t;"
    "if(t==='text')return 'text'}return 'none'})()"
)
PROBE_DELAY = 0.3  # клик уходит без ожидания ответа Chrome: дать фокусу смениться
PROBE_GAP = 0.2
PROBE_TIMEOUT = 5.0


async def focus_kind(session: str) -> str:
    """Вид поля в фокусе вкладки сессии; не узнали, значит unknown."""
    if not session:
        return "unknown"
    try:
        proc = await asyncio.create_subprocess_exec(
            handoff._agent_browser(), "eval", FOCUS_JS,
            env=dict(os.environ, **{handoff.SESSION_ENV: session}),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return "unknown"
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            out, _err = await proc.communicate()
    except TimeoutError:
        return "unknown"
    finally:
        # И по таймауту, и при отмене (экран закрыт): процесс не бросаем.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    try:
        kind = json.loads(out.decode("utf-8", "replace").strip() or '""')
    except ValueError:
        return "unknown"
    return kind if isinstance(kind, str) and kind in FOCUS_KINDS else "unknown"

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("default-src 'none'; script-src 'unsafe-inline'; "
                                "style-src 'unsafe-inline'; img-src blob: data:; connect-src 'self'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
}


def state_dir() -> Path:
    default = Path(__file__).resolve().parents[2] / "state"
    return Path(os.environ.get("CLAUDE_TG_BOT_STATE_DIR", str(default))).expanduser().resolve()


def bind_host() -> str:
    """Только адрес петли: наружу служба выходит лишь через Funnel."""
    host = os.environ.get("SCREEN_BIND_HOST", "").strip() or "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise RuntimeError(f"SCREEN_BIND_HOST={host!r} не адрес петли, не стартую")
    return host


def _num(v: Any, cap: float) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return max(-cap, min(cap, float(v)))


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def sanitize(msg: Any) -> dict[str, Any] | None:
    """Сообщение с телефона в трансляцию: только мышь, клавиши и ack, по белому
    списку полей. Всё остальное (config, touch, неизвестное) отбрасывается."""
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    if kind == "ack":
        seq = _int(msg.get("seq"))
        return {"type": "ack", "seq": seq} if seq is not None else None
    mods = _int(msg.get("modifiers", 0))
    mods = mods & SHIFT if mods is not None else 0
    if kind == "input_mouse":
        ev = msg.get("eventType")
        x, y = _num(msg.get("x"), COORD_CAP), _num(msg.get("y"), COORD_CAP)
        if ev not in MOUSE_EVENTS or x is None or y is None or x < 0 or y < 0:
            return None
        out: dict[str, Any] = {"type": kind, "eventType": ev, "x": x, "y": y, "modifiers": mods}
        button = msg.get("button", "none")
        out["button"] = button if button in MOUSE_BUTTONS else "none"
        clicks = _int(msg.get("clickCount", 0))
        out["clickCount"] = clicks if clicks is not None and 0 <= clicks <= 3 else 0
        buttons = _int(msg.get("buttons"))
        if buttons is not None and 0 <= buttons <= 7:
            out["buttons"] = buttons
        if ev == "mouseWheel":
            out["deltaX"] = _num(msg.get("deltaX", 0), WHEEL_CAP) or 0.0
            out["deltaY"] = _num(msg.get("deltaY", 0), WHEEL_CAP) or 0.0
        return out
    if kind == "input_keyboard":
        ev, key = msg.get("eventType"), msg.get("key")
        if ev not in KEY_EVENTS or not isinstance(key, str):
            return None
        out = {"type": kind, "eventType": ev, "key": key, "modifiers": mods}
        if key in NAMED_KEYS:
            extra = dict(NAMED_KEYS[key])
            if ev != "keyDown":
                extra.pop("text", None)
            out.update(extra)
        elif len(key) == 1 and key.isprintable():
            if ev == "keyDown":
                out["text"] = key
        else:
            return None
        return out
    return None


def from_browser(raw: str) -> str | None:
    """Из трансляции к телефону только кадры (без tabs, url, console)."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    if isinstance(msg, dict) and msg.get("type") == "frame":
        return raw
    return None


class Screen:
    def __init__(self, base: Path) -> None:
        self.base = base
        # id -> текущее соединение телефона: новое вытесняет старое.
        self.active: dict[str, web.WebSocketResponse] = {}
        self.bad_tokens: dict[str, int] = {}
        self.hits: list[float] = []
        self.pending = 0

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        self.hits = [t for t in self.hits if now - t < RATE_WINDOW]
        if len(self.hits) >= RATE_MAX:
            return False
        self.hits.append(now)
        return True

    def _blocked(self, hid: str) -> bool:
        return self.bad_tokens.get(hid, 0) >= MAX_BAD_TOKENS

    def _live(self, request: web.Request) -> str:
        """Только живая передача, иначе 404: адрес публичный и постоянный."""
        if not self._rate_ok():
            raise web.HTTPTooManyRequests()
        hid = request.match_info["hid"]
        rec = handoff.load(self.base, hid) if handoff._ID_RE.fullmatch(hid) else None
        if rec is None or not handoff.is_live(rec) or self._blocked(hid):
            raise web.HTTPNotFound()
        if len(self.bad_tokens) > 1000:
            self.bad_tokens.clear()
        return hid

    async def page(self, request: web.Request) -> web.Response:
        self._live(request)
        return web.Response(text=PAGE, content_type="text/html", charset="utf-8", headers=HEADERS)

    async def ws(self, request: web.Request) -> web.StreamResponse:
        hid = self._live(request)
        if self.pending >= MAX_PENDING:
            raise web.HTTPServiceUnavailable()
        phone = web.WebSocketResponse(max_msg_size=MAX_MSG, heartbeat=20, compress=False)
        self.pending += 1
        try:
            await phone.prepare(request)
            # Общий срок на токен: receive(timeout) заново отсчитывает время
            # после каждого служебного PING.
            async with asyncio.timeout(AUTH_TIMEOUT):
                first = await phone.receive()
            token = json.loads(first.data).get("token", "") if first.type == WSMsgType.TEXT else ""
        except (TimeoutError, ValueError, AttributeError):
            token = ""
        finally:
            self.pending -= 1
        rec = handoff.load(self.base, hid)
        if self._blocked(hid) or rec is None or not handoff.token_ok(rec, str(token)):
            self.bad_tokens[hid] = self.bad_tokens.get(hid, 0) + 1
            log.warning("screen %s: bad token (%d)", hid, self.bad_tokens[hid])
            await self._close(phone, "denied")
            return phone
        if not handoff.is_live(rec):
            await self._close(phone, rec.get("status") or "expired")
            return phone
        # Занять место до первого await: два одновременных входа не проксируют
        # оба (ревью Codex 20.10.2026).
        old = self.active.get(hid)
        self.active[hid] = phone
        if old is not None and not old.closed:
            await self._close(old, "replaced")
        log.info("screen %s: open", hid)
        frames = 0
        reason = "closed"
        try:
            await phone.send_json({"type": "info", "left": max(0, int(rec["expires"] - time.time())),
                                   "reason": rec.get("reason", ""), "site": rec.get("site", ""),
                                   "region": rec.get("region")})
            url = f"ws://127.0.0.1:{int(rec['stream_port'])}/?pacing=ack&maxFps={MAX_FPS}"
            async with ClientSession() as http:
                async with http.ws_connect(url, headers={"Origin": "http://127.0.0.1"},
                                           max_msg_size=0) as browser:
                    reason, frames = await self._pump(hid, phone, browser)
        except Exception as e:  # noqa: BLE001: причина в журнал без содержимого
            log.warning("screen %s: stream failed: %s", hid, type(e).__name__)
            reason = "stream"
        finally:
            if self.active.get(hid) is phone:
                self.active.pop(hid, None)
            await self._close(phone, reason)
            log.info("screen %s: closed (%s), frames %d", hid, reason, frames)
        return phone

    async def _pump(self, hid: str, phone: web.WebSocketResponse, browser) -> tuple[str, int]:
        frames = 0
        result = "closed"
        rec = handoff.load(self.base, hid)
        # Перепроверка после подключения к трансляции: итог мог прийти за это время.
        alive = rec is not None and handoff.is_live(rec)
        if not alive:
            return (rec or {}).get("status") or "expired", 0
        session = str(rec.get("session") or "")
        # Последняя просьба проверить фокус: повторы сливаются в одну.
        probe_id: int | None = None
        probe_wanted = asyncio.Event()

        async def prober() -> None:
            nonlocal probe_id
            while True:
                await probe_wanted.wait()
                await asyncio.sleep(PROBE_DELAY)
                probe_wanted.clear()
                pid, probe_id = probe_id, None
                if pid is None or not alive or self.active.get(hid) is not phone:
                    continue
                # alive обновляется раз в CHECK_EVERY: итог мог прийти только что.
                fresh = handoff.load(self.base, hid)
                if fresh is None or not handoff.is_live(fresh):
                    continue
                kind = await focus_kind(session)
                if alive and self.active.get(hid) is phone:
                    try:
                        await phone.send_json({"type": "focus", "id": pid, "kind": kind})
                    except ConnectionError:
                        return
                await asyncio.sleep(PROBE_GAP)

        async def to_phone() -> None:
            nonlocal frames
            async for m in browser:
                if m.type != WSMsgType.TEXT:
                    if m.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    continue
                if not alive or self.active.get(hid) is not phone:
                    return
                out = from_browser(m.data)
                if out is not None:
                    frames += 1
                    await phone.send_str(out)

        async def to_browser() -> None:
            nonlocal result, probe_id
            second, count = int(time.monotonic()), 0
            async for m in phone:
                if m.type != WSMsgType.TEXT:
                    if m.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    continue
                # Итог записан или окно вытеснено: ввод больше не пересылается.
                if not alive or self.active.get(hid) is not phone:
                    return
                now = int(time.monotonic())
                second, count = (now, 1) if now != second else (second, count + 1)
                if count > MSG_RATE_MAX:
                    result = "flood"
                    return
                try:
                    msg = json.loads(m.data)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") in ("done", "cant"):
                    if handoff.finish(self.base, hid, msg["type"], "page") is not None:
                        result = msg["type"]
                        return
                    continue
                if isinstance(msg, dict) and msg.get("type") in ("probe", "typed"):
                    pid = _int(msg.get("id"))
                    if pid is None:
                        continue
                    if msg["type"] == "probe":
                        probe_id = pid
                        probe_wanted.set()
                    else:
                        # Всё, что было до этой метки, ушло в трансляцию:
                        # телефон может стереть строку.
                        await phone.send_json({"type": "typed", "id": pid})
                    continue
                clean = sanitize(msg)
                if clean is not None:
                    await browser.send_json(clean)

        async def watch() -> None:
            nonlocal result, alive
            while True:
                await asyncio.sleep(CHECK_EVERY)
                rec = handoff.load(self.base, hid)
                if rec is None or not handoff.is_live(rec):
                    alive = False
                    result = (rec or {}).get("status") or "expired"
                    if result == handoff.OPEN:
                        result = "expired"
                    return

        tasks = [asyncio.create_task(c()) for c in (to_phone, to_browser, watch)]
        # Проверка фокуса вспомогательная: её сбой экран не закрывает.
        helper = asyncio.create_task(prober())
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            tasks.append(helper)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return result, frames

    @staticmethod
    async def _close(ws: web.WebSocketResponse, reason: str) -> None:
        if ws.closed:
            return
        try:
            await ws.send_json({"type": "closed", "reason": reason})
        except Exception:  # noqa: BLE001
            pass
        await ws.close()


async def _headers(request: web.Request, response: web.StreamResponse) -> None:
    """Служебные заголовки на всех ответах, включая 404, без версий в Server."""
    response.headers.pop("Server", None)
    for name, value in HEADERS.items():
        response.headers.setdefault(name, value)


async def _scrub_loop(base: Path) -> None:
    """Раз в минуту стереть незабранные ответы буквами у просроченных передач."""
    while True:
        try:
            handoff.scrub(base)
        except OSError as e:
            log.warning("scrub failed: %s", type(e).__name__)
        await asyncio.sleep(60)


def make_app(base: Path) -> web.Application:
    screen = Screen(base)
    app = web.Application(client_max_size=MAX_MSG)
    app.on_response_prepare.append(_headers)

    async def scrubber(app: web.Application):
        task = asyncio.create_task(_scrub_loop(base))
        yield
        task.cancel()
    app.cleanup_ctx.append(scrubber)
    app.router.add_get(r"/s/{hid}", screen.page)
    app.router.add_get(r"/ws/{hid}", screen.ws)
    return app


def main() -> int:
    from dotenv import load_dotenv

    from .config import BOT_ENV_FILE
    load_dotenv(BOT_ENV_FILE if BOT_ENV_FILE.is_file() else None)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Журнал доступа aiohttp не нужен: путь с id и так в наших строках.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    try:
        host = bind_host()
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    port = int(os.environ.get("SCREEN_PORT", DEFAULT_PORT))
    log.info("listening on %s:%d", host, port)
    web.run_app(make_app(handoff.handoff_dir(state_dir())), host=host, port=port,
                access_log=None, print=None)
    return 0


PAGE = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Экран агента</title>
<style>
:root { color-scheme: light dark; --bg:#f4f4f2; --fg:#1d1d1b; --bar:#ffffff; --muted:#6b6b66;
  --ok:#1f7a3a; --no:#a33a2a; --btn:#e7e7e3; --on:#2d5fa8; }
@media (prefers-color-scheme: dark) { :root { --bg:#151514; --fg:#ecece8; --bar:#22221f;
  --muted:#9a9a93; --ok:#3fa45f; --no:#d0604d; --btn:#33332f; --on:#4f82d0; } }
* { box-sizing: border-box; }
html, body { margin:0; background:var(--bg); color:var(--fg);
  font: 15px/1.35 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
#top { position:sticky; top:0; z-index:2; background:var(--bar); border-bottom:1px solid var(--btn); }
header { padding:8px 12px; display:flex; gap:8px; align-items:center; }
#kbbar { display:flex; gap:6px; padding:0 12px 8px; }
#kbt { flex:1; min-width:0; font:inherit; padding:10px; border-radius:10px;
  border:1px solid var(--muted); background:var(--bg); color:var(--fg); }
header .why { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#left { font-variant-numeric: tabular-nums; color:var(--muted); }
#wrap { overflow:auto; -webkit-overflow-scrolling:touch; position:relative; }
.dot { position:absolute; width:22px; height:22px; margin:-11px 0 0 -11px; border-radius:50%;
  border:3px solid #e8453c; pointer-events:none; transition:opacity .8s; }
#cv { display:block; width:100%; touch-action:none; background:#000; }
#wrap.zoom #cv { touch-action:pan-x pan-y; }
#wrap.drag #cv { touch-action:none; }
#msg { padding:24px 16px; text-align:center; color:var(--muted); }
.tools { position:sticky; bottom:0; z-index:2; background:var(--bar); padding:8px 12px;
  display:flex; flex-wrap:wrap; gap:6px; border-top:1px solid var(--btn); }
button { font:inherit; border:0; border-radius:10px; padding:10px 12px; background:var(--btn);
  color:var(--fg); min-width:44px; }
button.on { background:var(--on); color:#fff; }
button.ok { background:var(--ok); color:#fff; flex:1; }
button.no { background:var(--no); color:#fff; flex:1; }
.row { display:flex; gap:6px; width:100%; }
</style></head>
<body>
<div id="top">
<header><span class="why" id="why">Экран агента</span><span id="left"></span></header>
<div id="kbbar"><input id="kbt" type="text" placeholder="Коснись поля на сайте" autocomplete="off"
  autocapitalize="off" spellcheck="false"><button id="bins">Вставить</button></div>
</div>
<div id="wrap"><canvas id="cv" width="10" height="10"></canvas></div>
<div id="msg">Подключаюсь…</div>
<div class="tools">
  <div class="row">
    <button id="btab" title="Следующее поле">⇥</button>
    <button id="bbs" title="Стереть">⌫</button>
    <button id="ben" title="Отправить: Enter на сайте">⏎</button>
    <button id="bup" title="Прокрутить вверх">▲</button>
    <button id="bdn" title="Прокрутить вниз">▼</button>
    <button id="bdr" title="Тянуть (ползунок)">✋</button>
    <button id="bzm" title="Масштаб">1x</button>
    <button id="bfull" title="Весь экран или только капча" style="display:none">⛶</button>
  </div>
  <div class="row">
    <button class="ok" id="bdone">✅ Готово</button>
    <button class="no" id="bcant">🙅 Не могу</button>
  </div>
</div>
<script>
(() => {
  const hid = location.pathname.split("/").pop();
  const key = "ho-" + hid;
  let token = location.hash.slice(1);
  try {
    if (token) sessionStorage.setItem(key, token); else token = sessionStorage.getItem(key) || "";
  } catch (e) {}
  // Токен из адресной строки убрать: не попадёт в скриншот и историю.
  if (location.hash) history.replaceState(null, "", location.pathname);

  const cv = document.getElementById("cv"), ctx = cv.getContext("2d");
  const wrap = document.getElementById("wrap"), msgEl = document.getElementById("msg");
  const leftEl = document.getElementById("left"), kb = document.getElementById("kbt");
  let ws = null, finished = false, deadline = 0, imgW = 0, imgH = 0, zoom = 1, drag = false;
  // Область капчи от агента (--region): показываем только её с запасом, ⛶ весь экран.
  let region = null, full = false, crop = { x: 0, y: 0, w: 0, h: 0 };
  const PAD = 24;
  function cropFor(w, h) {
    if (!region || full) return { x: 0, y: 0, w, h };
    const x = Math.max(0, region[0] - PAD), y = Math.max(0, region[1] - PAD);
    const cw = Math.min(w - x, region[2] + 2 * PAD), ch = Math.min(h - y, region[3] + 2 * PAD);
    return cw > 20 && ch > 20 ? { x, y, w: cw, h: ch } : { x: 0, y: 0, w, h };
  }

  function say(t) { msgEl.textContent = t; msgEl.style.display = t ? "block" : "none"; }
  function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

  function tick() {
    if (!deadline || finished) return;
    const s = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    leftEl.textContent = Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  setInterval(tick, 1000);

  const REASONS = { done: "Готово. Агент продолжает, страницу можно закрыть.",
    cant: "Отмечено «Не могу». Агент пропустит, страницу можно закрыть.",
    expired: "Время вышло, экран закрыт.", denied: "Ссылка недействительна.",
    replaced: "Экран открыт в другом окне.", stream: "Трансляция оборвалась: агент закрыл вкладку?" };

  function connect() {
    if (!token) { say("В ссылке нет ключа. Открой экран кнопкой из Telegram."); return; }
    say("Подключаюсь…");
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/" + hid);
    ws.onopen = () => ws.send(JSON.stringify({ token }));
    ws.onmessage = async (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === "info") {
        deadline = Date.now() + m.left * 1000; tick();
        document.getElementById("why").textContent = (m.site ? m.site + ": " : "") + (m.reason || "");
        if (Array.isArray(m.region) && m.region.length === 4) {
          region = m.region; document.getElementById("bfull").style.display = "";
        }
        say("Жду картинку…");
      } else if (m.type === "frame") {
        // Темп ack: следующий кадр придёт только после ответа, наложений нет.
        try {
          const bin = atob(m.data), buf = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
          const bmp = await createImageBitmap(new Blob([buf], { type: "image/jpeg" }));
          imgW = bmp.width; imgH = bmp.height;
          crop = cropFor(imgW, imgH);
          if (cv.width !== crop.w || cv.height !== crop.h) { cv.width = crop.w; cv.height = crop.h; }
          ctx.drawImage(bmp, crop.x, crop.y, crop.w, crop.h, 0, 0, crop.w, crop.h);
          bmp.close && bmp.close();
          say("");
        } catch (e) {}
        send({ type: "ack", seq: m.seq });
      } else if (m.type === "focus") {
        if (m.id === probeSeq) onFocus(m.kind);
      } else if (m.type === "typed") {
        if (sending && m.id === typedSeq) { kb.value = ""; stopSending(); }
      } else if (m.type === "closed") {
        finished = true; leftEl.textContent = "";
        say(REASONS[m.reason] || "Экран закрыт.");
      }
    };
    ws.onclose = () => {
      // Вставка оборвалась: сколько букв дошло, неизвестно, сами не повторяем.
      const cut = sending ? "Вставка прервалась: проверь поле на сайте, текст остался в строке. " : "";
      stopSending();
      if (!finished) say(cut + "Связь прервалась. Обнови страницу; не грузится, включи v2rayNG или Tailscale.");
    };
  }

  // Координаты пальца в пиксели вкладки: кадр приходит в размере окна браузера.
  function pos(e) {
    const r = cv.getBoundingClientRect();
    return { x: Math.round(crop.x + (e.clientX - r.left) * crop.w / r.width),
             y: Math.round(crop.y + (e.clientY - r.top) * crop.h / r.height) };
  }
  // Точка на месте касания: видно, попал ли палец в поле сайта.
  function mark(e) {
    const r = wrap.getBoundingClientRect(), d = document.createElement("div");
    d.className = "dot";
    d.style.left = (e.clientX - r.left + wrap.scrollLeft) + "px";
    d.style.top = (e.clientY - r.top + wrap.scrollTop) + "px";
    wrap.appendChild(d);
    setTimeout(() => { d.style.opacity = "0"; }, 400);
    setTimeout(() => d.remove(), 1300);
  }
  function scale() { return crop.w / cv.getBoundingClientRect().width; }
  function mouse(ev, p, extra) {
    send(Object.assign({ type: "input_mouse", eventType: ev, x: p.x, y: p.y, button: "none", clickCount: 0 }, extra));
  }
  const HELD = { button: "left", buttons: 1, clickCount: 1 };
  const UP = { button: "left", buttons: 0, clickCount: 1 };
  let down = null, lastMove = 0;
  cv.addEventListener("pointerdown", (e) => {
    probeSeq++;  // новый жест: запоздалый ответ о фокусе уже не применяем
    if (drag && sending) return;  // ползунок во время вставки увёл бы фокус сайта
    down = { x: e.clientX, y: e.clientY, lastY: e.clientY, lastX: e.clientX, moved: false };
    if (drag && imgW) {
      // Ползунок: нажать, вести, отпустить, как мышью.
      cv.setPointerCapture(e.pointerId);
      const p = pos(e);
      mouse("mouseMoved", p, {});
      mouse("mousePressed", p, HELD);
    }
  });
  cv.addEventListener("pointermove", (e) => {
    if (!down) return;
    if (Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y) > 10) down.moved = true;
    if (drag) {
      const now = Date.now();
      if (now - lastMove >= 30) { lastMove = now; mouse("mouseMoved", pos(e), { button: "left", buttons: 1 }); }
    } else if (down.moved && zoom === 1) {
      const p = pos(e), k = scale();
      mouse("mouseWheel", p, { deltaX: (down.lastX - e.clientX) * k, deltaY: (down.lastY - e.clientY) * k });
      down.lastX = e.clientX; down.lastY = e.clientY;
    }
  });
  cv.addEventListener("pointercancel", (e) => {
    if (down && drag && imgW) mouse("mouseReleased", pos(e), UP);
    down = null;
  });
  cv.addEventListener("pointerup", (e) => {
    if (down && imgW) {
      const p = pos(e);
      if (drag) {
        mouse("mouseMoved", p, { button: "left", buttons: 1 });
        mouse("mouseReleased", p, UP);
      } else if (!down.moved && !sending) {
        // Пока идёт вставка, касание не уводит фокус сайта: буквы не уйдут в другое поле.
        mark(e);
        mouse("mouseMoved", p, {});
        mouse("mousePressed", p, HELD);
        mouse("mouseReleased", p, UP);
        probe();
      }
    }
    down = null;
  });
  // Строка ввода в фокусе: касание экрана его не отнимает, клавиатура телефона
  // не прячется между полями. В масштабе касание двигает картинку, не трогаем.
  cv.addEventListener("touchstart", (e) => {
    if (zoom === 1 && document.activeElement === kb) e.preventDefault();
  }, { passive: false });
  cv.addEventListener("mousedown", (e) => { if (document.activeElement === kb) e.preventDefault(); });

  function wheel(dy) {
    if (!imgW) return;
    mouse("mouseWheel", { x: Math.round(crop.x + crop.w / 2), y: Math.round(crop.y + crop.h / 2) },
          { deltaX: 0, deltaY: dy });
  }
  document.getElementById("bup").onclick = () => wheel(-300);
  document.getElementById("bdn").onclick = () => wheel(300);
  document.getElementById("bdr").onclick = (e) => {
    drag = !drag;
    e.target.classList.toggle("on", drag);
    wrap.classList.toggle("drag", drag);
  };
  document.getElementById("bfull").onclick = (e) => {
    full = !full;
    e.target.textContent = full ? "🔍" : "⛶";
  };
  document.getElementById("bzm").onclick = (e) => {
    zoom = zoom === 1 ? 2 : zoom === 2 ? 3 : 1;
    e.target.textContent = zoom + "x";
    cv.style.width = (zoom * 100) + "%";
    wrap.classList.toggle("zoom", zoom > 1);
  };

  function press(k) {
    send({ type: "input_keyboard", eventType: "keyDown", key: k });
    send({ type: "input_keyboard", eventType: "keyUp", key: k });
  }
  // Куда на сайте ушёл фокус, служба отвечает после касания. Поле: строка
  // сверху сама встаёт в фокус (клавиатура телефона без лишних касаний),
  // для пароля прячет буквы. Ответ на старое касание не применяем.
  let probeSeq = 0, lastKind = "";
  const MODES = { text: "text", password: "text", email: "email", tel: "tel", number: "numeric",
    search: "search", url: "url" };
  function probe() { send({ type: "probe", id: ++probeSeq }); }
  function onFocus(kind) {
    if (sending) return;
    if (MODES[kind]) {
      // Неотправленный пароль в открытое поле не переносим.
      if (lastKind === "password" && kind !== "password" && !sending) kb.value = "";
      kb.type = kind === "password" ? "password" : "text";
      kb.inputMode = MODES[kind];
      // Менеджер паролей телефона видит адрес экрана, а не сайта: не сохранять
      // туда чужие пароли (ревью 22.10.2026).
      kb.autocomplete = "off";
      kb.placeholder = kind === "password" ? "Пароль: набери и нажми «Вставить»" : "Набери и нажми «Вставить»";
      lastKind = kind;
      kb.focus();
    } else if (kind === "unknown") {
      kb.placeholder = "Если это поле, коснись этой строки";
    } else {
      kb.placeholder = "Коснись поля на сайте";
      if (document.activeElement === kb) kb.blur();
    }
  }
  // Текст набирается в поле сверху целиком (подсказки и автозамена телефона
  // не мешают, экран не уезжает) и уходит на сайт по «Вставить» по порядку.
  // Порциями по 10 знаков раз в 100 мс: пачка разом упиралась бы в предел
  // частоты службы. Поле чистится, только когда служба подтвердила, что весь
  // текст ушёл в браузер; без связи текст остаётся в строке. На время
  // вставки строка только для чтения, а у каждой вставки свой номер: таймер
  // прерванной не допишет её хвост в следующую.
  let sending = false, typedSeq = 0, gen = 0;
  function stopSending() { sending = false; gen++; kb.readOnly = false; }
  function insert() {
    if (sending || !kb.value) return;
    if (!ws || ws.readyState !== 1) { say("Нет связи: текст не отправлен, он остался в строке"); return; }
    const chars = Array.from(kb.value);
    if (chars.length > 200) { say("Не больше 200 знаков за раз"); return; }
    sending = true;
    kb.readOnly = true;
    const my = ++gen;
    let i = 0;
    const step = () => {
      if (!sending || my !== gen) return;
      for (const ch of chars.slice(i, i + 10)) press(ch);
      i += 10;
      if (i < chars.length) setTimeout(step, 100);
      else send({ type: "typed", id: ++typedSeq });
    };
    step();
  }
  document.getElementById("bins").onclick = () => { insert(); kb.focus(); };
  kb.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); insert(); } });
  // Кнопки не отнимают фокус у строки: клавиатура телефона не прячется.
  for (const id of ["btab", "bbs", "ben", "bins"]) {
    document.getElementById(id).addEventListener("mousedown", (e) => e.preventDefault());
  }
  document.getElementById("btab").onclick = () => { if (!sending) { press("Tab"); probe(); } };
  document.getElementById("bbs").onclick = () => { if (!sending) press("Backspace"); };
  document.getElementById("ben").onclick = () => { if (!sending) press("Enter"); };
  document.getElementById("bdone").onclick = () => send({ type: "done" });
  document.getElementById("bcant").onclick = () => { if (confirm("Точно не получается?")) send({ type: "cant" }); };
  // Ушёл за паролем в другое приложение: телефон рвёт связь в фоне.
  // Вернулся, подключаемся сами, без обновления страницы.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !finished && ws && ws.readyState > 1) connect();
  });

  connect();
})();
</script>
</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())

"""Общий модуль связи с Bot API (tg_api, BACKLOG шаг 2 от 07.11.2026):
один адрес сервера для всех, токен не в исключениях, повтор только неушедшего."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_tg_bot import lifecycle, notify, serious, tg_api  # noqa: E402

TOKEN = "123456:SECRET-token_value"
LOCAL = "http://127.0.0.1:8081"


def _ok(message_id: int = 7):
    return SimpleNamespace(status_code=200, text="ok",
                           json=lambda: {"ok": True, "result": {"message_id": message_id}})


# ---- адрес ----

@pytest.mark.parametrize("raw,want", [
    (None, tg_api.DEFAULT_URL), ("", tg_api.DEFAULT_URL), ("  ", tg_api.DEFAULT_URL),
    ("https://api.telegram.org/", "https://api.telegram.org"),
    (LOCAL, LOCAL), ("http://localhost:8081", "http://localhost:8081"), ("http://[::1]:8081", "http://[::1]:8081"),
    ("https://tg.example.com:8443", "https://tg.example.com:8443"),
])
def test_parse_url_ok(raw, want) -> None:
    assert tg_api.parse_url(raw) == want


@pytest.mark.parametrize("raw", [
    "http://192.168.1.5:8081",  # токен по сети открытым текстом
    "ftp://x", "api.telegram.org", "https://x/bot", "https://x?a=1", "https://u:p@x",
    "https://x:0", "https://x:99999", "https://", "http://127.0.0.1:abc",
    "https://x?", "https://x#", "http://127.0.0.1:8081#", "https://api.telegram.org x", "https://x:",
    "http://127.0.0.1:@evil.com", "http://localhost:80@evil.com",
    "/", "///", "https://api.tele\ngram.org", "https://api.tele\x00gram.org",
])
def test_parse_url_rejects(raw) -> None:
    with pytest.raises(ValueError) as e:
        tg_api.parse_url(raw)
    assert str(e.value).startswith(tg_api.ENV) and "://" not in str(e.value).replace("https://узел", "")


def test_ptb_urls() -> None:
    assert tg_api.ptb_urls(LOCAL) == (f"{LOCAL}/bot", f"{LOCAL}/file/bot")


def test_ptb_bot_gets_base_url() -> None:
    from telegram import Bot
    base_url, file_url = tg_api.ptb_urls(LOCAL)
    bot = Bot(TOKEN, base_url=base_url, base_file_url=file_url)
    assert bot.base_url == f"{LOCAL}/bot{TOKEN}"
    assert bot.base_file_url == f"{LOCAL}/file/bot{TOKEN}"


def test_build_app_uses_config_url() -> None:
    from claude_tg_bot import bot as bot_mod
    seen = {}

    class Stop(Exception):
        pass

    def fake_bot(token, **kw):
        seen.update(kw)
        raise Stop
    cfg = SimpleNamespace(telegram_bot_token=TOKEN, telegram_bot_api_url=LOCAL)
    with patch.object(bot_mod, "_SilentAwareBot", fake_bot), pytest.raises(Stop):
        bot_mod.build_app(cfg)
    assert seen["base_url"] == f"{LOCAL}/bot" and seen["base_file_url"] == f"{LOCAL}/file/bot"


def test_config_rejects_bad_url(monkeypatch) -> None:
    from claude_tg_bot import config
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv(tg_api.ENV, "http://10.0.0.1:8081")
    with patch.object(config, "load_dotenv"), pytest.raises(RuntimeError, match=tg_api.ENV):
        config.load_config(skip_hook_validation=True)


# ---- запрос ----

def test_post_default_and_env_url(monkeypatch) -> None:
    monkeypatch.delenv(tg_api.ENV, raising=False)
    with patch.object(httpx, "post", return_value=_ok()) as post:
        tg_api.post(TOKEN, "sendMessage", json={"a": 1})
    assert post.call_args.args[0] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert post.call_args.kwargs == {"json": {"a": 1}, "timeout": 10.0}
    monkeypatch.setenv(tg_api.ENV, LOCAL + "/")
    with patch.object(httpx, "post", return_value=_ok()) as post:
        tg_api.post(TOKEN, "sendDocument", data={"chat_id": 1}, files={"document": b"x"}, timeout=30.0)
    assert post.call_args.args[0] == f"{LOCAL}/bot{TOKEN}/sendDocument"
    assert set(post.call_args.kwargs) == {"data", "files", "timeout", "trust_env"}


def _raise_and_format(err: Exception) -> tuple[Exception, str]:
    with patch.object(httpx, "post", side_effect=err):
        try:
            tg_api.post(TOKEN, "sendMessage", json={})
        except Exception as e:  # noqa: BLE001
            return e, "".join(traceback.format_exception(e))
    raise AssertionError("не упало")


@pytest.mark.parametrize("err", [
    httpx.ConnectError(f"boom https://api.telegram.org/bot{TOKEN}/sendMessage"),
    httpx.ReadTimeout(f"timed out bot{TOKEN}"),
    httpx.InvalidURL(f"bad https://api.telegram.org/bot{TOKEN}/x"),
    httpx.HTTPStatusError(f"500 bot{TOKEN}", request=httpx.Request("POST", f"https://x/bot{TOKEN}"),
                          response=httpx.Response(500)),
])
def test_errors_carry_no_token(err) -> None:
    got, text = _raise_and_format(err)
    assert TOKEN not in text and TOKEN not in repr(got)
    assert isinstance(got, httpx.HTTPError)
    assert got.__cause__ is None and got.__context__ is None
    if not isinstance(err, (httpx.InvalidURL, httpx.HTTPStatusError)):
        assert type(got) is type(err)  # вызывающие ловят ReadTimeout отдельно


def test_retry_only_not_sent(monkeypatch) -> None:
    sleeps = []
    monkeypatch.setattr(tg_api.time, "sleep", sleeps.append)
    with patch.object(httpx, "post", side_effect=[httpx.ConnectError("x"), httpx.PoolTimeout("y"), _ok()]) as post:
        assert tg_api.post(TOKEN, "m", json={}, retry_not_sent=True).status_code == 200
    assert post.call_count == 3 and sleeps == list(tg_api.RETRY_DELAYS)
    for err in (httpx.ReadTimeout("t"), httpx.ReadError("r"), httpx.WriteTimeout("w"), httpx.RemoteProtocolError("p")):
        with patch.object(httpx, "post", side_effect=[err, _ok()]) as post, pytest.raises(type(err)):
            tg_api.post(TOKEN, "m", json={}, retry_not_sent=True)
        assert post.call_count == 1, err


def test_no_retry_by_default() -> None:
    with patch.object(httpx, "post", side_effect=[httpx.ConnectError("x"), _ok()]) as post, \
            pytest.raises(httpx.ConnectError):
        tg_api.post(TOKEN, "m", json={})
    assert post.call_count == 1


def test_retry_gives_up_after_delays(monkeypatch) -> None:
    monkeypatch.setattr(tg_api.time, "sleep", lambda s: None)
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("x")) as post, \
            pytest.raises(httpx.ConnectError):
        tg_api.post(TOKEN, "m", json={}, retry_not_sent=True)
    assert post.call_count == 1 + len(tg_api.RETRY_DELAYS)


def test_markers() -> None:
    assert tg_api.is_unconfirmed(httpx.ReadTimeout("x"))
    assert not tg_api.is_unconfirmed(httpx.ReadError("x"))
    assert tg_api.is_not_sent(httpx.ConnectTimeout("x"))
    assert not tg_api.is_not_sent(httpx.ReadError("x"))


def test_replies_shares_markers() -> None:
    from telegram.error import TimedOut

    from claude_tg_bot import replies
    assert replies._NOT_SENT_CAUSES is tg_api.NOT_SENT
    assert replies._SEND_RETRY_DELAYS is tg_api.RETRY_DELAYS
    e = TimedOut()
    e.__cause__ = httpx.ReadTimeout("")
    assert replies.is_unconfirmed(e)
    e.__cause__ = httpx.ReadError("")
    assert not replies.is_unconfirmed(e)
    assert not replies.is_unconfirmed(TimedOut())


# ---- асинхронный запрос (уведомление об остановке) ----

def _mock_async(handler):
    real = httpx.AsyncClient
    return patch.object(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_apost_url_and_scrub(monkeypatch) -> None:
    monkeypatch.setenv(tg_api.ENV, LOCAL)
    urls = []

    def ok(req):
        urls.append(str(req.url))
        return httpx.Response(200, json={"ok": True})
    with _mock_async(ok):
        r = asyncio.run(tg_api.apost(TOKEN, "sendMessage", json={"chat_id": 1}))
    assert r.status_code == 200 and urls == [f"{LOCAL}/bot{TOKEN}/sendMessage"]

    def boom(req):
        raise httpx.ConnectError(f"no route {req.url}")
    with _mock_async(boom):
        try:
            asyncio.run(tg_api.apost(TOKEN, "sendMessage", json={}))
        except httpx.ConnectError as e:
            assert TOKEN not in "".join(traceback.format_exception(e))
        else:
            raise AssertionError("не упало")


def test_apost_total_timeout(monkeypatch) -> None:
    monkeypatch.setenv(tg_api.ENV, LOCAL)

    async def slow(req):
        await asyncio.sleep(2)
        return httpx.Response(200)
    with _mock_async(slow), pytest.raises(TimeoutError):
        asyncio.run(tg_api.apost(TOKEN, "sendMessage", json={}, timeout=0.2))


@pytest.mark.parametrize("url,trust", [(LOCAL, False), ("https://api.telegram.org", True)])
def test_local_server_ignores_env_proxy(monkeypatch, url, trust) -> None:
    monkeypatch.setenv(tg_api.ENV, url)
    with patch.object(httpx, "post", return_value=_ok()) as post:
        tg_api.post(TOKEN, "m", json={})
    assert post.call_args.kwargs.get("trust_env", True) is trust
    seen = {}
    real = httpx.AsyncClient

    def client(**kw):
        seen.update(kw)
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200)), **kw)
    with patch.object(httpx, "AsyncClient", client):
        asyncio.run(tg_api.apost(TOKEN, "m", json={}))
    assert seen["trust_env"] is trust


def test_shutdown_notice_goes_through_tg_api(caplog) -> None:
    apost = AsyncMock(return_value=SimpleNamespace(status_code=200))
    with patch.object(tg_api, "apost", apost):
        asyncio.run(lifecycle._send_shutdown_notice_via_api(TOKEN, "5:9", "текст"))
    apost.assert_awaited_once_with(TOKEN, "sendMessage",
                                   json={"chat_id": 5, "text": "текст", "message_thread_id": 9}, timeout=5.0)
    err = httpx.ConnectError("x")
    with patch.object(tg_api, "apost", AsyncMock(side_effect=err)), caplog.at_level("WARNING"):
        asyncio.run(lifecycle._send_shutdown_notice_via_api(TOKEN, "5", "текст"))
    assert "ConnectError" in caplog.text and TOKEN not in caplog.text


# ---- вызывающие идут на адрес из настройки ----

def test_notify_uses_configured_url(monkeypatch) -> None:
    monkeypatch.setenv(tg_api.ENV, LOCAL)
    cfg = SimpleNamespace(primary_chat_id=111, telegram_bot_token=TOKEN, telegram_text_limit=4096,
                          allowed_user_ids=frozenset({111}), allowed_chat_ids=frozenset())
    with patch.object(notify, "load_config", return_value=cfg), \
            patch.object(httpx, "post", return_value=_ok()) as post:
        assert notify.main(["--pin", "привет"]) == 0
    assert [c.args[0] for c in post.call_args_list] == [
        f"{LOCAL}/bot{TOKEN}/sendMessage", f"{LOCAL}/bot{TOKEN}/pinChatMessage"]


def test_no_hand_built_bot_api_urls() -> None:
    """Адрес Bot API собирается только в tg_api (и в bot-down-notify.py без пакета):
    строк кода с узлом облака или путём `/bot` к токену нет нигде больше."""
    import ast
    src = ROOT / "src" / "claude_tg_bot"
    bad = []
    for path in src.rglob("*.py"):
        # serious: шаблоны распознавания; tg_probe: замерщик нарочно меряет
        # облачный путь своей настройкой TG_PROBE_BOT_API_URL (выключить до перехода).
        if path.name in ("tg_api.py", "serious.py", "tg_probe.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
                    and ("api.telegram.org" in node.value or node.value.endswith("/bot"))):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert bad == []


# ---- bot-down-notify.py ----

def _bot_down():
    spec = importlib.util.spec_from_file_location("bot_down_notify", ROOT / "bot-down-notify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BOT_DOWN_URLS = [
    "", LOCAL, LOCAL + "/", "http://localhost:8081", "http://LOCALHOST:8081", "http://localhost.:8081",
    "HTTPS://tg.example.com", "http://127.0.0.2:8081", "http://[::1]:8081", "http://[::ffff:127.0.0.1]:8081",
    "https://tg.example.com", "http://10.0.0.1:8081", "http://127.0.0.1.evil.com", "ftp://x",
    "http://127.0.0.1:@evil.com", "http://localhost:80@evil.com", "https://x/bot", "https://x?", "https://",
    "/", "https://api.tele\ngram.org",
]


@pytest.mark.parametrize("value", BOT_DOWN_URLS)
def test_bot_down_url_matches_bot(value) -> None:
    """Тревога принимает ровно те адреса, с которыми стартует бот."""
    try:
        want = tg_api.parse_url(value)
    except ValueError:
        want = None
    assert _bot_down().api_url({"TELEGRAM_BOT_API_URL": value}) == want


def test_bot_down_env_inline_comment(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_API_URL={LOCAL} # свой сервер\nA='x # y'\nB=\"{LOCAL}\" # c\nC=\"\"\nD=1\t# tab\n",
                   encoding="utf-8")
    got = _bot_down().read_env(env)
    assert got == {"TELEGRAM_BOT_API_URL": LOCAL, "A": "x # y", "B": LOCAL, "C": "", "D": "1"}


@pytest.mark.parametrize("value,want", [(LOCAL, LOCAL), ("http://10.0.0.1:8081", tg_api.DEFAULT_URL)])
def test_bot_down_posts_to_configured_url(tmp_path, monkeypatch, value, want) -> None:
    """Свой адрес из .env; негодный: всё равно облако (бот с ним не стартует, тревога важнее)."""
    mod = _bot_down()
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USER_IDS=5\n"
                                   f"TELEGRAM_BOT_API_URL={value}\n", encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bot-down-notify.py", "--test"])
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b'{"ok": true}'

    bodies = []

    def urlopen(req, timeout):
        seen.append(req.full_url)
        bodies.append(req.data.decode())
        return Resp()
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    assert mod.main() == 0
    assert seen == [f"{want}/bot{TOKEN}/sendMessage"]
    assert ("TELEGRAM_BOT_API_URL" in bodies[0]) == (want != value)


ALERT_TOKEN = "9999999:ALERT-secret"


def _run_bot_down(tmp_path, monkeypatch, env_text: str, fail_tokens=()):
    """fail_tokens: пары (токен, «os» | «http» | «notok»)."""
    mod = _bot_down()
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bot-down-notify.py", "--test"])
    sent = []

    class Resp:
        def __init__(self, ok):
            self.ok = ok

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            if self.ok == "str":
                return b'{"ok": "false"}'
            return b'{"ok": true}' if self.ok else b'{"ok": false}'

    def urlopen(req, timeout):
        sent.append((req.full_url, json.loads(req.data)))
        for t, kind in fail_tokens:
            if t in req.full_url:
                if kind == "http":
                    raise mod.urllib.error.HTTPError(req.full_url, 400, f"bad {req.full_url}", {}, None)
                if kind == "notok":
                    return Resp(False)
                if kind == "strok":
                    return Resp("str")
                raise OSError(f"down {req.full_url}")
        return Resp(True)
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod.main(), sent


ALERT_ENV = f"ALERT_BOT_TOKEN={ALERT_TOKEN}\nALERT_CHAT_ID=-1005\nALERT_THREAD_ID=77\n"


def test_bot_down_uses_alert_bot_in_cloud(tmp_path, monkeypatch) -> None:
    """Бот тревог в облако даже при своём сервере основного бота."""
    rc, sent = _run_bot_down(tmp_path, monkeypatch, f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USER_IDS=5\n"
                             f"TELEGRAM_BOT_API_URL={LOCAL}\n" + ALERT_ENV)
    assert rc == 0 and len(sent) == 1
    url, body = sent[0]
    assert url == f"https://api.telegram.org/bot{ALERT_TOKEN}/sendMessage"
    assert body["chat_id"] == -1005 and body["message_thread_id"] == 77
    assert "disable_notification" not in body  # тревога со звуком


def test_bot_down_alert_failure_falls_back_to_main(tmp_path, monkeypatch) -> None:
    rc, sent = _run_bot_down(tmp_path, monkeypatch, f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USER_IDS=5,3\n"
                             f"TELEGRAM_BOT_API_URL={LOCAL}\n" + ALERT_ENV, fail_tokens=((ALERT_TOKEN, "os"),))
    assert rc == 0  # бот тревог пробует трижды (разбор сбоев 07.11.2026), затем основной
    assert [u for u, _b in sent] == [f"https://api.telegram.org/bot{ALERT_TOKEN}/sendMessage"] * 3 + [
        f"{LOCAL}/bot{TOKEN}/sendMessage"]
    assert sent[3][1] == {"chat_id": 3, "text": sent[0][1]["text"]}


MAIN_ENV = f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USER_IDS=5\n"


@pytest.mark.parametrize("kind", ["os", "http", "notok", "strok"])
def test_bot_down_fallback_on_any_failure_without_token_in_stderr(tmp_path, monkeypatch, capsys, kind) -> None:
    rc, sent = _run_bot_down(tmp_path, monkeypatch, MAIN_ENV + ALERT_ENV,
                             fail_tokens=((ALERT_TOKEN, kind), (TOKEN, kind)))
    # Отказ Telegram (4xx) не повторяется, остальное по три попытки на путь.
    assert rc == 1 and len(sent) == (2 if kind == "http" else 6)
    err = capsys.readouterr().err
    assert TOKEN not in err and ALERT_TOKEN not in err and "SECRET" not in err


def test_bot_down_broken_users_still_alerts(tmp_path, monkeypatch) -> None:
    rc, sent = _run_bot_down(tmp_path, monkeypatch, f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USER_IDS=5;3\n"
                             + ALERT_ENV)
    assert rc == 0 and [u for u, _b in sent] == [f"https://api.telegram.org/bot{ALERT_TOKEN}/sendMessage"]


def test_bot_down_no_cloud_fallback_with_bad_url(tmp_path, monkeypatch) -> None:
    """Бот тревог не отправил, адрес основного негоден: в облако основным не шлём."""
    rc, sent = _run_bot_down(tmp_path, monkeypatch, MAIN_ENV + "TELEGRAM_BOT_API_URL=http://10.0.0.1:8081\n"
                             + ALERT_ENV, fail_tokens=((ALERT_TOKEN, "os"),))
    assert rc == 1 and [u for u, _b in sent] == [f"https://api.telegram.org/bot{ALERT_TOKEN}/sendMessage"] * 3


@pytest.mark.parametrize("extra,chat,thread", [
    ("ALERT_CHAT_ID=-1005\n", -1005, None),
    ("ALERT_CHAT_ID=-1005 # группа\nALERT_THREAD_ID=77 # тема\n", -1005, 77),
    ("ALERT_CHAT_ID=@pc_alerts\nALERT_THREAD_ID=x\n", "@pc_alerts", None),
    ("ALERT_CHAT_ID=-1005\nALERT_THREAD_ID=0\n", -1005, None),
    ("ALERT_CHAT_ID=-1005\nALERT_THREAD_ID=\u00b2\n", -1005, None),
    ("export\tALERT_CHAT_ID=-1005\nALERT_THREAD_ID= # без темы\n", -1005, None),
])
def test_bot_down_alert_parsing(tmp_path, monkeypatch, extra, chat, thread) -> None:
    rc, sent = _run_bot_down(tmp_path, monkeypatch, f"ALERT_BOT_TOKEN={ALERT_TOKEN}\n" + extra)
    assert rc == 0
    body = sent[0][1]
    assert body["chat_id"] == chat and body.get("message_thread_id") == thread


def test_bot_down_bad_alert_chat_number_uses_main(tmp_path, monkeypatch) -> None:
    rc, sent = _run_bot_down(tmp_path, monkeypatch, MAIN_ENV + f"ALERT_BOT_TOKEN={ALERT_TOKEN}\nALERT_CHAT_ID=--1005\n")
    assert rc == 0 and [u for u, _b in sent] == [f"https://api.telegram.org/bot{TOKEN}/sendMessage"]


def test_bot_down_hash_value_and_zero_chat(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("B=#abc\nC= #c\n", encoding="utf-8")
    assert _bot_down().read_env(env) == {"B": "#abc", "C": ""}
    rc, sent = _run_bot_down(tmp_path, monkeypatch, MAIN_ENV + f"ALERT_BOT_TOKEN={ALERT_TOKEN}\nALERT_CHAT_ID=0\n")
    assert rc == 0 and [u for u, _b in sent] == [f"https://api.telegram.org/bot{TOKEN}/sendMessage"]


def test_bot_down_half_alert_blocks_cloud_fallback_with_bad_url(tmp_path, monkeypatch) -> None:
    rc, sent = _run_bot_down(tmp_path, monkeypatch, MAIN_ENV + "TELEGRAM_BOT_API_URL=http://10.0.0.1:8081\n"
                             f"ALERT_BOT_TOKEN={ALERT_TOKEN}\nALERT_CHAT_ID=x\n")
    assert rc == 1 and sent == []


def test_bot_down_empty_url_with_comment_is_cloud(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_API_URL= # облако\n", encoding="utf-8")
    mod = _bot_down()
    assert mod.api_url(mod.read_env(env)) == "https://api.telegram.org"


@pytest.mark.parametrize("rc,out,raises,want", [
    (0, "failed\n", False, True), (0, "active\n", False, False), (0, "activating\n", False, False),
    (1, "", False, True), (0, "", False, True), (0, "", True, True),
])
def test_bot_down_unit_failed(monkeypatch, rc, out, raises, want) -> None:
    mod = _bot_down()

    def run(*a, **kw):
        if raises:
            raise OSError("no systemctl")
        return SimpleNamespace(returncode=rc, stdout=out)
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.unit_failed() is want


def test_bot_down_half_configured_alert_warns(tmp_path, monkeypatch, capsys) -> None:
    rc, sent = _run_bot_down(tmp_path, monkeypatch, MAIN_ENV + f"ALERT_BOT_TOKEN={ALERT_TOKEN}\nALERT_CHAT_ID=\n")
    assert rc == 0 and [u for u, _b in sent] == [f"https://api.telegram.org/bot{TOKEN}/sendMessage"]
    assert "ALERT_CHAT_ID" in capsys.readouterr().err


def test_bot_down_alert_only_and_bad_alert(tmp_path, monkeypatch) -> None:
    rc, sent = _run_bot_down(tmp_path, monkeypatch, ALERT_ENV)
    assert rc == 0 and len(sent) == 1
    rc, sent = _run_bot_down(tmp_path, monkeypatch, ALERT_ENV, fail_tokens=((ALERT_TOKEN, "os"),))
    assert rc == 1 and len(sent) == 3  # три попытки (разбор сбоев 07.11.2026)
    # Негодный номер чата: бота тревог нет, путь основного бота.
    rc, sent = _run_bot_down(tmp_path, monkeypatch, f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USER_IDS=5\n"
                             f"ALERT_BOT_TOKEN={ALERT_TOKEN}\nALERT_CHAT_ID=x\n")
    assert rc == 0 and [u for u, _b in sent] == [f"https://api.telegram.org/bot{TOKEN}/sendMessage"]


# ---- сторож команд агента узнаёт Bot API на своём сервере ----

def _why(cmd: str) -> set[str]:
    return serious.send_reasons("Bash", {"command": cmd}, None)


@pytest.mark.parametrize("cmd", [
    'curl -s "http://127.0.0.1:8081/bot$TOKEN/sendMessage" -d chat_id=5 -d text=hi',
    'API="http://localhost:8081/bot${TELEGRAM_BOT_TOKEN}"; curl -s "$API/sendVoice" -F voice=@a.ogg',
    "curl http://127.0.0.1:8081/bot123456:AAAbbb/forwardMessage -d chat_id=1",
    """python3 -c 'import os,httpx; httpx.post(f"http://127.0.0.1:8081/bot{os.environ["T"]}/sendMessage")'""",
    """python3 -c 'u="http://127.0.0.1:8081/bot"+tok+"/sendMessage"'""",
    """python3 -c 'u="http://127.0.0.1:8081/bot%s/sendMessage" % t'""",
    'python3 - <<EOF\nimport httpx\nhttpx.post(f"{base}/bot{tok}/sendPhoto")\nEOF',
    'curl "http://127.0.0.1:8081/bot$(cat ~/.tok)/sendMessage" -d text=hi',
    'curl http://127.0.0.1:8081/bot"$TOKEN"/sendMessage -d text=hi',
    'curl "http://127.0.0.1:8081/bot$1/sendMessage" -d text=hi',
    'curl "http://127.0.0.1:8081/bot$(< ~/.tok)/sendMessage" -d text=hi',
    """python3 -c 'from claude_tg_bot import tg_api; tg_api.post(t, "sendMessage", json={"chat_id": 1})'""",
    """python3 -c 'from claude_tg_bot import tg_api; from claude_tg_bot.config import load_config; """
    """tg_api.post(load_config(skip_hook_validation=True).telegram_bot_token, "sendMessage", json={})'""",
    """python3 -c 'from claude_tg_bot.tg_api import post; m="sendDocument"; post(t, m, data={})'""",
    'curl "http://127.0.0.1:8081/bot$( cat ~/.tok )/sendMessage" -d a=1',
])
def test_local_bot_api_send_is_card(cmd: str) -> None:
    found = _why(cmd)
    assert found and not serious.sends(found)


@pytest.mark.parametrize("cmd", [
    'curl -s "http://127.0.0.1:8081/bot$TOKEN/getUpdates"',
    "git commit -m 'tg_api: /bot$TOKEN/sendMessage на свой сервер'",
    "ls /srv/bot/send",
    "grep -rn '/bot{token}/sendMessage' src", "docker run bot12345:latest",
    'curl -s "http://127.0.0.1:8081/bot$TOKEN/getMe"',
    """python3 -c 'from claude_tg_bot import tg_api; print(tg_api.post(t, "getMe").json())'""",
    """grep -rn 'tg_api.post(.*"sendMessage"' src""", "git commit -m 'tg_api: \"sendMessage\" через модуль'",
    "pytest -q tests/test_tg_api_2026_11_07.py -k sendMessage",
])
def test_local_bot_api_reads_free(cmd: str) -> None:
    assert _why(cmd) == set()


def test_webfetch_docs_page_free() -> None:
    assert serious.send_reasons("WebFetch", {"url": "https://docs.example.org/bot%2Dapi/sendMessage.html"},
                                None) == set()


def test_webfetch_local_bot_api_send_is_card() -> None:
    found = serious.send_reasons("WebFetch", {"url": f"{LOCAL}/bot123456:AAA/sendMessage?chat_id=1"}, None)
    assert found and not serious.sends(found)

"""Напоминание о конце входа claude.ai (`loginExpiresAt`, Claude Code 2.1.318)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from claude_tg_bot import login_expiry as le

TZ = ZoneInfo("Asia/Dubai")
DAY = 86400
# Конец входа как на ПК 04.11.2026: 06.11 02:56:50.971 UTC.
EXPIRES = datetime(2026, 11, 6, 2, 56, 50, 971000, tzinfo=timezone.utc).timestamp()
# Самый ранний конец по правилу CLI: 03.11 02:56 UTC.
EARLIEST = EXPIRES - le.WINDOW


@pytest.mark.parametrize("value", [
    "2026-11-06T02:56:50.971Z",
    EXPIRES,
    int(EXPIRES * 1000),
    str(int(EXPIRES * 1000)),
])
def test_parse_three_formats(value) -> None:
    assert le.parse_expiry(value) == pytest.approx(EXPIRES, abs=0.001)


@pytest.mark.parametrize("value", [None, "", "завтра", True, 0, -5, float("nan"), {}, [],
                                   1e300, float("inf")])
def test_parse_rejects_garbage(value) -> None:
    assert le.parse_expiry(value) is None


def test_silent_beyond_ten_days() -> None:
    key, state = le.due_notice(EXPIRES, EXPIRES - 15 * DAY, {}, TZ)
    assert key is None
    assert state == {"expires_at": EXPIRES, "sent": []}


def test_one_message_per_threshold() -> None:
    state: dict = {}
    seen = []
    # Каждые 6 часов от 12 дней до конца до двух дней после.
    now = EXPIRES - 12 * DAY
    while now < EXPIRES + 2 * DAY:
        key, state = le.due_notice(EXPIRES, now, state, TZ)
        if key:
            seen.append(key)
        now += 6 * 3600
    assert seen == ["10", "3", "1", "expired"]


def test_downtime_sends_only_nearest() -> None:
    key, state = le.due_notice(EXPIRES, EARLIEST - 2 * DAY, {}, TZ)
    assert key == "3"
    assert state["sent"] == ["10", "3"]
    assert le.due_notice(EXPIRES, EARLIEST - 2 * DAY + 3600, state, TZ)[0] is None


def test_thresholds_count_to_earliest_end() -> None:
    """Пороги считаются до раннего конца: «1» за день до окна, а не до даты CLI."""
    assert le.due_notice(EXPIRES, EXPIRES - 12 * DAY, {}, TZ)[0] == "10"
    assert le.due_notice(EXPIRES, EARLIEST - DAY, {}, TZ)[0] == "1"
    # Окно открыто, дата CLI ещё впереди: последний порог, не «кончился».
    assert le.due_notice(EXPIRES, EARLIEST + 3600, {}, TZ)[0] == "1"
    assert le.due_notice(EXPIRES, EARLIEST, {}, TZ)[0] == "1"
    assert "любой момент" in le.notice_text("1", EXPIRES, EARLIEST, TZ)


def test_transient_logout_does_not_repeat_end() -> None:
    """Токен «стёрт», потом CLI снова отдал ту же дату: «кончился» не второй раз."""
    _key, state = le.logged_out_notice(EARLIEST + DAY, {"expires_at": EXPIRES, "sent": []})
    key, state = le.due_notice(EXPIRES, EARLIEST + DAY + 3600, state, TZ)
    assert key is None and "expired" in state["sent"]
    assert le.due_notice(EXPIRES, EXPIRES + 3600, state, TZ)[0] is None


def test_open_window_after_old_marks() -> None:
    """Как на ПК 04.11: отметки 10 и 3 по старому счёту, окно уже открыто."""
    old = {"expires_at": EXPIRES, "sent": ["10", "3"]}
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc).timestamp()
    key, state = le.due_notice(EXPIRES, now, old, TZ)
    assert key == "1" and state["sent"] == ["1", "10", "3"]


def test_small_shift_keeps_marks() -> None:
    _key, state = le.due_notice(EXPIRES, EARLIEST - 2 * DAY, {}, TZ)
    key, state = le.due_notice(EXPIRES + 5, EARLIEST - 2 * DAY + 3600, state, TZ)
    assert key is None and state["sent"] == ["10", "3"]


def test_shortened_expiry_keeps_marks() -> None:
    """Обновление токена сократило срок на сутки: это не перевход, «10» не повторяется."""
    _key, state = le.due_notice(EXPIRES, EARLIEST - 9 * DAY, {}, TZ)
    assert state["sent"] == ["10"]
    key, state = le.due_notice(EXPIRES - DAY, EARLIEST - 9 * DAY + 3600, state, TZ)
    assert key is None and state["sent"] == ["10"]


@pytest.mark.parametrize("sent", [None, 7, "x", [{}], [1, None]])
def test_broken_marks_do_not_break_check(sent) -> None:
    state = {"expires_at": EXPIRES, "sent": sent}
    assert le.due_notice(EXPIRES, EARLIEST - 2 * DAY, state, TZ)[0] == "3"
    assert le.logged_out_notice(EARLIEST + DAY, state)[0] == "expired"


def test_creeping_shift_rearms_thresholds() -> None:
    """Срок уезжает вперёд шагами меньше суток: пороги 10 и 3 не теряются."""
    _key, state = le.due_notice(EXPIRES, EXPIRES - 2 * DAY, {}, TZ)
    expires, now = EXPIRES, EXPIRES - 2 * DAY
    for _ in range(30):
        expires += 23 * 3600
        now += 3600
        key, state = le.due_notice(expires, now, state, TZ)
        assert key is None
    assert state["sent"] == []
    seen = []
    while now + 6 * 3600 < expires:
        now += 6 * 3600
        key, state = le.due_notice(expires, now, state, TZ)
        if key:
            seen.append(key)
    assert seen == ["10", "3", "1"]


def test_relogin_resets_marks() -> None:
    _key, state = le.due_notice(EXPIRES, EXPIRES - DAY, {}, TZ)
    assert le.due_notice(EXPIRES, EXPIRES - DAY / 2, state, TZ)[0] is None
    later = EXPIRES + 30 * DAY
    key, state = le.due_notice(later, EXPIRES - DAY / 2, state, TZ)
    assert key is None and state == {"expires_at": later, "sent": []}
    assert le.due_notice(later, later - 9 * DAY, state, TZ)[0] == "10"


def test_notice_text_has_window_and_days() -> None:
    with patch.object(le.socket, "gethostname", return_value="pc-1"):
        text = le.notice_text("3", EXPIRES, EARLIEST - 2 * DAY + 3600, TZ)
        gone = le.notice_text("expired", EXPIRES, EXPIRES + 1, TZ)
    assert "pc-1" in text and "03.11" in text and "06.11" in text and " 2 " in text
    assert "pc-1" in gone and "кончился" in gone and "06.11" not in gone
    # Окно открыто: «в любой момент», без «через 0 дн.».
    for now in (EARLIEST + 3600, EXPIRES - 3600):
        inside = le.notice_text("1", EXPIRES, now, TZ)
        assert "06.11" in inside and "любой момент" in inside and " 0 " not in inside
    # День раннего конца, окно ещё не открыто: время начала окна, не «в любой момент».
    before = le.notice_text("1", EXPIRES, EARLIEST - 3600, TZ)
    assert "06:56" in before and "06.11" in before and "любой момент" not in before


def _fake_claude(work: str, output: str, code: int = 0) -> str:
    """Поддельный CLI: ответ только на точный вызов `auth status --json`."""
    path = Path(work) / "claude"
    path.write_text(
        "#!/bin/sh\n[ $# -eq 3 ] && [ \"$1\" = auth ] && [ \"$2\" = status ] && [ \"$3\" = --json ] || exit 7\n"
        f"cat <<'EOF'\n{output}\nEOF\nexit {code}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


ISO = "2026-11-06T02:56:50.971Z"


@pytest.mark.parametrize("output,code,expected", [
    (json.dumps({"loggedIn": True, "loginExpiresAt": ISO}), 0, (True, EXPIRES)),
    (json.dumps({"loggedIn": True, "loginExpiresAt": None}), 0, (True, None)),
    # Вывод CLI 2.1.318 после конца: токен стёрт, даты нет, код 1.
    (json.dumps({"loggedIn": False, "loginExpiresAt": None}), 1, (False, None)),
    (json.dumps({"loggedIn": False, "loginExpiresAt": ISO}), 0, (False, None)),
    ("not json", 0, (None, None)),
    ("not json", 1, (None, None)),
    (json.dumps([1]), 0, (None, None)),
    (json.dumps({"loggedIn": "yes", "loginExpiresAt": ISO}), 0, (None, None)),
    (json.dumps({"loggedIn": True, "loginExpiresAt": ISO}), 1, (None, None)),
    (json.dumps({"loggedIn": False}), 2, (None, None)),
])
def test_read_login_status(output, code, expected) -> None:
    with tempfile.TemporaryDirectory() as work:
        got = asyncio.run(le.read_login_status(_fake_claude(work, output, code)))
    assert got.logged_in is expected[0]
    if expected[1] is None:
        assert got.expires is None
    else:
        assert got.expires == pytest.approx(expected[1], abs=0.001)


def test_read_login_status_missing_binary() -> None:
    assert asyncio.run(le.read_login_status("/nonexistent/claude")) == le.UNKNOWN


@pytest.mark.parametrize("parent", ["sleep 60", "exit 0"])
def test_read_login_expiry_timeout_kills_group(parent: str) -> None:
    """Зависший CLI или вышедший с потомком на том же выводе: возврат за таймаут, группа добита."""
    with tempfile.TemporaryDirectory() as work:
        mark = Path(work) / "child.pid"
        path = Path(work) / "claude"
        path.write_text(f"#!/bin/sh\n(sleep 60) &\necho $! > {mark}\n{parent}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        started = time.monotonic()
        with patch.object(le, "TIMEOUT", 1):
            assert asyncio.run(le.read_login_status(str(path))) == le.UNKNOWN
        assert time.monotonic() - started < 10
        child = int(mark.read_text())
        time.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)


def _ctx(state_dir: Path, send: AsyncMock) -> SimpleNamespace:
    cfg = SimpleNamespace(claude_bin="claude", state_dir=state_dir, primary_chat_id=42)
    return SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}),
                           bot=SimpleNamespace(send_message=send, username="bot"))


def _status(expires) -> le.LoginStatus:
    """Число: вход с этой датой; None: ответа нет; готовый LoginStatus как есть."""
    if isinstance(expires, le.LoginStatus):
        return expires
    return le.UNKNOWN if expires is None else le.LoginStatus(True, expires)


def _run_job(state_dir: Path, expires, now: float, send: AsyncMock) -> None:
    fake_now = datetime.fromtimestamp(now, timezone.utc)
    with patch.object(le, "read_login_status", AsyncMock(return_value=_status(expires))), \
            patch.object(le, "local_zone", return_value=TZ), \
            patch.object(le, "datetime", wraps=datetime) as dt:
        dt.now.return_value = fake_now
        dt.fromtimestamp.side_effect = datetime.fromtimestamp
        asyncio.run(le.check_login_expiry(_ctx(state_dir, send)))


def test_job_sends_once_with_login_button() -> None:
    send = AsyncMock()
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        _run_job(state_dir, EXPIRES, EARLIEST - 2 * DAY, send)
        _run_job(state_dir, EXPIRES, EARLIEST - 2 * DAY + 60, send)
        saved = json.loads((state_dir / le.STATE_FILE).read_text(encoding="utf-8"))
    assert send.await_count == 1
    args, kwargs = send.await_args
    assert args[0] == 42 and "06.11" in args[1]
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "login:claude"
    assert saved == {"expires_at": EXPIRES, "sent": ["10", "3"]}


def test_job_silent_far_and_without_date() -> None:
    send = AsyncMock()
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        _run_job(state_dir, EXPIRES, EXPIRES - 20 * DAY, send)
        _run_job(state_dir, None, EXPIRES - DAY, send)
        _run_job(state_dir, le.LoginStatus(True, None), EXPIRES - DAY, send)
    send.assert_not_awaited()


@pytest.mark.parametrize("state", [
    {},
    {"expires_at": True, "sent": []},
    {"expires_at": "06.11", "sent": []},
    {"expires_at": float("nan"), "sent": []},
])
def test_logged_out_silent_without_date(state) -> None:
    key, new_state = le.logged_out_notice(EARLIEST + DAY, state)
    assert key is None and new_state == state


def test_logged_out_before_window_marks_left() -> None:
    """Выход вручную до окна: с открытием окна «кончился» не придёт."""
    key, state = le.logged_out_notice(EARLIEST - 3600, {"expires_at": EXPIRES, "sent": ["10"]})
    assert key is None and state == {"expires_at": EXPIRES, "sent": ["10"], "left": True}
    assert le.logged_out_notice(EARLIEST + DAY, state)[0] is None


def test_transient_logout_before_window_keeps_marks() -> None:
    """Временный loggedIn:false до окна, потом снова дата: порог «3» не повторяется."""
    _key, state = le.due_notice(EXPIRES, EARLIEST - 2 * DAY, {}, TZ)
    _key, state = le.logged_out_notice(EARLIEST - 2 * DAY + 3600, state)
    key, state = le.due_notice(EXPIRES, EARLIEST - 2 * DAY + 7200, state, TZ)
    assert key is None and state == {"expires_at": EXPIRES, "sent": ["10", "3"]}
    # Отметка снята: после стирания токена в окне «кончился» придёт.
    assert le.logged_out_notice(EARLIEST + DAY, state)[0] == "expired"


def test_logged_out_in_window_once() -> None:
    state = {"expires_at": EXPIRES, "sent": ["1", "10", "3"]}
    key, state = le.logged_out_notice(EARLIEST + 3600, state)
    assert key == "expired"
    assert state == {"expires_at": EXPIRES, "sent": ["1", "10", "3", "expired"]}
    assert le.logged_out_notice(EARLIEST + 7200, state)[0] is None
    # Отметки битые: конец всё равно объявляется.
    assert le.logged_out_notice(EXPIRES + DAY, {"expires_at": EXPIRES, "sent": "x"})[0] == "expired"


def test_job_announces_end_after_token_cleared() -> None:
    """Вход был с датой, потом CLI стёр токен: «кончился» по сохранённой дате, один раз."""
    send = AsyncMock()
    gone = le.LoginStatus(False, None)
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        _run_job(state_dir, EXPIRES, EARLIEST - DAY / 2, send)
        assert send.await_count == 1
        _run_job(state_dir, gone, EARLIEST + DAY, send)
        _run_job(state_dir, gone, EARLIEST + DAY + 60, send)
        assert send.await_count == 2
        args, kwargs = send.await_args
        assert "кончился" in args[1]
        assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "login:claude"
        # Перевход: новая дата сбрасывает отметки.
        _run_job(state_dir, EXPIRES + 30 * DAY, EARLIEST + DAY + 120, send)
        saved = json.loads((state_dir / le.STATE_FILE).read_text(encoding="utf-8"))
    assert send.await_count == 2
    assert saved == {"expires_at": EXPIRES + 30 * DAY, "sent": []}


def test_job_logged_out_without_state_is_silent() -> None:
    send = AsyncMock()
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        _run_job(state_dir, le.LoginStatus(False, None), EXPIRES, send)
        assert not (state_dir / le.STATE_FILE).exists()
    send.assert_not_awaited()


def test_job_failed_send_keeps_mark_open() -> None:
    send = AsyncMock(side_effect=RuntimeError("telegram down"))
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        _run_job(state_dir, EXPIRES, EXPIRES - DAY, send)
        saved = json.loads((state_dir / le.STATE_FILE).read_text(encoding="utf-8"))
        assert saved == {"expires_at": EXPIRES, "sent": ["10", "3"]}
        ok = AsyncMock()
        _run_job(state_dir, EXPIRES, EXPIRES - DAY + 60, ok)
    assert ok.await_count == 1


def test_job_failed_send_then_token_cleared() -> None:
    """Первая отправка не ушла, потом CLI стёр токен: дата сохранена, «кончился» придёт."""
    gone = le.LoginStatus(False, None)
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        _run_job(state_dir, EXPIRES, EARLIEST + 3600, AsyncMock(side_effect=RuntimeError("down")))
        failed = AsyncMock(side_effect=RuntimeError("down"))
        _run_job(state_dir, gone, EARLIEST + DAY, failed)
        ok = AsyncMock()
        _run_job(state_dir, gone, EARLIEST + DAY + 60, ok)
        _run_job(state_dir, gone, EARLIEST + DAY + 120, ok)
    assert failed.await_count == 1 and ok.await_count == 1
    assert "кончился" in ok.await_args.args[1]


def test_job_login_without_date_mutes_old_date() -> None:
    """Вход без срока: поздний выход не даёт «кончился» по старой дате."""
    send = AsyncMock()
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        _run_job(state_dir, EXPIRES, EARLIEST - 5 * DAY, send)
        _run_job(state_dir, le.LoginStatus(True, None), EARLIEST - 4 * DAY, send)
        saved = json.loads((state_dir / le.STATE_FILE).read_text(encoding="utf-8"))
        assert saved == {"expires_at": EXPIRES, "sent": ["10"], "left": True}
        _run_job(state_dir, le.LoginStatus(False, None), EXPIRES + 30 * DAY, send)
        # Временный ответ без даты не повторяет порог, когда дата вернулась.
        _run_job(state_dir, EXPIRES, EARLIEST - 4 * DAY + 60, send)
    assert send.await_count == 1


def test_lifecycle_schedules_jobs() -> None:
    """В lifecycle.py вызовы планировщика с этим обработчиком, а не строки в комментарии."""
    import ast
    tree = ast.parse((Path(le.__file__).parent / "lifecycle.py").read_text(encoding="utf-8"))
    jobs = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("run_daily", "run_once") and node.args
                and ast.unparse(node.args[0]) == "login_expiry.check_login_expiry"):
            kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
            jobs[kw["name"]] = (node.func.attr, kw)
    assert jobs["'login-expiry'"][0] == "run_daily"
    assert jobs["'login-expiry'"][1]["time"] == "dtime(9, 30, tzinfo=local_zone())"
    assert jobs["'login-expiry-start'"] == ("run_once", {"when": "60", "name": "'login-expiry-start'"})


def test_parallel_checks_send_once() -> None:
    """Стартовая и ежедневная проверки сошлись: одно сообщение."""
    async def slow_send(*a, **k):
        await asyncio.sleep(0.05)

    send = AsyncMock(side_effect=slow_send)
    with tempfile.TemporaryDirectory() as d:
        ctx = _ctx(Path(d), send)
        fake_now = datetime.fromtimestamp(EARLIEST - 2 * DAY, timezone.utc)

        async def both():
            await asyncio.gather(le.check_login_expiry(ctx), le.check_login_expiry(ctx))

        with patch.object(le, "read_login_status", AsyncMock(return_value=_status(EXPIRES))), \
                patch.object(le, "local_zone", return_value=TZ), \
                patch.object(le, "datetime", wraps=datetime) as dt:
            dt.now.return_value = fake_now
            dt.fromtimestamp.side_effect = datetime.fromtimestamp
            asyncio.run(both())
    assert send.await_count == 1

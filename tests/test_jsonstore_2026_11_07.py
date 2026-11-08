"""Общий модуль файлов состояния jsonstore (ревизия повторов 07.11.2026, п. 2):
атомарная запись 0600, битый UTF-8 и битый JSON не роняют и не теряются,
замки, JSONL."""
from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_tg_bot import jsonstore  # noqa: E402
from claude_tg_bot.claude_runner import SessionStore  # noqa: E402

BAD_UTF8 = b'{"a": "\xff\xfe"}'
BAD_JSON = b'{"a": '


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _corrupt(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".corrupt-*"))


# ---- запись ----

def test_write_json_0600_utf8_and_replaces_old_mode(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "s.json"
    p.parent.mkdir()
    p.write_text("{}")
    os.chmod(p, 0o644)
    jsonstore.write_json(p, {"k": "привет"}, indent=1)
    assert _mode(p) == 0o600
    assert "привет" in p.read_text(encoding="utf-8")
    assert jsonstore.read_json(p) == {"k": "привет"}
    assert [x.name for x in p.parent.iterdir()] == ["s.json"]


def test_write_json_mode_and_mkdir(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "s.json"
    jsonstore.write_json(p, [1], mode=0o640)
    assert _mode(p) == 0o640 and jsonstore.read_json(p) == [1]


def test_write_failure_cleans_tmp_and_keeps_old(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    jsonstore.write_json(p, {"old": 1})
    with patch.object(jsonstore.os, "replace", side_effect=OSError("disk")), pytest.raises(OSError):
        jsonstore.write_json(p, {"new": 1})
    assert jsonstore.read_json(p) == {"old": 1}
    assert [x.name for x in tmp_path.iterdir()] == ["s.json"]


def test_unserializable_writes_nothing(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    with pytest.raises(TypeError):
        jsonstore.write_json(p, {"x": object()})
    assert list(tmp_path.iterdir()) == []


def test_tmp_name_is_not_json(tmp_path: Path) -> None:
    p = tmp_path / "abc.json"
    seen = []
    real = jsonstore.os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        real(src, dst)
    with patch.object(jsonstore.os, "replace", spy):
        jsonstore.write_json(p, {})
    assert seen[0].startswith(".abc.json.") and seen[0].endswith(".tmp")


def test_create_json_only_once(tmp_path: Path) -> None:
    p = tmp_path / "e.json"
    assert jsonstore.create_json(p, {"n": 1}) is True
    assert jsonstore.create_json(p, {"n": 2}) is False
    assert jsonstore.read_json(p) == {"n": 1} and _mode(p) == 0o600
    assert [x.name for x in tmp_path.iterdir()] == ["e.json"]


# ---- чтение ----

def test_read_missing_empty(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    assert jsonstore.read_json(p, {"d": 1}) == {"d": 1}
    p.write_bytes(b"  \n")
    assert jsonstore.read_json(p, {}) == {}
    assert p.exists() and _corrupt(p) == []


@pytest.mark.parametrize("raw", [BAD_UTF8, BAD_JSON])
def test_read_corrupt_kept_aside(tmp_path: Path, raw: bytes) -> None:
    p = tmp_path / "s.json"
    p.write_bytes(raw)
    assert jsonstore.read_json(p, {}) == {}
    assert not p.exists()
    [kept] = _corrupt(p)
    assert kept.read_bytes() == raw


@pytest.mark.parametrize("raw", [BAD_UTF8, BAD_JSON])
def test_read_corrupt_not_moved_when_asked(tmp_path: Path, raw: bytes) -> None:
    p = tmp_path / "s.json"
    p.write_bytes(raw)
    assert jsonstore.read_json(p, None, keep_corrupt=False) is None
    assert p.read_bytes() == raw and _corrupt(p) == []


def test_read_unreadable_is_default(tmp_path: Path) -> None:
    p = tmp_path / "dir.json"
    p.mkdir()
    assert jsonstore.read_json(p, "d") == "d"
    assert p.is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root читает и файл без прав")
def test_read_unreadable_file_kept_aside(tmp_path: Path) -> None:
    """Нечитаемый файл хранилища не затирается первой записью (ревью 08.11.2026)."""
    p = tmp_path / "sessions.json"
    p.write_text('{"a": {"v": "s1", "ts": 1}}', encoding="utf-8")
    p.chmod(0)
    assert jsonstore.read_dict(p) == {}
    kept = _corrupt(p)
    assert len(kept) == 1 and not p.exists()
    jsonstore.write_json(p, {})
    kept[0].chmod(0o600)
    assert "s1" in kept[0].read_text(encoding="utf-8")


@pytest.mark.skipif(os.geteuid() == 0, reason="root читает и файл без прав")
def test_read_unreadable_not_moved_when_asked(tmp_path: Path) -> None:
    p = tmp_path / "foreign.json"
    p.write_text("{}", encoding="utf-8")
    p.chmod(0)
    assert jsonstore.read_json(p, "d", keep_corrupt=False) == "d"
    assert p.exists() and _corrupt(p) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root читает и без прав")
def test_read_unreadable_parent_dir_is_default(tmp_path: Path) -> None:
    """Каталог без x: stat тоже без прав, только умолчание, без исключения;
    каталог 000 вместо файла не откладывается (ревью Opus 08.11.2026)."""
    d = tmp_path / "st"
    d.mkdir()
    (d / "s.json").write_text("{}", encoding="utf-8")
    d.chmod(0o600)
    try:
        assert jsonstore.read_dict(d / "s.json") == {}
    finally:
        d.chmod(0o700)
    q = tmp_path / "q.json"
    q.mkdir()
    q.chmod(0)
    try:
        assert jsonstore.read_json(q, "d") == "d"
        assert q.is_dir() and _corrupt(q) == []
    finally:
        q.chmod(0o700)


def test_read_transient_error_leaves_file(tmp_path: Path, monkeypatch) -> None:
    """EIO, EMFILE: файл цел, не откладывается, только умолчание (ревью Opus 08.11.2026)."""
    import errno
    p = tmp_path / "sessions.json"
    p.write_text('{"a": 1}', encoding="utf-8")

    def broken(*a, **k):
        raise OSError(errno.EMFILE, "Too many open files")
    monkeypatch.setattr(jsonstore, "open", broken, raising=False)
    assert jsonstore.read_dict(p) == {}
    monkeypatch.undo()
    assert _corrupt(p) == [] and jsonstore.read_dict(p) == {"a": 1}


def test_keep_aside_race_restores_fresh_file(tmp_path: Path) -> None:
    """Между проверкой и переносом другой процесс записал целый файл: он
    возвращается на место, а не уходит в .corrupt (ревью Codex 08.11.2026)."""
    p = tmp_path / "s.json"
    p.write_bytes(BAD_JSON)
    real = jsonstore.keep_aside

    def racing(path):
        jsonstore.write_json(path, {"fresh": 1})  # чужая запись в окне
        return real(path)

    with patch.object(jsonstore, "keep_aside", racing):
        # Возвращённый свежий файл и прочитан: хранилище его не затрёт.
        assert jsonstore.read_dict(p) == {"fresh": 1}
    assert jsonstore.read_json(p) == {"fresh": 1}
    assert _corrupt(p) == []


def test_jsonl_append_refuses_wide_foreign_file(tmp_path: Path) -> None:
    """Чужой журнал с правами шире 0600: не дописываем (fchmod не наш)."""
    p = tmp_path / "audit.jsonl"
    p.write_text("", encoding="utf-8")
    p.chmod(0o666)
    with patch.object(jsonstore.os, "fchmod", side_effect=PermissionError):
        with pytest.raises(PermissionError):
            jsonstore.jsonl_append(p, {"a": 1})
    assert p.read_text(encoding="utf-8") == ""
    p.chmod(0o600)
    with patch.object(jsonstore.os, "fchmod", side_effect=PermissionError):
        jsonstore.jsonl_append(p, {"a": 1})
    assert jsonstore.jsonl_tail(p) == [{"a": 1}]


# ---- замки ----

def test_locked_writes_only_on_change(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    with jsonstore.locked(p) as data:
        assert data == {}
    assert not p.exists()
    with jsonstore.locked(p) as data:
        data["a"] = "б"
    assert jsonstore.read_json(p) == {"a": "б"} and _mode(p) == 0o600
    mtime = p.stat().st_mtime_ns
    with jsonstore.locked(p) as data:
        pass
    assert p.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("raw", [BAD_UTF8, BAD_JSON, b"[1]"])
def test_locked_refuses_corrupt(tmp_path: Path, raw: bytes) -> None:
    p = tmp_path / "r.json"
    p.write_bytes(raw)
    with pytest.raises(ValueError):
        with jsonstore.locked(p):
            pass
    assert p.read_bytes() == raw and _corrupt(p) == []


def test_locked_exception_no_write(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    with pytest.raises(RuntimeError):
        with jsonstore.locked(p) as data:
            data["x"] = 1
            raise RuntimeError
    assert not p.exists()


def test_locked_serializes(tmp_path: Path) -> None:
    p = tmp_path / "r.json"

    def bump():
        for _ in range(20):
            with jsonstore.locked(p) as data:
                data["n"] = data.get("n", 0) + 1
    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert jsonstore.read_json(p) == {"n": 80}


# ---- JSONL ----

def test_jsonl_append_mode_and_fixes_old(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    p.write_text("")
    os.chmod(p, 0o644)
    jsonstore.jsonl_append(p, {"a": "я"})
    jsonstore.jsonl_append(p, {"b": 2})
    assert _mode(p) == 0o600
    assert jsonstore.jsonl_tail(p) == [{"a": "я"}, {"b": 2}]


def test_jsonl_tail_skips_bad_and_partial(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    p.write_bytes(b'{"n": 1}\n[1, 2]\n"s"\nnot json\n\xff\xfe\n{"n": 2}\n{"n": 3}\n')
    assert jsonstore.jsonl_tail(p) == [{"n": 1}, {"n": 2}, {"n": 3}]
    # Хвост режет первую строку посередине: обрезок пропущен.
    assert jsonstore.jsonl_tail(p, 12) == [{"n": 3}]
    assert jsonstore.jsonl_tail(tmp_path / "none.jsonl", 10) == []


def test_jsonl_tail_whole_line_at_boundary(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    p.write_bytes(b'{"n": 1}\n{"n": 2}\n')
    assert jsonstore.jsonl_tail(p, 9) == [{"n": 2}]
    assert jsonstore.jsonl_tail(p, 10) == [{"n": 2}]


def test_jsonl_trim_keeps_whole_tail(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    for n in range(100):
        jsonstore.jsonl_append(p, {"n": n})
    assert jsonstore.jsonl_trim(p, 10_000, 100) is False
    assert jsonstore.jsonl_trim(p, 500, 100) is True
    rows = jsonstore.jsonl_tail(p)
    assert rows and rows[-1] == {"n": 99} and p.stat().st_size <= 100
    assert rows == [{"n": n} for n in range(100 - len(rows), 100)]
    assert _mode(p) == 0o600
    jsonstore.jsonl_append(p, {"n": 100})
    assert jsonstore.jsonl_tail(p)[-1] == {"n": 100}


def test_jsonl_trim_waits_for_writer_and_loses_nothing(tmp_path: Path) -> None:
    """Писатель держит замок дописывания: подрезка ждёт, его строка остаётся."""
    p = tmp_path / "j.jsonl"
    for n in range(50):
        jsonstore.jsonl_append(p, {"n": n})
    entered, release = threading.Event(), threading.Event()

    def writer():
        with jsonstore._locked_append_fd(p, 0o600) as fd:
            entered.set()
            release.wait(5)
            os.write(fd, b'{"n": "late"}\n')
    t = threading.Thread(target=writer)
    t.start()
    entered.wait(5)
    done = threading.Event()
    trimmer = threading.Thread(target=lambda: (jsonstore.jsonl_trim(p, 100, 60), done.set()))
    trimmer.start()
    time.sleep(0.2)
    assert not done.is_set()
    release.set()
    t.join(5)
    trimmer.join(5)
    assert jsonstore.jsonl_tail(p)[-1] == {"n": "late"}


# ---- SessionStore на общем модуле ----

@pytest.mark.parametrize("raw", [BAD_UTF8, BAD_JSON])
def test_session_store_corrupt_kept_and_starts(tmp_path: Path, raw: bytes) -> None:
    p = tmp_path / "sessions.json"
    p.write_bytes(raw)
    store = SessionStore(p)
    assert store.data == {}
    [kept] = _corrupt(p)
    assert kept.read_bytes() == raw
    store.set("1", "sid")
    assert _mode(p) == 0o600 and SessionStore(p).get("1") == "sid"


@pytest.mark.parametrize("raw", [b"[1]", b'"s"', b"3"])
def test_read_dict_non_object_is_corrupt(tmp_path: Path, raw: bytes) -> None:
    p = tmp_path / "s.json"
    p.write_bytes(raw)
    assert jsonstore.read_dict(p, keep_corrupt=False) == {}
    assert p.exists()
    assert jsonstore.read_dict(p) == {}
    assert not p.exists() and len(_corrupt(p)) == 1
    assert jsonstore.read_dict(p) == {}


def test_corrupt_replaced_meanwhile_not_moved(tmp_path: Path) -> None:
    """Пока читали битый, другой процесс записал целый: его не откладываем,
    а читаем (иначе хранилище затёрло бы его первой записью, ревью Codex 08.11.2026)."""
    p = tmp_path / "s.json"
    p.write_bytes(BAD_JSON)
    real = jsonstore.json.loads

    def swap(text):
        jsonstore.write_json(p, {"fresh": 1})
        return real(text)
    with patch.object(jsonstore.json, "loads", swap):
        assert jsonstore.read_json(p, {}) == {"fresh": 1}
    assert jsonstore.read_json(p) == {"fresh": 1} and _corrupt(p) == []


def test_two_corrupt_copies_same_second_kept(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_bytes(BAD_JSON)
    jsonstore.read_json(p, {})
    p.write_bytes(BAD_UTF8)
    with patch.object(jsonstore.os, "getpid", return_value=1):
        jsonstore.read_json(p, {})
    assert sorted(k.read_bytes() for k in _corrupt(p)) == sorted([BAD_JSON, BAD_UTF8])


def test_not_kept_corrupt_warned_once_per_version(tmp_path: Path, caplog) -> None:
    p = tmp_path / "s.json"
    p.write_bytes(BAD_JSON)
    with caplog.at_level("WARNING", logger=jsonstore.log.name):
        for _ in range(3):
            jsonstore.read_dict(p, keep_corrupt=False)
        assert len(caplog.records) == 1
        p.write_bytes(BAD_UTF8 + b" ")
        jsonstore.read_dict(p, keep_corrupt=False)
        assert len(caplog.records) == 2


def test_jsonl_append_after_truncated_line(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    p.write_bytes(b'{"n": 1}\n{"n": 2, "cut')
    jsonstore.jsonl_append(p, {"n": 3})
    assert jsonstore.jsonl_tail(p) == [{"n": 1}, {"n": 3}]


def test_jsonl_trim_whole_line_at_boundary(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    p.write_bytes(b'{"n": 1}\n{"n": 2}\n{"n": 3}\n')
    assert jsonstore.jsonl_trim(p, 10, 18) is True
    assert jsonstore.jsonl_tail(p) == [{"n": 2}, {"n": 3}]


def test_jsonl_append_reopens_after_file_replaced(tmp_path: Path) -> None:
    """Писатель ждал замок на старом файле, а его заменили (подрезка):
    строка ложится в новый файл, а не в отвязанный старый."""
    p = tmp_path / "j.jsonl"
    jsonstore.jsonl_append(p, {"n": "old"})
    entered, release = threading.Event(), threading.Event()

    def holder():
        with jsonstore._locked_append_fd(p, 0o600):
            entered.set()
            release.wait(5)
            new = tmp_path / "new.jsonl"
            new.write_bytes(b'{"n": "kept"}\n')
            os.replace(new, p)
    t = threading.Thread(target=holder)
    t.start()
    entered.wait(5)
    w = threading.Thread(target=lambda: jsonstore.jsonl_append(p, {"n": "late"}))
    w.start()
    time.sleep(0.2)
    release.set()
    t.join(5)
    w.join(5)
    assert jsonstore.jsonl_tail(p) == [{"n": "kept"}, {"n": "late"}]


def test_jsonl_append_gives_up_when_always_replaced(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    real_stat = jsonstore.os.stat

    def other_inode(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        return os.stat_result((st.st_mode, st.st_ino + 1, *tuple(st)[2:]))
    with patch.object(jsonstore.os, "stat", other_inode), pytest.raises(OSError):
        jsonstore.jsonl_append(p, {"n": 1})


def test_jsonl_trim_no_newline_in_tail_leaves_empty(tmp_path: Path) -> None:
    p = tmp_path / "j.jsonl"
    p.write_bytes(b'{"n": 1}\n' + b"x" * 200)
    assert jsonstore.jsonl_trim(p, 100, 50) is True
    assert p.read_bytes() == b""
    jsonstore.jsonl_append(p, {"n": 2})
    assert jsonstore.jsonl_tail(p) == [{"n": 2}]

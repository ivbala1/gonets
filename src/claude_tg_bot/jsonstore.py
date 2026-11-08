"""Файлы состояния в JSON и JSONL: атомарная запись, чтение битого, замки.

Один способ на весь бот (ревизия повторов 07.11.2026, п. 2): до него было
около 16 копий записи и 15 копий чтения с разными правилами.

Запись (`write_text`, `write_json`, `create_json`): временный файл рядом
(mkstemp, права 0600), затем `os.replace` или ссылка; временный убирается при
любом сбое. fsync нет, как и раньше: пустой или обрезанный файл после сбоя
питания читается как битый (ниже).

Чтение (`read_json`): нет файла, пустой файл, не прочитался (права), не UTF-8
или не JSON это `default`, бот не падает. `UnicodeDecodeError` наследник
`ValueError`, но не `JSONDecodeError`, поэтому ловится `ValueError`. Битый
или без прав на чтение файл, который потом перезапишется, откладывается рядом как
`<имя>.corrupt-<время>` (`keep_corrupt`, сделано 31.10 для сессий): иначе
первая запись стёрла бы его без следа. Чужие файлы (`.credentials.json`) и
реестры с отказом на битом (`tasks.locked`) читаются с `keep_corrupt=False`.

Замки: `file_lock` (flock на файл-замок), `locked` (словарь под замком,
запись только при изменении, битый файл это отказ, а не пусто).

JSONL: `jsonl_append` дописывает строку под flock самого файла и
`jsonl_trim` подрезает его под тем же замком, поэтому дописывание из другого
процесса во время подрезки не теряется. `jsonl_tail` читает хвост, битые
строки и не-словари пропускает.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

MODE = 0o600


def _tmp_for(path: Path, mode: int) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Имя временного не кончается на .json: обходы каталогов по `*.json`
    # (отклики, передачи, события) его не примут за запись.
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    if mode != MODE:
        try:
            os.fchmod(fd, mode)
        except BaseException:
            os.close(fd)
            Path(tmp).unlink(missing_ok=True)
            raise
    return fd, tmp


def _write_tmp(path: Path, data: str, mode: int) -> str:
    fd, tmp = _tmp_for(path, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return tmp


def write_text(path: Path, data: str, *, mode: int = MODE) -> None:
    """Атомарно заменить файл текстом (utf-8)."""
    path = Path(path)
    tmp = _write_tmp(path, data, mode)
    try:
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dumps(obj: Any, *, indent: int | None = None) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent)


def write_json(path: Path, obj: Any, *, mode: int = MODE, indent: int | None = None) -> None:
    """Атомарно записать JSON (utf-8, без экранирования кириллицы)."""
    write_text(path, dumps(obj, indent=indent) + "\n", mode=mode)


def create_json(path: Path, obj: Any, *, mode: int = MODE, indent: int | None = None) -> bool:
    """Записать JSON, только если файла ещё нет (атомарно, ссылкой).
    False: файл уже есть, он не тронут."""
    path = Path(path)
    tmp = _write_tmp(path, dumps(obj, indent=indent) + "\n", mode)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        Path(tmp).unlink(missing_ok=True)
    return True


def keep_aside(path: Path) -> Path:
    """Отложить файл рядом как `<имя>.corrupt-<время>`; не вышло: он сам.
    Имя с номером процесса: два отложенных в одну секунду друг друга не затрут."""
    path = Path(path)
    kept = path.with_name(f"{path.name}.corrupt-{int(time.time())}-{os.getpid()}")
    try:
        os.replace(path, kept)
    except OSError:
        return path
    return kept


def _keep_aside_if_same(path: Path, seen: os.stat_result) -> Path:
    """Отложить, только если по пути всё тот же прочитанный файл: другой
    процесс мог уже отложить битый и записать целый, его не трогаем. Зовётся
    при открытом прочитанном файле: его номер не достанется новому."""
    try:
        now = os.stat(path)
    except OSError:
        return path
    sig = (seen.st_ino, seen.st_dev, seen.st_mtime_ns, seen.st_size)
    if (now.st_ino, now.st_dev, now.st_mtime_ns, now.st_size) != sig:
        return path
    kept = keep_aside(path)
    if kept == path:
        return path
    try:
        moved = os.stat(kept)
    except OSError:
        return kept
    if (moved.st_ino, moved.st_dev) != sig[:2]:
        # Между проверкой и переносом другой процесс записал целый файл, и
        # отложился он: вернуть на место, если путь ещё свободен (ревью
        # Codex 08.11.2026). Занят: целый остаётся рядом, след не теряется.
        try:
            os.link(kept, path)
        except OSError:
            log.warning("state file %s: a fresh copy was moved aside as %s by a race", path, kept)
            return kept
        Path(kept).unlink(missing_ok=True)
        return path
    return kept


# Предупреждение о неотложенном битом файле (keep_corrupt=False) один раз на
# его версию: такие файлы читаются на каждое сообщение и в обходах каталогов.
_warned: dict[str, tuple] = {}


def _warn(path: Path, seen: os.stat_result | None, msg: str, *args: Any) -> None:
    sig = (seen.st_ino, seen.st_mtime_ns, seen.st_size) if seen is not None else None
    if sig is not None and _warned.get(str(path)) == sig:
        return
    if sig is not None:
        _warned[str(path)] = sig
    log.warning(msg, *args)


_MISSING = object()


def _reread(path: Path, kept: Path, want_dict: bool) -> Any:
    """После откладывания: если по пути уже свежий файл другого процесса (он
    сменил файл до переноса или возвращён на место), прочесть его, а не
    отдать умолчание: иначе хранилище затёрло бы его первой записью (ревью
    Codex 08.11.2026). Без нового откладывания: один повтор, без цикла."""
    if kept != path or not path.exists():
        return _MISSING
    return _load(path, False, want_dict)


def _unreadable(path: Path, e: OSError, keep_corrupt: bool, want_dict: bool) -> Any:
    # Нет прав на чтение (устойчивое состояние) с keep_corrupt: отложить, как
    # битый, иначе первая запись хранилища (сессии, настройки) заменила бы его
    # без следа; раньше SessionStore на таком файле не стартовал. Временные
    # сбои (EIO, EMFILE, ENOMEM) файл не трогают: он цел (ревью 08.11.2026).
    if not (keep_corrupt and isinstance(e, PermissionError)):
        log.warning("state file %s unreadable (%s), using default", path, type(e).__name__)
        return _MISSING
    try:
        seen = os.stat(path)
    except OSError:  # и stat без прав (каталог без x): только умолчание
        log.warning("state file %s unreadable (%s), using default", path, type(e).__name__)
        return _MISSING
    if not stat.S_ISREG(seen.st_mode):  # каталог 000 и прочее не откладывать
        log.warning("state file %s unreadable (%s), using default", path, type(e).__name__)
        return _MISSING
    try:
        # Второе открытие после stat: файл мог смениться целым до stat, тогда
        # он читается и откладывать нечего; не открылся: по пути всё тот же
        # нечитаемый, сверка ниже ловит замену после stat.
        os.close(os.open(path, os.O_RDONLY))
    except PermissionError:
        kept = _keep_aside_if_same(path, seen)
        log.warning("state file %s unreadable (%s), kept as %s, using default", path, type(e).__name__, kept)
        return _reread(path, kept, want_dict)
    except OSError:
        log.warning("state file %s unreadable (%s), using default", path, type(e).__name__)
        return _MISSING
    return _load(path, False, want_dict)


def _load(path: Path, keep_corrupt: bool, want_dict: bool) -> Any:
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return _MISSING
    except OSError as e:
        return _unreadable(path, e, keep_corrupt, want_dict)
    with f:
        try:
            seen = os.fstat(f.fileno())
            raw = f.read()
        except OSError as e:
            return _unreadable(path, e, keep_corrupt, want_dict)
        data = _parse(path, raw, seen, keep_corrupt, want_dict)
    if isinstance(data, Path):  # битый отложен (или нет): по пути может быть свежий
        return _reread(path, data, want_dict)
    return data


def _parse(path: Path, raw: bytes, seen: os.stat_result, keep_corrupt: bool, want_dict: bool) -> Any:
    """Данные, `_MISSING` или, если битый откладывали, путь, где он теперь
    (сам `path`: не отложен, по пути уже другой файл)."""
    if not raw.strip():
        return _MISSING
    try:
        data = json.loads(raw.decode("utf-8"))
        if want_dict and not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except ValueError as e:
        if keep_corrupt:
            kept = _keep_aside_if_same(path, seen)
            log.warning("state file %s is corrupt (%s), kept as %s, using default", path, e.__class__.__name__, kept)
            return kept
        _warn(path, seen, "state file %s is corrupt (%s), using default", path, e.__class__.__name__)
        return _MISSING


def read_json(path: Path, default: Any = None, *, keep_corrupt: bool = True) -> Any:
    """Разобранный JSON или `default` (нет файла, пустой, не читается, битый).
    keep_corrupt: битый или без прав на чтение отложить рядом, чтобы
    следующая запись его не стёрла (временный сбой чтения не откладывается).
    Тип содержимого не проверяется."""
    data = _load(Path(path), keep_corrupt, want_dict=False)
    return default if data is _MISSING else data


def read_dict(path: Path, *, keep_corrupt: bool = True) -> dict:
    """Как read_json, но ждём словарь: иной JSON (список, строка) тоже битый,
    результат всегда dict ({} вместо нет, пусто, битый)."""
    data = _load(Path(path), keep_corrupt, want_dict=True)
    return {} if data is _MISSING else data


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Исключительный flock на файл-замок на время блока (между процессами)."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(path: Path, *, lock_path: Path | None = None, indent: int | None = 1) -> Iterator[dict]:
    """Словарь из файла под замком: блок меняет его, запись при изменении.
    Нечитаемый файл или не словарь это отказ (ValueError/OSError), а не пустой:
    иначе запись затёрла бы всё, что в нём было. Исключение в блоке: не пишем."""
    path = Path(path)
    with file_lock(lock_path or path.with_name(path.name + ".lock")):
        data = json.loads(path.read_bytes().decode("utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: не словарь")
        before = dumps(data, indent=indent)
        yield data
        after = dumps(data, indent=indent)
        if after != before:
            write_text(path, after, mode=MODE)


# ---- JSONL ----

@contextlib.contextmanager
def _locked_append_fd(path: Path, mode: int) -> Iterator[int]:
    """Дескриптор на дописывание под flock, на том же файле, что сейчас лежит
    по пути: подрезка заменяет файл, и замок старого уже не тот."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, mode)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                same = os.fstat(fd).st_ino == os.stat(path).st_ino
            except FileNotFoundError:
                same = False
            if same:
                yield fd
                return
        finally:
            os.close(fd)
    raise OSError(f"{path}: файл всё время заменяется")


def jsonl_append(path: Path, rec: Any, *, mode: int = MODE) -> None:
    """Дописать строку JSON. Права mode ставятся и старому файлу (журнал
    может нести секреты из ввода инструментов). Сбой это OSError."""
    path = Path(path)
    data = (dumps(rec) + "\n").encode("utf-8")
    with _locked_append_fd(path, mode) as fd:
        try:
            os.fchmod(fd, mode)
        except PermissionError:
            # Чужой файл: права не наши. Открыт шире нужного: не пишем, журнал
            # может нести секреты из ввода инструментов (ревью Codex 08.11.2026).
            if os.fstat(fd).st_mode & 0o777 & ~mode:
                raise
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            # Файл кончается обрезком (сбой посреди записи): новая строка
            # не должна к нему приклеиться и пропасть вместе с ним.
            data = b"\n" + data
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]


def jsonl_trim(path: Path, max_bytes: int, keep_bytes: int, *, mode: int = MODE) -> bool:
    """Больше max_bytes: оставить последние keep_bytes целыми строками.
    Под замком дописывания. True: подрезан. Нет файла: False."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= max_bytes:
        return False
    with _locked_append_fd(path, mode) as fd:
        size = os.fstat(fd).st_size
        if size <= max_bytes:
            return False
        start = max(0, size - keep_bytes)
        with open(path, "rb") as f:
            # С байта раньше, как jsonl_tail: целая первая строка не теряется.
            f.seek(max(0, start - 1))
            tail = f.read()
        cut = tail.find(b"\n") + 1 if start > 0 else 0
        if start > 0 and cut == 0:
            cut = len(tail)  # в хвосте ни одной целой строки: оставить пусто
        tmp_fd, tmp = _tmp_for(path, mode)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(tail[cut:])
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return True


def jsonl_tail(path: Path, max_bytes: int | None = None) -> list[dict]:
    """Записи-словари из последних max_bytes файла (None: весь), по порядку.
    Обрезанная первая строка хвоста, битые строки и не-словари пропускаются.
    Нет файла или не читается: пусто."""
    try:
        with open(path, "rb") as f:
            start = 0
            if max_bytes is not None:
                start = max(0, f.seek(0, os.SEEK_END) - max_bytes)
                # С байта раньше: если там конец строки, первая строка хвоста
                # целая и не теряется.
                f.seek(max(0, start - 1))
            raw = f.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if start > 0:
        lines = lines[1:]  # до первого конца строки: обрезок или пусто
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out

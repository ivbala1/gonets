"""Кнопка «📌 В список»: модель выбирает проект, код дописывает пункт в его
BACKLOG.md (раздел «Из новостей», у задачи из /task «Из задач») и отправляет
коммит одного этого файла.
Решение Ивана 13.10.2026: накопительных списков нет, у каждого пункта свой
проект; нет бэклога, он заводится с пометкой «разобрать в приоритете».
Проекты в backlogs.json: путь к файлу выбирает код, от модели только ключ."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ...i18n import _
from .core import Item, add_backlog_line, backlog_prompt, new_backlog_text, parse_backlog_choice

BACKLOGS_FILE = Path(__file__).resolve().parent / "backlogs.json"
CHOICE_TIMEOUT = 120
GIT_TIMEOUT = 60
# Ветка, куда идёт строка бэклога: у всех проектов из backlogs.json это main
# (проект шага 3, 1.8, п. 7).
MAIN = "main"


@dataclass
class Filed:
    status: str  # added | dup | none | error
    name: str = ""
    note: str = ""  # пояснение модели или что не вышло с git


def load_projects(path: Path = BACKLOGS_FILE) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [p for p in data if Path(p["path"]).expanduser().is_dir()]


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, **kw)


def write_and_commit(repo: Path, name: str, item: Item, today: str, lock_dir: Path) -> Filed:
    """Запись под замком на репозиторий. В коммит идёт только BACKLOG.md; если
    в нём уже были незакоммиченные правки или рабочая копия не на main, пункт
    дописывается, но коммита нет: чужие правки и ветки не трогаем."""
    path = repo / "BACKLOG.md"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"backlog-{repo.name}.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        dirty = bool(_git(repo, "status", "--porcelain", "--", "BACKLOG.md").stdout.strip())
        # Только в main: соседняя сессия могла переключить рабочую копию на
        # свою ветку под PR, строка бэклога ушла бы с ней (ревью 14.10).
        on_main = _git(repo, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip() == MAIN
        parent = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{MAIN}").stdout.strip()
        created = not path.is_file()
        via = "кнопкой «В бэклог» из темы задачи" if item.kind == "task" else "кнопкой «В список» из новостей"
        current = new_backlog_text(name, today, via) if created else path.read_text(encoding="utf-8")
        updated = add_backlog_line(current, item, today)
        if updated is None:
            return Filed("dup", name)
        path.write_text(updated, encoding="utf-8")
        if dirty:
            return Filed("added", name, _("не закоммичено: в BACKLOG.md были чужие правки"))
        if not on_main:
            return Filed("added", name, _("не закоммичено: репозиторий не на {branch}").format(branch=MAIN))
        # Хвост по источнику: задача из /task не новость (проект шага 3, 1.7).
        msg = f"Бэклог: {item.title[:60]} ({'задача из /task' if item.kind == 'task' else 'из новостей'})"
        before = ours = ""
        try:
            before = _index_entry(repo)
            if _git(repo, "add", "--", "BACKLOG.md").returncode != 0:
                return Filed("added", name, _("git add не прошёл, файл записан"))
            ours = _index_entry(repo)
            sha = _commit(repo, parent, msg)
        except (OSError, subprocess.SubprocessError) as e:
            _unstage(repo, before, ours or _staged_by_us(repo, before))
            return Filed("added", name, _("git commit: {error}").format(error=type(e).__name__))
        if sha is None:
            _unstage(repo, before, ours)
            return Filed("added", name, _("git commit не прошёл, файл записан"))
        try:
            target = _own_commit_target(repo, sha)
            if target is None:
                return Filed("added", name, _("закоммичено, не отправлено"))
            remote, merge = target
            proc = _git(repo, "push", "-q", remote, f"{sha}:{merge}")
        except (OSError, subprocess.SubprocessError) as e:
            return Filed("added", name, _("git push: {error}").format(error=type(e).__name__))
        if proc.returncode != 0:
            return Filed("added", name, _("git push не прошёл, файл записан"))
        return Filed("added", name)


def _index_entry(repo: Path) -> str:
    """Запись BACKLOG.md в настоящем индексе: «режим blob стадия\tпуть» или пусто."""
    return _git(repo, "ls-files", "-s", "--", "BACKLOG.md").stdout.strip()


def _staged_by_us(repo: Path, before: str) -> str:
    """Запись BACKLOG.md в индексе после сбоя, когда `ours` не успели снять
    (таймаут git add мог проиндексировать файл): наша, только если она новее
    прежней и её blob равен файлу в рабочем дереве."""
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        entry = _index_entry(repo)
        if entry and entry != before:
            blob = _git(repo, "hash-object", "--", "BACKLOG.md").stdout.strip()
            if entry.split("\t", 1)[0].split()[1] == blob:
                return entry
    return ""


def _unstage(repo: Path, before: str, ours: str) -> None:
    """Сорвавшийся коммит (main сдвинулась): запись BACKLOG.md в индексе
    возвращается к той, что была до бота, иначе строка бота в индексе
    блокировала бы следующие автокоммиты. Только если там всё ещё запись
    бота: проиндексированное владельцем в это окно не снимается, чужое в
    индексе и файл не трогаются (ревью Codex 14.10)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        if not ours or _index_entry(repo) != ours:
            return
        if not before:
            _git(repo, "rm", "-q", "--cached", "--", "BACKLOG.md")
            return
        mode, blob, _unused = before.split("\t", 1)[0].split()
        _git(repo, "update-index", "--cacheinfo", f"{mode},{blob},BACKLOG.md")


def _commit(repo: Path, parent: str, msg: str) -> str | None:
    """Коммит одного BACKLOG.md поверх parent (пусто: первый коммит) и сдвиг
    main на него, только если main всё ещё parent. Возвращает sha своего
    коммита или None. sha из commit-tree, а не вершина main после коммита:
    соседняя операция, заменившая коммит (commit --amend с тем же родителем),
    выдала бы свой за него, и на сервер ушёл бы он (ревью Codex 14.10).
    Дерево во временном индексе, как у commit --only: чужое из настоящего
    индекса в коммит не идёт."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        steps = (("read-tree", parent) if parent else ("read-tree", "--empty"),
                 ("update-index", "--add", "--", "BACKLOG.md"))
        for step in steps:
            if _git(repo, *step, env=env).returncode != 0:
                return None
        tree = _git(repo, "write-tree", env=env)
    if tree.returncode != 0:
        return None
    commit = _git(repo, "commit-tree", tree.stdout.strip(), *(("-p", parent) if parent else ()), "-m", msg)
    sha = commit.stdout.strip()
    if commit.returncode != 0 or not sha:
        return None
    # Со старым значением: если main ушла с parent (чужой коммит после
    # чтения parent), свой коммит на parent выкинул бы чужой из main.
    if _git(repo, "update-ref", "-m", f"commit: {msg}", f"refs/heads/{MAIN}", sha, parent).returncode != 0:
        return None
    return sha


def _own_commit_target(repo: Path, sha: str) -> tuple[str, str] | None:
    """Куда отправить свой коммит sha: (сервер, ветка сервера), если с ним main
    впереди своей ветки сервера ровно на 1 (@{u}, проект шага 3, 1.8). Иначе
    None: голый push увёз бы чужие неотправленные коммиты, в том числе уже
    лежащие на сервере в другой ветке. Отправляется ровно этот sha, так что
    коммит соседней сессии после проверки не уедет. Ветка своя и на сервере
    только main, а не текущая ветка рабочей копии (ревью Codex 14.10). Нет
    upstream (пустой сервер, ветка без отслеживания): не отправляем (ревью
    14.10)."""
    remote = _git(repo, "config", f"branch.{MAIN}.remote").stdout.strip()
    merge = _git(repo, "config", f"branch.{MAIN}.merge").stdout.strip()
    if remote in ("", ".") or merge != f"refs/heads/{MAIN}":
        return None
    ahead = _git(repo, "rev-list", "--count", f"{MAIN}@{{u}}..{sha}")
    if ahead.returncode != 0 or ahead.stdout.strip() != "1":
        return None
    return remote, merge


def file_item(item: Item, today: str, lock_dir: Path, *, projects_file: Path = BACKLOGS_FILE) -> Filed:
    """Выбрать проект и записать. Вызывать в потоке: модель отвечает секунды."""
    from .__main__ import DEFAULT_MODEL, run_claude  # там же безопасный запуск claude

    projects = load_projects(projects_file)
    model = os.environ.get("NEWS_BACKLOG_MODEL", "").strip() or os.environ.get("NEWS_DIGEST_MODEL", "").strip() or DEFAULT_MODEL
    try:
        answer = run_claude(backlog_prompt(item, projects), CHOICE_TIMEOUT, model)
    except RuntimeError as e:
        return Filed("error", note=str(e)[:200])
    key, why = parse_backlog_choice(answer, {p["key"] for p in projects})
    if key is None:
        return Filed("none", note=why)
    proj = next(p for p in projects if p["key"] == key)
    try:
        return write_and_commit(Path(proj["path"]).expanduser(), proj["name"], item, today, lock_dir)
    except OSError as e:
        return Filed("error", proj["name"], _("запись: {error}").format(error=type(e).__name__))

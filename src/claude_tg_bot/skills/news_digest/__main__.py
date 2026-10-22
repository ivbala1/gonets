"""Утренняя подборка полезного: новое в Claude, Codex, Cursor, плагины и репозитории → тема «Новости».

Зачем: Иван узнаёт о новых функциях своих инструментов поздно и из общих
новостей. Это не лента: в подборку идёт только то, что он может применить у
себя (решение Ивана 07.10.2026: «всё подряд мне есть где почитать»).

Порядок:
  1. Код читает источники из sources.json (ленты Atom/RSS, страницы без
     ленты по разнице со вчерашним снимком, поиск GitHub, первая страница
     Hacker News) и отбрасывает виденное и старое.
  2. claude -p без единого инструмента и MCP по profile.md выбирает до пяти
     пунктов и отвечает JSON; ссылки код берёт из кандидатов, не из ответа.
  3. Каждый пункт уходит отдельным тихим сообщением (HTML, без превью) в тему
     NEWS_TG_CHAT_ID/NEWS_TG_THREAD_ID с кнопками «В список» (бот дописывает
     пункт в список Домового) и «Мимо» (отзыв для следующих отборов). Решения
     Ивана 08.10.2026: по одному сообщению разом, все тихие, в пустой день
     ничего, учиться только на «мимо»; кнопку «Прикрутить» убрали: ставить
     что-то агент темы может только по ответу Ивана на пункт.
  4. Выпуск (с номерами сообщений) лежит в state/news_digest/digests, последняя
     подборка и отсеянное в state/news_digest/last.md. Отметка «видел» и
     снимки страниц сохраняются только для дошедшего: упал claude или
     Telegram, завтра те же записи придут снова.

Полезного нет: сообщений нет. Сбой: одна строка «Новости не собрались» в
тему. Источник молчит три прогона подряд: отдельная строка.
--dry-run печатает пункты и кнопки, ничего не шлёт и состояние не трогает.
--preview шлёт в тему по-настоящему, но «видел» не отмечает (проба вида).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from ...config import local_zone
from ...i18n import _
from ...notify import send_message
from ...dashes import soften_dashes
from .._oneshot import resolve_claude_bin, run_claude_oneshot
from .core import (
    NOTHING,
    Candidate,
    State,
    build_prompt,
    item_buttons,
    load_state,
    mark_seen,
    news_dir,
    page_candidate,
    parse_answer,
    parse_feed,
    parse_github_search,
    parse_hn,
    recent_feedback,
    render_item,
    save_digest,
    save_state,
    select_new,
    set_message_id,
    write_last,
)

SKILL_DIR = Path(__file__).resolve().parent
REPO_ROOT = SKILL_DIR.parents[3]
HOOK_SETTINGS = REPO_ROOT / "claude-hook-settings.json"
SOURCES_FILE = SKILL_DIR / "sources.json"
PROFILE_FILE = SKILL_DIR / "profile.md"
TZ = local_zone()
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) claude-tg-bot news-digest"
DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_SEC = 600
FIRST_RUN_HOURS = 48
WINDOW_DAYS = 7
INPUT_BUDGET = 60_000
FAIL_NOTICE_STREAK = 3


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else None


def state_root() -> Path:
    raw = os.environ.get("CLAUDE_TG_BOT_STATE_DIR", "").strip()
    return news_dir(Path(raw) if raw else REPO_ROOT / "state")


def fetch(client: httpx.Client, src: dict, state: State, now: datetime) -> tuple[list[Candidate], list[str] | None]:
    """Кандидаты одного источника и новый снимок (только у страниц)."""
    kind = src["type"]
    if kind == "feed":
        r = client.get(src["url"])
        r.raise_for_status()
        return parse_feed(r.text, src), None
    if kind == "page":
        r = client.get(src["url"])
        r.raise_for_status()
        cand, snap = page_candidate(r.text, src, state, is_html=src.get("format", "html") == "html")
        return ([cand] if cand else []), snap
    if kind == "github_search":
        since = (now - timedelta(days=int(src.get("days", WINDOW_DAYS)))).date().isoformat()
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("NEWS_GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = client.get("https://api.github.com/search/repositories", headers=headers, params={
            "q": f"{src['q']} created:>{since}", "sort": "stars", "order": "desc",
            "per_page": int(src.get("limit", 8)),
        })
        r.raise_for_status()
        return parse_github_search(r.json(), src), None
    if kind == "hn":
        r = client.get("https://hn.algolia.com/api/v1/search", params={"tags": "front_page", "hitsPerPage": 60})
        r.raise_for_status()
        return parse_hn(r.json(), src), None
    raise ValueError(f"неизвестный тип источника {kind!r}")


def installed_summary() -> str:
    """Что стоит на этой машине: плагины и моды Claude Code, скиллы, версии."""
    home = Path.home()
    lines = []
    with contextlib.suppress(OSError, ValueError, AttributeError):
        data = json.loads((home / ".claude/plugins/installed_plugins.json").read_text(encoding="utf-8"))
        names = sorted((data.get("plugins") or data).keys())
        lines.append(_("Плагины и моды Claude Code на ПК: {names}").format(names=", ".join(names) or _("нет")))
    with contextlib.suppress(OSError):
        skills = sorted(p.name for p in (home / ".claude/skills").iterdir() if (p / "SKILL.md").is_file())
        lines.append(_("Свои скиллы: {skills}").format(skills=", ".join(skills)))
    for name, cmd in (("Claude Code", "claude"), ("Codex CLI", "codex")):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
            if out:
                lines.append(f"{name}: {out.splitlines()[0]}")
    return "\n".join(lines)


def run_claude(prompt: str, timeout: int, model: str) -> str:
    """Ран без единого инструмента и MCP, как у chat_digest: во входе чужой
    текст из интернета, читать и писать claude здесь нечего."""
    args = [
        resolve_claude_bin(), "-p",
        "--output-format", "json",
        "--permission-mode", "plan",
        "--tools", "",
        "--no-session-persistence",
        "--settings", str(HOOK_SETTINGS),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--model", model,
    ]
    try:
        proc = run_claude_oneshot(args, input=prompt.encode("utf-8"), timeout=timeout, cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        raise RuntimeError(_("claude не уложился в {seconds} с").format(seconds=timeout)) from None
    except OSError as e:
        raise RuntimeError(_("claude не запустился: {error}").format(error=type(e).__name__)) from None
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        kind = (_("лимит или доступ") if any(k in stderr for k in ("429", "401", "rate", "limit"))
                else _("ошибка запуска"))
        raise RuntimeError(_("claude exit {rc}: {kind}").format(rc=proc.returncode, kind=kind))
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(_("claude stdout не JSON: {error}").format(error=e)) from e
    if payload.get("is_error"):
        raise RuntimeError(_("claude вернул is_error=true"))
    # Тире на дефис здесь: хук против тире в плановых ранах бота молчит, его
    # строка «Исправлено» подменила бы сводку (решение Ивана 22.10.2026).
    return soften_dashes((payload.get("result") or "").strip())


def _send(chat: int, thread: int, text: str, rows: list | None = None, *, html: bool = False) -> int | None:
    """message_id отправленного или None. Все сообщения темы тихие (решение Ивана 08.10)."""
    argv = ["--chat-id", str(chat), "--thread-id", str(thread), "--silent", "--no-preview"]
    if html:
        argv.append("--html")
    if rows:
        argv += ["--buttons", json.dumps(rows, ensure_ascii=False)]
    rc, mid = send_message([*argv, "--", text])
    return mid if rc == 0 else None


def _fail_line(reason: str) -> str:
    return _("Новости не собрались: {reason}").format(reason=reason)


@contextlib.contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_tg_bot.skills.news_digest")
    parser.add_argument("--dry-run", action="store_true", help="напечатать подборку, не слать и состояние не трогать")
    parser.add_argument("--since", type=int, default=None, metavar="ЧАСОВ",
                        help="считать новым всё невиденное за столько часов (по умолчанию неделя, первый прогон 48)")
    parser.add_argument("--print-prompt", action="store_true", help="с --dry-run: напечатать и промпт")
    parser.add_argument("--preview", action="store_true",
                        help="отправить в тему по-настоящему, но не отмечать виденным (проба вида)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    load_dotenv(REPO_ROOT / ".env")
    chat, thread = _int_env("NEWS_TG_CHAT_ID"), _int_env("NEWS_TG_THREAD_ID")
    if not args.dry_run and (chat is None or thread is None):
        # Fail-closed: не в личку и не в чужую тему.
        print(_("news_digest: нужны NEWS_TG_CHAT_ID и NEWS_TG_THREAD_ID в .env (пара)"), file=sys.stderr)
        return 1
    base = state_root()
    state_file = base / "state.json"

    with _lock(base / "run.lock") as locked:
        if not locked:
            print(_("news_digest: другой прогон ещё идёт, этот пропущен"))
            return 0
        state = load_state(state_file)
        now = datetime.now(timezone.utc)
        now_local = now.astimezone(TZ)
        hours = args.since if args.since is not None else (FIRST_RUN_HOURS if state.first_run else WINDOW_DAYS * 24)
        floor = now - timedelta(hours=hours)

        sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]
        cands: list[Candidate] = []
        snapshots: dict[str, list[str]] = {}
        failed: list[str] = []
        with httpx.Client(timeout=25.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            for src in sources:
                try:
                    got, snap = fetch(client, src, state, now)
                except Exception as e:  # noqa: BLE001 - один источник не роняет остальные
                    print(f"news_digest: {src['name']}: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
                    failed.append(src["name"])
                    continue
                cands.extend(got)
                if snap is not None:
                    snapshots[src["name"]] = snap
        streaks = {s["name"]: (state.fail_streak.get(s["name"], 0) + 1 if s["name"] in failed else 0) for s in sources}
        long_failed = [n for n, k in streaks.items() if k == FAIL_NOTICE_STREAK]

        held: set[str] = set()  # страницы, чьи новые строки модель сегодня не видела

        def commit(picked: list[Candidate], lost_sources: frozenset[str] = frozenset()) -> None:
            """Отметить виденным и сохранить снимки страниц. Снимок страницы, чей
            пункт не дошёл или не влез в бюджет промпта, не двигается: иначе её
            новые строки пропали бы навсегда."""
            if args.dry_run or args.preview:
                return
            mark_seen(state, picked, now_local.date().isoformat(), now)
            keep_old = held | lost_sources
            state.snapshots.update({k: v for k, v in snapshots.items() if k not in keep_old})
            state.fail_streak = {n: k for n, k in streaks.items() if k}
            save_state(state_file, state)

        if len(failed) == len(sources):
            msg = _fail_line(_("не прочитался ни один источник. Сеть ПК?"))
            print(msg, file=sys.stderr)
            if not args.dry_run:
                _send(chat, thread, msg)
                commit([])
            return 1
        if long_failed and not args.dry_run:
            _send(chat, thread, _("Новости: три прогона подряд не читаются {sources}. "
                                  "Похоже, поменялся адрес или формат, поправить sources.json.").format(
                                      sources=', '.join(long_failed)))

        picked = select_new(cands, state, floor, INPUT_BUDGET)
        picked_ids = {c.id for c in picked}
        held.update(c.source for c in cands
                    if c.source in snapshots and c.id not in picked_ids and c.id not in state.seen)
        print(_("news_digest: источников {sources}, не прочитаны {failed}, "
                "кандидатов {candidates}, новых {new}, граница {floor}Z").format(
                    sources=len(sources), failed=len(failed), candidates=len(cands), new=len(picked),
                    floor=f"{floor:%Y-%m-%d %H:%M}"))
        if not picked:
            commit([])
            return 0

        prompt = build_prompt(PROFILE_FILE.read_text(encoding="utf-8"), installed_summary(),
                              picked, floor, f"{now_local:%Y-%m-%d}", recent_feedback(base))
        if args.dry_run and args.print_prompt:
            print(prompt + "\n\n=====\n")
        model = os.environ.get("NEWS_DIGEST_MODEL", "").strip() or DEFAULT_MODEL
        timeout = _int_env("NEWS_DIGEST_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC
        try:
            raw = run_claude(prompt, timeout, model)
            items = [] if raw.strip() == NOTHING else parse_answer(raw, picked)
        except (RuntimeError, ValueError) as e:
            print(f"news_digest: {e}", file=sys.stderr)
            if not args.dry_run:
                _send(chat, thread, _fail_line(_("{error}. Завтра попробую снова с тем же.").format(error=str(e)[:200])))
            return 1

        digest_id = now_local.strftime("%Y%m%d%H%M")
        used = {r for i in items for r in i.refs}
        dropped = [c for c in picked if c.id not in used]
        if not items:
            print(_("news_digest: полезного нет, сообщений не будет"))
            if not args.dry_run:
                write_last(base, digest_id, [], dropped)
            commit(picked)
            return 0
        if args.dry_run:
            for item in items:
                print(render_item(item))
                print(_("кнопки: {buttons}").format(
                    buttons=" | ".join(b["text"] for row in item_buttons(item, digest_id) for b in row)) + "\n")
            print(_("news_digest: dry-run, {count} пунктов не отправлены, отсеяно {dropped}").format(
                count=len(items), dropped=len(dropped)))
            return 0
        # Выпуск пишется до отправки: кнопку могут нажать сразу после первого сообщения.
        save_digest(base, digest_id, items, dropped, now)
        delivered = []
        for item in items:
            mid = _send(chat, thread, render_item(item), item_buttons(item, digest_id), html=True)
            if mid is None:
                break
            set_message_id(base, digest_id, item.n, mid)
            delivered.append(item)
        write_last(base, digest_id, delivered, dropped, items[len(delivered):])
        if len(delivered) < len(items):
            lost = {r for i in items[len(delivered):] for r in i.refs}
            print(_("news_digest: доставлено {delivered} из {count}, остальное завтра").format(
                delivered=len(delivered), count=len(items)), file=sys.stderr)
            _send(chat, thread, _fail_line(_("дошло {delivered} из {count}, остальное пришлю завтра.").format(
                delivered=len(delivered), count=len(items))))
            commit([c for c in picked if c.id not in lost],
                   frozenset(c.source for c in picked if c.id in lost))
            return 1
        commit(picked)
        note = _(", --preview: виденным не отмечено") if args.preview else ""
        print(_("news_digest: {count} пунктов в теме, выпуск {digest}, отсеяно {dropped}{note}").format(
            count=len(items), digest=digest_id, dropped=len(dropped), note=note))
        return 0


if __name__ == "__main__":
    sys.exit(main())

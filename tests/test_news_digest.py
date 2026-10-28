"""Тесты news_digest: разбор лент и страниц, отсев виденного, ответ claude,
сообщение и кнопки, и главное: отметка «видел» только после доставки."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.i18n import _  # noqa: E402
from tests.texts import msg_re  # noqa: E402
from claude_tg_bot.skills.news_digest import __main__ as skill  # noqa: E402
from claude_tg_bot.skills.news_digest import backlog as news_backlog  # noqa: E402
from claude_tg_bot.skills.news_digest.core import (  # noqa: E402
    FEEDBACK_LABELS,
    INSTALL_RULE,
    Candidate,
    Item,
    State,
    add_backlog_line,
    append_feedback,
    build_prompt,
    find_item_by_message,
    item_buttons,
    load_item,
    load_state,
    page_candidate,
    parse_answer,
    parse_backlog_choice,
    parse_callback,
    parse_feed,
    parse_github_search,
    parse_hn,
    recent_feedback,
    render_item,
    reply_note,
    save_digest,
    select_new,
    set_message_id,
    topic_note,
    write_last,
)

NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)

ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/1/v2.1.292</id>
    <updated>2026-10-06T20:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/anthropics/claude-code/releases/tag/v2.1.292"/>
    <title>v2.1.292</title>
    <content type="html">&lt;h2&gt;What's changed&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Added an effort parameter to the Agent tool&lt;/li&gt;&lt;/ul&gt;</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/1/v2.2.0-alpha.1</id>
    <updated>2026-10-07T20:00:00Z</updated>
    <link rel="alternate" href="https://github.com/x/releases/tag/v2.2.0-alpha.1"/>
    <title>v2.2.0-alpha.1</title>
    <content type="html">alpha</content>
  </entry>
</feed>"""

RSS_FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Remote control for local agents</title><link>https://cursor.com/changelog/remote</link>
<guid>remote</guid><pubDate>Tue, 06 Oct 2026 00:00:00 GMT</pubDate><description>&lt;p&gt;See and reply&lt;/p&gt;</description></item>
</channel></rss>"""


def _src(**kw) -> dict:
    return {"name": "s", "weight": "focus", "type": "feed", "url": "https://e.x/feed", **kw}


def _cand(i: int, weight: str = "focus", days_ago: float = 1, body: str = "текст") -> Candidate:
    return Candidate(f"s:{i}", "s", weight, f"Заголовок {i}", f"https://e.x/{i}", body, NOW - timedelta(days=days_ago))


class FeedTests(unittest.TestCase):
    def test_atom_entry_with_html_content(self):
        cands = parse_feed(ATOM_FEED, _src())
        self.assertEqual(len(cands), 2)
        c = cands[0]
        self.assertEqual(c.url, "https://github.com/anthropics/claude-code/releases/tag/v2.1.292")
        self.assertIn("Added an effort parameter", c.body)
        self.assertNotIn("<li>", c.body)
        self.assertEqual(c.date, datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc))

    def test_exclude_and_include_by_title(self):
        self.assertEqual([c.title for c in parse_feed(ATOM_FEED, _src(exclude="alpha"))], ["v2.1.292"])
        self.assertEqual([c.title for c in parse_feed(ATOM_FEED, _src(include=r"^v\d+\.\d+\.0$"))], [])

    def test_rss(self):
        (c,) = parse_feed(RSS_FEED, _src())
        self.assertEqual(c.url, "https://cursor.com/changelog/remote")
        self.assertEqual(c.body, "See and reply")
        self.assertEqual(c.date.date().isoformat(), "2026-10-06")


class PageTests(unittest.TestCase):
    SRC = {"name": "p", "weight": "focus", "type": "page", "url": "https://e.x/notes", "title": "Заметки"}

    def test_first_read_then_only_new_lines(self):
        state = State()
        html1 = "<p>October 1, 2026 first feature that is long enough</p><p>short</p>"
        cand, snap = page_candidate(html1, self.SRC, state, is_html=True)
        self.assertIn(_("первое чтение страницы, новыми считать только записи после границы"), cand.title)
        self.assertIn("first feature", cand.body)
        self.assertNotIn("short", cand.body)
        state.snapshots["p"] = snap

        same, _snap = page_candidate(html1, self.SRC, state, is_html=True)
        self.assertIsNone(same)

        html2 = "<p>October 7, 2026 brand new feature line here</p>" + html1
        cand2, _snap = page_candidate(html2, self.SRC, state, is_html=True)
        self.assertEqual(cand2.body, "October 7, 2026 brand new feature line here")
        self.assertNotEqual(cand.id, cand2.id)


class ApiTests(unittest.TestCase):
    def test_github_search_min_stars(self):
        payload = {"items": [
            {"full_name": "a/big", "stargazers_count": 500, "html_url": "https://github.com/a/big",
             "description": "mods", "topics": ["claude-code"], "language": "TypeScript", "created_at": "2026-10-05T00:00:00Z"},
            {"full_name": "a/small", "stargazers_count": 3},
        ]}
        cands = parse_github_search(payload, {"name": "gh", "weight": "focus", "min_stars": 50})
        self.assertEqual([c.id for c in cands], ["gh:a/big"])
        self.assertIn("★500", cands[0].title)

    def test_hn_filter(self):
        payload = {"hits": [
            {"objectID": "1", "title": "Claude Code mods are here", "points": 900, "url": "https://x/1"},
            {"objectID": "2", "title": "Gardening tips", "points": 900},
            {"objectID": "3", "title": "New LLM", "points": 10},
        ]}
        cands = parse_hn(payload, {"name": "hn", "weight": "brief", "min_points": 100, "match": r"\b(LLM|Claude)"})
        self.assertEqual([c.id for c in cands], ["hn:1"])


class SelectTests(unittest.TestCase):
    def test_seen_and_old_dropped_focus_first(self):
        state = State(seen={"s:1": "2026-10-07"})
        cands = [_cand(1), _cand(2, "brief", 0.1), _cand(3, "focus", 2), _cand(4, "focus", 30)]
        picked = select_new(cands, state, NOW - timedelta(days=7), 100_000)
        self.assertEqual([c.id for c in picked], ["s:3", "s:2"])

    def test_budget_leaves_rest_for_tomorrow(self):
        cands = [_cand(i, body="x" * 1000) for i in range(5)]
        picked = select_new(cands, State(), NOW - timedelta(days=7), 2500)
        self.assertEqual(len(picked), 2)


class AnswerTests(unittest.TestCase):
    def test_url_comes_from_candidate_and_bad_refs_dropped(self):
        cands = [_cand(1), _cand(2)]
        raw = 'Вот:\n```json\n' + json.dumps({"items": [
            {"kind": "try", "refs": ["c2"], "title": "Мод \u2014 **второй**", "text": "т", "how": "к"},
            {"kind": "tools", "refs": ["c1", "c9"], "title": "Первый", "text": "т", "how": "к"},
            {"kind": "tools", "refs": ["c9"], "title": "Выдуманный"},
        ]}, ensure_ascii=False) + "\n```"
        items = parse_answer(raw, cands)
        self.assertEqual([(i.n, i.kind, i.url) for i in items],
                         [(1, "tools", "https://e.x/1"), (2, "try", "https://e.x/2")])
        self.assertEqual(items[1].title, "Мод - второй")
        self.assertEqual(items[0].refs, ["s:1"])

    def test_detail_and_length_limits(self):
        raw = json.dumps({"items": [{"kind": "tools", "refs": ["c1"], "title": "Т" * 200,
                                     "text": "x" * 900, "how": "y" * 900, "detail": "z" * 900}]})
        (item,) = parse_answer(raw, [_cand(1)])
        self.assertLessEqual(len(item.title), 80)
        self.assertLessEqual(len(item.text), 260)
        self.assertLessEqual(len(item.how), 180)
        self.assertLessEqual(len(item.detail), 300)
        plain = f"{item.title}\n{item.text}\nУ тебя: {item.how}\n{item.detail}"
        self.assertLess(len(plain), 1000)  # бот передаёт агенту до 1000 знаков цитаты

    def test_feedback_goes_into_prompt(self):
        prompt = build_prompt("профиль", "", [_cand(1)], NOW, "2026-10-08", ["- мимо: Скучное (hn)"])
        taste = _("## Отзывы владельца на прошлые подборки (свежие сверху)\n"
                  "«мимо» значит не нужно, «в список» значит отложил поставить. Это примеры вкуса: "
                  "не бери похожее на «мимо», ищи похожее на «в список», но не делай выводов по одному случаю.\n")
        self.assertIn(taste, prompt)
        self.assertIn("- мимо: Скучное (hn)", prompt)
        self.assertNotIn(taste, build_prompt("профиль", "", [_cand(1)], NOW, "2026-10-08"))

    def test_task_name_is_short_and_cut_by_word(self):
        # имя темы 1-3 слова до 24 знаков (решение Ивана 14.10.2026): длинное
        # режется по слову без многоточия, короткое и пустое как есть
        def name(task):
            raw = json.dumps({"items": [{"kind": "try", "refs": ["c1"], "title": "Т", "task": task}]})
            return parse_answer(raw, [_cand(1)])[0].task

        self.assertEqual(name("Секреты с телефона"), "Секреты с телефона")
        long = name("Правка секретов с телефона через менеджер паролей")
        self.assertEqual(long, "Правка секретов с")
        self.assertNotIn("…", long)
        self.assertLessEqual(len(name("Очень" * 20)), 24)
        self.assertEqual(name("Настроить автоматизированное резервное копирование"), "Настроить")
        self.assertEqual(name(""), "")
        self.assertEqual(name("**Версия** Immich"), "Версия Immich")

    def test_prompt_asks_short_task_name(self):
        prompt = build_prompt("профиль", "", [_cand(1)], NOW, "2026-10-08")
        self.assertIn(_("Ответь только JSON без пояснений вокруг:\n"
                        '{"items": [{"kind": "tools" или "try", "refs": ["c1"], "title": "что это, до 60 знаков", '
                        '"text": "что появилось, до 200 знаков", "how": "как применить у владельца, до 150 знаков", '
                        '"detail": "необязательно: подробности, до 300 знаков", '
                        '"task": "имя задачи внедрения у владельца для названия темы, 1-3 слова, до 24 знаков, по-русски, '
                        'понятно без контекста, например «Секреты с телефона»"}]}\n'
                        "Лимиты длины жёсткие: читают с телефона. В how не пересказывай text, в detail не повторяй ни то, ни другое: "
                        "там то, что нужно для решения (версия, где включается, чем ставится, оговорки), или пусто.\n"
                        'kind "tools": новое в том, чем он уже пользуется; "try": репозиторий, плагин, мод, который стоит поставить. '
                        'Порядок пунктов: сначала самое полезное. Полезного нет: {"items": []}.\n\n'), prompt)
        self.assertNotIn("до 40 знаков", prompt)

    def test_empty(self):
        self.assertEqual(parse_answer('{"items": []}', [_cand(1)]), [])

    def test_no_json_is_error(self):
        with self.assertRaises(ValueError):
            parse_answer("не знаю", [_cand(1)])


class MessageTests(unittest.TestCase):
    ITEM = Item(1, "tools", "Claude Code <2.1.292>", "Подагенты & уровень.", "Разведчики дешевле.",
                "https://e.x/1?a=1&b=2", "claude-code", "Флаг effort в описании агента.", ["s:1"])

    def test_render_html_escaped_link_in_title(self):
        html_text = render_item(self.ITEM)
        self.assertTrue(html_text.startswith('🔧 <b><a href="https://e.x/1?a=1&amp;b=2">Claude Code &lt;2.1.292&gt;</a></b>'))
        self.assertIn("Подагенты &amp; уровень.", html_text)
        self.assertIn(_("<b>У тебя:</b> {how}").format(how="разведчики дешевле."), html_text)
        self.assertIn("<blockquote expandable>Флаг effort в описании агента.</blockquote>", html_text)
        self.assertNotIn("\u2014", html_text)

    def test_render_states(self):
        self.assertTrue(render_item(self.ITEM, "saved", "Свод").endswith(_("📌 в бэклоге: {where}").format(where="Свод")))
        self.assertTrue(render_item(self.ITEM, "busy").endswith(_("📌 выбираю бэклог…")))
        self.assertTrue(render_item(self.ITEM, "work", "https://t.me/c/1/2?a&b").endswith(
            _('🛠 в работе: <a href="{link}">тема задачи</a>').format(link="https://t.me/c/1/2?a&amp;b")))
        self.assertTrue(render_item(self.ITEM, "work", "задача закрыта: <сделано>").endswith(
            "🛠 задача закрыта: &lt;сделано&gt;"))
        self.assertEqual(render_item(self.ITEM, "skipped"), "✖ <s>Claude Code &lt;2.1.292&gt;</s>")
        bare = Item(2, "try", "Мод", "", "", "https://e.x/2", "gh")
        self.assertEqual(render_item(bare), '🧩 <b><a href="https://e.x/2">Мод</a></b>')

    def test_buttons_by_state_and_callback(self):
        (row,) = item_buttons(self.ITEM, "202610080900")
        self.assertEqual([b["text"] for b in row], [_("📌 В список"), _("🛠 В работу"), _("✖ Мимо")])
        for b in row:
            self.assertLessEqual(len(b["data"].encode()), 64)
        self.assertEqual(parse_callback(row[0]["data"]), ("202610080900", 1, "p"))
        self.assertEqual(parse_callback(row[1]["data"]), ("202610080900", 1, "w"))
        self.assertEqual(parse_callback(row[2]["data"]), ("202610080900", 1, "x"))
        self.assertEqual(item_buttons(self.ITEM, "202610080900", "work"), [])
        (undo,) = item_buttons(self.ITEM, "202610080900", "skipped")
        self.assertEqual(parse_callback(undo[0]["data"]), ("202610080900", 1, "u"))
        self.assertEqual(item_buttons(self.ITEM, "202610080900", "saved"), [])
        self.assertEqual(item_buttons(self.ITEM, "202610080900", "busy"), [])
        self.assertIsNone(parse_callback("news:202610080900:1:t"))  # старая «Прикрутить»
        self.assertIsNone(parse_callback("news:../../etc:1:p"))

    def test_digest_store_message_lookup_and_old_format(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            save_digest(base, "202610080900", [self.ITEM], [_cand(2)], NOW)
            self.assertEqual(load_item(base, "202610080900", 1), self.ITEM)
            self.assertIsNone(load_item(base, "202610080900", 2))
            self.assertFalse((base / "last.md").exists())  # last.md пишется по факту доставки

            self.assertIsNone(find_item_by_message(base, 555))
            set_message_id(base, "202610080900", 1, 555)
            found = find_item_by_message(base, 555)
            self.assertEqual(found[0], "202610080900")
            self.assertEqual(found[1].message_id, 555)

            # выпуск первого вида: одним сообщением, без версии; кнопки его не обслуживают
            (base / "digests" / "202610071537.json").write_text(json.dumps(
                {"id": "202610071537", "items": [{"n": 1, "kind": "tools", "title": "t", "text": "", "how": "",
                                                  "url": "u", "source": "s"}]}))
            self.assertIsNone(load_item(base, "202610071537", 1))

    def test_last_md_sent_unsent_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            other = Item(2, "try", "Второй", "", "", "https://e.x/2", "gh")
            write_last(base, "202610080900", [self.ITEM], [_cand(3)], [other])
            last = (base / "last.md").read_text()
            self.assertIn("08.10.2026 09:00", last)
            unsent = _("## Не дошло, повторю в следующем прогоне\n\n{items}").format(items="")
            dropped = _("## Отсеяно (смотрел, но не взял)\n\n{items}").format(items="")
            self.assertLess(last.index("Claude Code"), last.index(unsent))
            self.assertLess(last.index(unsent), last.index("Второй"))
            self.assertIn("https://e.x/3", last.split(dropped)[1])
            write_last(base, "202610090900", [], [])
            last = (base / "last.md").read_text()
            self.assertIn(_("## Отправлено\n\n{items}").format(items=_("ничего")), last)
            self.assertNotIn(unsent, last)

    def test_backlog_same_page_url_different_items(self):
        # у новостей со страницы адрес общий: дубль это пара «заголовок и адрес»
        a = Item(1, "try", "Мод A", "", "как", "https://github.com/x/catalog", "mods-catalog")
        b = Item(2, "try", "Мод B", "", "как", "https://github.com/x/catalog", "mods-catalog")
        short = Item(3, "try", "Мод A", "", "как", "https://github.com/x/cat", "gh")
        text = "# Бэклог\n"
        for it in (a, b, short):
            text = add_backlog_line(text, it, "2026-10-08")
            self.assertIsNotNone(text)
        self.assertIsNone(add_backlog_line(text, a, "2026-10-09"))
        self.assertEqual(len([ln for ln in text.splitlines() if ln.startswith("- **")]), 3)

    def test_backlog_line_goes_to_end_of_news_section(self):
        current = "# Бот: бэклог\n\n## Из новостей\n\nвступление\n\n- **старый**\n\n## Принято\n\n- x\n"
        text = add_backlog_line(current, self.ITEM, "2026-10-08")
        lines = text.splitlines()
        new = "- **2026-10-08 Claude Code <2.1.292>.** Разведчики дешевле. https://e.x/1?a=1&b=2"
        self.assertEqual(lines[lines.index("- **старый**") + 1], new)
        self.assertEqual(lines[-1], "- x")
        self.assertEqual(text.count("## Из новостей"), 1)

    def test_backlog_section_created_when_missing(self):
        text = add_backlog_line("# Свод: бэклог\n\n## Со сроком\n\n- a\n", self.ITEM, "2026-10-08")
        self.assertTrue(text.endswith("https://e.x/1?a=1&b=2\n"))
        self.assertLess(text.index("- a"), text.index("## Из новостей"))

    def test_backlog_choice_only_known_keys(self):
        keys = {"bot", "svod"}
        self.assertEqual(parse_backlog_choice('{"project": "svod", "why": "роутер"}', keys), ("svod", "роутер"))
        self.assertEqual(parse_backlog_choice('ok {"project": "../etc", "why": "x"}', keys), (None, "x"))
        self.assertEqual(parse_backlog_choice('{"project": "none", "why": "не моё"}', keys), (None, "не моё"))
        self.assertEqual(parse_backlog_choice("мусор", keys), (None, ""))

    def test_backlog_commit_new_file_and_dirty(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            repo, origin, locks = Path(d) / "repo", Path(d) / "origin.git", Path(d) / "locks"
            subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
            subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
            # бэклог коммитит только в main: ветка не от init.defaultBranch машины
            subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
            for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
                subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
            (repo / "other.txt").write_text("чужая правка\n")
            subprocess.run(["git", "-C", str(repo), "add", "other.txt"], check=True)
            # пустой сервер: своей ветки на нём нет, отправлять не с чем сверить
            filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", locks)
            self.assertEqual((filed.status, filed.note), ("added", _("закоммичено, не отправлено")))
            text = (repo / "BACKLOG.md").read_text()
            self.assertTrue(text.startswith("# Проект: бэклог\n\n**Разобрать в приоритете:**"))
            log = subprocess.run(["git", "-C", str(repo), "show", "--stat", "--format=", "HEAD"],
                                 capture_output=True, text=True).stdout
            self.assertIn("BACKLOG.md", log)
            self.assertNotIn("other.txt", log)  # чужое из индекса в коммит не попало
            remote = subprocess.run(["git", "-C", str(origin), "log", "--oneline"], capture_output=True, text=True).stdout
            self.assertEqual(remote, "")
            self.assertEqual(news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-09", locks).status, "dup")
            (repo / "BACKLOG.md").write_text(text + "ручная строка\n")
            other = Item(2, "try", "Мод", "", "как", "https://e.x/2", "gh")
            filed = news_backlog.write_and_commit(repo, "Проект", other, "2026-10-09", locks)
            self.assertEqual(filed.status, "added")
            self.assertEqual(filed.note, _("не закоммичено: в BACKLOG.md были чужие правки"))
            self.assertIn("ручная строка", (repo / "BACKLOG.md").read_text())

    def test_backlog_does_not_push_foreign_commit(self):
        # push только когда не на сервере ровно свой коммит (проект шага 3, 1.8)
        import subprocess

        def git(repo: Path, *args: str) -> str:
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()

        with tempfile.TemporaryDirectory() as d:
            repo, origin, locks = Path(d) / "repo", Path(d) / "origin.git", Path(d) / "locks"
            subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
            subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
            git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
            for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
                git(repo, "config", k, v)
            (repo / "a.txt").write_text("1\n")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-qm", "начало")
            git(repo, "push", "-q", "-u", "origin", "HEAD")
            branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            server = git(origin, "rev-parse", branch)
            (repo / "a.txt").write_text("2\n")
            git(repo, "commit", "-qam", "чужой неотправленный")
            filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", locks)
            self.assertEqual((filed.status, filed.note), ("added", _("закоммичено, не отправлено")))
            self.assertIn("Бэклог:", git(repo, "log", "-1", "--format=%s"))
            self.assertEqual(git(origin, "rev-parse", branch), server)
            git(repo, "push", "-q")  # чужой ушёл, дальше свой коммит один
            other = Item(2, "try", "Мод", "", "как", "https://e.x/2", "gh")
            filed = news_backlog.write_and_commit(repo, "Проект", other, "2026-10-09", locks)
            self.assertEqual((filed.status, filed.note), ("added", ""))
            self.assertEqual(git(origin, "rev-parse", branch), git(repo, "rev-parse", "HEAD"))

    @staticmethod
    def _pushed_repo(d: str):
        """Клон с bare-сервером и одним отправленным коммитом: (repo, origin, git, ветка)."""
        import subprocess

        def git(repo: Path, *args: str) -> str:
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()

        repo, origin = Path(d) / "repo", Path(d) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
        git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git(repo, "config", k, v)
        (repo / "a.txt").write_text("1\n")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-qm", "начало")
        git(repo, "push", "-q", "-u", "origin", "HEAD")
        return repo, origin, git, git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    def test_backlog_does_not_push_commit_from_other_server_branch(self):
        # Чужой коммит уже на сервере в ветке feature, но не в main: счёт от
        # всех ссылок сервера его не видел, и push увозил его в main (ревью 14.10).
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            server = git(origin, "rev-parse", branch)
            git(repo, "checkout", "-qb", "feature")
            (repo / "a.txt").write_text("2\n")
            git(repo, "commit", "-qam", "черновик ветки")
            git(repo, "push", "-q", "origin", "feature")
            git(repo, "checkout", "-q", branch)
            git(repo, "merge", "-q", "--ff-only", "feature")
            filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual((filed.status, filed.note), ("added", _("закоммичено, не отправлено")))
            self.assertEqual(git(origin, "rev-parse", branch), server)

    def test_backlog_pushes_only_checked_commit(self):
        # Соседняя сессия коммитит сразу после проверки: уходит ровно
        # проверенный свой коммит, её коммит остаётся неотправленным (ревью 14.10).
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            real = news_backlog._git

            def racing(path, *args, **kw):
                proc = real(path, *args, **kw)
                if args[:1] == ("rev-list",):
                    (repo / "a.txt").write_text("соседняя\n")
                    git(repo, "commit", "-qam", "чужой коммит соседней сессии")
                return proc

            with patch.object(news_backlog, "_git", racing):
                filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual((filed.status, filed.note), ("added", ""))
            self.assertEqual(git(origin, "log", "-1", "--format=%s", branch), "Бэклог: Claude Code <2.1.292> (из новостей)")
            self.assertEqual(git(origin, "rev-parse", branch), git(repo, "rev-parse", "HEAD~1"))
            self.assertEqual(git(repo, "rev-list", "--count", "@{u}..HEAD"), "1")

    def test_backlog_does_not_push_foreign_commit_on_top_of_own(self):
        # Соседняя сессия сразу после своего коммита отправила его и
        # закоммитила свой поверх: проверка по текущему HEAD отправляла её
        # коммит (ревью Codex 14.10, №4)
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            real = news_backlog._git

            def racing(path, *args, **kw):
                proc = real(path, *args, **kw)
                if args[:1] == ("update-ref",):
                    git(repo, "push", "-q")
                    (repo / "a.txt").write_text("соседняя\n")
                    git(repo, "commit", "-qam", "чужой коммит соседней сессии")
                return proc

            with patch.object(news_backlog, "_git", racing):
                filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual((filed.status, filed.note), ("added", _("закоммичено, не отправлено")))
            self.assertEqual(git(origin, "log", "-1", "--format=%s", branch), "Бэклог: Claude Code <2.1.292> (из новостей)")
            self.assertEqual(git(origin, "rev-parse", branch), git(repo, "rev-parse", "HEAD~1"))

    def test_backlog_does_not_push_foreign_commit_replacing_own(self):
        # Соседняя сессия сразу после своего коммита C заменила его через
        # commit --amend на D с тем же родителем: проверка по родителю
        # вершины main отправляла D. Уходит ровно свой C (ревью Codex 14.10, №4)
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            real = news_backlog._git
            own: list[str] = []

            def racing(path, *args, **kw):
                proc = real(path, *args, **kw)
                if args[:1] == ("update-ref",):
                    own.append(git(repo, "rev-parse", branch))
                    (repo / "a.txt").write_text("соседняя\n")
                    git(repo, "commit", "-q", "--amend", "-am", "соседняя сессия переписала коммит")
                return proc

            with patch.object(news_backlog, "_git", racing):
                filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            replaced = git(repo, "rev-parse", branch)
            self.assertEqual(git(repo, "rev-parse", f"{replaced}~1"), git(repo, "rev-parse", f"{own[0]}~1"))
            self.assertEqual((filed.status, filed.note), ("added", ""))
            self.assertEqual(git(origin, "rev-parse", branch), own[0])
            self.assertEqual(git(origin, "show", f"{branch}:a.txt"), "1")
            self.assertEqual(git(origin, "log", "-1", "--format=%s", branch), "Бэклог: Claude Code <2.1.292> (из новостей)")

    def test_backlog_commit_does_not_drop_commit_made_meanwhile(self):
        # Соседняя сессия закоммитила, пока собирался свой коммит: main не
        # сдвигается на него, её коммит остаётся в main, строка в файле
        # (ревью Codex 14.10, №4)
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            server = git(origin, "rev-parse", branch)
            real = news_backlog._git

            def racing(path, *args, **kw):
                proc = real(path, *args, **kw)
                if args[:1] == ("commit-tree",):
                    (repo / "a.txt").write_text("соседняя\n")
                    git(repo, "commit", "-qam", "чужой коммит соседней сессии")
                return proc

            with patch.object(news_backlog, "_git", racing):
                filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual((filed.status, filed.note), ("added", _("git commit не прошёл, файл записан")))
            self.assertEqual(git(repo, "log", "-1", "--format=%s", branch), "чужой коммит соседней сессии")
            self.assertIn(self.ITEM.title, (repo / "BACKLOG.md").read_text(encoding="utf-8"))
            self.assertEqual(git(origin, "rev-parse", branch), server)

    def test_backlog_failed_update_ref_leaves_nothing_staged(self):
        # main сдвинулась, update-ref отказал: строка бота не остаётся
        # проиндексированной в настоящем индексе, иначе следующие автокоммиты
        # бэклога не шли бы; чужое в индексе и файл целы (ревью Codex 14.10)
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            (repo / "a.txt").write_text("чужое в индексе\n")
            git(repo, "add", "a.txt")
            real = news_backlog._git

            def refusing(path, *args, **kw):
                if args[:1] == ("update-ref",):
                    return subprocess.CompletedProcess(args, 1, "", "cannot lock ref")
                return real(path, *args, **kw)

            with patch.object(news_backlog, "_git", refusing):
                filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual((filed.status, filed.note), ("added", _("git commit не прошёл, файл записан")))
            self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "a.txt")
            self.assertIn(self.ITEM.title, (repo / "BACKLOG.md").read_text(encoding="utf-8"))

    def test_backlog_add_timeout_after_staging_leaves_nothing_staged(self):
        # git add успел проиндексировать файл и упал по таймауту: строка бота
        # не остаётся в индексе, чужое в индексе цело
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            (repo / "a.txt").write_text("чужое в индексе\n")
            git(repo, "add", "a.txt")
            real = news_backlog._git

            def slow_add(path, *args, **kw):
                proc = real(path, *args, **kw)
                if args[:1] == ("add",):
                    raise subprocess.TimeoutExpired("git add", 60)
                return proc

            with patch.object(news_backlog, "_git", slow_add):
                filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual((filed.status, filed.note), ("added", "git commit: TimeoutExpired"))
            self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "a.txt")
            self.assertIn(self.ITEM.title, (repo / "BACKLOG.md").read_text(encoding="utf-8"))

    def test_backlog_commit_leaves_index_clean_and_keeps_owner_staging(self):
        # Коммит собирается во временном индексе: после удачного BACKLOG.md в
        # настоящем индексе совпадает с вершиной, а проиндексированное
        # владельцем в окне коммита не снимается (ревью Codex 14.10)
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual(filed.status, "added")
            self.assertEqual(git(repo, "status", "--porcelain", "--", "BACKLOG.md"), "")
            real_commit = news_backlog._commit

            def owner_stages(path, parent, msg):
                (path / "own.md").write_text("правка владельца\n")
                blob = subprocess.run(["git", "-C", str(path), "hash-object", "-w", "own.md"],
                                      check=True, capture_output=True, text=True).stdout.strip()
                git(path, "update-index", "--cacheinfo", f"100644,{blob},BACKLOG.md")
                return real_commit(path, parent, msg)

            real_git = news_backlog._git

            def refusing(path, *args, **kw):
                if args[:1] == ("update-ref",):
                    return subprocess.CompletedProcess(args, 1, "", "cannot lock ref")
                return real_git(path, *args, **kw)

            # Владелец проиндексировал свою правку, пока бот коммитил, а
            # update-ref отказал: его индексация остаётся.
            item = replace(self.ITEM, title="Другой пункт", text="другой текст")
            with patch.object(news_backlog, "_commit", owner_stages), patch.object(news_backlog, "_git", refusing):
                filed = news_backlog.write_and_commit(repo, "Проект", item, "2026-10-09", Path(d) / "locks")
            self.assertEqual(filed.note, _("git commit не прошёл, файл записан"))
            staged = git(repo, "ls-files", "-s", "--", "BACKLOG.md").split()[1]
            self.assertEqual(staged, git(repo, "hash-object", "own.md"))

    def test_backlog_pushes_own_commit_to_main_after_branch_switch(self):
        # Соседняя сессия переключила рабочую копию на свою ветку сразу после
        # коммита: уходит свой коммит в main, её ветка на сервере не меняется
        # (ревью Codex 14.10, №4)
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, branch = self._pushed_repo(d)
            git(repo, "checkout", "-qb", "feature")
            git(repo, "push", "-q", "-u", "origin", "feature")
            (repo / "a.txt").write_text("ветка\n")
            git(repo, "commit", "-qam", "черновик ветки")
            git(repo, "checkout", "-q", branch)
            feature = git(origin, "rev-parse", "feature")
            real = news_backlog._git

            def racing(path, *args, **kw):
                proc = real(path, *args, **kw)
                if args[:1] == ("update-ref",):
                    git(repo, "checkout", "-q", "feature")
                return proc

            with patch.object(news_backlog, "_git", racing):
                filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual((filed.status, filed.note), ("added", ""))
            self.assertEqual(git(origin, "rev-parse", "feature"), feature)
            self.assertEqual(git(origin, "rev-parse", branch), git(repo, "rev-parse", branch))
            self.assertEqual(git(origin, "log", "-1", "--format=%s", branch), "Бэклог: Claude Code <2.1.292> (из новостей)")

    def test_backlog_not_committed_off_main(self):
        # Рабочая копия на чужой ветке под PR: строка в файле, но ни коммита
        # в эту ветку, ни отправки (ревью 14.10)
        with tempfile.TemporaryDirectory() as d:
            repo, origin, git, _git = self._pushed_repo(d)
            git(repo, "checkout", "-qb", "agent/fix")
            git(repo, "push", "-q", "-u", "origin", "agent/fix")
            server = git(origin, "rev-parse", "agent/fix")
            filed = news_backlog.write_and_commit(repo, "Проект", self.ITEM, "2026-10-08", Path(d) / "locks")
            self.assertEqual((filed.status, filed.note), ("added", _("не закоммичено: репозиторий не на {branch}").format(branch="main")))
            self.assertIn(self.ITEM.title, (repo / "BACKLOG.md").read_text(encoding="utf-8"))
            self.assertEqual(git(repo, "rev-parse", "HEAD"), server)
            self.assertEqual(git(origin, "rev-parse", "agent/fix"), server)

    def test_task_outcome_does_not_override_rating(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            append_feedback(base, "202610080900", self.ITEM, "w", NOW)
            append_feedback(base, "202610080900", self.ITEM, "task:drop", NOW)
            self.assertEqual(len(recent_feedback(base)), 1)
            self.assertTrue(recent_feedback(base)[0].startswith(f"- {_(FEEDBACK_LABELS['w'])}:"))

    def test_feedback_last_press_wins_and_undo_cancels(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            other = Item(2, "try", "Второй", "", "", "https://e.x/2", "gh")
            append_feedback(base, "202610080900", self.ITEM, "x", NOW)
            append_feedback(base, "202610080900", other, "p", NOW)
            append_feedback(base, "202610080900", self.ITEM, "u", NOW)
            pin, skip = _(FEEDBACK_LABELS["p"]), _(FEEDBACK_LABELS["x"])
            self.assertEqual(recent_feedback(base), [f"- {pin}: Второй (gh)"])
            append_feedback(base, "202610080900", self.ITEM, "x", NOW)
            self.assertEqual(recent_feedback(base),
                             [f"- {skip}: Claude Code <2.1.292> (claude-code)", f"- {pin}: Второй (gh)"])
            self.assertEqual(len(recent_feedback(base, limit=1)), 1)
            self.assertEqual(recent_feedback(Path(d) / "нет"), [])

    def test_notes_for_topic_agent(self):
        note = topic_note(Path("/s/news_digest"))
        self.assertIn("/s/news_digest/last.md", note)
        self.assertIn(_(INSTALL_RULE), note)
        self.assertIn("https://e.x/1?a=1&b=2", reply_note(self.ITEM))


class MainTests(unittest.TestCase):
    """Отметка «видел» только после доставки, fail-closed по адресату."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict("os.environ", {
            "CLAUDE_TG_BOT_STATE_DIR": self.tmp.name, "NEWS_TG_CHAT_ID": "-100", "NEWS_TG_THREAD_ID": "7",
        })
        self.env.start()
        self.sources = [{"name": "s", "weight": "focus", "type": "feed", "url": "u"}]
        patches = [
            patch.object(skill, "load_dotenv"),
            patch.object(skill, "installed_summary", return_value=""),
        ]
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)
        self.src_file = Path(self.tmp.name) / "sources.json"
        self.src_file.write_text(json.dumps({"sources": self.sources}))
        patch.object(skill, "SOURCES_FILE", self.src_file).start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _state(self) -> State:
        return load_state(Path(self.tmp.name) / "news_digest" / "state.json")

    def _cands(self, k: int = 1) -> list[Candidate]:
        now = datetime.now(timezone.utc)  # s:1 самый свежий, значит c1 в промпте
        return [Candidate(f"s:{i}", "s", "focus", f"T{i}", f"https://e.x/{i}", "b", now - timedelta(minutes=i))
                for i in range(1, k + 1)]

    def _run(self, answer: str, send=(101,), argv=(), k: int = 1):
        with patch.object(skill, "fetch", return_value=(self._cands(k), None)), \
             patch.object(skill, "run_claude", return_value=answer), \
             patch.object(skill, "_send", side_effect=list(send)) as sender:
            rc = skill.main(list(argv))
        return rc, sender

    def _base(self) -> Path:
        return Path(self.tmp.name) / "news_digest"

    def test_each_item_own_silent_html_message_and_seen(self):
        ans = json.dumps({"items": [{"kind": "tools", "refs": ["c1"], "title": "Один", "text": "t", "how": "h"},
                                    {"kind": "try", "refs": ["c2"], "title": "Два"}]})
        rc, sender = self._run(ans, send=(101, 102), k=3)
        self.assertEqual(rc, 0)
        self.assertEqual(sender.call_count, 2)
        first = sender.call_args_list[0]
        self.assertIn('<a href="https://e.x/1">Один</a>', first.args[2])
        self.assertTrue(first.kwargs["html"])
        self.assertEqual(first.args[3][0][0]["text"], _("📌 В список"))
        self.assertEqual(set(self._state().seen), {"s:1", "s:2", "s:3"})  # отсеянное тоже виденное
        self.assertEqual(find_item_by_message(self._base(), 102)[1].title, "Два")
        self.assertIn("T3", (self._base() / "last.md").read_text())

    def test_partial_delivery_keeps_undelivered_for_tomorrow(self):
        ans = json.dumps({"items": [{"kind": "tools", "refs": ["c1"], "title": "Один"},
                                    {"kind": "try", "refs": ["c2"], "title": "Два"}]})
        rc, sender = self._run(ans, send=(101, None, None), k=3)
        self.assertEqual(rc, 1)
        self.assertIn(_("Новости не собрались: {reason}").format(
            reason=_("дошло {delivered} из {count}, остальное пришлю завтра.").format(delivered=1, count=2)),
            sender.call_args_list[-1].args[2])
        self.assertEqual(set(self._state().seen), {"s:1", "s:3"})

    def test_preview_sends_but_does_not_mark_seen(self):
        ans = json.dumps({"items": [{"kind": "tools", "refs": ["c1"], "title": "Один"}]})
        rc, sender = self._run(ans, argv=["--preview"])
        self.assertEqual(rc, 0)
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(self._state().first_run)
        self.assertIsNotNone(find_item_by_message(self._base(), 101))

    def test_failed_delivery_does_not_mark_seen(self):
        ans = json.dumps({"items": [{"kind": "tools", "refs": ["c1"], "title": "T"}]})
        rc, _ = self._run(ans, send=(None, None))
        self.assertEqual(rc, 1)
        self.assertNotIn("s:1", self._state().seen)

    def test_partial_delivery_keeps_page_snapshot_of_lost_item(self):
        page = Candidate("pg:1", "pg", "focus", "Страница", "https://e.x/p", "новая строка", None)
        feed = Candidate("s:1", "s", "focus", "T1", "https://e.x/1", "b", datetime.now(timezone.utc))
        ans = json.dumps({"items": [{"kind": "tools", "refs": ["c1"], "title": "Из ленты"},
                                    {"kind": "tools", "refs": ["c2"], "title": "Со страницы"}]})

        def fetch(client, src, state, now):
            return ([page], ["h1"]) if src["name"] == "pg" else ([feed], None)

        self.src_file.write_text(json.dumps({"sources": [
            {"name": "s", "weight": "focus", "type": "feed", "url": "u"},
            {"name": "pg", "weight": "focus", "type": "page", "url": "p"}]}))
        with patch.object(skill, "fetch", side_effect=fetch), \
             patch.object(skill, "run_claude", return_value=ans), \
             patch.object(skill, "_send", side_effect=[101, None, None]):
            self.assertEqual(skill.main([]), 1)
        state = self._state()
        self.assertEqual(set(state.seen), {"s:1"})
        self.assertNotIn("pg", state.snapshots)  # строки недошедшего пункта придут завтра

    def test_nothing_useful_sends_nothing_but_marks_seen(self):
        rc, sender = self._run('{"items": []}')
        self.assertEqual(rc, 0)
        sender.assert_not_called()
        self.assertIn("s:1", self._state().seen)

    def test_claude_failure_reports_and_keeps_candidates(self):
        cand = Candidate("s:1", "s", "focus", "T", "https://e.x/1", "b", datetime.now(timezone.utc))
        with patch.object(skill, "fetch", return_value=([cand], None)), \
             patch.object(skill, "run_claude", side_effect=RuntimeError("claude exit 1: лимит или доступ")), \
             patch.object(skill, "_send", return_value=1) as send:
            rc = skill.main([])
        self.assertEqual(rc, 1)
        self.assertIn(_("Новости не собрались: {reason}").format(reason="claude exit 1: лимит или доступ"),
                      send.call_args.args[2])
        self.assertTrue(self._state().first_run)

    def test_empty_day_writes_last_md_with_dropped(self):
        rc, _sender = self._run('{"items": []}', k=2)
        self.assertEqual(rc, 0)
        last = (self._base() / "last.md").read_text()
        self.assertIn("T1", last.split(_("## Отсеяно (смотрел, но не взял)\n\n{items}").format(items=""))[1])

    def test_page_over_budget_keeps_old_snapshot(self):
        big = [Candidate(f"s:{i}", "s", "focus", f"T{i}", f"https://e.x/{i}", "x" * 500, datetime.now(timezone.utc))
               for i in range(3)]
        page = Candidate("pg:new", "pg", "focus", "Страница", "https://e.x/p", "новая строка", None)

        def fetch(client, src, state, now):
            return ([page], ["h-new"]) if src["name"] == "pg" else (big, None)

        self.src_file.write_text(json.dumps({"sources": [
            {"name": "s", "weight": "focus", "type": "feed", "url": "u"},
            {"name": "pg", "weight": "focus", "type": "page", "url": "p"}]}))
        with patch.object(skill, "fetch", side_effect=fetch), \
             patch.object(skill, "INPUT_BUDGET", 700), \
             patch.object(skill, "run_claude", return_value='{"items": []}'), \
             patch.object(skill, "_send", return_value=1):
            self.assertEqual(skill.main([]), 0)
        state = self._state()
        self.assertNotIn("pg:new", state.seen)
        self.assertNotIn("pg", state.snapshots)  # модель страницу не видела, строки ждут завтра

    def test_missing_target_fails_closed(self):
        with patch.dict("os.environ", {"NEWS_TG_THREAD_ID": ""}):
            self.assertEqual(skill.main([]), 1)


class BotNewsContextTests(unittest.TestCase):
    """Пометка агенту темы «Новости» в боте: только в своей теме, «gpt:» не трогает,
    при ответе на пункт добавляет его источник."""

    def setUp(self):
        from claude_tg_bot import bot
        self.bot = bot
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(state_dir=Path(self.tmp.name), codex_trigger_prefix="gpt:")
        base = Path(self.tmp.name) / "news_digest"
        item = Item(1, "tools", "Пункт", "т", "к", "https://e.x/1", "s")
        save_digest(base, "202610080900", [item], [], NOW)
        set_message_id(base, "202610080900", 1, 501)
        env = patch.dict("os.environ", {"NEWS_TG_CHAT_ID": "-100", "NEWS_TG_THREAD_ID": "7"})
        env.start()
        self.addCleanup(env.stop)

    def _msg(self, text="вопрос", chat=-100, thread=7, reply_id=None):
        reply = SimpleNamespace(message_id=reply_id) if reply_id else None
        return SimpleNamespace(text=text, chat_id=chat, message_thread_id=thread, reply_to_message=reply)

    def test_topic_note_and_reply_source(self):
        out = self.bot._news_context(self.cfg, self._msg(reply_id=501), "вопрос")
        self.assertRegex(out, msg_re("[Тема «Новости»: утренняя подборка полезного. Последний выпуск и отсеянное в {last}, "
                                   "все выпуски за месяц в {digests}. {rule}]"))
        self.assertIn("https://e.x/1", out)
        self.assertTrue(out.endswith("\n\nвопрос"))

    def test_root_reply_in_topic_has_no_item(self):
        out = self.bot._news_context(self.cfg, self._msg(reply_id=7), "вопрос")
        self.assertRegex(out, msg_re("[Тема «Новости»: утренняя подборка полезного. Последний выпуск и отсеянное в {last}, "
                                   "все выпуски за месяц в {digests}. {rule}]"))
        self.assertNotRegex(out, msg_re("[Это пункт подборки «{title}», источник: {url}. Сначала прочитай источник.]"))

    def test_other_topic_and_gpt_prefix_untouched(self):
        self.assertEqual(self.bot._news_context(self.cfg, self._msg(thread=8), "вопрос"), "вопрос")
        self.assertEqual(self.bot._news_context(self.cfg, self._msg(text="GPT: стоит?"), "GPT: стоит?"), "GPT: стоит?")


if __name__ == "__main__":
    unittest.main()

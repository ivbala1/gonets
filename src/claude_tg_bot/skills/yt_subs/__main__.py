"""Субтитры к видео по ссылке (YouTube и всё, что понимает yt-dlp).

Зачем: просьба «перескажи видео» упиралась в карточку на каждый вызов
yt-dlp, и 06.10.2026 карточка, пока Иван был за рулём, ушла в отказ по
таймауту. Вызов скилла бота из его venv проходит без карточки, поэтому
загрузка субтитров живёт здесь, а не в списке разрешённых команд: у yt-dlp
есть флаги, которые без спроса пускать нельзя (`--exec`, `--cookies-from-browser`,
`-o` в любой путь), а скилл зовёт его только с фиксированным набором.

Что делает: в два вызова yt-dlp. Первый берёт метаданные и список дорожек
(`-J`), второй по сохранённым метаданным качает ровно одну дорожку: запрос
сразу всех `ru.*,en.*` YouTube режет ответом 429 на автопереводах.
Порядок выбора: ручные субтитры на языке из `--lang`, затем автодорожка на
языке оригинала (`xx-orig`, так YouTube помечает распознанную речь), затем
автодорожка на языке из `--lang` (это уже машинный перевод).

Текст пишется в `/tmp/yt-subs/<id>.txt` абзацами, в stdout идут название,
канал, длительность, выбранная дорожка и путь к файлу. Файл читается
инструментом Read, а не печатается целиком: вывод Bash обрезается.

yt-dlp берётся через `uvx yt-dlp@latest`: YouTube ломает старые версии
часто, а свежая подтягивается сама. `--ignore-config` отсекает конфиги
с машины, адрес передаётся после `--`, чтобы не читаться как флаг.

Граница адресов проверяется до запросов; редиректы и смену DNS внутри
yt-dlp эта проверка не закрывает (ревью 12.10.2026).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ...approval import _curl_url_ok
from ...i18n import _

OUT_DIR = Path("/tmp/yt-subs")
TIMEOUT_SEC = 300
PARAGRAPH_CHARS = 800

_YTDLP_BASE = ["--ignore-config", "--no-playlist", "--no-warnings"]
_TAG_RE = re.compile(r"<[^>]+>")
_TS_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s+-->")


def _uvx() -> str:
    found = shutil.which("uvx")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uvx"
    if fallback.exists():
        return str(fallback)
    raise SystemExit(_("uvx не найден: yt-dlp запускать нечем"))


def _ytdlp(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = [_uvx(), "yt-dlp@latest", *_YTDLP_BASE, *args]
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(_("yt-dlp не уложился в {seconds} с").format(seconds=TIMEOUT_SEC))


def _fail(proc: subprocess.CompletedProcess[str], what: str) -> None:
    tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-5:])
    raise SystemExit(_("{what}: yt-dlp вернул {rc}\n{tail}").format(
        what=what, rc=proc.returncode, tail=tail))


def pick_track(info: dict, langs: list[str]) -> tuple[str, bool] | None:
    """(ключ дорожки, автоматическая ли) по порядку из шапки модуля."""
    manual = {k for k, v in (info.get("subtitles") or {}).items() if v and k != "live_chat"}
    auto = {k for k, v in (info.get("automatic_captions") or {}).items() if v}

    def by_lang(keys: set[str]) -> str | None:
        for lang in langs:
            if lang in keys:
                return lang
            prefixed = sorted(k for k in keys if k.startswith(lang + "-"))
            if prefixed:
                return prefixed[0]
        return None

    if found := by_lang(manual):
        return found, False
    if manual and not langs:
        return sorted(manual)[0], False
    orig = sorted(k for k in auto if k.endswith("-orig"))
    if orig:
        return orig[0], True
    if found := by_lang(auto):
        return found, True
    if manual:
        return sorted(manual)[0], False
    return None


def vtt_to_lines(text: str) -> list[str]:
    """Строки реплик без таймкодов и тегов. Автосубтитры YouTube идут
    «катящимися» репликами (каждая повторяет предыдущую строку), поэтому
    повтор подряд выбрасывается."""
    out: list[str] = []
    prev = ""
    in_note = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            in_note = False
            continue
        if in_note:
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            in_note = True
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:")) or "-->" in line or _TS_RE.match(line):
            continue
        if line.isdigit():
            continue  # номер реплики в SRT-подобных файлах
        cleaned = html.unescape(_TAG_RE.sub("", line)).strip()
        if not cleaned or cleaned == prev:
            continue
        out.append(cleaned)
        prev = cleaned
    return out


def to_paragraphs(lines: list[str], width: int = PARAGRAPH_CHARS) -> str:
    """Абзацы примерно по `width` знаков: одна строка на реплику дала бы
    тысячи строк, а одна строка на всё видео не читается Read целиком."""
    paras: list[str] = []
    cur: list[str] = []
    size = 0
    for line in lines:
        cur.append(line)
        size += len(line) + 1
        if (size >= width and line.endswith((".", "!", "?", "…"))) or size >= width * 2:
            paras.append(" ".join(cur))
            cur, size = [], 0
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yt_subs", description=__doc__.split("\n", 1)[0])
    p.add_argument("url")
    p.add_argument(
        "--lang", default="ru,en",
        help="языки по предпочтению через запятую (по умолчанию ru,en)",
    )
    args = p.parse_args(argv)

    # Та же граница адресов, что у curl/WebFetch (ревью 12.10.2026).
    if not _curl_url_ok(args.url):
        raise SystemExit(_("нужна публичная ссылка http(s):// без IP-адреса"))
    langs = [x.strip() for x in args.lang.split(",") if x.strip()]

    with tempfile.TemporaryDirectory(prefix="yt-subs-") as tmp:
        work = Path(tmp)
        proc = _ytdlp(["-J", "--skip-download", "--", args.url], work)
        if proc.returncode != 0:
            _fail(proc, _("метаданные не получены"))
        info = json.loads(proc.stdout)
        if info.get("_type") == "playlist":
            raise SystemExit(_("это плейлист, нужна ссылка на одно видео"))

        track = pick_track(info, langs)
        if track is None:
            raise SystemExit(_("у видео нет субтитров, ни ручных, ни автоматических"))
        key, is_auto = track

        # Проверяем все форматы выбранной дорожки до загрузки (ревью 12.10.2026).
        tracks = info.get("automatic_captions" if is_auto else "subtitles") or {}
        if any(not _curl_url_ok(fmt.get("url", "")) for fmt in tracks[key]):
            raise SystemExit(_("дорожка содержит непубличный адрес"))

        info_path = work / "info.json"
        info_path.write_text(proc.stdout, encoding="utf-8")
        proc = _ytdlp([
            "--load-info-json", str(info_path), "--skip-download",
            "--write-auto-subs" if is_auto else "--write-subs",
            "--sub-langs", re.escape(key), "--sub-format", "vtt/best",
            "-o", str(work / "v.%(ext)s"),
        ], work)
        if proc.returncode != 0:
            _fail(proc, _("дорожка {key} не скачалась").format(key=key))
        files = sorted(work.glob("v.*"))
        files = [f for f in files if f.suffix != ".json"]
        if not files:
            raise SystemExit(_("yt-dlp не записал файл дорожки {key}").format(key=key))
        lines = vtt_to_lines(files[0].read_text(encoding="utf-8", errors="replace"))

    if not lines:
        raise SystemExit(_("дорожка {key} пустая").format(key=key))
    vid = re.sub(r"[^\w-]", "_", str(info.get("id") or "video"))[:80]
    OUT_DIR.mkdir(mode=0o700, exist_ok=True)
    out = OUT_DIR / f"{vid}.txt"
    text = to_paragraphs(lines)
    out.write_text(text, encoding="utf-8")

    print(_("название: {title}").format(title=info.get('title') or '?'))
    print(_("канал: {channel}").format(channel=info.get('channel') or info.get('uploader') or '?'))
    print(_("длительность: {duration}").format(duration=info.get('duration_string') or '?'))
    print(_("дорожка: {key} ({kind})").format(
        key=key, kind=_('автоматическая') if is_auto else _('ручная')))
    print(_("текст: {path} ({count} знаков)").format(path=out, count=len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

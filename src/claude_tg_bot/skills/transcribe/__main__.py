"""Расшифровка записи (аудио или видео) из локального файла через OpenAI.

Зачем: 19.10.2026 агент расшифровывал запись интервью однострочником на
Python, который сам читал `.env` бота ради ключа OpenAI. Такая команда
справедливо идёт через карточку (трогает секрет), а расшифровка нужна часто.
Вызов скилла бота из его venv проходит без карточки, ключ скилл берёт из
`.env` сам, и агенту его видеть не нужно.

Что делает: ffmpeg перекодирует звук в моно mp3 и режет на куски по
`CHUNK_SEC` (предел OpenAI 25 МБ на файл), куски расшифровываются
параллельно и склеиваются по порядку. По умолчанию модель распознавания
из настроек бота (OPENAI_STT_MODEL, подсказки и языки оттуда же), каждый
кусок с меткой начала `[ЧЧ:ММ:СС]`. С `--segments` берётся whisper-1 с
метками на каждую фразу; язык он определяет сам, если `--lang` не задаёт
ровно один.

Границы: только обычный файл, путь с секретным именем отклоняется до чтения,
ffmpeg получает его как `file:` (никаких сетевых протоколов) и пропускает
лишь звуковую дорожку, так что файл без звука до OpenAI не доходит.
Запись длиннее `MAX_SEC` не расшифровывается: скилл идёт без карточки, и
расход должен быть ограничен.

Текст пишется в `/tmp/transcribe/<имя>.txt`, в stdout идут длительность,
число кусков, модель и путь. Файл читается инструментом Read.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ... import voice
from ...config import load_config
from ...danger import _secret_path
from ...i18n import _

OUT_DIR = Path("/tmp/transcribe")
CHUNK_SEC = 600
MAX_SEC = 4 * 3600
PARALLEL = 4
FFMPEG_TIMEOUT_SEC = 600
SEGMENTS_MODEL = "whisper-1"


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def check_input(raw: str) -> Path:
    """Путь до чтения: без секретов, обычный файл."""
    if _secret_path(raw, str(Path.cwd())):
        raise SystemExit(_("путь похож на секрет, расшифровывать не буду"))
    path = Path(raw).expanduser().resolve()
    if _secret_path(str(path)):
        raise SystemExit(_("путь похож на секрет, расшифровывать не буду"))
    if not path.is_file():
        raise SystemExit(_("нет такого файла: {path}").format(path=path))
    return path


def probe_duration(path: Path) -> float:
    """Длительность звуковой дорожки; нет дорожки, нет и расшифровки."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type:format=duration",
             "-of", "json", f"file:{path}"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(_("ffprobe не уложился в минуту"))
    info = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    if not info.get("streams"):
        raise SystemExit(_("в файле нет звуковой дорожки"))
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise SystemExit(_("ffprobe не назвал длительность"))


def split_audio(path: Path, work: Path) -> list[Path]:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", f"file:{path}",
             "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-b:a", "48k",
             "-f", "segment", "-segment_time", str(CHUNK_SEC),
             str(work / "c%03d.mp3")],
            capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(_("ffmpeg не уложился в {seconds} с").format(seconds=FFMPEG_TIMEOUT_SEC))
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise SystemExit(_("ffmpeg вернул {rc}\n{tail}").format(rc=proc.returncode, tail=tail))
    chunks = sorted(work.glob("c*.mp3"))
    if not chunks:
        raise SystemExit(_("ffmpeg не записал ни одного куска"))
    return chunks


def segment_lines(segments: list, offset: float) -> list[str]:
    out = []
    for seg in segments:
        start = seg["start"] if isinstance(seg, dict) else seg.start
        text = (seg["text"] if isinstance(seg, dict) else seg.text).strip()
        if text:
            out.append(f"[{hms(offset + start)}] {text}")
    return out


async def _one(client, cfg, chunk: Path, offset: float, segments: str | None) -> str:
    """segments: None для модели бота, иначе язык для whisper-1 ("" значит
    определить самому). Язык и подсказку из настроек бота whisper-1 не
    отдаём: при `ru` или русской подсказке он переводит английскую речь на
    русский (проверено 19.10.2026)."""
    if segments is None:
        text = await voice.transcribe(client, cfg, chunk)
        return f"[{hms(offset)}] {text}" if text else ""
    kwargs: dict = {"model": SEGMENTS_MODEL, "response_format": "verbose_json"}
    if segments:
        kwargs["language"] = segments
    if cfg.openai_stt_prompt:
        kwargs["prompt"] = cfg.openai_stt_prompt
    resp = await client.audio.transcriptions.create(
        file=(chunk.name, chunk.read_bytes()), **kwargs)
    return "\n".join(segment_lines(resp.segments or [], offset))


async def transcribe_chunks(client, cfg, chunks: list[Path], segments: str | None) -> list[str]:
    sem = asyncio.Semaphore(PARALLEL)

    async def run(i: int, chunk: Path) -> str:
        async with sem:
            return await _one(client, cfg, chunk, i * CHUNK_SEC, segments)

    return await asyncio.gather(*(run(i, c) for i, c in enumerate(chunks)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="transcribe", description=__doc__.split("\n", 1)[0])
    p.add_argument("file")
    p.add_argument("--prompt", default="",
                   help="подсказка модели: тема, имена, термины (заменяет OPENAI_STT_PROMPT)")
    p.add_argument("--lang", default="",
                   help="языки записи через запятую, например ru,en (заменяет OPENAI_STT_LANGUAGES)")
    p.add_argument("--segments", action="store_true",
                   help="метка времени на каждую фразу (модель whisper-1; язык определяет "
                        "сама, если в --lang не задан ровно один)")
    args = p.parse_args(argv)

    path = check_input(args.file)
    duration = probe_duration(path)
    if duration > MAX_SEC:
        raise SystemExit(_("запись {duration} длиннее предела {limit}").format(
            duration=hms(duration), limit=hms(MAX_SEC)))

    cfg = load_config(skip_hook_validation=True)
    changes: dict = {}
    if args.prompt or args.segments:
        changes["openai_stt_prompt"] = args.prompt
    if args.lang:
        changes["openai_stt_languages"] = tuple(
            x.strip() for x in args.lang.split(",") if x.strip())
    cfg = dataclasses.replace(cfg, **changes)
    client = voice.make_client(cfg)
    if client is None:
        raise SystemExit(_("у бота нет ключа OpenAI или пакета openai"))

    with tempfile.TemporaryDirectory(prefix="transcribe-") as tmp:
        chunks = split_audio(path, Path(tmp))
        whisper_lang = None
        if args.segments:
            langs = changes.get("openai_stt_languages", ())
            whisper_lang = langs[0] if len(langs) == 1 else ""
        parts = asyncio.run(transcribe_chunks(client, cfg, chunks, whisper_lang))

    text = "\n\n".join(x for x in parts if x).strip()
    if not text:
        raise SystemExit(_("модель вернула пустой текст"))
    name = re.sub(r"[^\w-]", "_", path.stem)[:80] or "audio"
    OUT_DIR.mkdir(mode=0o700, exist_ok=True)
    out = OUT_DIR / f"{name}.txt"
    out.write_text(text + "\n", encoding="utf-8")

    print(_("длительность: {duration}").format(duration=hms(duration)))
    print(_("кусков: {count}, модель: {model}").format(
        count=len(chunks), model=SEGMENTS_MODEL if args.segments else cfg.openai_stt_model))
    print(_("текст: {path} ({count} знаков)").format(path=out, count=len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

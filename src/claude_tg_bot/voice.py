"""STT (gpt-transcribe) + TTS (gpt-4o-mini-tts) через OpenAI API.

Telegram voice приходит в OGG+Opus — модели транскрипции его едят напрямую,
но gpt-transcribe/gpt-4o-*-transcribe отвергают расширение .oga (400
Unsupported file format), поэтому файл отдаём под именем *.ogg.
TTS просим в opus-формате — сразу годится для `send_voice`.

OpenAI SDK — optional-extra (`pip install -e '.[voice]'`). Если пакет не
установлен или ключ пуст — голос выключается, бот продолжает работать только с текстом.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    from openai import AsyncOpenAI

log = logging.getLogger(__name__)


def make_client(cfg: Config) -> Any | None:
    if not cfg.openai_api_key:
        return None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        log.warning("openai SDK not installed; voice disabled. `pip install -e '.[voice]'`")
        return None
    return AsyncOpenAI(api_key=cfg.openai_api_key)


async def transcribe(client: "AsyncOpenAI", cfg: Config, audio_path: Path) -> str:
    # Контейнер тот же OGG/Opus — ломается только валидация имени, см. docstring.
    filename = audio_path.name
    if filename.endswith(".oga"):
        filename = filename[:-4] + ".ogg"
    kwargs: dict[str, Any] = {"model": cfg.openai_stt_model}
    if cfg.openai_stt_model.startswith("gpt-transcribe"):
        # gpt-transcribe: prompt — описание контекста, keywords — буквальные
        # термины (имена проектов/технологий), languages — подсказки языков.
        # Одиночный language не поддерживается, response_format только json.
        kwargs["response_format"] = "json"
        if cfg.openai_stt_prompt:
            kwargs["prompt"] = cfg.openai_stt_prompt
        extra: dict[str, Any] = {}
        if cfg.openai_stt_keywords:
            extra["keywords"] = list(cfg.openai_stt_keywords)
        if cfg.openai_stt_languages:
            extra["languages"] = list(cfg.openai_stt_languages)
        if extra:
            kwargs["extra_body"] = extra
    else:
        # whisper-1 / gpt-4o-*-transcribe: language заметно снижает latency
        # (без него модель сначала детектит язык), prompt подсказывает термины.
        if cfg.openai_stt_languages:
            kwargs["language"] = cfg.openai_stt_languages[0]
        if cfg.openai_stt_prompt:
            kwargs["prompt"] = cfg.openai_stt_prompt
    resp = await client.audio.transcriptions.create(
        file=(filename, audio_path.read_bytes()),
        **kwargs,
    )
    return (resp.text or "").strip()


async def synthesize(client: "AsyncOpenAI", cfg: Config, text: str) -> bytes:
    clipped = text.strip()
    if len(clipped) > cfg.voice_reply_max_chars:
        clipped = clipped[: cfg.voice_reply_max_chars].rstrip() + "…"
    # instructions поддерживаются только gpt-4o-mini-tts (steerability). Для
    # tts-1/tts-1-hd передавать НЕ нужно — API не поймёт. Если пусто — не шлём.
    kwargs: dict[str, Any] = {
        "model": cfg.openai_tts_model,
        "voice": cfg.openai_tts_voice,
        "input": clipped,
        "response_format": "opus",
    }
    if cfg.openai_tts_instructions and "gpt-4o-mini-tts" in cfg.openai_tts_model:
        kwargs["instructions"] = cfg.openai_tts_instructions
    resp = await client.audio.speech.create(**kwargs)
    # openai 2.x: resp — AsyncHttpxBinaryResponseContent, .content отдаёт все байты
    return resp.content

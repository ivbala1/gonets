"""STT (gpt-transcribe) + TTS (gpt-4o-mini-tts) через OpenAI API.

Telegram voice приходит в OGG+Opus — модели транскрипции его едят напрямую,
но gpt-transcribe/gpt-4o-*-transcribe отвергают расширение .oga (400
Unsupported file format), поэтому файл отдаём под именем *.ogg.
TTS просим в opus-формате — сразу годится для `send_voice`.

OpenAI SDK — optional-extra (`pip install -e '.[voice]'`). Если пакет не
установлен или ключ пуст — голос выключается, бот продолжает работать только с текстом.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import procs
from .config import Config, agent_env

if TYPE_CHECKING:
    from openai import AsyncOpenAI

log = logging.getLogger(__name__)


# Ответ 429 с этими кодами значит пустой баланс или достигнутый лимит
# расходов проекта: повтор бесполезен, нужен платёж или новый лимит (случай
# 02.11.2026: голос не распознался дважды подряд).
QUOTA_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted"})
BILLING_URL = "platform.openai.com/settings/organization/billing"
# Срок одной попытки распознавания: у SDK по умолчанию 600 с на попытку, и
# зависшее соединение отдавало голос запасному пути через 10-30 минут (ревью
# 04.11.2026). gpt-transcribe разбирает 15 минут звука за десятки секунд.
OPENAI_STT_TIMEOUT_SEC = 120.0
# То же для озвучки: без срока зависшее соединение держало бы серию голосовых
# по 600 с на попытку. Кусок до VOICE_REPLY_MAX_CHARS (4000 знаков)
# озвучивается за десятки секунд. Оба срока httpx на каждую фазу (соединение,
# запись, пауза между порциями ответа), а не общий срок попытки.
OPENAI_TTS_TIMEOUT_SEC = 120.0


def _is_quota_code(value: Any) -> bool:
    """Только строка: список или словарь в чужом теле не роняет разбор."""
    return isinstance(value, str) and value in QUOTA_CODES


def _quota_in_body(body: Any) -> bool:
    """Тело ошибки OpenAI: {"error": {"code", "type"}} или сразу объект error
    (так его кладёт SDK в APIStatusError.body)."""
    if not isinstance(body, dict):
        return False
    inner = body.get("error")
    for obj in (body, inner if isinstance(inner, dict) else {}):
        if _is_quota_code(obj.get("code")) or _is_quota_code(obj.get("type")):
            return True
    return False


def quota_exhausted(err: BaseException) -> bool:
    """Исключение SDK о пустом балансе: 429 и код из QUOTA_CODES. Без импорта
    openai: пакет необязательный."""
    if getattr(err, "status_code", None) != 429:
        return False
    if _is_quota_code(getattr(err, "code", None)) or _is_quota_code(getattr(err, "type", None)):
        return True
    return _quota_in_body(getattr(err, "body", None))


def _response_quota_exhausted(response: Any) -> bool:
    """httpx-ответ 429 о пустом балансе. Тело ещё не прочитано: не знаем."""
    if getattr(response, "status_code", None) != 429:
        return False
    try:
        return _quota_in_body(json.loads(response.content))
    except Exception:
        return False


def _no_quota_retry(base: type) -> type:
    """Клиент, который не повторяет запрос при пустом балансе. Остальные
    повторы SDK (429 по частоте, 5xx, обрывы) остаются как были."""

    class NoQuotaRetryClient(base):  # type: ignore[misc, valid-type]
        def _should_retry(self, response: Any) -> bool:
            if _response_quota_exhausted(response):
                log.warning("OpenAI quota exhausted (429), not retrying")
                return False
            return super()._should_retry(response)

    return NoQuotaRetryClient


def make_client(cfg: Config) -> Any | None:
    if not cfg.openai_api_key:
        return None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        log.warning("openai SDK not installed; voice disabled. `pip install -e '.[voice]'`")
        return None
    return _no_quota_retry(AsyncOpenAI)(api_key=cfg.openai_api_key)


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
        timeout=OPENAI_STT_TIMEOUT_SEC,
        **kwargs,
    )
    return (resp.text or "").strip()


# Запасное распознавание GigaAM v3 на машине бота (BACKLOG «Новое (02.11.2026)»):
# отдельный процесс своего окружения, см. local_stt_worker.py. Замер 04.11.2026
# на ПК (i7-7700, без видеокарты): минута голоса около 9 секунд вместе с
# загрузкой модели, память около 0,5 ГБ.
LOCAL_STT_WORKER = Path(__file__).with_name("local_stt_worker.py")
_LOCAL_STT_TIMEOUT_BASE = 60.0
_LOCAL_STT_TIMEOUT_PER_SEC = 1.0
_LOCAL_STT_TIMEOUT_MAX = 1800.0


# Один процесс модели за раз: при пустом балансе каждое голосовое идёт сюда,
# пачка пересланных подняла бы несколько моделей на четырёх ядрах (ревью).
_LOCAL_STT_LOCK = asyncio.Semaphore(1)


def local_stt_ready(cfg: Config) -> bool:
    """Запасной путь настроен: интерпретатор, модель и VAD на месте. getattr:
    упрощённые конфиги тестов и инструментов этих полей не знают."""
    py, model, vad = (getattr(cfg, name, "") for name in (
        "local_stt_python", "local_stt_model_dir", "local_stt_vad_model"))
    return bool(py and model and vad) and os.access(py, os.X_OK) \
        and os.path.isdir(model) and os.path.isfile(vad)


def log_local_stt_status(cfg: Config) -> None:
    """При старте: запасной путь включён, выключен или настроен не до конца
    (опечатка в пути молча выключала бы его)."""
    if local_stt_ready(cfg):
        log.info("local STT fallback: enabled (%s)", os.path.basename(cfg.local_stt_model_dir.rstrip("/")))
    elif any(getattr(cfg, name, "") for name in (
            "local_stt_python", "local_stt_model_dir", "local_stt_vad_model")):
        log.warning("local STT fallback: DISABLED, LOCAL_STT_PYTHON/MODEL_DIR/VAD_MODEL "
                    "incomplete or not found (python must be a full path)")
    else:
        log.info("local STT fallback: off")


def _local_timeout(duration_sec: float | None) -> float:
    return min(_LOCAL_STT_TIMEOUT_MAX,
               _LOCAL_STT_TIMEOUT_BASE + _LOCAL_STT_TIMEOUT_PER_SEC * max(0.0, duration_sec or 0.0))


async def transcribe_local(cfg: Config, audio_path: Path, duration_sec: float | None = None) -> str:
    """Распознать файл GigaAM в отдельном процессе. Сбой, таймаут или
    непонятный ответ: RuntimeError (причина в журнале, без текста речи)."""
    async with _LOCAL_STT_LOCK:
        return await _transcribe_local_once(cfg, audio_path, duration_sec)


async def _transcribe_local_once(cfg: Config, audio_path: Path, duration_sec: float | None) -> str:
    # Своя группа процессов: при таймауте или отмене гаснет и ffmpeg воркера,
    # даже если сам воркер уже вышел (procs.run_bounded).
    rc, out, err = await procs.run_bounded(
        [cfg.local_stt_python, "-I", str(LOCAL_STT_WORKER),
         cfg.local_stt_model_dir, cfg.local_stt_vad_model, str(audio_path)],
        timeout=_local_timeout(duration_sec),
        env=agent_env(),  # модели на диске секреты бота не нужны
    )
    if rc != 0:
        tail = err.splitlines()[-1:] or [""]
        raise RuntimeError(f"local STT exit {rc}: {tail[0][:300]}")
    try:
        text = json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])["text"]
    except (ValueError, IndexError, KeyError, TypeError) as e:
        raise RuntimeError("local STT: bad output") from e
    if not isinstance(text, str):
        raise RuntimeError("local STT: bad output")
    return text.strip()


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
    resp = await client.audio.speech.create(timeout=OPENAI_TTS_TIMEOUT_SEC, **kwargs)
    # openai 2.x: resp — AsyncHttpxBinaryResponseContent, .content отдаёт все байты
    return resp.content

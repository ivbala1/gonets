"""Локальное распознавание голоса GigaAM v3 (sherpa-onnx) для запасного пути.

Запускается ОТДЕЛЬНЫМ интерпретатором окружения с sherpa-onnx и numpy
(`LOCAL_STT_PYTHON`), не процессом бота: тяжёлая модель не живёт в памяти бота,
падение не роняет его. Только stdlib, numpy и sherpa_onnx, без импорта пакета.

    python -I local_stt_worker.py <каталог модели> <silero_vad.onnx> <аудиофайл>

Ответ: одна строка JSON `{"text": "..."}` в stdout, код 0. Сбой: код не 0,
причина в stderr.

GigaAM берёт не больше ~25 секунд за раз: короткий звук идёт целиком, длинный
режется по паузам Silero VAD. VAD после MAX_SPEECH_SEC лишь ужесточает порог и
жёсткого реза не делает, поэтому кусок длиннее HARD_CUT_SEC режется ровно.
Куски распознаются по одному: пакетом sherpa дополняет их до общей длины и
держит всё в памяти разом (ревью 04.11.2026).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

SAMPLE_RATE = 16000
SINGLE_PASS_SEC = 20.0
MAX_SPEECH_SEC = 20.0
HARD_CUT_SEC = 25.0


def _decode(path: str):
    import numpy as np

    # `file:` не даёт ffmpeg толковать имя как протокол или URL.
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", "file:" + os.path.abspath(path),
         "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        check=True, capture_output=True, timeout=120,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _even(samples, step_sec: float) -> list:
    step = int(step_sec * SAMPLE_RATE)
    return [samples[i:i + step] for i in range(0, len(samples), step)]


def _cap(segments: list) -> list:
    out = []
    for seg in segments:
        out.extend(_even(seg, MAX_SPEECH_SEC) if len(seg) > HARD_CUT_SEC * SAMPLE_RATE else [seg])
    return out


def _segments(samples, vad_model: str) -> list:
    if len(samples) <= SINGLE_PASS_SEC * SAMPLE_RATE:
        return [samples] if len(samples) else []
    import numpy as np
    import sherpa_onnx

    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = vad_model
    cfg.silero_vad.min_silence_duration = 0.3
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.silero_vad.max_speech_duration = MAX_SPEECH_SEC
    cfg.sample_rate = SAMPLE_RATE
    window = cfg.silero_vad.window_size
    vad = sherpa_onnx.VoiceActivityDetector(
        cfg, buffer_size_in_seconds=len(samples) / SAMPLE_RATE + 5)
    out = []
    for start in range(0, len(samples), window):
        vad.accept_waveform(samples[start:start + window])
        while not vad.empty():
            out.append(np.asarray(vad.front.samples, dtype=np.float32))
            vad.pop()
    vad.flush()
    while not vad.empty():
        out.append(np.asarray(vad.front.samples, dtype=np.float32))
        vad.pop()
    # VAD не нашёл речи (тихая запись): режем ровно, пусть решит модель.
    return _cap(out) if out else _even(samples, MAX_SPEECH_SEC)


def transcribe(model_dir: str, vad_model: str, audio: str) -> str:
    import sherpa_onnx

    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=os.path.join(model_dir, "encoder.int8.onnx"),
        decoder=os.path.join(model_dir, "decoder.onnx"),
        joiner=os.path.join(model_dir, "joiner.onnx"),
        tokens=os.path.join(model_dir, "tokens.txt"),
        num_threads=max(1, min(4, os.cpu_count() or 1)),
        sample_rate=SAMPLE_RATE,
        feature_dim=64,
        decoding_method="greedy_search",
        model_type="nemo_transducer",
    )
    parts = []
    for seg in _segments(_decode(audio), vad_model):
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, seg)
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: local_stt_worker.py <model_dir> <silero_vad.onnx> <audio>", file=sys.stderr)
        return 2
    text = transcribe(argv[1], argv[2], argv[3])
    sys.stdout.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

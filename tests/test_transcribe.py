"""Тесты transcribe: граница входа, метки времени."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_tg_bot.skills.transcribe.__main__ import (
    check_input,
    hms,
    probe_duration,
    segment_lines,
)

# Настоящий ffmpeg есть не везде (у раннера GitHub его нет).
_NO_FFMPEG = not (shutil.which("ffmpeg") and shutil.which("ffprobe"))


class HelpersTests(unittest.TestCase):
    def test_hms(self) -> None:
        self.assertEqual(hms(0), "00:00:00")
        self.assertEqual(hms(3725.9), "01:02:05")

    def test_segment_lines_offset_and_empty(self) -> None:
        segs = [{"start": 5.2, "text": " Привет "}, {"start": 7, "text": "  "}]
        self.assertEqual(segment_lines(segs, 600), ["[00:10:05] Привет"])


class InputTests(unittest.TestCase):
    def test_missing_file_refused(self) -> None:
        with self.assertRaises(SystemExit):
            check_input("/nonexistent/x.m4a")

    @unittest.skipIf(_NO_FFMPEG, "нет ffmpeg или ffprobe")
    def test_file_without_audio_refused(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".m4a") as f:
            f.write(b"not audio at all")
            f.flush()
            with self.assertRaises(SystemExit):
                probe_duration(Path(f.name))

    @unittest.skipIf(_NO_FFMPEG, "нет ffmpeg или ffprobe")
    def test_real_audio_duration(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "tone.mp3"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=2", str(out)],
                check=True, timeout=60,
            )
            self.assertAlmostEqual(probe_duration(out), 2, delta=0.2)


if __name__ == "__main__":
    unittest.main()

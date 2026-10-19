"""Тесты yt_subs: выбор дорожки, чистка VTT, абзацы и то, что вызов скилла
со ссылкой YouTube проходит без карточки."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_tg_bot.approval import _SKILL_PYTHON, _bash_auto_allowed  # noqa: E402
from claude_tg_bot.skills.yt_subs.__main__ import (  # noqa: E402
    pick_track,
    to_paragraphs,
    vtt_to_lines,
)

_FMT = [{"ext": "vtt", "url": "x"}]


class PickTrackTests(unittest.TestCase):
    def test_manual_in_preferred_lang_wins(self) -> None:
        info = {
            "subtitles": {"en": _FMT, "ru": _FMT},
            "automatic_captions": {"ru-orig": _FMT, "ru": _FMT},
        }
        self.assertEqual(pick_track(info, ["ru", "en"]), ("ru", False))

    def test_manual_prefix_match(self) -> None:
        info = {"subtitles": {"en-US": _FMT}, "automatic_captions": {}}
        self.assertEqual(pick_track(info, ["ru", "en"]), ("en-US", False))

    def test_original_auto_beats_translation(self) -> None:
        # Английское видео: автоперевод на русский хуже оригинала.
        info = {"automatic_captions": {"ru": _FMT, "en": _FMT, "en-orig": _FMT}}
        self.assertEqual(pick_track(info, ["ru", "en"]), ("en-orig", True))

    def test_auto_translation_when_no_orig(self) -> None:
        info = {"automatic_captions": {"de": _FMT, "en": _FMT}}
        self.assertEqual(pick_track(info, ["ru", "en"]), ("en", True))

    def test_other_manual_before_nothing(self) -> None:
        info = {"subtitles": {"ka": _FMT}, "automatic_captions": {}}
        self.assertEqual(pick_track(info, ["ru", "en"]), ("ka", False))

    def test_live_chat_and_empty_ignored(self) -> None:
        info = {"subtitles": {"live_chat": _FMT, "ru": []}, "automatic_captions": None}
        self.assertIsNone(pick_track(info, ["ru"]))


class VttTests(unittest.TestCase):
    def test_rolling_auto_captions_deduped(self) -> None:
        vtt = (
            "WEBVTT\nKind: captions\nLanguage: ru\n\n"
            "00:00:00.000 --> 00:00:02.000 align:start position:0%\n"
            "Всем<00:00:00.500><c> привет</c>\n\n"
            "00:00:02.000 --> 00:00:02.010\n"
            "Всем привет\n\n"
            "00:00:02.010 --> 00:00:04.000\n"
            "Всем привет\n"
            "с вами подкаст &amp; гости\n\n"
            "NOTE служебное\nне текст\n\n"
            "1\n00:00:04,000 --> 00:00:05,000\nконец\n"
        )
        self.assertEqual(
            vtt_to_lines(vtt), ["Всем привет", "с вами подкаст & гости", "конец"],
        )

    def test_paragraphs_break_on_sentence_end(self) -> None:
        lines = ["а" * 30 + ".", "б" * 30, "в" * 30 + "."]
        text = to_paragraphs(lines, width=40)
        self.assertEqual(text.strip().split("\n\n")[0], "а" * 30 + ". " + "б" * 30 + " " + "в" * 30 + ".")
        long_run = to_paragraphs(["г" * 50] * 3, width=40)
        self.assertEqual(len(long_run.strip().split("\n\n")), 2)


class AutoAllowTests(unittest.TestCase):
    _CMD = f"{_SKILL_PYTHON} -m claude_tg_bot.skills.yt_subs"

    def test_call_with_url_needs_no_card(self) -> None:
        for cmd in (
            self._CMD + ' "https://www.youtube.com/watch?v=LgWaWJ2Xv4I&t=60s"',
            self._CMD + " 'https://youtu.be/FxkfmkUrQCI' --lang en",
            self._CMD + ' "https://youtu.be/x" 2>&1',
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(_bash_auto_allowed(cmd, None))

    def test_unquoted_ampersand_stays_on_card(self) -> None:
        # Без кавычек `&` отправит skill в фон и выполнит хвост отдельно.
        cmd = self._CMD + " https://www.youtube.com/watch?v=x&rm -rf ~"
        self.assertFalse(_bash_auto_allowed(cmd, None))


if __name__ == "__main__":
    unittest.main()

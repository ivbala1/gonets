"""Запасное распознавание голоса GigaAM (BACKLOG «Новое (02.11.2026)»): когда
OpenAI не распознал (пустой баланс, сбой) или ключа нет, бот распознаёт голос
на своей машине отдельным процессом и помечает эхо."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import importlib.util

from claude_tg_bot import bot as botmod
from claude_tg_bot import voice
from claude_tg_bot.i18n import _
from tests.test_openai_quota import _STT_QUOTA, _quota_error
from tests.test_queue2 import _VoiceBase, _settle, _update


def _cfg(py: str = "", model: str = "", vad: str = "") -> SimpleNamespace:
    return SimpleNamespace(local_stt_python=py, local_stt_model_dir=model, local_stt_vad_model=vad)


class ReadyTests(unittest.TestCase):
    def test_off_when_any_empty_or_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            vad = Path(d) / "vad.onnx"
            vad.write_bytes(b"x")
            self.assertFalse(voice.local_stt_ready(_cfg()))
            self.assertFalse(voice.local_stt_ready(_cfg(sys.executable, d, "")))
            self.assertFalse(voice.local_stt_ready(_cfg(sys.executable, d + "/nope", str(vad))))
            self.assertFalse(voice.local_stt_ready(_cfg(d + "/python", d, str(vad))))
            self.assertTrue(voice.local_stt_ready(_cfg(sys.executable, d, str(vad))))


class TranscribeLocalTests(unittest.TestCase):
    """Настоящий подпроцесс: вместо обработчика GigaAM маленький скрипт."""

    def _run(self, body: str, **kw) -> str:
        with tempfile.TemporaryDirectory() as d:
            worker = Path(d) / "w.py"
            worker.write_text("import sys, time, json\n" + body)
            audio = Path(d) / "a.ogg"
            audio.write_bytes(b"x")
            with patch.multiple(voice, LOCAL_STT_WORKER=worker, **kw):
                return asyncio.run(voice.transcribe_local(_cfg(sys.executable, d, d), audio, 3))

    def test_reads_last_json_line_and_passes_paths(self) -> None:
        text = self._run(
            "print('noise')\n"
            "print(json.dumps({'text': '  ' + str(len(sys.argv)) + ' ' + sys.argv[3][-5:] + '  '}))\n")
        self.assertEqual(text, "4 a.ogg")

    def test_nonzero_exit_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exit 3: boom"):
            self._run("sys.stderr.write('trace\\nboom\\n'); sys.exit(3)\n")

    def test_bad_output_raises(self) -> None:
        for body in ("print('not json')\n", "print(json.dumps({'text': 5}))\n", "pass\n"):
            with self.subTest(body=body), self.assertRaisesRegex(RuntimeError, "bad output"):
                self._run(body)

    def test_timeout_kills_worker(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "pid"
            with self.assertRaises(asyncio.TimeoutError):
                self._run(f"open({str(pidfile)!r}, 'w').write(str(__import__('os').getpid()))\n"
                          "time.sleep(30)\n",
                          _LOCAL_STT_TIMEOUT_BASE=1.0, _LOCAL_STT_TIMEOUT_PER_SEC=0.0)
            pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_cancel_kills_worker_and_its_child(self) -> None:
        """Отмена задачи (перезапуск, /cancel) гасит и ffmpeg воркера."""
        with tempfile.TemporaryDirectory() as d:
            pids = Path(d) / "pids"
            body = ("import subprocess, os\n"
                    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                    f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{c.pid}}')\n"
                    "time.sleep(30)\n")
            worker = Path(d) / "w.py"
            worker.write_text("import sys, time, json\n" + body)

            async def go() -> None:
                with patch.object(voice, "LOCAL_STT_WORKER", worker):
                    task = asyncio.create_task(voice.transcribe_local(_cfg(sys.executable, d, d), worker, 3))
                    for _ in range(100):
                        await asyncio.sleep(0.05)
                        if pids.exists() and pids.read_text().strip().count(" ") == 1:
                            break
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            asyncio.run(go())
            worker_pid, child_pid = map(int, pids.read_text().split())
        for pid in (worker_pid, child_pid):
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                __import__("time").sleep(0.05)
            else:
                self.fail(f"pid {pid} alive")

    def test_child_of_exited_worker_killed_on_timeout(self) -> None:
        """Воркер вышел сам, его потомок держит stdout: по таймауту гасится группа."""
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "pid"
            with self.assertRaises(asyncio.TimeoutError):
                self._run("import subprocess\n"
                          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                          f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
                          "sys.exit(1)\n",
                          _LOCAL_STT_TIMEOUT_BASE=1.0, _LOCAL_STT_TIMEOUT_PER_SEC=0.0)
            pid = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            __import__("time").sleep(0.05)
        else:
            self.fail(f"pid {pid} alive")

    def test_one_worker_at_a_time(self) -> None:
        async def go() -> list[str]:
            log: list[str] = []

            async def once(_cfg, _path, _dur):
                log.append("start")
                await asyncio.sleep(0.05)
                log.append("end")
                return "x"

            # Свежий семафор: модульный привязывается к циклу первого спора.
            with patch.object(voice, "_transcribe_local_once", once), \
                    patch.object(voice, "_LOCAL_STT_LOCK", asyncio.Semaphore(1)):
                await asyncio.gather(*(voice.transcribe_local(_cfg(), Path("a"), 1) for _ in range(3)))
            return log

        self.assertEqual(asyncio.run(go()), ["start", "end"] * 3)

    def test_timeout_grows_with_duration_and_is_capped(self) -> None:
        self.assertEqual(voice._local_timeout(None), voice._LOCAL_STT_TIMEOUT_BASE)
        self.assertGreater(voice._local_timeout(600), voice._local_timeout(60))
        self.assertEqual(voice._local_timeout(10 ** 6), voice._LOCAL_STT_TIMEOUT_MAX)


def _worker_module():
    spec = importlib.util.spec_from_file_location("local_stt_worker", voice.LOCAL_STT_WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WorkerCutTests(unittest.TestCase):
    """Нарезка без numpy и sherpa: на списках отсчётов."""

    def test_long_vad_piece_cut_evenly(self) -> None:
        w = _worker_module()
        sr = w.SAMPLE_RATE
        short, long_ = [0.0] * (10 * sr), [0.0] * (50 * sr)
        got = w._cap([short, long_])
        self.assertEqual([len(x) // sr for x in got], [10, 20, 20, 10])
        self.assertTrue(all(len(x) <= w.HARD_CUT_SEC * sr for x in got))

    def test_piece_at_hard_cut_kept(self) -> None:
        w = _worker_module()
        piece = [0.0] * int(w.HARD_CUT_SEC * w.SAMPLE_RATE)
        self.assertEqual(w._cap([piece]), [piece])

    def test_usage_on_wrong_args(self) -> None:
        self.assertEqual(_worker_module().main(["w.py"]), 2)


class OpenAiTimeoutTests(unittest.TestCase):
    def test_transcribe_sets_request_timeout(self) -> None:
        create = AsyncMock(return_value=SimpleNamespace(text=" ok "))
        client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
        cfg = SimpleNamespace(openai_stt_model="whisper-1", openai_stt_prompt="",
                              openai_stt_keywords=(), openai_stt_languages=())
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d) / "v.oga"
            audio.write_bytes(b"x")
            self.assertEqual(asyncio.run(voice.transcribe(client, cfg, audio)), "ok")
        self.assertEqual(create.await_args.kwargs["timeout"], voice.OPENAI_STT_TIMEOUT_SEC)
        self.assertLess(voice.OPENAI_STT_TIMEOUT_SEC, 600)


class VoiceFallbackTests(_VoiceBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.local = AsyncMock(return_value="Привет, локально.")
        for name, value in {"local_stt_ready": lambda _cfg: True,
                            "transcribe_local": self.local}.items():
            p = patch.object(botmod, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def _voice(self, stt=None):
        msg = self.voice()
        if stt is None:
            await botmod.on_voice(_update(msg), self.ctx)
        else:
            with patch.object(botmod, "transcribe", stt):
                await botmod.on_voice(_update(msg), self.ctx)
        await _settle()
        return "\n".join(self.said(msg))

    async def test_quota_goes_local_and_names_billing(self) -> None:
        said = await self._voice(AsyncMock(side_effect=_quota_error()))
        self.local.assert_awaited_once()
        self.assertIn(_("распознал локально: кончились деньги на OpenAI или достигнут "
                        "лимит расходов проекта, пополнить или поднять лимит {url}").format(
                            url=voice.BILLING_URL), said)
        self.assertIn("Привет, локально.", said)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("Привет, локально.", self.runs[0]["prompt"])

    async def test_other_failure_goes_local(self) -> None:
        said = await self._voice(AsyncMock(side_effect=RuntimeError("boom")))
        self.assertIn(_("распознал локально: OpenAI не распознал"), said)
        self.assertNotIn(voice.BILLING_URL, said)
        self.assertEqual(len(self.runs), 1)

    async def test_empty_openai_result_goes_local(self) -> None:
        self.heard = ""
        said = await self._voice()
        self.local.assert_awaited_once()
        self.assertIn(_("распознал локально: OpenAI не распознал"), said)
        self.assertEqual(len(self.runs), 1)

    async def test_openai_ok_local_untouched(self) -> None:
        self.heard = "Через OpenAI."
        said = await self._voice()
        self.local.assert_not_awaited()
        self.assertIn(_("🎙 Услышал: {heard}").format(heard="Через OpenAI."), said)

    async def test_no_key_uses_local_directly(self) -> None:
        self.bot_data["openai"] = None
        stt = AsyncMock()
        said = await self._voice(stt)
        stt.assert_not_awaited()
        self.assertIn(_("🎙 Услышал ({how}): {heard}").format(
            how=_("распознал локально"), heard="Привет, локально."), said)
        self.assertEqual(len(self.runs), 1)

    async def test_local_failure_keeps_quota_message(self) -> None:
        self.local.side_effect = RuntimeError("local boom")
        said = await self._voice(AsyncMock(side_effect=_quota_error()))
        self.local.assert_awaited_once()
        self.assertIn(_(_STT_QUOTA).format(url=voice.BILLING_URL), said)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.bot_data.get("active_work", 0), 0)

    async def test_local_failure_after_other_error_generic(self) -> None:
        self.local.side_effect = RuntimeError("local boom")
        said = await self._voice(AsyncMock(side_effect=RuntimeError("boom")))
        self.local.assert_awaited_once()
        self.assertIn(_("⚠️ не удалось распознать голос"), said)
        self.assertEqual(self.runs, [])

    async def test_empty_local_keeps_quota_reason(self) -> None:
        self.local.return_value = ""
        said = await self._voice(AsyncMock(side_effect=_quota_error()))
        self.assertIn(voice.BILLING_URL, said)
        self.assertIn(_("⚠️ пусто после распознавания ({how})").split(" (")[0], said)
        self.assertEqual(self.runs, [])

    async def test_local_holds_work_and_marks_slow_with_duration(self) -> None:
        """Пока идёт локальное распознавание: перезапуск ждёт (active_work),
        пачка склейки ждёт дольше 30 с (slow); после всё возвращается."""
        self.ctx.application.running = True  # склейка включена: есть держатель слота
        seen: list = []

        batches: list = []

        async def local(_cfg, _path, duration):
            batch = next(iter(self.bot_data.get("gather_batches", {}).values()), None)
            batches.append(batch)
            seen.append((self.bot_data.get("active_work", 0), getattr(batch, "slow", None), duration))
            if self.fail:
                raise RuntimeError("local boom")
            return "Локально."

        self.local.side_effect = local
        for self.fail in (False, True):
            with self.subTest(fail=self.fail):
                seen.clear()
                batches.clear()
                await self._voice(AsyncMock(side_effect=_quota_error()))
                self.assertEqual(seen[0], (1, 1, 2))
                self.assertIsNotNone(batches[0])
                self.assertEqual(batches[0].slow, 0)  # тот же объект после возврата
                self.assertEqual(self.bot_data.get("active_work", 0), 0)
        self.assertEqual(self.bot_data.get("active_work", 0), 0)


class VoiceLocalCancelTests(_VoiceBase):
    """/cancel посреди локального распознавания гасит его, голосовое никуда не идёт."""

    async def test_cancel_stops_local_quietly(self) -> None:
        started = asyncio.Event()
        killed: list = []

        async def local(_cfg, _path, _dur):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                killed.append(True)
                raise
            return "не должно"

        self.bot_data["openai"] = None
        msg = self.voice()
        with patch.object(botmod, "local_stt_ready", lambda _c: True), \
                patch.object(botmod, "transcribe_local", local):
            task = asyncio.create_task(botmod.on_voice(_update(msg), self.ctx))
            await asyncio.wait_for(started.wait(), 5)
            self.assertEqual(self.bot_data.get("active_work", 0), 1)
            cancel_msg = self.voice()
            await botmod.on_cancel(_update(cancel_msg), self.ctx)
            await asyncio.wait_for(task, 5)
        self.assertEqual(killed, [True])
        self.assertEqual(self.runs, [])
        self.assertEqual(self.bot_data.get("active_work", 0), 0)
        self.assertEqual(self.bot_data.get("local_stt"), {})
        said = "\n".join(self.said(msg))
        self.assertNotIn(_("⚠️ не удалось распознать голос"), said)
        self.assertIn(_("🗑 распознавание голосового отменено"), "\n".join(self.said(cancel_msg)))

    async def test_cancel_after_model_done_before_handler_resumes(self) -> None:
        """Гонка: модель уже вернула текст, обработчик ещё не продолжился, и
        тут /cancel. Отменённое не должно уйти в Claude (Sol, третий круг)."""
        counted: list[int] = []

        async def local(_cfg, _path, _dur):
            # Колбэк встаёт в очередь раньше пробуждения обработчика: сработает,
            # когда задача модели уже done, а обработчик ещё ждёт.
            asyncio.get_running_loop().call_soon(
                lambda: counted.append(botmod._local_stt_cancel(self.ctx.application)))
            return "Стоп."

        self.bot_data["openai"] = None
        msg = self.voice()
        with patch.object(botmod, "local_stt_ready", lambda _c: True), \
                patch.object(botmod, "transcribe_local", local):
            await botmod.on_voice(_update(msg), self.ctx)
            await _settle()
        self.assertEqual(counted, [1])
        self.assertEqual(self.runs, [])
        self.assertEqual(self.bot_data.get("active_work", 0), 0)
        self.assertNotIn("Стоп.", "\n".join(self.said(msg)))

    async def test_handler_cancel_propagates_and_kills_model(self) -> None:
        started = asyncio.Event()
        killed: list = []

        async def local(_cfg, _path, _dur):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                killed.append(True)
                raise

        self.bot_data["openai"] = None
        with patch.object(botmod, "local_stt_ready", lambda _c: True), \
                patch.object(botmod, "transcribe_local", local):
            task = asyncio.create_task(botmod.on_voice(_update(self.voice()), self.ctx))
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(killed, [True])
        self.assertEqual(self.bot_data.get("active_work", 0), 0)


class VoiceNoFallbackTests(_VoiceBase):
    async def test_no_key_no_local_disabled(self) -> None:
        self.bot_data["openai"] = None
        msg = self.voice()
        await botmod.on_voice(_update(msg), self.ctx)
        self.assertIn(_("⚠️ голос выключен (пустой OPENAI_API_KEY в .env)"), "\n".join(self.said(msg)))
        self.assertEqual(self.runs, [])


_REAL = {k: os.environ.get(k, "") for k in ("LOCAL_STT_PYTHON", "LOCAL_STT_MODEL_DIR", "LOCAL_STT_VAD_MODEL")}


@unittest.skipUnless(all(_REAL.values()), "нужны LOCAL_STT_* настоящей модели (проверка на ПК)")
class RealWorkerTests(unittest.TestCase):
    """Настоящий воркер и модель: короткий пример модели и он же 4 раза подряд
    (длиннее 20 с, идёт через VAD и нарезку)."""

    def _run(self, audio: Path) -> str:
        cfg = _cfg(_REAL["LOCAL_STT_PYTHON"], _REAL["LOCAL_STT_MODEL_DIR"], _REAL["LOCAL_STT_VAD_MODEL"])
        self.assertTrue(voice.local_stt_ready(cfg))
        return asyncio.run(voice.transcribe_local(cfg, audio, 60))

    def test_real_model(self) -> None:
        import subprocess
        example = Path(_REAL["LOCAL_STT_MODEL_DIR"]) / "test_wavs" / "example.wav"
        short = self._run(example)
        self.assertGreater(len(short), 20)
        with tempfile.TemporaryDirectory() as d:
            long_ = Path(d) / "long.ogg"
            subprocess.run(["ffmpeg", "-v", "error", "-stream_loop", "3", "-i", str(example),
                            "-c:a", "libopus", str(long_)], check=True)
            text = self._run(long_)
        self.assertGreater(len(text), 3 * len(short))


if __name__ == "__main__":
    unittest.main()

"""JS packaging and honest caption wait states; synthetic media/network only."""

import sys
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import test_hardening as base

from zoom2txt.gui.workers import RecoveryWorker
from zoom2txt.storage import StateStore, atomic_write
from zoom2txt.transcription import js_runtime as js
from zoom2txt.transcription import youtube_engine as yt


class CaptionRuntimeTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def test_frozen_runtime_is_explicit_and_handles_spaces(self):
        root = self.root / "frozen folder with spaces"
        binary = root / "runtime" / "deno.exe"
        atomic_write(binary, b"fixture")
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(root), create=True),
            patch.object(sys, "platform", "win32"),
        ):
            self.assertEqual(js.deno_path(), binary)
            self.assertEqual(
                js.youtube_js_args(),
                ["--no-js-runtimes", "--js-runtimes", f"deno:{binary}"],
            )

    def test_frozen_runtime_never_falls_back_to_developer_path(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(self.root), create=True),
            patch.object(js.shutil, "which", return_value="untrusted/deno") as find,
        ):
            self.assertIsNone(js.deno_path())
            with self.assertRaisesRegex(RuntimeError, "ZIP completo"):
                js.youtube_js_args()
            find.assert_not_called()

    def invoke(self, stderr="", code=1, max_attempts=1, cancel=None, output="Clase"):
        self.logs = []
        with (
            patch.object(yt, "yt_dlp_path", return_value="fixture-ytdlp"),
            patch.object(
                yt,
                "youtube_js_args",
                return_value=[
                    "--no-js-runtimes",
                    "--js-runtimes",
                    "deno:C:/with spaces/deno.exe",
                ],
            ),
            patch.object(yt, "run_process", return_value=NS(returncode=code, stderr=stderr)) as run,
        ):
            result = yt.descargar_subtitulos(
                "testVIDEO01",
                output,
                self.s.transcripts_dir,
                log=self.logs.append,
                max_attempts=max_attempts,
                cancel=cancel,
            )
        self.calls = run.call_args_list
        return result

    def test_processing_is_wait_not_error_and_no_download(self):
        self.assertIsNone(self.invoke("ERROR: We're processing this video. Check back later."))
        self.assertTrue(any("todavía está procesando" in line for line in self.logs))
        self.assertFalse(any("Consulta sin resultado válido" in line for line in self.logs))
        self.assertFalse(any(line.startswith("[!]") for line in self.logs))
        args = self.calls[0].args[0]
        for flag in (
            "--skip-download",
            "--ignore-config",
            "--no-playlist",
            "--js-runtimes",
        ):
            self.assertIn(flag, args)
        self.assertIn("deno:C:/with spaces/deno.exe", args)

    def test_processing_retries_at_120_seconds_with_cancellation(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = True
        self.assertIsNone(
            self.invoke("We're processing this video", max_attempts=105, cancel=cancel)
        )
        cancel.wait.assert_called_once_with(120)
        self.assertEqual(len(self.calls), 1)

    def test_wait_never_overwrites_existing_txt(self):
        good = self.s.transcripts_dir / "Clase_Para_NotebookLM.txt"
        atomic_write(good, base.VTT)
        self.invoke("We're processing this video")
        self.assertEqual(good.read_bytes(), base.VTT)

    def test_runtime_warning_stops_instead_of_105_retries(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        with self.assertRaisesRegex(RuntimeError, "motor JavaScript"):
            self.invoke(
                "No supported JavaScript runtime could be found. ERROR: fixture",
                max_attempts=105,
                cancel=cancel,
            )
        cancel.wait.assert_not_called()

    def test_deleted_private_or_login_required_stops_without_retry(self):
        for text in (
            "Video unavailable",
            "This video has been removed by the uploader",
            "Private video. Sign in",
            "Sign in to confirm you are not a bot",
        ):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(RuntimeError, "no permite consultar"),
            ):
                self.invoke(text, max_attempts=105)

    def test_no_captions_yet_has_a_non_error_message(self):
        self.assertIsNone(self.invoke("", code=0))
        self.assertTrue(any("todavía no ofrece subtítulos" in s for s in self.logs))

    def test_missing_runtime_stops_before_ytdlp(self):
        with (
            patch.object(yt, "yt_dlp_path", return_value="fixture"),
            patch.object(js, "deno_path", return_value=None),
            patch.object(yt, "run_process") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "motor JavaScript"):
                yt.descargar_subtitulos(
                    "testVIDEO01", "Clase", self.s.transcripts_dir, log=lambda _: None
                )
        run.assert_not_called()

    def test_private_api_captions_do_not_require_js(self):
        youtube = Mock()
        youtube.captions.return_value.list.return_value.execute.return_value = {"items": []}
        with patch.object(
            yt, "youtube_js_args", side_effect=AssertionError("Private API needs no JS")
        ):
            self.assertIsNone(
                yt.descargar_subtitulos(
                    "testVIDEO01",
                    "Clase",
                    self.s.transcripts_dir,
                    private=True,
                    youtube=youtube,
                    store=StateStore(self.s),
                    max_attempts=1,
                    log=lambda _: None,
                )
            )

    def test_cancelled_does_not_start_runtime_or_extractor(self):
        cancel = threading.Event()
        cancel.set()
        with (
            patch.object(yt, "youtube_js_args") as args,
            patch.object(yt, "run_process") as run,
        ):
            self.assertIsNone(
                yt.descargar_subtitulos(
                    "testVIDEO01", "Clase", self.s.transcripts_dir, cancel=cancel
                )
            )
        args.assert_not_called()
        run.assert_not_called()

    def test_recovery_identifies_which_class_failed(self):
        name = "Ciberseguridad755_Semana02_2026-09-02_fixture"
        worker = RecoveryWorker(self.s, {"id": "missing-fixture", "name": name})
        logs, results = [], []
        worker.log.connect(logs.append)
        worker.result.connect(results.append)
        worker.run()
        self.assertTrue(any(name in line for line in logs))
        self.assertTrue(results[0].startswith("No se pudo continuar: " + name))

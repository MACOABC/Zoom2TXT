"""Regression tests: synthetic data only; live HTTP forbidden."""

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, Mock, patch

_session = tempfile.TemporaryDirectory(prefix="zoom2txt-tests-")
os.environ["ZOOM2TXT_DATA_DIR"] = _session.name
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import httplib2
import requests
from PySide6.QtWidgets import QApplication, QMessageBox

from zoom2txt import config, security
from zoom2txt.blackboard import client as bc
from zoom2txt.blackboard import registry as reg
from zoom2txt.core import pipeline
from zoom2txt.gui import workers
from zoom2txt.gui.main_window import MainWindow
from zoom2txt.security import read_private_json, redact, write_private_json
from zoom2txt.storage import StateStore, atomic_write, safe_name
from zoom2txt.transcription import youtube_engine as yt

APP = QApplication.instance() or QApplication([])
VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHola, clase de prueba.\n"


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=_session.name)
        self.root = Path(self.temp.name)
        self.s = config.Settings(
            zoom_user="test",
            videos_dir=self.root / "videos",
            transcripts_dir=self.root / "txt",
            materials_dir=self.root / "material",
            term_start="2026-03-30",
        )
        for p in (self.s.videos_dir, self.s.transcripts_dir, self.s.materials_dir):
            p.mkdir()
        self.patches = [
            patch.object(config, "DATA_DIR", self.root),
            patch.object(pipeline, "DATA_DIR", self.root),
            patch.object(config, "SETTINGS_FILE", self.root / "settings.json"),
            patch.object(reg, "REGISTRY_FILE", self.root / "legacy.json"),
            patch.object(
                security,
                "_protect",
                side_effect=lambda data, decrypt=False: bytes(byte ^ 0xA5 for byte in data),
            ),
            patch.object(
                requests.sessions.Session,
                "request",
                side_effect=AssertionError("LIVE HTTP FORBIDDEN"),
            ),
            patch.object(
                httplib2.Http,
                "request",
                side_effect=AssertionError("LIVE GOOGLE HTTP FORBIDDEN"),
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_settings_json_shapes(self):
        for value in (
            [],
            None,
            "bad",
            123,
            {"wizard_done": "false", "delete_local_video": "true"},
        ):
            atomic_write(config.SETTINGS_FILE, json.dumps(value))
            result = config.Settings.load()
            self.assertFalse(result.delete_local_video)
            self.assertFalse(result.wizard_done)

    def test_settings_no_relative_paths(self):
        atomic_write(
            config.SETTINGS_FILE,
            json.dumps({"videos_dir": "", "transcripts_dir": "../outside"}),
        )
        self.assertTrue(config.Settings.load().transcripts_dir.is_absolute())

    def test_settings_no_plain_http(self):
        atomic_write(config.SETTINGS_FILE, json.dumps({"blackboard_url": "http://example.org"}))
        self.assertEqual(config.Settings.load().blackboard_url, "")

    def test_blackboard_url_has_no_institutional_default(self):
        self.assertEqual(config.Settings().blackboard_url, "")
        self.assertTrue(config.validate_blackboard_url("https://campus.example.edu")[0])
        self.assertFalse(
            config.validate_blackboard_url(
                "https://user:" + "pass" + chr(64) + "campus.example.edu"
            )[0]
        )

    def test_invalid_oauth_shapes(self):
        p = self.root / "client.json"
        for data in (
            None,
            [],
            {"installed": None},
            {"installed": {"client_id": "", "client_secret": ""}},
        ):
            atomic_write(p, json.dumps(data))
            self.assertFalse(config.validate_client_secrets(p)[0])

    def test_valid_desktop_client(self):
        p = self.root / "client.json"
        atomic_write(
            p,
            json.dumps(
                {
                    "installed": {
                        "client_id": "test.apps.googleusercontent.com",
                        "client_secret": "fake",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            ),
        )
        self.assertTrue(config.validate_client_secrets(p)[0])

    def test_scopes_are_not_credentials(self):
        p = self.root / "token.json"
        atomic_write(p, json.dumps({"scopes": yt.SCOPES}))
        self.assertFalse(yt.token_is_valid(p))

    def test_encrypted_json_round_trip(self):
        p = self.root / "token.json"
        value = {"refresh_token": "TEST_ONLY_SECRET"}
        write_private_json(p, value)
        self.assertNotIn("TEST_ONLY_SECRET", p.read_text())
        self.assertEqual(read_private_json(p), value)

    def test_legacy_secret_migrates(self):
        p = self.root / "session.json"
        atomic_write(p, '{"cookie":"DUMMY"}')
        self.assertEqual(read_private_json(p), {"cookie": "DUMMY"})
        self.assertNotIn("DUMMY", p.read_text())

    def test_redaction(self):
        result = redact(
            "bot12345:abcdefghijklmnopqrstuvwxyz email@example.com refresh_token=SECRET"
        )
        for value in ("abcdefghijklmnopqrstuvwxyz", "email@example.com", "SECRET"):
            self.assertNotIn(value, result)

    def test_atomic_replace_preserves_old(self):
        p = self.root / "good.txt"
        atomic_write(p, "original")
        with patch("zoom2txt.storage.os.replace", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                atomic_write(p, "new")
        self.assertEqual(p.read_text(), "original")

    def test_safe_names(self):
        for value in ("CON", "..", "x/y", "a" * 300, "x%(bad)s"):
            result = safe_name(value)
            self.assertLessEqual(len(result), 100)
            self.assertNotIn("/", result)
            self.assertNotIn("%", result)
        self.assertNotEqual(reg.short_name_for("Curso / 754"), reg.short_name_for("Curso / 755"))

    def test_registry_concurrent_updates(self):
        def put(n):
            StateStore(self.s).put("test", n, value=n)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(put, range(30)))
        self.assertEqual(len(StateStore(self.s).list("test")), 30)

    def test_claim_is_unique(self):
        store = StateStore(self.s)
        self.assertTrue(store.claim("job", "one", status="new"))
        self.assertFalse(store.claim("job", "one", status="new"))

    def test_atomic_quota_reservation(self):
        store = StateStore(self.s)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: store.reserve("today", 50, 100), range(10)))
        self.assertEqual(sum(results), 2)

    def test_corrupt_database_has_clear_error(self):
        p = self.root / "bad.sqlite3"
        atomic_write(p, b"not a database")
        with self.assertRaisesRegex(RuntimeError, "historial local"):
            StateStore(self.s, p)

    def test_accounts_are_separate(self):
        from dataclasses import replace

        StateStore(self.s).put("test", "x", value=1)
        self.assertEqual(StateStore(replace(self.s, zoom_user="someoneelse")).get("test", "x"), {})

    def test_registry_no_lost_records(self):
        a, b = reg.Registry(self.s), reg.Registry(self.s)
        p = self.root / "valid.txt"
        atomic_write(p, VTT)
        a.mark_done("c", "a", "A", p)
        b.mark_done("c", "b", "B", p)
        self.assertTrue(a.is_processed("c", "b"))
        self.assertTrue(b.is_processed("c", "a"))

    def test_registry_missing_and_omitted_are_distinct(self):
        r = reg.Registry(self.s)
        with self.assertRaises(ValueError):
            r.mark_done("c", "missing", "nothing")
        r.mark_skipped("c", "omitted", "skip", "Clase")
        self.assertEqual(r.records()[0]["status"], "omitted")
        r.restore(r.records()[0]["id"])
        self.assertFalse(r.is_processed("c", "omitted"))

    def test_registry_detects_modified_transcript(self):
        r = reg.Registry(self.s)
        p = self.root / "valid.txt"
        atomic_write(p, VTT)
        r.mark_done("c", "x", "X", p)
        atomic_write(p, "different")
        self.assertFalse(r.is_processed("c", "x"))

    def test_vtt_validation(self):
        self.assertTrue(yt.validate_vtt(VTT))
        for data in (
            b"",
            b"WEBVTT",
            b"<html>Login</html>",
            b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n",
        ):
            self.assertFalse(yt.validate_vtt(data))

    def test_youtube_progressive_vtt_matches_studio_text(self):
        from zoom2txt.cleaning.subtitle_cleaner import (
            migrar_transcripciones_anteriores,
            vtt_a_texto,
        )

        progressive = """WEBVTT
Kind: captions
Language: es

00:00:01.000 --> 00:00:02.000
Hola<00:00:01.200><c> clase</c>

00:00:02.000 --> 00:00:02.010
Hola clase

00:00:02.010 --> 00:00:04.000
Hola clase
de<00:00:02.300><c> prueba.</c>

00:00:04.000 --> 00:00:04.010


00:00:04.010 --> 00:00:05.000
Repite.

00:00:05.000 --> 00:00:05.010
Repite.

00:00:05.010 --> 00:00:06.000
Repite.
Repite.
"""
        self.assertEqual(
            vtt_a_texto(progressive),
            "Hola clase de prueba.\n\nRepite. Repite.",
        )
        old = self.s.transcripts_dir / "Clase_Para_NotebookLM.txt"
        normal = self.s.transcripts_dir / "Apuntes_Para_NotebookLM.txt"
        atomic_write(old, progressive)
        atomic_write(normal, "Texto que no debe cambiar.")
        self.assertEqual(migrar_transcripciones_anteriores(self.s.transcripts_dir), [old])
        self.assertEqual(old.read_text(), "Hola clase de prueba.\n\nRepite. Repite.")
        self.assertEqual(normal.read_text(), "Texto que no debe cambiar.")

    def test_stale_vtt_not_accepted(self):
        atomic_write(self.s.transcripts_dir / "Clase.es.vtt", VTT)
        with (
            patch.object(yt, "yt_dlp_path", return_value="fake"),
            patch.object(yt, "youtube_js_args", return_value=[]),
            patch.object(yt, "run_process", return_value=NS(returncode=1, stderr="failure")),
        ):
            result = yt.descargar_subtitulos(
                "vid",
                "Clase",
                self.s.transcripts_dir,
                log=lambda _: None,
                max_attempts=1,
            )
        self.assertIsNone(result)

    def test_public_subtitles_are_consolidated(self):
        def fake(args, **kwargs):
            base = Path(args[args.index("-o") + 1])
            atomic_write(Path(str(base) + ".es.vtt"), VTT)
            return NS(returncode=0, stderr="")

        with (
            patch.object(yt, "yt_dlp_path", return_value="fake"),
            patch.object(yt, "youtube_js_args", return_value=[]),
            patch.object(yt, "run_process", side_effect=fake),
        ):
            result = yt.descargar_subtitulos(
                "vid",
                "Clase",
                self.s.transcripts_dir,
                log=lambda _: None,
                max_attempts=1,
            )
        self.assertEqual(result.read_text(encoding="utf-8"), "Hola, clase de prueba.")

    def test_empty_subtitles_fail_preserve_good_txt(self):
        good = self.s.transcripts_dir / "Clase_Para_NotebookLM.txt"
        atomic_write(good, VTT)

        def fake(args, **kwargs):
            atomic_write(Path(args[args.index("-o") + 1] + ".es.vtt"), b"")
            return NS(returncode=0, stderr="")

        with (
            patch.object(yt, "yt_dlp_path", return_value="fake"),
            patch.object(yt, "youtube_js_args", return_value=[]),
            patch.object(yt, "run_process", side_effect=fake),
        ):
            with self.assertRaises(RuntimeError):
                yt.descargar_subtitulos(
                    "vid",
                    "Clase",
                    self.s.transcripts_dir,
                    log=lambda _: None,
                    max_attempts=1,
                )
        self.assertEqual(good.read_bytes(), VTT)

    def test_cancel_subtitle_before_request(self):
        cancel = threading.Event()
        cancel.set()
        with (
            patch.object(yt, "yt_dlp_path", return_value="fake"),
            patch.object(yt, "run_process") as run,
        ):
            self.assertIsNone(
                yt.descargar_subtitulos(
                    "vid",
                    "Clase",
                    self.s.transcripts_dir,
                    cancel=cancel,
                    log=lambda _: None,
                )
            )
            run.assert_not_called()

    def test_drive_failure_retains_video_and_txt(self):
        self.s.drive_dir = str(self.root / "absent")
        self.s.delete_local_video = True
        txt = self.s.transcripts_dir / "class.txt"
        vid = self.s.videos_dir / "class.mp4"
        atomic_write(txt, VTT)
        atomic_write(vid, b"video")
        self.assertFalse(pipeline._post_proceso(txt, vid, self.s, lambda _: None))
        self.assertTrue(txt.exists())
        self.assertTrue(vid.exists())

    def test_verified_copy_allows_opt_in_cleanup(self):
        folder = self.root / "copy"
        folder.mkdir()
        self.s.drive_dir = str(folder)
        self.s.delete_local_video = True
        txt = self.s.transcripts_dir / "class.txt"
        vid = self.s.videos_dir / "class.mp4"
        atomic_write(txt, VTT)
        atomic_write(vid, b"video")
        self.assertTrue(pipeline._post_proceso(txt, vid, self.s, lambda _: None))
        self.assertEqual((folder / "class.txt").read_bytes(), VTT)
        self.assertFalse(vid.exists())

    def test_no_delete_outside_videos_directory(self):
        self.s.delete_local_video = True
        txt = self.s.transcripts_dir / "class.txt"
        vid = self.root / "outside.mp4"
        atomic_write(txt, VTT)
        atomic_write(vid, b"video")
        pipeline._post_proceso(txt, vid, self.s, lambda _: None)
        self.assertTrue(vid.exists())

    def _pipeline_stubs(self):
        video = self.s.videos_dir / "fixture.mp4"
        atomic_write(video, b"video")
        youtube = Mock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"status": {"privacyStatus": "unlisted"}}]
        }
        return video, youtube

    def test_resume_waiting_does_not_reupload(self):
        video, youtube = self._pipeline_stubs()
        target = self.s.transcripts_dir / "result.txt"
        atomic_write(target, VTT)
        with (
            patch.object(pipeline, "descargar_zoom", return_value=video) as dl,
            patch.object(pipeline, "get_authenticated_service", return_value=youtube),
            patch.object(pipeline, "subir_a_youtube", return_value="12345678901") as up,
            patch.object(pipeline, "descargar_subtitulos", side_effect=[None, target]),
        ):
            args = (
                "https://example.invalid/rec",
                "",
                "Clase",
                self.s,
                lambda _: None,
                lambda _: None,
                threading.Event(),
            )
            self.assertEqual(pipeline.run_pipeline(*args), (None, "12345678901"))
            self.assertEqual(pipeline.run_pipeline(*args)[0], target)
            self.assertEqual(dl.call_count, 1)
            self.assertEqual(up.call_count, 1)

    def test_unknown_upload_cannot_repeat(self):
        video, youtube = self._pipeline_stubs()
        with (
            patch.object(pipeline, "descargar_zoom", return_value=video),
            patch.object(pipeline, "get_authenticated_service", return_value=youtube),
            patch.object(pipeline, "subir_a_youtube", side_effect=TimeoutError("uncertain")) as up,
        ):
            args = (
                "https://example.invalid/rec",
                "",
                "Clase",
                self.s,
                lambda _: None,
                lambda _: None,
                threading.Event(),
            )
            with self.assertRaises(TimeoutError):
                pipeline.run_pipeline(*args)
            with self.assertRaises(RuntimeError):
                pipeline.run_pipeline(*args)
            self.assertEqual(up.call_count, 1)

    def client(self):
        c = bc.BlackboardClient.__new__(bc.BlackboardClient)
        c.base = "https://example.invalid"
        c.log = lambda _: None
        c.session = Mock()
        c.coverage = []
        return c

    def test_forbidden_material_is_error(self):
        c = self.client()
        c.session.get.return_value = NS(status_code=403)
        with self.assertRaises(bc.BlackboardError):
            c.list_course_materials("c", "Course")

    def test_attachment_page_login_rejected(self):
        c = self.client()
        resp = Mock(
            status_code=200,
            headers={"Content-Type": "text/html"},
            url="https://example.invalid/login",
        )
        resp.iter_content.return_value = iter([b"<html>Login</html>"])
        c.session.get.return_value = MagicMock()
        c.session.get.return_value.__enter__.return_value = resp
        dest = self.root / "class.pdf"
        atomic_write(dest, b"%PDF-old")
        with self.assertRaises(bc.BlackboardError):
            c.download_attachment(
                bc.MaterialFile("c", "C", "i", "a", "class.pdf", "class.pdf"), dest
            )
        self.assertEqual(dest.read_bytes(), b"%PDF-old")

    def test_valid_pdf_is_accepted(self):
        c = self.client()
        data = b"%PDF-1.7\nvalid fixture"
        resp = Mock(
            status_code=200,
            headers={"Content-Length": str(len(data))},
            url="https://example.invalid/file",
        )
        resp.iter_content.return_value = iter([data])
        c.session.get.return_value = MagicMock()
        c.session.get.return_value.__enter__.return_value = resp
        dest = self.root / "file.pdf"
        c.download_attachment(bc.MaterialFile("c", "C", "i", "a", "file.pdf", "file.pdf"), dest)
        self.assertEqual(dest.read_bytes(), data)

    def test_lti_rejects_unknown_destination(self):
        c = self.client()
        embedded_credentials = "https://user:" + "password" + chr(64) + "zoom.us"
        for url in (
            "http://zoom.us",
            "https://zoom.us.evil.invalid",
            embedded_credentials,
        ):
            with self.assertRaises(bc.BlackboardError):
                c._validate_lti_url(url)
        c._validate_lti_url("https://applications.zoom.us/lti/rich")

    def test_material_cancel_preserves_previous(self):
        c = self.client()
        cancel = threading.Event()
        cancel.set()
        resp = Mock(status_code=200, headers={}, url="https://example.invalid/file")
        resp.iter_content.return_value = iter([b"%PDF-new"])
        c.session.get.return_value = MagicMock()
        c.session.get.return_value.__enter__.return_value = resp
        dest = self.root / "file.pdf"
        atomic_write(dest, b"%PDF-old")
        with self.assertRaises(InterruptedError):
            c.download_attachment(
                bc.MaterialFile("c", "C", "i", "a", "file.pdf", "file.pdf"),
                dest,
                cancel,
            )
        self.assertEqual(dest.read_bytes(), b"%PDF-old")

    def test_oauth_pair_rollback(self):
        from zoom2txt import oauth_storage as oauth

        client = self.root / "client.json"
        token = self.root / "token.json"
        candidate = self.root / "pending-client.json"
        candidate_token = self.root / "pending-token.json"
        for p, data in (
            (client, "old-client"),
            (token, "old-token"),
            (candidate, "new-client"),
            (candidate_token, "new-token"),
        ):
            atomic_write(p, data)
        original = oauth.atomic_write

        def fail(path, data):
            if path == token and data == b"new-token":
                raise OSError("disk full")
            original(path, data)

        with (
            patch.object(oauth, "DATA_DIR", self.root),
            patch.object(oauth, "CLIENT_SECRETS_FILE", client),
            patch.object(oauth, "TOKEN_FILE", token),
            patch.object(oauth, "atomic_write", side_effect=fail),
        ):
            with self.assertRaises(OSError):
                oauth.commit_oauth_change(candidate, candidate_token)
        self.assertEqual(client.read_text(), "old-client")
        self.assertEqual(token.read_text(), "old-token")

    def test_settings_layout_fits(self):
        from zoom2txt.gui import style
        from zoom2txt.gui.settings_dialog import SettingsDialog

        style.apply_theme(APP, "light")
        dialog = SettingsDialog(self.s)
        self.assertLess(dialog.minimumSizeHint().width(), 800)
        self.assertFalse(hasattr(dialog, "tg_edit"))
        dialog.close()

    def test_scan_does_not_persist_omissions(self):
        events = []
        recordings = [
            bc.Recording(
                "c",
                "Curso",
                "short",
                "T",
                "2026-04-01",
                extra={"startTime": "2026-04-01 12:00:00", "duration": 10},
            ),
            bc.Recording(
                "c",
                "Curso",
                "long",
                "T",
                "2026-04-01",
                extra={"startTime": "2026-04-01 12:00:00", "duration": 90},
            ),
        ]

        class Fake:
            def __init__(self, *a):
                pass

            def is_logged_in(self):
                return True

            def get_current_courses(self):
                return [{"id": "c", "name": "Curso"}]

            def find_zoom_content(self, *a):
                return "content"

            def open_zoom_portal(self, *a):
                events.append("portal")
                return ("origin", "id", {})

            def list_recordings(self, *a):
                return recordings

            def recording_detail(self, *a):
                events.append("detail")
                a[-1].play_url = "https://example.invalid/recording"
                return a[-1]

            def export_cookies(self, *a):
                events.append("cookies")

        items = []
        errors = []
        with patch.object(bc, "BlackboardClient", Fake):
            worker = workers.ScanWorker(self.s)
            worker.found.connect(items.append)
            worker.failed.connect(errors.append)
            worker.run()
        self.assertFalse(errors)
        self.assertEqual(len(items[0]), 1)
        self.assertEqual(reg.Registry(self.s).records(), [])
        self.assertEqual(events, ["portal", "detail", "cookies"])
        p = self.s.transcripts_dir / "existing.txt"
        atomic_write(p, VTT)
        reg.Registry(self.s).mark_done("c", "long", "Long", p)
        items.clear()
        with patch.object(bc, "BlackboardClient", Fake):
            worker.run()
        self.assertEqual(items, [[]])

    def test_queue_deduplicates_selection(self):
        target = self.s.transcripts_dir / "good.txt"
        atomic_write(target, VTT)
        item = dict(
            course_id="c",
            meeting_id="m",
            name="Clase",
            play_url="https://example.invalid/r",
        )
        with patch.object(workers, "run_pipeline", return_value=(target, None)) as run:
            worker = workers.QueueWorker([item, item], "", self.s, False)
            worker.run()
        self.assertEqual(run.call_count, 1)

    def test_private_captions_are_consolidated(self):
        youtube = Mock()
        youtube.captions.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "track", "snippet": {"language": "es", "status": "serving"}}]
        }
        youtube.captions.return_value.download.return_value.execute.return_value = VTT
        result = yt.descargar_subtitulos(
            "vid",
            "Private",
            self.s.transcripts_dir,
            youtube=youtube,
            private=True,
            store=StateStore(self.s),
            max_attempts=1,
            log=lambda _: None,
        )
        self.assertEqual(result.read_text(encoding="utf-8"), "Hola, clase de prueba.")

    def test_encrypted_cookie_export(self):
        c = self.client()
        c.session.cookies = requests.cookies.RequestsCookieJar()
        c.session.cookies.set("session", "DUMMY_COOKIE", domain="example.invalid", secure=True)
        path = self.root / "cookies.json"
        c.export_cookies(path)
        self.assertNotIn("DUMMY_COOKIE", path.read_text())
        self.assertIn("DUMMY_COOKIE", read_private_json(path)["netscape"])

    def test_material_version_and_hash(self):
        mf = bc.MaterialFile("c", "Curso", "i", "a", "file.pdf", "file.pdf", version="new")

        class Fake:
            def __init__(self, *a):
                self.coverage = []

            def is_logged_in(self):
                return True

            def get_current_courses(self):
                return [{"id": "c", "name": "Curso"}]

            def list_course_materials(self, *a):
                return [mf]

        results = []
        with patch.object(bc, "BlackboardClient", Fake):
            worker = workers.MaterialScanWorker(self.s)
            worker.found.connect(results.append)
            worker.run()
            item = results[-1][1][0]
            atomic_write(item["dest"], b"%PDF-fixture")
            from zoom2txt.storage import digest

            StateStore(self.s).put(
                "material", item["key"], version="old", checksum=digest(item["dest"])
            )
            worker.run()
            self.assertEqual(len(results[-1][1]), 1)
            StateStore(self.s).put("material", item["key"], version="new")
            worker.run()
            self.assertEqual(len(results[-1][1]), 0)
            atomic_write(item["dest"], b"corrupt")
            worker.run()
            self.assertEqual(len(results[-1][1]), 1)

    def test_public_desktop_has_no_remote_control_surface(self):
        settings = config.Settings()
        self.assertFalse(hasattr(settings, "telegram_chat_id"))
        self.assertFalse(hasattr(MainWindow, "_start_bot"))

    def test_rejected_close_does_not_stop_services(self):
        fake = NS(_busy=lambda: True, _closing=False)
        event = Mock()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            MainWindow.closeEvent(fake, event)
        event.ignore.assert_called_once()

    def test_contrast_aa(self):
        from zoom2txt.gui.style import PALETTES

        def lum(h):
            vs = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            return sum(
                a * b
                for a, b in zip(
                    [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in vs],
                    [0.2126, 0.7152, 0.0722],
                    strict=True,
                )
            )

        def ratio(a, b):
            x, y = sorted([lum(a), lum(b)])
            return (y + 0.05) / (x + 0.05)

        for p in PALETTES.values():
            for key in ("ACCENT", "ACCENT_HI", "ACCENT_DN"):
                self.assertGreaterEqual(ratio(p["ON_ACCENT"], p[key]), 4.5)
            self.assertGreaterEqual(ratio(p["SUCCESS"], p["CONSOLE_BG"]), 4.5)
            self.assertGreaterEqual(ratio(p["LINK"], p["SURFACE"]), 4.5)

    def test_gui_exception_is_reported_and_redacted(self):
        import sys

        from main import install_exception_handler

        old = sys.excepthook
        fake = NS(activeWindow=lambda: None)
        try:
            handler = install_exception_handler(fake)
            with patch.object(QMessageBox, "critical") as dialog:
                handler(RuntimeError, RuntimeError("upload?upload_id=SECRET"), None)
                self.assertNotIn("SECRET", dialog.call_args.args[2])
                self.assertIn("Historial", dialog.call_args.args[2])
            self.assertFalse(fake._reporting_error)
        finally:
            sys.excepthook = old

    def test_hidden_page_does_not_stretch_active_page(self):
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        from zoom2txt.gui.adaptive_stack import AdaptiveStack

        stack = AdaptiveStack()
        for height in (200, 800):
            page = QWidget()
            layout = QVBoxLayout(page)
            child = QWidget()
            child.setMinimumHeight(height)
            layout.addWidget(child)
            stack.addWidget(page)
        stack.setCurrentIndex(1)
        self.assertGreaterEqual(stack.minimumSizeHint().height(), 800)
        stack.setCurrentIndex(0)
        self.assertLess(stack.minimumSizeHint().height(), 300)
        self.assertFalse(stack.hasHeightForWidth())
        stack.close()

    def test_manual_recovery_uses_local_password_not_plain_file(self):
        from zoom2txt.auth import credentials

        state = StateStore(self.s)
        key = "manual-test"
        job = {"id": key, "name": "Clase", "status": "downloading"}
        path = self.root / "jobs" / state.namespace / (key + ".json")
        write_private_json(path, {"zoom_url": "https://zoom.us/rec/share/test"})
        with (
            patch.object(
                credentials, "get_zoom_password", return_value="dummy-password"
            ) as password,
            patch.object(workers, "run_pipeline", return_value=(None, None)) as process,
        ):
            worker = workers.RecoveryWorker(self.s, job)
            worker.run()
            password.assert_called_once_with("test")
            self.assertEqual(process.call_args.args[1], "dummy-password")
        self.assertNotIn("dummy-password", path.read_text())


if __name__ == "__main__":
    unittest.main()

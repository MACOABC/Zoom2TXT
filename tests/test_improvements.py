"""Batch scheduling, compulsory cleanup and campus setup regression tests.

All recordings, credentials and API responses are synthetic. Live HTTP is blocked.
"""

import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests
import test_hardening as base
from PySide6.QtWidgets import QLabel

from zoom2txt.auth import credentials
from zoom2txt.blackboard import client as bc
from zoom2txt.blackboard.browser_login import native_login_script
from zoom2txt.blackboard.registry import Registry, recording_key
from zoom2txt.core import pipeline
from zoom2txt.gui import cloud_setup, setup_wizard, workers
from zoom2txt.storage import StateStore, digest


class ImprovementsTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown
    _pipeline_stubs = base.HardeningTests._pipeline_stubs

    def run_job(self, **kwargs):
        return pipeline.run_pipeline(
            "https://example.invalid/recording",
            "",
            "Clase sintética",
            self.s,
            lambda _: None,
            lambda _: None,
            threading.Event(),
            job_key="fixture",
            **kwargs,
        )

    def items(self):
        return [
            dict(
                course_id="course",
                meeting_id=str(i),
                name=f"Clase {i}",
                play_url="https://example.invalid/recording",
            )
            for i in range(3)
        ]

    def test_upload_pass_never_queries_or_waits_for_captions(self):
        video, api = self._pipeline_stubs()
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=api),
            patch.object(pipeline, "descargar_zoom", return_value=video),
            patch.object(pipeline, "subir_a_youtube", return_value="testVIDEO01"),
            patch.object(pipeline, "descargar_subtitulos") as captions,
            patch.object(pipeline, "eliminar_video") as delete,
        ):
            self.assertEqual(self.run_job(defer_captions=True), (None, "testVIDEO01"))
        captions.assert_not_called()
        delete.assert_not_called()
        api.videos.assert_not_called()
        self.assertEqual(StateStore(self.s).get("job", "fixture")["status"], "waiting_captions")

    def test_all_uploads_precede_round_robin_caption_checks(self):
        state = StateStore(self.s)
        events = []
        attempts = {}
        txt = self.s.transcripts_dir / "synthetic.txt"
        txt.write_text("Texto de prueba", encoding="utf-8")

        def run(*args, **kwargs):
            name, key = args[2], kwargs["job_key"]
            prepare = kwargs["defer_captions"]
            events.append(("prepare" if prepare else "captions", name))
            self.assertEqual(kwargs["caption_attempts"], 1)
            if prepare:
                state.put("job", key, status="waiting_captions", video_id="testVIDEO01")
                return None, "testVIDEO01"
            attempts[name] = attempts.get(name, 0) + 1
            if name == "Clase 0" and attempts[name] == 1:
                return None, "testVIDEO01"
            state.put("job", key, status="done", youtube_deleted=True)
            return txt, "testVIDEO01"

        worker = workers.QueueWorker(self.items(), "", self.s, False)
        worker.poll_interval = 0
        totals = []
        worker.all_done.connect(lambda ok, failed: totals.append((ok, failed)))
        with patch.object(workers, "run_pipeline", side_effect=run):
            worker.run()
        self.assertEqual(events[:3], [("prepare", f"Clase {i}") for i in range(3)])
        self.assertEqual(events[3:], [("captions", f"Clase {i}") for i in (0, 1, 2, 0)])
        self.assertEqual(totals, [(3, 0)])

    def test_caption_failure_does_not_block_other_classes(self):
        txt = self.s.transcripts_dir / "synthetic.txt"
        txt.write_text("Texto", encoding="utf-8")
        visited = []

        def run(*args, **kwargs):
            if kwargs["defer_captions"]:
                return None, "testVIDEO01"
            visited.append(args[2])
            if args[2] == "Clase 0":
                raise RuntimeError("Respuesta sintética inválida")
            return txt, "testVIDEO01"

        worker = workers.QueueWorker(self.items(), "", self.s)
        worker.poll_interval = 0
        totals = []
        worker.all_done.connect(lambda ok, failed: totals.append((ok, failed)))
        with patch.object(workers, "run_pipeline", side_effect=run):
            worker.run()
        self.assertEqual(visited, [f"Clase {i}" for i in range(3)])
        self.assertEqual(totals, [(2, 1)])

    def test_pausing_caption_wait_preserves_uploaded_jobs(self):
        state = StateStore(self.s)
        worker = workers.QueueWorker(self.items(), "", self.s)

        def run(*args, **kwargs):
            state.put("job", kwargs["job_key"], status="waiting_captions", video_id="testVIDEO01")
            return None, "testVIDEO01"

        def pause(_):
            worker.cancel()
            return True

        with (
            patch.object(workers, "run_pipeline", side_effect=run),
            patch.object(worker.cancel_event, "wait", side_effect=pause),
        ):
            worker.run()
        self.assertEqual(len(state.list("job")), 3)
        self.assertTrue(all(row["status"] == "waiting_captions" for row in state.list("job")))
        self.assertFalse(Registry(self.s).records())

    def test_compulsory_deletion_retries_without_uploading_or_transcribing_again(self):
        video, api = self._pipeline_stubs()
        txt = self.s.transcripts_dir / "synthetic.txt"
        txt.write_text("Texto verificado", encoding="utf-8")
        self.s.auto_delete_youtube = False  # Old saved preference cannot disable cleanup.
        state = StateStore(self.s)
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=api),
            patch.object(pipeline, "descargar_zoom", return_value=video) as download,
            patch.object(pipeline, "subir_a_youtube", return_value="testVIDEO01") as upload,
            patch.object(pipeline, "descargar_subtitulos", return_value=txt) as captions,
            patch.object(
                pipeline, "eliminar_video", side_effect=[TimeoutError("fixture"), None]
            ) as delete,
        ):
            self.run_job()
            first = state.get("job", "fixture")
            self.assertEqual(first["status"], "cleanup_pending")
            self.assertTrue(first["cleanup_pending"])
            self.assertEqual(first["txt_hash"], digest(txt))
            pipeline.retry_pending_cleanup(self.s, lambda _: None, threading.Event())
            completed = state.get("job", "fixture")
            self.assertEqual(completed["status"], "done")
            self.assertTrue(completed["youtube_deleted"])
            self.assertFalse(completed["cleanup_pending"])
            self.run_job()
        self.assertEqual(download.call_count, 1)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(captions.call_count, 1)
        self.assertEqual(delete.call_count, 2)

    def test_missing_optional_copy_does_not_delay_remote_deletion(self):
        _, api = self._pipeline_stubs()
        txt = self.s.transcripts_dir / "synthetic.txt"
        txt.write_text("Texto verificado", encoding="utf-8")
        self.s.drive_dir = str(self.root / "unavailable-copy")
        state = StateStore(self.s)
        state.put(
            "job",
            "fixture",
            status="cleanup_pending",
            txt=str(txt),
            txt_hash=digest(txt),
            video_id="testVIDEO01",
            cleanup_pending=True,
        )
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=api),
            patch.object(pipeline, "eliminar_video") as delete,
        ):
            self.run_job()
        delete.assert_called_once()
        job = state.get("job", "fixture")
        self.assertTrue(job["youtube_deleted"])
        self.assertEqual(job["status"], "sync_pending")

    def test_empty_transcript_never_triggers_remote_deletion(self):
        video, api = self._pipeline_stubs()
        txt = self.s.transcripts_dir / "empty.txt"
        txt.write_bytes(b"")
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=api),
            patch.object(pipeline, "descargar_zoom", return_value=video),
            patch.object(pipeline, "subir_a_youtube", return_value="testVIDEO01"),
            patch.object(pipeline, "descargar_subtitulos", return_value=txt),
            patch.object(pipeline, "eliminar_video") as delete,
        ):
            with self.assertRaisesRegex(ValueError, "TXT"):
                self.run_job()
        delete.assert_not_called()

    def test_modified_transcript_keeps_deletion_pending(self):
        txt = self.s.transcripts_dir / "synthetic.txt"
        txt.write_text("Original", encoding="utf-8")
        state = StateStore(self.s)
        state.put(
            "job",
            "fixture",
            status="cleanup_pending",
            txt=str(txt),
            txt_hash=digest(txt),
            video_id="testVIDEO01",
            cleanup_pending=True,
        )
        txt.write_text("Modificado", encoding="utf-8")
        with patch.object(pipeline, "run_pipeline") as run:
            pipeline.retry_pending_cleanup(self.s, lambda _: None, threading.Event())
        run.assert_not_called()
        self.assertTrue(state.get("job", "fixture")["cleanup_pending"])

    def test_failed_cleanup_is_not_registered_as_completed(self):
        txt = self.s.transcripts_dir / "synthetic.txt"
        txt.write_text("Texto", encoding="utf-8")
        item = self.items()[0]
        key = recording_key(item["course_id"], item["meeting_id"])
        StateStore(self.s).put("job", key, status="cleanup_pending", cleanup_pending=True)
        with patch.object(workers, "run_pipeline", return_value=(txt, "testVIDEO01")):
            workers.QueueWorker([item], "", self.s).run()
        self.assertFalse(Registry(self.s).is_processed(item["course_id"], item["meeting_id"]))

    def test_cleanup_after_restart_updates_recording_registry(self):
        _, api = self._pipeline_stubs()
        txt = self.s.transcripts_dir / "synthetic.txt"
        txt.write_text("Texto", encoding="utf-8")
        state = StateStore(self.s)
        state.put(
            "job",
            "fixture",
            status="cleanup_pending",
            txt=str(txt),
            txt_hash=digest(txt),
            video_id="testVIDEO01",
            cleanup_pending=True,
            course_id="c",
            meeting_id="m",
        )
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=api),
            patch.object(pipeline, "eliminar_video"),
        ):
            pipeline.retry_pending_cleanup(self.s, lambda _: None, threading.Event())
        self.assertTrue(Registry(self.s).is_processed("c", "m"))

    def test_network_failure_does_not_request_another_blackboard_login(self):
        client = bc.BlackboardClient("https://example.invalid")
        with patch.object(client.session, "get", side_effect=requests.ConnectionError("fixture")):
            with self.assertRaisesRegex(bc.BlackboardError, "conexión"):
                client.is_logged_in()
        client.session.close()

    def test_expired_session_requests_login_but_server_failure_keeps_session(self):
        client = bc.BlackboardClient("https://example.invalid")
        with patch.object(client.session, "get", return_value=Mock(status_code=401)):
            self.assertFalse(client.is_logged_in())
        with patch.object(client.session, "get", return_value=Mock(status_code=503)):
            with self.assertRaisesRegex(bc.BlackboardError, "503"):
                client.is_logged_in()
        client.session.close()

    def test_blackboard_password_is_scoped_to_campus_and_separate_from_zoom(self):
        ring = Mock()
        with patch.object(credentials, "secure_keyring", return_value=ring):
            credentials.set_blackboard_password(
                "https://one.invalid", "student", "synthetic-password"
            )
            credentials.get_blackboard_password("https://two.invalid", "student")
            credentials.get_zoom_password("student")
        stored_key = ring.set_password.call_args.args[1]
        self.assertNotEqual(stored_key, ring.get_password.call_args_list[0].args[1])
        self.assertNotEqual(stored_key, ring.get_password.call_args_list[1].args[1])

    def test_json_is_desktop_and_error_help_links_to_its_project(self):
        path = self.root / "client_secrets.pending.json"
        path.write_text(
            json.dumps({"installed": {"project_id": "synthetic-project"}}), encoding="utf-8"
        )
        with patch.object(cloud_setup, "DATA_DIR", self.root):
            self.assertEqual(cloud_setup.configured_project(), "synthetic-project")
        with (
            patch.object(cloud_setup, "configured_project", return_value="synthetic-project"),
            patch.object(setup_wizard.QDesktopServices, "openUrl") as opened,
        ):
            page = setup_wizard.OAuthPage()
            page._open_audience()
            self.assertEqual(
                opened.call_args.args[0].toString(),
                "https://console.cloud.google.com/auth/audience?project=synthetic-project",
            )
            page.deleteLater()

    def test_wizard_explains_test_user_requirement_and_no_longer_requires_zoom_password(self):
        self.s.zoom_user = "student"
        self.s.blackboard_url = "https://example.invalid"
        with patch.object(setup_wizard, "get_blackboard_password", return_value=None):
            page = setup_wizard.ZoomPage(self.s)
            self.assertTrue(page.isComplete())
            self.assertEqual(page.pass_edit.text(), "")
            page.deleteLater()
        with patch.object(setup_wizard, "CLIENT_SECRETS_FILE", self.root / "missing.json"):
            page = setup_wizard.GoogleCloudPage()
            page._show_step(3)
            labels = page.steps.currentWidget().findChildren(QLabel)
            self.assertIn("aunque seas el creador", " ".join(label.text() for label in labels))
            page.deleteLater()

    def test_missing_blackboard_password_explains_migration_instead_of_claiming_reconnection(self):
        from types import SimpleNamespace

        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = SimpleNamespace(_password=None)
        hint = BlackboardLoginDialog._initial_hint(dialog)
        self.assertIn("Todavía no hay", hint)
        self.assertIn("versión anterior", hint)

    def test_legacy_password_button_reuses_existing_secret_without_retyping(self):
        from types import SimpleNamespace

        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = SimpleNamespace(_user="student", _store_and_connect=Mock(), _hint=Mock())
        with patch.object(
            credentials, "get_zoom_password", return_value="synthetic-previous-password"
        ):
            BlackboardLoginDialog._use_legacy_password(dialog)
        dialog._store_and_connect.assert_called_once_with("student", "synthetic-previous-password")

    def test_migration_stores_scoped_password_and_restarts_auto_login(self):
        from types import SimpleNamespace

        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = SimpleNamespace(
            _base_url="https://example.invalid",
            _password=None,
            _pass_edit=Mock(),
            _credentials_panel=Mock(),
            _hint=Mock(),
            _login_timer=Mock(),
            _try_login=Mock(),
        )
        with patch.object(credentials, "set_blackboard_password") as save:
            BlackboardLoginDialog._store_and_connect(dialog, "student", "synthetic-password")
        save.assert_called_once_with("https://example.invalid", "student", "synthetic-password")
        self.assertEqual(dialog._password, "synthetic-password")
        dialog._pass_edit.clear.assert_called_once()
        dialog._credentials_panel.hide.assert_called_once()
        dialog._try_login.assert_called_once()
        self.assertNotIn("synthetic-password", dialog._hint.setText.call_args.args[0])

    def test_migration_cannot_fall_back_to_plaintext_when_keyring_fails(self):
        from types import SimpleNamespace

        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = SimpleNamespace(
            _base_url="https://example.invalid", _password=None, _hint=Mock(), _try_login=Mock()
        )
        with patch.object(
            credentials, "set_blackboard_password", side_effect=RuntimeError("fixture")
        ):
            BlackboardLoginDialog._store_and_connect(dialog, "student", "synthetic-password")
        self.assertIsNone(dialog._password)
        dialog._try_login.assert_not_called()

    def test_login_script_rejects_unencrypted_campus_url(self):
        with self.assertRaises(ValueError):
            native_login_script("http://example.invalid", "student", "synthetic-password")


if __name__ == "__main__":
    unittest.main()

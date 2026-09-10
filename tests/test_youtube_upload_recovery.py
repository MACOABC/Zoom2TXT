"""Real Google SDK resumable protocol with synthetic HTTP, never a live upload."""

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httplib2
import test_hardening as base
from googleapiclient.discovery import build

from zoom2txt.core import pipeline
from zoom2txt.gui import workers
from zoom2txt.gui.history_dialog import HistoryDialog
from zoom2txt.gui.main_window import MainWindow
from zoom2txt.security import write_private_json
from zoom2txt.storage import StateStore, digest
from zoom2txt.transcription import youtube_engine as yt
from zoom2txt.transcription.youtube_recovery import find_existing_upload
from zoom2txt.workflow import WorkLedger

KEY = "26510a6afa1d8bc39fcf922d"
TITLE = "RedesAvanzadas753_Semana02_2026-08-31_26510a6a"
VIDEO = "testVIDEO01"
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=TEST_ONLY"
CHUNK = 8 * 1024 * 1024


class UploadRecoveryTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def media(self):
        path = self.s.videos_dir / "synthetic.mp4"
        path.write_bytes(b"0" * (CHUNK + 17))
        return path

    def service(self, handler):
        self.calls = []

        def request(uri, method="GET", body=None, headers=None, **kwargs):
            headers = headers or {}
            self.calls.append((uri, method, headers, body))
            result = handler(uri, method, body, headers)
            if isinstance(result, BaseException):
                raise result
            status, values, payload = result
            return httplib2.Response({"status": str(status), **values}), payload

        transport = SimpleNamespace(request=request)
        return build("youtube", "v3", http=transport, static_discovery=True)

    def test_308_without_location_and_checkpoint_before_first_block(self):
        saved = {}

        def handler(uri, method, body, headers):
            if method == "POST":
                metadata = json.loads(body)
                self.assertEqual(metadata["status"]["privacyStatus"], "unlisted")
                self.assertIn("Zoom2TXT: " + KEY, metadata["snippet"]["description"])
                return 200, {"location": SESSION}, b""
            self.assertEqual(saved.get("upload_uri"), SESSION)
            if headers["Content-Range"].startswith("bytes 0-"):
                return 308, {"range": f"bytes=0-{CHUNK - 1}"}, b""
            self.assertEqual(headers["Content-Range"], f"bytes {CHUNK}-{CHUNK + 16}/{CHUNK + 17}")
            return 200, {}, json.dumps({"id": VIDEO}).encode()

        result = yt.subir_a_youtube(
            self.service(handler),
            self.media(),
            TITLE,
            log=lambda _: None,
            checkpoint={"job_key": KEY, "save": lambda **v: saved.update(v)},
        )
        self.assertEqual(result, VIDEO)
        self.assertEqual(saved, {"upload_uri": "", "video_id": VIDEO, "status": "uploaded"})
        self.assertEqual([c[1] for c in self.calls], ["POST", "PUT", "PUT"])

    def test_first_block_failure_persists_session(self):
        saved = {}

        def handler(uri, method, body, headers):
            if method == "POST":
                return 200, {"location": SESSION}, b""
            self.assertEqual(saved.get("upload_uri"), SESSION)
            return TimeoutError("synthetic timeout")

        with self.assertRaises(TimeoutError):
            yt.subir_a_youtube(
                self.service(handler),
                self.media(),
                TITLE,
                log=lambda _: None,
                checkpoint={"save": lambda **v: saved.update(v)},
            )
        self.assertEqual(saved["upload_uri"], SESSION)

    def test_resume_probes_server_then_only_sends_remaining_bytes(self):
        def handler(uri, method, body, headers):
            self.assertEqual(method, "PUT")
            self.assertEqual(uri, SESSION)
            if headers["Content-Range"].startswith("bytes */"):
                self.assertEqual(
                    {k.lower(): str(v) for k, v in headers.items()}["content-length"],
                    "0",
                )
                return 308, {"range": f"bytes=0-{CHUNK - 1}"}, b""
            self.assertEqual(headers["Content-Range"], f"bytes {CHUNK}-{CHUNK + 16}/{CHUNK + 17}")
            return 201, {}, json.dumps({"id": VIDEO}).encode()

        result = yt.subir_a_youtube(
            self.service(handler),
            self.media(),
            TITLE,
            log=lambda _: None,
            checkpoint={"upload_uri": SESSION, "save": lambda **v: None},
        )
        self.assertEqual(result, VIDEO)
        self.assertEqual(len(self.calls), 2)

    def test_already_completed_session_uses_only_status_probe(self):
        def handler(uri, method, body, headers):
            self.assertEqual(method, "PUT")
            self.assertTrue(headers["Content-Range"].startswith("bytes */"))
            return 200, {}, json.dumps({"id": VIDEO}).encode()

        self.assertEqual(
            yt.subir_a_youtube(
                self.service(handler),
                self.media(),
                TITLE,
                log=lambda _: None,
                checkpoint={"upload_uri": SESSION, "save": lambda **v: None},
            ),
            VIDEO,
        )
        self.assertEqual(len(self.calls), 1)

    def test_expired_session_never_posts_a_replacement(self):
        with self.assertRaisesRegex(RuntimeError, "caducó"):
            yt.subir_a_youtube(
                self.service(lambda *args: (404, {}, b"{}")),
                self.media(),
                TITLE,
                log=lambda _: None,
                checkpoint={"upload_uri": SESSION, "save": lambda **v: None},
            )
        self.assertEqual([c[1] for c in self.calls], ["PUT"])

    def test_transport_factory_does_not_treat_308_as_redirect(self):
        client = self.root / "client.json"
        client.write_text(json.dumps({"installed": {"client_id": "synthetic"}}))
        credentials = Mock(expired=False, valid=True, client_id="synthetic")
        with (
            patch.object(yt, "token_is_valid", return_value=True),
            patch.object(yt, "read_private_json", return_value={}),
            patch.object(yt.Credentials, "from_authorized_user_info", return_value=credentials),
            patch.object(yt, "build") as factory,
        ):
            yt.get_authenticated_service(client, self.root / "token.json")
            transport = factory.call_args.kwargs["http"].http
            self.assertNotIn(308, transport.redirect_codes)
            self.assertIn(302, transport.redirect_codes)
            self.assertEqual(transport.timeout, 60)

    def owned(self, items, **extras):
        api = Mock()
        api.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "OWN_UPLOADS"}}}]
        }
        api.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": items,
            **extras,
        }
        api.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "status": {
                        "privacyStatus": "unlisted",
                        "uploadStatus": "processed",
                    },
                    "processingDetails": {"processingStatus": "succeeded"},
                }
            ]
        }
        return api

    def item(self, video=VIDEO, title=TITLE, description=""):
        return {
            "snippet": {"title": title, "description": description},
            "contentDetails": {"videoId": video},
        }

    def test_unique_owned_legacy_video_found(self):
        api = self.owned([self.item()])
        self.assertEqual(find_existing_upload(api, TITLE, KEY, threading.Event()), VIDEO)
        api.channels.return_value.list.assert_called_once_with(part="contentDetails", mine=True)
        api.videos.return_value.insert.assert_not_called()

    def test_exact_marker_survives_title_edit(self):
        api = self.owned([self.item(title="Título editado", description="Zoom2TXT: " + KEY)])
        self.assertEqual(find_existing_upload(api, TITLE, KEY, threading.Event()), VIDEO)

    def test_generic_or_partial_titles_never_match(self):
        api = self.owned([self.item(title="Clase 1")])
        self.assertIsNone(find_existing_upload(api, "Clase 1", KEY, threading.Event()))

    def test_multiple_matches_require_user_selection(self):
        api = self.owned([self.item(), self.item(video="testVIDEO02")])
        with self.assertRaisesRegex(RuntimeError, "varios videos"):
            find_existing_upload(api, TITLE, KEY, threading.Event())

    def test_incomplete_channel_scan_cannot_claim_unique_match(self):
        api = self.owned([self.item()], nextPageToken="more")
        with self.assertRaisesRegex(RuntimeError, "parcial"):
            find_existing_upload(api, TITLE, KEY, threading.Event())
        self.assertEqual(api.playlistItems.return_value.list.call_count, 2)

    def test_unknown_completed_upload_links_id_and_only_gets_captions(self):
        store = StateStore(self.s)
        store.put("job", KEY, status="upload_unknown", name=TITLE, display_name=TITLE)
        for n in range(6):
            WorkLedger(self.s, store).admit(f"already-{n}")
        api = self.owned([self.item()])
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=api),
            patch.object(pipeline, "descargar_zoom") as download,
            patch.object(pipeline, "subir_a_youtube") as upload,
            patch.object(pipeline, "descargar_subtitulos", return_value=None) as captions,
        ):
            result = pipeline.run_pipeline(
                "https://zoom.us/rec/test",
                "",
                TITLE,
                self.s,
                lambda _: None,
                lambda _: None,
                threading.Event(),
                job_key=KEY,
            )
        self.assertEqual(result, (None, VIDEO))
        self.assertEqual(store.get("job", KEY)["video_id"], VIDEO)
        self.assertEqual(store.get("job", KEY)["status"], "waiting_captions")
        upload.assert_not_called()
        download.assert_not_called()
        captions.assert_called_once()
        self.assertEqual(len(WorkLedger(self.s, store).daily()["jobs"]), 6)

    def test_unmatched_unknown_upload_never_reuploads(self):
        store = StateStore(self.s)
        store.put("job", KEY, status="upload_unknown", name=TITLE)
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=self.owned([])),
            patch.object(pipeline, "subir_a_youtube") as upload,
        ):
            with self.assertRaisesRegex(RuntimeError, "coincidencia segura"):
                pipeline.run_pipeline(
                    "https://zoom.us/rec/test",
                    "",
                    TITLE,
                    self.s,
                    lambda _: None,
                    lambda _: None,
                    threading.Event(),
                    job_key=KEY,
                )
        upload.assert_not_called()
        self.assertEqual(store.get("job", KEY)["status"], "upload_unknown")

    def test_auth_failure_does_not_download_or_spend_daily_slot(self):
        with (
            patch.object(
                pipeline,
                "get_authenticated_service",
                side_effect=yt.AuthorizationRequired("fixture"),
            ),
            patch.object(pipeline, "descargar_zoom") as download,
        ):
            with self.assertRaises(yt.AuthorizationRequired):
                pipeline.run_pipeline(
                    "https://zoom.us/rec/test",
                    "",
                    TITLE,
                    self.s,
                    lambda _: None,
                    lambda _: None,
                    threading.Event(),
                    job_key=KEY,
                )
        download.assert_not_called()
        self.assertFalse(WorkLedger(self.s).daily()["jobs"])

    def test_existing_resumable_session_does_not_spend_new_daily_slot(self):
        store = StateStore(self.s)
        mp4 = self.media()
        store.put(
            "job",
            KEY,
            status="uploading",
            name=TITLE,
            video=str(mp4),
            video_hash=digest(mp4),
        )
        write_private_json(
            self.root / "jobs" / store.namespace / (KEY + ".json"),
            {"upload_uri": SESSION},
        )
        for n in range(6):
            WorkLedger(self.s, store).admit(f"already-{n}")
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=self.owned([])),
            patch.object(pipeline, "descargar_zoom") as download,
            patch.object(pipeline, "subir_a_youtube", return_value=VIDEO) as upload,
            patch.object(pipeline, "descargar_subtitulos", return_value=None),
        ):
            pipeline.run_pipeline(
                "https://zoom.us/rec/test",
                "",
                TITLE,
                self.s,
                lambda _: None,
                lambda _: None,
                threading.Event(),
                job_key=KEY,
            )
        self.assertEqual(upload.call_args.kwargs["checkpoint"]["upload_uri"], SESSION)
        download.assert_not_called()
        self.assertEqual(len(WorkLedger(self.s, store).daily()["jobs"]), 6)

    def test_history_click_without_selection_has_feedback(self):
        dialog = HistoryDialog(self.s)
        with patch.object(base.QMessageBox, "information") as info:
            dialog.continue_job()
        info.assert_called_once()
        dialog.close()

    def test_queue_stops_after_common_auth_failure(self):
        items = [
            dict(
                course_id="course",
                meeting_id=str(n),
                name=f"Clase {n}",
                play_url="https://zoom.us/rec/test",
            )
            for n in range(6)
        ]
        worker = workers.QueueWorker(items, "", self.s, False)
        results = []
        worker.all_done.connect(lambda ok, failed: results.append((ok, failed)))
        with patch.object(
            workers, "run_pipeline", side_effect=yt.AuthorizationRequired("fixture")
        ) as run:
            worker.run()
        run.assert_called_once()
        self.assertEqual(results, [(0, 1)])

    def test_failed_server_video_is_not_reuploaded_or_waited_forever(self):
        store = StateStore(self.s)
        store.put("job", KEY, status="uploaded", name=TITLE, video_id=VIDEO)
        api = self.owned([])
        api.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"status": {"uploadStatus": "rejected", "privacyStatus": "private"}}]
        }
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=api),
            patch.object(pipeline, "subir_a_youtube") as upload,
            patch.object(pipeline, "descargar_subtitulos") as captions,
        ):
            with self.assertRaisesRegex(RuntimeError, "rechazado"):
                pipeline.run_pipeline(
                    "https://zoom.us/rec/test",
                    "",
                    TITLE,
                    self.s,
                    lambda _: None,
                    lambda _: None,
                    threading.Event(),
                    job_key=KEY,
                )
        upload.assert_not_called()
        captions.assert_not_called()
        self.assertEqual(store.get("job", KEY)["video_id"], VIDEO)

    def test_id_is_durable_before_upload_session_is_cleared(self):
        store = StateStore(self.s)
        mp4 = self.media()
        store.put(
            "job",
            KEY,
            status="downloaded",
            name=TITLE,
            video=str(mp4),
            video_hash=digest(mp4),
        )

        def upload(*args, **kwargs):
            save = kwargs["checkpoint"]["save"]
            save(upload_uri=SESSION)

            def check(path, data):
                self.assertEqual(store.get("job", KEY)["video_id"], VIDEO)

            with patch.object(pipeline, "write_private_json", side_effect=check):
                save(upload_uri="", video_id=VIDEO, status="uploaded")
            return VIDEO

        with (
            patch.object(pipeline, "get_authenticated_service", return_value=self.owned([])),
            patch.object(pipeline, "subir_a_youtube", side_effect=upload),
            patch.object(pipeline, "descargar_subtitulos", return_value=None),
        ):
            pipeline.run_pipeline(
                "https://zoom.us/rec/test",
                "",
                TITLE,
                self.s,
                lambda _: None,
                lambda _: None,
                threading.Event(),
                job_key=KEY,
            )

    def test_recovery_is_busy_and_second_attempt_is_blocked(self):
        window = MainWindow(self.s)
        worker = Mock()
        worker.isRunning.return_value = True
        window.recovery_worker = worker
        self.assertTrue(window._busy())
        with (
            patch.object(base.QMessageBox, "information") as info,
            patch("zoom2txt.gui.main_window.RecoveryWorker") as factory,
        ):
            window._recover({"id": KEY})
        info.assert_called_once()
        factory.assert_not_called()
        window._set_recovering(True)
        self.assertFalse(window.settings_btn.isEnabled())
        self.assertFalse(window.process_btn.isEnabled())
        window._cancel_recovery()
        worker.cancel.assert_called_once()
        window.recovery_worker = None
        window.close()

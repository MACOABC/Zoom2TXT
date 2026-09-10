"""Automatic recovery uses local bytes and confirmed YouTube state, offline fixtures."""

import threading
import time
import unittest
from unittest.mock import Mock, patch

import httplib2
import test_hardening as base
from googleapiclient.errors import HttpError

from zoom2txt import work_items
from zoom2txt.core import pipeline
from zoom2txt.gui.health_view import HealthView
from zoom2txt.gui.main_window import MainWindow
from zoom2txt.gui.workers import QueueWorker
from zoom2txt.security import write_private_json
from zoom2txt.storage import StateStore, digest
from zoom2txt.transcription.youtube_recovery import find_existing_upload
from zoom2txt.workflow import WorkLedger

KEY = "26510a6afa1d8bc39fcf922d"
TITLE = "RedesAvanzadas753_Semana02_2026-08-31_26510a6a"
VIDEO = "testVIDEO01"


class AutomaticContinuationTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def prepare(self, **values):
        self.video = self.s.videos_dir / "already-downloaded.mp4"
        self.video.write_bytes(b"synthetic-video")
        self.store = StateStore(self.s)
        self.store.put(
            "job",
            KEY,
            status="upload_unknown",
            name=TITLE,
            display_name=TITLE,
            video=str(self.video),
            video_hash=digest(self.video),
        )
        self.store.put("job", KEY, **values)
        api = Mock()
        api.channels.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "OWN_CHANNEL",
                    "contentDetails": {"relatedPlaylists": {"uploads": "OWN_UPLOADS"}},
                }
            ]
        }
        api.playlistItems.return_value.list.return_value.execute.return_value = {"items": []}
        self.remote = {
            "id": VIDEO,
            "status": {"privacyStatus": "unlisted", "uploadStatus": "processed"},
            "processingDetails": {"processingStatus": "succeeded"},
        }
        api.videos.return_value.list.return_value.execute.return_value = {"items": [self.remote]}
        return api

    def run_job(self, api, *, upload_effect=None):
        self.logs = []
        with (
            patch.object(pipeline, "get_authenticated_service", return_value=api),
            patch.object(pipeline, "descargar_zoom") as download,
            patch.object(
                pipeline, "subir_a_youtube", return_value=VIDEO, side_effect=upload_effect
            ) as upload,
            patch.object(pipeline, "descargar_subtitulos", return_value=None) as captions,
        ):
            self.download, self.upload, self.captions = download, upload, captions
            return pipeline.run_pipeline(
                "https://zoom.us/rec/test",
                "",
                TITLE,
                self.s,
                self.logs.append,
                lambda _: None,
                threading.Event(),
                job_key=KEY,
                auto_recover=True,
            )

    def test_absent_old_attempt_reuses_local_video_and_uploads_once(self):
        self.run_job(self.prepare())
        self.download.assert_not_called()
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.args[1], self.video)
        self.captions.assert_called_once()
        self.assertEqual(self.store.get("job", KEY)["youtube_channel_id"], "OWN_CHANNEL")

    def test_existing_video_gets_captions_without_new_daily_admission(self):
        api = self.prepare()
        api.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {"title": TITLE, "description": "Zoom2TXT: " + KEY},
                    "contentDetails": {"videoId": VIDEO},
                }
            ]
        }
        self.run_job(api)
        self.upload.assert_not_called()
        self.download.assert_not_called()
        self.captions.assert_called_once()
        self.assertEqual(WorkLedger(self.s).daily()["used"], 0)

    def test_deleted_saved_id_restarts_from_local_video(self):
        api = self.prepare(status="waiting_captions", video_id=VIDEO)
        api.videos.return_value.list.return_value.execute.side_effect = [
            {"items": []},
            {"items": [self.remote]},
        ]
        self.run_job(api)
        self.upload.assert_called_once()
        self.download.assert_not_called()

    def test_processing_saved_video_never_uploads(self):
        api = self.prepare(status="waiting_captions", video_id=VIDEO)
        self.remote["processingDetails"]["processingStatus"] = "processing"
        self.run_job(api)
        self.upload.assert_not_called()
        self.captions.assert_called_once()

    def test_incomplete_response_does_not_authorize_upload(self):
        api = self.prepare()
        api.playlistItems.return_value.list.return_value.execute.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "incompleta"):
            self.run_job(api)
        self.upload.assert_not_called()
        self.download.assert_not_called()

    def test_wrong_channel_preserves_upload_and_daily_budget(self):
        api = self.prepare(youtube_channel_id="OTHER_CHANNEL")
        with self.assertRaisesRegex(RuntimeError, "otro canal"):
            self.run_job(api)
        self.upload.assert_not_called()
        self.assertEqual(WorkLedger(self.s).daily()["used"], 0)

    def test_recent_unknown_attempt_waits_instead_of_duplicating(self):
        api = self.prepare(upload_attempt_at=time.time())
        with self.assertRaisesRegex(RuntimeError, "reciente"):
            self.run_job(api)
        self.upload.assert_not_called()

    def test_full_day_keeps_absent_class_pending_without_upload(self):
        api = self.prepare()
        for i in range(6):
            WorkLedger(self.s).admit(str(i))
        with self.assertRaisesRegex(RuntimeError, "6 clases"):
            self.run_job(api)
        self.upload.assert_not_called()
        self.download.assert_not_called()

    def test_expired_session_is_reconciled_then_replaced_once(self):
        api = self.prepare(status="uploading")
        write_private_json(
            self.root / "jobs" / self.store.namespace / (KEY + ".json"),
            {"upload_uri": "https://www.googleapis.com/upload/expired"},
        )
        error = RuntimeError("La sesión de subida caducó")
        error.__cause__ = HttpError(httplib2.Response({"status": "410"}), b"{}")
        self.run_job(api, upload_effect=[error, VIDEO])
        self.assertEqual(self.upload.call_count, 2)
        self.assertTrue(self.upload.call_args_list[0].kwargs["checkpoint"]["upload_uri"])
        self.assertFalse(self.upload.call_args_list[1].kwargs["checkpoint"]["upload_uri"])
        self.download.assert_not_called()

    def test_truncated_inventory_does_not_authorize_new_upload(self):
        api = self.prepare()
        api.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [],
            "pageInfo": {"totalResults": 10},
        }
        with self.assertRaisesRegex(RuntimeError, "parcial"):
            self.run_job(api)
        self.upload.assert_not_called()

    def test_reconciliation_reads_beyond_previous_200_limit(self):
        api = self.prepare()
        api.playlistItems.return_value.list.return_value.execute.side_effect = [
            {"items": [], "nextPageToken": str(i)} for i in range(4)
        ] + [
            {
                "items": [
                    {
                        "snippet": {"title": TITLE, "description": "Zoom2TXT: " + KEY},
                        "contentDetails": {"videoId": VIDEO},
                    }
                ]
            }
        ]
        self.assertEqual(
            find_existing_upload(api, TITLE, KEY, threading.Event(), max_pages=100, strict=True),
            VIDEO,
        )

    def test_queue_continues_uploaded_jobs_after_daily_limit(self):
        from zoom2txt.workflow import DailyLimitReached

        self.prepare()
        txt = self.s.transcripts_dir / "ready.txt"
        txt.write_text("Clase lista", encoding="utf-8")
        items = [
            dict(
                course_id="course",
                meeting_id=str(i),
                name="Clase",
                play_url="https://zoom.us/rec/test",
            )
            for i in range(2)
        ]
        worker = QueueWorker(items, "", self.s, False)
        with patch(
            "zoom2txt.gui.workers.run_pipeline",
            side_effect=[DailyLimitReached("Límite"), (txt, VIDEO)],
        ) as run:
            worker.run()
        self.assertEqual(run.call_count, 2)
        self.assertTrue(run.call_args.kwargs["auto_recover"])

    def test_mixed_results_remain_actionable_on_classes_screen(self):
        with patch.object(MainWindow, "_load_saved_work"), patch.object(HealthView, "refresh"):
            window = MainWindow(self.s)
            items = [
                dict(
                    course_id="course",
                    course_name="Redes avanzadas",
                    meeting_id=str(i),
                    name=TITLE,
                    start_time="2026-08-31",
                    play_url="https://zoom.us/rec/test",
                )
                for i in range(2)
            ]
            window._on_scan_found(items)
            window._queue_items = items
            window._queue_total = 2
            window._on_queue_item_finished(0, "ready.txt")
            window._on_queue_item_finished(1, "")
            window._on_queue_done(1, 1)
            self.assertFalse(window.table.isHidden())
            self.assertFalse(window.process_btn.isHidden())
            self.assertEqual(window.table.item(0, 5).text(), "TXT verificado")
            self.assertEqual(window.table.item(1, 5).text(), "Por resolver")
            self.assertEqual(len(window._scan_items), 2)
            window.close()

    def test_local_data_is_ready_without_blackboard_lookup(self):
        self.prepare()
        write_private_json(
            self.root / "jobs" / self.store.namespace / (KEY + ".json"),
            {"zoom_url": "https://zoom.us/rec/test"},
        )
        with patch.object(work_items, "DATA_DIR", self.root):
            data = work_items.continuation_data(self.s, KEY)
        self.assertEqual(data["play_url"], "https://zoom.us/rec/test")


if __name__ == "__main__":
    unittest.main()

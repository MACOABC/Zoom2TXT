"""Course isolation, durable daily admission and honest weekly reporting."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import test_hardening as base

from zoom2txt.blackboard import client as bc
from zoom2txt.blackboard import registry as reg
from zoom2txt.core import pipeline
from zoom2txt.gui import workers
from zoom2txt.storage import StateStore, atomic_write
from zoom2txt.workflow import DailyLimitReached, WorkLedger


class WorkflowTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown
    client = base.HardeningTests.client
    _pipeline_stubs = base.HardeningTests._pipeline_stubs

    def test_denied_access_is_not_missing_video(self):
        for code in (401, 403, 429, 500):
            client = self.client()
            client.session.get.return_value = Mock(status_code=code)
            with self.assertRaisesRegex(bc.BlackboardError, str(code)):
                client.recording_detail(
                    "origin",
                    "scid",
                    {},
                    bc.Recording("c", "Course", "m", "T", "2026-04-01"),
                )

    def test_largest_mp4_never_larger_audio(self):
        client = self.client()
        files = [
            {
                "fileType": "M4A",
                "fileSize": 99999,
                "playUrl": "https://zoom.us/rec/play/audio",
            },
            {
                "fileType": "MP4",
                "fileSize": 400,
                "playUrl": "https://zoom.us/rec/play/video1",
            },
            {
                "fileType": " mp4 ",
                "fileSize": "800",
                "playUrl": "https://zoom.us/rec/play/video2",
            },
            {
                "fileType": "CC",
                "fileSize": 999999,
                "playUrl": "https://zoom.us/rec/play/captions",
            },
        ]
        client.session.get.return_value = Mock(
            status_code=200,
            json=lambda: {"status": True, "result": {"recordingFiles": files}},
        )
        result = client.recording_detail(
            "origin", "scid", {}, bc.Recording("c", "Course", "m", "T", "2026-04-01")
        )
        self.assertTrue(result.play_url.endswith("video2"))
        self.assertEqual(result.extra["selected_video_bytes"], 800)

    def test_malformed_file_payload_is_not_empty_inventory(self):
        client = self.client()
        for payload in (
            [],
            None,
            {"status": False, "result": {}},
            {"result": None},
            {"result": {"recordingFiles": {}}},
        ):
            client.session.get.return_value = Mock(
                status_code=200, json=lambda payload=payload: payload
            )
            with self.assertRaises(bc.BlackboardError):
                client.recording_detail(
                    "origin",
                    "scid",
                    {},
                    bc.Recording("c", "Course", "m", "T", "2026-04-01"),
                )

    def test_actual_audio_only_is_reported_as_such(self):
        client = self.client()
        client._dump = Mock()
        client.session.get.return_value = Mock(
            status_code=200,
            json=lambda: {
                "status": True,
                "result": {
                    "recordingFiles": [
                        {"fileType": "M4A", "playUrl": "https://zoom.us/rec/play/audio"}
                    ]
                },
            },
        )
        with self.assertRaisesRegex(bc.BlackboardError, "solo audio"):
            client.recording_detail(
                "origin",
                "scid",
                {},
                bc.Recording(
                    "c",
                    "Course",
                    "m",
                    "T",
                    "2026-04-01",
                    play_url="https://zoom.us/rec/play/stale",
                ),
            )

    def test_six_per_day_persists_and_retry_is_free(self):
        ledger = WorkLedger(self.s)
        for n in range(6):
            self.assertTrue(ledger.admit(str(n)))
        self.assertFalse(WorkLedger(self.s).admit("seventh"))
        self.assertTrue(WorkLedger(self.s).admit("0"))
        self.assertEqual(ledger.daily()["used"], 6)

    def test_midnight_is_lima_not_utc(self):
        ledger = WorkLedger(self.s)
        before = datetime(2026, 4, 2, 4, 59, tzinfo=UTC)
        after = datetime(2026, 4, 2, 5, 0, tzinfo=UTC)
        for n in range(6):
            ledger.admit(str(n), before)
        self.assertEqual(ledger.daily(before)["day"], "2026-04-01")
        self.assertEqual(ledger.daily(before)["remaining"], 0)
        self.assertEqual(ledger.daily(after)["remaining"], 6)
        self.assertTrue(ledger.admit("tomorrow", after))

    def test_concurrent_admission_cannot_exceed_six(self):
        ledger = WorkLedger(self.s)
        with ThreadPoolExecutor(max_workers=8) as pool:
            admitted = list(pool.map(lambda n: ledger.admit(str(n)), range(12)))
        self.assertEqual(sum(admitted), 6)
        self.assertEqual(ledger.daily()["used"], 6)

    def test_daily_admission_is_per_profile(self):
        ledger = WorkLedger(self.s)
        for n in range(6):
            ledger.admit(str(n))
        self.assertEqual(WorkLedger(replace(self.s, zoom_user="other")).daily()["remaining"], 6)

    def test_full_day_prevents_download_and_records_deferred(self):
        ledger = WorkLedger(self.s)
        for n in range(6):
            ledger.admit(str(n))
        with patch.object(pipeline, "descargar_zoom") as download:
            with self.assertRaises(DailyLimitReached):
                pipeline.run_pipeline(
                    "https://zoom.us/rec/play/test",
                    "",
                    "Class",
                    self.s,
                    lambda _: None,
                    lambda _: None,
                    threading.Event(),
                    job_key="seventh",
                )
        download.assert_not_called()
        row = StateStore(self.s).get("job", "seventh")
        self.assertEqual(row["status"], "daily_limit")
        self.assertEqual(row["deferred_until"], ledger.daily()["next_day"])

    def test_uploaded_job_continues_when_daily_limit_full(self):
        ledger = WorkLedger(self.s)
        for n in range(6):
            ledger.admit(str(n))
        ledger.store.put("job", "uploaded", video_id="12345678901", status="waiting_captions")
        _, youtube = self._pipeline_stubs()
        with (
            patch.object(pipeline, "descargar_zoom") as download,
            patch.object(pipeline, "subir_a_youtube") as upload,
            patch.object(pipeline, "get_authenticated_service", return_value=youtube),
            patch.object(pipeline, "descargar_subtitulos", return_value=None),
        ):
            result = pipeline.run_pipeline(
                "https://zoom.us/rec/play/test",
                "",
                "Class",
                self.s,
                lambda _: None,
                lambda _: None,
                threading.Event(),
                job_key="uploaded",
            )
        self.assertEqual(result, (None, "12345678901"))
        download.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(ledger.daily()["used"], 6)

    def seed(self, now=None):
        ledger = WorkLedger(self.s)
        ledger.remember_courses([{"id": "c", "name": "Curso largo / 753"}])
        ledger.remember_course(
            "c",
            "Curso largo / 753",
            [
                {
                    "meeting_id": "m",
                    "name": "Clase Semana 1",
                    "start_time": "2026-04-01 17:00:00",
                    "duration": 90,
                },
                {
                    "meeting_id": "short",
                    "name": "Prueba corta",
                    "start_time": "2026-04-01 17:00:00",
                    "duration": 1,
                },
                {
                    "meeting_id": "week2",
                    "name": "Clase Semana 2",
                    "start_time": "2026-04-08 17:00:00",
                    "duration": 80,
                },
            ],
            now,
        )
        return ledger

    def test_backlog_survives_restart_omission_and_restore(self):
        self.seed()
        self.assertEqual(len(WorkLedger(self.s).pending()), 2)
        registry = reg.Registry(self.s)
        registry.mark_skipped("c", "m", "Omitted")
        self.assertEqual(len(WorkLedger(self.s).pending()), 1)
        registry.restore(reg.recording_key("c", "m"))
        self.assertEqual(len(WorkLedger(self.s).pending()), 2)

    def test_inventory_does_not_delete_short_alternative(self):
        ledger = self.seed()
        self.assertEqual(len(ledger.inventory()), 2)
        self.s.longest_per_day = False
        self.assertEqual(len(ledger.inventory()), 3)

    def test_verified_txt_removes_pending_and_missing_file_restores(self):
        ledger = self.seed()
        path = self.s.transcripts_dir / "class.txt"
        atomic_write(path, base.VTT)
        reg.Registry(self.s).mark_done("c", "m", "Class", path)
        self.assertEqual(len(ledger.pending()), 1)
        path.unlink()
        self.assertEqual(len(ledger.pending()), 2)

    def test_weekly_expectations_require_explicit_configuration(self):
        now = datetime(2026, 4, 9, 18, tzinfo=UTC)
        ledger = self.seed(now)
        self.assertTrue(all(r["missing"] is None for r in ledger.weekly(now)))
        ledger.set_expected("c", 2)
        rows = ledger.weekly(now)
        self.assertEqual(rows[0]["missing"], 1)
        self.assertIsNone(rows[1]["missing"])
        self.assertFalse(rows[1]["closed"])

    def test_weekly_failed_or_old_scan_cannot_claim_missing(self):
        sunday = datetime(2026, 4, 5, 18, tzinfo=UTC)
        monday = datetime(2026, 4, 6, 18, tzinfo=UTC)
        ledger = self.seed(sunday)
        ledger.set_expected("c", 2)
        self.assertIsNone(ledger.weekly(monday)[0]["missing"])
        self.seed(monday)
        self.assertEqual(ledger.weekly(monday)[0]["missing"], 1)
        ledger.course_error("c", "Course", "Network failure")
        self.assertIsNone(ledger.weekly(monday)[0]["missing"])
        self.assertEqual(len(ledger.pending()), 2)

    def test_expectations_do_not_leak_into_other_term(self):
        now = datetime(2026, 4, 9, 18, tzinfo=UTC)
        ledger = self.seed(now)
        ledger.set_expected("c", 2)
        self.s.term_start = "2026-08-24"
        self.assertEqual(ledger.inventory(), [])
        self.assertEqual(ledger.courses(), [])

    def test_weekly_plan_validation(self):
        ledger = self.seed()
        for count in (-1, 15, "2", None):
            with self.assertRaises(ValueError):
                ledger.set_expected("c", count)
        ledger.set_expected("c", 0)
        self.assertEqual(ledger.store.get("weekly_plan", "c")["count"], 0)

    def fake_scan(self, fail_first=False, ranges=None, hour=17):
        sessions = []

        class Fake:
            def __init__(self, *args):
                self.cookie_course = None
                self.session = Mock()
                sessions.append(self)

            def is_logged_in(self):
                return True

            def get_current_courses(self):
                return [{"id": str(i), "name": f"Course {i}"} for i in range(4)]

            def find_zoom_content(self, cid):
                return "content"

            def open_zoom_portal(self, cid, content):
                self.cookie_course = cid
                return "origin", cid, {}

            def list_recordings(self, origin, scid, headers, cid, name):
                return [
                    bc.Recording(
                        cid,
                        name,
                        f"{cid}-{i}",
                        "T",
                        "",
                        extra={
                            "startTime": f"2026-04-0{i + 1} {hour:02d}:00:00",
                            "duration": 90,
                        },
                    )
                    for i in range(3)
                ]

            def recording_detail(self, origin, scid, headers, rec):
                if self.cookie_course != rec.course_id or scid != rec.course_id:
                    raise bc.BlackboardError("Course session mismatch")
                if fail_first and rec.meeting_id == "0-0":
                    raise bc.BlackboardError("HTTP 403")
                rec.play_url = "https://zoom.us/rec/play/test"
                return rec

            def export_cookies(self, path):
                pass

        items, errors = [], []
        with patch.object(bc, "BlackboardClient", Fake):
            worker = workers.ScanWorker(self.s, ranges)
            worker.found.connect(items.append)
            worker.failed.connect(errors.append)
            worker.run()
        self.assertFalse(errors)
        for client in sessions:
            client.session.close.assert_called_once()
        return worker, items[0]

    def test_each_course_keeps_own_session(self):
        worker, items = self.fake_scan()
        self.assertEqual(len(items), 6)
        self.assertEqual(len(worker.all_pending), 12)
        self.assertFalse(worker.partial_errors)
        self.assertEqual(WorkLedger(self.s).daily()["used"], 0)
        self.assertTrue(all(not any(k.startswith("_") for k in i) for i in items))

    def test_failed_file_does_not_use_batch_slot(self):
        worker, items = self.fake_scan(fail_first=True)
        self.assertEqual(len(items), 6)
        self.assertEqual(len(worker.partial_errors), 1)
        self.assertEqual(len(WorkLedger(self.s).pending()), 12)

    def test_inventory_contains_all_weeks_not_only_filter(self):
        worker, items = self.fake_scan(ranges=[("2026-04-01", "2026-04-01")])
        self.assertEqual(len(items), 4)
        self.assertEqual(len(worker.all_pending), 4)
        self.assertEqual(len(WorkLedger(self.s).pending()), 12)

    def test_scan_full_day_keeps_backlog_not_false_all_done(self):
        ledger = WorkLedger(self.s)
        for n in range(6):
            ledger.admit(str(n))
        worker, items = self.fake_scan()
        self.assertEqual(items, [])
        self.assertEqual(len(worker.all_pending), 12)
        self.assertEqual(len(ledger.pending()), 12)

    def test_scan_remaining_two_not_six_after_restart(self):
        ledger = WorkLedger(self.s)
        for n in range(4):
            ledger.admit(str(n))
        worker, items = self.fake_scan()
        self.assertEqual(len(items), 2)
        self.assertEqual(len(worker.all_pending), 12)

    def test_backlog_ui_and_startup_notice(self):
        self.seed()
        with patch.object(base.MainWindow, "_setup_tray"):
            window = base.MainWindow(self.s)
            window._refresh_backlog_notice()
            self.assertIn("2 pendientes", window.backlog_notice.text())
            window._navigate(4)
            self.assertEqual(window.backlog.pending_table.rowCount(), 2)
            self.assertFalse(window.body_widget.isVisible())
            self.assertLess(window.minimumSizeHint().width(), 1180)
            window.close()
            window.deleteLater()
        base.APP.processEvents()

    def test_utc_without_suffix_converts_before_grouping_days(self):
        _, items = self.fake_scan(hour=1)
        self.assertEqual(items[0]["start_time"], "2026-03-31 20:00:00")
        self.assertTrue(all(i["start_time"].endswith("20:00:00") for i in items))

    def test_incomplete_recording_page_is_not_a_successful_inventory(self):
        client = self.client()
        for body in (
            {"list": [], "total": 3, "pageSize": 20},
            {"list": [{"meetingId": "a"}], "total": 3, "pageSize": 20},
            {"list": [{"topic": "no id"}], "total": 1, "pageSize": 20},
            {"list": [], "total": 0, "pageSize": 0},
        ):
            client.session.get.return_value = Mock(
                status_code=200,
                json=lambda body=body: {"status": True, "result": body},
            )
            with self.assertRaises(bc.BlackboardError):
                client.list_recordings("origin", "scid", {}, "c", "Course")

    def test_valid_empty_recording_list_is_allowed(self):
        client = self.client()
        client.session.get.return_value = Mock(
            status_code=200,
            json=lambda: {
                "status": True,
                "result": {"list": [], "total": 0, "pageSize": 20},
            },
        )
        self.assertEqual(client.list_recordings("origin", "scid", {}, "c", "Course"), [])

    def test_cancelled_before_start_does_not_use_daily_allowance(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(pipeline, "descargar_zoom") as download:
            self.assertEqual(
                pipeline.run_pipeline(
                    "https://zoom.us/rec/play/test",
                    "",
                    "Class",
                    self.s,
                    lambda _: None,
                    lambda _: None,
                    cancel,
                ),
                (None, None),
            )
        download.assert_not_called()
        self.assertEqual(WorkLedger(self.s).daily()["used"], 0)


if __name__ == "__main__":
    unittest.main()

"""Course selection must restrict discovery before batch admission or Zoom access."""

import unittest
from unittest.mock import Mock, patch

import test_hardening as base

from zoom2txt.blackboard import client as bc
from zoom2txt.blackboard.registry import Registry
from zoom2txt.demo import DemoMainWindow, demo_recordings
from zoom2txt.gui import workers
from zoom2txt.gui.health_view import HealthView
from zoom2txt.gui.main_window import MainWindow
from zoom2txt.workflow import WorkLedger

COURSES = [{"id": cid, "name": "Curso con el mismo nombre"} for cid in ("early", "chosen")]


class CourseSearchTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def scan(self, course_id=None, ranges=None):
        visited, details, sessions, result, errors, choices = [], [], [], [], [], []

        class Client:
            def __init__(self, *args):
                self.session = Mock()
                sessions.append(self.session)

            def is_logged_in(self):
                return True

            def get_current_courses(self):
                return COURSES

            def find_zoom_content(self, cid):
                visited.append(cid)
                return "zoom"

            def open_zoom_portal(self, cid, content):
                return "origin", cid, {}

            def list_recordings(self, origin, cid, headers, course_id, name):
                return [
                    bc.Recording(
                        cid,
                        name,
                        f"{cid}-{day}",
                        "Clase",
                        "",
                        extra={"startTime": f"2026-04-{day:02d}T17:00:00Z", "duration": 90},
                    )
                    for day in range(1, 5)
                ]

            def recording_detail(self, origin, cid, headers, rec):
                details.append(rec.course_id)
                rec.play_url = "https://zoom.us/rec/play/synthetic"
                return rec

            def export_cookies(self, path):
                pass

        worker = workers.ScanWorker(self.s, ranges, course_id=course_id)
        worker.found.connect(result.append)
        worker.failed.connect(errors.append)
        worker.courses_loaded.connect(choices.append)
        with patch.object(bc, "BlackboardClient", Client):
            worker.run()
        for session in sessions:
            session.close.assert_called_once()
        return worker, result, errors, visited, details, choices

    def test_selected_course_is_isolated_before_six_item_limit(self):
        worker, result, errors, visited, details, choices = self.scan("chosen")
        self.assertEqual(errors, [])
        self.assertEqual(visited, ["chosen"])
        self.assertEqual(details, ["chosen"] * 4)
        self.assertEqual(len(result[0]), 4)
        self.assertEqual({r["course_id"] for r in worker.all_pending}, {"chosen"})
        self.assertEqual(choices, [COURSES])
        self.assertEqual(WorkLedger(self.s).course_choices(), COURSES)
        self.assertEqual({r["course_id"] for r in WorkLedger(self.s).inventory()}, {"chosen"})

    def test_course_and_week_filters_intersect(self):
        _, result, errors, visited, _, _ = self.scan("chosen", [("2026-04-02", "2026-04-03")])
        self.assertEqual(errors, [])
        self.assertEqual(visited, ["chosen"])
        self.assertEqual([r["start_time"][:10] for r in result[0]], ["2026-04-02", "2026-04-03"])

    def test_all_courses_still_works_and_keeps_batch_limit(self):
        worker, result, errors, visited, _, _ = self.scan()
        self.assertEqual(errors, [])
        self.assertEqual(visited, ["early", "chosen"])
        self.assertEqual(len(result[0]), 6)
        self.assertEqual(len(worker.all_pending), 8)

    def test_missing_course_never_falls_back_to_all_courses(self):
        _, result, errors, visited, details, _ = self.scan("removed")
        self.assertEqual(result, [])
        self.assertEqual(visited, [])
        self.assertEqual(details, [])
        self.assertIn("ya no está disponible", errors[0])

    def test_processed_recording_stays_excluded(self):
        Registry(self.s).mark_skipped("chosen", "chosen-1", "Synthetic omission")
        worker, result, errors, _, _, _ = self.scan("chosen")
        self.assertEqual(errors, [])
        self.assertEqual(len(result[0]), 3)
        self.assertNotIn("chosen-1", [r["meeting_id"] for r in worker.all_pending])

    def test_course_list_loads_without_zoom_access(self):
        client = Mock()
        client.is_logged_in.return_value = True
        client.get_current_courses.return_value = COURSES
        result = []
        worker = workers.CourseListWorker(self.s)
        worker.found.connect(result.append)
        with patch.object(bc, "BlackboardClient", return_value=client):
            worker.run()
        self.assertEqual(result, [COURSES])
        client.find_zoom_content.assert_not_called()
        client.list_recordings.assert_not_called()
        client.session.close.assert_called_once()
        self.assertEqual(WorkLedger(self.s).course_choices(), COURSES)
        self.assertEqual(WorkLedger(self.s).courses(), [])  # A course list is not a scan.

    def test_expired_session_requests_login_without_losing_choices(self):
        WorkLedger(self.s).remember_courses(COURSES)
        client = Mock()
        client.is_logged_in.return_value = False
        login, result = [], []
        worker = workers.CourseListWorker(self.s)
        worker.needs_login.connect(lambda: login.append(True))
        worker.found.connect(result.append)
        with patch.object(bc, "BlackboardClient", return_value=client):
            worker.run()
        self.assertEqual(login, [True])
        self.assertEqual(result, [])
        self.assertEqual(WorkLedger(self.s).course_choices(), COURSES)
        client.get_current_courses.assert_not_called()
        client.session.close.assert_called_once()

    def test_cancelled_course_load_does_not_replace_saved_choices(self):
        WorkLedger(self.s).remember_courses(COURSES)
        client = Mock()
        client.get_current_courses.return_value = []
        worker = workers.CourseListWorker(self.s)
        worker.cancel()
        with patch.object(bc, "BlackboardClient", return_value=client):
            worker.run()
        self.assertEqual(WorkLedger(self.s).course_choices(), COURSES)

    def test_choices_do_not_leak_into_another_term(self):
        WorkLedger(self.s).remember_courses(COURSES)
        self.s.term_start = "2026-08-24"
        self.assertEqual(WorkLedger(self.s).course_choices(), [])

    def test_changing_course_clears_previous_results_and_filters_saved_work(self):
        WorkLedger(self.s).remember_courses(COURSES)
        with patch.object(HealthView, "refresh"):
            window = MainWindow(self.s)
        try:
            window._on_scan_found(demo_recordings())
            items = demo_recordings()
            items[0]["course_id"] = "chosen"
            with patch("zoom2txt.work_items.local_continuations", return_value=items[:1]) as load:
                window.course_combo.setCurrentIndex(window.course_combo.findData("chosen"))
            load.assert_called_once_with(self.s, [], course_id="chosen")
            self.assertEqual(window.table.rowCount(), 1)
            self.assertEqual(window._scan_items[0]["course_id"], "chosen")
            self.assertTrue(window.skip_all_btn.isHidden())
            self.assertIsNone(window.scan_worker)
        finally:
            window.close()

    def test_refresh_preserves_selected_course_even_if_removed(self):
        with patch.object(HealthView, "refresh"):
            window = MainWindow(self.s)
        try:
            window._set_courses(COURSES)
            window.course_combo.setCurrentIndex(window.course_combo.findData("chosen"))
            window._set_courses(COURSES[:1])
            self.assertEqual(window.course_combo.currentData(), "chosen")
            self.assertIn("no disponible", window.course_combo.currentText())
        finally:
            window.close()

    def test_demo_filters_courses_without_live_network(self):
        with patch.object(HealthView, "refresh"):
            window = DemoMainWindow(self.s)
        try:
            window.course_combo.setCurrentIndex(window.course_combo.findData("demo-course-2"))
            window._scan()
            self.assertEqual(window.table.rowCount(), 1)
            self.assertEqual(window._scan_items[0]["course_id"], "demo-course-2")
        finally:
            window.close()

    def test_saved_course_is_filtered_before_batch_limit(self):
        from zoom2txt.work_items import local_continuations

        ledger = WorkLedger(self.s)
        ledger.remember_courses(COURSES)
        for course in COURSES:
            ledger.remember_course(
                course["id"],
                course["name"],
                [
                    dict(
                        meeting_id=str(day),
                        name="Saved class",
                        start_time=f"2026-04-{day:02d} 12:00:00",
                        duration=90,
                    )
                    for day in range(1, 5)
                ],
            )
        self.s.batch_size = 2
        with patch("zoom2txt.work_items.continuation_data", return_value={"play_url": ""}):
            rows = local_continuations(self.s, course_id="chosen")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["course_id"] for row in rows}, {"chosen"})


if __name__ == "__main__":
    unittest.main()

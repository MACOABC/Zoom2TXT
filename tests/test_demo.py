"""The portfolio demo remains isolated from every live integration."""

import unittest
from unittest.mock import patch

import test_hardening as base

from zoom2txt.demo import DemoMainWindow, demo_recordings
from zoom2txt.gui.health_view import HealthView


class DemoTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def test_inventory_uses_only_synthetic_identifiers(self):
        rows = demo_recordings()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("demo" in row["course_id"] for row in rows))
        self.assertTrue(all(row["play_url"].startswith("demo://") for row in rows))

    def test_demo_loads_without_credentials_or_http(self):
        with patch.object(HealthView, "refresh"):
            window = DemoMainWindow(self.s)
            window._show_demo_inventory()
            self.assertEqual(window.table.rowCount(), 3)
            self.assertIn("datos sintéticos", window.scan_status.text())
            self.assertFalse(window.settings_btn.isEnabled())
            window.close()


if __name__ == "__main__":
    unittest.main()

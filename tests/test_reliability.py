"""Reliability center and credential-free backup tests; no live network."""

from __future__ import annotations

import unittest
import zipfile
from types import SimpleNamespace as NS
from unittest.mock import patch

import test_hardening as base

from zoom2txt import backup, health
from zoom2txt.gui.health_view import HealthView
from zoom2txt.storage import StateStore


class ReliabilityTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def test_quick_health_is_local_and_ready(self):
        tool = self.root / "yt-dlp.exe"
        runtime = self.root / "deno.exe"
        tool.write_bytes(b"fixture")
        runtime.write_bytes(b"fixture")
        with (
            patch.object(health, "credentials_status", return_value=[]),
            patch.object(health, "yt_dlp_path", return_value=str(tool)),
            patch.object(health, "deno_path", return_value=runtime),
            patch.object(
                health.shutil,
                "disk_usage",
                return_value=NS(free=20 * 1024**3),
            ),
        ):
            results = health.quick_health(self.s)
            self.assertFalse(health.pipeline_blockers(self.s))
        self.assertFalse([result for result in results if result.status == "error"])
        self.assertEqual(
            {result.key for result in results},
            {
                "accounts",
                "videos",
                "transcripts",
                "materials",
                "disk",
                "engines",
                "database",
                "drive",
            },
        )

    def test_low_disk_is_a_pipeline_blocker(self):
        tool = self.root / "yt-dlp.exe"
        runtime = self.root / "deno.exe"
        tool.write_bytes(b"fixture")
        runtime.write_bytes(b"fixture")
        with (
            patch.object(health, "credentials_status", return_value=[]),
            patch.object(health, "yt_dlp_path", return_value=str(tool)),
            patch.object(health, "deno_path", return_value=runtime),
            patch.object(
                health.shutil,
                "disk_usage",
                return_value=NS(free=512 * 1024**2),
            ),
        ):
            blockers = health.pipeline_blockers(self.s)
        self.assertEqual([result.key for result in blockers], ["disk"])

    def test_history_backup_excludes_secrets_and_restores_profile(self):
        state = StateStore(self.s)
        state.put("job", "class-one", status="done", name="Original")
        state.put("quota", "today", used=9000)
        state.put("daily_admissions", "2026-09-04", jobs=["local-class"])
        destination = self.root / "history.zip"
        backup.export_history(self.s, destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "state.sqlite3"})
            raw = archive.read("state.sqlite3")
            for secret_name in (b"token.json", b"client_secrets", b"cookies"):
                self.assertNotIn(secret_name, raw)

        state.put("job", "class-one", status="failed", name="Changed")
        state.put("quota", "today", used=1)
        state.put("daily_admissions", "2026-09-04", jobs=["current-class"])
        count, safety_copy = backup.restore_history(self.s, destination)
        self.assertGreaterEqual(count, 1)
        self.assertTrue(safety_copy.is_file())
        self.assertEqual(state.get("job", "class-one")["status"], "done")
        self.assertEqual(state.get("quota", "today")["used"], 1)
        self.assertEqual(state.get("daily_admissions", "2026-09-04")["jobs"], ["current-class"])

    def test_invalid_backup_is_rejected_before_merge(self):
        source = self.root / "foreign.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("../token.json", "secret")
        with self.assertRaisesRegex(ValueError, "respaldo"):
            backup.restore_history(self.s, source)

    def test_health_view_explains_errors_without_color_only(self):
        view = HealthView(self.s)
        view._loaded(
            [
                health.HealthResult(
                    "disk",
                    "Espacio en disco",
                    "error",
                    "Solo quedan 0.5 GB libres",
                    "Libera al menos 2 GB.",
                ),
                health.HealthResult("database", "Historial local", "ok", "Base accesible"),
            ]
        )
        self.assertEqual(view.table.item(0, 1).text(), "Acción necesaria")
        self.assertIn("1 acción necesaria", view.headline.text())
        self.assertTrue(view.export_button.isEnabled())
        view.close()


if __name__ == "__main__":
    unittest.main()

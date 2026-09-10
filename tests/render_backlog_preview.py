"""Qt screenshot fixtures: both themes, weekly comparison, empty and compact."""

import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
fixture = tempfile.TemporaryDirectory(prefix="zoom2txt-preview-")
os.environ["ZOOM2TXT_DATA_DIR"] = fixture.name
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QTabWidget

from zoom2txt.config import Settings
from zoom2txt.gui import style
from zoom2txt.gui.main_window import MainWindow
from zoom2txt.workflow import WorkLedger

app = QApplication([])
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
root = Path(fixture.name)
settings = Settings(
    zoom_user="demo",
    term_start="2026-08-24",
    videos_dir=root / "videos",
    transcripts_dir=root / "txt",
    materials_dir=root / "material",
)
now = datetime(2026, 9, 3, 21, 0, tzinfo=ZoneInfo("America/Lima"))


def settle():
    end = time.monotonic() + 0.45
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


with patch("zoom2txt.workflow.local_now", return_value=now):
    ledger = WorkLedger(settings)
    courses = [
        {"id": str(i), "name": name}
        for i, name in enumerate(
            (
                "APRENDIZAJE DE MÁQUINA / 755",
                "CIBERSEGURIDAD / 755",
                "INGENIERÍA DE SOFTWARE II / 853",
                "REDES AVANZADAS / 753",
            )
        )
    ]
    ledger.remember_courses(courses)
    for course in courses:
        dates = (
            ["2026-08-25", "2026-08-27", "2026-09-01"]
            if course["id"] != "2"
            else ["2026-08-27", "2026-09-03"]
        )
        ledger.remember_course(
            course["id"],
            course["name"],
            [
                {
                    "meeting_id": f"{course['id']}-{i}",
                    "name": f"Clase {i + 1}",
                    "start_time": d + " 17:00:00",
                    "duration": 110,
                }
                for i, d in enumerate(dates)
            ],
        )
        ledger.set_expected(course["id"], 2)
    for n in range(6):
        ledger.admit(str(n))
    for mode in ("dark", "light"):
        style.apply_theme(app, mode)
        with patch.object(MainWindow, "_setup_tray"):
            window = MainWindow(settings)
        window.resize(1180, 780)
        window.show()
        window._navigate(4)
        settle()
        window.grab().save(str(out / f"pendientes-{mode}.png"))
        window.backlog.findChild(QTabWidget).setCurrentIndex(1)
        settle()
        window.grab().save(str(out / f"semanas-{mode}.png"))
        window.resize(1024, 680)
        settle()
        window.grab().save(str(out / f"compacto-{mode}.png"))
        window._navigate(0)
        window._refresh_backlog_notice()
        settle()
        window.grab().save(str(out / f"inicio-{mode}.png"))
        window.close()
        window.deleteLater()
        settle()
    empty = Settings(
        zoom_user="empty",
        term_start="2026-08-24",
        videos_dir=root / "video-empty",
        transcripts_dir=root / "txt-empty",
        materials_dir=root / "material-empty",
    )
    with patch.object(MainWindow, "_setup_tray"):
        window = MainWindow(empty)
    window.show()
    window._navigate(4)
    settle()
    window.grab().save(str(out / "vacio-light.png"))
    window.close()
    window.deleteLater()
    settle()
fixture.cleanup()
print(out)

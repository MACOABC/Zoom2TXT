"""Render the real Qt widgets with fictitious data, never user accounts."""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
fixture = tempfile.TemporaryDirectory(prefix="zoom2txt-preview-")
os.environ["ZOOM2TXT_DATA_DIR"] = fixture.name
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from zoom2txt.blackboard.registry import Registry
from zoom2txt.config import Settings
from zoom2txt.gui import style
from zoom2txt.gui.history_dialog import HistoryDialog
from zoom2txt.gui.main_window import MainWindow
from zoom2txt.gui.settings_dialog import SettingsDialog
from zoom2txt.storage import StateStore, atomic_write

app = QApplication([])
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
settings = Settings(
    zoom_user="demo",
    videos_dir=Path(fixture.name) / "videos",
    transcripts_dir=Path(fixture.name) / "txt",
    materials_dir=Path(fixture.name) / "materials",
    term_start="2026-03-30",
    wizard_done=True,
)
txt = settings.transcripts_dir / "AprendizajeDeMaquina_Semana01_Para_NotebookLM.txt"
atomic_write(txt, b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nClase de aprendizaje de maquina.\n")
registry = Registry(settings)
registry.mark_done("c", "m", "AprendizajeDeMaquina_Semana01", txt)
registry.mark_skipped(
    "c", "m2", "Ya la tenia antes de instalar la app", "AprendizajeDeMaquina_Semana02"
)
StateStore(settings).put(
    "job",
    "demo",
    name="IngenieriaSoftware_Semana03",
    status="waiting_captions",
    video_id="DEMO1234567",
)


def settle(ms=250):
    until = time.monotonic() + ms / 1000
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(0.01)


for mode in ("dark", "light"):
    settings.theme = mode
    style.apply_theme(app, mode)
    with patch.object(MainWindow, "_setup_tray"):
        window = MainWindow(settings)
    window.resize(1280, 850)
    window.show()
    window._on_scan_found(
        [
            dict(
                course_name="APRENDIZAJE DE MÁQUINA / 754",
                name="AprendizajeDeMaquina_Semana01_2026-04-01",
                start_time="2026-04-01 10:00:00",
                sort_key="2026-04-01",
                course_id="c",
                meeting_id="m3",
            ),
            dict(
                course_name="INGENIERÍA DE SOFTWARE I / 752",
                name="IngenieriaSoftware_Semana02_2026-04-07",
                start_time="2026-04-07 14:00:00",
                sort_key="2026-04-07",
                course_id="d",
                meeting_id="m4",
            ),
        ]
    )
    window._log("[i] Muestra visual con datos ficticios. No se ha conectado a ninguna cuenta.")
    settle()
    window.grab().save(str(out / f"clases-{mode}.png"))
    window._on_material_found(
        (
            [
                {
                    "course": "APRENDIZAJE DE MÁQUINA / 754",
                    "total": 18,
                    "have": 15,
                    "missing": 3,
                    "note": "Adjuntos revisados",
                    "error": False,
                }
            ],
            ["fixture-1", "fixture-2", "fixture-3"],
        )
    )
    window._navigate(2)
    settle()
    window.grab().save(str(out / f"material-{mode}.png"))
    dialog = SettingsDialog(settings, window)
    dialog.show()
    settle()
    dialog.grab().save(str(out / f"ajustes-{mode}.png"))
    print(
        mode,
        "window",
        window.size().width(),
        window.size().height(),
        "settings min",
        dialog.minimumSizeHint().width(),
        dialog.minimumSizeHint().height(),
    )
    dialog.close()
    history = HistoryDialog(settings, window)
    history.show()
    settle()
    history.grab().save(str(out / f"historial-{mode}.png"))
    history.close()
    window._navigate(3)
    settle(700)
    window.grab().save(str(out / f"biblioteca-{mode}.png"))
    if window.library._loader:
        window.library._loader.wait(5000)
    window.resize(1024, 680)
    window._navigate(0)
    settle()
    window.grab().save(str(out / f"compacto-{mode}.png"))
    print(mode, "compact", window.width(), window.height())
    for obj in (
        window.stack,
        window.stack.currentWidget(),
        window.weeks_box,
        window._card,
    ):
        print(
            type(obj).__name__,
            "size",
            obj.size(),
            "min",
            obj.minimumSizeHint(),
            "hint",
            obj.sizeHint(),
            "hfw",
            obj.heightForWidth(obj.width()),
        )
    window.close()
    settle()

print("Fonts:", ", ".join(QFontDatabase.families()[:8]))
fixture.cleanup()

"""Render the changed desktop surface with synthetic queue results."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
fixture = tempfile.TemporaryDirectory(prefix="zoom2txt-continuation-preview-")
os.environ["ZOOM2TXT_DATA_DIR"] = fixture.name
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from zoom2txt.config import Settings
from zoom2txt.gui import style
from zoom2txt.gui.main_window import MainWindow

app = QApplication([])
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
settings = Settings(
    zoom_user="visual-test",
    term_start="2026-08-24",
    videos_dir=Path(fixture.name) / "videos",
    transcripts_dir=Path(fixture.name) / "txt",
    materials_dir=Path(fixture.name) / "material",
)
names = ["REDES AVANZADAS / 753", "CIBERSEGURIDAD / CYB / 755", "APRENDIZ. DE MÁQUINA / 755"]
items = [
    dict(
        course_id=str(i % 3),
        course_name=names[i % 3],
        meeting_id=str(i),
        name=f"Clase_Semana02_2026-09-0{i + 1}_26510a6a",
        start_time=f"2026-09-0{i + 1}",
        sort_key=f"2026-09-0{i + 1}",
        play_url="https://zoom.us/rec/example",
    )
    for i in range(6)
]
for mode, width, height in [("dark", 1180, 780), ("light", 1180, 780), ("dark", 880, 660)]:
    style.apply_theme(app, mode)
    with patch.object(MainWindow, "_load_saved_work"), patch.object(MainWindow, "_setup_tray"):
        window = MainWindow(settings)
        window.resize(width, height)
        window._on_scan_found(items)
        window.weeks_chk.setChecked(True)
        window.week_buttons[1].setChecked(True)
        window._queue_items = items
        window._queue_total = 6
        for i in (0, 1):
            window._on_queue_item_finished(i, "example.txt")
        window._on_queue_done(2, 1)
        window._set_class_status(
            2,
            "Por resolver",
            "YouTube no respondió. Tu descarga sigue guardada; vuelve a continuar esta tanda.",
        )
        window._set_class_status(3, "Video en esta PC")
        window._set_class_status(4, "Revisar YouTube")
        window._set_class_status(5, "Recuperar subida")
        window._show_class_detail(2, 5)
        window._log("[+] Video local verificado. Se reutiliza la descarga guardada.")
        window._log("[i] Dos TXT verificados. Las demás clases permanecen marcadas para continuar.")
        window.show()
        QTest.qWait(250)
        window.grab().save(str(out / f"continuation-{mode}-{width}.png"))
        window.close()
print(out)

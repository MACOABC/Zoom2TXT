"""Generate recruiter-facing screenshots from the offline synthetic demo."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
session = tempfile.TemporaryDirectory(prefix="zoom2txt-portfolio-capture-")
os.environ["ZOOM2TXT_DATA_DIR"] = session.name
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from zoom2txt import library
from zoom2txt.demo import DemoMainWindow, demo_settings
from zoom2txt.gui import style
from zoom2txt.gui.main_window import PAGE_ASSISTANT, PAGE_CLASSES, PAGE_LIBRARY
from zoom2txt.storage import atomic_write


def settle(app: QApplication, milliseconds: int = 300) -> None:
    until = time.monotonic() + milliseconds / 1000
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(0.01)


def capture(window: DemoMainWindow, path: Path, app: QApplication) -> None:
    settle(app)
    if not window.grab().save(str(path)):
        raise RuntimeError(f"No se pudo crear {path.name}")


def main() -> int:
    output = root / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    style.apply_theme(app, "dark")
    settings = demo_settings()
    window = DemoMainWindow(settings)
    window.resize(1280, 900)
    window.show()
    window._navigate(PAGE_CLASSES)
    window._show_demo_inventory()

    frames = []
    inventory = output / "demo-classes.png"
    capture(window, inventory, app)
    frames.append(inventory)

    # Tanda en curso: una clase lista, otra esperando texto y la tercera descargando.
    window.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    window._set_class_status(0, "TXT verificado", "Archivo sintético disponible.")
    window._set_class_status(1, "Texto en proceso", "YouTube genera subtítulos en la simulación.")
    window._set_class_status(2, "Preparando video", "Descarga simulada.")
    window._queue_items = list(window._scan_items)
    window._queue_total = 3
    window._active_queue_index = 2
    window._set_activity("Transcribiendo", "Clase 3/3", "info", True)
    window._on_stage(0)
    window._on_progress(0, 0.62, "177 de 286 MB · 9.4 MB/s · faltan 12 s")
    window.scan_status.setText("Simulación en curso · cada fila conserva su propio estado.")
    window._log("[+] Primera transcripción sintética verificada.")
    window._log("[i] 1 transcripción en proceso. Próxima revisión en 120 segundos.")
    window._log("[*] Descargando clase de Zoom: AnaliticaDeDatos_Semana02…")
    processing = output / "demo-processing.png"
    capture(window, processing, app)
    frames.append(processing)

    for index, course in enumerate(
        ("ArquitecturaDeSoftware", "SeguridadAplicada", "AnaliticaDeDatos"), 1
    ):
        for week in (1, 2, 3):
            atomic_write(
                settings.transcripts_dir
                / f"{course}_Semana{week:02d}_2026-09-0{week}_Para_NotebookLM.txt",
                (
                    "Transcripción sintética para una demostración segura. "
                    * (240 + index * 30 * week)
                ).strip(),
            )
    stats = library.scan_library(settings)
    window._navigate(PAGE_LIBRARY)
    window.library._loaded(stats)
    library_image = output / "demo-library.png"
    capture(window, library_image, app)
    frames.append(library_image)

    window._navigate(PAGE_ASSISTANT)
    settle(app, 400)
    window.assistant._trigger_quick_prompt("¿Qué temas entran al parcial?")
    assistant_image = output / "demo-assistant.png"
    capture(window, assistant_image, app)
    frames.append(assistant_image)

    gif_frames = [
        Image.open(path).convert("P", palette=Image.Palette.ADAPTIVE, colors=128) for path in frames
    ]
    gif_frames[0].save(
        output / "demo-flow.gif",
        save_all=True,
        append_images=gif_frames[1:],
        duration=(1800, 2400, 2400, 2800),
        loop=0,
        optimize=True,
    )
    window.assistant.cancel_all()
    window.close()
    session.cleanup()
    print("Capturas sintéticas creadas en docs/images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

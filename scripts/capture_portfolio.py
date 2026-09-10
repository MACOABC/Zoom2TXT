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
from zoom2txt.storage import atomic_write


def settle(app: QApplication, milliseconds: int = 250) -> None:
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
    window._show_demo_inventory()

    frames = []
    inventory = output / "demo-classes.png"
    capture(window, inventory, app)
    frames.append(inventory)

    window.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    window._set_class_status(0, "TXT verificado", "Archivo sintético disponible.")
    window._set_class_status(1, "Subtítulos…", "YouTube genera subtítulos en la simulación.")
    window._set_class_status(2, "Lista para simular", "Siguiente clase de la tanda.")
    window.stage_rows[0].set_state("done")
    window.stage_rows[1].set_state("done")
    window.stage_rows[2].set_state("active")
    window.queue_label.setText("2 de 3 · Seguridad aplicada")
    window.scan_status.setText("Simulación en curso · cada fila conserva su propio estado.")
    window._log("[+] Primera transcripción sintética verificada.")
    window._log("[*] Esperando subtítulos automáticos para la segunda clase…")
    processing = output / "demo-processing.png"
    capture(window, processing, app)
    frames.append(processing)

    for index, course in enumerate(
        ("ArquitecturaDeSoftware", "SeguridadAplicada", "AnaliticaDeDatos"), 1
    ):
        atomic_write(
            settings.transcripts_dir / f"{course}_Semana02_Para_NotebookLM.txt",
            ("Transcripción sintética para una demostración segura. " * (240 + index * 30)).strip(),
        )
    stats = library.scan_library(settings)
    window._navigate(3)
    window.library._loaded(stats)
    library_image = output / "demo-library.png"
    capture(window, library_image, app)
    frames.append(library_image)

    gif_frames = [
        Image.open(path).convert("P", palette=Image.Palette.ADAPTIVE, colors=128) for path in frames
    ]
    gif_frames[0].save(
        output / "demo-flow.gif",
        save_all=True,
        append_images=gif_frames[1:],
        duration=(1800, 2200, 2600),
        loop=0,
        optimize=True,
    )
    window.close()
    session.cleanup()
    print("Capturas sintéticas creadas en docs/images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

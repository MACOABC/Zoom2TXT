"""Offline product tour with synthetic academic data only."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer

from zoom2txt.config import DATA_DIR, Settings
from zoom2txt.gui.main_window import MainWindow
from zoom2txt.storage import atomic_write

DEMO_COURSES = (
    "ARQUITECTURA DE SOFTWARE / DEMO-101",
    "SEGURIDAD APLICADA / DEMO-202",
    "ANALÍTICA DE DATOS / DEMO-303",
)


def demo_settings() -> Settings:
    """Create an isolated profile; callers set ZOOM2TXT_DATA_DIR first."""
    settings = Settings(
        zoom_user="demo-user",
        blackboard_url="https://campus.example.invalid",
        term_start="2026-08-24",
        videos_dir=DATA_DIR / "videos-sinteticos",
        transcripts_dir=DATA_DIR / "transcripciones-sinteticas",
        materials_dir=DATA_DIR / "material-sintetico",
        wizard_done=False,
    )
    for folder in (
        settings.videos_dir,
        settings.transcripts_dir,
        settings.materials_dir,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    return settings


def demo_recordings() -> list[dict]:
    dates = ("2026-09-01", "2026-09-02", "2026-09-03")
    rows = []
    for index, (course, day) in enumerate(zip(DEMO_COURSES, dates, strict=True), 1):
        rows.append(
            {
                "course_id": f"demo-course-{index}",
                "course_name": course,
                "meeting_id": f"demo-recording-{index}",
                "job_key": f"demo-job-{index}",
                "name": f"{course.split(' / ')[0].title().replace(' ', '')}_Semana02_{day}",
                "start_time": day + "T17:00:00-05:00",
                "sort_key": day,
                "play_url": f"demo://recording/{index}",
            }
        )
    return rows


class DemoMainWindow(MainWindow):
    """Safe, network-free tour of the discovery and processing experience."""

    def __init__(self, settings: Settings):
        self._demo_queue: list[int] = []
        self._demo_position = 0
        self._demo_stage = 0
        super().__init__(settings)
        self.setWindowTitle("Zoom2TXT · Demostración")
        self.settings_btn.setEnabled(False)
        self.settings_btn.setToolTip("Las cuentas están desactivadas en la demostración.")
        self.history_btn.setEnabled(False)
        self.history_btn.setToolTip("La demostración no conserva historial.")
        for index, button in enumerate(self._nav_buttons):
            if index not in (0, 3):
                button.setEnabled(False)
                button.setToolTip(
                    "Esta sección requiere una cuenta real y está desactivada en la demostración."
                )

    def _setup_tray(self) -> None:
        self.tray = None

    def _refresh_backlog_notice(self) -> None:
        if hasattr(self, "backlog_notice"):
            self.backlog_notice.setText(
                "Recorrido seguro: 3 grabaciones ficticias · 0 conexiones externas."
            )

    def _load_saved_work(self) -> None:
        QTimer.singleShot(80, self._show_demo_inventory)

    def _scan(self) -> None:
        self._show_demo_inventory()

    def _show_demo_inventory(self) -> None:
        self._reset_stages()
        self.console.clear()
        self._on_scan_found(demo_recordings())
        self.scan_btn.setText("Reiniciar demostración")
        self.process_btn.setText("Simular flujo completo")
        self.skip_all_btn.hide()
        self.delete_chk.setEnabled(False)
        self.scan_status.setText(
            "Modo demo · datos sintéticos. No se conecta a Blackboard, Zoom, Google ni YouTube."
        )
        for row in range(self.table.rowCount()):
            self._set_class_status(
                row, "Lista para simular", "Grabación ficticia preparada para el recorrido."
            )
        self._log(
            "[i] Inventario sintético cargado. Ninguna credencial ni servicio externo está activo."
        )

    def _process_queue(self) -> None:
        if self._demo_queue:
            return
        selected = [
            row
            for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]
        if not selected:
            self.scan_status.setText(
                "Selecciona al menos una clase ficticia para iniciar el recorrido."
            )
            return
        self._demo_queue = selected
        self._queue_items = [self._scan_items[row] for row in selected]
        self._demo_position = 0
        self._demo_stage = 0
        self.table.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.process_btn.setEnabled(False)
        self._advance_demo()

    def _advance_demo(self) -> None:
        if not self._demo_queue:
            return
        row = self._demo_queue[self._demo_position]
        item = self._scan_items[row]
        labels = (
            "Descargando MP4…",
            "Subiendo como no listado…",
            "Esperando subtítulos…",
            "Verificando TXT…",
        )
        self._active_queue_index = self._demo_position
        self._on_stage(self._demo_stage)
        self._set_class_status(
            row, labels[self._demo_stage], "Simulación local; no se envían datos."
        )
        self.queue_label.setText(
            f"Clase {self._demo_position + 1}/{len(self._demo_queue)} · {item['course_name']}"
        )
        if self._demo_stage == 0:
            self._log(f"[*] Preparando {item['name']} con datos sintéticos…")
        if self._demo_stage < 3:
            self._demo_stage += 1
            QTimer.singleShot(360, self._advance_demo)
            return

        transcript = self.settings.transcripts_dir / f"{item['name']}_Para_NotebookLM.txt"
        atomic_write(
            transcript,
            (
                f"{item['course_name']}\n\n"
                "Transcripción sintética para demostrar el flujo de Zoom2TXT. "
                "No contiene una clase, una cuenta ni información personal real.\n"
            ),
        )
        self._set_class_status(row, "TXT verificado", f"Archivo sintético: {transcript.name}")
        self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self._log(f"[+] TXT sintético verificado: {transcript.name}")
        self._demo_position += 1
        self._demo_stage = 0
        if self._demo_position < len(self._demo_queue):
            QTimer.singleShot(360, self._advance_demo)
            return

        for stage in self.stage_rows:
            stage.set_state("done")
        self._result_path = transcript
        self.open_btn.show()
        self.table.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.process_btn.setEnabled(True)
        self.process_btn.hide()
        self.scan_status.setText(
            "Demostración completada. Abre Biblioteca para revisar los TXT sintéticos."
        )
        self.queue_label.setText(f"{len(self._demo_queue)} TXT sintéticos verificados")
        self._demo_queue = []
        self._active_queue_index = None

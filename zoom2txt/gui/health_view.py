"""Centro de estado: comprobaciones claras y diagnósticos sin secretos."""

from __future__ import annotations

import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zoom2txt import APP_NAME, __version__
from zoom2txt.gui import lucide, style
from zoom2txt.health import HealthResult, full_health
from zoom2txt.security import redact
from zoom2txt.storage import atomic_write


class HealthWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings

    def run(self) -> None:
        try:
            self.loaded.emit(full_health(self.settings, self.isInterruptionRequested))
        except Exception as exc:  # noqa: BLE001 — QThread exception boundary
            self.failed.emit(redact(exc))


class HealthView(QWidget):
    """A compact readiness surface; it does not download or upload classes."""

    settings_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._worker: HealthWorker | None = None
        self._results: list[HealthResult] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(16)

        summary = QFrame()
        summary.setObjectName("healthSummary")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(20, 18, 20, 18)
        summary_layout.setSpacing(20)
        copy = QVBoxLayout()
        copy.setSpacing(5)
        self.headline = QLabel("Comprueba si todo está listo")
        self.headline.setObjectName("healthHeadline")
        self.headline.setWordWrap(True)
        self.description = QLabel(
            "Valida cuentas, herramientas, carpetas, espacio y sesiones antes de procesar una clase."
        )
        self.description.setObjectName("muted")
        self.description.setWordWrap(True)
        copy.addWidget(self.headline)
        copy.addWidget(self.description)
        summary_layout.addLayout(copy, 1)
        self.refresh_button = QPushButton("Comprobar ahora")
        self.refresh_button.setProperty("variant", "primary")
        self.refresh_button.setIcon(lucide.icon("refresh-cw", style.ON_ACCENT, 16))
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.setAccessibleName("Comprobar el estado de Zoom2TXT")
        self.refresh_button.clicked.connect(self.refresh)
        summary_layout.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(summary)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Componente", "Estado", "Detalle"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAccessibleName("Comprobaciones del estado de Zoom2TXT")
        style.configure_table(self.table)
        self.table.setMinimumHeight(style.HEADER_HEIGHT + 8 * style.ROW_HEIGHT + 2)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        note = QLabel(
            "La comprobación no descarga clases ni crea videos en YouTube. El informe nunca incluye contraseñas, cookies ni tokens."
        )
        note.setObjectName("faint")
        note.setWordWrap(True)
        footer.addWidget(note, 1)
        self.export_button = QPushButton("Guardar informe")
        self.export_button.setIcon(lucide.icon("download", style.MUTED, 16))
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        self.settings_button = QPushButton("Abrir ajustes")
        self.settings_button.setIcon(lucide.icon("settings", style.MUTED, 16))
        self.settings_button.clicked.connect(self.settings_requested.emit)
        footer.addWidget(self.export_button)
        footer.addWidget(self.settings_button)
        layout.addLayout(footer)

        self._show_pending()

    def _show_pending(self) -> None:
        names = (
            "Cuentas",
            "Blackboard",
            "YouTube",
            "Motores de procesamiento",
            "Carpetas y espacio",
            "Historial local",
        )
        self.table.setRowCount(len(names))
        for row, name in enumerate(names):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem("Pendiente"))
            self.table.setItem(row, 2, QTableWidgetItem("Aún no comprobado"))

    def refresh(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self.refresh_button.setEnabled(False)
        self.settings_button.setEnabled(False)
        self.refresh_button.setText("Comprobando…")
        self.export_button.setEnabled(False)
        self.headline.setText("Revisando el entorno")
        self.description.setText(
            "Primero se validan los archivos locales; después, las sesiones de Blackboard y YouTube."
        )
        self._worker = HealthWorker(self.settings, self)
        self._worker.loaded.connect(self._loaded)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _worker_finished(self) -> None:
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Comprobar de nuevo")
        self.settings_button.setEnabled(True)
        worker = self.sender()
        if worker is self._worker:
            self._worker = None
        if worker:
            worker.deleteLater()

    def _loaded(self, results: list[HealthResult]) -> None:
        self._results = list(results)
        self.table.setRowCount(len(results))
        labels = {"ok": "Correcto", "warning": "Atención", "error": "Acción necesaria"}
        colors = {"ok": style.SUCCESS, "warning": style.WARNING, "error": style.ERROR}
        for row, result in enumerate(results):
            component = QTableWidgetItem(result.title)
            status = QTableWidgetItem(labels.get(result.status, result.status))
            status.setForeground(QColor(colors.get(result.status, style.TEXT)))
            status.setData(Qt.ItemDataRole.UserRole, result.status)
            detail_text = result.summary
            if result.detail:
                detail_text += " — " + result.detail
            detail = QTableWidgetItem(detail_text)
            detail.setToolTip(detail_text)
            self.table.setItem(row, 0, component)
            self.table.setItem(row, 1, status)
            self.table.setItem(row, 2, detail)

        errors = sum(result.status == "error" for result in results)
        warnings = sum(result.status == "warning" for result in results)
        if errors:
            self.headline.setText(
                f"{errors} acción necesaria" if errors == 1 else f"{errors} acciones necesarias"
            )
            self.description.setText(
                "Corrige los elementos marcados antes de iniciar otra clase; tus trabajos guardados no se borran."
            )
        elif warnings:
            self.headline.setText("Lista para trabajar, con observaciones")
            self.description.setText(
                "No hay bloqueos. Revisa las advertencias para reducir interrupciones futuras."
            )
        else:
            self.headline.setText("Todo listo para procesar clases")
            self.description.setText(
                "Cuentas, herramientas, carpetas y almacenamiento respondieron correctamente."
            )
        self.export_button.setEnabled(True)

    def _failed(self, error: str) -> None:
        self.headline.setText("No se pudo completar la comprobación")
        self.description.setText(
            "No se modificó ningún trabajo. Inténtalo otra vez o guarda el diagnóstico de la carpeta de datos."
        )
        QMessageBox.warning(self, "Comprobación incompleta", redact(error))

    def _export(self) -> None:
        if not self._results:
            return
        default = Path.home() / (f"Zoom2TXT-diagnostico-{datetime.now(UTC):%Y-%m-%d}.json")
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar diagnóstico sin credenciales",
            str(default),
            "Informe JSON (*.json)",
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.suffix.lower() != ".json":
            destination = destination.with_suffix(".json")
        payload = {
            "app": APP_NAME,
            "version": __version__,
            "generated_utc": datetime.now(UTC).isoformat(),
            "system": platform.system(),
            "python_bundled": bool(getattr(sys, "frozen", False)),
            "checks": [result.public() for result in self._results],
            "excluded": ["passwords", "OAuth tokens", "cookies", "client secrets"],
        }
        try:
            atomic_write(destination, json.dumps(payload, ensure_ascii=False, indent=2))
            QMessageBox.information(
                self,
                "Informe guardado",
                "Se creó un diagnóstico sin credenciales. Puedes compartirlo para pedir ayuda.",
            )
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo guardar", "Elige otra carpeta. " + redact(exc))

    def cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()

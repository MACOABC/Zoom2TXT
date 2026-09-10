"""Visible recovery controls; never discard all history to undo one omission."""

import re

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

from zoom2txt.blackboard.registry import Registry
from zoom2txt.gui import style
from zoom2txt.storage import StateStore, file_ok

LABELS = {
    "daily_limit": "Para otro día · límite de 6",
    "done": "Completada",
    "sync_pending": "Copia local pendiente",
    "waiting_captions": "Esperando subtítulos",
    "uploaded": "Subida; falta transcribir",
    "downloading": "Descarga interrumpida",
    "downloaded": "Video descargado",
    "uploading": "Subida reanudable",
    "upload_unknown": "Revisar subida en YouTube",
    "omitted": "Omitida por ti",
    "legacy_review": "Historial anterior · revisar",
    "pending": "Pendiente",
    "duplicate": "Alternativa descartada",
    "transcribed": "TXT validado",
}


class HistoryDialog(QDialog):
    resume = Signal(object)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.registry = Registry(settings)
        self.state = StateStore(settings)
        self.setWindowTitle("Historial y recuperación · Zoom2TXT")
        self.resize(1000, 660)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(16)
        title = QLabel("Cada clase, con su estado")
        title.setObjectName("h1")
        outer.addWidget(title)
        hint = QLabel(
            "Recupera una omisión, continúa un trabajo interrumpido o reintenta una copia. Tus archivos no se borran al restaurar."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        outer.addWidget(hint)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.records = self._table(["Clase", "Estado", "Detalle"])
        self.jobs = self._table(["Clase", "Estado", "Último problema"])
        self.tabs.addTab(self.records, "Clases y omisiones")
        self.tabs.addTab(self.jobs, "Trabajos")
        buttons = QHBoxLayout()
        self.restore_btn = QPushButton("Restaurar selección")
        self.restore_btn.setProperty("variant", "primary")
        self.restore_btn.clicked.connect(self.restore)
        self.resume_btn = QPushButton("Continuar trabajo")
        self.resume_btn.setProperty("variant", "primary")
        self.resume_btn.clicked.connect(self.continue_job)
        self.link_btn = QPushButton("Vincular ID de YouTube")
        self.link_btn.clicked.connect(self.link_video)
        self.reset_upload_btn = QPushButton("Revisar subida fallida")
        self.reset_upload_btn.clicked.connect(self.reset_upload)
        copies = QPushButton("Reintentar copias")
        copies.clicked.connect(lambda: self._emit_resume(None))
        for button in (self.restore_btn, self.resume_btn, self.link_btn, copies):
            buttons.addWidget(button)
        outer.addLayout(buttons)
        outer.addWidget(self.reset_upload_btn)
        self.tabs.currentChanged.connect(self._actions)
        self._actions(0)
        self.refresh()

    def _table(self, headers):
        table = QTableWidget(0, len(headers))
        style.configure_table(table)
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        return table

    def _actions(self, index):
        self.restore_btn.setVisible(index == 0)
        self.resume_btn.setVisible(index == 1)
        self.link_btn.setVisible(index == 1)
        self.reset_upload_btn.setVisible(index == 1)

    def refresh(self):
        self._records = self.registry.records()
        self._jobs = self.state.list("job")
        for table, rows in ((self.records, self._records), (self.jobs, self._jobs)):
            table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                status = LABELS.get(row.get("status"), row.get("status", "Pendiente"))
                if (
                    row.get("status") == "done"
                    and table is self.records
                    and not file_ok(row.get("path"), row.get("checksum", ""))
                ):
                    status = "Falta el TXT o fue modificado"
                if row.get("cleanup_pending"):
                    status += " · borrado YouTube pendiente"
                values = [
                    row.get("display_name") or row.get("name", "Clase"),
                    status,
                    row.get("error") or row.get("note", ""),
                ]
                for j, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    cell.setToolTip(str(value))
                    table.setItem(i, j, cell)

    def restore(self):
        rows = {i.row() for i in self.records.selectedIndexes()}
        if not rows:
            return
        if (
            QMessageBox.question(
                self,
                "Restaurar clases",
                f"{len(rows)} clases volverán a estar disponibles para revisar. No se borrarán archivos. ¿Continuar?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.state.backup()
        for index in rows:
            self.registry.restore(self._records[index]["id"])
        self.refresh()

    def _emit_resume(self, job):
        self.accept()
        self.resume.emit(job)

    def continue_job(self):
        row = self.jobs.currentRow()
        if row < 0:
            QMessageBox.information(
                self,
                "Selecciona una clase",
                "Haz clic en una fila de Trabajos y después en Continuar trabajo.",
            )
            return
        self._emit_resume(self._jobs[row])

    def reset_upload(self):
        index = self.jobs.currentRow()
        if index < 0:
            return
        job = self._jobs[index]
        if job.get("video_id") or job.get("status") not in (
            "uploading",
            "upload_unknown",
        ):
            QMessageBox.information(
                self,
                "No hace falta reiniciar",
                "Usa Continuar trabajo. Esta acción solo se necesita si una subida quedó incierta o caducó.",
            )
            return
        reply = QMessageBox.warning(
            self,
            "Comprueba YouTube antes de repetir",
            "Primero comprueba en YouTube Studio que ESTA clase NO existe. Si existe, cancela y usa Vincular ID.\n\n¿Confirmas que no existe y autorizas iniciar otra subida?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        from zoom2txt.config import DATA_DIR
        from zoom2txt.security import read_private_json, write_private_json

        path = DATA_DIR / "jobs" / self.state.namespace / (job["id"] + ".json")
        private = read_private_json(path)
        private["upload_uri"] = ""
        write_private_json(path, private)
        self.state.put("job", job["id"], status="downloaded", error="", retry_authorized=True)
        self.refresh()

    def link_video(self):
        index = self.jobs.currentRow()
        if index < 0:
            return
        job = self._jobs[index]
        if job.get("video_id"):
            QMessageBox.information(
                self,
                "Subida identificada",
                "Este trabajo ya tiene un video asociado. Usa Continuar trabajo.",
            )
            return
        value, ok = QInputDialog.getText(
            self,
            "Recuperar subida",
            "Revisa YouTube Studio. Pega el ID de 11 caracteres del video de ESTA clase en TU cuenta:",
        )
        if ok and re.fullmatch(r"[A-Za-z0-9_-]{11}", value.strip()):
            self.state.put("job", job["id"], video_id=value.strip(), status="uploaded", error="")
            self.refresh()
        elif ok:
            QMessageBox.warning(
                self,
                "ID inválido",
                "Escribe solo el ID de 11 caracteres, no la dirección completa.",
            )

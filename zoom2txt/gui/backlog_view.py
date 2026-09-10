"""Read-only local backlog and explicitly configured weekly expectations."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from zoom2txt.blackboard import weeks
from zoom2txt.gui import style
from zoom2txt.workflow import DAILY_LIMIT, WorkLedger


class BacklogView(QWidget):
    search_requested = Signal()
    changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(16)
        self.summary = QLabel()
        self.summary.setObjectName("h2")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.policy = QLabel()
        self.policy.setWordWrap(True)
        self.policy.setObjectName("muted")
        layout.addWidget(self.policy)
        actions = QHBoxLayout()
        plan = QPushButton("Clases esperadas por semana…")
        plan.clicked.connect(self.configure_plan)
        refresh = QPushButton("Buscar en Blackboard")
        refresh.setProperty("variant", "primary")
        refresh.clicked.connect(self.search_requested.emit)
        actions.addWidget(plan)
        actions.addStretch()
        actions.addWidget(refresh)
        layout.addLayout(actions)
        tabs = QTabWidget()
        self.pending_table = self._table(["Curso", "Semana", "Fecha", "Estado"])
        self.weekly_table = self._table(["Curso", "Semana", "En Zoom", "Horario", "Revisión"])
        for col, width in ((1, 76), (2, 88), (3, 88), (4, 208)):
            self.weekly_table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Fixed
            )
            self.weekly_table.setColumnWidth(col, width)
        self.weekly_table.horizontalHeaderItem(2).setToolTip(
            "Grabaciones publicadas encontradas en Zoom"
        )
        self.weekly_table.horizontalHeaderItem(3).setToolTip(
            "Sesiones semanales que configuraste para este curso"
        )
        tabs.addTab(self.pending_table, "Por transcribir")
        tabs.addTab(self.weekly_table, "Revisión semanal")
        layout.addWidget(tabs, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName("muted")
        layout.addWidget(self.note)

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        style.configure_table(table)
        table.setHorizontalHeaderLabels(headers)
        table.setMinimumHeight(220)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setMinimumSectionSize(65)
        table.setColumnWidth(0, 220)
        return table

    @staticmethod
    def _fill(table, rows):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(i, j, item)

    def refresh(self):
        ledger = WorkLedger(self.settings)
        daily = ledger.daily()
        pending = ledger.pending()
        self.summary.setText(
            f"{len(pending)} pendientes guardadas · {daily['used']}/{DAILY_LIMIT} clases iniciadas hoy"
        )
        self.policy.setText(
            f"Puedes iniciar {daily['remaining']} clases nuevas hoy. El contador se renueva el "
            f"{daily['next_day']} a las 00:00 (Lima). Reintentar una clase iniciada hoy no ocupa otro lugar; "
            "las que ya están en YouTube pueden seguir transcribiéndose. Es un límite de la app, no la cuota de Google."
        )
        monday = weeks.parse_start(self.settings.term_start)
        rows = []
        for item in pending:
            job = ledger.store.get("job", item["id"])
            if job.get("video_id"):
                status = "Subida · por completar"
            elif item["id"] in daily["jobs"]:
                status = "Iniciada hoy · reintentable"
            elif daily["remaining"] == 0:
                status = f"En cola · desde {daily['next_day']}"
            else:
                status = "Pendiente"
            week = weeks.week_of_str(monday, item["start_time"]) if monday else None
            rows.append(
                [
                    item["course_name"],
                    f"S{week}" if week else "—",
                    item["start_time"],
                    status,
                ]
            )
        self._fill(self.pending_table, rows)
        rows = []
        for item in ledger.weekly():
            if item["error"]:
                status = "Búsqueda incompleta"
            elif item["expected"] is None:
                status = "Configura tu horario"
            elif item["expected"] == 0:
                status = "Sin comparación"
            elif not item["closed"]:
                status = "Semana en curso"
            elif not item["fresh"]:
                status = "Busca para comprobar"
            elif item["missing"]:
                status = f"{item['missing']} sin aparecer · revisar"
            else:
                status = "Cantidad esperada lista"
            rows.append(
                [
                    item["course"],
                    f"S{item['week']}",
                    item["published"],
                    item["expected"] if item["expected"] is not None else "—",
                    status,
                ]
            )
        self._fill(self.weekly_table, rows)
        if not ledger.courses():
            self.note.setText(
                "Todavía no hay un inventario guardado. Busca clases una vez para descubrir tus cursos y sus grabaciones."
            )
        else:
            self.note.setText(
                "Último inventario guardado, de todo el ciclo: no depende del filtro de semanas. "
                "Buscar actualiza los datos; no descarga nada por sí solo. Solo se compara una semana cerrada "
                "después de revisarla. Feriados, cambios de horario o dos sesiones en una grabación pueden explicar diferencias. "
                "Las omisiones se restauran en Historial."
            )

    def configure_plan(self):
        ledger = WorkLedger(self.settings)
        courses = ledger.courses()
        if not courses:
            self.note.setText("Primero pulsa «Buscar en Blackboard» para identificar tus cursos.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Tu horario semanal")
        dialog.resize(560, 380)
        layout = QVBoxLayout(dialog)
        info = QLabel(
            "Indica las sesiones habituales de cada curso. El 2 es solo una sugerencia: "
            "se aplica al guardar. Usa 0 para no comparar ese curso. No se omite ni se descarga ninguna clase."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        host = QWidget()
        form = QFormLayout(host)
        form.setSpacing(12)
        fields = {}
        for course in courses:
            spin = QSpinBox()
            spin.setRange(0, 14)
            previous = ledger.store.get("weekly_plan", course["id"])
            spin.setValue(
                previous.get("count", 2)
                if previous.get("term_start") == self.settings.term_start
                else 2
            )
            label = QLabel(course["name"])
            label.setWordWrap(True)
            spin.setAccessibleName("Sesiones por semana de " + course["name"])
            form.addRow(label, spin)
            fields[course["id"]] = spin
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Guardar horario")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            for cid, spin in fields.items():
                ledger.set_expected(cid, spin.value())
            self.refresh()
            self.changed.emit()

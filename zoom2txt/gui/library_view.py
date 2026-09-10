"""Pestaña Biblioteca: reconoce en disco las transcripciones y videos
ya generados, muestra estadísticas (tarjetas KPI + gráficos) y una
lista navegable de los .txt."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zoom2txt import library
from zoom2txt.config import Settings
from zoom2txt.gui import style
from zoom2txt.gui.charts import BarChart, StatCard


class LibraryLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings

    def run(self):
        try:
            self.loaded.emit(library.scan_library(self.settings, self.isInterruptionRequested))
        except Exception as exc:
            self.failed.emit(str(exc))


class LibraryView(QWidget):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self._loader = None
        self._stat_cards = []
        self._card_columns = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(16)

        top = QHBoxLayout()
        intro = QLabel("Tus clases ya transcritas, reconocidas desde el disco.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        from zoom2txt.gui import lucide

        refresh = QPushButton("Actualizar")
        refresh.setIcon(lucide.icon("refresh-cw", style.MUTED, 16))
        refresh.clicked.connect(self.refresh)
        open_dir = QPushButton("Abrir carpeta")
        open_dir.clicked.connect(self._open_transcripts)
        top.addWidget(intro, 1)
        top.addWidget(open_dir)
        top.addWidget(refresh)
        lay.addLayout(top)

        self.cards_host = QWidget()
        self.cards_grid = QGridLayout(self.cards_host)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)
        self.cards_grid.setSpacing(12)
        lay.addWidget(self.cards_host)

        charts = QHBoxLayout()
        charts.setSpacing(12)
        self.chart_week = BarChart("Clases por semana", [])
        self.chart_course = BarChart("Clases por curso", [], accent=style.SUCCESS)
        charts.addWidget(self.chart_week, 1)
        charts.addWidget(self.chart_course, 1)

        self.hint = QLabel("")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Clase", "Curso", "Semana", "Palabras aprox.", "Actualizado"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(2, 72)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 175)
        style.configure_table(self.table)
        self.table.setMinimumHeight(style.HEADER_HEIGHT + 6 * style.ROW_HEIGHT + 2)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.doubleClicked.connect(self._open_selected)
        lay.addWidget(self.table, 1)
        lay.addLayout(charts)

    # ── Datos ─────────────────────────────────────────────────────
    def refresh(self) -> None:
        if self._loader and self._loader.isRunning():
            return
        self.hint.setText("Leyendo biblioteca… Puedes seguir usando la aplicación.")
        self._loader = LibraryLoader(self.settings, self)
        self._loader.loaded.connect(self._loaded)
        self._loader.failed.connect(
            lambda msg: self.hint.setText("No se pudo leer la biblioteca: " + msg)
        )
        self._loader.start()

    def _loaded(self, stats):
        self._fill_cards(stats)
        self._fill_charts(stats)
        self._fill_table(stats)
        if stats.txt_count == 0:
            self.hint.setText(
                "Aún no hay transcripciones. Procesa clases en "
                "«Clases» o «Manual» y aparecerán aquí."
            )
        else:
            self.hint.setText(
                "Doble clic para abrir. Texto consolidado sin etiquetas ni repeticiones técnicas."
            )

    def _fill_cards(self, stats: library.LibraryStats) -> None:
        while self.cards_grid.count():
            previous = self.cards_grid.takeAt(0).widget()
            previous.hide()
            previous.deleteLater()
        cards = [
            StatCard(str(stats.txt_count), "Transcripciones", "listas para NotebookLM"),
            StatCard(
                f"{stats.total_words:,}".replace(",", " "),
                "Palabras",
                f"~{stats.avg_words} por clase",
                style.TEXT,
            ),
            StatCard(
                str(len(stats.by_course)),
                "Cursos",
                "con clases guardadas",
                style.TEXT,
            ),
            StatCard(
                str(stats.videos),
                "Videos en disco",
                library.human_size(stats.videos_size),
                style.TEXT,
            ),
        ]
        self._stat_cards = cards
        self._card_columns = 0
        self._reflow_cards()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_cards()

    def _reflow_cards(self) -> None:
        columns = 4 if self.width() >= 800 else 2
        if self._card_columns == columns:
            return
        while self.cards_grid.count():
            self.cards_grid.takeAt(0)
        for column in range(4):
            self.cards_grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, card in enumerate(self._stat_cards):
            self.cards_grid.addWidget(card, index // columns, index % columns)
        self._card_columns = columns

    def _fill_charts(self, stats: library.LibraryStats) -> None:
        week_data = [(f"S{w}", stats.by_week[w]) for w in sorted(stats.by_week)]
        self.chart_week.set_data(week_data)
        # cursos: abreviar etiqueta a algo compacto
        course_data = [
            (self._abbrev(name), n)
            for name, n in sorted(stats.by_course.items(), key=lambda x: -x[1])
        ]
        self.chart_course.set_data(course_data)

    @staticmethod
    def _abbrev(name: str) -> str:
        # 'AprendizDeMaquina' → 'AprDeMaq' (primeras letras de mayúsculas)
        import re

        parts = re.findall(r"[A-Z][a-z]*", name) or [name]
        return "".join(p[:3] for p in parts)[:10]

    def _fill_table(self, stats: library.LibraryStats) -> None:
        self.table.setRowCount(0)
        for it in sorted(stats.items, key=lambda x: x.modified, reverse=True):
            row = self.table.rowCount()
            self.table.insertRow(row)
            name_item = QTableWidgetItem(it.name)
            name_item.setData(Qt.ItemDataRole.UserRole, str(it.path))
            week = f"S{it.week}" if it.week else "—"
            cells = [
                name_item,
                QTableWidgetItem(it.course),
                QTableWidgetItem(week),
                QTableWidgetItem(f"{it.words:,}".replace(",", " ")),
                QTableWidgetItem(it.modified.strftime("%d/%m/%Y %H:%M")),
            ]
            for col, cell in enumerate(cells):
                cell.setToolTip(cell.text())
                if col in (2, 3):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, cell)

    # ── Acciones ──────────────────────────────────────────────────
    def _open_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        path = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_transcripts(self) -> None:
        d = self.settings.transcripts_dir
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

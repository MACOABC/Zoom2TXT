"""Biblioteca: reconoce en disco las transcripciones ya generadas, las resume en
indicadores y gráficos, y permite encontrarlas por nombre, curso o por lo que se
dijo en clase. Cada una se abre en el lector integrado o se lleva al asistente."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zoom2txt import library
from zoom2txt.config import Settings
from zoom2txt.gui import style, widgets
from zoom2txt.gui.charts import BarChart
from zoom2txt.gui.command_palette import _fold, highlight_snippet


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


class _SortItem(QTableWidgetItem):
    """Celda que muestra un texto formateado pero se ordena por su valor real."""

    def __init__(self, text: str, key):
        super().__init__(text)
        self._key = key

    def __lt__(self, other) -> bool:
        if isinstance(other, _SortItem):
            return self._key < other._key
        return super().__lt__(other)


class _HitRow(QWidget):
    def __init__(self, hit: dict):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)
        where = " · ".join(x for x in (hit.get("course_name", ""), hit.get("locator", "")) if x)
        head = widgets.ElidedLabel(hit.get("title", "") + (f"  —  {where}" if where else ""))
        head.setObjectName("h3")
        lay.addWidget(head)
        excerpt = QLabel(highlight_snippet(hit.get("snippet", "")))
        excerpt.setTextFormat(Qt.TextFormat.RichText)
        excerpt.setObjectName("muted")
        excerpt.setWordWrap(True)
        lay.addWidget(excerpt)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)


class LibraryView(QWidget):
    ask_requested = Signal(str)

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self._loader = None
        self._stats: library.LibraryStats | None = None
        self._engine = None
        self._hits: list[dict] = []
        self._card_columns = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.search = widgets.SearchField("Buscar por nombre, curso o por algo dicho en clase…")
        self.search.textChanged.connect(self._schedule_filter)
        self.course_filter = QComboBox()
        self.course_filter.setAccessibleName("Filtrar la biblioteca por curso")
        self.course_filter.setMinimumWidth(170)
        self.course_filter.addItem("Todos los cursos", "")
        self.course_filter.currentIndexChanged.connect(self._apply_filter)
        open_dir = widgets.button("Abrir carpeta", "folder-open", "secondary")
        open_dir.clicked.connect(self._open_transcripts)
        refresh = widgets.icon_button("refresh-cw", "Actualizar la biblioteca")
        refresh.clicked.connect(self.refresh)
        top.addWidget(self.search, 1)
        top.addWidget(self.course_filter)
        top.addWidget(open_dir)
        top.addWidget(refresh)
        lay.addLayout(top)

        self.cards_host = QWidget()
        self.cards_grid = QGridLayout(self.cards_host)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)
        self.cards_grid.setSpacing(12)
        self.card_txt = widgets.KpiCard("file-check", "Transcripciones", "SUCCESS")
        self.card_words = widgets.KpiCard("text-search", "Palabras", "LINK")
        self.card_courses = widgets.KpiCard("graduation-cap", "Cursos", "VIOLET")
        self.card_videos = widgets.KpiCard("hard-drive", "Videos en disco", "INFO")
        self._stat_cards = [self.card_txt, self.card_words, self.card_courses, self.card_videos]
        lay.addWidget(self.cards_host)

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
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 130)
        self.table.setColumnWidth(4, 160)
        style.configure_table(self.table)
        self.table.setMinimumHeight(style.HEADER_HEIGHT + 6 * style.ROW_HEIGHT + 2)
        self.table.setShowGrid(False)
        self.table.setMouseTracking(True)
        self.table.setSortingEnabled(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(self._open_selected)
        lay.addWidget(self.table, 3)

        self.empty = widgets.EmptyState(
            "library",
            "Tu biblioteca está vacía",
            "Procesa clases en «Clases» o «Manual» y sus transcripciones aparecerán aquí, "
            "listas para leer, buscar y consultar con el asistente.",
        )
        self.empty.setObjectName("card")
        self.empty.hide()
        lay.addWidget(self.empty, 3)

        self.hits_title = QLabel("Dentro del contenido")
        self.hits_title.setObjectName("eyebrow")
        self.hits_title.hide()
        lay.addWidget(self.hits_title)
        self.hits_list = QListWidget()
        self.hits_list.setObjectName("palette")
        self.hits_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hits_list.setMinimumHeight(150)
        self.hits_list.itemActivated.connect(self._open_hit)
        self.hits_list.itemClicked.connect(self._open_hit)
        self.hits_list.hide()
        lay.addWidget(self.hits_list, 2)

        self.charts_host = QWidget()
        charts = QHBoxLayout(self.charts_host)
        charts.setContentsMargins(0, 0, 0, 0)
        charts.setSpacing(12)
        self.chart_week = BarChart("Clases por semana", [])
        self.chart_course = BarChart("Clases por curso", [], accent="VIOLET")
        charts.addWidget(self.chart_week, 1)
        charts.addWidget(self.chart_course, 1)
        lay.addWidget(self.charts_host)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(180)
        self._filter_timer.timeout.connect(self._apply_filter)
        self._reflow_cards()

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
        self._stats = stats
        self._fill_cards(stats)
        self._fill_charts(stats)
        self._fill_courses(stats)
        self._fill_table(stats)
        self._apply_filter()

    def _fill_cards(self, stats: library.LibraryStats) -> None:
        self.card_txt.set_value(str(stats.txt_count), "listas para NotebookLM")
        self.card_words.set_value(
            f"{stats.total_words:,}".replace(",", " "), f"~{stats.avg_words} por clase"
        )
        self.card_courses.set_value(str(len(stats.by_course)), "con clases guardadas")
        self.card_videos.set_value(str(stats.videos), library.human_size(stats.videos_size))

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
        course_data = [
            (self._abbrev(name), n)
            for name, n in sorted(stats.by_course.items(), key=lambda x: -x[1])
        ]
        self.chart_course.set_data(course_data)

    def _fill_courses(self, stats: library.LibraryStats) -> None:
        previous = self.course_filter.currentData()
        self.course_filter.blockSignals(True)
        self.course_filter.clear()
        self.course_filter.addItem("Todos los cursos", "")
        for name in sorted(stats.by_course, key=str.casefold):
            self.course_filter.addItem(f"{name} ({stats.by_course[name]})", name)
        index = self.course_filter.findData(previous)
        self.course_filter.setCurrentIndex(max(0, index))
        self.course_filter.blockSignals(False)

    @staticmethod
    def _abbrev(name: str) -> str:
        # 'AprendizDeMaquina' → 'AprDeMaq' (primeras letras de mayúsculas)
        import re

        parts = re.findall(r"[A-Z][a-z]*", name) or [name]
        return "".join(p[:3] for p in parts)[:10]

    def _fill_table(self, stats: library.LibraryStats) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for it in sorted(stats.items, key=lambda x: x.modified, reverse=True):
            row = self.table.rowCount()
            self.table.insertRow(row)
            name_item = QTableWidgetItem(it.name)
            name_item.setData(Qt.ItemDataRole.UserRole, str(it.path))
            week_item = _SortItem(f"S{it.week}" if it.week else "—", it.week or 0)
            words_item = _SortItem(f"{it.words:,}".replace(",", " "), int(it.words))
            date_item = _SortItem(it.modified.strftime("%d/%m/%Y %H:%M"), it.modified.timestamp())
            cells = [name_item, QTableWidgetItem(it.course), week_item, words_item, date_item]
            for col, cell in enumerate(cells):
                cell.setToolTip(str(cell.text()))
                if col in (2, 3):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, cell)
        self.table.setSortingEnabled(True)

    # ── Filtro y búsqueda de contenido ────────────────────────────
    def _schedule_filter(self) -> None:
        self._filter_timer.start()

    def _apply_filter(self) -> None:
        query = _fold(self.search.text()).strip()
        course = self.course_filter.currentData() or ""
        visible = 0
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0)
            course_cell = self.table.item(row, 1)
            haystack = _fold(f"{name.text()} {course_cell.text()}")
            match = all(word in haystack for word in query.split()) if query else True
            if course and course_cell.text() != course:
                match = False
            self.table.setRowHidden(row, not match)
            visible += match
        total = self.table.rowCount()
        self._search_content(self.search.text().strip())
        has_any = total > 0
        self.table.setVisible(has_any)
        self.empty.setVisible(not has_any)
        self.charts_host.setVisible(has_any and not query)
        self.cards_host.setVisible(not query or has_any)
        if not has_any:
            self.hint.setText("")
        elif query or course:
            extra = f" · {len(self._hits)} coincidencias dentro del contenido" if self._hits else ""
            self.hint.setText(f"{visible} de {total} transcripciones coinciden por nombre{extra}.")
        else:
            self.hint.setText(
                "Doble clic para leer. Clic derecho para más acciones. "
                "Texto consolidado sin etiquetas ni repeticiones técnicas."
            )

    def _search_content(self, query: str) -> None:
        self._hits = []
        self.hits_list.clear()
        if len(query) >= 3:
            try:
                if self._engine is None:
                    from zoom2txt.rag_engine import RAGEngine

                    self._engine = RAGEngine(self.settings)
                self._hits = self._engine.search_text(query, limit=20)
            except Exception:  # noqa: BLE001 — sin índice todavía: solo filtra por nombre
                self._hits = []
        for hit in self._hits:
            item = QListWidgetItem(self.hits_list)
            row = _HitRow(hit)
            item.setSizeHint(QSize(0, max(56, row.sizeHint().height())))
            self.hits_list.setItemWidget(item, row)
        self.hits_title.setVisible(bool(self._hits))
        self.hits_list.setVisible(bool(self._hits))

    # ── Acciones ──────────────────────────────────────────────────
    def _selected_path(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _open_reader(self, path: str, title: str = "", query: str = "", source_id=None) -> None:
        if not path:
            return
        from zoom2txt.gui.reader_dialog import ReaderDialog

        dialog = ReaderDialog(
            self.settings, Path(path), title=title, parent=self.window(), query=query,
            engine=self._engine, source_id=source_id,
        )  # fmt: skip
        dialog.ask_requested.connect(self.ask_requested.emit)
        dialog.exec()

    def _open_selected(self) -> None:
        path = self._selected_path()
        if path and Path(path).exists():
            self._open_reader(path, query=self.search.text().strip())

    def _open_hit(self, item: QListWidgetItem) -> None:
        row = self.hits_list.row(item)
        if 0 <= row < len(self._hits):
            hit = self._hits[row]
            self._open_reader(hit["path"], hit["title"], self.search.text().strip(), hit["source_id"])

    def _context_menu(self, position) -> None:
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        self.table.selectRow(index.row())
        path = self._selected_path()
        if not path:
            return
        menu = QMenu(self)
        menu.addAction("Leer en Zoom2TXT", self._open_selected)
        menu.addAction("Preguntar al asistente sobre esta clase", lambda: self.ask_requested.emit(path))
        menu.addSeparator()
        menu.addAction("Copiar el texto completo", lambda: self._copy_text(path))
        menu.addAction("Abrir con el programa habitual", lambda: self._open_external(path))
        menu.addAction("Mostrar en su carpeta", lambda: self._open_external(str(Path(path).parent)))
        menu.exec(self.table.viewport().mapToGlobal(position))

    @staticmethod
    def _copy_text(path: str) -> None:
        try:
            QApplication.clipboard().setText(
                Path(path).read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            pass

    @staticmethod
    def _open_external(path: str) -> None:
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_transcripts(self) -> None:
        d = self.settings.transcripts_dir
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

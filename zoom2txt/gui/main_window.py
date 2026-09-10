"""Ventana principal de Zoom2TXT: modo Automático (busca grabaciones
nuevas en Blackboard) y modo Manual (URL de Zoom pegada a mano)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QDate, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zoom2txt import __version__
from zoom2txt.auth.credentials import get_zoom_password
from zoom2txt.blackboard import weeks as wk
from zoom2txt.config import Settings, credentials_status
from zoom2txt.core.pipeline import STAGES
from zoom2txt.gui import style
from zoom2txt.gui.settings_dialog import SettingsDialog
from zoom2txt.gui.setup_wizard import SetupWizard
from zoom2txt.gui.workers import (
    DeleteVideoWorker,
    MaterialDownloadWorker,
    MaterialScanWorker,
    PipelineWorker,
    QueueWorker,
    RecoveryWorker,
    ScanWorker,
)

QtSizePolicy = QSizePolicy.Policy


class StageRow(QWidget):
    """Fila de un paso del pipeline: símbolo de estado + nombre."""

    _GLYPHS: ClassVar = {
        "pending": "○",
        "active": "●",
        "done": "✓",
        "error": "✕",
    }

    def __init__(self, text: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 3, 0, 3)
        self.dot = QLabel("○")
        self.dot.setFixedWidth(18)
        self.label = QLabel(text)
        row.addWidget(self.dot)
        row.addSpacing(6)
        row.addWidget(self.label)
        row.addStretch()
        self.set_state("pending")

    def set_state(self, state: str) -> None:
        colors = {
            "pending": style.BORDER_HI,
            "active": style.LINK,
            "done": style.SUCCESS,
            "error": style.ERROR,
        }
        self.dot.setText(self._GLYPHS[state])
        self.dot.setStyleSheet(f"color: {colors[state]}; font-size: 15px; font-weight: 700;")
        self.label.setStyleSheet(
            f"color: {style.TEXT if state in ('active', 'done') else style.MUTED};"
            f"font-weight: {600 if state == 'active' else 400};"
        )


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.worker: PipelineWorker | None = None
        self.scan_worker: ScanWorker | None = None
        self.queue_worker: QueueWorker | None = None
        self.mat_scan_worker: MaterialScanWorker | None = None
        self.mat_dl_worker: MaterialDownloadWorker | None = None
        self._material_missing: list = []
        self._scan_items: list[dict] = []
        self._pending_backlog = 0
        self._current_stage = -1
        self._result_path: Path | None = None
        self.setWindowTitle("Zoom2TXT")
        self.setMinimumSize(880, 560)
        self.resize(1180, 780)
        self.recovery_worker = None
        self._closing = False
        from zoom2txt.gui.icon import make_app_icon

        self.setWindowIcon(make_app_icon())
        self._build_ui()
        self._setup_tray()
        self._queue_total = 0
        self._backlog_timer = QTimer(self)
        self._backlog_timer.setInterval(60_000)
        self._backlog_timer.timeout.connect(self._refresh_backlog_notice)
        self._backlog_timer.start()
        QTimer.singleShot(0, self._refresh_backlog_notice)
        QTimer.singleShot(0, self._load_saved_work)

    def _setup_tray(self) -> None:
        """Bandeja del sistema: permite avisar cuando una transcripción
        termina aunque la ventana esté minimizada."""
        from PySide6.QtWidgets import QSystemTrayIcon

        from zoom2txt.gui.icon import make_app_icon

        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip("Zoom2TXT")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _notify(self, title: str, message: str) -> None:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        QApplication.alert(self, 0)  # parpadeo en la barra de tareas
        if getattr(self, "tray", None) and self.tray.isVisible():
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 8000)

    # ── UI ────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        from PySide6.QtWidgets import QButtonGroup

        from zoom2txt.gui import lucide
        from zoom2txt.gui.adaptive_stack import AdaptiveStack
        from zoom2txt.gui.library_view import LibraryView

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # ── Sidebar (estilo Linear) ───────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 20, 16, 16)
        side.setSpacing(4)

        title = QLabel(f'Zoom<span style="color:{style.LINK};">2TXT</span>')
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setObjectName("h1")
        subtitle = QLabel("Tu biblioteca de clases")
        subtitle.setObjectName("faint")
        side.addWidget(title)
        side.addWidget(subtitle)
        side.addSpacing(24)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        nav_items = [
            ("search", "Clases"),
            ("pen-line", "Manual"),
            ("folder-down", "Material"),
            ("book-open", "Biblioteca"),
            ("calendar", "Pendientes"),
            ("activity", "Estado"),
        ]
        self._nav_buttons: list[QPushButton] = []
        for i, (icon_name, label) in enumerate(nav_items):
            b = QPushButton(f"  {label}")
            b.setProperty("variant", "nav")
            b.setIcon(lucide.icon(icon_name, style.MUTED, 18))
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self._navigate(idx))
            self._nav_group.addButton(b, i)
            side.addWidget(b)
            self._nav_buttons.append(b)
        self.history_btn = QPushButton("  Historial")
        self.history_btn.setToolTip("Omisiones y recuperación de trabajos")
        self.history_btn.setProperty("variant", "nav")
        self.history_btn.setIcon(lucide.icon("refresh-cw", style.MUTED, 18))
        self.history_btn.clicked.connect(self._open_history)
        side.addWidget(self.history_btn)
        side.addStretch()

        self.settings_btn = QPushButton("  Ajustes")
        self.settings_btn.setProperty("variant", "nav")
        self.settings_btn.setIcon(lucide.icon("settings", style.MUTED, 18))
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(self._open_settings)
        side.addWidget(self.settings_btn)
        version = QLabel(f"v{__version__} · credenciales solo en esta PC")
        version.setObjectName("faint")
        version.setWordWrap(True)
        side.addWidget(version)
        shell.addWidget(sidebar)

        # ── Contenido ─────────────────────────────────────────────
        content = QWidget()
        outer = QVBoxLayout(content)
        self._content_layout = outer
        outer.setContentsMargins(32, 24, 32, 20)
        outer.setSpacing(16)
        from PySide6.QtWidgets import QScrollArea

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        shell.addWidget(scroll, 1)
        self.page_title = QLabel("Tus clases")
        self.page_title.setObjectName("h1")
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(self.page_title)
        self.page_subtitle = QLabel("De la grabación al texto, con cada paso bajo control.")
        self.page_subtitle.setObjectName("muted")
        self.page_subtitle.setWordWrap(True)
        heading.addWidget(self.page_subtitle)
        outer.addLayout(heading)

        card = QFrame()
        card.setObjectName("workspace")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)

        self.stack = AdaptiveStack()
        self.stack.addWidget(self._build_auto_tab())
        self.stack.addWidget(self._build_manual_tab())
        self.stack.addWidget(self._build_material_tab())
        self.library = LibraryView(self.settings)
        self.stack.addWidget(self.library)
        from zoom2txt.gui.backlog_view import BacklogView

        self.backlog = BacklogView(self.settings)
        self.backlog.search_requested.connect(self._search_backlog)
        self.backlog.changed.connect(self._refresh_backlog_notice)
        self.stack.addWidget(self.backlog)
        from zoom2txt.gui.health_view import HealthView

        self.health = HealthView(self.settings)
        self.health.settings_requested.connect(self._open_settings)
        self.stack.addWidget(self.health)
        card_lay.addWidget(self.stack)
        outer.addWidget(card)

        # Cuerpo: pasos + consola (se oculta en la pestaña Biblioteca)
        self.body_widget = QWidget()
        body = QHBoxLayout(self.body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(16)

        stages_card = QFrame()
        stages_card.setObjectName("card")
        stages_lay = QVBoxLayout(stages_card)
        stages_lay.setContentsMargins(16, 12, 16, 12)
        stages_title = QLabel("Progreso")
        stages_title.setObjectName("h2")
        stages_lay.addWidget(stages_title)
        self.queue_label = QLabel("")
        self.queue_label.setObjectName("accent")
        self.queue_label.setWordWrap(True)
        stages_lay.addWidget(self.queue_label)
        self.recovery_cancel_btn = QPushButton("Pausar recuperación")
        self.recovery_cancel_btn.clicked.connect(self._cancel_recovery)
        self.recovery_cancel_btn.hide()
        stages_lay.addWidget(self.recovery_cancel_btn)
        stages_lay.addSpacing(6)
        self.stage_rows = [StageRow(s) for s in STAGES]
        for r in self.stage_rows:
            stages_lay.addWidget(r)
        stages_lay.addStretch()
        self.open_btn = QPushButton("Abrir transcripciones")
        self.open_btn.hide()
        self.open_btn.clicked.connect(self._open_result)
        stages_lay.addWidget(self.open_btn)
        stages_card.setFixedWidth(230)

        console_box = QVBoxLayout()
        console_title = QLabel("Actividad")
        console_title.setObjectName("h2")
        self.console = QPlainTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(1500)
        self.console.setMinimumHeight(144)
        console_box.addWidget(console_title)
        console_box.addWidget(self.console, 1)

        body.addWidget(stages_card)
        body.addLayout(console_box, 1)
        outer.addWidget(self.body_widget, 1)
        self._card = card

        self._nav_buttons[0].setChecked(True)
        self._navigate(0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_content_layout"):
            compact = self.width() < 1120
            inset = 16 if compact else 32
            self._content_layout.setContentsMargins(inset, 16 if compact else 24, inset, 20)
            if hasattr(self, "table"):
                row_height = 32 if compact else style.ROW_HEIGHT
                self.table.verticalHeader().setMinimumSectionSize(row_height)
                self.table.verticalHeader().setDefaultSectionSize(row_height)
                self.table.setFixedHeight(style.HEADER_HEIGHT + 6 * row_height + 2)

    def _build_auto_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(12)

        top = QHBoxLayout()
        from zoom2txt.gui import lucide

        self.scan_btn = QPushButton("Buscar clases nuevas")
        self.scan_btn.setIcon(lucide.icon("search", style.MUTED, 16))
        self.scan_btn.clicked.connect(self._scan)
        self.scan_status = QLabel(
            "Busca en tu Blackboard las grabaciones que aún no has transcrito."
        )
        self.scan_status.setObjectName("muted")
        self.scan_status.setWordWrap(True)
        top.addWidget(self.scan_btn)
        top.addSpacing(12)
        top.addWidget(self.scan_status, 1)
        lay.addLayout(top)

        notice_row = QHBoxLayout()
        self.backlog_notice = QLabel("Busca una vez para guardar el inventario de tus cursos.")
        self.backlog_notice.setObjectName("muted")
        self.backlog_notice.setWordWrap(True)
        backlog_btn = QPushButton("Ver pendientes")
        backlog_btn.clicked.connect(lambda: self._navigate(4))
        notice_row.addWidget(self.backlog_notice, 1)
        notice_row.addWidget(backlog_btn)
        lay.addLayout(notice_row)

        # ── Filtro por semanas académicas ─────────────────────────
        self.weeks_box = QFrame()
        self.weeks_box.setObjectName("card")
        wlay = QVBoxLayout(self.weeks_box)
        wlay.setContentsMargins(16, 12, 16, 12)
        wtop = QHBoxLayout()
        self.weeks_chk = QCheckBox("Filtrar por semana del ciclo")
        self.weeks_chk.toggled.connect(self._on_weeks_toggled)
        self.weeks_hint = QLabel("")
        self.weeks_hint.setObjectName("muted")
        self.weeks_hint.setWordWrap(True)
        cfg_btn = QPushButton("Configurar inicio de clases…")
        cfg_btn.clicked.connect(self._configure_term_start)
        wtop.addWidget(self.weeks_chk)
        wtop.addSpacing(8)
        wtop.addStretch()
        wtop.addWidget(cfg_btn)
        wlay.addLayout(wtop)
        wlay.addWidget(self.weeks_hint)

        self.weeks_grid_host = QWidget()
        self.weeks_grid = QGridLayout(self.weeks_grid_host)
        self.weeks_grid.setContentsMargins(0, 8, 0, 0)
        self.weeks_grid.setSpacing(8)
        self.week_buttons: list[QPushButton] = []
        for i in range(wk.WEEKS_IN_TERM):
            b = QPushButton(f"S{i + 1}")
            b.setCheckable(True)
            b.setProperty("variant", "week")
            b.setFixedHeight(30)
            self.weeks_grid.addWidget(b, i // 8, i % 8)
            self.week_buttons.append(b)
        wlay.addWidget(self.weeks_grid_host)
        self.weeks_grid_host.hide()
        lay.addWidget(self.weeks_box)
        self._refresh_week_labels()

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["", "Curso", "Clase (editable)", "Sem.", "Fecha", "Estado"]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 44)
        self.table.setColumnWidth(3, 62)
        self.table.setColumnWidth(4, 110)
        self.table.setColumnWidth(5, 154)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        style.configure_table(self.table)
        self.table.setFixedHeight(style.HEADER_HEIGHT + 6 * style.ROW_HEIGHT + 2)
        self.table.hide()
        lay.addWidget(self.table)
        self.class_detail = QLabel("Selecciona una fila para ver el detalle de su estado.")
        self.class_detail.setObjectName("muted")
        self.class_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.class_detail.setWordWrap(True)
        self.class_detail.hide()
        self.table.cellClicked.connect(self._show_class_detail)
        lay.addWidget(self.class_detail)

        bottom = QHBoxLayout()
        self.delete_chk = QCheckBox("Eliminar cada video de YouTube al obtener su texto")
        self.delete_chk.setChecked(self.settings.auto_delete_youtube)
        self.skip_all_btn = QPushButton("Omitir todo lo pendiente")
        self.skip_all_btn.setToolTip(
            "Marca TODAS las grabaciones pendientes como ya procesadas sin "
            "transcribirlas. Útil si empiezas a usar la app a mitad de ciclo "
            "y las clases viejas ya las tienes."
        )
        self.skip_all_btn.clicked.connect(self._skip_all_pending)
        self.skip_all_btn.hide()
        from zoom2txt.gui import lucide

        self.process_btn = QPushButton("Procesar / continuar")
        self.process_btn.setToolTip(
            "Comprueba YouTube y continúa las clases marcadas desde el último paso guardado."
        )
        self.process_btn.setIcon(lucide.icon("play", style.ON_ACCENT, 16))
        self.process_btn.setProperty("variant", "primary")
        self.process_btn.clicked.connect(self._process_queue)
        self.process_btn.hide()
        self.cancel_queue_btn = QPushButton("Cancelar")
        self.cancel_queue_btn.setProperty("variant", "danger")
        self.cancel_queue_btn.clicked.connect(self._cancel_queue)
        self.cancel_queue_btn.hide()
        lay.addWidget(self.delete_chk)
        bottom.addWidget(self.skip_all_btn)
        bottom.addStretch()
        bottom.addWidget(self.process_btn)
        bottom.addWidget(self.cancel_queue_btn)
        lay.addLayout(bottom)
        return tab

    def _build_material_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(12)

        top = QHBoxLayout()
        from zoom2txt.gui import lucide

        self.mat_scan_btn = QPushButton("Buscar material")
        self.mat_scan_btn.setIcon(lucide.icon("folder-down", style.MUTED, 16))
        self.mat_scan_btn.clicked.connect(self._scan_material)
        self.mat_status = QLabel(
            "Descarga a tu carpeta todo el material "
            "(PDFs, Words, PPTs…) de todos tus cursos, "
            "y detecta lo que te falta."
        )
        self.mat_status.setObjectName("muted")
        self.mat_status.setWordWrap(True)
        open_btn = QPushButton("Abrir carpeta")
        open_btn.clicked.connect(self._open_materials_dir)
        top.addWidget(self.mat_scan_btn)
        top.addSpacing(12)
        top.addWidget(self.mat_status, 1)
        top.addWidget(open_btn)
        lay.addLayout(top)

        self.mat_table = QTableWidget(0, 5)
        self.mat_table.setHorizontalHeaderLabels(
            ["Curso", "Archivos", "Locales", "Faltan", "Revisión"]
        )
        self.mat_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            self.mat_table.setColumnWidth(col, 88)
        self.mat_table.setColumnWidth(4, 180)
        self.mat_table.verticalHeader().setVisible(False)
        self.mat_table.setAlternatingRowColors(True)
        self.mat_table.setShowGrid(False)
        self.mat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mat_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        style.configure_table(self.mat_table)
        self.mat_table.setFixedHeight(style.HEADER_HEIGHT + 5 * style.ROW_HEIGHT + 2)
        self.mat_table.hide()
        lay.addWidget(self.mat_table)

        bottom = QHBoxLayout()
        self.mat_dl_btn = QPushButton("Descargar lo que falta")
        self.mat_dl_btn.setIcon(lucide.icon("download", style.ON_ACCENT, 16))
        self.mat_dl_btn.setProperty("variant", "primary")
        self.mat_dl_btn.clicked.connect(self._download_material)
        self.mat_dl_btn.hide()
        self.mat_cancel_btn = QPushButton("Cancelar")
        self.mat_cancel_btn.setProperty("variant", "danger")
        self.mat_cancel_btn.clicked.connect(self._cancel_material)
        self.mat_cancel_btn.hide()
        bottom.addStretch()
        bottom.addWidget(self.mat_dl_btn)
        bottom.addWidget(self.mat_cancel_btn)
        lay.addLayout(bottom)
        lay.addStretch()
        return tab

    def _build_manual_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        lbl_url = QLabel("URL de la grabación de Zoom")
        lbl_url.setObjectName("h2")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://us02web.zoom.us/rec/share/…")
        lbl_name = QLabel("Nombre de la clase")
        lbl_name.setObjectName("h2")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Ej: Matematica_Clase01")
        lbl_url.setBuddy(self.url_edit)
        lbl_name.setBuddy(self.name_edit)

        actions = QHBoxLayout()
        from zoom2txt.gui import lucide

        self.start_btn = QPushButton("Procesar clase")
        self.start_btn.setIcon(lucide.icon("play", style.ON_ACCENT, 16))
        self.start_btn.setProperty("variant", "primary")
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn = QPushButton("Cancelar")
        self.cancel_btn.setProperty("variant", "danger")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.hide()
        actions.addWidget(self.start_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch()

        lay.addWidget(lbl_url)
        lay.addWidget(self.url_edit)
        lay.addSpacing(16)
        lay.addWidget(lbl_name)
        lay.addWidget(self.name_edit)
        lay.addSpacing(16)
        lay.addLayout(actions)
        lay.addStretch()
        return tab

    def _navigate(self, index: int) -> None:
        self._nav_buttons[index].setChecked(True)
        self.page_title.setText(
            [
                "Tus clases",
                "Transcripción manual",
                "Material de cursos",
                "Tu biblioteca",
                "Lo que falta",
                "Estado de la app",
            ][index]
        )
        self.page_subtitle.setText(
            [
                "Selecciona las semanas y revisa qué grabaciones faltan.",
                "Procesa un enlace de Zoom con recuperación si algo se interrumpe.",
                "Adjuntos verificados. Los recursos no exportables se indican por separado.",
                "Tus transcripciones en disco, organizadas para volver a ellas.",
                "Tu cola continúa mañana. Revisa también las semanas que necesitan atención.",
                "Comprueba el entorno antes de procesar y guarda un informe seguro si necesitas ayuda.",
            ][index]
        )
        self.stack.setCurrentIndex(index)
        self.stack.updateGeometry()
        if self.isVisible():
            from PySide6.QtCore import QEasingCurve, QPropertyAnimation
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            if getattr(self, "_page_fade", None):
                self._page_fade.stop()
            effect = QGraphicsOpacityEffect(self.stack)
            self.stack.setGraphicsEffect(effect)
            self._page_fade = QPropertyAnimation(effect, b"opacity", self)
            self._page_fade.setDuration(180)
            self._page_fade.setStartValue(0.75)
            self._page_fade.setEndValue(1.0)
            self._page_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._page_fade.start()
        is_library = self.stack.widget(index) is self.library
        is_backlog = self.stack.widget(index) is self.backlog
        is_health = self.stack.widget(index) is self.health
        # Las vistas de consulta ocupan toda la altura; el trabajo muestra pasos+consola.
        self.body_widget.setVisible(not (is_library or is_backlog or is_health))
        self._card.setSizePolicy(
            self._card.sizePolicy().horizontalPolicy(),
            QtSizePolicy.Expanding
            if is_library or is_backlog or is_health
            else QtSizePolicy.Preferred,
        )
        if is_library:
            self.library.refresh()
        if is_backlog:
            self.backlog.refresh()
        if is_health:
            self.health.refresh()

    def _search_backlog(self):
        if self._busy():
            return
        self._navigate(0)
        self.weeks_chk.setChecked(False)
        self._scan()

    def _refresh_backlog_notice(self):
        if self._closing:
            return
        from zoom2txt.workflow import DAILY_LIMIT, WorkLedger

        try:
            ledger = WorkLedger(self.settings)
            daily = ledger.daily()
            pending = len(ledger.pending())
            if not ledger.courses():
                text = f"Hoy: {daily['used']}/{DAILY_LIMIT} clases iniciadas. Busca para guardar el inventario."
            else:
                text = f"{pending} pendientes guardadas · hoy {daily['used']}/{DAILY_LIMIT} iniciadas. "
                text += (
                    f"Nuevas clases desde {daily['next_day']} a las 00:00 (Lima)."
                    if daily["remaining"] == 0
                    else f"Puedes iniciar {daily['remaining']} nuevas hoy."
                )
                missing = sum(r["missing"] or 0 for r in ledger.weekly())
                if missing:
                    text += f" {missing} sesiones esperadas sin aparecer: revisa Pendientes."
                if any(c.get("error") for c in ledger.courses()):
                    text += " Inventario parcial: vuelve a buscar."
            self.backlog_notice.setText(text)
            if self.stack.currentWidget() is self.backlog:
                self.backlog.refresh()
        except Exception:  # noqa: BLE001 — resilient UI read boundary
            self.backlog_notice.setText(
                "No se pudo leer el inventario local. Vuelve a buscar; no se han borrado tus clases."
            )

    # ── Semanas académicas ────────────────────────────────────────
    def _term_monday(self):
        return wk.parse_start(self.settings.term_start)

    def _refresh_week_labels(self) -> None:
        monday = self._term_monday()
        if not monday:
            self.weeks_chk.setEnabled(False)
            self.weeks_hint.setText(
                "Configura la fecha de inicio de clases para filtrar por semana."
            )
            return
        self.weeks_chk.setEnabled(True)
        actual = wk.current_week(monday)
        self.weeks_hint.setText(
            f"Semana actual: {actual}"
            if actual
            else "El ciclo aún no empieza o ya terminó, según la fecha configurada."
        )
        for i, b in enumerate(self.week_buttons, start=1):
            b.setToolTip(wk.week_label(monday, i))
            b.setText(f"S{i}" + ("  •" if i == actual else ""))

    def _on_weeks_toggled(self, on: bool) -> None:
        self.weeks_grid_host.setVisible(on)

    def _configure_term_start(self) -> None:
        from PySide6.QtWidgets import QDateEdit, QDialog, QDialogButtonBox

        dlg = QDialog(self)
        dlg.setWindowTitle("Inicio de clases")
        v = QVBoxLayout(dlg)
        lbl = QLabel("¿Qué día fue el primer día de la Semana 1 de este ciclo?")
        lbl.setWordWrap(True)
        de = QDateEdit()
        de.setCalendarPopup(True)
        de.setDisplayFormat("dddd dd/MM/yyyy")
        monday = self._term_monday()
        de.setDate(QDate(monday.year, monday.month, monday.day) if monday else QDate.currentDate())
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(lbl)
        v.addWidget(de)
        v.addWidget(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings.term_start = de.date().toString("yyyy-MM-dd")
            self.settings.save()
            self._refresh_week_labels()

    def _selected_week_ranges(self) -> list:
        monday = self._term_monday()
        if not self.weeks_chk.isChecked() or not monday:
            return []
        return [
            wk.week_range_str(monday, i)
            for i, b in enumerate(self.week_buttons, start=1)
            if b.isChecked()
        ]

    def _week_of(self, sort_key: str) -> str:
        monday = self._term_monday()
        if not monday or not sort_key:
            return ""
        from datetime import datetime

        try:
            d = datetime.strptime(sort_key[:10], "%Y-%m-%d").date()  # noqa: DTZ007
        except ValueError:
            return ""
        w = wk.current_week(monday, d)
        return f"S{w}" if w else ""

    # ── Utilidades comunes ────────────────────────────────────────
    def _log(self, text: str) -> None:
        """Consola con color según el tipo de mensaje."""
        import html as _html

        color = None
        from zoom2txt.security import redact

        text = redact(text)
        stripped = text.lstrip()
        if stripped.startswith("[+]"):
            color = style.SUCCESS
        elif stripped.startswith(("[✕]", "[!]")):
            color = style.ERROR
        elif stripped.startswith(("[i]", "══", "====")):
            color = style.LINK
        escaped = _html.escape(text).replace(" ", "&nbsp;")
        if color:
            self.console.appendHtml(f'<span style="color:{color};">{escaped}</span>')
        else:
            self.console.appendHtml(escaped or "&nbsp;")

    def _open_settings(self) -> None:
        if self._busy():
            QMessageBox.information(
                self,
                "Trabajo en curso",
                "Espera o pausa el trabajo antes de cambiar ajustes.",
            )
            return
        if SettingsDialog(self.settings, self).exec() == SettingsDialog.DialogCode.Accepted:
            from PySide6.QtWidgets import QApplication

            style.apply_theme(QApplication.instance(), self.settings.theme)
            self._scan_items = []
            self._build_ui()
            self._refresh_backlog_notice()
            self._load_saved_work()

    def _open_history(self):
        if self._busy():
            QMessageBox.information(
                self,
                "Trabajo en curso",
                "Espera o pausa la tarea antes de recuperar otras clases.",
            )
            return
        from zoom2txt.gui.history_dialog import HistoryDialog

        dialog = HistoryDialog(self.settings, self)
        dialog.resume.connect(self._recover)
        dialog.exec()
        self._refresh_backlog_notice()

    def _recover(self, job):
        if self._busy():
            QMessageBox.information(
                self,
                "Trabajo en curso",
                "Ya hay una tarea activa. Espera o pulsa Pausar recuperación antes de continuar otra clase.",
            )
            return
        self._navigate(0)
        self._reset_stages()
        self.recovery_worker = RecoveryWorker(self.settings, job, self)
        self._set_recovering(True)
        self.scan_status.setText(
            "Recuperación en curso. Consulta Progreso y Actividad; puedes pausarla."
        )
        self.recovery_worker.log.connect(self._log)
        self.recovery_worker.stage.connect(self._on_stage)
        self.recovery_worker.result.connect(self._recovery_result)
        self.recovery_worker.finished.connect(lambda: self._set_recovering(False))
        self.recovery_worker.finished.connect(self._refresh_backlog_notice)
        self.recovery_worker.start()

    def _set_recovering(self, active):
        for button in (
            self.settings_btn,
            self.scan_btn,
            self.mat_scan_btn,
            self.process_btn,
            self.start_btn,
            self.mat_dl_btn,
        ):
            button.setEnabled(not active)
        self.recovery_cancel_btn.setVisible(active)
        self.recovery_cancel_btn.setEnabled(active)

    def _cancel_recovery(self):
        if self.recovery_worker and self.recovery_worker.isRunning():
            self.recovery_worker.cancel()
            self.recovery_cancel_btn.setEnabled(False)
            self.scan_status.setText("Pausando recuperación; se conserva el progreso confirmado.")

    def _recovery_result(self, message):
        self._log("[i] " + message)
        self.scan_status.setText(message)
        if message == "Transcripción recuperada.":
            for row in self.stage_rows:
                row.set_state("done")
            self._notify("Transcripción recuperada", "El TXT está disponible en Biblioteca.")
        elif self._current_stage >= 0:
            self.stage_rows[self._current_stage].set_state(
                "error" if message.startswith("No se pudo continuar") else "pending"
            )

    def _ensure_credentials(self) -> str | None:
        """Devuelve la contraseña de Zoom, reabriendo el wizard si falta algo."""
        missing = credentials_status(self.settings)
        if missing:
            QMessageBox.information(
                self,
                "Falta configuración",
                "Falta completar tu configuración (credenciales de Zoom o de "
                "YouTube). Se abrirá el asistente para completarla.",
            )
            wizard = SetupWizard(self.settings, self)
            if wizard.exec() != wizard.DialogCode.Accepted:
                return None
        return get_zoom_password(self.settings.zoom_user)

    def _ensure_pipeline_health(self) -> bool:
        """Detiene fallos locales previsibles antes de bajar un video grande."""
        from zoom2txt.health import pipeline_blockers

        blockers = pipeline_blockers(self.settings)
        if not blockers:
            return True
        details = "\n".join(f"• {result.title}: {result.summary}" for result in blockers)
        QMessageBox.warning(
            self,
            "Zoom2TXT necesita atención",
            "No se inició ninguna descarga. Corrige estos puntos y vuelve a intentar:\n\n"
            + details,
        )
        self._navigate(5)
        return False

    def _reset_stages(self) -> None:
        for r in self.stage_rows:
            r.set_state("pending")
        self._current_stage = -1

    def _busy(self) -> bool:
        for w in (
            self.worker,
            self.scan_worker,
            self.queue_worker,
            self.mat_scan_worker,
            self.mat_dl_worker,
            self.recovery_worker,
        ):
            if w and w.isRunning():
                return True
        return False

    # ── Material de cursos ────────────────────────────────────────
    def _scan_material(self) -> None:
        if self._closing:
            return
        if self._busy():
            return
        self.mat_scan_btn.setEnabled(False)
        self.mat_dl_btn.hide()
        self.mat_status.setText("Revisando el material de tus cursos…")
        self.mat_scan_worker = MaterialScanWorker(self.settings, self)
        self.mat_scan_worker.log.connect(self._log)
        self.mat_scan_worker.needs_login.connect(self._on_mat_needs_login)
        self.mat_scan_worker.found.connect(self._on_material_found)
        self.mat_scan_worker.failed.connect(self._on_material_failed)
        self.mat_scan_worker.start()

    def _on_mat_needs_login(self) -> None:
        self.mat_scan_btn.setEnabled(True)
        self.mat_status.setText("Inicia sesión para continuar.")
        from zoom2txt.blackboard.client import BlackboardClient
        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = BlackboardLoginDialog(self.settings.blackboard_url, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            BlackboardClient(self.settings.blackboard_url, self._log).store_cookies(dialog.cookies)
            self._log("[+] Sesión de Blackboard guardada.")
            self._scan_material()

    def _on_material_found(self, payload) -> None:
        rows, missing = payload
        self.mat_scan_btn.setEnabled(True)
        self._material_missing = missing
        self.mat_table.setRowCount(0)
        self.mat_table.show()
        for r in rows:
            row = self.mat_table.rowCount()
            self.mat_table.insertRow(row)
            cells = [
                QTableWidgetItem(r["course"]),
                QTableWidgetItem(str(r["total"])),
                QTableWidgetItem(str(r["have"])),
                QTableWidgetItem(str(r["missing"])),
                QTableWidgetItem("Parcial · reintentar" if r.get("error") else "Revisado"),
            ]
            for col, cell in enumerate(cells):
                if col:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 3 and r["missing"]:
                    from PySide6.QtGui import QColor

                    cell.setForeground(QColor(style.WARNING))
                if col == 4 and r.get("error"):
                    from PySide6.QtGui import QColor

                    cell.setForeground(QColor(style.WARNING))
                cell.setToolTip(r.get("note", ""))
                self.mat_table.setItem(row, col, cell)
        errors = sum(bool(r.get("error")) for r in rows)
        for r in rows:
            if r.get("note"):
                self._log("[i] " + r["course"] + ": " + r["note"])
        if not missing:
            self.mat_status.setText(
                "Revisión incompleta: hay cursos con errores. Consulta Actividad y reintenta."
                if errors
                else "Adjuntos revisados y verificados. Los recursos no exportables se detallan en Actividad."
            )
            self.mat_dl_btn.hide()
        else:
            self.mat_status.setText(
                f"{len(missing)} archivos nuevos, modificados o sin verificar."
                + (
                    " 1 curso con errores."
                    if errors == 1
                    else f" {errors} cursos con errores."
                    if errors
                    else ""
                )
            )
            self.mat_dl_btn.setText(f"Descargar lo que falta ({len(missing)})")
            self.mat_dl_btn.show()

    def _on_material_failed(self, error: str) -> None:
        self.mat_scan_btn.setEnabled(True)
        self.mat_status.setText("La revisión falló.")
        self._log(f"[✕] Error revisando material: {error}")
        QMessageBox.critical(self, "Error en material", error)

    def _download_material(self) -> None:
        if self._busy() or not getattr(self, "_material_missing", None):
            return
        self.mat_dl_btn.hide()
        self.mat_cancel_btn.show()
        self.mat_scan_btn.setEnabled(False)
        self.mat_dl_worker = MaterialDownloadWorker(self.settings, self._material_missing, self)
        self.mat_dl_worker.log.connect(self._log)
        self.mat_dl_worker.progress.connect(
            lambda i, t: self.mat_status.setText(f"Descargando… {i}/{t}")
        )
        self.mat_dl_worker.finished_all.connect(self._on_material_done)
        self.mat_dl_worker.finished.connect(self._scan_material)
        self.mat_dl_worker.start()

    def _cancel_material(self) -> None:
        if self.mat_dl_worker:
            self.mat_dl_worker.cancel()
            self.mat_cancel_btn.setEnabled(False)

    def _on_material_done(self, ok: int, failed: int) -> None:
        self.mat_cancel_btn.hide()
        self.mat_cancel_btn.setEnabled(True)
        self.mat_scan_btn.setEnabled(True)
        cancelled = self.mat_dl_worker.cancel_event.is_set()
        msg = f"Material: {ok} archivos verificados, {failed} con errores." + (
            " Cancelado: quedan archivos pendientes."
            if cancelled
            else " Revisa el resultado de la nueva búsqueda."
        )
        self.mat_status.setText(msg)
        self._log(f"[+] {msg}")
        self._notify("Material descargado", msg)

    def _open_materials_dir(self) -> None:
        d = self.settings.materials_dir
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    # ── Modo automático ───────────────────────────────────────────
    def _scan(self) -> None:
        if self._closing:
            return
        if self._busy():
            return
        self.scan_btn.setEnabled(False)
        self._pending_backlog = 0
        self.scan_status.setText("Buscando en Blackboard…")
        ranges = self._selected_week_ranges()
        if self.weeks_chk.isChecked() and not ranges:
            self.scan_btn.setEnabled(True)
            self.scan_status.setText("Marca al menos una semana, o desactiva el filtro por semana.")
            return
        self.scan_worker = ScanWorker(self.settings, ranges, self)
        self._scan_items = []
        self.table.hide()
        self.process_btn.hide()
        self.skip_all_btn.hide()
        self.scan_worker.log.connect(self._log)
        self.scan_worker.needs_login.connect(self._on_needs_login)
        self.scan_worker.found.connect(self._on_scan_found)
        self.scan_worker.capped.connect(self._on_scan_capped)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.start()

    def _on_scan_capped(self, shown: int, total: int) -> None:
        self._pending_backlog = total - shown
        self._log(
            f"[i] {total} pendientes en el filtro; {shown} listas en esta tanda. Las restantes siguen guardadas. Máximo de la app: 6 clases nuevas al día; no es una cuota de Google. Consulta Actividad si alguna no se pudo preparar."
        )

    def _on_needs_login(self) -> None:
        self.scan_btn.setEnabled(True)
        self.scan_status.setText("Inicia sesión para continuar.")
        from zoom2txt.blackboard.client import BlackboardClient
        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = BlackboardLoginDialog(self.settings.blackboard_url, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            BlackboardClient(self.settings.blackboard_url, self._log).store_cookies(dialog.cookies)
            self._log("[+] Sesión de Blackboard guardada.")
            self._scan()

    def _on_scan_found(self, items: list) -> None:
        self.scan_btn.setEnabled(True)
        self._scan_items = list(items)
        self._refresh_backlog_notice()
        self.table.setRowCount(0)
        if not items:
            from zoom2txt.workflow import WorkLedger

            daily = WorkLedger(self.settings).daily()
            self.table.hide()
            self.class_detail.hide()
            self.process_btn.hide()
            self.skip_all_btn.hide()
            self.scan_status.setText(
                "Revisión incompleta: no se pudo comprobar si faltan clases. Consulta Actividad y vuelve a buscar."
                if getattr(self.scan_worker, "partial_errors", [])
                else (
                    f"Límite de 6 clases nuevas alcanzado. Tus pendientes siguen en cola para {daily['next_day']} a las 00:00 (Lima)."
                    if daily["remaining"] == 0 and getattr(self.scan_worker, "all_pending", [])
                    else "No hay pendientes en este filtro. Revisa Historial si omitiste clases o cambiaste las fechas."
                )
            )
            return
        self.table.show()
        self.class_detail.show()
        self.class_detail.setText("Selecciona una fila para ver el detalle de su estado.")
        self.process_btn.show()
        self.skip_all_btn.show()
        self.scan_status.setText(
            f"{len(items)} clases listas. La app comprobará YouTube y reutilizará tus archivos al continuar."
            + (
                " Revisión parcial: consulta Actividad."
                if getattr(self.scan_worker, "partial_errors", [])
                else ""
            )
        )
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked)
            curso = QTableWidgetItem(item["course_name"])
            curso.setFlags(Qt.ItemFlag.ItemIsEnabled)
            nombre = QTableWidgetItem(item["name"])
            semana = QTableWidgetItem(self._week_of(item.get("sort_key", "")))
            semana.setFlags(Qt.ItemFlag.ItemIsEnabled)
            semana.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            fecha = QTableWidgetItem(item["start_time"][:10])
            fecha.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for cell in (curso, nombre, semana, fecha):
                cell.setToolTip(cell.text())
            self.table.setItem(row, 0, chk)
            self.table.setItem(row, 1, curso)
            self.table.setItem(row, 2, nombre)
            self.table.setItem(row, 3, semana)
            self.table.setItem(row, 4, fecha)
            from zoom2txt.blackboard.registry import recording_key
            from zoom2txt.storage import StateStore
            from zoom2txt.work_items import job_status

            job = StateStore(self.settings).get(
                "job", item.get("job_key") or recording_key(item["course_id"], item["meeting_id"])
            )
            self._set_class_status(row, job_status(job), job.get("error", ""))

    def _load_saved_work(self):
        if self._closing or self._busy():
            return
        from zoom2txt.work_items import local_continuations

        try:
            items = local_continuations(self.settings, self._selected_week_ranges())
            if items:
                self._on_scan_found(items)
                self.skip_all_btn.hide()
                self.scan_status.setText(
                    "Tienes trabajos guardados. Pulsa Procesar / continuar para retomarlos juntos."
                )
        except Exception as exc:  # noqa: BLE001 — keep the main screen usable
            from zoom2txt.security import redact

            self.scan_status.setText(
                "No se pudo leer la cola guardada. Pulsa Buscar clases nuevas."
            )
            self._log("[!] " + redact(exc))

    def _set_class_status(self, row, text, detail=""):
        from PySide6.QtGui import QColor

        cell = QTableWidgetItem(text)
        cell.setFlags(Qt.ItemFlag.ItemIsEnabled)
        cell.setToolTip(detail or text)
        cell.setForeground(
            QColor(
                style.SUCCESS
                if text == "TXT verificado"
                else style.WARNING
                if text == "Por resolver"
                else style.TEXT
            )
        )
        self.table.setItem(row, 5, cell)

    def _show_class_detail(self, row, column):
        cell = self.table.item(row, 5)
        if cell:
            course = self.table.item(row, 1)
            name = self.table.item(row, 2)
            identity = " · ".join(item.text() for item in (course, name) if item)
            self.class_detail.setText(f"{identity}\n{cell.toolTip()}")

    def _queue_table_row(self, index):
        from zoom2txt.blackboard.registry import recording_key

        item = self._queue_items[index]
        key = recording_key(item["course_id"], item["meeting_id"])
        return next(
            (
                i
                for i, candidate in enumerate(self._scan_items)
                if recording_key(candidate["course_id"], candidate["meeting_id"]) == key
            ),
            None,
        )

    def _on_queue_item_finished(self, index, txt):
        from zoom2txt.blackboard.registry import recording_key
        from zoom2txt.storage import StateStore

        row = self._queue_table_row(index)
        if row is None:
            return
        item = self._queue_items[index]
        job = StateStore(self.settings).get(
            "job", item.get("job_key") or recording_key(item["course_id"], item["meeting_id"])
        )
        if txt:
            self._set_class_status(row, "TXT verificado", "Texto disponible en tu biblioteca.")
            self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        else:
            self._set_class_status(
                row,
                "Por resolver",
                job.get("error") or "Pendiente. Vuelve a continuar esta selección.",
            )
            self.class_detail.setText(
                job.get("error") or "Pendiente. Vuelve a continuar esta selección."
            )

    def _on_scan_failed(self, error: str) -> None:
        self._refresh_backlog_notice()
        self.scan_btn.setEnabled(True)
        self.scan_status.setText("La búsqueda falló.")
        self._log(f"[✕] Error buscando clases: {error}")
        QMessageBox.critical(self, "Error en la búsqueda", error)

    def _skip_all_pending(self) -> None:
        pending = self.scan_worker.all_pending if self.scan_worker else []
        if not pending:
            return
        answer = QMessageBox.question(
            self,
            "Omitir todo lo pendiente",
            f"Se marcarán {len(pending)} grabaciones como omitidas, "
            "SIN transcribirlas.\n\n"
            "Úsalo si esas clases ya las tienes de antes: a partir de ahora "
            "la búsqueda solo traerá las clases nuevas, y la numeración "
            "continuará desde donde va cada curso.\n\n¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        from zoom2txt.blackboard.registry import Registry

        registry = Registry(self.settings)
        for item in pending:
            registry.mark_skipped(
                item["course_id"],
                item["meeting_id"],
                "Omitida por el usuario",
                item["name"],
            )
        self._log(
            f"[+] {len(pending)} grabaciones marcadas como ya "
            f"omitidas. Puedes restaurarlas desde Historial."
        )
        self._pending_backlog = 0
        self._scan_items = []
        self.table.hide()
        self.table.setRowCount(0)
        self.process_btn.hide()
        self.skip_all_btn.hide()
        self.scan_status.setText("Ponte al día ✓ — busca de nuevo cuando haya clases nuevas.")
        self._refresh_backlog_notice()

    def _process_queue(self) -> None:
        if self._busy():
            return
        ranges = self._selected_week_ranges()
        if self.weeks_chk.isChecked() and not ranges:
            self.scan_status.setText(
                "Marca al menos una semana o desactiva el filtro para continuar."
            )
            return
        selected: list[dict] = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).checkState() != Qt.CheckState.Checked:
                continue
            item = dict(self._scan_items[row])
            if ranges and not any(lo <= item["start_time"][:10] <= hi for lo, hi in ranges):
                continue
            nombre = self.table.item(row, 2).text().strip()
            nombre = re.sub(r'[<>:"/\\|?*]', "_", nombre).strip(". ")
            if not nombre:
                nombre = item["name"]
            item["name"] = nombre
            selected.append(item)
        if not selected:
            QMessageBox.information(
                self,
                "Nada seleccionado",
                "Marca una grabación que esté dentro de las semanas elegidas.",
            )
            return
        password = self._ensure_credentials()
        if password is None:
            return
        if not self._ensure_pipeline_health():
            return
        self.settings.auto_delete_youtube = self.delete_chk.isChecked()
        self.settings.save()
        self._launch_queue(selected, password or "")

    def _launch_queue(self, selected: list, password: str) -> None:
        """Arranca la cola seleccionada desde la interfaz de escritorio."""
        self._queue_total = len(selected)
        self._queue_items = selected
        self._active_queue_index = None
        self.table.setEnabled(False)
        self._reset_stages()
        self.open_btn.hide()
        self.process_btn.hide()
        self.skip_all_btn.hide()
        self.cancel_queue_btn.show()
        self.scan_btn.setEnabled(False)
        self.settings_btn.setEnabled(False)

        self.queue_worker = QueueWorker(
            selected, password, self.settings, self.delete_chk.isChecked(), self
        )
        self.queue_worker.log.connect(self._log)
        self.queue_worker.stage.connect(self._on_stage)
        self.queue_worker.item_started.connect(self._on_queue_item)
        self.queue_worker.item_finished.connect(self._on_queue_item_finished)
        self.queue_worker.all_done.connect(self._on_queue_done)
        self.queue_worker.start()

    def _on_queue_item(self, index: int, name: str) -> None:
        self._active_queue_index = index
        row = self._queue_table_row(index)
        if row is not None:
            self._set_class_status(row, "Comprobando…")
        self.queue_label.setText(f"Clase {index + 1}: {name}")
        self._reset_stages()

    def _cancel_queue(self) -> None:
        if self.queue_worker:
            self.queue_worker.cancel()
            self._log("[!] Cancelando cola… (se detendrá al terminar el paso actual)")
            self.cancel_queue_btn.setEnabled(False)

    def _on_queue_done(self, ok: int, failed: int) -> None:
        self.queue_label.setText(f"{ok} TXT verificados · {failed} con problemas")
        self._active_queue_index = None
        self.table.setEnabled(True)
        self.cancel_queue_btn.hide()
        self.cancel_queue_btn.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.settings_btn.setEnabled(True)
        self._log("")
        self._log("============================================")
        self._log(f"Cola terminada: {ok} transcritas, {failed} con problemas.")
        self._notify(
            "Cola terminada",
            f"{ok} clases transcritas" + (f", {failed} con problemas." if failed else "."),
        )
        self._refresh_backlog_notice()
        self._log("[i] " + self.backlog_notice.text())
        if ok:
            self._result_path = self.settings.transcripts_dir / "x"
            self.open_btn.show()
        self._reset_stages()
        if ok == self._queue_total:
            for r in self.stage_rows:
                r.set_state("done")
        self.table.show()
        pending = any(
            self.table.item(i, 0).checkState() == Qt.CheckState.Checked
            for i in range(self.table.rowCount())
        )
        self.process_btn.setVisible(pending)
        self.process_btn.setText("Continuar pendientes")
        self.scan_status.setText(
            "Las clases pendientes siguen marcadas. Puedes continuarlas aquí; consulta el estado de cada fila."
            if pending
            else "Selección completada. Busca clases nuevas cuando quieras continuar."
        )

    # ── Modo manual ───────────────────────────────────────────────
    def _start(self) -> None:
        if self._busy():
            return
        url = self.url_edit.text().strip()
        nombre = self.name_edit.text().strip()
        if not url or not nombre:
            QMessageBox.warning(
                self,
                "Datos incompletos",
                "Ingresa la URL de la grabación y el nombre de la clase.",
            )
            return
        # El nombre se usa como nombre de archivo y título en YouTube:
        # fuera caracteres inválidos en Windows/Linux.
        nombre = re.sub(r'[<>:"/\\|?*]', "_", nombre).strip(". ")
        if not nombre:
            QMessageBox.warning(
                self,
                "Nombre inválido",
                "El nombre de la clase solo contiene caracteres "
                "no permitidos. Usa letras, números o guiones.",
            )
            return
        password = self._ensure_credentials()
        if not password:
            return
        if not self._ensure_pipeline_health():
            return

        self._reset_stages()
        self.open_btn.hide()
        self._result_path = None
        self._set_running(True)

        self.worker = PipelineWorker(url, password, nombre, self.settings, self)
        self.worker.log.connect(self._log)
        self.worker.stage.connect(self._on_stage)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self._log("[!] Cancelando… (se detendrá al terminar el paso actual)")
            self.cancel_btn.setEnabled(False)

    def _set_running(self, running: bool) -> None:
        self.start_btn.setVisible(not running)
        self.cancel_btn.setVisible(running)
        self.cancel_btn.setEnabled(True)
        self.url_edit.setEnabled(not running)
        self.name_edit.setEnabled(not running)
        self.settings_btn.setEnabled(not running)
        self.scan_btn.setEnabled(not running)

    def _on_stage(self, index: int) -> None:
        active = getattr(self, "_active_queue_index", None)
        if active is not None:
            row = self._queue_table_row(active)
            if row is not None:
                self._set_class_status(
                    row, ["Preparando video", "Subiendo…", "Subtítulos…", "Verificando TXT"][index]
                )
        self._current_stage = index
        for i, row in enumerate(self.stage_rows):
            row.set_state("done" if i < index else "active" if i == index else "pending")

    def _on_done(self, txt_path: str, video_id: str) -> None:
        self._refresh_backlog_notice()
        self._set_running(False)
        if txt_path:
            for row in self.stage_rows:
                row.set_state("done")
            self._result_path = Path(txt_path)
            self.open_btn.show()
            self._log("")
            self._log("============================================")
            self._log(
                f"¡Proceso terminado! Ya puedes arrastrar '{self._result_path.name}' a NotebookLM."
            )
            self._notify(
                "Transcripción lista",
                f"{self._result_path.name} está lista para NotebookLM.",
            )
            if video_id:
                from zoom2txt.storage import StateStore

                jobs = [
                    j
                    for j in StateStore(self.settings).list("job")
                    if j.get("video_id") == video_id
                ]
                if jobs and jobs[0].get("status") == "done":
                    self._ask_delete_video(video_id)
                else:
                    self._log(
                        "[i] Se conserva el video de YouTube mientras la copia solicitada está pendiente."
                    )
        else:
            self._log(
                "[!] El proceso terminó sin transcripción (cancelado o "
                "subtítulos no disponibles todavía)."
            )
            if video_id:
                self._log(f"    El video sigue en YouTube: https://youtu.be/{video_id}")

    def _ask_delete_video(self, video_id: str) -> None:
        answer = QMessageBox.question(
            self,
            "Transcripción lista",
            "Ya tienes el texto de la clase.\n\n"
            "¿Eliminar el video de YouTube? Es irreversible. Puedes conservarlo como respaldo; su privacidad actual no cambiará.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._log(f"[i] Video conservado en YouTube: https://youtu.be/{video_id}")
            return
        self._delete_worker = DeleteVideoWorker(video_id, self)
        self._delete_worker.log.connect(self._log)
        self._delete_worker.finished_ok.connect(self._on_delete_done)
        self._delete_worker.start()

    def _on_delete_done(self, ok: bool) -> None:
        if not ok:
            QMessageBox.warning(
                self,
                "No se pudo eliminar",
                "El video no se pudo eliminar automáticamente. Puedes "
                "borrarlo a mano en YouTube Studio.",
            )

    def _on_failed(self, error: str) -> None:
        self._set_running(False)
        if 0 <= self._current_stage < len(self.stage_rows):
            self.stage_rows[self._current_stage].set_state("error")
        self._log(f"[✕] Error: {error}")
        QMessageBox.critical(self, "Algo salió mal", error)

    def _open_result(self) -> None:
        folder = self._result_path.parent if self._result_path else self.settings.transcripts_dir
        if folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event):
        health_worker = getattr(getattr(self, "health", None), "_worker", None)
        if health_worker and health_worker.isRunning() and not self._busy():
            self._closing = True
            self.health.cancel()
            event.ignore()
            QTimer.singleShot(300, self.close)
            return
        if self._busy():
            if not self._closing:
                answer = QMessageBox.question(
                    self,
                    "Trabajo en curso",
                    "¿Pausar los trabajos y cerrar cuando terminen de detenerse? Se conserva el estado para continuar.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self._closing = True
                self.setEnabled(False)
                for worker in (
                    self.worker,
                    self.queue_worker,
                    self.scan_worker,
                    self.mat_scan_worker,
                    self.mat_dl_worker,
                    self.recovery_worker,
                    getattr(self.library, "_loader", None),
                    health_worker,
                ):
                    if worker and worker.isRunning():
                        if hasattr(worker, "cancel"):
                            worker.cancel()
                        worker.requestInterruption()
            event.ignore()
            QTimer.singleShot(300, self.close)
            return
        event.accept()

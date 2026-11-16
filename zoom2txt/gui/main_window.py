"""Ventana principal de Zoom2TXT.

El flujo es el de siempre —buscar grabaciones en Blackboard, descargar de Zoom,
subir a YouTube, esperar subtítulos, entregar el TXT y limpiar—; esta versión
reorganiza la interfaz alrededor de él: barra lateral por áreas, panel de inicio,
progreso real por paso, avisos no bloqueantes y un buscador de acciones (Ctrl+K).
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
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
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zoom2txt import __version__, logs
from zoom2txt.auth.credentials import get_zoom_password
from zoom2txt.blackboard import weeks as wk
from zoom2txt.config import Settings, credentials_status
from zoom2txt.core.pipeline import STAGES
from zoom2txt.gui import style, widgets
from zoom2txt.gui.settings_dialog import SettingsDialog
from zoom2txt.gui.setup_wizard import SetupWizard
from zoom2txt.gui.workers import (
    CleanupWorker,
    CourseListWorker,
    MaterialDownloadWorker,
    MaterialScanWorker,
    PipelineWorker,
    QueueWorker,
    RecoveryWorker,
    ScanWorker,
)

QtSizePolicy = QSizePolicy.Policy

PAGE_CLASSES, PAGE_MANUAL, PAGE_MATERIAL, PAGE_LIBRARY = 0, 1, 2, 3
PAGE_BACKLOG, PAGE_HEALTH, PAGE_ASSISTANT, PAGE_HOME = 4, 5, 6, 7

# (icono, etiqueta, título, subtítulo) por índice de página. Los índices 0–6 se
# conservan de versiones anteriores; Inicio se añadió al final.
PAGES = (
    ("scan-search", "Clases", "Tus clases",
     "Elige un curso y, si quieres, filtra por semanas para buscar tus grabaciones."),
    ("pen-line", "Manual", "Transcripción manual",
     "Procesa un enlace de Zoom con recuperación si algo se interrumpe."),
    ("folder-down", "Material", "Material de cursos",
     "Adjuntos verificados. Los recursos no exportables se indican por separado."),
    ("library", "Biblioteca", "Tu biblioteca",
     "Tus transcripciones en disco: búscalas, léelas y llévalas al asistente."),
    ("calendar-clock", "Pendientes", "Lo que falta",
     "Tu cola continúa mañana. Revisa también las semanas que necesitan atención."),
    ("activity", "Estado", "Estado de la app",
     "Comprueba el entorno antes de procesar y guarda un informe seguro si necesitas ayuda."),
    ("sparkles", "Asistente IA", "Asistente de estudio",
     "Pregunta sobre tus clases y materiales; cada respuesta cita su fuente."),
    ("layout-dashboard", "Inicio", "Inicio",
     "Tu ciclo de un vistazo: qué falta, qué sigue y cómo va el día."),
)  # fmt: skip

# Orden y agrupación en la barra lateral.
NAV_GROUPS = (
    ("", (PAGE_HOME,)),
    ("FLUJO", (PAGE_CLASSES, PAGE_MANUAL, PAGE_MATERIAL)),
    ("ESTUDIO", (PAGE_LIBRARY, PAGE_ASSISTANT)),
    ("CONTROL", (PAGE_BACKLOG, PAGE_HEALTH)),
)

STAGE_SHORT = ("Descarga", "Subida", "Subtítulos", "Verificación")
STAGE_STATUS = (
    "Preparando video",
    "Preparando texto…",
    "Texto en proceso…",
    "Verificando y limpiando",
)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.worker: PipelineWorker | None = None
        self.scan_worker: ScanWorker | None = None
        self.course_worker: CourseListWorker | None = None
        self._course_login_pending = False
        self._scan_login_pending = False
        self.queue_worker: QueueWorker | None = None
        self.mat_scan_worker: MaterialScanWorker | None = None
        self.mat_dl_worker: MaterialDownloadWorker | None = None
        self._material_missing: list = []
        self._scan_items: list[dict] = []
        self._queue_items: list[dict] = []
        self._active_queue_index: int | None = None
        self._pending_backlog = 0
        self._current_stage = -1
        self._result_path: Path | None = None
        self.setWindowTitle("Zoom2TXT")
        self.setMinimumSize(880, 560)
        self.resize(1240, 820)
        self.recovery_worker = None
        self.cleanup_worker = None
        self._closing = False
        from zoom2txt.gui.icon import make_app_icon

        self.setWindowIcon(make_app_icon())
        self._queue_total = 0
        self.toasts = widgets.ToastHost(self)
        self._build_ui()
        self._setup_tray()
        self._setup_shortcuts()
        self._backlog_timer = QTimer(self)
        self._backlog_timer.setInterval(60_000)
        self._backlog_timer.timeout.connect(self._refresh_backlog_notice)
        self._backlog_timer.start()
        QTimer.singleShot(0, self._refresh_backlog_notice)
        QTimer.singleShot(0, self._load_saved_work)
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.setInterval(120_000)
        self._cleanup_timer.timeout.connect(self._retry_cleanup)
        self._cleanup_timer.start()
        QTimer.singleShot(3000, self._retry_cleanup)
        if getattr(self.settings, "scan_on_start", False):
            QTimer.singleShot(1500, self._scan_on_start)

    def _scan_on_start(self) -> None:
        """Búsqueda automática opcional al abrir (solo si la configuración está completa)."""
        if self._closing or self._busy() or type(self).__name__ != "MainWindow":
            return
        try:
            if credentials_status(self.settings):
                return
        except Exception:  # noqa: BLE001 — una comprobación fallida no impide abrir la app
            return
        self._navigate(PAGE_CLASSES)
        self._scan()

    def _retry_cleanup(self):
        if self._closing or self._busy():
            return
        from zoom2txt.storage import StateStore

        if not any(
            row.get("cleanup_pending") and not row.get("youtube_deleted")
            for row in StateStore(self.settings).list("job")
        ):
            return
        self.cleanup_worker = CleanupWorker(self.settings, self)
        self.cleanup_worker.log.connect(self._log)
        self.cleanup_worker.finished.connect(self._cleanup_finished)
        self.cleanup_worker.start()

    def _cleanup_finished(self):
        self._refresh_backlog_notice()
        from zoom2txt.blackboard.registry import recording_key
        from zoom2txt.storage import StateStore

        state = StateStore(self.settings)
        for row, item in enumerate(self._scan_items):
            if row >= self.table.rowCount():
                break
            key = item.get("job_key") or recording_key(item["course_id"], item["meeting_id"])
            job = state.get("job", key)
            if job.get("youtube_deleted") and job.get("status") in ("done", "sync_pending"):
                self._set_class_status(
                    row, "TXT verificado", "Texto disponible; eliminación remota confirmada."
                )
                self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)

    def _setup_tray(self) -> None:
        """Bandeja del sistema: permite avisar cuando una transcripción
        termina aunque la ventana esté minimizada."""
        from PySide6.QtWidgets import QSystemTrayIcon

        from zoom2txt.gui.icon import make_app_icon

        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip("Zoom2TXT")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _setup_shortcuts(self) -> None:
        self._shortcuts = []

        def bind(sequence: str, slot) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

        bind("Ctrl+K", self._open_palette)
        bind("Ctrl+,", self._open_settings)
        bind("F5", self._shortcut_scan)
        for number, page in enumerate(
            (PAGE_HOME, PAGE_CLASSES, PAGE_MANUAL, PAGE_MATERIAL, PAGE_LIBRARY,
             PAGE_ASSISTANT, PAGE_BACKLOG, PAGE_HEALTH),
            start=1,
        ):  # fmt: skip
            bind(f"Ctrl+{number}", lambda page=page: self._navigate_if_enabled(page))

    def _navigate_if_enabled(self, page: int) -> None:
        if self._nav_buttons[page].isEnabled():
            self._navigate(page)

    def _shortcut_scan(self) -> None:
        if self.stack.currentIndex() == PAGE_MATERIAL:
            self._scan_material()
        elif self.scan_btn.isEnabled() and self._nav_buttons[PAGE_CLASSES].isEnabled():
            self._navigate(PAGE_CLASSES)
            self._scan()

    def _notify(self, title: str, message: str, tone: str = "success") -> None:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        QApplication.alert(self, 0)  # parpadeo en la barra de tareas
        if getattr(self, "toasts", None) is not None and self.isVisible():
            self.toasts.notify(title, message, tone)
        if (
            getattr(self, "tray", None)
            and self.tray.isVisible()
            and (self.isMinimized() or not self.isActiveWindow())
        ):
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 8000)

    # ── UI ────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        from PySide6.QtWidgets import QButtonGroup

        from zoom2txt.gui.adaptive_stack import AdaptiveStack
        from zoom2txt.gui.library_view import LibraryView

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # ── Barra lateral ─────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 14)
        side.setSpacing(2)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(6, 0, 0, 0)
        brand_row.setSpacing(10)
        from zoom2txt.gui.icon import make_app_icon

        logo = QLabel()
        logo.setPixmap(make_app_icon().pixmap(30, 30))
        logo.setFixedSize(30, 30)
        brand = QLabel("Zoom2TXT")
        brand.setObjectName("brand")
        brand_col = QVBoxLayout()
        brand_col.setSpacing(0)
        brand_col.addWidget(brand)
        tagline = QLabel("De la clase al texto")
        tagline.setObjectName("faint")
        brand_col.addWidget(tagline)
        brand_row.addWidget(logo)
        brand_row.addLayout(brand_col, 1)
        side.addLayout(brand_row)
        side.addSpacing(16)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        buttons: dict[int, widgets.NavButton] = {}
        for title, page_ids in NAV_GROUPS:
            if title:
                side.addSpacing(10)
                eyebrow = QLabel(title)
                eyebrow.setObjectName("eyebrow")
                eyebrow.setContentsMargins(10, 0, 0, 2)
                side.addWidget(eyebrow)
            for page in page_ids:
                icon_name, label = PAGES[page][0], PAGES[page][1]
                nav = widgets.NavButton(icon_name, label)
                nav.clicked.connect(lambda _=False, idx=page: self._navigate(idx))
                self._nav_group.addButton(nav, page)
                side.addWidget(nav)
                buttons[page] = nav
        self._nav_buttons: list[widgets.NavButton] = [buttons[i] for i in range(len(PAGES))]
        self.history_btn = widgets.NavButton("history", "Historial")
        self.history_btn.setCheckable(False)
        self.history_btn.setToolTip("Omisiones y recuperación de trabajos")
        self.history_btn.clicked.connect(self._open_history)
        side.addWidget(self.history_btn)
        side.addStretch()

        # Cupo diario (regla local de 6 clases nuevas por día)
        quota = QFrame()
        quota.setObjectName("card")
        quota_lay = QHBoxLayout(quota)
        quota_lay.setContentsMargins(12, 10, 12, 10)
        quota_lay.setSpacing(10)
        self.quota_ring = widgets.ProgressRing(40, 4.5)
        quota_col = QVBoxLayout()
        quota_col.setSpacing(0)
        self.quota_title = QLabel("Hoy")
        self.quota_title.setObjectName("h3")
        self.quota_note = QLabel("—")
        self.quota_note.setObjectName("faint")
        self.quota_note.setWordWrap(True)
        quota_col.addWidget(self.quota_title)
        quota_col.addWidget(self.quota_note)
        quota_lay.addWidget(self.quota_ring)
        quota_lay.addLayout(quota_col, 1)
        quota.setToolTip(
            "Clases nuevas iniciadas hoy. El máximo de 6 es una regla de la app para cuidar tu "
            "cuenta; no es la cuota de Google."
        )
        self.quota_card = quota
        side.addWidget(quota)
        side.addSpacing(8)

        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(4)
        self.settings_btn = widgets.NavButton("settings", "Ajustes")
        self.settings_btn.setCheckable(False)
        self.settings_btn.setToolTip("Ajustes (Ctrl+,)")
        self.settings_btn.clicked.connect(self._open_settings)
        footer_row.addWidget(self.settings_btn, 1)
        self.theme_toggle_btn = widgets.icon_button(
            "sun" if style.current_mode == "dark" else "moon", "Cambiar modo claro / oscuro", "MUTED", 17
        )
        self.theme_toggle_btn.clicked.connect(self._toggle_theme)
        footer_row.addWidget(self.theme_toggle_btn)
        side.addLayout(footer_row)

        version = QLabel(f"v{__version__} · credenciales solo en esta PC")
        version.setObjectName("faint")
        version.setWordWrap(True)
        version.setContentsMargins(10, 4, 0, 0)
        side.addWidget(version)
        shell.addWidget(sidebar)

        # ── Columna principal ─────────────────────────────────────
        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)
        shell.addWidget(main, 1)

        topbar = QWidget()
        topbar.setObjectName("topbar")
        top = QHBoxLayout(topbar)
        self._topbar_layout = top
        top.setContentsMargins(32, 16, 28, 14)
        top.setSpacing(16)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        self.page_title = QLabel("Tus clases")
        self.page_title.setObjectName("h1")
        self.page_subtitle = QLabel("")
        self.page_subtitle.setObjectName("muted")
        self.page_subtitle.setWordWrap(True)
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_subtitle)
        top.addLayout(heading, 1)
        self.palette_btn = widgets.button(
            "Buscar o ir a…", "search", "secondary", "Acciones, páginas y texto de tus clases (Ctrl+K)"
        )
        self.palette_btn.setMinimumWidth(190)
        self.palette_btn.setStyleSheet("text-align: left; padding-left: 12px;")
        self.palette_btn.clicked.connect(self._open_palette)
        palette_hint = QLabel("Ctrl K")
        palette_hint.setObjectName("kbd")
        top.addWidget(self.palette_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(palette_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        main_lay.addWidget(topbar)

        content = QWidget()
        content.setObjectName("pageHost")
        outer = QVBoxLayout(content)
        self._content_layout = outer
        outer.setContentsMargins(32, 20, 32, 20)
        outer.setSpacing(16)
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        main_lay.addWidget(scroll, 1)

        card = QFrame()
        card.setObjectName("workspace")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)

        self.stack = AdaptiveStack()
        self.stack.addWidget(self._build_auto_tab())
        self.stack.addWidget(self._build_manual_tab())
        self.stack.addWidget(self._build_material_tab())
        self.library = LibraryView(self.settings)
        self.library.ask_requested.connect(self._ask_about_source)
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
        from zoom2txt.gui.assistant_view import AssistantView

        self.assistant = AssistantView(self.settings, self)
        self.stack.addWidget(self.assistant)
        from zoom2txt.gui.home_view import HomeView

        self.home = HomeView(self.settings, self)
        self.home.scan_requested.connect(self._scan_from_home)
        self.home.navigate_requested.connect(self._navigate_if_enabled)
        self.home.continue_requested.connect(lambda: self._navigate(PAGE_CLASSES))
        self.stack.addWidget(self.home)
        card_lay.addWidget(self.stack)
        outer.addWidget(card)

        # ── Panel de actividad: estado, pasos y registro ──────────
        self.body_widget = QFrame()
        self.body_widget.setObjectName("card")
        self.activity_dock = self.body_widget
        body = QVBoxLayout(self.body_widget)
        body.setContentsMargins(18, 14, 18, 16)
        body.setSpacing(10)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status_dot = widgets.StatusDot()
        self.status_dot.set_state("FAINT")
        status_row.addWidget(self.status_dot)
        self.dock_badge = widgets.chip("En espera")
        status_row.addWidget(self.dock_badge)
        self.dock_text = widgets.ElidedLabel("Listo · selecciona tus clases para procesar")
        self.dock_text.setObjectName("muted")
        status_row.addWidget(self.dock_text, 1)
        self.dock_material_status = QLabel("")
        self.dock_material_status.setObjectName("faint")
        status_row.addWidget(self.dock_material_status)
        self.toggle_console_btn = widgets.button(
            "Registro", "terminal", "ghost", "Muestra u oculta el registro técnico detallado."
        )
        self.toggle_console_btn.setCheckable(True)
        self.toggle_console_btn.clicked.connect(self._toggle_console)
        status_row.addWidget(self.toggle_console_btn)
        body.addLayout(status_row)

        # Segunda fila: clase en curso y acciones que solo aparecen cuando aplican.
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self.queue_label = QLabel("")
        self.queue_label.setObjectName("accent")
        self.queue_label.setWordWrap(True)
        self.queue_label.hide()
        actions_row.addWidget(self.queue_label, 1)
        self.recovery_cancel_btn = widgets.button("Pausar recuperación", "pause", "secondary")
        self.recovery_cancel_btn.clicked.connect(self._cancel_recovery)
        self.recovery_cancel_btn.hide()
        actions_row.addWidget(self.recovery_cancel_btn)
        self.open_btn = widgets.button("Abrir transcripciones", "folder-open", "secondary")
        self.open_btn.hide()
        self.open_btn.clicked.connect(self._open_result)
        actions_row.addWidget(self.open_btn)
        body.addLayout(actions_row)

        self.stepper = widgets.PipelineStepper(list(STAGES))
        self.stage_rows = self.stepper.rows
        body.addWidget(self.stepper)

        self.console = QPlainTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(1500)
        self.console.setMinimumHeight(120)
        body.addWidget(self.console, 1)
        outer.addWidget(self.body_widget, 1)
        self._card = card
        collapsed = bool(getattr(self.settings, "activity_collapsed", False))
        self.console.setVisible(not collapsed)
        self.toggle_console_btn.setChecked(not collapsed)

        self._nav_buttons[PAGE_HOME].setChecked(True)
        self._navigate(self._initial_page())

    def _initial_page(self) -> int:
        return PAGE_HOME

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_content_layout"):
            compact = self.width() < 1120
            inset = 16 if compact else 32
            self._content_layout.setContentsMargins(inset, 14 if compact else 20, inset, 16)
            self._topbar_layout.setContentsMargins(inset, 12 if compact else 16, inset - 4, 12)
            self.palette_btn.setMinimumWidth(120 if compact else 190)
            # En ventanas bajas la tarjeta de cupo cede su sitio a la navegación.
            self.quota_card.setVisible(self.height() >= 690)
            if hasattr(self, "courses_btn") and compact != getattr(self, "_compact", None):
                self._compact = compact
                self.courses_btn.setText("" if compact else "Actualizar cursos")
                self.course_combo.setMinimumWidth(130 if compact else 200)
                self.course_combo.setMinimumContentsLength(12 if compact else 24)
                self.term_btn.setText(
                    "Inicio de clases…" if compact else "Configurar inicio de clases…"
                )
                self.table.setColumnWidth(5, 168 if compact else 196)
                self.table.setColumnWidth(4, 96 if compact else 110)
            if hasattr(self, "table"):
                row_height = 34 if compact else style.ROW_HEIGHT
                self.table.verticalHeader().setMinimumSectionSize(row_height)
                self.table.verticalHeader().setDefaultSectionSize(row_height)
                self.table.setFixedHeight(style.HEADER_HEIGHT + 6 * row_height + 2)
            if hasattr(self, "weeks_grid"):
                self._reflow_weeks()

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "table", None) and event.type() in (
            QEvent.Type.ShowToParent,
            QEvent.Type.HideToParent,
        ):
            self.classes_empty.setVisible(event.type() == QEvent.Type.HideToParent)
        return super().eventFilter(watched, event)

    def _build_auto_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # ── Ámbito de la búsqueda: curso, semanas y acción principal ──
        scope = QFrame()
        scope.setObjectName("card")
        scope_lay = QVBoxLayout(scope)
        scope_lay.setContentsMargins(16, 14, 16, 14)
        scope_lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.course_controls = QWidget()
        course_row = QHBoxLayout(self.course_controls)
        course_row.setContentsMargins(0, 0, 0, 0)
        course_row.setSpacing(8)
        course_label = QLabel("Curso")
        course_label.setObjectName("h3")
        self.course_combo = QComboBox()
        self.course_combo.setAccessibleName("Curso para buscar grabaciones")
        self.course_combo.setMinimumWidth(200)
        self.course_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.course_combo.setMinimumContentsLength(24)
        self.course_combo.addItem("Todos los cursos", None)
        course_label.setBuddy(self.course_combo)
        self.courses_btn = widgets.button(
            "Actualizar cursos", "refresh-cw", "secondary",
            "Actualizar cursos: carga la lista desde Blackboard sin abrir Zoom ni preparar grabaciones.",
        )  # fmt: skip
        self.courses_btn.setAccessibleName("Actualizar cursos")
        self.courses_btn.clicked.connect(self._refresh_courses)
        course_row.addWidget(course_label)
        course_row.addWidget(self.course_combo, 1)
        course_row.addWidget(self.courses_btn)
        top.addWidget(self.course_controls, 1)
        self.scan_btn = widgets.button(
            "Buscar clases nuevas", "search", "primary",
            "Busca en tu Blackboard las grabaciones que aún no has transcrito (F5).",
        )  # fmt: skip
        self.scan_btn.clicked.connect(self._scan)
        top.addWidget(self.scan_btn)
        scope_lay.addLayout(top)

        self.course_hint = QLabel(
            "Elige un curso o busca en todos. Puedes combinarlo con el filtro por semana."
        )
        self.course_hint.setObjectName("faint")
        self.course_hint.setWordWrap(True)
        scope_lay.addWidget(self.course_hint)
        from zoom2txt.workflow import WorkLedger

        self._set_courses(WorkLedger(self.settings).course_choices())
        self.course_combo.currentIndexChanged.connect(self._on_course_changed)
        scope_lay.addWidget(widgets.divider())

        # Filtro por semanas académicas
        self.weeks_box = QFrame()
        self.weeks_box.setObjectName("plain")
        wlay = QVBoxLayout(self.weeks_box)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.setSpacing(8)
        wtop = QHBoxLayout()
        wtop.setSpacing(10)
        self.weeks_chk = QCheckBox("Filtrar por semana del ciclo")
        self.weeks_chk.toggled.connect(self._on_weeks_toggled)
        self.weeks_hint = QLabel("")
        self.weeks_hint.setObjectName("faint")
        self.weeks_hint.setWordWrap(True)
        self.term_btn = widgets.button("Configurar inicio de clases…", "calendar-days", "ghost")
        self.term_btn.clicked.connect(self._configure_term_start)
        wtop.addWidget(self.weeks_chk)
        wtop.addWidget(self.weeks_hint, 1)
        wtop.addWidget(self.term_btn)
        wlay.addLayout(wtop)

        self.weeks_grid_host = QWidget()
        self.weeks_grid = QGridLayout(self.weeks_grid_host)
        self.weeks_grid.setContentsMargins(0, 2, 0, 0)
        self.weeks_grid.setSpacing(6)
        self.week_buttons: list[QPushButton] = []
        self._weeks_columns = 0
        for i in range(wk.WEEKS_IN_TERM):
            b = QPushButton(f"S{i + 1}")
            b.setCheckable(True)
            b.setProperty("variant", "week")
            b.setFixedHeight(30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.week_buttons.append(b)
        self._reflow_weeks()
        wlay.addWidget(self.weeks_grid_host)
        self.weeks_grid_host.hide()
        scope_lay.addWidget(self.weeks_box)
        lay.addWidget(scope)
        self._refresh_week_labels()

        # ── Estado de la búsqueda e inventario ────────────────────
        info = QHBoxLayout()
        info.setSpacing(12)
        self.scan_status = QLabel(
            "Busca en tu Blackboard las grabaciones que aún no has transcrito."
        )
        self.scan_status.setObjectName("h3")
        self.scan_status.setTextFormat(Qt.TextFormat.PlainText)
        self.scan_status.setWordWrap(True)
        info.addWidget(self.scan_status, 1)
        lay.addLayout(info)

        notice_row = QHBoxLayout()
        notice_row.setSpacing(8)
        notice_row.addWidget(widgets.icon_label("inbox", "FAINT", 15))
        self.backlog_notice = QLabel("Busca una vez para guardar el inventario de tus cursos.")
        self.backlog_notice.setObjectName("muted")
        self.backlog_notice.setWordWrap(True)
        backlog_btn = widgets.button("Ver pendientes", "arrow-right", "ghost")
        backlog_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        backlog_btn.clicked.connect(lambda: self._navigate(PAGE_BACKLOG))
        notice_row.addWidget(self.backlog_notice, 1)
        notice_row.addWidget(backlog_btn)
        lay.addLayout(notice_row)

        # ── Tabla de clases ───────────────────────────────────────
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["", "Curso", "Clase (editable)", "Sem.", "Fecha", "Estado"]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 44)
        self.table.setColumnWidth(3, 62)
        self.table.setColumnWidth(4, 110)
        self.table.setColumnWidth(5, 196)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setMouseTracking(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setItemDelegateForColumn(5, widgets.StatusDelegate(self.table))
        style.configure_table(self.table)
        self.table.setFixedHeight(style.HEADER_HEIGHT + 6 * style.ROW_HEIGHT + 2)
        self.classes_empty = widgets.EmptyState(
            "scan-search",
            "Aún no hay clases en esta vista",
            "Pulsa «Buscar clases nuevas» para revisar tu Blackboard. Los trabajos que "
            "dejaste a medias aparecen aquí automáticamente para continuarlos.",
        )
        self.classes_empty.setObjectName("card")
        self.classes_empty.setMinimumHeight(190)
        self.table.installEventFilter(self)
        self.table.hide()
        lay.addWidget(self.table)
        lay.addWidget(self.classes_empty)
        self.class_detail = QLabel("Selecciona una fila para ver el detalle de su estado.")
        self.class_detail.setObjectName("muted")
        self.class_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.class_detail.setWordWrap(True)
        self.class_detail.hide()
        self.table.cellClicked.connect(self._show_class_detail)
        lay.addWidget(self.class_detail)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.delete_chk = QCheckBox("Eliminar cada video de YouTube al obtener su texto")
        self.delete_chk.setChecked(True)
        self.delete_chk.setEnabled(False)
        self.delete_chk.setToolTip(
            "Obligatorio: después de verificar el TXT, se elimina la copia remota. Los fallos se reintentan automáticamente."
        )
        self.skip_all_btn = widgets.button("Omitir todo lo pendiente", "skip-forward", "ghost")
        self.skip_all_btn.setToolTip(
            "Marca TODAS las grabaciones pendientes como ya procesadas sin "
            "transcribirlas. Útil si empiezas a usar la app a mitad de ciclo "
            "y las clases viejas ya las tienes."
        )
        self.skip_all_btn.clicked.connect(self._skip_all_pending)
        self.skip_all_btn.hide()

        self.process_btn = widgets.button(
            "Procesar / continuar", "play", "primary",
            "Comprueba YouTube y continúa las clases marcadas desde el último paso guardado.",
        )  # fmt: skip
        self.process_btn.clicked.connect(self._process_queue)
        self.process_btn.hide()

        self.append_queue_btn = widgets.button(
            "Agregar a la cola activa", "plus", "primary",
            "Agrega las clases recién seleccionadas a la cola de transcripción en curso sin detenerla.",
        )  # fmt: skip
        self.append_queue_btn.clicked.connect(self._append_to_queue)
        self.append_queue_btn.hide()

        self.cancel_queue_btn = widgets.button("Cancelar", "square", "danger", token="ERROR")
        self.cancel_queue_btn.clicked.connect(self._cancel_queue)
        self.cancel_queue_btn.hide()
        lay.addWidget(self.delete_chk)
        bottom.addStretch()
        bottom.addWidget(self.skip_all_btn)
        bottom.addWidget(self.process_btn)
        bottom.addWidget(self.append_queue_btn)
        bottom.addWidget(self.cancel_queue_btn)
        lay.addLayout(bottom)
        return tab

    def _reflow_weeks(self) -> None:
        columns = 16 if self.width() >= 1180 else 8
        if columns == self._weeks_columns:
            return
        self._weeks_columns = columns
        while self.weeks_grid.count():
            self.weeks_grid.takeAt(0)
        for i, b in enumerate(self.week_buttons):
            self.weeks_grid.addWidget(b, i // columns, i % columns)

    def _build_material_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        card = QFrame()
        card.setObjectName("card")
        top = QHBoxLayout(card)
        top.setContentsMargins(16, 14, 16, 14)
        top.setSpacing(12)
        top.addWidget(widgets.icon_badge("folder-down", "LINK", 38))
        self.mat_status = QLabel(
            "Descarga a tu carpeta todo el material "
            "(PDFs, Words, PPTs…) de todos tus cursos, "
            "y detecta lo que te falta."
        )
        self.mat_status.setObjectName("muted")
        self.mat_status.setWordWrap(True)
        open_btn = widgets.button("Abrir carpeta", "folder-open", "secondary")
        open_btn.clicked.connect(self._open_materials_dir)
        self.mat_scan_btn = widgets.button("Buscar material", "search", "primary")
        self.mat_scan_btn.clicked.connect(self._scan_material)
        top.addWidget(self.mat_status, 1)
        top.addWidget(open_btn)
        top.addWidget(self.mat_scan_btn)
        lay.addWidget(card)

        self.mat_table = QTableWidget(0, 5)
        self.mat_table.setHorizontalHeaderLabels(
            ["Curso", "Archivos", "Locales", "Faltan", "Revisión"]
        )
        self.mat_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            self.mat_table.setColumnWidth(col, 88)
        self.mat_table.setColumnWidth(4, 190)
        self.mat_table.verticalHeader().setVisible(False)
        self.mat_table.setShowGrid(False)
        self.mat_table.setMouseTracking(True)
        self.mat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mat_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.mat_table.setItemDelegateForColumn(4, widgets.StatusDelegate(self.mat_table))
        style.configure_table(self.mat_table)
        self.mat_table.setFixedHeight(style.HEADER_HEIGHT + 5 * style.ROW_HEIGHT + 2)
        self.mat_table.hide()
        lay.addWidget(self.mat_table)

        bottom = QHBoxLayout()
        self.mat_dl_btn = widgets.button("Descargar lo que falta", "download", "primary")
        self.mat_dl_btn.clicked.connect(self._download_material)
        self.mat_dl_btn.hide()
        self.mat_cancel_btn = widgets.button("Cancelar", "square", "danger", token="ERROR")
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
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(8)

        lbl_url = QLabel("URL de la grabación de Zoom")
        lbl_url.setObjectName("h3")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://us02web.zoom.us/rec/share/…")
        lbl_name = QLabel("Nombre de la clase")
        lbl_name.setObjectName("h3")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Ej: Matematica_Clase01")
        self.recording_pass_edit = QLineEdit()
        self.recording_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.recording_pass_edit.setPlaceholderText("Opcional · clave de ESTA grabación de Zoom")
        lbl_url.setBuddy(self.url_edit)
        lbl_name.setBuddy(self.name_edit)
        lbl_pass = QLabel("Contraseña de la grabación · diferente del acceso a Blackboard")
        lbl_pass.setObjectName("h3")
        lbl_pass.setBuddy(self.recording_pass_edit)

        actions = QHBoxLayout()
        self.start_btn = widgets.button("Procesar clase", "play", "primary")
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn = widgets.button("Cancelar", "square", "danger", token="ERROR")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.hide()
        actions.addWidget(self.start_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch()

        lay.addWidget(lbl_url)
        lay.addWidget(self.url_edit)
        lay.addSpacing(10)
        lay.addWidget(lbl_name)
        lay.addWidget(self.name_edit)
        lay.addSpacing(10)
        lay.addWidget(lbl_pass)
        lay.addWidget(self.recording_pass_edit)
        lay.addSpacing(12)
        lay.addLayout(actions)
        outer.addWidget(card)
        outer.addStretch()
        return tab

    def _navigate(self, index: int) -> None:
        self._nav_buttons[index].setChecked(True)
        self.page_title.setText(PAGES[index][2])
        self.page_subtitle.setText(PAGES[index][3])
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
            self._page_fade.setDuration(160)
            self._page_fade.setStartValue(0.6)
            self._page_fade.setEndValue(1.0)
            self._page_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._page_fade.finished.connect(lambda: self.stack.setGraphicsEffect(None))
            self._page_fade.start()
        # Las vistas de consulta ocupan toda la altura; el trabajo muestra pasos + registro.
        full_height = index not in (PAGE_CLASSES, PAGE_MANUAL, PAGE_MATERIAL)
        self.body_widget.setVisible(not full_height)
        self._card.setSizePolicy(
            self._card.sizePolicy().horizontalPolicy(),
            QtSizePolicy.Expanding if full_height else QtSizePolicy.Preferred,
        )
        current = self.stack.widget(index)
        if current is self.library:
            self.library.refresh()
        elif current is self.backlog:
            self.backlog.refresh()
        elif current is self.health:
            self.health.refresh()
        elif current is getattr(self, "assistant", None):
            self.assistant.refresh()
        elif current is getattr(self, "home", None):
            self.home.refresh()

    def _search_backlog(self):
        if self._discovery_busy():
            return
        self._navigate(PAGE_CLASSES)
        self.weeks_chk.setChecked(False)
        self._scan()

    def _scan_from_home(self) -> None:
        if not self._nav_buttons[PAGE_CLASSES].isEnabled():
            return
        self._navigate(PAGE_CLASSES)
        self._scan()

    def _ask_about_source(self, source_path: str) -> None:
        """Abre el asistente con una sola clase o material como contexto."""
        self._navigate(PAGE_ASSISTANT)
        self.assistant.focus_on_path(source_path)

    def _refresh_backlog_notice(self):
        if self._closing:
            return
        from zoom2txt.workflow import DAILY_LIMIT, WorkLedger

        try:
            # Una sola lectura del inventario alimenta el aviso, el cupo y la portada.
            overview = WorkLedger(self.settings).overview()
            daily = overview["daily"]
            pending = len(overview["pending"])
            if not overview["courses"]:
                text = f"Hoy: {daily['used']}/{DAILY_LIMIT} clases iniciadas. Busca para guardar el inventario."
            else:
                text = f"{pending} pendientes guardadas · hoy {daily['used']}/{DAILY_LIMIT} iniciadas. "
                text += (
                    f"Nuevas clases desde {daily['next_day']} a las 00:00 (Lima)."
                    if daily["remaining"] == 0
                    else f"Puedes iniciar {daily['remaining']} nuevas hoy."
                )
                if overview["missing"]:
                    text += f" {overview['missing']} sesiones esperadas sin aparecer: revisa Pendientes."
                if overview["partial"]:
                    text += " Inventario parcial: vuelve a buscar."
            self.backlog_notice.setText(text)
            self._update_quota(daily, pending)
            if self.stack.currentWidget() is self.backlog:
                self.backlog.refresh()
            elif self.stack.currentWidget() is getattr(self, "home", None):
                self.home.refresh(overview)
        except Exception:  # noqa: BLE001 — resilient UI read boundary
            self.backlog_notice.setText(
                "No se pudo leer el inventario local. Vuelve a buscar; no se han borrado tus clases."
            )

    def _update_quota(self, daily: dict, pending: int) -> None:
        from zoom2txt.workflow import DAILY_LIMIT

        if not hasattr(self, "quota_ring"):
            return
        used = int(daily["used"])
        full = daily["remaining"] == 0
        self.quota_ring.set_value(used / DAILY_LIMIT, str(used), "WARNING" if full else "ACCENT")
        self.quota_title.setText(f"Hoy {used}/{DAILY_LIMIT} clases")
        self.quota_note.setText(
            f"Se renueva el {daily['next_day'][8:]}/{daily['next_day'][5:7]}"
            if full
            else f"Puedes iniciar {daily['remaining']} más"
        )
        self._nav_buttons[PAGE_BACKLOG].set_badge(
            str(pending) if pending else "", "WARNING" if pending else "ACCENT"
        )

    # ── Cursos y semanas académicas ───────────────────────────────
    def _set_courses(self, courses):
        previous = self.course_combo.currentData()
        previous_name = self.course_combo.currentText()
        self.course_combo.blockSignals(True)
        try:
            self.course_combo.clear()
            self.course_combo.addItem("Todos los cursos", None)
            for course in sorted(courses, key=lambda c: c["name"].casefold()):
                self.course_combo.addItem(course["name"], course["id"])
                self.course_combo.setItemData(
                    self.course_combo.count() - 1, course["name"], Qt.ItemDataRole.ToolTipRole
                )
            index = self.course_combo.findData(previous)
            if previous is not None and index < 0:
                # Keep the scope explicit until the user chooses another course.
                self.course_combo.addItem(
                    previous_name.removesuffix(" (no disponible)") + " (no disponible)", previous
                )
                index = self.course_combo.count() - 1
            self.course_combo.setCurrentIndex(max(0, index))
        finally:
            self.course_combo.blockSignals(False)
        self.course_combo.setToolTip(self.course_combo.currentText())
        self.course_hint.setText(
            f"{len(courses)} cursos del ciclo disponibles. El filtro por semana es opcional."
            if courses
            else "Pulsa Actualizar cursos para cargar los cursos de tu Blackboard."
        )

    def _on_course_changed(self):
        self.course_combo.setToolTip(self.course_combo.currentText())
        self._scan_items = []
        self._pending_backlog = 0
        self.scan_worker = None
        self.table.setRowCount(0)
        self.table.hide()
        self.class_detail.hide()
        self.process_btn.hide()
        self.skip_all_btn.hide()
        scope = self.course_combo.currentText()
        self.scan_status.setText(f"Pulsa Buscar clases nuevas para revisar: {scope}.")
        self._load_saved_work()

    def _refresh_courses(self):
        if self._closing or self._discovery_busy():
            return
        self.course_controls.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.course_hint.setText("Cargando cursos de Blackboard…")
        self._course_login_pending = False
        self.course_worker = CourseListWorker(self.settings, self)
        self.course_worker.log.connect(self._log)
        self.course_worker.found.connect(self._set_courses)
        self.course_worker.failed.connect(self._on_courses_failed)
        self.course_worker.needs_login.connect(self._request_course_login)
        self.course_worker.finished.connect(self._courses_finished)
        self.course_worker.start()

    def _request_course_login(self):
        self._course_login_pending = True

    def _courses_finished(self):
        self.course_controls.setEnabled(True)
        self.scan_btn.setEnabled(True)
        if self._course_login_pending and not self._closing:
            self._course_login_pending = False
            self.course_hint.setText("Conecta Blackboard para cargar tus cursos.")
            self._connect_blackboard(self._refresh_courses)

    def _on_courses_failed(self, error):
        self.course_hint.setText("No se pudieron actualizar los cursos. Vuelve a intentar.")
        self._log("[!] Cursos: " + error)

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
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)
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
        """Consola con color según el tipo de mensaje; también va al registro en disco."""
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
        if stripped:
            logs.write(text)

    def _set_activity(self, badge: str, text: str | None = None, tone: str = "", active: bool = False) -> None:
        """Estado global del panel de actividad (pastilla, punto y línea de detalle)."""
        self.dock_badge.setText(badge)
        widgets.set_tone(self.dock_badge, tone)
        self.status_dot.set_state(
            {"success": "SUCCESS", "warning": "WARNING", "error": "ERROR", "info": "ACCENT"}.get(
                tone, "FAINT"
            ),
            active,
        )
        if text is not None:
            self.dock_text.setFullText(text)

    def _open_settings(self) -> None:
        if not self.settings_btn.isEnabled():
            return
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

    def _open_palette(self) -> None:
        from zoom2txt.gui.command_palette import CommandPalette

        if getattr(self, "_palette", None) is not None and self._palette.isVisible():
            self._palette.close()
            return
        self._palette = CommandPalette(self, self._palette_actions(), self._palette_search)
        self._palette.open_centered()

    def _palette_actions(self) -> list[dict]:
        actions = []
        for index in (PAGE_HOME, PAGE_CLASSES, PAGE_MANUAL, PAGE_MATERIAL, PAGE_LIBRARY,
                      PAGE_ASSISTANT, PAGE_BACKLOG, PAGE_HEALTH):  # fmt: skip
            if self._nav_buttons[index].isEnabled():
                actions.append(
                    dict(
                        icon=PAGES[index][0],
                        title="Ir a " + PAGES[index][1],
                        hint=PAGES[index][3],
                        run=lambda index=index: self._navigate(index),
                    )
                )

        def add(icon, title, hint, run, enabled=True):
            if enabled:
                actions.append(dict(icon=icon, title=title, hint=hint, run=run))

        classes_ok = self._nav_buttons[PAGE_CLASSES].isEnabled()
        add("search", "Buscar clases nuevas", "Revisa Blackboard ahora (F5)",
            self._scan_from_home, classes_ok and self.scan_btn.isEnabled())
        add("refresh-cw", "Actualizar cursos", "Carga la lista de cursos de Blackboard",
            lambda: (self._navigate(PAGE_CLASSES), self._refresh_courses()), classes_ok)
        add("folder-down", "Buscar material de cursos", "Detecta adjuntos nuevos o modificados",
            lambda: (self._navigate(PAGE_MATERIAL), self._scan_material()),
            self._nav_buttons[PAGE_MATERIAL].isEnabled())
        add("history", "Abrir historial", "Omisiones y recuperación de trabajos",
            self._open_history, self.history_btn.isEnabled())
        add("settings", "Abrir ajustes", "Cuentas, carpetas y preferencias (Ctrl+,)",
            self._open_settings, self.settings_btn.isEnabled())
        add("moon" if style.current_mode == "dark" else "sun",
            "Cambiar a modo " + ("claro" if style.current_mode == "dark" else "oscuro"),
            "Alterna el tema de la aplicación", self._toggle_theme)
        add("folder-open", "Abrir carpeta de transcripciones", str(self.settings.transcripts_dir),
            lambda: self._open_folder(self.settings.transcripts_dir))
        add("folder-open", "Abrir carpeta de material", str(self.settings.materials_dir),
            lambda: self._open_folder(self.settings.materials_dir))
        add("folder-open", "Abrir carpeta de videos", str(self.settings.videos_dir),
            lambda: self._open_folder(self.settings.videos_dir))
        add("terminal", "Mostrar u ocultar el registro técnico", "Panel de actividad",
            self._toggle_console)
        add("file-text", "Abrir el registro de actividad en disco", "Diagnóstico sin credenciales",
            lambda: self._open_folder(logs.log_path().parent))
        return actions  # fmt: skip

    def _palette_search(self, query: str) -> list[dict]:
        """Resultados de texto completo en transcripciones y materiales indexados."""
        try:
            hits = self.assistant.engine.search_text(query, limit=12)
        except Exception:  # noqa: BLE001 — la búsqueda nunca debe romper la paleta
            return []
        results = []
        for hit in hits:
            where = " · ".join(x for x in (hit["course_name"], hit["locator"]) if x)
            results.append(
                dict(
                    icon="file-text" if hit["source_type"] != "transcript" else "captions",
                    title=hit["title"],
                    hint=where,
                    snippet=hit["snippet"],
                    run=lambda hit=hit: self._open_reader(hit["path"], hit["title"], query, hit["source_id"]),
                )
            )
        return results

    def _open_reader(self, path: str, title: str = "", query: str = "", source_id: int | None = None) -> None:
        from zoom2txt.gui.reader_dialog import ReaderDialog

        dialog = ReaderDialog(
            self.settings, Path(path), title=title, parent=self, query=query,
            engine=self.assistant.engine, source_id=source_id,
        )  # fmt: skip
        dialog.ask_requested.connect(self._ask_about_source)
        dialog.exec()

    @staticmethod
    def _open_folder(folder: Path) -> None:
        folder = Path(folder)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _recover(self, job):
        if self._busy():
            QMessageBox.information(
                self,
                "Trabajo en curso",
                "Ya hay una tarea activa. Espera o pulsa Pausar recuperación antes de continuar otra clase.",
            )
            return
        self._navigate(PAGE_CLASSES)
        self._reset_stages()
        self.recovery_worker = RecoveryWorker(self.settings, job, self)
        self._set_recovering(True)
        self.scan_status.setText(
            "Recuperación en curso. Consulta Progreso y Actividad; puedes pausarla."
        )
        self._set_activity("Recuperando", "Retomando el trabajo guardado…", "info", True)
        self.recovery_worker.log.connect(self._log)
        self.recovery_worker.stage.connect(self._on_stage)
        self.recovery_worker.progress.connect(self._on_progress)
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
            self._set_activity("Recuperada", message, "success")
            self._notify("Transcripción recuperada", "El TXT está disponible en Biblioteca.")
        else:
            failed = message.startswith("No se pudo continuar")
            if self._current_stage >= 0:
                self.stage_rows[self._current_stage].set_state("error" if failed else "pending")
            self._set_activity(
                "Por resolver" if failed else "Pendiente", message, "error" if failed else "warning"
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
        return get_zoom_password(self.settings.zoom_user) or ""

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
        self._navigate(PAGE_HEALTH)
        return False

    def _reset_stages(self) -> None:
        self.stepper.reset()
        self._current_stage = -1

    def _busy(self) -> bool:
        for w in (
            self.worker,
            self.course_worker,
            self.scan_worker,
            self.queue_worker,
            self.mat_scan_worker,
            self.mat_dl_worker,
            self.recovery_worker,
            self.cleanup_worker,
        ):
            if w and w.isRunning():
                return True
        return False

    def _transcription_busy(self) -> bool:
        for w in (self.worker, self.queue_worker, self.recovery_worker):
            if w and w.isRunning():
                return True
        return False

    def _material_busy(self) -> bool:
        for w in (self.mat_scan_worker, self.mat_dl_worker):
            if w and w.isRunning():
                return True
        return False

    def _discovery_busy(self) -> bool:
        for w in (self.course_worker, self.scan_worker):
            if w and w.isRunning():
                return True
        return False

    def _toggle_theme(self) -> None:
        new_mode = "light" if style.current_mode == "dark" else "dark"
        self.settings.theme = new_mode
        self.settings.save()
        app = QApplication.instance()
        if app:
            style.apply_theme(app, new_mode)
            widgets.set_icon(self.theme_toggle_btn, "sun" if new_mode == "dark" else "moon", "MUTED", 17)
            self.update()

    def _toggle_console(self) -> None:
        visible = not self.console.isVisible() if self.console.isVisibleTo(self.body_widget) or not self.body_widget.isVisible() else False
        if not self.body_widget.isVisible():
            visible = self.console.isHidden()
        self.console.setVisible(visible)
        self.toggle_console_btn.setChecked(visible)
        self.settings.activity_collapsed = not visible
        try:
            self.settings.save()
        except OSError:
            pass

    # ── Material de cursos ────────────────────────────────────────
    def _scan_material(self) -> None:
        if self._closing:
            return
        if self._material_busy():
            return
        self.mat_scan_btn.setEnabled(False)
        self.mat_dl_btn.hide()
        self.mat_status.setText("Revisando el material de tus cursos…")
        self.dock_material_status.setText("Revisando materiales…")
        self.mat_scan_worker = MaterialScanWorker(self.settings, self)
        self.mat_scan_worker.log.connect(self._log)
        self.mat_scan_worker.needs_login.connect(self._on_mat_needs_login)
        self.mat_scan_worker.found.connect(self._on_material_found)
        self.mat_scan_worker.failed.connect(self._on_material_failed)
        self.mat_scan_worker.start()

    def _on_mat_needs_login(self) -> None:
        self.mat_scan_btn.setEnabled(True)
        self.mat_status.setText("Inicia sesión para continuar.")
        self.dock_material_status.setText("")
        from zoom2txt.blackboard.client import BlackboardClient
        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = BlackboardLoginDialog(
            self.settings.blackboard_url, self, user=self.settings.zoom_user
        )
        if dialog.exec() == dialog.DialogCode.Accepted:
            BlackboardClient(self.settings.blackboard_url, self._log).store_cookies(dialog.cookies)
            self._log("[+] Sesión de Blackboard guardada.")
            self._scan_material()

    def _on_material_found(self, payload) -> None:
        rows, missing = payload
        self.mat_scan_btn.setEnabled(True)
        self.dock_material_status.setText("")
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
                if col and col < 4:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 3 and r["missing"]:
                    cell.setForeground(QColor(style.WARNING))
                if col == 4 and r.get("error"):
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
        self.dock_material_status.setText("")
        self._log(f"[✕] Error revisando material: {error}")
        QMessageBox.critical(self, "Error en material", error)

    def _download_material(self) -> None:
        if self._material_busy() or not getattr(self, "_material_missing", None):
            return
        self.mat_dl_btn.hide()
        self.mat_cancel_btn.show()
        self.mat_scan_btn.setEnabled(False)
        self.dock_material_status.setText(f"Descargando {len(self._material_missing)} archivos…")
        self.mat_dl_worker = MaterialDownloadWorker(self.settings, self._material_missing, self)
        self.mat_dl_worker.log.connect(self._log)
        self.mat_dl_worker.progress.connect(lambda i, t: self._on_mat_progress(i, t))
        self.mat_dl_worker.finished_all.connect(self._on_material_done)
        self.mat_dl_worker.finished.connect(self._scan_material)
        self.mat_dl_worker.start()

    def _on_mat_progress(self, current: int, total: int) -> None:
        self.mat_status.setText(f"Descargando… {current}/{total}")
        self.dock_material_status.setText(f"Materiales {current}/{total}")

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
        self.dock_material_status.setText(
            f"{ok} materiales listos" if not failed else f"{ok} listos · {failed} con errores"
        )
        self._log(f"[+] {msg}")
        self._notify("Material descargado", msg, "success" if not failed else "warning")

    def _open_materials_dir(self) -> None:
        self._open_folder(self.settings.materials_dir)

    # ── Modo automático ───────────────────────────────────────────
    def _scan(self) -> None:
        if self._closing:
            return
        if self._discovery_busy():
            return
        self.scan_btn.setEnabled(False)
        self._pending_backlog = 0
        self.scan_status.setText("Buscando en Blackboard…")
        ranges = self._selected_week_ranges()
        if self.weeks_chk.isChecked() and not ranges:
            self.scan_btn.setEnabled(True)
            self.scan_status.setText("Marca al menos una semana, o desactiva el filtro por semana.")
            return
        self.scan_worker = ScanWorker(
            self.settings, ranges, self, course_id=self.course_combo.currentData()
        )
        self.course_controls.setEnabled(False)
        self.weeks_box.setEnabled(False)
        self._scan_login_pending = False
        self._scan_items = []
        self.table.hide()
        self.process_btn.hide()
        self.skip_all_btn.hide()
        if not self._transcription_busy():
            self._set_activity("Buscando", "Revisando tus cursos en Blackboard…", "info", True)
        self.scan_worker.log.connect(self._log)
        self.scan_worker.needs_login.connect(self._request_scan_login)
        self.scan_worker.courses_loaded.connect(self._set_courses)
        self.scan_worker.found.connect(self._on_scan_found)
        self.scan_worker.capped.connect(self._on_scan_capped)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.finished.connect(self._scan_finished)
        self.scan_worker.start()

    def _request_scan_login(self):
        self._scan_login_pending = True

    def _scan_finished(self):
        self.course_controls.setEnabled(True)
        self.weeks_box.setEnabled(True)
        if not self._transcription_busy() and self.dock_badge.text() == "Buscando":
            self._set_activity("En espera", "Búsqueda terminada.")
        if self._scan_login_pending and not self._closing:
            self._scan_login_pending = False
            self._on_needs_login()

    def _on_scan_capped(self, shown: int, total: int) -> None:
        self._pending_backlog = total - shown
        self._log(
            f"[i] {total} pendientes en el filtro; {shown} listas en esta tanda. Las restantes siguen guardadas. Máximo de la app: 6 clases nuevas al día; no es una cuota de Google. Consulta Actividad si alguna no se pudo preparar."
        )

    def _on_needs_login(self) -> None:
        self.scan_btn.setEnabled(True)
        self.scan_status.setText(
            "Reconectando Blackboard; la sesión anterior caducó o aún no se ha configurado."
        )
        self._connect_blackboard(self._scan)

    def _connect_blackboard(self, retry):
        from zoom2txt.blackboard.client import BlackboardClient
        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = BlackboardLoginDialog(
            self.settings.blackboard_url, self, user=self.settings.zoom_user
        )
        if dialog.exec() == dialog.DialogCode.Accepted:
            client = BlackboardClient(self.settings.blackboard_url, self._log)
            try:
                client.store_cookies(dialog.cookies)
            finally:
                client.session.close()
            self._log("[+] Sesión de Blackboard guardada.")
            retry()

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
            self.classes_empty.set_content(
                "Sin pendientes en este filtro",
                "Estás al día con lo que hay publicado. Vuelve a buscar cuando tengas clases nuevas.",
                "circle-check",
            )
            return
        self.table.show()
        self.class_detail.show()
        self.class_detail.setText("Selecciona una fila para ver el detalle de su estado.")
        self.process_btn.show()
        self.skip_all_btn.show()
        ready = "1 clase lista." if len(items) == 1 else f"{len(items)} clases listas."
        self.scan_status.setText(
            ready + " La app comprobará YouTube y reutilizará tus archivos al continuar."
            + (
                " Revisión parcial: consulta Actividad."
                if getattr(self.scan_worker, "partial_errors", [])
                else ""
            )
        )
        from zoom2txt.blackboard.registry import recording_key
        from zoom2txt.storage import StateStore
        from zoom2txt.work_items import job_status

        state = StateStore(self.settings)
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
            job = state.get(
                "job", item.get("job_key") or recording_key(item["course_id"], item["meeting_id"])
            )
            self._set_class_status(row, job_status(job), job.get("error", ""))

    def _load_saved_work(self):
        if self._closing or self._busy():
            return
        from zoom2txt.work_items import local_continuations

        try:
            course_id = self.course_combo.currentData()
            items = local_continuations(
                self.settings, self._selected_week_ranges(), course_id=course_id
            )
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

    def _on_queue_waiting(self, index):
        row = self._queue_table_row(index)
        if row is not None:
            self._set_class_status(
                row,
                "Texto en proceso",
                "La clase ya está preparada. Las demás avanzan mientras se genera su transcripción.",
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
        if job.get("cleanup_pending"):
            self._set_class_status(
                row,
                "Eliminación pendiente",
                "TXT guardado. El borrado remoto se reintentará automáticamente; consulta Historial.",
            )
        elif job.get("status") == "waiting_captions" and not job.get("error"):
            self._on_queue_waiting(index)
        elif txt:
            self._set_class_status(row, "TXT verificado", "Texto disponible en tu biblioteca.")
            self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
            if getattr(self, "toasts", None) is not None and self.isVisible():
                self.toasts.notify("Transcripción lista", item.get("name", ""), "success", 4200)
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

    def _selected_for_queue(self, only_enabled: bool = False) -> list[tuple[int, dict]]:
        """Filas marcadas dentro del curso y las semanas elegidas, con su nombre editado."""
        ranges = self._selected_week_ranges()
        course_id = self.course_combo.currentData()
        selected: list[tuple[int, dict]] = []
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if not chk or chk.checkState() != Qt.CheckState.Checked:
                continue
            if only_enabled and not (chk.flags() & Qt.ItemFlag.ItemIsEnabled):
                continue  # ya fue agregada previamente a la cola
            item = dict(self._scan_items[row])
            if course_id is not None and item["course_id"] != course_id:
                continue
            if ranges and not any(lo <= item["start_time"][:10] <= hi for lo, hi in ranges):
                continue
            nombre = self.table.item(row, 2).text().strip()
            nombre = re.sub(r'[<>:"/\\|?*]', "_", nombre).strip(". ")
            item["name"] = nombre or item["name"]
            selected.append((row, item))
        return selected

    def _process_queue(self) -> None:
        if self.queue_worker and self.queue_worker.isRunning():
            self._append_to_queue()
            return
        if self._transcription_busy():
            QMessageBox.information(
                self,
                "Transcripción en curso",
                "Ya hay una tarea de transcripción o recuperación en curso. Espera a que termine.",
            )
            return
        ranges = self._selected_week_ranges()
        if self.weeks_chk.isChecked() and not ranges:
            self.scan_status.setText(
                "Marca al menos una semana o desactiva el filtro para continuar."
            )
            return
        selected = [item for _, item in self._selected_for_queue()]
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
        self.settings.auto_delete_youtube = True
        self.settings.save()
        self._launch_queue(selected, password or "")

    def _append_to_queue(self) -> None:
        if not (self.queue_worker and self.queue_worker.isRunning()):
            return
        to_add: list[dict] = []
        for row, item in self._selected_for_queue(only_enabled=True):
            to_add.append(item)
            chk = self.table.item(row, 0)
            chk.setFlags(chk.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsUserCheckable)
            self._set_class_status(row, "En espera…")

        if not to_add:
            QMessageBox.information(
                self,
                "Sin nuevas clases seleccionadas",
                "Marca una o más clases adicionales en la tabla para incorporarlas a la cola activa.",
            )
            return

        start_index = len(self._queue_items)
        self._queue_items.extend(to_add)
        self._queue_total += len(to_add)
        self.queue_worker.enqueue(to_add, start_index=start_index)
        self._log(f"[+] {len(to_add)} clase(s) agregadas dinámicamente a la cola activa en segundo plano.")
        self.scan_status.setText(f"{len(to_add)} clase(s) agregadas a la cola activa.")
        self.dock_text.setFullText(
            f"Cola ampliada (+{len(to_add)} clases). Total en tanda: {self._queue_total}"
        )

    def _launch_queue(self, selected: list, password: str) -> None:
        """Arranca la cola seleccionada desde la interfaz de escritorio."""
        self._queue_total = len(selected)
        self._queue_items = list(selected)
        self._active_queue_index = None
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                chk.setFlags(chk.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsUserCheckable)
        self._reset_stages()
        self.open_btn.hide()
        self.process_btn.hide()
        self.skip_all_btn.hide()
        self.append_queue_btn.show()
        self.cancel_queue_btn.show()
        self.scan_btn.setEnabled(False)
        self.course_controls.setEnabled(False)
        self._set_activity("Transcribiendo", f"Iniciando tanda ({len(selected)} clases)…", "info", True)

        self.queue_worker = QueueWorker(
            selected, password, self.settings, self.delete_chk.isChecked(), self
        )
        self.queue_worker.log.connect(self._log)
        self.queue_worker.stage.connect(self._on_stage)
        self.queue_worker.progress.connect(self._on_progress)
        self.queue_worker.round_wait.connect(self._on_round_wait)
        self.queue_worker.item_started.connect(self._on_queue_item)
        self.queue_worker.item_finished.connect(self._on_queue_item_finished)
        self.queue_worker.item_waiting.connect(self._on_queue_waiting)
        self.queue_worker.all_done.connect(self._on_queue_done)
        self.queue_worker.start()

    def _on_queue_item(self, index: int, name: str) -> None:
        self._active_queue_index = index
        row = self._queue_table_row(index)
        if row is not None:
            self._set_class_status(row, "Comprobando…")
        self.queue_label.setText(f"Clase {index + 1}: {name}")
        self.queue_label.show()
        self._set_activity(
            "Transcribiendo", f"Clase {index + 1}/{self._queue_total}: {name}", "info", True
        )
        self._reset_stages()

    def _cancel_queue(self) -> None:
        if self.queue_worker:
            self.queue_worker.cancel()
            self._log("[!] Cancelando cola… (se detendrá al terminar el paso actual)")
            self.cancel_queue_btn.setEnabled(False)
            self._set_activity("Pausando", "Se detendrá al terminar el paso actual.", "warning", True)

    def _on_queue_done(self, ok: int, failed: int) -> None:
        pending_count = max(0, self._queue_total - ok - failed)
        summary = f"{ok} completadas · {pending_count} pendientes · {failed} con problemas"
        self.queue_label.setText(summary)
        self.queue_label.show()
        self._active_queue_index = None
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if chk:
                chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        self.table.setEnabled(True)
        self.append_queue_btn.hide()
        self.cancel_queue_btn.hide()
        self.cancel_queue_btn.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.course_controls.setEnabled(True)
        self._set_activity(
            "Tanda lista", summary, "success" if not failed and not pending_count else "warning"
        )
        self._log("")
        self._log("============================================")
        self._log(
            f"Tanda revisada: {ok} completadas, {pending_count} pendientes, {failed} con problemas."
        )
        self._notify(
            "Tanda revisada",
            f"{ok} completadas · {pending_count} pendientes · {failed} con problemas.",
            "success" if not failed else "warning",
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
        if password is None:
            return
        if not self._ensure_pipeline_health():
            return

        self._reset_stages()
        self.open_btn.hide()
        self._result_path = None
        self._set_running(True)
        self._set_activity("Transcribiendo", nombre, "info", True)

        self.worker = PipelineWorker(
            url, self.recording_pass_edit.text() or password, nombre, self.settings, self
        )
        self.worker.log.connect(self._log)
        self.worker.stage.connect(self._on_stage)
        self.worker.progress.connect(self._on_progress)
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
        self.recording_pass_edit.setEnabled(not running)
        self.settings_btn.setEnabled(not running)
        self.scan_btn.setEnabled(not running)

    def _on_stage(self, index: int) -> None:
        active = getattr(self, "_active_queue_index", None)
        if active is not None:
            row = self._queue_table_row(active)
            if row is not None and 0 <= index < len(STAGE_STATUS):
                self._set_class_status(row, STAGE_STATUS[index])
        if 0 <= index < len(STAGE_STATUS):
            active_info = f"Clase {active + 1}: " if active is not None else ""
            self.dock_text.setFullText(f"{active_info}{STAGE_STATUS[index]}")
        self._current_stage = index
        for i, row in enumerate(self.stage_rows):
            row.set_state("done" if i < index else "active" if i == index else "pending")

    def _on_progress(self, stage: int, fraction: float, detail: str) -> None:
        """Avance real del paso activo (descarga, subida o consulta de subtítulos)."""
        if not 0 <= stage < len(self.stage_rows):
            return
        if self.stepper.state(stage) != "active":
            return
        self.stepper.set_progress(stage, None if fraction < 0 else fraction, detail)
        active = getattr(self, "_active_queue_index", None)
        prefix = f"Clase {active + 1}/{self._queue_total} · " if active is not None else ""
        percent = f" {fraction * 100:.0f}%" if fraction >= 0 else ""
        self.dock_text.setFullText(
            f"{prefix}{STAGE_SHORT[stage]}{percent}" + (f" · {detail}" if detail else "")
        )

    def _on_round_wait(self, seconds: int, pending: int) -> None:
        """Cuenta atrás hasta la próxima consulta de subtítulos de la tanda."""
        if seconds <= 0:
            return
        minutes, rest = divmod(int(seconds), 60)
        noun = "transcripción" if pending == 1 else "transcripciones"
        self._set_activity(
            "Esperando texto",
            f"{pending} {noun} en proceso en YouTube · próxima revisión en {minutes}:{rest:02d}",
            "info",
            True,
        )

    def _on_done(self, txt_path: str, video_id: str) -> None:
        self._refresh_backlog_notice()
        self._set_running(False)
        if not txt_path:
            self._log(
                "[i] Trabajo pendiente. Puedes continuar desde Historial sin repetir los pasos confirmados."
            )
            self._set_activity("Pendiente", "Continúa desde Historial sin repetir pasos.", "warning")
            return
        from zoom2txt.storage import StateStore

        jobs = [j for j in StateStore(self.settings).list("job") if j.get("video_id") == video_id]
        cleanup_pending = any(job.get("cleanup_pending") for job in jobs)
        for row in self.stage_rows:
            row.set_state("done")
        self._result_path = Path(txt_path)
        self.open_btn.show()
        if cleanup_pending:
            self.stage_rows[-1].set_state("error")
            self._log(
                "[!] TXT guardado; eliminación remota pendiente. Se reintentará automáticamente. Consulta Historial."
            )
            self._set_activity("Limpieza pendiente", self._result_path.name, "warning")
            self._notify(
                "Texto guardado · limpieza pendiente",
                "El TXT está disponible. La eliminación remota aún no se ha confirmado.",
                "warning",
            )
        else:
            self._log(
                f"[+] Clase completada. '{self._result_path.name}' está lista para NotebookLM; eliminación remota confirmada."
            )
            self._set_activity("Lista", self._result_path.name, "success")
            self._notify(
                "Transcripción lista", f"{self._result_path.name} está lista para NotebookLM."
            )

    def _on_failed(self, error: str) -> None:
        self._set_running(False)
        if 0 <= self._current_stage < len(self.stage_rows):
            self.stage_rows[self._current_stage].set_state("error")
        self._log(f"[✕] Error: {error}")
        self._set_activity("Por resolver", error, "error")
        QMessageBox.critical(self, "Algo salió mal", error)

    def _open_result(self) -> None:
        folder = self._result_path.parent if self._result_path else self.settings.transcripts_dir
        if folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event):
        assistant = getattr(self, "assistant", None)
        if assistant is not None and assistant.is_busy():
            assistant.cancel_all()
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
                    self.course_worker,
                    self.queue_worker,
                    self.scan_worker,
                    self.mat_scan_worker,
                    self.mat_dl_worker,
                    self.recovery_worker,
                    self.cleanup_worker,
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

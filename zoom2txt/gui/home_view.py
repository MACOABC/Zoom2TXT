"""Inicio: el estado del ciclo de un vistazo y el siguiente paso recomendado.

Solo lee el inventario local (lo que la última búsqueda guardó); no se conecta
a Blackboard, Zoom ni YouTube por sí mismo.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zoom2txt.blackboard import weeks
from zoom2txt.config import Settings
from zoom2txt.gui import widgets
from zoom2txt.workflow import DAILY_LIMIT, WorkLedger

PAGE_CLASSES, PAGE_LIBRARY, PAGE_BACKLOG, PAGE_HEALTH, PAGE_ASSISTANT = 0, 3, 4, 5, 6


def _thousands(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


class _PendingRow(QFrame):
    def __init__(self, course: str, date: str, week: int | None, status: str):
        super().__init__()
        self.setObjectName("plain")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 7, 0, 7)
        lay.setSpacing(10)
        lay.addWidget(widgets.icon_label("video", "FAINT", 16), 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(1)
        name = widgets.ElidedLabel(course)
        text.addWidget(name)
        try:
            year, month, day = date.split("-")
            shown = f"{day}/{month}/{year}"
        except ValueError:
            shown = date
        when = QLabel(shown + (f" · Semana {week}" if week else ""))
        when.setObjectName("faint")
        text.addWidget(when)
        lay.addLayout(text, 1)
        lay.addWidget(
            widgets.chip(status, widgets.status_tone(status)), 0, Qt.AlignmentFlag.AlignVCenter
        )


class HomeView(QWidget):
    scan_requested = Signal()
    continue_requested = Signal()
    navigate_requested = Signal(int)

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._columns = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        # ── Siguiente paso ────────────────────────────────────────
        hero = QFrame()
        hero.setObjectName("cardAccent")
        hero_lay = QHBoxLayout(hero)
        hero_lay.setContentsMargins(22, 20, 22, 20)
        hero_lay.setSpacing(18)
        copy = QVBoxLayout()
        copy.setSpacing(4)
        self.eyebrow = QLabel("TU CICLO")
        self.eyebrow.setObjectName("eyebrow")
        self.headline = QLabel("Empieza buscando tus clases")
        self.headline.setObjectName("h1")
        self.headline.setWordWrap(True)
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        copy.addWidget(self.eyebrow)
        copy.addWidget(self.headline)
        copy.addWidget(self.summary)
        hero_lay.addLayout(copy, 1)
        actions = QVBoxLayout()
        actions.setSpacing(8)
        self.scan_button = widgets.button("Buscar clases nuevas", "search", "primary")
        self.scan_button.setMinimumWidth(210)
        self.scan_button.clicked.connect(self.scan_requested.emit)
        self.continue_button = widgets.button("Continuar trabajos guardados", "play", "secondary")
        self.continue_button.clicked.connect(self.continue_requested.emit)
        self.continue_button.hide()
        actions.addStretch()
        actions.addWidget(self.scan_button)
        actions.addWidget(self.continue_button)
        actions.addStretch()
        hero_lay.addLayout(actions)
        lay.addWidget(hero)

        self.setup_banner = widgets.Banner("", "warning", "Ver estado")
        self.setup_banner.action.clicked.connect(lambda: self.navigate_requested.emit(PAGE_HEALTH))
        self.setup_banner.hide()
        lay.addWidget(self.setup_banner)

        # ── Indicadores ───────────────────────────────────────────
        self.kpi_host = QWidget()
        self.kpi_grid = QGridLayout(self.kpi_host)
        self.kpi_grid.setContentsMargins(0, 0, 0, 0)
        self.kpi_grid.setSpacing(12)
        self.kpi_pending = widgets.KpiCard("inbox", "Por transcribir", "WARNING")
        self.kpi_done = widgets.KpiCard("circle-check", "Clases transcritas", "SUCCESS")
        self.kpi_today = widgets.KpiCard("zap", "Iniciadas hoy", "LINK")
        self.kpi_sources = widgets.KpiCard("sparkles", "Fuentes en el asistente", "VIOLET")
        for card, page in (
            (self.kpi_pending, PAGE_BACKLOG),
            (self.kpi_done, PAGE_LIBRARY),
            (self.kpi_sources, PAGE_ASSISTANT),
        ):
            card.set_clickable(True)
            card.clicked.connect(lambda page=page: self.navigate_requested.emit(page))
        self._kpis = [self.kpi_pending, self.kpi_done, self.kpi_today, self.kpi_sources]
        lay.addWidget(self.kpi_host)

        # ── Ciclo y próximas clases ───────────────────────────────
        self.lower = QGridLayout()
        self.lower.setContentsMargins(0, 0, 0, 0)
        self.lower.setSpacing(12)
        self.cycle_card = widgets.Card("Avance por semana", "")
        self.week_strip = widgets.WeekStrip(weeks.WEEKS_IN_TERM)
        self.cycle_card.body.addWidget(self.week_strip)
        legend = QHBoxLayout()
        legend.setSpacing(8)
        legend.addWidget(widgets.chip("Completa", "success"))
        legend.addWidget(widgets.chip("Con pendientes", "warning"))
        legend.addWidget(widgets.chip("Sin clases publicadas"))
        legend.addStretch()
        self.cycle_card.body.addLayout(legend)
        self.cycle_card.body.addStretch()

        self.next_card = widgets.Card("Próximas por transcribir", "")
        self.next_link = widgets.button("Ver todas", "arrow-right", "ghost")
        self.next_link.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.next_link.clicked.connect(lambda: self.navigate_requested.emit(PAGE_BACKLOG))
        self.next_card.add_action(self.next_link)
        self.next_host = QWidget()
        self.next_list = QVBoxLayout(self.next_host)
        self.next_list.setContentsMargins(0, 0, 0, 0)
        self.next_list.setSpacing(0)
        self.next_card.body.addWidget(self.next_host)
        self.next_empty = widgets.EmptyState(
            "circle-check", "Estás al día", "No hay clases pendientes en el inventario guardado."
        )
        self.next_card.body.addWidget(self.next_empty)
        self.next_card.body.addStretch()
        lay.addLayout(self.lower)
        lay.addStretch()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._reflow()

    # ── Disposición adaptable ─────────────────────────────────────
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        columns = 4 if self.width() >= 900 else 2
        if columns == self._columns:
            return
        self._columns = columns
        while self.kpi_grid.count():
            self.kpi_grid.takeAt(0)
        for index, card in enumerate(self._kpis):
            self.kpi_grid.addWidget(card, index // columns, index % columns)
        for column in range(4):
            self.kpi_grid.setColumnStretch(column, 1 if column < columns else 0)
        while self.lower.count():
            self.lower.takeAt(0)
        if columns == 4:
            self.lower.addWidget(self.cycle_card, 0, 0)
            self.lower.addWidget(self.next_card, 0, 1)
            self.lower.setColumnStretch(0, 3)
            self.lower.setColumnStretch(1, 2)
        else:
            self.lower.addWidget(self.cycle_card, 0, 0)
            self.lower.addWidget(self.next_card, 1, 0)
            self.lower.setColumnStretch(0, 1)
            self.lower.setColumnStretch(1, 0)

    # ── Datos ─────────────────────────────────────────────────────
    def refresh(self, overview: dict | None = None) -> None:
        try:
            ledger = WorkLedger(self.settings)
            overview = overview or ledger.overview()
            stats = ledger.store.knowledge_stats()
            jobs = ledger.store.get_many("job")
        except Exception:  # noqa: BLE001 — la portada nunca debe impedir usar la app
            self.headline.setText("No se pudo leer el inventario local")
            self.summary.setText("Vuelve a buscar tus clases; no se ha borrado ningún dato.")
            return
        daily = overview["daily"]
        pending = overview["pending"]
        current = overview["current_week"]
        has_inventory = bool(overview["courses"])
        monday = weeks.parse_start(self.settings.term_start)

        self.eyebrow.setText(
            f"SEMANA {current} DE {weeks.WEEKS_IN_TERM}" if current else "TU CICLO"
        )
        waiting = sum(
            1 for job in jobs.values() if job.get("status") == "waiting_captions" and not job.get("txt")
        )
        resumable = sum(
            1
            for row in pending
            if jobs.get(row["id"], {}).get("video_id") or jobs.get(row["id"], {}).get("video")
        )
        self.continue_button.setVisible(bool(resumable))
        if not has_inventory:
            self.headline.setText("Empieza buscando tus clases")
            self.summary.setText(
                "La primera búsqueda guarda el inventario de tus cursos. Después verás aquí "
                "qué falta, cómo va cada semana y cuántas clases puedes iniciar hoy."
            )
        elif pending:
            noun = "clase por transcribir" if len(pending) == 1 else "clases por transcribir"
            self.headline.setText(f"Tienes {len(pending)} {noun}")
            parts = []
            if resumable:
                parts.append(
                    f"{resumable} ya {'tiene' if resumable == 1 else 'tienen'} trabajo guardado y "
                    "se retoman sin repetir pasos"
                )
            if waiting:
                parts.append(
                    f"{waiting} {'espera' if waiting == 1 else 'esperan'} subtítulos de YouTube"
                )
            parts.append(
                f"hoy puedes iniciar {daily['remaining']} más"
                if daily["remaining"]
                else f"el cupo de hoy está completo; continúa el {daily['next_day']}"
            )
            self.summary.setText(" ".join(part[0].upper() + part[1:] + "." for part in parts))
        else:
            self.headline.setText("Estás al día")
            self.summary.setText(
                "No hay clases pendientes en el inventario guardado. Busca de nuevo cuando "
                "tus profesores publiquen grabaciones."
            )
        if overview["partial"]:
            self.summary.setText(
                self.summary.text() + " El último inventario quedó incompleto: vuelve a buscar."
            )

        self.kpi_pending.set_value(
            str(len(pending)),
            "en el inventario guardado" if has_inventory else "busca para crear el inventario",
        )
        self.kpi_done.set_value(
            str(overview["transcribed"]), f"de {overview['inventory']} publicadas en el ciclo"
        )
        self.kpi_today.set_value(
            f"{daily['used']}/{DAILY_LIMIT}",
            f"puedes iniciar {daily['remaining']} más"
            if daily["remaining"]
            else f"se renueva el {daily['next_day']}",
        )
        self.kpi_sources.set_value(
            str(stats["sources"]),
            f"{_thousands(stats['words'])} palabras para consultar" if stats["words"] else "aún sin indexar",
        )

        self.week_strip.set_data(overview["by_week"], current)
        self.cycle_card.set_subtitle(
            "Clases transcritas sobre publicadas en cada semana del ciclo."
            if monday
            else "Configura el inicio de clases (en Clases → Configurar inicio) para ver tu avance."
        )
        self.week_strip.setVisible(bool(monday))

        while self.next_list.count():
            item = self.next_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        for index, row in enumerate(pending[:6]):
            job = jobs.get(row["id"], {})
            if job.get("status") == "waiting_captions":
                status = "Texto en proceso"
            elif job.get("video_id"):
                status = "Continuar"
            elif job.get("video"):
                status = "Video en esta PC"
            else:
                status = "Pendiente"
            week = weeks.week_of_str(monday, row["start_time"]) if monday else None
            if index:
                line = widgets.divider()
                self.next_list.addWidget(line)
                line.show()
            entry = _PendingRow(
                row.get("course_name", "Curso"), row["start_time"][:10], week, status
            )
            self.next_list.addWidget(entry)
            entry.show()
        self.next_host.setVisible(bool(pending))
        self.next_empty.setVisible(not pending)
        self.next_link.setVisible(len(pending) > 6)
        self.next_card.set_subtitle(
            f"{len(pending)} en total · las más antiguas primero" if pending else ""
        )
        self._check_setup()

    def _check_setup(self) -> None:
        if type(self.window()).__name__ == "DemoMainWindow":
            self.setup_banner.hide()
            return
        try:
            from zoom2txt.config import credentials_status

            missing = credentials_status(self.settings)
        except Exception:  # noqa: BLE001 — el almacén de credenciales varía por sistema
            missing = []
        if missing:
            labels = {
                "blackboard": "la dirección de tu Blackboard",
                "zoom": "tu usuario del campus",
                "client_secrets": "la credencial de Google",
                "token": "la autorización de YouTube",
            }
            self.setup_banner.show_message(
                "Falta completar la configuración: "
                + ", ".join(labels.get(item, item) for item in missing)
                + ". Al procesar se abrirá el asistente para terminarla.",
                "warning",
            )
        else:
            self.setup_banner.hide()

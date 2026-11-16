"""Pestaña «Asistente IA (Cursos)» estilo NotebookLM: consulta transcripciones y materiales
de estudio locales con blindaje de contexto estricto, costo $0 (Gemini Free Tier BYOK en la nube)
y cero consumo de modelos pesados en la PC del estudiante."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zoom2txt.config import (
    AI_STUDIO_APIKEY_URL,
    FREE_TIER_MODELS,
    Settings,
    save_gemini_api_key,
)
from zoom2txt.gui import lucide, style
from zoom2txt.gui.workers import (
    GeminiConnectionTestWorker,
    RagIngestFilesWorker,
    RagQueryWorker,
    RagSyncWorker,
)
from zoom2txt.rag_engine import RAGEngine, RAGResponse, infer_source_week
from zoom2txt.storage import StateStore, atomic_write

QUICK_STUDY_PROMPTS: tuple[tuple[str, str, str], ...] = (
    (
        "📝 Resumen de clase",
        "Genera un resumen ejecutivo de las clases y materiales seleccionados",
        "Haz un resumen estructurado con los puntos más importantes explicados en las fuentes seleccionadas, citando cada clase o documento.",
    ),
    (
        "❓ Preguntas de examen",
        "Genera preguntas de repaso tipo examen con respuestas",
        "Genera 5 preguntas de repaso tipo examen con sus respectivas respuestas detalladas basadas estrictamente en lo que el profesor enfatizó en estas fuentes.",
    ),
    (
        "📌 Conceptos clave",
        "Extrae definiciones, reglas y fórmulas mencionadas",
        "Extrae un glosario con todos los conceptos clave, definiciones, reglas o fórmulas explicadas en las fuentes seleccionadas.",
    ),
    (
        "📅 Fechas y entregas",
        "Busca fechas de evaluaciones, tareas o avisos del profesor",
        "¿Qué fechas, evaluaciones, tareas, advertencias o recomendaciones para el examen mencionó el profesor en estas clases?",
    ),
)


def render_markdown_to_html(text: str) -> str:
    """Convierte Markdown (negritas, listas, bloques de código y citas [Curso: ... | Fuente: ...])
    a HTML estilizado acorde al tema activo de Zoom2TXT."""
    if not text:
        return ""

    # Extraer bloques de código ```...``` primero para no alterarlos
    code_blocks: list[str] = []

    def _save_code_block(match: re.Match) -> str:
        code = html.escape(match.group(2).strip("\n"))
        block_html = (
            f'<pre style="background-color:{style.CONSOLE_BG}; color:{style.CONSOLE_FG}; '
            f"border:1px solid {style.BORDER}; border-radius:6px; padding:10px; "
            f'font-family:{style.FONT_MONO}; font-size:12.5px; margin:8px 0;">{code}</pre>'
        )
        code_blocks.append(block_html)
        return f"@@CODE_BLOCK_{len(code_blocks) - 1}@@"

    processed = re.sub(r"```([A-Za-z0-9_-]*)\n?(.*?)```", _save_code_block, text, flags=re.S)

    lines = processed.splitlines()
    out_lines: list[str] = []
    in_list = False

    def _format_inline(line_str: str) -> str:
        escaped = html.escape(line_str)
        # Código inline `...`
        escaped = re.sub(
            r"`([^`]+)`",
            rf'<code style="background-color:{style.SURFACE_3}; padding:2px 5px; border-radius:4px; font-family:{style.FONT_MONO}; font-size:12.5px;">\1</code>',
            escaped,
        )
        # Negrita **...**
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        # Cursiva *...*
        escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", escaped)
        # Etiquetas de citación [Curso: ... | Fuente: ...] o [Curso | Fuente]
        escaped = re.sub(
            r"\[([^\[\]]+?\|[^\[\]]+?)\]",
            rf'<span style="background-color:{style.ACCENT_SUBTLE}; color:{style.LINK}; '
            rf'border:1px solid {style.BORDER_HI}; border-radius:4px; padding:1px 6px; font-size:12px; font-weight:600;">[\1]</span>',
            escaped,
        )
        return escaped

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("@@CODE_BLOCK_") and stripped.endswith("@@"):
            if in_list:
                out_lines.append("</ul>")
                in_list = False
            idx = int(stripped.removeprefix("@@CODE_BLOCK_").removesuffix("@@"))
            out_lines.append(code_blocks[idx])
            continue

        if not stripped:
            if in_list:
                out_lines.append("</ul>")
                in_list = False
            out_lines.append("<br>")
            continue

        if stripped.startswith(("- ", "* ", "• ")):
            if not in_list:
                out_lines.append('<ul style="margin:4px 0 4px 18px;">')
                in_list = True
            out_lines.append(f'<li style="margin-bottom:4px;">{_format_inline(stripped[2:])}</li>')
            continue

        num_match = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if num_match:
            if not in_list:
                out_lines.append('<ul style="margin:4px 0 4px 18px;">')
                in_list = True
            out_lines.append(
                f'<li style="margin-bottom:4px;"><b>{num_match.group(1)}.</b> {_format_inline(num_match.group(2))}</li>'
            )
            continue

        if in_list:
            out_lines.append("</ul>")
            in_list = False

        if stripped.startswith("### "):
            out_lines.append(
                f'<div style="font-size:14.5px; font-weight:700; margin-top:8px; color:{style.LINK};">{_format_inline(stripped[4:])}</div>'
            )
        elif stripped.startswith("## "):
            out_lines.append(
                f'<div style="font-size:15.5px; font-weight:700; margin-top:10px; color:{style.LINK};">{_format_inline(stripped[3:])}</div>'
            )
        elif stripped.startswith("# "):
            out_lines.append(
                f'<div style="font-size:16.5px; font-weight:700; margin-top:10px; color:{style.LINK};">{_format_inline(stripped[2:])}</div>'
            )
        else:
            out_lines.append(
                f'<div style="margin-bottom:4px; line-height:1.45;">{_format_inline(stripped)}</div>'
            )

    if in_list:
        out_lines.append("</ul>")

    result = "\n".join(out_lines)
    for i, block in enumerate(code_blocks):
        result = result.replace(f"@@CODE_BLOCK_{i}@@", block)
    return result


class ChatInputEdit(QPlainTextEdit):
    """Área de texto que envía con Enter y permite salto de línea con Shift+Enter."""

    def __init__(self, on_submit, parent=None):
        super().__init__(parent)
        self._on_submit = on_submit
        self.setPlaceholderText(
            "Pregunta sobre tus clases o materiales… (Enter para enviar · Shift+Enter para salto de línea)"
        )
        self.setFixedHeight(74)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                event.accept()
                self._on_submit()
                return
        super().keyPressEvent(event)


class ApiKeyGuideDialog(QDialog):
    """Guía visual paso a paso dentro de la app para obtener y configurar la GEMINI_API_KEY gratuita."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cómo configurar el Asistente IA Gratis (Guía de 1 minuto)")
        self.setMinimumWidth(540)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        title = QLabel("🔑 Configura tu Asistente IA en 1 minuto (Costo $0)")
        title.setObjectName("h2")
        title.setWordWrap(True)
        lay.addWidget(title)

        guide_browser = QTextBrowser()
        guide_browser.setOpenExternalLinks(True)
        guide_browser.setMinimumHeight(290)
        guide_browser.setHtml(
            f'<div style="color:{style.TEXT}; font-size:13px; line-height:1.55;">'
            f"<p>Zoom2TXT utiliza la capa gratuita en la nube de <b>Google AI Studio (BYOK)</b>. "
            f"No necesitas pagar suscripciones, no pide tarjeta de crédito y <b>no consume RAM ni procesador en tu PC</b>:</p>"
            f"<ol>"
            f'<li style="margin-bottom:8px;"><b>Abre Google AI Studio:</b> Entra a '
            f'<a href="{AI_STUDIO_APIKEY_URL}" style="color:{style.LINK};">{AI_STUDIO_APIKEY_URL}</a> '
            f"iniciando sesión con tu cuenta personal de Google (<code>@gmail.com</code>).<br>"
            f'<span style="color:{style.MUTED}; font-size:12px;">💡 Tip: Usa tu Gmail personal; los correos institucionales de universidad suelen tener bloqueado Google AI Studio.</span></li>'
            f'<li style="margin-bottom:8px;"><b>Crea tu clave gratuita:</b> Haz clic en el botón azul '
            f"<b>«Create API key»</b> (si es tu primera vez, acepta los términos y elige "
            f"<i>«Create API key in new project»</i>).</li>"
            f'<li style="margin-bottom:8px;"><b>Cópiala y pégala en Zoom2TXT:</b> Copia la clave generada '
            f"(empieza con <code>AIzaSy…</code>), pégala en el campo <b>GEMINI_API_KEY</b> de la barra superior "
            f"(o en <i>Ajustes → Asistente IA</i>) y pulsa <b>«Probar Conexión API»</b>.</li>"
            f'<li style="margin-bottom:8px;"><b>Sincroniza y pregunta:</b> Pulsa '
            f"<b>«Sincronizar / Añadir Materiales»</b> para indexar tus clases transcritas o subir "
            f"diapositivas (<code>.pptx</code>), lecturas (<code>.pdf</code>) y guías (<code>.docx</code>). "
            f"Selecciona un curso o activa <b>«🌐 Consultar Todos los Cursos»</b> y escribe tu duda.</li>"
            f"</ol>"
            f'<div style="background-color:{style.SURFACE_2}; border:1px solid {style.BORDER}; '
            f'border-radius:6px; padding:8px 10px; font-size:12px; color:{style.MUTED};">'
            f"🔒 <b>Privacidad y Ligereza:</b> Tu clave se guarda cifrada únicamente en tu computadora, tus fuentes "
            f"se indexan en SQLite local y toda la inferencia de IA corre gratis en la nube de Google."
            f"</div>"
            f"</div>"
        )
        lay.addWidget(guide_browser)

        btn_row = QHBoxLayout()
        open_btn = QPushButton("🌐 Abrir Google AI Studio ahora")
        open_btn.setProperty("variant", "primary")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AI_STUDIO_APIKEY_URL)))
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        btn_row.addWidget(open_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_box)
        lay.addLayout(btn_row)


class ApiKeyConfigDialog(QDialog):
    """Diálogo modal limpio para configurar la clave de Gemini sin ensuciar la pantalla principal."""

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Configurar Asistente IA (Gemini)")
        self.setMinimumWidth(520)
        self._test_worker: GeminiConnectionTestWorker | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        title = QLabel("🤖 Configuración del Asistente IA")
        title.setObjectName("h2")
        lay.addWidget(title)

        info = QLabel(
            "Una sola clave gratuita sirve para todos los modelos (Flash, Flash-Lite, Pro, etc.). "
            "No necesitas tarjeta de crédito y la inferencia corre en la nube sin consumir la batería ni RAM de tu PC."
        )
        info.setObjectName("muted")
        info.setWordWrap(True)
        lay.addWidget(info)

        form = QFormLayout()
        form.setSpacing(8)

        self.key_edit = QLineEdit(self.settings.gemini_api_key)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("AIzaSy… o AQ… (clave de Google AI Studio)")
        form.addRow("GEMINI_API_KEY:", self.key_edit)

        self.semantic_chk = QCheckBox("Activar embeddings semánticos híbridos (opcional)")
        self.semantic_chk.setChecked(self.settings.use_semantic_embeddings)
        form.addRow("", self.semantic_chk)
        lay.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.get_key_btn = QPushButton("🔑 Obtener clave gratis (1 min)")
        self.get_key_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AI_STUDIO_APIKEY_URL)))
        self.test_btn = QPushButton("Probar Conexión")
        self.test_btn.clicked.connect(self._test_connection)
        btn_row.addWidget(self.get_key_btn)
        btn_row.addWidget(self.test_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("muted")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.hide()
        lay.addWidget(self.status_lbl)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _test_connection(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            self.status_lbl.setText("Ingresa una clave antes de probar.")
            self.status_lbl.setStyleSheet(f"color: {style.WARNING};")
            self.status_lbl.show()
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Probando…")
        self.status_lbl.setText("Conectando con Google AI Studio…")
        self.status_lbl.setStyleSheet(f"color: {style.MUTED};")
        self.status_lbl.show()
        self._test_worker = GeminiConnectionTestWorker(key, self.settings.gemini_model, self)
        self._test_worker.finished_test.connect(self._on_test_finished)
        self._test_worker.start()

    def _on_test_finished(self, ok: bool, message: str) -> None:
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Probar Conexión")
        self.status_lbl.setText(message)
        if ok:
            self.status_lbl.setStyleSheet(f"color: {style.SUCCESS}; font-weight: 600;")
        else:
            self.status_lbl.setStyleSheet(f"color: {style.ERROR}; font-weight: 600;")

    def _save_and_accept(self) -> None:
        key = self.key_edit.text().strip()
        self.settings.gemini_api_key = key
        save_gemini_api_key(key)
        self.settings.use_semantic_embeddings = self.semantic_chk.isChecked()
        self.settings.save()
        self.accept()


class AssistantView(QWidget):
    """Panel principal del Asistente de Estudio con IA en la Nube (estilo NotebookLM · $0 · 0% IA local)."""

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.store = StateStore(self.settings)
        self.engine = RAGEngine(self.settings, self.store)

        self.sync_worker: RagSyncWorker | None = None
        self.ingest_worker: RagIngestFilesWorker | None = None
        self.query_worker: RagQueryWorker | None = None
        self.test_worker: GeminiConnectionTestWorker | None = None
        self.demo_responder: Callable[[str, list[int]], RAGResponse] | None = None

        self._initial_sync_done = False
        self._updating_tree = False
        self._current_session_id: int | None = None
        self._countdown_remaining = 0

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick_countdown)

        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(12)

        # ── 1. Barra Superior Minimalista (Scope & Materiales) ─────────
        scope_card = QFrame()
        scope_card.setObjectName("card")
        scope_lay = QHBoxLayout(scope_card)
        scope_lay.setContentsMargins(14, 10, 14, 10)
        scope_lay.setSpacing(10)

        course_lbl = QLabel("📚 Curso:")
        course_lbl.setObjectName("h2")
        self.course_combo = QComboBox()
        self.course_combo.setAccessibleName("Curso para consultar en el Asistente IA")
        self.course_combo.setMinimumWidth(220)
        self.course_combo.setSizePolicy(
            self.course_combo.sizePolicy().horizontalPolicy().Expanding,
            self.course_combo.sizePolicy().verticalPolicy(),
        )
        self.course_combo.currentIndexChanged.connect(self._on_scope_changed)
        course_lbl.setBuddy(self.course_combo)

        self.week_filter_combo = QComboBox()
        self.week_filter_combo.setAccessibleName("Filtro rápido por semanas del ciclo")
        self.week_filter_combo.setToolTip(
            "Filtra automáticamente qué semanas marcar en la lista de fuentes (ideal para estudiar Parcial o Final)."
        )
        self.week_filter_combo.addItem("📅 Todas las semanas", "all")
        self.week_filter_combo.addItem("📅 Semanas 1–7 (Parcial)", "parcial")
        self.week_filter_combo.addItem("📅 Semanas 8–16 (Final)", "final")
        self.week_filter_combo.currentIndexChanged.connect(self._on_week_filter_changed)

        self.all_courses_btn = QPushButton("🌐 Todos los Cursos")
        self.all_courses_btn.setCheckable(True)
        self.all_courses_btn.setProperty("variant", "week")
        self.all_courses_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_courses_btn.setToolTip(
            "Activa el modo global para hacer preguntas combinadas sobre todos tus cursos."
        )
        self.all_courses_btn.toggled.connect(self._on_all_courses_toggled)

        # Botón amigable para gestionar materiales sin complicaciones
        self.materials_menu_btn = QPushButton("📂 Mis Materiales")
        self.materials_menu_btn.setToolTip("Abre la carpeta de lecturas o añade diapositivas y PDFs.")
        self.materials_menu = QMenu(self)
        open_folder_act = self.materials_menu.addAction("📂 Abrir carpeta de materiales en el explorador")
        open_folder_act.triggered.connect(self._open_materials_folder)
        add_files_act = self.materials_menu.addAction("➕ Añadir archivos (PDF, PPTX, Word, TXT)...")
        add_files_act.triggered.connect(self._on_sync_or_add_materials)
        sync_now_act = self.materials_menu.addAction("🔄 Buscar clases y lecturas nuevas ahora")
        sync_now_act.triggered.connect(self.sync_transcripts)
        self.materials_menu_btn.setMenu(self.materials_menu)

        # Botón de configuración de IA discreto y elegante
        self.api_status_btn = QPushButton("⚙️ Configuración IA")
        self.api_status_btn.clicked.connect(self._open_api_settings_dialog)

        # Controles funcionales y de compatibilidad con pruebas unitarias
        self.sync_btn = QPushButton("Sincronizar / Añadir Materiales")
        self.sync_btn.clicked.connect(self._on_sync_or_add_materials)
        self.sync_btn.hide()
        self.rescan_btn = QPushButton("Re-escanear TXT")
        self.rescan_btn.clicked.connect(self.sync_transcripts)
        self.rescan_btn.hide()

        self.api_key_edit = QLineEdit(self.settings.gemini_api_key)
        self.api_key_edit.hide()
        self.api_key_edit.editingFinished.connect(self._save_inline_api_key)

        self.test_api_btn = QPushButton("Probar Conexión API")
        self.test_api_btn.clicked.connect(self._test_api_connection)
        self.test_api_btn.hide()

        self.semantic_chk = QCheckBox("Embeddings (768d)")
        self.semantic_chk.setChecked(self.settings.use_semantic_embeddings)
        self.semantic_chk.toggled.connect(self._on_semantic_toggled)
        self.semantic_chk.hide()

        self.get_key_btn = QPushButton("Obtener API Key gratis (1 min)")
        self.get_key_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AI_STUDIO_APIKEY_URL)))
        self.get_key_btn.hide()

        self.guide_btn = QPushButton("❓ Cómo configurar")
        self.guide_btn.clicked.connect(self._show_api_guide)
        self.guide_btn.hide()

        scope_lay.addWidget(course_lbl)
        scope_lay.addWidget(self.course_combo, 1)
        scope_lay.addWidget(self.week_filter_combo)
        scope_lay.addWidget(self.all_courses_btn)
        scope_lay.addWidget(self.materials_menu_btn)
        scope_lay.addWidget(self.api_status_btn)

        outer.addWidget(scope_card)

        # Barra de progreso y status discretos
        self.progress_bar = QProgressBar()
        self.progress_bar.hide()
        outer.addWidget(self.progress_bar)

        self.scope_status = QLabel("")
        self.scope_status.hide()

        # ── 2. Cuerpo Dividido: Fuentes (Izquierda) + Chat (Centro) ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Panel Lateral Izquierdo ("Fuentes del Curso")
        sources_card = QFrame()
        sources_card.setObjectName("card")
        sources_card.setMinimumWidth(260)
        sources_card.setMaximumWidth(380)
        sources_lay = QVBoxLayout(sources_card)
        sources_lay.setContentsMargins(12, 12, 12, 12)
        sources_lay.setSpacing(8)

        sources_header = QHBoxLayout()
        sources_title = QLabel("Fuentes del Curso")
        sources_title.setObjectName("h2")
        self.sources_count_lbl = QLabel("0 activas")
        self.sources_count_lbl.setObjectName("faint")
        sources_header.addWidget(sources_title)
        sources_header.addStretch()
        sources_header.addWidget(self.sources_count_lbl)
        sources_lay.addLayout(sources_header)

        sources_actions = QHBoxLayout()
        self.check_all_btn = QPushButton("Marcar todas")
        self.check_all_btn.clicked.connect(lambda: self._set_all_sources_checked(True))
        self.uncheck_all_btn = QPushButton("Desmarcar todas")
        self.uncheck_all_btn.clicked.connect(lambda: self._set_all_sources_checked(False))
        sources_actions.addWidget(self.check_all_btn)
        sources_actions.addWidget(self.uncheck_all_btn)
        sources_lay.addLayout(sources_actions)

        self.sources_tree = QTreeWidget()
        self.sources_tree.setHeaderHidden(True)
        self.sources_tree.setRootIsDecorated(True)
        self.sources_tree.setIndentation(16)
        self.sources_tree.itemChanged.connect(self._on_source_item_changed)
        sources_lay.addWidget(self.sources_tree, 1)

        self.open_folder_btn = QPushButton("📂 Abrir carpeta de mis materiales")
        self.open_folder_btn.setToolTip(
            "Abre la carpeta en tu explorador de archivos. Puedes arrastrar aquí PDFs, Word o diapositivas y la app los leerá sola."
        )
        self.open_folder_btn.clicked.connect(self._open_materials_folder)
        sources_lay.addWidget(self.open_folder_btn)

        sources_hint = QLabel(
            "💡 Marca o desmarca qué clases o lecturas incluir. Si no marcas ninguna, la IA responderá con conocimiento general."
        )
        sources_hint.setObjectName("faint")
        sources_hint.setWordWrap(True)
        sources_lay.addWidget(sources_hint)

        splitter.addWidget(sources_card)

        # Panel Central de Chat e Interacción
        chat_card = QFrame()
        chat_card.setObjectName("card")
        chat_lay = QVBoxLayout(chat_card)
        chat_lay.setContentsMargins(14, 12, 14, 12)
        chat_lay.setSpacing(8)

        chat_header = QHBoxLayout()
        self.chat_title = QLabel("Conversación de Estudio")
        self.chat_title.setObjectName("h2")
        self.export_btn = QPushButton("Exportar MD/TXT")
        self.export_btn.setIcon(lucide.icon("download", style.MUTED, 15))
        self.export_btn.clicked.connect(self._export_chat)
        self.clear_btn = QPushButton("Limpiar chat")
        self.clear_btn.clicked.connect(self._clear_chat)
        chat_header.addWidget(self.chat_title, 1)
        chat_header.addWidget(self.export_btn)
        chat_header.addWidget(self.clear_btn)
        chat_lay.addLayout(chat_header)

        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(True)
        self.chat_view.setMinimumHeight(260)
        chat_lay.addWidget(self.chat_view, 1)

        # Aviso de estado / cuenta regresiva 429
        self.rate_limit_banner = QLabel("")
        self.rate_limit_banner.setWordWrap(True)
        self.rate_limit_banner.hide()
        chat_lay.addWidget(self.rate_limit_banner)

        self.analyzing_label = QLabel("🔎 Analizando fuentes y generando respuesta…")
        self.analyzing_label.setObjectName("accent")
        self.analyzing_label.hide()
        chat_lay.addWidget(self.analyzing_label)

        # Botones de estudio rápido (1 clic)
        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        self.quick_prompt_btns: list[QPushButton] = []
        for label, tooltip_text, prompt_text in QUICK_STUDY_PROMPTS:
            qbtn = QPushButton(label)
            qbtn.setToolTip(tooltip_text)
            qbtn.clicked.connect(
                lambda _checked=False, p=prompt_text: self._trigger_quick_prompt(p)
            )
            quick_row.addWidget(qbtn)
            self.quick_prompt_btns.append(qbtn)
        quick_row.addStretch()
        chat_lay.addLayout(quick_row)

        # Barra inferior de controles estilo Gemini (justo antes del cuadro de escribir)
        bottom_controls = QHBoxLayout()
        bottom_controls.setSpacing(8)

        model_icon = QLabel("✨ Modelo:")
        model_icon.setObjectName("muted")
        self.model_combo = QComboBox()
        for m in FREE_TIER_MODELS:
            self.model_combo.addItem(f"{m}", m)
        idx = self.model_combo.findData(self.settings.gemini_model)
        self.model_combo.setCurrentIndex(max(0, idx))
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.model_combo.setToolTip("Elige qué modelo de Gemini responde tu pregunta (100% gratis en la nube).")

        self.sources_indicator_lbl = QLabel("📚 Fuentes activas")
        self.sources_indicator_lbl.setObjectName("faint")

        bottom_controls.addWidget(model_icon)
        bottom_controls.addWidget(self.model_combo)
        bottom_controls.addWidget(self.sources_indicator_lbl)
        bottom_controls.addStretch()
        chat_lay.addLayout(bottom_controls)

        # Área de entrada de texto
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.input_edit = ChatInputEdit(self._send_question)
        self.send_btn = QPushButton("Preguntar")
        self.send_btn.setProperty("variant", "primary")
        self.send_btn.setFixedHeight(74)
        self.send_btn.clicked.connect(self._send_question)
        input_row.addWidget(self.input_edit, 1)
        input_row.addWidget(self.send_btn)
        chat_lay.addLayout(input_row)

        splitter.addWidget(chat_card)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, 1)

    # ── Ciclo de vida y sincronización ────────────────────────────
    def refresh(self) -> None:
        """Actualiza la lista de cursos, fuentes y mensajes; sincroniza transcripciones al abrir."""
        self.api_key_edit.setText(self.settings.gemini_api_key)
        idx = self.model_combo.findData(self.settings.gemini_model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.semantic_chk.setChecked(self.settings.use_semantic_embeddings)
        self._update_api_status_btn()

        self._reload_courses()
        self._reload_sources_tree()
        self._reload_chat_history()

        # Auto-sincronización silenciosa de clases y materiales en segundo plano
        self.sync_transcripts(silent_if_empty=True)

    def _reload_courses(self) -> None:
        from zoom2txt.workflow import WorkLedger

        ledger_courses = WorkLedger(self.settings, self.store).course_choices()
        rag_courses = self.store.list_rag_courses()

        merged: dict[str, str] = {}
        for c in ledger_courses:
            if c.get("id"):
                merged[str(c["id"])] = str(c.get("name") or c["id"])
        for c in rag_courses:
            if c.get("id"):
                cid = str(c["id"])
                cname = str(c.get("name") or cid)
                if cid not in merged or len(cname) > len(merged[cid]):
                    merged[cid] = cname

        prev_cid = self.course_combo.currentData()
        self.course_combo.blockSignals(True)
        try:
            self.course_combo.clear()
            if not merged:
                self.course_combo.addItem("Sin cursos indexados aún", None)
            else:
                for cid, cname in sorted(merged.items(), key=lambda x: x[1].casefold()):
                    self.course_combo.addItem(cname, cid)
            if prev_cid is not None:
                found_idx = self.course_combo.findData(prev_cid)
                if found_idx >= 0:
                    self.course_combo.setCurrentIndex(found_idx)
        finally:
            self.course_combo.blockSignals(False)

    def is_all_courses_mode(self) -> bool:
        return self.all_courses_btn.isChecked()

    def current_course_id(self) -> str | None:
        if self.is_all_courses_mode():
            return None
        data = self.course_combo.currentData()
        return str(data) if data else None

    def current_course_name(self) -> str:
        if self.is_all_courses_mode():
            return "Todos los cursos"
        return self.course_combo.currentText() or "Curso"

    def _on_all_courses_toggled(self, checked: bool) -> None:
        self.course_combo.setEnabled(not checked)
        if checked:
            self.scope_status.setText(
                "🌐 Modo Global Activo: consultando fuentes compartidas de TODOS tus cursos simultáneamente."
            )
        else:
            self.scope_status.setText(
                f"Ámbito activo: consultando exclusivamente las fuentes de «{self.current_course_name()}»."
            )
        self._reload_sources_tree()
        self._reload_chat_history()

    def _on_scope_changed(self) -> None:
        if not self.is_all_courses_mode():
            self.scope_status.setText(
                f"Ámbito activo: consultando exclusivamente las fuentes de «{self.current_course_name()}»."
            )
        self._reload_sources_tree()
        self._reload_chat_history()

    def _reload_sources_tree(self) -> None:
        self._updating_tree = True
        try:
            self.sources_tree.clear()
            all_mode = self.is_all_courses_mode()
            cid = self.current_course_id()
            if not all_mode and not cid:
                self.sources_count_lbl.setText("0 activas")
                return

            sources = self.store.list_knowledge_sources(
                course_id=cid,
                all_courses=all_mode,
                active_only=False,
            )

            badge_map = {
                "transcript": "🎙️ Clase",
                "pdf": "📄 PDF",
                "pptx": "📊 PPTX",
                "docx": "📝 DOCX",
                "txt": "📃 TXT",
            }

            active_count = 0
            if all_mode:
                by_course: dict[str, list[dict]] = {}
                for src in sources:
                    by_course.setdefault(src["course_name"] or src["course_id"], []).append(src)
                for cname, course_sources in sorted(by_course.items(), key=lambda x: x[0].casefold()):
                    parent_item = QTreeWidgetItem(self.sources_tree, [f"📚 {cname}"])
                    parent_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    for src in course_sources:
                        badge = badge_map.get(src["source_type"], "📄 Fuente")
                        child = QTreeWidgetItem(parent_item, [f"{badge} · {src['title']}"])
                        child.setFlags(
                            Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsUserCheckable
                            | Qt.ItemFlag.ItemIsSelectable
                        )
                        state = (
                            Qt.CheckState.Checked if src["is_active"] else Qt.CheckState.Unchecked
                        )
                        child.setCheckState(0, state)
                        child.setData(0, Qt.ItemDataRole.UserRole, int(src["id"]))
                        child.setToolTip(0, f"{src['course_name']}\n{src['file_path']}")
                        if src["is_active"]:
                            active_count += 1
                    parent_item.setExpanded(True)
            else:
                for src in sources:
                    badge = badge_map.get(src["source_type"], "📄 Fuente")
                    item = QTreeWidgetItem(self.sources_tree, [f"{badge} · {src['title']}"])
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsSelectable
                    )
                    state = Qt.CheckState.Checked if src["is_active"] else Qt.CheckState.Unchecked
                    item.setCheckState(0, state)
                    item.setData(0, Qt.ItemDataRole.UserRole, int(src["id"]))
                    item.setToolTip(0, src["file_path"])
                    if src["is_active"]:
                        active_count += 1

            self.sources_count_lbl.setText(f"{active_count}/{len(sources)} activas")
            if hasattr(self, "sources_indicator_lbl"):
                if active_count > 0:
                    self.sources_indicator_lbl.setText(f"📚 {active_count} fuente(s) de clase activas")
                else:
                    self.sources_indicator_lbl.setText("🌐 Modo libre (conocimiento general)")
        finally:
            self._updating_tree = False

    def _iter_source_items(self) -> list[QTreeWidgetItem]:
        items: list[QTreeWidgetItem] = []

        def _walk(node: QTreeWidgetItem) -> None:
            if node.data(0, Qt.ItemDataRole.UserRole) is not None:
                items.append(node)
            for i in range(node.childCount()):
                _walk(node.child(i))

        for i in range(self.sources_tree.topLevelItemCount()):
            _walk(self.sources_tree.topLevelItem(i))
        return items

    def get_active_source_ids(self) -> list[int]:
        ids: list[int] = []
        for item in self._iter_source_items():
            if item.checkState(0) == Qt.CheckState.Checked:
                sid = item.data(0, Qt.ItemDataRole.UserRole)
                if sid is not None:
                    ids.append(int(sid))
        return ids

    def _on_source_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_tree:
            return
        sid = item.data(0, Qt.ItemDataRole.UserRole)
        if sid is None:
            return
        checked = item.checkState(0) == Qt.CheckState.Checked
        self.store.set_source_active(int(sid), checked)
        all_items = self._iter_source_items()
        active_count = sum(1 for it in all_items if it.checkState(0) == Qt.CheckState.Checked)
        self.sources_count_lbl.setText(f"{active_count}/{len(all_items)} activas")
        if hasattr(self, "sources_indicator_lbl"):
            if active_count > 0:
                self.sources_indicator_lbl.setText(f"📚 {active_count} fuente(s) de clase activas")
            else:
                self.sources_indicator_lbl.setText("🌐 Modo libre (conocimiento general)")

    def _set_all_sources_checked(self, checked: bool) -> None:
        self._updating_tree = True
        try:
            all_items = self._iter_source_items()
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for item in all_items:
                item.setCheckState(0, state)
                sid = item.data(0, Qt.ItemDataRole.UserRole)
                if sid is not None:
                    self.store.set_source_active(int(sid), checked)
            active_count = len(all_items) if checked else 0
            self.sources_count_lbl.setText(f"{active_count}/{len(all_items)} activas")
            if hasattr(self, "sources_indicator_lbl"):
                if active_count > 0:
                    self.sources_indicator_lbl.setText(f"📚 {active_count} fuente(s) de clase activas")
                else:
                    self.sources_indicator_lbl.setText("🌐 Modo libre (conocimiento general)")
        finally:
            self._updating_tree = False

    def _on_week_filter_changed(self) -> None:
        mode = self.week_filter_combo.currentData()
        if mode == "all":
            self._set_all_sources_checked(True)
            return
        self._updating_tree = True
        try:
            all_items = self._iter_source_items()
            active_count = 0
            for item in all_items:
                week = infer_source_week(item.text(0)) or infer_source_week(item.toolTip(0))
                if mode == "parcial":
                    checked = week is None or (1 <= week <= 7)
                elif mode == "final":
                    checked = week is None or (8 <= week <= 20)
                else:
                    checked = True
                item.setCheckState(
                    0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
                sid = item.data(0, Qt.ItemDataRole.UserRole)
                if sid is not None:
                    self.store.set_source_active(int(sid), checked)
                if checked:
                    active_count += 1
            self.sources_count_lbl.setText(f"{active_count}/{len(all_items)} activas")
        finally:
            self._updating_tree = False

    def _trigger_quick_prompt(self, prompt_text: str) -> None:
        self.input_edit.setPlainText(prompt_text)
        self._send_question()

    # ── Sincronización e Ingesta de Materiales ────────────────────
    def sync_transcripts(self, silent_if_empty: bool = False) -> None:
        if type(self.window()).__name__ == "DemoMainWindow":
            return
        if self.sync_worker and self.sync_worker.isRunning():
            return
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.sync_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        self.scope_status.setText("Sincronizando transcripciones y materiales locales…")

        self.sync_worker = RagSyncWorker(
            self.settings,
            course_id=self.current_course_id(),
            parent=self,
        )
        self.sync_worker.progress.connect(self._on_ingest_progress)
        self.sync_worker.done.connect(
            lambda stats: self._on_sync_done(stats, silent_if_empty=silent_if_empty)
        )
        self.sync_worker.failed.connect(self._on_ingest_failed)
        self.sync_worker.start()

    def _on_sync_or_add_materials(self) -> None:
        if (self.sync_worker and self.sync_worker.isRunning()) or (
            self.ingest_worker and self.ingest_worker.isRunning()
        ):
            return

        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Añadir materiales de estudio al curso (o Cancela para solo sincronizar transcripciones)",
            str(self.settings.materials_dir if self.settings.materials_dir.exists() else Path.home()),
            "Documentos de curso (*.pdf *.pptx *.docx *.txt *.md)",
        )
        if not files:
            # Si el usuario no eligió archivos nuevos, sincroniza transcripciones y materiales existentes
            self.sync_transcripts(silent_if_empty=False)
            return

        cid = self.current_course_id() or "general"
        cname = (
            self.course_combo.currentText()
            if self.course_combo.currentData()
            else "Curso General"
        )
        paths = [Path(f) for f in files]

        self.progress_bar.setRange(0, len(paths))
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.sync_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        self.scope_status.setText(f"Indexando {len(paths)} archivo(s) en «{cname}»…")

        self.ingest_worker = RagIngestFilesWorker(
            self.settings,
            paths,
            course_id=cid,
            course_name=cname,
            parent=self,
        )
        self.ingest_worker.progress.connect(self._on_ingest_progress)
        self.ingest_worker.done.connect(self._on_files_ingested)
        self.ingest_worker.failed.connect(self._on_ingest_failed)
        self.ingest_worker.start()

    def _on_ingest_progress(self, current: int, total: int, filename: str) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(current)
        self.scope_status.setText(f"Indexando ({current}/{total}): {filename}")

    def _on_sync_done(self, stats: dict, silent_if_empty: bool = False) -> None:
        self.progress_bar.hide()
        self.sync_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self._reload_courses()
        self._reload_sources_tree()
        t_idx = stats.get("transcripts_indexed", 0)
        m_idx = stats.get("materials_indexed", 0)
        if t_idx or m_idx or not silent_if_empty:
            self.scope_status.setText(
                f"Sincronización completada: {t_idx} transcripción(es) y {m_idx} material(es) listos para consultar."
            )

    def _on_files_ingested(self, ok: int, failed: int) -> None:
        self.progress_bar.hide()
        self.sync_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self._reload_courses()
        self._reload_sources_tree()
        self.scope_status.setText(
            f"Materiales añadidos: {ok} indexados correctamente"
            + (f", {failed} con errores." if failed else ".")
        )

    def _on_ingest_failed(self, error: str) -> None:
        self.progress_bar.hide()
        self.sync_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self.scope_status.setText(f"No se pudo completar la sincronización: {error}")

    # ── Configuración de API Key y Modelos ────────────────────────
    def _save_inline_api_key(self) -> None:
        key = self.api_key_edit.text().strip()
        self.settings.gemini_api_key = key
        save_gemini_api_key(key)

    def _on_model_changed(self) -> None:
        model = self.model_combo.currentData()
        if model:
            self.settings.gemini_model = model
            self.settings.save()

    def _on_semantic_toggled(self, checked: bool) -> None:
        self.settings.use_semantic_embeddings = checked
        self.settings.save()

    def _open_materials_folder(self) -> None:
        self.settings.materials_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.materials_dir)))

    def _open_api_settings_dialog(self) -> None:
        dlg = ApiKeyConfigDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.api_key_edit.setText(self.settings.gemini_api_key)
            self._update_api_status_btn()
            self._reload_chat_history()

    def _update_api_status_btn(self) -> None:
        from zoom2txt.config import load_gemini_api_key

        has_key = bool(self.settings.gemini_api_key.strip() or load_gemini_api_key())
        if has_key:
            self.api_status_btn.setText("⚙️ IA Conectada")
            self.api_status_btn.setToolTip(
                "Tu clave de Gemini está lista. Haz clic si deseas cambiarla o ver opciones."
            )
            self.api_status_btn.setProperty("variant", "")
        else:
            self.api_status_btn.setText("🔑 Conectar Clave (1 min)")
            self.api_status_btn.setProperty("variant", "primary")
            self.api_status_btn.setToolTip(
                "Configura tu clave gratuita de Google AI Studio (Costo $0, sin tarjeta)."
            )
        self.api_status_btn.style().unpolish(self.api_status_btn)
        self.api_status_btn.style().polish(self.api_status_btn)

    def _show_api_guide(self) -> None:
        self._open_api_settings_dialog()

    def _test_api_connection(self) -> None:
        if self.test_worker and self.test_worker.isRunning():
            return
        self._save_inline_api_key()
        key = self.api_key_edit.text().strip()
        model = self.model_combo.currentData()
        self.test_api_btn.setEnabled(False)
        self.test_api_btn.setText("Probando…")
        self.scope_status.setText("Probando conexión con Google AI Studio…")
        self.test_worker = GeminiConnectionTestWorker(key, model, self)
        self.test_worker.finished_test.connect(self._on_test_api_finished)
        self.test_worker.start()

    def _on_test_api_finished(self, ok: bool, message: str) -> None:
        self.test_api_btn.setEnabled(True)
        self.test_api_btn.setText("Probar Conexión API")
        self.scope_status.setText(message)
        if ok:
            QMessageBox.information(self, "Conexión API verificada", message)
        else:
            QMessageBox.warning(self, "Prueba de API", message)

    # ── Historial de Chat y Consultas RAG ─────────────────────────
    def _reload_chat_history(self) -> None:
        all_mode = self.is_all_courses_mode()
        cid = self.current_course_id()
        title = "Todos los cursos" if all_mode else self.current_course_name()
        self.chat_title.setText(f"Conversación · {title}")
        self._current_session_id = self.store.get_or_create_chat_session(
            course_id=cid,
            all_courses=all_mode,
            title=title,
        )
        messages = self.store.get_chat_messages(self._current_session_id)
        self._render_messages(messages)

    def _render_messages(self, messages: list[dict]) -> None:
        if not messages:
            scope_desc = (
                "todos tus cursos (Fuentes Compartidas)"
                if self.is_all_courses_mode()
                else f"«{html.escape(self.current_course_name())}»"
            )
            has_key = bool(self.settings.gemini_api_key.strip() or self.api_key_edit.text().strip())
            if self.demo_responder is not None:
                key_status_html = (
                    f'<div style="background-color:{style.ACCENT_SUBTLE}; color:{style.SUCCESS}; '
                    f'border:1px solid {style.BORDER_HI}; border-radius:6px; padding:8px 10px; margin-top:10px;">'
                    f"✓ <b>Modo demostración activo (datos sintéticos offline).</b> "
                    f"Puedes usar los botones rápidos o escribir tu pregunta sin API Key."
                    f"</div>"
                )
            elif has_key:
                key_status_html = (
                    f'<div style="background-color:{style.ACCENT_SUBTLE}; color:{style.SUCCESS}; '
                    f'border:1px solid {style.BORDER_HI}; border-radius:6px; padding:8px 10px; margin-top:10px;">'
                    f"✓ <b>GEMINI_API_KEY configurada.</b> Ya puedes escribir tu pregunta abajo."
                    f"</div>"
                )
            else:
                key_status_html = (
                    f'<div style="background-color:{style.SURFACE_2}; border:1px solid {style.BORDER_HI}; '
                    f'border-radius:8px; padding:12px 14px; margin-top:12px;">'
                    f'<div style="font-size:13.5px; font-weight:700; color:{style.LINK}; margin-bottom:6px;">'
                    f"🔑 Cómo configurar tu Asistente IA en 1 minuto (100% Gratis · Costo $0 · 0% consumo en tu PC)</div>"
                    f'<ol style="margin:4px 0 6px 18px; color:{style.TEXT}; font-size:12.5px;">'
                    f"<li>Haz clic arriba en <b>«Obtener API Key gratis (1 min)»</b> o entra a "
                    f'<a href="{AI_STUDIO_APIKEY_URL}" style="color:{style.LINK};">{AI_STUDIO_APIKEY_URL}</a> '
                    f"con tu cuenta personal de Google (<code>@gmail.com</code>).</li>"
                    f"<li>Pulsa el botón azul <b>«Create API key»</b> (no pide tarjeta de crédito).</li>"
                    f"<li>Copia la clave generada (<code>AIzaSy…</code>), pégala en el cuadro "
                    f"<b>GEMINI_API_KEY</b> de arriba y pulsa <b>«Probar Conexión API»</b>.</li>"
                    f"<li>Pulsa <b>«Sincronizar / Añadir Materiales»</b> para cargar tus clases o subir "
                    f"archivos PDF, PPTX, DOCX o TXT, y haz tu pregunta abajo.</li>"
                    f"</ol>"
                    f"</div>"
                )
            welcome_html = (
                f'<div style="color:{style.MUTED}; padding:14px; line-height:1.5;">'
                f'<div style="font-size:15px; font-weight:600; color:{style.TEXT}; margin-bottom:6px;">'
                f"👋 Asistente de Estudio con IA en la Nube (Estilo NotebookLM · 100% Gratis · 0% carga en tu PC)</div>"
                f"Estás consultando <b>{scope_desc}</b>.<br>"
                f"• Responde <b>únicamente</b> con base en las transcripciones y materiales marcados a la izquierda.<br>"
                f"• Cada afirmación incluye su cita verificable <code>[Curso | Fuente]</code>.<br>"
                f"• Si algo no se dijo en clase ni está en tus documentos, te lo indicará sin inventar datos."
                f"{key_status_html}"
                f"</div>"
            )
            self.chat_view.setHtml(welcome_html)
            return

        blocks: list[str] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            model_used = msg.get("model_used", "")
            if role == "user":
                blocks.append(
                    f'<div style="background-color:{style.SURFACE_2}; border:1px solid {style.BORDER}; '
                    f'border-radius:8px; padding:10px 12px; margin-bottom:10px;">'
                    f'<div style="font-size:12px; font-weight:700; color:{style.MUTED}; margin-bottom:4px;">🧑‍🎓 Tú</div>'
                    f'<div style="color:{style.TEXT};">{render_markdown_to_html(content)}</div>'
                    f"</div>"
                )
            else:
                model_badge = (
                    f' <span style="font-size:11px; color:{style.FAINT}; font-weight:400;">({html.escape(model_used)})</span>'
                    if model_used
                    else ""
                )
                blocks.append(
                    f'<div style="background-color:{style.SURFACE}; border:1px solid {style.BORDER_HI}; '
                    f'border-radius:8px; padding:10px 12px; margin-bottom:12px;">'
                    f'<div style="font-size:12px; font-weight:700; color:{style.LINK}; margin-bottom:6px;">'
                    f"🤖 Asistente Zoom2TXT{model_badge}</div>"
                    f'<div style="color:{style.TEXT};">{render_markdown_to_html(content)}</div>'
                    f"</div>"
                )

        self.chat_view.setHtml("\n".join(blocks))
        scrollbar = self.chat_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _send_question(self) -> None:
        if self.query_worker and self.query_worker.isRunning():
            return
        question = self.input_edit.toPlainText().strip()
        if not question:
            return

        self._save_inline_api_key()
        active_ids = self.get_active_source_ids()

        if self.demo_responder is not None:
            resp = self.demo_responder(question, active_ids)
            self._on_query_done(question, resp)
            return

        from zoom2txt.config import load_gemini_api_key

        api_key = self.settings.gemini_api_key.strip() or load_gemini_api_key()
        if not api_key:
            self._open_api_settings_dialog()
            return

        self.rate_limit_banner.hide()
        self._countdown_timer.stop()
        self.analyzing_label.setText("🔎 Analizando fuentes locales…")
        self.analyzing_label.show()
        self.send_btn.setEnabled(False)
        self.input_edit.setReadOnly(True)

        self.query_worker = RagQueryWorker(
            engine=self.engine,
            question=question,
            course_id=self.current_course_id(),
            all_courses=self.is_all_courses_mode(),
            active_source_ids=active_ids,
            preferred_model=self.model_combo.currentData(),
            stream=False,
            parent=self,
        )
        self.query_worker.done.connect(lambda resp: self._on_query_done(question, resp))
        self.query_worker.rate_limited.connect(self._on_rate_limited)
        self.query_worker.failed.connect(self._on_query_failed)
        self.query_worker.start()

    def _on_query_done(self, question: str, resp: RAGResponse) -> None:
        self.analyzing_label.hide()
        self.send_btn.setEnabled(True)
        self.input_edit.setReadOnly(False)
        self.input_edit.clear()

        if self._current_session_id is None:
            self._current_session_id = self.store.get_or_create_chat_session(
                course_id=self.current_course_id(),
                all_courses=self.is_all_courses_mode(),
                title=self.current_course_name(),
            )
        self.store.append_chat_message(self._current_session_id, "user", question)
        self.store.append_chat_message(
            self._current_session_id,
            "assistant",
            resp.answer,
            citations=resp.citations,
            model_used=resp.model_used,
        )
        messages = self.store.get_chat_messages(self._current_session_id)
        self._render_messages(messages)

    def _on_rate_limited(self, retry_after: int, message: str) -> None:
        """Manejo amigable de error 429: conserva la pregunta escrita e inicia cuenta regresiva."""
        self.analyzing_label.hide()
        self.input_edit.setReadOnly(False)
        self._countdown_remaining = max(5, int(retry_after))
        self.send_btn.setEnabled(False)
        self.rate_limit_banner.setStyleSheet(
            f"background-color:{style.SURFACE_2}; color:{style.WARNING}; "
            f"border:1px solid {style.WARNING}; border-radius:6px; padding:8px 10px; font-weight:600;"
        )
        self._update_countdown_banner()
        self.rate_limit_banner.show()
        self._countdown_timer.start()

    def _tick_countdown(self) -> None:
        self._countdown_remaining -= 1
        if self._countdown_remaining <= 0:
            self._countdown_timer.stop()
            self.send_btn.setEnabled(True)
            self.rate_limit_banner.setStyleSheet(
                f"background-color:{style.ACCENT_SUBTLE}; color:{style.SUCCESS}; "
                f"border:1px solid {style.BORDER_HI}; border-radius:6px; padding:8px 10px; font-weight:600;"
            )
            self.rate_limit_banner.setText(
                "✓ Cuota por minuto renovada. Ya puedes pulsar «Preguntar» (tu pregunta sigue lista abajo)."
            )
            return
        self._update_countdown_banner()

    def _update_countdown_banner(self) -> None:
        self.rate_limit_banner.setText(
            f"⏳ Límite gratuito por minuto (429) alcanzado. Tu pregunta se conservó intacta; "
            f"puedes volver a enviarla en {self._countdown_remaining} s…"
        )

    def _on_query_failed(self, error: str) -> None:
        self.analyzing_label.hide()
        self.send_btn.setEnabled(True)
        self.input_edit.setReadOnly(False)
        self.rate_limit_banner.setStyleSheet(
            f"background-color:{style.SURFACE_2}; color:{style.ERROR}; "
            f"border:1px solid {style.ERROR}; border-radius:6px; padding:8px 10px;"
        )
        self.rate_limit_banner.setText(f"No se pudo completar la consulta: {error}")
        self.rate_limit_banner.show()

    def _clear_chat(self) -> None:
        if self._current_session_id is not None:
            self.store.clear_chat_session(self._current_session_id)
            self._render_messages([])

    def _export_chat(self) -> None:
        if self._current_session_id is None:
            return
        messages = self.store.get_chat_messages(self._current_session_id)
        if not messages:
            QMessageBox.information(
                self, "Conversación vacía", "Aún no hay mensajes para exportar en esta vista."
            )
            return

        default_name = f"Zoom2TXT-Asistente-{datetime.now(UTC):%Y-%m-%d}.md"
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar conversación de estudio",
            str(Path.home() / default_name),
            "Markdown (*.md);;Texto plano (*.txt)",
        )
        if not selected:
            return
        dest = Path(selected)
        lines = [
            f"# Asistente IA Zoom2TXT — {self.current_course_name()}",
            f"Exportado: {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
            "",
        ]
        for msg in messages:
            speaker = "Estudiante" if msg["role"] == "user" else f"Asistente ({msg.get('model_used', 'IA')})"
            lines.append(f"## {speaker}")
            lines.append(msg["content"])
            lines.append("")

        try:
            atomic_write(dest, "\n".join(lines))
            QMessageBox.information(
                self,
                "Conversación exportada",
                f"Se guardó la conversación en:\n{dest}",
            )
        except OSError as exc:
            QMessageBox.warning(self, "Error al exportar", str(exc))

    def is_busy(self) -> bool:
        for w in (self.sync_worker, self.ingest_worker, self.query_worker, self.test_worker):
            if w and w.isRunning():
                return True
        return False

    def cancel_all(self) -> None:
        self._countdown_timer.stop()
        for w in (self.sync_worker, self.ingest_worker, self.query_worker, self.test_worker):
            if w and w.isRunning():
                if hasattr(w, "cancel"):
                    w.cancel()
                w.requestInterruption()
                w.wait(2000)

    def closeEvent(self, event) -> None:
        self.cancel_all()
        super().closeEvent(event)

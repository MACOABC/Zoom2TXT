"""Asistente de estudio: conversa con tus clases y materiales.

Cada respuesta se ancla a las fuentes marcadas y cita con ``[n]`` el pasaje del
que sale cada afirmación; un clic en la cita muestra el texto original. La
inferencia corre en Gemini con la clave gratuita del propio estudiante (BYOK);
en la PC solo se guarda y se busca el texto.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
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
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zoom2txt import study
from zoom2txt.ai_models import AUTO_MODEL, model_label
from zoom2txt.config import AI_STUDIO_APIKEY_URL, Settings, save_gemini_api_key
from zoom2txt.gui import lucide, style, widgets
from zoom2txt.gui.workers import (
    GeminiConnectionTestWorker,
    ModelRefreshWorker,
    RagIngestFilesWorker,
    RagQueryWorker,
    RagSyncWorker,
    StudyWorker,
)
from zoom2txt.rag_engine import FULL_CONTEXT_WORDS, RAGEngine, RAGResponse, infer_source_week
from zoom2txt.storage import StateStore, atomic_write

# (etiqueta, ayuda, prompt) — se conserva para integraciones anteriores.
QUICK_STUDY_PROMPTS: tuple[tuple[str, str, str], ...] = tuple(
    (study.TOOLS[key].label, study.TOOLS[key].hint, study.TOOLS[key].prompt)
    for key in study.QUICK_TOOL_KEYS
)
TOOL_ORDER = ("resumen", "guia", "flashcards", "quiz", "conceptos", "fechas")
SOURCE_BADGES = {
    "transcript": ("captions", "Clase"),
    "pdf": ("file-text", "PDF"),
    "pptx": ("layers", "PPTX"),
    "docx": ("file-text", "DOCX"),
    "txt": ("file-text", "TXT"),
}
MODE_LABELS = {
    "full": ("Fuentes completas", "success"),
    "retrieval": ("Fragmentos relevantes", "info"),
    "sample": ("Muestra de todas las fuentes", "info"),
    "general": ("Conocimiento general", "warning"),
    "none": ("Sin fuentes", "warning"),
}
_CITE_INLINE = re.compile(r"\[(\d{1,3}(?:\s*[,\-–y]\s*\d{1,3})*)\]")
_LEGACY_CITE = re.compile(r"\[([^\[\]|]+?\|[^\[\]]+?)\]")


# ── Markdown → HTML (subconjunto que Qt sabe pintar) ──────────────
def _inline(text: str, valid: set[int] | None) -> str:
    escaped = html.escape(text, quote=False)
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    escaped = re.sub(
        r"`([^`]+)`",
        lambda m: keep(
            f'<span style="font-family:{style.FONT_MONO}; font-size:12.5px; '
            f'background-color:{style.SURFACE_3};">&nbsp;{m.group(1)}&nbsp;</span>'
        ),
        escaped,
    )
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda m: keep(f'<a href="{m.group(2)}" style="color:{style.LINK};">{m.group(1)}</a>'),
        escaped,
    )

    def cite(match: re.Match) -> str:
        numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
        body = match.group(1)
        if re.fullmatch(r"\d+\s*[\-–]\s*\d+", body.strip()) and 0 < numbers[1] - numbers[0] <= 12:
            numbers = list(range(numbers[0], numbers[1] + 1))
        if valid is None or not all(n in valid for n in numbers):
            return match.group(0)
        return keep(
            "".join(
                f'<a href="cite:{n}" style="color:{style.LINK}; background-color:{style.ACCENT_SUBTLE}; '
                f'text-decoration:none; font-size:11px; font-weight:700;">&nbsp;{n}&nbsp;</a>'
                + ("&#8201;" if i < len(numbers) - 1 else "")
                for i, n in enumerate(numbers)
            )
        )

    escaped = _CITE_INLINE.sub(cite, escaped)
    escaped = _LEGACY_CITE.sub(
        lambda m: keep(
            f'<span style="color:{style.LINK}; background-color:{style.ACCENT_SUBTLE}; '
            f'font-size:11.5px; font-weight:600;">&nbsp;{m.group(1)}&nbsp;</span>'
        ),
        escaped,
    )
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<![\*\w])\*([^*\n]+?)\*(?![\*\w])", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<![_\w])_([^_\n]+?)_(?![_\w])", r"<i>\1</i>", escaped)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], escaped)


def render_markdown_to_html(text: str, valid_citations: set[int] | None = None) -> str:
    """Convierte el Markdown de una respuesta (títulos, listas, tablas, citas,
    bloques de código) a HTML acorde al tema activo. ``valid_citations`` son los
    números de fuente que deben convertirse en enlaces ``cite:n``."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    list_stack: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            out.append(
                '<p style="margin:0 0 9px 0; line-height:150%;">'
                + "<br>".join(_inline(line, valid_citations) for line in paragraph)
                + "</p>"
            )
            paragraph.clear()

    def close_lists(depth: int = 0) -> None:
        while len(list_stack) > depth:
            out.append(f"</{list_stack.pop()}>")

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            close_lists()
            block: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            code = html.escape("\n".join(block))
            out.append(
                f'<table width="100%" cellspacing="0" cellpadding="10" style="margin:6px 0 10px 0;">'
                f'<tr><td style="background-color:{style.CONSOLE_BG}; border:1px solid {style.BORDER};">'
                f'<pre style="margin:0; font-family:{style.FONT_MONO}; font-size:12.5px; '
                f'color:{style.CONSOLE_FG};">{code}</pre></td></tr></table>'
            )
            continue

        if (
            "|" in stripped
            and index + 1 < len(lines)
            and re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", lines[index + 1].strip())
        ):
            flush_paragraph()
            close_lists()

            def cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]

            header = cells(stripped)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(cells(lines[index]))
                index += 1
            border = f"border:1px solid {style.BORDER};"
            table = [
                '<table cellspacing="0" cellpadding="7" style="border-collapse:collapse; margin:6px 0 10px 0;">',
                "<tr>"
                + "".join(
                    f'<th style="{border} background-color:{style.SURFACE_2}; font-weight:650; '
                    f'text-align:left;">{_inline(cell, valid_citations)}</th>'
                    for cell in header
                )
                + "</tr>",
            ]
            for row in rows:
                table.append(
                    "<tr>"
                    + "".join(
                        f'<td style="{border}">{_inline(cell, valid_citations)}</td>' for cell in row
                    )
                    + "</tr>"
                )
            table.append("</table>")
            out.append("".join(table))
            continue

        if not stripped:
            flush_paragraph()
            close_lists()
            index += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", stripped)
        if heading:
            flush_paragraph()
            close_lists()
            level = len(heading.group(1))
            size = {1: 18, 2: 16.5, 3: 15}.get(level, 14)
            out.append(
                f'<p style="margin:12px 0 6px 0; font-size:{size}px; font-weight:700; '
                f'color:{style.TEXT};">{_inline(heading.group(2), valid_citations)}</p>'
            )
            index += 1
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_paragraph()
            close_lists()
            out.append(
                f'<table width="100%" cellspacing="0" cellpadding="0" style="margin:8px 0;"><tr>'
                f'<td style="background-color:{style.BORDER}; font-size:1px;">&nbsp;</td></tr></table>'
            )
            index += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            close_lists()
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append(
                f'<table width="100%" cellspacing="0" cellpadding="8" style="margin:4px 0 10px 0;"><tr>'
                f'<td width="3" style="background-color:{style.ACCENT};"></td>'
                f'<td style="background-color:{style.SURFACE_2}; color:{style.MUTED};">'
                + "<br>".join(_inline(line, valid_citations) for line in quote if line)
                + "</td></tr></table>"
            )
            continue

        bullet = re.match(r"^(\s*)([-*•+]|\d{1,3}[.)])\s+(.*)$", raw)
        if bullet:
            flush_paragraph()
            depth = min(3, len(bullet.group(1).replace("\t", "    ")) // 2) + 1
            kind = "ol" if bullet.group(2)[0].isdigit() else "ul"
            close_lists(depth)
            while len(list_stack) < depth:
                out.append(
                    f'<{kind} style="margin:2px 0 8px 0; -qt-list-indent:{len(list_stack) + 1};">'
                )
                list_stack.append(kind)
            out.append(
                f'<li style="margin-bottom:4px; line-height:145%;">'
                f"{_inline(bullet.group(3), valid_citations)}</li>"
            )
            index += 1
            continue

        if list_stack and raw.startswith(("  ", "\t")):
            # Continuación de un elemento de lista.
            out.append(f"<br>{_inline(stripped, valid_citations)}")
            index += 1
            continue

        close_lists()
        paragraph.append(stripped)
        index += 1

    flush_paragraph()
    close_lists()
    return "\n".join(out)


class ChatInputEdit(QPlainTextEdit):
    """Entrada que envía con Enter, salta de línea con Mayús+Enter y crece
    con el texto hasta seis líneas."""

    def __init__(self, on_submit, parent=None):
        super().__init__(parent)
        self._on_submit = on_submit
        self.setObjectName("composer")
        self.setPlaceholderText("Pregunta sobre tus clases o materiales…")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.textChanged.connect(self._fit)
        self._fit()

    def _fit(self) -> None:
        # En QPlainTextEdit la «altura» del documento es su número de líneas visuales.
        visual = max(2, min(6, int(self.document().size().height())))
        self.setFixedHeight(self.fontMetrics().lineSpacing() * visual + 14)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                event.accept()
                self._on_submit()
                return
        super().keyPressEvent(event)


class ApiKeyGuideDialog(QDialog):
    """Guía paso a paso para obtener y configurar la GEMINI_API_KEY gratuita."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cómo conectar el Asistente IA (guía de 1 minuto)")
        self.setMinimumWidth(560)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        title = QLabel("Conecta tu Asistente IA en 1 minuto, sin costo")
        title.setObjectName("h2")
        title.setWordWrap(True)
        lay.addWidget(title)

        guide_browser = QTextBrowser()
        guide_browser.setOpenExternalLinks(True)
        guide_browser.setMinimumHeight(300)
        guide_browser.setHtml(
            f'<div style="color:{style.TEXT}; font-size:13.5px;">'
            f'<p style="line-height:150%;">Zoom2TXT usa la capa gratuita de <b>Google AI Studio</b> con tu '
            f"propia clave. No pide tarjeta y la IA corre en la nube, sin cargar tu PC.</p>"
            f'<ol style="line-height:150%;">'
            f'<li style="margin-bottom:8px;"><b>Abre Google AI Studio:</b> entra a '
            f'<a href="{AI_STUDIO_APIKEY_URL}" style="color:{style.LINK};">{AI_STUDIO_APIKEY_URL}</a> '
            f"con tu cuenta personal de Google. Los correos institucionales suelen tenerlo bloqueado.</li>"
            f'<li style="margin-bottom:8px;"><b>Crea tu clave:</b> pulsa <b>«Create API key»</b> y, si es '
            f"tu primera vez, acepta los términos y elige crearla en un proyecto nuevo.</li>"
            f'<li style="margin-bottom:8px;"><b>Pégala en Zoom2TXT:</b> copia la clave, pégala en '
            f"<b>Configuración de IA</b> y pulsa <b>«Probar conexión»</b>.</li>"
            f'<li style="margin-bottom:8px;"><b>Pregunta:</b> tus transcripciones se indexan solas; añade '
            f"PDF, diapositivas o Word desde <b>Fuentes</b>, marca qué incluir y escribe tu duda.</li>"
            f"</ol>"
            f'<p style="color:{style.MUTED}; font-size:12.5px; line-height:150%;"><b>Privacidad:</b> la clave se '
            f"guarda cifrada solo en esta computadora y tus archivos se indexan en una base local. Al "
            f"preguntar, los pasajes relevantes se envían a Google para generar la respuesta; en la capa "
            f"gratuita Google puede usar ese contenido para mejorar sus productos.</p>"
            f"</div>"
        )
        lay.addWidget(guide_browser)

        btn_row = QHBoxLayout()
        open_btn = widgets.button("Abrir Google AI Studio", "external-link", "primary")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AI_STUDIO_APIKEY_URL)))
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.button(QDialogButtonBox.StandardButton.Close).setText("Cerrar")
        close_box.rejected.connect(self.reject)
        btn_row.addWidget(open_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_box)
        lay.addLayout(btn_row)


class ApiKeyConfigDialog(QDialog):
    """Clave de Gemini, comprobación de conexión y preferencias de búsqueda."""

    def __init__(self, settings: Settings, parent=None, engine: RAGEngine | None = None):
        super().__init__(parent)
        self.settings = settings
        self.engine = engine
        self.setWindowTitle("Configuración de IA")
        self.setMinimumWidth(540)
        self._test_worker: GeminiConnectionTestWorker | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        title = QLabel("Configuración del Asistente IA")
        title.setObjectName("h2")
        lay.addWidget(title)

        info = QLabel(
            "Una sola clave gratuita de Google AI Studio sirve para todos los modelos. "
            "No necesitas tarjeta y la inferencia corre en la nube, sin consumir tu PC."
        )
        info.setObjectName("muted")
        info.setWordWrap(True)
        lay.addWidget(info)

        form = QFormLayout()
        form.setSpacing(10)
        self.key_edit = QLineEdit(self.settings.gemini_api_key)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Pega aquí tu clave de Google AI Studio")
        form.addRow("GEMINI_API_KEY", self.key_edit)
        self.semantic_chk = QCheckBox("Búsqueda semántica (embeddings) además de la búsqueda por palabras")
        self.semantic_chk.setToolTip(
            "Mejora la recuperación cuando preguntas con otras palabras que las del profesor. "
            "Usa una pequeña parte de tu cuota gratuita de embeddings."
        )
        self.semantic_chk.setChecked(self.settings.use_semantic_embeddings)
        form.addRow("", self.semantic_chk)
        lay.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.get_key_btn = widgets.button("Obtener clave gratis", "key-round", "secondary")
        self.get_key_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AI_STUDIO_APIKEY_URL)))
        self.guide_btn = widgets.button("Ver guía", "circle-help", "ghost")
        self.guide_btn.clicked.connect(lambda: ApiKeyGuideDialog(self).exec())
        self.test_btn = widgets.button("Probar conexión", "zap", "secondary")
        self.test_btn.clicked.connect(self._test_connection)
        btn_row.addWidget(self.get_key_btn)
        btn_row.addWidget(self.guide_btn)
        btn_row.addStretch()
        btn_row.addWidget(self.test_btn)
        lay.addLayout(btn_row)

        self.status = widgets.Banner("", "info")
        self.status.hide()
        self.status_lbl = self.status
        lay.addWidget(self.status)

        self.usage_lbl = QLabel("")
        self.usage_lbl.setObjectName("faint")
        self.usage_lbl.setWordWrap(True)
        lay.addWidget(self.usage_lbl)
        self._refresh_usage()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("Guardar")
        save.setProperty("variant", "primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _refresh_usage(self) -> None:
        if self.engine is None:
            return
        try:
            exhausted = [model_label(m) for m in self.engine.router.exhausted_today()]
            text = self.engine.router.usage_summary() + " (contador local de esta app)."
            if exhausted:
                text += " Sin cuota hasta mañana: " + ", ".join(exhausted) + "."
            self.usage_lbl.setText(text)
        except Exception:  # noqa: BLE001 — informativo
            self.usage_lbl.setText("")

    def _test_connection(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            self.status.show_message("Ingresa una clave antes de probar.", "warning")
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Probando…")
        self.status.show_message("Conectando con Google AI Studio…", "info")
        self._test_worker = GeminiConnectionTestWorker(key, self.settings.gemini_model, self)
        self._test_worker.finished_test.connect(self._on_test_finished)
        self._test_worker.start()

    def _on_test_finished(self, ok: bool, message: str) -> None:
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Probar conexión")
        self.status.show_message(message, "success" if ok else "error")
        self._refresh_usage()

    def _save_and_accept(self) -> None:
        key = self.key_edit.text().strip()
        self.settings.gemini_api_key = key
        save_gemini_api_key(key)
        self.settings.use_semantic_embeddings = self.semantic_chk.isChecked()
        self.settings.save()
        self.accept()

    def done(self, result: int) -> None:
        if self._test_worker and self._test_worker.isRunning():
            self._test_worker.wait(1500)
        super().done(result)


class SourcePeekDialog(QDialog):
    """Muestra el pasaje exacto detrás de una cita y permite abrir la fuente."""

    open_reader = Signal(dict)

    def __init__(self, citation: dict, parent=None):
        super().__init__(parent)
        self.citation = citation
        self.setWindowTitle("Fuente citada")
        self.resize(640, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        icon_name, kind = SOURCE_BADGES.get(citation.get("source_type", ""), ("file-text", "Fuente"))
        head.addWidget(widgets.icon_badge(icon_name, "LINK", 38), 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(f"[{citation.get('n', '·')}] {citation.get('source_title', 'Fuente')}")
        title.setObjectName("h2")
        title.setWordWrap(True)
        title.setTextFormat(Qt.TextFormat.PlainText)
        meta = " · ".join(
            x for x in (kind, citation.get("course_name", ""), citation.get("locator", "")) if x
        )
        sub = QLabel(meta)
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(sub)
        head.addLayout(titles, 1)
        lay.addLayout(head)
        body = QTextBrowser()
        body.setObjectName("reader")
        snippet = str(citation.get("snippet", "")).strip()
        body.setPlainText(
            (snippet + (" …" if len(snippet) >= 690 else ""))
            or "El pasaje no se guardó con este mensaje. Abre la fuente para leerla completa."
        )
        lay.addWidget(body, 1)
        row = QHBoxLayout()
        note = QLabel(
            "Inicio del pasaje entregado al modelo." if not citation.get("whole") else "Inicio de la fuente."
        )
        note.setObjectName("faint")
        row.addWidget(note, 1)
        open_btn = widgets.button("Abrir en el lector", "book-open", "primary")
        open_btn.clicked.connect(self._open)
        open_btn.setEnabled(bool(citation.get("path") or citation.get("source_id")))
        close_btn = widgets.button("Cerrar", None, "secondary")
        close_btn.clicked.connect(self.reject)
        row.addWidget(close_btn)
        row.addWidget(open_btn)
        lay.addLayout(row)

    def _open(self) -> None:
        self.open_reader.emit(self.citation)
        self.accept()


class _AutoBrowser(QTextBrowser):
    """QTextBrowser sin barras que ajusta su altura al contenido."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self.setMinimumWidth(80)

    def _fit(self, *_args) -> None:
        self.setFixedHeight(max(20, int(self.document().size().height()) + 2))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.document().setTextWidth(self.viewport().width())
        self._fit()

    def wheelEvent(self, event) -> None:
        event.ignore()  # el desplazamiento pertenece a la conversación


class _MessageWidget(QFrame):
    cite_clicked = Signal(int)
    copy_requested = Signal(str)

    def __init__(self, role: str, parent=None):
        super().__init__(parent)
        self.role = role
        self.setObjectName("plain")
        self._markdown = ""
        self._citations: list[dict] = []
        self._html = ""
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        if role == "user":
            outer.addStretch(1)
            self.bubble = QFrame()
            self.bubble.setObjectName("userBubble")
            bubble_lay = QVBoxLayout(self.bubble)
            bubble_lay.setContentsMargins(14, 10, 14, 10)
            self.text = QLabel("")
            self.text.setWordWrap(True)
            self.text.setTextFormat(Qt.TextFormat.PlainText)
            self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bubble_lay.addWidget(self.text)
            outer.addWidget(self.bubble, 5)
            self._restyle()
            style.on_theme_change(self._restyle)
            return
        outer.addWidget(widgets.icon_badge("sparkles", "LINK", 30), 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(8)
        name = QLabel("Asistente")
        name.setObjectName("h3")
        header.addWidget(name)
        self.model_chip = widgets.chip("")
        self.model_chip.hide()
        self.mode_chip = widgets.chip("")
        self.mode_chip.hide()
        self.note_chip = widgets.chip("", "warning")
        self.note_chip.hide()
        header.addWidget(self.model_chip)
        header.addWidget(self.mode_chip)
        header.addWidget(self.note_chip)
        header.addStretch()
        column.addLayout(header)
        self.body = _AutoBrowser()
        self.body.anchorClicked.connect(self._on_anchor)
        column.addWidget(self.body)
        self.sources_host = QWidget()
        self.sources_flow = widgets.FlowLayout(self.sources_host, 6)
        self.sources_host.hide()
        column.addWidget(self.sources_host)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(2)
        self.copy_btn = widgets.icon_button("copy", "Copiar respuesta", "FAINT", 15)
        self.copy_btn.clicked.connect(lambda: self.copy_requested.emit(self._markdown))
        self.usage = QLabel("")
        self.usage.setObjectName("faint")
        self.actions.addWidget(self.copy_btn)
        self.actions.addWidget(self.usage)
        self.actions.addStretch()
        self.actions_host = QWidget()
        self.actions_host.setLayout(self.actions)
        self.actions_host.hide()
        column.addWidget(self.actions_host)
        outer.addLayout(column, 1)
        style.on_theme_change(self._rerender)

    def _restyle(self) -> None:
        self.bubble.setStyleSheet(
            f"QFrame#userBubble {{ background-color: {style.USER_BUBBLE}; border: 1px solid "
            f"{style.BORDER}; border-radius: 14px; }}"
        )

    def _on_anchor(self, url: QUrl) -> None:
        if url.scheme() == "cite":
            try:
                self.cite_clicked.emit(int(url.path() or url.toString().split(":", 1)[1]))
            except (ValueError, IndexError):
                pass
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def set_user_text(self, text: str) -> None:
        self._markdown = text
        self.text.setText(text)
        self._html = "<p>" + html.escape(text) + "</p>"

    def set_markdown(self, markdown: str, citations: list[dict] | None = None, streaming: bool = False) -> None:
        self._markdown = markdown
        if citations is not None:
            self._citations = citations
        self._rerender(streaming)

    def _rerender(self, streaming: bool = False) -> None:
        if self.role == "user":
            return
        valid = {int(c["n"]) for c in self._citations if c.get("n")} if self._citations else None
        if streaming:
            valid = None
        body = render_markdown_to_html(self._markdown, valid)
        if streaming:
            body += f'<span style="color:{style.LINK};">▍</span>'
        self._html = body
        self.body.setHtml(
            f'<div style="color:{style.TEXT}; font-size:14px;">{body or "&nbsp;"}</div>'
        )

    def set_meta(
        self, model: str = "", mode: str = "", note: str = "", usage: str = "", finished: bool = True
    ) -> None:
        if self.role == "user":
            return
        self.model_chip.setText(model_label(model) if model.startswith("gemini") else model)
        self.model_chip.setVisible(bool(model))
        label, tone = MODE_LABELS.get(mode, ("", ""))
        self.mode_chip.setText(label)
        widgets.set_tone(self.mode_chip, tone)
        self.mode_chip.setVisible(bool(label))
        self.note_chip.setText(note)
        self.note_chip.setVisible(bool(note))
        self.usage.setText(usage)
        self.actions_host.setVisible(finished)

    def set_sources(self, citations: list[dict]) -> None:
        if self.role == "user":
            return
        self._citations = citations
        while self.sources_flow.count():
            item = self.sources_flow.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        numbered = [c for c in citations if c.get("n")]
        for citation in numbered[:14]:
            title = str(citation.get("source_title", "Fuente"))
            short = title if len(title) <= 34 else title[:32] + "…"
            where = citation.get("locator", "")
            chip = QPushButton(f"{citation['n']} · {short}" + (f" · {where}" if where and len(where) < 18 else ""))
            chip.setProperty("variant", "chip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip(f"{title}\n{citation.get('course_name', '')}\n{where}".strip())
            chip.clicked.connect(lambda _=False, n=int(citation["n"]): self.cite_clicked.emit(n))
            self.sources_flow.addWidget(chip)
            chip.show()
        self.sources_host.setVisible(bool(numbered))

    def citation(self, number: int) -> dict | None:
        return next((c for c in self._citations if c.get("n") == number), None)

    def html(self) -> str:
        return self._html

    def markdown(self) -> str:
        return self._markdown


class ChatTranscript(QScrollArea):
    """Conversación: lista vertical de mensajes con desplazamiento que sigue al
    texto mientras se genera."""

    cite_clicked = Signal(object, int)
    suggestion_clicked = Signal(str)
    link_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._host = QWidget()
        self._host.setObjectName("plain")
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(4, 6, 10, 6)
        self._layout.setSpacing(18)
        self._layout.addStretch()
        self.setWidget(self._host)
        self._messages: list[_MessageWidget] = []
        self._welcome: QWidget | None = None
        self._welcome_html = ""
        self._pinned = True
        self.verticalScrollBar().rangeChanged.connect(self._follow)
        self.verticalScrollBar().valueChanged.connect(self._track)
        self.setMinimumHeight(200)

    def _track(self, value: int) -> None:
        bar = self.verticalScrollBar()
        self._pinned = value >= bar.maximum() - 60

    def _follow(self, _minimum: int, maximum: int) -> None:
        if self._pinned:
            self.verticalScrollBar().setValue(maximum)

    def scroll_to_end(self) -> None:
        self._pinned = True
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def clear(self) -> None:
        for widget in [*self._messages, self._welcome]:
            if widget is not None:
                self._layout.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
        self._messages = []
        self._welcome = None
        self._welcome_html = ""

    def show_welcome(self, title: str, body_html: str, suggestions: list[str], tone_note: tuple[str, str] | None) -> None:
        self.clear()
        box = QWidget()
        box.setObjectName("plain")
        row = QHBoxLayout(box)
        row.setContentsMargins(12, 26, 12, 10)
        column = QWidget()
        column.setObjectName("plain")
        column.setMaximumWidth(640)
        row.addStretch(1)
        row.addWidget(column, 20)
        row.addStretch(1)
        lay = QVBoxLayout(column)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(widgets.icon_badge("sparkles", "LINK", 48), 0, Qt.AlignmentFlag.AlignHCenter)
        head = QLabel(title)
        head.setObjectName("h1")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.setWordWrap(True)
        lay.addWidget(head)
        body = QLabel(body_html)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setObjectName("muted")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        body.setOpenExternalLinks(False)
        body.linkActivated.connect(self.link_clicked.emit)
        lay.addWidget(body)
        if tone_note:
            banner = widgets.Banner(tone_note[1], tone_note[0])
            banner.label.setTextFormat(Qt.TextFormat.PlainText)
            lay.addWidget(banner)
        if suggestions:
            lay.addSpacing(6)
            for suggestion in suggestions:
                chip = QPushButton(suggestion)
                chip.setProperty("variant", "secondary")
                chip.setCursor(Qt.CursorShape.PointingHandCursor)
                chip.setStyleSheet("text-align: left; padding: 9px 14px;")
                widgets.set_icon(chip, "message-square", "FAINT", 15)
                chip.clicked.connect(lambda _=False, s=suggestion: self.suggestion_clicked.emit(s))
                lay.addWidget(chip)
        self._welcome = box
        self._welcome_html = f"<h3>{html.escape(title)}</h3>{body_html}" + (
            f"<p>{html.escape(tone_note[1])}</p>" if tone_note else ""
        )
        self._layout.insertWidget(0, box)
        box.show()

    def _drop_welcome(self) -> None:
        if self._welcome is not None:
            self._layout.removeWidget(self._welcome)
            self._welcome.hide()
            self._welcome.deleteLater()
            self._welcome = None
            self._welcome_html = ""

    def add_message(self, role: str) -> _MessageWidget:
        self._drop_welcome()
        widget = _MessageWidget(role)
        widget.cite_clicked.connect(lambda n, w=widget: self.cite_clicked.emit(w, n))
        widget.copy_requested.connect(lambda text: QApplication.clipboard().setText(text))
        self._layout.insertWidget(self._layout.count() - 1, widget)
        widget.show()
        self._messages.append(widget)
        return widget

    def remove_message(self, widget: _MessageWidget | None) -> None:
        if widget is not None and widget in self._messages:
            self._messages.remove(widget)
            self._layout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()

    def messages(self) -> list[_MessageWidget]:
        return list(self._messages)

    def toHtml(self) -> str:
        return self._welcome_html + "\n".join(
            f'<div class="{m.role}">{m.html()}</div>' for m in self._messages
        )

    def toPlainText(self) -> str:
        return "\n\n".join(m.markdown() for m in self._messages)


class AssistantView(QWidget):
    """Panel del Asistente de Estudio (fuentes · conversación · herramientas)."""

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.store = StateStore(self.settings)
        self.engine = RAGEngine(self.settings, self.store)

        self.sync_worker: RagSyncWorker | None = None
        self.ingest_worker: RagIngestFilesWorker | None = None
        self.query_worker: RagQueryWorker | None = None
        self.test_worker: GeminiConnectionTestWorker | None = None
        self.study_worker: StudyWorker | None = None
        self.models_worker: ModelRefreshWorker | None = None
        self.demo_responder: Callable[[str, list[int]], RAGResponse] | None = None
        self.demo_study: Callable[[str, int], study.StudyResult] | None = None

        self._initial_sync_done = False
        self._updating_tree = False
        self._current_session_id: int | None = None
        self._session_choice: dict[str, int] = {}
        self._countdown_remaining = 0
        self._pending_question = ""
        self._pending_user: _MessageWidget | None = None
        self._pending_reply: _MessageWidget | None = None
        self._stream_text = ""
        self._sources_cache: list[dict] = []

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(70)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.timeout.connect(self._paint_stream)

        self._build_ui()

    # ── Construcción ──────────────────────────────────────────────
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        # Ámbito
        scope_card = QFrame()
        scope_card.setObjectName("card")
        scope_lay = QHBoxLayout(scope_card)
        scope_lay.setContentsMargins(12, 9, 12, 9)
        scope_lay.setSpacing(8)
        self.sources_toggle_btn = widgets.icon_button(
            "panel-left", "Mostrar u ocultar el panel de fuentes", "MUTED", 17, checkable=True
        )
        self.sources_toggle_btn.setChecked(True)
        self.sources_toggle_btn.toggled.connect(self._toggle_sources_panel)
        self.course_combo = QComboBox()
        self.course_combo.setAccessibleName("Curso para consultar en el Asistente IA")
        self.course_combo.setMinimumWidth(120)
        self.course_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.course_combo.currentIndexChanged.connect(self._on_scope_changed)
        self.all_courses_btn = QPushButton("Todos los cursos")
        self.all_courses_btn.setCheckable(True)
        self.all_courses_btn.setProperty("variant", "chip")
        self.all_courses_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_courses_btn.setToolTip(
            "Modo global: pregunta combinando las fuentes de todos tus cursos."
        )
        widgets.set_icon(self.all_courses_btn, "globe", "MUTED", 14)
        self.all_courses_btn.toggled.connect(self._on_all_courses_toggled)

        self.materials_menu_btn = widgets.button(
            "Añadir", "plus", "secondary", "Añade archivos o vuelve a leer tus carpetas."
        )
        self.materials_menu = QMenu(self)
        self.materials_menu.addAction(
            "Añadir archivos (PDF, PPTX, Word, TXT)…", self._on_sync_or_add_materials
        )
        self.materials_menu.addAction("Buscar clases y materiales nuevos ahora", self.sync_transcripts)
        self.materials_menu.addSeparator()
        self.materials_menu.addAction("Abrir carpeta de materiales", self._open_materials_folder)
        self.materials_menu_btn.setMenu(self.materials_menu)

        self.api_status_btn = widgets.button("Configuración IA", "settings", "secondary")
        self.api_status_btn.clicked.connect(self._open_api_settings_dialog)

        scope_lay.addWidget(self.sources_toggle_btn)
        scope_lay.addWidget(self.course_combo, 1)
        scope_lay.addWidget(self.all_courses_btn)
        scope_lay.addWidget(self.materials_menu_btn)
        scope_lay.addWidget(self.api_status_btn)
        outer.addWidget(scope_card)

        # Controles heredados (se conservan, ocultos, para integraciones y pruebas)
        self.sync_btn = QPushButton("Sincronizar / Añadir Materiales", self)
        self.sync_btn.clicked.connect(self._on_sync_or_add_materials)
        self.sync_btn.hide()
        self.rescan_btn = QPushButton("Re-escanear TXT", self)
        self.rescan_btn.clicked.connect(self.sync_transcripts)
        self.rescan_btn.hide()
        self.api_key_edit = QLineEdit(self.settings.gemini_api_key, self)
        self.api_key_edit.hide()
        self.api_key_edit.editingFinished.connect(self._save_inline_api_key)
        self.test_api_btn = QPushButton("Probar conexión", self)
        self.test_api_btn.clicked.connect(self._test_api_connection)
        self.test_api_btn.hide()
        self.semantic_chk = QCheckBox("Embeddings", self)
        self.semantic_chk.setChecked(self.settings.use_semantic_embeddings)
        self.semantic_chk.toggled.connect(self._on_semantic_toggled)
        self.semantic_chk.hide()
        self.get_key_btn = QPushButton("Obtener clave gratis", self)
        self.get_key_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AI_STUDIO_APIKEY_URL)))
        self.get_key_btn.hide()
        self.guide_btn = QPushButton("Cómo configurar", self)
        self.guide_btn.clicked.connect(self._show_api_guide)
        self.guide_btn.hide()
        self.open_folder_btn = QPushButton("Abrir carpeta de materiales", self)
        self.open_folder_btn.clicked.connect(self._open_materials_folder)
        self.open_folder_btn.hide()

        self.progress_bar = QProgressBar()
        self.progress_bar.setProperty("thin", "true")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        outer.addWidget(self.progress_bar)
        self.scope_status = QLabel("")
        self.scope_status.setObjectName("faint")
        self.scope_status.setWordWrap(True)
        self.scope_status.hide()
        outer.addWidget(self.scope_status)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        # ── Fuentes ───────────────────────────────────────────────
        self.sources_card = QFrame()
        self.sources_card.setObjectName("card")
        self.sources_card.setMinimumWidth(240)
        self.sources_card.setMaximumWidth(400)
        sources_lay = QVBoxLayout(self.sources_card)
        sources_lay.setContentsMargins(12, 12, 12, 12)
        sources_lay.setSpacing(8)
        sources_header = QHBoxLayout()
        sources_title = QLabel("Fuentes")
        sources_title.setObjectName("h2")
        self.sources_count_lbl = widgets.chip("0 activas")
        sources_header.addWidget(sources_title)
        sources_header.addStretch()
        sources_header.addWidget(self.sources_count_lbl)
        sources_lay.addLayout(sources_header)

        self.week_filter_combo = QComboBox()
        self.week_filter_combo.setAccessibleName("Filtro rápido por semanas del ciclo")
        self.week_filter_combo.setToolTip(
            "Marca de golpe las semanas del parcial o del final. Las fuentes sin semana se conservan."
        )
        self.week_filter_combo.addItem("Todas las semanas", "all")
        self.week_filter_combo.addItem("Semanas 1–7 (Parcial)", "parcial")
        self.week_filter_combo.addItem("Semanas 8–16 (Final)", "final")
        self.week_filter_combo.currentIndexChanged.connect(self._on_week_filter_changed)
        sources_lay.addWidget(self.week_filter_combo)

        self.sources_search = widgets.SearchField("Filtrar fuentes…")
        self.sources_search.textChanged.connect(self._filter_sources_tree)
        sources_lay.addWidget(self.sources_search)

        sources_actions = QHBoxLayout()
        sources_actions.setSpacing(6)
        self.check_all_btn = widgets.button("Marcar todas", "square-check", "ghost")
        self.check_all_btn.clicked.connect(lambda: self._set_all_sources_checked(True))
        self.uncheck_all_btn = widgets.button("Ninguna", "square", "ghost")
        self.uncheck_all_btn.clicked.connect(lambda: self._set_all_sources_checked(False))
        sources_actions.addWidget(self.check_all_btn)
        sources_actions.addWidget(self.uncheck_all_btn)
        sources_actions.addStretch()
        sources_lay.addLayout(sources_actions)

        self.sources_tree = QTreeWidget()
        self.sources_tree.setHeaderHidden(True)
        self.sources_tree.setRootIsDecorated(True)
        self.sources_tree.setIndentation(14)
        self.sources_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sources_tree.customContextMenuRequested.connect(self._source_context_menu)
        self.sources_tree.itemChanged.connect(self._on_source_item_changed)
        self.sources_tree.itemDoubleClicked.connect(lambda item, _c: self._open_source_item(item))
        sources_lay.addWidget(self.sources_tree, 1)

        self.sources_empty = QLabel(
            "Aún no hay fuentes en este curso. Tus transcripciones se añaden solas; usa "
            "«Añadir» para sumar PDF, diapositivas o Word."
        )
        self.sources_empty.setObjectName("faint")
        self.sources_empty.setWordWrap(True)
        self.sources_empty.hide()
        sources_lay.addWidget(self.sources_empty)

        self.context_meter = QLabel("")
        self.context_meter.setObjectName("faint")
        self.context_meter.setWordWrap(True)
        sources_lay.addWidget(self.context_meter)
        self.splitter.addWidget(self.sources_card)

        # ── Conversación ──────────────────────────────────────────
        chat_card = QFrame()
        chat_card.setObjectName("card")
        chat_lay = QVBoxLayout(chat_card)
        chat_lay.setContentsMargins(16, 12, 16, 14)
        chat_lay.setSpacing(8)

        chat_header = QHBoxLayout()
        chat_header.setSpacing(4)
        self.chat_title = widgets.ElidedLabel("Conversación")
        self.chat_title.setObjectName("h2")
        chat_header.addWidget(self.chat_title, 1)
        self.sessions_btn = widgets.icon_button("history", "Conversaciones anteriores")
        self.sessions_btn.clicked.connect(self._show_sessions_menu)
        self.new_chat_btn = widgets.icon_button("message-square-plus", "Nueva conversación")
        self.new_chat_btn.clicked.connect(self._new_chat)
        self.export_btn = widgets.icon_button("download", "Exportar conversación (Markdown o texto)")
        self.export_btn.clicked.connect(self._export_chat)
        self.clear_btn = widgets.icon_button("trash-2", "Vaciar esta conversación")
        self.clear_btn.clicked.connect(self._clear_chat)
        for btn in (self.sessions_btn, self.new_chat_btn, self.export_btn, self.clear_btn):
            chat_header.addWidget(btn)
        chat_lay.addLayout(chat_header)
        chat_lay.addWidget(widgets.divider())

        self.chat_view = ChatTranscript()
        self.chat_view.cite_clicked.connect(self._on_cite_clicked)
        self.chat_view.suggestion_clicked.connect(self._trigger_quick_prompt)
        self.chat_view.link_clicked.connect(self._on_welcome_link)
        chat_lay.addWidget(self.chat_view, 1)

        self.rate_limit_banner = widgets.Banner("", "warning")
        self.rate_limit_banner.hide()
        chat_lay.addWidget(self.rate_limit_banner)

        busy_row = QHBoxLayout()
        busy_row.setSpacing(8)
        self.busy_spinner = widgets.Spinner(16)
        self.analyzing_label = QLabel("Analizando fuentes…")
        self.analyzing_label.setObjectName("accent")
        busy_row.addWidget(self.busy_spinner)
        busy_row.addWidget(self.analyzing_label, 1)
        self.busy_host = QWidget()
        self.busy_host.setLayout(busy_row)
        busy_row.setContentsMargins(0, 0, 0, 0)
        self.busy_host.hide()
        chat_lay.addWidget(self.busy_host)

        # Herramientas de estudio
        tools_host = QWidget()
        tools_flow = widgets.FlowLayout(tools_host, 6)
        self.quick_prompt_btns: list[QPushButton] = []
        for key in TOOL_ORDER:
            tool = study.TOOLS[key]
            chip = QPushButton(tool.label)
            chip.setProperty("variant", "chip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip(tool.hint)
            widgets.set_icon(chip, tool.icon, "MUTED", 14)
            chip.clicked.connect(lambda _=False, k=key: self.run_tool(k))
            tools_flow.addWidget(chip)
            self.quick_prompt_btns.append(chip)
        self.saved_btn = QPushButton("Guardados")
        self.saved_btn.setProperty("variant", "chip")
        self.saved_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.saved_btn.setToolTip("Tarjetas y simulacros que ya generaste; repetirlos no gasta cuota.")
        widgets.set_icon(self.saved_btn, "bookmark", "MUTED", 14)
        self.saved_btn.clicked.connect(self._show_saved_menu)
        tools_flow.addWidget(self.saved_btn)
        chat_lay.addWidget(tools_host)

        # Entrada
        self.composer = QFrame()
        self.composer.setObjectName("composerBox")
        composer_lay = QVBoxLayout(self.composer)
        composer_lay.setContentsMargins(12, 8, 8, 8)
        composer_lay.setSpacing(4)
        self.input_edit = ChatInputEdit(self._send_question)
        self.input_edit.installEventFilter(self)
        composer_lay.addWidget(self.input_edit)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.model_combo = QComboBox()
        self.model_combo.setAccessibleName("Modelo de Gemini")
        self.model_combo.setMinimumWidth(150)
        self.model_combo.setToolTip(
            "Automático elige el modelo según la tarea y la cuota disponible; también puedes fijar uno."
        )
        self._fill_models()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.grounding_btn = QPushButton("Solo mis fuentes")
        self.grounding_btn.setCheckable(True)
        self.grounding_btn.setProperty("variant", "chip")
        self.grounding_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.grounding_btn.setChecked(self.settings.ai_grounding != "tutor")
        self.grounding_btn.setToolTip(
            "Activado: responde únicamente con lo que dicen tus fuentes.\n"
            "Desactivado: además puede complementar con conocimiento general, en un apartado aparte."
        )
        self._sync_grounding_label()
        self.grounding_btn.toggled.connect(self._on_grounding_toggled)
        self.sources_indicator_lbl = widgets.ElidedLabel("")
        self.sources_indicator_lbl.setObjectName("faint")
        self.stop_btn = widgets.button("Detener", "square", "secondary")
        self.stop_btn.clicked.connect(self._stop_generation)
        self.stop_btn.hide()
        self.send_btn = widgets.button("Preguntar", "arrow-up", "primary", "Enviar (Enter)")
        self.send_btn.clicked.connect(lambda: self._send_question())
        controls.addWidget(self.model_combo)
        controls.addWidget(self.grounding_btn)
        controls.addWidget(self.sources_indicator_lbl, 1)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.send_btn)
        composer_lay.addLayout(controls)
        chat_lay.addWidget(self.composer)

        self.splitter.addWidget(chat_card)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([290, 700])
        outer.addWidget(self.splitter, 1)
        style.on_theme_change(self._on_theme_changed)

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "input_edit", None) and event.type() in (
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
        ):
            self.composer.setProperty("focused", "true" if event.type() == QEvent.Type.FocusIn else "false")
            widgets.repolish(self.composer)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        narrow = self.width() < 720
        if narrow and self.sources_toggle_btn.isChecked() and not getattr(self, "_auto_hidden", False):
            self._auto_hidden = True
            self.sources_toggle_btn.setChecked(False)
        elif not narrow and getattr(self, "_auto_hidden", False):
            self._auto_hidden = False
            self.sources_toggle_btn.setChecked(True)
        if narrow != getattr(self, "_compact", None):
            # En ventanas angostas las etiquetas largas se acortan para no desbordar.
            self._compact = narrow
            self.all_courses_btn.setText("Todos" if narrow else "Todos los cursos")
            self.materials_menu_btn.setText("" if narrow else "Añadir")
            self.model_combo.setMinimumWidth(118 if narrow else 150)
            self._update_api_status_btn()
            self._sync_grounding_label()

    def _toggle_sources_panel(self, visible: bool) -> None:
        self.sources_card.setVisible(visible)

    def _on_theme_changed(self) -> None:
        if not self.chat_view.messages():
            self._render_messages([])

    def _fill_models(self) -> None:
        current = self.settings.gemini_model or AUTO_MODEL
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem("Automático", AUTO_MODEL)
        self.model_combo.setItemData(
            0, "Elige el modelo según la tarea y salta los que agotaron su cuota.", Qt.ItemDataRole.ToolTipRole
        )
        try:
            models = self.engine.router.available()
        except Exception:  # noqa: BLE001
            models = []
        for info in models:
            self.model_combo.addItem(info.short, info.id)
            self.model_combo.setItemData(
                self.model_combo.count() - 1, f"{info.id}\n{info.blurb}", Qt.ItemDataRole.ToolTipRole
            )
        index = self.model_combo.findData(current)
        self.model_combo.setCurrentIndex(max(0, index))
        self.model_combo.blockSignals(False)

    # ── Ciclo de vida y sincronización ────────────────────────────
    def refresh(self) -> None:
        """Actualiza cursos, fuentes y conversación; sincroniza lo nuevo en segundo plano."""
        self.api_key_edit.setText(self.settings.gemini_api_key)
        self._fill_models()
        self.semantic_chk.setChecked(self.settings.use_semantic_embeddings)
        self._update_api_status_btn()

        self._reload_courses()
        self._reload_sources_tree()
        self._reload_chat_history()

        # Auto-sincronización silenciosa de clases y materiales en segundo plano
        self.sync_transcripts(silent_if_empty=True)
        self._maybe_refresh_models()

    def _maybe_refresh_models(self) -> None:
        if self.demo_responder is not None or not self._has_key():
            return
        if self.models_worker and self.models_worker.isRunning():
            return
        try:
            if self.engine.router.discovery_is_fresh():
                return
        except Exception:  # noqa: BLE001
            return
        self.models_worker = ModelRefreshWorker(self.engine, self)
        self.models_worker.done.connect(lambda _models: self._fill_models())
        self.models_worker.start()

    def _has_key(self) -> bool:
        from zoom2txt.config import load_gemini_api_key

        return bool(self.settings.gemini_api_key.strip() or load_gemini_api_key())

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

    def _scope_key(self) -> str:
        return "__ALL__" if self.is_all_courses_mode() else str(self.current_course_id() or "")

    def _on_all_courses_toggled(self, checked: bool) -> None:
        self.course_combo.setEnabled(not checked)
        widgets.set_icon(self.all_courses_btn, "globe", "ON_ACCENT" if checked else "MUTED", 14)
        self._reload_sources_tree()
        self._reload_chat_history()

    def _on_scope_changed(self) -> None:
        self._reload_sources_tree()
        self._reload_chat_history()

    # ── Árbol de fuentes ──────────────────────────────────────────
    def _reload_sources_tree(self) -> None:
        self._updating_tree = True
        try:
            self.sources_tree.clear()
            all_mode = self.is_all_courses_mode()
            cid = self.current_course_id()
            if not all_mode and not cid:
                self._sources_cache = []
                self._update_source_counts()
                return

            sources = self.store.list_knowledge_sources(
                course_id=cid, all_courses=all_mode, active_only=False
            )
            self._sources_cache = sources
            sources = sorted(
                sources,
                key=lambda s: (
                    0 if s["source_type"] == "transcript" else 1,
                    infer_source_week(s["title"]) or 99,
                    s["title"].casefold(),
                ),
            )

            def add(parent, src) -> None:
                icon_name, badge = SOURCE_BADGES.get(src["source_type"], ("file-text", "Fuente"))
                item = QTreeWidgetItem(parent, [src["title"]])
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsSelectable
                )
                item.setCheckState(
                    0, Qt.CheckState.Checked if src["is_active"] else Qt.CheckState.Unchecked
                )
                item.setData(0, Qt.ItemDataRole.UserRole, int(src["id"]))
                item.setData(0, Qt.ItemDataRole.UserRole + 1, src["file_path"])
                words = int(src.get("word_count") or 0)
                item.setToolTip(
                    0,
                    f"{badge} · {src['course_name']}\n"
                    + (f"{words:,} palabras\n".replace(",", " ") if words else "")
                    + src["file_path"],
                )
                item.setIcon(0, lucide.icon(icon_name, style.FAINT, 15))

            if all_mode:
                by_course: dict[str, list[dict]] = {}
                for src in sources:
                    by_course.setdefault(src["course_name"] or src["course_id"], []).append(src)
                for cname, course_sources in sorted(by_course.items(), key=lambda x: x[0].casefold()):
                    parent_item = QTreeWidgetItem(self.sources_tree, [cname])
                    parent_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    font = parent_item.font(0)
                    font.setBold(True)
                    parent_item.setFont(0, font)
                    for src in course_sources:
                        add(parent_item, src)
                    parent_item.setExpanded(True)
            else:
                for src in sources:
                    add(self.sources_tree, src)
            self._filter_sources_tree()
            self._update_source_counts()
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

    def _update_source_counts(self) -> None:
        all_items = self._iter_source_items()
        active_ids = {
            int(it.data(0, Qt.ItemDataRole.UserRole))
            for it in all_items
            if it.checkState(0) == Qt.CheckState.Checked
        }
        self.sources_count_lbl.setText(f"{len(active_ids)}/{len(all_items)} activas")
        widgets.set_tone(self.sources_count_lbl, "success" if active_ids else "")
        self.sources_empty.setVisible(not all_items)
        words = sum(int(s.get("word_count") or 0) for s in self._sources_cache if int(s["id"]) in active_ids)
        if not active_ids:
            self.sources_indicator_lbl.setFullText("Sin fuentes · conocimiento general")
            self.context_meter.setText(
                "Sin fuentes marcadas, el asistente responde con conocimiento general y lo avisa."
            )
        else:
            noun = "fuente" if len(active_ids) == 1 else "fuentes"
            self.sources_indicator_lbl.setFullText(f"{len(active_ids)} {noun} activas")
            amount = f"{words:,}".replace(",", " ")
            if words and words <= FULL_CONTEXT_WORDS["chat"]:
                self.context_meter.setText(
                    f"{amount} palabras: caben completas, el asistente leerá todo el texto."
                )
            elif words:
                self.context_meter.setText(
                    f"{amount} palabras: se buscarán los fragmentos más relevantes para cada pregunta. "
                    "Marca menos fuentes para que las lea completas."
                )
            else:
                self.context_meter.setText("")

    def _on_source_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_tree:
            return
        sid = item.data(0, Qt.ItemDataRole.UserRole)
        if sid is None:
            return
        checked = item.checkState(0) == Qt.CheckState.Checked
        self.store.set_source_active(int(sid), checked)
        for src in self._sources_cache:
            if int(src["id"]) == int(sid):
                src["is_active"] = checked
        self._update_source_counts()

    def _apply_checks(self, decide: Callable[[QTreeWidgetItem], bool]) -> None:
        self._updating_tree = True
        try:
            states: dict[int, bool] = {}
            for item in self._iter_source_items():
                checked = bool(decide(item))
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                states[int(item.data(0, Qt.ItemDataRole.UserRole))] = checked
            self.store.set_sources_active(states)
            for src in self._sources_cache:
                if int(src["id"]) in states:
                    src["is_active"] = states[int(src["id"])]
        finally:
            self._updating_tree = False
        self._update_source_counts()

    def _set_all_sources_checked(self, checked: bool) -> None:
        self._apply_checks(lambda _item: checked)

    def _on_week_filter_changed(self) -> None:
        mode = self.week_filter_combo.currentData()

        def decide(item: QTreeWidgetItem) -> bool:
            if mode == "all":
                return True
            week = infer_source_week(item.text(0)) or infer_source_week(
                str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
            )
            if mode == "parcial":
                return week is None or (1 <= week <= 7)
            return week is None or (8 <= week <= 20)

        self._apply_checks(decide)

    def _filter_sources_tree(self) -> None:
        from zoom2txt.gui.command_palette import _fold

        query = _fold(self.sources_search.text()).strip()
        for item in self._iter_source_items():
            item.setHidden(bool(query) and query not in _fold(item.text(0)))

    def _source_context_menu(self, position) -> None:
        item = self.sources_tree.itemAt(position)
        if item is None or item.data(0, Qt.ItemDataRole.UserRole) is None:
            return
        menu = QMenu(self)
        menu.addAction("Leer en Zoom2TXT", lambda: self._open_source_item(item))
        menu.addAction("Usar solo esta fuente", lambda: self._apply_checks(lambda it: it is item))
        menu.addSeparator()
        menu.addAction("Quitar del asistente (no borra el archivo)", lambda: self._remove_source(item))
        menu.exec(self.sources_tree.viewport().mapToGlobal(position))

    def _open_source_item(self, item: QTreeWidgetItem) -> None:
        sid = item.data(0, Qt.ItemDataRole.UserRole)
        if sid is None:
            return
        self._open_reader(
            {"path": item.data(0, Qt.ItemDataRole.UserRole + 1), "source_title": item.text(0), "source_id": sid}
        )

    def _remove_source(self, item: QTreeWidgetItem) -> None:
        sid = item.data(0, Qt.ItemDataRole.UserRole)
        if sid is None:
            return
        self.store.delete_knowledge_source(int(sid))
        self._reload_sources_tree()

    def focus_on_path(self, source_path: str) -> None:
        """Deja activa una sola fuente (la del archivo indicado) y abre su curso."""
        target = str(Path(source_path).resolve()) if source_path else ""
        match = next(
            (
                s
                for s in self.store.list_knowledge_sources(all_courses=True)
                if str(s["file_path"]) == target
            ),
            None,
        )
        if match is None:
            try:
                from zoom2txt.rag_engine import ingest_file

                sid = ingest_file(self.settings, Path(source_path), store=self.store)
            except Exception:  # noqa: BLE001
                sid = None
            if sid is None:
                self._set_status("No se pudo abrir esa fuente en el asistente.")
                return
            match = next(
                s for s in self.store.list_knowledge_sources(all_courses=True) if int(s["id"]) == int(sid)
            )
        if self.all_courses_btn.isChecked():
            self.all_courses_btn.setChecked(False)
        self._reload_courses()
        index = self.course_combo.findData(match["course_id"])
        if index >= 0:
            self.course_combo.setCurrentIndex(index)
        self._reload_sources_tree()
        wanted = int(match["id"])
        self._apply_checks(lambda it: int(it.data(0, Qt.ItemDataRole.UserRole)) == wanted)
        self._set_status(f"Contexto: solo «{match['title']}».")
        self.input_edit.setFocus()

    def _trigger_quick_prompt(self, prompt_text: str) -> None:
        self.input_edit.setPlainText(prompt_text)
        self._send_question()

    def _set_status(self, text: str) -> None:
        self.scope_status.setText(text)
        self.scope_status.setVisible(bool(text))

    # ── Sincronización e ingesta de materiales ────────────────────
    def sync_transcripts(self, silent_if_empty: bool = False) -> None:
        if type(self.window()).__name__ == "DemoMainWindow":
            return
        if self.sync_worker and self.sync_worker.isRunning():
            return
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.sync_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        if not silent_if_empty:
            self._set_status("Buscando transcripciones y materiales nuevos…")

        self.sync_worker = RagSyncWorker(
            self.settings, course_id=self.current_course_id(), parent=self
        )
        self.sync_worker.progress.connect(self._on_ingest_progress)
        self.sync_worker.done.connect(
            lambda stats: self._on_sync_done(stats, silent_if_empty=silent_if_empty)
        )
        self.sync_worker.failed.connect(self._on_ingest_failed)
        self.sync_worker.finished.connect(self.progress_bar.hide)
        self.sync_worker.start()

    def _on_sync_or_add_materials(self) -> None:
        if (self.sync_worker and self.sync_worker.isRunning()) or (
            self.ingest_worker and self.ingest_worker.isRunning()
        ):
            return

        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Añadir materiales de estudio al curso",
            str(self.settings.materials_dir if self.settings.materials_dir.exists() else Path.home()),
            "Documentos de curso (*.pdf *.pptx *.docx *.txt *.md)",
        )
        if not files:
            return

        cid = self.current_course_id() or "general"
        cname = (
            self.course_combo.currentText() if self.course_combo.currentData() else "Curso General"
        )
        paths = [Path(f) for f in files]

        self.progress_bar.setRange(0, len(paths))
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.sync_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        self._set_status(f"Indexando {len(paths)} archivo(s) en «{cname}»…")

        self.ingest_worker = RagIngestFilesWorker(
            self.settings, paths, course_id=cid, course_name=cname, parent=self
        )
        self.ingest_worker.progress.connect(self._on_ingest_progress)
        self.ingest_worker.done.connect(self._on_files_ingested)
        self.ingest_worker.failed.connect(self._on_ingest_failed)
        self.ingest_worker.start()

    def _on_ingest_progress(self, current: int, total: int, filename: str) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(current)
        # La primera indexación de un ciclo entero puede tardar minutos: se
        # informa el avance en cuanto deja de ser instantánea.
        started = getattr(self, "_ingest_started", None)
        if started is None or current <= 1:
            self._ingest_started = started = time.monotonic()
        if time.monotonic() - started > 2.5 and total:
            self._set_status(f"Preparando tus fuentes para el asistente… {current} de {total}")

    def _on_sync_done(self, stats: dict, silent_if_empty: bool = False) -> None:
        self.progress_bar.hide()
        self.sync_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        before = len(self._sources_cache)
        self._reload_courses()
        self._reload_sources_tree()
        added = len(self._sources_cache) - before
        self._ingest_started = None
        if self.scope_status.text().startswith("Preparando tus fuentes"):
            self._set_status("")
        if not silent_if_empty:
            t_idx = stats.get("transcripts_indexed", 0)
            m_idx = stats.get("materials_indexed", 0)
            self._set_status(f"Índice al día: {t_idx} transcripciones y {m_idx} materiales listos.")
        elif added > 0:
            self._set_status(f"Se añadieron {added} fuentes nuevas a este curso.")
        if not self.chat_view.messages():
            self._render_messages([])

    def _on_files_ingested(self, ok: int, failed: int) -> None:
        self.progress_bar.hide()
        self.sync_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self._reload_courses()
        self._reload_sources_tree()
        self._set_status(
            f"Materiales añadidos: {ok} indexados" + (f", {failed} no se pudieron leer." if failed else ".")
        )

    def _on_ingest_failed(self, error: str) -> None:
        self.progress_bar.hide()
        self.sync_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self._set_status(f"No se pudo completar la sincronización: {error}")

    # ── Configuración ─────────────────────────────────────────────
    def _save_inline_api_key(self) -> None:
        key = self.api_key_edit.text().strip()
        if key != self.settings.gemini_api_key:
            self.settings.gemini_api_key = key
            save_gemini_api_key(key)

    def _on_model_changed(self) -> None:
        model = self.model_combo.currentData()
        if model:
            self.settings.gemini_model = model
            self.settings.save()

    def _sync_grounding_label(self) -> None:
        strict = self.grounding_btn.isChecked()
        compact = bool(getattr(self, "_compact", False))
        self.grounding_btn.setText(
            ("Solo fuentes" if compact else "Solo mis fuentes")
            if strict
            else ("+ general" if compact else "Fuentes + general")
        )
        widgets.set_icon(
            self.grounding_btn, "shield-check", "ON_ACCENT" if strict else "MUTED", 14
        )

    def _on_grounding_toggled(self, strict: bool) -> None:
        self.settings.ai_grounding = "strict" if strict else "tutor"
        self._sync_grounding_label()
        try:
            self.settings.save()
        except OSError:
            pass

    def _on_semantic_toggled(self, checked: bool) -> None:
        self.settings.use_semantic_embeddings = checked
        self.settings.save()

    def _open_materials_folder(self) -> None:
        self.settings.materials_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.materials_dir)))

    def _open_api_settings_dialog(self) -> None:
        dlg = ApiKeyConfigDialog(self.settings, self, self.engine)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.api_key_edit.setText(self.settings.gemini_api_key)
            self.semantic_chk.setChecked(self.settings.use_semantic_embeddings)
            self._update_api_status_btn()
            self._fill_models()
            self._maybe_refresh_models()
            if not self.chat_view.messages():
                self._render_messages([])

    def _update_api_status_btn(self) -> None:
        has_key = self._has_key()
        compact = bool(getattr(self, "_compact", False))
        if has_key:
            self.api_status_btn.setText("IA" if compact else "IA conectada")
            self.api_status_btn.setToolTip(
                "Tu clave de Gemini está lista. Clic para cambiarla o ver el uso de hoy."
            )
            self.api_status_btn.setProperty("variant", "secondary")
            widgets.set_icon(self.api_status_btn, "circle-check", "SUCCESS", 16)
        else:
            self.api_status_btn.setText("Conectar" if compact else "Conectar IA")
            self.api_status_btn.setProperty("variant", "primary")
            self.api_status_btn.setToolTip(
                "Configura tu clave gratuita de Google AI Studio (sin tarjeta)."
            )
            widgets.set_icon(self.api_status_btn, "key-round", "ON_ACCENT", 16)
        widgets.repolish(self.api_status_btn)

    def _show_api_guide(self) -> None:
        ApiKeyGuideDialog(self).exec()

    def _on_welcome_link(self, target: str) -> None:
        if target == "action:connect":
            self._open_api_settings_dialog()
        elif target == "action:guide":
            self._show_api_guide()
        elif target.startswith("http"):
            QDesktopServices.openUrl(QUrl(target))

    def _test_api_connection(self) -> None:
        if self.test_worker and self.test_worker.isRunning():
            return
        self._save_inline_api_key()
        key = self.api_key_edit.text().strip()
        self.test_api_btn.setEnabled(False)
        self.test_worker = GeminiConnectionTestWorker(key, self.model_combo.currentData(), self)
        self.test_worker.finished_test.connect(self._on_test_api_finished)
        self.test_worker.start()

    def _on_test_api_finished(self, ok: bool, message: str) -> None:
        self.test_api_btn.setEnabled(True)
        self._set_status(message)

    # ── Conversaciones ────────────────────────────────────────────
    def _reload_chat_history(self) -> None:
        all_mode = self.is_all_courses_mode()
        cid = self.current_course_id()
        title = "Todos los cursos" if all_mode else self.current_course_name()
        self.chat_title.setFullText(f"Conversación · {title}")
        chosen = self._session_choice.get(self._scope_key())
        valid = {s["id"] for s in self.store.list_chat_sessions(cid, all_mode, limit=200)}
        if chosen in valid:
            self._current_session_id = chosen
        else:
            self._current_session_id = self.store.get_or_create_chat_session(
                course_id=cid, all_courses=all_mode, title=title
            )
        self._discard_pending()
        self._render_messages(self.store.get_chat_messages(self._current_session_id))

    def _new_chat(self) -> None:
        if self.is_busy_generating():
            return
        if not self.store.get_chat_messages(self._current_session_id or -1):
            self.input_edit.setFocus()
            return
        session = self.store.create_chat_session(
            self.current_course_id(), self.is_all_courses_mode(), self.current_course_name()
        )
        self._session_choice[self._scope_key()] = session
        self._reload_chat_history()
        self.input_edit.setFocus()

    def _show_sessions_menu(self) -> None:
        sessions = self.store.list_chat_sessions(
            self.current_course_id(), self.is_all_courses_mode(), limit=20
        )
        menu = QMenu(self)
        shown = 0
        for session in sessions:
            if not session["messages"]:
                continue
            first = " ".join(session["first_question"].split())
            label = (first[:52] + "…") if len(first) > 53 else first or "Conversación"
            stamp = session["updated_at"][:10]
            action = menu.addAction(f"{label}   ·   {stamp}")
            action.setCheckable(True)
            action.setChecked(session["id"] == self._current_session_id)
            action.triggered.connect(lambda _=False, sid=session["id"]: self._select_session(sid))
            shown += 1
        if not shown:
            empty = menu.addAction("Aún no hay conversaciones guardadas en este curso")
            empty.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Nueva conversación", self._new_chat)
        if shown:
            menu.addAction("Eliminar la conversación actual", self._delete_session)
        menu.exec(self.sessions_btn.mapToGlobal(self.sessions_btn.rect().bottomLeft()))

    def _select_session(self, session_id: int) -> None:
        if self.is_busy_generating():
            return
        self._session_choice[self._scope_key()] = session_id
        self._reload_chat_history()

    def _delete_session(self) -> None:
        if self._current_session_id is None or self.is_busy_generating():
            return
        self.store.delete_chat_session(self._current_session_id)
        self._session_choice.pop(self._scope_key(), None)
        self._reload_chat_history()

    def _welcome(self) -> None:
        scope = (
            "todos tus cursos"
            if self.is_all_courses_mode()
            else f"«{html.escape(self.current_course_name())}»"
        )
        active = len(self.get_active_source_ids())
        if self.demo_responder is not None:
            note = ("success", "Modo demostración: datos sintéticos, sin conexión ni clave.")
        elif self._has_key():
            note = None
        else:
            note = (
                "info",
                "Para preguntar necesitas una clave gratuita de Google AI Studio "
                "(aistudio.google.com/apikey). Se obtiene en un minuto y no pide tarjeta.",
            )
        link = f'style="color:{style.LINK}; text-decoration:none; font-weight:600;"'
        body = (
            f"Estás consultando {scope} con {active} "
            f"{'fuente activa' if active == 1 else 'fuentes activas'}. Cada respuesta cita el "
            "pasaje del que sale; un clic en la cita te muestra el texto original."
        )
        if note is not None and note[0] == "info":
            body += (
                f'<br><br><a href="action:connect" {link}>Conectar mi clave</a> &nbsp;·&nbsp; '
                f'<a href="action:guide" {link}>Ver la guía paso a paso</a> &nbsp;·&nbsp; '
                f'<a href="{AI_STUDIO_APIKEY_URL}" {link}>{AI_STUDIO_APIKEY_URL}</a>'
            )
        suggestions = (
            [
                "¿Cuáles fueron las ideas principales?",
                "¿Qué recalcó el profesor para el examen?",
                "Explícame el tema más difícil con un ejemplo",
                "¿Qué tareas o fechas se mencionaron?",
            ]
            if active
            else []
        )
        self.chat_view.show_welcome("¿Qué quieres repasar hoy?", body, suggestions, note)

    def _render_messages(self, messages: list[dict]) -> None:
        if not messages:
            self._welcome()
            return
        self.chat_view.clear()
        for msg in messages:
            role = "user" if msg.get("role") == "user" else "assistant"
            widget = self.chat_view.add_message(role)
            if role == "user":
                widget.set_user_text(msg.get("content", ""))
                continue
            meta = msg.get("meta") or {}
            citations = [c for c in (msg.get("citations") or []) if isinstance(c, dict)]
            widget.set_markdown(msg.get("content", ""), citations)
            widget.set_sources([c for c in citations if not c.get("implicit")])
            widget.set_meta(
                model=msg.get("model_used", ""),
                mode=meta.get("mode", ""),
                note=self._meta_note(meta),
            )
        self.chat_view.scroll_to_end()

    @staticmethod
    def _meta_note(meta: dict) -> str:
        if meta.get("stopped"):
            return "Detenida"
        if meta.get("fallback_from"):
            return f"{model_label(meta['fallback_from'])} no estaba disponible"
        return ""

    # ── Preguntar ─────────────────────────────────────────────────
    def is_busy_generating(self) -> bool:
        return bool(
            (self.query_worker and self.query_worker.isRunning())
            or (self.study_worker and self.study_worker.isRunning())
        )

    def _history(self) -> list[dict]:
        if self._current_session_id is None:
            return []
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self.store.get_chat_messages(self._current_session_id)[-8:]
        ]

    def _set_generating(self, active: bool, text: str = "") -> None:
        self.busy_host.setVisible(active)
        if text:
            self.analyzing_label.setText(text)
        self.send_btn.setVisible(not active)
        self.stop_btn.setVisible(active)
        self.stop_btn.setEnabled(True)
        self.input_edit.setReadOnly(active)
        for chip in (*self.quick_prompt_btns, self.saved_btn):
            chip.setEnabled(not active)

    def _send_question(self, instruction: str = "", task: str = "chat", label: str = "") -> None:
        if self.is_busy_generating() or self._countdown_timer.isActive():
            return
        question = self.input_edit.toPlainText().strip()
        if not question:
            return

        self._save_inline_api_key()
        active_ids = self.get_active_source_ids()

        if self.demo_responder is not None:
            self._pending_question = question
            self._on_query_done(question, self.demo_responder(question, active_ids))
            self.input_edit.clear()
            return

        if not self._has_key():
            self._open_api_settings_dialog()
            return

        self.rate_limit_banner.hide()
        self._countdown_timer.stop()
        history = self._history()
        self._pending_question = question
        self._pending_user = self.chat_view.add_message("user")
        self._pending_user.set_user_text(label or question)
        self._pending_reply = self.chat_view.add_message("assistant")
        self._pending_reply.set_markdown("", [], streaming=True)
        self._stream_text = ""
        self.input_edit.clear()
        self.chat_view.scroll_to_end()
        self._set_generating(True, "Leyendo tus fuentes…")

        self.query_worker = RagQueryWorker(
            engine=self.engine,
            question=question,
            course_id=self.current_course_id(),
            all_courses=self.is_all_courses_mode(),
            active_source_ids=active_ids,
            preferred_model=self.model_combo.currentData(),
            stream=True,
            parent=self,
            history=history,
            grounding="strict" if self.grounding_btn.isChecked() else "tutor",
            task=task,
            instruction=instruction,
        )
        shown = label or question
        self.query_worker.chunk_received.connect(self._on_chunk)
        self.query_worker.reset.connect(self._on_stream_reset)
        self.query_worker.done.connect(lambda resp, q=shown: self._on_query_done(q, resp))
        self.query_worker.rate_limited.connect(self._on_rate_limited)
        self.query_worker.failed.connect(self._on_query_failed)
        self.query_worker.cancelled.connect(self._on_query_cancelled)
        self.query_worker.start()

    def run_tool(self, key: str) -> None:
        """Lanza una herramienta de estudio sobre las fuentes marcadas."""
        tool = study.TOOLS[key]
        if self.is_busy_generating():
            return
        if tool.kind == "markdown":
            self.input_edit.setPlainText(tool.label + " de las fuentes seleccionadas")
            self._send_question(instruction=tool.prompt, task="deep", label=tool.label)
            return
        active_ids = self.get_active_source_ids()
        if not active_ids:
            QMessageBox.information(
                self,
                tool.label,
                "Marca al menos una clase o material en Fuentes para generar " + tool.label.lower() + ".",
            )
            return
        from zoom2txt.gui.study_dialogs import StudySetupDialog

        setup = StudySetupDialog(tool, len(active_ids), self)
        if setup.exec() != QDialog.DialogCode.Accepted:
            return
        count, focus = setup.values()
        title = f"{self.current_course_name()}" + (f" · {focus}" if focus else "")
        if self.demo_study is not None:
            self._on_study_done(self.demo_study(key, count))
            return
        if not self._has_key():
            self._open_api_settings_dialog()
            return
        self.rate_limit_banner.hide()
        self._set_generating(True, f"Preparando {tool.label.lower()} con tus fuentes…")
        self.study_worker = StudyWorker(
            self.engine,
            key,
            self,
            course_id=self.current_course_id(),
            all_courses=self.is_all_courses_mode(),
            active_source_ids=active_ids,
            preferred_model=self.model_combo.currentData(),
            count=count,
            focus=focus,
            title=title,
        )
        self.study_worker.done.connect(self._on_study_done)
        self.study_worker.rate_limited.connect(self._on_rate_limited)
        self.study_worker.failed.connect(self._on_query_failed)
        self.study_worker.cancelled.connect(lambda: self._set_generating(False))
        self.study_worker.start()

    def _on_study_done(self, result: study.StudyResult) -> None:
        self._set_generating(False)
        try:
            study.save_deck(self.store, result, self._scope_key())
        except Exception:  # noqa: BLE001, S110 — si no se guarda, igual se puede usar ahora
            pass
        self._open_deck(result)

    def _open_deck(self, result: study.StudyResult) -> None:
        from zoom2txt.gui.study_dialogs import FlashcardsDialog, QuizDialog

        dialog = (
            FlashcardsDialog(result, self)
            if result.kind == "flashcards"
            else QuizDialog(result, self.store, self)
        )
        dialog.exec()

    def _show_saved_menu(self) -> None:
        menu = QMenu(self)
        decks = study.list_decks(self.store)[:15]
        for deck in decks:
            kind = "Tarjetas" if deck.get("deck_type") == "flashcards" else "Simulacro"
            stamp = datetime.fromtimestamp(float(deck.get("created", 0)), UTC).strftime("%d/%m")
            attempts = deck.get("attempts") or []
            best = max((a["score"] / a["total"] for a in attempts if a.get("total")), default=None)
            extra = f" · mejor {round(best * 20)}/20" if best is not None else ""
            action = menu.addAction(
                f"{kind} · {deck.get('title', '')} ({len(deck.get('items', []))}) · {stamp}{extra}"
            )
            action.triggered.connect(lambda _=False, d=deck: self._open_saved(d))
        if not decks:
            empty = menu.addAction("Aún no has generado tarjetas ni simulacros")
            empty.setEnabled(False)
        menu.exec(self.saved_btn.mapToGlobal(self.saved_btn.rect().topLeft()))

    def _open_saved(self, deck: dict) -> None:
        self._open_deck(
            study.StudyResult(
                kind=deck.get("deck_type", "flashcards"),
                tool=deck.get("tool", ""),
                title=deck.get("title", ""),
                items=list(deck.get("items", [])),
                citations=list(deck.get("citations", [])),
                model_used=deck.get("model", ""),
                deck_id=str(deck.get("id", "")),
            )
        )

    def _on_chunk(self, piece: str) -> None:
        if not self._stream_text:
            self.analyzing_label.setText("Escribiendo la respuesta…")
        self._stream_text += piece
        if not self._stream_timer.isActive():
            self._stream_timer.start()

    def _on_stream_reset(self) -> None:
        self._stream_text = ""
        self.analyzing_label.setText("Cambiando de modelo…")
        if self._pending_reply is not None:
            self._pending_reply.set_markdown("", [], streaming=True)

    def _paint_stream(self) -> None:
        if self._pending_reply is not None and self._stream_text:
            self._pending_reply.set_markdown(self._stream_text, [], streaming=True)

    def _discard_pending(self) -> None:
        self._stream_timer.stop()
        self.chat_view.remove_message(self._pending_user)
        self.chat_view.remove_message(self._pending_reply)
        self._pending_user = None
        self._pending_reply = None
        self._stream_text = ""

    def _restore_question(self) -> None:
        if self._pending_question and not self.input_edit.toPlainText().strip():
            self.input_edit.setPlainText(self._pending_question)
        self._pending_question = ""

    def _on_query_done(self, question: str, resp: RAGResponse, stopped: bool = False) -> None:
        self._stream_timer.stop()
        self._set_generating(False)
        if self._current_session_id is None:
            self._current_session_id = self.store.get_or_create_chat_session(
                course_id=self.current_course_id(),
                all_courses=self.is_all_courses_mode(),
                title=self.current_course_name(),
            )
        meta = {
            "mode": getattr(resp, "mode", ""),
            "grounding": getattr(resp, "grounding", ""),
            "fallback_from": getattr(resp, "fallback_from", ""),
            "input_tokens": getattr(resp, "input_tokens", 0),
            "output_tokens": getattr(resp, "output_tokens", 0),
        }
        if stopped:
            meta["stopped"] = True
        self.store.append_chat_message(self._current_session_id, "user", question)
        self.store.append_chat_message(
            self._current_session_id,
            "assistant",
            resp.answer,
            citations=resp.citations,
            model_used=resp.model_used,
            meta=meta,
        )
        self._pending_question = ""
        if self._pending_reply is None:
            self._render_messages(self.store.get_chat_messages(self._current_session_id))
            return
        reply = self._pending_reply
        self._pending_user = None
        self._pending_reply = None
        self._stream_text = ""
        citations = [c for c in resp.citations if isinstance(c, dict)]
        reply.set_markdown(resp.answer, citations)
        reply.set_sources([c for c in citations if not c.get("implicit")])
        tokens = meta["input_tokens"] + meta["output_tokens"]
        reply.set_meta(
            model=resp.model_used,
            mode=meta["mode"],
            note=self._meta_note(meta),
            usage=f"{tokens:,} tokens".replace(",", " ") if tokens else "",
        )

    def _stop_generation(self) -> None:
        self.stop_btn.setEnabled(False)
        for worker in (self.query_worker, self.study_worker):
            if worker and worker.isRunning():
                worker.cancel()

    def _on_query_cancelled(self) -> None:
        partial = self._stream_text.strip()
        if partial and self._pending_question:
            question = self._pending_user.markdown() if self._pending_user else self._pending_question
            self._on_query_done(
                question,
                RAGResponse(answer=partial, citations=[], chunks=[], model_used="", mode=""),
                stopped=True,
            )
            return
        self._set_generating(False)
        self._discard_pending()
        self._restore_question()

    def _on_rate_limited(self, retry_after: int, message: str) -> None:
        """Error 429: la pregunta se conserva y se muestra una cuenta regresiva."""
        self._set_generating(False)
        self._discard_pending()
        self._restore_question()
        self.input_edit.setReadOnly(False)
        self._countdown_remaining = max(5, int(retry_after))
        self._rate_message = message
        self._daily_limit = self._countdown_remaining >= 1800
        if self._daily_limit:
            self.rate_limit_banner.show_message(
                "Se agotó la cuota gratuita diaria de los modelos disponibles. Google la renueva a "
                "medianoche (hora del Pacífico). Tu pregunta sigue escrita; mientras tanto puedes "
                "repasar tus tarjetas y simulacros guardados.",
                "warning",
            )
            return
        self.send_btn.setEnabled(False)
        self._update_countdown_banner()
        self.rate_limit_banner.show()
        self._countdown_timer.start()

    def _tick_countdown(self) -> None:
        self._countdown_remaining -= 1
        if self._countdown_remaining <= 0:
            self._countdown_timer.stop()
            self.send_btn.setEnabled(True)
            self.rate_limit_banner.show_message(
                "Cuota por minuto renovada. Ya puedes pulsar «Preguntar»; tu pregunta sigue lista.",
                "success",
            )
            return
        self._update_countdown_banner()

    def _update_countdown_banner(self) -> None:
        self.rate_limit_banner.show_message(
            "Límite gratuito por minuto alcanzado (429). Tu pregunta se conservó intacta; "
            f"podrás volver a enviarla en {self._countdown_remaining} s…",
            "warning",
        )

    def _on_query_failed(self, error: str) -> None:
        self._set_generating(False)
        self._discard_pending()
        self._restore_question()
        self.send_btn.setEnabled(True)
        self.rate_limit_banner.show_message(f"No se pudo completar la consulta: {error}", "error")

    def _on_cite_clicked(self, message: _MessageWidget, number: int) -> None:
        citation = message.citation(number)
        if citation is None:
            return
        dialog = SourcePeekDialog(citation, self)
        dialog.open_reader.connect(self._open_reader)
        dialog.exec()

    def _open_reader(self, citation: dict) -> None:
        from zoom2txt.gui.reader_dialog import ReaderDialog

        path = str(citation.get("path") or "")
        if not path and citation.get("source_id"):
            match = next(
                (
                    s
                    for s in self.store.list_knowledge_sources(all_courses=True)
                    if int(s["id"]) == int(citation["source_id"])
                ),
                None,
            )
            path = match["file_path"] if match else ""
        if not path:
            return
        snippet_words = [w for w in str(citation.get("snippet", "")).split()[:6] if len(w) > 3]
        ReaderDialog(
            self.settings,
            Path(path),
            title=str(citation.get("source_title", "")),
            parent=self.window(),
            query=" ".join(snippet_words[:1]) if citation.get("snippet") and not citation.get("whole") else "",
            engine=self.engine,
            source_id=citation.get("source_id"),
        ).exec()

    def _clear_chat(self) -> None:
        if self._current_session_id is not None and not self.is_busy_generating():
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
            speaker = (
                "Estudiante"
                if msg["role"] == "user"
                else f"Asistente ({model_label(msg.get('model_used') or '') or 'IA'})"
            )
            lines.append(f"## {speaker}")
            lines.append(msg["content"])
            numbered = [c for c in msg.get("citations", []) if isinstance(c, dict) and c.get("n")]
            if numbered:
                lines.append("")
                lines.append("**Fuentes citadas**")
                for c in numbered:
                    where = f" ({c['locator']})" if c.get("locator") else ""
                    lines.append(f"- [{c['n']}] {c.get('source_title', '')} — {c.get('course_name', '')}{where}")
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

    def _workers(self) -> tuple:
        return (
            self.sync_worker,
            self.ingest_worker,
            self.query_worker,
            self.test_worker,
            self.study_worker,
            self.models_worker,
        )

    def is_busy(self) -> bool:
        return any(w and w.isRunning() for w in self._workers())

    def cancel_all(self) -> None:
        self._countdown_timer.stop()
        self._stream_timer.stop()
        for w in self._workers():
            if w and w.isRunning():
                if hasattr(w, "cancel"):
                    w.cancel()
                w.requestInterruption()
                w.wait(2000)

    def closeEvent(self, event) -> None:
        self.cancel_all()
        super().closeEvent(event)

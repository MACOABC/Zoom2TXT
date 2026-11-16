"""Lector integrado: abre una transcripción o un material indexado, busca dentro
del texto con resaltado, copia, ajusta el tamaño de letra y lleva la fuente al
asistente. No modifica el archivo."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QShortcut,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
)

from zoom2txt.config import Settings
from zoom2txt.gui import style, widgets
from zoom2txt.library import TXT_SUFFIX

READING_WPM = 200


def load_source_text(path: Path, engine=None, source_id: int | None = None) -> str:
    """Texto legible de una fuente: del archivo si es de texto, del índice si es
    un PDF/PPTX/DOCX ya indexado, o extrayéndolo en el momento."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md") and path.is_file():
        raw = path.read_text(encoding="utf-8", errors="replace")
        if raw.lstrip().startswith("WEBVTT"):
            from zoom2txt.cleaning.subtitle_cleaner import vtt_a_texto

            raw = vtt_a_texto(raw)
        return raw.strip()
    if engine is not None and source_id is not None:
        text = engine.source_text(int(source_id))
        if text:
            return text
    if path.is_file():
        from zoom2txt.rag_engine import extract_sections_from_file

        return "\n\n".join(s.text for s in extract_sections_from_file(path)).strip()
    return ""


class ReaderDialog(QDialog):
    ask_requested = Signal(str)

    def __init__(
        self,
        settings: Settings,
        path: Path,
        title: str = "",
        parent=None,
        query: str = "",
        engine=None,
        source_id: int | None = None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.path = Path(path)
        self._matches: list[QTextCursor] = []
        self._current = -1
        self._font_px = 15
        name = title or (
            self.path.name[: -len(TXT_SUFFIX)] if self.path.name.endswith(TXT_SUFFIX) else self.path.stem
        )
        self.setWindowTitle(name + " · Lector")
        self.resize(940, 720)
        if parent is not None:
            self.resize(min(1000, parent.width() - 80), max(520, parent.height() - 60))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title_label = widgets.ElidedLabel(name)
        self.title_label.setObjectName("h1")
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        titles.addWidget(self.title_label)
        titles.addWidget(self.meta_label)
        head.addLayout(titles, 1)
        self.ask_button = widgets.button(
            "Preguntar al asistente", "sparkles", "primary",
            "Abre el Asistente IA con esta fuente como único contexto.",
        )  # fmt: skip
        self.ask_button.clicked.connect(self._ask)
        head.addWidget(self.ask_button, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.search = widgets.SearchField("Buscar en el texto…")
        self.search.textChanged.connect(self._schedule_search)
        self.search.returnPressed.connect(self.find_next)
        bar.addWidget(self.search, 1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("faint")
        self.count_label.setMinimumWidth(64)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self.count_label)
        prev_btn = widgets.icon_button("chevron-up", "Coincidencia anterior (Mayús+Enter)")
        prev_btn.clicked.connect(self.find_previous)
        next_btn = widgets.icon_button("chevron-down", "Coincidencia siguiente (Enter)")
        next_btn.clicked.connect(self.find_next)
        bar.addWidget(prev_btn)
        bar.addWidget(next_btn)
        bar.addSpacing(8)
        smaller = widgets.icon_button("minus", "Letra más pequeña")
        smaller.clicked.connect(lambda: self._zoom(-1))
        larger = widgets.icon_button("plus", "Letra más grande")
        larger.clicked.connect(lambda: self._zoom(1))
        bar.addWidget(smaller)
        bar.addWidget(larger)
        bar.addSpacing(8)
        self.copy_button = widgets.button("Copiar todo", "copy", "secondary")
        self.copy_button.clicked.connect(self._copy_all)
        bar.addWidget(self.copy_button)
        open_file = widgets.icon_button("external-link", "Abrir el archivo con su programa habitual")
        open_file.clicked.connect(self._open_file)
        open_folder = widgets.icon_button("folder-open", "Mostrar en su carpeta")
        open_folder.clicked.connect(self._open_folder)
        bar.addWidget(open_file)
        bar.addWidget(open_folder)
        lay.addLayout(bar)

        self.view = QTextBrowser()
        self.view.setObjectName("reader")
        self.view.setOpenLinks(False)
        self.view.setReadOnly(True)
        lay.addWidget(self.view, 1)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(160)
        self._search_timer.timeout.connect(self._run_search)
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.setInterval(1600)
        self._copy_timer.timeout.connect(lambda: self.copy_button.setText("Copiar todo"))
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self._focus_search)
        QShortcut(QKeySequence("Shift+Return"), self, activated=self.find_previous)
        QShortcut(QKeySequence("F3"), self, activated=self.find_next)
        QShortcut(QKeySequence("Shift+F3"), self, activated=self.find_previous)

        try:
            self._text = load_source_text(self.path, engine, source_id)
        except Exception as exc:  # noqa: BLE001 — un archivo ilegible se explica, no rompe
            from zoom2txt.security import redact

            self._text = ""
            self.meta_label.setText("No se pudo leer esta fuente: " + redact(exc))
        self._render()
        if query:
            self.search.setText(self._best_term(query))
            self._run_search()

    # ── Presentación ──────────────────────────────────────────────
    def _render(self) -> None:
        if not self._text:
            self.view.setPlainText(
                "No hay texto disponible. Puede que el archivo se haya movido o esté vacío."
            )
            self.ask_button.setEnabled(False)
            self.copy_button.setEnabled(False)
            return
        words = len(self._text.split())
        minutes = max(1, round(words / READING_WPM))
        self.meta_label.setText(
            f"{words:,}".replace(",", " ")
            + f" palabras · {minutes} min de lectura · {self.path.suffix.lstrip('.').upper() or 'TXT'}"
        )
        self.view.setPlainText(self._text)
        self._apply_typography()

    def _apply_typography(self) -> None:
        document = self.view.document()
        font = QFont(self.font())
        font.setPixelSize(self._font_px)
        document.setDefaultFont(font)
        cursor = QTextCursor(document)
        cursor.select(QTextCursor.SelectionType.Document)
        block = QTextBlockFormat()
        block.setLineHeight(155, 1)  # 1 = altura proporcional (QTextBlockFormat.ProportionalHeight)
        block.setBottomMargin(4)
        cursor.mergeBlockFormat(block)

    def _zoom(self, step: int) -> None:
        self._font_px = max(12, min(24, self._font_px + step))
        self._apply_typography()
        self._highlight()

    # ── Búsqueda ──────────────────────────────────────────────────
    @staticmethod
    def _best_term(query: str) -> str:
        import re

        words = re.findall(r"[^\W_]{4,}", query)
        return max(words, key=len) if words else query.strip()

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _schedule_search(self) -> None:
        self._search_timer.start()

    def _run_search(self) -> None:
        term = self.search.text().strip()
        self._matches = []
        self._current = -1
        if len(term) >= 2:
            document = self.view.document()
            cursor = QTextCursor(document)
            while len(self._matches) < 2000:
                cursor = document.find(term, cursor, QTextDocument.FindFlag(0))
                if cursor.isNull():
                    break
                self._matches.append(QTextCursor(cursor))
        if self._matches:
            self._current = 0
        self._highlight()
        self._reveal()

    def _highlight(self) -> None:
        selections = []
        soft = QTextCharFormat()
        soft.setBackground(QColor(style.WARNING_BG))
        soft.setForeground(QColor(style.WARNING))
        strong = QTextCharFormat()
        strong.setBackground(QColor(style.ACCENT))
        strong.setForeground(QColor(style.ON_ACCENT))
        for index, cursor in enumerate(self._matches):
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format = strong if index == self._current else soft
            selections.append(selection)
        self.view.setExtraSelections(selections)
        if self._matches:
            self.count_label.setText(f"{self._current + 1}/{len(self._matches)}")
        else:
            self.count_label.setText("0" if len(self.search.text().strip()) >= 2 else "")

    def _reveal(self) -> None:
        if 0 <= self._current < len(self._matches):
            cursor = QTextCursor(self._matches[self._current])
            cursor.clearSelection()
            self.view.setTextCursor(cursor)
            self.view.ensureCursorVisible()

    def find_next(self) -> None:
        if self._matches:
            self._current = (self._current + 1) % len(self._matches)
            self._highlight()
            self._reveal()

    def find_previous(self) -> None:
        if self._matches:
            self._current = (self._current - 1) % len(self._matches)
            self._highlight()
            self._reveal()

    def match_count(self) -> int:
        return len(self._matches)

    # ── Acciones ──────────────────────────────────────────────────
    def _copy_all(self) -> None:
        QApplication.clipboard().setText(self._text)
        self.copy_button.setText("Copiado")
        self._copy_timer.start()

    def _open_file(self) -> None:
        if self.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))

    def _open_folder(self) -> None:
        if self.path.parent.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))

    def _ask(self) -> None:
        self.ask_requested.emit(str(self.path))
        self.accept()

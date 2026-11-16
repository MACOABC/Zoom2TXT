"""Paleta de comandos (Ctrl+K): ir a cualquier sección, lanzar acciones y buscar
texto dentro de todas las clases y materiales indexados, sin tocar el ratón."""

from __future__ import annotations

import html
import unicodedata
from collections.abc import Callable

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zoom2txt.gui import style, widgets


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(c for c in value if not unicodedata.combining(c))


def match_score(query: str, title: str, hint: str = "") -> int:
    """Puntuación de coincidencia (0 = no coincide). Prioriza el inicio del
    título, luego palabras completas y por último subsecuencias."""
    q = _fold(query).strip()
    if not q:
        return 1
    t, h = _fold(title), _fold(hint)
    if t.startswith(q):
        return 100
    words = q.split()
    if all(any(part.startswith(word) for part in t.split()) for word in words):
        return 80
    if q in t:
        return 60
    if all(word in t or word in h for word in words):
        return 40
    position = 0
    for char in q.replace(" ", ""):
        position = t.find(char, position)
        if position < 0:
            return 0
        position += 1
    return 15


def highlight_snippet(snippet: str) -> str:
    """Convierte las marcas [[ ]] del extracto FTS en negrita con color."""
    safe = html.escape(" ".join(str(snippet).split()))
    return safe.replace("[[", f'<span style="color:{style.LINK}; font-weight:700;">').replace(
        "]]", "</span>"
    )


class _Row(QWidget):
    def __init__(self, entry: dict):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 12, 8)
        lay.setSpacing(12)
        lay.addWidget(widgets.icon_badge(entry.get("icon", "circle"), "LINK", 30), 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(1)
        title = widgets.ElidedLabel(entry.get("title", ""))
        title.setObjectName("h3")
        column.addWidget(title)
        hint = entry.get("hint", "")
        if hint:
            sub = widgets.ElidedLabel(hint)
            sub.setObjectName("faint")
            column.addWidget(sub)
        if entry.get("snippet"):
            excerpt = QLabel(highlight_snippet(entry["snippet"]))
            excerpt.setTextFormat(Qt.TextFormat.RichText)
            excerpt.setObjectName("muted")
            excerpt.setWordWrap(True)
            column.addWidget(excerpt)
        lay.addLayout(column, 1)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)


class CommandPalette(QDialog):
    def __init__(
        self,
        parent,
        actions: list[dict],
        search: Callable[[str], list[dict]] | None = None,
    ):
        super().__init__(parent)
        self._actions = actions
        self._search = search
        self._entries: list[dict] = []
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("card")
        panel.setStyleSheet(
            f"QFrame#card {{ background-color: {style.SURFACE}; border: 1px solid {style.BORDER_HI};"
            " border-radius: 14px; }"
        )
        outer.addWidget(panel)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(6, 2, 4, 2)
        top.setSpacing(10)
        top.addWidget(widgets.icon_label("search", "FAINT", 18))
        self.input = QLineEdit()
        self.input.setProperty("variant", "ghost")
        self.input.setPlaceholderText("Escribe una acción, una sección o algo dicho en clase…")
        self.input.textChanged.connect(self._on_text)
        self.input.installEventFilter(self)
        top.addWidget(self.input, 1)
        esc = QLabel("Esc")
        esc.setObjectName("kbd")
        top.addWidget(esc)
        lay.addLayout(top)
        lay.addWidget(widgets.divider())

        self.list = QListWidget()
        self.list.setObjectName("palette")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.itemClicked.connect(lambda _item: self._run_current())
        lay.addWidget(self.list, 1)

        self.footer = QLabel("↑ ↓ para moverte · Enter para abrir")
        self.footer.setObjectName("faint")
        self.footer.setContentsMargins(8, 0, 0, 0)
        lay.addWidget(self.footer)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(140)
        self._debounce.timeout.connect(self._rebuild)
        self.resize(640, 460)
        self._rebuild()

    def open_centered(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            width = min(680, max(420, parent.width() - 120))
            self.resize(width, min(480, max(320, parent.height() - 160)))
            top_left = parent.mapToGlobal(parent.rect().topLeft())
            self.move(top_left.x() + (parent.width() - width) // 2, top_left.y() + 90)
        self.show()
        self.input.setFocus()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: 5, Qt.Key.Key_PageUp: -5}[key]
                count = self.list.count()
                if count:
                    self.list.setCurrentRow(max(0, min(count - 1, self.list.currentRow() + step)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run_current()
                return True
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(watched, event)

    def _on_text(self) -> None:
        self._debounce.start()

    def entries_for(self, query: str) -> list[dict]:
        scored = [
            (match_score(query, action["title"], action.get("hint", "")), index, action)
            for index, action in enumerate(self._actions)
        ]
        matched = [item for item in scored if item[0] > 0]
        matched.sort(key=lambda item: (-item[0], item[1]))
        entries = [action for _, _, action in matched][: 30 if not query.strip() else 8]
        if self._search is not None and len(query.strip()) >= 3:
            entries += self._search(query.strip())
        return entries

    def _rebuild(self) -> None:
        query = self.input.text()
        self._entries = self.entries_for(query)
        self.list.clear()
        for entry in self._entries:
            item = QListWidgetItem(self.list)
            row = _Row(entry)
            item.setSizeHint(QSize(0, max(46, row.sizeHint().height())))
            self.list.setItemWidget(item, row)
        if self._entries:
            self.list.setCurrentRow(0)
            texts = sum(1 for entry in self._entries if entry.get("snippet"))
            self.footer.setText(
                "↑ ↓ para moverte · Enter para abrir"
                + (f" · {texts} coincidencias en tus clases" if texts else "")
            )
        else:
            self.footer.setText(
                "Sin coincidencias. Prueba con otra palabra; la búsqueda en clases necesita 3 letras."
            )

    def _run_current(self) -> None:
        row = self.list.currentRow()
        if not 0 <= row < len(self._entries):
            return
        action = self._entries[row].get("run")
        self.close()
        if callable(action):
            QTimer.singleShot(0, action)

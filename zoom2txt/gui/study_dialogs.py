"""Estudio interactivo: mazo de tarjetas con volteo y simulacro con nota.

Ambos trabajan sobre un ``StudyResult`` ya generado y guardado, así que repasar
o repetir un examen no consume cuota de la API.
"""

from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from zoom2txt import study
from zoom2txt.gui import style, widgets
from zoom2txt.storage import atomic_write


class StudySetupDialog(QDialog):
    """Cantidad y enfoque opcional antes de generar tarjetas o un simulacro."""

    def __init__(self, tool: study.StudyTool, sources: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tool.label)
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)
        title = QLabel("Generar " + tool.label.lower())
        title.setObjectName("h2")
        lay.addWidget(title)
        info = QLabel(
            f"{tool.hint} Se crearán a partir de las {sources} fuentes marcadas y quedarán "
            "guardadas para repetirlas sin gastar cuota."
            if sources != 1
            else f"{tool.hint} Se crearán a partir de la fuente marcada y quedarán guardadas "
            "para repetirlas sin gastar cuota."
        )
        info.setObjectName("muted")
        info.setWordWrap(True)
        lay.addWidget(info)
        form = QFormLayout()
        form.setSpacing(10)
        self.count = QSpinBox()
        self.count.setRange(5, 30)
        self.count.setValue(tool.default_count)
        form.addRow("Cantidad" if tool.kind == "flashcards" else "Preguntas", self.count)
        self.focus = QLineEdit()
        self.focus.setPlaceholderText("Opcional · p. ej. «regresión logística» o «unidad 2»")
        form.addRow("Enfocar en", self.focus)
        lay.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Generar")
        ok.setProperty("variant", "primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def values(self) -> tuple[int, str]:
        return self.count.value(), self.focus.text().strip()


def _source_note(result: study.StudyResult, number: int) -> str:
    for citation in result.citations:
        if citation.get("n") == number:
            where = citation.get("locator") or ""
            return citation.get("source_title", "") + (f" · {where}" if where else "")
    return ""


class FlashcardsDialog(QDialog):
    """Repaso con tarjetas: Espacio voltea, ← → navega, 1 y 2 marcan el resultado."""

    def __init__(self, result: study.StudyResult, parent=None):
        super().__init__(parent)
        self.result = result
        self._order = list(range(len(result.items)))
        self._position = 0
        self._flipped = False
        self._known: set[int] = set()
        self._missed: set[int] = set()
        self.setWindowTitle(f"Tarjetas · {result.title}")
        self.resize(720, 540)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)
        head = QHBoxLayout()
        title = widgets.ElidedLabel(result.title)
        title.setObjectName("h2")
        head.addWidget(title, 1)
        self.counter = widgets.chip("")
        head.addWidget(self.counter)
        shuffle = widgets.icon_button("shuffle", "Barajar")
        shuffle.clicked.connect(self._shuffle)
        export = widgets.icon_button("download", "Exportar (CSV para Anki o Markdown)")
        export.clicked.connect(self._export)
        head.addWidget(shuffle)
        head.addWidget(export)
        lay.addLayout(head)
        self.progress = QProgressBar()
        self.progress.setProperty("thin", "true")
        self.progress.setTextVisible(False)
        lay.addWidget(self.progress)

        self.card = QPushButton()
        self.card.setObjectName("flashcard")
        self.card.setCursor(Qt.CursorShape.PointingHandCursor)
        self.card.setMinimumHeight(260)
        self.card.clicked.connect(self.flip)
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(34, 26, 34, 22)
        self.side = QLabel("PREGUNTA")
        self.side.setObjectName("eyebrow")
        self.side.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.source = QLabel("")
        self.source.setObjectName("faint")
        self.source.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.source.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        card_lay.addWidget(self.side, 0, Qt.AlignmentFlag.AlignHCenter)
        card_lay.addStretch()
        card_lay.addWidget(self.text)
        card_lay.addStretch()
        card_lay.addWidget(self.source)
        lay.addWidget(self.card, 1)

        self.hint = QLabel("Clic o Espacio para ver la respuesta · ← → para moverte")
        self.hint.setObjectName("faint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.hint)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.prev_btn = widgets.button("Anterior", "arrow-left", "secondary")
        self.prev_btn.clicked.connect(lambda: self._move(-1))
        self.missed_btn = widgets.button("Aún no la sé", "rotate-ccw", "secondary")
        self.missed_btn.clicked.connect(lambda: self._mark(False))
        self.known_btn = widgets.button("La sabía", "check", "primary")
        self.known_btn.clicked.connect(lambda: self._mark(True))
        self.next_btn = widgets.button("Siguiente", "arrow-right", "secondary")
        self.next_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.next_btn.clicked.connect(lambda: self._move(1))
        actions.addWidget(self.prev_btn)
        actions.addStretch()
        actions.addWidget(self.missed_btn)
        actions.addWidget(self.known_btn)
        actions.addStretch()
        actions.addWidget(self.next_btn)
        lay.addLayout(actions)
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.summary)

        for key, slot in (
            ("Space", self.flip),
            ("Right", lambda: self._move(1)),
            ("Left", lambda: self._move(-1)),
            ("1", lambda: self._mark(False)),
            ("2", lambda: self._mark(True)),
        ):
            QShortcut(QKeySequence(key), self, activated=slot)
        self._apply_card_style()
        style.on_theme_change(self._apply_card_style)
        self._show()

    def _apply_card_style(self) -> None:
        self.card.setStyleSheet(
            f"QPushButton#flashcard {{ background-color: {style.SURFACE}; border: 1px solid "
            f"{style.BORDER_HI}; border-radius: 18px; }}"
            f"QPushButton#flashcard:hover {{ border-color: {style.LINK}; background-color: {style.SURFACE}; }}"
            f"QPushButton#flashcard:focus {{ border: 2px solid {style.LINK}; padding: 0; }}"
        )

    def current_index(self) -> int:
        return self._order[self._position]

    def _show(self) -> None:
        index = self.current_index()
        item = self.result.items[index]
        self.side.setText("RESPUESTA" if self._flipped else "PREGUNTA")
        self.text.setText(item["back"] if self._flipped else item["front"])
        self.text.setStyleSheet(
            f"font-size: {17 if self._flipped else 21}px; font-weight: {500 if self._flipped else 650};"
            f" color: {style.TEXT};"
        )
        note = _source_note(self.result, int(item.get("source") or 0))
        self.source.setText(note if self._flipped else "")
        total = len(self._order)
        self.counter.setText(f"{self._position + 1} / {total}")
        self.progress.setRange(0, total)
        self.progress.setValue(len(self._known) + len(self._missed))
        self.prev_btn.setEnabled(self._position > 0)
        self.next_btn.setEnabled(self._position < total - 1)
        self.summary.setText(
            f"{len(self._known)} sabidas · {len(self._missed)} por repasar"
            if self._known or self._missed
            else ""
        )

    def flip(self) -> None:
        self._flipped = not self._flipped
        self._show()

    def _move(self, step: int) -> None:
        target = self._position + step
        if 0 <= target < len(self._order):
            self._position = target
            self._flipped = False
            self._show()

    def _mark(self, known: bool) -> None:
        index = self.current_index()
        (self._known if known else self._missed).add(index)
        (self._missed if known else self._known).discard(index)
        if self._position < len(self._order) - 1:
            self._move(1)
        else:
            self._finish()

    def _finish(self) -> None:
        self._show()
        if self._missed:
            answer = QMessageBox.question(
                self,
                "Mazo completado",
                f"Sabías {len(self._known)} de {len(self._order)}. ¿Repasar solo las "
                f"{len(self._missed)} que aún no sabes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._order = sorted(self._missed)
                self._known.clear()
                self._missed.clear()
                self._position = 0
                self._flipped = False
                self._show()
        else:
            self.summary.setText(f"Mazo completado: sabías las {len(self._order)} tarjetas.")

    def _shuffle(self) -> None:
        random.shuffle(self._order)  # noqa: S311 — barajar tarjetas no es criptografía
        self._position = 0
        self._flipped = False
        self._show()

    def _export(self) -> None:
        selected, chosen = QFileDialog.getSaveFileName(
            self,
            "Exportar tarjetas",
            str(Path.home() / "Tarjetas-Zoom2TXT.csv"),
            "CSV para Anki o Quizlet (*.csv);;Markdown (*.md)",
        )
        if not selected:
            return
        destination = Path(selected)
        as_markdown = destination.suffix.lower() == ".md" or "Markdown" in chosen
        try:
            atomic_write(
                destination,
                study.flashcards_to_markdown(self.result.title, self.result.items)
                if as_markdown
                else study.flashcards_to_csv(self.result.items),
            )
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo exportar", str(exc))


class QuizDialog(QDialog):
    """Simulacro de opción múltiple con corrección inmediata y nota final."""

    def __init__(self, result: study.StudyResult, store=None, parent=None):
        super().__init__(parent)
        self.result = result
        self.store = store
        self._index = 0
        self._answers: dict[int, int] = {}
        self.setWindowTitle(f"Simulacro · {result.title}")
        self.resize(760, 600)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)
        head = QHBoxLayout()
        title = widgets.ElidedLabel(result.title)
        title.setObjectName("h2")
        head.addWidget(title, 1)
        self.counter = widgets.chip("")
        head.addWidget(self.counter)
        export = widgets.icon_button("download", "Exportar a Markdown")
        export.clicked.connect(self._export)
        head.addWidget(export)
        lay.addLayout(head)
        self.progress = QProgressBar()
        self.progress.setProperty("thin", "true")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, len(result.items))
        lay.addWidget(self.progress)

        self.pages = QStackedWidget()
        lay.addWidget(self.pages, 1)

        # Página de pregunta
        question_page = QWidget()
        q_lay = QVBoxLayout(question_page)
        q_lay.setContentsMargins(0, 6, 0, 0)
        q_lay.setSpacing(10)
        self.question = QLabel("")
        self.question.setWordWrap(True)
        self.question.setTextFormat(Qt.TextFormat.PlainText)
        self.question.setStyleSheet("font-size: 17px; font-weight: 650;")
        q_lay.addWidget(self.question)
        self.options_host = QVBoxLayout()
        self.options_host.setSpacing(8)
        q_lay.addLayout(self.options_host)
        self.option_group = QButtonGroup(self)
        self.option_group.setExclusive(True)
        self.option_buttons: list[QPushButton] = []
        self.feedback = widgets.Banner("", "info")
        self.feedback.hide()
        q_lay.addWidget(self.feedback)
        q_lay.addStretch()
        self.pages.addWidget(question_page)

        # Página de resultados
        result_page = QWidget()
        r_lay = QVBoxLayout(result_page)
        r_lay.setContentsMargins(0, 6, 0, 0)
        r_lay.setSpacing(12)
        score_row = QHBoxLayout()
        score_row.setSpacing(18)
        self.ring = widgets.ProgressRing(96, 9)
        score_col = QVBoxLayout()
        self.score_title = QLabel("")
        self.score_title.setObjectName("h1")
        self.score_note = QLabel("")
        self.score_note.setObjectName("muted")
        self.score_note.setWordWrap(True)
        score_col.addStretch()
        score_col.addWidget(self.score_title)
        score_col.addWidget(self.score_note)
        score_col.addStretch()
        score_row.addWidget(self.ring)
        score_row.addLayout(score_col, 1)
        r_lay.addLayout(score_row)
        self.review_scroll = QScrollArea()
        self.review_scroll.setWidgetResizable(True)
        self.review_host = QWidget()
        self.review_lay = QVBoxLayout(self.review_host)
        self.review_lay.setContentsMargins(0, 0, 8, 0)
        self.review_lay.setSpacing(8)
        self.review_scroll.setWidget(self.review_host)
        r_lay.addWidget(self.review_scroll, 1)
        self.pages.addWidget(result_page)

        actions = QHBoxLayout()
        self.hint = QLabel("Elige una opción (teclas 1–4)")
        self.hint.setObjectName("faint")
        actions.addWidget(self.hint, 1)
        self.retry_btn = widgets.button("Repetir simulacro", "rotate-ccw", "secondary")
        self.retry_btn.clicked.connect(self.restart)
        self.retry_btn.hide()
        self.next_btn = widgets.button("Siguiente", "arrow-right", "primary")
        self.next_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.next_btn.clicked.connect(self._next)
        self.next_btn.setEnabled(False)
        actions.addWidget(self.retry_btn)
        actions.addWidget(self.next_btn)
        lay.addLayout(actions)

        for number in range(1, 7):
            QShortcut(
                QKeySequence(str(number)), self, activated=lambda n=number - 1: self._choose_key(n)
            )
        QShortcut(QKeySequence("Return"), self, activated=self._next_key)
        style.on_theme_change(self._restyle_options)
        self._show_question()

    # ── Pregunta ──────────────────────────────────────────────────
    def _option_style(self, state: str) -> str:
        border, background, color = {
            "idle": (style.BORDER_HI, style.SURFACE_2, style.TEXT),
            "correct": (style.SUCCESS, style.SUCCESS_BG, style.SUCCESS),
            "wrong": (style.ERROR, style.ERROR_BG, style.ERROR),
            "dim": (style.BORDER, style.SURFACE, style.FAINT),
        }[state]
        hover = (
            f"QPushButton:hover {{ border-color: {style.LINK}; background-color: {style.SURFACE_3}; }}"
            if state == "idle"
            else ""
        )
        return (
            f"QPushButton {{ text-align: left; padding: 12px 14px; border-radius: 10px; font-weight: 550;"
            f" border: 1px solid {border}; background-color: {background}; color: {color}; }}"
            f"QPushButton:disabled {{ border: 1px solid {border}; background-color: {background}; color: {color}; }}"
            + hover
        )

    def _restyle_options(self) -> None:
        if self.pages.currentIndex() == 0:
            self._paint_options()

    def _paint_options(self) -> None:
        item = self.result.items[self._index]
        chosen = self._answers.get(self._index)
        for position, button in enumerate(self.option_buttons):
            if chosen is None:
                state = "idle"
            elif position == item["answer"]:
                state = "correct"
            elif position == chosen:
                state = "wrong"
            else:
                state = "dim"
            button.setStyleSheet(self._option_style(state))

    def _show_question(self) -> None:
        self.pages.setCurrentIndex(0)
        item = self.result.items[self._index]
        total = len(self.result.items)
        self.counter.setText(f"Pregunta {self._index + 1} de {total}")
        self.progress.setValue(len(self._answers))
        self.question.setText(item["question"])
        for button in self.option_buttons:
            self.option_group.removeButton(button)
            button.hide()
            button.deleteLater()
        self.option_buttons = []
        for position, option in enumerate(item["options"]):
            button = QPushButton(f"{'ABCDEF'[position]}.  {option}")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, p=position: self.choose(p))
            self.option_group.addButton(button, position)
            self.options_host.addWidget(button)
            self.option_buttons.append(button)
        self.feedback.hide()
        self.next_btn.setEnabled(False)
        self.next_btn.setText("Siguiente" if self._index < total - 1 else "Ver resultado")
        self.next_btn.show()
        self.retry_btn.hide()
        self.hint.setText("Elige una opción (teclas 1–4)")
        self._paint_options()

    def _choose_key(self, position: int) -> None:
        if self.pages.currentIndex() == 0 and position < len(self.option_buttons):
            self.choose(position)

    def _next_key(self) -> None:
        if self.next_btn.isVisible() and self.next_btn.isEnabled():
            self._next()

    def choose(self, position: int) -> None:
        if self._index in self._answers:
            return
        item = self.result.items[self._index]
        self._answers[self._index] = position
        correct = position == item["answer"]
        for button in self.option_buttons:
            button.setEnabled(False)
        self._paint_options()
        explanation = item.get("explanation") or ""
        source = _source_note(self.result, int(item.get("source") or 0))
        text = ("Correcto. " if correct else f"La respuesta es {'ABCDEF'[item['answer']]}. ") + explanation
        if source:
            text += f"\nFuente: {source}"
        self.feedback.show_message(text.strip(), "success" if correct else "error")
        self.progress.setValue(len(self._answers))
        self.next_btn.setEnabled(True)
        self.hint.setText("Enter para continuar")

    def _next(self) -> None:
        if self._index < len(self.result.items) - 1:
            self._index += 1
            self._show_question()
        else:
            self._show_results()

    # ── Resultado ─────────────────────────────────────────────────
    def score(self) -> tuple[int, int]:
        right = sum(
            1 for index, answer in self._answers.items() if answer == self.result.items[index]["answer"]
        )
        return right, len(self.result.items)

    def _show_results(self) -> None:
        right, total = self.score()
        ratio = right / total if total else 0
        self.ring.set_value(
            ratio, f"{round(ratio * 20)}/20", "ACCENT" if ratio >= 0.55 else "WARNING"
        )
        self.score_title.setText(f"{right} de {total} correctas")
        self.score_note.setText(
            "Excelente dominio del material."
            if ratio >= 0.85
            else "Buen avance; repasa las que fallaste abajo."
            if ratio >= 0.55
            else "Conviene repasar este material antes del examen. Mira las explicaciones."
        )
        while self.review_lay.count():
            widget = self.review_lay.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        for index, item in enumerate(self.result.items):
            chosen = self._answers.get(index)
            ok = chosen == item["answer"]
            card = QFrame()
            card.setObjectName("card")
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(14, 10, 14, 10)
            card_lay.setSpacing(4)
            top = QHBoxLayout()
            top.addWidget(widgets.chip("Correcta" if ok else "Fallada", "success" if ok else "error"))
            question = QLabel(f"{index + 1}. {item['question']}")
            question.setWordWrap(True)
            question.setObjectName("h3")
            question.setTextFormat(Qt.TextFormat.PlainText)
            top.addWidget(question, 1)
            card_lay.addLayout(top)
            answer = QLabel(
                "Respuesta: " + item["options"][item["answer"]]
                + (
                    f"\nTu elección: {item['options'][chosen]}"
                    if chosen is not None and not ok
                    else ""
                )
                + (f"\n{item['explanation']}" if item.get("explanation") else "")
            )
            answer.setWordWrap(True)
            answer.setObjectName("muted")
            answer.setTextFormat(Qt.TextFormat.PlainText)
            card_lay.addWidget(answer)
            self.review_lay.addWidget(card)
        self.review_lay.addStretch()
        self.pages.setCurrentIndex(1)
        self.counter.setText("Resultado")
        self.next_btn.hide()
        self.retry_btn.show()
        self.hint.setText("El intento quedó guardado en este simulacro.")
        if self.store is not None and self.result.deck_id:
            try:
                study.record_attempt(self.store, self.result.deck_id, right, total)
            except Exception:  # noqa: BLE001, S110 — guardar el intento es opcional
                pass

    def restart(self) -> None:
        self._index = 0
        self._answers = {}
        self._show_question()

    def _export(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar simulacro",
            str(Path.home() / "Simulacro-Zoom2TXT.md"),
            "Markdown (*.md)",
        )
        if not selected:
            return
        try:
            atomic_write(
                Path(selected),
                study.quiz_to_markdown(self.result.title, self.result.items, self._answers or None),
            )
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo exportar", str(exc))

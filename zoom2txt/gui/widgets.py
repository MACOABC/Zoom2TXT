"""Componentes propios de Zoom2TXT 3, pintados con QPainter sobre los tokens de
``style``: navegación lateral, stepper del pipeline, toasts, tarjetas KPI,
estados vacíos y utilidades de iconos que se re-tiñen al cambiar el tema."""

from __future__ import annotations

import weakref

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zoom2txt.gui import lucide, style

# ── Iconos temáticos ──────────────────────────────────────────────
_themed: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _retint_all() -> None:
    for widget, (name, token, size) in list(_themed.items()):
        try:
            _apply_icon(widget, name, token, size)
        except RuntimeError:
            continue


def _apply_icon(widget, name: str, token: str, size: int) -> None:
    color = getattr(style, token, token)
    if isinstance(widget, QLabel):
        widget.setPixmap(lucide.pixmap(name, color, size))
    else:
        widget.setIcon(lucide.icon(name, color, size))
        widget.setIconSize(QSize(size, size))


style.on_theme_change(_retint_all)


def set_icon(widget, name: str, token: str = "MUTED", size: int = 16):
    """Asigna un icono Lucide a un botón o etiqueta y lo mantiene acorde al tema.
    ``token`` es el nombre de un color de ``style`` (p. ej. 'MUTED', 'ON_ACCENT')."""
    _themed[widget] = (name, token, size)
    _apply_icon(widget, name, token, size)
    return widget


def icon_label(name: str, token: str = "MUTED", size: int = 16) -> QLabel:
    label = QLabel()
    label.setFixedSize(size, size)
    return set_icon(label, name, token, size)


def icon_button(
    name: str, tooltip: str = "", token: str = "MUTED", size: int = 17, checkable: bool = False
) -> QPushButton:
    button = QPushButton()
    button.setProperty("variant", "icon")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setCheckable(checkable)
    button.setFixedSize(size + 16, size + 16)
    if tooltip:
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
    return set_icon(button, name, token, size)


def button(
    text: str,
    icon: str | None = None,
    variant: str | None = None,
    tooltip: str = "",
    token: str | None = None,
) -> QPushButton:
    """Botón de texto con icono opcional y variante de estilo."""
    btn = QPushButton(text)
    if variant:
        btn.setProperty("variant", variant)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        btn.setToolTip(tooltip)
    if icon:
        set_icon(btn, icon, token or ("ON_ACCENT" if variant == "primary" else "MUTED"), 16)
    return btn


def repolish(widget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def chip(text: str = "", tone: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("chip", "true")
    if tone:
        label.setProperty("tone", tone)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return label


def set_tone(widget, tone: str) -> None:
    if widget.property("tone") != tone:
        widget.setProperty("tone", tone)
        repolish(widget)


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


def _font(px: float, weight: int = 500) -> QFont:
    font = QFont()
    font.setPixelSize(round(px))
    font.setWeight(QFont.Weight(weight))
    return font


# ── Navegación lateral ────────────────────────────────────────────
class NavButton(QAbstractButton):
    """Elemento de la barra lateral: icono, etiqueta, contador opcional y marca
    de selección. Se comporta como un botón «checkable» estándar."""

    def __init__(self, icon_name: str, label: str, parent=None):
        super().__init__(parent)
        self._icon = icon_name
        self._badge = ""
        self._badge_tone = "ACCENT"
        self._hover = False
        self._keyboard_focus = False
        self.setText(label)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(label)
        self.setProperty("variant", "nav")

    def set_badge(self, text: str, tone: str = "ACCENT") -> None:
        text = str(text or "")
        if (text, tone) != (self._badge, self._badge_tone):
            self._badge, self._badge_tone = text, tone
            self.update()

    def badge(self) -> str:
        return self._badge

    def sizeHint(self) -> QSize:
        return QSize(200, 38)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        # El anillo de foco solo aparece al navegar con el teclado.
        self._keyboard_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._keyboard_focus = False
        super().focusOutEvent(event)

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 1.5, -0.5, -1.5)
        enabled = self.isEnabled()
        checked = self.isChecked()
        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(style.ACCENT_SUBTLE))
            painter.drawRoundedRect(rect, 9, 9)
            painter.setBrush(QColor(style.ACCENT))
            painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 9, 3, rect.height() - 18), 1.5, 1.5)
        elif self._hover and enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(style.SURFACE_3))
            painter.drawRoundedRect(rect, 9, 9)
        if self.hasFocus() and self._keyboard_focus:
            painter.setPen(QPen(QColor(style.LINK), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)

        if not enabled:
            color = style.mix(style.FAINT, style.SIDEBAR, 0.45)
        elif checked:
            color = style.LINK
        elif self._hover:
            color = style.TEXT
        else:
            color = style.MUTED
        painter.drawPixmap(QPoint(14, (self.height() - 18) // 2), lucide.pixmap(self._icon, color, 18))
        painter.setPen(QColor(color))
        painter.setFont(_font(13.5, 650 if checked else 550))
        right = self.width() - 12
        if self._badge:
            badge_font = _font(11, 700)
            metrics = QFontMetrics(badge_font)
            badge_w = max(20, metrics.horizontalAdvance(self._badge) + 12)
            badge_rect = QRectF(right - badge_w, (self.height() - 18) / 2, badge_w, 18)
            tone = getattr(style, self._badge_tone, style.ACCENT)
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(style.rgba_color(tone, 0.16)))
            painter.drawRoundedRect(badge_rect, 9, 9)
            painter.setPen(QColor(tone if enabled else color))
            painter.setFont(badge_font)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, self._badge)
            painter.restore()
            right = int(badge_rect.left()) - 6
        text_rect = QRectF(42, 0, max(0, right - 42), self.height())
        metrics = QFontMetrics(painter.font())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            metrics.elidedText(self.text().strip(), Qt.TextElideMode.ElideRight, int(text_rect.width())),
        )
        painter.end()


# ── Stepper del pipeline ──────────────────────────────────────────
class _StageHandle:
    """Compatibilidad con ``stage_rows[i].set_state(...)`` de versiones previas."""

    def __init__(self, stepper: PipelineStepper, index: int):
        self._stepper = stepper
        self._index = index

    def set_state(self, state: str) -> None:
        self._stepper.set_state(self._index, state)

    @property
    def state(self) -> str:
        return self._stepper.state(self._index)


class PipelineStepper(QWidget):
    """Los cuatro pasos del flujo en una línea: descarga → subida → subtítulos →
    verificación. El paso activo late y puede mostrar un avance real (0-1)."""

    def __init__(self, stages: list[str], icons: list[str] | None = None, parent=None):
        super().__init__(parent)
        self._stages = list(stages)
        self._icons = icons or ["download", "cloud-upload", "captions", "shield-check"]
        self._states = ["pending"] * len(stages)
        self._progress: dict[int, float] = {}
        self._detail: dict[int, str] = {}
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.rows = [_StageHandle(self, i) for i in range(len(stages))]
        style.on_theme_change(self.update)

    def sizeHint(self) -> QSize:
        return QSize(520, 92)

    def state(self, index: int) -> str:
        return self._states[index]

    def set_state(self, index: int, state: str) -> None:
        if not 0 <= index < len(self._states) or state not in ("pending", "active", "done", "error"):
            return
        self._states[index] = state
        if state != "active":
            self._progress.pop(index, None)
            self._detail.pop(index, None)
        active = any(s == "active" for s in self._states)
        if active and not self._timer.isActive() and self.isVisible():
            self._timer.start()
        elif not active:
            self._timer.stop()
        self.update()

    def set_progress(self, index: int, fraction: float | None, detail: str = "") -> None:
        if not 0 <= index < len(self._states):
            return
        if fraction is None:
            self._progress.pop(index, None)
        else:
            self._progress[index] = max(0.0, min(1.0, float(fraction)))
        self._detail[index] = detail
        self.update()

    def reset(self) -> None:
        self._states = ["pending"] * len(self._stages)
        self._progress.clear()
        self._detail.clear()
        self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if any(s == "active" for s in self._states):
            self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.035) % 1.0
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = len(self._stages)
        width = self.width()
        slot = width / count
        node = 34.0
        cy = 24.0
        centers = [slot * i + slot / 2 for i in range(count)]
        colors = {
            "pending": style.BORDER_HI,
            "active": style.ACCENT,
            "done": style.SUCCESS,
            "error": style.ERROR,
        }
        # Conectores
        for i in range(count - 1):
            x1, x2 = centers[i] + node / 2 + 6, centers[i + 1] - node / 2 - 6
            painter.setPen(QPen(QColor(style.BORDER), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(int(x1), int(cy), int(x2), int(cy))
            state = self._states[i]
            fill = 1.0 if state == "done" else self._progress.get(i, 0.0) if state == "active" else 0.0
            if fill > 0:
                painter.setPen(
                    QPen(QColor(colors["done" if state == "done" else "active"]), 2,
                         Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                )
                painter.drawLine(int(x1), int(cy), int(x1 + (x2 - x1) * fill), int(cy))
        # Nodos
        for i, label in enumerate(self._stages):
            state = self._states[i]
            cx = centers[i]
            circle = QRectF(cx - node / 2, cy - node / 2, node, node)
            color = QColor(colors[state])
            if state == "active":
                pulse = 0.5 - abs(self._phase - 0.5)
                halo = QColor(style.ACCENT)
                halo.setAlphaF(0.10 + 0.22 * pulse * 2)
                grow = 4 + 5 * pulse * 2
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(halo)
                painter.drawEllipse(circle.adjusted(-grow, -grow, grow, grow))
            painter.setPen(QPen(color, 1.6))
            if state in ("active", "done"):
                painter.setBrush(QColor(style.ACCENT_SUBTLE if state == "active" else style.SUCCESS_BG))
            elif state == "error":
                painter.setBrush(QColor(style.ERROR_BG))
            else:
                painter.setBrush(QColor(style.SURFACE_2))
            painter.drawEllipse(circle)
            if state == "active" and i in self._progress:
                arc_pen = QPen(QColor(style.ACCENT), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                painter.setPen(arc_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawArc(circle.adjusted(-3.5, -3.5, 3.5, 3.5), 90 * 16,
                                int(-360 * 16 * self._progress[i]))
            glyph = {"done": "check", "error": "x"}.get(state, self._icons[i % len(self._icons)])
            glyph_color = {
                "pending": style.FAINT, "active": style.LINK, "done": style.SUCCESS, "error": style.ERROR,
            }[state]
            painter.drawPixmap(QPoint(int(cx - 8), int(cy - 8)), lucide.pixmap(glyph, glyph_color, 16, 2.2))
            painter.setFont(_font(12.5, 650 if state == "active" else 520))
            painter.setPen(QColor(style.TEXT if state in ("active", "done") else style.MUTED))
            text_rect = QRectF(cx - slot / 2 + 4, cy + node / 2 + 6, slot - 8, 18)
            metrics = QFontMetrics(painter.font())
            painter.drawText(
                text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(text_rect.width())),
            )
            detail = self._detail.get(i, "") if state == "active" else ""
            if state == "active" and i in self._progress and not detail:
                detail = f"{self._progress[i] * 100:.0f}%"
            if detail:
                painter.setFont(_font(11.5, 600))
                painter.setPen(QColor(style.LINK))
                detail_rect = QRectF(cx - slot / 2 + 4, cy + node / 2 + 24, slot - 8, 16)
                metrics = QFontMetrics(painter.font())
                painter.drawText(
                    detail_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                    metrics.elidedText(detail, Qt.TextElideMode.ElideRight, int(detail_rect.width())),
                )
        painter.end()


# ── Indicadores ───────────────────────────────────────────────────
class StatusDot(QWidget):
    """Punto de estado; late suavemente mientras ``active`` es verdadero."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tone = "FAINT"
        self._active = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(16, 16)

    def set_state(self, tone: str, active: bool = False) -> None:
        self._tone, self._active = tone, active
        if active and self.isVisible():
            self._timer.start()
        elif not active:
            self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._active:
            self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.04) % 1.0
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(getattr(style, self._tone, style.FAINT))
        painter.setPen(Qt.PenStyle.NoPen)
        if self._active:
            pulse = 1 - abs(self._phase * 2 - 1)
            halo = QColor(color)
            halo.setAlphaF(0.12 + 0.25 * pulse)
            radius = 4.5 + 3 * pulse
            painter.setBrush(halo)
            painter.drawEllipse(QRectF(8 - radius, 8 - radius, radius * 2, radius * 2))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(4, 4, 8, 8))
        painter.end()


class Spinner(QWidget):
    def __init__(self, size: int = 18, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(size, size)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 14) % 360
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        painter.setPen(QPen(QColor(style.BORDER_HI), 2))
        painter.drawEllipse(rect)
        painter.setPen(QPen(QColor(style.ACCENT), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, -self._angle * 16, 100 * 16)
        painter.end()


class ProgressRing(QWidget):
    """Anillo de avance con valor central (p. ej. clases del día)."""

    def __init__(self, size: int = 64, thickness: float = 6.0, parent=None):
        super().__init__(parent)
        self._value = 0.0
        self._text = ""
        self._tone = "ACCENT"
        self._thickness = thickness
        self.setFixedSize(size, size)
        style.on_theme_change(self.update)

    def set_value(self, fraction: float, text: str = "", tone: str = "ACCENT") -> None:
        self._value = max(0.0, min(1.0, float(fraction)))
        self._text, self._tone = text, tone
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inset = self._thickness / 2 + 1
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        painter.setPen(QPen(QColor(style.SURFACE_3), self._thickness))
        painter.drawEllipse(rect)
        if self._value > 0:
            painter.setPen(
                QPen(QColor(getattr(style, self._tone, style.ACCENT)), self._thickness,
                     Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            )
            painter.drawArc(rect, 90 * 16, int(-360 * 16 * self._value))
        if self._text:
            painter.setPen(QColor(style.TEXT))
            painter.setFont(_font(max(11, self.width() * 0.22), 700))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()


# ── Tarjetas ──────────────────────────────────────────────────────
class KpiCard(QFrame):
    """Indicador: icono en pastilla, valor grande, etiqueta y nota."""

    clicked = Signal()

    def __init__(self, icon_name: str, label: str, tone: str = "LINK", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._tone = tone
        self._clickable = False
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(14)
        self._badge = _IconBadge(icon_name, tone)
        outer.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(1)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("kpiValue")
        self.name_label = QLabel(label)
        self.name_label.setObjectName("kpiLabel")
        self.note_label = QLabel("")
        self.note_label.setObjectName("faint")
        self.note_label.setWordWrap(True)
        column.addWidget(self.value_label)
        column.addWidget(self.name_label)
        column.addWidget(self.note_label)
        outer.addLayout(column, 1)
        self.setMinimumHeight(96)

    def set_value(self, value: str, note: str | None = None) -> None:
        self.value_label.setText(str(value))
        if note is not None:
            self.note_label.setText(note)
            self.note_label.setVisible(bool(note))

    def set_clickable(self, clickable: bool = True) -> None:
        self._clickable = clickable
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        )

    def mouseReleaseEvent(self, event) -> None:
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _IconBadge(QWidget):
    def __init__(self, icon_name: str, tone: str = "LINK", size: int = 38, parent=None):
        super().__init__(parent)
        self._icon, self._tone = icon_name, tone
        self.setFixedSize(size, size)
        style.on_theme_change(self.update)

    def set_icon(self, icon_name: str, tone: str | None = None) -> None:
        self._icon = icon_name
        if tone:
            self._tone = tone
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = getattr(style, self._tone, style.LINK)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(style.rgba_color(color, 0.14)))
        painter.drawRoundedRect(QRectF(self.rect()), 10, 10)
        size = round(self.width() * 0.5)
        offset = (self.width() - size) // 2
        painter.drawPixmap(QPoint(offset, offset), lucide.pixmap(self._icon, color, size))
        painter.end()


def icon_badge(icon_name: str, tone: str = "LINK", size: int = 38) -> QWidget:
    return _IconBadge(icon_name, tone, size)


class EmptyState(QWidget):
    """Estado vacío con icono, título, explicación y acción opcional."""

    def __init__(self, icon_name: str, title: str, text: str = "", action: str = "", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 28, 24, 28)
        lay.setSpacing(8)
        lay.addStretch()
        self.badge = _IconBadge(icon_name, "FAINT", 52)
        lay.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(6)
        self.title = QLabel(title)
        self.title.setObjectName("h2")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title)
        self.text = QLabel(text)
        self.text.setObjectName("muted")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        self.text.setMaximumWidth(460)
        lay.addWidget(self.text, 0, Qt.AlignmentFlag.AlignHCenter)
        self.button = QPushButton(action)
        self.button.setProperty("variant", "primary")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setVisible(bool(action))
        lay.addSpacing(6)
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch()

    def set_content(self, title: str, text: str = "", icon_name: str | None = None) -> None:
        self.title.setText(title)
        self.text.setText(text)
        if icon_name:
            self.badge.set_icon(icon_name)


class Banner(QFrame):
    """Aviso en línea con icono, texto y acción opcional. ``tone``: info,
    success, warning o error."""

    _ICONS = {"info": "info", "success": "circle-check", "warning": "triangle-alert", "error": "circle-alert"}
    _TOKENS = {"info": "INFO", "success": "SUCCESS", "warning": "WARNING", "error": "ERROR"}

    def __init__(self, text: str = "", tone: str = "info", action: str = "", parent=None):
        super().__init__(parent)
        self.setProperty("banner", "true")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(10)
        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.label, 1)
        self.action = QPushButton(action)
        self.action.setProperty("variant", "secondary")
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.setVisible(bool(action))
        lay.addWidget(self.action, 0, Qt.AlignmentFlag.AlignTop)
        self._tone = ""
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        tone = tone if tone in self._ICONS else "info"
        if tone != self._tone:
            self._tone = tone
            self.setProperty("tone", tone)
            set_icon(self.icon, self._ICONS[tone], self._TOKENS[tone], 16)
            repolish(self)

    def tone(self) -> str:
        return self._tone

    def setText(self, text: str) -> None:
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()

    def show_message(self, text: str, tone: str = "info") -> None:
        self.set_tone(tone)
        self.label.setText(text)
        self.show()


class SearchField(QLineEdit):
    """Campo de búsqueda con lupa integrada y botón de limpiar."""

    def __init__(self, placeholder: str = "Buscar…", parent=None):
        super().__init__(parent)
        self.setProperty("variant", "search")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        style.on_theme_change(self.update)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.drawPixmap(QPoint(11, (self.height() - 16) // 2), lucide.pixmap("search", style.FAINT, 16))
        painter.end()


class ElidedLabel(QLabel):
    """Etiqueta de una línea que recorta con puntos suspensivos."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setFullText(text)

    def setFullText(self, text: str) -> None:
        self._full = str(text)
        self.setToolTip(self._full)
        self._elide()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        metrics = QFontMetrics(self.font())
        super().setText(
            metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, max(10, self.width() - 2))
        )


# ── Toasts ────────────────────────────────────────────────────────
class _Toast(QFrame):
    closed = Signal(object)

    def __init__(self, title: str, message: str, tone: str, parent):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        token = Banner._TOKENS.get(tone, "INFO")
        color = getattr(style, token)
        self.setStyleSheet(
            f"QFrame#card {{ background-color: {style.SURFACE_3}; border: 1px solid {style.BORDER_HI};"
            f" border-left: 3px solid {color}; border-radius: 10px; }}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 11, 10, 11)
        lay.setSpacing(10)
        lay.addWidget(icon_label(Banner._ICONS.get(tone, "info"), token, 18), 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(2)
        head = QLabel(title)
        head.setObjectName("h3")
        head.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(head)
        if message:
            body = QLabel(message)
            body.setObjectName("muted")
            body.setWordWrap(True)
            body.setTextFormat(Qt.TextFormat.PlainText)
            column.addWidget(body)
        lay.addLayout(column, 1)
        close = icon_button("x", "Cerrar aviso", "FAINT", 14)
        close.clicked.connect(self.dismiss)
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.setFixedWidth(360)
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._closing = False

    def appear(self, timeout_ms: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        if timeout_ms > 0:
            self._life = QTimer(self)
            self._life.setSingleShot(True)
            self._life.timeout.connect(self.dismiss)
            self._life.start(timeout_ms)

    def dismiss(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(lambda: self.closed.emit(self))
        self._anim.start()


class ToastHost(QWidget):
    """Capa de avisos no bloqueantes anclada abajo a la derecha de su ventana."""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self._window = window
        self._toasts: list[_Toast] = []
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addStretch()
        window.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._window and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._reposition()
        return False

    def notify(self, title: str, message: str = "", tone: str = "info", timeout_ms: int = 5200) -> None:
        while len(self._toasts) >= 4:
            self._remove(self._toasts[0])
        toast = _Toast(title, message, tone, self)
        toast.closed.connect(self._remove)
        self._toasts.append(toast)
        self._layout.addWidget(toast, 0, Qt.AlignmentFlag.AlignRight)
        self._reposition()
        self.show()
        self.raise_()
        toast.appear(timeout_ms)

    def _remove(self, toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
            self._layout.removeWidget(toast)
            toast.hide()
            toast.deleteLater()
        if not self._toasts:
            self.hide()
        else:
            self._reposition()

    def _reposition(self) -> None:
        if not self._toasts:
            return
        self.adjustSize()
        height = self._layout.sizeHint().height()
        self.setGeometry(
            self._window.width() - 360 - 22,
            max(8, self._window.height() - height - 22),
            360,
            height,
        )


# ── Varios ────────────────────────────────────────────────────────
class Card(QFrame):
    """Tarjeta con cabecera (título, descripción y acciones) y cuerpo."""

    def __init__(self, title: str = "", subtitle: str = "", parent=None, object_name: str = "card"):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(12)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("h2")
        titles.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("muted")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        titles.addWidget(self.subtitle_label)
        self.header.addLayout(titles, 1)
        if title or subtitle:
            self.body.addLayout(self.header)

    def add_action(self, widget: QWidget) -> None:
        self.header.addWidget(widget, 0, Qt.AlignmentFlag.AlignTop)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


class WeekStrip(QWidget):
    """Tira S1–S16 del ciclo: muestra de un vistazo qué semanas están completas,
    con pendientes o sin clases publicadas."""

    def __init__(self, weeks: int = 16, parent=None):
        super().__init__(parent)
        self._weeks = weeks
        self._data: dict[int, tuple[int, int]] = {}
        self._current: int | None = None
        self.setMinimumHeight(54)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        style.on_theme_change(self.update)

    def set_data(self, data: dict[int, tuple[int, int]], current: int | None) -> None:
        """data: semana → (publicadas, pendientes)."""
        self._data, self._current = dict(data), current
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(480, 54)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gap = 6.0
        cell_w = (self.width() - gap * (self._weeks - 1)) / self._weeks
        for week in range(1, self._weeks + 1):
            x = (week - 1) * (cell_w + gap)
            rect = QRectF(x, 2, cell_w, 30)
            published, pending = self._data.get(week, (0, 0))
            if published and not pending:
                fill, border, text = style.SUCCESS_BG, style.ACCENT_LINE, style.SUCCESS
            elif pending:
                fill, border, text = style.WARNING_BG, style.rgba_color(style.WARNING, 0.4), style.WARNING
            else:
                fill, border, text = style.SURFACE_2, style.BORDER, style.FAINT
            painter.setBrush(QColor(fill))
            is_current = week == self._current
            painter.setPen(QPen(QColor(style.LINK if is_current else border), 1.6 if is_current else 1))
            painter.drawRoundedRect(rect, 7, 7)
            painter.setPen(QColor(text))
            painter.setFont(_font(11.5, 700 if is_current else 600))
            label = str(published - pending) + "/" + str(published) if published else "·"
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
            painter.setPen(QColor(style.LINK if is_current else style.FAINT))
            painter.setFont(_font(10.5, 700 if is_current else 500))
            painter.drawText(QRectF(x, 35, cell_w, 16), Qt.AlignmentFlag.AlignCenter, f"S{week}")
        painter.end()


def rounded_path(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


# ── Tablas ────────────────────────────────────────────────────────
from PySide6.QtWidgets import QStyle, QStyledItemDelegate  # noqa: E402

_STATUS_TONES = (
    ("success", ("txt verificado", "txt disponible", "correcto", "cantidad esperada lista", "revisado", "listo")),
    ("error", ("acción necesaria", "error", "falló")),
    (
        "warning",
        ("por resolver", "pendiente", "atención", "parcial", "recuperar", "incompleta", "sin aparecer",
         "configura", "busca para", "en cola"),
    ),
    (
        "info",
        ("comprobando", "preparando", "texto en proceso", "verificando", "descargando", "subiendo",
         "esperando", "continuar", "en espera", "iniciada", "subida", "semana en curso", "video en esta pc"),
    ),
)  # fmt: skip


def status_tone(text: str) -> str:
    """Tono de la pastilla para un texto de estado ('' = neutro)."""
    low = str(text or "").casefold()
    for tone, words in _STATUS_TONES:
        if any(word in low for word in words):
            return tone
    return ""


class StatusDelegate(QStyledItemDelegate):
    """Pinta la celda de estado como una pastilla con punto de color. El texto,
    el tooltip y los datos del ítem no cambian."""

    _TOKENS = {
        "success": ("SUCCESS", "SUCCESS_BG"),
        "warning": ("WARNING", "WARNING_BG"),
        "error": ("ERROR", "ERROR_BG"),
        "info": ("INFO", "INFO_BG"),
        "": ("MUTED", "SURFACE_3"),
    }

    def paint(self, painter, option, index) -> None:
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(style.ACCENT_SUBTLE))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, QColor(style.SURFACE_2))
        painter.setPen(QPen(QColor(style.BORDER), 1))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        fg_token, bg_token = self._TOKENS[status_tone(text)]
        fg, bg = getattr(style, fg_token), getattr(style, bg_token)
        font = _font(12, 650)
        metrics = QFontMetrics(font)
        available = option.rect.width() - 20
        label = metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(20, available - 26))
        width = min(available, metrics.horizontalAdvance(label) + 28)
        pill = QRectF(
            option.rect.left() + 10, option.rect.center().y() - 11, max(30, width), 22
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(pill, 11, 11)
        painter.setBrush(QColor(fg))
        painter.drawEllipse(QRectF(pill.left() + 9, pill.center().y() - 3, 6, 6))
        painter.setPen(QColor(fg))
        painter.setFont(font)
        painter.drawText(
            pill.adjusted(20, 0, -6, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            label,
        )
        painter.restore()


# ── Layout fluido ─────────────────────────────────────────────────
from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QLayout  # noqa: E402


class FlowLayout(QLayout):
    """Coloca los widgets en fila y salta de línea cuando no caben (chips)."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect, apply: bool) -> int:
        margins = self.contentsMargins()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        right = rect.right() - margins.right()
        line_height = 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > right + 1 and line_height > 0:
                x = rect.x() + margins.left()
                y += line_height + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x += hint.width() + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()

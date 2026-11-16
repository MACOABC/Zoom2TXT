"""Gráficos ligeros dibujados con QPainter (sin dependencias externas), en el
lenguaje visual del tema: tarjeta KPI clásica y barras con valores."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from zoom2txt.gui import style


class StatCard(QFrame):
    """Tarjeta KPI: número grande + etiqueta + subtítulo opcional."""

    def __init__(self, value: str, label: str, sub: str = "", accent: str | None = None):
        super().__init__()
        self.setObjectName("metric")
        self.setMinimumHeight(88)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 12, 8)
        lay.setSpacing(4)
        num = QLabel(value)
        num.setObjectName("kpiValue")
        if accent:
            num.setStyleSheet(f"color: {accent};")
        lbl = QLabel(label)
        lbl.setObjectName("kpiLabel")
        lbl.setWordWrap(True)
        lay.addWidget(num)
        lay.addWidget(lbl)
        if sub:
            s = QLabel(sub)
            s.setWordWrap(True)
            s.setObjectName("faint")
            lay.addWidget(s)
        lay.addStretch()


class BarChart(QWidget):
    """Barras verticales con etiquetas. data: lista de (etiqueta, valor).
    ``accent`` acepta un color o el nombre de un token de ``style``."""

    def __init__(self, title: str, data: list[tuple[str, float]], accent: str | None = None):
        super().__init__()
        self._title = title
        self._data = data
        self._accent = accent or "ACCENT"
        self.setMinimumHeight(176)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        style.on_theme_change(self.update)

    def set_data(self, data: list[tuple[str, float]]) -> None:
        self._data = data
        self.update()

    def _color(self) -> QColor:
        return QColor(getattr(style, self._accent, self._accent))

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        p.setBrush(QColor(style.SURFACE))
        p.setPen(QPen(QColor(style.BORDER), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), style.RADIUS, style.RADIUS)

        pad = 18
        p.setPen(QColor(style.TEXT))
        f = QFont()
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(pad, pad - 4, w - 2 * pad, 22), Qt.AlignmentFlag.AlignLeft, self._title)

        if not self._data:
            p.setPen(QColor(style.FAINT))
            small = QFont()
            small.setPixelSize(13)
            p.setFont(small)
            p.drawText(self.rect().adjusted(0, 18, 0, 0), Qt.AlignmentFlag.AlignCenter, "Sin datos todavía")
            p.end()
            return

        top = pad + 34
        bottom = h - pad - 20  # espacio para etiquetas
        chart_h = max(10, bottom - top)
        vmax = max(v for _, v in self._data) or 1
        n = len(self._data)
        gap = 10
        slot = max(1.0, (w - 2 * pad) / n)
        bw = max(2.0, min(34.0, slot - gap))
        label_every = max(1, int(48 / slot) + (1 if slot < 48 else 0))
        p.setPen(QPen(QColor(style.BORDER), 1))
        p.drawLine(int(pad), int(bottom), int(w - pad), int(bottom))

        base = self._color()
        fsmall = QFont()
        fsmall.setPixelSize(11)
        fsmall.setWeight(QFont.Weight.DemiBold)
        for i, (label, value) in enumerate(self._data):
            x = pad + i * slot + (slot - bw) / 2
            bh = chart_h * (value / vmax)
            y = bottom - bh
            gradient = QLinearGradient(0, y, 0, bottom)
            gradient.setColorAt(0.0, base)
            faded = QColor(base)
            faded.setAlphaF(0.45)
            gradient.setColorAt(1.0, faded)
            p.setBrush(gradient)
            p.setPen(Qt.PenStyle.NoPen)
            radius = min(6.0, bw / 2)
            p.drawRoundedRect(QRectF(x, y, bw, max(bh, 2)), radius, radius)

            if i % label_every:
                continue

            p.setPen(QColor(style.TEXT))
            p.setFont(fsmall)
            p.drawText(
                QRectF(x - gap / 2, y - 18, bw + gap, 16),
                Qt.AlignmentFlag.AlignCenter,
                str(int(value)),
            )
            p.setPen(QColor(style.FAINT))
            p.drawText(
                QRectF(pad + i * slot + 3, bottom + 4, max(1, slot - 6), 18),
                Qt.AlignmentFlag.AlignCenter,
                p.fontMetrics().elidedText(
                    label, Qt.TextElideMode.ElideRight, max(1, int(slot - 6))
                ),
            )
        p.end()

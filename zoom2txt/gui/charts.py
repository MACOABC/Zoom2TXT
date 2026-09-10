"""Gráficos ligeros dibujados con QPainter (sin dependencias externas):
tarjeta KPI y gráfico de barras, en el lenguaje visual del tema."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QFrame, QSizePolicy, QVBoxLayout, QWidget

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
        from PySide6.QtWidgets import QLabel

        num = QLabel(value)
        num.setStyleSheet(f"font-size: 26px; font-weight: 700; color: {accent or style.LINK};")
        lbl = QLabel(label)
        lbl.setStyleSheet(f"font-size: 13px; color: {style.MUTED};")
        lbl.setWordWrap(True)
        lay.addWidget(num)
        lay.addWidget(lbl)
        if sub:
            s = QLabel(sub)
            s.setWordWrap(True)
            s.setStyleSheet(f"font-size: 12px; color: {style.MUTED};")
            lay.addWidget(s)
        lay.addStretch()


class BarChart(QWidget):
    """Barras verticales con etiquetas. data: lista de (etiqueta, valor)."""

    def __init__(self, title: str, data: list[tuple[str, float]], accent: str | None = None):
        super().__init__()
        self._title = title
        self._data = data
        self._accent = accent or style.LINK
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_data(self, data: list[tuple[str, float]]) -> None:
        self._data = data
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # fondo tarjeta
        p.setBrush(QColor(style.SURFACE))
        p.setPen(QPen(QColor(style.BORDER), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 8, 8)

        pad = 16
        # título
        p.setPen(QColor(style.TEXT))
        f = QFont()
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.drawText(
            QRectF(pad, pad - 4, w - 2 * pad, 22),
            Qt.AlignmentFlag.AlignLeft,
            self._title,
        )

        if not self._data:
            p.setPen(QColor(style.MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Sin datos todavía")
            p.end()
            return

        top = pad + 28
        bottom = h - pad - 22  # espacio para etiquetas
        chart_h = bottom - top
        vmax = max(v for _, v in self._data) or 1
        n = len(self._data)
        gap = 10
        slot = max(1.0, (w - 2 * pad) / n)
        bw = max(1.0, min(40.0, slot - gap))
        label_every = max(1, int(48 / slot) + (1 if slot < 48 else 0))
        p.setPen(QPen(QColor(style.BORDER_HI), 1))
        p.drawLine(int(pad), int(bottom), int(w - pad), int(bottom))

        fsmall = QFont()
        fsmall.setPixelSize(11)
        for i, (label, value) in enumerate(self._data):
            x = pad + i * slot + (slot - bw) / 2
            bh = chart_h * (value / vmax)
            y = bottom - bh
            p.setBrush(QColor(self._accent))
            p.setPen(Qt.PenStyle.NoPen)
            radius = min(6.0, bw / 2)
            p.drawRoundedRect(QRectF(x, y, bw, max(bh, 2)), radius, radius)

            if i % label_every:
                continue

            # valor encima
            p.setPen(QColor(style.TEXT))
            p.setFont(fsmall)
            p.drawText(
                QRectF(x - gap / 2, y - 18, bw + gap, 16),
                Qt.AlignmentFlag.AlignCenter,
                str(int(value)),
            )
            # etiqueta debajo
            p.setPen(QColor(style.MUTED))
            p.drawText(
                QRectF(pad + i * slot + 3, bottom + 4, max(1, slot - 6), 18),
                Qt.AlignmentFlag.AlignCenter,
                p.fontMetrics().elidedText(
                    label, Qt.TextElideMode.ElideRight, max(1, int(slot - 6))
                ),
            )
        p.end()

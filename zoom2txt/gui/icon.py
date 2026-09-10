"""Ícono de Zoom2TXT dibujado en tiempo de ejecución: cuadrado
redondeado con degradado violeta y una «Z» con líneas de texto —
de video a texto. Sin archivos externos que empaquetar."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)

_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _paint(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    p.setBrush(QColor("#176b52"))
    p.setPen(Qt.PenStyle.NoPen)
    radius = size * 0.18
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # Geometric Z: independent of installed fonts.
    from PySide6.QtGui import QPainterPath

    pen = QPen(QColor("#ffffff"))
    pen.setWidthF(max(1.5, size * 0.085))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    shape = QPainterPath()
    shape.moveTo(size * 0.29, size * 0.25)
    shape.lineTo(size * 0.71, size * 0.25)
    shape.lineTo(size * 0.29, size * 0.64)
    shape.lineTo(size * 0.71, size * 0.64)
    p.drawPath(shape)

    # líneas de "texto" bajo la Z (el resultado final: un .txt)
    if size >= 24:
        pen = QPen(QColor(255, 255, 255, 200))
        pen.setWidthF(max(1.0, size * 0.05))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        y = size * 0.80
        p.drawLine(
            QRectF(size * 0.24, y, 0, 0).topLeft(),
            QRectF(size * 0.76, y, 0, 0).topLeft(),
        )
        y2 = size * 0.88
        p.drawLine(
            QRectF(size * 0.32, y2, 0, 0).topLeft(),
            QRectF(size * 0.68, y2, 0, 0).topLeft(),
        )

    p.end()
    return pm


def make_app_icon() -> QIcon:
    icon = QIcon()
    for s in _SIZES:
        icon.addPixmap(_paint(s))
    return icon


def save_pngs(folder) -> list:
    """Guarda los PNG del ícono (para generar el .ico del ejecutable)."""
    from pathlib import Path

    out = []
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for s in _SIZES:
        path = folder / f"icon_{s}.png"
        _paint(s).save(str(path), "PNG")
        out.append(path)
    return out

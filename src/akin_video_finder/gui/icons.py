"""Small vector icons drawn with QPainter.

The GUI ships no SVG icon set, and adding one would drag a new data file and
the Qt SVG plugin into the PyInstaller build.  These icons are line drawings
on a transparent pixmap, which keeps the packaging story unchanged and lets a
caller tint them with the active theme.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _pen(color: QColor, width: float = 1.7) -> QPen:
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _pixmap(size: int) -> tuple[QPixmap, QPainter]:
    # Draw at 2x and let QIcon downscale; this stays sharp at 125/150% DPI
    # without depending on a device-pixel-ratio available only after startup.
    # The shapes are drawn on a 24-unit canvas, so scale that canvas to the
    # requested size. The pixmap's device-pixel-ratio already makes QPainter
    # work in logical pixels; scaling by 2 here would draw the icon twice as
    # large and clip its right and bottom edges.
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / 24.0, size / 24.0)
    return pixmap, painter


def _magnifier(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color))
    painter.drawEllipse(QRectF(3.5, 3.5, 11.0, 11.0))
    painter.drawLine(QPointF(13.2, 13.2), QPointF(19.0, 19.0))


def _shield(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color))
    path = QPainterPath()
    path.moveTo(11.0, 2.5)
    path.lineTo(19.0, 5.5)
    path.lineTo(19.0, 11.5)
    path.cubicTo(19.0, 16.5, 15.4, 19.4, 11.0, 21.0)
    path.cubicTo(6.6, 19.4, 3.0, 16.5, 3.0, 11.5)
    path.lineTo(3.0, 5.5)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(QPointF(7.2, 11.4), QPointF(10.0, 14.1))
    painter.drawLine(QPointF(10.0, 14.1), QPointF(15.0, 8.7))


def _eye(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color))
    path = QPainterPath()
    path.moveTo(2.5, 11.0)
    path.cubicTo(5.0, 5.5, 17.0, 5.5, 19.5, 11.0)
    path.cubicTo(17.0, 16.5, 5.0, 16.5, 2.5, 11.0)
    painter.drawPath(path)
    painter.drawEllipse(QRectF(9.0, 8.5, 4.0, 5.0))


def _check(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color, 2.2))
    painter.drawLine(QPointF(3.5, 11.0), QPointF(8.5, 16.0))
    painter.drawLine(QPointF(8.5, 16.0), QPointF(18.5, 5.0))


def _play(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    path = QPainterPath()
    path.moveTo(7.0, 4.5)
    path.lineTo(18.0, 11.0)
    path.lineTo(7.0, 17.5)
    path.closeSubpath()
    painter.drawPath(path)


def _folder(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color))
    path = QPainterPath()
    path.moveTo(2.5, 6.5)
    path.lineTo(2.5, 17.5)
    path.lineTo(19.5, 17.5)
    path.lineTo(19.5, 7.5)
    path.lineTo(10.5, 7.5)
    path.lineTo(8.5, 5.0)
    path.lineTo(2.5, 5.0)
    path.closeSubpath()
    painter.drawPath(path)


def _file(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color))
    path = QPainterPath()
    path.moveTo(5.0, 3.0)
    path.lineTo(14.5, 3.0)
    path.lineTo(18.0, 6.5)
    path.lineTo(18.0, 19.0)
    path.lineTo(5.0, 19.0)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(QPointF(14.5, 3.0), QPointF(14.5, 6.5))
    painter.drawLine(QPointF(14.5, 6.5), QPointF(18.0, 6.5))


def _trash(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color))
    painter.drawLine(QPointF(4.0, 6.0), QPointF(18.0, 6.0))
    painter.drawLine(QPointF(7.0, 6.0), QPointF(7.5, 3.5))
    painter.drawLine(QPointF(7.5, 3.5), QPointF(14.5, 3.5))
    painter.drawLine(QPointF(14.5, 3.5), QPointF(15.0, 6.0))
    path = QPainterPath()
    path.moveTo(5.5, 6.0)
    path.lineTo(6.5, 19.0)
    path.lineTo(15.5, 19.0)
    path.lineTo(16.5, 6.0)
    painter.drawPath(path)
    painter.drawLine(QPointF(9.0, 9.0), QPointF(9.3, 16.0))
    painter.drawLine(QPointF(13.0, 9.0), QPointF(12.7, 16.0))


def _export(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color))
    painter.drawRect(QRectF(3.5, 9.0, 17.0, 10.5))
    painter.drawLine(QPointF(11.0, 3.0), QPointF(11.0, 13.5))
    painter.drawLine(QPointF(11.0, 13.5), QPointF(7.5, 10.0))
    painter.drawLine(QPointF(11.0, 13.5), QPointF(14.5, 10.0))


def _info(painter: QPainter, color: QColor) -> None:
    painter.setPen(_pen(color))
    painter.drawEllipse(QRectF(3.0, 3.0, 16.0, 16.0))
    painter.drawLine(QPointF(11.0, 10.0), QPointF(11.0, 16.0))
    painter.drawPoint(QPointF(11.0, 7.0))


_DRAWERS = {
    "magnifier": _magnifier,
    "shield": _shield,
    "eye": _eye,
    "check": _check,
    "play": _play,
    "folder": _folder,
    "file": _file,
    "trash": _trash,
    "export": _export,
    "info": _info,
}


def icon(name: str, color: str = "#e6e8ea", size: int = 20) -> QIcon:
    """A tinted line icon; an unknown name returns a null icon."""
    drawer = _DRAWERS.get(name)
    if drawer is None:
        return QIcon()
    pixmap, painter = _pixmap(size)
    try:
        drawer(painter, QColor(color))
    finally:
        painter.end()
    return QIcon(pixmap)

"""Table delegates for the two-line cells and status badges.

``QTableWidgetItem`` cannot hold rich text or a second line without turning
every row into a widget; the ignored-pairs table has ~1,500 rows, so widgets
are not an option.  These delegates paint the same two-line shape the review
uses while keeping one item (and its text) per cell for search, tests and the
existing click mapping.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from akin_video_finder.gui.theme import font_px, token


class CheckDelegate(QStyledItemDelegate):
    """A centred, themed checkbox for a table cell.

    The platform's item-view checkbox followed the system palette rather than
    the app tokens: in the dark theme it drew a black tick on a dark row (and
    on some styles the tick landed outside its box). Painting the 16 px box and
    its tick from the same tokens as the rest of the page keeps it legible and
    keeps the tick inside the box.
    """

    BOX = 16.0

    @staticmethod
    def _checked(state: object) -> bool:
        # index.data() returns an int for a state the item was born with and
        # a CheckState enum for one set through setCheckState(Checked), so
        # comparing to only one spelling made a checked box look unchecked.
        return state in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)

    @classmethod
    def _box_for(cls, rect: QRect) -> QRectF:
        # QRect.center() truncates half-pixels: in a 26 px cell it returned
        # y=12, so the box sat 4 px from the top and 6 px from the bottom.
        # Measuring the QRectF centre keeps the margins equal at every DPI.
        center = QRectF(rect).center()
        return QRectF(
            center.x() - cls.BOX / 2,
            center.y() - cls.BOX / 2,
            cls.BOX,
            cls.BOX,
        )

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        state = index.data(Qt.ItemDataRole.CheckStateRole)
        # An excluded row and a pattern row have no checkable state; both
        # fall through to the plain paint, leaving the cell blank rather
        # than drawing a disabled-looking box or dash.
        if state is None or not (index.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        if opt.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(opt.rect, opt.palette.highlight())
        checked = self._checked(state)
        box = self._box_for(opt.rect)
        accent = QColor(token("accent"))
        painter.setPen(QPen(accent if checked else QColor(token("text_faint")), 1.4))
        painter.setBrush(QColor(token("accent")) if checked else QColor(token("surface_sunken")))
        painter.drawRoundedRect(box, 3, 3)
        if checked:
            tick = QPainterPath()
            tick.moveTo(box.left() + 3.2, box.center().y())
            tick.lineTo(box.center().x() - 0.6, box.bottom() - 3.4)
            tick.lineTo(box.right() - 3.0, box.top() + 3.6)
            painter.setPen(QPen(QColor(token("accent_fg")), 1.8))
            painter.drawPath(tick)
        painter.restore()

    def editorEvent(
        self,
        event: QEvent,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        if event.type() == QEvent.Type.MouseButtonRelease:
            state = index.data(Qt.ItemDataRole.CheckStateRole)
            if state is not None and (index.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                model.setData(
                    index,
                    Qt.CheckState.Unchecked.value
                    if self._checked(state)
                    else Qt.CheckState.Checked.value,
                    Qt.ItemDataRole.CheckStateRole,
                )
                return True
        return super().editorEvent(event, model, option, index)


class DeleteCellDelegate(QStyledItemDelegate):
    """A centred red cross; clicking the cell removes its row.

    Painted rather than added as a button widget: the folder table keeps one
    item per cell like the other delegate-drawn tables, and the whole narrow
    column is the hit target. The cell's accessible name carries the action
    because a drawn cross has no text of its own.
    """

    RADIUS = 4.5

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        if opt.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(opt.rect, opt.palette.highlight())
        if opt.state & QStyle.StateFlag.State_MouseOver:
            # A wash built from the danger hue, not the stylesheet's
            # rgba() token: QColor does not parse css rgba(), and an
            # invalid brush painted the hover square black.
            wash = QColor(token("danger"))
            wash.setAlpha(36)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(wash)
            painter.drawRoundedRect(opt.rect.adjusted(7, 3, -7, -3), 4, 4)
        painter.setPen(
            QPen(
                QColor(token("danger")),
                1.7,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        center = QRectF(opt.rect).center()
        painter.drawLine(
            QPointF(center.x() - self.RADIUS, center.y() - self.RADIUS),
            QPointF(center.x() + self.RADIUS, center.y() + self.RADIUS),
        )
        painter.drawLine(
            QPointF(center.x() + self.RADIUS, center.y() - self.RADIUS),
            QPointF(center.x() - self.RADIUS, center.y() + self.RADIUS),
        )
        painter.restore()


class TwoLineDelegate(QStyledItemDelegate):
    """Primary text on line one, muted secondary on line two."""

    def __init__(
        self,
        primary_role: int = int(Qt.ItemDataRole.UserRole) + 1,
        secondary_role: int = int(Qt.ItemDataRole.UserRole) + 2,
    ) -> None:
        super().__init__()
        self.primary_role = primary_role
        self.secondary_role = secondary_role

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        background = index.data(Qt.ItemDataRole.BackgroundRole)
        if background is not None:
            painter.fillRect(opt.rect, QColor(background))
        elif opt.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(opt.rect, opt.palette.highlight())
        primary = str(index.data(self.primary_role) or opt.text or "")
        secondary = str(index.data(self.secondary_role) or "")
        width = max(10, opt.rect.width() - 12)
        primary_font = font_px(12, 600)
        secondary_font = font_px(10, 400)
        painter.setFont(primary_font)
        painter.setPen(QColor(token("text")))
        primary_metrics = painter.fontMetrics()
        painter.drawText(
            opt.rect.adjusted(6, 2, -6, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            primary_metrics.elidedText(primary, Qt.TextElideMode.ElideMiddle, width),
        )
        if secondary:
            painter.setFont(secondary_font)
            painter.setPen(QColor(token("text_muted")))
            metrics = painter.fontMetrics()
            painter.drawText(
                opt.rect.adjusted(6, 19, -6, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                metrics.elidedText(secondary, Qt.TextElideMode.ElideMiddle, width),
            )
        painter.restore()

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(120, 38)


class ActionLinkDelegate(QStyledItemDelegate):
    """An accent, underlined action cell that reads as a link, not plain text."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        if opt.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(opt.rect, opt.palette.highlight())
        font = font_px(11, 600)
        font.setUnderline(True)
        painter.setFont(font)
        painter.setPen(QColor(token("accent")))
        painter.drawText(
            opt.rect.adjusted(4, 0, -4, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
        )
        painter.restore()


class BadgeDelegate(QStyledItemDelegate):
    """A coloured kind label in the folder table's Type column.

    The rows used to carry rounded pills, which made two value-coloured
    words the brightest thing on the page and read as controls. The kind is
    a label, so it is plain text: blue for a scan folder, amber for an
    exclusion, muted for a pattern.
    """

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text:
            return
        painter.save()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(opt.rect, opt.palette.highlight())
        if text == "Scan folder":
            color = token("kind_scan")
        elif text == "Excluded":
            color = token("warning")
        elif text == "Pattern":
            color = token("kind_pattern")
        else:
            color = token("text_muted")
        painter.setPen(opt.palette.highlightedText().color() if selected else QColor(color))
        painter.setFont(font_px(10, 600))
        painter.drawText(
            opt.rect.adjusted(6, 0, -4, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(
                text, Qt.TextElideMode.ElideRight, max(1, opt.rect.width() - 10)
            ),
        )
        painter.restore()

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(90, 26)


class PatternPathDelegate(QStyledItemDelegate):
    """A code chip for a name pattern — the chip is the whole cell."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if index.data(Qt.ItemDataRole.UserRole) != "pattern":
            super().paint(painter, option, index)
            return
        painter.save()
        if opt.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(opt.rect, opt.palette.highlight())
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPixelSize(11)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        chip_width = min(opt.rect.width() - 12, metrics.horizontalAdvance(text) + 12)
        chip_height = min(20.0, opt.rect.height() - 6)
        chip = QRectF(
            opt.rect.left() + 6,
            opt.rect.center().y() - chip_height / 2,
            chip_width,
            chip_height,
        )
        painter.setPen(QColor(token("border")))
        painter.setBrush(QColor(token("surface_sunken")))
        painter.drawRoundedRect(chip, 4, 4)
        painter.setPen(QColor(token("accent_text")))
        # A long pattern used to paint at full width and clip mid-token.
        # Elide inside the chip; the item's tooltip keeps the whole string.
        painter.drawText(
            chip.adjusted(6, 0, -6, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(
                text,
                Qt.TextElideMode.ElideMiddle,
                int(max(1, chip.width() - 12)),
            ),
        )
        painter.restore()

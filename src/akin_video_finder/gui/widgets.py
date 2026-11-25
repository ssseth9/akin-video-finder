"""Shared visual primitives for the review GUI.

These are deliberately small: the review's design language is a card surface,
a semantic badge, a page header, a keycap and one empty-state layout.  Keeping
them in one module means the same radius, padding and type scale are used on
every screen instead of being re-invented in each builder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QRect, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from akin_video_finder.gui.icons import icon as _icon
from akin_video_finder.gui.theme import TYPE, badge_style, button_style, font_px, token


class Card(QFrame):
    """A raised surface with the review's border, radius and padding."""

    def __init__(self, parent: QWidget | None = None, padding: int = 16, spacing: int = 10) -> None:
        super().__init__(parent)
        self.setObjectName("uiCard")
        self.setStyleSheet(
            "QFrame#uiCard {"
            f"background:{token('surface')};"
            f"border:1px solid {token('border')};"
            "border-radius:8px;"
            "}"
        )
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(spacing)

    @property
    def body(self) -> QVBoxLayout:
        """The card's content layout."""
        return self._layout


class WrappedLabel(QLabel):
    """A word-wrapped label that keeps the height its text actually needs.

    A wrapped label inside a card's row is measured at one width and placed
    at another: Qt keeps the height from the measurement, and the last line
    is painted clipped even though the card had room to grow. Pinning the
    minimum height to the height the text needs at the width it got lets the
    card grow instead.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        needed = self.heightForWidth(self.width())
        if needed != self.minimumHeight():
            self.setMinimumHeight(needed)


class PageHeader(QWidget):
    """A page title, one-line explanation and an optional primary action."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        action: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.title_label = QLabel(title)
        self.title_label.setFont(font_px(TYPE["page"], 600))
        self.title_label.setStyleSheet(f"color:{token('text')}")
        text.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setFont(font_px(TYPE["body"], 400))
        self.subtitle_label.setStyleSheet(f"color:{token('text_muted')}")
        self.subtitle_label.setWordWrap(True)
        text.addWidget(self.subtitle_label)
        row.addLayout(text, 1)
        self.action_layout = QHBoxLayout()
        self.action_layout.setSpacing(8)
        if action is not None:
            self.action_layout.addWidget(action)
        row.addLayout(self.action_layout, 0)


class Badge(QLabel):
    """A rounded semantic badge; text is always present, never colour-only."""

    def __init__(self, text: str, kind: str = "neutral", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setFont(font_px(TYPE["chip"], 600))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        self.setStyleSheet(badge_style(kind))


class CountChip(Badge):
    """A neutral count chip used beside a title or action."""

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(str(count), "neutral", parent)

    def set_count(self, count: int) -> None:
        self.setText(str(count))


class Keycap(QLabel):
    """A small keyboard-key rendering for shortcut legends."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setFont(font_px(TYPE["chip"], 600))
        # One letter's cap, so a narrow glyph ("I") is as wide as a wide one
        # ("C") and neighbouring keycaps line up.  14 = the 6 px side padding
        # and 1 px border on each side below.
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance("C") + 14)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "QLabel {"
            f"background:{token('surface_raised')};"
            f"color:{token('text_secondary')};"
            f"border:1px solid {token('border')};"
            "border-bottom:2px solid "
            f"{token('border')};"
            "border-radius:4px;padding:1px 6px;"
            "}"
        )


class KeyedButton(QPushButton):
    """A button whose shortcut key sits inside the button face.

    A keycap floating beside the button reads as a separate control; inside
    the face it is part of the action.  The child labels are transparent to
    mouse events so the whole face still clicks the button.
    """

    def __init__(
        self,
        text: str,
        key: str,
        key_side: str = "left",
        role: str = "secondary",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAccessibleName(text)
        self.setMinimumHeight(30)
        # The layout owns the padding; the QSS padding would double it.
        self.setStyleSheet(button_style(role).replace("padding:6px 14px;", "padding:0px;"))
        self.setProperty("role", role)
        if role == "primary":
            text_color = token("accent_fg")
        elif role == "danger_ghost":
            text_color = token("danger")
        elif role == "ghost":
            text_color = token("text_secondary")
        else:
            text_color = token("text")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(6)
        keycap = Keycap(key)
        label = QLabel(text)
        label.setFont(font_px(12, 600))
        label.setStyleSheet(f"color:{text_color}")
        self._label = label
        for child in (keycap, label):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            # A stretched button (the card's Keep) must share its extra
            # width with the spacers, not inflate the keycap into a bar
            child.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row.addStretch(1)
        if key_side == "left":
            row.addWidget(keycap, 0, Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            row.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(keycap, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)

    def set_label_text(self, text: str) -> None:
        """Change the face text.

        The text lives in a child label, so ``QPushButton.setText`` would
        paint a second copy behind the layout.  A checked button that
        carries its own QSS colour cannot inherit one either.
        """
        self._label.setText(text)
        self.setAccessibleName(text)

    def set_label_color(self, color: str) -> None:
        self._label.setStyleSheet(f"color:{color}")

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        layout = self.layout()
        if layout is None:
            return hint
        inner = layout.sizeHint()
        return QSize(max(hint.width(), inner.width() + 2), max(hint.height(), inner.height() + 2))


class EmptyState(QWidget):
    """Icon, title, explanation and one or two actions."""

    def __init__(
        self,
        icon_name: str,
        title: str,
        body: str,
        parent: QWidget | None = None,
        icon_color: str | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 28, 24, 28)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label = QLabel()
        icon_size = 40
        pixmap = _icon(icon_name, icon_color or token("text_muted"), icon_size).pixmap(
            icon_size, icon_size
        )
        self.icon_label.setPixmap(pixmap)
        # A pixmap label is happy to shrink below the pixmap when the page is
        # tight; the paint is then clipped at the bottom (the magnifier's
        # handle and the lens' lower arc disappeared). Pin it to the icon's
        # logical size so the layout has to make room.
        self.icon_label.setFixedSize(pixmap.deviceIndependentSize().toSize())
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title_label = QLabel(title)
        self.title_label.setFont(font_px(TYPE["section"], 600))
        self.title_label.setStyleSheet(f"color:{token('text')}")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)
        self.body_label = QLabel(body)
        self.body_label.setFont(font_px(TYPE["body"], 400))
        self.body_label.setStyleSheet(f"color:{token('text_muted')}")
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label.setWordWrap(True)
        layout.addWidget(self.body_label)
        self.actions_layout = QHBoxLayout()
        self.actions_layout.setSpacing(8)
        self.actions_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(self.actions_layout)

    def add_action(self, text: str, role: str, callback: Callable[[], None]) -> QPushButton:
        """Add an action button and return it."""
        from akin_video_finder.gui.theme import apply_button_role

        button = QPushButton(text)
        apply_button_role(button, role)
        button.clicked.connect(callback)
        self.actions_layout.addWidget(button)
        return button


class BackdropBlur(QWidget):
    """A blurred snapshot of a window, shown behind a modal dialog.

    Qt has no backdrop filter: a modal dialog is its own top-level window, so
    the parent cannot be blurred with a style sheet.  The overlay grabs the
    parent once, blurs the grab, and paints it over the parent's client area;
    the dialog then sits on top of the blurred copy.  The grab is taken at a
    quarter size and scaled back up, which is what keeps a 4K window's first
    paint from stalling on the blur.
    """

    def __init__(
        self,
        window: QWidget,
        radius: int = 24,
        dim: int = 25,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._radius = radius
        self._dim = dim
        self._pixmap: QPixmap | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def refresh(self) -> None:
        """Grab the window without the overlay, blur it, and cover the window."""
        self.hide()
        pixmap = self._window.grab()
        if pixmap.isNull():
            return
        self._pixmap = _blurred(pixmap, self._radius)
        self.setGeometry(self._window.rect())
        self.show()
        self.raise_()

    def paintEvent(self, event: Any) -> None:
        if self._pixmap is None:
            return
        painter = QPainter(self)
        # The widget's rect is in logical units, but the painter can be drawing
        # straight into a device-sized backing store; painting the pixmap into
        # self.rect() then covered only a 1/dpr corner of the window.
        dpr = self.devicePixelRatioF()
        self._paint(painter, QSizeF(self.width() * dpr, self.height() * dpr))

    def _paint(self, painter: QPainter, device: QSizeF) -> None:
        """Paint the snapshot across ``device`` pixels, whatever the transform."""
        pixmap = self._pixmap
        if pixmap is None:
            return
        painter.setWorldTransform(QTransform())
        target = QRectF(0, 0, device.width(), device.height())
        painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        if self._dim:
            painter.fillRect(target, QColor(0, 0, 0, self._dim))


def _blurred(pixmap: QPixmap, radius: int) -> QPixmap:
    """Blur a pixmap by blurring a quarter-size copy and scaling it back."""
    scale = 4
    small = pixmap.scaled(
        max(1, pixmap.width() // scale),
        max(1, pixmap.height() // scale),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    # Pad the small copy by the blur radius so the effect samples the image's
    # edge pixels instead of transparency; without it the backdrop fades out
    # at the window edges and the unblurred page shows through.
    pad = max(1, int(radius / scale) * 3 + 2)
    padded = _pad_edges(small.toImage(), pad)
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(padded))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius / scale)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    scene.setSceneRect(0, 0, padded.width(), padded.height())
    result = QImage(padded.size(), QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    scene.render(
        painter,
        QRectF(result.rect()),
        QRectF(0, 0, padded.width(), padded.height()),
    )
    painter.end()
    cropped = result.copy(pad, pad, small.width(), small.height())
    # The blur fades the alpha at the crop's edge; the snapshot is of an
    # opaque window, so drop the channel rather than let the page show through.
    opaque = cropped.convertToFormat(QImage.Format.Format_RGB32)
    return QPixmap.fromImage(opaque).scaled(
        pixmap.size(),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _pad_edges(image: QImage, pad: int) -> QImage:
    """Grow an image by ``pad`` pixels, repeating its edge pixels outward."""
    width, height = image.width(), image.height()
    padded = QImage(
        width + pad * 2,
        height + pad * 2,
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    padded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(padded)
    painter.drawImage(pad, pad, image)
    painter.drawImage(QRect(pad, 0, width, pad), image, QRect(0, 0, width, 1))
    painter.drawImage(QRect(pad, pad + height, width, pad), image, QRect(0, height - 1, width, 1))
    painter.drawImage(QRect(0, pad, pad, height), image, QRect(0, 0, 1, height))
    painter.drawImage(QRect(pad + width, pad, pad, height), image, QRect(width - 1, 0, 1, height))
    painter.drawImage(QRect(0, 0, pad, pad), image, QRect(0, 0, 1, 1))
    painter.drawImage(QRect(pad + width, 0, pad, pad), image, QRect(width - 1, 0, 1, 1))
    painter.drawImage(QRect(0, pad + height, pad, pad), image, QRect(0, height - 1, 1, 1))
    painter.drawImage(
        QRect(pad + width, pad + height, pad, pad),
        image,
        QRect(width - 1, height - 1, 1, 1),
    )
    painter.end()
    return padded

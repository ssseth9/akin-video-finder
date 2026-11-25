"""Draw the application icon and write it as PNGs and a multi-size .ico.

The mark is original artwork: a video card (the copy you keep) with a second
card ghosted behind it (the copy the scan found), on the app's accent blue.
It is drawn from shapes rather than shipped as a bitmap so any size can be
rendered crisply, and the small sizes are not downscaled mush of the large one.

Run from the repository root:

    .venv/Scripts/python packaging/make_icon.py

Outputs (committed, so a build does not need this script):

    resources/icons/akin-video-finder.ico         16, 24, 32, 48, 64, 128, 256
    resources/icons/akin-video-finder-<n>.png     the same sizes as PNGs
    resources/icons/akin-video-finder-1024.png    the master artwork
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# Drawing needs no window, but Qt wants an application object for QPainter
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPolygonF,
)

CANVAS = 1024
SIZES = (16, 24, 32, 48, 64, 128, 256)
MASTER = 1024
OUT_DIR = Path(__file__).resolve().parents[1] / "resources" / "icons"

# Windows writes these into the resource section; the order in the file
# decides which one Explorer picks when it wants "large" or "small"
ICO_SIZES = tuple(sorted(SIZES, reverse=True))

TILE_TOP = "#2b9af3"
TILE_BOTTOM = "#0b4f8f"
CARD = "#0a3f70"
GHOST = QColor(255, 255, 255, 150)


def _artwork(painter: QPainter) -> None:
    """Paint the mark at CANVAS×CANVAS; callers scale the result."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    inset = 48.0
    tile = QRectF(inset, inset, CANVAS - 2 * inset, CANVAS - 2 * inset)
    gradient = QLinearGradient(QPointF(0, inset), QPointF(0, CANVAS - inset))
    gradient.setColorAt(0.0, QColor(TILE_TOP))
    gradient.setColorAt(1.0, QColor(TILE_BOTTOM))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawRoundedRect(tile, 190, 190)

    # The duplicate: the same card, a step up and to the right, ghosted
    painter.setBrush(GHOST)
    painter.drawRoundedRect(QRectF(372, 140, 512, 420), 64, 64)

    # The copy: solid card carried by the accent colour
    painter.setBrush(QColor(CARD))
    painter.drawRoundedRect(QRectF(132, 400, 700, 466), 74, 74)

    # Play mark — white on the navy card so it survives a 16px title bar
    height = 340.0
    centre = QPointF(482, 633)
    painter.setBrush(QColor("#ffffff"))
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(centre.x() - height * 0.34, centre.y() - height / 2),
                QPointF(centre.x() - height * 0.34, centre.y() + height / 2),
                QPointF(centre.x() + height * 0.56, centre.y()),
            ]
        )
    )


def render(size: int) -> QImage:
    """The mark at one size, drawn large and scaled down smoothly."""
    big = QImage(CANVAS, CANVAS, QImage.Format.Format_ARGB32_Premultiplied)
    big.fill(Qt.GlobalColor.transparent)
    painter = QPainter(big)
    _artwork(painter)
    painter.end()
    if size == CANVAS:
        return big
    return big.scaled(
        size,
        size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _dib(image: QImage) -> bytes:
    """One .ico image: a 32-bit bottom-up DIB with its (empty) AND mask.

    The classic form rather than PNG-in-ICO: every Windows version and every
    consumer of the file (Explorer, the taskbar, Inno Setup) reads it, and it
    is what QIcon expects from a multi-size file too.
    """
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        width,
        height * 2,  # XOR bitmap + AND mask share the height
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression: BI_RGB
        0,  # biSizeImage
        0,
        0,
        0,
        0,
    )
    stride = width * 4
    rows = [bytes(image.constScanLine(y))[:stride] for y in range(height - 1, -1, -1)]
    # 1 bit per pixel, each row padded out to a multiple of four bytes
    mask_stride = ((width + 31) // 32) * 4
    mask = bytes(mask_stride * height)
    return header + b"".join(rows) + mask


def write_ico(images: dict[int, QImage], path: Path) -> None:
    """Write every size into one .ico file."""
    entries = []
    payload = bytearray()
    offset = 6 + 16 * len(images)
    for size in ICO_SIZES:
        data = images[size]
        entries.append(
            struct.pack(
                "<BBBBHHII",
                size % 256,  # 256 is written as 0
                size % 256,
                0,  # colours in palette
                0,  # reserved
                1,  # planes
                32,  # bits per pixel
                len(data),
                offset + len(payload),
            )
        )
        payload += data
    with path.open("wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, len(images)))
        handle.write(b"".join(entries))
        handle.write(payload)


def main() -> int:
    if QGuiApplication.instance() is None:
        QGuiApplication(sys.argv[:1])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    images = {size: _dib(render(size)) for size in ICO_SIZES}
    write_ico(images, OUT_DIR / "akin-video-finder.ico")
    for size in SIZES:
        render(size).save(str(OUT_DIR / f"akin-video-finder-{size}.png"))
    render(MASTER).save(str(OUT_DIR / "akin-video-finder-1024.png"))

    ico = OUT_DIR / "akin-video-finder.ico"
    print(
        f"wrote {ico} ({ico.stat().st_size:,} bytes, sizes: {', '.join(str(s) for s in ICO_SIZES)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

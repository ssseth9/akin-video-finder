"""The app has a face: one original icon, drawn at every size Windows asks.

The window had none — the title bar and taskbar showed the generic Python
launcher. The mark is drawn from shapes (packaging/make_icon.py) and shipped
as a multi-size .ico plus PNGs, so 16px in a title bar is a drawing of the
mark rather than a shrunken 256px bitmap.
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QIcon, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.gui.icons import icon  # noqa: E402
from akin_video_finder.gui.theme import token  # noqa: E402

ICON_DIR = Path(gui_app.__file__).resolve().parents[3] / "resources" / "icons"
SIZES = (16, 24, 32, 48, 64, 128, 256)


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


class TestIcon:
    def test_the_ico_holds_every_size(self, qapp):
        icon = QIcon(str(ICON_DIR / "akin-video-finder.ico"))
        assert not icon.isNull()
        assert set(SIZES) <= {size.width() for size in icon.availableSizes()}

    def test_each_size_is_also_shipped_as_a_png(self, qapp):
        for size in SIZES:
            image = QImage(str(ICON_DIR / f"akin-video-finder-{size}.png"))
            assert not image.isNull(), size
            assert (image.width(), image.height()) == (size, size)

    def test_the_app_hands_out_an_icon(self, qapp):
        assert not gui_app._app_icon().isNull()

    def test_the_icon_is_found_from_a_source_checkout(self):
        # A frozen build resolves it under sys._MEIPASS instead; both point
        # at the same tree, laid out by the spec files
        assert gui_app._resource_path("resources", "icons", "akin-video-finder.ico").is_file()

    def test_a_small_drawn_icon_is_not_clipped(self, qapp):
        # The painter used to scale a 24-unit drawing by 2 as well as the
        # pixmap's device ratio, so a 16 px icon lost its right and bottom
        # edges — the folder became a corner.
        image = icon("folder", token("text_muted"), 16).pixmap(16, 16).toImage()
        for x in range(image.width()):
            assert image.pixelColor(x, image.height() - 1).alpha() == 0
        for y in range(image.height()):
            assert image.pixelColor(image.width() - 1, y).alpha() == 0

    def test_the_icon_is_found_inside_a_frozen_bundle(self, qapp, tmp_path, monkeypatch):
        # PyInstaller unpacks datas under sys._MEIPASS — the same relative
        # tree the specs declare for the data file
        bundle = tmp_path / "_MEI12345"
        (bundle / "resources" / "icons").mkdir(parents=True)
        (bundle / "resources" / "icons" / "akin-video-finder.ico").write_bytes(
            (ICON_DIR / "akin-video-finder.ico").read_bytes()
        )
        monkeypatch.setattr(gui_app.sys, "_MEIPASS", str(bundle), raising=False)
        assert gui_app._resource_path("resources", "icons") == bundle / "resources" / "icons"
        assert not gui_app._app_icon().isNull()

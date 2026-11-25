"""Two pair cards must fit the window they are shown in.

A QLabel reports its pixmap as its minimum size. The contact sheet is 1280
wide, so once a preview arrived the image label demanded 1280, the card was
pinned at its 1,300 px maximum, and a side-by-side pair needed ~2,600 px —
more than the maximized dialog on a 1,920 px screen. The second card was
laid out past the right edge (its badge, sheet, path and buttons simply not
on screen) and the centred header, centred over a row wider than the window,
was clipped at the edge too. Cards looked like half a dialog.
"""

import os
import time
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QRect  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402
from akin_video_finder.reporting.sheets import sheet_name  # noqa: E402

# A maximized window on a 1,920 px screen, at 100% scaling
WINDOW_SIZE = (1920, 1000)


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.01)


@pytest.fixture()
def window(tmp_path, qapp, monkeypatch):
    """A MainWindow over one pair, both sheets already rendered on disk."""
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)

    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    ids = []
    for i in range(2):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x" * (100 + i))
        ids.append(
            repo.create_file(
                root_id,
                str(video),
                str(video).lower(),
                video.name,
                ".mp4",
                100 + i,
                1000 + i,
                scan_id,
            )
        )
    repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()

    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    # The sheets the scan would have rendered — full native size, as real ones are
    for i, row in enumerate(sorted(repo.get_files_for_root(root_id), key=lambda r: r["id"])):
        pixmap = QPixmap(1280, 540)
        pixmap.fill(QColor(10 + i, 40, 40))
        assert pixmap.save(str(win.sheet_loader.sheets_dir / sheet_name(row)), "JPEG")

    yield win
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()
    repo.close()


def _open_dialog(window, qapp):
    dialog = gui_app.CompareDialog(window)
    dialog.show()
    # Undo the maximize: offscreen screens are 800x800, and the case that
    # broke is an ordinary wide window, not a maximized one
    dialog.setWindowState(gui_app.Qt.WindowState.WindowNoState)
    dialog.resize(*WINDOW_SIZE)
    _pump(qapp, 0.4)
    return dialog


def test_a_sheet_does_not_set_the_cards_minimum_width(window, qapp):
    """The sheets are 1280 wide; the pair must still fit a 1920 px window."""
    dialog = _open_dialog(window, qapp)
    try:
        for card in dialog._dialog_cards:
            assert not card.image_label.pixmap().isNull()  # the sheet is on it
        needed = sum(card.minimumSizeHint().width() for card in dialog._dialog_cards)
        assert needed < WINDOW_SIZE[0], f"a pair wants {needed} px"
    finally:
        dialog.close()
        dialog.deleteLater()


def test_both_cards_sit_inside_the_window(window, qapp):
    dialog = _open_dialog(window, qapp)
    try:
        left, right = (holder.geometry() for holder in dialog._holders)
        assert right.left() >= left.right()  # side by side, not overlapping
        assert right.right() <= dialog.width()  # ... and card 2 on screen
        assert left.width() > 0 and right.width() > 0
    finally:
        dialog.close()
        dialog.deleteLater()


def _fit_to(window, qapp, monkeypatch, available):
    monkeypatch.setattr(gui_app.CompareDialog, "_screen_available", lambda self: available)
    dialog = gui_app.CompareDialog(window)
    dialog.show()
    _pump(qapp, 0.2)  # give the shown layout the cards' final widths
    return dialog


def test_window_uses_the_whole_screen_width_when_it_cannot_fit_two_native_sheets(
    window, qapp, monkeypatch
):
    """A 1920 px monitor cannot fit 2 x 1280, so the window takes all of it."""
    available = QRect(0, 0, 1920, 1040)
    dialog = _fit_to(window, qapp, monkeypatch, available)
    try:
        assert dialog.width() == available.width()
        assert dialog.width() < gui_app._COMPARE_PAIR_WIDTH
        # The old 1500 px cap gave each card ~740 px; a fitted window gives
        # both sheets the widest split the monitor allows.
        assert all(card.width() > 900 for card in dialog._dialog_cards)
    finally:
        dialog.close()
        dialog.deleteLater()


def test_window_caps_at_pair_width_on_an_ultrawide(window, qapp, monkeypatch):
    """Wider than two sheets is blank space; the pair width is the ceiling."""
    dialog = _fit_to(window, qapp, monkeypatch, QRect(0, 0, 3440, 1400))
    try:
        assert dialog.width() == gui_app._COMPARE_PAIR_WIDTH
        assert all(
            card.width() == gui_app.CARD_MAX_WIDTH for card in dialog._dialog_cards
        )
    finally:
        dialog.close()
        dialog.deleteLater()


def test_window_fits_its_content_and_is_centred(window, qapp, monkeypatch):
    available = QRect(0, 0, 1920, 1040)
    dialog = _fit_to(window, qapp, monkeypatch, available)
    try:
        margins = dialog.layout().contentsMargins()
        needed = (
            dialog._content.sizeHint().height() + margins.top() + margins.bottom()
        )
        assert dialog.height() >= needed  # nothing cropped vertically
        assert dialog.height() <= available.height()  # never larger than the screen
        expected_y = available.y() + (available.height() - dialog.height()) // 2
        assert dialog.y() == expected_y
    finally:
        dialog.close()
        dialog.deleteLater()

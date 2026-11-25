"""Changing the Compare pair must not flash or stall the window.

Reported as "when I press ignore, loading the next pair is a bit weird —
I can't say exactly because it happened too fast". Two things happened on
every swap, and both were only visible for a frame or two:

* ``_prefetch`` re-requested the next six pairs' sheets even though they
  were already cached, so the loader emitted ``sheet_ready`` for each,
  and every emit re-decoded the image on the UI thread for cards that
  already showed it. Measured 74 ms of stall per swap with a 12-file
  prefetch window.
* The pair's cards were rebuilt from scratch, so their sheet height was
  unknown again: the first paint drew the placeholder at the label's
  120 px minimum instead of the sheet's height, and the two sides sat at
  different heights until the 50 ms equalize debounce fired.
"""

import os
import time
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402
from akin_video_finder.reporting.sheets import sheet_name  # noqa: E402

# Enough pairs that a swap's prefetch window is full (6 pairs = 12 files)
PAIR_COUNT = 8


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(app, seconds: float) -> None:
    """Run the event loop, including the DeferredDelete step that the
    real loop performs — processEvents() alone leaves deleted cards alive
    and makes the paint sequence differ from the app."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.004)


@pytest.fixture()
def window(tmp_path, qapp, monkeypatch):
    """A MainWindow over eight pairs with every sheet already on disk."""
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)

    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    for i in range(PAIR_COUNT * 2):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x" * (100 + i))
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
    repo.commit()
    rows = sorted(repo.get_files_for_root(root_id), key=lambda r: r["id"])
    for p in range(PAIR_COUNT):
        repo.create_pair_comparison(
            scan_id,
            rows[2 * p]["id"],
            rows[2 * p + 1]["id"],
            f"v{p}",
            "VERY_LIKELY",
            1.0 - p / 100,
            0.01,
            12,
        )
    repo.commit()

    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    # State the app is in before Compare opens: the scan rendered every
    # sheet, so the prefetcher has nothing left to do
    for i, row in enumerate(rows):
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


def test_pair_swap_paints_the_cached_sheet_immediately(window, qapp, monkeypatch):
    """The first paint of a new pair shows the sheet, at its final size."""
    paints: dict[int, list[tuple[bool, int]]] = {}  # per card instance
    original = gui_app.FileCard.paintEvent

    def paint(self, event):  # type: ignore[no-untyped-def]
        paints.setdefault(id(self), []).append(
            (not self.image_label.pixmap().isNull(), self.image_label.height())
        )
        original(self, event)

    monkeypatch.setattr(gui_app.FileCard, "paintEvent", paint)

    dialog = gui_app.CompareDialog(window)
    try:
        dialog.show()
        _pump(qapp, 0.6)  # the first pair settles
        dialog._ignore()
        _pump(qapp, 0.4)

        cards = dialog._dialog_cards
        assert len(cards) == 2
        settled = {id(card): card.image_label.height() for card in cards}
        assert len(set(settled.values())) == 1  # both sides still match
        assert settled[id(cards[0])] > 120  # taller than the placeholder
        for card in cards:
            first_paint = paints[id(card)][0]
            assert first_paint == (True, settled[id(card)]), (
                "first paint of the swapped-in card drew a placeholder or "
                f"used the wrong height: {first_paint}"
            )
    finally:
        dialog.close()
        dialog.deleteLater()


def test_pair_swap_does_no_sheet_work_for_cached_pairs(window, qapp, monkeypatch):
    """Ignoring a pair must not re-request or re-decode rendered sheets."""
    requests: list[tuple[int, int]] = []
    decodes: list[bool] = []  # True when the card already showed that path
    original_request = gui_app.SheetLoader.request
    original_set_sheet = gui_app.FileCard.set_sheet

    def request(self, file_row, duration_s, *, priority=0):  # type: ignore[no-untyped-def]
        requests.append((file_row["id"], priority))
        return original_request(self, file_row, duration_s, priority=priority)

    def set_sheet(self, path):  # type: ignore[no-untyped-def]
        decodes.append(path == self.sheet_path)
        return original_set_sheet(self, path)

    monkeypatch.setattr(gui_app.SheetLoader, "request", request)
    monkeypatch.setattr(gui_app.FileCard, "set_sheet", set_sheet)

    dialog = gui_app.CompareDialog(window)
    try:
        dialog.show()
        _pump(qapp, 0.6)
        requests.clear()
        decodes.clear()

        dialog._ignore()
        _pump(qapp, 0.4)

        # Prefetching pairs whose sheets are on disk is pure overhead
        assert requests == []
        # Exactly one decode per newly shown card, and none repeated
        assert decodes == [False, False]
    finally:
        dialog.close()
        dialog.deleteLater()

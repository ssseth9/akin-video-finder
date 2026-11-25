"""Both sides of the Compare pairs window must be the same height.

The keeper card carries an extra "Best copy" row, and the height match
used to run once at construction — before the cards had their real width
and therefore their real sheet-image height. The result was one sheet
sitting a whole row higher than the other.
"""

import os
import time
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


@pytest.fixture()
def window(tmp_path, qapp, monkeypatch):
    """A MainWindow over one reviewable pair, with sheets stubbed out."""
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)

    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    ids = []
    for i, size in enumerate((1, 999)):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x" * size)
        ids.append(
            repo.create_file(
                root_id,
                str(video),
                str(video).lower(),
                video.name,
                ".mp4",
                size,
                1000 + i,
                scan_id,
            )
        )
    repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()

    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    yield win
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()
    repo.close()


def test_compare_cards_share_one_height(window, qapp):
    dialog = gui_app.CompareDialog(window)
    try:
        dialog.show()
        _pump(qapp, 0.6)  # let layout settle and the debounce fire

        cards = dialog._dialog_cards
        assert len(cards) == 2
        # One side is the keeper, so it really does own the extra row
        assert sorted(c.is_keeper for c in cards) == [False, True]
        assert cards[0].height() == cards[1].height()
        assert cards[0].height() > 0
    finally:
        dialog.close()
        dialog.deleteLater()

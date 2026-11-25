"""The bar names the root it is walking by its folder, not by its path.

Discovery's message was the one path that reached the progress bar. The bar
elides, so ``P:\\\\_portable\\\\_AI\\\\vids`` arrived with the folders — the part
that tells two roots apart — removed and the volume kept, on a scan that
already knows which roots it is running. The scan log still records the whole
line: it is the record, and its rows carry paths anyway.
"""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.core.models import ProgressEvent  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402

ROOT = "P:/_portable/_AI/vids"


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _config(tmp_path):
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    return replace(
        cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports"))
    )


@pytest.fixture
def window(tmp_path, qapp):
    created = []

    def _make():
        repo = DatabaseRepository(tmp_path / "v.db")
        win = gui_app.MainWindow(_config(tmp_path), repo, tmp_path / "config.toml")
        win.reload_data()
        created.append((win, repo))
        return win

    yield _make

    for win, repo in created:
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
        repo.close()


def _discovery(root=ROOT):
    """The event the scan service emits before it walks a root."""
    return ProgressEvent(
        phase="discovery",
        completed_count=0,
        total_count=0,
        current_root=root,
        message=f"Scanning root: {root}",
    )


def _records(win) -> list[str]:
    return [e.text for e in win._scan_log_records]


class TestDiscoveryRoot:
    def test_the_bar_names_the_folder_not_the_path(self, window):
        win = window()
        win._on_scan_progress(_discovery())
        shown = win.progress_bar.format()
        assert "Scanning root: vids" in shown
        # The folders are what the elision takes; the volume is what it keeps
        assert "P:" not in shown
        assert "_portable" not in shown

    def test_the_log_keeps_the_whole_path(self, window):
        # The bar is a reading; the log is the record. Losing the path there
        # would lose which of two same-named roots the scan walked
        win = window()
        win._on_scan_progress(_discovery())
        assert any(f"Scanning root: {ROOT}" in text for text in _records(win))

    def test_a_counted_phase_is_untouched(self, window):
        win = window()
        win._on_scan_progress(
            ProgressEvent(
                phase="probing",
                completed_count=3,
                total_count=10,
                current_root=ROOT,
            )
        )
        assert win.progress_bar.format().startswith("Reading details   3/10")

    def test_a_root_at_a_drive_has_no_folder_to_fold(self, window):
        win = window()
        win._on_scan_progress(_discovery("P:/"))
        # Nothing between the drive and the scan, so the line keeps what it has
        assert "Scanning root: P:/" in win.progress_bar.format()


class TestBarMessage:
    def test_a_message_without_the_root_is_returned_whole(self):
        text = "Processed 6,449 files (0 unchanged)"
        assert gui_app._bar_message(text, ROOT) == text

    def test_the_root_is_folded_to_its_last_folder(self):
        assert gui_app._bar_message(f"Scanning root: {ROOT}", ROOT) == "Scanning root: vids"

    def test_a_root_without_parents_is_left_alone(self):
        # Path("vids").name is "vids": a rewrite would print the same text
        assert gui_app._bar_message("Scanning root: vids", "vids") == "Scanning root: vids"

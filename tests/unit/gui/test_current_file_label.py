"""The bar's second line names what the scan is working on.

It shows the file name, not the path. On that line a path elides to
"…\\clip.mp4" — the folders are what gets cut, and the name is what a reader is
looking for — so the whole path lives in the tooltip and in the scan log's
per-file rows instead.
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


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _config(tmp_path):
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    return replace(
        cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports"))
    )


@pytest.fixture()
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


def _event(phase, done=0, total=0, path=None, peer=None):
    return ProgressEvent(
        phase=phase,
        completed_count=done,
        total_count=total or None,
        current_path=path,
        peer_path=peer,
        message=f"{phase} {done}/{total}" if total else "",
    )


class _Signal:
    def connect(self, _slot):
        pass


class _FakeWorker:
    """Stands in for the scan thread: _start_scan only wires one and starts it."""

    def __init__(self, *_args, **_kwargs):
        self.progress = _Signal()
        self.finished_scan = _Signal()

    def start(self) -> None:
        pass

    def isRunning(self) -> bool:  # noqa: N802 - Qt's spelling
        return False


class TestCurrentFile:
    def test_a_pair_names_both_files(self, window):
        win = window()
        win._on_scan_progress(_event("offset", 5, 400, "P:/_AI/vids/a.mp4", "Q:/clips/b.mp4"))
        # Both halves, because a comparison is about two files
        assert win.current_file_label.full_text() == "a.mp4  vs  b.mp4"

    def test_the_path_is_the_tooltip(self, window):
        win = window()
        win._on_scan_progress(_event("probing", 5, 400, "P:/_AI/vids/a.mp4"))
        assert win.current_file_label.full_text() == "a.mp4"
        assert "P:/_AI/vids/a.mp4" in win.current_file_label.toolTip()
        assert "P:/" not in win.current_file_label.full_text()

    def test_a_phase_that_names_no_files_clears_the_line(self, window):
        win = window()
        win._on_scan_progress(_event("comparing", 1, 10, "P:/v/a.mp4", "P:/v/b.mp4"))
        assert win.current_file_label.full_text() == "a.mp4  vs  b.mp4"
        # Blocking counts pairs it never names — the last name would be a lie
        win._on_scan_progress(_event("blocking", 1, 10))
        assert win.current_file_label.full_text() == ""
        assert win.current_file_label.toolTip() == ""

    def test_a_fast_phase_repaints_a_few_times_a_second_at_most(self, window, monkeypatch):
        win = window()
        clock = [1_000.0]
        monkeypatch.setattr(gui_app.time, "monotonic", lambda: clock[0])
        win._on_scan_progress(_event("fingerprinting", 1, 100, "P:/v/a.mp4"))
        assert win.current_file_label.full_text() == "a.mp4"
        # Fingerprinting emits an event per file — 200 a second is a blur
        clock[0] += 0.05
        win._on_scan_progress(_event("fingerprinting", 2, 100, "P:/v/b.mp4"))
        assert win.current_file_label.full_text() == "a.mp4"
        clock[0] += 0.2
        win._on_scan_progress(_event("fingerprinting", 3, 100, "P:/v/c.mp4"))
        assert win.current_file_label.full_text() == "c.mp4"

    def test_the_line_appears_when_a_scan_starts(self, window, monkeypatch):
        monkeypatch.setattr(gui_app, "ScanWorker", _FakeWorker)
        win = window()
        assert win.current_file_label.isHidden()
        win._start_scan()
        assert not win.current_file_label.isHidden()

    def test_the_line_disappears_with_the_bar(self, window):
        win = window()
        win.current_file_label.setVisible(True)
        win._on_scan_progress(_event("probing", 5, 400, "P:/v/a.mp4"))
        assert win.current_file_label.full_text() == "a.mp4"
        win._set_progress_idle()
        assert win.current_file_label.isHidden()
        assert win.current_file_label.full_text() == ""

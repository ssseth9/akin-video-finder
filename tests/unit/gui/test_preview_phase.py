"""Previews are produced by the scan, never by the review screen.

Regression: ``_on_scan_finished`` read ``len(sheet_loader.pending)``
immediately after ``reload_data()``, but that only scheduled a deferred
warm-up — so the count was always 0, the gate never engaged, and the UI
jumped to Result with every preview still unrendered. That deferred
warm-up also ran on every startup and reload, re-rendering the sheets of
the previous scan before the user had asked for anything. These tests pin
the sequencing: request synchronously on a completed scan, hold the scan
tab until the queue drains, and do no preview work on a plain load.
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


@pytest.fixture()
def window(tmp_path, qapp, monkeypatch):
    """A MainWindow over one VERY_LIKELY pair, with FFmpeg stubbed out."""
    calls: list[str] = []

    def fake_sheet(*args, **kwargs):
        # Slow enough that a worker cannot finish and pop its pending entry
        # before _on_scan_finished measures the queue — an instant stub
        # races the count and makes the gate assertion flaky
        time.sleep(0.2)
        calls.append(str(args[0]))
        return True

    monkeypatch.setattr(gui_app, "generate_contact_sheet", fake_sheet)

    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    ids = []
    for i in range(2):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x")
        ids.append(
            repo.create_file(
                root_id, str(video), str(video).lower(), video.name, ".mp4", 1, 1000 + i, scan_id
            )
        )
    repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()

    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    win.reload_data()
    yield win, repo, calls

    # Timers outlive the widget unless stopped explicitly — a 15 s watchdog
    # firing after teardown would signal into a deleted window
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()
    repo.close()


def _drain(win, timeout: float = 20.0) -> None:
    """Process events until the preview queue reports empty."""
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and win._preview_total:
        app.processEvents()
        time.sleep(0.02)


class TestPreviewPhase:
    def test_scan_finished_requests_previews_synchronously(self, window):
        win, _repo, _calls = window
        win._on_scan_finished({"status": "completed"})
        # Both files of the pair are queued before the gate is measured
        assert win._preview_total == 2
        assert win._rendering_previews is True

    def test_startup_reload_queues_no_previews(self, window):
        """A fresh window over an old scan must not start rendering."""
        win, _repo, calls = window
        assert win.sheet_loader.pending == {}
        assert getattr(win, "_preview_total", 0) == 0
        _drain(win, timeout=1.0)
        assert calls == []

    def test_holds_scan_tab_until_previews_drain(self, window):
        win, _repo, calls = window
        win._on_scan_finished({"status": "completed"})
        assert win.progress_bar.format() == "rendering previews %v/%m"
        assert win.progress_phase_label.text() == "Rendering previews"
        assert "2 contact sheets" in win.progress_meta_label.text()
        assert win.progress_percent_label.text() == "0%"
        assert win.tabs.currentWidget() is not win._result_container

        _drain(win)

        assert win._preview_total == 0
        assert win._rendering_previews is False
        assert win.tabs.currentWidget() is win._result_container
        assert win.progress_bar.format() == "%p%"
        assert len(calls) == 2

    def test_cancelled_scan_skips_previews(self, window):
        win, _repo, calls = window
        win._on_scan_finished({"status": "interrupted"})
        # No trustworthy pair list — results open immediately, no renders
        assert win._preview_total == 0
        assert win._rendering_previews is False
        assert win.tabs.currentWidget() is win._result_container
        _drain(win, timeout=1.0)
        assert calls == []

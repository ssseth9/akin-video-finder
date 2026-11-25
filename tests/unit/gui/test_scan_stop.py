"""Stop reads as a state, not as a click that may or may not have landed.

The cancel token is tested between files, batches and phases, so a 4K
fingerprint or a shift probe finishes first. Until this said so, the bar kept
counting toward a phase about to be abandoned, the button still read "Stop",
and a second click added a second line to the log — three ways of looking like
the click had been ignored.
"""

import os
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


class _FakeWorker:
    """Just enough of a ScanWorker to answer a Stop click."""

    def __init__(self) -> None:
        self.cancels = 0

    def isRunning(self) -> bool:  # noqa: N802 - QThread's spelling
        return True

    def cancel(self) -> None:
        self.cancels += 1


def test_cancel_before_the_service_exists_is_replayed(tmp_path, qapp, monkeypatch):
    """A Stop clicked before run() reaches the service must still land."""
    created: list[object] = []

    class FakeRepo:
        def __init__(self, path):
            pass

        def commit(self):
            pass

        def close(self):
            pass

    class FakeService:
        def __init__(self, *args, **kwargs):
            self.cancels = 0
            created.append(self)

        def cancel(self):
            self.cancels += 1

        def scan(self):
            return {"status": "interrupted"}

    monkeypatch.setattr(gui_app, "DatabaseRepository", FakeRepo)
    monkeypatch.setattr(gui_app, "ScanService", FakeService)

    worker = gui_app.ScanWorker(_config(tmp_path), tmp_path / "v.db")
    worker.cancel()
    worker.run()

    assert len(created) == 1
    assert created[0].cancels == 1


def _records(win):
    return list(win._scan_log_records)


class TestTheStopIsAskedForOnce:
    def test_one_click_asks_the_engine_once(self, window):
        win = window()
        worker = _FakeWorker()
        win.scan_worker = worker
        win._cancel_scan()
        assert worker.cancels == 1

    def test_a_second_click_does_not_ask_again(self, window):
        # The button is inert after the first click; the guard is what makes
        # that true even for a click already queued when the state changed
        win = window()
        worker = _FakeWorker()
        win.scan_worker = worker
        win._cancel_scan()
        win._cancel_scan()
        assert worker.cancels == 1

    def test_the_log_gains_one_row_not_a_stack_of_them(self, window):
        win = window()
        win.scan_worker = _FakeWorker()
        win._cancel_scan()
        win._cancel_scan()
        rows = [r for r in _records(win) if "Stop requested" in r.text]
        assert len(rows) == 1


class TestTheStopSaysWhereItLands:
    def test_the_row_names_the_boundary(self, window):
        win = window()
        win.scan_worker = _FakeWorker()
        win._cancel_scan()
        row = next(r for r in _records(win) if "Stop requested" in r.text)
        assert "file in progress finishes first" in row.text

    def test_the_row_belongs_to_the_scan_not_to_a_phase(self, window):
        # A pill saying COMPARE would tie the stop to whichever phase happened
        # to be running; a stop is a scan-level action
        win = window()
        win.scan_worker = _FakeWorker()
        win._cancel_scan()
        row = next(r for r in _records(win) if "Stop requested" in r.text)
        assert row.stage == "scan"
        assert row.level == "info"

    def test_the_button_says_stopping_and_takes_no_more_clicks(self, window):
        win = window()
        win.scan_worker = _FakeWorker()
        win._cancel_scan()
        assert "Stopping" in win.scan_button.text()
        assert not win.scan_button.isEnabled()


class TestTheBarStopsPredicting:
    def test_a_rate_and_an_estimate_give_way_to_the_state(self, window):
        win = window()
        win.scan_worker = _FakeWorker()
        win._cancel_scan()
        win._bar_counts = ("probing", 120, 400)
        win._refresh_bar_text()
        shown = win.progress_bar.format()
        assert "120/400" in shown
        assert "stopping" in shown
        # Both of these promise work the stop just withdrew
        assert "left" not in shown
        assert "/s" not in shown and "/min" not in shown

    def test_the_ease_toward_the_peak_still_runs(self, window):
        # Only the open-ended creep stops: a bar owed real progress should
        # still show it after a stop, because those units did finish
        win = window()
        win._stopping = True
        win._progress_shown = 10.0
        win._progress_peak = 40.0
        win._tick_progress()
        assert win._progress_shown > 10.0

    def test_the_creep_stops_with_the_scan(self, window):
        win = window()
        win._stopping = True
        win._progress_shown = 40.0
        win._progress_peak = 40.0
        win.progress_bar.setMaximum(1000)
        win._tick_progress()
        assert win._progress_shown == 40.0
        assert win.progress_bar.value() == 400


class TestTheStopIsOver:
    def test_an_idle_window_hands_the_button_back(self, window):
        win = window()
        win.scan_worker = _FakeWorker()
        win._cancel_scan()
        win._set_progress_idle()
        assert win._stopping is False
        assert win.scan_button.isEnabled()

    def test_a_finished_scan_hands_the_button_back(self, window):
        # Previews keep the button repurposed as "Stop", so the restore has to
        # happen where the scan ends rather than only on the idle path
        win = window()
        win.scan_worker = _FakeWorker()
        win._cancel_scan()
        win._on_scan_finished({"status": "interrupted"})
        assert win._stopping is False
        assert win.scan_button.isEnabled()

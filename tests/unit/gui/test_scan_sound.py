"""The end of a completed scan is heard, not only seen.

The window is usually left in the background while a scan runs, so the run
ends with a short original chime. It waits for the preview queue when the scan
rendered sheets; a cancelled run and a headless window stay silent.
"""

import os
import wave
from dataclasses import replace
from pathlib import Path

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
def window(tmp_path, qapp):
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))
    repo = DatabaseRepository(tmp_path / "v.db")
    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    win.reload_data()
    yield win
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()
    repo.close()


@pytest.fixture()
def plays(monkeypatch):
    played: list[Path] = []
    monkeypatch.setattr(gui_app, "play_scan_complete", played.append)
    return played


def test_a_finished_scan_chimes_with_its_own_sound(window, plays):
    window.show()
    window._on_scan_finished({"status": "completed"})

    assert len(plays) == 1
    assert plays[0].name == "scan-complete.wav"
    assert plays[0].exists()


def test_the_chime_waits_for_the_previews(window, plays):
    window.show()
    window._rendering_previews = True
    window._preview_total = 2
    window._chime_when_previews_done = True

    window._on_pending_changed(1)
    assert plays == []

    window._on_pending_changed(0)
    assert len(plays) == 1


def test_a_stopped_scan_stays_silent(window, plays):
    window.show()
    window._on_scan_finished({"status": "interrupted"})

    assert plays == []


def test_a_hidden_window_stays_silent(window, plays):
    window._on_scan_finished({"status": "completed"})

    assert plays == []


def test_the_chime_is_a_short_loadable_wav():
    path = gui_app._resource_path("resources", "sounds", "scan-complete.wav")
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 44_100
        assert 1.0 <= wav.getnframes() / wav.getframerate() <= 2.5

"""reload_data() must only load what the review screen actually reads.

Probe metadata used to be fetched for every file in the inventory — one
query per file on every startup, reload, un-dismiss and settings save —
but it is only ever read for files that appear in a pair. On a large
library that was thousands of wasted queries before the window appeared.
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


def test_probe_metadata_is_loaded_only_for_pair_files(tmp_path, qapp):
    """A library of six files with one pair loads two probe records."""
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))

    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    ids = []
    for i in range(6):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x")
        ids.append(
            repo.create_file(
                root_id, str(video), str(video).lower(), video.name, ".mp4", 1, 1000 + i, scan_id
            )
        )
    # Only v0 and v1 are a pair — the other four are unrelated inventory
    repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()

    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    try:
        assert set(win.probes) == {ids[0], ids[1]}
        # The full inventory is still available for pair filtering
        assert set(win.files) == set(ids)
    finally:
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
        repo.close()

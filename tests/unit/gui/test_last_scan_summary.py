"""The Scan tab must say what the last scan did — and whether it still holds.

``scan_runs`` has always carried the start time, the file counts and the
config fingerprint, and ``get_comparison_counts`` the pair counts, but the
GUI read none of it: the home tab could not answer "when did I last scan,
what did it find, are these results still valid for my settings?".
"""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.application.scan_service import (  # noqa: E402
    config_fingerprint,
    legacy_config_fingerprint,
)
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.core.models import ScanStatus  # noqa: E402
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

    def _make(with_pair=False, fingerprint="match", status=ScanStatus.COMPLETED):
        cfg = _config(tmp_path)
        repo = DatabaseRepository(tmp_path / "v.db")
        scan_id = repo.create_scan(
            "test",
            config_fingerprint={
                "match": config_fingerprint(cfg),
                "stale": config_fingerprint(
                    replace(cfg, matching=replace(cfg.matching, possible_threshold=0.1))
                ),
                "legacy": legacy_config_fingerprint(cfg),
                "none": None,
            }[fingerprint],
        )
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        ids = []
        for i in range(2):
            video = tmp_path / f"v{i}.mp4"
            video.write_bytes(b"x")
            ids.append(
                repo.create_file(
                    root_id,
                    str(video),
                    str(video).lower(),
                    video.name,
                    ".mp4",
                    1,
                    1000 + i,
                    scan_id,
                )
            )
        if with_pair:
            repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
        repo.update_scan_status(scan_id, status, total_files=2, present_files=2)
        repo.commit()
        win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
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


class TestLastScanSummary:
    def test_says_when_there_is_no_scan(self, tmp_path, qapp):
        cfg = _config(tmp_path)
        repo = DatabaseRepository(tmp_path / "empty.db")
        win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
        assert "No scan yet" in win._last_scan_label.text()
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        repo.close()

    def test_reports_age_files_and_pairs(self, window):
        win = window(with_pair=True)
        text = win._last_scan_label.text()
        assert text.startswith("Last scan just now")
        assert "2 files" in text
        assert "1 pair" in text

    def test_names_an_incomplete_scan(self, window):
        win = window(status=ScanStatus.INTERRUPTED)
        assert "(interrupted)" in win._last_scan_label.text()

    def test_matching_settings_raise_no_warning(self, window):
        win = window(fingerprint="match")
        assert "settings changed" not in win._last_scan_label.text()
        assert win._last_scan_notice.isHidden()
        assert win._last_scan_dot.isHidden()

    def test_changed_settings_are_flagged(self, window):
        win = window(fingerprint="stale")
        assert not win._last_scan_notice.isHidden()
        assert win._last_scan_notice.text() == "Settings changed since this scan"
        assert not win._last_scan_dot.isHidden()
        assert "folders" in win._last_scan_notice.toolTip()

    def test_a_message_temporarily_takes_the_freshness_note(self, window):
        win = window(fingerprint="stale")
        assert not win._last_scan_notice.isHidden()

        win._show_status_message("Settings saved.", 1000)
        assert win.status_message_label.text() == "Settings saved."
        assert win._last_scan_notice.isHidden()
        assert win._last_scan_dot.isHidden()

        win._status_timer.stop()
        win._reset_status_message()
        assert not win._last_scan_notice.isHidden()
        assert not win._last_scan_dot.isHidden()

    def test_legacy_scan_rows_are_not_flagged_after_upgrade(self, window):
        win = window(fingerprint="legacy")
        assert "settings changed" not in win._last_scan_label.text()

    def test_scan_without_a_fingerprint_is_not_flagged(self, window):
        win = window(fingerprint="none")
        assert "settings changed" not in win._last_scan_label.text()


class TestResultTabTitle:
    def test_carries_the_pair_count(self, window):
        win = window(with_pair=True)
        assert win.tabs.tabText(win.tabs.indexOf(win._result_container)) == "Results (1)"

    def test_stays_plain_without_pairs(self, window):
        win = window()
        assert win.tabs.tabText(win.tabs.indexOf(win._result_container)) == "Results"

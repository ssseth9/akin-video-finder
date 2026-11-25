"""The Settings cleanup must agree with itself before it deletes anything.

The dialog is the only place a user sees what the check found, so the counts
it shows and the rows it removes have to come from the same plan — and a
"no" has to leave the database untouched.
"""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

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
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    kept = tmp_path / "kept.mp4"
    kept.write_bytes(b"x")
    gone = tmp_path / "gone.mp4"
    gone.write_bytes(b"y")
    for path in (kept, gone):
        repo.create_file(
            root_id,
            str(path),
            str(path).lower(),
            path.name,
            path.suffix,
            1,
            1000,
            scan_id,
        )
    repo.commit()
    gone.unlink()

    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    win.reload_data()
    yield win, repo, kept
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()
    repo.close()


class TestCleanupRecords:
    def test_confirmed_dialog_removes_only_the_gone_row(
        self, window, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win, repo, kept = window
        asked: dict[str, str] = {}

        def answer(parent, title, text, *args, **kwargs):
            asked["text"] = text
            return QMessageBox.StandardButton.Yes

        monkeypatch.setattr(QMessageBox, "question", answer)

        win._cleanup_records()

        assert {row["original_path"] for row in repo.get_inventory_files()} == {str(kept)}
        assert "Rows to remove: 1" in asked["text"]
        assert "Still on disk: 1" in asked["text"]

    def test_declined_dialog_keeps_every_row(self, window, monkeypatch: pytest.MonkeyPatch) -> None:
        win, repo, _ = window
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.No,
        )

        win._cleanup_records()

        assert len(repo.get_inventory_files()) == 2

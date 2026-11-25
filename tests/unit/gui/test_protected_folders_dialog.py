"""Protected folders in the Tools page and the Actions safety path."""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.loader import load_config  # noqa: E402
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


def test_dialog_adds_and_removes_folders(window, tmp_path, monkeypatch) -> None:
    folder = tmp_path / "Masters"
    folder.mkdir()
    monkeypatch.setattr(
        gui_app.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *args, **kwargs: str(folder)),
    )

    dialog = gui_app.ProtectedFoldersDialog(window)
    dialog._add_folder()
    dialog._add_folder()

    assert dialog.paths == [str(folder)]
    assert dialog.list_widget.count() == 1

    dialog.list_widget.setCurrentRow(0)
    dialog._remove_selected()

    assert dialog.paths == []
    assert dialog.list_widget.count() == 0


def test_save_writes_protected_paths(window, tmp_path, monkeypatch) -> None:
    folder = tmp_path / "Masters"
    folder.mkdir()

    class AcceptedDialog:
        def __init__(self, parent):
            self.paths = [str(folder)]

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui_app, "ProtectedFoldersDialog", AcceptedDialog)
    window._edit_protected_folders()

    assert window.config.actions.protected_paths == [str(folder)]
    reloaded = load_config(window.config_path)
    assert reloaded.actions.protected_paths == [str(folder)]


def test_cancel_leaves_protected_paths_unchanged(window, monkeypatch) -> None:
    class RejectedDialog:
        def __init__(self, parent):
            self.paths = ["C:\\Masters"]

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui_app, "ProtectedFoldersDialog", RejectedDialog)
    before = list(window.config.actions.protected_paths)
    window._edit_protected_folders()

    assert window.config.actions.protected_paths == before

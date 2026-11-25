"""The Tools page reads as four parallel actions.

The buttons used to be as wide as their labels and pinned to the top of their
cards: "Export report…" was the widest, "Clean up records…" the narrowest, and
the row of mixed widths read as a hierarchy the page does not have.
"""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

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
    win.show()
    qapp.processEvents()
    yield win
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()
    repo.close()


def _buttons(win) -> list[QPushButton]:
    return win.settings_tab.findChildren(QPushButton)


def test_the_actions_read_as_short_parallel_verbs(window):
    assert [button.text() for button in _buttons(window)] == [
        "Export report…",
        "Review pairs…",
        "Edit…",
        "Clean up…",
    ]


def test_the_three_buttons_share_one_width(window):
    widths = {button.width() for button in _buttons(window)}

    assert len(widths) == 1


def test_the_buttons_sit_in_the_middle_of_their_cards(window):
    for button in _buttons(window):
        card = button.parentWidget()
        assert abs(button.geometry().center().y() - card.rect().center().y()) <= 4

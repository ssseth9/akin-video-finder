"""The Compare card's path row must stay one line high.

A long path used to wrap: three lines on one card and one on its pair, so
that card's chips and buttons sat two rows lower and the two sides of the
comparison were visibly out of step until the height equalizer papered
over the difference — with a hole in the middle of the shorter card.
"""

import os
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402

LONG_PATH = (
    "E:\\Videos\\2024\\A very long folder name for the path label test\\"
    "012246kbps_2020_01_31_Example Clip With A Long Descriptive Name "
    "And Several More Words That Must Elide In The Card Row.mp4"
)
LONG_NAME = LONG_PATH.rsplit("\\", 1)[1]


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def path_label(qapp):
    label = gui_app.PathLabel(LONG_PATH)
    label.show()
    qapp.processEvents()
    yield label
    label.close()
    label.deleteLater()


def _settle(qapp, label, width):
    label.resize(width, 20)
    qapp.processEvents()
    return label.text()


class TestPathRoot:
    def test_drive_keeps_its_separator(self):
        assert gui_app._path_root("E:\\Videos\\clip.mp4") == "E:\\"

    def test_unc_share_is_the_root(self):
        assert gui_app._path_root("\\\\nas\\media\\shows\\S01E02.mkv") == "\\\\nas\\media\\"

    def test_posix_root_is_a_bare_separator(self):
        assert gui_app._path_root("/home/x/clip.mp4") == "/"


class TestPathLabel:
    def test_wide_label_shows_the_whole_path(self, path_label, qapp):
        width = path_label.fontMetrics().horizontalAdvance(LONG_PATH) + 10
        assert _settle(qapp, path_label, width) == LONG_PATH

    def test_folders_elide_but_the_name_survives(self, path_label, qapp):
        metrics = path_label.fontMetrics()
        width = metrics.horizontalAdvance(f"E:\\…\\{LONG_NAME}") + 4
        text = _settle(qapp, path_label, width)
        assert text == f"E:\\…\\{LONG_NAME}"
        assert text.startswith("E:\\")  # the volume still identifies the drive
        assert text.endswith(LONG_NAME)  # and the name is never cut

    def test_tooltip_carries_the_whole_path(self, path_label, qapp):
        _settle(qapp, path_label, 200)
        assert path_label.toolTip() == LONG_PATH
        assert path_label.text() != LONG_PATH  # the visible text really is elided

    @pytest.mark.parametrize("width", [80, 120, 200, 300, 500, 900])
    def test_elided_text_never_wider_than_the_label(self, path_label, qapp, width):
        text = _settle(qapp, path_label, width)
        assert path_label.fontMetrics().horizontalAdvance(text) <= width

    @pytest.mark.parametrize("width", [120, 900])
    def test_row_is_one_line_high(self, path_label, qapp, width):
        _settle(qapp, path_label, width)
        assert path_label.wordWrap() is False
        assert path_label.sizeHint().height() <= path_label.fontMetrics().height() + 4


class TestThePathRowOpensExplorer:
    """The path row names where a copy lives; a click should land there.

    The sheet's click plays the video, which answers "which copy is this".
    The path answers "where is it", and the action a reviewer wants next —
    move it, rename it, look at its neighbours — lives in Explorer, so the
    row opens the folder with that file selected.
    """

    def test_the_row_uses_the_link_cursor(self, qapp):
        label = gui_app.ClickablePathLabel(LONG_PATH)
        assert label.cursor().shape() == Qt.CursorShape.PointingHandCursor
        label.deleteLater()

    def test_a_press_emits_clicked(self, qapp):
        label = gui_app.ClickablePathLabel(LONG_PATH)
        seen = []
        label.clicked.connect(lambda: seen.append(True))

        label.mousePressEvent(None)

        assert seen == [True]
        label.deleteLater()

    def test_both_rows_look_clickable(self, window, qapp):
        dialog = gui_app.CompareDialog(window)
        try:
            dialog.show()
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.02)

            for card in dialog._dialog_cards:
                assert card.image_label.cursor().shape() == Qt.CursorShape.PointingHandCursor
                assert card.path_label.cursor().shape() == Qt.CursorShape.PointingHandCursor
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_reveal_asks_explorer_to_select_the_file(self, monkeypatch, tmp_path):
        # A real file: the path branch must be the one the user gets
        video = tmp_path / "a b.mp4"
        video.write_bytes(b"x")
        calls: list[tuple[object, ...]] = []

        monkeypatch.setattr(gui_app.subprocess, "Popen", lambda *a, **kw: calls.append(a))
        monkeypatch.setattr(gui_app.sys, "platform", "win32")
        card = SimpleNamespace(file_row={"original_path": str(video)})

        gui_app.FileCard._reveal_file(card)

        # One hand-built line: the switch and the quoted path are a single
        # token. The argv form quotes the switch too, and explorer answers
        # that with Documents.
        assert calls == [(f'explorer /select,"{video}"',)]

    def test_a_missing_file_opens_its_folder(self, monkeypatch, tmp_path):
        # Selecting a file that no longer exists falls back to Documents, so
        # a stale row opens the folder that is still there
        gone = tmp_path / "gone.mp4"
        calls: list[tuple[object, ...]] = []

        monkeypatch.setattr(gui_app.subprocess, "Popen", lambda *a, **kw: calls.append(a))
        monkeypatch.setattr(gui_app.sys, "platform", "win32")
        card = SimpleNamespace(file_row={"original_path": str(gone)})

        gui_app.FileCard._reveal_file(card)

        assert calls == [(["explorer", str(tmp_path)],)]

    def test_missing_separator_falls_back_to_middle_elision(self, qapp):
        label = gui_app.PathLabel("a-very-long-name-without-any-folders-at-all.mp4")
        label.show()
        try:
            text = _settle(qapp, label, 120)
            assert "…" in text
            assert label.fontMetrics().horizontalAdvance(text) <= 120
        finally:
            label.close()
            label.deleteLater()


@pytest.fixture()
def window(tmp_path, qapp, monkeypatch):
    """A MainWindow over one pair whose paths differ wildly in length."""
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)

    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "r")))
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    paths = [LONG_PATH, str(tmp_path / "v0.mp4")]
    ids = [
        repo.create_file(root_id, p, p.lower(), os.path.basename(p), ".mp4", 100, 1000 + i, scan_id)
        for i, p in enumerate(paths)
    ]
    repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()

    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    yield win
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()
    repo.close()


def test_both_cards_use_one_elided_row(window, qapp):
    dialog = gui_app.CompareDialog(window)
    try:
        dialog.show()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)

        labels = [card.path_label for card in dialog._dialog_cards]
        assert all(isinstance(label, gui_app.PathLabel) for label in labels)
        assert all(not label.wordWrap() for label in labels)
        # The long path is elided; the short one has room to show as it is
        assert labels[0].toolTip() == LONG_PATH
        assert labels[1].toolTip().endswith("v0.mp4")
        # Identical row height is what keeps the pair's chips level
        assert labels[0].height() == labels[1].height()
        assert labels[0].height() <= labels[0].fontMetrics().height() + 4
    finally:
        dialog.close()
        dialog.deleteLater()

"""The accessibility pass: names, buddies, and keyboard parity.

A GUI test report found controls announced without a name (the sort combo,
both matching sliders, the log filter, the icon-only play overlay), a Compare
dialog with no focus anchor for assistive technology, and two actions that only
a mouse could reach (a scan-log row's reveal, an ignored pair's "Show again").
These tests pin the fixes: the visible label names its control, the icon-only
buttons carry the file they act on, and Enter/Space do what a click does.
"""

import os
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QAccessible, QKeySequence, QShortcut, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _config(tmp_path) -> Config:
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    return replace(
        cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports"))
    )


def _ac_name(widget) -> str:
    iface = QAccessible.queryAccessibleInterface(widget)
    if iface is not None and iface.text(QAccessible.Text.Name):
        return iface.text(QAccessible.Text.Name)
    return widget.accessibleName()


@pytest.fixture()
def window(tmp_path, qapp, monkeypatch):
    """A window over one pair, with both videos present on disk."""
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
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
    repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()
    win = gui_app.MainWindow(_config(tmp_path), repo, tmp_path / "config.toml")
    win.reload_data()
    yield win, repo, ids
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()


def _buddy_label(window, control) -> QLabel | None:
    for label in window.findChildren(QLabel):
        if label.buddy() is control:
            return label
    return None


def test_visible_labels_name_their_controls(window):
    win, _repo, _ids = window
    assert win._sort_combo.accessibleName() == "Sort by"
    sort_label = _buddy_label(win, win._sort_combo)
    assert sort_label is not None and sort_label.text() == "Sort by"

    for slider, name in (
        (win.strictness_slider, "Strictness"),
        (win.duration_slider, "Duration difference limit"),
    ):
        assert slider.accessibleName() == name
        label = _buddy_label(win, slider)
        assert label is not None and label.text() == name

    assert win._log_filter.accessibleName() == "Filter scan log"


def test_icon_and_decision_controls_carry_the_file(window):
    win, _repo, _ids = window
    card = next(c for cards in win.cards.values() for c in cards)
    file_name = Path(card.file_row["original_path"]).name
    assert _ac_name(card.image_label.play_button) == "Open in player"
    assert card.image_label.play_button.accessibleDescription() == file_name
    assert card.keep_button.accessibleName() == f"Keep {file_name}"
    assert card.remove_button.accessibleName() == f"Mark {file_name} for removal"

    card.set_decision("keep")
    assert card.keep_button.accessibleName() == f"Kept: {file_name}"
    card.set_decision(None)
    assert card.keep_button.accessibleName() == f"Keep {file_name}"


def test_folder_check_cells_say_what_they_toggle(window):
    win, _repo, _ids = window
    sub = win.folders_table.item(0, win._COL_SUB)
    skip = win.folders_table.item(0, win._COL_EXCL)
    assert sub is not None and skip is not None
    assert sub.data(Qt.ItemDataRole.AccessibleTextRole) == "Include subfolders"
    assert skip.data(Qt.ItemDataRole.AccessibleTextRole) == "Skip this folder"
    shown = str(Path(win.config.roots[0].path))
    assert sub.data(Qt.ItemDataRole.AccessibleDescriptionRole) == shown
    assert skip.data(Qt.ItemDataRole.AccessibleDescriptionRole) == shown


def _open_compare(window, qapp):
    win, _repo, _ids = window
    dialog = gui_app.CompareDialog(win)
    dialog.show()
    dialog.activateWindow()
    QTest.qWait(30)
    qapp.processEvents()
    return dialog


def test_compare_dialog_has_a_focus_anchor(window, qapp):
    dialog = _open_compare(window, qapp)
    try:
        for button in (
            dialog.close_button,
            dialog.details_button,
            dialog.prev_button,
            dialog.next_button,
            dialog.undo_button,
            dialog.ignore_button,
        ):
            assert button.focusPolicy() != Qt.FocusPolicy.NoFocus
            assert button.accessibleName()
        assert _ac_name(dialog.next_button) == "Next pair"
        # The reviewer starts where the reviewer acts
        assert QApplication.focusWidget() is dialog.next_button
        for card in dialog._dialog_cards:
            for control in (card.image_label.play_button, card.keep_button, card.remove_button):
                assert control.focusPolicy() != Qt.FocusPolicy.NoFocus
    finally:
        dialog.close()
        dialog.deleteLater()


def test_compare_space_is_not_a_window_shortcut(window, qapp):
    dialog = _open_compare(window, qapp)
    try:
        space = QKeySequence(Qt.Key.Key_Space)
        assert [s for s in dialog.findChildren(QShortcut) if s.key() == space] == []
        # ←/→ still page, so the only lost shortcut duplicated one of them
        arrows = {s.key().toString() for s in dialog.findChildren(QShortcut)}
        assert "Right" in arrows and "Left" in arrows
    finally:
        dialog.close()
        dialog.deleteLater()


def test_enter_opens_the_log_row_under_the_cursor(window, monkeypatch):
    win, _repo, ids = window
    opened: list[str] = []
    monkeypatch.setattr(win, "_reveal_log_path", opened.append)
    video = str(Path(win.files[ids[0]]["original_path"]))
    win._scan_log_records.append(gui_app._LogEntry("00:00:01", "v0.mp4", path=video))
    win._render_log()

    cursor = win.scan_log.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    win.scan_log.setTextCursor(cursor)
    QTest.keyClick(win.scan_log, Qt.Key_Return)

    assert opened == [video]


def test_a_single_click_opens_the_log_row_under_the_pointer(window, monkeypatch):
    win, _repo, ids = window
    opened: list[str] = []
    monkeypatch.setattr(win, "_reveal_log_path", opened.append)
    video = str(Path(win.files[ids[0]]["original_path"]))
    win._scan_log_records.append(gui_app._LogEntry("00:00:01", "v0.mp4", path=video))
    win._render_log()

    cursor = win.scan_log.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    win.scan_log.setTextCursor(cursor)
    QTest.mouseClick(
        win.scan_log.viewport(),
        Qt.MouseButton.LeftButton,
        pos=win.scan_log.cursorRect(cursor).center(),
    )

    assert opened == [video]


def _dismiss(win, repo, ids):
    key_a = win.files[ids[0]]["normalized_path_key"]
    key_b = win.files[ids[1]]["normalized_path_key"]
    repo.dismiss_pair(key_a, key_b)
    repo.commit()


def test_ignored_row_acts_on_enter_and_space(window, qapp):
    win, repo, ids = window
    _dismiss(win, repo, ids)
    dialog = gui_app.DismissedDialog(win)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.table.rowCount() == 1
        # Space on the checkbox column checks the row, once
        dialog.table.setCurrentCell(0, 0)
        QTest.keyClick(dialog.table, Qt.Key_Space)
        assert dialog.table.item(0, 0).checkState() == Qt.CheckState.Checked
        assert dialog._undo_selected.isEnabled()

        # Enter on the Action column shows the pair again, like a click
        dialog.table.setCurrentCell(0, 4)
        QTest.keyClick(dialog.table, Qt.Key_Return)
        assert repo.get_dismissed_items() == []
    finally:
        dialog.close()
        dialog.deleteLater()


def test_ignored_cells_have_accessible_names(window, qapp):
    win, repo, ids = window
    _dismiss(win, repo, ids)
    dialog = gui_app.DismissedDialog(win)
    try:
        assert dialog.table.accessibleName() == "Ignored pairs"
        name_a = Path(win.files[ids[0]]["original_path"]).name
        name_b = Path(win.files[ids[1]]["original_path"]).name
        assert (
            dialog.table.item(0, 0).data(Qt.ItemDataRole.AccessibleTextRole)
            == f"Show {name_a} and {name_b} again"
        )
        assert dialog.table.item(0, 4).data(Qt.ItemDataRole.AccessibleTextRole) == "Show again"
    finally:
        dialog.close()
        dialog.deleteLater()

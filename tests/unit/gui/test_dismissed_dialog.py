"""The Ignored-pairs dialog: what it costs to open, and undoing in bulk.

A few thousand dismissals is an ordinary library after one wrong "Dismiss all
shown", and this dialog is the way back. It used to carry a QPushButton per
row — 170 ms to build, another 300 ms to lay out — and offered only
one-at-a-time undo, so the way back was a few thousand clicks.
"""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402

STAMPS = json.dumps([0.0, 10.0, 20.0])


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _add_file(repo: DatabaseRepository, path: Path, seed: int, scan_id: int, root_id: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    file_id = repo.create_file(
        root_id, str(path), str(path).lower(), path.name, path.suffix, 1, 1000, scan_id
    )
    hashes = bytes(range(seed, seed + 96))
    repo.upsert_signature(file_id, "ahash8-v3", STAMPS, b"frames", hashes, 1, 1000)
    return file_id


@pytest.fixture()
def library(tmp_path, qapp):
    """One root, three videos, three dismissed pairs."""
    root = tmp_path / "library"
    cfg = Config(roots=[RootConfig(path=str(root))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))

    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(root), str(root).lower())
    ids = {
        name: _add_file(repo, root / name, seed, scan_id, root_id)
        for name, seed in (("a.mp4", 0), ("b.mp4", 40), ("c.mp4", 80))
    }
    edges = (
        ("a.mp4", "b.mp4", 0.63, 0.21),
        ("a.mp4", "c.mp4", 0.61, 0.24),
        ("b.mp4", "c.mp4", 0.60, 0.27),
    )
    for first, second, match, shift in edges:
        repo.create_pair_comparison(
            scan_id, ids[first], ids[second], "v1", "POSSIBLE", match, shift, 12
        )
    for first, second, *_rest in edges:
        repo.dismiss_pair(str(root / first).lower(), str(root / second).lower())
    # Two batches an hour apart, the way real use leaves them: one wrong
    # "Dismiss all shown" is one timestamp, and undoing it means finding it
    for first, second, when in (
        ("a.mp4", "b.mp4", "2026-11-12T05:35:00"),
        ("a.mp4", "c.mp4", "2026-11-12T05:35:00"),
        ("b.mp4", "c.mp4", "2026-11-12T06:10:00"),
    ):
        repo.conn.execute(
            "UPDATE dismissed_pairs SET created_at = ? WHERE key_a = ? AND key_b = ?",
            (when, str(root / first).lower(), str(root / second).lower()),
        )
    repo.commit()

    created = []

    def _open():
        win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
        win.reload_data()
        created.append(win)
        return win

    yield _open, repo, ids, root

    for win in created:
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
    repo.close()


def _dialog(window) -> gui_app.DismissedDialog:
    dlg = gui_app.DismissedDialog(window)
    return dlg


def test_a_row_carries_no_widget(library) -> None:
    """The per-row QPushButton was what made the dialog slow to open at all."""
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())

    assert dlg.table.rowCount() == 3
    for row in range(dlg.table.rowCount()):
        assert dlg.table.cellWidget(row, 4) is None
        assert dlg.table.item(row, 4).text() == "Show again"


def test_undoing_one_row_removes_it_and_its_dismissal(library) -> None:
    _open, repo, _ids, _root = library
    win = _open()
    dlg = _dialog(win)
    before = len(win.dismissed)

    dlg._on_cell_clicked(0, 4)

    assert dlg.table.rowCount() == 2
    assert len(dlg._row_items) == 2
    assert len(win.dismissed) == before - 1
    assert len(repo.get_dismissed_pairs()) == before - 1


def test_clicking_another_column_does_not_undo(library) -> None:
    """The row is selectable; only the Undo cell acts."""
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())

    dlg._on_cell_clicked(0, 1)

    assert dlg.table.rowCount() == 3


def test_undoing_everything_shown_takes_them_all_at_once(library) -> None:
    """One rebuild for the batch, not one per row."""
    _open, repo, _ids, _root = library
    win = _open()
    dlg = _dialog(win)
    assert win.all_pairs == []

    dlg._undismiss_many(list(dlg._row_items))

    assert dlg.table.rowCount() == 0
    assert win.dismissed == set()
    assert win.dismissed_content == set()
    assert repo.get_dismissed_pairs() == set()
    # The review gets all three pairs back
    assert len(win.all_pairs) == 3


def test_the_filter_decides_what_all_shown_means(library) -> None:
    """The way back from a wrong bulk dismiss acts on what is actually shown."""
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())
    assert len(dlg._visible_items()) == 3

    dlg._filter.setText("c.mp4")

    # Only the two pairs that mention c.mp4, and the button says how many
    visible = dlg._visible_items()
    assert len(visible) == 2
    assert all("c.mp4" in item["_search"] for item in visible)
    assert "2" in dlg._undo_shown.text()

    dlg._filter.setText("nothing matches this")
    assert dlg._visible_items() == []
    assert not dlg._undo_shown.isEnabled()


def test_the_filter_matches_the_ignored_date(library) -> None:
    """The filter has to accept the date exactly as the column shows it."""
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())
    assert len(dlg._visible_items()) == 3
    # Rows come newest first, so this one is from the earlier batch
    shown = dlg.table.item(1, 3).text()

    dlg._filter.setText(shown)
    assert len(dlg._visible_items()) == 2
    assert "2" in dlg._undo_shown.text()

    # A day, a month or a year is enough to narrow it
    dlg._filter.setText("2026-11-12")
    assert len(dlg._visible_items()) == 3
    dlg._filter.setText("2026-11")
    assert len(dlg._visible_items()) == 3

    dlg._filter.setText(shown[:-5] + " 07:00")
    assert dlg._visible_items() == []


def test_clicking_the_date_filters_to_that_batch(library) -> None:
    """One click isolates the batch that shares a timestamp."""
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())
    newest = dlg.table.item(0, 3).text()

    dlg._on_cell_clicked(0, 3)

    assert dlg._filter.text() == newest
    assert len(dlg._visible_items()) == 1


def test_the_bulk_buttons_follow_the_selection(library) -> None:
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())
    assert not dlg._undo_selected.isEnabled()

    dlg.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert dlg._undo_selected.isEnabled()
    assert dlg._selected_rows() == [0]

    dlg.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    assert dlg._selected_rows() == [0, 1]
    assert "2" in dlg._undo_selected.text()


def test_no_ignored_pairs_says_so_instead_of_an_empty_table(library) -> None:
    """An empty table with headers and a live filter reads as broken."""
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())
    dlg._undismiss_many(list(dlg._row_items))

    assert dlg._hint.text() == "No ignored pairs — pairs you ignore in Results appear here."
    assert dlg._hint.isHidden() is False
    assert dlg.table.isHidden() is True
    assert dlg._filter.isHidden() is True
    assert not dlg._undo_shown.isEnabled()


def test_a_filter_without_matches_says_no_matches(library) -> None:
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())

    dlg._filter.setText("nothing matches this")

    assert dlg._hint.text() == "No matches."
    assert dlg.table.isHidden() is False
    assert not dlg._undo_shown.isEnabled()


def test_the_bulk_button_count_is_grammatical_for_one_pair(library) -> None:
    """The dialog can hold exactly one row; "Show all 1 pairs…" read as a bug."""
    _open, _repo, _ids, _root = library
    dlg = _dialog(_open())
    newest = dlg.table.item(0, 3).text()

    dlg._filter.setText(newest)

    assert dlg._visible_items()
    assert len(dlg._visible_items()) == 1
    assert dlg._undo_shown.text() == "Show all 1 pair…"

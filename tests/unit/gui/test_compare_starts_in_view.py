"""Compare must open at the pair the reviewer is looking at.

The dialog used to start at pair 1 always, so a reviewer who had scrolled to
a specific pair had to find it again by hand. The review page cannot answer
"which pair is in view?" by index either: its boxes are built from the shown
pairs but skip any whose file row is missing, so box *i* is not necessarily
``_shown_pairs()[i]``. Each box carries its comparison, which turns the
question into a lookup instead of a guess.

The toolbar count and the dialog's plan are checked here too — both come
from ``pending_remove_ids``, so a mark on a file the inventory calls missing
cannot be counted while being refused.
"""

import os
import time
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.application.actions import ActionService  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(app, seconds: float) -> None:
    """Run the event loop, including the DeferredDelete step that the real
    loop performs — processEvents() alone leaves deleted widgets alive."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.005)


@pytest.fixture()
def make_window(tmp_path, qapp, monkeypatch):
    """A review page over a database of the given file sizes and pairs."""
    # Rendering contact sheets needs ffmpeg and real videos; the page
    # geometry under test does not depend on the sheet images
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)
    created: list[tuple[object, DatabaseRepository]] = []

    def _make(sizes: list[int], pairs: list[tuple[int, int]]):
        cfg = Config(roots=[RootConfig(path=str(tmp_path))])
        cfg = replace(
            cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports"))
        )
        repo = DatabaseRepository(tmp_path / "v.db")
        scan_id = repo.create_scan("test")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        ids: list[int] = []
        for i, size in enumerate(sizes):
            video = tmp_path / f"v{i}.mp4"
            # Real size and mtime: the removal plan re-checks both against
            # the inventory before it promises to move a file
            video.write_bytes(b"x" * size)
            stat = video.stat()
            ids.append(
                repo.create_file(
                    root_id,
                    str(video),
                    str(video).lower(),
                    video.name,
                    ".mp4",
                    stat.st_size,
                    stat.st_mtime_ns,
                    scan_id,
                )
            )
        for a, b in pairs:
            repo.create_pair_comparison(scan_id, ids[a], ids[b], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
        repo.commit()
        win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
        win.reload_data()
        created.append((win, repo))
        return win, ids, repo

    yield _make

    for win, repo in created:
        # Timers outlive the widget unless stopped explicitly
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
        repo.close()


def _boxes(win) -> list:
    return [box for box in win._pair_boxes if isinstance(box, gui_app.PairBox)]


class TestWhichBoxIsInView:
    """``_first_visible_box`` reads the scroll position, not the page."""

    def test_the_top_intersecting_box_wins(self):
        # Three boxes 100 px tall, 20 px apart; a viewport of 100 px at 130
        # starts inside the second box
        spans = [(0, 100), (120, 220), (240, 340)]
        assert gui_app._first_visible_box(spans, 130, 100) == 1

    def test_a_sliver_of_the_previous_box_does_not_win(self):
        # The viewport starts 5 px inside the first box, so the second box
        # fills almost all of it. The pair being read is the second one;
        # the first is only a leftover edge at the top of the screen.
        spans = [(0, 100), (120, 220), (240, 340)]
        assert gui_app._first_visible_box(spans, 95, 100) == 1

    def test_the_top_of_the_page_is_the_first_box(self):
        spans = [(0, 100), (120, 220), (240, 340)]
        assert gui_app._first_visible_box(spans, 0, 100) == 0

    def test_a_viewport_in_the_gap_names_the_next_box(self):
        # The gap between two boxes belongs to neither, but the reader is
        # about to read the one below it — the box above is already gone
        spans = [(0, 100), (120, 220), (240, 340)]
        assert gui_app._first_visible_box(spans, 105, 10) == 1

    def test_a_viewport_past_the_last_box_names_the_last_box(self):
        # A short page, or a scroll position left over from a longer one:
        # nothing else can be shown
        spans = [(0, 100), (120, 220), (240, 340)]
        assert gui_app._first_visible_box(spans, 900, 100) == 2

    def test_no_boxes_is_no_answer(self):
        assert gui_app._first_visible_box([], 0, 100) is None


class TestThePageReportsItsPair:
    """The review page hands back the comparison its top box carries."""

    def test_each_reachable_scroll_position_names_its_own_box(self, make_window, qapp):
        win, _ids, _repo = make_window([100, 110, 120, 130], [(0, 1), (1, 2), (2, 3)])
        win.show()
        win.resize(700, 420)
        _pump(qapp, 0.3)

        boxes = _boxes(win)
        assert len(boxes) == 3
        scroll = win._result_container.findChild(QScrollArea)
        assert scroll is not None
        content = scroll.widget()
        bar = scroll.verticalScrollBar()
        assert bar.maximum() > 0, "the page must scroll for this test to mean anything"

        reached = []
        for i, box in enumerate(boxes):
            top = box.mapTo(content, QPoint(0, 0)).y()
            if top > bar.maximum():
                continue  # the scrollbar cannot put this box at the top
            bar.setValue(top)
            _pump(qapp, 0.05)
            reached.append(i)
            assert win._pair_in_view() is box.comparison

        assert len(reached) >= 2, f"only boxes {reached} could be scrolled to"

    def test_an_empty_page_has_no_pair_in_view(self, make_window, qapp):
        win, _ids, _repo = make_window([100], [])
        win.show()
        _pump(qapp, 0.2)
        assert _boxes(win) == []
        assert win._pair_in_view() is None


class TestTheDialogStartsThere:
    """``CompareDialog`` opens on the pair it was handed, not always pair 1."""

    def test_starts_at_the_pair_it_was_given(self, make_window, qapp):
        win, _ids, _repo = make_window([100, 110, 120], [(0, 1), (1, 2), (0, 2)])
        _pump(qapp, 0.2)
        third = win._visible_pairs()[2]

        dialog = gui_app.CompareDialog(win, start_pair=third)
        try:
            assert dialog.index == 2
            assert dialog.pairs[dialog.index] is third
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_starts_at_the_first_pair_without_one(self, make_window, qapp):
        win, _ids, _repo = make_window([100, 110], [(0, 1)])
        _pump(qapp, 0.2)

        dialog = gui_app.CompareDialog(win)
        try:
            assert dialog.index == 0
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_falls_back_when_the_pair_is_filtered_out(self, make_window, qapp):
        # "Hide decided" drops pairs the review page still shows, so the
        # pair in view can legitimately be absent — the dialog then starts
        # at the first pair rather than guessing a neighbour
        win, _ids, _repo = make_window([100, 110, 120], [(0, 1), (1, 2), (0, 2)])
        _pump(qapp, 0.2)
        hidden = win._visible_pairs()[1]
        win.apply_decision(hidden["file_a_id"], "keep")
        win._result_hide_decided = True

        dialog = gui_app.CompareDialog(win, start_pair=hidden)
        try:
            assert hidden not in dialog.pairs
            assert dialog.index == 0
        finally:
            dialog.close()
            dialog.deleteLater()


class TestTheToolbarCountMatchesThePlan:
    """One helper feeds both, so the count cannot promise what the plan refuses."""

    def test_a_mark_on_a_missing_file_is_counted_by_neither(self, make_window, qapp, tmp_path):
        win, ids, repo = make_window([100, 110, 120], [(0, 1), (1, 2)])
        _pump(qapp, 0.2)
        # Two marks on files that are there, one on a file the inventory
        # has already lost — the case that promised sixteen files the
        # dialog refused to list. The lost file is gone from disk too, so
        # the plan would report it as skipped while the toolbar counted it
        for fid in (ids[0], ids[1]):
            repo.set_decision(win.scan_id, fid, "remove")
        repo.set_decision(win.scan_id, ids[2], "remove")
        repo.mark_file_missing(ids[2], win.scan_id)
        repo.commit()
        (tmp_path / "v2.mp4").unlink()

        win.reload_data()
        _pump(qapp, 0.2)

        # The count and the dialog's list describe the same two files
        assert win._quarantine_button.text() == "Marked files 2"
        plan = ActionService(repo).plan_removals(win.scan_id)
        assert {r.file_id for r in plan.removals} == {ids[0], ids[1]}
        assert not plan.skipped

    def test_one_mark_reads_as_one_file(self, make_window, qapp):
        win, ids, repo = make_window([100, 110], [(0, 1)])
        _pump(qapp, 0.2)
        repo.set_decision(win.scan_id, ids[0], "remove")
        repo.commit()

        win.reload_data()
        _pump(qapp, 0.2)

        assert win._quarantine_button.text() == "Marked files 1"

    def test_no_marks_reads_as_no_count(self, make_window, qapp):
        win, _ids, _repo = make_window([100, 110], [(0, 1)])
        _pump(qapp, 0.2)
        assert win._quarantine_button.text() == "Marked files"

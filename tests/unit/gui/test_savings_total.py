"""The toolbar's "~X GB recoverable" figure must count each file once.

A film kept in four copies is six pairs, and the figure used to add the
smaller file of every pair — so it claimed the same three redundant copies
twice. It also only refreshed on a full page rebuild, so a single ignored
pair left the page while its space stayed in the total.
"""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402

GB = 1_000_000_000


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def make_window(tmp_path, qapp):
    """A review page over a database of the given file sizes and pairs."""
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
            video.write_bytes(b"x")
            ids.append(
                repo.create_file(
                    root_id,
                    str(video),
                    str(video).lower(),
                    video.name,
                    ".mp4",
                    size,
                    1000 + i,
                    scan_id,
                )
            )
        for pair in pairs:
            # A pair may carry its match fraction; the fixture default is a
            # certain duplicate, which is what the older tests describe
            a, b, fraction = (*pair, 1.0)[:3]
            classification = "VERY_LIKELY" if fraction >= 0.86 else "POSSIBLE"
            repo.create_pair_comparison(
                scan_id, ids[a], ids[b], "v1", classification, fraction, 0.01, 12
            )
        repo.commit()
        win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
        win.reload_data()
        created.append((win, repo))
        return win, ids

    yield _make

    for win, repo in created:
        # Timers outlive the widget unless stopped explicitly
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
        repo.close()


class TestEachFileCountedOnce:
    """A copy reachable through several pairs is still one copy."""

    def test_four_copies_count_three_not_six(self, make_window):
        # One film in four copies is six pairs; three of the four files are
        # redundant, so 3 GB — not the 6 GB the per-pair sum reported
        win, _ids = make_window([GB] * 4, [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)])
        assert win._savings_label.text() == "~3.0 GB recoverable"

    def test_chain_of_pairs_is_one_set(self, make_window):
        # A–B and B–C share B, so all three files are one set of duplicates
        win, _ids = make_window([GB, GB, GB], [(0, 1), (1, 2)])
        assert win._savings_label.text() == "~2.0 GB recoverable"

    def test_file_smaller_in_two_pairs_counts_once(self, make_window):
        # A 0.5 GB copy against two 1 GB copies: keep a 1 GB copy, free the
        # other two = 1.5 GB. The old per-pair "smaller file" sum counted the
        # 0.5 GB copy twice and reported 1.0 GB.
        win, _ids = make_window([GB // 2, GB, GB], [(0, 1), (0, 2)])
        assert win._savings_label.text() == "~1.5 GB recoverable"

    def test_separate_sets_add_up(self, make_window):
        win, _ids = make_window([GB] * 4, [(0, 1), (2, 3)])
        assert win._savings_label.text() == "~2.0 GB recoverable"

    def test_nothing_shown_shows_no_figure(self, make_window):
        win, _ids = make_window([GB], [])
        assert win._savings_label.text() == ""


class TestTheFigureText:
    """A range while the page holds anything the matcher is not sure about."""

    def test_no_space_is_no_figure(self):
        assert gui_app._savings_text(0, 0) == ""

    def test_agreement_is_one_number(self):
        assert gui_app._savings_text(GB, GB) == "~1.0 GB recoverable"

    def test_a_zero_floor_is_up_to_the_high_end(self):
        assert gui_app._savings_text(0, 2 * GB) == "Up to 2.0 GB recoverable"

    def test_a_nonzero_range_spells_out_from_to(self):
        assert gui_app._savings_text(GB, 2 * GB) == "From 1.0 to 2.0 GB recoverable"

    def test_megabytes_stay_megabytes(self):
        """A 4.1 MB pair used to read "~0.0 GB recoverable"."""
        assert gui_app._savings_text(4_100_000, 4_100_000) == "~4.1 MB recoverable"
        assert gui_app._size_text(4_100_000) == "4.1 MB"
        assert gui_app._size_text(2_000_000_000) == "2.0 GB"
        assert gui_app._size_text(512_000) == "512 KB"


class TestRangeUntilCertain:
    """The high end treats every shown pair as real; the low end only 100 %."""

    def test_a_weak_pair_only_raises_the_high_end(self, make_window):
        # Two 1 GB pairs: one certain, one a 83 % maybe. The old single
        # figure said 2.0 GB, which presented the guess as a fact.
        win, _ids = make_window([GB] * 4, [(0, 1, 1.0), (2, 3, 0.83)])
        assert win._savings_label.text() == "From 1.0 to 2.0 GB recoverable"

    def test_all_certain_keeps_a_single_number(self, make_window):
        win, _ids = make_window([GB, GB], [(0, 1, 1.0)])
        assert win._savings_label.text() == "~1.0 GB recoverable"


class TestIgnoringOnePairUpdatesTheTotal:
    def test_total_follows_a_single_ignore_without_a_rebuild(self, make_window):
        win, _ids = make_window([GB] * 4, [(0, 1), (2, 3)])
        label = win._savings_label
        assert label.text() == "~2.0 GB recoverable"

        win._dismiss_pair(win.all_pairs[0], win._pair_boxes[0])

        # Same label instance: the figure was recomputed in place, not by
        # rebuilding the page
        assert win._savings_label is label
        assert label.text() == "~1.0 GB recoverable"

    def test_the_tab_progress_and_headers_follow_the_same_ignore(self, make_window):
        """One ignored pair leaves the page, and the rest renumber.

        The ignored pair used to count as filter-hidden — "1 hidden" on a
        page nobody filtered — while the tab kept the old total, the removed
        section heading kept the old count, and the remaining headers still
        said "of 4".
        """
        win, _ids = make_window([GB] * 8, [(0, 1), (2, 3), (4, 5), (6, 7)])
        result_idx = win.tabs.indexOf(win._result_container)
        assert win.tabs.tabText(result_idx) == "Results (4)"
        assert win._review_progress_label.text() == "0 of 4 reviewed"

        comparison = win.all_pairs[2]
        box = next(b for b in win._pair_boxes if b.comparison is comparison)
        win._dismiss_pair(comparison, box)

        assert win.tabs.tabText(result_idx) == "Results (3)"
        assert win._review_progress_label.text() == "0 of 3 reviewed"
        headers = [label.text() for label in win._pair_headers]
        assert headers == ["Pair 1 of 3", "Pair 2 of 3", "Pair 3 of 3"]

    def test_tooltip_states_the_assumption(self, make_window):
        win, _ids = make_window([GB, GB], [(0, 1)])
        tip = win._savings_label.toolTip()
        assert "counted once" in tip
        assert "recommends" in tip

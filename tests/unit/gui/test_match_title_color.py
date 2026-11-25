"""The pair title's colour has to carry the percentage it prints.

Every title rendered in the same grey, so a page sorted by match read as one
wall of identical text: an 83 % pair and a 100 % pair looked alike until the
number was parsed. The hue runs from the theme's warning amber at the lowest
score a review shows to its success green at a certain one. Amber is the
floor, not red: a weak candidate is weak evidence, not an error.
"""

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.gui.theme import token  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402

GB = 1_000_000_000
FLOOR = 0.58


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def make_window(tmp_path, qapp):
    """A review page over one pair per match fraction given."""
    created: list[tuple[object, DatabaseRepository]] = []

    def _make(fractions: list[float]):
        cfg = Config(roots=[RootConfig(path=str(tmp_path))])
        cfg = replace(
            cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports"))
        )
        repo = DatabaseRepository(tmp_path / "v.db")
        scan_id = repo.create_scan("test")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        ids: list[int] = []
        for i in range(2 * len(fractions)):
            video = tmp_path / f"v{i}.mp4"
            video.write_bytes(b"x")
            ids.append(
                repo.create_file(
                    root_id,
                    str(video),
                    str(video).lower(),
                    video.name,
                    ".mp4",
                    GB,
                    1000 + i,
                    scan_id,
                )
            )
        for n, fraction in enumerate(fractions):
            classification = "VERY_LIKELY" if fraction >= 0.86 else "POSSIBLE"
            repo.create_pair_comparison(
                scan_id, ids[2 * n], ids[2 * n + 1], "v1", classification, fraction, 0.01, 12
            )
        repo.commit()
        win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
        win.reload_data()
        created.append((win, repo))
        return win

    yield _make

    for win, repo in created:
        # Timers outlive the widget unless stopped explicitly
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
        repo.close()


def _hue(color: str) -> float:
    return QColor(color).hueF() * 360.0


class TestMatchColor:
    """Amber at the review floor, green at 100 %, no red left in between."""

    def test_a_certain_match_is_green(self, qapp):
        assert _hue(gui_app._match_color(1.0, FLOOR)) == pytest.approx(
            QColor(token("success")).hueF() * 360.0, abs=2.0
        )

    def test_the_review_floor_is_amber(self, qapp):
        assert _hue(gui_app._match_color(FLOOR, FLOOR)) == pytest.approx(
            QColor(token("warning")).hueF() * 360.0, abs=2.0
        )

    def test_a_strong_possible_sits_between(self, qapp):
        floor_hue = QColor(token("warning")).hueF() * 360.0
        top_hue = QColor(token("success")).hueF() * 360.0
        assert floor_hue < _hue(gui_app._match_color(0.83, FLOOR)) < top_hue

    def test_below_the_floor_stays_amber_and_never_turns_red(self, qapp):
        assert _hue(gui_app._match_color(0.05, FLOOR)) == pytest.approx(
            QColor(token("warning")).hueF() * 360.0, abs=2.0
        )
        assert _hue(gui_app._match_color(0.05, FLOOR)) > 20.0


class TestTheTitleWearsIt:
    """The colour the page shows is the colour the percentage earns."""

    def test_each_title_carries_its_own_percentage(self, make_window, qapp):
        win = make_window([1.0, 0.83])
        floor = win.config.matching.possible_threshold

        styles = {}
        for box in win._pair_boxes:
            titles = [label for label in box.findChildren(QLabel) if "% match" in label.text()]
            assert titles
            styles[titles[0].text()] = titles[0].styleSheet()

        assert any(gui_app._match_color(1.0, floor) in style for style in styles.values())
        assert any(gui_app._match_color(0.83, floor) in style for style in styles.values())

"""The card's facts share one strip.

All five metric chips keep to one line, and the verdict badge sits at the
right end of that same line instead of on a row of its own beneath it. The
strip is what gives the cards a fixed height, so a badge appearing on one
copy cannot move its buttons out of line with the peer's.
"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


class _Loader:
    def request(self, *args, **kwargs) -> None:
        pass


def _probe() -> dict:
    return {
        "container_duration": 1626.368,
        "container_bitrate": 7_860_000,
        "primary_video_width": 1920,
        "primary_video_height": 1080,
        "primary_video_codec": "h264",
    }


def _card(width: int = 420) -> gui_app.FileCard:
    probe = _probe()
    row = {
        "id": 1,
        "original_path": r"E:\videos\clip.mp4",
        "size": 1_600_000_000,
    }
    window = SimpleNamespace(
        decisions={},
        sheet_loader=_Loader(),
        _duration=lambda _file_id: 1626.368,
    )
    card = gui_app.FileCard(window, row, probe, [(row, probe)], False, [], tie_label="Identical")
    card.set_decision(None)
    card.resize(width, 600)
    card.show()
    card.layout().activate()
    return card


def test_every_metric_chip_shares_one_row(qapp):
    card = _card()
    chips = card.findChildren(QLabel, "metricChip")

    assert len(chips) == 5
    assert len({chip.geometry().top() for chip in chips}) == 1
    assert all(chip.geometry().right() <= card.width() for chip in chips)


def test_the_status_badge_sits_at_the_end_of_the_metric_row(qapp):
    card = _card()
    chips = card.findChildren(QLabel, "metricChip")

    assert card.tie_badge.parentWidget() is card.metrics_strip
    assert not card.tie_badge.isHidden()
    assert card.tie_badge.geometry().left() > chips[-1].geometry().right()
    assert card.tie_badge.geometry().center().y() == card.metrics_strip.rect().center().y()


def test_the_card_widens_to_hold_the_strip(qapp):
    card = _card()
    chips = card.findChildren(QLabel, "metricChip")
    strip = sum(chip.sizeHint().width() for chip in chips)

    assert card.minimumWidth() >= strip

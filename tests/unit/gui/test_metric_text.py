"""The duration chip uses one fixed, precise clock format.

Two copies a fraction of a second apart must not render the same chip while
the keeper key compares 6873.42 against 6873.61; the review's answer is
``HH:MM:SS.mmm`` on every card, not a mixed human/precise pair.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from akin_video_finder.gui.app import _metric_text  # noqa: E402


def _probe(duration: float) -> dict:
    return {
        "status": "success",
        "container_duration": duration,
        "container_bitrate": 5_000_000,
        "primary_video_width": 1280,
        "primary_video_height": 720,
        "primary_video_codec": "h264",
    }


def _file(size: int = 1_000_000) -> dict:
    return {"id": 1, "size": size}


def test_duration_chip_uses_the_fixed_millisecond_clock() -> None:
    assert _metric_text(_file(), _probe(6873.42))["duration"] == "01:54:33.420"


def test_a_fractional_difference_is_visible() -> None:
    first = _metric_text(_file(), _probe(6873.42))["duration"]
    second = _metric_text(_file(), _probe(6873.61))["duration"]
    assert first != second


def test_a_missing_probe_reads_unknown() -> None:
    assert _metric_text(_file(), None)["duration"] == "?"


def test_the_chips_use_human_units_and_codec_names() -> None:
    texts = _metric_text(_file(), _probe(6873.42))
    assert texts["resolution"] == "1,280 × 720"
    assert texts["bitrate"] == "5.00 Mbps"
    assert texts["codec"] == "H.264"
    assert texts["size"] == "1 MB" == "1 MB"


def test_the_reading_order_puts_resolution_first_and_size_last() -> None:
    texts = _metric_text(_file(), _probe(6873.42))
    assert list(texts) == ["resolution", "bitrate", "codec", "duration", "size"]

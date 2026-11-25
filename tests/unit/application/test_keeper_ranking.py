"""Keeper recommendation: length is evidence, not a veto.

The ranking used to put completeness first with a 0.5% tolerance, so a copy a
few seconds longer beat every other consideration. On a real pair that meant a
640x352 MPEG-4 file (772 MB, 2.3 Mbps) was recommended over a 1280x720 h264
one (1.6 GB, 5.0 Mbps) purely because it ran 30s longer — 1.1% of the runtime.

These tests pin the trade-off that replaced it: quality is measured in log2
"stops" from bitrate, pixels, codec and audio, and each percent of missing
length costs a configurable fraction of a stop.
"""

from __future__ import annotations

from typing import Any

import pytest

from akin_video_finder.application.analysis import _describe, _member_key, _quality_stops
from akin_video_finder.configuration.models import KeeperConfig
from akin_video_finder.core.models import ProbeStatus


def _probe(
    duration: float,
    width: int = 1280,
    height: int = 720,
    bitrate: int = 5_000_000,
    codec: str = "h264",
    status: str = ProbeStatus.SUCCESS.value,
) -> dict[str, Any]:
    return {
        "status": status,
        "container_duration": duration,
        "container_bitrate": bitrate,
        "primary_video_width": width,
        "primary_video_height": height,
        "primary_video_codec": codec,
        "primary_audio_codec": "aac",
        "primary_audio_channels": 2,
        "primary_video_fps": "25/1",
    }


def _file(file_id: int, path: str, size: int = 1_000_000) -> dict[str, Any]:
    return {"id": file_id, "original_path": path, "file_name": path.split("/")[-1], "size": size}


def _keeper_id(
    left: tuple[dict[str, Any], dict[str, Any]],
    right: tuple[dict[str, Any], dict[str, Any]],
    keeper: KeeperConfig | None = None,
) -> int:
    config = keeper or KeeperConfig()
    max_duration = max(
        float(left[1]["container_duration"] or 0.0),
        float(right[1]["container_duration"] or 0.0),
    )
    keys = {
        member["id"]: _member_key(member, probe, max_duration, config)
        for member, probe in (left, right)
    }
    return max(keys, key=lambda fid: keys[fid])


# A representative pair from the bug report: the smaller file is longer by
# 30s, the larger one is 4x the pixels with 2x the bitrate.
HIGH_QUALITY = (
    _file(
        1,
        "e:/videos/sample_high_quality_1280x720.mp4",
        1_613_530_000,
    ),
    _probe(2607.0, width=1280, height=720, bitrate=4_952_000, codec="h264"),
)
LONGER_LOW_QUALITY = (
    _file(
        2,
        "e:/videos/sample_longer_low_quality_640x352.mkv",
        772_631_000,
    ),
    _probe(2637.0, width=640, height=352, bitrate=2_344_000, codec="mpeg4"),
)


class TestTheReportedPair:
    def test_the_high_quality_copy_wins_despite_being_shorter(self) -> None:
        assert _keeper_id(HIGH_QUALITY, LONGER_LOW_QUALITY) == 1
        assert _keeper_id(LONGER_LOW_QUALITY, HIGH_QUALITY) == 1

    def test_the_verdict_does_not_depend_on_argument_order(self) -> None:
        assert _keeper_id(HIGH_QUALITY, LONGER_LOW_QUALITY) == _keeper_id(
            LONGER_LOW_QUALITY, HIGH_QUALITY
        )

    def test_quality_lead_is_measured_in_stops(self) -> None:
        better = _quality_stops(*HIGH_QUALITY, KeeperConfig())
        worse = _quality_stops(*LONGER_LOW_QUALITY, KeeperConfig())
        # ~2.5 stops: 2.1x bitrate (1.1), 4.1x pixels (1.0), newer codec (0.5)
        assert 2.0 < better - worse < 3.5

    def test_the_losing_card_explains_itself(self) -> None:
        reasons = _describe(
            *LONGER_LOW_QUALITY,
            HIGH_QUALITY[1],
            max_duration=2637.0,
        )
        assert "30.00s longer, but weaker quality" in reasons
        assert "lower resolution (640x352 vs 1280x720)" in reasons
        assert "lower bitrate (2.3 vs 5.0 Mbps)" in reasons
        assert "older codec (mpeg4 vs h264)" in reasons


class TestTheTradeOff:
    """How much longer a copy is decides how much quality it takes to win."""

    def test_a_truly_truncated_copy_still_loses(self) -> None:
        # 20% of the runtime missing is not a rounding error: no quality lead
        # in the same footage buys that back
        complete = (
            _file(1, "e:/v/full.mp4", 900_000_000),
            _probe(3000.0, width=640, height=352, bitrate=2_000_000, codec="mpeg4"),
        )
        truncated = (
            _file(2, "e:/v/short.mp4", 1_500_000_000),
            _probe(2400.0, width=1280, height=720, bitrate=5_000_000, codec="h264"),
        )

        assert _keeper_id(complete, truncated) == 1

    def test_a_few_seconds_missing_is_not_a_reason_to_lose(self) -> None:
        # 0.2% shorter with equal quality: the longer copy still wins, because
        # duration is the last tiebreak when nothing else separates them
        long_copy = (_file(1, "e:/v/a.mp4"), _probe(3000.0))
        short_copy = (_file(2, "e:/v/b.mp4"), _probe(2994.0))

        assert _keeper_id(long_copy, short_copy) == 1

    def test_the_exchange_rate_is_configurable(self) -> None:
        # A 2.5-stop quality lead outweighs 1.1% of length at the default
        # rate, and loses once a percent costs more than that lead
        assert _keeper_id(HIGH_QUALITY, LONGER_LOW_QUALITY) == 1
        strict = KeeperConfig(length_penalty_per_percent=5.0)
        assert _keeper_id(HIGH_QUALITY, LONGER_LOW_QUALITY, strict) == 2

    def test_zero_makes_length_only_a_tiebreak(self) -> None:
        config = KeeperConfig(length_penalty_per_percent=0.0)

        assert _keeper_id(HIGH_QUALITY, LONGER_LOW_QUALITY, config) == 1

    def test_a_copy_that_does_not_decode_cannot_win(self) -> None:
        broken = (_file(1, "e:/v/broken.mp4"), _probe(2637.0, status="failed"))
        good = (_file(2, "e:/v/good.mp4"), _probe(2607.0))

        assert _keeper_id(broken, good) == 2

    def test_a_preferred_folder_breaks_an_otherwise_even_tie(self) -> None:
        left = (_file(1, "e:/media/a.mp4"), _probe(2600.0))
        right = (_file(2, "e:/archive/a.mp4"), _probe(2600.0))

        assert _keeper_id(left, right) == 1
        assert _keeper_id(left, right, KeeperConfig(preferred_path_keywords=["archive"])) == 2


class TestQualityStops:
    def test_bitrate_doubling_is_one_stop(self) -> None:
        low = _quality_stops({}, _probe(100.0, bitrate=2_000_000), KeeperConfig())
        high = _quality_stops({}, _probe(100.0, bitrate=4_000_000), KeeperConfig())

        assert high - low == pytest.approx(1.0, abs=0.02)

    def test_bitrate_outweighs_resolution(self) -> None:
        # Spec: a dense 720p encode must not lose to a thin 4K one
        dense = _quality_stops(
            {}, _probe(100.0, width=1280, height=720, bitrate=15_000_000), KeeperConfig()
        )
        thin = _quality_stops(
            {}, _probe(100.0, width=3840, height=2160, bitrate=8_000_000), KeeperConfig()
        )

        assert dense > thin

    def test_16x_the_pixels_is_one_stop(self) -> None:
        small = _quality_stops({}, _probe(100.0, width=640, height=360), KeeperConfig())
        large = _quality_stops({}, _probe(100.0, width=2560, height=1440), KeeperConfig())

        assert large - small == pytest.approx(1.0, abs=0.02)

    def test_pixels_stop_counting_above_4k(self) -> None:
        # An 8K upscale must not outrank a 4K original on declared size alone
        four_k = _quality_stops({}, _probe(100.0, width=3840, height=2160), KeeperConfig())
        eight_k = _quality_stops({}, _probe(100.0, width=7680, height=4320), KeeperConfig())

        assert eight_k == four_k

    def test_a_missing_probe_scores_nothing(self) -> None:
        assert _quality_stops({}, None, KeeperConfig()) == 0.0

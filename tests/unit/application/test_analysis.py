"""Unit tests for pair analysis: keeper scoring."""

from typing import Any

import pytest

from akin_video_finder.application.analysis import (
    _member_key,
    _parse_fps,
)
from akin_video_finder.configuration.models import KeeperConfig


def _probe(
    duration: float = 60.0,
    status: str = "success",
    width: int = 1280,
    height: int = 720,
    fps: str = "25/1",
    bitrate: int = 2_000_000,
    vcodec: str = "h264",
    acodec: str = "aac",
    channels: int = 2,
    sample_rate: int = 48000,
) -> dict[str, Any]:
    return {
        "status": status,
        "container_duration": duration,
        "container_bitrate": bitrate,
        "primary_video_codec": vcodec,
        "primary_video_width": width,
        "primary_video_height": height,
        "primary_video_fps": fps,
        "primary_audio_codec": acodec,
        "primary_audio_channels": channels,
        "primary_audio_sample_rate": sample_rate,
    }


def _member(file_id: int, name: str, size: int = 100) -> dict[str, Any]:
    return {"id": file_id, "file_name": name, "original_path": f"/v/{name}", "size": size}


class TestParseFps:
    def test_rational(self) -> None:
        assert _parse_fps("30000/1001") == pytest.approx(29.97, abs=0.01)

    def test_integer_rational(self) -> None:
        assert _parse_fps("25/1") == 25.0

    def test_zero_denominator(self) -> None:
        assert _parse_fps("0/0") == 0.0

    def test_plain_number(self) -> None:
        assert _parse_fps("24") == 24.0

    def test_none_and_garbage(self) -> None:
        assert _parse_fps(None) == 0.0
        assert _parse_fps("abc") == 0.0


class TestMemberKey:
    """Ranking follows spec section 15 priority order."""

    def test_completeness_dominates_a_partial_copy(self) -> None:
        keeper = KeeperConfig()
        # Half the runtime is a real truncation: it outweighs 27x the pixels
        short_hi = _member_key(
            _member(1, "a.mp4"), _probe(duration=50, width=3840, height=2160), 100.0, keeper
        )
        long_lo = _member_key(
            _member(2, "b.mp4"), _probe(duration=100, width=640, height=480), 100.0, keeper
        )
        assert long_lo > short_hi

    def test_decode_failure_penalized(self) -> None:
        keeper = KeeperConfig()
        ok = _member_key(_member(1, "a.mp4"), _probe(), 60.0, keeper)
        bad = _member_key(_member(2, "b.mp4"), _probe(status="error"), 60.0, keeper)
        assert ok > bad

    def test_resolution_outranks_density(self) -> None:
        """Higher resolution wins even when bitrate density is lower.

        A well-encoded 720p packs more bits per pixel than a typical 4K
        file, but the 4K original still carries more real detail and is
        the better keeper.
        """
        keeper = KeeperConfig()
        dense_sd = _member_key(
            _member(1, "a.mp4"),
            _probe(width=1280, height=720, bitrate=3_200_000),
            60.0,
            keeper,
        )
        real_uhd = _member_key(
            _member(2, "b.mp4"),
            _probe(width=3840, height=2160, bitrate=15_100_000),
            60.0,
            keeper,
        )
        assert real_uhd > dense_sd

    def test_bitrate_outranks_resolution(self) -> None:
        """Bitrate is the primary quality signal: a dense 720p beats a
        thin 4K encode."""
        keeper = KeeperConfig()
        dense_sd = _member_key(
            _member(1, "a.mp4"),
            _probe(width=1280, height=720, bitrate=15_000_000),
            60.0,
            keeper,
        )
        thin_uhd = _member_key(
            _member(2, "b.mp4"),
            _probe(width=3840, height=2160, bitrate=8_000_000),
            60.0,
            keeper,
        )
        assert dense_sd > thin_uhd

    def test_density_decides_same_resolution(self) -> None:
        """Equal resolution: higher bits-per-megapixel wins."""
        keeper = KeeperConfig()
        low_bitrate = _member_key(
            _member(1, "a.mp4"),
            _probe(width=1920, height=1080, bitrate=4_000_000),
            60.0,
            keeper,
        )
        high_bitrate = _member_key(
            _member(2, "b.mp4"),
            _probe(width=1920, height=1080, bitrate=9_000_000),
            60.0,
            keeper,
        )
        assert high_bitrate > low_bitrate

    def test_no_probe_gets_zeroes(self) -> None:
        key = _member_key(_member(1, "a.mp4"), None, 60.0, KeeperConfig())
        # No decode, no quality evidence
        assert key[0] == 0.0
        assert key[1] == 0.0

    def test_preferred_extension_and_path(self) -> None:
        keeper = KeeperConfig(preferred_extensions=[".mp4"], preferred_path_keywords=["archive"])
        plain = _member_key(_member(1, "a.wmv", 100), _probe(), 60.0, KeeperConfig())
        preferred = _member_key(_member(2, "b.mp4", 100), _probe(), 60.0, keeper)
        pref_path = _member_key(
            {"id": 3, "file_name": "c.wmv", "original_path": "/archive/c.wmv", "size": 100},
            _probe(),
            60.0,
            keeper,
        )
        # Both preferred signals rank above a plain member with no prefs
        assert preferred[2] > 0
        assert pref_path[2] > 0
        assert plain[2] == 0

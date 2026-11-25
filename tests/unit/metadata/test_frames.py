"""Unit tests for frame extraction helpers."""

from akin_video_finder.metadata.frames import (
    frame_distance,
    is_blank_frame,
    sample_timestamps,
)


class TestFrameDistance:
    """Tests for normalized frame distance."""

    def test_identical_frames_zero(self) -> None:
        frame = bytes(range(256))
        assert frame_distance(frame, frame) == 0.0

    def test_max_distance(self) -> None:
        a = bytes([0] * 256)
        b = bytes([255] * 256)
        assert frame_distance(a, b) == 1.0

    def test_partial_distance(self) -> None:
        a = bytes([100] * 256)
        b = bytes([100] * 128) + bytes([110] * 128)
        # Half the pixels differ by 10/255
        expected = (128 * 10) / (256 * 255)
        assert abs(frame_distance(a, b) - expected) < 1e-9

    def test_mismatched_lengths(self) -> None:
        assert frame_distance(b"\x00" * 100, b"\x00" * 50) == 1.0
        assert frame_distance(b"", b"") == 1.0


class TestIsBlankFrame:
    """Tests for blank/uninformative frame detection."""

    def test_uniform_frame_is_blank(self) -> None:
        assert is_blank_frame(bytes([128] * 256))

    def test_empty_frame_is_blank(self) -> None:
        assert is_blank_frame(b"")

    def test_varied_frame_not_blank(self) -> None:
        assert not is_blank_frame(bytes(range(256)))


class TestSampleTimestamps:
    """Tests for duration-aware timestamp sampling."""

    def test_short_video_uses_minimum(self) -> None:
        ts = sample_timestamps(30.0, min_frames=8, max_frames=24)
        assert len(ts) == 8
        assert ts[0] >= 0
        assert ts[-1] < 30.0

    def test_long_video_capped(self) -> None:
        ts = sample_timestamps(3600.0, min_frames=8, max_frames=24)
        assert len(ts) == 24

    def test_medium_video_proportional(self) -> None:
        ts = sample_timestamps(600.0, min_frames=8, max_frames=24, target_interval_seconds=60.0)
        assert len(ts) == 10

    def test_timestamps_sorted_and_within_duration(self) -> None:
        ts = sample_timestamps(733.0, min_frames=8, max_frames=24)
        assert ts == sorted(ts)
        assert all(0 < t < 733.0 for t in ts)

    def test_zero_duration(self) -> None:
        ts = sample_timestamps(0.0)
        assert len(ts) == 8

"""Audio signature extraction and caching.

A window that never decodes used to be reported as an empty signature,
which the cache stored as "this file has no audio". Scan-time disk
contention made that happen to files that do have a track, and the
divergence gate then skipped them for good. These tests pin the
distinction: only ffprobe decides whether a file has audio, and a failed
decode is never cached.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from akin_video_finder.metadata import audio as audio_mod


@pytest.fixture()
def no_decode(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every window decode fails."""
    attempted: list[Path] = []

    def fake_decode(
        file_path: Path,
        start_seconds: float,
        window_seconds: float,
        sample_rate: int,
        ffmpeg_path: str,
        timeout_seconds: int,
    ) -> None:
        attempted.append(file_path)
        return None

    monkeypatch.setattr(audio_mod, "_decode_pcm", fake_decode)
    return attempted


@pytest.fixture()
def fixed_pcm(monkeypatch: pytest.MonkeyPatch):
    """Every window decodes to the same small tone."""

    def fake_decode(
        file_path: Path,
        start_seconds: float,
        window_seconds: float,
        sample_rate: int,
        ffmpeg_path: str,
        timeout_seconds: int,
    ) -> np.ndarray:
        t = np.arange(2048, dtype=np.float32) / sample_rate
        return np.sin(2.0 * np.pi * 440.0 * t)

    monkeypatch.setattr(audio_mod, "_decode_pcm", fake_decode)


class TestFailedDecode:
    def test_all_windows_failing_is_an_error_not_a_signature(self, no_decode: list[Path]) -> None:
        sig, windows, error = audio_mod.extract_audio_signature(
            Path("e:/vids/busy.mp4"), 300.0, windows=8
        )
        assert sig is None
        assert windows == 0
        assert error is not None
        assert "no window decoded" in error
        assert "busy.mp4" in error

    def test_the_extractor_stops_after_three_consecutive_failures(
        self, no_decode: list[Path]
    ) -> None:
        # A dead file must not cost one timeout per window.
        audio_mod.extract_audio_signature(Path("e:/vids/dead.mp4"), 3600.0, windows=8)
        assert len(no_decode) == 3


class TestSuccessfulDecode:
    def test_signature_is_one_band_vector_per_window(self, fixed_pcm: None) -> None:
        bands = 16
        sig, windows, error = audio_mod.extract_audio_signature(
            Path("e:/vids/ok.mp4"), 300.0, windows=8, bands=bands
        )
        assert error is None
        assert windows == 8
        assert sig is not None
        assert len(sig) == 8 * bands

    def test_windows_are_loudness_normalized_bytes(self, fixed_pcm: None) -> None:
        sig, windows, _ = audio_mod.extract_audio_signature(
            Path("e:/vids/ok.mp4"), 300.0, windows=8, bands=8
        )
        assert sig is not None
        values = np.frombuffer(sig, dtype=np.uint8)
        assert values.max() == 255  # the strongest band is pinned to full


class TestDistance:
    def test_empty_signature_never_matches(self) -> None:
        assert audio_mod.audio_distance(b"", b"\x00" * 64, 32) == 1.0
        assert audio_mod.audio_distance(b"\x00" * 64, b"", 32) == 1.0

    def test_identical_signatures_are_zero(self) -> None:
        sig = bytes(range(32))
        assert audio_mod.audio_distance(sig, sig, 32) == 0.0

    def test_shifted_signatures_compare_at_the_offset(self) -> None:
        bands = 4
        a = np.tile(np.array([10, 20, 30, 40], dtype=np.uint8), (8, 1))
        b = a.copy()

        assert (
            audio_mod.audio_distance_shifted(
                a.tobytes(), b.tobytes(), bands, 30.0, 30.0, 0.0, 2.0, 8
            )
            == 0.0
        )

    def test_shifted_signatures_detect_divergence(self) -> None:
        bands = 4
        a = np.tile(np.array([10, 20, 30, 40], dtype=np.uint8), (8, 1))
        b = np.tile(np.array([200, 210, 220, 230], dtype=np.uint8), (8, 1))

        distance = audio_mod.audio_distance_shifted(
            a.tobytes(), b.tobytes(), bands, 30.0, 30.0, 0.0, 2.0, 8
        )

        assert distance > 0.5


class TestWindowStarts:
    def test_windows_stay_inside_the_file(self) -> None:
        starts = audio_mod.audio_window_starts(600.0, 8, 2.0)
        assert len(starts) == 8
        assert starts[0] >= 0.0
        assert starts[-1] + 2.0 <= 600.0

    def test_short_file_collapses_to_one_window(self) -> None:
        starts = audio_mod.audio_window_starts(1.0, 8, 2.0)
        assert starts == [0.0]

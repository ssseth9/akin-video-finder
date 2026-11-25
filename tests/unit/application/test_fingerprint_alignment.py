"""Frame correspondence between fingerprints that do not hold the same samples.

The incident these tests pin down: two files 178 bytes apart, identical at
every timestamp both fingerprints sampled, scored 15%. One fingerprint had
been stored half-extracted (48 of 91 samples, covering 5%..52% of the film)
and the comparison scaled its frame *indices* across the other file's whole
timeline, pairing the middle of one video with the end of the other.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from akin_video_finder.application.perceptual_match import (
    CLASS_REJECTED,
    CLASS_VERY_LIKELY,
    FingerprintData,
    PerceptualMatchResult,
    PerceptualMatchService,
    _corresponding_frames,
    _fingerprint_is_complete,
    _usable_frames,
)
from akin_video_finder.configuration.models import MatchingConfig

SIZE = 16
FRAME_BYTES = SIZE * SIZE
# The pair from the reference library: 45:43, one sample per 30 s
DURATION = 2743.39
OTHER_DURATION = 2760.0


def _frame(seed: int) -> bytes:
    """Non-blank 16x16 pattern; far-apart seeds are far apart in distance."""
    return bytes((x * 13 + y * 7 + seed * 37) % 251 for y in range(SIZE) for x in range(SIZE))


BLANK = bytes(FRAME_BYTES)


class _RecordingRepository:
    """Stands in for the repository: records pair verdicts, answers nothing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_pair_comparison(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return len(self.calls)


def _service() -> tuple[PerceptualMatchService, _RecordingRepository]:
    repo = _RecordingRepository()
    service = PerceptualMatchService(repo, MatchingConfig())  # type: ignore[arg-type]
    return service, repo


def _file(file_id: int, duration: float = DURATION) -> dict[str, Any]:
    return {"id": file_id, "original_path": f"e:/vids/{file_id}.mp4", "_duration": duration}


def _grid(service: PerceptualMatchService, duration: float = DURATION) -> list[float]:
    return service._sample_grid(duration)


def _fingerprint(timestamps: list[float], seeds: list[int]) -> FingerprintData:
    frames = [_frame(seed) for seed in seeds]
    return FingerprintData(
        frame_count=len(frames),
        timestamps=tuple(timestamps),
        frames=b"".join(frames),
        frame_size=FRAME_BYTES,
    )


def _compare(
    service: PerceptualMatchService,
    repo: _RecordingRepository,
    fp_a: FingerprintData,
    fp_b: FingerprintData,
    duration_a: float = DURATION,
    duration_b: float = DURATION,
) -> dict[str, Any]:
    service._compare_pair(
        1,
        _file(1, duration_a),
        _file(2, duration_b),
        False,
        {1: fp_a, 2: fp_b},
        PerceptualMatchResult(),
    )
    assert len(repo.calls) == 1
    return repo.calls[0]


class TestCorrespondence:
    def test_a_truncated_fingerprint_is_not_stretched_across_the_timeline(self) -> None:
        # The exact shape of the incident: identical content, but B was
        # stored with only its first 48 samples
        service, repo = _service()
        grid = _grid(service)
        seeds = [i % 7 for i in range(len(grid))]
        fp_a = _fingerprint(grid, seeds)
        fp_b = _fingerprint(grid[:48], seeds[:48])

        call = _compare(service, repo, fp_a, fp_b)

        assert call["classification"] == CLASS_VERY_LIKELY
        assert call["match_fraction"] == 1.0
        # Only the frames both fingerprints have are compared — the old
        # index mapping compared 48 pairs too, but paired the wrong moments
        assert call["frames_compared"] == 48
        evidence = json.loads(call["evidence"])
        assert evidence["coverage_a"] == 1.0
        assert evidence["coverage_b"] == pytest.approx(47 / 90, abs=0.001)

    def test_a_dropped_middle_sample_shifts_nothing(self) -> None:
        service, repo = _service()
        grid = _grid(service)
        seeds = [i % 7 for i in range(len(grid))]
        missing = 45
        fp_a = _fingerprint(grid, seeds)
        fp_b = _fingerprint(
            [t for i, t in enumerate(grid) if i != missing],
            [s for i, s in enumerate(seeds) if i != missing],
        )

        call = _compare(service, repo, fp_a, fp_b)

        assert call["classification"] == CLASS_VERY_LIKELY
        assert call["match_fraction"] == 1.0
        # The frame without a counterpart is missing data, not a mismatch
        assert call["frames_compared"] == len(grid) - 1

    def test_a_one_sample_count_difference_still_aligns(self) -> None:
        # Durations 0.6% apart: same relative grid, one more sample in B
        service, repo = _service()
        fp_a = _fingerprint(_grid(service), [1] * 91)
        fp_b = _fingerprint(_grid(service, OTHER_DURATION), [1] * 92)

        call = _compare(service, repo, fp_a, fp_b, DURATION, OTHER_DURATION)

        assert call["match_fraction"] == 1.0
        assert call["frames_compared"] == 91

    def test_different_content_is_still_rejected(self) -> None:
        service, repo = _service()
        grid = _grid(service)
        fp_a = _fingerprint(grid, [i % 7 for i in range(len(grid))])
        fp_b = _fingerprint(grid, [(i % 7) + 40 for i in range(len(grid))])

        call = _compare(service, repo, fp_a, fp_b)

        assert call["classification"] == CLASS_REJECTED
        assert call["match_fraction"] < 0.7

    def test_frames_outside_the_other_timeline_are_skipped(self) -> None:
        # Fingerprints that share no sampled position (one covers the head,
        # the other the tail) have nothing to say about each other
        service, _ = _service()
        grid = _grid(service)
        fp_a = _fingerprint(grid[:40], [1] * 40)
        fp_b = _fingerprint(grid[-40:], [1] * 40)
        step = service._grid_step(DURATION)

        idx_a, idx_b = _corresponding_frames(fp_a, DURATION, fp_b, DURATION, step, step)

        assert idx_a.size == 0
        assert idx_b.size == 0


class TestFingerprintIntegrity:
    def test_a_failed_tail_makes_a_fingerprint_incomplete(self) -> None:
        # Decoding stopped at sample 60: the row would describe 61 of 91
        # samples while claiming to describe the file
        timestamps = _grid(_service()[0])
        frames: list[bytes | None] = [_frame(i) for i in range(61)] + [None] * 30
        kept_frames, kept_timestamps = _usable_frames(timestamps, frames)

        assert len(kept_frames) == 61
        assert not _fingerprint_is_complete(timestamps, frames, kept_timestamps)

    def test_a_blank_tail_is_a_real_tail(self) -> None:
        # End credits: the samples decoded, they just carry no signal. That
        # must not trigger a re-extraction on every scan
        timestamps = _grid(_service()[0])
        frames: list[bytes | None] = [_frame(i) for i in range(61)] + [BLANK] * 30
        kept_frames, kept_timestamps = _usable_frames(timestamps, frames)

        assert len(kept_frames) == 61
        assert _fingerprint_is_complete(timestamps, frames, kept_timestamps)

    def test_one_failed_last_sample_does_not_condemn_the_row(self) -> None:
        # A seek past a bogus container duration can lose the very last
        # sample on a healthy file; the span is still covered
        timestamps = _grid(_service()[0])
        frames: list[bytes | None] = [_frame(i) for i in range(90)] + [None]
        kept_frames, kept_timestamps = _usable_frames(timestamps, frames)

        assert len(kept_frames) == 90
        assert _fingerprint_is_complete(timestamps, frames, kept_timestamps)

    def test_a_failed_tail_is_retried_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = _service()
        timestamps = _grid(service)
        step = (timestamps[-1] - timestamps[0]) / (len(timestamps) - 1)

        def frame_for(timestamp: float) -> bytes:
            return _frame(int(round((timestamp - timestamps[0]) / step)))

        calls: list[list[float]] = []

        def fake_extract(
            file_path: Any, timestamps_seconds: list[float], **kwargs: Any
        ) -> list[bytes | None]:
            calls.append(list(timestamps_seconds))
            out: list[bytes | None] = [frame_for(t) for t in timestamps_seconds]
            if len(calls) == 1:
                out[60:] = [None] * (len(out) - 60)
            return out

        monkeypatch.setattr(
            "akin_video_finder.application.perceptual_match.extract_gray_frames", fake_extract
        )
        service._detect_crop = lambda file_row, duration: None  # type: ignore[method-assign]

        fp, error = service._extract_fingerprint(_file(1))

        assert error is None
        assert fp is not None
        assert fp.frame_count == len(timestamps)
        assert len(calls) == 4  # first pass + 31 tail samples in three batches

    def test_a_tail_that_never_decodes_is_an_error_not_a_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _ = _service()
        timestamps = _grid(service)
        cutoff = timestamps[60]

        def fake_extract(
            file_path: Any, timestamps_seconds: list[float], **kwargs: Any
        ) -> list[bytes | None]:
            return [_frame(i) if t < cutoff else None for i, t in enumerate(timestamps_seconds)]

        monkeypatch.setattr(
            "akin_video_finder.application.perceptual_match.extract_gray_frames", fake_extract
        )
        service._detect_crop = lambda file_row, duration: None  # type: ignore[method-assign]

        fp, error = service._extract_fingerprint(_file(1))

        assert fp is None
        assert error is not None
        assert "incomplete" in error

    def test_blank_tail_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = _service()
        timestamps = _grid(service)
        calls: list[int] = []

        def fake_extract(
            file_path: Any, timestamps_seconds: list[float], **kwargs: Any
        ) -> list[bytes | None]:
            calls.append(len(timestamps_seconds))
            return [_frame(i) for i in range(60)] + [BLANK] * (len(timestamps_seconds) - 60)

        monkeypatch.setattr(
            "akin_video_finder.application.perceptual_match.extract_gray_frames", fake_extract
        )
        service._detect_crop = lambda file_row, duration: None  # type: ignore[method-assign]

        fp, error = service._extract_fingerprint(_file(1))

        assert error is None
        assert fp is not None
        assert fp.frame_count == 60
        assert calls == [len(timestamps)]

    def test_a_half_stored_fingerprint_is_recomputed(self) -> None:
        service, _ = _service()
        timestamps = _grid(service)
        file_row = {**_file(1), "size": 10, "mtime_ns": 20}

        def cached(last: float) -> dict[str, Any]:
            return {
                "file_size": 10,
                "mtime_ns": 20,
                "algorithm_version": service._fp_version,
                "frame_count": 2,
                "timestamps": json.dumps([timestamps[0], last]),
                "frames": _frame(1) + _frame(2),
            }

        # 52% of the span: the row that produced the 15% verdict
        assert not service._cached_fingerprint_usable(cached(timestamps[47]), file_row)
        assert service._cached_fingerprint_usable(cached(timestamps[-1]), file_row)

    def test_a_changed_file_invalidates_the_cache(self) -> None:
        service, _ = _service()
        timestamps = _grid(service)
        cached = {
            "file_size": 11,
            "mtime_ns": 20,
            "algorithm_version": service._fp_version,
            "frame_count": 1,
            "timestamps": json.dumps([timestamps[0], timestamps[-1]]),
            "frames": _frame(1),
        }

        assert not service._cached_fingerprint_usable(
            cached, {**_file(1), "size": 10, "mtime_ns": 20}
        )

"""Trimmed-pair scoring: a match share, not a span of matched timestamps.

The incident these tests pin down: two unrelated 45-minute videos matched 12
of 88 frames — 6.4% of all frame pairs sit within the distance threshold, so
coincidences at one offset are expected — and those 12 matches, spread over
89% of the shorter timeline, were reported as an "89% match". PRODUCT_SPEC
section 6 asks for the share of the shorter video's frames that match.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from akin_video_finder.application.perceptual_match import (
    CLASS_POSSIBLE,
    CLASS_REJECTED,
    CLASS_VERY_LIKELY,
    FingerprintData,
    PerceptualMatchResult,
    PerceptualMatchService,
)
from akin_video_finder.configuration.models import MatchingConfig

SIZE = 16
FRAME_BYTES = SIZE * SIZE
# The pair from the reference library
SHORT_DURATION = 2647.313
LONG_DURATION = 2795.052


def _frame(seed: int) -> bytes:
    """Non-blank 16x16 pattern; different seeds are far apart in distance.

    Pseudo-random bytes: two different seeds sit ~0.33 apart, so only the
    pairs a test means to match can fall inside the distance threshold.
    """
    return random.Random(seed).randbytes(FRAME_BYTES)


class _RecordingRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_pair_comparison(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return len(self.calls)


def _service() -> tuple[PerceptualMatchService, _RecordingRepository]:
    repo = _RecordingRepository()
    return PerceptualMatchService(repo, MatchingConfig()), repo  # type: ignore[arg-type]


def _file(file_id: int, duration: float) -> dict[str, Any]:
    return {"id": file_id, "original_path": f"e:/vids/{file_id}.mp4", "_duration": duration}


def _fingerprint(timestamps: list[float], seeds: list[int]) -> FingerprintData:
    frames = [_frame(seed) for seed in seeds]
    return FingerprintData(
        frame_count=len(frames),
        timestamps=tuple(timestamps),
        frames=b"".join(frames),
        frame_size=FRAME_BYTES,
    )


def _compare_trimmed(
    service: PerceptualMatchService,
    repo: _RecordingRepository,
    fp_short: FingerprintData,
    fp_long: FingerprintData,
    short_duration: float,
    long_duration: float,
) -> dict[str, Any]:
    service._compare_trimmed(
        1,
        _file(1, short_duration),
        _file(2, long_duration),
        fp_short,
        fp_long,
        long_duration - short_duration,
        PerceptualMatchResult(),
    )
    assert len(repo.calls) == 1
    return repo.calls[0]


class TestTrimScoring:
    def test_scattered_matches_are_not_a_trim(self) -> None:
        # 12 coincidences at one offset, spread across the whole timeline:
        # the old span rule called this 89% and put it in front of a reviewer
        service, repo = _service()
        short_ts = service._sample_grid(SHORT_DURATION)
        long_ts = service._sample_grid(LONG_DURATION)
        assert len(short_ts) == 88 and len(long_ts) == 93

        matched_short = [4, 5, 6, 7, 8, 9, 10, 11, 45, 52, 85, 86]
        long_seeds = [1000 + j for j in range(len(long_ts))]
        for i in matched_short:
            j = min(range(len(long_ts)), key=lambda index: abs(long_ts[index] - short_ts[i]))
            long_seeds[j] = i

        fp_short = _fingerprint(short_ts, list(range(len(short_ts))))
        fp_long = _fingerprint(long_ts, long_seeds)

        call = _compare_trimmed(service, repo, fp_short, fp_long, SHORT_DURATION, LONG_DURATION)

        assert call["classification"] == CLASS_REJECTED
        assert call["match_fraction"] == pytest.approx(len(matched_short) / 88)
        evidence = json.loads(call["evidence"])
        assert evidence["matching_frames"] == len(matched_short)
        assert evidence["frames_compared"] == 88
        # The span the matches covered is still recorded — it is context,
        # not the match share
        assert evidence["short_coverage"] > 0.8

    def test_a_dense_trim_is_very_likely(self) -> None:
        # Every frame of the shorter copy matches at one offset, and the
        # shared portion spans most of the longer file as well
        service, repo = _service()
        short_ts = [30.0 + i * 36.0 for i in range(16)]
        long_ts = [30.0 + i * 36.0 for i in range(18)]

        fp_short = _fingerprint(short_ts, list(range(16)))
        fp_long = _fingerprint(long_ts, [*range(16), 200, 201])

        call = _compare_trimmed(service, repo, fp_short, fp_long, 600.0, 700.0)

        assert call["classification"] == CLASS_VERY_LIKELY
        assert call["match_fraction"] == 1.0
        evidence = json.loads(call["evidence"])
        assert evidence["matching_frames"] == 16
        assert evidence["long_coverage"] >= 0.7

    def test_a_dense_but_short_overlap_is_possible(self) -> None:
        # A 5-minute copy inside a 17-minute film: all of its frames match,
        # but the shared portion covers little of the longer file
        service, repo = _service()
        short_ts = [30.0 + i * 18.0 for i in range(16)]
        long_ts = [30.0 + i * 18.0 for i in range(33)]

        fp_short = _fingerprint(short_ts, list(range(16)))
        fp_long = _fingerprint(long_ts, [*range(16), *range(300, 317)])

        call = _compare_trimmed(service, repo, fp_short, fp_long, 300.0, 1000.0)

        assert call["classification"] == CLASS_POSSIBLE
        assert call["match_fraction"] == 1.0
        evidence = json.loads(call["evidence"])
        assert evidence["long_coverage"] < 0.7

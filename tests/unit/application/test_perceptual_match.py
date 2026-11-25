"""Unit tests for candidate blocking and the duration gate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from akin_video_finder.application.perceptual_match import (
    PerceptualMatchResult,
    PerceptualMatchService,
    SignatureData,
)
from akin_video_finder.configuration.models import MatchingConfig
from akin_video_finder.persistence.repository import DatabaseRepository

SIZE = 16
FRAME_BYTES = SIZE * SIZE
FRAMES = 12


def test_cache_versions_track_height_and_audio_window() -> None:
    """A changed frame height or audio window must invalidate the cache."""
    base = MatchingConfig()
    taller = replace(base, frame_height=base.frame_height + 1)
    longer_audio = replace(base, audio_window_seconds=base.audio_window_seconds + 0.5)
    no_crop = replace(base, crop_detect=False)
    repo = MagicMock()
    a = PerceptualMatchService(repo, base)
    b = PerceptualMatchService(repo, taller)
    c = PerceptualMatchService(repo, longer_audio)
    d = PerceptualMatchService(repo, no_crop)

    assert a._fp_version != b._fp_version
    assert a._sig_version != b._sig_version
    assert a._bag_version != b._bag_version
    assert a._audio_version != c._audio_version
    # Crop is applied to signature/fingerprint frames only. Head bags
    # decode uncropped windows, so their cache must survive the toggle.
    assert a._fp_version != d._fp_version
    assert a._sig_version != d._sig_version
    assert a._bag_version == d._bag_version


def _gradient(shift: int = 0) -> bytes:
    """16x16 frame with a left-to-right gradient, optionally shifted."""
    return bytes(min(255, (x + shift) * 16) for _y in range(SIZE) for x in range(SIZE))


def _noise(seed: int) -> bytes:
    """Deterministic pseudo-random frame."""
    out = bytearray(FRAME_BYTES)
    value = seed
    for i in range(FRAME_BYTES):
        value = (value * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = value >> 16 & 0xFF
    return bytes(out)


def _signature(frames: list[bytes], hashes: tuple[int, ...]) -> SignatureData:
    return SignatureData(
        frame_count=len(frames),
        timestamps=tuple(float(i) for i in range(len(frames))),
        frames=b"".join(frames),
        hashes=hashes,
        frame_size=FRAME_BYTES,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Any:
    r = DatabaseRepository(tmp_path / "test.db")
    yield r
    r.close()


class TestDurationGate:
    """A pair whose shorter copy is under ``min_duration_ratio`` is not compared.

    The gate is a work filter, not a display filter: it runs on what the
    indexes proposed, before a fingerprint, a frame distance or a shift probe
    is spent on the pair. That is the whole point — on the reference library
    half the comparisons were pairs the review list would hide anyway.
    """

    def _service(self, repo: DatabaseRepository, **overrides: Any) -> PerceptualMatchService:
        return PerceptualMatchService(repo, MatchingConfig(**overrides))

    def _files(self, dur_a: float, dur_b: float) -> list[dict[str, Any]]:
        return [
            {
                "id": fid,
                "original_path": f"e:/vids/{fid}.mp4",
                "normalized_path_key": f"e:/vids/{fid}.mp4",
                "_duration": duration,
            }
            for fid, duration in ((1, dur_a), (2, dur_b))
        ]

    @staticmethod
    def _identical() -> dict[int, SignatureData]:
        """Two files whose sparse hashes agree on every frame."""
        full = (1 << 64) - 1
        return {fid: _signature([_gradient()] * FRAMES, (full,) * FRAMES) for fid in (1, 2)}

    def test_pair_below_the_gate_is_never_compared(self, repo: DatabaseRepository) -> None:
        service = self._service(repo)
        service._key_by_id = {1: "e:/vids/1.mp4", 2: "e:/vids/2.mp4"}
        result = PerceptualMatchResult()
        pairs = service._generate_candidate_pairs(
            self._files(600.0, 1200.0), self._identical(), {}, result, None
        )
        assert pairs == []
        assert result.duration_pruned == 1
        # Not merely rejected: the pair never became a candidate, so the dense
        # stages that follow candidate generation never saw it either
        assert result.signature_pairs == 0

    def test_near_equal_lengths_still_compare(self, repo: DatabaseRepository) -> None:
        service = self._service(repo)
        service._key_by_id = {1: "e:/vids/1.mp4", 2: "e:/vids/2.mp4"}
        result = PerceptualMatchResult()
        pairs = service._generate_candidate_pairs(
            self._files(1200.0, 1300.0), self._identical(), {}, result, None
        )
        assert len(pairs) == 1
        assert result.duration_pruned == 0

    def test_a_lower_gate_keeps_the_same_pair(self, repo: DatabaseRepository) -> None:
        # The threshold is a setting, and the boundary is inclusive: a library
        # of scenes inside compilations runs this at ~0.5
        service = self._service(repo, min_duration_ratio=0.5)
        service._key_by_id = {1: "e:/vids/1.mp4", 2: "e:/vids/2.mp4"}
        result = PerceptualMatchResult()
        pairs = service._generate_candidate_pairs(
            self._files(600.0, 1200.0), self._identical(), {}, result, None
        )
        assert len(pairs) == 1
        assert result.duration_pruned == 0

    def test_an_unmeasurable_length_is_never_pruned(self, repo: DatabaseRepository) -> None:
        # The gate skips work that cannot pay off. A missing duration is not
        # evidence of a difference, so it must not hide a pair
        service = self._service(repo)
        service._key_by_id = {1: "e:/vids/1.mp4", 2: "e:/vids/2.mp4"}
        result = PerceptualMatchResult()
        pairs = service._generate_candidate_pairs(
            self._files(0.0, 1200.0), self._identical(), {}, result, None
        )
        assert len(pairs) == 1
        assert result.duration_pruned == 0

    def test_head_bag_candidate_is_pruned_before_the_probe(self, repo: DatabaseRepository) -> None:
        # The head-window index proposes pairs the sparse stages cannot see, so
        # it needs the gate too — a probe is the most expensive thing a scan
        # does with a pair
        service = self._service(repo, head_bag_min_shared=1)
        service._key_by_id = {1: "e:/vids/1.mp4", 2: "e:/vids/2.mp4"}
        full = (1 << 64) - 1
        flipped = full ^ 0x0001_0001_0001_0001
        signatures = {
            1: _signature([_gradient()] * FRAMES, (full,) * FRAMES),
            2: _signature([_noise(3)] * FRAMES, (flipped,) * FRAMES),
        }
        result = PerceptualMatchResult()
        pairs = service._generate_candidate_pairs(
            self._files(600.0, 1200.0),
            signatures,
            {},
            result,
            None,
            {1: (full,), 2: (full,)},
        )
        assert pairs == []
        assert result.duration_pruned == 1
        assert result.offset_probes_candidates == 0

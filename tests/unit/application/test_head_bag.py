"""Dense head-window hashes: the candidate path an advert cannot break.

Every candidate source before this one samples frames at fractions of each
file's own duration, so a copy that opens with an advert hands those samples
different content. Measured on the real shifted pair: zero shared average-hash
keys, and the four sparse keys they did share belong to buckets holding every
file in the library — the bucket guard drops them, so the pair never reached
the probe at all.

These tests pin the replacement signal — frames sampled *by second*, matched
without regard to position — its cache, and the ordering it feeds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from akin_video_finder.application import perceptual_match as pm
from akin_video_finder.application.perceptual_match import (
    PerceptualMatchResult,
    PerceptualMatchService,
)
from akin_video_finder.configuration.models import MatchingConfig
from akin_video_finder.persistence.repository import DatabaseRepository


class _BagRepo:
    """Head-bag store, in memory."""

    def __init__(self) -> None:
        self.bags: dict[int, dict[str, Any]] = {}
        self.writes: list[tuple[int, bytes]] = []

    def get_head_bag(self, file_id: int) -> dict[str, Any] | None:
        return self.bags.get(file_id)

    def upsert_head_bag(
        self,
        file_id: int,
        algorithm_version: str,
        window_seconds: float,
        hashes: bytes,
        file_size: int,
        mtime_ns: int,
    ) -> None:
        self.writes.append((file_id, hashes))
        self.bags[file_id] = {
            "file_id": file_id,
            "algorithm_version": algorithm_version,
            "window_seconds": window_seconds,
            "frame_count": len(hashes) // 8,
            "hashes": hashes,
            "file_size": file_size,
            "mtime_ns": mtime_ns,
        }


def _service(repo: Any = None, **overrides: Any) -> PerceptualMatchService:
    return PerceptualMatchService(repo or _BagRepo(), MatchingConfig(**overrides))  # type: ignore[arg-type]


def _row(file_id: int, size: int = 1000, mtime: int = 5, duration: float = 3000.0):
    return {
        "id": file_id,
        "original_path": f"e:/vids/{file_id}.mp4",
        "size": size,
        "mtime_ns": mtime,
        "_duration": duration,
    }


def _patch_decode(monkeypatch: pytest.MonkeyPatch, hashes: list[int]) -> None:
    """Fake ffmpeg: every frame carries its hash in its first byte."""
    monkeypatch.setattr(
        pm, "extract_gray_window", lambda *a, **k: [bytes([h]) * 256 for h in hashes]
    )
    monkeypatch.setattr(pm, "frame_ahash", lambda frame, width, height: int(frame[0]))
    monkeypatch.setattr(pm, "is_blank_frame", lambda frame, **kwargs: False)


def _hash(index: int) -> int:
    """A 64-bit hash with bits in every band, distinct per index."""
    return (index * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF


class TestHeadBagHits:
    """Which pairs the index proposes, and on what evidence."""

    def test_a_shifted_copy_still_collides(self) -> None:
        service = _service()
        content = [_hash(index) for index in range(100, 190)]

        # File 1 opens with 20 seconds of advert: same frames, 20s later
        hits = service._head_bag_hits({1: tuple([_hash(9999)] * 20 + content), 2: tuple(content)})

        # 90 shared frames, four 16-bit bands each
        assert hits[(1, 2)] == 4 * len(content)

    def test_a_pair_below_the_threshold_is_not_proposed(self) -> None:
        # Five shared frames are 20 shared bands, under the default of 24
        service = _service()
        shared = [_hash(index) for index in range(5)]

        hits = service._head_bag_hits({1: (*shared, _hash(9001)), 2: (*shared, _hash(9002))})

        assert hits == {}

    def test_a_frame_shared_by_the_whole_library_is_ignored(self) -> None:
        # Idents and black lead-ins appear everywhere; pairing all of them
        # with all of them would bury every real candidate
        service = _service(signature_max_bucket_files=2)
        bags = {
            file_id: (_hash(0), _hash(file_id), _hash(file_id + 100)) for file_id in range(1, 5)
        }

        assert service._head_bag_hits(bags) == {}

    def test_a_pair_inside_a_normal_bucket_is_proposed(self) -> None:
        service = _service(signature_max_bucket_files=200)
        content = [_hash(index) for index in range(7)]
        bags = {1: tuple(content), 2: tuple(content), 3: (_hash(500),)}

        assert service._head_bag_hits(bags) == {(1, 2): 28}

    def test_near_identical_frames_still_collide(self) -> None:
        # Two encodes of one film never agree bit-for-bit: bands keep the match
        # alive where whole-hash equality would miss entirely
        service = _service()
        content = [_hash(index) for index in range(30)]

        hits = service._head_bag_hits(
            {
                1: tuple(content),
                2: tuple(value ^ 0b1111 for value in content),
            }
        )

        # Four bits differ, so the three high bands still match
        assert hits[(1, 2)] >= 3 * len(content)

    def test_repeated_frames_do_not_inflate_the_evidence(self) -> None:
        # A still scene held for a minute is one shared frame, not sixty
        service = _service(head_bag_min_shared=5)

        hits = service._head_bag_hits({1: (_hash(7),) * 60, 2: (_hash(7),) * 60})

        assert hits == {}


class TestHeadBagCache:
    """One decode per file per version, and never a silent failure."""

    def test_a_cached_bag_is_reused_without_decoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _BagRepo()
        service = _service(repo)
        row = _row(1)
        payload = b"".join(value.to_bytes(8, "little") for value in (11, 12, 13))
        repo.bags[1] = {
            "file_id": 1,
            "algorithm_version": service._bag_version,
            "window_seconds": 90.0,
            "frame_count": 3,
            "hashes": payload,
            "file_size": row["size"],
            "mtime_ns": row["mtime_ns"],
        }
        monkeypatch.setattr(pm, "extract_gray_window", lambda *a, **k: pytest.fail("decoded again"))
        result = PerceptualMatchResult()

        bags = service._ensure_head_bags([row], result, None)

        assert bags[1] == (11, 12, 13)
        assert result.head_bag_cache_hits == 1
        assert result.files_head_bagged == 0

    def test_a_bag_from_another_version_is_recomputed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _BagRepo()
        service = _service(repo)
        row = _row(1)
        repo.bags[1] = {
            "file_id": 1,
            "algorithm_version": "headbag-v0",
            "window_seconds": 90.0,
            "frame_count": 1,
            "hashes": (99).to_bytes(8, "little"),
            "file_size": row["size"],
            "mtime_ns": row["mtime_ns"],
        }
        _patch_decode(monkeypatch, [5, 6, 7, 8, 9, 10, 11])
        result = PerceptualMatchResult()

        bags = service._ensure_head_bags([row], result, None)

        assert bags[1] == (5, 6, 7, 8, 9, 10, 11)
        assert result.files_head_bagged == 1
        assert result.head_bag_cache_hits == 0

    def test_a_changed_file_is_recomputed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _BagRepo()
        service = _service(repo)
        row = _row(1, size=2000)
        repo.bags[1] = {
            "file_id": 1,
            "algorithm_version": service._bag_version,
            "window_seconds": 90.0,
            "frame_count": 1,
            "hashes": (99).to_bytes(8, "little"),
            "file_size": 1000,
            "mtime_ns": row["mtime_ns"],
        }
        _patch_decode(monkeypatch, [1, 2, 3, 4, 5, 6, 7])

        service._ensure_head_bags([row], PerceptualMatchResult(), None)

        assert repo.writes and repo.writes[0][0] == 1

    def test_the_bag_is_stored_as_eight_byte_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _BagRepo()
        service = _service(repo)
        _patch_decode(monkeypatch, [5, 6, 7, 8, 9, 10, 11])

        result = PerceptualMatchResult()
        service._ensure_head_bags([_row(1)], result, None)

        expected = b"".join(value.to_bytes(8, "little") for value in (5, 6, 7, 8, 9, 10, 11))
        assert repo.writes == [(1, expected)]
        assert repo.bags[1]["frame_count"] == 7
        assert repo.bags[1]["algorithm_version"] == service._bag_version

    def test_a_failed_decode_is_reported_and_not_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Caching an empty bag would hide the file from every later scan, the
        # way a poisoned audio signature hid audio for good
        repo = _BagRepo()
        service = _service(repo)
        monkeypatch.setattr(pm, "extract_gray_window", lambda *a, **k: [])
        result = PerceptualMatchResult()

        bags = service._ensure_head_bags([_row(1)], result, None)

        assert bags == {}
        assert repo.writes == []
        assert any("Head window failed" in message for message in result.errors)

    def test_the_window_stops_at_the_end_of_a_short_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_window(*args: Any, **kwargs: Any) -> list[bytes]:
            seen.update(kwargs)
            return [bytes([h]) * 256 for h in range(1, 8)]

        monkeypatch.setattr(pm, "extract_gray_window", fake_window)
        monkeypatch.setattr(pm, "frame_ahash", lambda frame, width, height: int(frame[0]))
        monkeypatch.setattr(pm, "is_blank_frame", lambda frame, **kwargs: False)
        service = _service()

        service._extract_head_bag(_row(1, duration=12.0))

        assert seen["start_seconds"] == 0.0
        assert seen["window_seconds"] == 12.0


class TestHeadBagStorage:
    """The cache has to survive a restart, or every scan re-decodes."""

    def test_a_bag_survives_a_round_trip(self, tmp_path: Path) -> None:
        repo = DatabaseRepository(tmp_path / "headbag.db")
        # Foreign keys are enforced, so the file needs its root and scan
        repo.conn.execute(
            "INSERT INTO configured_roots (id, original_path, normalized_path_key,"
            " created_at, updated_at) VALUES (1, 'e:/v', 'e:/v', 'now', 'now')"
        )
        repo.conn.execute(
            "INSERT INTO scan_runs (id, start_time, status, application_version)"
            " VALUES (1, 'now', 'completed', 'test')"
        )
        repo.conn.execute(
            "INSERT INTO files (id, root_id, original_path, normalized_path_key,"
            " file_name, extension, size, mtime_ns, presence, first_seen_scan_id,"
            " last_seen_scan_id, created_at, updated_at) VALUES"
            " (1, 1, 'e:/v/a.mp4', 'e:/v/a.mp4', 'a.mp4', '.mp4', 10, 20, 'present',"
            " 1, 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        payload = b"".join(value.to_bytes(8, "little") for value in (7, 8, 9))

        repo.upsert_head_bag(1, "headbag-v1-s90-f1-w16", 90.0, payload, 10, 20)
        row = repo.get_head_bag(1)

        assert row is not None
        assert row["hashes"] == payload
        assert row["frame_count"] == 3
        assert row["algorithm_version"] == "headbag-v1-s90-f1-w16"
        assert repo.get_head_bag(2) is None
        repo.conn.close()

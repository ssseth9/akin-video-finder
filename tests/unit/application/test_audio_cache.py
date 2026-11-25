"""Audio signature caching across scans.

A decode that fails under load must not be remembered as "this file has
no audio": that verdict is permanent (it is keyed on size + mtime) and it
made the audio divergence gate skip those files for good. ffprobe owns
the has-a-track question; the cache only ever holds real windows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from akin_video_finder.application.perceptual_match import (
    PerceptualMatchResult,
    PerceptualMatchService,
)
from akin_video_finder.configuration.models import MatchingConfig
from akin_video_finder.persistence.repository import DatabaseRepository

BANDS = 32
WINDOWS = 8
SIGNATURE = bytes([7]) * (BANDS * WINDOWS)
SIZE = 1000
MTIME_NS = 1000


@pytest.fixture()
def repo(tmp_path: Path) -> Any:
    r = DatabaseRepository(tmp_path / "test.db")
    yield r
    r.close()


@pytest.fixture()
def file_ids(repo: DatabaseRepository, tmp_path: Path) -> tuple[int, int]:
    """Two real file rows — audio_fingerprints has a foreign key."""
    scan_id = repo.create_scan("0.1.0")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    ids = []
    for name in ("a.mp4", "b.mp4"):
        path = tmp_path / name
        path.write_bytes(b"x")
        ids.append(
            repo.create_file(
                root_id=root_id,
                original_path=str(path),
                normalized_path_key=str(path).lower(),
                file_name=path.name,
                extension=path.suffix,
                size=SIZE,
                mtime_ns=MTIME_NS,
                scan_id=scan_id,
            )
        )
    repo.commit()
    return ids[0], ids[1]


def _row(file_id: int, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": file_id,
        "original_path": f"e:/vids/{file_id}.mp4",
        "normalized_path_key": f"e:/vids/{file_id}.mp4",
        "size": SIZE,
        "mtime_ns": MTIME_NS,
        "_duration": 600.0,
        "_audio_stream_count": 1,
    }
    row.update(extra)
    return row


@pytest.fixture()
def decoded(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the ffmpeg-backed per-file extractor with a counting stub."""

    def fake_extract(self: Any, file_row: dict[str, Any]) -> tuple[bytes | None, int, str | None]:
        decoded.append(file_row["id"])
        return SIGNATURE, WINDOWS, None

    decoded: list[int] = []
    monkeypatch.setattr(PerceptualMatchService, "_extract_audio", fake_extract)
    return decoded


class TestCacheAcceptance:
    def test_entries_with_real_windows_are_reused(
        self, repo: DatabaseRepository, file_ids: tuple[int, int], decoded: list[int]
    ) -> None:
        service = PerceptualMatchService(repo, MatchingConfig())
        a, b = file_ids
        repo.upsert_audio_fingerprint(
            a, service._audio_version, WINDOWS, BANDS, SIGNATURE, SIZE, MTIME_NS
        )
        repo.commit()

        sigs = service._ensure_audio([(_row(a), _row(b), False)], PerceptualMatchResult(), None)

        assert sigs[a] == SIGNATURE
        assert decoded == [b]

    def test_a_zero_window_entry_is_not_trusted(
        self, repo: DatabaseRepository, file_ids: tuple[int, int], decoded: list[int]
    ) -> None:
        service = PerceptualMatchService(repo, MatchingConfig())
        a, b = file_ids
        # What a contended scan used to leave behind: an empty signature
        # recorded as a successful extraction.
        repo.upsert_audio_fingerprint(a, service._audio_version, 0, BANDS, b"", SIZE, MTIME_NS)
        repo.commit()

        sigs = service._ensure_audio([(_row(a), _row(b), False)], PerceptualMatchResult(), None)

        assert sigs[a] == SIGNATURE
        assert sorted(decoded) == sorted([a, b])

    def test_a_file_without_an_audio_stream_is_never_decoded(
        self, repo: DatabaseRepository, file_ids: tuple[int, int], decoded: list[int]
    ) -> None:
        service = PerceptualMatchService(repo, MatchingConfig())
        a, b = file_ids
        silent = _row(a, _audio_stream_count=0)

        result = PerceptualMatchResult()
        sigs = service._ensure_audio([(silent, _row(b), False)], result, None)

        assert sigs[a] == b""
        assert sigs[b] == SIGNATURE
        assert decoded == [b]
        assert result.fingerprint_errors == 0

    def test_a_stale_size_forces_re_extraction(
        self, repo: DatabaseRepository, file_ids: tuple[int, int], decoded: list[int]
    ) -> None:
        service = PerceptualMatchService(repo, MatchingConfig())
        a, b = file_ids
        repo.upsert_audio_fingerprint(
            a, service._audio_version, WINDOWS, BANDS, SIGNATURE, SIZE - 1, MTIME_NS
        )
        repo.commit()

        service._ensure_audio([(_row(a), _row(b), False)], PerceptualMatchResult(), None)

        assert a in decoded


class TestFailedExtractionIsNotCached:
    def test_an_error_leaves_no_row_behind(
        self,
        repo: DatabaseRepository,
        file_ids: tuple[int, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = PerceptualMatchService(repo, MatchingConfig())
        a, b = file_ids

        def failing(self: Any, file_row: dict[str, Any]) -> tuple[bytes | None, int, str | None]:
            return None, 0, f"Audio signature failed for {file_row['original_path']}"

        monkeypatch.setattr(PerceptualMatchService, "_extract_audio", failing)

        result = PerceptualMatchResult()
        sigs = service._ensure_audio([(_row(a), _row(b), False)], result, None)

        assert sigs == {}
        assert result.fingerprint_errors == 2
        assert repo.get_audio_fingerprint(a) is None
        assert repo.get_audio_fingerprint(b) is None

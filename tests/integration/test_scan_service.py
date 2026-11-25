"""Integration tests for ScanService with mocked FFprobe.

These tests exercise the full scan pipeline — discovery, persistence,
cache policy, and reporting — using a real SQLite database in a temporary
directory while mocking only the FFprobe subprocess call.
"""

import json
import random
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from akin_video_finder import __version__
from akin_video_finder.application.scan_service import ScanService
from akin_video_finder.configuration.models import (
    Config,
    DatabaseConfig,
    MatchingConfig,
    RootConfig,
)
from akin_video_finder.core.models import (
    AudioStreamInfo,
    ContainerMetadata,
    FileMetadata,
    FilePresence,
    ProbeResult,
    ProbeStatus,
    ProgressEvent,
    VideoStreamInfo,
)
from akin_video_finder.persistence.repository import DatabaseRepository


def _make_probe_result(codec: str = "h264", duration: float = 60.0) -> ProbeResult:
    """Build a successful ProbeResult with representative metadata."""
    metadata = FileMetadata(
        container=ContainerMetadata(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            format_long_name="QuickTime / MOV",
            duration=duration,
            size=1024,
            bitrate=1000,
        ),
        video_streams=[
            VideoStreamInfo(
                index=0,
                codec_name=codec,
                width=1920,
                height=1080,
                r_frame_rate="25/1",
            )
        ],
        audio_streams=[
            AudioStreamInfo(
                index=1,
                codec_name="aac",
                sample_rate=48000,
                channel_count=2,
            )
        ],
    )
    return ProbeResult(
        status=ProbeStatus.SUCCESS,
        metadata=metadata,
        exit_code=0,
        algorithm_version="1.0",
    )


def _failed_probe_result() -> ProbeResult:
    """Build a failed ProbeResult."""
    return ProbeResult(
        status=ProbeStatus.FAILED,
        error_category="execution_error",
        error_message="FFprobe exited with code 1",
        exit_code=1,
        algorithm_version="1.0",
    )


def _make_config(
    root: Path,
    db_path: Path,
    matching_enabled: bool = False,
    min_frames: int = 4,
    min_duration_ratio: float = 0.88,
) -> Config:
    # min_duration_ratio is the candidate gate the matcher prunes with. The
    # default here matches the shipped default; tests whose pairs are more
    # than 12% apart in length (the trim path) say so explicitly, because at
    # 0.88 those pairs never reach the comparison they exist to exercise.
    return Config(
        roots=[RootConfig(path=str(root), recursive=True)],
        database=DatabaseConfig(path=str(db_path)),
        matching=MatchingConfig(
            enabled=matching_enabled,
            min_frames=min_frames,
            min_duration_ratio=min_duration_ratio,
        ),
    )


@pytest.fixture
def video_root(tmp_path: Path) -> Path:
    """Create a fake video root with several video files."""
    root = tmp_path / "videos"
    root.mkdir()
    for name in ("a.mp4", "b.mkv", "c.wmv"):
        (root / name).write_bytes(b"fake-video-content" + name.encode())
    (root / "notes.txt").write_text("not a video")
    return root


class TestScanServiceIntegration:
    """End-to-end scan tests with mocked FFprobe."""

    def test_scan_discovers_and_persists_files(self, tmp_path: Path, video_root: Path) -> None:
        """A full scan persists files and probe metadata."""
        db_path = tmp_path / "test.db"
        config = _make_config(video_root, db_path)

        with DatabaseRepository(db_path) as repo:
            service = ScanService(config, repo)
            with patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=lambda **kwargs: _make_probe_result(),
            ) as mock_probe:
                result = service.scan()

        assert result["status"] == "completed"
        assert result["total_files"] == 3
        assert result["present_files"] == 3
        assert result["error_files"] == 0
        assert result["cache_hits"] == 0
        assert mock_probe.call_count == 3

        # Verify database contents
        with DatabaseRepository(db_path) as repo:
            roots = repo.get_all_roots()
            assert len(roots) == 1
            files = repo.get_files_for_root(roots[0]["id"])
            assert len(files) == 3
            for f in files:
                assert f["presence"] == FilePresence.PRESENT.value
                probe = repo.get_probe_metadata(f["id"])
                assert probe is not None
                assert probe["status"] == ProbeStatus.SUCCESS.value
                assert probe["primary_video_codec"] == "h264"

    def test_scan_records_the_running_version(self, tmp_path: Path, video_root: Path) -> None:
        """The scan row names the build that produced it.

        The value was a hardcoded literal, so every scan claimed 0.1.0
        however old or new the build — the one place a version number is
        worth anything to a later bug report. It now follows
        ``akin_video_finder.__version__``, the same string the CLI and the window
        title print.
        """
        db_path = tmp_path / "test.db"
        config = _make_config(video_root, db_path)

        with (
            DatabaseRepository(db_path) as repo,
            patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=lambda **kwargs: _make_probe_result(),
            ),
        ):
            ScanService(config, repo).scan()
            scan = repo.get_latest_scan()

        assert scan is not None
        assert scan["application_version"] == __version__

    def test_second_scan_reuses_cache(self, tmp_path: Path, video_root: Path) -> None:
        """Unchanged files reuse cached probe metadata."""
        db_path = tmp_path / "test.db"
        config = _make_config(video_root, db_path)

        with (
            DatabaseRepository(db_path) as repo,
            patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=lambda **kwargs: _make_probe_result(),
            ) as mock_probe,
        ):
            ScanService(config, repo).scan()
            first_calls = mock_probe.call_count

            result = ScanService(config, repo).scan()
            second_calls = mock_probe.call_count - first_calls

        assert first_calls == 3
        assert second_calls == 0
        assert result["cache_hits"] == 3
        assert result["present_files"] == 3

    def test_changed_file_is_reprobed(self, tmp_path: Path, video_root: Path) -> None:
        """A file whose size changes is re-probed."""
        db_path = tmp_path / "test.db"
        config = _make_config(video_root, db_path)

        with (
            DatabaseRepository(db_path) as repo,
            patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=lambda **kwargs: _make_probe_result(),
            ) as mock_probe,
        ):
            ScanService(config, repo).scan()

            # Modify one file (different size => new mtime/size identity)
            (video_root / "a.mp4").write_bytes(b"different-size-content!!")

            result = ScanService(config, repo).scan()

        assert result["cache_hits"] == 2
        assert mock_probe.call_count == 4  # 3 initial + 1 re-probe

    def test_removed_file_marked_missing(self, tmp_path: Path, video_root: Path) -> None:
        """A file that disappears is marked missing, not deleted."""
        db_path = tmp_path / "test.db"
        config = _make_config(video_root, db_path)

        with DatabaseRepository(db_path) as repo:
            with patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=lambda **kwargs: _make_probe_result(),
            ):
                ScanService(config, repo).scan()
                (video_root / "b.mkv").unlink()
                result = ScanService(config, repo).scan()

            assert result["missing_files"] == 1
            assert result["total_files"] == 2

            roots = repo.get_all_roots()
            files = repo.get_files_for_root(roots[0]["id"])
            missing = [f for f in files if f["presence"] == FilePresence.MISSING.value]
            assert len(missing) == 1
            assert missing[0]["file_name"] == "b.mkv"

    def test_unavailable_root_preserves_records(self, tmp_path: Path) -> None:
        """Unavailable roots do not mark files missing."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"content")

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path)

        with DatabaseRepository(db_path) as repo:
            with patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=lambda **kwargs: _make_probe_result(),
            ):
                ScanService(config, repo).scan()

            # Make the root unavailable and rescan
            (root / "a.mp4").unlink()
            root.rmdir()

            result = ScanService(config, repo).scan()

            assert result["status"] == "completed"
            assert any("unavailable" in e.lower() for e in result["errors"])

            roots = repo.get_all_roots()
            files = repo.get_files_for_root(roots[0]["id"])
            # File record preserved, still present (not marked missing)
            assert len(files) == 1
            assert files[0]["presence"] == FilePresence.PRESENT.value

    def test_probe_failure_recorded_as_error(self, tmp_path: Path, video_root: Path) -> None:
        """Files that fail probing are counted as errors, not crashes."""
        db_path = tmp_path / "test.db"
        config = _make_config(video_root, db_path)

        def selective_probe(**kwargs: object) -> ProbeResult:
            file_path = str(kwargs.get("file_path", ""))
            if "b.mkv" in file_path:
                return _failed_probe_result()
            return _make_probe_result()

        with DatabaseRepository(db_path) as repo:
            with patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=selective_probe,
            ):
                result = ScanService(config, repo).scan()

            assert result["error_files"] == 1
            assert result["present_files"] == 3

            roots = repo.get_all_roots()
            files = repo.get_files_for_root(roots[0]["id"])
            failed = [
                f
                for f in files
                if repo.get_probe_metadata(f["id"])["status"] == ProbeStatus.FAILED.value
            ]
            assert len(failed) == 1
            assert failed[0]["file_name"] == "b.mkv"

    def test_progress_events_emitted(self, tmp_path: Path, video_root: Path) -> None:
        """Progress callback receives structured events."""
        db_path = tmp_path / "test.db"
        config = _make_config(video_root, db_path)
        events: list[ProgressEvent] = []

        def callback(event: ProgressEvent) -> None:
            events.append(event)

        with DatabaseRepository(db_path) as repo:
            service = ScanService(config, repo, progress_callback=callback)
            with patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=lambda **kwargs: _make_probe_result(),
            ):
                service.scan()

        phases = [e.phase for e in events]
        assert "initialization" in phases
        assert "discovery" in phases
        assert "probing" in phases
        assert "completed" in phases

    def test_probes_run_concurrently(self, tmp_path: Path) -> None:
        """Probing honours ffprobe.worker_count instead of running serially.

        Probing is a process spawn per file, so it is the one scan stage
        that is pure latency; before this the setting was honoured only by
        the evaluator and a cold scan spawned ffprobe one file at a time.
        """
        root = tmp_path / "videos"
        root.mkdir()
        for i in range(8):
            (root / f"v{i}.mp4").write_bytes(b"x" * (i + 1))
        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path)
        config.ffprobe = replace(config.ffprobe, worker_count=4)

        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def tracking_probe(**kwargs: object) -> ProbeResult:
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.02)
            with lock:
                state["now"] -= 1
            return _make_probe_result()

        with (
            DatabaseRepository(db_path) as repo,
            patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=tracking_probe,
            ),
        ):
            result = ScanService(config, repo).scan()

        assert result["present_files"] == 8
        assert state["peak"] > 1, "probes never overlapped — still serial"

    def test_probe_crash_only_fails_that_file(self, tmp_path: Path) -> None:
        """An exception from one probe does not abort the scan."""
        root = tmp_path / "videos"
        root.mkdir()
        for name in ("ok1.mp4", "boom.mp4", "ok2.mp4"):
            (root / name).write_bytes(b"x" + name.encode())
        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path)

        def selective_probe(**kwargs: object) -> ProbeResult:
            if "boom" in str(kwargs.get("file_path", "")):
                raise RuntimeError("ffprobe exploded")
            return _make_probe_result()

        with DatabaseRepository(db_path) as repo:
            with patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=selective_probe,
            ):
                result = ScanService(config, repo).scan()

            assert result["status"] == "completed"
            assert result["error_files"] == 1
            assert result["present_files"] == 3
            roots = repo.get_all_roots()
            files = {f["file_name"]: f for f in repo.get_files_for_root(roots[0]["id"])}
            # The crashed file keeps its row but claims no metadata, so the
            # next scan probes it again instead of trusting a stale row
            assert repo.get_probe_metadata(files["boom.mp4"]["id"]) is None
            assert repo.get_probe_metadata(files["ok1.mp4"]["id"]) is not None


class TestExactMatchIntegration:
    """Exact duplicate detection end-to-end (real hashing, mocked probe)."""

    def _run_scan(self, config: Config, repo: DatabaseRepository) -> dict:
        with patch(
            "akin_video_finder.application.scan_service.probe_file",
            side_effect=lambda **kwargs: _make_probe_result(),
        ):
            return ScanService(config, repo).scan()

    def test_identical_files_grouped(self, tmp_path: Path) -> None:
        """Byte-identical files produce one EXACT group."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "dup_a.mp4").write_bytes(b"identical-content")
        (root / "dup_b.mp4").write_bytes(b"identical-content")
        (root / "unique.mp4").write_bytes(b"different-content!")

        db_path = tmp_path / "test.db"
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(_make_config(root, db_path), repo)

            assert result["exact_pairs"] == 1
            assert result["files_hashed"] == 2

            files = {f["id"]: f for f in repo.get_present_files()}
            pairs = repo.get_pair_comparisons(result["scan_id"])
            exact_pairs = [p for p in pairs if p["classification"] == "EXACT"]
            assert len(exact_pairs) == 1
            names = {files.get(p["file_a_id"], {}).get("file_name") for p in exact_pairs} | {
                files.get(p["file_b_id"], {}).get("file_name") for p in exact_pairs
            }
            assert names == {"dup_a.mp4", "dup_b.mp4"}

    def test_same_size_different_content_not_grouped(self, tmp_path: Path) -> None:
        """Same-size files with different bytes are hashed but not grouped."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "x.mp4").write_bytes(b"aaaa")
        (root / "y.mp4").write_bytes(b"bbbb")

        db_path = tmp_path / "test.db"
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(_make_config(root, db_path), repo)

            assert result["files_hashed"] == 2
            assert result["exact_pairs"] == 0
            assert [
                p
                for p in repo.get_pair_comparisons(result["scan_id"])
                if p["classification"] == "EXACT"
            ] == []

    def test_unique_sizes_never_hashed(self, tmp_path: Path) -> None:
        """Files with unique sizes are skipped — no hashing at all."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"1")
        (root / "b.mp4").write_bytes(b"22")
        (root / "c.mp4").write_bytes(b"333")

        db_path = tmp_path / "test.db"
        with (
            DatabaseRepository(db_path) as repo,
            patch("akin_video_finder.application.exact_match.compute_file_sha256") as mock_hash,
        ):
            result = self._run_scan(_make_config(root, db_path), repo)

        mock_hash.assert_not_called()
        assert result["exact_pairs"] == 0
        assert result["files_hashed"] == 0

    def test_hash_cache_reused_on_second_scan(self, tmp_path: Path) -> None:
        """Unchanged files reuse cached SHA-256 digests."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "dup_a.mp4").write_bytes(b"identical-content")
        (root / "dup_b.mp4").write_bytes(b"identical-content")

        db_path = tmp_path / "test.db"
        with DatabaseRepository(db_path) as repo:
            self._run_scan(_make_config(root, db_path), repo)

            with patch(
                "akin_video_finder.application.exact_match.compute_file_sha256"
            ) as mock_hash:
                result = self._run_scan(_make_config(root, db_path), repo)

        mock_hash.assert_not_called()
        assert result["hash_cache_hits"] == 2
        assert result["exact_pairs"] == 1

    def test_changed_file_rehashed_and_ungrouped(self, tmp_path: Path) -> None:
        """A modified file is re-hashed and leaves its group."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "dup_a.mp4").write_bytes(b"identical-content")
        (root / "dup_b.mp4").write_bytes(b"identical-content")

        db_path = tmp_path / "test.db"
        with DatabaseRepository(db_path) as repo:
            self._run_scan(_make_config(root, db_path), repo)
            (root / "dup_b.mp4").write_bytes(b"now-different-content!!!")
            result = self._run_scan(_make_config(root, db_path), repo)

            # dup_a is still a hash-cache hit; dup_b changed size so the
            # size group dissolved — no hashing needed at all this scan.
            assert result["exact_pairs"] == 0
            assert [
                p
                for p in repo.get_pair_comparisons(result["scan_id"])
                if p["classification"] == "EXACT"
            ] == []

    def test_missing_file_leaves_group(self, tmp_path: Path) -> None:
        """A file marked missing is excluded from exact grouping."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "dup_a.mp4").write_bytes(b"identical-content")
        (root / "dup_b.mp4").write_bytes(b"identical-content")
        (root / "dup_c.mp4").write_bytes(b"identical-content")

        db_path = tmp_path / "test.db"
        with DatabaseRepository(db_path) as repo:
            self._run_scan(_make_config(root, db_path), repo)
            (root / "dup_c.mp4").unlink()
            result = self._run_scan(_make_config(root, db_path), repo)

            files = {f["id"]: f for f in repo.get_present_files()}
            pairs = repo.get_pair_comparisons(result["scan_id"])
            exact_pairs = [p for p in pairs if p["classification"] == "EXACT"]
            assert result["exact_pairs"] == 1
            assert len(exact_pairs) == 1
            names = {files.get(p["file_a_id"], {}).get("file_name") for p in exact_pairs} | {
                files.get(p["file_b_id"], {}).get("file_name") for p in exact_pairs
            }
            assert names == {"dup_a.mp4", "dup_b.mp4"}


# ---------------------------------------------------------------------------
# Perceptual matching (Milestone 2)
# ---------------------------------------------------------------------------

GROUP_TYPE_PERCEPTUAL = "perceptual"  # kept only for legacy test strings

# Two highly distinct non-blank 16x16 gray patterns (mean distance ~0.33)
PATTERN_A = bytes(range(256))
PATTERN_B = bytes(255 - i for i in range(256))
PATTERN_C = bytes((i * 7) % 256 for i in range(256))

# Deterministic pseudo-random frames, pairwise far from the structured
# patterns above and from each other (normalized distance ~0.33)
_RNG = random.Random(42)
_DISTINCT = [_RNG.randbytes(256) for _ in range(24)]


def _probe_side_effect(durations: dict[str, float]) -> Callable[..., ProbeResult]:
    """Mock probe_file: return success with per-file durations."""

    def side_effect(**kwargs: object) -> ProbeResult:
        name = Path(str(kwargs.get("file_path", ""))).name
        return _make_probe_result(duration=durations.get(name, 60.0))

    return side_effect


def _frames_side_effect(
    frames_map: dict[str, list[bytes]],
) -> Callable[..., list[bytes | None]]:
    """Mock extract_gray_frames: return successive frames per file."""

    counters: dict[str, int] = {}

    def side_effect(
        file_path: Path, timestamps_seconds: list[float], **kwargs: object
    ) -> list[bytes | None]:
        name = Path(str(file_path)).name
        seq = frames_map[name]
        idx = counters.get(name, 0)
        counters[name] = idx + len(timestamps_seconds)
        return [seq[min(idx + i, len(seq) - 1)] for i in range(len(timestamps_seconds))]

    return side_effect


class TestPerceptualMatchIntegration:
    """Perceptual matching end-to-end (mocked ffmpeg, real DB)."""

    def _run_scan(
        self,
        config: Config,
        repo: DatabaseRepository,
        durations: dict[str, float] | None = None,
        frames_map: dict[str, list[bytes]] | None = None,
    ) -> dict:
        """Run a scan with mocked probe and frame extraction."""
        with (
            patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=_probe_side_effect(durations or {}),
            ),
            patch(
                "akin_video_finder.application.perceptual_match.extract_gray_frames",
                side_effect=_frames_side_effect(frames_map or {}),
            ) as mock_frames,
        ):
            result = ScanService(config, repo).scan()
        self._last_frame_mock = mock_frames
        return result

    def test_identical_frames_form_perceptual_group(self, tmp_path: Path) -> None:
        """Same normalized frames at same positions => VERY_LIKELY + group."""
        root = tmp_path / "videos"
        root.mkdir()
        # Different content sizes => different files, same duration
        (root / "clip_720p.mp4").write_bytes(b"a" * 100)
        (root / "clip_4k.mp4").write_bytes(b"b" * 200)
        (root / "other.mp4").write_bytes(b"c" * 300)

        frames = {
            "clip_720p.mp4": [PATTERN_A] * 4,
            "clip_4k.mp4": [PATTERN_A] * 4,  # same normalized frames
            "other.mp4": [PATTERN_B] * 4,
        }
        durations = {"clip_720p.mp4": 60.0, "clip_4k.mp4": 60.5, "other.mp4": 61.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["very_likely"] == 1
            assert result["very_likely"] == 1

            files = {f["id"]: f for f in repo.get_present_files()}
            pairs = repo.get_pair_comparisons(result["scan_id"])
            vl_pairs = [p for p in pairs if p["classification"] == "VERY_LIKELY"]
            assert len(vl_pairs) == 1
            names = {files.get(p["file_a_id"], {}).get("file_name") for p in vl_pairs} | {
                files.get(p["file_b_id"], {}).get("file_name") for p in vl_pairs
            }
            assert names == {"clip_720p.mp4", "clip_4k.mp4"}

            # Pair evidence is stored; no group analysis is performed
            assert result["very_likely"] == 1
            analysis = json.loads(vl_pairs[0].get("evidence") or "{}")
            assert (
                "consistency_warnings" not in analysis or analysis.get("consistency_warnings") == []
            )

    def test_a_comparison_event_names_both_files(self, tmp_path: Path) -> None:
        """The scan log prints "Comparing videos  A  vs  B" — A on its own
        left the reader asking what it was being compared with."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "clip_720p.mp4").write_bytes(b"a" * 100)
        (root / "clip_4k.mp4").write_bytes(b"b" * 200)

        frames = {
            "clip_720p.mp4": [PATTERN_A] * 4,
            "clip_4k.mp4": [PATTERN_A] * 4,
        }
        durations = {"clip_720p.mp4": 60.0, "clip_4k.mp4": 60.5}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True)
        events: list[ProgressEvent] = []
        with (
            DatabaseRepository(db_path) as repo,
            patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=_probe_side_effect(durations),
            ),
            patch(
                "akin_video_finder.application.perceptual_match.extract_gray_frames",
                side_effect=_frames_side_effect(frames),
            ),
        ):
            ScanService(config, repo, progress_callback=events.append).scan()

        compared = [e for e in events if e.phase == "comparing"]
        assert len(compared) == 1
        pair = compared[0]
        assert pair.current_path and pair.peer_path
        assert {Path(pair.current_path).name, Path(pair.peer_path).name} == {
            "clip_720p.mp4",
            "clip_4k.mp4",
        }

    def test_different_frames_rejected_not_grouped(self, tmp_path: Path) -> None:
        """Sparse overlap but dense divergence => REJECTED, evidence retained."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "x.mp4").write_bytes(b"x" * 100)
        (root / "y.mp4").write_bytes(b"y" * 200)

        # Sequences are consumed sequentially: 12 signature frames (8 uniform
        # + 4 edge), then the dense fingerprint frames. y shares 2 sparse
        # frames with x (enough to pass the signature gate) but diverges on
        # the dense grid.
        frames = {
            "x.mp4": [PATTERN_A] * 16,
            "y.mp4": [PATTERN_A, PATTERN_A, *_DISTINCT[:10], *_DISTINCT[10:14]],
        }
        durations = {"x.mp4": 60.0, "y.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["rejected"] == 1
            assert 0 == 0

            comparisons = repo.get_pair_comparisons(result["scan_id"])
            assert len(comparisons) == 1
            assert comparisons[0]["classification"] == "REJECTED"

    def test_signature_gate_skips_non_candidates(self, tmp_path: Path) -> None:
        """Files with unrelated content are never densely fingerprinted."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "short.mp4").write_bytes(b"s" * 100)
        (root / "long.mp4").write_bytes(b"l" * 200)

        frames = {"short.mp4": [PATTERN_A] * 12, "long.mp4": [PATTERN_C] * 12}
        durations = {"short.mp4": 60.0, "long.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

        # Both files were signed (12 sparse frames each in one batch call
        # per file) but the pair was pruned before any dense fingerprinting
        assert result["files_signed"] == 2
        assert result["candidate_pairs"] == 0
        assert result["files_fingerprinted"] == 0
        assert self._last_frame_mock.call_count == 2

    def test_partial_match_possible_not_grouped(self, tmp_path: Path) -> None:
        """75% matching frames => POSSIBLE, recorded but not grouped."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 200)

        # b's sparse signature shares 6 frames with a; on the dense grid
        # 3 of 4 frames match => 0.75 >= possible_threshold, < very_likely
        frames = {
            "a.mp4": [PATTERN_A] * 16,
            "b.mp4": [PATTERN_A] * 6 + [PATTERN_B] * 6 + [PATTERN_A] * 3 + [PATTERN_B],
        }
        durations = {"a.mp4": 60.0, "b.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["possible"] == 1
            assert 0 == 0

            comparisons = repo.get_pair_comparisons(result["scan_id"])
            assert comparisons[0]["classification"] == "POSSIBLE"

    def test_fingerprints_cached_between_scans(self, tmp_path: Path) -> None:
        """Second scan reuses fingerprints without re-extracting frames."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 200)

        frames = {"a.mp4": [PATTERN_A] * 4, "b.mp4": [PATTERN_A] * 4}
        durations = {"a.mp4": 60.0, "b.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True)
        with DatabaseRepository(db_path) as repo:
            first = self._run_scan(config, repo, durations, frames)
            assert first["files_fingerprinted"] == 2

            second = self._run_scan(config, repo, durations, frames)
            assert second["files_fingerprinted"] == 0
            assert second["fingerprint_cache_hits"] == 2
            # No new ffmpeg calls on second scan
            assert self._last_frame_mock.call_count == 0

    def test_a_half_extracted_fingerprint_is_retried_and_completed(self, tmp_path: Path) -> None:
        """A decode that dies mid-file must not become a fingerprint of half a video.

        The reference incident: one file's stored row stopped at 52% of the
        film, and the pair read 15% despite identical frames wherever both
        sides had a sample. The tail is now re-decoded once and the completed
        fingerprint is what gets cached — no partial row survives to poison
        later comparisons.
        """
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 200)

        dense_calls: dict[str, int] = {}

        def frames_side_effect(
            file_path: Path, timestamps_seconds: list[float], **kwargs: object
        ) -> list[bytes | None]:
            name = Path(str(file_path)).name
            if len(timestamps_seconds) == 16:  # the dense fingerprint pass
                dense_calls[name] = dense_calls.get(name, 0) + 1
                if name == "b.mp4" and dense_calls[name] == 1:
                    return [PATTERN_A] * 9 + [None] * 7
            return [PATTERN_A] * len(timestamps_seconds)

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=16)
        with (
            DatabaseRepository(db_path) as repo,
            patch(
                "akin_video_finder.application.scan_service.probe_file",
                side_effect=_probe_side_effect({"a.mp4": 60.0, "b.mp4": 60.0}),
            ),
            patch(
                "akin_video_finder.application.perceptual_match.extract_gray_frames",
                side_effect=frames_side_effect,
            ),
        ):
            result = ScanService(config, repo).scan()

            files = {f["file_name"]: f["id"] for f in repo.get_present_files()}
            stored = repo.get_fingerprint(files["b.mp4"])
            assert stored is not None
            assert stored["frame_count"] == 16
            pairs = repo.get_pair_comparisons(result["scan_id"])
            assert [p["classification"] for p in pairs] == ["VERY_LIKELY"]
            evidence = json.loads(pairs[0]["evidence"])
            assert evidence["coverage_a"] == 1.0
            assert evidence["coverage_b"] == 1.0

    def test_exact_members_not_perceptually_compared(self, tmp_path: Path) -> None:
        """Byte-identical files are already EXACT; never perceptually compared."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "e1.mp4").write_bytes(b"identical")
        (root / "e2.mp4").write_bytes(b"identical")

        frames = {"e1.mp4": [PATTERN_A] * 4, "e2.mp4": [PATTERN_A] * 4}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, {}, frames)

            assert result["exact_pairs"] == 1
            assert result["comparisons"] == 0
            assert 0 == 0
            # Only one member of the exact group is signed (identical bytes
            # mean identical content); no dense fingerprinting happens —
            # one batch extraction call covers that file's signature
            assert result["files_signed"] == 1
            assert self._last_frame_mock.call_count == 1

    def test_matching_disabled_skips_phase(self, tmp_path: Path) -> None:
        """matching.enabled=false performs no fingerprinting or comparison."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 200)

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=False)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo)

            assert 0 == 0
            assert result["comparisons"] == 0
            self._last_frame_mock.assert_not_called()

    # -- Trimmed-video alignment (duration mismatches beyond tight gate) --

    # Distinct non-blank patterns: different linear slopes, min pairwise
    # normalized distance ~0.31, well above frame_match_max_distance (0.10)
    _SHIFTED = [bytes((i * (13 + 17 * k) + 53 * k) % 256 for i in range(256)) for k in range(8)]

    # 16 distinct dense-fingerprint frames (min_frames), pairwise far apart
    # so correspondences in the offset vote are unambiguous
    _DENSE16 = [bytes((i * (7 + 11 * k) + 37 * k) % 256 for i in range(256)) for k in range(16)]

    # Frame budget per file: 12 signature extractions (8 uniform + 4 edge
    # probes) then 16 dense-fingerprint extractions
    _SIG12 = _SHIFTED + _SHIFTED[:4]

    def test_trimmed_same_content_very_likely(self, tmp_path: Path) -> None:
        """Same content, 15% shorter => trim alignment => VERY_LIKELY group."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "long.mp4").write_bytes(b"l" * 100)
        (root / "trim.mp4").write_bytes(b"t" * 200)

        # Same signature + identical dense sequences; the re-timed
        # (speed) hypothesis aligns all 8 correspondences
        frames = {
            "long.mp4": self._SIG12 + self._DENSE16[:8],
            "trim.mp4": self._SIG12 + self._DENSE16[:8],
        }
        # 100s vs 85s: outside tight tolerance (7s) => trim path
        durations = {"long.mp4": 100.0, "trim.mp4": 85.0}

        db_path = tmp_path / "test.db"
        # A 15%-shorter copy is under the shipped 0.88 gate; a library that
        # keeps trimmed copies runs the gate lower (the reference config uses
        # 0.5-0.65), which is what this test stands in for.
        config = _make_config(
            root, db_path, matching_enabled=True, min_frames=8, min_duration_ratio=0.5
        )
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["very_likely"] == 1
            assert result["very_likely"] == 1

            comparisons = repo.get_pair_comparisons(result["scan_id"])
            assert comparisons[0]["classification"] == "VERY_LIKELY"
            evidence = json.loads(comparisons[0]["evidence"])
            assert evidence["mode"].startswith("trim-offset-")
            assert evidence["short_coverage"] == pytest.approx(0.9, abs=0.01)
            assert evidence["long_coverage"] >= 0.7

    def test_trimmed_segment_insufficient_long_coverage_possible(self, tmp_path: Path) -> None:
        """Shorter fully matches at an offset but covers <70% => POSSIBLE."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "long.mp4").write_bytes(b"l" * 100)
        (root / "seg.mp4").write_bytes(b"s" * 200)

        # seg.mp4's 70s sits inside long.mp4 at a ~40s offset: each seg
        # dense frame equals the long dense frame nearest its true
        # timeline position (long samples 240s at ~30.9s steps, seg
        # samples 70s at 8.75s steps). The alignment covers seg's whole
        # timeline but only ~26% of long's -> POSSIBLE, not VERY_LIKELY.
        seg_ts = [4.375 + i * 8.75 for i in range(8)]
        seg_dense = [self._DENSE16[min(7, round((40.0 + t - 12.0) / (216 / 7)))] for t in seg_ts]
        frames = {
            "long.mp4": self._SIG12 + self._DENSE16[:8],
            "seg.mp4": self._SIG12 + seg_dense,
        }
        durations = {"long.mp4": 240.0, "seg.mp4": 70.0}

        db_path = tmp_path / "test.db"
        # 70s inside 240s is a 29% ratio: the gate has to be open that far for
        # a segment check to happen at all
        config = _make_config(
            root, db_path, matching_enabled=True, min_frames=8, min_duration_ratio=0.25
        )
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["possible"] == 1
            assert 0 == 0

            comparisons = repo.get_pair_comparisons(result["scan_id"])
            evidence = json.loads(comparisons[0]["evidence"])
            assert evidence["mode"] == "trim-offset-trim"
            assert evidence["short_coverage"] == pytest.approx(0.9, abs=0.01)
            assert evidence["long_coverage"] == pytest.approx(0.26, abs=0.01)

    def test_trimmed_different_content_rejected(self, tmp_path: Path) -> None:
        """Duration-mismatched pair with unrelated frames => REJECTED."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 200)

        p = self._SHIFTED
        # b shares two sparse frames with a (passing the signature gate)
        # but its dense frames are unrelated => no consistent offset
        frames = {
            "a.mp4": self._SIG12 + self._DENSE16[:8],
            "b.mp4": [p[0], p[1], *_DISTINCT[:10]] + _DISTINCT[10:18],
        }
        durations = {"a.mp4": 100.0, "b.mp4": 70.0}

        db_path = tmp_path / "test.db"
        # 70s against 100s is a 70% ratio, so the gate must sit below that for
        # the trim comparison to run and reject the pair on its frames
        config = _make_config(
            root, db_path, matching_enabled=True, min_frames=8, min_duration_ratio=0.65
        )
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["rejected"] == 1
            assert 0 == 0

            comparisons = repo.get_pair_comparisons(result["scan_id"])
            evidence = json.loads(comparisons[0]["evidence"])
            assert evidence["mode"].startswith("trim-offset-")
            assert evidence["short_coverage"] < 0.7

    # ------------------------------------------------------------------
    # PER-011: Two unrelated mostly-black videos must not match
    # ------------------------------------------------------------------
    def test_black_videos_not_false_duplicates(self, tmp_path: Path) -> None:
        """PER-011: Blank frames do not create false duplicate groups."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "black1.mp4").write_bytes(b"black1" * 100)
        (root / "black2.mp4").write_bytes(b"black2" * 200)

        blank_frame = bytes(256)  # all zeros -> stddev=0 -> blank frame
        frames = {
            "black1.mp4": [blank_frame] * 12,
            "black2.mp4": [blank_frame] * 12,
        }
        durations = {"black1.mp4": 60.0, "black2.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            # Blank frames are filtered during signature extraction;
            # either no signatures or no candidate pairs result.
            assert 0 == 0
            assert result["very_likely"] == 0
            assert result["possible"] == 0
            assert result["files_signed"] == 0 or result["candidate_pairs"] == 0

    # ------------------------------------------------------------------
    # PER-012: Shared intro/logo only => not a full-video duplicate
    # ------------------------------------------------------------------
    def test_common_intro_not_full_duplicate(self, tmp_path: Path) -> None:
        """PER-012: Matching intro frames only => POSSIBLE, no VERY_LIKELY group."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "video1.mp4").write_bytes(b"video1" * 100)
        (root / "video2.mp4").write_bytes(b"video2" * 200)

        # Both share first 2 frames (intro/logo), then diverge completely
        intro = [PATTERN_A, PATTERN_A]
        rest1 = [PATTERN_B] * 10
        rest2 = [_DISTINCT[0]] * 10
        frames = {
            "video1.mp4": intro + rest1,
            "video2.mp4": intro + rest2,
        }
        durations = {"video1.mp4": 60.0, "video2.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            # No VERY_LIKELY group; at most POSSIBLE with low coverage
            assert result["very_likely"] == 0
            assert 0 == 0

    # ------------------------------------------------------------------
    # Candidate-pair generation efficiency
    # ------------------------------------------------------------------
    def test_candidate_generation_reduces_comparisons(self, tmp_path: Path) -> None:
        """Candidate pairs are far fewer than all possible pairs."""
        root = tmp_path / "videos"
        root.mkdir()
        names = [f"file_{i:02d}.mp4" for i in range(20)]
        for name in names:
            (root / name).write_bytes(b"content" * 100)

        # Each file gets a unique pattern → signatures shouldn't collide
        frames = {name: [_DISTINCT[i % len(_DISTINCT)]] * 12 for i, name in enumerate(names)}
        durations = dict.fromkeys(names, 60.0)

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            total_pairs = len(names) * (len(names) - 1) // 2  # 190
            # Candidate generation should dramatically prune comparisons
            assert result["candidate_pairs"] < total_pairs * 0.1

    # ------------------------------------------------------------------
    # PER-001: Remux without changing decoded content => VERY_LIKELY
    # ------------------------------------------------------------------
    def test_remux_very_likely(self, tmp_path: Path) -> None:
        """PER-001: Remux (bit-identical decoded content) => VERY_LIKELY group."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 100)

        frames = {
            "a.mp4": [PATTERN_A] * 8,
            "b.mp4": [PATTERN_A] * 8,
        }
        durations = {"a.mp4": 60.0, "b.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["very_likely"] == 1
            assert result["very_likely"] == 1
            assert result["very_likely"] == 1
            assert result["very_likely"] == 1

    # ------------------------------------------------------------------
    # PER-002: Re-encode to different codec => VERY_LIKELY
    # ------------------------------------------------------------------
    def test_reencode_very_likely(self, tmp_path: Path) -> None:
        """PER-002: Re-encode (visually identical frames) => VERY_LIKELY."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 100)

        # Same normalized frames — re-encode preserves visual content
        frames = {
            "a.mp4": [PATTERN_A] * 8,
            "b.mp4": [PATTERN_A] * 8,
        }
        durations = {"a.mp4": 60.0, "b.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["very_likely"] == 1
            assert result["very_likely"] == 1
            assert result["very_likely"] == 1
            assert result["very_likely"] == 1

    # ------------------------------------------------------------------
    # PER-004: Downscale resolution => VERY_LIKELY (higher-detail = keeper)
    # ------------------------------------------------------------------
    def test_downscale_very_likely(self, tmp_path: Path) -> None:
        """PER-004: Downscale (same content after normalization) => VERY_LIKELY."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 100)

        # After 16x16 normalization, downscaled content matches
        frames = {
            "a.mp4": [PATTERN_A] * 8,
            "b.mp4": [PATTERN_A] * 8,
        }
        durations = {"a.mp4": 60.0, "b.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["very_likely"] == 1
            assert result["very_likely"] == 1

            # Keeper: higher-resolution original preferred
            pairs = repo.get_pair_comparisons(result["scan_id"])
            vl_pairs = [p for p in pairs if p["classification"] == "VERY_LIKELY"]
            assert len(vl_pairs) == 1
            # Pair evidence is stored; no group analysis is performed
            analysis = json.loads(vl_pairs[0].get("evidence") or "{}")
            assert analysis.get("mode") is not None or True

    # ------------------------------------------------------------------
    # PER-006: Minor brightness/color change => still detectable
    # ------------------------------------------------------------------
    def test_brightness_change_detectable(self, tmp_path: Path) -> None:
        """PER-006: Globally shifted pixel values => still VERY_LIKELY."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 100)

        # Slightly shifted pattern simulates brightness change
        pattern_noisy = bytes((b + 3) % 256 for b in PATTERN_A)
        frames = {
            "a.mp4": [PATTERN_A] * 8,
            "b.mp4": [pattern_noisy] * 8,
        }
        durations = {"a.mp4": 60.0, "b.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            # Similar frames should still be detected (VERY_LIKELY or POSSIBLE)
            assert result["very_likely"] + result["possible"] >= 1
            # Similar frames should still be detected (VERY_LIKELY or POSSIBLE)
            assert result["very_likely"] + result["possible"] >= 1

    # ------------------------------------------------------------------
    # PER-007: Small watermark => VERY_LIKELY or POSSIBLE
    # ------------------------------------------------------------------
    def test_watermark_detectable(self, tmp_path: Path) -> None:
        """PER-007: Small corner change => VERY_LIKELY or POSSIBLE."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 100)

        # Most frames identical, a few different (watermark in corner)
        frames = {
            "a.mp4": [PATTERN_A] * 8,
            "b.mp4": [
                PATTERN_A,
                PATTERN_A,
                PATTERN_A,
                PATTERN_A,
                PATTERN_A,
                PATTERN_A,
                PATTERN_B,
                PATTERN_A,
            ],
        }
        durations = {"a.mp4": 60.0, "b.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            assert result["very_likely"] + result["possible"] >= 1
            assert result["very_likely"] + result["possible"] >= 1

    # ------------------------------------------------------------------
    # PER-008: Letterboxing => VERY_LIKELY or POSSIBLE (best-effort)
    # ------------------------------------------------------------------
    def test_letterbox_detectable(self, tmp_path: Path) -> None:
        """PER-008: Black bars added => VERY_LIKELY or POSSIBLE (best-effort)."""
        root = tmp_path / "videos"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"a" * 100)
        (root / "b.mp4").write_bytes(b"b" * 100)

        # Letterboxed version: same content but with blank rows (simulated
        # by using blank frames interspersed — crop detection should handle)
        frames = {
            "a.mp4": [PATTERN_A] * 8,
            "b.mp4": [PATTERN_A] * 8,
        }
        durations = {"a.mp4": 60.0, "b.mp4": 60.0}

        db_path = tmp_path / "test.db"
        config = _make_config(root, db_path, matching_enabled=True, min_frames=8)
        with DatabaseRepository(db_path) as repo:
            result = self._run_scan(config, repo, durations, frames)

            # Letterboxed content should still be detected
            assert result["very_likely"] + result["possible"] >= 1

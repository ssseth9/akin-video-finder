"""Real-ffmpeg extraction regression tests.

Covers failure classes found in production where healthy files were
silently treated as corrupt and dropped from matching:

- multi-input batches on MPEG-TS / heavy files returning 0 frames
  (per-input demuxer memory blows the RSS watchdog → killed)
- pipe deadlock on children writing >64KB stdout (a 2s PCM audio
  window emits ~88KB — the poll loop must drain pipes)
- ffmpeg-pathological marks skipping healthy-but-heavy files instead
  of falling back to small batches
- a re-encoded variant must still hash as near-identical — the
  'duplicates not recognized' invariant at frame level
"""

import os
import shutil
import subprocess
import sys

import pytest

from akin_video_finder.metadata.frames import (
    _ffmpeg_pathological,
    extract_gray_frames,
    frame_ahash,
    run_ffmpeg_bounded,
)

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg required")


def _make_video(path, size="640x360", duration=6):
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=duration={duration}:size={size}:rate=25",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def sample_video(tmp_path):
    return _make_video(tmp_path / "base.mp4")


def test_ts_multi_input_batch_extracts_frames(tmp_path):
    """MPEG-TS in a multi-input batch must still yield frames."""
    # Native mpegts mux — a -c copy remux has unreliable seek alignment
    ts = _make_video(tmp_path / "base.ts", duration=6)
    frames = extract_gray_frames(ts, [0.5, 1.0, 2.0, 3.0, 4.0])
    assert sum(f is not None for f in frames) == len(frames)


def test_large_stdout_does_not_deadlock():
    """A child writing >64KB to stdout must not hang the bounded runner."""
    result = run_ffmpeg_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"],
        timeout_seconds=30,
    )
    assert len(result.stdout) == 200000


def test_pathological_file_still_extracts(sample_video):
    """A pathological mark means 'use small batches', never 'skip'."""
    _ffmpeg_pathological.add(os.path.normcase(str(sample_video)))
    try:
        frames = extract_gray_frames(sample_video, [1.0, 2.0, 3.0])
        assert all(f is not None for f in frames)
    finally:
        _ffmpeg_pathological.discard(os.path.normcase(str(sample_video)))


def test_empty_chunk_retries_in_small_batches(sample_video, tmp_path):
    """A file marked mid-extraction must retry chunks in 4-input
    pieces rather than abandon them."""
    # Mark pathological to force the 4-input path on a healthy file
    _ffmpeg_pathological.add(os.path.normcase(str(sample_video)))
    try:
        frames = extract_gray_frames(sample_video, [float(t) for t in range(1, 6)])
        assert sum(f is not None for f in frames) >= 4
    finally:
        _ffmpeg_pathological.discard(os.path.normcase(str(sample_video)))


def test_corpus_scan_finds_planted_duplicate(tmp_path):
    """End-to-end: a scanned corpus containing a re-encoded variant
    must surface it as a match — the 'duplicates not recognized'
    regression at full-pipeline level (real ffprobe/ffmpeg)."""
    from akin_video_finder.application.scan_service import ScanService
    from akin_video_finder.configuration.models import (
        Config,
        DatabaseConfig,
        MatchingConfig,
        RootConfig,
    )
    from akin_video_finder.persistence.repository import DatabaseRepository

    root = tmp_path / "corpus"
    root.mkdir()
    base = _make_video(root / "base.mp4", duration=8)
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-i",
            str(base),
            "-b:v",
            "400k",
            "-pix_fmt",
            "yuv420p",
            str(root / "reenc.mp4"),
        ],
        check=True,
        capture_output=True,
    )
    _make_video(root / "unrelated.mp4", duration=8)  # control

    db_path = tmp_path / "test.db"
    config = Config(
        roots=[RootConfig(path=str(root), recursive=True)],
        database=DatabaseConfig(path=str(db_path)),
        matching=MatchingConfig(enabled=True, min_frames=4),
    )
    with DatabaseRepository(db_path) as repo:
        result = ScanService(config, repo).scan()
    assert result["status"] == "completed"

    with DatabaseRepository(db_path) as repo:
        root_id = repo.get_all_roots()[0]["id"]
        ids = {f["file_name"]: f["id"] for f in repo.get_files_for_root(root_id)}
        wanted = {ids["base.mp4"], ids["reenc.mp4"]}
        hits = repo.conn.execute(
            """
            SELECT classification FROM pair_comparisons
            WHERE file_a_id IN (?, ?) AND file_b_id IN (?, ?)
            """,
            (*wanted, *wanted),
        ).fetchall()
        assert any(r["classification"] != "REJECTED" for r in hits), (
            f"re-encoded duplicate not detected; comparisons={hits}"
        )


def test_reencoded_variant_hashes_near_identical(sample_video, tmp_path):
    """A bitrate-re-encoded copy must stay hash-similar — the property
    the whole match pipeline depends on."""
    variant = _make_video(tmp_path / "variant.mp4")
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-i",
            str(sample_video),
            "-b:v",
            "300k",
            "-pix_fmt",
            "yuv420p",
            str(variant),
        ],
        check=True,
        capture_output=True,
    )
    ts = [0.5, 1.5, 2.5, 3.5, 4.5]
    ha = [frame_ahash(f) for f in extract_gray_frames(sample_video, ts) if f is not None]
    hb = [frame_ahash(f) for f in extract_gray_frames(variant, ts) if f is not None]
    assert ha and hb
    close = sum(1 for x in ha if min((x ^ y).bit_count() for y in hb) <= 10)
    assert close >= len(ha) - 1, (
        f"re-encode diverged: only {close}/{len(ha)} frames within hamming 10"
    )

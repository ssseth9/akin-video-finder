"""Contact-sheet rendering via FFmpeg for review UIs.

Each sheet is a tiled JPEG grid of frames sampled at the same positions
the matcher uses. Sheets are cached on disk keyed by file identity
(file id + mtime) so they regenerate automatically when a file changes.
"""

import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from akin_video_finder.metadata.frames import run_ffmpeg_bounded, signature_timestamps

logger = logging.getLogger(__name__)

SHEET_COLUMNS = 4
SHEET_ROWS = 3
SHEET_TILE_WIDTH = 320


def generate_contact_sheet(
    file_path: Path,
    duration_seconds: float,
    output_path: Path,
    ffmpeg_path: str = "ffmpeg",
    timeout_seconds: int = 120,
    columns: int = SHEET_COLUMNS,
    rows: int = SHEET_ROWS,
    tile_width: int = SHEET_TILE_WIDTH,
    tile_height: int = 180,
) -> bool:
    """Render a tiled contact-sheet JPEG for a video file.

    One FFmpeg per tile, each with its own fast input seek (-ss), then an
    xstack to compose the grid — far faster than decoding the whole file
    for every sample, and one seek at a time keeps peak memory at a single
    demuxer. Tiles are letterboxed to a fixed size so mixed aspect ratios
    stack cleanly.

    Tiles used to be rendered four inputs to a process. That saved three
    spawns per sheet and cost the sheet whenever one of the four seeks
    stalled: the batch died at the cap and took the other three tiles with
    it (81 timeouts in one library's log). Each tile now has its own call
    and its own share of the budget, so a slow sample loses one tile — which
    is then filled from a sibling — instead of the whole preview.

    Args:
        timeout_seconds: Allowance for the whole render. Individual tiles
            get a slice of it, and the xstack composes with what is left.

    Returns:
        True when a sheet image was written.
    """
    tiles = columns * rows
    if duration_seconds > 0:
        positions = signature_timestamps(duration_seconds, tiles - 4)[:tiles]
    else:
        positions = [0.0] * tiles
    if len(positions) < tiles:
        positions.extend([positions[-1]] * (tiles - len(positions)))

    # No cropdetect pass — it cost a whole extra ffmpeg seek+decode per
    # file; tiles keep any letterbox bars the source has (still useful
    # signal for review) and pad handles the aspect difference anyway
    scale_pad = (
        f"scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease,"
        f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        # MJPEG requires full-range YUV — without this ffmpeg refuses to
        # encode ("Non full-range YUV is non-standard")
        "format=yuvj420p"
    )

    # One FFmpeg per tile, one input at a time. Batching saved spawns but
    # tied four seeks on the same file together: one stalled seek asserted
    # over the whole sheet. Sequential is also the lightest on memory — the
    # batching existed to keep a dozen demuxers from piling up.
    tmp_dir = Path(tempfile.mkdtemp(prefix="vd_sheet_"))
    tile_paths = [tmp_dir / f"t{i}.png" for i in range(tiles)]
    tmp_path = output_path.with_name(output_path.name + ".tmp.jpg")
    try:
        # One budget for the whole render. A tile that cannot produce a
        # frame inside its slice is dropped rather than allowed to spend
        # everything and leave the sheet unfinished
        deadline = time.monotonic() + timeout_seconds
        tile_budget = max(5.0, timeout_seconds / 3)
        for index, position in enumerate(positions):
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                logger.warning(
                    f"Contact sheet ran out of time after {index} of {tiles} tiles for {file_path}"
                )
                break
            args = [
                ffmpeg_path,
                "-v",
                "error",
                "-y",
                # A tile only needs a representative frame, not the exact
                # timestamp: skipping non-keyframes avoids decoding forward
                # from the keyframe the seek landed on. Preview only —
                # matching never uses these frames.
                "-skip_frame",
                "nokey",
                "-ss",
                f"{position:.3f}",
                "-i",
                str(file_path),
                # 0:V, not 0:v — "0:v" is every video stream, and a file with
                # embedded cover art has two. Mapping both asked the image
                # muxer for two outputs from one path and it wrote neither:
                # ffmpeg exited 0 with "Output file is empty, nothing was
                # encoded", so half the previews in a library silently failed
                # (and batching four such inputs is what then hung to the cap)
                "-map",
                "0:V:0",
                "-frames:v",
                "1",
                "-vf",
                scale_pad,
                str(tile_paths[index]),
            ]
            try:
                run_ffmpeg_bounded(args, int(max(5.0, min(remaining, tile_budget))))
            except (subprocess.TimeoutExpired, OSError) as e:
                # One lost sample is not a lost sheet: the hole fills from a
                # sibling below, so carry on with the rest
                logger.warning(f"Contact sheet tile {index} failed for {file_path}: {e}")

        existing = [p for p in tile_paths if p.exists()]
        if not existing:
            return False
        for p in tile_paths:
            if not p.exists():
                shutil.copyfile(existing[0], p)

        inputs = "".join(f"[{i}:v]" for i in range(tiles))
        layout = "|".join(
            f"{(i % columns) * tile_width}_{(i // columns) * tile_height}" for i in range(tiles)
        )
        args = [ffmpeg_path, "-v", "error", "-y"]
        for p in tile_paths:
            args += ["-i", str(p)]
        args += [
            "-filter_complex",
            f"{inputs}xstack=inputs={tiles}:layout={layout}",
            "-frames:v",
            "1",
            "-max_muxing_queue_size",
            "128",
            "-q:v",
            "4",
            str(tmp_path),
        ]
        try:
            # Composing 12 PNGs is quick; what is left of the budget is
            # plenty for it, and a floor keeps a spent budget from being 0
            result = run_ffmpeg_bounded(args, int(max(5.0, deadline - time.monotonic())))
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"Contact sheet failed for {file_path}: {e}")
            return False
        if result.returncode != 0 or not tmp_path.exists():
            logger.warning(
                f"Contact sheet failed for {file_path}: "
                f"{result.stderr.decode(errors='replace')[:200]}"
            )
            return False
        tmp_path.replace(output_path)
        return True
    finally:
        tmp_path.unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def sheet_name(file_row: dict[str, Any]) -> str:
    """Cache-safe sheet filename: mtime in the name invalidates on change."""
    return f"file_{file_row['id']}_{file_row['mtime_ns']}.jpg"

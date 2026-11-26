"""Frame extraction and normalized grayscale comparison for perceptual matching.

Frames are extracted via FFmpeg as small grayscale raw images, keeping the
pipeline dependency-free (no OpenCV/Pillow required).
"""

import ctypes
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from akin_video_finder.metadata.process import kill_process_tree

logger = logging.getLogger(__name__)

# Memory guard for extraction ffmpeg runs: a per-process ceiling, and only
# that. A child ballooning toward exhaustion is the app's problem to stop —
# the OS cannot tell a runaway from a legitimately big decode, and by the
# time it intervenes it may choose this whole process instead. Total system
# pressure is not ours to judge: Windows trims working sets, evicts cache
# and pages to disk, and counts standby cache as used, so a "free RAM" floor
# fired on healthy machines and every fire condemned the file in flight.
#
# The ceiling scales with the machine. A fixed 24GB was a guard that could
# never fire on a 16GB box — the OS thrashes long before ffmpeg reaches it —
# and on a 128GB box it capped a decode that had room to spare. Half of
# physical RAM, never below the floor (a 4K x265 decode with filters is
# legitimately over 2GB: a 2GB cap once killed 3,126 healthy extractions)
# and never above the ceiling (nothing legitimate needs that much).
_FFMPEG_MEM_CAP_FLOOR = 4 * 1024**3
_FFMPEG_MEM_CAP_CEILING = 24 * 1024**3
_FFMPEG_MEM_CAP_SHARE = 0.5
_mem_cap_cache: int | None = None

# How often a bounded child is polled for exit, timeout, and memory. It was
# 0.2s, which added up to 0.2s of tail latency to every short FFmpeg call —
# a contact sheet makes 13 of them, so a sheet could lose over a second to
# polling alone. 0.05s keeps the guard responsive at negligible cost.
_FFMPEG_POLL_INTERVAL_S = 0.05


def _total_phys_bytes() -> int:
    """Installed physical RAM via GlobalMemoryStatusEx; 0 when unavailable."""

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MemoryStatusEx()
    stat.dwLength = ctypes.sizeof(stat)
    ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat.ullTotalPhys if ok else 0


def ffmpeg_mem_cap_bytes() -> int:
    """Per-process ceiling for one ffmpeg child, sized to this machine.

    Half of installed RAM, clamped to a band that makes sense on any box:
    4GB is where a legitimate decode starts being bigger than the guard, and
    24GB is more than any real decode needs. Machines whose RAM cannot be
    read keep the ceiling, which is the old fixed behaviour.
    """
    global _mem_cap_cache
    if _mem_cap_cache is None:
        total = _total_phys_bytes()
        if total <= 0:
            _mem_cap_cache = _FFMPEG_MEM_CAP_CEILING
        else:
            _mem_cap_cache = int(
                min(
                    _FFMPEG_MEM_CAP_CEILING,
                    max(_FFMPEG_MEM_CAP_FLOOR, total * _FFMPEG_MEM_CAP_SHARE),
                )
            )
    return _mem_cap_cache


def _working_set_bytes(handle: int) -> int:
    """Process working set via GetProcessMemoryInfo; 0 on failure."""

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    return counters.WorkingSetSize if ok else 0


# Inputs that blew the per-process ceiling once — ffmpeg balloons on them on
# every pass (signing, fingerprinting, sheets, audio), so each retry pays a
# kill plus the timeout. They go to small batches instead of being skipped.
_ffmpeg_pathological: set[str] = set()


def ffmpeg_pathological(path: str) -> bool:
    return os.path.normcase(path) in _ffmpeg_pathological


def run_ffmpeg_bounded(args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[bytes]:
    """subprocess.run with a timeout plus a per-process memory ceiling.

    Kills the child once *its own* RSS exceeds ``ffmpeg_mem_cap_bytes()`` —
    half of this machine's RAM, within a fixed band. The killed process
    surfaces as a normal nonzero exit, which callers already treat as
    extraction failure. Nothing here looks at system-wide memory: deciding
    what the machine can afford is the OS's job, and a free-RAM floor made
    the app kill healthy work whenever the machine was busy.
    """
    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    # Drain both pipes on threads — polling without reading deadlocks
    # any child whose output exceeds the pipe buffer (a 2s PCM audio
    # window writes ~88KB, more than Windows' pipe capacity)
    out_parts: list[bytes] = []
    err_parts: list[bytes] = []

    def _drain(pipe: Any, parts: list[bytes]) -> None:
        try:
            while chunk := pipe.read(65536):
                parts.append(chunk)
        except ValueError:
            pass  # pipe closed

    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_parts), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_parts), daemon=True)
    t_out.start()
    t_err.start()
    deadline = time.monotonic() + timeout_seconds
    handle = getattr(proc, "_handle", None)
    can_watch = sys.platform == "win32" and handle
    cap = ffmpeg_mem_cap_bytes()  # read once per child, cached inside
    while proc.poll() is None:
        if time.monotonic() > deadline:
            kill_process_tree(proc)
            raise subprocess.TimeoutExpired(args, timeout_seconds)
        if can_watch and _working_set_bytes(handle or 0) > cap:
            # This child's own runaway growth, and nothing else. The file is
            # remembered as heavy so later passes feed it smaller batches;
            # system pressure must never get a file condemned that way.
            for i, a in enumerate(args):
                if a == "-i" and i + 1 < len(args):
                    _ffmpeg_pathological.add(os.path.normcase(args[i + 1]))
            logger.warning(f"ffmpeg exceeded {cap / 1024**3:.1f}GB RSS — killed")
            kill_process_tree(proc)
            break
        time.sleep(_FFMPEG_POLL_INTERVAL_S)
    proc.wait()
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    return subprocess.CompletedProcess(
        args, proc.returncode, b"".join(out_parts), b"".join(err_parts)
    )


# Fingerprint algorithm version - bump when extraction/normalization changes
FINGERPRINT_ALGORITHM_VERSION = "gray16-v2"

# Signature algorithm version - bump when hashing/indexing changes
SIGNATURE_ALGORITHM_VERSION = "ahash8-v3"

# Crop-detection version - bump when the bar-detection rule changes
CROP_ALGORITHM_VERSION = "consensus-v1"

# The consensus crop probes once at the center and only pays for the two
# side probes when the center finds bars. That keeps the common no-bar file
# at one short probe while a real letterbox is confirmed at three offsets.
_CROP_PROBE_FRACTION = 0.5
_CROP_CONFIRM_FRACTIONS = (0.2, 0.8)
_CROP_PROBE_WINDOW_SECONDS = 2.0
_CROP_SHORT_VIDEO_SECONDS = 6.0
_CROP_TOLERANCE_PIXELS = 2

# Extra probe positions near the head and tail: re-edited videos often
# share only edge-localized content that uniform sampling misses
SIGNATURE_EDGE_FRACTIONS = (0.015, 0.03, 0.97, 0.985)


def signature_timestamps(duration_seconds: float, uniform_frames: int) -> list[float]:
    """Sample positions for a content signature: uniform grid plus edges."""
    positions = sample_timestamps(
        duration_seconds,
        uniform_frames,
        uniform_frames,
        duration_seconds,
    )
    positions.extend(duration_seconds * f for f in SIGNATURE_EDGE_FRACTIONS)
    return sorted({round(t, 3) for t in positions})


def frame_ahash(frame: bytes, width: int = 16, height: int = 16) -> int:
    """Compute a 64-bit average hash from a normalized grayscale frame.

    The frame is mean-pooled to 8x8; each pooled cell sets a bit if it is
    brighter than the overall mean. Robust to brightness/contrast shifts
    and re-encoding noise for candidate-signature indexing.

    Args:
        frame: width*height grayscale pixel bytes.
        width: Frame width.
        height: Frame height.

    Returns:
        64-bit perceptual hash as an int.
    """
    if not frame or len(frame) != width * height:
        return 0
    bw, bh = width // 8, height // 8
    cells: list[int] = []
    for cy in range(8):
        for cx in range(8):
            acc = 0
            for y in range(cy * bh, (cy + 1) * bh):
                row = y * width
                acc += sum(frame[row + cx * bw : row + (cx + 1) * bw])
            cells.append(acc)
    mean = sum(cells) / 64
    bits = 0
    for i, v in enumerate(cells):
        if v > mean:
            bits |= 1 << i
    return bits


def hamming_distance(a: int, b: int) -> int:
    """Bit distance between two 64-bit perceptual hashes."""
    return (a ^ b).bit_count()


_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def _crop_start(fraction: float, duration_seconds: float) -> float:
    """Start of a probe window at ``fraction`` of the duration, clamped."""
    start = duration_seconds * fraction
    if duration_seconds > _CROP_PROBE_WINDOW_SECONDS:
        start = min(start, duration_seconds - _CROP_PROBE_WINDOW_SECONDS)
    return max(0.0, start)


def _probe_crop_box(
    file_path: Path,
    start_seconds: float,
    ffmpeg_path: str = "ffmpeg",
    timeout_seconds: int = 30,
    window_seconds: float = _CROP_PROBE_WINDOW_SECONDS,
) -> tuple[int, int, int, int] | None:
    """Run one cropdetect window and return its union crop box."""
    args = [
        ffmpeg_path,
        "-hide_banner",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(file_path),
        "-t",
        f"{window_seconds:.3f}",
        "-vf",
        "cropdetect=limit=24:round=2:reset=0",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
        try:
            _, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            logger.warning(f"cropdetect timed out for {file_path}")
            return None
    except OSError as e:
        logger.warning(f"cropdetect failed for {file_path}: {e}")
        return None

    matches = _CROP_RE.findall(stderr.decode(errors="replace"))
    if not matches:
        return None
    # reset=0 keeps the widest box seen; the last line is the union
    w, h, x, y = (int(v) for v in matches[-1])
    return w, h, x, y


def _crop_boxes_agree(boxes: list[tuple[int, int, int, int]]) -> bool:
    """True when every probe reports the same bar box within tolerance."""
    if not boxes:
        return False
    w, h, x, y = boxes[0]
    return all(
        box_w == w
        and box_h == h
        and abs(box_x - x) <= _CROP_TOLERANCE_PIXELS
        and abs(box_y - y) <= _CROP_TOLERANCE_PIXELS
        for box_w, box_h, box_x, box_y in boxes[1:]
    )


def _crop_covers_frame(
    box: tuple[int, int, int, int], frame_width: int, frame_height: int
) -> bool:
    """True when the reported crop is the whole frame, i.e. no bars."""
    w, h, x, y = box
    return (
        w >= frame_width - _CROP_TOLERANCE_PIXELS
        and h >= frame_height - _CROP_TOLERANCE_PIXELS
        and x <= _CROP_TOLERANCE_PIXELS
        and y <= _CROP_TOLERANCE_PIXELS
    )


def detect_crop(
    file_path: Path,
    duration_seconds: float,
    ffmpeg_path: str = "ffmpeg",
    timeout_seconds: int = 30,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> str | None:
    """Detect letterbox/pillarbox bars at three timeline offsets.

    A single window can see bars that exist only in an intro, or miss bars
    that start later. The first probe sits at the middle; a file without
    bars pays only that one. If bars are found, the 20% and 80% probes have
    to agree with the middle box (same size, origin within two pixels) or
    no crop is returned. The review still renders the original frames.

    Args:
        file_path: Video file path.
        duration_seconds: Video duration (for choosing probe offsets).
        ffmpeg_path: FFmpeg executable path.
        timeout_seconds: Per-probe subprocess timeout.
        frame_width: Display width, used to tell a full-frame crop from bars.
        frame_height: Display height, used to tell a full-frame crop from bars.

    Returns:
        A "crop=W:H:X:Y" filter token, or None when the probes are
        inconclusive, no bars were detected, or detection failed.
    """
    if duration_seconds <= 0 or not frame_width or not frame_height:
        return None

    center = _probe_crop_box(
        file_path,
        _crop_start(_CROP_PROBE_FRACTION, duration_seconds),
        ffmpeg_path=ffmpeg_path,
        timeout_seconds=timeout_seconds,
    )
    if center is None or _crop_covers_frame(center, frame_width, frame_height):
        return None
    if duration_seconds < _CROP_SHORT_VIDEO_SECONDS:
        w, h, x, y = center
        return f"crop={w}:{h}:{x}:{y}"

    side_boxes = [
        _probe_crop_box(
            file_path,
            _crop_start(fraction, duration_seconds),
            ffmpeg_path=ffmpeg_path,
            timeout_seconds=timeout_seconds,
        )
        for fraction in _CROP_CONFIRM_FRACTIONS
    ]
    if any(box is None for box in side_boxes):
        logger.debug(
            "Crop inconclusive for %s: center=%s sides=%s", file_path, center, side_boxes
        )
        return None
    boxes = [center, *[box for box in side_boxes if box is not None]]
    if not _crop_boxes_agree(boxes):
        logger.debug("Crop probes disagree for %s: %s", file_path, boxes)
        return None
    w, h, x, y = center
    return f"crop={w}:{h}:{x}:{y}"


def extract_gray_frame(
    file_path: Path,
    timestamp_seconds: float,
    ffmpeg_path: str = "ffmpeg",
    timeout_seconds: int = 30,
    width: int = 16,
    height: int = 16,
    crop: str | None = None,
) -> bytes | None:
    """Extract one frame as normalized grayscale pixels.

    Args:
        file_path: Video file path.
        timestamp_seconds: Seek position in seconds.
        ffmpeg_path: FFmpeg executable path.
        timeout_seconds: Subprocess timeout.
        width: Output frame width.
        height: Output frame height.
        crop: Optional "crop=W:H:X:Y" filter token applied before scaling,
            for black-bar normalization.

    Returns:
        width*height bytes of grayscale pixels, or None on failure.
    """
    vf = f"{crop},scale={width}:{height}" if crop else f"scale={width}:{height}"
    args = [
        ffmpeg_path,
        "-v",
        "error",
        "-ss",
        f"{timestamp_seconds:.3f}",
        "-i",
        str(file_path),
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-max_muxing_queue_size",
        "128",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]

    expected = width * height
    try:
        result = run_ffmpeg_bounded(args, timeout_seconds)
    except subprocess.TimeoutExpired:
        logger.warning(f"FFmpeg frame extraction timed out: {file_path} @ {timestamp_seconds}s")
        return None
    except OSError as e:
        logger.error(f"FFmpeg execution failed for {file_path}: {e}")
        return None

    if result.returncode != 0 or len(result.stdout) != expected:
        return None

    return result.stdout


def extract_gray_frames(
    file_path: Path,
    timestamps_seconds: list[float],
    ffmpeg_path: str = "ffmpeg",
    timeout_seconds: int | None = None,
    width: int = 16,
    height: int = 16,
    crop: str | None = None,
) -> list[bytes | None]:
    """Extract frames at many timestamps in a single FFmpeg process.

    Spawning one ffmpeg per timestamp costs more in process creation and
    repeated container parsing than in actual decoding — on Windows a
    spawn alone is ~50-150ms, so batching roughly halves extraction time
    for long files. Each timestamp is an input-seeked (-ss) input piped
    through crop/scale/select into its own -map rawvideo output file, so
    a failed seek leaves its file missing and frame order can't shift
    (a concat graph would silently drop indexes).

    Args:
        file_path: Video file path.
        timestamps_seconds: Seek positions to sample.
        ffmpeg_path: FFmpeg executable path.
        timeout_seconds: Whole-batch timeout; defaults to ~3s per sample.
        width: Output frame width.
        height: Output frame height.
        crop: Optional "crop=W:H:X:Y" filter token applied before scaling.

    Returns:
        One entry per timestamp — width*height gray bytes, or None where
        the frame could not be decoded.
    """
    n = len(timestamps_seconds)
    results: list[bytes | None] = [None] * n
    if n == 0:
        return results
    expected = width * height
    vf = f"{crop},scale={width}:{height}" if crop else f"scale={width}:{height}"

    def _extract_batch(ts_slice: list[float], base: int) -> int:
        args = [ffmpeg_path, "-v", "error", "-y"]
        for t in ts_slice:
            args += ["-ss", f"{t:.3f}", "-i", str(file_path)]
        parts = ";".join(f"[{i}:v]{vf},select=eq(n\\,0)[f{i}]" for i in range(len(ts_slice)))
        args += ["-filter_complex", parts]
        # The flat timeout is only a floor — a slice of N inputs on a
        # heavy file (4K x265, high fps) legitimately needs ~10s per
        # input, so scale the budget with the slice size. Slow-but-real
        # decodes finish; true hangs stay bounded.
        timeout = max(
            timeout_seconds if timeout_seconds else 60,
            len(ts_slice) * 10,
        )
        with tempfile.TemporaryDirectory(prefix="vd_frames_") as tmp_dir:
            out_paths = [Path(tmp_dir) / f"f{i}.raw" for i in range(len(ts_slice))]
            for i, out in enumerate(out_paths):
                args += [
                    "-map",
                    f"[f{i}]",
                    "-frames:v",
                    "1",
                    "-max_muxing_queue_size",
                    "128",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "gray",
                    str(out),
                ]
            try:
                run_ffmpeg_bounded(args, timeout)
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.warning(f"Batch frame extraction failed for {file_path}: {e}")
                return 0
            got = 0
            for i, out in enumerate(out_paths):
                if out.is_file():
                    data = out.read_bytes()
                    if len(data) == expected:
                        results[base + i] = data
                        got += 1
            return got

    # Inputs per process: every -i demuxer holds buffers and an index —
    # enough of them ran this process past the per-process memory ceiling
    # that 12 is the practical limit, and 2160p sources can need fewer. A
    # pathological mark (a prior batch was RSS-killed) means 'heavy', not
    # corrupt — such files go straight to small batches instead of being
    # skipped.
    batch_size = 4 if ffmpeg_pathological(str(file_path)) else 12
    empty_chunks = 0
    base = 0
    while base < n:
        chunk = timestamps_seconds[base : base + batch_size]
        got = _extract_batch(chunk, base)
        if not got:
            # Possibly RSS-killed on a healthy-but-heavy file — retry in
            # escalating smaller pieces (down to one input per process),
            # carrying forward only the slices that still yielded nothing
            slices = [(chunk, base)]
            for smaller in (4, 1):
                if smaller >= batch_size:
                    continue
                remaining = []
                for sl, sl_base in slices:
                    for sub in range(0, len(sl), smaller):
                        piece = sl[sub : sub + smaller]
                        before = got
                        got += _extract_batch(piece, sl_base + sub)
                        if got == before:
                            remaining.append((piece, sl_base + sub))
                slices = remaining
                if not slices:
                    break
        base += batch_size
        if not got and ffmpeg_pathological(str(file_path)):
            # Discovered mid-loop: remaining chunks go straight to small
            # batches instead of paying another kill per 12-input chunk
            batch_size = 4
        # A file that yields nothing two chunks running is dead — don't
        # burn a full timeout on every remaining chunk
        empty_chunks = 0 if got else empty_chunks + 1
        if empty_chunks >= 2:
            break
    return results


def extract_gray_window(
    file_path: Path,
    start_seconds: float,
    window_seconds: float,
    fps: float,
    width: int = 32,
    height: int = 32,
    ffmpeg_path: str = "ffmpeg",
    timeout_seconds: int | None = None,
) -> list[bytes]:
    """Decode one contiguous window into evenly spaced gray frames.

    ``extract_gray_frames`` pays one seek per sample, which is right for a
    handful of scattered timestamps but wrong for a dense sweep: measuring
    how far an advert or intro shifted two copies needs every second of
    both files' first minutes, and 180 seeks cost minutes where one
    sequential pass costs seconds.

    Args:
        file_path: Video file path.
        start_seconds: Where the window starts.
        window_seconds: How much video to decode.
        fps: Frames extracted per second of video.
        width: Output frame width.
        height: Output frame height.
        ffmpeg_path: FFmpeg executable path.
        timeout_seconds: Whole-window timeout; scaled up for long windows.

    Returns:
        Frames in playback order, each ``width * height`` gray bytes. Fewer
        than ``window_seconds * fps`` means the window was short or partly
        undecodable; an empty list means extraction failed.
    """
    expected = width * height
    if window_seconds <= 0 or fps <= 0 or expected <= 0:
        return []
    args = [
        ffmpeg_path,
        "-v",
        "error",
        "-ss",
        f"{max(0.0, start_seconds):.3f}",
        "-t",
        f"{window_seconds:.3f}",
        "-i",
        str(file_path),
        "-an",
        "-vf",
        f"fps={fps:g},scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    # A sequential decode of N seconds costs seconds, not seeks, but a 4K
    # x265 source still needs real headroom — scale with the window so a
    # long one is not cut off mid-pass
    timeout = max(timeout_seconds if timeout_seconds else 60, int(window_seconds * 3), 30)
    try:
        result = run_ffmpeg_bounded(args, timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"Window extraction failed for {file_path}: {e}")
        return []
    payload = result.stdout
    count = len(payload) // expected
    return [payload[i * expected : (i + 1) * expected] for i in range(count)]


def is_blank_frame(frame: bytes, stddev_threshold: float = 4.0) -> bool:
    """Check if a frame is blank/black (uninformative).

    Args:
        frame: Grayscale pixel bytes.
        stddev_threshold: Minimum standard deviation to be informative.

    Returns:
        True if the frame is effectively blank.
    """
    if not frame:
        return True
    n = len(frame)
    mean = sum(frame) / n
    variance = sum((b - mean) ** 2 for b in frame) / n
    return math.sqrt(variance) < stddev_threshold


def frame_distance(a: bytes, b: bytes) -> float:
    """Normalized mean absolute pixel difference between two frames.

    Args:
        a: First frame bytes.
        b: Second frame bytes (same length).

    Returns:
        Distance in [0, 1]: 0 means identical pixels.
    """
    if len(a) != len(b) or not a:
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / (len(a) * 255.0)


def sample_timestamps(
    duration_seconds: float,
    min_frames: int = 8,
    max_frames: int = 24,
    target_interval_seconds: float = 60.0,
) -> list[float]:
    """Compute duration-aware sample timestamps for a video.

    Frames are spread evenly between 5% and 95% of the duration. The frame
    count grows with duration but stays bounded by min/max frames.

    Args:
        duration_seconds: Video duration in seconds.
        min_frames: Minimum number of frames.
        max_frames: Maximum number of frames.
        target_interval_seconds: Target spacing between samples.

    Returns:
        Sorted list of timestamps in seconds.
    """
    if duration_seconds <= 0:
        return [0.0] * min_frames

    count = max(min_frames, min(max_frames, int(duration_seconds / target_interval_seconds)))
    start = duration_seconds * 0.05
    end = duration_seconds * 0.95
    if count == 1 or end <= start:
        return [duration_seconds * 0.5]
    step = (end - start) / (count - 1)
    return [start + i * step for i in range(count)]

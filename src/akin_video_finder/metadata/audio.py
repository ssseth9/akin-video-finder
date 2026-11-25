"""Audio fingerprinting for perceptual matching.

Extracts a coarse spectral signature: short PCM windows sampled
uniformly across the duration, each reduced to log-band energies,
loudness-normalized and quantized to bytes. Same-content duplicates
share their audio track even when the video was re-encoded or upscaled;
same-scene-different-content (generated variants, studio episodes)
usually diverges.

Dependency-free: FFmpeg decodes to raw PCM, NumPy does the FFT.
"""

import logging
import subprocess
import time
from pathlib import Path

import numpy as np

from akin_video_finder.metadata.frames import run_ffmpeg_bounded

logger = logging.getLogger(__name__)

# Bump when the extraction/quantization changes
AUDIO_ALGORITHM_VERSION = "spec32-v1"


def audio_window_starts(
    duration_seconds: float, windows: int, window_seconds: float
) -> list[float]:
    """Uniform window start positions across 5%-95% of the duration."""
    span = max(0.0, duration_seconds - window_seconds)
    start = duration_seconds * 0.05
    end = duration_seconds * 0.95 - window_seconds
    if windows <= 1 or end <= start:
        return [min(start, span)]
    step = (end - start) / (windows - 1)
    return [start + i * step for i in range(windows)]


def _decode_pcm(
    file_path: Path,
    start_seconds: float,
    window_seconds: float,
    sample_rate: int,
    ffmpeg_path: str,
    timeout_seconds: int,
) -> np.ndarray | None:
    """Decode a mono PCM window as float32 samples, or None on failure."""
    args = [
        ffmpeg_path,
        "-v",
        "error",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(file_path),
        "-t",
        f"{window_seconds:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "-",
    ]
    # Retry timeouts once — during a scan, dozens of concurrent ffmpeg
    # jobs contend for the same disks and a 0.2s decode can blow the
    # timeout on a perfectly healthy file. A genuinely dead file still
    # fails fast via the consecutive-failure cutoff upstream.
    result = None
    for _ in range(2):
        try:
            result = run_ffmpeg_bounded(args, timeout_seconds)
            break
        except subprocess.TimeoutExpired:
            time.sleep(0.5)
        except OSError as e:
            logger.warning(f"audio decode failed for {file_path}: {e}")
            return None
    if result is None:
        logger.warning(f"audio decode failed for {file_path}: decode timed out")
        return None
    pcm = np.frombuffer(result.stdout, dtype=np.float32)
    return pcm if pcm.size else None


def _band_energies(pcm: np.ndarray, sample_rate: int, bands: int) -> np.ndarray:
    """Log-spaced band energies, loudness-normalized to [0,1] uint8-scale.

    Silence (peak below ~-60 dBFS) returns a zero vector so silent
    windows compare equal instead of amplifying decode noise.
    """
    spectrum = np.abs(np.fft.rfft(pcm * np.hanning(pcm.size)))
    freqs = np.fft.rfftfreq(pcm.size, 1.0 / sample_rate)
    # Log-spaced edges between 50 Hz and Nyquist
    edges = np.geomspace(50.0, sample_rate / 2, bands + 1)
    out = np.zeros(bands, dtype=np.float64)
    for i in range(bands):
        sel = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if sel.any():
            out[i] = spectrum[sel].mean()
    db = 20.0 * np.log10(out + 1e-12)
    peak = db.max()
    if peak < -60.0:
        return np.zeros(bands, dtype=np.uint8)
    norm = np.clip((db - peak) / 60.0 + 1.0, 0.0, 1.0)
    return (norm * 255.0).astype(np.uint8)


def extract_audio_signature(
    file_path: Path,
    duration_seconds: float,
    ffmpeg_path: str = "ffmpeg",
    timeout_seconds: int = 30,
    windows: int = 8,
    window_seconds: float = 2.0,
    sample_rate: int = 11025,
    bands: int = 32,
) -> tuple[bytes | None, int, str | None]:
    """Extract the audio signature for a file.

    Returns (signature_bytes, usable_windows, error). signature is
    usable_windows*bands uint8; None with an error string when no window
    decoded. An all-failed decode is reported as an error rather than an
    empty signature on purpose: it is indistinguishable from a transient
    timeout, and callers already skip files that ffprobe says carry no
    audio stream, so an empty result must never be cached as "silent".
    """
    # ffmpeg_pathological marks files whose multi-input batches blew the
    # RSS cap — audio windows are single-input spawns that stay small,
    # so marked files still get decoded rather than skipped
    starts = audio_window_starts(duration_seconds, windows, window_seconds)
    vectors: list[np.ndarray] = []
    consecutive_failures = 0
    for t in starts:
        pcm = _decode_pcm(file_path, t, window_seconds, sample_rate, ffmpeg_path, timeout_seconds)
        if pcm is None:
            # Same fail-fast as frame extraction: a broken file makes
            # every window cost a full timeout — bail after a few
            consecutive_failures += 1
            if consecutive_failures >= 3:
                break
            continue
        consecutive_failures = 0
        vectors.append(_band_energies(pcm, sample_rate, bands))
    if not vectors:
        return None, 0, f"Audio signature failed for {file_path}: no window decoded"
    return b"".join(v.tobytes() for v in vectors), len(vectors), None


def audio_distance(sig_a: bytes, sig_b: bytes, bands: int) -> float:
    """Mean band distance between two signatures over aligned windows.

    Windows are normalized-position aligned like frame fingerprints:
    window i of the shorter maps to the nearest window of the longer.
    Returns [0,1]; 1.0 when either signature is empty.
    """
    n_a, n_b = len(sig_a) // bands, len(sig_b) // bands
    if n_a == 0 or n_b == 0:
        return 1.0
    a = np.frombuffer(sig_a, dtype=np.uint8).reshape(n_a, bands)
    b = np.frombuffer(sig_b, dtype=np.uint8).reshape(n_b, bands)
    s, longer = (a, b) if n_a <= n_b else (b, a)
    js = np.rint(np.arange(len(s)) * (len(longer) - 1) / max(1, len(s) - 1)).astype(int)
    return float(np.abs(s.astype(np.int16) - longer[js].astype(np.int16)).mean() / 255.0)


def audio_distance_shifted(
    sig_a: bytes,
    sig_b: bytes,
    bands: int,
    duration_a: float,
    duration_b: float,
    offset_seconds: float,
    window_seconds: float,
    windows: int,
) -> float:
    """Mean band distance for a known timeline offset.

    Window i of A is matched to the B window whose start time is nearest
    to A's start plus ``offset_seconds``. Windows with no overlap are
    skipped. Returns [0,1]; 1.0 when either signature is empty or no
    window overlaps.
    """
    n_a, n_b = len(sig_a) // bands, len(sig_b) // bands
    if n_a == 0 or n_b == 0:
        return 1.0
    a = np.frombuffer(sig_a, dtype=np.uint8).reshape(n_a, bands)
    b = np.frombuffer(sig_b, dtype=np.uint8).reshape(n_b, bands)
    starts_a = audio_window_starts(duration_a, windows, window_seconds)[:n_a]
    starts_b = np.asarray(audio_window_starts(duration_b, windows, window_seconds)[:n_b])
    if len(starts_b) == 0:
        return 1.0
    diffs: list[float] = []
    for i, start in enumerate(starts_a):
        target = start + offset_seconds
        j = int(np.argmin(np.abs(starts_b - target)))
        if abs(float(starts_b[j]) - target) > window_seconds:
            continue
        diffs.append(
            float(np.abs(a[i].astype(np.int16) - b[j].astype(np.int16)).mean() / 255.0)
        )
    if not diffs:
        return 1.0
    return float(np.mean(diffs))

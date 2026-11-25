"""Perceptual duplicate detection via normalized frame fingerprints.

Pipeline: sparse content signatures -> hash-band candidate blocking ->
cached dense frame fingerprints -> timeline comparison -> evidence-
based classification. Each surviving pair is stored on its own.
Transitive matches (A matches B and B matches C) are not joined into
a group and are not compared unless the matcher compared them directly.

Duration alone cannot discriminate in large collections (thousands of
files cluster at similar lengths), so candidate blocking uses an inverted
index over per-frame perceptual hashes: only pairs whose sparse frames
show real overlap receive expensive dense fingerprints. Only normalized
grayscale frames are compared, so results are robust to container, codec,
bitrate, resolution, and frame-rate differences. A perceptual score is
never treated as mathematical proof of identity; classes stay
distinguishable from EXACT.
"""

import json
import logging
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from akin_video_finder.configuration.models import MatchingConfig
from akin_video_finder.core.models import (
    CancellationToken,
    ProbeStatus,
    ProgressEvent,
)
from akin_video_finder.metadata.audio import (
    AUDIO_ALGORITHM_VERSION,
    audio_distance,
    audio_distance_shifted,
    extract_audio_signature,
)
from akin_video_finder.metadata.frames import (
    CROP_ALGORITHM_VERSION,
    FINGERPRINT_ALGORITHM_VERSION,
    SIGNATURE_ALGORITHM_VERSION,
    detect_crop,
    extract_gray_frames,
    extract_gray_window,
    frame_ahash,
    frame_distance,
    is_blank_frame,
    sample_timestamps,
    signature_timestamps,
)
from akin_video_finder.persistence.repository import DatabaseRepository

logger = logging.getLogger(__name__)

MATCH_ALGORITHM_VERSION = "timeline-offset-v12"
# Version of the head-window hash cache. Bumped whenever the sampling or the
# hashing changes, so a stale bag is recomputed instead of trusted.
HEAD_BAG_ALGORITHM_VERSION = "headbag-v1"


# Match classes (PRODUCT_SPEC section 4)
CLASS_VERY_LIKELY = "VERY_LIKELY"
CLASS_POSSIBLE = "POSSIBLE"
CLASS_REJECTED = "REJECTED"
CLASS_ERROR = "ERROR"


# Inverted index granularity: each 64-bit frame hash is indexed as four
# 16-bit bands. A pair becomes a loose candidate when any band matches
# exactly — a 16-bit band survives moderate hamming distance (and errors
# clustered in one image region), while unrelated hashes almost never
# collide at 16 bits. Verify stages then apply the real hamming/distance
# thresholds, so the band width only trades cheap comparisons for recall.
_INDEX_BANDS = 4
_INDEX_BAND_BITS = 16
_INDEX_BAND_MASK = (1 << _INDEX_BAND_BITS) - 1

# Timeline-offset probe. An advertisement, station ident or studio intro
# prepended to one copy shifts every frame of that copy against the other,
# and every stage that assumes "frame i of A is the same moment as frame i
# of B" is blind to it: the corresponding-timeline comparison reads
# distance ~0.16 at every index, the trim path only runs when the
# *durations* diverge (a 20s ad plus a little extra tail leaves the two
# lengths inside the 2% tolerance), and the sparse gate witnesses no shared
# frame because its samples land ~20s apart in content. Such a pair never
# reached review at all — a short ad was enough to hide a true duplicate.
#
# The probe measures the shift from the head of both files (one sequential
# decode each at 1fps: an ad is a whole number of seconds, and a second of
# content offset still correlates well above the bar), then confirms it at
# six points spread across the whole timeline. Nothing coincidental
# survives six confirmations 500s apart.

# Head window decoded to measure the shift.
_OFFSET_LEADIN_SECONDS = 180.0
# One sample per second of video.
_OFFSET_LEADIN_FPS = 1.0
# 32x32: enough detail to reject a lookalike scene, small enough to decode
# 180 of them per file.
_OFFSET_PROBE_SIZE = 32
# "This checkpoint shows the same moment." Measured on a real pair 2.5x
# apart in bitrate whose true shift is 19.8s but whose integer-second
# alignment lands at 20.0: 0.635 to 0.989, mean 0.82. The same frames one
# second off the shift measure 0.15-0.35, and unrelated content 0.00-0.30,
# so 0.60 sits in the gap with room on both sides.
#
# Correlation is a *layout* measure: two unrelated dark scenes with a person
# in the middle correlate 0.7-0.76, which is why a checkpoint must also agree
# in pixel distance (the same normalized bar the frame comparison uses). A
# pair of unrelated 22-minute videos passed on correlation alone (5 of 6
# checkpoints, mean 0.657); its distances at those same checkpoints were
# 0.116-0.174, while a genuine 4s-shifted copy measured 0.026-0.044.
_OFFSET_MIN_CORRELATION = 0.60
# Mean correlation over every checkpoint for a VERY_LIKELY verdict. Below
# this the pair is real but the copies differ enough to deserve a look.
_OFFSET_VERY_LIKELY_CORRELATION = 0.80
# Matched seconds required before a shift is worth confirming.
_OFFSET_MIN_LEAD_MATCHES = 8
# Confirmation points, as fractions of the shorter duration.
_OFFSET_VERIFY_FRACTIONS = (0.15, 0.28, 0.41, 0.54, 0.67, 0.80)
_OFFSET_VERIFY_MIN_MATCHES = 4
# A longer ad than this is out of scope.
_OFFSET_MAX_SHIFT_SECONDS = 180
# Only near-equal lengths are probed: a real trim is the trim path's job.
# This is the probe's own sanity bound on top of matching.min_duration_ratio,
# which has already removed every pair whose shorter copy is under that
# fraction of the longer one before a probe candidate is even considered.
_OFFSET_MAX_DURATION_GAP = 300.0
_OFFSET_MIN_DURATION_SECONDS = 30.0
# A head bag shorter than this describes no lead-in at all: treat it as a
# failed extraction rather than caching something that can never match
_HEAD_BAG_MIN_FRAMES = 4

# A fingerprint may end early only because the rest of the file was blank
# (end credits), never because a decode failed: a half-extracted fingerprint
# presents itself as a description of the whole video, and the comparison
# stretches it across the other file's full timeline. Below this share of
# the sample span, the samples after the last kept frame have to explain
# themselves — decoded-and-blank is a real tail, failed is a truncation.
_FINGERPRINT_TAIL_SHARE = 0.9
# The same question for a cached fingerprint, which cannot say *why* its
# tail is missing. The bar sits lower because a legitimate black tail would
# otherwise be re-extracted on every scan, and it still catches both
# half-extracted rows found on the reference library (52% and 68% of the
# span). Cost of being wrong: one redundant extraction, not a wrong verdict.
_CACHED_SPAN_SHARE = 0.8
# Failed tail samples are retried in ffmpeg batches of this size. Everything
# before the tail decoded, so this is the one place a transient failure (a
# drive waking up, a child killed by the memory guard) is still recoverable.
_TAIL_RETRY_SAMPLES = 12
# Correspondence tolerance, as a share of one sample-grid step. A step is
# ~1% of a long video; 0.6 of a step absorbs the sub-step drift of two
# durations inside the tolerance without pairing neighbouring samples.
_ALIGN_TOLERANCE_STEPS = 0.6

# Version of the *verdict* the probe records for a pair it judged and found
# no shift in. Every constant that decides such a verdict is in the string,
# so changing any of them retires the whole cache instead of silently
# answering with an older algorithm's conclusions.
_OFFSET_VERDICT_VERSION = (
    f"offset-verdict-v1-s{_OFFSET_LEADIN_SECONDS:g}-f{_OFFSET_LEADIN_FPS:g}"
    f"-w{_OFFSET_PROBE_SIZE}-c{_OFFSET_MIN_CORRELATION:g}"
    f"-l{_OFFSET_MIN_LEAD_MATCHES}-v{_OFFSET_VERIFY_MIN_MATCHES}"
    f"-x{_OFFSET_MAX_SHIFT_SECONDS}"
)

# A negative verdict now carries the measurements the decision used, so a
# changed distance or divergence bar can re-judge the same bytes without
# decoding either file again. The marker versions that payload.
_OFFSET_EVIDENCE_VERSION = 1

# Verdicts written before the payload existed carry the bars they were judged
# with in their version string. Such a row is still valid when it was decided
# at least as strictly as the current settings: a checkpoint that fails a
# stricter bar also failed the looser one it was measured against.
_LEGACY_OFFSET_VERDICT_VERSION = re.compile(
    re.escape(_OFFSET_VERDICT_VERSION)
    + r"-d([0-9]+(?:\.[0-9]+)?)-g([0-9]+(?:\.[0-9]+)?)-b([0-9]+(?:\.[0-9]+)?)"
)


def _unpack_hashes(blob: bytes) -> tuple[int, ...]:
    """Decode a signature hash blob into 64-bit ints."""
    return tuple(int.from_bytes(blob[i : i + 8], "little") for i in range(0, len(blob) - 7, 8))


@dataclass(frozen=True)
class FingerprintData:
    """Decoded visual fingerprint for one file."""

    frame_count: int
    timestamps: tuple[float, ...]
    frames: bytes
    frame_size: int

    def frame(self, index: int) -> bytes:
        """Return the grayscale bytes for one frame."""
        start = index * self.frame_size
        return self.frames[start : start + self.frame_size]


def _usable_frames(
    timestamps: list[float], frames: list[bytes | None]
) -> tuple[list[bytes], list[float]]:
    """Frames worth storing: decodable and not blank, in timeline order.

    Sampling stops after three consecutive failed decodes — on a broken file
    every seek fails, and the rest of the list would cost a timeout each.
    """
    kept_frames: list[bytes] = []
    kept_timestamps: list[float] = []
    consecutive_failures = 0
    for timestamp, frame in zip(timestamps, frames, strict=True):
        if frame is None:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                break
            continue
        consecutive_failures = 0
        if is_blank_frame(frame):
            continue  # down-weight uninformative frames
        kept_frames.append(frame)
        kept_timestamps.append(timestamp)
    return kept_frames, kept_timestamps


def _fingerprint_is_complete(
    timestamps: list[float], frames: list[bytes | None], kept_timestamps: list[float]
) -> bool:
    """True when sampling reached the end of the file.

    Blank frames are dropped on purpose (credits carry no signal), so the
    last *kept* sample may legitimately sit well before the last sample. What
    decides is what happened to the samples after it: decoded-and-blank is a
    real tail, failed or never attempted is a truncated extraction.

    The distinction matters because a stored fingerprint carries no record of
    how much of the video it describes. A truncated one is compared as if it
    covered the whole file: on a byte-identical pair whose first fingerprint
    stopped at 52% of the film, the index mapping read 15% match.
    """
    if not timestamps:
        return True
    if not kept_timestamps:
        return False
    if kept_timestamps[-1] >= _FINGERPRINT_TAIL_SHARE * timestamps[-1]:
        return True
    last_kept_index = max(
        index for index, timestamp in enumerate(timestamps) if timestamp <= kept_timestamps[-1]
    )
    return all(frame is not None for frame in frames[last_kept_index + 1 :])


def _corresponding_frames(
    fp_a: FingerprintData,
    duration_a: float,
    fp_b: FingerprintData,
    duration_b: float,
    step_a: float,
    step_b: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Frame indices of two fingerprints that show the same moment.

    Fingerprints sample fixed *relative* positions (5%..95% of each file), so
    position — not index — identifies the moment, and the share of the
    duration is the coordinate both files agree on.

    Scaling one index range onto the other used to stand in for this, which
    only holds while both fingerprints have a sample at every grid position.
    A dropped blank frame, a failed decode or an older half-extracted row
    shifts every index after it: frame 30 of A was then compared with frame
    57 of B, i.e. different moments of the film. On the reference library
    such a pair — 178 bytes apart, identical at every shared timestamp —
    scored 15%.

    A sample with no counterpart inside the tolerance has no counterpart:
    it is skipped rather than matched to the nearest survivor, because a
    missing frame is missing data, not a mismatch.
    """
    if not fp_a.frame_count or not fp_b.frame_count or duration_a <= 0 or duration_b <= 0:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty
    rel_a = np.asarray(fp_a.timestamps, dtype=float) / duration_a
    rel_b = np.asarray(fp_b.timestamps, dtype=float) / duration_b
    right = np.clip(np.searchsorted(rel_b, rel_a), 0, len(rel_b) - 1)
    left = np.clip(right - 1, 0, len(rel_b) - 1)
    delta_left = np.abs(rel_a - rel_b[left])
    delta_right = np.abs(rel_b[right] - rel_a)
    nearest = np.where(delta_left <= delta_right, left, right)
    tolerance = _ALIGN_TOLERANCE_STEPS * max(step_a, step_b)
    index_a: list[int] = []
    index_b: list[int] = []
    used: set[int] = set()
    for i in np.nonzero(np.minimum(delta_left, delta_right) <= tolerance)[0]:
        j = int(nearest[i])
        if j in used:
            continue  # one counterpart per frame; keep the first in order
        used.add(j)
        index_a.append(int(i))
        index_b.append(j)
    return np.asarray(index_a, dtype=np.int64), np.asarray(index_b, dtype=np.int64)


@dataclass(frozen=True)
class SignatureData:
    """Sparse content signature for candidate blocking."""

    frame_count: int
    timestamps: tuple[float, ...]
    frames: bytes
    hashes: tuple[int, ...]
    frame_size: int

    def frame(self, index: int) -> bytes:
        """Return the grayscale bytes for one sparse frame."""
        start = index * self.frame_size
        return self.frames[start : start + self.frame_size]


def _divergent_block_fraction(
    mat_a: np.ndarray,
    mat_b: np.ndarray,
    width: int,
    height: int,
    cutoff: float,
) -> float:
    """Fraction of 4x4 spatial blocks whose distance exceeds the cutoff.

    mat_a/mat_b are matched frame rows, (n, width*height) uint8 or int.
    A re-encode diverges smoothly; same-scene-different-content keeps the
    layout but differs in detail, so a few blocks spike well above the
    global mean even when every frame "matches" overall.
    """
    n = mat_a.shape[0]
    if n == 0:
        return 0.0
    b = 4
    d = np.abs(mat_a.astype(np.int16) - mat_b.astype(np.int16)) / 255.0
    blocks = d.reshape(n, height // b, b, width // b, b).mean(axis=(2, 4))
    return float(np.mean(blocks > cutoff))


def _downscale_gray(frame: bytes, size: int, factor: int = 2) -> bytes:
    """Average ``factor`` x ``factor`` blocks of a ``size`` x ``size`` frame.

    The offset probe reads 32x32 so the head-window vote has enough detail to
    propose a shift; the *decision* at a checkpoint is made on the same
    16x16 scale the frame comparison uses, so both paths speak one threshold.
    """
    small = size // factor
    if small <= 0 or len(frame) != size * size:
        return b""
    out = bytearray(small * small)
    area = factor * factor
    for y in range(small):
        for x in range(small):
            total = 0
            for dy in range(factor):
                start = (y * factor + dy) * size + x * factor
                total += sum(frame[start : start + factor])
            out[y * small + x] = total // area
    return bytes(out)


def _checkpoint_matches(
    correlation: float,
    distance: float,
    divergence: float,
    config: MatchingConfig,
) -> bool:
    """Whether one checkpoint shows the same picture in both files.

    Three bars, all measured on the checkpoint itself:

    - correlation: the *layout* agrees (cheap, brightness-invariant);
    - pixel distance: the picture agrees, not only the layout;
    - divergent blocks: the *detail* agrees. Different videos with similar
      framing and lighting match on layout and can sit just under the distance
      bar while whole patches of the frame differ. Measured on a false pair:
      distances 0.089-0.141 (under the 0.141 bar) with 6-50% of its 4x4
      blocks diverging, against 0% at every checkpoint of a genuine copy.
    """
    return (
        correlation >= _OFFSET_MIN_CORRELATION
        and distance <= config.frame_match_max_distance
        and divergence <= config.vl_local_divergence_max
    )


def _shared_hash_frames(sig_a: SignatureData, sig_b: SignatureData, hamming_max: int) -> int:
    """Count sig_a frames that have a close hash match in sig_b."""
    shared = 0
    for ha in sig_a.hashes:
        for hb in sig_b.hashes:
            if (ha ^ hb).bit_count() <= hamming_max:
                shared += 1
                break
    return shared


def _frame_matrix(frames: list[bytes], frame_size: int) -> np.ndarray:
    """Stack frames into an (n, frame_size) float32 matrix."""
    return (
        np.frombuffer(b"".join(frames), dtype=np.uint8)
        .reshape(len(frames), frame_size)
        .astype(np.float32)
    )


def _correlation_matrix(mat_a: np.ndarray, mat_b: np.ndarray) -> np.ndarray:
    """Pearson correlation between every row of one frame set and the other.

    Per-frame mean/std normalisation is what lets the same footage survive a
    2.5x bitrate difference. It is a *layout* measure: different videos with
    similar framing and lighting have measured 0.667-0.808, above the 0.60
    checkpoint bar — which is why the probe also requires pixel distance and
    local detail (see ``_checkpoint_matches``).
    """
    a = mat_a - mat_a.mean(axis=1, keepdims=True)
    b = mat_b - mat_b.mean(axis=1, keepdims=True)
    a /= np.linalg.norm(a, axis=1, keepdims=True) + 1e-6
    b /= np.linalg.norm(b, axis=1, keepdims=True) + 1e-6
    return a @ b.T


@dataclass(frozen=True)
class OffsetProbeResult:
    """Confirmed constant timeline shift between two near-equal files."""

    offset_seconds: float
    lead_matches: int
    verified: int
    checked: int
    correlations: tuple[float, ...]
    distances: tuple[float, ...] = ()
    divergences: tuple[float, ...] = ()

    @property
    def mean_correlation(self) -> float:
        """Mean correlation across the checkpoints that could be read."""
        if not self.correlations:
            return 0.0
        return sum(self.correlations) / len(self.correlations)

    @property
    def mean_distance(self) -> float:
        """Mean normalized pixel distance across the checkpoints."""
        if not self.distances:
            return 1.0
        return sum(self.distances) / len(self.distances)

    @property
    def mean_divergence(self) -> float:
        """Mean divergent-block fraction across the checkpoints.

        No data means no penalty, the same convention the audio gate uses.
        """
        if not self.divergences:
            return 0.0
        return sum(self.divergences) / len(self.divergences)


@dataclass(frozen=True)
class _StoredOffsetVerdict:
    """What a stored negative verdict still says under this scan's settings.

    ``still_a_miss`` is the verdict. ``hit`` is set when the stored
    measurements now clear the current bars although the row was written as
    a miss — a looser distance or divergence bar since then — so the pair can
    be reported without decoding it again.
    """

    still_a_miss: bool
    hit: OffsetProbeResult | None = None


@dataclass
class PerceptualMatchResult:
    """Statistics from a perceptual matching pass."""

    files_signed: int = 0
    signature_cache_hits: int = 0
    signature_errors: int = 0
    signature_pairs: int = 0
    candidate_files: int = 0
    candidate_pairs: int = 0
    files_fingerprinted: int = 0
    fingerprint_cache_hits: int = 0
    fingerprint_errors: int = 0
    files_head_bagged: int = 0
    head_bag_cache_hits: int = 0
    comparisons: int = 0
    # Pairs the duration gate removed before anything was computed for them:
    # their shorter copy is under ``matching.min_duration_ratio`` of the
    # longer one, so they cannot be duplicates of each other. Counted after
    # the indexes have proposed them, which is where the saving starts —
    # fingerprints, frame distances and shift probes are all skipped.
    duration_pruned: int = 0
    very_likely: int = 0
    possible: int = 0
    rejected: int = 0
    offset_probes: int = 0
    offset_probe_hits: int = 0
    # Every pair that passed the probe's filters. It equals ``offset_probes``
    # plus the ones already judged, and it exists so "62 probed" cannot be read
    # as "62 candidates" — the filters, not a cap, decide what is left out
    offset_probes_candidates: int = 0
    # Candidates an earlier scan already judged and this one did not re-probe
    offset_probes_cached: int = 0
    # Pairs an earlier scan confirmed as shifted and this one re-tested. They
    # are the reason a reported shift stays reported: the review reads one
    # scan's comparisons, so an un-re-checked pair silently disappears
    offset_probes_reconfirmed: int = 0
    errors: list[str] = field(default_factory=list)


class PerceptualMatchService:
    """Finds perceptually equivalent videos and records pair evidence."""

    def __init__(
        self,
        repository: DatabaseRepository,
        config: MatchingConfig,
        progress_callback: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        """Initialize the perceptual-match service.

        Args:
            repository: Database repository.
            config: Matching configuration.
            progress_callback: Optional callback for progress events.
        """
        self.repository = repository
        self.config = config
        self.progress_callback = progress_callback
        self.frame_size = config.frame_width * config.frame_height
        self._dismissed: set[tuple[str, str]] = set()
        self._key_by_id: dict[int, str] = {}
        self._audio: dict[int, bytes] = {}
        # Crop is applied before the frame grid, so enabling it changes the
        # pixels a cached signature or fingerprint was built from.
        crop_suffix = f"-c{CROP_ALGORITHM_VERSION}" if config.crop_detect else ""
        # Fingerprint version encodes the sampling/grid parameters, so cached
        # fingerprints auto-invalidate when matching config changes
        self._fp_version = (
            f"{FINGERPRINT_ALGORITHM_VERSION}-w{config.frame_width}"
            f"-h{config.frame_height}"
            f"-i{config.target_frame_interval_seconds:g}"
            f"-f{config.min_frames}-{config.max_frames}"
            f"{crop_suffix}"
        )
        # Edge probes are part of the signature layout; the algorithm version
        # bumps whenever SIGNATURE_EDGE_FRACTIONS or hashing changes
        self._sig_version = (
            f"{SIGNATURE_ALGORITHM_VERSION}-w{config.frame_width}"
            f"-h{config.frame_height}-f{config.signature_frames}"
            f"{crop_suffix}"
        )
        self._audio_version = (
            f"{AUDIO_ALGORITHM_VERSION}-w{config.audio_windows}"
            f"-b{config.audio_bands}-r{config.audio_sample_rate}"
            f"-s{config.audio_window_seconds:g}"
        )
        # Head-window cache version: sampling and hashing parameters decide it
        self._bag_version = (
            f"{HEAD_BAG_ALGORITHM_VERSION}-s{config.head_bag_seconds:g}"
            f"-f{config.head_bag_fps:g}-w{config.frame_width}"
            f"-h{config.frame_height}"
        )
        # A stored "no shift" verdict answers for the measurements it
        # recorded, not for the bars they were judged with: the distance and
        # divergence caps are re-applied when the row is read, so tightening
        # or loosening strictness re-judges without decoding the pair again.
        # Only the per-block cutoff stays in the key — it changes the
        # divergence values themselves, not the bar they are compared with.
        self._offset_verdict_version = (
            f"{_OFFSET_VERDICT_VERSION}-e{_OFFSET_EVIDENCE_VERSION}"
            f"-b{config.vl_local_divergence_distance:g}"
        )
        # file_id -> audio signature bytes for the current scan (empty
        # bytes = probed but no audio stream); {} when audio is disabled
        self._audio: dict[int, bytes] = {}
        # file_id -> cropdetect result shared across signature and
        # fingerprint passes for the same scan
        self._crop_cache: dict[int, str | None] = {}
        # file_id -> head-window frames (1fps gray), decoded once per scan:
        # the shift an ad leaves behind belongs to the file, not the pair
        self._lead_cache: dict[int, list[bytes]] = {}
        # Pairs a timeline shift hid from every index-aligned stage. Their
        # verdict is recorded directly — dense fingerprints cannot compare
        # frames that never line up
        self._offset_hits: list[tuple[dict[str, Any], dict[str, Any], OffsetProbeResult]] = []

    def _emit_progress(self, event: ProgressEvent) -> None:
        if self.progress_callback:
            self.progress_callback(event)

    def _detect_crop(self, file_row: dict[str, Any], duration: float) -> str | None:
        """Resolve the letterbox crop box for a file, if enabled.

        Cached per file id — signature and fingerprint passes would
        otherwise each pay a cropdetect subprocess for the same file.
        """
        if not self.config.crop_detect:
            return None
        file_id = file_row["id"]
        if file_id not in self._crop_cache:
            width = int(file_row.get("_width") or 0)
            height = int(file_row.get("_height") or 0)
            self._crop_cache[file_id] = detect_crop(
                Path(file_row["original_path"]),
                duration,
                ffmpeg_path=self.config.ffmpeg_path,
                # Once per file and cached — give heavy files real
                # headroom (4K window decodes legitimately exceed 30s)
                timeout_seconds=max(self.config.frame_timeout_seconds, 90),
                frame_width=width or None,
                frame_height=height or None,
            )
        return self._crop_cache[file_id]

    def find_perceptual_duplicates(
        self,
        scan_id: int,
        cancellation_token: CancellationToken | None = None,
    ) -> PerceptualMatchResult:
        """Run candidate blocking, fingerprinting, and pairwise comparison.

        Args:
            scan_id: Scan ID to attribute results to.
            cancellation_token: Optional cancellation token.

        Returns:
            Perceptual matching statistics.
        """
        result = PerceptualMatchResult()
        # EXACT rows written by the hash pass must survive — only the
        # classes this pass produces are recomputed.
        for classification in (CLASS_VERY_LIKELY, CLASS_POSSIBLE, CLASS_REJECTED, CLASS_ERROR):
            self.repository.delete_pair_comparisons(scan_id, classification)

        # Reviewer-dismissed false positives are skipped in candidate
        # pairing, permanently. Dismissal is per pair — A–B being hidden
        # does not hide A–C.
        self._dismissed = self.repository.get_dismissed_pairs()
        # Crop boxes are recomputed per scan — a replaced file (same id,
        # new mtime) must not inherit a stale one
        self._crop_cache = {}
        self._lead_cache = {}
        self._offset_hits = []

        # Files eligible for matching: present, probed OK, has video, duration
        # known — and under a root that was reachable in this scan
        files = self._eligible_files(scan_id)
        self._key_by_id = {f["id"]: f["normalized_path_key"] for f in files}
        if len(files) < 2:
            self.repository.commit()
            return result

        # Files already proven byte-identical are EXACT pairs. They still
        # need one signature so a non-identical sibling can match them,
        # but they are not compared perceptually against each other.
        exact_group_of = self._exact_pair_map(scan_id)

        # Sparse content signatures for every eligible file (cheap: a few
        # frames hashed once, then cached by file identity). Byte-identical
        # files share content, so only one member per exact group is signed.
        signatures = self._ensure_signatures(files, result, exact_group_of, cancellation_token)
        if cancellation_token and cancellation_token.is_cancelled():
            self.repository.commit()
            return result

        # Dense head-window hashes. The only candidate signal that survives a
        # prepended advert, and cached by file identity, so only the first
        # scan after a version bump decodes the library
        head_bags: dict[int, tuple[int, ...]] = {}
        if self.config.head_bag_enabled:
            head_bags = self._ensure_head_bags(files, result, cancellation_token)
            if cancellation_token and cancellation_token.is_cancelled():
                self.repository.commit()
                return result

        # Candidate blocking: only pairs whose sparse signatures show real
        # overlap proceed to dense fingerprint extraction and comparison
        pairs = self._generate_candidate_pairs(
            files,
            signatures,
            exact_group_of,
            result,
            cancellation_token,
            head_bags,
        )
        result.candidate_pairs = len(pairs)
        result.candidate_files = len({f["id"] for a, b, _ in pairs for f in (a, b)})

        offset_pairs = [
            (short_file, long_file, False)
            for short_file, long_file, _ in self._offset_hits
        ]
        if not pairs and not offset_pairs:
            self.repository.commit()
            return result

        # Fingerprint only candidate files
        fingerprints = {}
        if pairs:
            fingerprints = self._ensure_fingerprints(files, pairs, result, cancellation_token)
            if cancellation_token and cancellation_token.is_cancelled():
                self.repository.commit()
                return result

        # Audio signatures for candidate and offset-hit files — the
        # VERY_LIKELY divergence gate. Same cost class as fingerprints
        # (short PCM windows), cached per file like other extractions
        if self.config.audio_enabled:
            self._audio = self._ensure_audio(pairs + offset_pairs, result, cancellation_token)
            if cancellation_token and cancellation_token.is_cancelled():
                self.repository.commit()
                return result

        # Verdicts for pairs a timeline shift hid from every index-aligned
        # stage. They never became candidates, so nothing else can record
        # them — and a scan with no ordinary candidates can still have them.
        # Audio is ready now, so the shift verdict can apply the gate.
        for file_short, file_long, probe in self._offset_hits:
            self._record_offset_hit(scan_id, file_short, file_long, probe, result)

        if not pairs:
            self.repository.commit()
            return result

        # Compare each candidate pair on the corresponding timeline
        for i, (file_a, file_b, is_trim) in enumerate(pairs):
            if cancellation_token and cancellation_token.is_cancelled():
                break

            self._emit_progress(
                ProgressEvent(
                    phase="comparing",
                    completed_count=i + 1,
                    total_count=len(pairs),
                    current_path=file_a["original_path"],
                    peer_path=file_b["original_path"],
                    message=f"Comparing {i + 1}/{len(pairs)}",
                )
            )
            self._compare_pair(scan_id, file_a, file_b, is_trim, fingerprints, result)
            result.comparisons += 1

        self.repository.commit()
        return result

    def _eligible_files(self, scan_id: int) -> list[dict[str, Any]]:
        """Present, non-symlink files with successful probes and known duration."""
        scanned_roots = self.repository.get_scanned_root_ids(scan_id)
        eligible = []
        for f in self.repository.get_present_files():
            if f["is_symlink"] or f["root_id"] not in scanned_roots:
                continue
            probe = self.repository.get_probe_metadata(f["id"])
            if (
                probe
                and probe["status"] == ProbeStatus.SUCCESS.value
                and probe["has_video"]
                and probe["container_duration"]
            ):
                f["_duration"] = float(probe["container_duration"])
                f["_width"] = probe.get("primary_video_width") or 0
                f["_height"] = probe.get("primary_video_height") or 0
                # ffprobe is authoritative about whether a track exists.
                # The audio extractor cannot tell "no stream" from "decode
                # failed", so it must never be the one to decide.
                audio_streams = probe.get("audio_stream_count")
                f["_audio_stream_count"] = 1 if audio_streams is None else int(audio_streams)
                eligible.append(f)
        return eligible

    def _exact_pair_map(self, scan_id: int) -> dict[int, int]:
        """Map each file to a representative of its byte-identical set.

        Built from this scan's EXACT pairs only. Connected identical files
        share one representative so only one of them is signed; the map is
        not a review group and is not persisted.
        """
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for row in self.repository.get_pair_comparisons(scan_id):
            if row["classification"] != "EXACT":
                continue
            a, b = row["file_a_id"], row["file_b_id"]
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            parent[find(a)] = find(b)
        return {file_id: find(file_id) for file_id in parent}

    def _ensure_signatures(
        self,
        files: list[dict[str, Any]],
        result: PerceptualMatchResult,
        exact_group_of: dict[int, int],
        cancellation_token: CancellationToken | None,
    ) -> dict[int, SignatureData]:
        """Compute or reuse sparse content signatures for all files."""
        signatures: dict[int, SignatureData] = {}
        signed_groups: set[int] = set()
        pending: list[dict[str, Any]] = []

        # Resolve cached signatures first; queue the rest for extraction
        for file_row in files:
            gid = exact_group_of.get(file_row["id"])
            if gid is not None:
                if gid in signed_groups:
                    continue  # identical bytes => identical content; sibling already signed
                signed_groups.add(gid)
            cached = self.repository.get_signature(file_row["id"])
            if (
                cached
                and cached["file_size"] == file_row["size"]
                and cached["mtime_ns"] == file_row["mtime_ns"]
                and cached["algorithm_version"] == self._sig_version
            ):
                result.signature_cache_hits += 1
                signatures[file_row["id"]] = SignatureData(
                    frame_count=cached["frame_count"],
                    timestamps=tuple(json.loads(cached["timestamps"])),
                    frames=cached["frames"],
                    hashes=_unpack_hashes(cached["hashes"]),
                    frame_size=self.frame_size,
                )
            else:
                pending.append(file_row)

        # FFmpeg extraction is I/O- and decode-bound; run a small pool of
        # workers while DB writes stay serialized on this thread
        total = len(files)
        done = len(signatures)
        workers = max(1, self.config.signature_workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._extract_signature, f): f for f in pending}
            for future in as_completed(futures):
                if cancellation_token and cancellation_token.is_cancelled():
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                file_row = futures[future]
                done += 1
                self._emit_progress(
                    ProgressEvent(
                        phase="signing",
                        completed_count=done,
                        total_count=total,
                        current_path=file_row["original_path"],
                        message=f"Signing {done}/{total}",
                    )
                )
                sig, error_msg = future.result()
                if error_msg:
                    logger.error(error_msg)
                    result.errors.append(error_msg)
                    result.signature_errors += 1
                if sig is None:
                    continue
                self.repository.upsert_signature(
                    file_row["id"],
                    self._sig_version,
                    json.dumps(list(sig.timestamps)),
                    sig.frames,
                    b"".join(h.to_bytes(8, "little") for h in sig.hashes),
                    file_row["size"],
                    file_row["mtime_ns"],
                )
                # Commit per file: extraction is slow, so each signature is
                # durable progress a crash or interruption must not lose
                self.repository.commit()
                signatures[file_row["id"]] = sig
                result.files_signed += 1

        return signatures

    def _extract_signature(
        self, file_row: dict[str, Any]
    ) -> tuple[SignatureData | None, str | None]:
        """Extract sparse frames and compute a content signature.

        Runs on a worker thread; performs no repository writes. Returns the
        signature plus an error message on failure.
        """
        duration = file_row["_duration"]
        timestamps = signature_timestamps(duration, self.config.signature_frames)
        crop = self._detect_crop(file_row, duration)

        frames_out = extract_gray_frames(
            Path(file_row["original_path"]),
            timestamps,
            ffmpeg_path=self.config.ffmpeg_path,
            width=self.config.frame_width,
            height=self.config.frame_height,
            crop=crop,
        )
        kept_frames: list[bytes] = []
        kept_timestamps: list[float] = []
        kept_hashes: list[int] = []
        consecutive_failures = 0
        for t, frame in zip(timestamps, frames_out, strict=True):
            if frame is None:
                # Extraction failed (timeout/undecodable). Bail out fast
                # instead of paying it for every timestamp.
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    break
                continue
            consecutive_failures = 0
            if is_blank_frame(frame):
                continue
            kept_frames.append(frame)
            kept_timestamps.append(t)
            kept_hashes.append(
                frame_ahash(frame, self.config.frame_width, self.config.frame_height)
            )

        if len(kept_frames) < max(2, len(timestamps) // 2):
            error_msg = (
                f"Signature failed for {file_row['original_path']}: "
                f"only {len(kept_frames)}/{len(timestamps)} usable frames"
            )
            return None, error_msg

        return (
            SignatureData(
                frame_count=len(kept_frames),
                timestamps=tuple(kept_timestamps),
                frames=b"".join(kept_frames),
                hashes=tuple(kept_hashes),
                frame_size=self.frame_size,
            ),
            None,
        )

    def _generate_candidate_pairs(
        self,
        files: list[dict[str, Any]],
        signatures: dict[int, SignatureData],
        exact_group_of: dict[int, int],
        result: PerceptualMatchResult,
        cancellation_token: CancellationToken | None,
        head_bags: dict[int, tuple[int, ...]] | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, Any], bool]]:
        """Content-signature-gated candidate pair generation.

        An inverted index over 16-bit bands of the per-frame perceptual
        hashes collects file pairs that share a band — genuinely similar
        frames collide at that width with high probability, while
        unrelated ones almost never do. Each collected pair is verified in
        two stages: shared frame hashes within a hamming bound, then real
        frame distances on the sparse frames. Only overlaps of at least
        ``signature_min_shared_frames`` frames proceed to dense
        fingerprints and timeline comparison. This replaces duration
        blocking, which cannot discriminate in large collections.
        """
        by_id = {f["id"]: f for f in files}

        # Inverted index: (band index, 16-bit band) -> file ids
        prefix_index: dict[tuple[int, int], set[int]] = {}
        for file_id, sig in signatures.items():
            for h in sig.hashes:
                for band in range(_INDEX_BANDS):
                    key = (band, (h >> (band * _INDEX_BAND_BITS)) & _INDEX_BAND_MASK)
                    prefix_index.setdefault(key, set()).add(file_id)

        cap = self.config.signature_max_bucket_files

        def _pairs_with_hits(
            index: dict[Any, set[int]], min_hits: int
        ) -> dict[tuple[int, int], int]:
            """Pairs colliding on >= min_hits distinct keys, with their count.

            Only qualifying pairs are materialised — the loose set holds every
            pair seen at all, which is an order of magnitude larger.
            """
            loose: set[tuple[int, int]] = set()
            counts: dict[tuple[int, int], int] = {}
            for bucket in index.values():
                if len(bucket) > cap:
                    continue  # uninformative slice (e.g. uniform dark frames)
                members = sorted(bucket)
                for ai in range(len(members)):
                    for bi in range(ai + 1, len(members)):
                        pair = (members[ai], members[bi])
                        if pair in loose:
                            counts[pair] = counts.get(pair, 1) + 1
                        else:
                            loose.add(pair)
            return {pair: hits for pair, hits in counts.items() if hits >= min_hits}

        # A pair must collide on >= signature_min_band_hits average-hash
        # band keys to reach stage 1 — single coincidental 16-bit-band hits
        # dominate the loose set on large collections (millions) but can
        # never produce a real match: two near-identical frames share
        # several bands, not one.
        candidate_set = set(_pairs_with_hits(prefix_index, self.config.signature_min_band_hits))
        del prefix_index

        # The head-window index proposes pairs of its own — a copy behind a
        # prepended advert shares its opening frames but no sparse sample —
        # so it joins the duration gate below instead of being filtered
        # later, after the dense stages have already run.
        bag_hits = self._head_bag_hits(head_bags or {})

        # Duration gate. Everything the indexes proposed passes one number
        # before a single frame is decoded: a pair whose shorter copy is
        # under ``matching.min_duration_ratio`` of the longer one cannot be a
        # duplicate of it, and comparing one is the most expensive work a
        # scan can do for no return. Applied to the union rather than per
        # index, so a pair two indexes proposed is counted once.
        duration_dropped = self._pairs_below_duration_gate(candidate_set | set(bag_hits), by_id)
        if duration_dropped:
            result.duration_pruned = len(duration_dropped)
            candidate_set.difference_update(duration_dropped)
            bag_hits = {key: hits for key, hits in bag_hits.items() if key not in duration_dropped}

        # Sorted for determinism: identical inputs must produce identical
        # comparison order, scores and stored evidence
        candidate_ids = sorted(candidate_set)
        del candidate_set
        result.signature_pairs = len(candidate_ids)

        # Stage 1 verify: >= min_shared frame hashes within the hamming bound
        hamming_max = self.config.signature_max_hamming
        min_shared = self.config.signature_min_shared_frames
        hash_verified: list[tuple[int, int]] = []
        n_loose = len(candidate_ids)
        for i, (id_a, id_b) in enumerate(candidate_ids):
            if i % 4096 == 0:
                if cancellation_token and cancellation_token.is_cancelled():
                    return []
                self._emit_progress(
                    ProgressEvent(
                        phase="blocking",
                        completed_count=i,
                        total_count=n_loose,
                        message=f"Blocking {i:,}/{n_loose:,}",
                    )
                )
            sig_a, sig_b = signatures[id_a], signatures[id_b]
            shared = _shared_hash_frames(sig_a, sig_b, hamming_max)
            if shared < min_shared:
                shared = max(shared, _shared_hash_frames(sig_b, sig_a, hamming_max))
            if shared >= min_shared:
                hash_verified.append((id_a, id_b))

        # Stage 2 verify: >= min_shared sparse frames within frame distance.
        # Vectorized: one broadcast distance matrix per pair — the pure-Python
        # version took ~5ms/pair and stalled silently for minutes here
        threshold = self.config.frame_match_max_distance
        mats = {
            fid: np.frombuffer(sig.frames, dtype=np.uint8)
            .reshape(sig.frame_count, self.frame_size)
            .astype(np.int16)
            for fid, sig in signatures.items()
        }
        pairs: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
        n_verified = len(hash_verified)
        for i, (id_a, id_b) in enumerate(hash_verified):
            if i % 512 == 0:
                if cancellation_token and cancellation_token.is_cancelled():
                    return []
                self._emit_progress(
                    ProgressEvent(
                        phase="verifying",
                        completed_count=i,
                        total_count=n_verified,
                        message=f"Verifying {i:,}/{n_verified:,}",
                    )
                )
            if id_a in exact_group_of and exact_group_of.get(id_a) == exact_group_of.get(id_b):
                continue  # already proven byte-identical
            if not _signature_overlap(mats[id_a], mats[id_b], threshold, min_shared):
                continue
            file_a, file_b = by_id[id_a], by_id[id_b]
            tolerance = max(
                self.config.duration_tolerance_seconds,
                self.config.duration_tolerance_ratio
                * min(file_a["_duration"], file_b["_duration"]),
            )
            is_trim = abs(file_a["_duration"] - file_b["_duration"]) > tolerance
            ka = self._key_by_id.get(file_a["id"], "")
            kb = self._key_by_id.get(file_b["id"], "")
            if tuple(sorted((ka, kb))) in self._dismissed:
                continue  # reviewer marked this pair a false positive
            if file_a["_duration"] <= file_b["_duration"]:
                pairs.append((file_a, file_b, is_trim))
            else:
                pairs.append((file_b, file_a, is_trim))
        self._collect_offset_probes(
            bag_hits,
            by_id,
            pairs,
            exact_group_of,
            result,
            cancellation_token,
        )
        return pairs

    def _duration_ratio_allows(self, file_a: dict[str, Any], file_b: dict[str, Any]) -> bool:
        """Whether two files are close enough in length to be worth comparing.

        The gate that keeps a clip from being compared with the two-hour
        compilation that contains it. Lengths are compared as a ratio, not as
        a difference: five minutes short of a ten-minute video and five
        minutes short of a two-hour one are not the same thing, and an
        absolute bound cannot express that.

        A file whose length could not be measured is allowed through. The
        gate exists to skip work that cannot pay off; a missing duration is
        not evidence of a difference, and pruning on it would hide a pair for
        the wrong reason.
        """
        dur_a = float(file_a.get("_duration") or 0.0)
        dur_b = float(file_b.get("_duration") or 0.0)
        if dur_a <= 0 or dur_b <= 0:
            return True
        return min(dur_a, dur_b) / max(dur_a, dur_b) >= self.config.min_duration_ratio

    def _pairs_below_duration_gate(
        self,
        pairs: Iterable[tuple[int, int]],
        by_id: dict[int, dict[str, Any]],
    ) -> set[tuple[int, int]]:
        """The proposed pairs whose lengths rule them out, as a set.

        Returned instead of deleted in place because three indexes propose
        pairs and the same pair can appear in all of them: the caller prunes
        each collection with one set, so what reaches the scan log is a count
        of pairs, not of proposals.
        """
        dropped: set[tuple[int, int]] = set()
        for id_a, id_b in pairs:
            file_a, file_b = by_id.get(id_a), by_id.get(id_b)
            if file_a is None or file_b is None:
                continue  # not an eligible file: nothing to judge
            if not self._duration_ratio_allows(file_a, file_b):
                dropped.add((id_a, id_b))
        return dropped

    def _ensure_head_bags(
        self,
        files: list[dict[str, Any]],
        result: PerceptualMatchResult,
        cancellation_token: CancellationToken | None,
    ) -> dict[int, tuple[int, ...]]:
        """Dense opening-minute hashes per file, cached by identity.

        The only candidate signal that survives a prepended advert: sampled by
        second rather than by fraction of each file's own duration. One
        sequential decode per file, so the first scan after a version bump
        pays for the library and later scans read the cache.
        """
        bags: dict[int, tuple[int, ...]] = {}
        pending: list[dict[str, Any]] = []
        for file_row in files:
            cached = self.repository.get_head_bag(file_row["id"])
            if (
                cached
                and cached["file_size"] == file_row["size"]
                and cached["mtime_ns"] == file_row["mtime_ns"]
                and cached["algorithm_version"] == self._bag_version
            ):
                result.head_bag_cache_hits += 1
                bags[file_row["id"]] = _unpack_hashes(cached["hashes"])
            else:
                pending.append(file_row)
        if not pending:
            return bags

        total = len(pending)
        workers = max(1, self.config.signature_workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._extract_head_bag, f): f for f in pending}
            for done, future in enumerate(as_completed(futures), start=1):
                if cancellation_token and cancellation_token.is_cancelled():
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                file_row = futures[future]
                hashes, error = future.result()
                if error is not None:
                    result.errors.append(error)
                    continue
                result.files_head_bagged += 1
                bags[file_row["id"]] = hashes
                self.repository.upsert_head_bag(
                    file_row["id"],
                    self._bag_version,
                    self.config.head_bag_seconds,
                    b"".join(value.to_bytes(8, "little") for value in hashes),
                    file_row["size"],
                    file_row["mtime_ns"],
                )
                self._emit_progress(
                    ProgressEvent(
                        phase="headbag",
                        completed_count=done,
                        total_count=total,
                        current_path=file_row["original_path"],
                        message=f"Head window {done}/{total}",
                    )
                )
        return bags

    def _extract_head_bag(self, file_row: dict[str, Any]) -> tuple[tuple[int, ...], str | None]:
        """Hash a file's opening minutes, one sample per second.

        Runs on a worker thread; performs no repository writes.
        """
        window = min(self.config.head_bag_seconds, max(1.0, float(file_row["_duration"])))
        frames = extract_gray_window(
            Path(file_row["original_path"]),
            start_seconds=0.0,
            window_seconds=window,
            fps=self.config.head_bag_fps,
            width=self.config.frame_width,
            height=self.config.frame_height,
            ffmpeg_path=self.config.ffmpeg_path,
            timeout_seconds=self.config.frame_timeout_seconds,
        )
        # Black or uniform frames are dropped: every file has them, and one
        # shared dark frame must not stand in for shared content
        hashes = tuple(
            frame_ahash(frame, self.config.frame_width, self.config.frame_height)
            for frame in frames
            if not is_blank_frame(frame)
        )
        if len(hashes) < _HEAD_BAG_MIN_FRAMES:
            return (), (
                f"Head window failed for {file_row['original_path']}: {len(hashes)} usable frames"
            )
        return hashes, None

    def _head_bag_hits(self, bags: dict[int, tuple[int, ...]]) -> dict[tuple[int, int], int]:
        """Pairs whose opening minutes share frames, regardless of when.

        Position-free on purpose: the index claims "both files show these
        frames somewhere in their first minutes", never "at the same
        timestamp", which is exactly the claim a prepended advert breaks.

        Keys are 16-bit bands of each frame hash rather than whole hashes.
        Two encodes of one film rarely agree bit-for-bit: measured on the real
        pair, 5 exact 64-bit matches against 40 frames inside 4 bits. A pair
        within d bits always shares a band while d stays below the band count,
        so banding buys tolerance without a per-pair comparison. Measured
        shared bands: 101 for the 20s-shifted pair, 0 to 3 for unrelated
        files, which is where the default threshold comes from.

        Buckets past the configured cap are dropped as uninformative, the same
        guard the signature index uses — a common lead-in appears in many files
        and would otherwise pair everything with everything.
        """
        index: dict[tuple[int, int], set[int]] = {}
        for file_id, hashes in bags.items():
            for value in set(hashes):
                for band in range(_INDEX_BANDS):
                    key = (
                        band,
                        (value >> (band * _INDEX_BAND_BITS)) & _INDEX_BAND_MASK,
                    )
                    index.setdefault(key, set()).add(file_id)
        cap = self.config.signature_max_bucket_files
        counts: dict[tuple[int, int], int] = {}
        for bucket in index.values():
            if not 2 <= len(bucket) <= cap:
                continue
            members = sorted(bucket)
            for position, first in enumerate(members):
                for second in members[position + 1 :]:
                    pair = (first, second)
                    counts[pair] = counts.get(pair, 0) + 1
        minimum = self.config.head_bag_min_shared
        return {pair: hits for pair, hits in counts.items() if hits >= minimum}

    def _offset_pairs_to_recheck(
        self,
        compared: set[tuple[int, int]],
        verdicts: dict[tuple[int, int], dict[str, Any]],
        by_id: dict[int, dict[str, Any]],
        exact_group_of: dict[int, int],
    ) -> list[tuple[int, int]]:
        """Pairs an earlier scan confirmed as shifted, for this one to re-test.

        A hit leaves no verdict, so nothing in the queue remembers it; the pair
        is proposed again only if the head-window index happens to put it inside
        this scan's candidate set. The Result list reads a single scan's comparisons, so
        a confirmed shift that is not re-probed leaves the list while both files
        sit on disk, which is what "the pairs went missing" looked like from the
        review.

        Re-testing them first is cheap by construction: the pairs are the ones
        already confirmed, so there are as many as there are shifts.

        Returns:
            Sorted file-id pairs, excluding the ones this scan already compared
            (they are on the report anyway), the ones whose verdict now says
            otherwise, the ones whose files moved out of scope, and the ones
            whose lengths fall under ``matching.min_duration_ratio`` — a
            confirmed shift is not re-probed when the gate would not show it.
        """
        pairs: list[tuple[int, int]] = []
        for id_a, id_b in self.repository.get_offset_probe_hit_pairs():
            key = (id_a, id_b) if id_a < id_b else (id_b, id_a)
            if key in compared:
                continue
            file_a, file_b = by_id.get(key[0]), by_id.get(key[1])
            if file_a is None or file_b is None:
                continue
            if min(file_a["_duration"], file_b["_duration"]) < _OFFSET_MIN_DURATION_SECONDS:
                continue
            if abs(file_a["_duration"] - file_b["_duration"]) > _OFFSET_MAX_DURATION_GAP:
                continue
            if not self._duration_ratio_allows(file_a, file_b):
                # Below the review gate: the pair is not a duplicate, so
                # re-testing it would spend two decodes to keep something no
                # list will show. Not counted in duration_pruned — the pair
                # is reached here only because no index proposed it this
                # scan, and it was already counted when one did.
                continue
            group_a = exact_group_of.get(key[0])
            if group_a is not None and group_a == exact_group_of.get(key[1]):
                continue  # already proven byte-identical
            key_a = self._key_by_id.get(key[0], "")
            key_b = self._key_by_id.get(key[1], "")
            if key_a and key_b and tuple(sorted((key_a, key_b))) in self._dismissed:
                continue  # reviewer already called this pair a false positive
            verdict = verdicts.get(key)
            if verdict is not None:
                stored = self._stored_offset_verdict(verdict, file_a, file_b)
                if stored is not None and stored.still_a_miss:
                    continue  # a later probe judged it no shift after all
            pairs.append(key)
        return sorted(pairs)

    def _collect_offset_probes(
        self,
        bag_hits: dict[tuple[int, int], int],
        by_id: dict[int, dict[str, Any]],
        pairs: list[tuple[dict[str, Any], dict[str, Any], bool]],
        exact_group_of: dict[int, int],
        result: PerceptualMatchResult,
        cancellation_token: CancellationToken | None,
    ) -> None:
        """Probe pairs whose timelines may simply start differently.

        Head-window overlap is the candidate source: both files show the same
        frames somewhere in their opening minutes, which is what a prepended
        advert leaves intact — 296 pairs on the 6,440-file reference library at
        a floor of 24 shared bands. It replaced a second tier built on the
        sparse signature's 4x4 layout keys: twelve samples per file made "share
        two of forty-eight bands" a chance event, so that tier proposed 24,002
        candidates after the filters, none above four shared bands, and none of
        the twenty confirmed shifts — see ``offset-probe.md#the-coarse-tier-was-removed``.

        A probe is the most expensive test in the scan — 180 s decoded from each
        file, about ten seconds a pair — so a pair an earlier scan already
        judged is not judged again. A negative verdict stores the checkpoint
        measurements the decision used, so changing strictness only re-applies
        the bars to those numbers; the pair is decoded again only when the
        bytes or the probe's own parameters change. What a hit leaves behind is
        the opposite problem: because it stores no verdict, the pair returns
        only if the head-window index proposes it again, and the review list
        reads one scan's comparisons. So the pairs an earlier scan confirmed
        are re-tested first — a reported shift stays reported, which is the
        whole point of reporting it.

        Every candidate that survives the filters is probed: there is no
        per-scan cap, because a cap made a scan's result set partial by
        construction and asked the user to run the scan again to learn what it
        had skipped. What bounds the phase is the index itself — the shared-band
        floor and the bucket cap — not an arbitrary number of probes. The
        numbers are still reported: candidates, how many were already known, how
        many known shifts were re-checked. Pairs the duration gate has already
        pruned never arrive here at all — the head-window candidates are
        filtered against ``matching.min_duration_ratio`` before this runs.
        """
        # Pairs are stored shorter-first, so comparing them in that order
        # would miss the already-compared ones and probe them again
        compared = {tuple(sorted((file_a["id"], file_b["id"]))) for file_a, file_b, _ in pairs}
        verdicts = self.repository.get_offset_verdicts(
            _OFFSET_VERDICT_VERSION, self._offset_verdict_version
        )
        recheck = self._offset_pairs_to_recheck(compared, verdicts, by_id, exact_group_of)
        if not recheck and not bag_hits:
            return
        recheck_set = set(recheck)
        head_candidates: list[tuple[int, float, int, int]] = []
        cached = 0
        for id_a, id_b in bag_hits:
            key = (id_a, id_b) if id_a < id_b else (id_b, id_a)
            if key in compared or key in recheck_set:
                continue
            file_a, file_b = by_id.get(key[0]), by_id.get(key[1])
            if file_a is None or file_b is None:
                continue
            dur_a, dur_b = file_a["_duration"], file_b["_duration"]
            if min(dur_a, dur_b) < _OFFSET_MIN_DURATION_SECONDS:
                continue
            gap = abs(dur_a - dur_b)
            if gap > _OFFSET_MAX_DURATION_GAP:
                continue
            group_a = exact_group_of.get(key[0])
            if group_a is not None and group_a == exact_group_of.get(key[1]):
                continue  # already proven byte-identical
            key_a = self._key_by_id.get(key[0], "")
            key_b = self._key_by_id.get(key[1], "")
            if key_a and key_b and tuple(sorted((key_a, key_b))) in self._dismissed:
                continue  # reviewer already called this pair a false positive
            verdict = verdicts.get(key)
            if verdict is not None:
                stored = self._stored_offset_verdict(verdict, file_a, file_b)
                if stored is not None:
                    if stored.still_a_miss:
                        cached += 1
                    elif stored.hit is not None:
                        # The same bytes, re-judged under a looser bar: the
                        # measurements already clear it, so this is a hit
                        # without decoding either file again
                        file_short, file_long = (
                            (file_a, file_b)
                            if file_a["_duration"] < file_b["_duration"]
                            else (file_b, file_a)
                        )
                        self._offset_hits.append((file_short, file_long, stored.hit))
                        result.offset_probe_hits += 1
                        cached += 1
                    continue
            shared = bag_hits.get(key, 0)
            # Negative count: the pair sharing more of its opening sorts
            # first, with the length gap breaking ties
            head_candidates.append((-shared, gap, key[0], key[1]))
        result.offset_probes_reconfirmed = len(recheck)
        result.offset_probes_candidates = len(recheck) + len(head_candidates) + cached
        result.offset_probes_cached = cached
        if not recheck and not head_candidates:
            return
        head_candidates.sort()
        # Known shifts go first: they are the pairs a reviewer is already
        # looking at, and re-testing one costs what testing a new pair costs
        probe_pairs: list[tuple[int, int]] = list(recheck)
        probe_pairs += [(id_a, id_b) for _strength, _gap, id_a, id_b in head_candidates]
        for index, (id_a, id_b) in enumerate(probe_pairs):
            if cancellation_token and cancellation_token.is_cancelled():
                return
            file_a, file_b = by_id[id_a], by_id[id_b]
            if file_a["_duration"] >= file_b["_duration"]:
                file_long, file_short = file_a, file_b
            else:
                file_long, file_short = file_b, file_a
            # Both halves of the pair belong in the log: "checking 37/400"
            # says how far along the phase is, never what it is looking at
            # One-based: the event names the pair being probed, so the first
            # of eleven reads "1/11" while it runs, not "0/11" — a zero on a
            # phase whose units take minutes reads as a scan that has not
            # started. It also makes the phase's result row count every pair
            # it probed instead of stopping one short.
            self._emit_progress(
                ProgressEvent(
                    phase="offset",
                    completed_count=index + 1,
                    total_count=len(probe_pairs),
                    current_path=file_long["original_path"],
                    peer_path=file_short["original_path"],
                    message=f"Offset check {index + 1:,}/{len(probe_pairs):,}",
                )
            )
            result.offset_probes += 1
            probe, decided, evidence = self._probe_timeline_offset_with_verdict(
                file_long, file_short
            )
            if probe is not None:
                result.offset_probe_hits += 1
                self._offset_hits.append((file_short, file_long, probe))
            elif decided:
                self.repository.save_offset_verdict(
                    id_a,
                    id_b,
                    self._offset_verdict_version,
                    file_a["size"],
                    file_a["mtime_ns"],
                    file_b["size"],
                    file_b["mtime_ns"],
                    json.dumps(evidence) if evidence is not None else None,
                )

    @staticmethod
    def _verdict_is_current(
        verdict: dict[str, Any], file_a: dict[str, Any], file_b: dict[str, Any]
    ) -> bool:
        """Whether a stored verdict still describes the bytes on disk.

        A verdict answers for the files it was probed against, not for the
        paths they had then: a replaced file is probed again, exactly as a
        replaced file invalidates its cached fingerprint.
        """
        return bool(
            verdict["size_a"] == file_a.get("size")
            and verdict["mtime_a"] == file_a.get("mtime_ns")
            and verdict["size_b"] == file_b.get("size")
            and verdict["mtime_b"] == file_b.get("mtime_ns")
        )

    def _stored_offset_verdict(
        self,
        verdict: dict[str, Any],
        file_a: dict[str, Any],
        file_b: dict[str, Any],
    ) -> _StoredOffsetVerdict | None:
        """Re-judge a stored verdict under this scan's bars.

        Returns ``None`` when the row cannot answer for these bytes — a
        replaced file, a retired algorithm, or a row from before the
        measurements were stored. Otherwise the row is either still a miss or
        its own measurements now clear the bars, which is a hit that costs no
        decode.
        """
        if not self._verdict_is_current(verdict, file_a, file_b):
            return None
        version = str(verdict.get("algorithm_version") or "")
        if version != self._offset_verdict_version:
            if self._legacy_verdict_is_still_strict_enough(version):
                return _StoredOffsetVerdict(True)
            return None
        raw = verdict.get("evidence")
        if not raw:
            return None
        try:
            evidence = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(evidence, dict):
            return None
        kind = evidence.get("kind")
        if kind == "lead":
            # The head windows did not line up at any shift. That decision
            # reads none of the strictness bars, so it survives all of them.
            return _StoredOffsetVerdict(True)
        if kind != "checkpoints":
            return None
        try:
            correlations = tuple(float(value) for value in evidence["correlations"])
            distances = tuple(float(value) for value in evidence["distances"])
            divergences = tuple(float(value) for value in evidence["divergences"])
            offset_seconds = float(evidence["offset_seconds"])
            lead_matches = int(evidence["lead_matches"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (
            len(correlations) == len(distances) == len(divergences)
            and len(correlations) >= _OFFSET_VERIFY_MIN_MATCHES
        ):
            return None
        verified = sum(
            1
            for correlation, distance, divergence in zip(
                correlations, distances, divergences, strict=True
            )
            if _checkpoint_matches(correlation, distance, divergence, self.config)
        )
        if verified < _OFFSET_VERIFY_MIN_MATCHES:
            return _StoredOffsetVerdict(True)
        return _StoredOffsetVerdict(
            False,
            OffsetProbeResult(
                offset_seconds=offset_seconds,
                lead_matches=lead_matches,
                verified=verified,
                checked=len(correlations),
                correlations=correlations,
                distances=distances,
                divergences=divergences,
            ),
        )

    def _legacy_verdict_is_still_strict_enough(self, version: str) -> bool:
        """Whether a verdict from before the evidence payload still holds.

        Those rows carry the distance, divergence and per-block bars they were
        judged with in the version string. A miss decided at bars at least as
        strict as this scan's is still a miss; anything looser has to be
        measured again, because the pair may now clear the bar.
        """
        match = _LEGACY_OFFSET_VERDICT_VERSION.fullmatch(version)
        if match is None:
            return False
        old_distance, old_divergence, old_block = (float(part) for part in match.groups())
        return (
            old_block == self.config.vl_local_divergence_distance
            and self.config.frame_match_max_distance <= old_distance
            and self.config.vl_local_divergence_max <= old_divergence
        )

    def _probe_timeline_offset(
        self, file_long: dict[str, Any], file_short: dict[str, Any]
    ) -> OffsetProbeResult | None:
        """Confirm a constant timeline shift between two near-equal files."""
        probe, _decided, _evidence = self._probe_timeline_offset_with_verdict(
            file_long, file_short
        )
        return probe

    def _probe_timeline_offset_with_verdict(
        self, file_long: dict[str, Any], file_short: dict[str, Any]
    ) -> tuple[OffsetProbeResult | None, bool, dict[str, Any] | None]:
        """Confirm a shift, and say whether "no shift" is a fact or a failure.

        The head measurement only *proposes* a shift; the verdict comes from
        re-extracting frames at that shift across the whole timeline. One or
        two identical frames can be a reused shot; six positions spread over
        hundreds of seconds of video cannot. Correlation says the layout
        agrees; only the pixel distance says the picture does — two different
        dark scenes with a person in the middle correlate 0.7, a genuine
        re-encode correlates 0.99 at distance ~0.03.

        ``decided`` is False when the answer cannot be trusted — a head
        window that failed to decode, or too few readable checkpoints to
        confirm anything. Such a pair stays in the queue: remembering a
        decode failure as "no shift" would hide a real copy for as long as
        its bytes stayed unchanged.

        The third value is the evidence behind a decided "no shift": the
        per-checkpoint measurements, or a marker that the head windows never
        lined up. Storing them lets a later strictness change re-judge the
        pair without decoding either file again.
        """
        lead_long = self._lead_frames(file_long)
        lead_short = self._lead_frames(file_short)
        if len(lead_long) < _OFFSET_MIN_LEAD_MATCHES or len(lead_short) < _OFFSET_MIN_LEAD_MATCHES:
            return None, False, None
        measured = self._measure_lead_offset(lead_long, lead_short)
        if measured is None:
            return None, True, {"kind": "lead"}
        offset, lead_matches = measured
        dur_long, dur_short = file_long["_duration"], file_short["_duration"]
        short_times: list[float] = []
        long_times: list[float] = []
        for fraction in _OFFSET_VERIFY_FRACTIONS:
            t_short = dur_short * fraction
            t_long = t_short + offset
            if t_short < 2.0 or t_long < 2.0 or t_long > dur_long - 2.0:
                continue
            short_times.append(round(t_short, 3))
            long_times.append(round(t_long, 3))
        if len(short_times) < _OFFSET_VERIFY_MIN_MATCHES:
            return None, False, None
        size = _OFFSET_PROBE_SIZE * _OFFSET_PROBE_SIZE
        frames_short = extract_gray_frames(
            Path(file_short["original_path"]),
            short_times,
            ffmpeg_path=self.config.ffmpeg_path,
            width=_OFFSET_PROBE_SIZE,
            height=_OFFSET_PROBE_SIZE,
        )
        frames_long = extract_gray_frames(
            Path(file_long["original_path"]),
            long_times,
            ffmpeg_path=self.config.ffmpeg_path,
            width=_OFFSET_PROBE_SIZE,
            height=_OFFSET_PROBE_SIZE,
        )
        correlations: list[float] = []
        distances: list[float] = []
        divergences: list[float] = []
        for frame_short, frame_long in zip(frames_short, frames_long, strict=True):
            if frame_short is None or frame_long is None:
                continue
            small_short = _downscale_gray(frame_short, _OFFSET_PROBE_SIZE)
            small_long = _downscale_gray(frame_long, _OFFSET_PROBE_SIZE)
            if not small_short or not small_long:
                continue
            correlations.append(
                float(
                    _correlation_matrix(
                        _frame_matrix([frame_long], size),
                        _frame_matrix([frame_short], size),
                    )[0, 0]
                )
            )
            distances.append(frame_distance(small_short, small_long))
            divergences.append(
                _divergent_block_fraction(
                    np.frombuffer(small_short, dtype=np.uint8).reshape(1, -1),
                    np.frombuffer(small_long, dtype=np.uint8).reshape(1, -1),
                    _OFFSET_PROBE_SIZE // 2,
                    _OFFSET_PROBE_SIZE // 2,
                    self.config.vl_local_divergence_distance,
                )
            )
        if len(correlations) < _OFFSET_VERIFY_MIN_MATCHES:
            return None, False, None
        verified = sum(
            1
            for correlation, distance, divergence in zip(
                correlations, distances, divergences, strict=True
            )
            if _checkpoint_matches(correlation, distance, divergence, self.config)
        )
        if verified < _OFFSET_VERIFY_MIN_MATCHES:
            return (
                None,
                True,
                {
                    "kind": "checkpoints",
                    "offset_seconds": offset,
                    "lead_matches": lead_matches,
                    "correlations": correlations,
                    "distances": distances,
                    "divergences": divergences,
                },
            )
        return (
            OffsetProbeResult(
                offset_seconds=offset,
                lead_matches=lead_matches,
                verified=verified,
                checked=len(correlations),
                correlations=tuple(correlations),
                distances=tuple(distances),
                divergences=tuple(divergences),
            ),
            True,
            None,
        )

    def _lead_frames(self, file_row: dict[str, Any]) -> list[bytes]:
        """The file's head window at 1fps, decoded once per scan.

        Cached by file id: one file takes part in many candidate pairs, and
        the shift it carries belongs to the file, not to the pair.
        """
        file_id = file_row["id"]
        cached = self._lead_cache.get(file_id)
        if cached is None:
            cached = extract_gray_window(
                Path(file_row["original_path"]),
                start_seconds=0.0,
                window_seconds=min(_OFFSET_LEADIN_SECONDS, file_row["_duration"]),
                fps=_OFFSET_LEADIN_FPS,
                width=_OFFSET_PROBE_SIZE,
                height=_OFFSET_PROBE_SIZE,
                ffmpeg_path=self.config.ffmpeg_path,
                timeout_seconds=max(self.config.frame_timeout_seconds, 120),
            )
            self._lead_cache[file_id] = cached
        return cached

    def _measure_lead_offset(
        self, lead_long: list[bytes], lead_short: list[bytes]
    ) -> tuple[float, int] | None:
        """Shift whose alignment best explains two head windows.

        Returns ``(offset, matched_seconds)`` where offset is
        ``long_time - short_time`` for corresponding content: an ad of 20s
        at the head of the long copy gives +20. Frames are 1fps, so an index
        difference is a second.
        """
        n_long, n_short = len(lead_long), len(lead_short)
        if n_long < _OFFSET_MIN_LEAD_MATCHES or n_short < _OFFSET_MIN_LEAD_MATCHES:
            return None
        size = _OFFSET_PROBE_SIZE * _OFFSET_PROBE_SIZE
        corr = _correlation_matrix(_frame_matrix(lead_long, size), _frame_matrix(lead_short, size))
        max_shift = min(_OFFSET_MAX_SHIFT_SECONDS, n_long - 1)
        best: tuple[int, float, int] | None = None
        for offset in range(-max_shift, max_shift + 1):
            start = max(0, offset)
            stop = min(n_long, n_short + offset)
            if stop - start < _OFFSET_MIN_LEAD_MATCHES:
                continue
            idx = np.arange(start, stop)
            values = corr[idx, idx - offset]
            matches = int((values >= _OFFSET_MIN_CORRELATION).sum())
            if matches < _OFFSET_MIN_LEAD_MATCHES:
                continue
            mean = float(values.mean())
            if best is None or (matches, mean) > (best[0], best[1]):
                best = (matches, mean, offset)
        if best is None:
            return None
        return float(best[2]), best[0]

    def _ensure_fingerprints(
        self,
        files: list[dict[str, Any]],
        pairs: list[tuple[dict[str, Any], dict[str, Any], bool]],
        result: PerceptualMatchResult,
        cancellation_token: CancellationToken | None,
    ) -> dict[int, FingerprintData]:
        """Compute or reuse fingerprints for all candidate files."""
        candidate_ids = {f["id"] for a, b, _ in pairs for f in (a, b)}
        by_id = {f["id"]: f for f in files}
        fingerprints: dict[int, FingerprintData] = {}
        pending: list[dict[str, Any]] = []

        for file_id in sorted(candidate_ids):
            file_row = by_id[file_id]
            cached = self.repository.get_fingerprint(file_id)
            if cached and self._cached_fingerprint_usable(cached, file_row):
                result.fingerprint_cache_hits += 1
                fingerprints[file_id] = FingerprintData(
                    frame_count=cached["frame_count"],
                    timestamps=tuple(json.loads(cached["timestamps"])),
                    frames=cached["frames"],
                    frame_size=self.frame_size,
                )
            else:
                pending.append(file_row)

        total = len(candidate_ids)
        done = len(fingerprints)
        workers = max(1, self.config.signature_workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._extract_fingerprint, f): f for f in pending}
            for future in as_completed(futures):
                if cancellation_token and cancellation_token.is_cancelled():
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                file_row = futures[future]
                done += 1
                self._emit_progress(
                    ProgressEvent(
                        phase="fingerprinting",
                        completed_count=done,
                        total_count=total,
                        current_path=file_row["original_path"],
                        message=f"Fingerprinting {done}/{total}",
                    )
                )
                fp, error_msg = future.result()
                if error_msg:
                    logger.error(error_msg)
                    result.errors.append(error_msg)
                    result.fingerprint_errors += 1
                if fp is None:
                    continue
                self.repository.upsert_fingerprint(
                    file_row["id"],
                    self._fp_version,
                    json.dumps(list(fp.timestamps)),
                    fp.frames,
                    file_row["size"],
                    file_row["mtime_ns"],
                )
                # Commit per file: fingerprinting is slow, so each result is
                # durable progress a crash or interruption must not lose
                self.repository.commit()
                fingerprints[file_row["id"]] = fp
                result.files_fingerprinted += 1

        return fingerprints

    def _ensure_audio(
        self,
        pairs: list[tuple[dict[str, Any], dict[str, Any], bool]],
        result: PerceptualMatchResult,
        cancellation_token: CancellationToken | None,
    ) -> dict[int, bytes]:
        """Compute or reuse audio signatures for all candidate files."""
        candidate_ids = {f["id"] for a, b, _ in pairs for f in (a, b)}
        by_id = {f["id"]: f for a, b, _ in pairs for f in (a, b)}
        sigs: dict[int, bytes] = {}
        pending: list[dict[str, Any]] = []

        for file_id in sorted(candidate_ids):
            file_row = by_id[file_id]
            # ffprobe already settled whether an audio track exists, so a
            # file without one costs nothing here: no ffmpeg spawn and no
            # empty signature. A failed decode used to be cached as exactly
            # that empty signature, which made the divergence gate silently
            # skip the file forever (783 such rows in the reference DB had
            # a stream ffprobe could see).
            if not file_row.get("_audio_stream_count", 1):
                sigs[file_id] = b""
                continue
            cached = self.repository.get_audio_fingerprint(file_id)
            if (
                cached
                and cached["window_count"] > 0
                and cached["file_size"] == file_row["size"]
                and cached["mtime_ns"] == file_row["mtime_ns"]
                and cached["algorithm_version"] == self._audio_version
            ):
                sigs[file_id] = cached["signature"]
            else:
                pending.append(file_row)

        total = len(candidate_ids)
        done = len(sigs)
        workers = max(1, self.config.signature_workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._extract_audio, f): f for f in pending}
            for future in as_completed(futures):
                if cancellation_token and cancellation_token.is_cancelled():
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                file_row = futures[future]
                done += 1
                self._emit_progress(
                    ProgressEvent(
                        phase="audio",
                        completed_count=done,
                        total_count=total,
                        current_path=file_row["original_path"],
                        message=f"Audio fingerprint {done}/{total}",
                    )
                )
                sig, n_windows, error_msg = future.result()
                if error_msg:
                    logger.error(error_msg)
                    result.errors.append(error_msg)
                    result.fingerprint_errors += 1
                if sig is None:
                    continue
                self.repository.upsert_audio_fingerprint(
                    file_row["id"],
                    self._audio_version,
                    n_windows,
                    self.config.audio_bands,
                    sig,
                    file_row["size"],
                    file_row["mtime_ns"],
                )
                self.repository.commit()
                sigs[file_row["id"]] = sig

        return sigs

    def _extract_audio(self, file_row: dict[str, Any]) -> tuple[bytes | None, int, str | None]:
        """Extract the audio signature for one file (worker thread)."""
        return extract_audio_signature(
            Path(file_row["original_path"]),
            file_row["_duration"],
            ffmpeg_path=self.config.ffmpeg_path,
            # A 2s PCM window decode is cheap — don't pay the full frame
            # timeout per window on a dead file (8 x 30s stalls seen)
            timeout_seconds=min(self.config.frame_timeout_seconds, 12),
            windows=self.config.audio_windows,
            window_seconds=self.config.audio_window_seconds,
            sample_rate=self.config.audio_sample_rate,
            bands=self.config.audio_bands,
        )

    def _cached_fingerprint_usable(self, cached: dict[str, Any], file_row: dict[str, Any]) -> bool:
        """Whether a cached fingerprint can still stand for the whole file.

        File size, mtime and version prove the *file* has not changed. They
        say nothing about how much of it the stored samples cover, and a row
        written by a run whose decode died halfway is a fingerprint of half a
        video forever after (measured on the reference library: two rows at
        52% and 68% of the span, one of them a pair that is byte-identical
        wherever both sides have a sample, scored 15%). A blank tail is fine
        and answers for itself; a tail far from the end is re-extracted.
        """
        if (
            cached["file_size"] != file_row["size"]
            or cached["mtime_ns"] != file_row["mtime_ns"]
            or cached["algorithm_version"] != self._fp_version
        ):
            return False
        timestamps = json.loads(cached["timestamps"])
        if not timestamps:
            return False
        grid = self._sample_grid(file_row["_duration"])
        if not grid:
            return True
        return float(max(timestamps)) >= _CACHED_SPAN_SHARE * grid[-1]

    def _sample_grid(self, duration: float) -> list[float]:
        """The absolute timestamps a fingerprint of this duration samples."""
        return sample_timestamps(
            duration,
            self.config.min_frames,
            self.config.max_frames,
            self.config.target_frame_interval_seconds,
        )

    def _grid_step(self, duration: float) -> float:
        """One sample-grid step, as a share of the duration."""
        grid = self._sample_grid(duration)
        if duration <= 0 or len(grid) < 2:
            return 1.0
        return (grid[-1] - grid[0]) / duration / (len(grid) - 1)

    def _fingerprint_coverage(self, fp: FingerprintData, duration: float) -> float:
        """Share of the sampled window a fingerprint actually covers.

        Stored with the pair evidence: a comparison over half a fingerprint
        is a real verdict about half a video, and the number that says so
        otherwise only exists inside the fingerprint row.
        """
        if not fp.frame_count:
            return 0.0
        grid = self._sample_grid(duration)
        if len(grid) < 2:
            return 1.0
        expected = grid[-1] - grid[0]
        if expected <= 0:
            return 1.0
        return min(1.0, (fp.timestamps[-1] - fp.timestamps[0]) / expected)

    def _extract_fingerprint(
        self, file_row: dict[str, Any]
    ) -> tuple[FingerprintData | None, str | None]:
        """Extract dense frames for one file.

        Runs on a worker thread; performs no repository writes. Returns the
        fingerprint plus an error message on failure.
        """
        duration = file_row["_duration"]
        timestamps = sample_timestamps(
            duration,
            self.config.min_frames,
            self.config.max_frames,
            self.config.target_frame_interval_seconds,
        )
        crop = self._detect_crop(file_row, duration)

        frames_out = extract_gray_frames(
            Path(file_row["original_path"]),
            timestamps,
            ffmpeg_path=self.config.ffmpeg_path,
            width=self.config.frame_width,
            height=self.config.frame_height,
            crop=crop,
        )
        kept_frames, kept_timestamps = _usable_frames(timestamps, frames_out)

        # A tail that failed to decode is retried once: everything before it
        # decoded, so the failure was the machine (a drive waking up, a child
        # killed by the memory guard), not the file.
        if not _fingerprint_is_complete(timestamps, frames_out, kept_timestamps):
            self._retry_failed_tail(file_row, timestamps, frames_out, kept_timestamps, crop)
            kept_frames, kept_timestamps = _usable_frames(timestamps, frames_out)

        # Require at least half of the intended samples to succeed
        if len(kept_frames) < max(2, len(timestamps) // 2):
            error_msg = (
                f"Fingerprint failed for {file_row['original_path']}: "
                f"only {len(kept_frames)}/{len(timestamps)} usable frames"
            )
            return None, error_msg

        # A fingerprint that stops early because decodes failed describes
        # part of the video while claiming all of it, and every later
        # comparison trusts that claim. No fingerprint this scan (the error
        # is logged and shown in the scan summary) beats a wrong one cached
        # until the file itself changes.
        if not _fingerprint_is_complete(timestamps, frames_out, kept_timestamps):
            error_msg = (
                f"Fingerprint incomplete for {file_row['original_path']}: "
                f"last usable frame at {kept_timestamps[-1]:.0f}s of "
                f"{timestamps[-1]:.0f}s, the tail would not decode"
            )
            return None, error_msg

        return (
            FingerprintData(
                frame_count=len(kept_frames),
                timestamps=tuple(kept_timestamps),
                frames=b"".join(kept_frames),
                frame_size=self.frame_size,
            ),
            None,
        )

    def _retry_failed_tail(
        self,
        file_row: dict[str, Any],
        timestamps: list[float],
        frames_out: list[bytes | None],
        kept_timestamps: list[float],
        crop: str | None,
    ) -> None:
        """Re-decode the samples after the last kept frame, once.

        The first pass can lose a whole tail to one bad moment; a second pass
        recovers it without paying for the file's fingerprint again. A tail
        that yields nothing at all is the file's answer rather than the
        machine's — stop there instead of spending a timeout per sample.
        """
        last_kept = kept_timestamps[-1] if kept_timestamps else -1.0
        missing = [
            index
            for index, timestamp in enumerate(timestamps)
            if timestamp > last_kept and frames_out[index] is None
        ]
        for start in range(0, len(missing), _TAIL_RETRY_SAMPLES):
            batch = missing[start : start + _TAIL_RETRY_SAMPLES]
            retried = extract_gray_frames(
                Path(file_row["original_path"]),
                [timestamps[index] for index in batch],
                ffmpeg_path=self.config.ffmpeg_path,
                width=self.config.frame_width,
                height=self.config.frame_height,
                crop=crop,
            )
            if all(frame is None for frame in retried):
                return
            for index, frame in zip(batch, retried, strict=True):
                frames_out[index] = frame

    def _record_offset_hit(
        self,
        scan_id: int,
        file_short: dict[str, Any],
        file_long: dict[str, Any],
        probe: OffsetProbeResult,
        result: PerceptualMatchResult,
    ) -> None:
        """Persist the verdict of a confirmed timeline shift.

        VERY_LIKELY needs every checkpoint confirming, a high mean correlation
        and a low mean pixel distance: 32x32 frames sampled a fraction of a
        second off the true shift still land near 0.8 on genuine copies, so
        "all six passed" alone would overstate a heavily re-encoded pair.
        Anything less certain goes to review as POSSIBLE — this path exists to
        make the pair visible, not to act on it.
        """
        match_fraction = probe.verified / probe.checked if probe.checked else 0.0
        distance = probe.mean_distance
        duration_difference = abs(file_long["_duration"] - file_short["_duration"])
        if (
            probe.checked
            and probe.verified == probe.checked
            and probe.mean_correlation >= _OFFSET_VERY_LIKELY_CORRELATION
            and probe.mean_distance <= self.config.very_likely_mean_distance
        ):
            classification = CLASS_VERY_LIKELY
            result.very_likely += 1
        else:
            classification = CLASS_POSSIBLE
            result.possible += 1

        # Audio divergence gate, aligned by the measured shift. A shifted
        # copy carries the same soundtrack at the offset, so the windows are
        # compared after applying it.
        audio_dist: float | None = None
        if classification == CLASS_VERY_LIKELY:
            sig_s = self._audio.get(file_short["id"])
            sig_l = self._audio.get(file_long["id"])
            if sig_s and sig_l:
                audio_dist = audio_distance_shifted(
                    sig_s,
                    sig_l,
                    self.config.audio_bands,
                    file_short["_duration"],
                    file_long["_duration"],
                    probe.offset_seconds,
                    self.config.audio_window_seconds,
                    self.config.audio_windows,
                )
                if audio_dist > self.config.audio_divergence_threshold:
                    classification = CLASS_POSSIBLE
                    result.very_likely -= 1
                    result.possible += 1

        result.comparisons += 1
        evidence = json.dumps(
            {
                "mode": "offset-probe",
                "offset_seconds": round(probe.offset_seconds, 2),
                "match_fraction": match_fraction,
                "matching_frames": probe.verified,
                "frames_compared": probe.checked,
                "mean_frame_distance": round(distance, 4),
                "lead_matches": probe.lead_matches,
                "correlations": [round(value, 4) for value in probe.correlations],
                "distances": [round(value, 4) for value in probe.distances],
                "divergences": [round(value, 4) for value in probe.divergences],
                "mean_divergence": round(probe.mean_divergence, 4),
                "duration_difference": duration_difference,
                # The timelines do not start together, which is exactly why
                # this path exists: normalized-position frames would catch
                # different moments
                "aligned": False,
                "local_divergent_blocks": 0.0,
                "audio_distance": audio_dist,
            }
        )
        self.repository.create_pair_comparison(
            scan_id=scan_id,
            file_a_id=file_short["id"],
            file_b_id=file_long["id"],
            algorithm_version=MATCH_ALGORITHM_VERSION,
            classification=classification,
            match_fraction=match_fraction,
            mean_frame_distance=distance,
            frames_compared=probe.checked,
            duration_difference=duration_difference,
            evidence=evidence,
        )

    def _compare_pair(
        self,
        scan_id: int,
        file_a: dict[str, Any],
        file_b: dict[str, Any],
        is_trim: bool,
        fingerprints: dict[int, FingerprintData],
        result: PerceptualMatchResult,
    ) -> None:
        """Compare two files and record evidence."""
        fp_a = fingerprints.get(file_a["id"])
        fp_b = fingerprints.get(file_b["id"])
        duration_diff = abs(file_a["_duration"] - file_b["_duration"])

        if fp_a is None or fp_b is None:
            result.errors.append(
                "Missing fingerprint for pair: "
                f"{file_a['original_path']} <-> {file_b['original_path']}"
            )
            return

        if is_trim:
            self._compare_trimmed(scan_id, file_a, file_b, fp_a, fp_b, duration_diff, result)
            return

        # Corresponding-timeline comparison: pair each frame with the frame
        # at the same *relative position* in the other file. Index scaling
        # (`rint(arange(n) * (n_l - 1) / (n_s - 1))`) stood here and reads
        # the same only while both fingerprints hold every grid sample; one
        # dropped blank frame shifts the rest, and a half-extracted one
        # compares the middle of A with the end of B — 15% on a pair that is
        # byte-identical at every shared timestamp.
        idx_a, idx_b = _corresponding_frames(
            fp_a,
            file_a["_duration"],
            fp_b,
            file_b["_duration"],
            self._grid_step(file_a["_duration"]),
            self._grid_step(file_b["_duration"]),
        )
        compared = int(idx_a.size)
        mat_a = np.frombuffer(fp_a.frames, dtype=np.uint8).reshape(
            fp_a.frame_count, self.frame_size
        )
        mat_b = np.frombuffer(fp_b.frames, dtype=np.uint8).reshape(
            fp_b.frame_count, self.frame_size
        )
        # A frame with no counterpart is missing data, not a mismatch: it is
        # absent from `compared` instead of being counted as a failure
        dists = (
            np.abs(mat_a[idx_a].astype(np.int16) - mat_b[idx_b].astype(np.int16)).mean(axis=1)
            / 255.0
        )
        matched_mask = dists <= self.config.frame_match_max_distance
        matching = int(matched_mask.sum())
        total_distance = float(dists.sum())
        matched_distance = float(dists[matched_mask].sum())

        match_fraction = matching / compared if compared else 0.0
        mean_distance = total_distance / compared if compared else 1.0
        matched_mean = matched_distance / matching if matching else 1.0

        # VERY_LIKELY requires the average distance gate in both branches:
        # nearly all aligned frames match, or most match with a low mean.
        # The high-fraction branch used to bypass the mean and admit pairs
        # with a handful of very different frames.
        strong_evidence = (
            match_fraction >= self.config.possible_threshold
            and mean_distance <= self.config.very_likely_mean_distance
        )
        partial_overlap = (
            matching >= self.config.possible_min_matched_frames
            and matched_mean <= self.config.very_likely_mean_distance
        )
        if (
            match_fraction >= self.config.very_likely_threshold or strong_evidence
        ) and mean_distance <= self.config.very_likely_mean_distance:
            classification = CLASS_VERY_LIKELY
            result.very_likely += 1
        elif match_fraction >= self.config.possible_threshold or partial_overlap:
            classification = CLASS_POSSIBLE
            result.possible += 1
        else:
            classification = CLASS_REJECTED
            result.rejected += 1

        # Local-structure check: same-scene-different-content (same-
        # parameter generations, same-studio episodes) matches on layout
        # but diverges in detail. Matched frames whose blocks differ
        # sharply downgrade VERY_LIKELY to POSSIBLE for human review.
        divergent = 0.0
        if classification == CLASS_VERY_LIKELY:
            divergent = _divergent_block_fraction(
                mat_a[idx_a[matched_mask]],
                mat_b[idx_b[matched_mask]],
                self.config.frame_width,
                self.config.frame_height,
                self.config.vl_local_divergence_distance,
            )
            if divergent > self.config.vl_local_divergence_max:
                classification = CLASS_POSSIBLE
                result.very_likely -= 1
                result.possible += 1

        # Audio divergence gate: re-encodes/upscales share the audio
        # track; same-scene-different-content does not. Silent/no-audio
        # files keep the visual verdict — no signal, no penalty
        audio_dist: float | None = None
        if classification == CLASS_VERY_LIKELY:
            sig_a = self._audio.get(file_a["id"])
            sig_b = self._audio.get(file_b["id"])
            if sig_a and sig_b:
                audio_dist = audio_distance(sig_a, sig_b, self.config.audio_bands)
                if audio_dist > self.config.audio_divergence_threshold:
                    classification = CLASS_POSSIBLE
                    result.very_likely -= 1
                    result.possible += 1

        evidence = json.dumps(
            {
                "match_fraction": match_fraction,
                "matching_frames": matching,
                "frames_compared": compared,
                "mean_frame_distance": mean_distance,
                "duration_difference": duration_diff,
                # How much of each file's sampled window the fingerprint
                # covers. 1.0 on any complete fingerprint; an older row that
                # was stored half-extracted says so here instead of reading
                # as a confident verdict about the whole video.
                "coverage_a": round(self._fingerprint_coverage(fp_a, file_a["_duration"]), 3),
                "coverage_b": round(self._fingerprint_coverage(fp_b, file_b["_duration"]), 3),
                "local_divergent_blocks": divergent,
                "audio_distance": audio_dist,
            }
        )

        self.repository.create_pair_comparison(
            scan_id=scan_id,
            file_a_id=file_a["id"],
            file_b_id=file_b["id"],
            algorithm_version=MATCH_ALGORITHM_VERSION,
            classification=classification,
            match_fraction=match_fraction,
            mean_frame_distance=mean_distance,
            frames_compared=compared,
            duration_difference=duration_diff,
            evidence=evidence,
        )

    def _compare_trimmed(
        self,
        scan_id: int,
        file_a: dict[str, Any],
        file_b: dict[str, Any],
        fp_a: FingerprintData,
        fp_b: FingerprintData,
        duration_diff: float,
        result: PerceptualMatchResult,
    ) -> None:
        """Align a duration-mismatched pair via dominant-offset voting.

        Every frame pair whose normalized distance is within the match
        threshold votes for a timeline offset; the densest offset window
        is the alignment. This is robust to self-similar content where a
        greedy monotone-subsequence search chains look-alike frames at
        arbitrary offsets, and to distributed cuts — those simply vote
        for several offsets and the dominant one is measured.

        Coverage rules per PRODUCT_SPEC section 6: VERY_LIKELY needs
        >= trim_short_coverage_min of the shorter file's *frames* matched and
        matched timestamps spanning >= trim_long_coverage_min of the longer
        video's duration. A clip whose own frames all match but which covers
        little of the longer file is a partial overlap (POSSIBLE), not a
        duplicate; a pair whose frames match only here and there is not a
        partial overlap at all — it is noise, however wide a span those
        coincidences happen to cover.
        """
        if file_a["_duration"] <= file_b["_duration"]:
            s_fp, l_fp = fp_a, fp_b
        else:
            s_fp, l_fp = fp_b, fp_a
        dur_l = max(file_a["_duration"], file_b["_duration"])
        dur_s = min(file_a["_duration"], file_b["_duration"])
        n_s, n_l = s_fp.frame_count, l_fp.frame_count
        threshold = self.config.frame_match_max_distance

        mat_s = np.frombuffer(s_fp.frames, dtype=np.uint8).reshape(n_s, self.frame_size)
        mat_l = np.frombuffer(l_fp.frames, dtype=np.uint8).reshape(n_l, self.frame_size)
        dist = (
            np.abs(mat_s[:, None, :].astype(np.int16) - mat_l[None, :, :].astype(np.int16)).mean(
                axis=2
            )
            / 255.0
        )

        # Dominant-offset alignment: every frame pair within the match
        # threshold votes for a timeline offset. A spec-compliant trim is
        # one continuous portion, so genuine correspondences cluster at a
        # constant offset; a re-timed copy clusters after scaling by the
        # duration ratio. Self-similar content matches everywhere and
        # spreads votes over all offsets, so only the consistent segment
        # counts. Per matched short frame keep the closest in-window
        # long frame.
        l_step = (l_fp.timestamps[-1] - l_fp.timestamps[0]) / max(1, n_l - 1)
        s_step = (s_fp.timestamps[-1] - s_fp.timestamps[0]) / max(1, n_s - 1)

        cand = np.argwhere(dist <= threshold)
        pairs: list[tuple[int, int]] = []
        offset_mode = "trim"
        if len(cand):
            cand_i = cand[:, 0]
            cand_j = cand[:, 1]
            l_ts_arr = np.asarray(l_fp.timestamps)
            s_ts_arr = np.asarray(s_fp.timestamps)

            # Two alignment hypotheses: a trim keeps content at a constant
            # offset (l_ts ~ c + s_ts); a re-timed copy scales the timeline
            # (l_ts ~ c + s_ts * dur_l/dur_s). Vote under both and keep the
            # alignment covering more of the shorter file.
            ratio = dur_l / dur_s if dur_s else 1.0
            hypotheses = {
                "trim": (l_ts_arr[cand_j] - s_ts_arr[cand_i], l_step + s_step),
                "speed": (
                    l_ts_arr[cand_j] - s_ts_arr[cand_i] * ratio,
                    l_step + s_step * ratio,
                ),
            }
            best: tuple[int, float, float, np.ndarray, str] | None = None
            for mode, (cand_off, win) in hypotheses.items():
                order = np.argsort(cand_off)
                offs, c_i = cand_off[order], cand_i[order]

                # Two-pointer window maximizing distinct short frames
                in_window: dict[int, int] = {}
                best_count, lo_off = 0, float(offs[0])
                hi = 0
                for lo in range(len(offs)):
                    while hi < len(offs) and offs[hi] <= offs[lo] + win:
                        in_window[c_i[hi]] = in_window.get(c_i[hi], 0) + 1
                        hi += 1
                    if len(in_window) > best_count:
                        best_count, lo_off = len(in_window), float(offs[lo])
                    key = c_i[lo]
                    in_window[key] -= 1
                    if not in_window[key]:
                        del in_window[key]
                if best is None or best_count > best[0]:
                    best = (best_count, lo_off, lo_off + win, cand_off, mode)

            if best is not None:
                _, lo_off, hi_off, cand_off, offset_mode = best
                best_per_i: dict[int, tuple[int, float]] = {}
                for i, j, off in zip(cand_i, cand_j, cand_off, strict=True):
                    if lo_off <= off <= hi_off:
                        d = float(dist[i, j])
                        if i not in best_per_i or d < best_per_i[i][1]:
                            best_per_i[i] = (int(j), d)
                pairs = sorted((int(i), j) for i, (j, _) in best_per_i.items())

        matched = len(pairs)
        if pairs:
            s_ts = [s_fp.timestamps[i] for i, _ in pairs]
            l_ts = [l_fp.timestamps[j] for _, j in pairs]
            s_first_ts, s_last_ts = min(s_ts), max(s_ts)
            l_first_ts, l_last_ts = min(l_ts), max(l_ts)
            mean_distance = sum(dist[i][j] for i, j in pairs) / matched
            offset_seconds = l_first_ts - s_first_ts
        else:
            s_first_ts = s_last_ts = l_first_ts = l_last_ts = 0.0
            mean_distance = 1.0
            offset_seconds = 0.0

        # Spec section 6 asks for a share of the shorter video's *frames*:
        # "at least 80% of the shorter video must match". The rule used to
        # read the span those matched timestamps happened to cover instead,
        # and a handful of coincidences at one offset can span almost a whole
        # film: a 45-minute pair of unrelated videos matched 12 of 88 frames
        # (mean distance 0.130, barely under the 0.141 threshold) scattered
        # over 89% of the shorter timeline and reached review as "89% match".
        # On the reference library all 141 trim pairs that reached the review
        # list had a matched share below 0.53 — the span rule was admitting
        # noise. The span stays as evidence; it is not a match fraction.
        matched_share = matched / n_s if n_s else 0.0
        short_coverage = (s_last_ts - s_first_ts) / dur_s if pairs and dur_s else 0.0
        long_coverage = (l_last_ts - l_first_ts) / dur_l if pairs and dur_l else 0.0

        # A share needs a minimum correspondence count as well: a 16-frame
        # fingerprint could otherwise reach 0.8 with 13 coincidences.
        enough_evidence = matched >= max(3, self.config.possible_min_matched_frames // 2)
        if (
            matched_share >= self.config.trim_short_coverage_min
            and long_coverage >= self.config.trim_long_coverage_min
            and matched >= self.config.possible_min_matched_frames
        ):
            classification = CLASS_VERY_LIKELY
            result.very_likely += 1
        elif matched_share >= self.config.possible_threshold and enough_evidence:
            classification = CLASS_POSSIBLE
            result.possible += 1
        else:
            classification = CLASS_REJECTED
            result.rejected += 1

        # Same local-structure gate as the non-trim path, applied to the
        # matched offset correspondences
        divergent = 0.0
        if classification == CLASS_VERY_LIKELY:
            pi = np.array([i for i, _ in pairs])
            pj = np.array([j for _, j in pairs])
            divergent = _divergent_block_fraction(
                mat_s[pi],
                mat_l[pj],
                self.config.frame_width,
                self.config.frame_height,
                self.config.vl_local_divergence_distance,
            )
            if divergent > self.config.vl_local_divergence_max:
                classification = CLASS_POSSIBLE
                result.very_likely -= 1
                result.possible += 1

        # Audio divergence gate for the trim path. The windows are aligned by
        # the measured trim offset, not by normalized position, so the same
        # soundtrack compares the same moments.
        audio_dist: float | None = None
        if classification == CLASS_VERY_LIKELY:
            short_file, long_file = (
                (file_a, file_b)
                if file_a["_duration"] <= file_b["_duration"]
                else (file_b, file_a)
            )
            sig_s = self._audio.get(short_file["id"])
            sig_l = self._audio.get(long_file["id"])
            if sig_s and sig_l:
                audio_dist = audio_distance_shifted(
                    sig_s,
                    sig_l,
                    self.config.audio_bands,
                    short_file["_duration"],
                    long_file["_duration"],
                    offset_seconds,
                    self.config.audio_window_seconds,
                    self.config.audio_windows,
                )
                if audio_dist > self.config.audio_divergence_threshold:
                    classification = CLASS_POSSIBLE
                    result.very_likely -= 1
                    result.possible += 1

        evidence = json.dumps(
            {
                "mode": f"trim-offset-{offset_mode}",
                # The share of the shorter file's frames that matched — the
                # number the review list shows and sorts by
                "match_fraction": matched_share,
                "matching_frames": matched,
                "frames_compared": n_s,
                "mean_frame_distance": mean_distance,
                "duration_difference": duration_diff,
                "offset_seconds": offset_seconds,
                # Span of the matched timestamps, as a share of each duration.
                # Kept as context: it says how much of the timeline the
                # alignment covers, not how much of it matches
                "short_coverage": short_coverage,
                "long_coverage": long_coverage,
                "local_divergent_blocks": divergent,
                "audio_distance": audio_dist,
            }
        )
        self.repository.create_pair_comparison(
            scan_id=scan_id,
            file_a_id=file_a["id"],
            file_b_id=file_b["id"],
            algorithm_version=MATCH_ALGORITHM_VERSION,
            classification=classification,
            match_fraction=matched_share,
            mean_frame_distance=mean_distance,
            frames_compared=n_s,
            duration_difference=duration_diff,
            evidence=evidence,
        )


def _signature_overlap(
    mat_a: "np.ndarray", mat_b: "np.ndarray", threshold: float, min_shared: int
) -> bool:
    """True if either file has enough sparse frames matching the other.

    ``mat_*`` are (frame_count, frame_size) int16 matrices. Equivalent to
    checking every frame pair with frame_distance <= threshold, both
    directions, returning true once either side reaches ``min_shared``.
    """
    if not mat_a.size or not mat_b.size:
        return False
    diff = np.abs(mat_a[:, None, :] - mat_b[None, :, :]).mean(axis=2) / 255.0
    if (diff.min(axis=1) <= threshold).sum() >= min_shared:
        return True
    return bool((diff.min(axis=0) <= threshold).sum() >= min_shared)

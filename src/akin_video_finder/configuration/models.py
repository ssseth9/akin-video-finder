"""Configuration data models for Akin Video Finder."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class RootConfig:
    """Configuration for a single scan root."""

    path: str
    recursive: bool = True


@dataclass(frozen=True)
class ActionsConfig:
    """Action safety configuration."""

    protected_paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate and deduplicate protected folders."""
        seen: set[str] = set()
        unique: list[str] = []
        for raw in self.protected_paths:
            text = raw.strip()
            if not text:
                raise ValueError("Protected folder path cannot be empty")
            path = Path(text)
            if not path.is_absolute():
                raise ValueError(f"Protected folder must be an absolute path: {text}")
            key = os.path.normcase(str(path.resolve(strict=False)))
            if key in seen:
                continue
            seen.add(key)
            unique.append(text)
        if len(unique) != len(self.protected_paths):
            object.__setattr__(self, "protected_paths", unique)


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    file_path: str | None = None


@dataclass(frozen=True)
class FFprobeConfig:
    """FFprobe configuration."""

    executable_path: str = "ffprobe"
    timeout_seconds: int = 60
    worker_count: int = 2
    max_stderr_bytes: int = 8192


@dataclass(frozen=True)
class DatabaseConfig:
    """Database configuration."""

    path: str = "akin_video_finder.db"


@dataclass(frozen=True)
class MatchingConfig:
    """Perceptual matching configuration."""

    enabled: bool = True
    ffmpeg_path: str = "ffmpeg"
    frame_timeout_seconds: int = 30
    frame_width: int = 16
    frame_height: int = 16
    min_frames: int = 16
    max_frames: int = 96
    target_frame_interval_seconds: float = 30.0
    duration_tolerance_ratio: float = 0.02
    duration_tolerance_seconds: float = 5.0
    # Candidate gate: a pair whose shorter copy is under this fraction of
    # the longer one is never compared — no fingerprints, no frame
    # distances, no shift probe. A clip inside a compilation is not a
    # duplicate of it, and comparing one is the most expensive work a scan
    # does for the least return. Measured on the 6,441-file reference
    # library: 0.5 removed 51.6% of the comparisons and kept 5 of the 39
    # pairs a reviewer could act on; the conservative 0.88 default removed
    # 89.6% and kept none of them — such a library wants ~0.5. Unknown
    # lengths are never pruned: the gate only judges what it can measure.
    min_duration_ratio: float = 0.88
    frame_match_max_distance: float = 0.10
    very_likely_threshold: float = 0.9
    # 0.09 measured on the corpus: the composed brightness+trim pairs sit at
    # 0.08x, and 0.09 keeps 132/132 at or above expectation with zero false
    # positives. 0.08 dropped those two composed pairs to POSSIBLE.
    very_likely_mean_distance: float = 0.09
    possible_threshold: float = 0.7
    # Alternative POSSIBLE tier: a partial overlap is review-worthy when at
    # least this many frames align with strong (low) mean distance, even if
    # the coverage fraction stays below possible_threshold
    possible_min_matched_frames: int = 6
    # Sparse content signatures used to block candidate pairs before dense
    # fingerprint extraction (replaces duration-only blocking at scale)
    signature_frames: int = 8
    signature_min_shared_frames: int = 2
    # Skip uninformative 24-bit hash slices shared by more files than this
    signature_max_bucket_files: int = 200
    # A pair must share at least this many exact 16-bit band keys to be
    # candidate-verified; 1 restores the original loose behaviour
    # One shared 16-bit band is enough to enter verification. Two was a
    # recall ceiling: a pair 10 bits apart can share no exact 16-bit band
    # while the Hamming verifier would accept it.
    signature_min_band_hits: int = 1
    # Two signature frames count as shared when their 64-bit hashes differ
    # by at most this many bits
    signature_max_hamming: int = 10
    # Dense head-window hashes. Sampling by second instead of by fraction of
    # each file's own duration is the only candidate signal that survives a
    # prepended advert: a copy that starts 20s late still shares most of its
    # first minutes of frames. One sequential decode per file, cached by
    # identity, so the cost is paid once per file per version.
    head_bag_enabled: bool = True
    # Seconds decoded from the start of each file (1 fps => 90 frames)
    head_bag_seconds: float = 90.0
    head_bag_fps: float = 1.0
    # Shared 16-bit hash bands (4 per frame) before a pair is worth probing for
    # a timeline shift. Measured: the real 20s-shifted pair shares 101,
    # unrelated files share 0 to 3.
    head_bag_min_shared: int = 24
    # Concurrent ffmpeg processes for sparse signature extraction
    signature_workers: int = 4
    # Detect and remove letterbox/pillarbox bars before hashing frames
    crop_detect: bool = True
    # UI-only knob (0-100): the GUI slider's last position. The engine
    # reads the real thresholds below; strictness just records how they
    # were chosen so the slider survives a restart
    strictness: int = 50
    # Local-structure divergence gate for VERY_LIKELY: a true re-encode
    # diverges smoothly across the frame, while same-scene-different-
    # content (same-parameter generations, same-studio episodes) keeps
    # the layout but differs in detail — blocks far above the global
    # mean. If more than vl_local_divergence_max of a pair's 4x4 pixel
    # blocks exceed vl_local_divergence_distance, VERY_LIKELY downgrades
    # to POSSIBLE instead of a confident match
    vl_local_divergence_distance: float = 0.15
    vl_local_divergence_max: float = 0.20
    # Audio divergence gate for VERY_LIKELY: same-scene-different-content
    # almost never shares an audio track. Sampled spectral windows are
    # compared per normalized position; above audio_divergence_threshold
    # a VL pair downgrades to POSSIBLE. Files without an audio stream
    # keep their visual classification
    audio_enabled: bool = True
    audio_windows: int = 8
    audio_window_seconds: float = 2.0
    audio_sample_rate: int = 11025
    audio_bands: int = 32
    audio_divergence_threshold: float = 0.10
    # Coverage requirements for trimmed matches (PRODUCT_SPEC section 6)
    trim_short_coverage_min: float = 0.8
    trim_long_coverage_min: float = 0.7


@dataclass(frozen=True)
class KeeperConfig:
    """Keeper-recommendation preferences (PRODUCT_SPEC section 15)."""

    preferred_extensions: list[str] = field(default_factory=list)
    preferred_path_keywords: list[str] = field(default_factory=list)
    # How much missing length one percent of the longer copy's runtime may
    # cost, in quality "stops" (log2 units). Quality is measured from bitrate,
    # pixels, codec and audio, so a copy with twice the bitrate and four times
    # the pixels gains roughly 2.5 stops and may run ~5% shorter before the
    # longer copy wins on completeness alone. Zero makes length irrelevant
    # except as the final tiebreak; a very large value makes it decide again.
    length_penalty_per_percent: float = 0.5


@dataclass(frozen=True)
class ReportingConfig:
    """Reporting configuration."""

    output_directory: str = "reports"
    # Deprecated alias of matching.min_duration_ratio. The threshold moved
    # into [matching] when it stopped being a display filter and started
    # deciding which pairs a scan compares at all; the loader mirrors the
    # two, so this value always equals that one, and [reporting] keeps
    # being read for configs written before the move.
    review_min_duration_ratio: float = 0.88


@dataclass
class Config:
    """Main application configuration."""

    roots: list[RootConfig]
    # `.ts`/`.mts` are deliberately absent: those suffixes are as common in
    # TypeScript sources as in MPEG transport streams, and on the reference
    # library 544 of the 550 `.ts` rows were `.d.ts` declarations plus 6
    # `.mts` modules — probe attempts that could never succeed. The scanner
    # still refuses `*.d.ts`/`*.d.mts`/`*.d.cts`, so a library that really
    # holds transport streams can add `.ts`/`.mts` back without the
    # declarations coming with them.
    video_extensions: list[str] = field(
        default_factory=lambda: [
            ".mp4",
            ".mkv",
            ".avi",
            ".mov",
            ".webm",
            ".m4v",
            ".mpg",
            ".mpeg",
            ".m2ts",
            ".wmv",
            ".flv",
        ]
    )
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    follow_symlinks: bool = False
    include_hidden: bool = False
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ffprobe: FFprobeConfig = field(default_factory=FFprobeConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    keeper: KeeperConfig = field(default_factory=KeeperConfig)
    actions: ActionsConfig = field(default_factory=ActionsConfig)

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        # An empty root list is a valid state: a fresh install has no folders
        # yet, and the GUI lets a reviewer remove the last one before adding
        # another. Anything that scans must gate on this itself rather than
        # relying on construction to fail.
        for root in self.roots:
            if not root.path:
                raise ValueError("Root path cannot be empty")

        # Overlapping roots walk the same file twice, create a second row for
        # the same path, and can pair a file with itself.
        root_keys = [
            os.path.normcase(str(Path(root.path).resolve())) for root in self.roots
        ]
        for index, left in enumerate(root_keys):
            for right in root_keys[index + 1 :]:
                if (
                    left == right
                    or left.startswith(right + os.sep)
                    or right.startswith(left + os.sep)
                ):
                    raise ValueError("Configured roots must not overlap")

        if self.ffprobe.worker_count < 1:
            raise ValueError("FFprobe worker count must be at least 1")

        if self.ffprobe.timeout_seconds < 1:
            raise ValueError("FFprobe timeout must be at least 1 second")

        if self.ffprobe.max_stderr_bytes < 0:
            raise ValueError("FFprobe max_stderr_bytes cannot be negative")

        if self.matching.frame_width < 4 or self.matching.frame_height < 4:
            raise ValueError("Matching frame size must be at least 4x4")

        if self.matching.min_frames < 2:
            raise ValueError("Matching min_frames must be at least 2")

        if self.matching.max_frames < self.matching.min_frames:
            raise ValueError("Matching max_frames must be >= min_frames")

        if self.matching.frame_timeout_seconds < 1:
            raise ValueError("Matching frame timeout must be at least 1 second")

        if self.matching.target_frame_interval_seconds <= 0:
            raise ValueError("target_frame_interval_seconds must be positive")

        if not 0 < self.matching.frame_match_max_distance <= 1:
            raise ValueError("frame_match_max_distance must be in (0, 1]")

        if not 0 <= self.matching.possible_threshold <= self.matching.very_likely_threshold <= 1:
            raise ValueError("Thresholds must satisfy 0 <= possible <= very_likely <= 1")

        if not 0 < self.matching.very_likely_mean_distance <= 1:
            raise ValueError("very_likely_mean_distance must be in (0, 1]")

        if self.matching.possible_min_matched_frames < 1:
            raise ValueError("possible_min_matched_frames must be at least 1")

        if self.matching.signature_frames < 4:
            raise ValueError("signature_frames must be at least 4")

        if self.matching.signature_min_shared_frames < 1:
            raise ValueError("signature_min_shared_frames must be at least 1")

        if self.matching.signature_max_bucket_files < 1:
            raise ValueError("signature_max_bucket_files must be at least 1")

        if self.matching.signature_min_band_hits < 1:
            raise ValueError("signature_min_band_hits must be at least 1")

        if not 0 < self.matching.signature_max_hamming <= 64:
            raise ValueError("signature_max_hamming must be in (0, 64]")

        if self.matching.signature_workers < 1:
            raise ValueError("signature_workers must be at least 1")

        if self.matching.head_bag_enabled:
            if self.matching.head_bag_seconds <= 0:
                raise ValueError("head_bag_seconds must be positive")
            if not 0 < self.matching.head_bag_fps <= 10:
                raise ValueError("head_bag_fps must be in (0, 10]")
            if self.matching.head_bag_min_shared < 1:
                raise ValueError("head_bag_min_shared must be at least 1")

        if not 0 <= self.matching.strictness <= 100:
            raise ValueError("strictness must be in [0, 100]")

        if self.keeper.length_penalty_per_percent < 0:
            raise ValueError("keeper.length_penalty_per_percent cannot be negative")

        if not 0 < self.matching.trim_short_coverage_min <= 1:
            raise ValueError("trim_short_coverage_min must be in (0, 1]")

        if not 0 < self.matching.trim_long_coverage_min <= 1:
            raise ValueError("trim_long_coverage_min must be in (0, 1]")

        if not 0 < self.reporting.review_min_duration_ratio <= 1:
            raise ValueError("reporting.review_min_duration_ratio must be in (0, 1]")

        if not 0 < self.matching.min_duration_ratio <= 1:
            raise ValueError("matching.min_duration_ratio must be in (0, 1]")


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""

    def __init__(self, message: str, path: str | None = None) -> None:
        self.message = message
        self.path = path
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if self.path:
            return f"Configuration error in {self.path}: {self.message}"
        return f"Configuration error: {self.message}"

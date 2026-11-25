"""Application service for scanning video files."""

import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from akin_video_finder import __version__
from akin_video_finder.application.exact_match import (
    ExactMatchResult,
    ExactMatchService,
)
from akin_video_finder.application.perceptual_match import (
    PerceptualMatchResult,
    PerceptualMatchService,
)
from akin_video_finder.configuration.models import Config, RootConfig
from akin_video_finder.core.cache_policy import CachePolicy
from akin_video_finder.core.models import (
    CancellationToken,
    DiscoveredFile,
    FilePresence,
    ProbeStatus,
    ProgressEvent,
    RootAvailability,
    ScanStatus,
)
from akin_video_finder.core.path_utils import get_path_key
from akin_video_finder.discovery.scanner import DiscoveryError, FileScanner
from akin_video_finder.metadata.ffprobe import PROBE_ALGORITHM_VERSION, ProbeResult, probe_file
from akin_video_finder.persistence.repository import DatabaseRepository

logger = logging.getLogger(__name__)

# Matching settings that are pure performance or environment knobs: they
# change how long a scan takes, never what it finds, so they must not mark
# results stale
_FINGERPRINT_IGNORED_MATCHING = ("ffmpeg_path", "frame_timeout_seconds", "signature_workers")

# Bumped when the payload changes, so a scan recorded by an older build can
# be compared the way that build compared it (see fingerprint_matches).
# v3: the coarse candidate tier and its two settings are gone, so a payload
# from v2 was computed over a different set of keys and described a scan that
# searched a different candidate space.
FINGERPRINT_VERSION = "v3"


def config_fingerprint(config: Config) -> str:
    """Fingerprint the settings that change what a scan finds.

    Covers the roots, the video extensions and the matching thresholds —
    not the worker counts or tool paths. Stored on the scan row, so the GUI
    can tell whether the results on screen still match the current config.

    Args:
        config: Configuration to fingerprint.

    Returns:
        Versioned fingerprint string.
    """
    matching = sorted(
        (key, value)
        for key, value in asdict(config.matching).items()
        if key not in _FINGERPRINT_IGNORED_MATCHING
    )
    payload = repr(
        (
            config.roots,
            config.video_extensions,
            config.include_patterns,
            config.exclude_patterns,
            config.follow_symlinks,
            matching,
        )
    )
    digest = hashlib.md5(payload.encode()).hexdigest()
    return f"{FINGERPRINT_VERSION}:{digest}"


def legacy_config_fingerprint(config: Config) -> str:
    """Fingerprint as recorded by builds before ``FINGERPRINT_VERSION``."""
    return hashlib.md5((str(config.roots) + str(config.video_extensions)).encode()).hexdigest()


def fingerprint_matches(stored: str | None, config: Config) -> bool:
    """Whether a stored scan fingerprint still describes ``config``.

    An older scan carries an unversioned hash over roots and extensions
    only; it is compared the way it was written so that upgrading does not
    flag every existing scan as stale.
    """
    if not stored:
        return True
    if stored.startswith(f"{FINGERPRINT_VERSION}:"):
        return stored == config_fingerprint(config)
    return stored == legacy_config_fingerprint(config)


@dataclass
class RootScanStats:
    """Statistics collected while scanning a single root."""

    total_files: int = 0
    present_files: int = 0
    missing_files: int = 0
    error_files: int = 0
    cache_hits: int = 0
    errors: list[str] = field(default_factory=list)


class ScanService:
    """Service for orchestrating video scans."""

    def __init__(
        self,
        config: Config,
        repository: DatabaseRepository,
        progress_callback: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        """Initialize the scan service.

        Args:
            config: Application configuration.
            repository: Database repository.
            progress_callback: Optional callback for progress events.
        """
        self.config = config
        self.repository = repository
        self.progress_callback = progress_callback
        self.cache_policy = CachePolicy(algorithm_version=PROBE_ALGORITHM_VERSION)
        self.cancellation_token = CancellationToken()

    def _emit_progress(self, event: ProgressEvent) -> None:
        """Emit a progress event.

        Args:
            event: Progress event to emit.
        """
        if self.progress_callback:
            self.progress_callback(event)

    def scan(self) -> dict:
        """Execute a full scan of all configured roots.

        Returns:
            Scan result summary.
        """
        # Create scan record
        scan_id = self.repository.create_scan(
            application_version=__version__,
            config_fingerprint=self._compute_config_fingerprint(),
        )

        self._emit_progress(
            ProgressEvent(
                phase="initialization",
                completed_count=0,
                total_count=0,
                message="Scan initialized",
            )
        )

        total_files = 0
        present_files = 0
        missing_files = 0
        error_files = 0
        cache_hits = 0
        errors: list[str] = []

        try:
            for root_config in self.config.roots:
                if self.cancellation_token.is_cancelled():
                    self.repository.update_scan_status(
                        scan_id,
                        ScanStatus.INTERRUPTED,
                        total_files=total_files,
                        present_files=present_files,
                        missing_files=missing_files,
                        error_files=error_files,
                        cache_hits=cache_hits,
                        error_summary="; ".join(errors),
                    )
                    return {
                        "scan_id": scan_id,
                        "status": ScanStatus.INTERRUPTED.value,
                        "total_files": total_files,
                        "present_files": present_files,
                        "missing_files": missing_files,
                        "error_files": error_files,
                        "cache_hits": cache_hits,
                        "files_hashed": 0,
                        "hash_cache_hits": 0,
                        "exact_pairs": 0,
                        "files_signed": 0,
                        "signature_cache_hits": 0,
                        "signature_pairs": 0,
                        "files_fingerprinted": 0,
                        "fingerprint_cache_hits": 0,
                        "candidate_pairs": 0,
                        "comparisons": 0,
                        "duration_pruned": 0,
                        "very_likely": 0,
                        "possible": 0,
                        "rejected": 0,
                        "offset_probes": 0,
                        "offset_probe_hits": 0,
                        "offset_probes_candidates": 0,
                        "offset_probes_cached": 0,
                        "offset_probes_reconfirmed": 0,
                        "errors": errors,
                    }

                try:
                    root_stats = self._scan_root(scan_id, root_config)
                    total_files += root_stats.total_files
                    present_files += root_stats.present_files
                    missing_files += root_stats.missing_files
                    error_files += root_stats.error_files
                    cache_hits += root_stats.cache_hits
                    errors.extend(root_stats.errors)
                except Exception as e:
                    error_msg = f"Failed to scan root {root_config.path}: {e}"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    error_files += 1

            # Exact-match phase: hash size-collision candidates
            exact_result = ExactMatchResult()
            if not self.cancellation_token.is_cancelled():
                matcher = ExactMatchService(self.repository, progress_callback=self._emit_progress)
                exact_result = matcher.find_exact_duplicates(scan_id, self.cancellation_token)
                errors.extend(exact_result.errors)

            # Perceptual-match phase: fingerprints + pairwise comparison
            perceptual_result = PerceptualMatchResult()
            if self.config.matching.enabled and not self.cancellation_token.is_cancelled():
                perceptual_matcher = PerceptualMatchService(
                    self.repository,
                    self.config.matching,
                    progress_callback=self._emit_progress,
                )
                perceptual_result = perceptual_matcher.find_perceptual_duplicates(
                    scan_id, self.cancellation_token
                )
                errors.extend(perceptual_result.errors)

            final_status = (
                ScanStatus.INTERRUPTED
                if self.cancellation_token.is_cancelled()
                else ScanStatus.COMPLETED
            )
            self.repository.update_scan_status(
                scan_id,
                final_status,
                total_files=total_files,
                present_files=present_files,
                missing_files=missing_files,
                error_files=error_files,
                cache_hits=cache_hits,
                error_summary="; ".join(errors) if errors else None,
            )

            self._emit_progress(
                ProgressEvent(
                    phase="completed",
                    completed_count=total_files,
                    total_count=total_files,
                    message=f"Scan completed: {total_files} files",
                )
            )

            return {
                "scan_id": scan_id,
                "status": final_status.value,
                "total_files": total_files,
                "present_files": present_files,
                "missing_files": missing_files,
                "error_files": error_files,
                "cache_hits": cache_hits,
                "files_hashed": exact_result.files_hashed,
                "hash_cache_hits": exact_result.hash_cache_hits,
                "exact_pairs": exact_result.pairs_found,
                "files_signed": perceptual_result.files_signed,
                "signature_cache_hits": perceptual_result.signature_cache_hits,
                "signature_pairs": perceptual_result.signature_pairs,
                "files_fingerprinted": perceptual_result.files_fingerprinted,
                "fingerprint_cache_hits": perceptual_result.fingerprint_cache_hits,
                "files_head_bagged": perceptual_result.files_head_bagged,
                "head_bag_cache_hits": perceptual_result.head_bag_cache_hits,
                "candidate_pairs": perceptual_result.candidate_pairs,
                "comparisons": perceptual_result.comparisons,
                "duration_pruned": perceptual_result.duration_pruned,
                "very_likely": perceptual_result.very_likely,
                "possible": perceptual_result.possible,
                "rejected": perceptual_result.rejected,
                "offset_probes": perceptual_result.offset_probes,
                "offset_probe_hits": perceptual_result.offset_probe_hits,
                "offset_probes_candidates": perceptual_result.offset_probes_candidates,
                "offset_probes_cached": perceptual_result.offset_probes_cached,
                "offset_probes_reconfirmed": perceptual_result.offset_probes_reconfirmed,
                "errors": errors,
            }

        except Exception as e:
            logger.exception("Scan failed")
            self.repository.update_scan_status(
                scan_id,
                ScanStatus.FAILED,
                total_files=total_files,
                present_files=present_files,
                missing_files=missing_files,
                error_files=error_files,
                cache_hits=cache_hits,
                error_summary=str(e),
            )
            raise

    def _scan_root(self, scan_id: int, root_config: RootConfig) -> RootScanStats:
        """Scan a single root.

        Args:
            scan_id: Scan ID.
            root_config: Root configuration.

        Returns:
            Root scan statistics.
        """
        root_path = root_config.path
        normalized_key = get_path_key(root_path)

        # Get or create root record
        root_id = self.repository.get_or_create_root(root_path, normalized_key)

        # Check root availability
        root_available = Path(root_path).exists()
        availability = (
            RootAvailability.AVAILABLE if root_available else RootAvailability.UNAVAILABLE
        )

        # Create root scan state
        root_scan_state_id = self.repository.create_root_scan_state(scan_id, root_id, availability)

        self._emit_progress(
            ProgressEvent(
                phase="discovery",
                completed_count=0,
                total_count=0,
                current_root=root_path,
                message=f"Scanning root: {root_path}",
            )
        )

        if not root_available:
            self.repository.update_root_scan_state(
                root_scan_state_id,
                discovery_completed=False,
                error_message="Root unavailable",
            )
            return RootScanStats(errors=[f"Root unavailable: {root_path}"])

        # Discover files
        scanner = FileScanner(
            video_extensions=self.config.video_extensions,
            include_patterns=self.config.include_patterns,
            exclude_patterns=self.config.exclude_patterns,
            follow_symlinks=self.config.follow_symlinks,
            include_hidden=self.config.include_hidden,
        )

        discovered_files = []
        try:
            for discovered_file in scanner.scan_root(
                root_path,
                root_id,
                recursive=root_config.recursive,
                cancellation_token=self.cancellation_token,
            ):
                discovered_files.append(discovered_file)
        except DiscoveryError as e:
            self.repository.update_root_scan_state(
                root_scan_state_id,
                discovery_completed=False,
                error_message=str(e),
                availability=RootAvailability.UNAVAILABLE.value,
            )
            return RootScanStats(errors=[str(e)])

        # Mark discovery complete
        self.repository.update_root_scan_state(root_scan_state_id, discovery_completed=True)

        # Process discovered files
        return self._process_files(scan_id, root_id, discovered_files)

    def _process_files(
        self, scan_id: int, root_id: int, discovered_files: list[DiscoveredFile]
    ) -> RootScanStats:
        """Process discovered files.

        Args:
            scan_id: Scan ID.
            root_id: Root ID.
            discovered_files: List of discovered files.

        Returns:
            Processing statistics.
        """
        total_files = len(discovered_files)
        present_files = 0
        missing_files = 0
        error_files = 0
        cache_hits = 0
        errors: list[str] = []

        # Track previously seen files for this root
        previous_files = {
            f["normalized_path_key"]: f for f in self.repository.get_files_for_root(root_id)
        }

        # Decide and probe before touching the database. ffprobe is a
        # process spawn per file (~100 ms on Windows) and used to run
        # serially, even though ffprobe.worker_count existed for exactly
        # this — only the evaluator honoured it. The decisions read cached
        # rows only, so a cancelled probe phase leaves no half-written
        # state: rows are created in the apply loop, next to their
        # metadata, in one transaction each.
        probe_targets = [
            discovered_file
            for discovered_file in discovered_files
            if self._needs_probe(discovered_file, previous_files)
        ]
        probes = self._probe_files(probe_targets)

        for discovered_file in discovered_files:
            if self.cancellation_token.is_cancelled():
                break

            try:
                stats = self._process_file(
                    scan_id,
                    root_id,
                    discovered_file,
                    previous_files,
                    probes.get(discovered_file.normalized_path_key),
                )
                present_files += stats["present"]
                missing_files += stats["missing"]
                error_files += stats["error"]
                cache_hits += stats["cache_hit"]
            except Exception as e:
                error_msg = f"Error processing {discovered_file.original_path}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)
                error_files += 1

        # Close the probing phase even when every file was a cache hit
        self._emit_progress(
            ProgressEvent(
                phase="probing",
                completed_count=total_files,
                total_count=total_files,
                current_root=f"Root {root_id}",
                message=f"Processed {total_files} files ({cache_hits} unchanged)",
            )
        )

        # Mark previously seen but not discovered files as missing. A
        # cancelled walk is partial, so its absent keys mean "not looked at",
        # not "deleted".
        if not self.cancellation_token.is_cancelled():
            discovered_keys = {f.normalized_path_key for f in discovered_files}
            for key, cached_file in previous_files.items():
                if (
                    key not in discovered_keys
                    and cached_file.get("presence") == FilePresence.PRESENT.value
                ):
                    self.repository.mark_file_missing(cached_file["id"], scan_id)
                    missing_files += 1

        return RootScanStats(
            total_files=total_files,
            present_files=present_files,
            missing_files=missing_files,
            error_files=error_files,
            cache_hits=cache_hits,
            errors=errors,
        )

    def _needs_probe(self, discovered_file: DiscoveredFile, previous_files: dict[str, Any]) -> bool:
        """Whether this file's cached metadata is missing or stale."""
        cached_file = previous_files.get(discovered_file.normalized_path_key)
        cached_probe = (
            self.repository.get_probe_metadata(cached_file["id"]) if cached_file else None
        )
        return self.cache_policy.should_reprobe(
            discovered_file.size,
            discovered_file.mtime_ns,
            cached_file or {},
            cached_probe,
        )

    def _probe_files(self, targets: list[DiscoveredFile]) -> dict[str, ProbeResult | None]:
        """Probe files on the configured pool, keyed by normalized path.

        Probing is pure latency — an ffprobe process spawn per file — so it
        parallelizes far better than the decode-bound phases. Results are
        consumed in submission order, so the window only has to keep every
        worker busy while bounding memory on a very large root. A file
        whose probe raises is recorded as None instead of sinking the
        batch; the apply loop turns that into an error for that file alone.
        """
        if not targets:
            return {}

        workers = max(1, self.config.ffprobe.worker_count)
        ffprobe_path = self.config.ffprobe.executable_path
        timeout_seconds = self.config.ffprobe.timeout_seconds
        max_stderr_bytes = self.config.ffprobe.max_stderr_bytes
        total = len(targets)
        window = max(workers * 4, 8)

        def _probe(discovered_file: DiscoveredFile) -> ProbeResult | None:
            try:
                return probe_file(
                    file_path=Path(discovered_file.original_path),
                    ffprobe_path=ffprobe_path,
                    timeout_seconds=timeout_seconds,
                    max_stderr_bytes=max_stderr_bytes,
                )
            except Exception:
                logger.exception(f"FFprobe crashed for {discovered_file.original_path}")
                return None

        results: dict[str, ProbeResult | None] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, total, window):
                if self.cancellation_token.is_cancelled():
                    break
                chunk = targets[start : start + window]
                for discovered_file, probe_result in zip(
                    chunk, pool.map(_probe, chunk), strict=True
                ):
                    results[discovered_file.normalized_path_key] = probe_result
                    done += 1
                    self._emit_progress(
                        ProgressEvent(
                            phase="probing",
                            completed_count=done,
                            total_count=total,
                            current_path=discovered_file.original_path,
                            message=f"Probing {done}/{total}",
                        )
                    )
        return results

    def _process_file(
        self,
        scan_id: int,
        root_id: int,
        discovered_file: DiscoveredFile,
        previous_files: dict[str, Any],
        probe_result: ProbeResult | None,
    ) -> dict[str, int]:
        """Store one file row and its probe result in a single transaction.

        Args:
            scan_id: Scan ID.
            root_id: Root ID.
            discovered_file: Discovered file.
            previous_files: Previously seen files for this root.
            probe_result: Result from the parallel probe pass, or None when
                the file was a cache hit, the phase was cancelled, or the
                probe crashed.

        Returns:
            Processing statistics for this file.
        """
        cached_file = previous_files.get(discovered_file.normalized_path_key)
        cached_probe = None

        if cached_file:
            cached_probe = self.repository.get_probe_metadata(cached_file["id"])

        # Check if we can reuse cache
        should_reprobe = self.cache_policy.should_reprobe(
            discovered_file.size,
            discovered_file.mtime_ns,
            cached_file or {},
            cached_probe,
        )

        if not should_reprobe and cached_file and cached_probe:
            # Cache hit
            self.repository.update_file(
                cached_file["id"],
                discovered_file.size,
                discovered_file.mtime_ns,
                scan_id,
                FilePresence.PRESENT,
                discovered_file.is_symlink,
                discovered_file.link_target,
            )
            return {"present": 1, "missing": 0, "error": 0, "cache_hit": 1}

        # Need to probe
        if cached_file:
            # Update existing file
            file_id = cached_file["id"]
            self.repository.update_file(
                file_id,
                discovered_file.size,
                discovered_file.mtime_ns,
                scan_id,
                FilePresence.PRESENT,
                discovered_file.is_symlink,
                discovered_file.link_target,
            )
        else:
            # Create new file
            file_id = self.repository.create_file(
                root_id=root_id,
                original_path=discovered_file.original_path,
                normalized_path_key=discovered_file.normalized_path_key,
                file_name=discovered_file.file_name,
                extension=discovered_file.extension,
                size=discovered_file.size,
                mtime_ns=discovered_file.mtime_ns,
                scan_id=scan_id,
                is_symlink=discovered_file.is_symlink,
                link_target=discovered_file.link_target,
            )

        # The probe already ran on the pool. None here means it was skipped
        # (cancelled phase) or crashed — keep the row so the next scan
        # retries it, but never claim metadata we never received. A cached
        # probe from before the change must go, or the next scan would trust
        # its old size/mtime and never retry.
        if probe_result is None:
            if cached_probe:
                self.repository.delete_probe_metadata(file_id)
            self.repository.commit()
            return {"present": 1, "missing": 0, "error": 1, "cache_hit": 0}

        # Store probe result
        if cached_probe:
            self.repository.update_probe_metadata(
                file_id=file_id,
                status=probe_result.status,
                algorithm_version=probe_result.algorithm_version,
                error_category=probe_result.error_category,
                error_message=probe_result.error_message,
                stderr=probe_result.stderr,
                exit_code=probe_result.exit_code,
                container_format_name=probe_result.metadata.container.format_name
                if probe_result.metadata
                else None,
                container_format_long_name=probe_result.metadata.container.format_long_name
                if probe_result.metadata
                else None,
                container_duration=probe_result.metadata.container.duration
                if probe_result.metadata
                else None,
                container_size=probe_result.metadata.container.size
                if probe_result.metadata
                else None,
                container_bitrate=probe_result.metadata.container.bitrate
                if probe_result.metadata
                else None,
                container_start_time=probe_result.metadata.container.start_time
                if probe_result.metadata
                else None,
                has_video=probe_result.metadata.has_video if probe_result.metadata else False,
                video_stream_count=probe_result.metadata.video_stream_count
                if probe_result.metadata
                else 0,
                audio_stream_count=probe_result.metadata.audio_stream_count
                if probe_result.metadata
                else 0,
                subtitle_stream_count=probe_result.metadata.subtitle_stream_count
                if probe_result.metadata
                else 0,
                primary_video_codec=probe_result.metadata.video_streams[0].codec_name
                if probe_result.metadata and probe_result.metadata.video_streams
                else None,
                primary_video_width=probe_result.metadata.video_streams[0].width
                if probe_result.metadata and probe_result.metadata.video_streams
                else None,
                primary_video_height=probe_result.metadata.video_streams[0].height
                if probe_result.metadata and probe_result.metadata.video_streams
                else None,
                primary_video_fps=probe_result.metadata.video_streams[0].r_frame_rate
                if probe_result.metadata and probe_result.metadata.video_streams
                else None,
                primary_audio_codec=probe_result.metadata.audio_streams[0].codec_name
                if probe_result.metadata and probe_result.metadata.audio_streams
                else None,
                primary_audio_sample_rate=probe_result.metadata.audio_streams[0].sample_rate
                if probe_result.metadata and probe_result.metadata.audio_streams
                else None,
                primary_audio_channels=probe_result.metadata.audio_streams[0].channel_count
                if probe_result.metadata and probe_result.metadata.audio_streams
                else None,
            )
        else:
            self.repository.create_probe_metadata(
                file_id=file_id,
                status=probe_result.status,
                algorithm_version=probe_result.algorithm_version,
                error_category=probe_result.error_category,
                error_message=probe_result.error_message,
                stderr=probe_result.stderr,
                exit_code=probe_result.exit_code,
                container_format_name=probe_result.metadata.container.format_name
                if probe_result.metadata
                else None,
                container_format_long_name=probe_result.metadata.container.format_long_name
                if probe_result.metadata
                else None,
                container_duration=probe_result.metadata.container.duration
                if probe_result.metadata
                else None,
                container_size=probe_result.metadata.container.size
                if probe_result.metadata
                else None,
                container_bitrate=probe_result.metadata.container.bitrate
                if probe_result.metadata
                else None,
                container_start_time=probe_result.metadata.container.start_time
                if probe_result.metadata
                else None,
                has_video=probe_result.metadata.has_video if probe_result.metadata else False,
                video_stream_count=probe_result.metadata.video_stream_count
                if probe_result.metadata
                else 0,
                audio_stream_count=probe_result.metadata.audio_stream_count
                if probe_result.metadata
                else 0,
                subtitle_stream_count=probe_result.metadata.subtitle_stream_count
                if probe_result.metadata
                else 0,
                primary_video_codec=probe_result.metadata.video_streams[0].codec_name
                if probe_result.metadata and probe_result.metadata.video_streams
                else None,
                primary_video_width=probe_result.metadata.video_streams[0].width
                if probe_result.metadata and probe_result.metadata.video_streams
                else None,
                primary_video_height=probe_result.metadata.video_streams[0].height
                if probe_result.metadata and probe_result.metadata.video_streams
                else None,
                primary_video_fps=probe_result.metadata.video_streams[0].r_frame_rate
                if probe_result.metadata and probe_result.metadata.video_streams
                else None,
                primary_audio_codec=probe_result.metadata.audio_streams[0].codec_name
                if probe_result.metadata and probe_result.metadata.audio_streams
                else None,
                primary_audio_sample_rate=probe_result.metadata.audio_streams[0].sample_rate
                if probe_result.metadata and probe_result.metadata.audio_streams
                else None,
                primary_audio_channels=probe_result.metadata.audio_streams[0].channel_count
                if probe_result.metadata and probe_result.metadata.audio_streams
                else None,
            )

        self.repository.commit()

        if probe_result.status == ProbeStatus.SUCCESS:
            return {"present": 1, "missing": 0, "error": 0, "cache_hit": 0}
        else:
            return {"present": 1, "missing": 0, "error": 1, "cache_hit": 0}

    def cancel(self) -> None:
        """Cancel the current scan."""
        self.cancellation_token.cancel()

    def _compute_config_fingerprint(self) -> str:
        """Compute a fingerprint of the configuration.

        Returns:
            Configuration fingerprint string.
        """
        return config_fingerprint(self.config)

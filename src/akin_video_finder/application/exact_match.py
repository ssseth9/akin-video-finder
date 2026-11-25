"""Exact duplicate detection via full-file SHA-256 hashing.

Only files sharing an exact byte size are hashed. A pair may be reported
as EXACT only when complete SHA-256 digests are equal — partial or
sampled hashes are never used to prove identity. Each directly compared
combination is its own pair; files are not clustered into a group.
"""

import hashlib
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from akin_video_finder.core.models import CancellationToken, ProgressEvent
from akin_video_finder.persistence.repository import DatabaseRepository

logger = logging.getLogger(__name__)

HASH_ALGORITHM_VERSION = "sha256-1"
CLASS_EXACT = "EXACT"
EXACT_ALGORITHM_VERSION = "sha256-pair-v1"


def compute_file_sha256(path: Path) -> str:
    """Compute the complete SHA-256 digest of a file.

    Args:
        path: File path.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@dataclass
class ExactMatchResult:
    """Statistics from an exact-match pass."""

    size_groups: int = 0
    candidate_files: int = 0
    files_hashed: int = 0
    hash_cache_hits: int = 0
    pairs_found: int = 0
    errors: list[str] = field(default_factory=list)


class ExactMatchService:
    """Finds byte-identical files and records each combination as a pair."""

    def __init__(
        self,
        repository: DatabaseRepository,
        progress_callback: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        """Initialize the exact-match service.

        Args:
            repository: Database repository.
            progress_callback: Optional callback for progress events.
        """
        self.repository = repository
        self.progress_callback = progress_callback

    def _emit_progress(self, event: ProgressEvent) -> None:
        if self.progress_callback:
            self.progress_callback(event)

    def find_exact_duplicates(
        self,
        scan_id: int,
        cancellation_token: CancellationToken | None = None,
    ) -> ExactMatchResult:
        """Hash size-collision candidates and persist each EXACT pair.

        Args:
            scan_id: Scan ID to attribute pairs to.
            cancellation_token: Optional cancellation token.

        Returns:
            Exact-match statistics.
        """
        result = ExactMatchResult()

        # Pairs are recomputed per scan; drop stale EXACT rows first so a
        # file that changed or went missing cannot keep an old pair.
        self.repository.delete_pair_comparisons(scan_id, CLASS_EXACT)

        # Group present files by exact size, limited to roots that were
        # actually scanned this run — files under removed or unavailable
        # roots are stale inventory, not live candidates. Symlinks are
        # aliases to an already-inventoried target, not copies.
        scanned_roots = self.repository.get_scanned_root_ids(scan_id)
        by_size: dict[int, list[dict[str, Any]]] = {}
        for f in self.repository.get_present_files():
            if f["is_symlink"] or f["root_id"] not in scanned_roots:
                continue
            by_size.setdefault(f["size"], []).append(f)

        candidates = [group for group in by_size.values() if len(group) > 1]
        result.size_groups = len(candidates)
        result.candidate_files = sum(len(group) for group in candidates)

        if not candidates:
            self.repository.commit()
            return result

        # Hash candidates, reusing cached digests when identity is unchanged
        by_hash: dict[str, list[dict[str, Any]]] = {}
        completed = 0
        for group in candidates:
            for file_row in group:
                if cancellation_token and cancellation_token.is_cancelled():
                    self.repository.commit()
                    return result

                completed += 1
                self._emit_progress(
                    ProgressEvent(
                        phase="hashing",
                        completed_count=completed,
                        total_count=result.candidate_files,
                        current_path=file_row["original_path"],
                        message=f"Hashing {completed}/{result.candidate_files}",
                    )
                )

                digest = self._hash_file(file_row, result)
                if digest is not None:
                    by_hash.setdefault(digest, []).append(file_row)

        # Every combination of an identical set is its own pair. A reviewer's
        # dismissal hides that one pair; the other combinations stay visible.
        dismissed = self.repository.get_dismissed_pairs()
        for members in by_hash.values():
            if len(members) < 2:
                continue
            for file_a, file_b in itertools.combinations(members, 2):
                key = tuple(sorted((file_a["normalized_path_key"], file_b["normalized_path_key"])))
                if key in dismissed:
                    continue
                if file_a["id"] > file_b["id"]:
                    file_a, file_b = file_b, file_a
                self.repository.create_pair_comparison(
                    scan_id=scan_id,
                    file_a_id=file_a["id"],
                    file_b_id=file_b["id"],
                    algorithm_version=EXACT_ALGORITHM_VERSION,
                    classification=CLASS_EXACT,
                    match_fraction=1.0,
                    mean_frame_distance=0.0,
                    frames_compared=0,
                    duration_difference=0.0,
                    evidence=json.dumps({"mode": "sha256", "sha256": True}),
                )
                result.pairs_found += 1

        self.repository.commit()
        return result

    def _hash_file(self, file_row: dict[str, Any], result: ExactMatchResult) -> str | None:
        """Hash a file, reusing the cache when file identity is unchanged."""
        cached = self.repository.get_exact_hash(file_row["id"])
        if (
            cached
            and cached["file_size"] == file_row["size"]
            and cached["mtime_ns"] == file_row["mtime_ns"]
            and cached["algorithm_version"] == HASH_ALGORITHM_VERSION
        ):
            result.hash_cache_hits += 1
            return str(cached["sha256"])

        try:
            digest = compute_file_sha256(Path(file_row["original_path"]))
        except OSError as e:
            error_msg = f"Hash failed for {file_row['original_path']}: {e}"
            logger.error(error_msg)
            result.errors.append(error_msg)
            return None

        self.repository.upsert_exact_hash(
            file_row["id"],
            digest,
            file_row["size"],
            file_row["mtime_ns"],
            HASH_ALGORITHM_VERSION,
        )
        # Commit per file: hashing is I/O-bound and slow, so each result is
        # durable progress a crash or interruption must not lose
        self.repository.commit()
        result.files_hashed += 1
        return digest

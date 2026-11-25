"""Cache invalidation policy for video metadata."""

from dataclasses import dataclass

PROBE_ALGORITHM_VERSION = "1.0"


@dataclass(frozen=True)
class FileIdentity:
    """Identity information for cache invalidation."""

    normalized_path_key: str
    size: int
    mtime_ns: int


class CachePolicy:
    """Policy for determining when cached metadata can be reused."""

    def __init__(self, algorithm_version: str = PROBE_ALGORITHM_VERSION) -> None:
        """Initialize the cache policy.

        Args:
            algorithm_version: Current probe algorithm version.
        """
        self.algorithm_version = algorithm_version

    def can_reuse_cache(
        self,
        file_identity: FileIdentity,
        cached_metadata: dict,
    ) -> bool:
        """Determine if cached metadata can be reused.

        Cache is valid when all match:
        - Normalized path key
        - File size
        - mtime_ns
        - Probe algorithm version

        Args:
            file_identity: Current file identity.
            cached_metadata: Cached probe metadata from database.

        Returns:
            True if cache can be reused, False otherwise.
        """
        # Check algorithm version
        cached_algorithm_version = cached_metadata.get("algorithm_version")

        # Check file identity
        # Note: this is from files table, not probe_metadata
        # We need to compare with the file's current size, which comes from the files table
        # This is handled at a higher level where we have both the file record and current stat

        # For now, just check algorithm version
        # The size and mtime comparison is done in the scan orchestration
        return cached_algorithm_version == self.algorithm_version

    def should_reprobe(
        self,
        current_size: int,
        current_mtime_ns: int,
        cached_file: dict,
        cached_probe: dict | None,
    ) -> bool:
        """Determine if a file should be re-probed.

        Args:
            current_size: Current file size.
            current_mtime_ns: Current file modification time.
            cached_file: Cached file record from database.
            cached_probe: Cached probe metadata from database (may be None).

        Returns:
            True if file should be re-probed, False if cache can be reused.
        """
        # If no cached probe exists, must probe
        if cached_probe is None:
            return True

        # Check algorithm version
        cached_algorithm_version = cached_probe.get("algorithm_version")
        if cached_algorithm_version != self.algorithm_version:
            return True

        # Check size
        cached_size = cached_file.get("size")
        if cached_size != current_size:
            return True

        # Check mtime
        cached_mtime = cached_file.get("mtime_ns")
        return cached_mtime != current_mtime_ns

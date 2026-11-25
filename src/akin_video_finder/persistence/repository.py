"""Repository layer for database operations."""

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from akin_video_finder.core.content_identity import content_key
from akin_video_finder.core.models import (
    FilePresence,
    ProbeStatus,
    RootAvailability,
    ScanStatus,
)
from akin_video_finder.persistence.schema import initialize_database

# SQLite allows 999 bound parameters by default; stay well inside it
_DELETE_CHUNK = 500


class DatabaseRepository:
    """Repository for database operations."""

    def __init__(self, db_path: Path) -> None:
        """Initialize the repository.

        Args:
            db_path: Path to the database file.
        """
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._conn is None:
            self._conn = initialize_database(self.db_path)
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "DatabaseRepository":
        """Context manager entry."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Context manager exit."""
        self.close()

    # Root operations

    def get_or_create_root(self, original_path: str, normalized_path_key: str) -> int:
        """Get an existing root or create a new one.

        Args:
            original_path: Original root path.
            normalized_path_key: Normalized path key for comparison.

        Returns:
            Root ID.
        """
        cursor = self.conn.execute(
            """
            SELECT id FROM configured_roots WHERE normalized_path_key = ?
            """,
            (normalized_path_key,),
        )
        row = cursor.fetchone()

        if row:
            return int(row[0])

        now = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO configured_roots
            (original_path, normalized_path_key, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (original_path, normalized_path_key, now, now),
        )
        lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise RuntimeError("Failed to create configured root record")
        return lastrowid

    def get_root_by_normalized_key(self, normalized_path_key: str) -> dict[str, Any] | None:
        """Get a root by its normalized path key.

        Args:
            normalized_path_key: Normalized path key.

        Returns:
            Root dict or None if not found.
        """
        cursor = self.conn.execute(
            """
            SELECT id, original_path, normalized_path_key, created_at, updated_at
            FROM configured_roots
            WHERE normalized_path_key = ?
            """,
            (normalized_path_key,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all_roots(self) -> list[dict[str, Any]]:
        """Get all configured roots.

        Returns:
            List of root dicts.
        """
        cursor = self.conn.execute(
            """
            SELECT id, original_path, normalized_path_key, created_at, updated_at
            FROM configured_roots
            ORDER BY original_path
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_file_count(self) -> int:
        """Total files in the database across all roots."""
        row = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()
        return int(row[0])

    def mark_interrupted_scans(self) -> int:
        """Flag scans left at 'running' — orphaned by a killed process."""
        cursor = self.conn.execute(
            "UPDATE scan_runs SET status = 'interrupted' WHERE status = 'running'"
        )
        self.conn.commit()
        return cursor.rowcount

    # Scan operations

    def create_scan(
        self,
        application_version: str,
        config_fingerprint: str | None = None,
    ) -> int:
        """Create a new scan run.

        Args:
            application_version: Application version string.
            config_fingerprint: Optional configuration fingerprint.

        Returns:
            Scan ID.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO scan_runs (
                start_time, status, application_version, config_fingerprint
            )
            VALUES (?, ?, ?, ?)
            """,
            (now, ScanStatus.RUNNING.value, application_version, config_fingerprint),
        )
        result = cursor.lastrowid
        assert result is not None
        return result

    def update_scan_status(
        self,
        scan_id: int,
        status: ScanStatus,
        total_files: int = 0,
        present_files: int = 0,
        missing_files: int = 0,
        error_files: int = 0,
        cache_hits: int = 0,
        error_summary: str | None = None,
    ) -> None:
        """Update scan status and counts.

        Args:
            scan_id: Scan ID.
            status: New scan status.
            total_files: Total file count.
            present_files: Present file count.
            missing_files: Missing file count.
            error_files: Error file count.
            cache_hits: Cache hit count.
            error_summary: Optional error summary.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            UPDATE scan_runs
            SET end_time = ?,
                status = ?,
                total_files = ?,
                present_files = ?,
                missing_files = ?,
                error_files = ?,
                cache_hits = ?,
                error_summary = ?
            WHERE id = ?
            """,
            (
                now,
                status.value,
                total_files,
                present_files,
                missing_files,
                error_files,
                cache_hits,
                error_summary,
                scan_id,
            ),
        )
        self.conn.commit()

    def get_scan(self, scan_id: int) -> dict[str, Any] | None:
        """Get a scan by ID.

        Args:
            scan_id: Scan ID.

        Returns:
            Scan dict or None if not found.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM scan_runs WHERE id = ?
            """,
            (scan_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_latest_scan(self) -> dict[str, Any] | None:
        """Get the most recent scan.

        Returns:
            Scan dict or None if no scans exist.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM scan_runs
            ORDER BY start_time DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    # Root scan state operations

    def create_root_scan_state(
        self,
        scan_id: int,
        root_id: int,
        availability: RootAvailability,
    ) -> int:
        """Create a root scan state.

        Args:
            scan_id: Scan ID.
            root_id: Root ID.
            availability: Root availability state.

        Returns:
            Root scan state ID.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO root_scan_states (scan_id, root_id, availability, start_time)
            VALUES (?, ?, ?, ?)
            """,
            (scan_id, root_id, availability.value, now),
        )
        result = cursor.lastrowid
        assert result is not None
        return result

    def update_root_scan_state(
        self,
        root_scan_state_id: int,
        discovery_completed: bool,
        error_message: str | None = None,
        availability: str | None = None,
    ) -> None:
        """Update root scan state.

        Args:
            root_scan_state_id: Root scan state ID.
            discovery_completed: Whether discovery is complete.
            error_message: Optional error message.
            availability: Optional availability override.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            UPDATE root_scan_states
            SET discovery_completed = ?,
                error_message = ?,
                end_time = ?,
                availability = COALESCE(?, availability)
            WHERE id = ?
            """,
            (discovery_completed, error_message, now, availability, root_scan_state_id),
        )

    def get_root_scan_state(self, scan_id: int, root_id: int) -> dict[str, Any] | None:
        """Get root scan state for a scan and root.

        Args:
            scan_id: Scan ID.
            root_id: Root ID.

        Returns:
            Root scan state dict or None if not found.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM root_scan_states
            WHERE scan_id = ? AND root_id = ?
            """,
            (scan_id, root_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_scanned_root_ids(self, scan_id: int) -> set[int]:
        """Root IDs that were reachable in a given scan.

        Files under roots removed from the config or unavailable during
        the scan are excluded — their inventory is stale and unreadable.
        """
        cursor = self.conn.execute(
            """
            SELECT DISTINCT root_id FROM root_scan_states
            WHERE scan_id = ? AND availability = ?
            """,
            (scan_id, RootAvailability.AVAILABLE.value),
        )
        return {row[0] for row in cursor.fetchall()}

    # File operations

    def get_file_by_key(self, root_id: int, normalized_path_key: str) -> dict[str, Any] | None:
        """Get a file by root and normalized path key.

        Args:
            root_id: Root ID.
            normalized_path_key: Normalized path key.

        Returns:
            File dict or None if not found.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM files
            WHERE root_id = ? AND normalized_path_key = ?
            """,
            (root_id, normalized_path_key),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def create_file(
        self,
        root_id: int,
        original_path: str,
        normalized_path_key: str,
        file_name: str,
        extension: str,
        size: int,
        mtime_ns: int,
        scan_id: int,
        is_symlink: bool = False,
        link_target: str | None = None,
    ) -> int:
        """Create a new file record.

        Args:
            root_id: Root ID.
            original_path: Original file path.
            normalized_path_key: Normalized path key.
            file_name: File name.
            extension: File extension.
            size: File size in bytes.
            mtime_ns: Modification time in nanoseconds.
            scan_id: Scan ID.
            is_symlink: Whether the path is a symbolic link.
            link_target: Resolved link target path, if readable.

        Returns:
            File ID.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO files (
                root_id, original_path, normalized_path_key, file_name, extension,
                size, mtime_ns, presence, is_symlink, link_target,
                first_seen_scan_id, last_seen_scan_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                original_path,
                normalized_path_key,
                file_name,
                extension,
                size,
                mtime_ns,
                FilePresence.PRESENT.value,
                int(is_symlink),
                link_target,
                scan_id,
                scan_id,
                now,
                now,
            ),
        )
        result = cursor.lastrowid
        assert result is not None
        return result

    def update_file(
        self,
        file_id: int,
        size: int,
        mtime_ns: int,
        scan_id: int,
        presence: FilePresence = FilePresence.PRESENT,
        is_symlink: bool = False,
        link_target: str | None = None,
    ) -> None:
        """Update file metadata and presence.

        Args:
            file_id: File ID.
            size: File size in bytes.
            mtime_ns: Modification time in nanoseconds.
            scan_id: Scan ID.
            presence: File presence state.
            is_symlink: Whether the path is a symbolic link.
            link_target: Resolved link target path, if readable.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            UPDATE files
            SET size = ?,
                mtime_ns = ?,
                presence = ?,
                is_symlink = ?,
                link_target = ?,
                last_seen_scan_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (size, mtime_ns, presence.value, int(is_symlink), link_target, scan_id, now, file_id),
        )

    def mark_file_missing(self, file_id: int, scan_id: int) -> None:
        """Mark a file as missing.

        Args:
            file_id: File ID.
            scan_id: Scan ID.
        """
        self._set_file_presence(file_id, scan_id, FilePresence.MISSING)

    def mark_file_present(self, file_id: int, scan_id: int) -> None:
        """Mark a file as present again (e.g. after a restore).

        Args:
            file_id: File ID.
            scan_id: Scan ID.
        """
        self._set_file_presence(file_id, scan_id, FilePresence.PRESENT)

    def _set_file_presence(self, file_id: int, scan_id: int, presence: FilePresence) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            UPDATE files
            SET presence = ?,
                last_seen_scan_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (presence.value, scan_id, now, file_id),
        )

    def get_files_for_root(self, root_id: int) -> list[dict[str, Any]]:
        """Get all files for a root.

        Args:
            root_id: Root ID.

        Returns:
            List of file dicts.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM files WHERE root_id = ?
            """,
            (root_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    # Probe metadata operations

    def get_probe_metadata(self, file_id: int) -> dict[str, Any] | None:
        """Get probe metadata for a file.

        Args:
            file_id: File ID.

        Returns:
            Probe metadata dict or None if not found.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM probe_metadata WHERE file_id = ?
            """,
            (file_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def delete_probe_metadata(self, file_id: int) -> None:
        """Drop a file's cached probe so the next scan retries it."""
        self.conn.execute("DELETE FROM probe_metadata WHERE file_id = ?", (file_id,))

    def create_probe_metadata(
        self,
        file_id: int,
        status: ProbeStatus,
        algorithm_version: str,
        error_category: str | None = None,
        error_message: str | None = None,
        stderr: str = "",
        exit_code: int | None = None,
        container_format_name: str | None = None,
        container_format_long_name: str | None = None,
        container_duration: float | None = None,
        container_size: int | None = None,
        container_bitrate: int | None = None,
        container_start_time: float | None = None,
        has_video: bool = False,
        video_stream_count: int = 0,
        audio_stream_count: int = 0,
        subtitle_stream_count: int = 0,
        primary_video_codec: str | None = None,
        primary_video_width: int | None = None,
        primary_video_height: int | None = None,
        primary_video_fps: str | None = None,
        primary_audio_codec: str | None = None,
        primary_audio_sample_rate: int | None = None,
        primary_audio_channels: int | None = None,
    ) -> int:
        """Create probe metadata for a file.

        Args:
            file_id: File ID.
            status: Probe status.
            algorithm_version: Algorithm version.
            error_category: Optional error category.
            error_message: Optional error message.
            stderr: Captured stderr.
            exit_code: Exit code.
            container_format_name: Container format name.
            container_format_long_name: Container format long name.
            container_duration: Container duration.
            container_size: Container size.
            container_bitrate: Container bitrate.
            container_start_time: Container start time.
            has_video: Whether file has video.
            video_stream_count: Video stream count.
            audio_stream_count: Audio stream count.
            subtitle_stream_count: Subtitle stream count.
            primary_video_codec: Primary video codec.
            primary_video_width: Primary video width.
            primary_video_height: Primary video height.
            primary_video_fps: Primary video FPS.
            primary_audio_codec: Primary audio codec.
            primary_audio_sample_rate: Primary audio sample rate.
            primary_audio_channels: Primary audio channel count.

        Returns:
            Probe metadata ID.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO probe_metadata (
                file_id, status, algorithm_version, probed_at,
                error_category, error_message, stderr, exit_code,
                container_format_name, container_format_long_name, container_duration,
                container_size, container_bitrate, container_start_time,
                has_video, video_stream_count, audio_stream_count, subtitle_stream_count,
                primary_video_codec, primary_video_width, primary_video_height, primary_video_fps,
                primary_audio_codec, primary_audio_sample_rate, primary_audio_channels
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                status.value,
                algorithm_version,
                now,
                error_category,
                error_message,
                stderr,
                exit_code,
                container_format_name,
                container_format_long_name,
                container_duration,
                container_size,
                container_bitrate,
                container_start_time,
                has_video,
                video_stream_count,
                audio_stream_count,
                subtitle_stream_count,
                primary_video_codec,
                primary_video_width,
                primary_video_height,
                primary_video_fps,
                primary_audio_codec,
                primary_audio_sample_rate,
                primary_audio_channels,
            ),
        )
        result = cursor.lastrowid
        assert result is not None
        return result

    def update_probe_metadata(
        self,
        file_id: int,
        status: ProbeStatus,
        algorithm_version: str,
        error_category: str | None = None,
        error_message: str | None = None,
        stderr: str = "",
        exit_code: int | None = None,
        container_format_name: str | None = None,
        container_format_long_name: str | None = None,
        container_duration: float | None = None,
        container_size: int | None = None,
        container_bitrate: int | None = None,
        container_start_time: float | None = None,
        has_video: bool = False,
        video_stream_count: int = 0,
        audio_stream_count: int = 0,
        subtitle_stream_count: int = 0,
        primary_video_codec: str | None = None,
        primary_video_width: int | None = None,
        primary_video_height: int | None = None,
        primary_video_fps: str | None = None,
        primary_audio_codec: str | None = None,
        primary_audio_sample_rate: int | None = None,
        primary_audio_channels: int | None = None,
    ) -> None:
        """Update probe metadata for a file.

        Args:
            file_id: File ID.
            status: Probe status.
            algorithm_version: Algorithm version.
            error_category: Optional error category.
            error_message: Optional error message.
            stderr: Captured stderr.
            exit_code: Exit code.
            container_format_name: Container format name.
            container_format_long_name: Container format long name.
            container_duration: Container duration.
            container_size: Container size.
            container_bitrate: Container bitrate.
            container_start_time: Container start time.
            has_video: Whether file has video.
            video_stream_count: Video stream count.
            audio_stream_count: Audio stream count.
            subtitle_stream_count: Subtitle stream count.
            primary_video_codec: Primary video codec.
            primary_video_width: Primary video width.
            primary_video_height: Primary video height.
            primary_video_fps: Primary video FPS.
            primary_audio_codec: Primary audio codec.
            primary_audio_sample_rate: Primary audio sample rate.
            primary_audio_channels: Primary audio channel count.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            UPDATE probe_metadata
            SET status = ?,
                algorithm_version = ?,
                probed_at = ?,
                error_category = ?,
                error_message = ?,
                stderr = ?,
                exit_code = ?,
                container_format_name = ?,
                container_format_long_name = ?,
                container_duration = ?,
                container_size = ?,
                container_bitrate = ?,
                container_start_time = ?,
                has_video = ?,
                video_stream_count = ?,
                audio_stream_count = ?,
                subtitle_stream_count = ?,
                primary_video_codec = ?,
                primary_video_width = ?,
                primary_video_height = ?,
                primary_video_fps = ?,
                primary_audio_codec = ?,
                primary_audio_sample_rate = ?,
                primary_audio_channels = ?
            WHERE file_id = ?
            """,
            (
                status.value,
                algorithm_version,
                now,
                error_category,
                error_message,
                stderr,
                exit_code,
                container_format_name,
                container_format_long_name,
                container_duration,
                container_size,
                container_bitrate,
                container_start_time,
                has_video,
                video_stream_count,
                audio_stream_count,
                subtitle_stream_count,
                primary_video_codec,
                primary_video_width,
                primary_video_height,
                primary_video_fps,
                primary_audio_codec,
                primary_audio_sample_rate,
                primary_audio_channels,
                file_id,
            ),
        )

    def get_present_files(self) -> list[dict[str, Any]]:
        """Get all files currently marked present.

        Returns:
            List of file dicts.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM files WHERE presence = ?
            """,
            (FilePresence.PRESENT.value,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_inventory_files(self) -> list[dict[str, Any]]:
        """Get every file row together with the path of the root it came from.

        Returns:
            List of dicts with ``id``, ``root_id``, ``root_path``,
            ``original_path`` and ``presence``.
        """
        cursor = self.conn.execute(
            """
            SELECT f.id,
                   f.root_id,
                   r.original_path AS root_path,
                   f.original_path,
                   f.presence
            FROM files f
            JOIN configured_roots r ON r.id = f.root_id
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_file_ids_with_actions(self) -> set[int]:
        """Get the ids of files a quarantine or delete action refers to.

        Returns:
            Set of file ids that still have a recorded action.
        """
        cursor = self.conn.execute("SELECT DISTINCT file_id FROM file_actions")
        return {row[0] for row in cursor.fetchall()}

    def delete_files(self, file_ids: Iterable[int]) -> int:
        """Delete file rows; probes, hashes, signatures and pairs cascade.

        Args:
            file_ids: File ids to delete.

        Returns:
            Number of rows deleted.
        """
        ids = list(file_ids)
        deleted = 0
        for start in range(0, len(ids), _DELETE_CHUNK):
            chunk = ids[start : start + _DELETE_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            cursor = self.conn.execute(
                f"DELETE FROM files WHERE id IN ({placeholders})",
                chunk,
            )
            deleted += cursor.rowcount
        self.commit()
        return deleted

    # Exact hash operations

    def get_exact_hash(self, file_id: int) -> dict[str, Any] | None:
        """Get the cached SHA-256 hash record for a file.

        Args:
            file_id: File ID.

        Returns:
            Hash record dict or None if not found.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM exact_hashes WHERE file_id = ?
            """,
            (file_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_exact_hash(
        self,
        file_id: int,
        sha256: str,
        file_size: int,
        mtime_ns: int,
        algorithm_version: str,
    ) -> None:
        """Insert or replace the cached hash for a file.

        Args:
            file_id: File ID.
            sha256: Full-file SHA-256 digest (hex).
            file_size: File size at hashing time.
            mtime_ns: Modification time at hashing time.
            algorithm_version: Hash algorithm version.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO exact_hashes (
                file_id, sha256, file_size, mtime_ns, algorithm_version, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, sha256, file_size, mtime_ns, algorithm_version, now),
        )

    # Visual fingerprint operations

    def get_fingerprint(self, file_id: int) -> dict[str, Any] | None:
        """Get the cached visual fingerprint for a file.

        Args:
            file_id: File ID.

        Returns:
            Fingerprint record dict or None if not found.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM visual_fingerprints WHERE file_id = ?
            """,
            (file_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_fingerprint(
        self,
        file_id: int,
        algorithm_version: str,
        timestamps: str,
        frames: bytes,
        file_size: int,
        mtime_ns: int,
    ) -> None:
        """Insert or replace a cached visual fingerprint.

        Args:
            file_id: File ID.
            algorithm_version: Fingerprint algorithm version.
            timestamps: JSON array of sample timestamps.
            frames: Concatenated grayscale frame bytes.
            file_size: File size at fingerprinting time.
            mtime_ns: Modification time at fingerprinting time.
        """
        now = datetime.now(UTC).isoformat()
        frame_count = len(json.loads(timestamps))
        self.conn.execute(
            """
            INSERT OR REPLACE INTO visual_fingerprints (
                file_id, algorithm_version, frame_count, timestamps,
                frames, file_size, mtime_ns, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                algorithm_version,
                frame_count,
                timestamps,
                frames,
                file_size,
                mtime_ns,
                now,
            ),
        )

    # Audio fingerprint operations

    def get_audio_fingerprint(self, file_id: int) -> dict[str, Any] | None:
        """Get the cached audio fingerprint for a file."""
        cursor = self.conn.execute(
            """
            SELECT * FROM audio_fingerprints WHERE file_id = ?
            """,
            (file_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_audio_fingerprint(
        self,
        file_id: int,
        algorithm_version: str,
        window_count: int,
        bands: int,
        signature: bytes,
        file_size: int,
        mtime_ns: int,
    ) -> None:
        """Insert or replace a cached audio fingerprint.

        Args:
            file_id: File ID.
            algorithm_version: Audio fingerprint algorithm version.
            window_count: Number of usable spectral windows.
            bands: Bands per window.
            signature: window_count*bands uint8 band energies.
            file_size: File size at extraction time.
            mtime_ns: Modification time at extraction time.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO audio_fingerprints (
                file_id, algorithm_version, window_count, bands,
                signature, file_size, mtime_ns, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                algorithm_version,
                window_count,
                bands,
                signature,
                file_size,
                mtime_ns,
                now,
            ),
        )

    # Content signature operations

    def get_signature(self, file_id: int) -> dict[str, Any] | None:
        """Get the cached content signature for a file."""
        cursor = self.conn.execute(
            """
            SELECT * FROM content_signatures WHERE file_id = ?
            """,
            (file_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_signature(
        self,
        file_id: int,
        algorithm_version: str,
        timestamps: str,
        frames: bytes,
        hashes: bytes,
        file_size: int,
        mtime_ns: int,
    ) -> None:
        """Insert or replace a cached content signature."""
        now = datetime.now(UTC).isoformat()
        frame_count = len(json.loads(timestamps))
        self.conn.execute(
            """
            INSERT OR REPLACE INTO content_signatures (
                file_id, algorithm_version, frame_count, timestamps,
                frames, hashes, content_key, file_size, mtime_ns, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                algorithm_version,
                frame_count,
                timestamps,
                frames,
                hashes,
                content_key(algorithm_version, frame_count, timestamps, hashes),
                file_size,
                mtime_ns,
                now,
            ),
        )

    def get_head_bag(self, file_id: int) -> dict[str, Any] | None:
        """Get the cached head-window hashes for a file."""
        cursor = self.conn.execute("SELECT * FROM head_bags WHERE file_id = ?", (file_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_head_bag(
        self,
        file_id: int,
        algorithm_version: str,
        window_seconds: float,
        hashes: bytes,
        file_size: int,
        mtime_ns: int,
    ) -> None:
        """Insert or replace a cached head-window hash bag."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO head_bags (
                file_id, algorithm_version, window_seconds, frame_count,
                hashes, file_size, mtime_ns, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                algorithm_version,
                window_seconds,
                len(hashes) // 8,
                hashes,
                file_size,
                mtime_ns,
                now,
            ),
        )

    def get_offset_verdicts(
        self, base_version: str, preferred_version: str | None = None
    ) -> dict[tuple[int, int], dict[str, Any]]:
        """Negative offset-probe verdicts recorded by earlier scans.

        Pairs are keyed ``(smaller_id, larger_id)``. Callers must still check
        the file identity columns and the row's own algorithm version: a
        verdict describes the file *content* that was probed, not whatever
        now sits at that path.

        Rows from a version prefix are returned together because a verdict
        written before the measurements were stored can still be valid: its
        version string carries the bars it was judged with, and the caller
        decides whether they were at least as strict as this scan's. When one
        pair has rows at several versions, the newest row at
        ``preferred_version`` wins, then the newest row overall — an old row
        must not shadow the measurements a later scan stored.

        Args:
            base_version: Probe version prefix whose verdicts are wanted.
            preferred_version: Exact version to rank above older ones.

        Returns:
            Mapping of file-id pair to the stored row.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM offset_probe_verdicts
            WHERE algorithm_version LIKE ?
            ORDER BY file_a_id, file_b_id,
                     (algorithm_version = ?) ASC, probed_at ASC
            """,
            (f"{base_version}-%", preferred_version or ""),
        )
        return {(row["file_a_id"], row["file_b_id"]): dict(row) for row in cursor.fetchall()}

    def save_offset_verdict(
        self,
        file_a_id: int,
        file_b_id: int,
        algorithm_version: str,
        size_a: int,
        mtime_a: int,
        size_b: int,
        mtime_b: int,
        evidence: str | None = None,
    ) -> None:
        """Remember that a probe judged a pair and found no shift.

        Storing the identity columns lets ``get_offset_verdicts`` tell a
        verdict about these exact bytes from one about a file that has since
        been replaced. ``evidence`` carries the checkpoint measurements, so a
        later strictness change can re-judge the pair without decoding it
        again.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO offset_probe_verdicts (
                file_a_id, file_b_id, algorithm_version,
                size_a, mtime_a, size_b, mtime_b, evidence, probed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_a_id,
                file_b_id,
                algorithm_version,
                size_a,
                mtime_a,
                size_b,
                mtime_b,
                evidence,
                now,
            ),
        )

    def get_offset_probe_hit_pairs(self) -> list[tuple[int, int]]:
        """Pairs whose stored evidence records a confirmed timeline shift.

        A hit deliberately leaves no row in ``offset_probe_verdicts`` — only a
        miss does — so the pairs a scan must re-check are re-derived from the
        comparison rows the hit wrote. A reported pair is read from one scan's
        comparisons, so a shift that is not re-confirmed this scan drops out of
        the review list even though its files are still there.

        ``REJECTED`` rows never come from the probe path, and excluding them
        keeps this a scan over the pairs that reached review rather than over
        every comparison ever made (20 rows out of 824,015 on the reference
        database).

        Returns:
            File-id pairs in stored order, each once.
        """
        cursor = self.conn.execute(
            """
            SELECT DISTINCT file_a_id, file_b_id FROM pair_comparisons
            WHERE classification != 'REJECTED'
              AND evidence LIKE '%"mode": "offset-probe"%'
            """
        )
        return [(int(row[0]), int(row[1])) for row in cursor.fetchall()]

    def get_content_keys(self, file_ids: Iterable[int]) -> dict[int, str]:
        """Content keys for the given files.

        Args:
            file_ids: File IDs to look up.

        Returns:
            Mapping of file ID to content key. Files that have no signature
            yet are absent rather than given a placeholder.
        """
        keys: dict[int, str] = {}
        ids = list(file_ids)
        # Chunked to stay inside SQLite's default parameter limit
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ", ".join("?" * len(chunk))
            cursor = self.conn.execute(
                f"""
                SELECT file_id, content_key FROM content_signatures
                WHERE content_key IS NOT NULL AND file_id IN ({placeholders})
                """,
                chunk,
            )
            keys.update({row[0]: row[1] for row in cursor.fetchall()})
        return keys

    def get_content_keys_for_path_keys(self, path_keys: Iterable[str]) -> dict[str, str]:
        """Content keys for the given normalized path keys.

        The by-path twin of :meth:`get_content_keys`, for callers that hold
        paths rather than file ids — the Dismissed dialog lists paths. Files
        that have no signature are absent, as they are there.

        Args:
            path_keys: Normalized path keys to look up.

        Returns:
            Mapping of path key to content key.
        """
        keys: dict[str, str] = {}
        wanted = list(path_keys)
        # Chunked to stay inside SQLite's default parameter limit
        for start in range(0, len(wanted), 400):
            chunk = wanted[start : start + 400]
            placeholders = ", ".join("?" * len(chunk))
            cursor = self.conn.execute(
                f"""
                SELECT f.normalized_path_key, s.content_key
                FROM files f
                JOIN content_signatures s ON s.file_id = f.id
                WHERE s.content_key IS NOT NULL
                  AND f.normalized_path_key IN ({placeholders})
                """,
                chunk,
            )
            keys.update({row[0]: row[1] for row in cursor.fetchall()})
        return keys

    # Pair comparison operations

    def create_pair_comparison(
        self,
        scan_id: int,
        file_a_id: int,
        file_b_id: int,
        algorithm_version: str,
        classification: str,
        match_fraction: float,
        mean_frame_distance: float,
        frames_compared: int,
        duration_difference: float | None = None,
        evidence: str | None = None,
    ) -> int:
        """Record the outcome of a pairwise comparison.

        Args:
            scan_id: Scan ID.
            file_a_id: First file ID.
            file_b_id: Second file ID.
            algorithm_version: Comparison algorithm version.
            classification: Match class (VERY_LIKELY, POSSIBLE, REJECTED, ERROR).
            match_fraction: Fraction of aligned frames that matched.
            mean_frame_distance: Mean normalized pixel distance.
            frames_compared: Number of aligned frame pairs compared.
            duration_difference: Absolute duration difference in seconds.
            evidence: Optional JSON evidence blob.

        Returns:
            Comparison record ID.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            """
            INSERT OR REPLACE INTO pair_comparisons (
                scan_id, file_a_id, file_b_id, algorithm_version, classification,
                match_fraction, mean_frame_distance, frames_compared,
                duration_difference, evidence, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                file_a_id,
                file_b_id,
                algorithm_version,
                classification,
                match_fraction,
                mean_frame_distance,
                frames_compared,
                duration_difference,
                evidence,
                now,
            ),
        )
        result = cursor.lastrowid
        assert result is not None
        return result

    def get_comparison_counts(self, scan_id: int) -> dict[str, int]:
        """Pair-comparison counts by classification for a scan."""
        rows = self.conn.execute(
            """
            SELECT classification, COUNT(*) FROM pair_comparisons
            WHERE scan_id = ? GROUP BY classification
            """,
            (scan_id,),
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def get_pair_comparisons(self, scan_id: int) -> list[dict[str, Any]]:
        """Get all pair comparisons recorded for a scan.

        Args:
            scan_id: Scan ID.

        Returns:
            List of comparison dicts.
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM pair_comparisons WHERE scan_id = ? ORDER BY id
            """,
            (scan_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def delete_pair_comparisons(self, scan_id: int, classification: str | None = None) -> None:
        """Delete pair comparisons for a scan, optionally one class only.

        Args:
            scan_id: Scan ID.
            classification: When set, only rows of this class are deleted.
        """
        if classification is None:
            self.conn.execute(
                """
                DELETE FROM pair_comparisons WHERE scan_id = ?
                """,
                (scan_id,),
            )
            return
        self.conn.execute(
            """
            DELETE FROM pair_comparisons WHERE scan_id = ? AND classification = ?
            """,
            (scan_id, classification),
        )

    def delete_pair_comparisons_for_scan(self, scan_id: int) -> None:
        """Delete all pair comparisons for a scan.

        Args:
            scan_id: Scan ID.
        """
        self.delete_pair_comparisons(scan_id)

    def set_decision(self, scan_id: int, file_id: int, decision: str) -> None:
        """Record or update a human review decision for a file.

        Args:
            scan_id: Scan ID the decision applies to.
            file_id: File the decision is about.
            decision: One of 'keep', 'remove'.
        """
        if decision not in ("keep", "remove"):
            raise ValueError(f"Invalid decision: {decision}")
        self.conn.execute(
            """
            INSERT INTO review_decisions (file_id, scan_id, decision, decided_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file_id, scan_id)
            DO UPDATE SET decision = excluded.decision,
                          decided_at = excluded.decided_at
            """,
            (file_id, scan_id, decision, datetime.now(UTC).isoformat()),
        )

    def clear_decision(self, scan_id: int, file_id: int) -> None:
        """Remove a review decision, returning the file to undecided."""
        self.conn.execute(
            """
            DELETE FROM review_decisions WHERE file_id = ? AND scan_id = ?
            """,
            (file_id, scan_id),
        )

    def get_decisions(self, scan_id: int) -> dict[int, str]:
        """Get all review decisions for a scan.

        Returns:
            Mapping of file_id to decision ('keep'|'remove').
        """
        cursor = self.conn.execute(
            """
            SELECT file_id, decision FROM review_decisions WHERE scan_id = ?
            """,
            (scan_id,),
        )
        return {row["file_id"]: row["decision"] for row in cursor.fetchall()}

    def dismiss_pair(self, key_a: str, key_b: str) -> None:
        """Mark a pair of normalized path keys as a false positive.

        The pair is stored sorted so (a,b) and (b,a) are the same row.
        Dismissed pairs are permanent and scan-independent.
        """
        a, b = sorted((key_a, key_b))
        self.conn.execute(
            """
            INSERT OR IGNORE INTO dismissed_pairs (key_a, key_b, created_at)
            VALUES (?, ?, ?)
            """,
            (a, b, datetime.now(UTC).isoformat()),
        )

    def dismiss_pairs(self, pairs: Iterable[tuple[str, str]]) -> int:
        """Dismiss many pairs at once, permanently.

        Each pair is stored sorted so (a,b) and (b,a) are the same row.
        Duplicates within the input collapse to one row.

        Args:
            pairs: Iterable of ``(path_key_a, path_key_b)`` pairs.

        Returns:
            Number of distinct pairs written.
        """
        rows = sorted({tuple(sorted((a, b))) for a, b in pairs})
        if not rows:
            return 0
        now = datetime.now(UTC).isoformat()
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO dismissed_pairs (key_a, key_b, created_at)
            VALUES (?, ?, ?)
            """,
            [(a, b, now) for a, b in rows],
        )
        return len(rows)

    def undismiss_pair(self, key_a: str, key_b: str) -> None:
        """Undo a pair dismissal so it can be reported again."""
        a, b = sorted((key_a, key_b))
        self.conn.execute(
            "DELETE FROM dismissed_pairs WHERE key_a = ? AND key_b = ?",
            (a, b),
        )

    def get_dismissed_pairs(self) -> set[tuple[str, str]]:
        """All dismissed pair keys, each stored sorted (key_a < key_b)."""
        cursor = self.conn.execute("SELECT key_a, key_b FROM dismissed_pairs")
        return {(row["key_a"], row["key_b"]) for row in cursor.fetchall()}

    def get_dismissed_content_pairs(self) -> set[tuple[str, str]]:
        """Content pairs covered by the ignored pairs, stored sorted.

        Each dismissal names two paths; resolved through the files and their
        signatures it also names two *contents*, which is what lets a
        decision made in one copy of a library hold for every other copy of
        the same two videos. Derived on read rather than stored, so a
        dismissal made before content keys existed is covered too, and
        un-ignoring a pair withdraws its content pair with it.
        """
        cursor = self.conn.execute(
            """
            SELECT DISTINCT ca, cb FROM (
                SELECT
                    MIN(sa.content_key, sb.content_key) AS ca,
                    MAX(sa.content_key, sb.content_key) AS cb
                FROM dismissed_pairs d
                JOIN files fa ON fa.normalized_path_key = d.key_a
                JOIN files fb ON fb.normalized_path_key = d.key_b
                JOIN content_signatures sa
                    ON sa.file_id = fa.id AND sa.content_key IS NOT NULL
                JOIN content_signatures sb
                    ON sb.file_id = fb.id AND sb.content_key IS NOT NULL
            )
            """
        )
        return {(row[0], row[1]) for row in cursor.fetchall()}

    def content_pair_still_dismissed(self, content_a: str, content_b: str) -> bool:
        """Does any remaining dismissal still imply this pair of contents?

        Asked before withdrawing a content pair: several dismissed paths can
        resolve to the same two contents (the same videos in a mirror root),
        so one un-ignore withdraws the content pair only when it was the last
        dismissal standing behind it.

        Starts from the signature index rather than from the dismissal table,
        so it reads the handful of files carrying those two contents instead
        of rebuilding every derived pair. Both orientations are covered: the
        sides are matched on membership, not order.

        Args:
            content_a: One content key of the pair.
            content_b: The other content key.

        Returns:
            True if some dismissed pair's files still carry these contents.
        """
        cursor = self.conn.execute(
            """
            SELECT 1
            FROM content_signatures sa
            JOIN files fa ON fa.id = sa.file_id
            JOIN dismissed_pairs d ON d.key_a = fa.normalized_path_key
            JOIN files fb ON fb.normalized_path_key = d.key_b
            JOIN content_signatures sb ON sb.file_id = fb.id
            WHERE sa.content_key IN (?, ?)
              AND sb.content_key IN (?, ?)
              AND sa.content_key <> sb.content_key
            LIMIT 1
            """,
            (content_a, content_b, content_a, content_b),
        )
        return cursor.fetchone() is not None

    def get_dismissed_items(self) -> list[dict[str, Any]]:
        """All ignored pairs, newest first.

        Legacy ``dismissed_groups`` rows are not listed: groups no longer
        exist, so there is nothing to un-ignore.
        """
        items: list[dict[str, Any]] = [
            {
                "kind": "pair",
                "key_a": row["key_a"],
                "key_b": row["key_b"],
                "member_keys": sorted((row["key_a"], row["key_b"])),
                "created_at": row["created_at"],
            }
            for row in self.conn.execute("SELECT key_a, key_b, created_at FROM dismissed_pairs")
        ]
        items.sort(key=lambda i: i["created_at"], reverse=True)
        return items

    def get_display_paths_for_keys(self, keys: Iterable[str]) -> dict[str, str]:
        """Map normalised path keys to their last-seen original spelling.

        Dismissed pairs store only the key (lower-cased, separator-normalised)
        so an ignore survives a drive letter or case change. The ignored-pairs
        dialog still needs to show the folder that distinguishes two files of
        the same name; the inventory keeps the original path, and this is a
        read-only lookup for display. Missing rows simply fall back to the key
        at the call site.
        """
        unique = list(dict.fromkeys(keys))
        display: dict[str, str] = {}
        for start in range(0, len(unique), _DELETE_CHUNK):
            chunk = unique[start : start + _DELETE_CHUNK]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                "SELECT normalized_path_key, original_path FROM files "
                f"WHERE normalized_path_key IN ({placeholders}) ORDER BY id",
                chunk,
            )
            for key, original_path in rows:
                display[str(key)] = str(original_path)
        return display

    def record_action(
        self,
        file_id: int,
        scan_id: int,
        action: str,
        source_path: str,
        quarantine_path: str,
        status: str = "executed",
        error: str | None = None,
    ) -> int:
        """Record an executed file action (quarantine move).

        Returns:
            The new action row id.
        """
        cursor = self.conn.execute(
            """
            INSERT INTO file_actions
                (file_id, scan_id, action, source_path, quarantine_path,
                 status, executed_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                scan_id,
                action,
                source_path,
                quarantine_path,
                status,
                datetime.now(UTC).isoformat(),
                error,
            ),
        )
        return int(cursor.lastrowid or 0)

    def get_actions(self, scan_id: int | None = None) -> list[dict[str, Any]]:
        """Get file actions, optionally filtered to one scan."""
        if scan_id is None:
            cursor = self.conn.execute("SELECT * FROM file_actions ORDER BY id")
        else:
            cursor = self.conn.execute(
                """
                SELECT * FROM file_actions WHERE scan_id = ? ORDER BY id
                """,
                (scan_id,),
            )
        return [dict(row) for row in cursor.fetchall()]

    def get_action_by_id(self, action_id: int) -> dict[str, Any] | None:
        """Get a single file action row by id."""
        cursor = self.conn.execute(
            """
            SELECT * FROM file_actions WHERE id = ?
            """,
            (action_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def update_action_status(self, action_id: int, status: str, error: str | None = None) -> None:
        """Update an action's status; sets restored_at for 'restored'."""
        restored_at = datetime.now(UTC).isoformat() if status == "restored" else None
        self.conn.execute(
            """
            UPDATE file_actions
            SET status = ?, restored_at = COALESCE(?, restored_at), error = ?
            WHERE id = ?
            """,
            (status, restored_at, error, action_id),
        )

    def commit(self) -> None:
        """Commit the current transaction."""
        self.conn.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        self.conn.rollback()

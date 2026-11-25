"""Database schema and migrations for Akin Video Finder."""

import sqlite3
from pathlib import Path
from typing import Final

from akin_video_finder.core.content_identity import content_key

# Current schema version
SCHEMA_VERSION: Final = 16


class Migration:
    """Base class for database migrations."""

    version: int
    description: str

    def apply(self, conn: sqlite3.Connection) -> None:
        """Apply the migration to the database."""
        raise NotImplementedError


class Migration1(Migration):
    """Initial schema creation for Milestone 1."""

    version = 1
    description = "Initial schema: roots, scans, files, probe metadata"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the initial schema."""
        # Schema version table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )
        """)

        # Configured roots
        conn.execute("""
            CREATE TABLE IF NOT EXISTS configured_roots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT NOT NULL,
                normalized_path_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Scan runs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                status TEXT NOT NULL,
                application_version TEXT NOT NULL,
                config_fingerprint TEXT,
                total_files INTEGER DEFAULT 0,
                present_files INTEGER DEFAULT 0,
                missing_files INTEGER DEFAULT 0,
                error_files INTEGER DEFAULT 0,
                cache_hits INTEGER DEFAULT 0,
                error_summary TEXT
            )
        """)

        # Per-root scan state
        conn.execute("""
            CREATE TABLE IF NOT EXISTS root_scan_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                root_id INTEGER NOT NULL,
                availability TEXT NOT NULL,
                discovery_completed BOOLEAN NOT NULL DEFAULT 0,
                error_message TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                FOREIGN KEY (scan_id) REFERENCES scan_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (root_id) REFERENCES configured_roots(id) ON DELETE CASCADE,
                UNIQUE(scan_id, root_id)
            )
        """)

        # Files
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_id INTEGER NOT NULL,
                original_path TEXT NOT NULL,
                normalized_path_key TEXT NOT NULL,
                file_name TEXT NOT NULL,
                extension TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                presence TEXT NOT NULL DEFAULT 'present',
                first_seen_scan_id INTEGER NOT NULL,
                last_seen_scan_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (root_id) REFERENCES configured_roots(id) ON DELETE CASCADE,
                FOREIGN KEY (first_seen_scan_id) REFERENCES scan_runs(id),
                FOREIGN KEY (last_seen_scan_id) REFERENCES scan_runs(id),
                UNIQUE(root_id, normalized_path_key)
            )
        """)

        # Probe metadata
        conn.execute("""
            CREATE TABLE IF NOT EXISTS probe_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                probed_at TEXT NOT NULL,
                error_category TEXT,
                error_message TEXT,
                stderr TEXT,
                exit_code INTEGER,
                container_format_name TEXT,
                container_format_long_name TEXT,
                container_duration REAL,
                container_size INTEGER,
                container_bitrate INTEGER,
                container_start_time REAL,
                has_video BOOLEAN NOT NULL DEFAULT 0,
                video_stream_count INTEGER NOT NULL DEFAULT 0,
                audio_stream_count INTEGER NOT NULL DEFAULT 0,
                subtitle_stream_count INTEGER NOT NULL DEFAULT 0,
                primary_video_codec TEXT,
                primary_video_width INTEGER,
                primary_video_height INTEGER,
                primary_video_fps TEXT,
                primary_audio_codec TEXT,
                primary_audio_sample_rate INTEGER,
                primary_audio_channels INTEGER,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)

        # Create indexes for common queries
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_root_id
            ON files(root_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_presence
            ON files(presence)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_last_seen_scan
            ON files(last_seen_scan_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_probe_metadata_status
            ON probe_metadata(status)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_root_scan_states_scan_id
            ON root_scan_states(scan_id)
        """)


class Migration2(Migration):
    """Exact-match hashing and duplicate group tables."""

    version = 2
    description = "Exact hashes and duplicate groups"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create exact-hash cache and duplicate group tables."""
        # Cached full-file SHA-256 digests, invalidated by file identity
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exact_hashes (
                file_id INTEGER PRIMARY KEY,
                sha256 TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                algorithm_version TEXT NOT NULL,
                computed_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)

        # Duplicate groups (exact now, perceptual later)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS duplicate_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_type TEXT NOT NULL,
                scan_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (scan_id) REFERENCES scan_runs(id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS duplicate_group_members (
                group_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                PRIMARY KEY (group_id, file_id),
                FOREIGN KEY (group_id) REFERENCES duplicate_groups(id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_size
            ON files(size)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exact_hashes_sha256
            ON exact_hashes(sha256)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_duplicate_groups_type
            ON duplicate_groups(group_type)
        """)


class Migration3(Migration):
    """Perceptual fingerprints and pair comparison evidence."""

    version = 3
    description = "Visual fingerprints and pair comparisons"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create fingerprint cache and pair comparison tables."""
        # Cached normalized grayscale frame fingerprints
        conn.execute("""
            CREATE TABLE IF NOT EXISTS visual_fingerprints (
                file_id INTEGER PRIMARY KEY,
                algorithm_version TEXT NOT NULL,
                frame_count INTEGER NOT NULL,
                timestamps TEXT NOT NULL,
                frames BLOB NOT NULL,
                file_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                computed_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)

        # Per-pair comparison evidence (all evaluated pairs retained)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pair_comparisons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                file_a_id INTEGER NOT NULL,
                file_b_id INTEGER NOT NULL,
                algorithm_version TEXT NOT NULL,
                classification TEXT NOT NULL,
                match_fraction REAL NOT NULL,
                mean_frame_distance REAL NOT NULL,
                frames_compared INTEGER NOT NULL,
                duration_difference REAL,
                evidence TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (scan_id) REFERENCES scan_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (file_a_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (file_b_id) REFERENCES files(id) ON DELETE CASCADE,
                UNIQUE(scan_id, file_a_id, file_b_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pair_comparisons_scan_id
            ON pair_comparisons(scan_id)
        """)


class Migration4(Migration):
    """Group analysis: keeper recommendations and consistency warnings."""

    version = 4
    description = "Add analysis JSON column to duplicate_groups"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Add the analysis column."""
        conn.execute("""
            ALTER TABLE duplicate_groups ADD COLUMN analysis TEXT
        """)


class Migration5(Migration):
    """Sparse content signatures for scalable candidate pruning."""

    version = 5
    description = "Content signatures table for candidate pruning"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the signature cache table."""
        # Sparse per-frame perceptual hashes used to block candidate pairs
        # before dense fingerprint comparison. Cached by file identity.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content_signatures (
                file_id INTEGER PRIMARY KEY,
                algorithm_version TEXT NOT NULL,
                frame_count INTEGER NOT NULL,
                timestamps TEXT NOT NULL,
                frames BLOB NOT NULL,
                hashes BLOB NOT NULL,
                file_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                computed_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)


class Migration6(Migration):
    """Symbolic link detection: aliases are excluded from duplicate matching."""

    version = 6
    description = "Add is_symlink and link_target columns to files"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Add the link columns."""
        conn.execute("""
            ALTER TABLE files ADD COLUMN is_symlink INTEGER NOT NULL DEFAULT 0
        """)
        conn.execute("""
            ALTER TABLE files ADD COLUMN link_target TEXT
        """)


class Migration7(Migration):
    """Human review decisions: keep/remove per file per scan."""

    version = 7
    description = "Review decisions table for the GUI reviewer"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the review decisions table."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_decisions (
                file_id INTEGER NOT NULL,
                scan_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                PRIMARY KEY (file_id, scan_id),
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (scan_id) REFERENCES scan_runs(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_decisions_scan_id
            ON review_decisions(scan_id)
        """)


class Migration8(Migration):
    """Executed file actions: quarantine moves with restore manifest."""

    version = 8
    description = "File actions table recording quarantine moves"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the file actions manifest table."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                scan_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                source_path TEXT NOT NULL,
                quarantine_path TEXT NOT NULL,
                status TEXT NOT NULL,
                executed_at TEXT NOT NULL,
                restored_at TEXT,
                error TEXT,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (scan_id) REFERENCES scan_runs(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_file_actions_scan_id
            ON file_actions(scan_id)
        """)


class Migration9(Migration):
    """Dismissed pairs: reviewer-marked false positives, hidden forever."""

    version = 9
    description = "Dismissed pair table for false-positive suppression"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the dismissed pairs table."""
        # Keyed by normalized path keys (stable across rescans and file
        # id churn), stored sorted so (a,b) and (b,a) dedupe naturally.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dismissed_pairs (
                key_a TEXT NOT NULL,
                key_b TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (key_a, key_b)
            )
        """)


class Migration10(Migration):
    """Dismissed groups: reviewer-marked false-positive groups."""

    version = 10
    description = "Dismissed group table for false-positive suppression"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the dismissed groups table."""
        # group_key is the member normalized path keys, sorted and joined
        # with '|' (illegal in Windows paths, so unambiguous). Whole-set
        # identity: a group that gains or loses a member is a new group.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dismissed_groups (
                group_key TEXT PRIMARY KEY,
                member_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


class Migration11(Migration):
    """Audio fingerprints for the VERY_LIKELY divergence gate."""

    version = 11
    description = "Audio fingerprint table for audio divergence checks"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the audio fingerprints table."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audio_fingerprints (
                file_id INTEGER PRIMARY KEY,
                algorithm_version TEXT NOT NULL,
                window_count INTEGER NOT NULL,
                bands INTEGER NOT NULL,
                signature BLOB NOT NULL,
                file_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                computed_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)


class Migration12(Migration):
    """Content keys, so an ignore covers every copy of the two videos.

    Dismissals are keyed by path, which made them local to one copy of a
    library: the same pair under a mirror root came back as a new pair. The
    content key of a file is derived from its signature, so the same pair
    can be recognised wherever its files live.
    """

    version = 12
    description = "Add content_key to content_signatures for content-aware dismissals"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Add and index the column, then fill it for existing signatures."""
        conn.execute("ALTER TABLE content_signatures ADD COLUMN content_key TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_signatures_content_key"
            " ON content_signatures(content_key)"
        )
        # Resolving a dismissal's paths back to files needs this: files are
        # unique per root, so the existing index does not lead with the path
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_normalized_path_key ON files(normalized_path_key)"
        )
        # Every signature already in the database gets its key now; the
        # alternative is a lazy computation on the review path, once per
        # reload, for as long as the database lives
        rows = conn.execute(
            "SELECT file_id, algorithm_version, frame_count, timestamps, hashes"
            " FROM content_signatures"
        ).fetchall()
        conn.executemany(
            "UPDATE content_signatures SET content_key = ? WHERE file_id = ?",
            [(content_key(row[1], row[2], row[3], row[4]), row[0]) for row in rows],
        )


class Migration13(Migration):
    """Dense head-window hashes, so a shifted copy can be proposed at all.

    Every candidate path before this one keys on frames sampled at fractions
    of each file's own duration. A prepended advert moves those samples onto
    different moments, and the two copies of one film then share no
    informative key: measured on a real 20s-shifted pair, zero shared
    average-hash keys, and the four coarse keys they do share belong to
    buckets holding every file in the library, which the bucket guard drops.
    The head bag samples the same *seconds* instead of the same fractions —
    1 fps over the first minutes, indexed without positions — so a copy that
    starts 20s late still collides on about 70 of its frames.
    """

    version = 13
    description = "Add head_bags for shift-tolerant timeline-offset candidates"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the head-window hash cache."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS head_bags (
                file_id INTEGER PRIMARY KEY,
                algorithm_version TEXT NOT NULL,
                window_seconds REAL NOT NULL,
                frame_count INTEGER NOT NULL,
                hashes BLOB NOT NULL,
                file_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                computed_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)


class Migration14(Migration):
    """Executed offset probes, so a re-scan advances instead of repeating.

    The timeline-offset probe is the most expensive phase: 180s of video is
    decoded from both files of every candidate pair, roughly ten seconds a
    pair. Until now a probe that found no shift left no trace, so the next
    scan rebuilt the same ranked queue and spent its budget on the same
    pairs — a library with more candidates than one scan's budget could
    never be drained, and a strictness change looked like it "started over".

    A verdict is stored only when the probe could judge the pair (both head
    windows decoded, enough checkpoints read); a decode failure stores
    nothing, so it is retried rather than remembered as "no shift". The file
    size and mtime are part of the row: a replaced file invalidates its
    verdict the same way it invalidates a cached fingerprint, and the
    algorithm version invalidates all of them when the probe changes.

    Positive hits are not stored here — a hit becomes a pair comparison row,
    which is what the review reads.
    """

    version = 14
    description = "Remember offset-probe verdicts so a re-scan advances"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the negative-verdict cache."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offset_probe_verdicts (
                file_a_id INTEGER NOT NULL,
                file_b_id INTEGER NOT NULL,
                algorithm_version TEXT NOT NULL,
                size_a INTEGER NOT NULL,
                mtime_a INTEGER NOT NULL,
                size_b INTEGER NOT NULL,
                mtime_b INTEGER NOT NULL,
                probed_at TEXT NOT NULL,
                PRIMARY KEY (file_a_id, file_b_id, algorithm_version),
                FOREIGN KEY (file_a_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (file_b_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)


class Migration15(Migration):
    """Index the file columns of the pair table, so a cascade stays linear.

    Every table that references a file cascades when its row is deleted, and
    the pair table is the one that matters: 826,829 comparisons on the
    reference library and no index on either file column, so each cascaded
    delete scanned the whole table. Removing the 1,690 rows a cleanup found
    ran for minutes; with both columns indexed it is a lookup per pair.

    The columns are indexed separately rather than together: a cascade
    arrives with one file id and needs every pair it appears in on either
    side, which a composite index cannot serve.
    """

    version = 15
    description = "Index the pair table's file columns for cascading deletes"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Create the two lookup indexes."""
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pair_comparisons_file_a ON pair_comparisons(file_a_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pair_comparisons_file_b ON pair_comparisons(file_b_id)"
        )


class Migration16(Migration):
    """Keep the measurements behind an offset-probe verdict.

    A verdict used to answer only "no shift", and its version string carried
    the strictness bars it was judged with — so every adjustment of the
    distance or divergence bar retired the whole cache and re-decoded the
    same queue. The verdict itself already measured per-checkpoint
    correlation, pixel distance and divergent-block share; storing those
    lets a later scan re-apply the current bars to the same numbers. Only the
    per-block cutoff still retires the cache, because it changes the measured
    divergence rather than the bar it is compared with.

    Existing rows have no measurements. The verdict version changed with this
    migration, so their next scan re-tests them once; rows written before the
    bars were versioned are treated as stale for the same reason. From then
    on, changing strictness costs no probes.
    """

    version = 16
    description = "Store offset-probe measurements so strictness changes re-judge without decoding"

    def apply(self, conn: sqlite3.Connection) -> None:
        """Add the evidence column to the verdict cache."""
        conn.execute("ALTER TABLE offset_probe_verdicts ADD COLUMN evidence TEXT")


# Registry of all migrations
MIGRATIONS: dict[int, type[Migration]] = {
    1: Migration1,
    2: Migration2,
    3: Migration3,
    4: Migration4,
    5: Migration5,
    6: Migration6,
    7: Migration7,
    8: Migration8,
    9: Migration9,
    10: Migration10,
    11: Migration11,
    12: Migration12,
    13: Migration13,
    14: Migration14,
    15: Migration15,
    16: Migration16,
}


def get_current_schema_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version from the database.

    Args:
        conn: Database connection.

    Returns:
        Current schema version, or 0 if no version is set.
    """
    try:
        cursor = conn.execute("SELECT MAX(version) FROM schema_version")
        result = cursor.fetchone()
        return result[0] if result[0] is not None else 0
    except sqlite3.OperationalError:
        # Table doesn't exist yet
        return 0


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations to bring the database up to current version.

    Args:
        conn: Database connection.
    """
    current_version = get_current_schema_version(conn)

    if current_version > SCHEMA_VERSION:
        raise ValueError(
            f"Database schema version {current_version} is newer than "
            f"application schema version {SCHEMA_VERSION}"
        )

    for version in range(current_version + 1, SCHEMA_VERSION + 1):
        migration_class = MIGRATIONS.get(version)
        if migration_class is None:
            raise ValueError(f"No migration found for version {version}")

        migration = migration_class()
        _apply_migration(conn, version, migration)


def _apply_migration(conn: sqlite3.Connection, version: int, migration: object) -> None:
    """Apply one migration and its version row as one unit.

    A savepoint keeps the DDL and the version INSERT together. Without it a
    crash after a non-idempotent ALTER and before the version row leaves a
    column that the next start tries to add again.
    """
    from datetime import UTC, datetime

    conn.execute("SAVEPOINT migration")
    try:
        migration.apply(conn)  # type: ignore[attr-defined]
        conn.execute(
            """
            INSERT INTO schema_version (version, applied_at, description)
            VALUES (?, ?, ?)
            """,
            (version, datetime.now(UTC).isoformat(), migration.description),  # type: ignore[attr-defined]
        )
        conn.execute("RELEASE migration")
    except Exception:
        conn.execute("ROLLBACK TO migration")
        conn.execute("RELEASE migration")
        raise
    conn.commit()


def initialize_database(db_path: Path) -> sqlite3.Connection:
    """Initialize a database connection and apply migrations.

    Args:
        db_path: Path to the database file.

    Returns:
        Database connection with schema applied.
    """
    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON")

    # Set journal mode to WAL for better concurrency
    conn.execute("PRAGMA journal_mode = WAL")

    # Set synchronous mode to NORMAL for safety with reasonable performance
    conn.execute("PRAGMA synchronous = NORMAL")

    # Set busy timeout to 5 seconds
    conn.execute("PRAGMA busy_timeout = 5000")

    # Apply migrations
    apply_migrations(conn)

    return conn

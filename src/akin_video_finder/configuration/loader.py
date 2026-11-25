"""Configuration loader for Akin Video Finder."""

import logging
import re
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from akin_video_finder.configuration.models import (
    ActionsConfig,
    Config,
    ConfigValidationError,
    DatabaseConfig,
    FFprobeConfig,
    KeeperConfig,
    LoggingConfig,
    MatchingConfig,
    ReportingConfig,
    RootConfig,
)

logger = logging.getLogger(__name__)

# Allowed [matching] keys by expected type (module constants)
_MATCHING_BOOL_FIELDS = {"enabled", "crop_detect", "head_bag_enabled"}
_MATCHING_INT_FIELDS = {
    "frame_timeout_seconds",
    "frame_width",
    "frame_height",
    "min_frames",
    "max_frames",
    "possible_min_matched_frames",
    "signature_frames",
    "signature_min_shared_frames",
    "signature_max_bucket_files",
    "signature_min_band_hits",
    "signature_max_hamming",
    "signature_workers",
    "head_bag_min_shared",
    "strictness",
}
_MATCHING_FLOAT_FIELDS = {
    "target_frame_interval_seconds",
    "duration_tolerance_ratio",
    "duration_tolerance_seconds",
    "min_duration_ratio",
    "frame_match_max_distance",
    "very_likely_threshold",
    "very_likely_mean_distance",
    "possible_threshold",
    "trim_short_coverage_min",
    "trim_long_coverage_min",
    "head_bag_seconds",
    "head_bag_fps",
}
_MATCHING_STR_FIELDS = {"ffmpeg_path"}

# Options an older build wrote that no longer exist: the coarse candidate tier
# and the per-scan probe cap it needed (see
# offset-probe.md#the-coarse-tier-was-removed and
# offset-probe.md#there-is-no-per-scan-probe-cap). Accepting them and dropping
# them keeps a saved config loading instead of failing on a setting that has
# nothing left to configure. They are not dataclass fields, so the next save
# leaves them out.
_RETIRED_MATCHING_OPTIONS = frozenset(
    {
        "signature_coarse_min_hits",
        "offset_max_coarse_probes_per_scan",
        "offset_max_probes_per_scan",
    }
)


def load_config(config_path: Path) -> Config:
    """Load and validate configuration from a TOML file.

    Args:
        config_path: Path to the configuration file.

    Returns:
        Validated Config object.

    Raises:
        ConfigValidationError: If the configuration is invalid.
        FileNotFoundError: If the configuration file does not exist.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigValidationError(f"Invalid TOML syntax: {e}", str(config_path)) from e
    except OSError as e:
        raise ConfigValidationError(f"Failed to read file: {e}", str(config_path)) from e

    try:
        config = _parse_config(data)
    except (ValueError, KeyError, TypeError) as e:
        raise ConfigValidationError(str(e), str(config_path)) from e
    # Frozen builds ship ffmpeg/ffprobe under _MEIPASS/bin — use them
    # while config still names the default PATH lookup. Config
    # dataclasses are frozen — rebuild rather than assign
    return replace(
        config,
        matching=replace(
            config.matching,
            ffmpeg_path=_resolve_tool(config.matching.ffmpeg_path),
        ),
        ffprobe=replace(
            config.ffprobe,
            executable_path=_resolve_tool(config.ffprobe.executable_path),
        ),
    )


def _resolve_tool(path: str) -> str:
    """Prefer a bundled binary in the frozen build over a PATH name."""
    if path not in ("ffmpeg", "ffprobe"):
        return path
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "bin" / f"{path}.exe"
        if bundled.exists():
            return str(bundled)
    return path


def _parse_config(data: dict[str, Any]) -> Config:
    """Parse configuration dictionary into Config object.

    Args:
        data: Configuration dictionary from TOML.

    Returns:
        Config object.

    Raises:
        ValueError: If required fields are missing or invalid.
    """
    known_top_level = {
        "roots",
        "video_extensions",
        "include_patterns",
        "exclude_patterns",
        "follow_symlinks",
        "include_hidden",
        "logging",
        "ffprobe",
        "database",
        "reporting",
        "matching",
        "keeper",
        "actions",
    }
    for key in data:
        if key not in known_top_level:
            logger.warning("Unknown configuration key: %s", key)

    roots_data = data.get("roots", [])
    if not isinstance(roots_data, list):
        raise ValueError("'roots' must be a list")

    roots = []
    for i, root_data in enumerate(roots_data):
        if not isinstance(root_data, dict):
            raise ValueError(f"Root {i} must be a dictionary")
        for key in root_data:
            if key not in {"path", "recursive"}:
                logger.warning("Unknown root key: %s", key)
        path = root_data.get("path")
        if not path or not isinstance(path, str):
            raise ValueError(f"Root {i} must have a 'path' string")
        recursive = root_data.get("recursive", True)
        if not isinstance(recursive, bool):
            raise ValueError(f"Root {i} 'recursive' must be a boolean")
        roots.append(RootConfig(path=path, recursive=recursive))

    video_extensions = data.get("video_extensions")
    if video_extensions is not None:
        if not isinstance(video_extensions, list):
            raise ValueError("'video_extensions' must be a list of strings")
        if not all(isinstance(ext, str) for ext in video_extensions):
            raise ValueError("'video_extensions' must be a list of strings")
        # Ensure extensions start with dot
        video_extensions = [ext if ext.startswith(".") else f".{ext}" for ext in video_extensions]

    include_patterns = data.get("include_patterns")
    if include_patterns is not None:
        if not isinstance(include_patterns, list):
            raise ValueError("'include_patterns' must be a list of strings")
        if not all(isinstance(p, str) for p in include_patterns):
            raise ValueError("'include_patterns' must be a list of strings")

    exclude_patterns = data.get("exclude_patterns")
    if exclude_patterns is not None:
        if not isinstance(exclude_patterns, list):
            raise ValueError("'exclude_patterns' must be a list of strings")
        if not all(isinstance(p, str) for p in exclude_patterns):
            raise ValueError("'exclude_patterns' must be a list of strings")

    follow_symlinks = data.get("follow_symlinks", False)
    if not isinstance(follow_symlinks, bool):
        raise ValueError("'follow_symlinks' must be a boolean")

    include_hidden = data.get("include_hidden", False)
    if not isinstance(include_hidden, bool):
        raise ValueError("'include_hidden' must be a boolean")

    # Parse logging config
    logging_data = data.get("logging", {})
    if not isinstance(logging_data, dict):
        raise ValueError("'logging' must be a dictionary")
    for key in logging_data:
        if key not in {"level", "file"}:
            logger.warning("Unknown logging key: %s", key)
    logging_level = logging_data.get("level", "INFO")
    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if logging_level not in valid_levels:
        raise ValueError(f"'logging.level' must be one of {valid_levels}")
    logging_file = logging_data.get("file")
    if logging_file is not None and not isinstance(logging_file, str):
        raise ValueError("'logging.file' must be a string")
    logging_config = LoggingConfig(level=logging_level, file_path=logging_file)

    # Parse FFprobe config
    ffprobe_data = data.get("ffprobe", {})
    if not isinstance(ffprobe_data, dict):
        raise ValueError("'ffprobe' must be a dictionary")
    for key in ffprobe_data:
        if key not in {"executable_path", "timeout_seconds", "worker_count", "max_stderr_bytes"}:
            logger.warning("Unknown ffprobe key: %s", key)
    ffprobe_path = ffprobe_data.get("executable_path", "ffprobe")
    if not isinstance(ffprobe_path, str):
        raise ValueError("'ffprobe.executable_path' must be a string")
    ffprobe_timeout = ffprobe_data.get("timeout_seconds", 60)
    if not isinstance(ffprobe_timeout, int) or ffprobe_timeout < 1:
        raise ValueError("'ffprobe.timeout_seconds' must be a positive integer")
    ffprobe_workers = ffprobe_data.get("worker_count", 2)
    if not isinstance(ffprobe_workers, int) or ffprobe_workers < 1:
        raise ValueError("'ffprobe.worker_count' must be a positive integer")
    ffprobe_stderr = ffprobe_data.get("max_stderr_bytes", 8192)
    if not isinstance(ffprobe_stderr, int) or ffprobe_stderr < 0:
        raise ValueError("'ffprobe.max_stderr_bytes' must be a non-negative integer")
    ffprobe_config = FFprobeConfig(
        executable_path=ffprobe_path,
        timeout_seconds=ffprobe_timeout,
        worker_count=ffprobe_workers,
        max_stderr_bytes=ffprobe_stderr,
    )

    # Parse database config
    database_data = data.get("database", {})
    if not isinstance(database_data, dict):
        raise ValueError("'database' must be a dictionary")
    for key in database_data:
        if key != "path":
            logger.warning("Unknown database key: %s", key)
    database_path = database_data.get("path", "akin_video_finder.db")
    if not isinstance(database_path, str):
        raise ValueError("'database.path' must be a string")
    database_config = DatabaseConfig(path=database_path)

    # Parse reporting config
    reporting_data = data.get("reporting", {})
    if not isinstance(reporting_data, dict):
        raise ValueError("'reporting' must be a dictionary")
    for key in reporting_data:
        if key not in {"output_directory", "review_min_duration_ratio"}:
            logger.warning("Unknown reporting key: %s", key)
    reporting_dir = reporting_data.get("output_directory", "reports")
    if not isinstance(reporting_dir, str):
        raise ValueError("'reporting.output_directory' must be a string")
    ratio = reporting_data.get("review_min_duration_ratio")
    if ratio is not None and (isinstance(ratio, bool) or not isinstance(ratio, (int, float))):
        raise ValueError("'reporting.review_min_duration_ratio' must be a number")
    reporting_config = ReportingConfig(
        output_directory=reporting_dir,
        review_min_duration_ratio=float(ratio) if ratio is not None else 0.88,
    )

    # Parse matching config
    matching_data = data.get("matching", {})
    if not isinstance(matching_data, dict):
        raise ValueError("'matching' must be a dictionary")

    matching_kwargs: dict[str, Any] = {}
    for key, value in matching_data.items():
        if key in _RETIRED_MATCHING_OPTIONS:
            continue  # written by a build that still had the coarse tier
        if key not in MatchingConfig.__dataclass_fields__:
            raise ValueError(f"Unknown 'matching' option: {key}")
        if key in _MATCHING_BOOL_FIELDS and not isinstance(value, bool):
            raise ValueError(f"'matching.{key}' must be a boolean")
        if key in _MATCHING_INT_FIELDS and not isinstance(value, int):
            raise ValueError(f"'matching.{key}' must be an integer")
        if key in _MATCHING_FLOAT_FIELDS and not isinstance(value, (int, float)):
            raise ValueError(f"'matching.{key}' must be a number")
        if key in _MATCHING_STR_FIELDS and not isinstance(value, str):
            raise ValueError(f"'matching.{key}' must be a string")
        matching_kwargs[key] = value
    if "min_duration_ratio" not in matching_kwargs and ratio is not None:
        # The threshold used to live in [reporting]. It moved into [matching]
        # because it now decides which pairs a scan compares, so a change to
        # it has to invalidate the config fingerprint — a stored scan made
        # under a different gate really is a different result set.
        matching_kwargs["min_duration_ratio"] = float(ratio)
    matching_config = MatchingConfig(**matching_kwargs)
    # One number, two readers: the exporter and older GUI code paths still
    # ask [reporting], so the mirror is kept rather than left behind at the
    # default and silently disagreeing with the gate the scan applied.
    reporting_config = replace(
        reporting_config, review_min_duration_ratio=matching_config.min_duration_ratio
    )

    # Parse keeper preferences
    keeper_data = data.get("keeper", {})
    if not isinstance(keeper_data, dict):
        raise ValueError("'keeper' must be a dictionary")
    keeper_kwargs: dict[str, Any] = {}
    for key, value in keeper_data.items():
        if key in ("preferred_extensions", "preferred_path_keywords"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"'keeper.{key}' must be a list of strings")
        elif key == "length_penalty_per_percent":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"'keeper.{key}' must be a number")
            if value < 0:
                raise ValueError(f"'keeper.{key}' cannot be negative")
            value = float(value)
        else:
            raise ValueError(f"Unknown 'keeper' option: {key}")
        keeper_kwargs[key] = value
    if "preferred_extensions" in keeper_kwargs:
        keeper_kwargs["preferred_extensions"] = [
            ext if ext.startswith(".") else f".{ext}"
            for ext in keeper_kwargs["preferred_extensions"]
        ]
    keeper_config = KeeperConfig(**keeper_kwargs)

    actions_data = data.get("actions", {})
    if not isinstance(actions_data, dict):
        raise ValueError("'actions' must be a dictionary")
    for key in actions_data:
        if key != "protected_paths":
            logger.warning("Unknown actions key: %s", key)
    protected_paths = actions_data.get("protected_paths", [])
    if not isinstance(protected_paths, list) or not all(
        isinstance(path, str) for path in protected_paths
    ):
        raise ValueError("'actions.protected_paths' must be a list of strings")
    actions_config = ActionsConfig(protected_paths=list(protected_paths))

    config_kwargs: dict[str, Any] = {
        "roots": roots,
        "include_patterns": include_patterns or [],
        "exclude_patterns": exclude_patterns or [],
        "follow_symlinks": follow_symlinks,
        "include_hidden": include_hidden,
        "logging": logging_config,
        "ffprobe": ffprobe_config,
        "database": database_config,
        "reporting": reporting_config,
        "matching": matching_config,
        "keeper": keeper_config,
        "actions": actions_config,
    }
    if video_extensions is not None:
        config_kwargs["video_extensions"] = video_extensions
    return Config(**config_kwargs)


def _set_table_keys(text: str, table: str, key_values: dict[str, str | None]) -> str:
    """Set scalar keys inside a TOML table, preserving everything else.

    A value of None removes the key. Missing keys are inserted directly
    under the table header; a missing table is appended at the end.
    """
    lines = text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.strip() == f"[{table}]"), None)
    if header_idx is None:
        block = [f"[{table}]"] + [f"{k} = {v}" for k, v in key_values.items() if v is not None]
        return text.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"

    end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if lines[j].lstrip().startswith("["):
            end = j
            break

    seen: set[str] = set()
    kept: list[str] = []
    for j in range(header_idx + 1, end):
        line = lines[j]
        match = re.match(r"\s*(\w+)\s*=", line)
        if match and match.group(1) in key_values:
            key = match.group(1)
            seen.add(key)
            value = key_values[key]
            if value is not None:
                kept.append(f"{key} = {value}")
            continue
        kept.append(line)

    inserts = [f"{k} = {v}" for k, v in key_values.items() if v is not None and k not in seen]
    new_lines = lines[: header_idx + 1] + inserts + kept + lines[end:]
    return "\n".join(new_lines) + "\n"


def _set_top_level_key(text: str, key: str, value: str) -> str:
    """Set a top-level TOML key — must sit before the first table header."""
    lines = text.splitlines()
    first_header = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    for i in range(first_header):
        if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
            lines[i] = f"{key} = {value}"
            return "\n".join(lines) + "\n"
    lines.insert(first_header, f"{key} = {value}")
    return "\n".join(lines) + "\n"


def _toml_str(value: str) -> str:
    """A TOML basic string — escapes backslashes and quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_str_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def save_config(config: Config, path: Path) -> None:
    """Write roots and logging back to a TOML file, preserving the rest.

    ``[[roots]]`` blocks are replaced wholesale (comments inside them are
    dropped); other tables keep their content and comments verbatim.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()

    # Remove existing [[roots]] blocks (array-of-tables: each block runs
    # until the next section header or EOF)
    kept: list[str] = []
    insert_at = 0
    i = 0
    while i < len(lines):
        if lines[i].strip() == "[[roots]]":
            if not any(line.strip() == "[[roots]]" for line in kept):
                insert_at = len(kept)
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("["):
                i += 1
            continue
        kept.append(lines[i])
        i += 1

    roots_lines: list[str] = []
    for root in config.roots:
        roots_lines += [
            "[[roots]]",
            f"path = {_toml_str(root.path)}",
            f"recursive = {'true' if root.recursive else 'false'}",
            "",
        ]
    if roots_lines and insert_at > 0 and kept[insert_at - 1] != "":
        roots_lines.insert(0, "")
    kept[insert_at:insert_at] = roots_lines

    text = "\n".join(kept)
    text = _set_table_keys(
        text,
        "logging",
        {
            "level": _toml_str(config.logging.level),
            "file": _toml_str(config.logging.file_path) if config.logging.file_path else None,
        },
    )
    text = _set_table_keys(
        text,
        "reporting",
        # The threshold moved to [matching]. Dropping the old key keeps a
        # config written by an older build from carrying a second, silent
        # copy of the gate that would take over again if the new key went
        # missing.
        {"review_min_duration_ratio": None},
    )
    text = _set_top_level_key(text, "exclude_patterns", _toml_str_array(config.exclude_patterns))
    text = _set_top_level_key(
        text, "include_hidden", "true" if config.include_hidden else "false"
    )
    text = _set_table_keys(
        text,
        "actions",
        {"protected_paths": _toml_str_array(config.actions.protected_paths)},
    )
    text = _set_table_keys(
        text,
        "matching",
        {
            "frame_match_max_distance": repr(config.matching.frame_match_max_distance),
            "very_likely_threshold": repr(config.matching.very_likely_threshold),
            "very_likely_mean_distance": repr(config.matching.very_likely_mean_distance),
            "possible_threshold": repr(config.matching.possible_threshold),
            "possible_min_matched_frames": str(config.matching.possible_min_matched_frames),
            "signature_min_shared_frames": str(config.matching.signature_min_shared_frames),
            "signature_max_hamming": str(config.matching.signature_max_hamming),
            "signature_min_band_hits": str(config.matching.signature_min_band_hits),
            "head_bag_enabled": "true" if config.matching.head_bag_enabled else "false",
            "head_bag_seconds": repr(config.matching.head_bag_seconds),
            "head_bag_fps": repr(config.matching.head_bag_fps),
            "head_bag_min_shared": str(config.matching.head_bag_min_shared),
            "min_duration_ratio": repr(config.matching.min_duration_ratio),
            "strictness": str(config.matching.strictness),
        },
    )
    # A file written by a build that still had the coarse tier carries two
    # settings nothing reads now; saving is where they stop being carried
    if any(key in text for key in _RETIRED_MATCHING_OPTIONS):
        text = _set_table_keys(text, "matching", dict.fromkeys(_RETIRED_MATCHING_OPTIONS))
    path.write_text(text, encoding="utf-8")


def generate_example_config() -> str:
    """Generate an example configuration file as a string.

    Returns:
        Example configuration TOML content.
    """
    return """# Akin Video Finder Configuration
# This file controls how the application scans and processes video files.

# Source roots to scan. Each root can be on a different drive letter.
# recursive: true to scan subdirectories, false to scan only the root directory.
# None are pre-filled: a first run has not chosen a folder yet, and inventing
# "D:/Videos" or "E:/Media" pointed the first scan at drives that may not
# exist. Add folders on the Scan tab (or add [[roots]] blocks here); until
# then the app explains that a folder is needed and keeps Scan disabled.

# Video file extensions to include. Case-insensitive.
# Add or remove extensions as needed. `.ts` and `.mts` are left out by
# default: those suffixes are as common in TypeScript sources as in MPEG
# transport streams, so a scanned downloads folder spends its probes on
# `.d.ts` files. Add them back if you really hold transport streams; the
# scanner still skips `*.d.ts`/`*.d.mts`/`*.d.cts`.
video_extensions = [
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

# Optional glob patterns to include. Files matching these patterns are included
# even if they don't match the default extension list.
# include_patterns = [
#     "*.custom",
# ]

# Optional glob patterns to exclude. Files matching these patterns are skipped
# even if they match the extension list.
# exclude_patterns = [
#     "*_sample.*",
#     "*_trailer.*",
# ]

# Whether to follow symbolic links and junctions. Default is false for safety.
follow_symlinks = false

# Whether to scan hidden and system entries. Default is false: dot-folders,
# Windows hidden/system items, and $RECYCLE.BIN / System Volume Information
# are skipped. A root you add explicitly is always scanned.
include_hidden = false

# Logging configuration
[logging]
level = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
# file = "akin_video_finder.log"  # Optional: log to file in addition to console

# FFprobe configuration
[ffprobe]
executable_path = "ffprobe"  # Path to ffprobe executable
timeout_seconds = 60        # Maximum time to wait for ffprobe per file
worker_count = 2            # Number of concurrent ffprobe processes
max_stderr_bytes = 8192     # Maximum stderr to capture per file

# Database configuration
[database]
path = "akin_video_finder.db"  # SQLite database file path

# Reporting configuration
[reporting]
output_directory = "reports"  # Directory for exported reports

# Perceptual matching configuration (Milestone 2)
[matching]
enabled = true                # Set to false to skip perceptual matching
ffmpeg_path = "ffmpeg"        # Path to ffmpeg executable
frame_timeout_seconds = 30    # Timeout per frame extraction
frame_width = 16              # Normalized frame width for fingerprints
frame_height = 16             # Normalized frame height for fingerprints
min_frames = 8                # Minimum frames sampled per video
max_frames = 96               # Maximum frames sampled per video
target_frame_interval_seconds = 30.0  # Roughly one frame per N seconds
duration_tolerance_ratio = 0.02       # Candidate gate: relative duration slack
duration_tolerance_seconds = 5.0      # Candidate gate: absolute duration slack
# A pair is not compared at all when the shorter copy is under this fraction
# of the longer one (0-1]. A clip inside a compilation is not a duplicate of
# it. Lower it to compare more length-different pairs (~0.5 for scenes inside
# compilations); 1.0 compares only copies whose durations agree exactly. The
# shipped default only decides what a fresh install does.
min_duration_ratio = 0.88
frame_match_max_distance = 0.10       # Normalized mean pixel difference per frame
very_likely_threshold = 0.9   # Matching-frame fraction for VERY_LIKELY
very_likely_mean_distance = 0.08  # Mean pixel distance bound for strong evidence
possible_threshold = 0.7      # Matching-frame fraction for POSSIBLE
possible_min_matched_frames = 6  # Min strongly-matched frames for POSSIBLE (partial overlap)
signature_frames = 8            # Sparse frames hashed per file for candidate blocking
signature_min_shared_frames = 2 # Sparse frames that must match for a pair to be compared
signature_max_bucket_files = 200  # Skip uninformative hash slices larger than this
signature_min_band_hits = 2       # Shared 16-bit band keys required before pair verification
head_bag_enabled = true           # Dense opening-minute hashes: finds a copy behind a prepended ad
head_bag_seconds = 90.0           # Seconds decoded from each file's start for that index
head_bag_fps = 1.0                # Samples per second over that window
head_bag_min_shared = 24          # Shared 16-bit hash bands (4 per frame) before a shift is probed
signature_max_hamming = 10        # Max bit distance for shared signature frames
signature_workers = 4             # Concurrent ffmpeg processes for signatures
crop_detect = true                # Remove letterbox/pillarbox bars before hashing frames
trim_short_coverage_min = 0.8 # >=80% of the shorter video must match
trim_long_coverage_min = 0.7  # Shared span must cover >=70% of the longer video

[keeper]
# Which copy of a duplicate pair to recommend keeping.
# Extensions that win a tie, e.g. [".mkv", ".mp4"]
preferred_extensions = []
# Path substrings that win a tie, e.g. ["archive"]
preferred_path_keywords = []
# Quality "stops" (log2 units) charged for each 1% a copy is shorter than its
# pair. Quality comes from bitrate, pixels, codec and audio, so the default
# 0.5 lets a copy with twice the bitrate and four times the pixels run about
# 5% shorter and still be recommended; a higher value prefers longer copies,
# and 0 makes length matter only as a tiebreak.
length_penalty_per_percent = 0.5

[actions]
# Folders that may be scanned but must never be moved or deleted. Use
# absolute paths; a whole drive is protected by listing its root.
protected_paths = []
"""

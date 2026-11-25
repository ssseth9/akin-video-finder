"""Tests for configuration loading and validation."""

import logging
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from akin_video_finder.configuration.loader import (
    generate_example_config,
    load_config,
    save_config,
)
from akin_video_finder.configuration.models import ConfigValidationError


def test_load_valid_config(tmp_path: Path) -> None:
    """Test loading a valid configuration file."""
    config_content = r"""
[[roots]]
path = "D:/Videos"
recursive = true

[[roots]]
path = "E:/Media"
recursive = false

[logging]
level = "DEBUG"

[ffprobe]
executable_path = "ffprobe"
timeout_seconds = 30
worker_count = 4
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    config = load_config(config_file)

    assert len(config.roots) == 2
    assert config.roots[0].path == "D:/Videos"
    assert config.roots[0].recursive is True
    assert config.roots[1].path == "E:/Media"
    assert config.roots[1].recursive is False
    assert config.logging.level == "DEBUG"
    assert config.ffprobe.executable_path == "ffprobe"
    assert config.ffprobe.timeout_seconds == 30
    assert config.ffprobe.worker_count == 4


def test_load_config_with_defaults(tmp_path: Path) -> None:
    """Test loading a minimal configuration with defaults."""
    config_content = r"""
[[roots]]
path = "D:/Videos"
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    config = load_config(config_file)

    assert len(config.roots) == 1
    assert config.roots[0].path == "D:/Videos"
    assert config.roots[0].recursive is True
    assert config.logging.level == "INFO"
    assert config.ffprobe.timeout_seconds == 60
    assert config.ffprobe.worker_count == 2


def test_load_config_missing_file(tmp_path: Path) -> None:
    """Test that loading a non-existent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Configuration file not found"):
        load_config(tmp_path / "nonexistent.toml")


def test_load_config_invalid_toml(tmp_path: Path) -> None:
    """Test that invalid TOML raises ConfigValidationError."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("invalid toml [[[[")

    with pytest.raises(ConfigValidationError, match="Invalid TOML syntax"):
        load_config(config_file)


def test_load_config_without_roots_is_the_first_run_state(tmp_path: Path) -> None:
    """An empty config loads with no folders instead of failing.

    Nothing has been chosen yet on a fresh install, and the GUI lets the last
    folder be removed before another is added; the scan controls, not the
    config constructor, are what keep an empty list from starting a scan.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text("")

    config = load_config(config_file)

    assert config.roots == []


def test_load_config_empty_root_path(tmp_path: Path) -> None:
    """Test that empty root path raises error."""
    config_content = r"""
[[roots]]
path = ""
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    with pytest.raises(ConfigValidationError, match="path"):
        load_config(config_file)


def test_load_config_invalid_root_type(tmp_path: Path) -> None:
    """Test that non-dict root raises error."""
    config_content = """
roots = ["not", "a", "dict"]
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    with pytest.raises(ConfigValidationError, match="Root 0 must be a dictionary"):
        load_config(config_file)


def test_load_config_invalid_recursive_type(tmp_path: Path) -> None:
    """Test that non-boolean recursive raises error."""
    config_content = """
[[roots]]
path = "D:/Videos"
recursive = "yes"
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    with pytest.raises(ConfigValidationError, match="'recursive' must be a boolean"):
        load_config(config_file)


def test_load_config_invalid_video_extensions(tmp_path: Path) -> None:
    """Test that invalid video_extensions raises error."""
    config_content = r"""
video_extensions = ["not", "a", "valid", "extension"]

[[roots]]
path = "D:/Videos"
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    # Should not raise error, just normalize the extensions
    config = load_config(config_file)
    assert ".not" in config.video_extensions
    assert ".valid" in config.video_extensions


def test_load_config_custom_video_extensions(tmp_path: Path) -> None:
    """Test custom video extensions are loaded and normalized."""
    config_content = r"""
[[roots]]
path = "D:/Videos"
video_extensions = ["mp4", "mkv", ".avi"]
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    config = load_config(config_file)

    # Custom extensions should be loaded and normalized
    assert ".mp4" in config.video_extensions
    assert ".mkv" in config.video_extensions
    assert ".avi" in config.video_extensions


def test_load_config_invalid_logging_level(tmp_path: Path) -> None:
    """Test that invalid logging level raises error."""
    config_content = """
[[roots]]
path = "D:/Videos"

[logging]
level = "INVALID"
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    with pytest.raises(ConfigValidationError, match="'logging.level' must be one of"):
        load_config(config_file)


def test_load_config_invalid_ffprobe_timeout(tmp_path: Path) -> None:
    """Test that invalid ffprobe timeout raises error."""
    config_content = """
[[roots]]
path = "D:/Videos"

[ffprobe]
timeout_seconds = -1
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    with pytest.raises(
        ConfigValidationError, match="'ffprobe.timeout_seconds' must be a positive integer"
    ):
        load_config(config_file)


def test_load_config_invalid_ffprobe_workers(tmp_path: Path) -> None:
    """Test that invalid ffprobe worker count raises error."""
    config_content = """
[[roots]]
path = "D:/Videos"

[ffprobe]
worker_count = 0
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    with pytest.raises(
        ConfigValidationError, match="'ffprobe.worker_count' must be a positive integer"
    ):
        load_config(config_file)


def test_load_config_include_exclude_patterns(tmp_path: Path) -> None:
    """Test include and exclude patterns are loaded."""
    config_content = r"""
include_patterns = ["*.mp4", "*.mkv"]
exclude_patterns = ["*_sample.*"]

[[roots]]
path = "D:/Videos"
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)

    config = load_config(config_file)

    assert config.include_patterns == ["*.mp4", "*.mkv"]
    assert config.exclude_patterns == ["*_sample.*"]


def test_save_config_roundtrips_matching_fields(tmp_path: Path) -> None:
    """save_config lists matching keys explicitly; none may be dropped."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(generate_example_config())
    base = load_config(config_file)

    matching = replace(
        base.matching,
        signature_min_band_hits=3,
    )
    save_config(replace(base, matching=matching), config_file)

    reloaded = load_config(config_file)
    assert reloaded.matching.signature_min_band_hits == 3


def test_retired_matching_keys_still_load_and_are_dropped_on_save(tmp_path: Path) -> None:
    """A config written by an older build must keep working.

    Three keys configured things the app no longer has — the coarse candidate
    tier and the per-scan probe cap that existed to tame it. Failing the load
    would strand a user's file over settings that have nothing left to set, so
    they are accepted and then left out of the next save.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[[roots]]\npath = "D:/Videos"\n\n[matching]\n'
        "signature_coarse_min_hits = 2\n"
        "offset_max_coarse_probes_per_scan = 50\n"
        "offset_max_probes_per_scan = 400\n"
        "signature_min_band_hits = 3\n"
    )

    config = load_config(config_file)

    assert config.matching.signature_min_band_hits == 3

    save_config(config, config_file)
    saved = config_file.read_text()
    assert "signature_coarse_min_hits" not in saved
    assert "offset_max_coarse_probes_per_scan" not in saved
    assert "offset_max_probes_per_scan" not in saved


def test_min_duration_ratio_moved_into_matching(tmp_path: Path) -> None:
    """The duration threshold is a scan gate now, so it lives in [matching].

    It started as a list filter in [reporting]; once it decides which pairs a
    scan compares, its section has to be the one the config fingerprint
    covers, or results would silently disagree with the settings that made
    them. Old configs keep loading through the alias, and both fields are
    mirrored so no reader sees a stale second copy.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text('[[roots]]\npath = "D:/Videos"\n')
    assert load_config(config_file).matching.min_duration_ratio == 0.88

    config_file.write_text(
        '[[roots]]\npath = "D:/Videos"\n\n[reporting]\nreview_min_duration_ratio = 0.5\n'
    )
    config = load_config(config_file)
    assert config.matching.min_duration_ratio == 0.5
    assert config.reporting.review_min_duration_ratio == 0.5

    config_file.write_text(
        '[[roots]]\npath = "D:/Videos"\n\n[reporting]\nreview_min_duration_ratio = 0.5\n'
        "\n[matching]\nmin_duration_ratio = 0.7\n"
    )
    config = load_config(config_file)
    assert config.matching.min_duration_ratio == 0.7
    # The mirror follows the gate, not the leftover alias
    assert config.reporting.review_min_duration_ratio == 0.7


def test_min_duration_ratio_is_validated(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[[roots]]\npath = "D:/Videos"\n\n[matching]\nmin_duration_ratio = 0\n')

    with pytest.raises(ConfigValidationError, match="min_duration_ratio must be in"):
        load_config(config_file)


def test_target_frame_interval_must_be_positive(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[[roots]]\npath = "D:/Videos"\n\n'
        "[matching]\ntarget_frame_interval_seconds = 0\n"
    )

    with pytest.raises(
        ConfigValidationError, match="target_frame_interval_seconds must be positive"
    ):
        load_config(config_file)


def test_overlapping_roots_are_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[[roots]]\npath = "D:/Videos"\n\n[[roots]]\npath = "D:/Videos/Sub"\n'
    )

    with pytest.raises(ConfigValidationError, match="Configured roots must not overlap"):
        load_config(config_file)


def test_unknown_keys_are_reported(tmp_path: Path, caplog) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[[roots]]\npath = "D:/Videos"\n\n[ffprobe]\ntimeout_secondz = 30\n'
    )

    with caplog.at_level(logging.WARNING):
        load_config(config_file)

    assert "timeout_secondz" in caplog.text


def test_save_config_writes_the_gate_into_matching(tmp_path: Path) -> None:
    """Saving must not leave the old [reporting] key behind.

    Two copies could disagree, and the leftover would silently become the gate
    again if the new key ever went missing.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[[roots]]\npath = "D:/Videos"\n\n[reporting]\nreview_min_duration_ratio = 0.5\n'
    )
    save_config(load_config(config_file), config_file)

    text = config_file.read_text(encoding="utf-8")
    assert "review_min_duration_ratio" not in text
    assert "min_duration_ratio = 0.5" in text
    assert load_config(config_file).matching.min_duration_ratio == 0.5


def test_include_hidden_and_protected_paths_roundtrip(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(generate_example_config())
    base = load_config(config_file)

    protected = str(tmp_path / "Masters")
    updated = replace(
        base,
        include_hidden=True,
        actions=replace(base.actions, protected_paths=[protected]),
    )
    save_config(updated, config_file)

    reloaded = load_config(config_file)
    assert reloaded.include_hidden is True
    assert reloaded.actions.protected_paths == [protected]
    text = config_file.read_text(encoding="utf-8")
    assert "[actions]" in text
    assert "protected_paths" in text


def test_relative_protected_path_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[[roots]]\npath = "D:/Videos"\n\n[actions]\nprotected_paths = ["relative/folder"]\n'
    )

    with pytest.raises(ConfigValidationError, match="must be an absolute path"):
        load_config(config_file)


def test_default_extensions_leave_typescript_suffixes_out(tmp_path: Path) -> None:
    """`.ts`/`.mts` collide with TypeScript sources, so they are not defaults.

    The scanner's `*.d.ts`/`*.d.mts`/`*.d.cts` guard is not enough on its own:
    on the reference library 6 plain `.ts` modules and 6 `*.d.mts` files under
    one node_modules tree still reached ffprobe, and the 544 `*.d.ts`
    declarations cost a failed probe each before the guard existed. A library
    that really holds MPEG transport streams can name the extension
    explicitly, and the declarations stay skipped.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text('[[roots]]\npath = "D:/Videos"\n')
    defaults = load_config(config_file).video_extensions
    assert ".ts" not in defaults
    assert ".mts" not in defaults
    assert ".m2ts" in defaults

    config_file.write_text(
        'video_extensions = [".mp4", ".ts", ".mts"]\n[[roots]]\npath = "D:/Videos"\n'
    )
    explicit = load_config(config_file).video_extensions
    assert ".ts" in explicit
    assert ".mts" in explicit


def _key_paths(data: dict[str, Any], prefix: str = "") -> set[str]:
    """Every setting name in a parsed TOML document, as dotted paths."""
    paths: set[str] = set()
    for key, value in data.items():
        path = f"{prefix}{key}"
        paths.add(path)
        if isinstance(value, dict):
            paths |= _key_paths(value, f"{path}.")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    paths |= _key_paths(item, f"{path}.")
    return paths


def test_repo_example_config_documents_every_template_key() -> None:
    """README promises config.example.toml lists every setting.

    The file carries hand-written commentary the template lacks, so it cannot
    simply be regenerated. This guard fails when a setting reaches the
    template but never reaches the file readers actually open.
    """
    root = Path(__file__).resolve().parents[3]
    shipped = tomllib.loads((root / "config.example.toml").read_text(encoding="utf-8"))
    template = tomllib.loads(generate_example_config())

    assert sorted(_key_paths(template) - _key_paths(shipped)) == []


def test_generate_example_config() -> None:
    """Test that example config is generated."""
    example = generate_example_config()
    # No placeholder roots: a first run has not chosen a folder yet, and
    # inventing D:/Videos pointed the first scan at a drive that may not exist
    assert tomllib.loads(example).get("roots", []) == []
    assert "[logging]" in example
    assert "[ffprobe]" in example
    assert "[database]" in example
    assert "[reporting]" in example

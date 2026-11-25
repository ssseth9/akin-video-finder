"""End-to-end matching gate over the committed synthetic clips.

This runs the real discovery, probe, signature, blocking, fingerprint, and
comparison path. The manifest is the ground truth: a threshold change that
turns a negative into a match, or drops a known duplicate, fails here before
it reaches a library.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from akin_video_finder.application.scan_service import ScanService
from akin_video_finder.configuration.models import (
    Config,
    MatchingConfig,
    ReportingConfig,
    RootConfig,
)
from akin_video_finder.persistence.repository import DatabaseRepository

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "matching"
_RANK = {"EXACT": 3, "VERY_LIKELY": 2, "POSSIBLE": 1, "REJECTED": 0, "ERROR": 0, "NONE": 0}


def _rank(classification: str | None) -> int:
    return _RANK.get(classification or "NONE", 0)


def _pair_map(
    repository: DatabaseRepository, scan_id: int
) -> tuple[dict[tuple[str, str], str], dict[int, str]]:
    files = {row["id"]: Path(row["original_path"]).name for row in repository.get_present_files()}
    pairs: dict[tuple[str, str], str] = {}
    for row in repository.get_pair_comparisons(scan_id):
        name_a = files.get(row["file_a_id"])
        name_b = files.get(row["file_b_id"])
        if name_a and name_b:
            pairs[tuple(sorted((name_a, name_b)))] = row["classification"]
    return pairs, files


def _scan(tmp_path: Path) -> tuple[dict, dict[tuple[str, str], str]]:
    matching = replace(
        MatchingConfig(),
        min_duration_ratio=0.8,
        head_bag_enabled=False,
        signature_workers=2,
        crop_detect=True,
    )
    config = Config(
        roots=[RootConfig(path=str(FIXTURE_DIR), recursive=True)],
        matching=matching,
        reporting=ReportingConfig(output_directory=str(tmp_path / "reports")),
    )
    repository = DatabaseRepository(tmp_path / "corpus.db")
    try:
        result = ScanService(config, repository).scan()
        assert result["status"] == "completed"
        pairs, _files = _pair_map(repository, result["scan_id"])
        return result, pairs
    finally:
        repository.close()


def _failures(
    manifest: dict,
    pairs: dict[tuple[str, str], str],
) -> list[str]:
    failures: list[str] = []
    base = manifest["base"]
    clips = manifest["clips"]
    by_name = {clip["file"]: clip for clip in clips}

    for clip in clips:
        name = clip["file"]
        if name == base:
            continue
        expected = clip["expected_vs_base"]
        actual = pairs.get(tuple(sorted((base, name))))
        if expected == "NEGATIVE":
            if _rank(actual) >= _RANK["VERY_LIKELY"]:
                failures.append(
                    f"unexpected match: {base} vs {name} -> {actual} (expected below VERY_LIKELY)"
                )
        elif expected == "EXACT":
            if actual != "EXACT":
                failures.append(f"expected EXACT: {base} vs {name} -> {actual}")
        elif _rank(actual) < _RANK[expected]:
            failures.append(
                f"missed match: {base} vs {name} -> {actual} (expected at least {expected})"
            )

    names = [clip["file"] for clip in clips]
    for i, name_a in enumerate(names):
        for name_b in names[i + 1 :]:
            family_a = by_name[name_a]["content_family"]
            family_b = by_name[name_b]["content_family"]
            if family_a == family_b:
                continue
            actual = pairs.get(tuple(sorted((name_a, name_b))))
            if _rank(actual) >= _RANK["VERY_LIKELY"]:
                failures.append(
                    f"cross-family match: {name_a} vs {name_b} -> {actual} "
                    "(expected below VERY_LIKELY)"
                )
    return failures


@pytest.mark.integration
def test_committed_corpus_matches_the_manifest(tmp_path: Path) -> None:
    if not FIXTURE_DIR.is_dir():
        pytest.skip("matching fixtures are not present")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe are not available")

    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    result, pairs = _scan(tmp_path)

    assert result["present_files"] == len(manifest["clips"])
    failures = _failures(manifest, pairs)
    assert not failures, "\n".join(failures)


@pytest.mark.integration
def test_warm_cache_reproduces_the_same_pairs(tmp_path: Path) -> None:
    if not FIXTURE_DIR.is_dir():
        pytest.skip("matching fixtures are not present")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe are not available")

    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    matching = replace(
        MatchingConfig(),
        min_duration_ratio=0.8,
        head_bag_enabled=False,
        signature_workers=2,
        crop_detect=True,
    )
    config = Config(
        roots=[RootConfig(path=str(FIXTURE_DIR), recursive=True)],
        matching=matching,
        reporting=ReportingConfig(output_directory=str(tmp_path / "reports")),
    )
    repository = DatabaseRepository(tmp_path / "corpus.db")
    try:
        first = ScanService(config, repository).scan()
        first_pairs = _pair_map(repository, first["scan_id"])[0]
        second = ScanService(config, repository).scan()
        second_pairs = _pair_map(repository, second["scan_id"])[0]
    finally:
        repository.close()

    assert second["status"] == "completed"
    assert second["signature_cache_hits"] > 0
    assert second["fingerprint_cache_hits"] > 0
    assert second_pairs == first_pairs
    failures = _failures(manifest, second_pairs)
    assert not failures, "\n".join(failures)

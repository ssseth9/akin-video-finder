"""Export tolerates evidence that is not JSON.

``evidence`` is written by the matcher as JSON, but an imported or older
database can hold a plain-text summary. ``json.loads`` raised inside the
export, so one such row aborted the whole report: no file written, and the
GUI caught only ``OSError``, so the click looked ignored.
"""

import json
from pathlib import Path

from akin_video_finder.configuration.loader import load_config
from akin_video_finder.persistence.repository import DatabaseRepository
from akin_video_finder.reporting.exporter import (
    _evidence_value,
    export_csv_report,
    export_json_report,
)


def _repo_with_evidence(tmp_path: Path, evidence: object) -> tuple[DatabaseRepository, int]:
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    ids = []
    for i in range(2):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x")
        ids.append(
            repo.create_file(
                root_id, str(video), str(video).lower(), video.name, ".mp4", 1, 1000 + i, scan_id
            )
        )
    repo.create_pair_comparison(
        scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12, evidence=evidence
    )
    repo.commit()
    return repo, scan_id


class TestEvidenceValue:
    def test_json_is_parsed(self):
        assert _evidence_value('{"mode": "full", "matching_frames": 4}') == {
            "mode": "full",
            "matching_frames": 4,
        }

    def test_plain_text_is_kept_as_a_string(self):
        assert _evidence_value("matched 4 of 4 frames") == "matched 4 of 4 frames"

    def test_truncated_json_is_kept_as_a_string(self):
        assert _evidence_value('{"mode": "full"} extra') == '{"mode": "full"} extra'

    def test_empty_is_none(self):
        assert _evidence_value(None) is None
        assert _evidence_value("") is None


def test_json_export_survives_a_plain_text_evidence_row(tmp_path):
    repo, scan_id = _repo_with_evidence(tmp_path, "matched 38 of 48 frames")
    try:
        content = export_json_report(repo, scan_id)
    finally:
        repo.close()

    report = json.loads(content)
    assert report["pair_comparisons"][0]["evidence"] == "matched 38 of 48 frames"


def test_csv_export_has_one_line_per_row(tmp_path):
    repo, scan_id = _repo_with_evidence(tmp_path, None)
    try:
        content = export_csv_report(repo, scan_id)
    finally:
        repo.close()

    assert "\r" not in content
    assert len(content.splitlines()) == 3


def test_json_export_includes_configuration_and_actions(tmp_path):
    repo, scan_id = _repo_with_evidence(tmp_path, None)
    try:
        file_id = repo.conn.execute("SELECT id FROM files ORDER BY id LIMIT 1").fetchone()[0]
        repo.record_action(
            file_id,
            scan_id,
            "delete",
            str(tmp_path / "v0.mp4"),
            "",
            status="executed",
        )
        repo.commit()
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[database]\npath = "{(tmp_path / "v.db").as_posix()}"\n',
            encoding="utf-8",
        )
        config = load_config(config_path)
        report = json.loads(export_json_report(repo, scan_id, config))
    finally:
        repo.close()

    assert report["configuration"]["database"]["path"].endswith("v.db")
    assert len(report["actions"]) == 1
    assert report["actions"][0]["action"] == "delete"
    assert report["actions"][0]["status"] == "executed"

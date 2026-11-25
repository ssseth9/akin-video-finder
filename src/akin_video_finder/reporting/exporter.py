"""Report export functionality."""

import csv
import json
from dataclasses import asdict
from io import StringIO
from typing import TYPE_CHECKING, Any

from akin_video_finder.persistence.repository import DatabaseRepository

if TYPE_CHECKING:
    from akin_video_finder.configuration.models import Config


def _evidence_value(raw: str | None) -> Any:
    """The stored evidence as JSON, falling back to the raw text.

    ``evidence`` is written as JSON by the matcher, but a database that was
    imported or written by an older build can hold a plain-text summary.
    ``json.loads`` used to raise inside the export, so one such row aborted
    the whole JSON report with no file written and no visible error.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def export_report(
    repository: DatabaseRepository,
    scan_id: int,
    format: str,
    config: "Config | None" = None,
) -> str:
    """Export an inventory report.

    Args:
        repository: Database repository.
        scan_id: Scan ID to export.
        format: Export format ("json" or "csv").
        config: Resolved configuration to include in the JSON report.

    Returns:
        Report content as string.
    """
    if format == "json":
        return export_json_report(repository, scan_id, config)
    elif format == "csv":
        return export_csv_report(repository, scan_id)
    else:
        raise ValueError(f"Unsupported format: {format}")


def export_json_report(
    repository: DatabaseRepository,
    scan_id: int,
    config: "Config | None" = None,
) -> str:
    """Export a JSON report.

    Args:
        repository: Database repository.
        scan_id: Scan ID to export.
        config: Resolved configuration to include in the report.

    Returns:
        JSON report content as string.
    """
    scan = repository.get_scan(scan_id)
    if not scan:
        raise ValueError(f"Scan {scan_id} not found")

    roots = repository.get_all_roots()

    files_data = []
    for root in roots:
        files = repository.get_files_for_root(root["id"])
        for file in files:
            probe = repository.get_probe_metadata(file["id"])
            file_data = {
                "id": file["id"],
                "root_id": file["root_id"],
                "root_path": root["original_path"],
                "original_path": file["original_path"],
                "file_name": file["file_name"],
                "extension": file["extension"],
                "size": file["size"],
                "mtime_ns": file["mtime_ns"],
                "presence": file["presence"],
                "is_symlink": bool(file["is_symlink"]),
                "link_target": file["link_target"],
                "probe_status": probe["status"] if probe else None,
                "probe_algorithm_version": probe["algorithm_version"] if probe else None,
                "container_format": probe["container_format_name"] if probe else None,
                "duration": probe["container_duration"] if probe else None,
                "has_video": probe["has_video"] if probe else None,
                "video_stream_count": probe["video_stream_count"] if probe else None,
                "audio_stream_count": probe["audio_stream_count"] if probe else None,
                "primary_video_codec": probe["primary_video_codec"] if probe else None,
                "primary_video_width": probe["primary_video_width"] if probe else None,
                "primary_video_height": probe["primary_video_height"] if probe else None,
                "primary_video_fps": probe["primary_video_fps"] if probe else None,
                "primary_audio_codec": probe["primary_audio_codec"] if probe else None,
                "probe_error_category": probe["error_category"] if probe else None,
                "probe_error_message": probe["error_message"] if probe else None,
            }
            files_data.append(file_data)

    pair_comparisons = repository.get_pair_comparisons(scan_id)

    report = {
        "scan_id": scan["id"],
        "scan_status": scan["status"],
        "start_time": scan["start_time"],
        "end_time": scan["end_time"],
        "application_version": scan["application_version"],
        "configuration": asdict(config) if config is not None else None,
        "total_files": scan["total_files"],
        "present_files": scan["present_files"],
        "missing_files": scan["missing_files"],
        "error_files": scan["error_files"],
        "cache_hits": scan["cache_hits"],
        "roots": [
            {
                "id": root["id"],
                "original_path": root["original_path"],
                "normalized_path_key": root["normalized_path_key"],
            }
            for root in roots
        ],
        "pair_comparisons": [
            {
                "file_a_id": c["file_a_id"],
                "file_b_id": c["file_b_id"],
                "classification": c["classification"],
                "match_fraction": c["match_fraction"],
                "mean_frame_distance": c["mean_frame_distance"],
                "frames_compared": c["frames_compared"],
                "duration_difference": c["duration_difference"],
                "evidence": _evidence_value(c.get("evidence")),
            }
            for c in pair_comparisons
        ],
        "actions": [dict(row) for row in repository.get_actions(scan_id)],
        "files": files_data,
    }

    return json.dumps(report, indent=2, ensure_ascii=False, default=str)


def export_csv_report(repository: DatabaseRepository, scan_id: int) -> str:
    """Export a CSV report.

    Args:
        repository: Database repository.
        scan_id: Scan ID to export.

    Returns:
        CSV report content as string.
    """
    scan = repository.get_scan(scan_id)
    if not scan:
        raise ValueError(f"Scan {scan_id} not found")

    roots = {root["id"]: root["original_path"] for root in repository.get_all_roots()}

    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")

    # Write header
    header = [
        "File ID",
        "Root Path",
        "Original Path",
        "File Name",
        "Extension",
        "Size (bytes)",
        "Presence",
        "Probe Status",
        "Container Format",
        "Duration (s)",
        "Video Streams",
        "Audio Streams",
        "Video Codec",
        "Video Width",
        "Video Height",
        "Video FPS",
        "Audio Codec",
        "Error Category",
        "Error Message",
        "Exact Pair",
        "Perceptual Pair",
        "Symlink",
        "Link Target",
    ]
    writer.writerow(header)

    # Map file_id -> a pair id of each class, for the inventory columns
    file_to_exact: dict[int, int] = {}
    file_to_perceptual: dict[int, int] = {}
    for comparison in repository.get_pair_comparisons(scan_id):
        target = file_to_exact if comparison["classification"] == "EXACT" else file_to_perceptual
        if comparison["classification"] in ("EXACT", "VERY_LIKELY", "POSSIBLE"):
            target.setdefault(comparison["file_a_id"], comparison["id"])
            target.setdefault(comparison["file_b_id"], comparison["id"])

    # Write file rows
    for root in roots.values():
        root_id = [r["id"] for r in repository.get_all_roots() if r["original_path"] == root][0]
        files = repository.get_files_for_root(root_id)
        for file in files:
            probe = repository.get_probe_metadata(file["id"])
            row = [
                file["id"],
                roots.get(file["root_id"], ""),
                file["original_path"],
                file["file_name"],
                file["extension"],
                file["size"],
                file["presence"],
                probe["status"] if probe else "",
                probe["container_format_name"] if probe else "",
                probe["container_duration"] if probe else "",
                probe["video_stream_count"] if probe else "",
                probe["audio_stream_count"] if probe else "",
                probe["primary_video_codec"] if probe else "",
                probe["primary_video_width"] if probe else "",
                probe["primary_video_height"] if probe else "",
                probe["primary_video_fps"] if probe else "",
                probe["primary_audio_codec"] if probe else "",
                probe["error_category"] if probe else "",
                probe["error_message"] if probe else "",
                file_to_exact.get(file["id"], ""),
                file_to_perceptual.get(file["id"], ""),
                "yes" if file["is_symlink"] else "",
                file["link_target"] or "",
            ]
            writer.writerow(row)

    return output.getvalue()

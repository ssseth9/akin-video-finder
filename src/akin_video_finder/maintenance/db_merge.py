"""Merge two Akin Video Finder databases.

Imports all data from a source database into a target database,
remapping row IDs. Files deduplicate on (root, normalized path key);
cached artifacts (probes, hashes, signatures, fingerprints) union —
the target's copy wins on conflict. Scans import as new scan runs
with their original timestamps, so history from both databases stays
reviewable.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup_database(path: Path) -> Path:
    """Online-backup a database (WAL-safe) next to the original."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    src = sqlite3.connect(str(path))
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def merge_databases(source: Path, target: Path) -> dict[str, int]:
    """Merge source database contents into target.

    Returns:
        Row counts imported/skipped per table.
    """
    src = sqlite3.connect(str(source))
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(target))
    dst.row_factory = sqlite3.Row
    stats: dict[str, int] = {}
    try:
        dst.execute("PRAGMA foreign_keys = ON")
        dst.execute("PRAGMA busy_timeout = 30000")
        dst.execute("BEGIN")

        # --- roots: union by normalized_path_key --------------------
        dst_roots = {
            r["normalized_path_key"]: r["id"]
            for r in dst.execute("SELECT id, normalized_path_key FROM configured_roots")
        }
        root_map: dict[int, int] = {}
        added = 0
        for r in src.execute("SELECT * FROM configured_roots"):
            key = r["normalized_path_key"]
            if key in dst_roots:
                root_map[r["id"]] = dst_roots[key]
            else:
                cur = dst.execute(
                    "INSERT INTO configured_roots"
                    " (original_path, normalized_path_key, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (r["original_path"], key, r["created_at"], r["updated_at"]),
                )
                assert cur.lastrowid is not None
                root_map[r["id"]] = cur.lastrowid
                added += 1
        stats["roots_added"] = added

        # --- scans: import as new runs, preserving timestamps -------
        scan_map: dict[int, int] = {}
        scan_times: dict[int, str] = {
            r["id"]: r["start_time"] for r in dst.execute("SELECT id, start_time FROM scan_runs")
        }
        for s in src.execute("SELECT * FROM scan_runs ORDER BY id"):
            cur = dst.execute(
                "INSERT INTO scan_runs"
                " (start_time, end_time, status, application_version,"
                "  config_fingerprint, total_files, present_files,"
                "  missing_files, error_files, cache_hits, error_summary)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s["start_time"],
                    s["end_time"],
                    s["status"],
                    s["application_version"],
                    s["config_fingerprint"],
                    s["total_files"],
                    s["present_files"],
                    s["missing_files"],
                    s["error_files"],
                    s["cache_hits"],
                    s["error_summary"],
                ),
            )
            assert cur.lastrowid is not None
            scan_map[s["id"]] = cur.lastrowid
            scan_times[cur.lastrowid] = s["start_time"]
        stats["scans_imported"] = len(scan_map)

        # --- root scan states ----------------------------------------
        n = 0
        for r in src.execute("SELECT * FROM root_scan_states"):
            dst.execute(
                "INSERT INTO root_scan_states"
                " (scan_id, root_id, availability, discovery_completed,"
                "  error_message, start_time, end_time)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_map[r["scan_id"]],
                    root_map[r["root_id"]],
                    r["availability"],
                    r["discovery_completed"],
                    r["error_message"],
                    r["start_time"],
                    r["end_time"],
                ),
            )
            n += 1
        stats["root_states"] = n

        # --- files: union by (root, path_key) ------------------------
        dst_files = {
            (r["root_id"], r["normalized_path_key"]): (r["id"], r["last_seen_scan_id"])
            for r in dst.execute(
                "SELECT id, root_id, normalized_path_key, last_seen_scan_id FROM files"
            )
        }
        file_map: dict[int, int] = {}
        inserted = merged = 0
        for f in src.execute("SELECT * FROM files"):
            dst_root = root_map[f["root_id"]]
            key = (dst_root, f["normalized_path_key"])
            if key in dst_files:
                fid, dst_last_seen = dst_files[key]
                # Union presence: 'present' in either DB wins; last_seen
                # points at whichever observation is newer by timestamp
                src_seen = scan_map[f["last_seen_scan_id"]]
                newer = (
                    src_seen
                    if scan_times.get(src_seen, "") > scan_times.get(dst_last_seen, "")
                    else dst_last_seen
                )
                dst.execute(
                    "UPDATE files SET presence = CASE WHEN ? = 'present'"
                    " THEN 'present' ELSE presence END,"
                    " last_seen_scan_id = ? WHERE id = ?",
                    (f["presence"], newer, fid),
                )
                file_map[f["id"]] = fid
                merged += 1
            else:
                cur = dst.execute(
                    "INSERT INTO files"
                    " (root_id, original_path, normalized_path_key, file_name,"
                    "  extension, size, mtime_ns, presence, first_seen_scan_id,"
                    "  last_seen_scan_id, created_at, updated_at,"
                    "  is_symlink, link_target)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        dst_root,
                        f["original_path"],
                        f["normalized_path_key"],
                        f["file_name"],
                        f["extension"],
                        f["size"],
                        f["mtime_ns"],
                        f["presence"],
                        scan_map[f["first_seen_scan_id"]],
                        scan_map[f["last_seen_scan_id"]],
                        f["created_at"],
                        f["updated_at"],
                        f["is_symlink"],
                        f["link_target"],
                    ),
                )
                assert cur.lastrowid is not None
                file_map[f["id"]] = cur.lastrowid
                dst_files[key] = (cur.lastrowid, scan_map[f["last_seen_scan_id"]])
                inserted += 1
        stats["files_inserted"] = inserted
        stats["files_merged"] = merged

        # --- probe metadata: keep dst's, backfill, upgrade on ok -----
        dst_probes = {
            r["file_id"]: r["status"]
            for r in dst.execute("SELECT file_id, status FROM probe_metadata")
        }
        probe_cols = (
            "status, algorithm_version, probed_at, error_category,"
            " error_message, stderr, exit_code, container_format_name,"
            " container_format_long_name, container_duration, container_size,"
            " container_bitrate, container_start_time, has_video,"
            " video_stream_count, audio_stream_count, subtitle_stream_count,"
            " primary_video_codec, primary_video_width, primary_video_height,"
            " primary_video_fps, primary_audio_codec,"
            " primary_audio_sample_rate, primary_audio_channels"
        )
        inserted = upgraded = 0
        for p in src.execute("SELECT * FROM probe_metadata"):
            if p["file_id"] not in file_map:
                continue
            fid = file_map[p["file_id"]]
            values = tuple(p[c.strip()] for c in probe_cols.split(","))
            existing = dst_probes.get(fid)
            if existing is None:
                dst.execute(
                    f"INSERT INTO probe_metadata (file_id, {probe_cols})"
                    f" VALUES ({', '.join(['?'] * (len(values) + 1))})",
                    (fid, *values),
                )
                dst_probes[fid] = p["status"]
                inserted += 1
            elif existing != "ok" and p["status"] == "ok":
                assignments = ", ".join(f"{c.strip()} = ?" for c in probe_cols.split(","))
                dst.execute(
                    f"UPDATE probe_metadata SET {assignments} WHERE file_id = ?",
                    (*values, fid),
                )
                upgraded += 1
        stats["probes_inserted"] = inserted
        stats["probes_upgraded"] = upgraded

        # --- per-file caches: union, dst's copy wins -----------------
        cache_cols: dict[str, str] = {
            "exact_hashes": "sha256, file_size, mtime_ns, algorithm_version, computed_at",
            "visual_fingerprints": (
                "algorithm_version, frame_count, timestamps, frames,"
                " file_size, mtime_ns, computed_at"
            ),
            "content_signatures": (
                "algorithm_version, frame_count, timestamps, frames, hashes,"
                " content_key, file_size, mtime_ns, computed_at"
            ),
        }
        for table, cols in cache_cols.items():
            col_list = [c.strip() for c in cols.split(",")]
            placeholders = ", ".join(["?"] * (len(col_list) + 1))
            n = 0
            for r in src.execute(f"SELECT * FROM {table}"):
                if r["file_id"] not in file_map:
                    continue
                cur = dst.execute(
                    f"INSERT OR IGNORE INTO {table} (file_id, {cols}) VALUES ({placeholders})",
                    (file_map[r["file_id"]], *(r[c] for c in col_list)),
                )
                n += cur.rowcount
            stats[f"{table}_inserted"] = n

        # --- pair comparisons (per imported scan) --------------------
        n = 0
        for c in src.execute("SELECT * FROM pair_comparisons"):
            a, b = file_map.get(c["file_a_id"]), file_map.get(c["file_b_id"])
            if a is None or b is None:
                continue
            dst.execute(
                "INSERT INTO pair_comparisons"
                " (scan_id, file_a_id, file_b_id, algorithm_version,"
                "  classification, match_fraction, mean_frame_distance,"
                "  frames_compared, duration_difference, evidence, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_map[c["scan_id"]],
                    a,
                    b,
                    c["algorithm_version"],
                    c["classification"],
                    c["match_fraction"],
                    c["mean_frame_distance"],
                    c["frames_compared"],
                    c["duration_difference"],
                    c["evidence"],
                    c["created_at"],
                ),
            )
            n += 1
        stats["comparisons_imported"] = n

        # --- duplicate groups + members (legacy) ---------------------
        # Matching no longer writes groups; these tables only exist in
        # databases created before the pair model. Copying them keeps old
        # history intact without affecting current results.
        group_map: dict[int, int] = {}
        for g in src.execute("SELECT * FROM duplicate_groups"):
            cur = dst.execute(
                "INSERT INTO duplicate_groups (group_type, scan_id, created_at, analysis)"
                " VALUES (?, ?, ?, ?)",
                (g["group_type"], scan_map[g["scan_id"]], g["created_at"], g["analysis"]),
            )
            assert cur.lastrowid is not None
            group_map[g["id"]] = cur.lastrowid
        stats["groups_imported"] = len(group_map)
        n = 0
        for m in src.execute("SELECT * FROM duplicate_group_members"):
            if m["group_id"] in group_map and m["file_id"] in file_map:
                dst.execute(
                    "INSERT OR IGNORE INTO duplicate_group_members (group_id, file_id)"
                    " VALUES (?, ?)",
                    (group_map[m["group_id"]], file_map[m["file_id"]]),
                )
                n += 1
        stats["group_members_imported"] = n

        # --- review decisions ----------------------------------------
        n = 0
        for d in src.execute("SELECT * FROM review_decisions"):
            if d["file_id"] not in file_map:
                continue
            dst.execute(
                "INSERT OR IGNORE INTO review_decisions"
                " (file_id, scan_id, decision, decided_at) VALUES (?, ?, ?, ?)",
                (file_map[d["file_id"]], scan_map[d["scan_id"]], d["decision"], d["decided_at"]),
            )
            n += 1
        stats["decisions_imported"] = n

        # --- file actions (restore manifest references real paths) ---
        n = 0
        for a in src.execute("SELECT * FROM file_actions"):
            if a["file_id"] not in file_map:
                continue
            dst.execute(
                "INSERT INTO file_actions"
                " (file_id, scan_id, action, source_path, quarantine_path,"
                "  status, executed_at, restored_at, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_map[a["file_id"]],
                    scan_map[a["scan_id"]],
                    a["action"],
                    a["source_path"],
                    a["quarantine_path"],
                    a["status"],
                    a["executed_at"],
                    a["restored_at"],
                    a["error"],
                ),
            )
            n += 1
        stats["actions_imported"] = n

        # --- ignored pairs/groups (path-keyed, no remapping) ---------
        imported = 0
        for d in src.execute("SELECT * FROM dismissed_pairs"):
            imported += dst.execute(
                "INSERT OR IGNORE INTO dismissed_pairs (key_a, key_b, created_at) VALUES (?, ?, ?)",
                (d["key_a"], d["key_b"], d["created_at"]),
            ).rowcount
        stats["dismissed_pairs_imported"] = imported
        imported = 0
        for g in src.execute("SELECT * FROM dismissed_groups"):
            imported += dst.execute(
                "INSERT OR IGNORE INTO dismissed_groups"
                " (group_key, member_count, created_at) VALUES (?, ?, ?)",
                (g["group_key"], g["member_count"], g["created_at"]),
            ).rowcount
        stats["dismissed_groups_imported"] = imported

        dst.commit()
        return stats
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()

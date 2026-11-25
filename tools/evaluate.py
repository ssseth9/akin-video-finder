"""Evaluate perceptual matching against a labelled corpus.

Runs the real scan pipeline (discovery -> probe -> exact -> perceptual ->
analysis) on a corpus built by ``tools/build_corpus.py`` in a scratch
database, then compares predicted pair classes against the manifest
labels: confusion matrix, precision/recall at the actionable
(VERY_LIKELY+) level, and a per-transformation breakdown.

Usage:
    python tools/build_corpus.py --output eval_corpus
    python tools/evaluate.py --corpus eval_corpus

Exit code is non-zero when any hard negative is detected (false
positive at VERY_LIKELY+) or any expected match is missed entirely,
so the tool doubles as a regression gate.
"""

import argparse
import itertools
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from akin_video_finder.application.exact_match import ExactMatchService
from akin_video_finder.application.perceptual_match import PerceptualMatchService
from akin_video_finder.application.scan_service import ScanService
from akin_video_finder.configuration.models import Config, FFprobeConfig, MatchingConfig, RootConfig
from akin_video_finder.core.models import ProbeResult, RootAvailability
from akin_video_finder.core.path_utils import normalize_path_key
from akin_video_finder.metadata.ffprobe import probe_file
from akin_video_finder.persistence.repository import DatabaseRepository

RANK = {"NONE": 0, "REJECTED": 1, "POSSIBLE": 2, "VERY_LIKELY": 3, "EXACT": 4}
RANK_NAME = {v: k for k, v in RANK.items()}
NEGATIVE = "NEGATIVE"
DETECTED = {"EXACT", "VERY_LIKELY"}
EXPECTED_ORDER = ["EXACT", "VERY_LIKELY", "POSSIBLE", NEGATIVE]
PREDICTED_ORDER = ["EXACT", "VERY_LIKELY", "POSSIBLE", "REJECTED", "NONE"]


def expected_class(a: dict, b: dict) -> str:
    """Ground-truth pair class derived from manifest labels."""
    if a["exact_set"] == b["exact_set"]:
        return "EXACT"
    if a["family"] != b["family"]:
        return NEGATIVE
    return RANK_NAME[min(RANK[a["expected_vs_base"]], RANK[b["expected_vs_base"]])]


def record_probe(repo: DatabaseRepository, file_id: int, pr: ProbeResult) -> None:
    """Store a probe result — same column mapping ScanService uses."""
    meta = pr.metadata
    vs = meta.video_streams[0] if meta and meta.video_streams else None
    au = meta.audio_streams[0] if meta and meta.audio_streams else None
    repo.create_probe_metadata(
        file_id=file_id,
        status=pr.status,
        algorithm_version=pr.algorithm_version,
        error_category=pr.error_category,
        error_message=pr.error_message,
        stderr=pr.stderr,
        exit_code=pr.exit_code,
        container_format_name=meta.container.format_name if meta else None,
        container_format_long_name=meta.container.format_long_name if meta else None,
        container_duration=meta.container.duration if meta else None,
        container_size=meta.container.size if meta else None,
        container_bitrate=meta.container.bitrate if meta else None,
        container_start_time=meta.container.start_time if meta else None,
        has_video=meta.has_video if meta else False,
        video_stream_count=meta.video_stream_count if meta else 0,
        audio_stream_count=meta.audio_stream_count if meta else 0,
        subtitle_stream_count=meta.subtitle_stream_count if meta else 0,
        primary_video_codec=vs.codec_name if vs else None,
        primary_video_width=vs.width if vs else None,
        primary_video_height=vs.height if vs else None,
        primary_video_fps=vs.r_frame_rate if vs else None,
        primary_audio_codec=au.codec_name if au else None,
        primary_audio_sample_rate=au.sample_rate if au else None,
        primary_audio_channels=au.channel_count if au else None,
    )


def run_labels_direct(
    repo: DatabaseRepository,
    labels: dict,
    matching: MatchingConfig,
    ffprobe: FFprobeConfig,
) -> tuple[int, dict[str, int], int, int]:
    """Seed labelled files into the scratch DB and run the real matcher.

    Pair classification only depends on the two files' signatures and
    fingerprints, so instead of scanning whole parent dirs (or staging
    copies) we insert inventory+probe rows for exactly the labelled paths
    at their original locations, then run the exact-hash and perceptual
    services — identical code path to a real scan.

    Returns (scan_id, id_by_normcase_path, missing_count, seeded_count).
    """
    unique: dict[str, str] = {}
    for p in labels["pairs"]:
        for k in ("a", "b"):
            unique[os.path.normcase(str(Path(p[k]).resolve()))] = p[k]

    scan_id = repo.create_scan("eval-direct")
    root_ids: dict[str, int] = {}
    id_by_key: dict[str, int] = {}
    to_probe: list[tuple[str, Path, int]] = []
    missing = 0
    for key, orig in sorted(unique.items()):
        path = Path(orig)
        if not path.exists():
            missing += 1
            continue
        parent = path.parent.resolve()
        parent_key = os.path.normcase(str(parent))
        if parent_key not in root_ids:
            rid = repo.get_or_create_root(str(parent), normalize_path_key(parent))
            root_ids[parent_key] = rid
        rid = root_ids[parent_key]
        # Reruns reuse the seeded row — its cached signature/fingerprint
        # stays valid, so an interrupted eval resumes where it stopped
        existing = repo.get_file_by_key(rid, normalize_path_key(path))
        if existing and repo.get_probe_metadata(existing["id"]):
            id_by_key[key] = existing["id"]
        else:
            to_probe.append((key, path, rid))

    probes: dict[str, ProbeResult] = {}
    with ThreadPoolExecutor(max_workers=ffprobe.worker_count) as pool:
        futures = {
            pool.submit(
                probe_file,
                path,
                ffprobe_path=ffprobe.executable_path,
                timeout_seconds=ffprobe.timeout_seconds,
            ): key
            for key, path, _ in to_probe
        }
        for i, fut in enumerate(as_completed(futures), 1):
            probes[futures[fut]] = fut.result()
            if i % 100 == 0:
                print(f"  probed {i}/{len(to_probe)} labelled files", flush=True)

    for key, path, rid in to_probe:
        st = path.stat()
        fid = repo.create_file(
            root_id=rid,
            original_path=str(path),
            normalized_path_key=normalize_path_key(path),
            file_name=path.name,
            extension=path.suffix.lstrip("."),
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            scan_id=scan_id,
            is_symlink=path.is_symlink(),
        )
        record_probe(repo, fid, probes[key])
        id_by_key[key] = fid

    for rid in set(root_ids.values()):
        repo.create_root_scan_state(scan_id, rid, RootAvailability.AVAILABLE)
    repo.commit()

    exact = ExactMatchService(repo).find_exact_duplicates(scan_id)
    print(f"  exact pass: {exact.files_hashed} hashed, {exact.pairs_found} pairs", flush=True)
    per = PerceptualMatchService(repo, matching).find_perceptual_duplicates(scan_id)
    print(
        f"  perceptual pass: {per.comparisons} comparisons, "
        f"{per.very_likely} VL / {per.possible} POS / {per.rejected} REJ",
        flush=True,
    )
    return scan_id, id_by_key, missing, len(id_by_key)


def build_state(repo: DatabaseRepository, scan_id: int) -> dict:
    """Snapshot predicted pair classes + exact-set structure for a scan."""
    path_by_id = {f["id"]: f["original_path"] for f in repo.get_present_files()}
    id_by_path = {os.path.normcase(str(Path(v).resolve())): k for k, v in path_by_id.items()}

    predicted: dict[tuple[int, int], str] = {}
    evidence: dict[tuple[int, int], dict] = {}
    for row in repo.get_pair_comparisons(scan_id):
        key = tuple(sorted((row["file_a_id"], row["file_b_id"])))
        predicted[key] = row["classification"]
        evidence[key] = json.loads(row["evidence"] or "{}")

    exact_set_of: dict[int, int] = {}
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for row in repo.get_pair_comparisons(scan_id):
        if row["classification"] != "EXACT":
            continue
        a, b = row["file_a_id"], row["file_b_id"]
        parent[find(a)] = find(b)
        key = tuple(sorted((a, b)))
        predicted[key] = "EXACT"
    for file_id in list(parent):
        exact_set_of[file_id] = find(file_id)
    siblings: dict[int, list[int]] = {}
    for fid, gid in exact_set_of.items():
        siblings.setdefault(gid, []).append(fid)

    return {
        "id_by_path": id_by_path,
        "predicted": predicted,
        "evidence": evidence,
        "exact_set_of": exact_set_of,
        "siblings": siblings,
    }


def pred_ids(state: dict, ia: int | None, ib: int | None) -> tuple[str, dict]:
    """Predicted class between two file ids, inheriting across exact sets."""
    if ia is None or ib is None or ia == ib:
        return "NONE", {}
    members_a = state["siblings"].get(state["exact_set_of"].get(ia, -1), [ia])
    members_b = state["siblings"].get(state["exact_set_of"].get(ib, -1), [ib])
    best, best_ev = "NONE", {}
    for x, y in itertools.product(members_a, members_b):
        if x == y:
            continue
        key = tuple(sorted((x, y)))
        cls = state["predicted"].get(key, "NONE")
        if RANK[cls] > RANK[best]:
            best, best_ev = cls, state["evidence"].get(key, {})
    return best, best_ev


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--db", type=Path, default=None, help="scratch DB (default: temp file)")
    parser.add_argument(
        "--from-db",
        type=Path,
        default=None,
        help="copy this database to the scratch DB first — reuses cached probes, "
        "hashes, signatures, and fingerprints. Dismissed rows are cleared on the "
        "copy so ignored pairs can be re-measured",
    )
    parser.add_argument("--frame-match-max-distance", type=float, default=None)
    parser.add_argument(
        "--min-duration-ratio",
        type=float,
        default=None,
        help="duration gate for candidate pairs. Use a small value such as "
        "0.01 to evaluate every pair in the corpus, including trims and segments",
    )
    parser.add_argument("--very-likely-threshold", type=float, default=None)
    parser.add_argument("--possible-threshold", type=float, default=None)
    parser.add_argument("--very-likely-mean-distance", type=float, default=None)
    parser.add_argument("--keep-db", action="store_true", help="keep the scratch DB after the run")
    parser.add_argument(
        "--labels",
        type=Path,
        default=None,
        help="dismissed-pairs labels JSON from tools/export_dismissed.py",
    )
    parser.add_argument(
        "--extra-root",
        type=Path,
        action="append",
        default=[],
        help="extra scan root (repeatable) so labelled files are discovered",
    )
    parser.add_argument(
        "--labels-stage",
        action="store_true",
        help="scan only the labelled files, hardlinked into a per-drive "
        ".vd_eval_stage dir — pair classifications are pairwise, so this "
        "gives identical verdicts without scanning whole parent folders",
    )
    parser.add_argument(
        "--stage-copy-budget",
        type=float,
        default=8.0,
        metavar="GB",
        help="max GiB to copy for files that cannot be hardlinked "
        "(e.g. volumes rejecting CreateHardLink); beyond the budget the "
        "file's parent dir is scanned non-recursively instead",
    )
    parser.add_argument(
        "--labels-direct",
        action="store_true",
        help="evaluate labelled pairs without scanning any directories — "
        "seed inventory+probe rows for just the labelled files at their "
        "original paths and run the real exact+perceptual services",
    )
    args = parser.parse_args()
    if args.labels_direct and not args.labels:
        parser.error("--labels-direct requires --labels")
    if args.labels_direct and args.labels_stage:
        parser.error("--labels-direct and --labels-stage are mutually exclusive")

    manifest = json.loads((args.corpus / "manifest.json").read_text())
    files = [f for f in manifest["files"] if (args.corpus / f["path"]).exists()]
    print(f"{len(files)} corpus files")

    labels = json.loads(args.labels.read_text()) if args.labels else {"pairs": []}

    label_dirs: list[str] = []
    staged: dict[str, str] = {}
    if args.labels and args.labels_stage:
        # Hardlink each labelled file into <drive>:\.vd_eval_stage — only
        # the labelled files get scanned, extracted, and compared
        unique: dict[str, str] = {}
        for p in labels["pairs"]:
            for k in ("a", "b"):
                key = os.path.normcase(str(Path(p[k]).resolve()))
                unique[key] = p[k]
        missing = 0
        copy_budget = int(args.stage_copy_budget * 1024**3)
        # Hardlinks must share the source volume; copies can go anywhere,
        # so they land on the local drive — a mounted/encrypted volume may
        # refuse CreateHardLink AND stall on writes
        copy_dir = Path(__file__).resolve().parent.parent / ".eval_stage"
        fallback_dirs: set[str] = set()
        for i, (key, orig) in enumerate(sorted(unique.items())):
            src = Path(orig)
            if not src.exists():
                missing += 1
                continue
            drive = (src.drive or src.anchor[:2]).upper()
            stage_dir = Path(drive + "\\.vd_eval_stage")
            stage_dir.mkdir(exist_ok=True)
            link = stage_dir / f"{i:05d}_{src.name}"
            copy = copy_dir / f"{i:05d}_{src.name}"
            target = link if link.exists() else copy
            if target.exists() and target.stat().st_size != src.stat().st_size:
                target.unlink()  # partial copy from an interrupted run
            if link.exists():
                pass
            elif copy.exists():
                link = copy
            else:
                try:
                    os.link(src, link)
                except OSError:
                    try:
                        if src.stat().st_size > copy_budget:
                            raise OSError("over copy budget")
                        copy_dir.mkdir(exist_ok=True)
                        shutil.copy2(src, copy)
                        copy_budget -= src.stat().st_size
                        link = copy
                    except OSError:
                        # Not linkable/copyable — scan its parent dir instead
                        copy.unlink(missing_ok=True)
                        fallback_dirs.add(str(src.parent.resolve()))
                        continue
            staged[key] = str(link.resolve())
            d = os.path.normcase(str(link.parent))
            if d not in label_dirs:
                label_dirs.append(d)
        label_dirs += sorted(d for d in fallback_dirs if os.path.normcase(d) not in label_dirs)
        print(
            f"staged {len(staged)} labelled files, {missing} missing, "
            f"{len(fallback_dirs)} parent dirs scanned for un-linkable files"
        )
    elif args.labels and not args.labels_direct:
        # Each labelled file's parent dir is added as a non-recursive root
        # so dismissed pairs get scanned without crawling whole drives.
        # Deduped case-insensitively — mixed-case paths would resolve to
        # the same configured root and trip a UNIQUE constraint
        label_dirs = sorted(
            {
                os.path.normcase(str(Path(p[k]).resolve().parent))
                for p in labels["pairs"]
                for k in ("a", "b")
            }
        )

    overrides = {
        "frame_match_max_distance": args.frame_match_max_distance,
        "min_duration_ratio": args.min_duration_ratio,
        "very_likely_threshold": args.very_likely_threshold,
        "possible_threshold": args.possible_threshold,
        "very_likely_mean_distance": args.very_likely_mean_distance,
    }
    matching = MatchingConfig(
        ffmpeg_path=args.ffmpeg,
        **{k: v for k, v in overrides.items() if v is not None},
    )
    config = Config(
        roots=[
            RootConfig(path=str(args.corpus.resolve()), recursive=True),
            *[RootConfig(path=str(r.resolve()), recursive=True) for r in args.extra_root],
            *[RootConfig(path=d, recursive=False) for d in label_dirs],
        ],
        matching=matching,
        ffprobe=FFprobeConfig(executable_path=args.ffprobe),
    )

    tmp = tempfile.TemporaryDirectory()
    db_path = args.db or Path(tmp.name) / "eval.db"
    if args.from_db is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Backup API, not a file copy — the source may be WAL-mode and
        # open in the app, where committed data still lives in -wal
        src = sqlite3.connect(f"file:{args.from_db}?mode=ro", uri=True)
        dst = sqlite3.connect(db_path)
        src.backup(dst)
        dst.close()
        src.close()
    with DatabaseRepository(db_path) as repo:
        if args.from_db is not None:
            # Cleared on the scratch copy only — the labelled run must
            # re-measure pairs the live DB suppresses
            repo.conn.execute("DELETE FROM dismissed_pairs")
            repo.conn.execute("DELETE FROM dismissed_groups")
            repo.commit()
        result = ScanService(config, repo).scan()
        print(
            f"scan: {result['present_files']} files, {result['comparisons']} comparisons, "
            f"{result['very_likely']} VL / {result['possible']} POS / {result['rejected']} REJ, "
            f"{result['exact_pairs']} exact pairs"
        )

        # Snapshot before any direct-labels pass: its exact-match phase
        # deletes and rebuilds exact groups
        corpus_state = build_state(repo, result["scan_id"])
        label_state = corpus_state
        if args.labels is not None and args.labels_direct:
            direct_scan, direct_ids, n_missing, n_seeded = run_labels_direct(
                repo, labels, matching, config.ffprobe
            )
            print(
                f"labels-direct: seeded {n_seeded} files ({n_missing} missing) "
                f"under scan {direct_scan}",
                flush=True,
            )
            label_state = build_state(repo, direct_scan)

    def pred(a: dict, b: dict) -> tuple[str, dict]:
        return pred_ids(
            corpus_state,
            corpus_state["id_by_path"].get(
                os.path.normcase(str((args.corpus / a["path"]).resolve()))
            ),
            corpus_state["id_by_path"].get(
                os.path.normcase(str((args.corpus / b["path"]).resolve()))
            ),
        )

    # --- Per-file view: every file vs its family base ---
    print("\n== Per-transformation results vs family base ==")
    failures: list[str] = []
    by_family: dict[str, list[dict]] = {}
    for f in files:
        by_family.setdefault(f["family"], []).append(f)
    for fam, members in by_family.items():
        base = next((m for m in members if m["transform"] == "base"), None)
        if base is None or len(members) < 2:
            continue
        for m in members:
            if m is base:
                continue
            cls, ev = pred(m, base)
            exp = "EXACT" if m["exact_set"] == base["exact_set"] else m["expected_vs_base"]
            ok = RANK[cls] >= RANK[exp]
            mark = "" if ok else "  <-- MISS"
            print(
                f"  {fam}/{m['transform']:<12} expected {exp:<11} got {cls:<11}"
                f"  frac={ev.get('match_fraction', 0):.2f}"
                f" dist={ev.get('mean_frame_distance', 0):.3f}{mark}"
            )
            if not ok:
                failures.append(f"{fam}/{m['transform']}: expected {exp}, got {cls}")

    # --- Pair-level confusion matrix over all corpus pairs ---
    matrix: Counter = Counter()
    pair_rows: list[tuple[dict, dict, str, str]] = []
    for a, b in itertools.combinations(files, 2):
        exp = expected_class(a, b)
        got, _ = pred(a, b)
        matrix[(exp, got)] += 1
        pair_rows.append((a, b, exp, got))

    print("\n== Confusion matrix (expected x predicted) ==")
    header = "expected\\pred " + "".join(f"{c:>12}" for c in PREDICTED_ORDER)
    print(header)
    for exp in EXPECTED_ORDER:
        row = f"{exp:<13}" + "".join(f"{matrix[(exp, c)]:>12}" for c in PREDICTED_ORDER)
        print(row)

    # --- Actionable precision/recall (detected = EXACT | VERY_LIKELY) ---
    # A POSSIBLE pair reported as POSSIBLE is correct, not a miss: it is
    # shown in the review list for a human decision. So "recall" here is
    # actionable recall — the share of positives that came out strong
    # enough to act on without further scrutiny. Class-level recall (every
    # positive reported at or above its expected class) is printed below.
    tp = sum(1 for _, _, e, g in pair_rows if e != NEGATIVE and g in DETECTED)
    fp = sum(1 for _, _, e, g in pair_rows if e == NEGATIVE and g in DETECTED)
    fn = sum(1 for _, _, e, g in pair_rows if e != NEGATIVE and g not in DETECTED)
    tn = sum(1 for _, _, e, g in pair_rows if e == NEGATIVE and g not in DETECTED)
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    print("\n== Actionable detection (EXACT + VERY_LIKELY) ==")
    print(f"  precision {prec:.3f}  actionable recall {rec:.3f}  (tp={tp} fp={fp} fn={fn} tn={tn})")
    print(f"  {fn} positive pair(s) reported as POSSIBLE for review, not missed")

    positives = sum(1 for _, _, e, _ in pair_rows if e != NEGATIVE)
    at_or_above = sum(1 for _, _, e, g in pair_rows if e != NEGATIVE and RANK[g] >= RANK[e])
    class_recall = at_or_above / positives if positives else 1.0
    print("\n== Class-level recall ==")
    print(
        f"  {class_recall:.3f}  ({at_or_above}/{positives} positive pairs at "
        "or above their expected class)"
    )

    fp_rows = [(a, b, g) for a, b, e, g in pair_rows if e == NEGATIVE and g in DETECTED]
    fn_rows = [(a, b, e, g) for a, b, e, g in pair_rows if e != NEGATIVE and RANK[g] < RANK[e]]
    if fp_rows:
        print("\n  FALSE POSITIVES (hard negatives detected):")
        for a, b, g in fp_rows:
            print(f"    {a['path']} <-> {b['path']}: {g}")
    if fn_rows:
        print("\n  MISSES (below expected class):")
        for a, b, e, g in fn_rows:
            print(f"    {a['path']} <-> {b['path']}: expected {e}, got {g}")

    # --- Dismissed-pair labels: real-world decisions from the app ---
    dismissed_fp: list[tuple[str, str, str]] = []
    if args.labels is not None:
        skipped: list[str] = []
        evaluated = 0
        print("\n== Dismissed-pair labels (expected class per labels file) ==")

        def _label_id(path_str: str) -> int | None:
            key = os.path.normcase(str(Path(path_str).resolve()))
            scan_path = staged.get(key, key)
            return label_state["id_by_path"].get(os.path.normcase(scan_path))

        for pair in labels["pairs"]:
            ia = _label_id(pair["a"])
            ib = _label_id(pair["b"])
            if ia is None or ib is None:
                skipped.append(pair["a"])
                continue
            evaluated += 1
            exp = pair.get("expected", NEGATIVE)
            got, ev = pred_ids(label_state, ia, ib)
            # A NEGATIVE pair passes only below POSSIBLE — anything higher
            # puts it back in the app's review list
            ok = RANK[got] < RANK["POSSIBLE"] if exp == NEGATIVE else RANK[got] >= RANK[exp]
            mark = "" if ok else ("  <-- RESURFACED" if exp == NEGATIVE else "  <-- MISS")
            print(
                f"  {Path(pair['a']).name} <-> {Path(pair['b']).name}: "
                f"expected {exp:<11} got {got:<11}"
                f"  frac={ev.get('match_fraction', 0):.2f}"
                f" dist={ev.get('mean_frame_distance', 0):.3f}{mark}"
            )
            if not ok:
                dismissed_fp.append((pair["a"], pair["b"], got))
        print(
            f"  {evaluated} pairs evaluated, {len(dismissed_fp)} below "
            f"expected, {len(skipped)} skipped "
            "(file missing or on an unavailable drive)"
        )

    if not args.keep_db:
        tmp.cleanup()
    else:
        print(f"\ndb kept: {db_path}")

    print()
    if fp_rows or fn_rows or failures or dismissed_fp:
        print(
            f"FAIL: {len(fp_rows)} false positives, "
            f"{len(fn_rows) + len(failures)} misses, "
            f"{len(dismissed_fp)} dismissed pairs resurfaced"
        )
        return 1
    print("PASS: all pairs classified at or above expectation")
    return 0


if __name__ == "__main__":
    sys.exit(main())

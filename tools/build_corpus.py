"""Build a labelled evaluation corpus for perceptual matching.

Generates a small set of synthetic base videos with FFmpeg, derives a
transformation matrix from each (remux, re-encode, resize, trim, ...),
and writes a manifest labelling every file with its family and the
minimum match class it should reach against the family base. Pairs
across families are hard negatives; byte copies share an ``exact_set``
so the evaluator can require EXACT.

Usage:
    python tools/build_corpus.py --output eval_corpus
"""

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BASE_DURATION = 30
BASE_SIZE = "640x360"
BASE_RATE = 25
ENCODE = ["-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac"]


@dataclass(frozen=True)
class Transform:
    """A derived file: ffmpeg args (after -i base) + expected class vs base."""

    name: str
    args: list[str]
    expected: str
    ext: str = ".mp4"


@dataclass(frozen=True)
class Family:
    """A base source plus its transformation set."""

    name: str
    source: str
    audio_hz: int
    transforms: tuple[Transform, ...]


# Full transformation matrix for the primary family; reduced sets for the
# secondary families keep corpus build time reasonable while still
# exercising cross-family negatives.
FULL_MATRIX: tuple[Transform, ...] = (
    Transform("remux", ["-c", "copy"], "VERY_LIKELY", ".mkv"),
    Transform("reencode", ["-c:v", "libx265", "-crf", "28", "-c:a", "copy"], "VERY_LIKELY"),
    Transform("resize", ["-vf", "scale=320:180", *ENCODE], "VERY_LIKELY"),
    Transform("fps", ["-vf", "fps=12", *ENCODE], "VERY_LIKELY"),
    Transform("lowbitrate", ["-b:v", "150k", "-c:a", "copy"], "VERY_LIKELY"),
    Transform("brightness", ["-vf", "eq=brightness=0.08", *ENCODE], "VERY_LIKELY"),
    Transform("letterbox", ["-vf", "pad=640:440:0:40:black", *ENCODE], "POSSIBLE"),
    # A content crop has no black bars, so cropdetect cannot normalize it.
    # The coarse layout path that used to catch it was removed as chance-level,
    # so a content crop is a negative in the corpus. See context/matching-quality.md.
    Transform("crop", ["-vf", "crop=608:342:16:9", *ENCODE], "NONE"),
    Transform(
        "watermark",
        ["-vf", "drawbox=x=500:y=20:w=120:h=40:c=white:t=fill", *ENCODE],
        "POSSIBLE",
    ),
    Transform("trim_head", ["-ss", "4", *ENCODE], "VERY_LIKELY"),
    Transform("trim_tail", ["-t", "26", *ENCODE], "VERY_LIKELY"),
    # 18s of the 30s base: long coverage ~0.6 < trim_long_coverage_min
    Transform("trim_both", ["-ss", "6", "-t", "18", *ENCODE], "POSSIBLE"),
    # 12s middle clip: a partial overlap, not a full duplicate
    Transform("segment", ["-ss", "10", "-t", "12", *ENCODE], "POSSIBLE"),
)

MEDIUM_MATRIX: tuple[Transform, ...] = (
    Transform("reencode", ["-c:v", "libx265", "-crf", "28", "-c:a", "copy"], "VERY_LIKELY"),
    Transform("resize", ["-vf", "scale=320:180", *ENCODE], "VERY_LIKELY"),
    Transform("letterbox", ["-vf", "pad=640:440:0:40:black", *ENCODE], "POSSIBLE"),
    Transform("trim_head", ["-ss", "4", *ENCODE], "VERY_LIKELY"),
    Transform("segment", ["-ss", "10", "-t", "12", *ENCODE], "POSSIBLE"),
)

LIGHT_MATRIX: tuple[Transform, ...] = (
    Transform("reencode", ["-c:v", "libx265", "-crf", "28", "-c:a", "copy"], "VERY_LIKELY"),
    Transform("trim_both", ["-ss", "6", "-t", "18", *ENCODE], "POSSIBLE"),
)

FAMILIES: tuple[Family, ...] = (
    Family(
        "a",
        f"testsrc2=duration={BASE_DURATION}:size={BASE_SIZE}:rate={BASE_RATE}",
        440,
        FULL_MATRIX,
    ),
    Family("b", f"mandelbrot=size={BASE_SIZE}:rate={BASE_RATE}", 550, MEDIUM_MATRIX),
    Family("c", f"life=size={BASE_SIZE}:rate={BASE_RATE}", 660, LIGHT_MATRIX),
)

# Each negative is its own family: every pair involving it must stay undetected
NEGATIVES: tuple[Family, ...] = (
    Family("neg_bars", f"smptehdbars=size={BASE_SIZE}:rate={BASE_RATE}", 330, ()),
    Family("neg_black", f"color=c=black:size={BASE_SIZE}:rate={BASE_RATE}", 220, ()),
    Family(
        "neg_noise",
        f"nullsrc=size={BASE_SIZE}:rate={BASE_RATE},noise=alls=60:allf=t+u",
        880,
        (),
    ),
    Family("neg_testsrc", f"testsrc=size={BASE_SIZE}:rate={BASE_RATE}", 500, ()),
)


def _run_ffmpeg(args: list[str], ffmpeg: str) -> None:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-y", *args],
        capture_output=True,
        timeout=300,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        raise RuntimeError(f"ffmpeg failed: {' '.join(args)}\n{stderr}")


def _render_base(family: Family, base_path: Path, ffmpeg: str) -> None:
    source = family.source
    timed = "duration=" in source
    video_input = source if timed else f"{source},format=yuv420p"
    args = ["-f", "lavfi", "-i", video_input]
    args += ["-f", "lavfi", "-i", f"sine=frequency={family.audio_hz}:duration={BASE_DURATION}"]
    if not timed:
        args += ["-t", str(BASE_DURATION)]
    args += ["-shortest", "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    args += [str(base_path)]
    _run_ffmpeg(args, ffmpeg)


def build(output_dir: Path, ffmpeg: str) -> list[dict]:
    """Generate the corpus and return manifest file entries."""
    output_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []

    for family in (*FAMILIES, *NEGATIVES):
        fam_dir = output_dir / f"family_{family.name}"
        fam_dir.mkdir(exist_ok=True)
        base_path = fam_dir / "base.mp4"
        print(f"[{family.name}] base <- {family.source}")
        _render_base(family, base_path, ffmpeg)
        entries.append(
            {
                "path": str(base_path.relative_to(output_dir)),
                "family": family.name,
                "transform": "base",
                "expected_vs_base": "VERY_LIKELY",
                "exact_set": f"{family.name}/base",
            }
        )

        if not family.transforms:
            continue

        # Byte copy: member of the same exact_set, EXACT vs the base
        copy_path = fam_dir / "copy.mp4"
        shutil.copyfile(base_path, copy_path)
        entries.append(
            {
                "path": str(copy_path.relative_to(output_dir)),
                "family": family.name,
                "transform": "copy",
                "expected_vs_base": "EXACT",
                "exact_set": f"{family.name}/base",
            }
        )

        for t in family.transforms:
            out_path = fam_dir / f"{t.name}{t.ext}"
            print(f"[{family.name}] {t.name} (expect {t.expected})")
            _run_ffmpeg(["-i", str(base_path), *t.args, str(out_path)], ffmpeg)
            entries.append(
                {
                    "path": str(out_path.relative_to(output_dir)),
                    "family": family.name,
                    "transform": t.name,
                    "expected_vs_base": t.expected,
                    "exact_set": f"{family.name}/{t.name}",
                }
            )

    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval_corpus"))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    entries = build(args.output, args.ffmpeg)
    manifest = {
        "version": 1,
        "base_duration_seconds": BASE_DURATION,
        "files": entries,
    }
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\n{len(entries)} files -> {args.output}")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

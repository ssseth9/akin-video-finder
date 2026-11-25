"""Main CLI entry point for Akin Video Finder."""

import logging
import sys
from pathlib import Path

import typer

from akin_video_finder import __version__
from akin_video_finder.application.maintenance import CleanupService
from akin_video_finder.application.scan_service import ScanService
from akin_video_finder.configuration.loader import (
    ConfigValidationError,
    generate_example_config,
    load_config,
)
from akin_video_finder.core.models import FilePresence, ProgressEvent, ScanStatus
from akin_video_finder.persistence.repository import DatabaseRepository

app = typer.Typer(
    name="akin-video-finder",
    help="Akin Video Finder - A Windows-native tool for finding duplicate videos",
    add_completion=False,
)

logger = logging.getLogger(__name__)


def _err(message: str) -> None:
    """Print an error to stderr AND record it in the log file."""
    typer.echo(message, err=True)
    logger.error(message)


def _make_console_safe() -> None:
    """Keep output alive when a path holds characters the code page cannot encode.

    A scan's error summary lists the paths that failed, and a path may hold
    Cyrillic or CJK characters. A Windows console handle is already UTF-8,
    but a redirected stream is encoded with the locale code page (cp1252
    here), so ``typer.echo`` raises ``UnicodeEncodeError`` and the command
    dies mid-output — the report it was printing is lost over one
    unprintable character. A redirected stream is switched to UTF-8 to match
    the reports; a console handle only gains ``errors="replace"`` so a stray
    character cannot kill it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def setup_logging(level: str, file_path: str | None = None) -> None:
    """Setup logging configuration.

    Args:
        level: Logging level.
        file_path: Optional log file path.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if file_path:
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        handlers.append(file_handler)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )


@app.command()
def doctor(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to configuration file",
        exists=True,
    ),
) -> None:
    """Validate configuration and dependencies."""
    typer.echo(f"Akin Video Finder v{__version__}")
    typer.echo("Running diagnostics...")
    typer.echo()

    # Load configuration
    try:
        config = load_config(config_path)
        typer.echo("[OK] Configuration file is valid")
    except FileNotFoundError:
        _err(f"[ERROR] Configuration file not found: {config_path}")
        raise typer.Exit(code=1) from None
    except ConfigValidationError as e:
        _err(f"[ERROR] {e}")
        raise typer.Exit(code=1) from None

    # Check FFprobe
    import shutil

    ffprobe_path = config.ffprobe.executable_path
    if shutil.which(ffprobe_path):
        typer.echo(f"[OK] FFprobe found: {ffprobe_path}")
    else:
        _err(f"[ERROR] FFprobe not found: {ffprobe_path}")
        _err("  Install with: winget install --id Gyan.FFmpeg -e")
    # Check FFmpeg (optional for this milestone)
    ffmpeg_path = "ffmpeg"
    if shutil.which(ffmpeg_path):
        typer.echo(f"[OK] FFmpeg found: {ffmpeg_path} (optional)")
    else:
        typer.echo(f"[WARN] FFmpeg not found: {ffmpeg_path} (optional for this milestone)")

    # Check database parent directory
    db_path = Path(config.database.path)
    db_parent = db_path.parent
    if db_parent.exists():
        typer.echo(f"[OK] Database parent directory exists: {db_parent}")
        # Check write access
        test_file = db_parent / ".write_test"
        try:
            test_file.touch()
            test_file.unlink()
            typer.echo("[OK] Database parent directory is writable")
        except OSError:
            _err(f"[ERROR] Database parent directory is not writable: {db_parent}")
    else:
        typer.echo(f"[WARN] Database parent directory does not exist: {db_parent}")
        typer.echo("  It will be created on first scan")

    # Check report directory
    report_dir = Path(config.reporting.output_directory)
    if report_dir.exists():
        typer.echo(f"[OK] Report directory exists: {report_dir}")
        test_file = report_dir / ".write_test"
        try:
            test_file.touch()
            test_file.unlink()
            typer.echo("[OK] Report directory is writable")
        except OSError:
            _err(f"[ERROR] Report directory is not writable: {report_dir}")
    else:
        typer.echo(f"[WARN] Report directory does not exist: {report_dir}")
        typer.echo("  It will be created on first export")

    # Check roots
    typer.echo()
    typer.echo("Configured roots:")
    for i, root in enumerate(config.roots, 1):
        root_path = Path(root.path)
        if root_path.exists():
            if root_path.is_dir():
                typer.echo(f"  {i}. [OK] {root.path} (available)")
            else:
                _err(f"  {i}. [ERROR] {root.path} (not a directory)")
        else:
            typer.echo(f"  {i}. [WARN] {root.path} (unavailable)")

    typer.echo()
    typer.echo("Diagnostics complete.")


@app.command()
def init_config(
    output_path: Path = typer.Option(
        Path("config.toml"),
        "--output",
        "-o",
        help="Output path for configuration file",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing configuration file",
    ),
) -> None:
    """Generate an example configuration file."""
    if output_path.exists() and not force:
        typer.echo(f"Configuration file already exists: {output_path}")
        typer.echo("Use --force to overwrite")
        raise typer.Exit(code=1)

    example = generate_example_config()
    output_path.write_text(example, encoding="utf-8")
    typer.echo(f"Example configuration written to: {output_path}")
    typer.echo("Edit the file to configure your scan roots and settings")


@app.command()
def scan(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to configuration file",
        exists=True,
    ),
) -> None:
    """Scan configured roots for video files."""
    # Load configuration
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ConfigValidationError) as e:
        _err(f"Configuration error: {e}")
        raise typer.Exit(code=1) from None

    if not config.roots:
        # A scan with nothing to walk still creates a scan row and reports
        # zero files, which reads as "your library is empty" rather than
        # "you have not chosen a library". Say which one it is.
        _err(f"No scan roots configured — add [[roots]] to {config_path}")
        raise typer.Exit(code=1) from None

    # Setup logging
    setup_logging(
        config.logging.level,
        # No configured file = log next to the database so errors persist
        config.logging.file_path or str(Path(config.database.path).with_suffix(".log")),
    )

    # Initialize repository
    db_path = Path(config.database.path)
    repository = DatabaseRepository(db_path)

    # Progress callback
    def progress_callback(event: ProgressEvent) -> None:
        if event.phase == "completed" or event.phase == "error":
            total = event.total_count or "?"
            typer.echo(f"[{event.phase}] {event.completed_count}/{total} - {event.message}")

    # Create scan service
    scan_service = ScanService(config, repository, progress_callback)

    # Handle Ctrl+C
    def handle_interrupt() -> None:
        typer.echo("\nInterrupting scan...")
        scan_service.cancel()
        typer.echo("Waiting for in-flight operations to complete...")

    import signal

    if sys.platform != "win32":
        signal.signal(signal.SIGINT, lambda sig, frame: handle_interrupt())

    try:
        # Run scan
        result = scan_service.scan()

        # Print summary
        typer.echo()
        typer.echo("Scan Summary:")
        typer.echo(f"  Status: {result['status']}")
        typer.echo(f"  Total files: {result['total_files']}")
        typer.echo(f"  Present: {result['present_files']}")
        typer.echo(f"  Missing: {result['missing_files']}")
        typer.echo(f"  Probe errors: {result['error_files']}")
        typer.echo(f"  Extraction errors: {len(result['errors'])}")
        typer.echo(f"  Cache hits: {result['cache_hits']}")
        typer.echo(f"  Files hashed: {result['files_hashed']}")
        typer.echo(f"  Hash cache hits: {result['hash_cache_hits']}")
        typer.echo(f"  Exact duplicate pairs: {result['exact_pairs']}")
        typer.echo(f"  Files signed: {result['files_signed']}")
        typer.echo(f"  Signature cache hits: {result['signature_cache_hits']}")
        typer.echo(f"  Signature-shared pairs: {result['signature_pairs']}")
        typer.echo(f"  Files fingerprinted: {result['files_fingerprinted']}")
        typer.echo(f"  Fingerprint cache hits: {result['fingerprint_cache_hits']}")
        typer.echo(f"  Candidate pairs compared: {result['comparisons']}")
        # A pair the duration gate dropped was never compared, so it cannot
        # be counted among the comparisons — and a scan that quietly compares
        # half of what the indexes proposed has to say so, or "fewer pairs
        # than last time" reads as a regression.
        pruned = result.get("duration_pruned", 0)
        if pruned:
            pct = config.matching.min_duration_ratio * 100
            typer.echo(
                f"  Pairs dropped by duration: {pruned} "
                f"(shorter copy under {pct:.0f}% of the longer one)"
            )
        typer.echo(f"  VERY_LIKELY pairs: {result['very_likely']}")
        typer.echo(f"  POSSIBLE pairs: {result['possible']}")
        # The offset probe is the one phase that decodes 180s from both files
        # of a pair, so a headless run reports it: how many candidates the
        # filters left for it, and how many of those an earlier scan had
        # already judged
        probes = result.get("offset_probes", 0)
        cached = result.get("offset_probes_cached", 0)
        rechecked = result.get("offset_probes_reconfirmed", 0)
        if probes or cached:
            candidates = result.get("offset_probes_candidates", 0) or probes + cached
            known = f" ({cached} already known)" if cached else ""
            typer.echo(f"  Offset probes: {probes} of {candidates} candidate pairs{known}")
            if rechecked:
                typer.echo(f"  Offset shifts re-checked: {rechecked}")
            typer.echo(f"  Offset shifts found: {result.get('offset_probe_hits', 0)}")

        pairs = repository.get_pair_comparisons(result["scan_id"])
        actionable = [
            c for c in pairs if c["classification"] in ("EXACT", "VERY_LIKELY", "POSSIBLE")
        ]
        if actionable:
            files = {
                f["id"]: f
                for root in repository.get_all_roots()
                for f in repository.get_files_for_root(root["id"])
            }
            typer.echo()
            typer.echo("Duplicate pairs:")
            for comparison in actionable:
                left = files.get(comparison["file_a_id"], {}).get("original_path", "?")
                right = files.get(comparison["file_b_id"], {}).get("original_path", "?")
                typer.echo(f"  {comparison['classification']}: {left}")
                typer.echo(f"    {right}")

        if result["errors"]:
            typer.echo()
            typer.echo("Extraction errors:")
            for error in result["errors"][:10]:  # Show first 10 errors
                typer.echo(f"  - {error}")
            if len(result["errors"]) > 10:
                typer.echo(f"  ... and {len(result['errors']) - 10} more")

        repository.close()

        if result["status"] == ScanStatus.COMPLETED.value:
            return
        elif result["status"] == ScanStatus.INTERRUPTED.value:
            raise typer.Exit(code=130)
        else:
            raise typer.Exit(code=1)

    except KeyboardInterrupt:
        handle_interrupt()
        repository.close()
        raise typer.Exit(code=130) from None
    except Exception as e:
        _err(f"Scan failed: {e}")
        repository.close()
        raise typer.Exit(code=1) from None


@app.command()
def status(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to configuration file",
        exists=True,
    ),
) -> None:
    """Show the status of the most recent scan."""
    # Load configuration
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ConfigValidationError) as e:
        _err(f"Configuration error: {e}")
        raise typer.Exit(code=1) from None

    # Initialize repository
    db_path = Path(config.database.path)
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}")
        typer.echo("Run a scan first to create the database")
        raise typer.Exit(code=1)

    repository = DatabaseRepository(db_path)

    try:
        # Get latest scan
        latest_scan = repository.get_latest_scan()
        if not latest_scan:
            typer.echo("No scans found in database")
            return

        # Print scan status
        typer.echo(f"Akin Video Finder v{__version__}")
        typer.echo()
        typer.echo("Most Recent Scan:")
        typer.echo(f"  Scan ID: {latest_scan['id']}")
        typer.echo(f"  Status: {latest_scan['status']}")
        typer.echo(f"  Start time: {latest_scan['start_time']}")
        typer.echo(f"  End time: {latest_scan['end_time'] or 'In progress'}")
        typer.echo(f"  Total files: {latest_scan['total_files']}")
        typer.echo(f"  Present: {latest_scan['present_files']}")
        typer.echo(f"  Missing: {latest_scan['missing_files']}")
        typer.echo(f"  Probe errors: {latest_scan['error_files']}")
        typer.echo(f"  Cache hits: {latest_scan['cache_hits']}")

        if latest_scan["error_summary"]:
            typer.echo()
            typer.echo(f"Extraction errors: {latest_scan['error_summary']}")

        # Get root states
        typer.echo()
        typer.echo("Root States:")
        roots = repository.get_all_roots()
        for root in roots:
            root_state = repository.get_root_scan_state(latest_scan["id"], root["id"])
            if root_state:
                availability = root_state["availability"]
                typer.echo(f"  {root['original_path']}: {availability}")
                if root_state["error_message"]:
                    typer.echo(f"    Error: {root_state['error_message']}")

    except Exception as e:
        _err(f"Error reading status: {e}")
        raise typer.Exit(code=1) from None
    finally:
        repository.close()


@app.command()
def cleanup(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to configuration file",
        exists=True,
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Delete the rows instead of only listing what would go",
    ),
) -> None:
    """Remove inventory rows whose file is no longer on disk.

    Every stored path is checked against the filesystem as it is now. A root
    that is not available is skipped whole, so an unplugged drive keeps its
    inventory, and a row the app quarantined or deleted is kept because its
    action record is what restore needs.
    """
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ConfigValidationError) as e:
        _err(f"Configuration error: {e}")
        raise typer.Exit(code=1) from None

    db_path = Path(config.database.path)
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}")
        typer.echo("Run a scan first to create the database")
        raise typer.Exit(code=1)

    repository = DatabaseRepository(db_path)
    try:
        service = CleanupService(repository)
        plan = service.plan()

        typer.echo(f"Akin Video Finder v{__version__}")
        typer.echo()
        flagged = sum(
            1 for candidate in plan.candidates if candidate.presence == FilePresence.MISSING.value
        )
        typer.echo(f"Checked {plan.examined:,} stored paths")
        typer.echo(f"  on disk:            {plan.on_disk:,}")
        typer.echo(f"  kept, app acted:    {plan.app_action:,}")
        typer.echo(f"  to remove:          {plan.deletable:,} ({flagged:,} already marked missing)")
        by_root: dict[str, int] = {}
        for candidate in plan.candidates:
            by_root[candidate.root] = by_root.get(candidate.root, 0) + 1
        for root, count in sorted(by_root.items()):
            typer.echo(f"    {count:>6,}  {root}")
        for root in plan.unavailable_roots:
            typer.echo(f"  skipped, not there: {root}")
        typer.echo()

        if not plan.candidates:
            typer.echo("Nothing to clean up.")
            repository.close()
            return

        if not apply:
            for candidate in plan.candidates[:10]:
                typer.echo(f"  {candidate.path}")
            if plan.deletable > 10:
                typer.echo(f"  ... and {plan.deletable - 10:,} more")
            typer.echo()
            typer.echo("Nothing was deleted. Re-run with --apply to remove these rows.")
            repository.close()
            return

        deleted = service.apply(plan)
        typer.echo(f"Removed {deleted:,} rows. Their cached probes, hashes,")
        typer.echo("signatures and pair history went with them; no file was touched.")
        repository.close()

    except Exception as e:
        _err(f"Error cleaning up: {e}")
        repository.close()
        raise typer.Exit(code=1) from None


@app.command()
def report(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to configuration file",
        exists=True,
    ),
    format: str = typer.Option(
        "json",
        "--format",
        "-f",
        help="Report format: json or csv",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path (default: stdout)",
    ),
) -> None:
    """Export inventory report."""
    if format not in ("json", "csv"):
        _err(f"Invalid format: {format}")
        typer.echo("Supported formats: json, csv")
        raise typer.Exit(code=1)

    # Load configuration
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ConfigValidationError) as e:
        _err(f"Configuration error: {e}")
        raise typer.Exit(code=1) from None

    # Initialize repository
    db_path = Path(config.database.path)
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}")
        typer.echo("Run a scan first to create the database")
        raise typer.Exit(code=1)

    repository = DatabaseRepository(db_path)

    try:
        # Get latest scan
        latest_scan = repository.get_latest_scan()
        if not latest_scan:
            typer.echo("No scans found in database")
            raise typer.Exit(code=0)

        # Export report
        from akin_video_finder.reporting.exporter import export_report

        report_content = export_report(repository, latest_scan["id"], format, config)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(report_content, encoding="utf-8")
            typer.echo(f"Report exported to: {output_path}")
        else:
            typer.echo(report_content)

        repository.close()

    except Exception as e:
        _err(f"Error exporting report: {e}")
        repository.close()
        raise typer.Exit(code=1) from None


@app.command()
def gui(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to configuration file",
        exists=True,
    ),
) -> None:
    """Launch the graphical review and scan-control application."""
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ConfigValidationError) as e:
        _err(f"Configuration error: {e}")
        raise typer.Exit(code=1) from None

    # Without this the root logger stays at WARNING and worker diagnostics
    # (sheet render timings etc.) never reach the console
    setup_logging(
        config.logging.level,
        # No configured file = log next to the database so errors persist
        config.logging.file_path or str(Path(config.database.path).with_suffix(".log")),
    )

    try:
        from akin_video_finder.gui.app import run_gui
    except ImportError:
        _err("GUI requires PySide6: pip install pyside6")
        raise typer.Exit(code=1) from None

    raise typer.Exit(code=run_gui(config, config_path))


@app.command()
def merge_db(
    source: Path = typer.Option(
        ...,
        "--source",
        "-s",
        help="Database to merge from (left unchanged)",
        exists=True,
    ),
    target: Path = typer.Option(
        ...,
        "--target",
        "-t",
        help="Database to merge into",
        exists=True,
    ),
    no_backup: bool = typer.Option(
        False,
        "--no-backup",
        help="Skip automatic backups of both databases",
    ),
) -> None:
    """Merge another database's scans, cache, and review state into one.

    Files deduplicate on normalized path; per-file caches union;
    scans import as new runs with original timestamps. Both databases
    are backed up (*.bak-<timestamp>) unless --no-backup is given.
    """
    from akin_video_finder.maintenance.db_merge import backup_database, merge_databases

    if not no_backup:
        for path in (source, target):
            bak = backup_database(path)
            typer.echo(f"Backed up {path} -> {bak.name}")

    try:
        stats = merge_databases(source, target)
    except Exception as e:
        _err(f"Merge failed (target rolled back): {e}")
        raise typer.Exit(code=1) from None

    typer.echo("Merge complete:")
    for key, value in stats.items():
        typer.echo(f"  {key}: {value}")


def main() -> None:
    """Main entry point."""
    _make_console_safe()
    app()


if __name__ == "__main__":
    main()

"""Tests for the status command's empty-database path."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from akin_video_finder.cli.main import app
from akin_video_finder.persistence.repository import DatabaseRepository


def test_status_on_empty_database_exits_zero(tmp_path: Path) -> None:
    """An existing database with no scans is a normal empty state, not an error."""
    db_path = tmp_path / "empty.db"
    repository = DatabaseRepository(db_path)
    assert repository.get_latest_scan() is None
    repository.close()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[database]\npath = '" + db_path.as_posix() + "'\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "No scans found in database" in result.output

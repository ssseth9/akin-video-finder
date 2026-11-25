"""Dismissed rows store keys; the dialog still needs the original paths."""

from pathlib import Path

from akin_video_finder.persistence.repository import DatabaseRepository


def test_display_paths_keep_the_original_spelling(tmp_path: Path) -> None:
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root = tmp_path / "Library"
    root.mkdir()
    root_id = repo.get_or_create_root(str(root), str(root).lower())
    video = root / "Film.MP4"
    video.write_bytes(b"x")
    repo.create_file(
        root_id,
        str(video),
        str(video).lower(),
        video.name,
        ".mp4",
        1,
        1000,
        scan_id,
    )
    repo.commit()
    key = str(video).lower()
    display = repo.get_display_paths_for_keys([key, "C:/missing.mp4"])
    assert display[key] == str(video)
    assert "C:/missing.mp4" not in display
    repo.close()

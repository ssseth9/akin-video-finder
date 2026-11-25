"""An ignored pair must stay ignored in every copy of the library.

The same two videos often live in more than one root — a library and a
mirror of it. Ignoring the pair in one copy recorded two paths, so the
mirror's copy of the very same pair came back as a fresh candidate and had
to be ignored all over again, which is what the reviewer was seeing.
"""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402

STAMPS = json.dumps([0.0, 10.0, 20.0])
HASHES_A = bytes(range(96))
HASHES_B = bytes(range(96, 192))


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _add_file(
    repo: DatabaseRepository, path: Path, hashes: bytes, scan_id: int, root_id: int
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    file_id = repo.create_file(
        root_id, str(path), str(path).lower(), path.name, path.suffix, 1, 1000, scan_id
    )
    repo.upsert_signature(file_id, "ahash8-v3", STAMPS, b"frames", hashes, 1, 1000)
    return file_id


def _key(path: Path) -> str:
    return str(path).lower()


@pytest.fixture()
def library(tmp_path, qapp):
    """Two roots holding the same two videos, each pair already compared."""
    library_root = tmp_path / "library"
    mirror_root = tmp_path / "mirror"
    cfg = Config(roots=[RootConfig(path=str(library_root)), RootConfig(path=str(mirror_root))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))

    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    library_id = repo.get_or_create_root(str(library_root), _key(library_root))
    mirror_id = repo.get_or_create_root(str(mirror_root), _key(mirror_root))
    pairs = {}
    for name, hashes in (("a.mp4", HASHES_A), ("b.mp4", HASHES_B)):
        pairs[f"library_{name}"] = _add_file(repo, library_root / name, hashes, scan_id, library_id)
        pairs[f"mirror_{name}"] = _add_file(repo, mirror_root / name, hashes, scan_id, mirror_id)
    # The matcher found the pair in both copies, so the review holds two rows
    repo.create_pair_comparison(
        scan_id,
        pairs["library_a.mp4"],
        pairs["library_b.mp4"],
        "v1",
        "POSSIBLE",
        0.63,
        0.21,
        12,
    )
    repo.create_pair_comparison(
        scan_id, pairs["mirror_a.mp4"], pairs["mirror_b.mp4"], "v1", "POSSIBLE", 0.63, 0.21, 12
    )
    repo.commit()

    created = []

    def _open():
        win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
        win.reload_data()
        created.append(win)
        return win

    yield _open, repo, pairs, library_root, mirror_root

    for win in created:
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
    repo.close()


def test_both_copies_are_up_for_review_before_anything_is_ignored(library) -> None:
    _open, _repo, _pairs, _library_root, _mirror_root = library
    win = _open()
    assert len(win.all_pairs) == 2


def test_ignoring_one_copy_hides_the_other(library) -> None:
    """The heart of the report: ignore the library pair, the mirror's goes too."""
    _open, repo, pairs, library_root, mirror_root = library

    repo.dismiss_pair(_key(library_root / "a.mp4"), _key(library_root / "b.mp4"))
    repo.commit()

    win = _open()
    assert (win.all_pairs) == []
    # Only one path pair was ever dismissed…
    assert win.dismissed == {(_key(library_root / "a.mp4"), _key(library_root / "b.mp4"))}
    # …and the mirror's paths are not among them
    assert (_key(mirror_root / "a.mp4"), _key(mirror_root / "b.mp4")) not in win.dismissed
    # The decision is carried by the content instead
    assert len(win.dismissed_content) == 1
    assert len(win.content_keys) == 4


def test_a_different_video_is_not_swept_up(library) -> None:
    """Content keys compare videos, so an unrelated pair still shows."""
    _open, repo, pairs, library_root, _mirror_root = library
    # Replace the mirror's "b" signature with something else entirely
    conn = repo.conn
    conn.execute(
        "UPDATE content_signatures SET hashes = ?, content_key = 'unrelated0000000'"
        " WHERE file_id = ?",
        (bytes(range(1, 97)), pairs["mirror_b.mp4"]),
    )
    repo.commit()

    repo.dismiss_pair(_key(library_root / "a.mp4"), _key(library_root / "b.mp4"))
    repo.commit()

    win = _open()
    assert len(win.all_pairs) == 1
    assert win.all_pairs[0]["file_a_id"] in (pairs["mirror_a.mp4"], pairs["mirror_b.mp4"])


def test_the_review_updates_without_a_reload(library) -> None:
    """A dismissal retires the pair on the spot, not one refresh later."""
    _open, repo, pairs, _library_root, _mirror_root = library
    win = _open()
    comparison = next(c for c in win.all_pairs if c["file_a_id"] == pairs["library_a.mp4"])
    mirror = next(c for c in win.all_pairs if c["file_a_id"] == pairs["mirror_a.mp4"])

    repo.dismiss_pair(
        win.files[comparison["file_a_id"]]["normalized_path_key"],
        win.files[comparison["file_b_id"]]["normalized_path_key"],
    )
    repo.commit()
    win.note_pair_dismissed(comparison)

    assert win._pair_dismissed(comparison)
    # The mirror's copy follows immediately, before any reload reads the row:
    # its content keys were looked up when the dismissal was recorded
    assert win._pair_dismissed(mirror)


def test_unignoring_the_last_dismissal_brings_both_copies_back(library) -> None:
    """Withdrawing the only dismissal behind a content pair restores every copy."""
    _open, repo, _pairs, library_root, _mirror_root = library
    key_a, key_b = _key(library_root / "a.mp4"), _key(library_root / "b.mp4")
    repo.dismiss_pair(key_a, key_b)
    repo.commit()
    win = _open()
    assert win.all_pairs == []

    content = win.content_pair_for_paths(key_a, key_b)
    assert content is not None
    repo.undismiss_pair(key_a, key_b)
    repo.commit()
    win.withdraw_dismissal(key_a, key_b, content)

    # Both roots again, from one withdrawal
    assert len(win.all_pairs) == 2
    assert win.dismissed == set()
    assert win.dismissed_content == set()


def test_a_second_dismissal_of_the_same_contents_still_holds(library) -> None:
    """Two dismissed paths can share one content pair; withdrawing one keeps the other."""
    _open, repo, _pairs, library_root, mirror_root = library
    key_a, key_b = _key(library_root / "a.mp4"), _key(library_root / "b.mp4")
    mirror_a, mirror_b = _key(mirror_root / "a.mp4"), _key(mirror_root / "b.mp4")
    repo.dismiss_pair(key_a, key_b)
    repo.dismiss_pair(mirror_a, mirror_b)
    repo.commit()
    win = _open()
    assert win.all_pairs == []

    content = win.content_pair_for_paths(key_a, key_b)
    repo.undismiss_pair(key_a, key_b)
    repo.commit()
    win.withdraw_dismissal(key_a, key_b, content)

    # The mirror's dismissal still stands behind the same two contents, so
    # the library's copy stays hidden too — withdrawing too eagerly would
    # bring back a pair the reviewer deliberately ignored
    assert win.all_pairs == []
    assert win.dismissed == {(mirror_a, mirror_b)}
    assert win.dismissed_content == {content}


def test_unignoring_does_not_re_read_the_dismissal_sets(library, monkeypatch) -> None:
    """The point of the withdrawal: no re-derivation of every ignored pair."""
    _open, repo, _pairs, library_root, _mirror_root = library
    key_a, key_b = _key(library_root / "a.mp4"), _key(library_root / "b.mp4")
    repo.dismiss_pair(key_a, key_b)
    repo.commit()
    win = _open()
    content = win.content_pair_for_paths(key_a, key_b)
    repo.undismiss_pair(key_a, key_b)
    repo.commit()

    def _re_read() -> None:
        raise AssertionError("un-ignoring re-read a whole dismissal set")

    monkeypatch.setattr(repo, "get_dismissed_pairs", _re_read)
    monkeypatch.setattr(repo, "get_dismissed_content_pairs", _re_read)

    win.withdraw_dismissal(key_a, key_b, content)
    assert len(win.all_pairs) == 2


def test_a_file_without_a_signature_is_asked_about_once(library, monkeypatch) -> None:
    """Signature-less files used to be re-queried for every pair, every reload.

    The lookup omits them instead of returning a placeholder, so a "is it in
    the map?" test never settles — the negative result has to be remembered.
    """
    _open, repo, pairs, _library_root, mirror_root = library
    # One dismissal, so the content lookups are switched on at all
    repo.dismiss_pair(_key(mirror_root / "a.mp4"), _key(mirror_root / "b.mp4"))
    repo.conn.execute("DELETE FROM content_signatures WHERE file_id = ?", (pairs["library_b.mp4"],))
    repo.commit()

    asked: list[list[int]] = []
    real = repo.get_content_keys

    def counted(file_ids):
        asked.append(list(file_ids))
        return real(file_ids)

    monkeypatch.setattr(repo, "get_content_keys", counted)
    win = _open()
    assert win.dismissed_content
    after_reload = len(asked)

    comparison = next(
        c for c in win._candidates if pairs["library_b.mp4"] in (c["file_a_id"], c["file_b_id"])
    )
    for _ in range(3):
        assert not win._pair_dismissed(comparison)
    assert len(asked) == after_reload

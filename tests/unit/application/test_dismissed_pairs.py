"""Unit tests for persistent false-positive pair dismissal."""

import json
import sqlite3
from pathlib import Path

import pytest

from akin_video_finder.persistence.repository import DatabaseRepository
from akin_video_finder.persistence.schema import SCHEMA_VERSION, _apply_migration


@pytest.fixture
def repo(tmp_path: Path):
    repository = DatabaseRepository(tmp_path / "test.db")
    yield repository
    repository.close()


class TestDismissedPairs:
    def test_dismiss_and_query_roundtrip(self, repo: DatabaseRepository) -> None:
        repo.dismiss_pair("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")
        repo.commit()

        assert repo.get_dismissed_pairs() == {("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")}

    def test_pair_order_is_canonical(self, repo: DatabaseRepository) -> None:
        # Dismissing (b, a) must store and match the same pair as (a, b)
        repo.dismiss_pair("e:\\vids\\b.mp4", "e:\\vids\\a.mp4")
        repo.commit()

        dismissed = repo.get_dismissed_pairs()
        assert dismissed == {("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")}

        # Re-dismissing in either order stays a single row
        repo.dismiss_pair("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")
        repo.commit()
        assert repo.get_dismissed_pairs() == {("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")}

    def test_undismiss_pair(self, repo: DatabaseRepository) -> None:
        repo.dismiss_pair("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")
        repo.commit()
        repo.undismiss_pair("e:\\vids\\b.mp4", "e:\\vids\\a.mp4")
        repo.commit()

        assert repo.get_dismissed_pairs() == set()

    def test_dismissal_survives_reopen(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        repo.dismiss_pair("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")
        repo.commit()
        repo.close()

        reopened = DatabaseRepository(tmp_path / "test.db")
        try:
            assert reopened.get_dismissed_pairs() == {("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")}
        finally:
            reopened.close()

    def test_schema_version_includes_dismissed_table(self, repo: DatabaseRepository) -> None:
        version = repo.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        table = repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dismissed_pairs'"
        ).fetchone()
        assert table is not None

    def test_dismissal_does_not_touch_decisions_or_actions(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x")
        repo.create_file(
            root_id=root_id,
            original_path=str(video),
            normalized_path_key=str(video).lower(),
            file_name=video.name,
            extension=video.suffix,
            size=1,
            mtime_ns=0,
            scan_id=scan_id,
        )
        repo.dismiss_pair(str(video).lower(), str(tmp_path / "other.mp4").lower())
        repo.commit()

        assert repo.get_decisions(scan_id) == {}
        assert repo.get_actions() == []
        # The file row itself is untouched
        assert repo.get_file_by_key(root_id, str(video).lower()) is not None


class TestBulkDismissPairs:
    """Tests for ``dismiss_pairs``, the bulk path behind "Dismiss all shown"."""

    def _row_count(self, repo: DatabaseRepository) -> int:
        cursor = repo.conn.execute("SELECT COUNT(*) FROM dismissed_pairs")
        return cursor.fetchone()[0]

    def test_bulk_dismiss_and_query_roundtrip(self, repo: DatabaseRepository) -> None:
        written = repo.dismiss_pairs(
            [
                ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
                ("e:\\vids\\c.mp4", "e:\\vids\\d.mp4"),
                ("e:\\vids\\e.mp4", "e:\\vids\\f.mp4"),
            ]
        )
        repo.commit()

        assert written == 3
        assert repo.get_dismissed_pairs() == {
            ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
            ("e:\\vids\\c.mp4", "e:\\vids\\d.mp4"),
            ("e:\\vids\\e.mp4", "e:\\vids\\f.mp4"),
        }

    def test_bulk_dismiss_canonicalizes_order(self, repo: DatabaseRepository) -> None:
        # Reversed input order must land in the same sorted row as dismiss_pair
        repo.dismiss_pairs([("e:\\vids\\b.mp4", "e:\\vids\\a.mp4")])
        repo.commit()

        assert repo.get_dismissed_pairs() == {("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")}

    def test_bulk_dismiss_collapses_duplicate_input(self, repo: DatabaseRepository) -> None:
        written = repo.dismiss_pairs(
            [
                ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
                ("e:\\vids\\b.mp4", "e:\\vids\\a.mp4"),
                ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
            ]
        )
        repo.commit()

        assert written == 1
        assert repo.get_dismissed_pairs() == {("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")}
        assert self._row_count(repo) == 1

    def test_bulk_dismiss_empty_input_is_noop(self, repo: DatabaseRepository) -> None:
        repo.dismiss_pair("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")
        repo.commit()

        assert repo.dismiss_pairs([]) == 0
        repo.commit()

        assert repo.get_dismissed_pairs() == {("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")}

    def test_bulk_dismiss_accepts_generator(self, repo: DatabaseRepository) -> None:
        # The signature takes any Iterable, not just a list
        pairs = [
            ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
            ("e:\\vids\\c.mp4", "e:\\vids\\d.mp4"),
        ]
        written = repo.dismiss_pairs(pair for pair in pairs)
        repo.commit()

        assert written == 2
        assert len(repo.get_dismissed_pairs()) == 2

    def test_bulk_dismiss_does_not_duplicate_existing_pair(self, repo: DatabaseRepository) -> None:
        repo.dismiss_pair("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")
        repo.commit()

        repo.dismiss_pairs(
            [
                ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
                ("e:\\vids\\c.mp4", "e:\\vids\\d.mp4"),
            ]
        )
        repo.commit()

        assert len(repo.get_dismissed_pairs()) == 2
        assert self._row_count(repo) == 2

    def test_bulk_dismissal_survives_reopen(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        repo.dismiss_pairs(
            [
                ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
                ("e:\\vids\\c.mp4", "e:\\vids\\d.mp4"),
            ]
        )
        repo.commit()
        repo.close()

        reopened = DatabaseRepository(tmp_path / "test.db")
        try:
            assert len(reopened.get_dismissed_pairs()) == 2
        finally:
            reopened.close()

    def test_bulk_dismissed_pair_can_be_undismissed(self, repo: DatabaseRepository) -> None:
        repo.dismiss_pairs([("e:\\vids\\a.mp4", "e:\\vids\\b.mp4")])
        repo.commit()

        repo.undismiss_pair("e:\\vids\\b.mp4", "e:\\vids\\a.mp4")
        repo.commit()

        assert repo.get_dismissed_pairs() == set()

    def test_bulk_dismissed_pairs_appear_in_dismissed_items(self, repo: DatabaseRepository) -> None:
        repo.dismiss_pairs(
            [
                ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
                ("e:\\vids\\c.mp4", "e:\\vids\\d.mp4"),
            ]
        )
        repo.commit()

        items = repo.get_dismissed_items()
        assert len(items) == 2
        assert all(item["kind"] == "pair" for item in items)
        assert {tuple(item["member_keys"]) for item in items} == {
            ("e:\\vids\\a.mp4", "e:\\vids\\b.mp4"),
            ("e:\\vids\\c.mp4", "e:\\vids\\d.mp4"),
        }


STAMPS = json.dumps([0.0, 10.0, 20.0])
HASHES_A = bytes(range(96))
HASHES_B = bytes(range(96, 192))


def _add_file(repo: DatabaseRepository, path: Path, hashes: bytes | None, scan_id: int) -> int:
    """Create a file row at ``path``, with a signature unless ``hashes`` is None."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    root_id = repo.get_or_create_root(str(path.parent), str(path.parent).lower())
    file_id = repo.create_file(
        root_id, str(path), str(path).lower(), path.name, path.suffix, 1, 1000, scan_id
    )
    if hashes is not None:
        repo.upsert_signature(file_id, "ahash8-v3", STAMPS, b"frames", hashes, 1, 1000)
    return file_id


class TestContentDismissal:
    """A decision made in one copy of a library holds for the other copies.

    The same two videos here exist under two roots — a library and a mirror
    of it — with their own paths and file rows. Only one copy was ever
    ignored, and the mirror used to come back as a brand-new pair because
    the dismissal named paths rather than the videos.
    """

    def _seed(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        scan_id = repo.create_scan("0.1.0")
        for root in ("library", "mirror"):
            _add_file(repo, tmp_path / root / "a.mp4", HASHES_A, scan_id)
            _add_file(repo, tmp_path / root / "b.mp4", HASHES_B, scan_id)
        repo.commit()

    def _ids(self, repo: DatabaseRepository) -> dict[str, int]:
        return {
            row["normalized_path_key"]: row["id"]
            for row in repo.conn.execute("SELECT id, normalized_path_key FROM files")
        }

    def _key(self, path: Path) -> str:
        return str(path).lower()

    def test_copies_share_a_key_and_videos_do_not(self, repo, tmp_path) -> None:
        self._seed(repo, tmp_path)
        ids = self._ids(repo)
        keys = repo.get_content_keys(ids.values())

        library_a = keys[ids[self._key(tmp_path / "library" / "a.mp4")]]
        mirror_a = keys[ids[self._key(tmp_path / "mirror" / "a.mp4")]]
        library_b = keys[ids[self._key(tmp_path / "library" / "b.mp4")]]
        assert library_a == mirror_a
        assert library_a != library_b

    def _content_pair(self, repo: DatabaseRepository, tmp_path: Path, root: str) -> tuple[str, str]:
        ids = self._ids(repo)
        keys = repo.get_content_keys(ids.values())
        return tuple(  # type: ignore[return-value]
            sorted(
                (
                    keys[ids[self._key(tmp_path / root / "a.mp4")]],
                    keys[ids[self._key(tmp_path / root / "b.mp4")]],
                )
            )
        )

    def test_a_dismissal_names_the_content_as_well_as_the_paths(self, repo, tmp_path) -> None:
        self._seed(repo, tmp_path)
        content_pair = self._content_pair(repo, tmp_path, "library")

        repo.dismiss_pair(
            self._key(tmp_path / "library" / "a.mp4"), self._key(tmp_path / "library" / "b.mp4")
        )
        repo.commit()

        assert repo.get_dismissed_content_pairs() == {content_pair}

    def test_the_same_pair_in_the_mirror_is_covered(self, repo, tmp_path) -> None:
        self._seed(repo, tmp_path)
        library_pair = self._content_pair(repo, tmp_path, "library")
        mirror_pair = self._content_pair(repo, tmp_path, "mirror")
        assert library_pair == mirror_pair  # same videos, so same content pair

        repo.dismiss_pair(
            self._key(tmp_path / "library" / "a.mp4"), self._key(tmp_path / "library" / "b.mp4")
        )
        repo.commit()

        assert mirror_pair in repo.get_dismissed_content_pairs()
        # ... while the mirror's own paths were never dismissed
        mirror_paths = tuple(
            sorted(
                (self._key(tmp_path / "mirror" / "a.mp4"), self._key(tmp_path / "mirror" / "b.mp4"))
            )
        )
        assert mirror_paths not in repo.get_dismissed_pairs()

    def test_un_ignoring_withdraws_the_content_pair(self, repo, tmp_path) -> None:
        self._seed(repo, tmp_path)
        path_a = self._key(tmp_path / "library" / "a.mp4")
        path_b = self._key(tmp_path / "library" / "b.mp4")

        repo.dismiss_pair(path_a, path_b)
        repo.commit()
        assert repo.get_dismissed_content_pairs()

        repo.undismiss_pair(path_a, path_b)
        repo.commit()
        assert repo.get_dismissed_content_pairs() == set()

    def test_an_unsigned_pair_has_no_content_pair(self, repo, tmp_path) -> None:
        # Nothing identifies the content yet — the pairing is skipped rather
        # than guessed from paths or sizes
        scan_id = repo.create_scan("0.1.0")
        _add_file(repo, tmp_path / "library" / "a.mp4", HASHES_A, scan_id)
        _add_file(repo, tmp_path / "library" / "b.mp4", None, scan_id)
        repo.commit()

        repo.dismiss_pair(
            self._key(tmp_path / "library" / "a.mp4"), self._key(tmp_path / "library" / "b.mp4")
        )
        repo.commit()

        assert repo.get_dismissed_pairs()  # the dismissal itself stands
        assert repo.get_dismissed_content_pairs() == set()

    def test_the_ignored_list_stays_one_row_per_path_pair(self, repo, tmp_path) -> None:
        """The dialog lists what the reviewer ignored, and names files.

        A decision now also covers the same videos in another root, but that
        coverage is derived — it was never an ignore of its own, and it must
        not surface as a row of content keys where the dialog shows names.
        """
        self._seed(repo, tmp_path)
        path_a = self._key(tmp_path / "library" / "a.mp4")
        path_b = self._key(tmp_path / "library" / "b.mp4")

        repo.dismiss_pair(path_a, path_b)
        repo.commit()

        items = repo.get_dismissed_items()
        assert len(items) == 1  # the mirror is covered, not listed
        assert len(repo.get_dismissed_content_pairs()) == 1
        assert items[0]["kind"] == "pair"
        assert items[0]["member_keys"] == [path_a, path_b]
        assert [Path(key).name for key in items[0]["member_keys"]] == ["a.mp4", "b.mp4"]

    def test_signatures_carry_the_key(self, repo, tmp_path) -> None:
        version = repo.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        columns = [row[1] for row in repo.conn.execute("PRAGMA table_info(content_signatures)")]
        assert "content_key" in columns

        scan_id = repo.create_scan("0.1.0")
        file_id = _add_file(repo, tmp_path / "library" / "a.mp4", HASHES_A, scan_id)
        repo.commit()

        signature = repo.get_signature(file_id)
        assert signature is not None
        assert repo.get_content_keys([file_id]) == {file_id: signature["content_key"]}


def test_migration_and_version_row_roll_back_together(tmp_path: Path) -> None:
    """A failed migration must not leave its DDL committed without a version."""
    db = sqlite3.connect(tmp_path / "migration.db")
    db.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, "
        "applied_at TEXT, description TEXT)"
    )
    db.execute("CREATE TABLE t (x INTEGER)")
    db.commit()

    class FailingMigration:
        description = "adds a column then fails"

        def apply(self, conn: sqlite3.Connection) -> None:
            conn.execute("ALTER TABLE t ADD COLUMN y INTEGER")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _apply_migration(db, 1, FailingMigration())

    columns = [row[1] for row in db.execute("PRAGMA table_info(t)")]
    assert "y" not in columns
    assert db.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0
    db.close()

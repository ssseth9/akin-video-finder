"""Unit tests for the inventory cleanup."""

import os
from pathlib import Path

import pytest

from akin_video_finder.application import maintenance
from akin_video_finder.application.maintenance import CleanupService
from akin_video_finder.persistence.repository import DatabaseRepository


@pytest.fixture
def repo(tmp_path: Path):
    repository = DatabaseRepository(tmp_path / "test.db")
    yield repository
    repository.close()


def _add_file(
    repo: DatabaseRepository,
    scan_id: int,
    root_id: int,
    path: Path,
    *,
    missing: bool = False,
) -> int:
    stat = path.stat()
    file_id = repo.create_file(
        root_id=root_id,
        original_path=str(path),
        normalized_path_key=str(path).lower(),
        file_name=path.name,
        extension=path.suffix,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        scan_id=scan_id,
    )
    if missing:
        repo.mark_file_missing(file_id, scan_id)
    repo.commit()
    return file_id


def _scan_and_root(repo: DatabaseRepository, tmp_path: Path) -> tuple[int, int]:
    scan_id = repo.create_scan("0.1.0")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    repo.commit()
    return scan_id, root_id


class TestPlan:
    """What the plan decides to delete, and what it refuses to."""

    def test_deletes_the_row_of_a_file_that_is_gone(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        scan_id, root_id = _scan_and_root(repo, tmp_path)
        gone = tmp_path / "gone.mp4"
        gone.write_bytes(b"x" * 10)
        file_id = _add_file(repo, scan_id, root_id, gone, missing=True)
        present = tmp_path / "here.mp4"
        present.write_bytes(b"y" * 10)
        _add_file(repo, scan_id, root_id, present)
        gone.unlink()

        plan = CleanupService(repo).plan()

        assert [candidate.file_id for candidate in plan.candidates] == [file_id]
        assert plan.on_disk == 1
        assert plan.examined == 2

    def test_keeps_a_row_the_app_acted_on(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        scan_id, root_id = _scan_and_root(repo, tmp_path)
        quarantined = tmp_path / "moved.mp4"
        quarantined.write_bytes(b"x" * 10)
        file_id = _add_file(repo, scan_id, root_id, quarantined, missing=True)
        repo.record_action(
            file_id,
            scan_id,
            "quarantine",
            str(quarantined),
            str(tmp_path / "quarantine" / "moved.mp4"),
        )
        repo.commit()
        quarantined.unlink()

        plan = CleanupService(repo).plan()

        assert not plan.candidates
        assert plan.app_action == 1

    def test_keeps_a_row_the_file_merely_hides_from_stat(
        self, repo: DatabaseRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, root_id = _scan_and_root(repo, tmp_path)
        locked = tmp_path / "locked.mp4"
        locked.write_bytes(b"x" * 10)
        file_id = _add_file(repo, scan_id, root_id, locked, missing=True)

        class _Denied:
            """Stand-in for the os module whose stat refuses to answer."""

            path = os.path

            @staticmethod
            def stat(path: str) -> os.stat_result:
                raise PermissionError(13, "Access is denied", path)

        monkeypatch.setattr(maintenance, "os", _Denied)

        plan = CleanupService(repo).plan()

        assert not plan.candidates
        assert plan.on_disk == 1
        assert repo.get_file_by_key(root_id, str(locked).lower())["id"] == file_id

    def test_skips_a_root_that_is_not_there(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        drive = tmp_path / "unplugged"
        drive.mkdir()
        clip = drive / "clip.mp4"
        clip.write_bytes(b"x" * 4)
        scan_id, root_id = _scan_and_root(repo, drive)
        _add_file(repo, scan_id, root_id, clip, missing=True)
        # The drive goes away; every row under it now looks absent
        clip.unlink()
        drive.rmdir()

        plan = CleanupService(repo).plan()

        assert not plan.candidates
        assert plan.unavailable_roots == [str(drive)]
        assert plan.examined == 0
        assert repo.get_file_by_key(root_id, str(clip).lower()) is not None


class TestApply:
    """What the deletion takes with it."""

    def test_pair_table_is_indexed_for_the_cascade(self, repo: DatabaseRepository) -> None:
        names = {
            row[0]
            for row in repo.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'pair_comparisons'"
            )
        }
        assert {"idx_pair_comparisons_file_a", "idx_pair_comparisons_file_b"} <= names

    def test_removes_the_row_and_its_pairs(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        scan_id, root_id = _scan_and_root(repo, tmp_path)
        first = tmp_path / "a.mp4"
        first.write_bytes(b"x" * 10)
        second = tmp_path / "b.mp4"
        second.write_bytes(b"y" * 10)
        first_id = _add_file(repo, scan_id, root_id, first, missing=True)
        second_id = _add_file(repo, scan_id, root_id, second)
        repo.create_pair_comparison(
            scan_id,
            first_id,
            second_id,
            "test-v1",
            "POSSIBLE",
            0.9,
            0.1,
            10,
        )
        repo.commit()
        first.unlink()

        service = CleanupService(repo)
        deleted = service.apply(service.plan())

        assert deleted == 1
        assert not repo.get_pair_comparisons(scan_id)
        assert repo.get_file_by_key(root_id, str(second).lower()) is not None

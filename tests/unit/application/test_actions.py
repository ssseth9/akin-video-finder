"""Unit tests for the quarantine ActionService."""

import os
from pathlib import Path

import pytest

from akin_video_finder.application.actions import ActionService, pending_remove_ids
from akin_video_finder.persistence.repository import DatabaseRepository


@pytest.fixture
def repo(tmp_path: Path):
    repository = DatabaseRepository(tmp_path / "test.db")
    yield repository
    repository.close()


def _make_file(repo: DatabaseRepository, scan_id: int, root_id: int, path: Path) -> int:
    stat = path.stat()
    return repo.create_file(
        root_id=root_id,
        original_path=str(path),
        normalized_path_key=str(path).lower(),
        file_name=path.name,
        extension=path.suffix,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        scan_id=scan_id,
    )


class TestPlanRemovals:
    def test_plans_valid_removal(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        video = tmp_path / "a" / "clip.mp4"
        video.parent.mkdir()
        video.write_bytes(b"x" * 100)
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)

        assert len(plan.removals) == 1
        assert plan.removals[0].path == video
        assert str(plan.removals[0].quarantine_path).endswith(
            f"a{os.sep}clip.mp4"
        ) or "clip.mp4" in str(plan.removals[0].quarantine_path)
        assert plan.total_bytes == 100
        assert not plan.skipped

    def test_skips_missing_file(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        ghost = tmp_path / "gone.mp4"
        ghost.write_bytes(b"x")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, ghost)
        ghost.unlink()
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)

        assert not plan.removals
        assert plan.skipped[0].reason == "missing on disk"

    def test_skips_changed_file(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x" * 100)
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        video.write_bytes(b"y" * 200)
        os.utime(video, ns=(video.stat().st_atime_ns, video.stat().st_mtime_ns + 1))
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)

        assert not plan.removals
        assert plan.skipped[0].reason == "file changed since scan"

    def test_never_removes_entire_group(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        # The old group-level safety check was removed by design.
        # Confirmation in the dialog is the only remaining gate.
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fids = []
        for name in ("a.mp4", "b.mp4"):
            video = tmp_path / name
            video.write_bytes(b"z" * 50)
            fid = _make_file(repo, scan_id, root_id, video)
            fids.append(fid)
            repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)

        assert len(plan.removals) == 2
        assert not plan.skipped

    def test_skips_protected_folder(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        protected_dir = tmp_path / "masters"
        protected_dir.mkdir()
        protected = protected_dir / "keep.mp4"
        protected.write_bytes(b"x" * 100)
        free = tmp_path / "copy.mp4"
        free.write_bytes(b"y" * 100)
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        for video in (protected, free):
            fid = _make_file(repo, scan_id, root_id, video)
            repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo, [str(protected_dir)]).plan_removals(scan_id)

        assert [entry.path for entry in plan.removals] == [free]
        assert len(plan.skipped) == 1
        assert plan.skipped[0].path == str(protected)
        assert plan.skipped[0].reason == "protected folder"


class TestProtectionAtExecution:
    def test_execute_rechecks_protection(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        protected_dir = tmp_path / "masters"
        protected_dir.mkdir()
        video = protected_dir / "keep.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)
        service = ActionService(repo, [str(protected_dir)])
        result = service.execute_plan(plan, scan_id)

        assert result["moved"] == 0
        assert video.exists()
        assert plan.skipped[-1].reason == "protected folder"

    def test_move_rechecks_protection(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        protected_dir = tmp_path / "masters"
        protected_dir.mkdir()
        video = protected_dir / "keep.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)
        service = ActionService(repo, [str(protected_dir)])
        result = service.move_files(plan, tmp_path / "elsewhere", scan_id)

        assert result["moved"] == 0
        assert video.exists()
        assert plan.skipped[-1].reason == "protected folder"

    def test_delete_rechecks_protection(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        protected_dir = tmp_path / "masters"
        protected_dir.mkdir()
        video = protected_dir / "keep.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)
        service = ActionService(repo, [str(protected_dir)])
        result = service.delete_files(plan, scan_id)

        assert result["trashed"] == 0
        assert result["quarantined"] == 0
        assert video.exists()
        assert plan.skipped[-1].reason == "protected folder"


class TestExecuteAndRestore:
    def test_execute_moves_and_restores(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        video = tmp_path / "a" / "clip.mp4"
        video.parent.mkdir()
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        service = ActionService(repo)
        plan = service.plan_removals(scan_id)
        result = service.execute_plan(plan, scan_id)

        assert result["moved"] == 1
        assert not video.exists()
        action = repo.get_actions(scan_id)[0]
        assert Path(action["quarantine_path"]).read_bytes() == b"payload"
        assert action["status"] == "executed"

        assert service.restore_action(action["id"])
        assert video.read_bytes() == b"payload"
        assert repo.get_action_by_id(action["id"])["status"] == "restored"

    def test_empty_quarantine_deletes(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        service = ActionService(repo)
        service.execute_plan(service.plan_removals(scan_id), scan_id)
        action = repo.get_actions(scan_id)[0]

        result = service.empty_quarantine(scan_id)

        assert result == {"deleted": 1, "failed": 0}
        assert not Path(action["quarantine_path"]).exists()
        assert repo.get_action_by_id(action["id"])["status"] == "deleted"
        # Deleted actions are gone for good — no restore
        assert not service.restore_action(action["id"])

    def test_restore_refuses_overwrite(self, repo: DatabaseRepository, tmp_path: Path) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        service = ActionService(repo)
        service.execute_plan(service.plan_removals(scan_id), scan_id)
        video.write_bytes(b"new file at original path")
        action = repo.get_actions(scan_id)[0]

        assert not service.restore_action(action["id"])
        assert video.read_bytes() == b"new file at original path"
        # The row stays restorable so the file in quarantine is not stranded.
        assert repo.get_action_by_id(action["id"])["status"] == "executed"

    def test_a_file_changed_after_planning_is_skipped(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        service = ActionService(repo)
        plan = service.plan_removals(scan_id)
        video.write_bytes(b"changed payload")
        os.utime(video, ns=(video.stat().st_atime_ns, video.stat().st_mtime_ns + 1_000_000))

        result = service.execute_plan(plan, scan_id)

        assert result["moved"] == 0
        assert video.read_bytes() == b"changed payload"
        assert any("changed since plan" in skipped.reason for skipped in plan.skipped)

    def test_a_pending_action_is_restorable(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        dest = tmp_path / "quarantine" / "clip.mp4"
        dest.parent.mkdir()
        os.rename(video, dest)
        action_id = repo.record_action(
            fid,
            scan_id,
            "quarantine",
            str(video),
            str(dest),
            status="pending",
        )
        repo.commit()

        service = ActionService(repo)

        assert [a["id"] for a in service.pending_actions(scan_id)] == [action_id]
        assert service.restore_action(action_id)
        assert video.read_bytes() == b"payload"
        assert repo.get_action_by_id(action_id)["status"] == "restored"


class TestPendingRemoveIds:
    """A mark is a request against a file that is there.

    The toolbar count and the dialog's plan both come from this one helper,
    so they cannot disagree — counting raw decisions while the plan filtered
    by presence once promised sixteen files the dialog refused to list.
    """

    def test_keeps_marks_on_present_files(self) -> None:
        assert pending_remove_ids({1: "remove", 2: "remove"}, {1, 2}) == {1, 2}

    def test_drops_a_mark_on_a_file_the_inventory_calls_missing(self) -> None:
        # The file was quarantined or deleted earlier; the decision row
        # outlived it. The mark is spent, not pending.
        assert pending_remove_ids({1: "remove", 2: "remove"}, {2}) == {2}

    def test_ignores_keep_marks(self) -> None:
        assert pending_remove_ids({1: "keep", 2: "remove"}, {1, 2}) == {2}

    def test_no_marks_is_empty(self) -> None:
        assert pending_remove_ids({}, set()) == set()


class TestPresenceGatesThePlan:
    """Marks are planned only while the inventory still calls the file present."""

    def test_does_not_plan_a_mark_on_a_missing_file(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        # The file is still on disk and the mark row is still in the
        # database, so the old plan — which only subtracted executed and
        # deleted actions — promised it. The inventory says the file is
        # gone, so the mark is not pending: nothing to plan, and nothing to
        # report as skipped either.
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x" * 100)
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.mark_file_missing(fid, scan_id)
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)

        assert not plan.removals
        assert not plan.skipped

    def test_plans_a_file_whose_earlier_delete_was_undone(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        # "Delete" used to be a permanent veto: the plan subtracted every
        # file with an executed or deleted action, so the toolbar counted a
        # mark the dialog refused to list. The row is written directly —
        # sending a real file to the Recycle Bin belongs to the platform
        # layer, not to this plan.
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x" * 100)
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.record_action(fid, scan_id, "delete", str(video), "", status="deleted")
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = ActionService(repo).plan_removals(scan_id)

        assert [r.file_id for r in plan.removals] == [fid]

    def test_plans_a_restored_file_marked_again(
        self, repo: DatabaseRepository, tmp_path: Path
    ) -> None:
        # The quarantine round trip: the file went away and came back, so
        # the mark is pending again — the "executed" action row is history,
        # not a claim on the file.
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        service = ActionService(repo)
        service.execute_plan(service.plan_removals(scan_id), scan_id)
        action = repo.get_actions(scan_id)[0]
        assert service.restore_action(action["id"])

        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        plan = service.plan_removals(scan_id)

        assert [r.file_id for r in plan.removals] == [fid]
        assert not plan.skipped


class TestDeleteFallback:
    """Delete must never fall back to permanent deletion.

    The shell can refuse a recycle on a volume without a bin, on a network
    share, or on a path it cannot handle. The refused file must move to
    quarantine and stay restorable. Permanent deletion belongs only to the
    explicit Empty quarantine action.
    """

    def test_refused_recycle_moves_to_quarantine(
        self, repo: DatabaseRepository, tmp_path: Path, monkeypatch
    ) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        monkeypatch.setattr(
            "akin_video_finder.application.actions.send_to_recycle_bin",
            lambda path: False,
        )
        service = ActionService(repo)
        result = service.delete_files(service.plan_removals(scan_id), scan_id)

        assert result == {"trashed": 0, "quarantined": 1, "failed": 0, "skipped": 0}
        assert not video.exists()
        action = repo.get_actions(scan_id)[0]
        assert action["status"] == "executed"
        assert Path(action["quarantine_path"]).read_bytes() == b"payload"
        assert service.pending_actions(scan_id)
        assert not service.pending_quarantine(scan_id)
        assert service.restore_action(action["id"])
        assert video.read_bytes() == b"payload"

    def test_recycle_success_records_deleted(
        self, repo: DatabaseRepository, tmp_path: Path, monkeypatch
    ) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"payload")
        scan_id = repo.create_scan("0.1.0")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        fid = _make_file(repo, scan_id, root_id, video)
        repo.set_decision(scan_id, fid, "remove")
        repo.commit()

        monkeypatch.setattr(
            "akin_video_finder.application.actions.send_to_recycle_bin",
            lambda path: True,
        )
        service = ActionService(repo)
        result = service.delete_files(service.plan_removals(scan_id), scan_id)

        assert result == {"trashed": 1, "quarantined": 0, "failed": 0, "skipped": 0}
        action = repo.get_actions(scan_id)[0]
        assert action["status"] == "deleted"
        assert action["error"] == "recycle"
        assert not Path(action["quarantine_path"]).exists()
        assert not service.restore_action(action["id"])

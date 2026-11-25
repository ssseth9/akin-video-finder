"""Safe file actions: quarantine removals decided during review.

Non-destructive by design — files marked "remove" are moved to a
per-drive ``_akin_video_finder_quarantine`` directory and every move is
recorded in ``file_actions`` so it can be restored later. Nothing is
ever hard-deleted.
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from akin_video_finder.core.path_utils import is_protected, protected_path_keys
from akin_video_finder.infrastructure.windows_platform import send_to_recycle_bin
from akin_video_finder.persistence.repository import DatabaseRepository

logger = logging.getLogger(__name__)

QUARANTINE_DIRNAME = "_akin_video_finder_quarantine"
PROTECTED_FOLDER_REASON = "protected folder"


@dataclass
class PlannedRemoval:
    """A file eligible for quarantine."""

    file_id: int
    path: Path
    size: int
    mtime_ns: int
    quarantine_path: Path


@dataclass
class SkippedRemoval:
    """A remove-marked file that failed a safety check."""

    file_id: int
    path: str
    reason: str


@dataclass
class RemovalPlan:
    """Planned quarantine batch for a scan's remove decisions."""

    removals: list[PlannedRemoval] = field(default_factory=list)
    skipped: list[SkippedRemoval] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(r.size for r in self.removals)


def _quarantine_path_for(source: Path, date_stamp: str) -> Path | None:
    """Map a source path to its quarantine location on the same drive.

    ``E:\\a\\b\\c.mp4`` → ``E:\\_akin_video_finder_quarantine\\<date>\\a\\b\\c.mp4``
    """
    anchor = source.anchor
    if not anchor:
        return None
    try:
        relative = source.relative_to(anchor)
    except ValueError:
        return None
    return Path(anchor) / QUARANTINE_DIRNAME / date_stamp / relative


def _changed_since_plan(entry: PlannedRemoval) -> bool:
    """Whether the file changed after the plan captured its identity."""
    try:
        stat = entry.path.stat()
    except OSError:
        return True
    return stat.st_size != entry.size or stat.st_mtime_ns != entry.mtime_ns


def pending_remove_ids(decisions: dict[int, str], present: set[int]) -> set[int]:
    """Remove-marked files that still need an action.

    A mark is a request against a file that is there. Once an action has
    taken the file away — quarantine, move, delete — the inventory says
    ``missing`` and the mark is spent. If the file comes back (restored
    from the Recycle Bin, put back by hand, found again by a later scan)
    the inventory says ``present`` and the mark is pending again: the
    earlier action row is history, not a claim on the file.

    The plan used to subtract every file with an executed/deleted action
    instead, which made "delete" a permanent veto. Sixteen files restored
    from the bin and re-marked were refused by the dialog while the
    toolbar went on counting them — the marked-versus-acted mismatch.
    """
    return {
        file_id
        for file_id, decision in decisions.items()
        if decision == "remove" and file_id in present
    }


class ActionService:
    """Plans and executes quarantine moves from review decisions."""

    def __init__(
        self, repository: DatabaseRepository, protected_paths: list[str] | None = None
    ) -> None:
        self.repository = repository
        self.protected_paths = list(protected_paths or [])

    def _protected_keys(self) -> tuple[str, ...]:
        return protected_path_keys(self.protected_paths)

    def plan_removals(self, scan_id: int) -> RemovalPlan:
        """Build the quarantine plan for all 'remove' decisions.

        Safety checks per file: still exists on disk, and size/mtime match
        the scan inventory. Confirmation in the dialog is the only gate
        against removing every copy of a match. Only marks on files the
        inventory still calls present are planned — see
        ``pending_remove_ids`` for why action history alone cannot decide.
        """
        plan = RemovalPlan()
        decisions = self.repository.get_decisions(scan_id)
        if "remove" not in decisions.values():
            return plan

        files = {
            f["id"]: f
            for root in self.repository.get_all_roots()
            for f in self.repository.get_files_for_root(root["id"])
        }
        present = {fid for fid, row in files.items() if row.get("presence", "present") == "present"}
        remove_ids = pending_remove_ids(decisions, present)
        if not remove_ids:
            return plan

        date_stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        protected = self._protected_keys()
        for fid in sorted(remove_ids):
            # ``remove_ids`` only ever holds ids the inventory returned, so
            # the row is guaranteed — no "not in the inventory" branch
            row = files[fid]
            path = Path(row["original_path"])
            if is_protected(path, protected):
                plan.skipped.append(SkippedRemoval(fid, str(path), PROTECTED_FOLDER_REASON))
                continue
            if not path.exists():
                plan.skipped.append(SkippedRemoval(fid, str(path), "missing on disk"))
                continue
            stat = path.stat()
            if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
                plan.skipped.append(SkippedRemoval(fid, str(path), "file changed since scan"))
                continue
            dest = _quarantine_path_for(path, date_stamp)
            if dest is None:
                plan.skipped.append(
                    SkippedRemoval(fid, str(path), "cannot map to a quarantine drive")
                )
                continue
            if path.is_symlink():
                plan.skipped.append(
                    SkippedRemoval(fid, str(path), "symlink — remove the link manually")
                )
                continue
            plan.removals.append(PlannedRemoval(fid, path, stat.st_size, stat.st_mtime_ns, dest))
        return plan

    def execute_plan(self, plan: RemovalPlan, scan_id: int) -> dict[str, int]:
        """Move planned files to quarantine and record each action.

        Per-file failures are recorded and skipped — one bad file never
        aborts the batch.
        """
        moved = failed = 0
        protected = self._protected_keys()
        for entry in plan.removals:
            if is_protected(entry.path, protected):
                plan.skipped.append(
                    SkippedRemoval(entry.file_id, str(entry.path), PROTECTED_FOLDER_REASON)
                )
                continue
            if _changed_since_plan(entry):
                plan.skipped.append(
                    SkippedRemoval(entry.file_id, str(entry.path), "file changed since plan")
                )
                continue
            dest = self._dedupe_destination(entry.quarantine_path)
            # Intent first: if the post-op commit fails, a 'pending' row
            # still proves the action was attempted — the alternative is
            # a moved file with no durable record at all
            action_id = self.repository.record_action(
                entry.file_id,
                scan_id,
                "quarantine",
                str(entry.path),
                str(dest),
                status="pending",
            )
            self.repository.commit()
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.rename(entry.path, dest)
            except OSError as e:
                logger.warning(f"Quarantine failed for {entry.path}: {e}")
                self.repository.update_action_status(action_id, "failed", str(e))
                failed += 1
                continue
            self.repository.update_action_status(action_id, "executed")
            # No longer at the scanned path — drop it from review;
            # the action consumed its 'remove' mark
            self.repository.mark_file_missing(entry.file_id, scan_id)
            self.repository.clear_decision(scan_id, entry.file_id)
            moved += 1
        self.repository.commit()
        return {"moved": moved, "failed": failed, "skipped": len(plan.skipped)}

    def move_files(self, plan: RemovalPlan, dest_dir: Path, scan_id: int) -> dict[str, int]:
        """Move planned files to a user-chosen directory; still restorable.

        shutil.move handles cross-drive destinations (copy+delete under
        the hood) that os.rename cannot span.
        """
        moved = failed = 0
        protected = self._protected_keys()
        for entry in plan.removals:
            if is_protected(entry.path, protected):
                plan.skipped.append(
                    SkippedRemoval(entry.file_id, str(entry.path), PROTECTED_FOLDER_REASON)
                )
                continue
            if _changed_since_plan(entry):
                plan.skipped.append(
                    SkippedRemoval(entry.file_id, str(entry.path), "file changed since plan")
                )
                continue
            dest = self._dedupe_destination(dest_dir / entry.path.name)
            action_id = self.repository.record_action(
                entry.file_id,
                scan_id,
                "move",
                str(entry.path),
                str(dest),
                status="pending",
            )
            self.repository.commit()
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(entry.path), str(dest))
            except OSError as e:
                logger.warning(f"Move failed for {entry.path}: {e}")
                self.repository.update_action_status(action_id, "failed", str(e))
                failed += 1
                continue
            self.repository.update_action_status(action_id, "executed")
            self.repository.mark_file_missing(entry.file_id, scan_id)
            self.repository.clear_decision(scan_id, entry.file_id)
            moved += 1
        self.repository.commit()
        return {"moved": moved, "failed": failed, "skipped": len(plan.skipped)}

    def delete_files(self, plan: RemovalPlan, scan_id: int) -> dict[str, int]:
        """Delete planned files — Recycle Bin first, quarantine when refused.

        A file is never permanently deleted here. When the shell refuses the
        recycle, the file moves to the same-drive quarantine folder and stays
        restorable. Permanent deletion happens only from the explicit
        ``empty_quarantine`` action.

        Recorded with action 'delete' so these rows never collide with the
        restorable move/quarantine listing. A refused recycle is recorded as
        executed with its quarantine path, so Restore still works.
        """
        trashed = quarantined = failed = 0
        protected = self._protected_keys()
        for entry in plan.removals:
            if is_protected(entry.path, protected):
                plan.skipped.append(
                    SkippedRemoval(entry.file_id, str(entry.path), PROTECTED_FOLDER_REASON)
                )
                continue
            if _changed_since_plan(entry):
                plan.skipped.append(
                    SkippedRemoval(entry.file_id, str(entry.path), "file changed since plan")
                )
                continue
            dest = self._dedupe_destination(entry.quarantine_path)
            action_id = self.repository.record_action(
                entry.file_id,
                scan_id,
                "delete",
                str(entry.path),
                str(dest),
                status="pending",
            )
            self.repository.commit()
            try:
                if send_to_recycle_bin(entry.path):
                    self.repository.update_action_status(action_id, "deleted", "recycle")
                    trashed += 1
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(entry.path, dest)
                    self.repository.update_action_status(action_id, "executed", "quarantine")
                    quarantined += 1
            except OSError as e:
                logger.warning(f"Delete failed for {entry.path}: {e}")
                self.repository.update_action_status(action_id, "failed", str(e))
                failed += 1
                continue
            self.repository.mark_file_missing(entry.file_id, scan_id)
            self.repository.clear_decision(scan_id, entry.file_id)
        self.repository.commit()
        return {
            "trashed": trashed,
            "quarantined": quarantined,
            "failed": failed,
            "skipped": len(plan.skipped),
        }

    def pending_quarantine(self, scan_id: int | None = None) -> list[dict]:
        """Executed quarantine moves whose file still sits in quarantine.

        Scoped to the real quarantine action — user-destined moves must
        never be reachable by a bulk 'empty quarantine' delete.
        """
        return [
            a
            for a in self.repository.get_actions(scan_id)
            if a["status"] == "executed"
            and a["action"] == "quarantine"
            and Path(a["quarantine_path"]).exists()
        ]

    def pending_actions(self, scan_id: int | None = None) -> list[dict]:
        """Move/quarantine actions whose destination still exists.

        Pending rows are included because a crash between the move and the
        status update leaves a real file in quarantine with no other record.
        """
        return [
            a
            for a in self.repository.get_actions(scan_id)
            if a["status"] in {"executed", "pending"}
            and a["quarantine_path"]
            and Path(a["quarantine_path"]).exists()
        ]

    def empty_quarantine(self, scan_id: int | None = None) -> dict[str, int]:
        """Permanently delete every file still in quarantine.

        This is the only destructive operation in the application —
        irreversible by design, and only reachable after the review →
        quarantine → verify chain.
        """
        deleted = failed = 0
        for action in self.pending_quarantine(scan_id):
            try:
                Path(action["quarantine_path"]).unlink()
            except OSError as e:
                logger.warning(f"Delete failed for {action['quarantine_path']}: {e}")
                self.repository.update_action_status(action["id"], "failed", str(e))
                failed += 1
                continue
            self.repository.update_action_status(action["id"], "deleted")
            deleted += 1
        self.repository.commit()
        return {"deleted": deleted, "failed": failed}

    def restore_action(self, action_id: int) -> bool:
        """Move a quarantined file back to its original location.

        Refuses to overwrite an existing file at the source path.
        """
        action = self.repository.get_action_by_id(action_id)
        return self._restore(action) if action else False

    def _restore(self, action: dict) -> bool:
        if action["status"] not in {"executed", "pending"}:
            return False
        src = Path(action["quarantine_path"])
        dst = Path(action["source_path"])
        if not src.exists() or dst.exists():
            # Keep the row restorable. Marking it failed used to strand a
            # file that is still in quarantine.
            logger.warning(
                "Restore blocked for action %s: source missing or destination occupied",
                action["id"],
            )
            return False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # shutil.move — a user-destined move can span drives
            shutil.move(str(src), str(dst))
        except OSError as e:
            logger.warning("Restore failed for action %s: %s", action["id"], e)
            return False
        self.repository.update_action_status(action["id"], "restored")
        # Back at the scanned path — return it to review
        self.repository.mark_file_present(action["file_id"], action["scan_id"])
        self.repository.commit()
        return True

    @staticmethod
    def _dedupe_destination(dest: Path) -> Path:
        """Avoid overwriting an existing quarantine file."""
        if not dest.exists():
            return dest
        for i in range(1, 1000):
            candidate = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
            if not candidate.exists():
                return candidate
        return dest

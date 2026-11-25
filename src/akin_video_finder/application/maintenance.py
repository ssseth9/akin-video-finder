"""Removing inventory rows that no file backs any more.

The inventory is a cache of what the roots held, keyed by path. A file
deleted outside the app, or renamed, leaves its row behind, and nothing in
a scan ever revisits it. Those rows are not history worth keeping — they are
rows waiting to be removed — so the removal is decided by looking at the
filesystem at the moment the user asks for it, not by inferring anything
from scan bookkeeping.

Two guards keep a cleanup from destroying the inventory of a drive that is
merely unplugged:

- a root whose own path does not exist is skipped whole, exactly as a scan
  skips it;
- a row with a ``file_actions`` record is kept, because that record is what
  the app's restore needs to put a quarantined file back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from akin_video_finder.persistence.repository import DatabaseRepository


def file_is_gone(path: str) -> bool:
    """Whether a path is absent, as opposed to merely unreadable.

    ``os.path.exists`` answers False for both, and deleting the row of a file
    that is only locked or access-denied would be a deletion the cleanup
    cannot justify. Only "not found" means gone; anything else keeps the row.
    """
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except (OSError, ValueError):
        return False
    return False


@dataclass(frozen=True)
class CleanupCandidate:
    """One row the filesystem no longer backs."""

    file_id: int
    path: str
    root: str
    presence: str


@dataclass
class CleanupPlan:
    """What a cleanup would remove, and what it will not touch."""

    candidates: list[CleanupCandidate] = field(default_factory=list)
    on_disk: int = 0
    app_action: int = 0
    unavailable_roots: list[str] = field(default_factory=list)

    @property
    def deletable(self) -> int:
        """Number of rows the cleanup would delete."""
        return len(self.candidates)

    @property
    def examined(self) -> int:
        """Number of rows whose path was checked."""
        return self.on_disk + self.app_action + self.deletable


class CleanupService:
    """Decide and apply the removal of rows no file backs."""

    def __init__(self, repository: DatabaseRepository) -> None:
        """Initialize the service.

        Args:
            repository: Repository holding the inventory.
        """
        self.repository = repository

    def plan(self) -> CleanupPlan:
        """Check every inventory path against the filesystem.

        Returns:
            The plan, with the rows to delete and the reasons the rest stay.
        """
        plan = CleanupPlan()
        root_available: dict[str, bool] = {}
        with_actions = self.repository.get_file_ids_with_actions()
        for row in self.repository.get_inventory_files():
            root = row["root_path"]
            if root not in root_available:
                # A root that is not there says nothing about its files
                root_available[root] = Path(root).exists()
                if not root_available[root]:
                    plan.unavailable_roots.append(root)
            if not root_available[root]:
                continue
            path = row["original_path"]
            if not file_is_gone(path):
                plan.on_disk += 1
            elif row["id"] in with_actions:
                # The app moved or deleted it: the action is how restore works
                plan.app_action += 1
            else:
                plan.candidates.append(
                    CleanupCandidate(
                        file_id=row["id"],
                        path=path,
                        root=root,
                        presence=row["presence"],
                    )
                )
        return plan

    def apply(self, plan: CleanupPlan) -> int:
        """Delete the rows a plan holds, cascading to everything derived.

        Args:
            plan: Plan produced by :meth:`plan`.

        Returns:
            Number of rows deleted.
        """
        return self.repository.delete_files(candidate.file_id for candidate in plan.candidates)

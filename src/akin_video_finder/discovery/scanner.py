"""File discovery across multiple configured roots."""

import fnmatch
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

from akin_video_finder.core.models import CancellationToken, DiscoveredFile, FilePresence
from akin_video_finder.core.path_utils import get_path_key
from akin_video_finder.infrastructure.windows_platform import (
    is_access_denied_error,
    is_lock_violation_error,
    is_symbolic_link,
    should_follow_link,
)

logger = logging.getLogger(__name__)

_WINDOWS_HIDDEN_ATTRIBUTE = 0x2
_WINDOWS_SYSTEM_ATTRIBUTE = 0x4
_SYSTEM_DIRECTORY_NAMES = frozenset({"$recycle.bin", "system volume information"})


class DiscoveryError(Exception):
    """Exception raised during file discovery."""

    def __init__(self, message: str, path: str | None = None) -> None:
        self.message = message
        self.path = path
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if self.path:
            return f"Discovery error at {self.path}: {self.message}"
        return f"Discovery error: {self.message}"


class FileScanner:
    """Scanner for discovering video files across roots."""

    def __init__(
        self,
        video_extensions: list[str],
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        follow_symlinks: bool = False,
        include_hidden: bool = False,
    ) -> None:
        """Initialize the file scanner.

        Args:
            video_extensions: List of video file extensions (e.g., [".mp4", ".mkv"]).
            include_patterns: Optional glob patterns to include.
            exclude_patterns: Optional glob patterns to exclude.
            follow_symlinks: Whether to follow symbolic links and junctions.
            include_hidden: Whether to scan hidden, system, and dot entries.
        """
        self.video_extensions = [ext.lower() for ext in video_extensions]
        self.include_patterns = include_patterns or []
        self.exclude_patterns = exclude_patterns or []
        self.follow_symlinks = follow_symlinks
        self.include_hidden = include_hidden

    def _is_hidden_or_system(self, path: Path) -> bool:
        """True when a child entry is hidden, system, dot-prefixed, or a known system folder."""
        if path.name.startswith("."):
            return True
        if path.name.lower() in _SYSTEM_DIRECTORY_NAMES:
            return True
        if sys.platform == "win32":
            try:
                attributes = path.stat(follow_symlinks=False).st_file_attributes
            except OSError:
                return False
            return bool(attributes & (_WINDOWS_HIDDEN_ATTRIBUTE | _WINDOWS_SYSTEM_ATTRIBUTE))
        return False

    def matches_extension(self, path: Path) -> bool:
        """Check if a file matches a video extension.

        Args:
            path: File path.

        Returns:
            True if the file extension matches, False otherwise.
        """
        # TypeScript declarations (foo.d.ts, foo.d.mts, foo.d.cts) are source
        # files whose final suffix collides with MPEG-TS video extensions
        if path.name.lower().endswith((".d.ts", ".d.mts", ".d.cts")):
            return False
        return path.suffix.lower() in self.video_extensions

    def matches_include_pattern(self, path: Path) -> bool:
        """Check if a file matches an include pattern.

        Args:
            path: File path.

        Returns:
            True if the file matches an include pattern, False otherwise.
        """
        if not self.include_patterns:
            return True

        file_name = path.name
        return any(fnmatch.fnmatch(file_name, pattern) for pattern in self.include_patterns)

    def matches_exclude_pattern(self, path: Path) -> bool:
        """Check if a file matches an exclude pattern.

        Args:
            path: File path.

        Returns:
            True if the file matches an exclude pattern, False otherwise.
        """
        if not self.exclude_patterns:
            return False

        file_name = path.name
        if any(fnmatch.fnmatch(file_name, pattern) for pattern in self.exclude_patterns):
            return True
        # Folder-style patterns ("P:/video/tmp/*") match the full path —
        # fnmatch's * crosses separators, so they cover nested files too
        posix = path.as_posix()
        return any(fnmatch.fnmatch(posix, pattern) for pattern in self.exclude_patterns)

    def should_include_file(self, path: Path) -> bool:
        """Determine if a file should be included in discovery.

        Args:
            path: File path.

        Returns:
            True if the file should be included, False otherwise.
        """
        if not self.matches_extension(path):
            return False

        if not self.matches_include_pattern(path):
            return False

        return not self.matches_exclude_pattern(path)

    def should_recurse_directory(self, path: Path) -> bool:
        """Determine if discovery should recurse into a directory.

        Args:
            path: Directory path.

        Returns:
            True if recursion should continue, False otherwise.
        """
        # Don't follow reparse points if not configured to do so
        return should_follow_link(path, self.follow_symlinks)

    def _dir_excluded(self, path: Path) -> bool:
        """Exclude-pattern match for a directory — prunes whole subtrees.

        A pattern matching the directory name ("_AI") or its full path
        skips the walk entirely; without this, excluded folders still
        cost a directory read per entry.
        """
        for pattern in self.exclude_patterns:
            # The GUI stores folder exclusions as "folder/*". The directory
            # match must see "folder", or the subtree is walked and every
            # file is then rejected one by one.
            directory_pattern = pattern[:-2] if pattern.endswith("/*") else pattern
            if fnmatch.fnmatch(path.name, directory_pattern) or fnmatch.fnmatch(
                path.as_posix(), directory_pattern
            ):
                return True
        return False

    def scan_root(
        self,
        root_path: str,
        root_id: int,
        recursive: bool = True,
        cancellation_token: CancellationToken | None = None,
    ) -> Iterator[DiscoveredFile]:
        """Scan a single root for video files.

        Args:
            root_path: Root directory path.
            root_id: Root ID from database.
            recursive: Whether to scan recursively.
            cancellation_token: Optional cancellation token.

        Yields:
            DiscoveredFile objects.

        Raises:
            DiscoveryError: If the root is not accessible.
        """
        root = Path(root_path)

        if not root.exists():
            raise DiscoveryError(f"Root does not exist: {root_path}", root_path)

        if not root.is_dir():
            raise DiscoveryError(f"Root is not a directory: {root_path}", root_path)

        if recursive:
            yield from self._scan_recursive(root, root_id, cancellation_token)
        else:
            yield from self._scan_nonrecursive(root, root_id, cancellation_token)

    def _scan_recursive(
        self,
        directory: Path,
        root_id: int,
        cancellation_token: CancellationToken | None = None,
    ) -> Iterator[DiscoveredFile]:
        """Recursively scan a directory.

        Args:
            directory: Directory to scan.
            root_id: Root ID.
            cancellation_token: Optional cancellation token.

        Yields:
            DiscoveredFile objects.
        """
        try:
            for entry in directory.iterdir():
                if cancellation_token and cancellation_token.is_cancelled():
                    return

                try:
                    if not self.include_hidden and self._is_hidden_or_system(entry):
                        continue
                    if entry.is_file() and self.should_include_file(entry):
                        yield self._create_discovered_file(entry, root_id)
                    elif (
                        entry.is_dir()
                        and self.should_recurse_directory(entry)
                        and not self._dir_excluded(entry)
                    ):
                        try:
                            yield from self._scan_recursive(entry, root_id, cancellation_token)
                        except DiscoveryError as e:
                            # One unreadable subdirectory must not discard the
                            # readable siblings that were already found.
                            logger.warning("Skipping unreadable directory %s: %s", entry, e)
                            continue
                except OSError as e:
                    # Log error but continue scanning other files
                    if is_access_denied_error(e) or is_lock_violation_error(e):
                        # Skip inaccessible files/directories
                        continue
                    else:
                        # Other errors, continue
                        continue

        except OSError as e:
            if is_access_denied_error(e):
                raise DiscoveryError(
                    f"Access denied to directory: {directory}", str(directory)
                ) from None
            else:
                raise DiscoveryError(f"Error scanning directory: {e}", str(directory)) from None

    def _scan_nonrecursive(
        self,
        directory: Path,
        root_id: int,
        cancellation_token: CancellationToken | None = None,
    ) -> Iterator[DiscoveredFile]:
        """Scan a directory without recursion.

        Args:
            directory: Directory to scan.
            root_id: Root ID.
            cancellation_token: Optional cancellation token.

        Yields:
            DiscoveredFile objects.
        """
        try:
            for entry in directory.iterdir():
                if cancellation_token and cancellation_token.is_cancelled():
                    return

                try:
                    if not self.include_hidden and self._is_hidden_or_system(entry):
                        continue
                    if entry.is_file() and self.should_include_file(entry):
                        yield self._create_discovered_file(entry, root_id)
                except OSError:
                    # Skip files that can't be accessed
                    continue

        except OSError as e:
            if is_access_denied_error(e):
                raise DiscoveryError(
                    f"Access denied to directory: {directory}", str(directory)
                ) from None
            else:
                raise DiscoveryError(f"Error scanning directory: {e}", str(directory)) from None

    def _create_discovered_file(self, path: Path, root_id: int) -> DiscoveredFile:
        """Create a DiscoveredFile from a Path.

        Args:
            path: File path.
            root_id: Root ID.

        Returns:
            DiscoveredFile object.
        """
        try:
            stat = path.stat()
            size = stat.st_size
            # Use nanosecond precision if available, otherwise use microseconds
            mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))
        except OSError:
            # If we can't stat the file, use placeholder values
            # This will cause re-probing on next scan
            size = 0
            mtime_ns = 0

        is_link = is_symbolic_link(path)
        link_target = None
        if is_link:
            try:
                link_target = os.readlink(path)
            except OSError:
                link_target = None

        return DiscoveredFile(
            root_id=root_id,
            original_path=str(path),
            normalized_path_key=get_path_key(path),
            file_name=path.name,
            extension=path.suffix,
            size=size,
            mtime_ns=mtime_ns,
            presence=FilePresence.PRESENT,
            is_symlink=is_link,
            link_target=link_target,
        )

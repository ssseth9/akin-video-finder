"""Tests for Windows platform utilities."""

from pathlib import Path

from akin_video_finder.infrastructure.windows_platform import (
    is_access_denied_error,
    is_lock_violation_error,
    is_reparse_point,
    is_symbolic_link,
    should_follow_link,
)


class TestIsReparsePoint:
    """Tests for is_reparse_point function."""

    def test_nonexistent_path(self, tmp_path: Path) -> None:
        """Test non-existent path returns False."""
        assert not is_reparse_point(tmp_path / "nonexistent")

    def test_regular_file(self, tmp_path: Path) -> None:
        """Test regular file returns False."""
        regular_file = tmp_path / "regular.txt"
        regular_file.write_text("content")
        assert not is_reparse_point(regular_file)

    def test_regular_directory(self, tmp_path: Path) -> None:
        """Test regular directory returns False."""
        regular_dir = tmp_path / "regular_dir"
        regular_dir.mkdir()
        assert not is_reparse_point(regular_dir)


class TestIsSymbolicLink:
    """Tests for is_symbolic_link function."""

    def test_nonexistent_path(self, tmp_path: Path) -> None:
        """Test non-existent path returns False."""
        assert not is_symbolic_link(tmp_path / "nonexistent")

    def test_regular_file(self, tmp_path: Path) -> None:
        """Test regular file returns False."""
        regular_file = tmp_path / "regular.txt"
        regular_file.write_text("content")
        assert not is_symbolic_link(regular_file)

    def test_regular_directory(self, tmp_path: Path) -> None:
        """Test regular directory returns False."""
        regular_dir = tmp_path / "regular_dir"
        regular_dir.mkdir()
        assert not is_symbolic_link(regular_dir)


class TestShouldFollowLink:
    """Tests for should_follow_link function."""

    def test_follow_disabled_regular_file(self, tmp_path: Path) -> None:
        """Test that regular files are always followed even when follow_symlinks=False."""
        regular_file = tmp_path / "regular.txt"
        regular_file.write_text("content")
        # Regular files are always followed (they're not links)
        assert should_follow_link(regular_file, follow_symlinks=False)

    def test_follow_enabled_regular_file(self, tmp_path: Path) -> None:
        """Test regular file is followed when enabled (returns True)."""
        regular_file = tmp_path / "regular.txt"
        regular_file.write_text("content")
        # Regular files should be followed (they're not links)
        assert should_follow_link(regular_file, follow_symlinks=True)


class TestIsAccessDeniedError:
    """Tests for is_access_denied_error function."""

    def test_oserror_without_winerror(self) -> None:
        """Test OSError without winerror attribute returns False."""
        error = OSError("Some error")
        assert not is_access_denied_error(error)

    def test_oserror_with_different_winerror(self) -> None:
        """Test OSError with different winerror returns False."""
        error = OSError("Some error")
        error.winerror = 32  # Sharing violation, not access denied
        assert not is_access_denied_error(error)

    def test_access_denied_error(self) -> None:
        """Test access denied error returns True."""
        error = OSError("Access denied")
        error.winerror = 5
        assert is_access_denied_error(error)


class TestIsLockViolationError:
    """Tests for is_lock_violation_error function."""

    def test_oserror_without_winerror(self) -> None:
        """Test OSError without winerror attribute returns False."""
        error = OSError("Some error")
        assert not is_lock_violation_error(error)

    def test_oserror_with_different_winerror(self) -> None:
        """Test OSError with different winerror returns False."""
        error = OSError("Some error")
        error.winerror = 5  # Access denied, not lock violation
        assert not is_lock_violation_error(error)

    def test_sharing_violation_error(self) -> None:
        """Test sharing violation error returns True."""
        error = OSError("Sharing violation")
        error.winerror = 32
        assert is_lock_violation_error(error)

    def test_lock_violation_error(self) -> None:
        """Test lock violation error returns True."""
        error = OSError("Lock violation")
        error.winerror = 33
        assert is_lock_violation_error(error)

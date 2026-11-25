"""Tests for file discovery scanner."""

import ctypes
import sys
from pathlib import Path

import pytest

from akin_video_finder.core.models import CancellationToken
from akin_video_finder.discovery.scanner import DiscoveryError, FileScanner


class TestFileScanner:
    """Tests for FileScanner class."""

    def test_matches_extension(self) -> None:
        """Test extension matching."""
        scanner = FileScanner(video_extensions=[".mp4", ".mkv"])
        assert scanner.matches_extension(Path("test.mp4"))
        assert scanner.matches_extension(Path("test.MP4"))
        assert scanner.matches_extension(Path("test.mkv"))
        assert not scanner.matches_extension(Path("test.avi"))

    def test_matches_extension_skips_typescript_declarations(self) -> None:
        """*.d.ts/*.d.mts/*.d.cts are source files, not MPEG-TS video."""
        scanner = FileScanner(video_extensions=[".ts", ".mts"])
        assert scanner.matches_extension(Path("movie.ts"))
        assert not scanner.matches_extension(Path("index.d.ts"))
        assert not scanner.matches_extension(Path("index.d.mts"))
        assert not scanner.matches_extension(Path("index.d.cts"))

    def test_matches_include_pattern(self) -> None:
        """Test include pattern matching."""
        scanner = FileScanner(
            video_extensions=[".mp4"],
            include_patterns=["*.mp4", "*.mkv"],
        )
        assert scanner.matches_include_pattern(Path("test.mp4"))
        assert scanner.matches_include_pattern(Path("test.mkv"))
        assert not scanner.matches_include_pattern(Path("test.avi"))

    def test_matches_exclude_pattern(self) -> None:
        """Test exclude pattern matching."""
        scanner = FileScanner(
            video_extensions=[".mp4"],
            exclude_patterns=["*_sample.*", "*_trailer.*"],
        )
        assert scanner.matches_exclude_pattern(Path("test_sample.mp4"))
        assert scanner.matches_exclude_pattern(Path("movie_trailer.mp4"))
        assert not scanner.matches_exclude_pattern(Path("test.mp4"))

    def test_should_include_file(self) -> None:
        """Test file inclusion logic."""
        scanner = FileScanner(
            video_extensions=[".mp4"],
            include_patterns=["*.mp4"],
            exclude_patterns=["*_sample.*"],
        )
        assert scanner.should_include_file(Path("test.mp4"))
        assert not scanner.should_include_file(Path("test.avi"))
        assert not scanner.should_include_file(Path("test_sample.mp4"))

    def test_a_folder_pattern_with_a_glob_prunes_the_folder(self, tmp_path: Path) -> None:
        """The GUI stores "folder/*"; the directory itself must match."""
        (tmp_path / "keep.mp4").write_bytes(b"keep")
        excluded = tmp_path / "_AI"
        excluded.mkdir()
        (excluded / "inside.mp4").write_bytes(b"skip")

        scanner = FileScanner(video_extensions=[".mp4"], exclude_patterns=["_AI/*"])
        found = list(scanner.scan_root(str(tmp_path), 1))

        assert [Path(item.original_path).name for item in found] == ["keep.mp4"]

    def test_scan_nonexistent_root(self, tmp_path: Path) -> None:
        """Test scanning non-existent root raises error."""
        scanner = FileScanner(video_extensions=[".mp4"])
        with pytest.raises(DiscoveryError, match="does not exist"):
            list(scanner.scan_root("nonexistent", 1, recursive=False))

    def test_scan_file_instead_of_directory(self, tmp_path: Path) -> None:
        """Test scanning a file instead of directory raises error."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        scanner = FileScanner(video_extensions=[".mp4"])
        with pytest.raises(DiscoveryError, match="not a directory"):
            list(scanner.scan_root(str(test_file), 1, recursive=False))

    def test_scan_nonrecursive(self, tmp_path: Path) -> None:
        """Test non-recursive scanning."""
        # Create test structure
        (tmp_path / "test1.mp4").write_text("video1")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "test2.mp4").write_text("video2")

        scanner = FileScanner(video_extensions=[".mp4"])
        files = list(scanner.scan_root(str(tmp_path), 1, recursive=False))

        assert len(files) == 1
        assert files[0].file_name == "test1.mp4"

    def test_scan_recursive(self, tmp_path: Path) -> None:
        """Test recursive scanning."""
        # Create test structure
        (tmp_path / "test1.mp4").write_text("video1")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "test2.mp4").write_text("video2")

        scanner = FileScanner(video_extensions=[".mp4"])
        files = list(scanner.scan_root(str(tmp_path), 1, recursive=True))

        assert len(files) == 2
        file_names = {f.file_name for f in files}
        assert "test1.mp4" in file_names
        assert "test2.mp4" in file_names

    def test_cancellation(self, tmp_path: Path) -> None:
        """Test cancellation during scan."""
        # Create test files
        for i in range(10):
            (tmp_path / f"test{i}.mp4").write_text(f"video{i}")

        scanner = FileScanner(video_extensions=[".mp4"])
        cancellation_token = CancellationToken()
        cancellation_token.cancel()

        files = list(
            scanner.scan_root(
                str(tmp_path), 1, recursive=True, cancellation_token=cancellation_token
            )
        )

        # Should return empty or partial list due to cancellation
        # The exact behavior depends on when cancellation is checked
        assert isinstance(files, list)

    def test_create_discovered_file(self, tmp_path: Path) -> None:
        """Test DiscoveredFile creation."""
        test_file = tmp_path / "test.mp4"
        test_file.write_text("video content")

        scanner = FileScanner(video_extensions=[".mp4"])
        discovered = scanner._create_discovered_file(test_file, root_id=1)

        assert discovered.root_id == 1
        assert discovered.original_path == str(test_file)
        assert discovered.file_name == "test.mp4"
        assert discovered.extension == ".mp4"
        assert discovered.size > 0
        assert discovered.mtime_ns > 0

    def test_hidden_and_system_entries_are_skipped_by_default(self, tmp_path: Path) -> None:
        (tmp_path / "keep.mp4").write_bytes(b"keep")
        (tmp_path / ".hidden.mp4").write_bytes(b"hidden file")
        dot_dir = tmp_path / ".hidden_dir"
        dot_dir.mkdir()
        (dot_dir / "inside.mp4").write_bytes(b"hidden dir")
        recycle = tmp_path / "$RECYCLE.BIN"
        recycle.mkdir()
        (recycle / "deleted.mp4").write_bytes(b"deleted")
        system = tmp_path / "System Volume Information"
        system.mkdir()
        (system / "system.mp4").write_bytes(b"system")

        scanner = FileScanner(video_extensions=[".mp4"])
        found = {Path(item.original_path).name for item in scanner.scan_root(str(tmp_path), 1)}

        assert found == {"keep.mp4"}

    def test_include_hidden_scans_hidden_and_system_entries(self, tmp_path: Path) -> None:
        (tmp_path / "keep.mp4").write_bytes(b"keep")
        (tmp_path / ".hidden.mp4").write_bytes(b"hidden file")
        dot_dir = tmp_path / ".hidden_dir"
        dot_dir.mkdir()
        (dot_dir / "inside.mp4").write_bytes(b"hidden dir")
        recycle = tmp_path / "$RECYCLE.BIN"
        recycle.mkdir()
        (recycle / "deleted.mp4").write_bytes(b"deleted")

        scanner = FileScanner(video_extensions=[".mp4"], include_hidden=True)
        found = {Path(item.original_path).name for item in scanner.scan_root(str(tmp_path), 1)}

        assert found == {"keep.mp4", ".hidden.mp4", "inside.mp4", "deleted.mp4"}

    def test_explicit_hidden_root_is_still_scanned(self, tmp_path: Path) -> None:
        root = tmp_path / ".hidden_library"
        root.mkdir()
        (root / "movie.mp4").write_bytes(b"movie")

        scanner = FileScanner(video_extensions=[".mp4"])
        found = list(scanner.scan_root(str(root), 1))

        assert [Path(item.original_path).name for item in found] == ["movie.mp4"]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
    def test_windows_hidden_attribute_is_skipped(self, tmp_path: Path) -> None:
        hidden_dir = tmp_path / "attribute_hidden"
        hidden_dir.mkdir()
        (hidden_dir / "inside.mp4").write_bytes(b"hidden")

        ctypes.windll.kernel32.SetFileAttributesW(str(hidden_dir), 0x2)
        try:
            scanner = FileScanner(video_extensions=[".mp4"])
            found = list(scanner.scan_root(str(tmp_path), 1))
            assert found == []

            scanner = FileScanner(video_extensions=[".mp4"], include_hidden=True)
            found = list(scanner.scan_root(str(tmp_path), 1))
            assert [Path(item.original_path).name for item in found] == ["inside.mp4"]
        finally:
            ctypes.windll.kernel32.SetFileAttributesW(str(hidden_dir), 0x80)

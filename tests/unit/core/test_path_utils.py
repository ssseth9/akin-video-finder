"""Tests for Windows path normalization and comparison."""

from pathlib import Path

from akin_video_finder.core.path_utils import (
    get_drive_letter,
    get_path_key,
    is_drive_root,
    is_protected,
    normalize_path_key,
    paths_equivalent,
    protected_path_keys,
)


class TestNormalizePathKey:
    """Tests for normalize_path_key function."""

    def test_basic_path(self) -> None:
        """Test basic path normalization."""
        result = normalize_path_key("D:\\Videos\\File.mp4")
        assert result == "d:\\videos\\file.mp4"

    def test_forward_slashes(self) -> None:
        """Test forward slashes are converted to backslashes."""
        result = normalize_path_key("D:/Videos/File.mp4")
        assert "d:\\videos\\file.mp4" in result or result == "d:/videos/file.mp4"

    def test_mixed_slashes(self) -> None:
        """Test mixed slashes are normalized."""
        result = normalize_path_key("D:/Videos\\File.mp4")
        assert "file.mp4" in result.lower()

    def test_case_insensitive(self) -> None:
        """Test case is normalized to lowercase."""
        result1 = normalize_path_key("D:\\VIDEOS\\FILE.MP4")
        result2 = normalize_path_key("d:\\videos\\file.mp4")
        assert result1 == result2

    def test_trailing_separator(self) -> None:
        """Test trailing separator is removed (except drive root)."""
        result = normalize_path_key("D:\\Videos\\")
        assert result == "d:\\videos" or result.endswith("videos")

    def test_drive_root_preserves_trailing_separator(self) -> None:
        """Test drive root keeps its trailing separator."""
        # Drive roots are special
        result = normalize_path_key("C:\\")
        assert "c:" in result.lower()

    def test_redundant_separators(self) -> None:
        """Test redundant separators are collapsed."""
        result = normalize_path_key("D:\\\\Videos\\\\File.mp4")
        assert "file.mp4" in result.lower()

    def test_dot_components(self) -> None:
        """Test current directory components are normalized."""
        result = normalize_path_key("D:\\Videos\\.\\File.mp4")
        assert "file.mp4" in result.lower()

    def test_dotdot_components(self) -> None:
        """Test parent directory components are normalized."""
        result = normalize_path_key("D:\\Videos\\Subdir\\..\\File.mp4")
        assert "file.mp4" in result.lower()

    def test_empty_path(self) -> None:
        """Test empty path returns empty string."""
        assert normalize_path_key("") == ""

    def test_path_object(self) -> None:
        """Test Path objects are handled."""
        result = normalize_path_key(Path("D:\\Videos\\File.mp4"))
        assert "file.mp4" in result.lower()

    def test_unc_path(self) -> None:
        """Test UNC path normalization."""
        result = normalize_path_key("\\\\Server\\Share\\File.mp4")
        assert "file.mp4" in result.lower()

    def test_unc_path_with_forward_slashes(self) -> None:
        """Test UNC path with forward slashes."""
        result = normalize_path_key("//Server/Share/File.mp4")
        assert "file.mp4" in result.lower()

    def test_relative_path(self) -> None:
        """Test relative path normalization."""
        result = normalize_path_key("Videos\\File.mp4")
        assert "file.mp4" in result.lower()
        result2 = normalize_path_key(".\\Videos\\File.mp4")
        assert "file.mp4" in result2.lower()


class TestPathsEquivalent:
    """Tests for paths_equivalent function."""

    def test_identical_paths(self) -> None:
        """Test identical paths are equivalent."""
        assert paths_equivalent("D:\\Videos\\File.mp4", "D:\\Videos\\File.mp4")

    def test_case_difference(self) -> None:
        """Test case differences are ignored."""
        assert paths_equivalent("D:\\Videos\\File.mp4", "d:\\videos\\file.mp4")
        assert paths_equivalent("D:\\VIDEOS\\FILE.MP4", "d:\\videos\\file.mp4")

    def test_separator_difference(self) -> None:
        """Test separator differences are ignored."""
        assert paths_equivalent("D:\\Videos\\File.mp4", "D:/Videos/File.mp4")
        assert paths_equivalent("D:/Videos\\File.mp4", "D:\\Videos/File.mp4")

    def test_trailing_separator_difference(self) -> None:
        """Test trailing separator differences are ignored."""
        assert paths_equivalent("D:\\Videos", "D:\\Videos\\")
        assert paths_equivalent("D:\\Videos\\File.mp4", "D:\\Videos\\File.mp4\\")

    def test_redundant_separator_difference(self) -> None:
        """Test redundant separator differences are ignored."""
        assert paths_equivalent("D:\\Videos\\File.mp4", "D:\\\\Videos\\\\File.mp4")

    def test_different_paths(self) -> None:
        """Test different paths are not equivalent."""
        assert not paths_equivalent("D:\\Videos\\File1.mp4", "D:\\Videos\\File2.mp4")
        assert not paths_equivalent("D:\\Videos\\File.mp4", "E:\\Videos\\File.mp4")

    def test_path_objects(self) -> None:
        """Test Path objects are handled."""
        assert paths_equivalent(Path("D:\\Videos\\File.mp4"), "d:\\videos\\file.mp4")


class TestGetPathKey:
    """Tests for get_path_key function."""

    def test_get_path_key(self) -> None:
        """Test get_path_key returns normalized key."""
        assert get_path_key("D:\\Videos\\File.mp4") == "d:\\videos\\file.mp4"


class TestIsDriveRoot:
    """Tests for is_drive_root function."""

    def test_drive_root_with_backslash(self) -> None:
        """Test drive root with backslash."""
        assert is_drive_root("C:\\")
        assert is_drive_root("D:\\")

    def test_drive_root_without_backslash(self) -> None:
        """Test drive root without backslash."""
        assert is_drive_root("C:")

    def test_drive_root_case_insensitive(self) -> None:
        """Test drive root is case-insensitive."""
        assert is_drive_root("c:\\")
        assert is_drive_root("D:")

    def test_not_drive_root(self) -> None:
        """Test non-drive-root paths."""
        assert not is_drive_root("C:\\Videos")
        assert not is_drive_root("D:\\Videos\\File.mp4")
        assert not is_drive_root("\\\\Server\\Share")
        assert not is_drive_root("Videos")

    def test_path_object(self) -> None:
        """Test Path objects are handled."""
        assert is_drive_root(Path("C:\\"))


class TestGetDriveLetter:
    """Tests for get_drive_letter function."""

    def test_get_drive_letter(self) -> None:
        """Test extracting drive letter."""
        assert get_drive_letter("C:\\Videos") == "C"
        assert get_drive_letter("D:\\Videos\\File.mp4") == "D"
        assert get_drive_letter("E:") == "E"

    def test_case_insensitive(self) -> None:
        """Test drive letter extraction is case-insensitive."""
        assert get_drive_letter("c:\\Videos") == "C"
        assert get_drive_letter("d:\\Videos") == "D"

    def test_no_drive_letter(self) -> None:
        """Test paths without drive letters return None."""
        assert get_drive_letter("\\\\Server\\Share") is None
        assert get_drive_letter("Videos") is None
        assert get_drive_letter("\\relative\\path") is None

    def test_path_object(self) -> None:
        """Test Path objects are handled."""
        assert get_drive_letter(Path("C:\\Videos")) == "C"


class TestProtectedPaths:
    """Protected folders use separator-bounded prefixes, not substrings."""

    def test_child_is_protected(self, tmp_path: Path) -> None:
        root = tmp_path / "Videos"
        keys = protected_path_keys([str(root)])
        assert is_protected(root / "a.mp4", keys)
        assert is_protected(root, keys)

    def test_sibling_with_same_prefix_is_not_protected(self, tmp_path: Path) -> None:
        root = tmp_path / "Videos"
        sibling = tmp_path / "Videos2"
        keys = protected_path_keys([str(root)])
        assert not is_protected(sibling / "b.mp4", keys)
        assert not is_protected(tmp_path / "Videos.mp4", keys)

    def test_case_and_trailing_separator(self, tmp_path: Path) -> None:
        root = tmp_path / "Videos"
        keys = protected_path_keys([str(root).upper() + "\\"])
        assert is_protected(root / "a.mp4", keys)

    def test_missing_protected_folder_still_protects_children(self, tmp_path: Path) -> None:
        root = tmp_path / "not-created-yet"
        keys = protected_path_keys([str(root)])
        assert is_protected(root / "later.mp4", keys)

    def test_no_keys_protects_nothing(self, tmp_path: Path) -> None:
        assert not is_protected(tmp_path / "a.mp4", ())

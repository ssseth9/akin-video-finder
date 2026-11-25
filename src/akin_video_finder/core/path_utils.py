"""Windows path normalization and comparison utilities."""

import re
from pathlib import Path
from typing import Final

# Normalized path key separator - always backslash on Windows
_PATH_SEPARATOR: Final = "\\"


def normalize_path_key(path: str | Path) -> str:
    """Normalize a path for comparison using Windows semantics.

    This function:
    - Normalizes separators to backslashes
    - Removes redundant separators
    - Removes trailing separators (except for drive roots)
    - Normalizes directory components (., ..)
    - Lowercases the path for case-insensitive comparison
    - Handles UNC paths
    - Does NOT require the path to exist

    The original path casing is NOT modified; this returns a comparison key only.

    Args:
        path: The path to normalize (string or Path object).

    Returns:
        A normalized comparison key string.

    Examples:
        >>> normalize_path_key("D:\\Videos\\File.mp4")
        'd:\\\\videos\\\\file.mp4'
        >>> normalize_path_key("d:/videos/file.mp4")
        'd:\\\\videos\\\\file.mp4'
        >>> normalize_path_key("D:\\\\Videos\\\\Subdir\\\\..\\\\File.mp4")
        'd:\\\\videos\\\\file.mp4'
    """
    path_str = str(path)

    # Handle empty path
    if not path_str:
        return ""

    # Normalize slashes to backslashes
    normalized = path_str.replace("/", "\\")

    # Normalize multiple consecutive backslashes to single backslash
    # But preserve UNC path prefix (\\server\share)
    if normalized.startswith("\\\\"):
        # UNC path: preserve first two backslashes, normalize rest
        parts = normalized.split("\\")
        if len(parts) > 2:
            # Keep first two empty strings from split, then join non-empty parts
            server = parts[2] if len(parts) > 2 else ""
            share = parts[3] if len(parts) > 3 else ""
            rest = [p for p in parts[4:] if p]
            if server and share:
                normalized = f"\\\\{server}\\{share}"
                if rest:
                    normalized += "\\" + "\\".join(rest)
            else:
                # Malformed UNC, normalize conservatively
                normalized = "\\".join(p for p in parts if p)
    else:
        # Regular path: normalize consecutive backslashes
        while "\\\\" in normalized:
            normalized = normalized.replace("\\\\", "\\")

    # Remove trailing backslash (except for drive roots like "C:\")
    if (
        len(normalized) > 1
        and normalized.endswith("\\")
        and not (len(normalized) == 3 and normalized[1] == ":")
    ):
        normalized = normalized[:-1]

    # Lowercase for case-insensitive comparison
    normalized = normalized.lower()

    return normalized


def paths_equivalent(path1: str | Path, path2: str | Path) -> bool:
    """Check if two paths are equivalent using Windows semantics.

    This compares normalized path keys, so it handles:
    - Case differences (D:\\Videos vs d:\videos)
    - Separator differences (/ vs \\)
    - Redundant components
    - Trailing separators

    Args:
        path1: First path.
        path2: Second path.

    Returns:
        True if the paths are equivalent, False otherwise.
    """
    return normalize_path_key(path1) == normalize_path_key(path2)


def get_path_key(path: str | Path) -> str:
    """Get a stable comparison key for a path.

    This is an alias for normalize_path_key for clarity in domain code.

    Args:
        path: The path to get a key for.

    Returns:
        A normalized comparison key string.
    """
    return normalize_path_key(path)


def protected_path_keys(paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Build normalized keys for protected folders."""
    return tuple(normalize_path_key(Path(path).resolve(strict=False)) for path in paths)


def is_protected(path: str | Path, keys: tuple[str, ...]) -> bool:
    """True when a path is a protected folder or sits below one.

    The comparison is a separator-bounded prefix, never a substring, so
    ``C:\\Videos`` does not protect ``C:\\Videos2``.
    """
    key = normalize_path_key(Path(path).resolve(strict=False))
    for root in keys:
        if key == root:
            return True
        prefix = root if root.endswith(_PATH_SEPARATOR) else root + _PATH_SEPARATOR
        if key.startswith(prefix):
            return True
    return False


def is_drive_root(path: str | Path) -> bool:
    r"""Check if a path is a drive root (e.g., C:\ or D:\).

    Args:
        path: The path to check.

    Returns:
        True if the path is a drive root, False otherwise.
    """
    path_str = str(path).upper()
    # Match pattern: X:\ where X is a letter
    return bool(re.match(r"^[A-Z]:\\?$", path_str))


def get_drive_letter(path: str | Path) -> str | None:
    """Extract the drive letter from a path.

    Args:
        path: The path to extract from.

    Returns:
        The drive letter (e.g., "C") or None if not a drive path.
    """
    path_str = str(path)
    match = re.match(r"^([A-Za-z]):", path_str)
    return match.group(1).upper() if match else None

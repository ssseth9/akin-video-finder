"""Windows-specific platform utilities."""

import os
from pathlib import Path
from typing import Final


def is_reparse_point(path: Path) -> bool:
    """Check if a path is a Windows reparse point (junction, symlink, etc.).

    This uses the Windows-specific FILE_ATTRIBUTE_REPARSE_POINT flag.

    Args:
        path: The path to check.

    Returns:
        True if the path is a reparse point, False otherwise.
        Returns False if the path does not exist.
    """
    try:
        # Get file attributes using os.stat
        # st_file_attributes is available on Windows
        stat_result = os.stat(path, follow_symlinks=False)
        # Check for FILE_ATTRIBUTE_REPARSE_POINT (0x400)
        file_attributes = getattr(stat_result, "st_file_attributes", 0)
        return bool(file_attributes & 0x400)
    except (OSError, AttributeError):
        # Path doesn't exist or attribute not available
        return False


def is_junction(path: Path) -> bool:
    """Check if a path is a directory junction.

    On Windows, junctions are a type of reparse point. This function
    specifically checks for junctions vs. other reparse points.

    Args:
        path: The path to check.

    Returns:
        True if the path is a junction, False otherwise.
        Returns False if the path does not exist.
    """
    if not is_reparse_point(path):
        return False

    try:
        # Try to read the junction target
        # This is a heuristic - junctions can be distinguished from symlinks
        # by the fact that they don't require special permissions to create
        # and have different attribute flags
        stat_result = os.stat(path, follow_symlinks=False)
        file_attributes = getattr(stat_result, "st_file_attributes", 0)

        # Junctions have FILE_ATTRIBUTE_DIRECTORY (0x10) and REPARSE_POINT (0x400)
        # but not FILE_ATTRIBUTE_OFFLINE (0x1000) typically
        # Additional check: junctions work without special privileges
        # This is a practical distinction
        return bool(file_attributes & 0x10)
    except OSError:
        return False


def is_symbolic_link(path: Path) -> bool:
    """Check if a path is a symbolic link.

    Args:
        path: The path to check.

    Returns:
        True if the path is a symbolic link, False otherwise.
        Returns False if the path does not exist.
    """
    try:
        return path.is_symlink()
    except OSError:
        return False


def should_follow_link(path: Path, follow_symlinks: bool) -> bool:
    """Determine whether to follow a reparse point or symbolic link.

    Args:
        path: The path to check.
        follow_symlinks: Whether symlinks are configured to be followed.

    Returns:
        True if the link should be followed, False otherwise.
    """
    # Regular files and directories are always followed
    if not is_reparse_point(path):
        return True

    if not follow_symlinks:
        # If following is disabled, never follow any reparse point
        return False

    # If following is enabled, follow symlinks but still avoid junctions
    # unless explicitly configured (current implementation: treat same as symlinks)
    return is_symbolic_link(path)


# Windows error codes
ERROR_ACCESS_DENIED: Final = 5
ERROR_SHARING_VIOLATION: Final = 32
ERROR_LOCK_VIOLATION: Final = 33

# SHFileOperationW opcodes/flags for Recycle Bin deletes
FO_DELETE: Final = 0x3
FOF_ALLOWUNDO: Final = 0x40
FOF_NOCONFIRMATION: Final = 0x10
FOF_SILENT: Final = 0x4
FOF_NOERRORUI: Final = 0x400


def is_access_denied_error(error: OSError) -> bool:
    """Check if an OSError is an access-denied error.

    Args:
        error: The OSError to check.

    Returns:
        True if the error is access-denied, False otherwise.
    """
    return error.winerror == ERROR_ACCESS_DENIED if hasattr(error, "winerror") else False


def is_lock_violation_error(error: OSError) -> bool:
    """Check if an OSError is a lock or sharing violation error.

    Args:
        error: The OSError to check.

    Returns:
        True if the error is a lock violation, False otherwise.
    """
    if not hasattr(error, "winerror"):
        return False
    return error.winerror in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION)


def send_to_recycle_bin(path: Path) -> bool:
    """Send a file to the Windows Recycle Bin via SHFileOperationW.

    Args:
        path: The file to recycle.

    Returns:
        True when the shell accepted the recycle. False on non-Windows,
        a shell error, or user abort — the caller decides whether to
        fall back to a permanent delete.
    """
    import sys

    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    # pFrom must be double-NUL terminated — Python appends one NUL
    op.pFrom = str(path) + "\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except (AttributeError, OSError):
        return False
    return result == 0 and not op.fAnyOperationsAborted

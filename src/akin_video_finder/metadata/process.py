"""Process helpers shared by the metadata extractors."""

from __future__ import annotations

import contextlib
import subprocess
import sys


def kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill a child process and every child it spawned.

    ``process.kill()`` terminates only the direct child. A decoder that is
    stuck can leave a grandchild alive, and the worker then waits on the pipe
    forever. ``taskkill /T`` kills the tree, and closing the pipes releases
    any reader that is still blocked.
    """
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0,
            )
    with contextlib.suppress(OSError):
        process.kill()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=5)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()

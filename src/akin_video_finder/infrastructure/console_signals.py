"""Ctrl+C handling for the GUI process.

The GUI is the one place where Ctrl+C is not a stop. In a window the key is a
copy shortcut, and the window already has a Stop button that says what it
means; a GUI process that dies on Ctrl+C loses the scan it was running and
leaves the row saying ``running``. The CLI is not guarded this way — a
terminal has no Stop button, so ``scan`` keeps Ctrl+C as its graceful cancel.
"""

from __future__ import annotations

import contextlib
import signal


def ignore_ctrl_c() -> None:
    """Make Ctrl+C do nothing in this process.

    Called from ``run_gui`` before ``QApplication`` exists. A packaged GUI has
    no console at all — Windows never delivers Ctrl+C to it — but a console
    launch (``akin-video-finder gui``, ``python -m akin_video_finder.gui``) would raise
    ``KeyboardInterrupt`` out of the Qt event loop and take the window, and the
    scan inside it, down mid-write. ``SIG_IGN`` is set at the C level, so no
    exception is ever raised; the GUI's Stop button stays the only stop.

    A non-main thread cannot install a signal handler (``ValueError``), and an
    embedded interpreter may refuse the signal outright (``OSError``); in both
    cases there is nothing to guard and the call is a no-op.
    """
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, signal.SIG_IGN)

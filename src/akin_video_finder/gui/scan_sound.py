"""The chime that says a scan has finished.

The window is often left in the background while a scan grinds through a
library, so the end of the run needs a cue that is heard rather than watched.
The sound is a short synthesised arpeggio shipped with the app, not one of the
Windows system sounds: those are shared with every other program and can be
switched off or replaced by the user's scheme.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def play_scan_complete(path: Path) -> None:
    """Play the completion chime without blocking the GUI thread."""
    if sys.platform != "win32":
        return
    try:
        import winsound

        winsound.PlaySound(
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
    except (OSError, RuntimeError):
        logger.exception("Could not play the scan completion sound")

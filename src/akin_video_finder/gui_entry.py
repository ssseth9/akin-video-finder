"""Double-click GUI entry point — no console, no --config flag.

Used by the packaged build (packaging/akin-video-finder-gui.spec). Resolves
config.toml next to the executable when writable (portable mode), else
%APPDATA%/akin-video-finder (installed to Program Files). Creates the config
from the example template on first run, then launches the GUI.
"""

import contextlib
import os
import sys
from pathlib import Path

from akin_video_finder.cli.main import main


def _writable(directory: Path) -> bool:
    try:
        probe = directory / ".write-probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _config_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        if not _writable(base):
            # Installed to a protected dir (e.g. Program Files)
            base = Path(os.environ["APPDATA"]) / "akin-video-finder"
    else:
        base = Path.cwd()
    base.mkdir(parents=True, exist_ok=True)
    config = base / "config.toml"
    if not config.exists():
        # First run — write the commented example config. init-config
        # ends with typer.Exit/SystemExit — swallow it or the process
        # dies before the GUI ever launches
        sys.argv = ["akin-video-finder", "init-config", "--output", str(config)]
        with contextlib.suppress(SystemExit):
            main()
    return config


if __name__ == "__main__":
    sys.argv = ["akin-video-finder", "gui", "--config", str(_config_path())]
    main()

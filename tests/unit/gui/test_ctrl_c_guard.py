"""The GUI's Ctrl+C guard, and the scope it must not cross.

``run_gui`` installs the guard before the event loop exists, so a copy
shortcut in the console that launched the window cannot take the window — and
the scan inside it — down. The CLI is deliberately not guarded: a terminal has
no Stop button, so ``scan`` keeps Ctrl+C as its graceful cancel.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import akin_video_finder.cli.main as cli_main  # noqa: E402
import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402


def _config(tmp_path: Any) -> Config:
    return Config(roots=[RootConfig(path=str(tmp_path))])


class _FakeApp:
    """Just enough QApplication to reach the event loop and leave it."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv

    def setWindowIcon(self, icon: Any) -> None:  # noqa: N802 - Qt's spelling
        pass

    def exec(self) -> int:
        return 0


class _FakeRepository:
    def __init__(self, path: Any) -> None:
        self.path = path

    def mark_interrupted_scans(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeWindow:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def showMaximized(self) -> None:  # noqa: N802 - Qt's spelling
        pass


class TestRunGuiIgnoresCtrlC:
    def test_the_guard_runs_before_the_application(self, monkeypatch: Any, tmp_path: Any) -> None:
        """Order matters: SIG_IGN must be set before the Qt event loop exists."""
        events: list[str] = []

        def fake_guard() -> None:
            events.append("guard")

        class RecordingApp(_FakeApp):
            def __init__(self, argv: list[str]) -> None:
                super().__init__(argv)
                events.append("app")

        monkeypatch.setattr(gui_app, "ignore_ctrl_c", fake_guard)
        monkeypatch.setattr(gui_app, "QApplication", RecordingApp)
        monkeypatch.setattr(gui_app, "_app_icon", lambda: None)
        monkeypatch.setattr(gui_app, "DatabaseRepository", _FakeRepository)
        monkeypatch.setattr(gui_app, "MainWindow", _FakeWindow)

        code = gui_app.run_gui(_config(tmp_path), tmp_path / "config.toml")

        assert code == 0
        assert events == ["guard", "app"]


class TestTheCliKeepsItsOwnCtrlC:
    """The GUI's guard is a GUI decision; the CLI's cancel is untouched."""

    def test_scan_still_handles_the_interrupt(self) -> None:
        source = inspect.getsource(cli_main)
        assert "handle_interrupt" in source
        assert "signal.signal(signal.SIGINT" in source

    def test_the_guard_is_not_imported_by_the_cli(self) -> None:
        source = inspect.getsource(cli_main)
        assert "ignore_ctrl_c" not in source
        assert "console_signals" not in source

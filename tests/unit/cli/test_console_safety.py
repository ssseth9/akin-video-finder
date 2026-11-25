"""Tests for the console guard the CLI installs before it prints anything.

A scan's error summary carries the paths that failed, and a path may hold
Cyrillic or CJK characters. On Windows a redirected stdout is encoded with the
locale code page (cp1252 here), so printing such a path raised
``UnicodeEncodeError`` and ``akin-video-finder status`` died after the scan header —
the rest of the report, including the errors it was called to show, was lost.
"""

from __future__ import annotations

import io
import sys

import pytest

import akin_video_finder.cli.main as cli_main
from akin_video_finder.cli.main import _make_console_safe


class _Pipe(io.BytesIO):
    """A buffer that reports itself as redirected, the way a pipe does."""

    def isatty(self) -> bool:
        return False


class _Console(io.BytesIO):
    """A buffer that reports itself as a console handle."""

    def isatty(self) -> bool:
        return True


def _stream(buffer: io.BytesIO, encoding: str) -> io.TextIOWrapper:
    """Wrap a buffer the way ``sys.stdout`` is wrapped when the CLI starts."""
    return io.TextIOWrapper(buffer, encoding=encoding, errors="strict")


class TestMakeConsoleSafe:
    """Tests for _make_console_safe function."""

    def test_a_redirected_stream_prints_a_cyrillic_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failing case: a cp1252 pipe and a folder named in Cyrillic."""
        stream = _stream(_Pipe(), "cp1252")
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", _stream(_Pipe(), "cp1252"))

        _make_console_safe()

        stream.write("e:\\Видео\\Пример\\file.wmv\n")
        stream.flush()
        assert "Видео" in stream.buffer.getvalue().decode("utf-8")

    def test_a_redirected_stream_is_switched_to_utf8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """UTF-8 matches the reports, so a path survives instead of being replaced."""
        stream = _stream(_Pipe(), "cp1252")
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", _stream(_Pipe(), "cp1252"))

        _make_console_safe()

        assert stream.encoding is not None
        assert stream.encoding.lower().replace("-", "") == "utf8"
        assert stream.errors == "replace"

    def test_a_console_handle_keeps_its_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A console handle is already UTF-8; only the error mode needs widening."""
        stream = _stream(_Console(), "utf-8")
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", _stream(_Console(), "utf-8"))

        _make_console_safe()

        assert stream.encoding is not None
        assert stream.encoding.lower().replace("-", "") == "utf8"
        assert stream.errors == "replace"

    def test_a_stream_without_reconfigure_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pytest's capture objects and other stand-ins must not crash the CLI."""

        class _Plain:
            def write(self, text: str) -> int:
                return len(text)

        monkeypatch.setattr(sys, "stdout", _Plain())
        monkeypatch.setattr(sys, "stderr", _Plain())

        _make_console_safe()

    def test_a_stream_that_refuses_reconfigure_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A closed or detached stream raises on reconfigure; the CLI still runs."""

        class _Refusing(_Console):
            def isatty(self) -> bool:
                raise ValueError("detached")

        monkeypatch.setattr(sys, "stdout", _stream(_Refusing(), "cp1252"))
        monkeypatch.setattr(sys, "stderr", _stream(_Pipe(), "cp1252"))

        _make_console_safe()


class TestMainRunsTheGuard:
    """The guard must run before the command does, or the first echo can still die."""

    def test_main_widens_the_streams_before_the_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Order matters: a command that prints before the guard is unprotected."""
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_make_console_safe", lambda: calls.append("guard"))
        monkeypatch.setattr(cli_main, "app", lambda: calls.append("app"))

        cli_main.main()

        assert calls == ["guard", "app"]

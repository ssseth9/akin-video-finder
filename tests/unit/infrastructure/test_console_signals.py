"""Tests for the Ctrl+C guard the GUI installs.

The GUI is the one place where Ctrl+C is not a stop: in a window the key is a
copy shortcut, and the window's Stop button is the only stop. The CLI is not
guarded — a terminal has no Stop button, so ``scan`` keeps Ctrl+C as its
graceful cancel.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator

import pytest

from akin_video_finder.infrastructure.console_signals import ignore_ctrl_c


@pytest.fixture()
def restore_sigint() -> Iterator[None]:
    """Put the SIGINT handler back afterwards, whatever the test did to it."""
    original = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, signal.SIG_DFL if original is None else original)


class TestIgnoreCtrlC:
    """Tests for ignore_ctrl_c function."""

    def test_sets_the_ignore_handler(self, restore_sigint: None) -> None:
        """The handler the OS consults is SIG_IGN, not a Python callable."""
        ignore_ctrl_c()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN

    def test_a_real_ctrl_c_is_swallowed(self, restore_sigint: None) -> None:
        """The real signal, not a mocked handler: raising it must not raise."""
        signal.signal(signal.SIGINT, signal.default_int_handler)
        ignore_ctrl_c()
        signal.raise_signal(signal.SIGINT)

    def test_the_guard_is_what_swallows_it(self, restore_sigint: None) -> None:
        """Without the guard the same raise is a KeyboardInterrupt."""
        signal.signal(signal.SIGINT, signal.default_int_handler)
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)

        ignore_ctrl_c()
        signal.raise_signal(signal.SIGINT)

    def test_off_the_main_thread_is_a_no_op(self, restore_sigint: None) -> None:
        """run_gui may be called from an embedding thread; there is no handler."""
        errors: list[BaseException] = []

        def call() -> None:
            try:
                ignore_ctrl_c()
            except BaseException as exc:  # noqa: BLE001 - the test is the assertion
                errors.append(exc)

        thread = threading.Thread(target=call)
        thread.start()
        thread.join()
        assert errors == []

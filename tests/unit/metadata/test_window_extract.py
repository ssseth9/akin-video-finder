"""extract_gray_window: one sequential decode for a dense sweep.

The timeline-offset probe needs every second of both files' first minutes.
Reusing the seek-based extractor would cost one ffmpeg input per sample
(180 of them); this pass decodes the window once, so the measurement is
affordable.
"""

import subprocess
from pathlib import Path

import pytest

from akin_video_finder.metadata import frames


class _Captured:
    """Records what the extractor asked ffmpeg to do."""

    def __init__(self) -> None:
        self.args: list[str] = []
        self.timeout: int = 0
        self.stdout: bytes = b""
        self.calls: int = 0


@pytest.fixture()
def captured() -> _Captured:
    return _Captured()


def _install(monkeypatch: pytest.MonkeyPatch, captured: _Captured, returncode: int = 0) -> None:
    def fake_run(args: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
        captured.calls += 1
        captured.args = args
        captured.timeout = timeout
        return subprocess.CompletedProcess(args, returncode, captured.stdout, b"")

    monkeypatch.setattr(frames, "run_ffmpeg_bounded", fake_run)


class TestWindowExtraction:
    def test_window_is_one_pass_that_yields_frames_in_order(
        self, monkeypatch: pytest.MonkeyPatch, captured: _Captured
    ) -> None:
        wanted = [bytes([value]) * 16 for value in range(3)]
        captured.stdout = b"".join(wanted)
        _install(monkeypatch, captured)

        out = frames.extract_gray_window(Path("e:/vids/a.mp4"), 0.0, 3.0, 1.0, width=4, height=4)

        assert out == wanted
        assert captured.calls == 1  # not one process per frame
        assert captured.args[captured.args.index("-ss") + 1] == "0.000"
        assert captured.args[captured.args.index("-t") + 1] == "3.000"
        assert captured.args[captured.args.index("-vf") + 1] == "fps=1,scale=4:4"
        assert captured.args[-1] == "-"
        assert "-an" in captured.args

    def test_a_partial_trailing_frame_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch, captured: _Captured
    ) -> None:
        captured.stdout = bytes(16) + bytes(16) + b"half"
        _install(monkeypatch, captured)

        out = frames.extract_gray_window(Path("e:/vids/a.mp4"), 0.0, 3.0, 1.0, width=4, height=4)

        assert len(out) == 2
        assert all(len(frame) == 16 for frame in out)

    def test_timeout_scales_with_the_window(
        self, monkeypatch: pytest.MonkeyPatch, captured: _Captured
    ) -> None:
        captured.stdout = bytes(16)
        _install(monkeypatch, captured)

        frames.extract_gray_window(Path("e:/vids/a.mp4"), 0.0, 180.0, 1.0, width=4, height=4)

        assert captured.timeout >= 540

    def test_empty_window_never_starts_ffmpeg(
        self, monkeypatch: pytest.MonkeyPatch, captured: _Captured
    ) -> None:
        _install(monkeypatch, captured)

        assert frames.extract_gray_window(Path("a.mp4"), 0.0, 0.0, 1.0) == []
        assert frames.extract_gray_window(Path("a.mp4"), 0.0, 10.0, 0.0) == []
        assert captured.calls == 0

    def test_failed_decode_returns_no_frames(
        self, monkeypatch: pytest.MonkeyPatch, captured: _Captured
    ) -> None:
        captured.stdout = b""
        _install(monkeypatch, captured, returncode=1)

        assert frames.extract_gray_window(Path("a.mp4"), 0.0, 10.0, 1.0) == []

    def test_partial_output_from_a_killed_decode_is_kept(
        self, monkeypatch: pytest.MonkeyPatch, captured: _Captured
    ) -> None:
        """The probe can measure with a short window; silence is the only failure."""
        captured.stdout = bytes(16) * 5
        _install(monkeypatch, captured, returncode=1)

        out = frames.extract_gray_window(Path("a.mp4"), 0.0, 10.0, 1.0, width=4, height=4)

        assert len(out) == 5

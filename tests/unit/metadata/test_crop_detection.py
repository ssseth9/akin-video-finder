"""Consensus black-bar detection.

A single cropdetect window can see bars that exist only in an intro or miss
bars that start later. The center probe decides whether the two side probes
are worth paying for, and a crop is returned only when all three agree.
"""

from pathlib import Path

import pytest

from akin_video_finder.metadata import frames


def _fake_probe(monkeypatch: pytest.MonkeyPatch, boxes):
    """Patch the single-window probe with a scripted box sequence."""
    calls: list[float] = []

    def probe(file_path, start_seconds, **kwargs):
        calls.append(start_seconds)
        return boxes[len(calls) - 1]

    monkeypatch.setattr(frames, "_probe_crop_box", probe)
    return calls


class TestConsensusCrop:
    def test_no_bars_costs_one_probe_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_probe(monkeypatch, [(160, 110, 0, 0)])
        result = frames.detect_crop(Path("clip.mp4"), 60.0, frame_width=160, frame_height=110)
        assert result is None
        assert len(calls) == 1

    def test_three_agreeing_probes_return_the_center_box(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_probe(
            monkeypatch,
            [(160, 90, 0, 10), (160, 90, 0, 10), (160, 90, 0, 10)],
        )
        result = frames.detect_crop(Path("clip.mp4"), 60.0, frame_width=160, frame_height=110)
        assert result == "crop=160:90:0:10"
        assert len(calls) == 3
        assert calls[0] == 30.0
        assert calls[1] == 12.0
        assert calls[2] >= 46.0

    def test_origin_within_two_pixels_agrees(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_probe(
            monkeypatch,
            [(160, 90, 0, 10), (160, 90, 2, 8), (160, 90, 1, 12)],
        )
        result = frames.detect_crop(Path("clip.mp4"), 60.0, frame_width=160, frame_height=110)
        assert result == "crop=160:90:0:10"

    def test_side_disagreement_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_probe(
            monkeypatch,
            [(160, 90, 0, 10), (160, 90, 0, 10), (160, 80, 0, 15)],
        )
        assert frames.detect_crop(
            Path("clip.mp4"), 60.0, frame_width=160, frame_height=110
        ) is None

    def test_missing_side_probe_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_probe(monkeypatch, [(160, 90, 0, 10), None, (160, 90, 0, 10)])
        assert frames.detect_crop(
            Path("clip.mp4"), 60.0, frame_width=160, frame_height=110
        ) is None

    def test_short_video_skips_consensus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_probe(monkeypatch, [(160, 90, 0, 10)])
        result = frames.detect_crop(Path("clip.mp4"), 4.0, frame_width=160, frame_height=110)
        assert result == "crop=160:90:0:10"
        assert len(calls) == 1

    def test_unknown_dimensions_skip_detection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_probe(monkeypatch, [(160, 90, 0, 10)])
        assert frames.detect_crop(Path("clip.mp4"), 60.0) is None
        assert calls == []


class TestProbeParsing:
    def test_last_union_box_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeProc:
            def communicate(self, timeout=None):
                return b"", b"crop=160:90:0:10\ncrop=158:88:1:11\n"

        monkeypatch.setattr(frames.subprocess, "Popen", lambda *a, **k: FakeProc())
        box = frames._probe_crop_box(Path("clip.mp4"), 10.0)
        assert box == (158, 88, 1, 11)

    def test_no_box_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeProc:
            def communicate(self, timeout=None):
                return b"", b"no crop output\n"

        monkeypatch.setattr(frames.subprocess, "Popen", lambda *a, **k: FakeProc())
        assert frames._probe_crop_box(Path("clip.mp4"), 10.0) is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeProc:
            def communicate(self, timeout=None):
                raise frames.subprocess.TimeoutExpired("ffmpeg", timeout)

        monkeypatch.setattr(frames.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(frames, "kill_process_tree", lambda proc: None)
        assert frames._probe_crop_box(Path("clip.mp4"), 10.0) is None

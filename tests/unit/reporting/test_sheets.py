"""One FFmpeg per tile: a slow seek must cost a tile, not the sheet.

Sheets used to render four inputs to a single FFmpeg process. When any one
of the four seeks stalled the batch died at the cap and the sheet was lost
with it — 81 such timeouts in one library's log, and every one of them left
the review card sitting on "rendering…". Each tile now has its own process
and its own slice of the budget.
"""

import subprocess
from pathlib import Path

import pytest

from akin_video_finder.reporting import sheets


class FakeFfmpeg:
    """Stands in for ``run_ffmpeg_bounded``: records calls, writes the files."""

    def __init__(self, stall: set[float] | None = None, xstack_ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[int] = []
        self.stall = stall or set()
        self.xstack_ok = xstack_ok

    def __call__(self, args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(args))
        self.timeouts.append(timeout_seconds)
        if "xstack=inputs" in " ".join(args):
            if self.xstack_ok:
                Path(args[-1]).write_bytes(b"jpeg")
            return subprocess.CompletedProcess(args, 0 if self.xstack_ok else 1, b"", b"boom")
        position = float(args[args.index("-ss") + 1])
        if position in self.stall:
            raise subprocess.TimeoutExpired(args, timeout_seconds)
        Path(args[-1]).write_bytes(b"png")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def tile_calls(self) -> list[list[str]]:
        return [c for c in self.calls if "xstack=inputs" not in " ".join(c)]


@pytest.fixture()
def fake(monkeypatch):
    recorder = FakeFfmpeg()

    def _install(**kwargs):
        recorder.__init__(**kwargs)
        monkeypatch.setattr(sheets, "run_ffmpeg_bounded", recorder)
        return recorder

    return _install


def _render(tmp_path: Path, seconds: float = 600.0) -> bool:
    return sheets.generate_contact_sheet(
        tmp_path / "video.mp4",
        seconds,
        tmp_path / "sheet.jpg",
        timeout_seconds=60,
    )


def test_every_tile_gets_its_own_single_input_call(tmp_path, fake):
    ffmpeg = fake()
    assert _render(tmp_path)

    tiles = sheets.SHEET_COLUMNS * sheets.SHEET_ROWS
    assert len(ffmpeg.tile_calls()) == tiles
    for call in ffmpeg.tile_calls():
        # One input per process: four seeks in one command is what turned a
        # single stall into a lost sheet
        assert call.count("-i") == 1
        assert call.count("-ss") == 1


def test_every_tile_skips_non_keyframes(tmp_path, fake):
    """A preview tile needs a representative frame, not the exact timestamp.

    Decoding forward from the keyframe the seek landed on was most of a
    tile's cost; skipping non-keyframes measured 0.25s against 0.39s.
    """
    ffmpeg = fake()
    assert _render(tmp_path)

    for call in ffmpeg.tile_calls():
        assert call[call.index("-skip_frame") + 1] == "nokey"


def test_the_tile_maps_the_video_and_not_the_cover_art(tmp_path, fake):
    """`0:v` is every video stream; a file with cover art has two.

    Mapping both asked the image muxer for two outputs at one path, so it
    wrote neither and ffmpeg still exited 0 — "Output file is empty, nothing
    was encoded". Half the sheets in a real library failed that way, with
    nothing in the log but a 60-second timeout. `0:V` is video only.
    """
    ffmpeg = fake()
    assert _render(tmp_path)

    for call in ffmpeg.tile_calls():
        assert call[call.index("-map") + 1] == "0:V:0"


def test_one_stalled_tile_still_produces_the_sheet(tmp_path, fake):
    ffmpeg = fake()
    # The third sample stalls; the rest are fine
    position = sheets.signature_timestamps(600.0, sheets.SHEET_COLUMNS * sheets.SHEET_ROWS - 4)[2]
    ffmpeg.stall = {round(position, 3)}

    assert _render(tmp_path)
    assert (tmp_path / "sheet.jpg").exists()
    # The hole was filled from a sibling, so all twelve inputs still stack
    xstack = next(c for c in ffmpeg.calls if "xstack=inputs" in " ".join(c))
    assert xstack.count("-i") == sheets.SHEET_COLUMNS * sheets.SHEET_ROWS


def test_every_tile_stalling_is_a_failure(tmp_path, fake):
    positions = sheets.signature_timestamps(600.0, sheets.SHEET_COLUMNS * sheets.SHEET_ROWS - 4)
    fake(stall={round(p, 3) for p in positions} | {0.0})

    assert not _render(tmp_path)
    assert not (tmp_path / "sheet.jpg").exists()


def test_no_tile_may_spend_the_whole_budget(tmp_path, fake):
    ffmpeg = fake()
    assert _render(tmp_path)

    # A third of the allowance each, so a hung sample cannot starve the rest
    tile_timeouts = [
        timeout
        for call, timeout in zip(ffmpeg.calls, ffmpeg.timeouts, strict=True)
        if "xstack=inputs" not in " ".join(call)
    ]
    assert tile_timeouts
    assert max(tile_timeouts) <= 20


def test_a_spent_budget_stops_the_tiles(tmp_path, fake, monkeypatch):
    ffmpeg = fake()
    clock = [0.0]

    def _now() -> float:
        clock[0] += 7.0  # every call burns seven seconds of the allowance
        return clock[0]

    monkeypatch.setattr(sheets.time, "monotonic", _now)
    # Eight tiles arrived before the allowance ran out: the sheet is built
    # from those, with the holes filled from a sibling. A partial sheet beats
    # no sheet — but the remaining tiles must not be attempted
    assert _render(tmp_path)
    assert (tmp_path / "sheet.jpg").exists()
    assert len(ffmpeg.tile_calls()) < sheets.SHEET_COLUMNS * sheets.SHEET_ROWS


def test_a_failed_compose_is_a_failure(tmp_path, fake):
    fake(xstack_ok=False)
    assert not _render(tmp_path)
    assert not (tmp_path / "sheet.jpg").exists()

"""SheetLoader queue semantics — the regression-prone part of the GUI.

Uses workers=0 so nothing consumes the queue: tests drain it manually
and apply the same staleness rule the worker does, making ordering
deterministic without thread timing.
"""

import os
import queue
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.gui.app import SheetLoader  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def loader(tmp_path, qapp):
    return SheetLoader(tmp_path / "sheets", "ffmpeg", workers=0)


def _row(i: int) -> dict:
    return {
        "id": i,
        "original_path": f"/v/f{i}.mp4",
        "mtime_ns": i,
        "presence": "present",
    }


def _drain(loader: SheetLoader) -> list[tuple[str, int]]:
    """Pull every queued item applying the worker's stale rule.

    Returns (kind, file_id) where kind is 'render' or 'stale'.
    """
    out = []
    while True:
        try:
            _prio, _tie, seq, row, _dur, path = loader._sheet_queue.get_nowait()
        except queue.Empty:
            return out
        entry = loader.pending.get(path)
        if entry is None or entry[2] != seq:
            out.append(("stale", row["id"]))
        else:
            out.append(("render", row["id"]))


class TestOrdering:
    def test_warmup_runs_in_review_order(self, loader):
        for i in (1, 2, 3):
            loader.request(_row(i), 10.0, priority=2)
        assert _drain(loader) == [("render", 1), ("render", 2), ("render", 3)]

    def test_interactive_newest_first(self, loader):
        for i in (1, 2, 3):
            loader.request(_row(i), 10.0)
        assert _drain(loader) == [("render", 3), ("render", 2), ("render", 1)]

    def test_tiers_interactive_prefetch_warmup(self, loader):
        loader.request(_row(1), 10.0, priority=2)
        loader.request(_row(2), 10.0, priority=1)
        loader.request(_row(3), 10.0)
        assert _drain(loader) == [("render", 3), ("render", 2), ("render", 1)]

    def test_interactive_supersedes_queued_warmup(self, loader):
        loader.request(_row(1), 10.0, priority=2)
        old_seq = loader._sheet_seq
        loader.request(_row(1), 10.0)  # interactive request on same file
        # New generation owns the pending entry; the old queue entry is
        # stale and gets skipped when pulled
        assert loader.pending[loader.sheets_dir / "file_1_1.jpg"][2] > old_seq
        assert _drain(loader) == [("render", 1), ("stale", 1)]

    def test_warmup_never_supersedes_queued(self, loader):
        loader.request(_row(1), 10.0)  # interactive queued
        loader.request(_row(1), 10.0, priority=2)  # warm-up is a no-op
        assert _drain(loader) == [("render", 1)]


class TestExpiryAndFailure:
    def test_queued_job_never_expires(self, loader):
        loader.request(_row(1), 10.0, priority=2)
        failed = []
        loader.sheet_failed.connect(failed.append)
        loader._expire_pending()
        assert failed == []
        assert len(loader.pending) == 1  # deadline starts at worker pull

    def test_inflight_job_expires(self, loader):
        loader.request(_row(1), 10.0)
        path = loader.sheets_dir / "file_1_1.jpg"
        fid, _dl, seq, prio = loader.pending[path]
        # Simulate a worker having pulled it long past the deadline
        loader.pending[path] = (fid, time.monotonic() - 1, seq, prio)
        failed = []
        loader.sheet_failed.connect(failed.append)
        loader._expire_pending()
        assert failed == [1]
        assert not loader.pending

    def test_missing_file_fails_without_budget(self, loader):
        failed = []
        loader.sheet_failed.connect(failed.append)
        loader.request({**_row(1), "presence": "missing"}, 10.0)
        assert failed == [1]
        assert loader._sheet_queue.empty()
        assert not loader.failed  # retry budget not spent on missing

    def test_cached_sheet_emits_ready(self, loader):
        path = loader.sheets_dir / "file_1_1.jpg"
        path.write_bytes(b"x")
        ready = []
        loader.sheet_ready.connect(lambda fid, p: ready.append(fid))
        loader.request(_row(1), 10.0)
        assert ready == [1]
        assert loader._sheet_queue.empty()

    def test_retry_budget_suppresses_then_decays(self, loader):
        path = loader.sheets_dir / "file_1_1.jpg"
        loader.failed[path] = (loader._max_attempts, time.monotonic())
        failed = []
        loader.sheet_failed.connect(failed.append)
        loader.request(_row(1), 10.0)
        assert failed == [1]  # suppressed within cooldown
        loader.request(_row(1), 10.0)
        assert loader._sheet_queue.empty()
        # Budget older than the cooldown decays — retry allowed again
        loader.failed[path] = (loader._max_attempts, time.monotonic() - 400)
        loader.request(_row(1), 10.0)
        assert not loader._sheet_queue.empty()


class TestSerializedRendering:
    """One renderer at a time — concurrent Popen lifecycle used to hang.

    The report captured four to six worker threads parked in
    ``subprocess``/``CloseHandle`` while every preview stayed on
    "rendering…". Rendering is serialized and results are emitted by a
    GUI-thread pump.
    """

    def _video(self, tmp_path: Path, name: str) -> dict:
        path = tmp_path / name
        path.write_bytes(b"x")
        return {
            "id": abs(hash(name)) % 10_000,
            "original_path": str(path),
            "mtime_ns": 1,
            "presence": "present",
        }

    def test_renders_never_overlap_even_when_more_workers_are_asked(
        self, tmp_path, qapp, monkeypatch
    ):
        guard = threading.Lock()
        active = 0
        peak = 0

        def fake(src, duration, out, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.15)
            Path(out).write_bytes(b"x")
            with guard:
                active -= 1
            return True

        monkeypatch.setattr(gui_app, "generate_contact_sheet", fake)
        loader = SheetLoader(tmp_path / "sheets", "ffmpeg", workers=4)
        ready: list[int] = []
        loader.sheet_ready.connect(lambda fid, path: ready.append(fid))
        rows = [self._video(tmp_path, f"v{i}.mp4") for i in range(4)]
        for row in rows:
            loader.request(row, 10.0)

        deadline = time.monotonic() + 20
        while len(ready) < len(rows) and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        loader.shutdown()

        assert sorted(ready) == sorted(row["id"] for row in rows)
        assert peak == 1

    def test_shutdown_stops_the_pump_and_drops_the_queue(self, loader):
        loader.request(_row(1), 10.0)
        ready: list[int] = []
        failed: list[int] = []
        loader.sheet_ready.connect(lambda fid, path: ready.append(fid))
        loader.sheet_failed.connect(failed.append)

        loader.shutdown()
        loader._drain_results()

        assert not loader._pump.isActive()
        assert not loader.watchdog.isActive()
        assert loader.pending == {}
        assert loader._sheet_queue.empty()
        assert ready == []
        assert failed == []

    def test_a_stalled_render_is_replaced(self, tmp_path, qapp, monkeypatch):
        started = threading.Event()
        release = threading.Event()

        def fake(src, duration, out, **kwargs):
            if Path(src).name == "stuck.mp4":
                started.set()
                release.wait(10)
                return False
            Path(out).write_bytes(b"x")
            return True

        monkeypatch.setattr(gui_app, "generate_contact_sheet", fake)
        loader = SheetLoader(tmp_path / "sheets", "ffmpeg", workers=1)
        stuck = self._video(tmp_path, "stuck.mp4")
        good = self._video(tmp_path, "good.mp4")
        ready: list[int] = []
        failed: list[int] = []
        loader.sheet_ready.connect(lambda fid, path: ready.append(fid))
        loader.sheet_failed.connect(failed.append)

        loader.request(stuck, 10.0)
        assert started.wait(5)
        loader.request(good, 10.0)

        path = loader.sheets_dir / f"file_{stuck['id']}_1.jpg"
        fid, _deadline, seq, prio = loader.pending[path]
        loader.pending[path] = (fid, time.monotonic() - 1, seq, prio)
        loader._expire_pending()

        assert failed == [stuck["id"]]
        assert loader._worker_gen == 2
        deadline = time.monotonic() + 10
        while not ready and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        release.set()
        loader.shutdown()
        assert ready == [good["id"]]

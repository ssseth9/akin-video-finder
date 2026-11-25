"""The ffmpeg watchdog stops runaways, and nothing else.

It used to kill any child whenever *system-wide* free RAM fell below a
floor. Windows reports standby cache as used and trims working sets by
design, so a healthy machine routinely sits under such a floor — every
render running at that moment was killed, and the file it was reading was
added to the "pathological" set, so later passes fed healthy files smaller
batches as if they were damaged. Deciding what the machine can afford is
the OS's job; what belongs here is one child's own runaway growth.
"""

import sys

import pytest

from akin_video_finder.metadata import frames


@pytest.fixture(autouse=True)
def clean_pathological():
    before = set(frames._ffmpeg_pathological)
    yield
    frames._ffmpeg_pathological.clear()
    frames._ffmpeg_pathological.update(before)


class TestCapSizing:
    """The ceiling has to make sense on the machine it runs on.

    A fixed 24GB could never fire on a 16GB box — the OS thrashes first — so
    the guard was missing exactly where a runaway decoder hurts most, while
    on a 128GB box it constrained work that had room to spare.
    """

    @pytest.fixture(autouse=True)
    def fresh(self):
        frames._mem_cap_cache = None
        yield
        frames._mem_cap_cache = None

    @pytest.mark.parametrize(
        ("total_gb", "expected_gb"),
        [
            (4, 4),  # 2GB would be below the floor: keeps 4
            (8, 4),  # half of 8 is the floor exactly
            (16, 8),
            (32, 16),
            (64, 24),  # half is 32, over the ceiling
            (128, 24),
        ],
    )
    def test_the_ceiling_follows_installed_ram(self, monkeypatch, total_gb, expected_gb):
        monkeypatch.setattr(frames, "_total_phys_bytes", lambda: total_gb * 1024**3)
        assert frames.ffmpeg_mem_cap_bytes() == expected_gb * 1024**3

    def test_unknown_ram_keeps_the_ceiling(self, monkeypatch):
        # GlobalMemoryStatusEx failed, or not Windows: no worse than before
        monkeypatch.setattr(frames, "_total_phys_bytes", lambda: 0)
        assert frames.ffmpeg_mem_cap_bytes() == frames._FFMPEG_MEM_CAP_CEILING

    def test_the_cap_is_read_once(self, monkeypatch):
        calls = []

        def _total():
            calls.append(1)
            return 16 * 1024**3

        monkeypatch.setattr(frames, "_total_phys_bytes", _total)
        frames.ffmpeg_mem_cap_bytes()
        frames.ffmpeg_mem_cap_bytes()
        frames.ffmpeg_mem_cap_bytes()
        assert len(calls) == 1  # not once per 0.2s poll


def test_a_child_over_the_ceiling_is_killed_and_called_heavy(monkeypatch, tmp_path):
    victim = tmp_path / "big.mp4"
    victim.write_bytes(b"x")
    monkeypatch.setattr(frames, "_working_set_bytes", lambda _h: frames.ffmpeg_mem_cap_bytes() + 1)

    result = frames.run_ffmpeg_bounded([sys.executable, "-c", "import time; time.sleep(5)"], 30)

    assert result.returncode != 0  # killed before it could sleep
    assert frames.ffmpeg_pathological(str(victim)) is False  # nothing to condemn yet

    result = frames.run_ffmpeg_bounded(
        [sys.executable, "-c", "import time; time.sleep(5)", "-i", str(victim)], 30
    )
    assert result.returncode != 0
    assert frames.ffmpeg_pathological(str(victim)) is True


def test_a_child_under_the_ceiling_is_left_alone(monkeypatch):
    monkeypatch.setattr(frames, "_working_set_bytes", lambda _h: 512 * 1024**2)

    result = frames.run_ffmpeg_bounded([sys.executable, "-c", "print('done')"], 30)

    assert result.returncode == 0
    assert b"done" in result.stdout


def test_short_children_are_not_delayed_by_the_poll_interval():
    """The poll interval is a tail-latency budget, not a sleep for the child.

    At 0.2s a contact sheet's 13 FFmpeg calls could lose over a second after
    their children had already exited. The interval is pinned so a later
    change has to argue with this test.
    """
    assert frames._FFMPEG_POLL_INTERVAL_S == 0.05


def test_the_watchdog_never_looks_at_system_memory():
    """A free-RAM floor killed healthy renders; it must not come back.

    ``GlobalMemoryStatusEx`` counts standby cache as used, so the number it
    reports has little to do with whether the machine can afford one more
    decoder. Nothing in this module may consult it.
    """
    assert not hasattr(frames, "_FFMPEG_AVAIL_FLOOR_BYTES")
    assert not hasattr(frames, "_available_phys_bytes")
    with pytest.raises(OSError):  # sanity: a missing binary is still an error
        frames.run_ffmpeg_bounded(["definitely-not-a-real-binary"], 5)

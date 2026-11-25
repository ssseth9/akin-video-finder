"""Timeline-offset probe: a prepended ad must not hide a duplicate.

A copy that starts with an advert — or an intro, or a station ident — has
every frame shifted against the other copy. Nothing that assumes "frame i of
A is the same moment as frame i of B" can see such a pair: the
corresponding-timeline comparison reads distance ~0.16 at every index, the
sparse gate witnesses no shared frame (its samples land ~20s apart in
content), and the trim path only runs when the *durations* diverge, which a
20s ad plus a little extra tail keeps inside the 2% tolerance. The pair used
to reach review never. These tests pin the probe that finds it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from akin_video_finder.application import perceptual_match as pm
from akin_video_finder.application.perceptual_match import (
    CLASS_POSSIBLE,
    CLASS_VERY_LIKELY,
    OffsetProbeResult,
    PerceptualMatchResult,
    PerceptualMatchService,
)
from akin_video_finder.configuration.models import MatchingConfig

SIZE = pm._OFFSET_PROBE_SIZE
FRAME_BYTES = SIZE * SIZE
AD_SECONDS = 20


def _moment(index: int) -> bytes:
    """A distinct, reproducible frame for one moment of content."""
    value = (index * 2654435761 + 12345) & 0x7FFFFFFF
    out = bytearray(FRAME_BYTES)
    for i in range(FRAME_BYTES):
        value = (value * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = (value >> 16) & 0xFF
    return bytes(out)


CONTENT = [_moment(i) for i in range(400)]
ADVERT = [_moment(5000 + i) for i in range(AD_SECONDS)]
OTHER = [_moment(9000 + i) for i in range(400)]


def _files(dur_long: float = 3041.0, dur_short: float = 3021.0) -> tuple[dict[str, Any], dict]:
    long_row: dict[str, Any] = {
        "id": 1,
        "original_path": "e:/vids/long.mp4",
        "normalized_path_key": "e:/vids/long.mp4",
        "_duration": dur_long,
    }
    short_row: dict[str, Any] = {
        "id": 2,
        "original_path": "e:/vids/short.mp4",
        "normalized_path_key": "e:/vids/short.mp4",
        "_duration": dur_short,
    }
    return long_row, short_row


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ad_seconds: int = AD_SECONDS,
    short_content: list[bytes] | None = None,
) -> None:
    """Fake both extractors against an in-memory pair of timelines."""
    short_content = short_content if short_content is not None else CONTENT

    def long_at(seconds: float) -> bytes:
        index = int(seconds)
        if index < ad_seconds:
            return ADVERT[index % len(ADVERT)]
        return CONTENT[(index - ad_seconds) % len(CONTENT)]

    def short_at(seconds: float) -> bytes:
        return short_content[int(seconds) % len(short_content)]

    def is_long(path: Any) -> bool:
        return "long" in str(path)

    def fake_window(
        path: Any,
        start_seconds: float,
        window_seconds: float,
        fps: float,
        **kwargs: Any,
    ) -> list[bytes]:
        at = long_at if is_long(path) else short_at
        return [at(i + start_seconds) for i in range(int(window_seconds))]

    def fake_frames(path: Any, timestamps: list[float], **kwargs: Any) -> list[bytes]:
        at = long_at if is_long(path) else short_at
        return [at(t) for t in timestamps]

    monkeypatch.setattr(pm, "extract_gray_window", fake_window)
    monkeypatch.setattr(pm, "extract_gray_frames", fake_frames)


class _VerdictRepo:
    """Offset-probe verdict store, in memory."""

    def __init__(self) -> None:
        self.verdicts: dict[tuple[int, int], dict[str, Any]] = {}
        self.saved: list[tuple[int, int]] = []
        self.hit_pairs: list[tuple[int, int]] = []

    def get_offset_probe_hit_pairs(self) -> list[tuple[int, int]]:
        return list(self.hit_pairs)

    def get_offset_verdicts(
        self, algorithm_version: str, preferred_version: str | None = None
    ) -> dict[tuple[int, int], dict[str, Any]]:
        return dict(self.verdicts)

    def save_offset_verdict(
        self,
        file_a_id: int,
        file_b_id: int,
        algorithm_version: str,
        size_a: int,
        mtime_a: int,
        size_b: int,
        mtime_b: int,
        evidence: str | None = None,
    ) -> None:
        self.saved.append((file_a_id, file_b_id))
        self.verdicts[(file_a_id, file_b_id)] = {
            "file_a_id": file_a_id,
            "file_b_id": file_b_id,
            "algorithm_version": algorithm_version,
            "size_a": size_a,
            "mtime_a": mtime_a,
            "size_b": size_b,
            "mtime_b": mtime_b,
            "evidence": evidence,
        }


def _service(repo: Any = None, **overrides: Any) -> PerceptualMatchService:
    return PerceptualMatchService(repo or _VerdictRepo(), MatchingConfig(**overrides))  # type: ignore[arg-type]


class TestMeasureLeadOffset:
    """The head of both files is enough to *propose* a shift."""

    def test_prepended_advert_is_measured(self) -> None:
        lead_long = [ADVERT[i] for i in range(AD_SECONDS)] + [
            CONTENT[i] for i in range(180 - AD_SECONDS)
        ]
        lead_short = [CONTENT[i] for i in range(180)]

        measured = _service()._measure_lead_offset(lead_long, lead_short)

        assert measured is not None
        offset, matches = measured
        assert offset == pytest.approx(float(AD_SECONDS))
        assert matches >= 100

    def test_unrelated_heads_measure_nothing(self) -> None:
        measured = _service()._measure_lead_offset(
            [OTHER[i] for i in range(180)], [CONTENT[i] for i in range(180)]
        )
        assert measured is None

    def test_windows_too_short_to_judge_measure_nothing(self) -> None:
        assert _service()._measure_lead_offset(CONTENT[:4], CONTENT[:4]) is None


class TestProbeTimelineOffset:
    """Only the whole-timeline check may confirm a shift."""

    def test_shifted_copy_is_confirmed_at_every_checkpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        long_row, short_row = _files()

        probe = _service()._probe_timeline_offset(long_row, short_row)

        assert probe is not None
        assert probe.offset_seconds == pytest.approx(float(AD_SECONDS))
        assert probe.checked == len(pm._OFFSET_VERIFY_FRACTIONS)
        assert probe.verified == probe.checked
        assert min(probe.correlations) >= pm._OFFSET_MIN_CORRELATION

    def test_matching_head_but_different_film_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shared opening is not a shared film."""

        def head_window(
            path: Any, start_seconds: float, window_seconds: float, fps: float, **kwargs: Any
        ) -> list[bytes]:
            if "long" in str(path):
                return [ADVERT[i] for i in range(AD_SECONDS)] + [
                    CONTENT[i] for i in range(int(window_seconds) - AD_SECONDS)
                ]
            return [CONTENT[i] for i in range(int(window_seconds))]

        def body_frames(path: Any, timestamps: list[float], **kwargs: Any) -> list[bytes]:
            if "long" in str(path):
                return [CONTENT[(int(t) - AD_SECONDS) % len(CONTENT)] for t in timestamps]
            return [OTHER[int(t) % len(OTHER)] for t in timestamps]

        monkeypatch.setattr(pm, "extract_gray_window", head_window)
        monkeypatch.setattr(pm, "extract_gray_frames", body_frames)
        long_row, short_row = _files()

        probe = _service()._probe_timeline_offset(long_row, short_row)

        assert probe is None

        # The miss keeps the numbers it was judged on, so a later strictness
        # change re-judges them instead of decoding both files again
        _probe, decided, evidence = _service()._probe_timeline_offset_with_verdict(
            long_row, short_row
        )
        assert decided is True
        assert evidence is not None
        assert evidence["kind"] == "checkpoints"
        assert len(evidence["correlations"]) == len(evidence["distances"])
        assert len(evidence["distances"]) == len(evidence["divergences"])

    def test_identical_timelines_report_a_zero_shift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, ad_seconds=0)
        long_row, short_row = _files(dur_long=3021.0, dur_short=3021.0)

        probe = _service()._probe_timeline_offset(long_row, short_row)

        assert probe is not None
        assert probe.offset_seconds == 0.0
        assert probe.verified == probe.checked


class TestCollectOffsetProbes:
    """Which near-miss pairs deserve the extra extractions."""

    def _rows(self, *specs: tuple[int, float]) -> dict[int, dict[str, Any]]:
        return {
            fid: {
                "id": fid,
                "original_path": f"e:/vids/{fid}.mp4",
                "normalized_path_key": f"e:/vids/{fid}.mp4",
                "size": 1000 + fid,
                "mtime_ns": 5 + fid,
                "_duration": duration,
            }
            for fid, duration in specs
        }

    def test_a_dismissed_pair_is_not_resurrected(self) -> None:
        service = _service()
        rows = self._rows((1, 3021.0), (2, 3034.0))
        service._key_by_id = {fid: row["normalized_path_key"] for fid, row in rows.items()}
        service._dismissed = {
            tuple(sorted((rows[1]["normalized_path_key"], rows[2]["normalized_path_key"])))
        }
        result = PerceptualMatchResult()

        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert result.offset_probes == 0
        assert service._offset_hits == []

    def test_duration_gaps_beyond_the_scope_are_skipped(self) -> None:
        service = _service()
        rows = self._rows((1, 3000.0), (2, 1000.0))
        result = PerceptualMatchResult()

        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert result.offset_probes == 0

    def test_exact_groups_and_compared_pairs_are_skipped(self) -> None:
        service = _service()
        rows = self._rows((1, 3021.0), (2, 3034.0))
        result = PerceptualMatchResult()

        service._collect_offset_probes({(1, 2): 2}, rows, [], {1: 9, 2: 9}, result, None)
        assert result.offset_probes == 0

        service._collect_offset_probes(
            {(1, 2): 2}, rows, [(rows[1], rows[2], False)], {}, result, None
        )
        assert result.offset_probes == 0

    def test_a_compared_pair_is_recognised_whichever_order_it_was_stored(
        self,
    ) -> None:
        # Pairs are stored shorter-first, the index proposes them id-first:
        # matching on the stored order alone re-probed pairs twice
        service = _service()
        rows = self._rows((1, 3021.0), (2, 3034.0))
        result = PerceptualMatchResult()

        service._collect_offset_probes(
            {(1, 2): 2}, rows, [(rows[2], rows[1], False)], {}, result, None
        )

        assert result.offset_probes == 0

    def test_nearly_equal_lengths_come_first_and_all_candidates_are_probed(self) -> None:
        service = _service()
        rows = self._rows((1, 1000.0), (2, 1010.0), (3, 1080.0), (4, 1020.0))
        order: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            order.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes(
            {(1, 2): 2, (1, 3): 2, (1, 4): 2}, rows, [], {}, result, None
        )

        # gaps are 10s, 20s and 80s: closest first, and nothing is left over —
        # there is no per-scan cap to defer a candidate to a later scan
        assert order == [(1, 2), (1, 4), (1, 3)]
        assert result.offset_probes == 3
        assert result.offset_probes_candidates == 3

    def test_head_window_evidence_outranks_a_smaller_duration_gap(self) -> None:
        """A pair whose opening minutes match is probed before a pair that only
        looks close by length. Ordering by gap alone spent the phase on
        whichever two files happened to have similar durations."""

        service = _service()
        rows = self._rows((1, 1000.0), (2, 1001.0), (3, 1000.5), (4, 1020.0))
        order: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            order.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes(
            # (1, 2) is one second apart, (1, 4) twenty; (1, 4) is the one
            # whose opening actually matches
            {(1, 2): 3, (1, 3): 4, (1, 4): 70},
            rows,
            [],
            {},
            result,
            None,
        )

        assert order[0] == (1, 4)
        assert result.offset_probes == 3

    def test_each_probe_names_both_files(self) -> None:
        """A count says how far the phase is; only the paths say what it is
        looking at, which is what a slow scan has to be able to explain."""
        events: list[Any] = []
        service = _service()
        service._emit_progress = events.append  # type: ignore[method-assign]
        rows = self._rows((1, 3021.0), (2, 3034.0))
        service._probe_timeline_offset_with_verdict = lambda *a, **k: (None, True, {"kind": "lead"})  # type: ignore[method-assign]

        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, PerceptualMatchResult(), None)

        assert events[0].current_path == "e:/vids/2.mp4"  # the longer copy first
        assert events[0].peer_path == "e:/vids/1.mp4"

    def test_the_count_is_one_based_and_ends_at_the_total(self) -> None:
        """The event names the pair being probed, so the first of three reads
        "1/3" rather than "0/3" — a zero on a phase whose units take minutes
        reads as a scan that has not started."""
        events: list[Any] = []
        service = _service()
        service._emit_progress = events.append  # type: ignore[method-assign]
        rows = self._rows((1, 1000.0), (2, 1010.0), (3, 1080.0))
        service._probe_timeline_offset_with_verdict = lambda *a, **k: (None, True, {"kind": "lead"})  # type: ignore[method-assign]

        service._collect_offset_probes(
            {(1, 2): 2, (1, 3): 2, (2, 3): 2}, rows, [], {}, PerceptualMatchResult(), None
        )

        assert [event.completed_count for event in events] == [1, 2, 3]
        assert {event.total_count for event in events} == {3}

    def test_a_judged_pair_is_not_tested_again(self) -> None:
        """A scan that finds nothing must still leave the next one further
        along: without a verdict the same strongest pairs are re-probed
        every scan and the queue never drains."""
        repo = _VerdictRepo()
        service = _service(repo)
        rows = self._rows((1, 3021.0), (2, 3034.0))
        service._probe_timeline_offset_with_verdict = lambda *a, **k: (None, True, {"kind": "lead"})  # type: ignore[method-assign]

        first = PerceptualMatchResult()
        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, first, None)
        assert repo.saved == [(1, 2)]
        assert json.loads(repo.verdicts[(1, 2)]["evidence"]) == {"kind": "lead"}
        assert (first.offset_probes, first.offset_probes_cached) == (1, 0)
        assert first.offset_probes_candidates == 1

        second = PerceptualMatchResult()
        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, second, None)
        assert second.offset_probes == 0
        assert (second.offset_probes_cached, second.offset_probes_candidates) == (1, 1)

    def test_a_decode_failure_is_not_remembered_as_no_shift(self) -> None:
        """A probe that could not judge the pair must stay in the queue, or
        the failure would hide a real copy for as long as its bytes last."""
        repo = _VerdictRepo()
        service = _service(repo)
        rows = self._rows((1, 3021.0), (2, 3034.0))
        service._probe_timeline_offset_with_verdict = lambda *a, **k: (None, False, None)  # type: ignore[method-assign]

        result = PerceptualMatchResult()
        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert repo.saved == []
        assert result.offset_probes == 1

    def test_a_verdict_for_replaced_bytes_is_ignored(self) -> None:
        repo = _VerdictRepo()
        service = _service(repo)
        rows = self._rows((1, 3021.0), (2, 3034.0))
        repo.verdicts[(1, 2)] = {
            "size_a": rows[1]["size"],
            "mtime_a": rows[1]["mtime_ns"],
            "size_b": rows[2]["size"],
            "mtime_b": rows[2]["mtime_ns"] + 1,
        }
        service._probe_timeline_offset_with_verdict = lambda *a, **k: (None, True, {"kind": "lead"})  # type: ignore[method-assign]

        result = PerceptualMatchResult()
        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert result.offset_probes == 1


class TestStoredVerdictEvidence:
    """A verdict keeps the checkpoint numbers, not just the conclusion.

    The probe is the scan's most expensive phase; changing strictness used to
    retire every verdict and decode the same queue again. The stored
    measurements let the current bars be re-applied without decoding either
    file, and a looser bar can even surface a stored miss as a hit.
    """

    def _rows(self) -> dict[int, dict[str, Any]]:
        return {
            fid: {
                "id": fid,
                "original_path": f"e:/vids/{fid}.mp4",
                "normalized_path_key": f"e:/vids/{fid}.mp4",
                "size": 1000 + fid,
                "mtime_ns": 5 + fid,
                "_duration": duration,
            }
            for fid, duration in ((1, 3021.0), (2, 3034.0))
        }

    def _store(
        self,
        repo: _VerdictRepo,
        service: PerceptualMatchService,
        rows: dict[int, dict[str, Any]],
        *,
        correlations: tuple[float, ...] = (0.95,) * 6,
        distances: tuple[float, ...] = (0.13,) * 6,
        divergences: tuple[float, ...] = (0.0,) * 6,
    ) -> None:
        repo.verdicts[(1, 2)] = {
            "size_a": rows[1]["size"],
            "mtime_a": rows[1]["mtime_ns"],
            "size_b": rows[2]["size"],
            "mtime_b": rows[2]["mtime_ns"],
            "algorithm_version": service._offset_verdict_version,
            "evidence": json.dumps(
                {
                    "kind": "checkpoints",
                    "offset_seconds": 20.0,
                    "lead_matches": 160,
                    "correlations": correlations,
                    "distances": distances,
                    "divergences": divergences,
                }
            ),
        }

    def _forbid_probing(self, service: PerceptualMatchService) -> None:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the pair should have been judged from stored evidence")

        service._probe_timeline_offset_with_verdict = fail  # type: ignore[method-assign]

    def test_a_strictness_change_rejudges_without_decoding(self) -> None:
        repo = _VerdictRepo()
        service = _service(repo, frame_match_max_distance=0.12)
        rows = self._rows()
        self._store(repo, service, rows, distances=(0.13,) * 6)
        self._forbid_probing(service)

        result = PerceptualMatchResult()
        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert result.offset_probes == 0
        assert result.offset_probes_cached == 1
        assert service._offset_hits == []

    def test_a_looser_bar_reports_a_stored_miss_as_a_hit(self) -> None:
        repo = _VerdictRepo()
        service = _service(repo, frame_match_max_distance=0.12)
        rows = self._rows()
        # Six checkpoints at 0.115: below this scan's 0.12 bar, above the
        # 0.11 the pair was originally judged with
        self._store(repo, service, rows, distances=(0.115,) * 6)
        self._forbid_probing(service)

        result = PerceptualMatchResult()
        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert result.offset_probes == 0
        assert result.offset_probe_hits == 1
        assert len(service._offset_hits) == 1
        hit = service._offset_hits[0][2]
        assert hit.verified == 6
        assert hit.checked == 6
        assert hit.offset_seconds == pytest.approx(20.0)

    def test_a_row_without_measurements_is_probed_once_then_remembered(self) -> None:
        repo = _VerdictRepo()
        service = _service(repo)
        rows = self._rows()
        repo.verdicts[(1, 2)] = {
            "size_a": rows[1]["size"],
            "mtime_a": rows[1]["mtime_ns"],
            "size_b": rows[2]["size"],
            "mtime_b": rows[2]["mtime_ns"],
            "algorithm_version": service._offset_verdict_version,
            "evidence": None,
        }
        probed: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            probed.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert probed == [(1, 2)]
        assert result.offset_probes == 1

    def test_a_legacy_verdict_judged_at_looser_bars_is_reused(self) -> None:
        repo = _VerdictRepo()
        service = _service(repo, frame_match_max_distance=0.12)
        rows = self._rows()
        repo.verdicts[(1, 2)] = {
            "size_a": rows[1]["size"],
            "mtime_a": rows[1]["mtime_ns"],
            "size_b": rows[2]["size"],
            "mtime_b": rows[2]["mtime_ns"],
            "algorithm_version": (
                f"{pm._OFFSET_VERDICT_VERSION}-d0.13"
                f"-g{service.config.vl_local_divergence_max:g}"
                f"-b{service.config.vl_local_divergence_distance:g}"
            ),
            "evidence": None,
        }
        self._forbid_probing(service)

        result = PerceptualMatchResult()
        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert result.offset_probes == 0
        assert result.offset_probes_cached == 1

    def test_a_legacy_verdict_judged_at_stricter_bars_is_reprobed(self) -> None:
        repo = _VerdictRepo()
        service = _service(repo, frame_match_max_distance=0.12)
        rows = self._rows()
        repo.verdicts[(1, 2)] = {
            "size_a": rows[1]["size"],
            "mtime_a": rows[1]["mtime_ns"],
            "size_b": rows[2]["size"],
            "mtime_b": rows[2]["mtime_ns"],
            "algorithm_version": (
                f"{pm._OFFSET_VERDICT_VERSION}-d0.11"
                f"-g{service.config.vl_local_divergence_max:g}"
                f"-b{service.config.vl_local_divergence_distance:g}"
            ),
            "evidence": None,
        }
        probed: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            probed.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert probed == [(1, 2)]

    def test_the_per_block_cutoff_still_retires_the_cache(self) -> None:
        repo = _VerdictRepo()
        service = _service(repo, frame_match_max_distance=0.12)
        rows = self._rows()
        repo.verdicts[(1, 2)] = {
            "size_a": rows[1]["size"],
            "mtime_a": rows[1]["mtime_ns"],
            "size_b": rows[2]["size"],
            "mtime_b": rows[2]["mtime_ns"],
            "algorithm_version": (
                f"{pm._OFFSET_VERDICT_VERSION}-d0.13"
                f"-g{service.config.vl_local_divergence_max:g}-b0.10"
            ),
            "evidence": None,
        }
        probed: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            probed.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes({(1, 2): 2}, rows, [], {}, result, None)

        assert probed == [(1, 2)]


class TestKnownShiftsAreRechecked:
    """A pair a scan confirmed as shifted has to be re-tested by the next one.

    The Result list is built from one scan's comparisons, so a hit that is not
    re-confirmed this scan leaves the list although both files are still there.
    On the reference library exactly that happened: 10 pairs dropped out of
    scan 174 that scan 170 had confirmed, every one of them with probe
    evidence, half with both files present.
    """

    def _rows(self, *specs: tuple[int, float]) -> dict[int, dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        for fid, duration in specs:
            rows[fid] = {
                "id": fid,
                "original_path": f"e:/vids/{fid}.mp4",
                "size": 1000 + fid,
                "mtime_ns": 5_000 + fid,
                "_duration": duration,
            }
        return rows

    def test_a_known_shift_is_probed_with_no_candidates_and_no_budget(self) -> None:
        repo = _VerdictRepo()
        repo.hit_pairs = [(1, 2)]
        service = _service(repo)
        rows = self._rows((1, 3021.0), (2, 3034.0))
        probed: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            probed.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes({}, rows, [], {}, result, None)

        assert probed == [(1, 2)]
        assert result.offset_probes == 1
        assert result.offset_probes_reconfirmed == 1

    def test_a_known_shift_goes_first_and_is_not_probed_twice(self) -> None:
        repo = _VerdictRepo()
        repo.hit_pairs = [(1, 3)]
        service = _service(repo)
        rows = self._rows((1, 3021.0), (2, 3022.0), (3, 3034.0))
        probed: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            probed.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        # the same pair is proposed by the head-window tier as well
        service._collect_offset_probes({(1, 2): 50, (1, 3): 90}, rows, [], {}, result, None)

        assert probed == [(1, 3), (1, 2)]
        assert result.offset_probes_reconfirmed == 1

    def test_a_known_shift_already_compared_is_left_to_the_comparison(self) -> None:
        repo = _VerdictRepo()
        repo.hit_pairs = [(1, 2)]
        service = _service(repo)
        rows = self._rows((1, 3021.0), (2, 3034.0))
        probed: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            probed.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes({}, rows, [(rows[1], rows[2], False)], {}, result, None)

        assert probed == []
        assert result.offset_probes_reconfirmed == 0

    def test_a_known_shift_judged_no_shift_later_is_dropped(self) -> None:
        repo = _VerdictRepo()
        repo.hit_pairs = [(1, 2)]
        rows = self._rows((1, 3021.0), (2, 3034.0))
        service = _service(repo)
        repo.verdicts[(1, 2)] = {
            "size_a": rows[1]["size"],
            "mtime_a": rows[1]["mtime_ns"],
            "size_b": rows[2]["size"],
            "mtime_b": rows[2]["mtime_ns"],
            "algorithm_version": service._offset_verdict_version,
            "evidence": json.dumps({"kind": "lead"}),
        }
        probed: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            probed.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes({}, rows, [], {}, result, None)

        assert probed == []
        assert result.offset_probes_candidates == 0

    def test_a_known_shift_whose_file_is_gone_is_not_probed(self) -> None:
        repo = _VerdictRepo()
        repo.hit_pairs = [(1, 2)]
        service = _service(repo)
        rows = self._rows((1, 3021.0))  # file 2 no longer eligible
        probed: list[tuple[int, int]] = []

        def fake_probe(
            file_long: dict[str, Any], file_short: dict[str, Any]
        ) -> tuple[None, bool, dict[str, Any] | None]:
            probed.append(tuple(sorted((file_long["id"], file_short["id"]))))
            return None, True, {"kind": "lead"}

        service._probe_timeline_offset_with_verdict = fake_probe  # type: ignore[method-assign]
        result = PerceptualMatchResult()

        service._collect_offset_probes({}, rows, [], {}, result, None)

        assert probed == []
        assert result.offset_probes_reconfirmed == 0


class _StubRepo:
    """Captures comparison rows instead of writing them."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def create_pair_comparison(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


class TestRecordOffsetHit:
    def test_every_checkpoint_confirmed_is_very_likely(self) -> None:
        repo = _StubRepo()
        service = PerceptualMatchService(repo, MatchingConfig())  # type: ignore[arg-type]
        long_row, short_row = _files()
        probe = OffsetProbeResult(
            offset_seconds=20.0,
            lead_matches=160,
            verified=6,
            checked=6,
            correlations=(0.97, 0.98, 0.96, 0.99, 0.95, 0.97),
            distances=(0.03, 0.02, 0.04, 0.03, 0.02, 0.03),
            divergences=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        )
        result = PerceptualMatchResult()

        service._record_offset_hit(5, short_row, long_row, probe, result)

        row = repo.rows[0]
        assert row["classification"] == CLASS_VERY_LIKELY
        assert row["file_a_id"] == short_row["id"]
        assert row["file_b_id"] == long_row["id"]
        assert row["match_fraction"] == pytest.approx(1.0)
        assert row["frames_compared"] == 6
        assert row["mean_frame_distance"] == pytest.approx(0.0283, abs=0.001)
        evidence = json.loads(row["evidence"])
        assert evidence["mode"] == "offset-probe"
        assert evidence["offset_seconds"] == pytest.approx(20.0)
        assert evidence["aligned"] is False
        assert evidence["lead_matches"] == 160
        assert evidence["distances"] == [0.03, 0.02, 0.04, 0.03, 0.02, 0.03]
        assert evidence["divergences"] == [0.0] * 6
        assert evidence["mean_divergence"] == 0.0
        assert result.very_likely == 1
        assert result.comparisons == 1

    def test_one_divergent_checkpoint_leaves_it_for_review(self) -> None:
        repo = _StubRepo()
        service = PerceptualMatchService(repo, MatchingConfig())  # type: ignore[arg-type]
        long_row, short_row = _files()
        probe = OffsetProbeResult(
            offset_seconds=20.0,
            lead_matches=160,
            verified=5,
            checked=6,
            correlations=(0.97, 0.12, 0.96, 0.99, 0.95, 0.97),
            distances=(0.03, 0.31, 0.03, 0.02, 0.03, 0.02),
        )
        result = PerceptualMatchResult()

        service._record_offset_hit(5, short_row, long_row, probe, result)

        assert repo.rows[0]["classification"] == CLASS_POSSIBLE
        assert result.possible == 1

    def test_every_checkpoint_at_the_pixel_bar_but_not_below_is_possible(self) -> None:
        # All six confirm inside the distance bar, yet the mean sits above
        # the very-likely bar: a heavily re-encoded copy, shown for review
        repo = _StubRepo()
        service = PerceptualMatchService(repo, MatchingConfig())  # type: ignore[arg-type]
        long_row, short_row = _files()
        probe = OffsetProbeResult(
            offset_seconds=20.0,
            lead_matches=160,
            verified=6,
            checked=6,
            correlations=(0.95,) * 6,
            distances=(0.12,) * 6,
        )
        result = PerceptualMatchResult()

        service._record_offset_hit(5, short_row, long_row, probe, result)

        assert repo.rows[0]["classification"] == CLASS_POSSIBLE
        assert result.possible == 1


class _ScanRepo:
    """Just enough repository for a scan that finds no candidates."""

    def __init__(self, files: list[dict[str, Any]]) -> None:
        self.files = files
        self.rows: list[dict[str, Any]] = []
        self.commits = 0

    def delete_pair_comparisons(self, scan_id: int, classification: str) -> None:
        return None

    def get_dismissed_pairs(self) -> set[tuple[str, str]]:
        return set()

    def get_pair_comparisons(self, scan_id: int) -> list[dict[str, Any]]:
        return []

    def get_scanned_root_ids(self, scan_id: int) -> set[int]:
        return {1}

    def get_present_files(self) -> list[dict[str, Any]]:
        return self.files

    def get_probe_metadata(self, file_id: int) -> dict[str, Any]:
        return {
            "status": "success",
            "has_video": 1,
            "container_duration": 3000.0,
            "audio_stream_count": 1,
        }

    def get_head_bag(self, file_id: int) -> dict[str, Any] | None:
        return None

    def upsert_head_bag(
        self,
        file_id: int,
        algorithm_version: str,
        window_seconds: float,
        hashes: bytes,
        file_size: int,
        mtime_ns: int,
    ) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def create_pair_comparison(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


class TestProbeHitsReachTheDatabase:
    def test_a_scan_with_no_candidates_still_records_a_confirmed_shift(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pair that started all this produced neither candidate nor row."""

        files = [
            {
                "id": fid,
                "original_path": f"e:/vids/{fid}.mp4",
                "normalized_path_key": f"e:/vids/{fid}.mp4",
                "is_symlink": 0,
                "root_id": 1,
            }
            for fid in (1, 2)
        ]
        repo = _ScanRepo(files)
        service = PerceptualMatchService(repo, MatchingConfig())  # type: ignore[arg-type]
        monkeypatch.setattr(service, "_ensure_signatures", lambda *a, **k: {})
        monkeypatch.setattr(service, "_ensure_head_bags", lambda *a, **k: {})
        monkeypatch.setattr(service, "_ensure_audio", lambda *a, **k: {})

        def no_candidates(*args: Any, **kwargs: Any) -> list[Any]:
            long_row = {**files[0], "_duration": 3041.0}
            short_row = {**files[1], "_duration": 3021.0}
            service._offset_hits.append(
                (
                    short_row,
                    long_row,
                    OffsetProbeResult(20.0, 160, 6, 6, (0.97,) * 6, (0.03,) * 6),
                )
            )
            return []

        monkeypatch.setattr(service, "_generate_candidate_pairs", no_candidates)

        result = service.find_perceptual_duplicates(1)

        assert len(repo.rows) == 1
        assert repo.rows[0]["classification"] == CLASS_VERY_LIKELY
        assert result.very_likely == 1
        assert repo.commits == 1


class TestCheckpointBars:
    """Correlation reads layout, distance reads the picture, divergence reads detail.

    The first pair that started this: two unrelated 22-minute videos, five of
    six checkpoints correlated above 0.60 (0.583-0.763), reported as POSSIBLE,
    83% match. Their distances at those same checkpoints were 0.116-0.174,
    while a genuine 4-second-shifted copy measured 0.026-0.044 at 0.978-0.998.

    The second pair: two completely different videos whose close-up framing and
    lighting happen to agree. Five of six checkpoints sat just under the 0.141
    distance bar (0.089-0.141), so distance alone still reported *83% match*;
    6-50% of their 4x4 blocks diverged, against 0% at every checkpoint of a
    genuine copy.
    """

    def test_a_genuine_checkpoint_passes_all_three_bars(self) -> None:
        assert pm._checkpoint_matches(0.99, 0.03, 0.0, MatchingConfig())

    def test_a_completely_different_pair_fails_on_divergence(self) -> None:
        config = MatchingConfig(frame_match_max_distance=0.141)
        correlations = (0.667, 0.780, 0.766, 0.777, 0.808, 0.779)
        distances = (0.1413, 0.1085, 0.1152, 0.1012, 0.0892, 0.1356)
        divergences = (0.500, 0.062, 0.250, 0.125, 0.062, 0.312)

        def count(use_divergence: bool) -> int:
            return sum(
                1
                for correlation, distance, divergence in zip(
                    correlations, distances, divergences, strict=True
                )
                if pm._checkpoint_matches(
                    correlation, distance, divergence if use_divergence else 0.0, config
                )
            )

        # Distance alone confirmed 5 of 6 and reported the pair; the detail
        # gate leaves 3, below the four a verdict needs
        assert count(use_divergence=False) == 5
        assert count(use_divergence=True) == 3

    def test_a_low_correlation_fails_even_with_identical_pixels(self) -> None:
        assert not pm._checkpoint_matches(0.55, 0.01, 0.0, MatchingConfig())

    def test_a_large_distance_fails_even_at_high_correlation(self) -> None:
        assert not pm._checkpoint_matches(0.99, 0.20, 0.0, MatchingConfig())

    def test_a_diverging_picture_fails_even_at_a_low_distance(self) -> None:
        # Layout and average brightness agree; whole patches of detail do not
        assert not pm._checkpoint_matches(0.99, 0.02, 0.5, MatchingConfig())

    def test_an_unjudged_divergence_is_not_a_penalty(self) -> None:
        # Probes built before the divergence field existed keep their verdict
        probe = OffsetProbeResult(20.0, 160, 6, 6, (0.97,) * 6, (0.03,) * 6)
        assert probe.mean_divergence == 0.0


class TestDownscale:
    def test_a_constant_frame_stays_constant(self) -> None:
        small = pm._downscale_gray(bytes([100]) * FRAME_BYTES, SIZE)
        assert small == bytes([100]) * (16 * 16)

    def test_blocks_are_averaged(self) -> None:
        frame = bytearray(FRAME_BYTES)
        frame[0], frame[1], frame[SIZE], frame[SIZE + 1] = 10, 20, 30, 40
        small = pm._downscale_gray(bytes(frame), SIZE)
        assert small[0] == 25
        assert small[1] == 0

    def test_a_frame_of_the_wrong_size_is_refused(self) -> None:
        assert pm._downscale_gray(b"", SIZE) == b""
        assert pm._downscale_gray(bytes(10), SIZE) == b""

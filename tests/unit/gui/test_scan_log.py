"""The Scan log must read like a scan, not like a file dump.

Regression: every progress event appended one raw line — one per file, one
per comparison, ~20,000 rows on a 6,000-file library — all in the same dim
colour. The 5,000-block cap then dropped the *oldest* rows, so the phase
headers and milestones at the top of a long scan were gone long before the
user scrolled to them, and nothing could be filtered, searched, copied or
saved.
"""

import os
import re
import time
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.core.models import ProgressEvent  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _config(tmp_path):
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    return replace(
        cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports"))
    )


@pytest.fixture()
def window(tmp_path, qapp):
    created = []

    def _make():
        repo = DatabaseRepository(tmp_path / "v.db")
        win = gui_app.MainWindow(_config(tmp_path), repo, tmp_path / "config.toml")
        win.reload_data()
        created.append((win, repo))
        return win

    yield _make

    for win, repo in created:
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
        repo.close()


def _records(win) -> list[str]:
    return [e.text for e in win._scan_log_records]


def _shown(win) -> list[str]:
    return [e.text for e in win._log_rendered]


def _event(phase, done, total=0, path=None):
    return ProgressEvent(
        phase=phase,
        completed_count=done,
        total_count=total or None,
        current_path=path,
        message=f"{phase} {done}/{total}" if total else "",
    )


def _age_phase(win, seconds: float) -> None:
    """Move the running phase's clocks back: `seconds` of work happened.

    The rate is the average since the phase opened, so a test that wants a
    rate either waits seconds or says the phase is older than it is.
    """
    state = win._phase_log
    state["started"] -= seconds
    state["last_progress_at"] -= seconds


class TestRowFormat:
    def test_rows_carry_a_millisecond_stamp(self, window):
        win = window()
        win._log("hello")
        assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}", win._log_rendered[-1].stamp)

    def test_duration_reads_like_a_duration(self):
        assert gui_app._duration(0.84) == "840 ms"
        assert gui_app._duration(26.94) == "26.9 s"
        assert gui_app._duration(192) == "3 m 12 s"


class TestFilters:
    def test_file_rows_are_shown_by_default_and_can_be_hidden(self, window):
        win = window()
        win._on_scan_progress(_event("probing", 1, 10, "P:/v/a.mp4"))
        assert any("a.mp4" in t for t in _records(win))
        assert any("a.mp4" in t for t in _shown(win))
        # The rows of the scan that already ran must come back, not just
        # the ones of the next scan
        win._log_files_box.setChecked(False)
        assert not any("a.mp4" in t for t in _shown(win))

    def test_errors_only_hides_the_running_commentary(self, window):
        win = window()
        win._log("Reading details  3 of 10 files, 40/s")
        win._log("  Signature failed for P:/v/a.mp4", level="error")
        win._log_errors_box.setChecked(True)
        assert _shown(win) == ["  Signature failed for P:/v/a.mp4"]
        assert "Errors only" in win._log_footer.text()

    def test_filter_matches_row_text(self, window):
        win = window()
        win._log("Reading video frames  P:/v/a.mp4")
        win._log("Finding exact copies  P:/v/b.mp4")
        win._log_filter.setText("b.mp4")
        assert _shown(win) == ["Finding exact copies  P:/v/b.mp4"]

    def test_filter_box_ends_after_its_placeholder(self, window):
        win = window()
        # The placeholder is the legend for what the box matches, and the
        # box is no wider than that legend: the empty track beyond it used
        # to push Copy away from the filter.
        placeholder = win._log_filter.placeholderText()
        width = win._log_filter.fontMetrics().horizontalAdvance(placeholder)
        assert win._log_filter.minimumWidth() == win._log_filter.maximumWidth()
        assert width < win._log_filter.maximumWidth() <= width + 60

    def test_the_toolbar_names_errors_and_the_copy_action(self, window):
        win = window()
        assert win._log_errors_box.text() == "Errors only"
        assert "Copy log" in [b.text() for b in win.findChildren(gui_app.QPushButton)]

    def test_footer_says_how_much_is_hidden(self, window):
        win = window()
        win._log_files_box.setChecked(False)
        win._log("only row")
        assert "1 of 1 lines shown" in win._log_footer.text()
        win._on_scan_progress(_event("probing", 1, 2, "P:/v/a.mp4"))
        assert "File rows hidden" in win._log_footer.text()
        assert "of 3 lines shown" in win._log_footer.text()


class TestPhaseNames:
    """The log speaks about videos, not about the pipeline that reads them."""

    PHASES = (
        "discovery",
        "probing",
        "hashing",
        "signing",
        "blocking",
        "verifying",
        "offset",
        "fingerprinting",
        "audio",
        "comparing",
    )

    def test_every_phase_the_scanner_emits_has_a_label(self):
        assert set(gui_app._PHASE_LABELS) == set(self.PHASES)

    def test_no_pipeline_vocabulary_survives(self):
        jargon = ("probe", "hash", "sign", "fingerprint", "block", "verif", "phase")
        for label in gui_app._PHASE_LABELS.values():
            assert not [word for word in jargon if word in label.lower()], label

    def test_banners_use_the_label(self, window):
        win = window()
        for phase in ("discovery", "probing"):
            win._on_scan_progress(_event(phase, 1, 2, "P:/v/a.mp4"))
        # Sentence case: the heading names the phase, the rail supplies the
        # branch, and no second ALL-CAPS vocabulary is introduced.
        headings = [e.text for e in win._scan_log_records if e.level == "heading"]
        assert headings == ["Finding videos", "Reading details"]

    def test_unknown_phase_keeps_a_tidied_name(self):
        assert gui_app._phase_label("new_stage") == "New stage"


class TestScanShape:
    def test_files_log_one_header_and_one_done_row(self, window):
        win = window()
        # Phases only: the per-file rows are on by default now, so this test
        # has to ask for the view it is about
        win._log_files_box.setChecked(False)
        for i in range(1, 51):
            win._on_scan_progress(_event("probing", i, 50, f"P:/v/{i}.mp4"))
        assert _shown(win) == ["Reading details"]
        win._on_scan_progress(_event("hashing", 0, 0))
        done_row = next(t for t in _records(win) if t.startswith("Reading details") and " in " in t)
        # The count and the column are what the row is for; the duration is
        # whatever the machine took (a busy CI box can make 50 events take
        # 16 ms, which is not the thing under test)
        assert re.fullmatch(
            r"Reading details {7}50 files in (?:\d+ ms|\d+\.\d s|\d+ m \d+ s)", done_row
        ), done_row

    def test_a_running_phase_puts_nothing_in_the_log(self, window):
        # The log is a record, not a counter: a row every five seconds that
        # repeated the progress bar's own count tripled its length to say
        # what the bar already said
        win = window()
        for i in range(1, 4):
            win._on_scan_progress(_event("comparing", i, 20, f"P:/v/{i}.mp4"))
        _age_phase(win, 30.0)
        win._on_scan_progress(_event("comparing", 5, 20, "P:/v/5.mp4"))
        assert not any(" of 20 pairs" in t for t in _records(win))
        # ... while the bar keeps the live reading
        assert win.progress_bar.format().startswith("Comparing videos   5/20")

    def test_phase_messages_survive_for_root_scanning(self, window):
        win = window()
        win._on_scan_progress(
            ProgressEvent(phase="discovery", completed_count=0, message="Scanning root: P:/Videos")
        )
        assert any("Scanning root: P:/Videos" in t for t in _shown(win))

    def test_a_second_scan_starts_a_clean_log(self, window):
        """A rescan opens on its own rows — the previous run is in its report."""
        win = window()
        win._begin_scan_log()
        win._log("first scan")
        win._begin_scan_log()
        win._log("second scan")
        texts = _records(win)
        assert "first scan" not in texts
        assert "second scan" in texts
        assert sum(1 for t in texts if t == "Scan started") == 1
        # The widget, not just the records behind it
        assert not any("first scan" in t for t in _shown(win))


class TestSummary:
    def _result(self, errors=()):
        return {
            "status": "completed",
            "total_files": 6449,
            "present_files": 6449,
            "missing_files": 0,
            "error_files": len(errors),
            "cache_hits": 6449,
            "files_hashed": 0,
            "hash_cache_hits": 6449,
            "exact_pairs": 0,
            "files_signed": 0,
            "signature_cache_hits": 6444,
            "files_fingerprinted": 0,
            "fingerprint_cache_hits": 1420,
            "candidate_pairs": 6563,
            "comparisons": 6563,
            "very_likely": 7,
            "possible": 480,
            "rejected": 6075,
            "errors": list(errors),
        }

    @staticmethod
    def _pair(classification="POSSIBLE", a=1, b=2):
        return {
            "classification": classification,
            "file_a_id": a,
            "file_b_id": b,
            "match_fraction": 0.9,
            "mean_frame_distance": 0.02,
        }

    def _window(self, window, pairs=(), decisions=None):
        win = window()
        win.all_pairs = list(pairs)
        win.decisions = dict(decisions or {})
        return win

    def test_leads_with_what_happened_and_how_long_it_took(self, window):
        win = self._window(window)
        win._scan_started = time.monotonic() - 65
        win._log_scan_summary(self._result())
        assert _records(win)[0] == "Scan finished in 1 m 5 s"

    def test_a_stopped_scan_says_stopped(self, window):
        win = self._window(window)
        result = self._result()
        result["status"] = "interrupted"
        win._log_scan_summary(result)
        assert _records(win)[0].startswith("Scan stopped")

    def test_counts_only_the_pairs_left_to_review(self, window):
        # The reviewer already decided one of the three
        pairs = [
            self._pair("VERY_LIKELY", 1, 2),
            self._pair("POSSIBLE", 3, 4),
            self._pair("POSSIBLE", 5, 6),
        ]
        win = self._window(window, pairs, decisions={5: "keep"})
        win._log_scan_summary(self._result())
        assert "2 pairs left to review: 1 very likely duplicate and 1 possible duplicate" in (
            _records(win)
        )

    def test_counts_every_pair_the_scan_compared(self, window):
        # The length slider gates the next scan; it does not hide pairs the
        # last scan already compared, so a short pair still counts here.
        pairs = [self._pair("POSSIBLE", 1, 2), self._pair("POSSIBLE", 3, 4)]
        win = self._window(window, pairs)
        win.probes = {
            1: {"container_duration": 100.0},
            2: {"container_duration": 100.0},
            3: {"container_duration": 100.0},
            4: {"container_duration": 20.0},
        }
        win._log_scan_summary(self._result())
        line = next(t for t in _records(win) if "left to review" in t)
        assert line == "2 possible duplicates left to review"

    def test_one_class_is_not_counted_twice(self, window):
        # "23 pairs left to review: 23 possible duplicates" said it twice
        pairs = [self._pair("POSSIBLE", 2 * i + 1, 2 * i + 2) for i in range(23)]
        win = self._window(window, pairs)
        win._log_scan_summary(self._result())
        line = next(t for t in _records(win) if "left to review" in t)
        assert line == "23 possible duplicates left to review"

    def test_mixed_classes_keep_their_breakdown(self, window):
        pairs = [
            self._pair("VERY_LIKELY", 1, 2),
            self._pair("VERY_LIKELY", 3, 4),
            self._pair("POSSIBLE", 5, 6),
        ]
        win = self._window(window, pairs)
        win._log_scan_summary(self._result())
        line = next(t for t in _records(win) if "left to review" in t)
        assert line == "3 pairs left to review: 2 very likely duplicates and 1 possible duplicate"

    def test_a_short_pair_is_still_left_to_review(self, window):
        pairs = [self._pair("POSSIBLE", 1, 2)]
        win = self._window(window, pairs)
        win.probes = {
            1: {"container_duration": 100.0},
            2: {"container_duration": 10.0},
        }
        win._log_scan_summary(self._result())
        texts = _records(win)
        assert "1 possible duplicate left to review" in texts
        assert not any(t.startswith("No pairs to review") for t in texts)
        assert "Every pair has a decision" not in texts

    def test_a_fully_reviewed_scan_says_so(self, window):
        pairs = [self._pair("POSSIBLE", 1, 2), self._pair("POSSIBLE", 3, 4)]
        win = self._window(window, pairs, decisions={1: "keep", 3: "remove"})
        win._log_scan_summary(self._result())
        assert "Every pair has a decision" in _records(win)

    def test_checked_videos_do_not_mention_a_missing_count_of_zero(self, window):
        win = self._window(window)
        win._log_files_box.setChecked(False)
        win._log_scan_summary(self._result())
        assert "6,449 videos checked" in _records(win)
        # A zero is the normal case, so it is not worth a clause (the count
        # itself still sits in the details block)
        assert not any("missing" in t for t in _shown(win))

    def test_missing_files_say_they_are_not_on_disk(self, window):
        win = self._window(window)
        result = self._result()
        result["missing_files"] = 3
        win._log_scan_summary(result)
        assert "6,449 videos checked, 3 not found on disk" in _records(win)

    def test_nothing_found_reads_plainly(self, window):
        win = self._window(window)
        result = self._result()
        result.update(very_likely=0, possible=0, rejected=0, comparisons=0)
        win._log_scan_summary(result)
        texts = _records(win)
        assert "No duplicates found" in texts
        assert "No errors" in texts

    def test_everything_dismissed_is_not_reported_as_nothing_found(self, window):
        win = self._window(window)  # all_pairs empty, matcher found some
        win._log_scan_summary(self._result())
        assert "No pairs left to review" in _records(win)

    def test_no_rejected_pairs_line(self, window):
        win = self._window(window, [self._pair("POSSIBLE", 1, 2)])
        win._log_scan_summary(self._result())
        assert not any("turned out not to match" in t for t in _records(win))

    def test_errors_are_counted_without_pointing_at_the_rows_below(self, window):
        win = self._window(window)
        errors = [f"Signature failed for P:/v/{i}.mp4" for i in range(3)]
        win._log_scan_summary(self._result(errors))
        assert "3 errors" in _records(win)
        assert not any("listed in red" in t for t in _records(win))

    def test_counters_live_in_a_details_block_behind_the_files_box(self, window):
        win = self._window(window, [self._pair("POSSIBLE", 1, 2)], decisions={1: "keep"})
        win._log_files_box.setChecked(False)
        win._log_scan_summary(self._result())
        # Jargon does not belong in the phase view — a user reads four lines
        # and is done; support ticks Files for the rest
        assert not any("reused:" in t for t in _shown(win))
        win._log_files_box.setChecked(True)
        shown = _shown(win)
        assert any(t.startswith("  reused: 6,449 probes, ") for t in shown)
        assert any(t.startswith("  computed: 0 hashed, 0 signed, 0 fingerprinted") for t in shown)
        assert any(
            t.startswith("  matches: 1 possible duplicate (1 already decided)") for t in shown
        )
        assert any(
            t.startswith("  pairs: 6,563 candidates, 6,563 compared, 6,075 rejected") for t in shown
        )
        assert any(t.startswith("  files: 6,449 found, 6,449 present") for t in shown)

    def test_every_error_is_logged_as_an_error_row(self, window):
        win = window()
        errors = [f"Signature failed for P:/v/{i}.mp4 (0 usable frames)" for i in range(60)]
        win._log_scan_summary(self._result(errors))
        logged = [e for e in win._scan_log_records if e.level == "error"]
        assert len(logged) == 60
        # Nothing is summarised away any more — the old cut-off hid the tail
        assert not any("more" in e.text for e in logged)

    def test_error_row_points_at_the_file_it_names(self, window, tmp_path):
        win = window()
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x")
        win._log_scan_summary(self._result([f"Signature failed for {video} (0/12 usable frames)"]))
        assert [e.path for e in win._scan_log_records if e.level == "error"] == [str(video)]

    def test_every_error_follows_its_count(self, window):
        # With the Files box on, five detail rows sat between "6 errors" and
        # the errors themselves, and the count read as their heading
        win = window()
        errors = [f"Signature failed for P:/v/{i}.mp4 (0 usable frames)" for i in range(6)]
        win._log_files_box.setChecked(True)
        win._log_scan_summary(self._result(errors))
        rows = [(e.level, e.text) for e in win._scan_log_records]
        counted = next(i for i, (_lvl, t) in enumerate(rows) if t == "6 errors")
        assert [lvl for lvl, _t in rows[counted + 1 : counted + 7]] == ["error"] * 6
        # …and the diagnostics still come after them
        assert any(t.startswith("  reused:") for _lvl, t in rows[counted + 7 :])


class TestSummaryFollowsTheReview:
    """The summary counts what the Result tab will show, so it reads the
    database through reload_data() — which has to happen before it runs."""

    RESULT = {
        "status": "interrupted",  # also skips the preview phase
        "total_files": 2,
        "present_files": 2,
        "missing_files": 0,
        "error_files": 0,
        "cache_hits": 0,
        "files_hashed": 0,
        "hash_cache_hits": 0,
        "exact_pairs": 0,
        "files_signed": 0,
        "signature_cache_hits": 0,
        "files_fingerprinted": 0,
        "fingerprint_cache_hits": 0,
        "candidate_pairs": 1,
        "comparisons": 1,
        "very_likely": 1,
        "possible": 0,
        "rejected": 0,
        "errors": [],
    }

    def _one_decided_pair(self, win, tmp_path, decision):
        repo = win.repository
        scan_id = repo.create_scan("test")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        ids = []
        for i in range(2):
            video = tmp_path / f"v{i}.mp4"
            video.write_bytes(b"x")
            path = str(video)
            ids.append(
                repo.create_file(
                    root_id, path, path.lower(), video.name, ".mp4", 10 + i, 1 + i, scan_id
                )
            )
        repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
        if decision:
            repo.set_decision(scan_id, ids[0], decision)
        repo.commit()

    def test_an_untouched_pair_is_reported_as_left_to_review(self, window, tmp_path):
        win = window()
        self._one_decided_pair(win, tmp_path, decision=None)
        win._on_scan_finished(self.RESULT)
        assert "1 very likely duplicate left to review" in _records(win)

    def test_a_decided_pair_is_not_reported_as_left_to_review(self, window, tmp_path):
        win = window()
        self._one_decided_pair(win, tmp_path, decision="keep")
        win._on_scan_finished(self.RESULT)
        assert "Every pair has a decision" in _records(win)


class TestClickToReveal:
    def test_row_maps_back_to_its_record(self, window):
        win = window()
        win._log("first")
        win._log("second", path="P:/v/b.mp4")
        assert win._log_record_for_block(0).text == "first"
        assert win._log_record_for_block(1).text == "second"

    def test_only_rows_naming_a_file_are_clickable(self, window):
        win = window()
        win._log("first")
        win._log("second", path="P:/v/b.mp4")
        assert not win._log_row_has_target(0)
        assert win._log_row_has_target(1)

    def test_the_log_carries_no_tooltip_or_toolbar_hint(self, window):
        win = window()
        # A tooltip on the log is anchored to the whole viewport: it opened
        # anywhere over the log and stayed while the pointer rested there
        assert win.scan_log.toolTip() == ""
        assert not hasattr(win, "_log_hint")

    def test_copy_uses_the_rows_on_screen(self, window):
        win = window()
        win._log_files_box.setChecked(False)
        win._log("first")
        win._log("second", path="P:/v/b.mp4", level="detail")
        lines = win._log_lines(win._log_rendered).splitlines()
        assert len(lines) == 1
        assert lines[0].endswith("first")


class TestEmphasis:
    """The stage's colour lives in its pill, not in the row's text.

    The phase rows went blue-and-bold (as loud as their banner), then bold
    only, then blue only — each time pulling the eye to a duration instead
    of to the numbers the log exists for. The STAGE column's pill carries
    the colour now, and every row's text keeps the palette's own colour, so
    the eye follows the numbers rather than the paint.
    """

    def test_banners_and_the_scan_headline_are_headings(self, window):
        win = window()
        win._begin_scan_log()
        win._on_scan_progress(_event("discovery", 1, 2, "P:/v/a.mp4"))
        assert [e.text for e in win._scan_log_records if e.level == "heading"] == [
            "Scan started",
            "Finding videos",
        ]

    def test_a_phase_result_is_plain_log_text(self, window):
        win = window()
        win._on_scan_progress(_event("discovery", 1, 2, "P:/v/a.mp4"))
        win._on_scan_progress(_event("probing", 1, 2, "P:/v/a.mp4"))  # ends discovery
        result = next(e for e in win._scan_log_records if e.level == "phase")
        # The count and the column are what the row is for; the duration is
        # whatever the machine took, and a loaded box can make it 16 ms
        assert result.text.startswith("Finding videos        1 file in "), result.text
        # No colour, no weight — the palette's own text, same as any line
        assert gui_app._LOG_STYLES["phase"] == (None, None, False)
        assert gui_app._LOG_STYLES["phase"] == gui_app._LOG_STYLES["info"]

    def test_only_warnings_and_errors_colour_their_text(self):
        # The banner is bold but uncoloured: the STAGE pill beside it is
        # what carries the stage's colour, and two colours on the row that
        # says the least is one too many
        assert gui_app._LOG_STYLES["heading"] == (None, None, True)
        for level in ("heading", "phase", "info", "summary"):
            assert gui_app._LOG_STYLES[level][:2] == (None, None), level
        for level in ("phase", "info", "summary"):
            assert gui_app._LOG_STYLES[level][2] is False, level
        assert gui_app._LOG_STYLES["warn"][:2] != (None, None)
        assert gui_app._LOG_STYLES["error"][:2] != (None, None)

    def test_a_count_never_reaches_the_log_as_a_row(self, window):
        win = window()
        win._on_scan_progress(_event("probing", 1, 6449, "P:/v/a.mp4"))
        _age_phase(win, 30.0)
        win._on_scan_progress(_event("probing", 2500, 6449, "P:/v/b.mp4"))
        assert not any(" of 6,449 files" in t for t in _records(win))

    def test_every_row_starts_its_numbers_in_the_same_column(self, window):
        # A phase's result row used two spaces, so "Finding lookalikes
        # 585,728 pairs" sat four columns left of the rows above it
        win = window()
        win._on_scan_progress(_event("discovery", 1, 2, "P:/v/a.mp4"))
        win._on_scan_progress(_event("probing", 1, 6449, "P:/v/a.mp4"))  # ends discovery
        win._on_scan_progress(_event("fingerprinting", 1, 6449, "P:/v/b.mp4"))  # ends probing
        width = gui_app._LOG_PHASE_WIDTH
        finished = [t for t in _records(win) if " in " in t and t[:width].strip()]
        assert len(finished) >= 2
        for row in finished:
            assert row[width].isdigit(), row

    def test_the_end_of_a_scan_is_a_heading(self, window):
        win = window()
        win._scan_started = time.monotonic()
        win._log_scan_summary(TestSummary()._result())
        assert win._scan_log_records[0].level == "heading"


class TestRowLayout:
    def test_a_long_row_keeps_to_one_line(self, window):
        # A wrapped path pushes its own timestamp out of the column and the
        # next row reads as part of the previous one
        win = window()
        assert win.scan_log.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap

    def test_a_volume_is_written_one_way(self, window):
        win = window()
        win._on_scan_progress(_event("comparing", 1, 20, "e:/vids/a.mp4"))
        win._log("Signature failed for p:\\vids\\b.mp4: only 0/12 usable frames", level="error")
        assert _records(win)[-2:] == [
            "Comparing videos      E:/vids/a.mp4",
            "Signature failed for P:\\vids\\b.mp4: only 0/12 usable frames",
        ]

    def test_colons_that_are_not_drives_are_left_alone(self):
        # A time or a ratio must not be mistaken for a path
        assert gui_app._tidy_drive_letters("stopped at 12:30 (a:b)") == "stopped at 12:30 (a:b)"


class TestRailColumn:
    """Timestamp, a rail node/branch, then the event.

    The stage keeps its metadata for filtering, but it is no longer a pill
    column: the heading row names the phase once, and the rail's ``●``/``├``
    marks carry the timeline.
    """

    def test_the_header_names_the_two_visible_columns(self, window):
        win = window()
        header = win._log_header.text()
        assert header.startswith("TIMESTAMP")
        assert "STAGE" not in header

    def test_every_phase_row_carries_its_stage(self, window):
        win = window()
        win._on_scan_progress(_event("discovery", 1, 2, "P:/v/a.mp4"))
        win._on_scan_progress(_event("probing", 1, 2, "P:/v/a.mp4"))  # ends discovery
        stages = {e.level: e.stage for e in win._scan_log_records}
        assert stages["heading"] == "probing"
        assert stages["detail"] == "probing"
        assert stages["phase"] == "discovery"

    def test_the_rail_uses_a_node_for_headings_and_a_branch_for_details(self, window):
        win = window()
        win._on_scan_progress(_event("discovery", 1, 2, "P:/v/a.mp4"))
        heading = next(e for e in win._scan_log_records if e.level == "heading")
        detail = next(e for e in win._scan_log_records if e.level == "detail")
        assert gui_app._LOG_EVENT_MARKS["heading"] in win._log_row_html(heading)
        assert gui_app._LOG_EVENT_MARKS["detail"] in win._log_row_html(detail)
        assert gui_app._LOG_EVENT_MARKS["info"] in win._log_row_html(
            gui_app._LogEntry(stamp="00:00:00", text="summary", level="summary")
        )

    def test_a_row_with_no_stage_leaves_no_stage_text(self, window):
        win = window()
        win._log("2 pairs left to review", level="summary")
        entry = win._scan_log_records[-1]
        assert entry.stage is None
        assert "SCAN" not in win._log_row_html(entry)

    def test_the_event_does_not_repeat_the_stage(self, window):
        # "Finding videos        6,449 files in 27.5 s" is the result row;
        # the heading above already named the phase, and the rail marks it.
        win = window()
        win._on_scan_progress(_event("discovery", 1, 2, "P:/v/a.mp4"))
        win._on_scan_progress(_event("probing", 1, 2, "P:/v/a.mp4"))  # ends discovery
        result = next(e for e in win._scan_log_records if e.level == "phase")
        # The count and a duration, never the stage's own name. The duration is
        # measured, so it cannot be asserted exactly — a busy machine turns
        # "0 ms" into something else and the test would fail for no reason.
        text = gui_app._event_text(result)
        assert text.startswith("1 file in ")
        assert text.endswith(" ms")
        assert "Finding videos" not in text

    def test_a_banner_keeps_its_own_words(self, window):
        # The banner is the phase's name; stripping it would leave an empty row
        win = window()
        win._on_scan_progress(_event("discovery", 1, 2, "P:/v/a.mp4"))
        banner = next(e for e in win._scan_log_records if e.level == "heading")
        assert gui_app._event_text(banner) == "Finding videos"

    @staticmethod
    def _report(errors=()):
        return {
            "status": "completed",
            "total_files": 3,
            "missing_files": 0,
            "errors": list(errors),
        }

    def test_the_report_keeps_its_stage(self, window):
        # The report is the scan's own outcome, so its rows keep the SCAN
        # pill — without it the stage column went blank exactly where a
        # reader looks to find out what went wrong
        win = window()
        win.all_pairs = []
        win.decisions = {}
        win._log_scan_summary(self._report())
        report = [e for e in win._scan_log_records if e.level in ("summary", "detail")]
        assert report
        assert all(e.stage == "scan" for e in report), [e.text for e in report]

    def test_a_failure_row_names_the_stage_that_produced_it(self, window):
        win = window()
        win.all_pairs = []
        win.decisions = {}
        win._log_scan_summary(
            self._report(
                [
                    "Audio signature failed for P:/v/a.mp4",
                    "Missing fingerprint for pair: P:/v/a.mp4 <-> P:/v/b.mp4",
                ]
            )
        )
        stages = [e.stage for e in win._scan_log_records if e.level == "error"]
        assert stages == ["audio", "comparing"]


class TestLiveRate:
    """The rate belongs to the progress bar, and it must be a real one.

    Fingerprinting printed `2,011 of 2,013 files, 45 files/s` on a library
    where 2,004 fingerprints were already cached. 45 was those cached files
    divided by the phase's elapsed time; the stage was extracting about one
    file every six seconds. The reading moved to the bar, which updates in
    place, but it is measured the same honest way.
    """

    def test_a_cache_head_start_is_not_counted_as_speed(self, window):
        win = window()
        # The phase opens at the cache's answer, not at zero
        win._on_scan_progress(_event("fingerprinting", 2005, 2013, "P:/v/a.mp4"))
        _age_phase(win, 45.0)
        win._on_scan_progress(_event("fingerprinting", 2008, 2013, "P:/v/b.mp4"))
        text = win.progress_bar.format()
        # Three files in 45 s — not 2,008 of them. Slow enough to read as a
        # per-minute figure: 0.067 files/s is 4 files/min, which is the
        # number you would actually plan around
        assert "2,008/2,013" in text
        assert "4 files/min" in text
        assert "45" not in text

    def test_a_working_phase_reports_its_real_rate(self, window, monkeypatch):
        win = window()
        # The rate is a slope against the wall clock, so a busy machine adds
        # milliseconds the reading never meant to include: 1,000 files over
        # 5.000 s reads 200, over 5.003 s it rounds down to 199. The clock is
        # the test's, so the assertion is about the arithmetic, not the load.
        clock = [1_000.0]
        monkeypatch.setattr(gui_app.time, "monotonic", lambda: clock[0])
        win._on_scan_progress(_event("probing", 100, 6449, "P:/v/a.mp4"))
        clock[0] += 5.0
        win._on_scan_progress(_event("probing", 1100, 6449, "P:/v/b.mp4"))
        # A thousand files in the five seconds since the baseline
        assert (
            win.progress_bar.format()
            == "Reading details   1,100/6,449   200 files/s   ~27 s left   %p%"
        )

    def test_a_working_phase_says_how_much_is_left(self, window, monkeypatch):
        win = window()
        clock = [1_000.0]
        monkeypatch.setattr(gui_app.time, "monotonic", lambda: clock[0])
        win._on_scan_progress(_event("probing", 100, 6449, "P:/v/a.mp4"))
        clock[0] += 5.0
        win._on_scan_progress(_event("probing", 1100, 6449, "P:/v/b.mp4"))
        # 5,349 files still to read at 200 a second
        assert "~27 s left" in win.progress_bar.format()

    def test_a_stalled_phase_stops_promising_two_seconds(self, window, monkeypatch):
        # Events arrive when a unit finishes. The last file of a phase can be
        # a 4K decode that emits nothing for minutes, and the bar must not
        # keep the fast units' "~2 s left" frozen for as long as it runs.
        win = window()
        clock = [1_000.0]
        monkeypatch.setattr(gui_app.time, "monotonic", lambda: clock[0])
        win._on_scan_progress(_event("probing", 5, 11, "P:/v/a.mp4"))
        clock[0] += 10.0
        win._on_scan_progress(_event("probing", 10, 11, "P:/v/b.mp4"))
        # One file left at half a file a second
        assert "~2 s left" in win.progress_bar.format()
        # Thirty seconds pass with no event: the last file is still being read
        clock[0] += 30.0
        win._refresh_bar_text()
        text = win.progress_bar.format()
        assert "~2 s left" not in text
        assert "10/11" in text

    def test_a_young_phase_promises_no_time(self, window, monkeypatch):
        # One event is not a slope — the first pair of a phase can be a 40 ms
        # checksum or a nine-second decode, and a reading that swings from
        # "~2 s left" to "~4 h left" inside a second is worse than none
        win = window()
        clock = [1_000.0]
        monkeypatch.setattr(gui_app.time, "monotonic", lambda: clock[0])
        win._on_scan_progress(_event("probing", 1, 6449, "P:/v/a.mp4"))
        clock[0] += 0.5
        win._on_scan_progress(_event("probing", 80, 6449, "P:/v/b.mp4"))
        text = win.progress_bar.format()
        assert "files/s" in text
        assert "left" not in text

    def test_an_hour_long_phase_reads_as_hours(self, window, monkeypatch):
        # The shift test is the phase that spends an hour and a half on a big
        # library — the one the per-minute reading alone did not answer
        win = window()
        clock = [1_000.0]
        monkeypatch.setattr(gui_app.time, "monotonic", lambda: clock[0])
        win._on_scan_progress(_event("offset", 20, 420, "P:/v/a.mp4"))
        clock[0] += 600.0
        win._on_scan_progress(_event("offset", 40, 420, "P:/v/b.mp4"))
        # 380 pairs to go at 20 every ten minutes
        text = win.progress_bar.format()
        assert "2 pairs/min" in text
        assert "~3 h 10 min left" in text

    def test_a_long_silence_is_averaged_not_erased(self, window, monkeypatch):
        # A phase that spends five minutes on one unit and then rushes through
        # ten more must keep the slow unit in its average. The ten-second
        # window forgot it: the rush read as a five-file burst over the last
        # ten seconds and promised a finish in seconds, while the phase was
        # still minutes from done.
        win = window()
        clock = [1_000.0]
        monkeypatch.setattr(gui_app.time, "monotonic", lambda: clock[0])
        win._on_scan_progress(_event("probing", 10, 100, "P:/v/a.mp4"))
        # The timer repaints while the slow unit runs, which used to fill the
        # window with flat readings and drop the slow unit out of it
        clock[0] += 290.0
        win._refresh_bar_text()
        clock[0] += 10.0
        win._on_scan_progress(_event("probing", 60, 100, "P:/v/b.mp4"))
        # 50 files in 300 s is 10 files/min; 40 left at that pace is 4 min
        text = win.progress_bar.format()
        assert "10 files/min" in text
        assert "~4 min left" in text
        # Ten more finish in the next second, but the average moves only from
        # 50 files in 300 s to 60 files in 301 s
        clock[0] += 1.0
        win._on_scan_progress(_event("probing", 70, 100, "P:/v/c.mp4"))
        text = win.progress_bar.format()
        assert "12 files/min" in text
        assert "~3 min left" in text
        assert "~3 s left" not in text

    def test_a_slow_phase_is_not_declared_stalled_between_units(self, window, monkeypatch):
        # The shift test's units take minutes. Ten seconds without an event is
        # normal there, so the guard must wait for the phase's own gaps.
        win = window()
        clock = [1_000.0]
        monkeypatch.setattr(gui_app.time, "monotonic", lambda: clock[0])
        win._on_scan_progress(_event("offset", 20, 420, "P:/v/a.mp4"))
        clock[0] += 600.0
        win._on_scan_progress(_event("offset", 40, 420, "P:/v/b.mp4"))
        # Five quiet minutes are a normal wait for the next unit: the reading
        # stays on the bar
        clock[0] += 300.0
        win._refresh_bar_text()
        text = win.progress_bar.format()
        assert "2 pairs/min" in text
        assert "~3 h 10 min left" in text
        # Twenty-two quiet minutes are not: the guard withdraws both
        clock[0] += 1_000.0
        win._refresh_bar_text()
        text = win.progress_bar.format()
        assert "pairs/min" not in text
        assert "left" not in text

    def test_estimates_read_as_coarse_units(self):
        assert gui_app._eta_text(0.4) == "~1 s left"
        assert gui_app._eta_text(27.4) == "~27 s left"
        assert gui_app._eta_text(60.0) == "~1 min left"
        assert gui_app._eta_text(5400.0) == "~1 h 30 min left"
        # A minute that rounds up to the hour is an hour, not "~1 h 60 min"
        assert gui_app._eta_text(7199.0) == "~2 h left"
        assert gui_app._eta_text(129_600.0) == "~2 days left"

    def test_a_slow_phase_shows_no_rate_at_all(self, window):
        win = window()
        win._on_scan_progress(_event("fingerprinting", 2005, 2013, "P:/v/a.mp4"))
        _age_phase(win, 12.0)
        win._on_scan_progress(_event("fingerprinting", 2005, 2013, "P:/v/b.mp4"))
        # "0 files/min" would read as stuck; the counts still say where it is
        text = win.progress_bar.format()
        assert "files/s" not in text
        assert "files/min" not in text

    def test_a_finished_phase_shows_no_rate(self, window):
        win = window()
        win._on_scan_progress(_event("probing", 100, 100, "P:/v/a.mp4"))
        text = win.progress_bar.format()
        assert "files/s" not in text
        assert "files/min" not in text

    def test_rates_read_as_rates(self):
        # Fast phases keep per-second figures; a slow one states the minute
        assert gui_app._rate_phrase(1.0, "file") == "1 file/s"
        assert gui_app._rate_phrase(500.0, "file") == "500 files/s"
        assert gui_app._rate_phrase(38955.0, "pair") == "38,955 pairs/s"
        # The offset probe's real reading: "0.1 pairs/s" is 6 pairs a minute
        assert gui_app._rate_phrase(0.1, "pair") == "6 pairs/min"
        assert gui_app._rate_phrase(0.2, "file") == "12 files/min"
        # Singular unit when the minute carries exactly one
        assert gui_app._rate_phrase(1.0 / 60.0, "pair") == "1 pair/min"

    def test_a_rate_that_rounds_to_zero_is_left_out(self):
        # 0.0008 pairs/s is 0.048/min: "0 pairs/min" would read as stuck
        assert gui_app._rate_text(0.0008, "file") == ""
        assert gui_app._rate_text(0.001, "file") == "0.1 files/min"


class TestPairsReadInFull:
    """ "Comparing videos  A" left the obvious question open: with what?"""

    def test_a_comparison_names_both_files(self, window):
        win = window()
        win._on_scan_progress(
            ProgressEvent(
                phase="comparing",
                completed_count=1,
                total_count=10,
                current_path="E:/v/a.mp4",
                peer_path="E:/v/b.mp4",
            )
        )
        assert _records(win)[-1] == "Comparing videos      E:/v/a.mp4  vs  E:/v/b.mp4"

    def test_a_single_file_phase_still_names_one_file(self, window):
        win = window()
        win._on_scan_progress(_event("fingerprinting", 1, 10, "E:/v/a.mp4"))
        assert _records(win)[-1].endswith("  E:/v/a.mp4")

    def test_the_first_file_is_the_click_target(self, window):
        win = window()
        win._on_scan_progress(
            ProgressEvent(
                phase="comparing",
                completed_count=1,
                total_count=10,
                current_path="E:/v/a.mp4",
                peer_path="E:/v/b.mp4",
            )
        )
        assert [e.path for e in win._scan_log_records if e.level == "detail"] == ["E:/v/a.mp4"]


class TestOffsetProbeIsVisible:
    """The matcher phase whose cost has to be visible in the summary.

    The timeline-offset probe decodes 180s from *both* files of every pair it
    examines — by far the most expensive test in a scan — so the summary names
    its candidate count even though it appears in no other number. Pairs an
    earlier scan already judged are named apart from the ones examined now,
    because "40 of 400" alone cannot tell progress from a repeat.
    """

    @staticmethod
    def _summary(probes=9, hits=2, cached=0, candidates=None):
        if candidates is None:
            candidates = probes + cached
        return {
            "status": "completed",
            "total_files": 0,
            "missing_files": 0,
            "error_files": 0,
            "errors": [],
            "offset_probes": probes,
            "offset_probe_hits": hits,
            "offset_probes_candidates": candidates,
            "offset_probes_cached": cached,
        }

    @staticmethod
    def _window(window):
        win = window()
        win.all_pairs = []
        win.decisions = {}
        return win

    def test_a_scan_that_probed_says_how_many(self, window):
        win = self._window(window)
        win._log_scan_summary(self._summary())
        assert any(
            t.startswith("  probe: 9 of 9 candidate pairs tested, 2 found a shift")
            for t in _records(win)
        )

    def test_a_scan_that_probed_nothing_says_nothing(self, window):
        # Every scan that has no near-equal durations would otherwise carry a
        # row of zeroes about a phase that never ran
        win = self._window(window)
        win._log_scan_summary(self._summary(probes=0, hits=0))
        assert not any("probe:" in t for t in _records(win))

    def test_a_repeat_scan_names_the_pairs_an_earlier_scan_judged(self, window):
        # "400 tested" and "400 tested again" look identical unless the
        # already-known ones are named; only one of them is progress
        win = self._window(window)
        win._log_scan_summary(self._summary(probes=40, hits=0, cached=360))
        row = next(e for e in win._scan_log_records if "probe:" in e.text)
        assert row.text == (
            "  probe: 40 of 400 candidate pairs tested (360 already known), 0 found a shift"
        )
        assert row.level == "detail"


class TestDurationGateSummary:
    """A scan that skipped pairs has to say so.

    The duration gate removes pairs before anything is compared, so its count
    appears in no other number of the summary. Without its own row, a scan
    that compared half of what the indexes proposed looks like a scan that
    found less.
    """

    @staticmethod
    def _summary(pruned: int = 1467) -> dict:
        return {
            "status": "completed",
            "total_files": 6441,
            "missing_files": 0,
            "error_files": 0,
            "errors": [],
            "candidate_pairs": 2843,
            "comparisons": 1376,
            "rejected": 1371,
            "duration_pruned": pruned,
        }

    @staticmethod
    def _window(window):
        win = window()
        win.all_pairs = []
        win.decisions = {}
        return win

    def test_the_gate_is_named_with_its_threshold(self, window):
        win = self._window(window)
        win.config = replace(
            win.config, matching=replace(win.config.matching, min_duration_ratio=0.65)
        )
        win._log_scan_summary(self._summary())

        row = next(e for e in win._scan_log_records if e.text.startswith("  gate:"))
        assert row.text == (
            "  gate: 1,467 candidate pairs skipped before comparison "
            "(shorter copy under 65% of the longer one)"
        )
        assert row.level == "detail"

    def test_a_scan_that_pruned_nothing_says_nothing(self, window):
        win = self._window(window)
        win._log_scan_summary(self._summary(pruned=0))

        assert not any("gate:" in e.text for e in win._scan_log_records)

"""The idle Scan tab must look idle.

Regression: the progress bar was created visible, and every finish path
parked it at maximum with a ``"%p%"`` format — so a finished (or never
started) scan showed a full blue bar reading 100% on every tab for the
rest of the session. That reads as "still working" and steals a row of
height from the tables. The folder list, the Scan log and the Result page
were also blank when empty, and elided folder paths carried no tooltip.
"""

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _config(tmp_path, roots=None, excludes=()):
    # The default is the test's own tmp folder; a test that wants the
    # never-chosen state passes roots=[] explicitly
    chosen = roots if roots is not None else [str(tmp_path)]
    cfg = Config(roots=[RootConfig(path=r) for r in chosen])
    cfg = replace(cfg, exclude_patterns=list(excludes))
    return replace(
        cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports"))
    )


def _make_window(tmp_path, config, *, with_pair=False, scan_only=False, dismiss_pair=False):
    repo = DatabaseRepository(tmp_path / "v.db")
    if with_pair or scan_only:
        scan_id = repo.create_scan("test")
        root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        ids = []
        for i in range(2):
            video = tmp_path / f"v{i}.mp4"
            video.write_bytes(b"x")
            ids.append(
                repo.create_file(
                    root_id,
                    str(video),
                    str(video).lower(),
                    video.name,
                    ".mp4",
                    1,
                    1000 + i,
                    scan_id,
                )
            )
        if with_pair:
            repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "VERY_LIKELY", 1.0, 0.01, 12)
        if dismiss_pair:
            keys = sorted((str(tmp_path / "v0.mp4").lower(), str(tmp_path / "v1.mp4").lower()))
            repo.dismiss_pair(keys[0], keys[1])
        repo.commit()
    win = gui_app.MainWindow(config, repo, tmp_path / "config.toml")
    win.reload_data()
    return win, repo


@pytest.fixture()
def make_window(tmp_path, qapp):
    created: list[tuple[object, DatabaseRepository]] = []

    def _make(config, *, with_pair=False, scan_only=False, dismiss_pair=False):
        win, repo = _make_window(
            tmp_path,
            config,
            with_pair=with_pair,
            scan_only=scan_only,
            dismiss_pair=dismiss_pair,
        )
        created.append((win, repo))
        return win

    yield _make

    for win, repo in created:
        # Timers outlive the widget unless stopped explicitly
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
        repo.close()


def _result_texts(win) -> list[str]:
    return [label.text() for label in win._result_container.findChildren(QLabel)]


def _window_texts(win) -> list[str]:
    return [label.text() for label in win.findChildren(QLabel)]


class TestMatchingControls:
    """Both sliders keep their timing out of the panel.

    They carried 10px chips instead — "next scan" and "updates this view" —
    which named the timing in the pipeline's own vocabulary. A reader who
    does not already know the pipeline cannot tell whether that is good or
    bad. The timing lives in the sliders' tooltips, so the panel itself is
    just the two controls and their value labels.
    """

    def test_no_scope_chips_survive(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        texts = _window_texts(win)
        assert "next scan" not in texts
        assert "updates this view" not in texts
        # The duration limit is a section heading of its own now; the scope
        # note beside the slider was removed with the old boxed layout.
        assert "Applies to every option" not in texts

    def test_the_duration_value_says_what_it_measures(self, make_window, tmp_path):
        config = _config(tmp_path)
        config = replace(
            config,
            matching=replace(config.matching, min_duration_ratio=0.9),
        )
        win = make_window(config)
        assert win.duration_value.text() == "Up to 10%"
        win.duration_slider.setValue(15)
        assert win.duration_value.text() == "Up to 7.5%"
        win.duration_slider.setValue(1)
        assert win.duration_value.text() == "Up to 0.5%"
        # 0 means no length difference at all, not "up to 0%"
        win.duration_slider.setValue(0)
        assert win.duration_value.text() == "Equal"
        assert win.duration_slider.minimum() == 0
        # 25% is the gate's top, at half a point per slider unit
        assert win.duration_slider.maximum() == 50

    def test_a_tolerance_over_the_new_maximum_loads_at_the_maximum(self, make_window, tmp_path):
        config = _config(tmp_path)
        config = replace(
            config,
            matching=replace(config.matching, min_duration_ratio=0.5),
        )
        win = make_window(config)
        assert win.duration_slider.value() == 50
        assert win.duration_value.text() == "Up to 25%"
        # The gate the scan reads follows the label, not the old value
        assert win.config.matching.min_duration_ratio == pytest.approx(0.75)

    def test_the_labels_say_what_the_sliders_do(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        texts = " ".join(_window_texts(win))
        assert "Strictness" in texts
        assert "Duration difference limit" in texts
        # "(possible matches)" was a parenthetical nobody could act on
        assert "Max duration difference" not in texts

    def test_the_length_block_aligns_with_the_strictness_rows(self, make_window, tmp_path):
        # The value and the track sit on the Custom hint's edge, under the
        # section heading that names them.
        win = make_window(_config(tmp_path))
        win.resize(1500, 900)
        win.show()
        QApplication.processEvents()
        labels = {label.text(): label for label in win.findChildren(QLabel)}
        edge = labels["Strictness"].mapTo(win, labels["Strictness"].rect().topLeft()).x()
        for widget in (win.duration_value, labels["Loose"]):
            assert widget.mapTo(win, widget.rect().topLeft()).x() == edge

    def test_moving_the_duration_slider_leaves_the_preset_alone(self, make_window, tmp_path):
        # The duration limit applies whatever preset is chosen, so it is not a
        # custom strictness: moving it keeps the preset selected, the custom
        # block dimmed, and the slider itself live.
        win = make_window(_config(tmp_path))
        assert win._profile_radios[50].isChecked() is True
        assert win._custom_panel.isEnabled() is False
        assert win.duration_slider.isEnabled() is True
        win.duration_slider.setValue(win.duration_slider.value() + 1)
        assert win._profile_radios[50].isChecked() is True
        assert win._custom_radio.isChecked() is False
        assert win._custom_panel.isEnabled() is False
        assert win.duration_slider.isEnabled() is True

    def test_a_preset_leaves_the_length_tolerance_unchanged(self, make_window, tmp_path):
        # The presets describe content similarity only. They used to look as
        # if they owned the length tolerance because the whole custom block
        # dimmed — including its slider — while the value still applied.
        win = make_window(_config(tmp_path))
        win.duration_slider.setValue(20)
        tolerance = win.config.matching.min_duration_ratio
        win._profile_radios[75].setChecked(True)
        assert win.duration_slider.value() == 20
        assert win.config.matching.min_duration_ratio == tolerance

    def test_the_duration_ends_match_the_inverted_slider(self, make_window, tmp_path):
        # Both matching sliders put the loose end on the left: "Loose" for
        # strictness and for the duration gate, where it allows the biggest
        # difference. Inverting the track makes the thumb move right as the
        # setting gets stricter, so "Equal" sits on the right.
        win = make_window(_config(tmp_path))
        win.resize(1500, 900)
        win.show()
        QApplication.processEvents()
        labels = {label.text(): label for label in win.findChildren(QLabel)}
        assert labels["Loose"].x() < labels["Equal"].x()
        assert win.duration_slider.invertedAppearance() is True

    def test_moving_strictness_selects_custom_even_on_a_preset_value(self, make_window, tmp_path):
        # Dragging through 50 (a preset's value) used to check that preset's
        # radio, which disables the custom panel — and the handle still being
        # held. Moving the slider is a custom setting by definition.
        win = make_window(_config(tmp_path))
        win._profile_radios[50].setChecked(True)
        assert win._custom_panel.isEnabled() is False
        win.strictness_slider.setValue(51)
        win.strictness_slider.setValue(50)
        assert win._custom_radio.isChecked() is True
        assert win._custom_panel.isEnabled() is True

    def test_choosing_a_preset_keeps_the_preset_selected(self, make_window, tmp_path):
        # The preset writes its value into the slider; that write must not be
        # read as a slider move and bounce the selection back to Custom.
        win = make_window(_config(tmp_path))
        win._custom_radio.setChecked(True)
        win._profile_radios[75].setChecked(True)
        assert win.strictness_slider.value() == 75
        assert win._profile_radios[75].isChecked() is True
        assert win._custom_radio.isChecked() is False

    def test_a_preset_dims_the_whole_custom_area(self, make_window, tmp_path):
        # Disabling the frame only greyed the slider: the labels kept their
        # own colours and the area still read as live. The veil dims all of
        # it, and Custom alone brings it back. The duration limit sits outside
        # the veil and stays live under every preset.
        win = make_window(_config(tmp_path))
        assert win._custom_veil.opacity() < 1.0
        assert win.duration_slider.isEnabled() is True
        win._custom_radio.setChecked(True)
        assert win._custom_veil.opacity() == 1.0
        win._profile_radios[50].setChecked(True)
        assert win._custom_veil.opacity() < 1.0
        assert win.duration_slider.isEnabled() is True

    def test_the_presets_behave_as_one_radio_set(self, make_window, tmp_path):
        # Each option sits in its own row widget, so Qt's parent-scoped
        # auto-exclusivity never applied: all four could stay checked at once.
        win = make_window(_config(tmp_path))
        radios = [*win._profile_radios.values(), win._custom_radio]
        win._profile_radios[75].setChecked(True)
        assert [r.isChecked() for r in radios] == [True, False, False, False]
        win._custom_radio.setChecked(True)
        assert [r.isChecked() for r in radios] == [False, False, False, True]


class TestDurationSliderIsAScanSetting:
    """The slider must reach the matcher, not only label the list.

    It used to be written to [reporting] alone, where nothing that decides
    which pairs a scan compares could see it — a scan then compared pairs the
    user had already said were not duplicates, and the list threw them away
    afterwards. Saving it into [matching] is what makes the next scan skip
    them instead.
    """

    def test_moving_it_saves_the_gate_into_matching(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))

        win.duration_slider.setValue(20)

        assert win.config.matching.min_duration_ratio == pytest.approx(0.9)
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "min_duration_ratio = 0.9" in text
        # The deprecated [reporting] copy is removed on save, not written again
        assert "review_min_duration_ratio" not in text

    def test_zero_saves_a_gate_that_accepts_only_equal_durations(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))

        win.duration_slider.setValue(0)

        assert win.config.matching.min_duration_ratio == pytest.approx(1.0)
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "min_duration_ratio = 1.0" in text


class TestWindowChrome:
    """The frame around the pages: an icon, and air under the title bar."""

    def test_the_window_carries_the_app_icon(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        assert not win.windowIcon().isNull()

    def test_the_tab_strip_is_not_flush_against_the_title_bar(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        top = win.centralWidget().layout().contentsMargins().top()
        # Flush, the tab strip reads as part of the title bar
        assert top >= 6

    def test_the_log_comes_before_the_review_destination(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        labels = [win.tabs.tabText(i) for i in range(win.tabs.count())]
        assert labels[0] == "Scan"
        assert labels[1] == "Scan log"
        assert labels[2].startswith("Results")
        assert labels[3] == "Tools"


class TestScanTabLayout:
    def test_the_drop_zone_paints_its_dashed_rectangle(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        zone = win.findChild(gui_app.DropZone)
        assert zone is not None
        # A custom QWidget does not paint a QSS background/border without it,
        # which is why the dashed rectangle was missing at first.
        assert zone.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)

    def test_the_folders_card_keeps_its_heading_at_the_top(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        win.resize(1500, 900)
        win.show()
        QApplication.processEvents()
        heading = next(
            child for child in win.findChildren(QLabel) if child.text() == "Folders to scan"
        )
        assert heading.geometry().top() < 30


class TestStatusStrip:
    """The bottom strip is the one status surface."""

    def test_the_idle_strip_shows_the_last_scan_instead_of_ready(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        assert not hasattr(win, "status_ready_label")
        assert not hasattr(win, "settings_hint_label")
        assert "No scan yet" in win._last_scan_label.text()
        assert win._last_scan_label.parent() is win._status_strip
        assert win._progress_panel.isHidden()
        assert not win._status_strip.isHidden()
        assert "Ready" not in [label.text() for label in win._status_strip.findChildren(QLabel)]

    def test_a_message_lands_in_the_strip_and_schedules_its_clear(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        win._show_status_message("Settings saved.", 1000)
        assert win.status_message_label.text() == "Settings saved."
        assert win._status_timer.isActive()
        win._status_timer.stop()
        win._reset_status_message()
        assert win.status_message_label.text() == ""

    def test_the_strip_carries_no_scan_button(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        assert not hasattr(win, "scan_compact_button")
        texts = [b.text() for b in win._status_strip.findChildren(QPushButton)]
        assert texts == []


class TestEmptyReviewControls:
    """The review toolbar is inert when the page has nothing to act on."""

    def test_an_empty_page_disables_every_review_action(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        assert not win._sort_combo.isEnabled()
        assert not win._hide_decided_box.isEnabled()
        assert not win._compare_button.isEnabled()
        assert not win._dismiss_all_action.isEnabled()
        assert not win._quarantine_button.isEnabled()

    def test_a_pair_enables_the_review_actions(self, make_window, tmp_path):
        win = make_window(_config(tmp_path), with_pair=True)
        assert win._sort_combo.isEnabled()
        assert win._hide_decided_box.isEnabled()
        assert win._compare_button.isEnabled()
        assert win._dismiss_all_action.isEnabled()
        # Nothing is marked for removal yet
        assert not win._quarantine_button.isEnabled()

    def test_marked_files_lives_while_a_mark_does(self, make_window, tmp_path):
        win = make_window(_config(tmp_path), with_pair=True)
        win.cards[win.all_pairs[0]["file_b_id"]][0]._decide("remove")
        assert win._quarantine_button.isEnabled()
        assert win._quarantine_button.text() == "Marked files 1"

    def test_a_filter_that_hides_every_pair_keeps_the_filters_live(self, make_window, tmp_path):
        win = make_window(_config(tmp_path), with_pair=True)
        win.cards[win.all_pairs[0]["file_a_id"]][0]._decide("keep")
        win._hide_decided_box.setChecked(True)
        assert win._sort_combo.isEnabled()
        assert win._hide_decided_box.isEnabled()
        assert not win._compare_button.isEnabled()
        assert not win._dismiss_all_action.isEnabled()


class TestIdleProgressBar:
    def test_hidden_at_startup(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        assert win.progress_bar.isHidden()
        assert win.progress_bar.isHidden()

    def test_hidden_after_previews_drain(self, make_window, tmp_path):
        win = make_window(_config(tmp_path, [str(tmp_path)]))
        win.progress_bar.setVisible(True)
        win._preview_total = 2
        win._rendering_previews = True

        win._on_pending_changed(0)

        assert win._rendering_previews is False
        assert win.progress_bar.isHidden()
        assert win.progress_bar.format() == "%p%"

    def test_hidden_after_cancelled_scan(self, make_window, tmp_path):
        win = make_window(_config(tmp_path, [str(tmp_path)]))
        win.progress_bar.setVisible(True)

        win._on_scan_finished({"status": "interrupted"})

        assert win.progress_bar.isHidden()
        assert win.tabs.currentWidget() is win._result_container


class TestToolsCards:
    def test_the_cards_grow_to_fit_their_wrapped_text(self, make_window, tmp_path):
        # The wrapped description used to be measured at one width and placed
        # at another, so its last line was painted clipped while the card had
        # spare room below it.
        win = make_window(_config(tmp_path))
        win.resize(900, 720)
        win.show()
        win.tabs.setCurrentIndex(3)
        QApplication.processEvents()
        cards = win.tabs.widget(3).findChildren(gui_app.Card)
        assert cards
        for card in cards:
            labels = card.findChildren(gui_app.WrappedLabel)
            assert labels
            for label in labels:
                assert label.height() >= label.heightForWidth(label.width())


class TestEmptyStates:
    def test_folders_hint_visible_without_rows(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        win.folders_table.setRowCount(0)
        win._refresh_folders_hint()
        assert win.folders_table.rowCount() == 0
        assert win._folders_hint.isHidden() is False

    def test_folders_hint_hides_after_adding(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        win.folders_table.setRowCount(0)
        win._refresh_folders_hint()
        assert win._append_folder_row(str(tmp_path / "new"), excluded=False) is True
        assert win._folders_hint.isHidden() is True

    def test_scan_log_placeholder(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        assert "scan" in win.scan_log.placeholderText().lower()

    def test_result_hint_without_pairs(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        assert any("run a scan" in text.lower() for text in _result_texts(win))
        assert "Nothing to review" in _result_texts(win)

    def test_the_empty_state_icon_is_not_squeezed(self, make_window, tmp_path):
        # The label used to shrink below the pixmap when the page was tight and
        # painted the bottom off the icon: the magnifier lost its handle.
        win = make_window(_config(tmp_path))
        win.resize(900, 700)
        win.show()
        QApplication.processEvents()
        win.tabs.setCurrentWidget(win._result_container)
        QApplication.processEvents()
        state = win.findChild(gui_app.EmptyState)
        icon = state.icon_label
        wanted = icon.pixmap().deviceIndependentSize().toSize()
        assert icon.size() == wanted

    def test_result_hint_when_filters_hide_everything(self, make_window, tmp_path):
        win = make_window(_config(tmp_path), with_pair=True)
        win._result_hide_decided = True
        for file_id in win.files:
            win.decisions[file_id] = "keep"

        win._rebuild_review_tabs()

        assert any("hidden by the current filters" in text for text in _result_texts(win))

    def test_a_finished_scan_without_duplicates_says_so(self, make_window, tmp_path):
        """It used to say "run a scan" at a scan that had just finished."""
        win = make_window(_config(tmp_path), scan_only=True)
        texts = _result_texts(win)
        assert "No duplicates found" in texts
        assert any("2 files checked" in text for text in texts)
        buttons = [b.text() for b in win._result_container.findChildren(QPushButton)]
        assert "Scan again" not in buttons
        assert "Go to Scan" in buttons

    def test_the_filter_row_says_there_is_nothing_to_review(self, make_window, tmp_path):
        # A "Pairs to review (0)" heading and its sort hint above "No
        # duplicates found" repeated the empty state; the filter row now
        # carries a short progress reading instead.
        win = make_window(_config(tmp_path), scan_only=True)
        assert win._review_progress_label.text() == "No pairs"
        assert win._review_progress.value() == 0

    def test_the_filter_row_counts_the_pairs(self, make_window, tmp_path):
        win = make_window(_config(tmp_path), with_pair=True)
        assert win._review_progress_label.text() == "0 of 1 reviewed"
        assert win._review_progress.value() == 0

    def test_all_pairs_ignored_offers_the_ignored_list(self, make_window, tmp_path):
        win = make_window(_config(tmp_path), with_pair=True, dismiss_pair=True)
        texts = _result_texts(win)
        assert "1 pair is ignored" in texts
        assert "Review ignored pairs" in [
            b.text() for b in win._result_container.findChildren(QPushButton)
        ]

    def test_moving_the_duration_slider_keeps_existing_pairs(self, make_window, tmp_path):
        # The slider is a setting for the next scan, not a filter over rows a
        # scan already compared: a short pair stays on the page and in the
        # list until that rescan.
        win = make_window(_config(tmp_path), with_pair=True)
        pair = win.all_pairs[0]
        win.probes[pair["file_a_id"]] = {"container_duration": 100.0}
        win.probes[pair["file_b_id"]] = {"container_duration": 10.0}

        win.duration_slider.setValue(50)

        assert win._visible_pairs() == win.all_pairs
        assert win._pair_boxes
        assert not any("outside the length tolerance" in text for text in _result_texts(win))
        assert "Reset length tolerance" not in [
            b.text() for b in win._result_container.findChildren(QPushButton)
        ]


class TestFolderRowRendering:
    def test_root_row_tooltip_is_full_path(self, make_window, tmp_path):
        root = str(tmp_path / "a" / "very" / "long" / "folder" / "name")
        win = make_window(_config(tmp_path, [root]))
        item = win.folders_table.item(0, win._COL_PATH)
        assert item is not None
        assert item.toolTip() == root

    def test_excluded_row_tooltip_is_full_path(self, make_window, tmp_path):
        win = make_window(_config(tmp_path, excludes=["C:/library/Excluded/*"]))
        item = win.folders_table.item(1, win._COL_PATH)
        assert item is not None
        # Displayed in the platform's separator form; the config keeps the
        # forward-slash form the loader writes
        assert item.text() == str(Path("C:/library/Excluded"))
        assert item.toolTip() == str(Path("C:/library/Excluded"))

    def test_paths_elide_in_the_middle(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        assert win.folders_table.textElideMode() == Qt.TextElideMode.ElideMiddle
        # Word wrap beats elision: with it on, a narrow column wrapped the
        # path instead and every cell read "C:…"
        assert win.folders_table.wordWrap() is False

    def test_a_long_name_pattern_keeps_its_whole_text_in_the_tooltip(self, make_window, tmp_path):
        long_pattern = "some_really_long_release_group_name_*_sample.*"
        win = make_window(_config(tmp_path, excludes=[long_pattern]))
        # The pattern row is the one after the root row
        item = win.folders_table.item(win.folders_table.rowCount() - 1, win._COL_PATH)
        assert item is not None
        assert item.data(Qt.ItemDataRole.UserRole) == "pattern"
        assert long_pattern in item.toolTip()

    def test_adding_a_listed_folder_says_it_is_already_listed(self, make_window, tmp_path):
        win = make_window(_config(tmp_path))
        win._add_exclude_paths([str(tmp_path)])

        assert "already listed" in win.status_message_label.text().lower()


class TestNoScanRoots:
    """An empty folder list is a state, not an error.

    A fresh install has chosen nothing, and the last folder may be removed
    before another is added. The scan could still be started, walked nothing
    and reported zero pairs — a result that reads as "your library is empty".
    """

    def test_a_fresh_install_can_have_no_folders(self, make_window, tmp_path):
        win = make_window(_config(tmp_path, roots=[]))
        assert win.config.roots == []
        assert win.folders_table.rowCount() == 0
        assert win._folders_hint.isHidden() is False

    def test_scan_is_disabled_and_says_why(self, make_window, tmp_path):
        win = make_window(_config(tmp_path, roots=[]))
        assert not win.scan_now_button.isEnabled()
        assert "folder" in win.scan_now_button.toolTip().lower()

    def test_adding_a_folder_enables_scan(self, make_window, tmp_path):
        win = make_window(_config(tmp_path, roots=[]))
        win._append_folder_row(str(tmp_path / "new"), excluded=False)
        win._save_settings(quiet=True)
        assert [r.path for r in win.config.roots] == [str(tmp_path / "new")]
        assert win.scan_now_button.isEnabled()

    def test_removing_the_last_folder_saves_the_empty_list(self, make_window, tmp_path):
        win = make_window(_config(tmp_path, [str(tmp_path)]))
        win._remove_folder_row(0)
        win._save_settings(quiet=True)
        assert win.config.roots == []
        assert win.folders_table.rowCount() == 0
        assert not win.scan_now_button.isEnabled()
        assert "0 folders selected for the next scan" in win.db_status.text()

    def test_scan_does_not_start_without_a_folder(self, make_window, tmp_path):
        win = make_window(_config(tmp_path, roots=[]))
        win._start_scan()
        assert win.scan_worker is None
        assert "add a folder" in win.status_message_label.text().lower()


class TestReportExportErrors:
    def test_a_failed_export_says_so(self, make_window, tmp_path, monkeypatch):
        """Only OSError used to be caught.

        A malformed evidence row raised ValueError out of the handler, which
        left the reviewer with no file and no message — the click read as
        ignored.
        """
        from akin_video_finder.reporting import exporter

        win = make_window(_config(tmp_path))
        win.scan_id = 1
        monkeypatch.setattr(
            gui_app.QFileDialog,
            "getSaveFileName",
            lambda *a, **k: (str(tmp_path / "out.json"), "JSON (*.json)"),
        )
        warnings: list[str] = []
        monkeypatch.setattr(
            gui_app.QMessageBox, "warning", lambda parent, title, text: warnings.append(text)
        )
        monkeypatch.setattr(gui_app.QMessageBox, "information", lambda *a, **k: None)

        def boom(repository, scan_id, fmt, config=None):
            raise ValueError("bad evidence")

        monkeypatch.setattr(exporter, "export_report", boom)
        win._export_report()

        assert warnings
        assert "bad evidence" in warnings[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Explorer's /select form is Windows-only")
class TestRevealInExplorer:
    def test_a_log_row_uses_the_same_hand_built_select_form(
        self, make_window, tmp_path, monkeypatch
    ):
        """The log row used the argv form, which explorer quotes wrongly.

        Double-clicking a per-file failure therefore opened Documents instead
        of the file's folder — the card's reveal had the working form all
        along.
        """
        win = make_window(_config(tmp_path))
        target = tmp_path / "clip.mp4"
        target.write_bytes(b"x")
        calls: list[object] = []
        monkeypatch.setattr(gui_app.subprocess, "Popen", lambda *a, **k: calls.append(a[0]))

        win._reveal_log_path(str(target))

        assert calls == [f'explorer /select,"{target}"']

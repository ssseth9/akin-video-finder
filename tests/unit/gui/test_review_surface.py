"""The review surface: pair header, details, decision badges, empty states."""

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, QSizeF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
import akin_video_finder.gui.widgets as gui_widgets  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.core.models import ProbeStatus  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def review(tmp_path, qapp, monkeypatch):
    """One POSSIBLE pair with evidence and real probe metrics."""
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    ids = []
    for i, (size, duration) in enumerate(((1_000_000, 6873.42), (2_000_000, 6873.61))):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x" * size)
        file_id = repo.create_file(
            root_id,
            str(video),
            str(video).lower(),
            video.name,
            ".mp4",
            size,
            1000 + i,
            scan_id,
        )
        repo.create_probe_metadata(
            file_id,
            ProbeStatus.SUCCESS,
            "v1",
            container_duration=duration,
            container_bitrate=5_000_000,
            primary_video_codec="h264",
            primary_video_width=1920,
            primary_video_height=1080,
        )
        ids.append(file_id)
    evidence = json.dumps(
        {
            "mode": "trim-offset-trim",
            "matching_frames": 38,
            "frames_compared": 48,
            "coverage_a": 1.0,
            "coverage_b": 1.0,
            "offset_seconds": 12.0,
            "audio_distance": 0.004,
        }
    )
    repo.create_pair_comparison(
        scan_id,
        ids[0],
        ids[1],
        "v1",
        "POSSIBLE",
        0.79,
        0.105,
        48,
        evidence=evidence,
    )
    repo.commit()
    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    win.reload_data()
    yield win, repo, ids
    win._sheet_sweep.stop()
    win._sheet_sweep_idle.stop()
    win.sheet_loader.watchdog.stop()
    win.close()
    win.deleteLater()
    repo.close()


def _labels(widget) -> list[str]:
    return [
        label.full_text() if isinstance(label, gui_app.ElidedLabel) else label.text()
        for label in widget.findChildren(QLabel)
    ]


def test_pair_header_says_the_sentence_and_hides_raw_diagnostics(review) -> None:
    win, _repo, _ids = review
    texts = _labels(win._result_container)
    assert "Pair 1 of 1" in texts
    assert "Possible duplicate" in texts
    assert "79.00% match" in texts
    assert any(t == "38 of 48 sampled frames matched · trimmed copy · audio similar" for t in texts)
    assert not any("dist 0.105" in t or "mode=" in t for t in texts)


def test_the_details_dialog_keeps_the_raw_values(review) -> None:
    win, _repo, _ids = review
    dialog = gui_app.PairDetailsDialog(win, win.all_pairs[0])
    texts = " ".join(_labels(dialog))
    assert "trim-offset-trim" in texts
    assert "38 of 48" in texts
    assert "+12.000 s" in texts
    assert "0.0040" in texts
    # The exact values behind the chips: bytes, raw seconds, bps, codec.
    assert "1,000,000 bytes" in texts
    assert "6873.42 s" in texts
    assert "1920 × 1080" in texts
    assert "5,000,000 bps" in texts
    assert "H.264" in texts


def test_file_card_path_is_clickable_and_has_no_show_in_folder_link(review) -> None:
    win, _repo, ids = review
    card = win.cards[ids[0]][0]
    assert card.name_label.text() == "v0.mp4"
    assert "v0.mp4" not in card.folder_label.text()
    assert card.name_label.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert card.folder_label.cursor().shape() == Qt.CursorShape.PointingHandCursor
    button_texts = [b.text() for b in card.findChildren(QPushButton)]
    assert "Show in folder" not in button_texts
    assert "Open in player" not in button_texts
    assert card.image_label.play_button.toolTip() == "Open in player"


def test_only_the_best_metric_chip_is_marked(review) -> None:
    win, _repo, ids = review
    cards = [win.cards[ids[0]][0], win.cards[ids[1]][0]]
    chip_texts = [t for card in cards for t in _labels(card) if "▲" in t]
    assert chip_texts
    assert not any("GB" in text and "▲" in text for text in chip_texts)


def test_a_decision_replaces_the_recommendation_badge(review) -> None:
    win, _repo, ids = review
    keeper = win.cards[ids[0]][0]
    keeper._decide("keep")
    peer = win.cards[ids[1]][0]
    assert keeper.kept_badge.text() == "✓ Kept"
    assert peer.remove_badge.text() == "Remove"
    assert keeper.recommended_badge.text() == ""
    assert peer.recommended_badge.text() == ""


def test_ignore_is_a_neutral_action_with_no_keyboard_key(review) -> None:
    """Ignore sits beside Keep/Mark rather than owning a page's accent.

    The redesign puts the recurring action in every row, but the action is
    not the page's primary button: blue is left for the one thing the page
    starts (Compare), and no I keycap is added to a row that already has a
    Compare window with its own shortcut.
    """
    win, _repo, _ids = review
    buttons = [
        button
        for button in win._pair_boxes[0].findChildren(QPushButton)
        if button.text() == "Ignore pair"
    ]
    assert len(buttons) == 1
    assert buttons[0].property("role") == "secondary"
    assert buttons[0].findChildren(QLabel) == []


def test_the_progress_reading_follows_a_decision(review) -> None:
    win, _repo, ids = review
    assert win._review_progress_label.text() == "0 of 1 reviewed"
    win.cards[ids[0]][0]._decide("keep")
    assert win._review_progress_label.text() == "1 of 1 reviewed"
    assert win._review_progress.value() == 100


def test_ignoring_a_pair_offers_undo_and_undo_restores_it(review) -> None:
    win, repo, _ids = review
    comparison = win.all_pairs[0]
    box = win._pair_boxes[0]
    assert win._review_toast.isHidden()

    win._dismiss_pair(comparison, box)

    assert win.all_pairs == []
    assert win._review_toast._message.text() == "Pair ignored"
    assert not win._review_toast.isHidden()
    assert repo.get_dismissed_pairs()

    undo = next(
        button for button in win._review_toast.findChildren(QPushButton) if button.text() == "Undo"
    )
    undo.click()

    assert len(win.all_pairs) == 1
    assert repo.get_dismissed_pairs() == set()
    assert win._review_progress_label.text() == "0 of 1 reviewed"


def test_the_results_page_binds_undo_but_not_ignore(review) -> None:
    win, _repo, _ids = review
    keys = {
        shortcut.key().toString()
        for shortcut in win._result_container.findChildren(gui_app.QShortcut)
    }
    assert "U" in keys
    assert "I" not in keys


def test_a_certain_match_turns_the_whole_confidence_marker_green(review) -> None:
    win, _repo, _ids = review
    win.all_pairs[0]["match_fraction"] = 1.0
    win._rebuild_review_tabs()
    box = win._pair_boxes[0]
    confidence = gui_app._match_color(1.0, win.config.matching.possible_threshold)
    pill = box.findChild(QLabel, "confidencePill")
    dot = box.findChild(QLabel, "confidenceDot")
    halo = box.findChild(QLabel, "confidenceDotHalo")
    assert pill is not None and dot is not None and halo is not None
    assert confidence in pill.styleSheet()
    assert confidence in dot.styleSheet()
    assert gui_app._with_alpha(confidence, 0.13) in halo.styleSheet()


def test_pair_details_names_the_classification_in_badge_words(review) -> None:
    """The enum leaked into the one human-facing row in that dialog."""
    win, _repo, ids = review
    comparison = dict(win.all_pairs[0])
    comparison["classification"] = "VERY_LIKELY"

    dialog = gui_app.PairDetailsDialog(win, comparison)
    try:
        texts = [label.text() for label in dialog.findChildren(QLabel)]
        assert "Very likely duplicate" in texts
        assert "VERY_LIKELY" not in texts
    finally:
        dialog.close()
        dialog.deleteLater()


def test_strictness_midpoint_is_the_untouched_default() -> None:
    """Visiting the slider and returning to 50% used to rewrite thresholds.

    The linear map's midpoint was 0.105 / 0.085 / 7 frames where the
    MatchingConfig defaults are 0.10 / 0.08 / 6, so a scan run after a
    glance at the slider used different matching values than the first run.
    """
    from akin_video_finder.configuration.models import MatchingConfig

    defaults = MatchingConfig()
    fields = gui_app._strictness_fields(50)
    for key, value in fields.items():
        assert value == getattr(defaults, key), key
    # The measured ends are untouched
    assert fields["frame_match_max_distance"] == 0.1
    assert gui_app._strictness_fields(0)["frame_match_max_distance"] == 0.15
    assert gui_app._strictness_fields(100)["frame_match_max_distance"] == 0.06


def test_marking_one_copy_for_removal_keeps_the_other(review) -> None:
    win, _repo, ids = review
    win.cards[ids[1]][0]._decide("remove")
    assert win.decisions[ids[1]] == "remove"
    assert win.decisions[ids[0]] == "keep"
    assert win.cards[ids[0]][0].kept_badge.text() == "✓ Kept"


def test_marking_the_kept_copy_for_removal_flips_the_first_to_keep(review) -> None:
    """A pair must not lose both files.

    Removing one copy auto-keeps its peer; clicking Remove on that peer used
    to be accepted as well, leaving a pair whose only two files were both
    selected for deletion. The invariant now moves the removal and keeps the
    earlier copy.
    """
    win, _repo, ids = review
    win.cards[ids[1]][0]._decide("remove")
    win.cards[ids[0]][0]._decide("remove")

    assert win.decisions[ids[0]] == "remove"
    assert win.decisions[ids[1]] == "keep"
    assert win.cards[ids[1]][0].kept_badge.text() == "✓ Kept"
    assert win.fully_removed_sets() == []
    assert win._quarantine_button.text() == "Marked files 1"


def test_a_third_copy_still_lets_the_pair_keep_its_other_copy(review, tmp_path) -> None:
    """The flip is pair-local: a set with another kept copy loses nothing."""
    win, repo, ids = review
    root_id = repo.get_all_roots()[0]["id"]
    video = tmp_path / "v2.mp4"
    video.write_bytes(b"x")
    third = repo.create_file(
        root_id, str(video), str(video).lower(), video.name, ".mp4", 1, 1002, win.scan_id
    )
    repo.create_pair_comparison(win.scan_id, ids[0], third, "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()
    win.reload_data()

    win.cards[ids[1]][0]._decide("remove")  # v1 out, v0 kept
    card = next(c for c in win.cards[third] if ids[0] in c.peer_ids)
    card._decide("remove")  # v2 out; its peer v0 is already kept

    assert win.decisions[ids[0]] == "keep"
    assert win.decisions[ids[1]] == "remove"
    assert win.decisions[third] == "remove"
    assert win.fully_removed_sets() == []


def test_a_decided_copy_leaves_no_pair_half_marked(review, tmp_path) -> None:
    """A mark on one copy used to complete only the pair it was made in.

    Reported with a set of more than three copies: the pairs sharing the
    decided copy kept one side marked and left the other blank. Every pair
    now reads one kept and one removed.
    """
    win, repo, ids = review
    root_id = repo.get_all_roots()[0]["id"]
    extra = []
    for i in (2, 3):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x")
        file_id = repo.create_file(
            root_id,
            str(video),
            str(video).lower(),
            video.name,
            ".mp4",
            1,
            1000 + i,
            win.scan_id,
        )
        repo.create_pair_comparison(
            win.scan_id, ids[0], file_id, "v1", "VERY_LIKELY", 1.0, 0.01, 12
        )
        extra.append(file_id)
    repo.commit()
    win.reload_data()

    win.cards[ids[0]][0]._decide("remove")

    assert win.cards[ids[0]][0].remove_badge.text() == "Remove"
    for peer in (ids[1], *extra):
        assert win.decisions[peer] == "keep"
        card = next(c for c in win.cards[peer] if ids[0] in c.peer_ids)
        assert card.kept_badge.text() == "✓ Kept"
    assert win.fully_removed_sets() == []

    # The Compare dialog builds its cards from the same decisions, so the
    # pair it opens on reads one kept and one removed too.
    comparison = next(
        c for c in win.all_pairs if {c["file_a_id"], c["file_b_id"]} == {ids[0], extra[0]}
    )
    dialog = gui_app.CompareDialog(win, start_pair=comparison)
    try:
        peer_card = next(
            card for card in dialog._dialog_cards if card.file_row["id"] == extra[0]
        )
        assert peer_card.kept_badge.text() == "✓ Kept"
        assert peer_card.keep_button.isChecked()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_deciding_one_copy_of_four_leaves_no_blank_side(review, tmp_path) -> None:
    """Even where every copy is paired with every other, no pair is left
    with one side marked and the other blank."""
    win, repo, ids = review
    root_id = repo.get_all_roots()[0]["id"]
    copies = list(ids)
    for i in (2, 3):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x")
        copies.append(
            repo.create_file(
                root_id,
                str(video),
                str(video).lower(),
                video.name,
                ".mp4",
                1,
                1000 + i,
                win.scan_id,
            )
        )
    for index, first in enumerate(copies):
        for second in copies[index + 1 :]:
            if {first, second} == set(ids):
                continue  # the fixture's own pair
            repo.create_pair_comparison(
                win.scan_id, first, second, "v1", "VERY_LIKELY", 1.0, 0.01, 12
            )
    repo.commit()
    win.reload_data()

    win.cards[ids[0]][0]._decide("keep")

    assert len(win.all_pairs) == 6
    for comparison in win.all_pairs:
        assert win.decisions.get(comparison["file_a_id"])
        assert win.decisions.get(comparison["file_b_id"])


def test_keeping_the_other_copy_flips_the_first_to_remove(review) -> None:
    """Keep is symmetric with Remove: a pair keeps one copy.

    Keeping one copy and then keeping its peer used to leave two ✓ Kept
    cards on one pair. The second keep now takes over, exactly as a second
    remove already did.
    """
    win, _repo, ids = review
    win.cards[ids[0]][0]._decide("keep")
    win.cards[ids[1]][0]._decide("keep")

    assert win.decisions[ids[1]] == "keep"
    assert win.decisions[ids[0]] == "remove"
    assert win.cards[ids[1]][0].kept_badge.text() == "✓ Kept"


def test_a_loaded_half_decided_pair_is_completed(review) -> None:
    """A pair cannot load with one side marked and the other blank.

    A decision written before cross-pair completion existed — or made while
    its other pair was hidden — used to reach the review as one ✓ Kept card
    beside two unchecked buttons. Loading completes the pair.
    """
    win, repo, ids = review
    repo.set_decision(win.scan_id, ids[0], "keep")
    repo.commit()

    win.reload_data()

    assert win.decisions[ids[0]] == "keep"
    assert win.decisions[ids[1]] == "remove"
    assert win.cards[ids[0]][0].keep_button.isChecked()
    assert win.cards[ids[1]][0].remove_button.isChecked()
    assert win.cards[ids[1]][0].remove_badge.text() == "Remove"


def test_loading_completes_a_chain_of_half_decided_pairs(review, tmp_path) -> None:
    """A fill can complete the next pair along, so the pass repeats."""
    win, repo, ids = review
    root_id = repo.get_all_roots()[0]["id"]
    video = tmp_path / "v2.mp4"
    video.write_bytes(b"x")
    third = repo.create_file(
        root_id, str(video), str(video).lower(), video.name, ".mp4", 1, 1002, win.scan_id
    )
    repo.create_pair_comparison(win.scan_id, ids[1], third, "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()
    repo.set_decision(win.scan_id, ids[0], "keep")
    repo.commit()

    win.reload_data()

    for comparison in win.all_pairs:
        assert bool(win.decisions.get(comparison["file_a_id"])) == bool(
            win.decisions.get(comparison["file_b_id"])
        )


def test_un_ignoring_a_pair_completes_it_when_one_side_was_decided_hidden(
    review, tmp_path
) -> None:
    """A pair ignored while a copy was decided comes back whole."""
    win, repo, ids = review
    root_id = repo.get_all_roots()[0]["id"]
    video = tmp_path / "v2.mp4"
    video.write_bytes(b"x")
    third = repo.create_file(
        root_id, str(video), str(video).lower(), video.name, ".mp4", 1, 1002, win.scan_id
    )
    repo.create_pair_comparison(win.scan_id, ids[0], third, "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()
    win.reload_data()

    key_a = win.files[ids[0]]["normalized_path_key"]
    key_b = win.files[third]["normalized_path_key"]
    repo.dismiss_pair(key_a, key_b)
    repo.commit()
    win.reload_data()

    win.cards[ids[0]][0]._decide("keep")
    assert win.decisions[ids[1]] == "remove"
    assert not win.decisions.get(third)

    repo.undismiss_pair(key_a, key_b)
    win.withdraw_dismissal(key_a, key_b, None)

    assert win.decisions[third] == "remove"


def test_keep_then_keep_with_three_copies_keeps_the_acted_on_pair(
    review, tmp_path
) -> None:
    """Each action completes every pair the decided copy appears in.

    v0-v1: keep v0 → v1 removed. v0-v2: keep v2 → v0 becomes removed, and
    the pairs v0 left half-decided are completed from v2's decision: v1 is
    a duplicate of v2 too, so it stays removed. v1-v2: keep v1 → v2 becomes
    removed and v0 follows v1's decision. No pair is left with only one
    side marked.
    """
    win, repo, ids = review
    root_id = repo.get_all_roots()[0]["id"]
    video = tmp_path / "v2.mp4"
    video.write_bytes(b"x")
    third = repo.create_file(
        root_id, str(video), str(video).lower(), video.name, ".mp4", 1, 1002, win.scan_id
    )
    repo.create_pair_comparison(win.scan_id, ids[0], third, "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.create_pair_comparison(win.scan_id, ids[1], third, "v1", "VERY_LIKELY", 1.0, 0.01, 12)
    repo.commit()
    win.reload_data()

    win.cards[ids[0]][0]._decide("keep")  # v0 kept, v1 removed
    v2_card = next(c for c in win.cards[third] if ids[0] in c.peer_ids)
    v2_card._decide("keep")  # v2 kept, v0 taken over, v1 follows v2

    assert win.decisions[third] == "keep"
    assert win.decisions[ids[0]] == "remove"
    assert win.decisions[ids[1]] == "remove"

    loser_card = next(c for c in win.cards[ids[1]] if third in c.peer_ids)
    loser_card._decide("keep")  # v1 kept, v2 taken over, v0 follows v1

    assert win.decisions[ids[1]] == "keep"
    assert win.decisions[third] == "remove"
    assert win.decisions[ids[0]] == "remove"
    assert win.fully_removed_sets() == []


def test_compare_keep_takes_over_a_kept_peer(review) -> None:
    """The dialog's 1/2 keep shortcut follows the card's take-over rule."""
    win, _repo, ids = review
    win.apply_decision(ids[0], "keep")
    dialog = gui_app.CompareDialog(win, start_pair=win.all_pairs[0])
    try:
        side = dialog._file_ids.index(ids[1])
        dialog._keep(side)

        assert win.decisions[ids[1]] == "keep"
        assert win.decisions[ids[0]] == "remove"
    finally:
        dialog.close()
        dialog.deleteLater()


def test_undoing_the_second_keep_clears_both(review) -> None:
    win, _repo, ids = review
    win.cards[ids[0]][0]._decide("keep")
    win.cards[ids[1]][0]._decide("keep")
    win.cards[ids[1]][0]._decide(None)

    assert not win.decisions.get(ids[0])
    assert not win.decisions.get(ids[1])


def test_legacy_all_removed_set_is_called_out_in_actions(review, tmp_path, monkeypatch) -> None:
    """Decisions written before the invariant are still guarded."""
    win, repo, ids = review
    repo.set_decision(win.scan_id, ids[0], "remove")
    repo.set_decision(win.scan_id, ids[1], "remove")
    repo.commit()
    win.reload_data()
    assert win.fully_removed_sets()
    # The fixture's inventory mtime is synthetic, so the real plan would skip
    # both files as changed; the banner is what this test is about
    plan = SimpleNamespace(
        removals=[
            SimpleNamespace(path=str(tmp_path / "v0.mp4"), size=1_000_000),
            SimpleNamespace(path=str(tmp_path / "v1.mp4"), size=2_000_000),
        ],
        total_bytes=3_000_000,
        skipped=[],
    )
    monkeypatch.setattr(gui_app.ActionService, "plan_removals", lambda self, scan_id: plan)

    dialog = gui_app.ActionsDialog(win)
    try:
        texts = " ".join(_labels(dialog))
        assert "every copy of 1 video" in texts
        assert "nothing would be left" in texts
    finally:
        dialog.close()
        dialog.deleteLater()


def test_undoing_the_removal_clears_the_auto_keep(review) -> None:
    win, _repo, ids = review
    card = win.cards[ids[1]][0]
    card._decide("remove")
    card._decide(None)
    assert not win.decisions.get(ids[1])
    assert not win.decisions.get(ids[0])


def test_a_second_click_on_keep_undoes_the_decision(review) -> None:
    win, _repo, ids = review
    card = win.cards[ids[0]][0]
    card.keep_button.click()
    assert win.decisions[ids[0]] == "keep"
    card.keep_button.click()
    assert not win.decisions.get(ids[0])
    assert not win.decisions.get(ids[1])


def test_a_second_click_on_remove_undoes_the_decision(review) -> None:
    win, _repo, ids = review
    card = win.cards[ids[1]][0]
    card.remove_button.click()
    assert win.decisions[ids[1]] == "remove"
    card.remove_button.click()
    assert not win.decisions.get(ids[1])
    assert not win.decisions.get(ids[0])


def test_the_card_has_no_undo_button(review) -> None:
    win, _repo, ids = review
    card = win.cards[ids[0]][0]
    button_texts = [b.text() for b in card.findChildren(QPushButton)]
    assert "Undo decision" not in button_texts


def test_a_double_click_does_not_cancel_the_decision(review) -> None:
    """The second press of a double-click is not a second click.

    It reached the button as another ``clicked``, and the card's own
    "second click undoes" rule cancelled the decision the reviewer had just
    made — a double-click on Keep left the pair untouched.
    """
    win, _repo, ids = review
    card = win.cards[ids[0]][0]

    # A real double-click is click + dblclick; QTest.mouseDClick sends only
    # the dblclick half, so both are needed to describe the bug
    QTest.mouseClick(card.keep_button, Qt.MouseButton.LeftButton)
    assert win.decisions[ids[0]] == "keep"
    QTest.mouseDClick(card.keep_button, Qt.MouseButton.LeftButton)

    assert win.decisions[ids[0]] == "keep"
    assert card.keep_button.isChecked()
    assert not card.remove_button.isChecked()


def test_compare_evidence_strip_names_itself(review) -> None:
    win, _repo, _ids = review
    dialog = gui_app.CompareDialog(win, start_pair=win.all_pairs[0])
    try:
        assert dialog.evidence_title.text() == "Why this pair"
        assert "38 of 48 sampled frames matched" in dialog.evidence_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_compare_empty_state_reports_the_run(review) -> None:
    win, _repo, _ids = review
    dialog = gui_app.CompareDialog(win, start_pair=win.all_pairs[0])
    dialog._ignore()
    assert dialog._stack.currentWidget() is dialog._empty
    assert "1 pair checked" in dialog._empty.body_label.text()
    buttons = [b.text() for b in dialog._empty.findChildren(QPushButton)]
    assert "Review ignored pairs" in buttons
    assert "Scan again" not in buttons


def test_compare_dialog_blurs_the_page_behind_it(review) -> None:
    win, _repo, _ids = review
    win.show()
    dialog = gui_app.CompareDialog(win, start_pair=win.all_pairs[0])
    try:
        dialog.show()
        assert dialog._backdrop is not None
        assert dialog._backdrop.isVisible()
        assert dialog._backdrop._pixmap is not None
        assert not dialog._backdrop._pixmap.isNull()
    finally:
        dialog.close()
        dialog.deleteLater()
    assert not dialog._backdrop.isVisible()


def test_backdrop_fills_the_device_area_not_the_logical_rect(qapp) -> None:
    """A scaled display paints into a device-sized store while the painter's
    transform stays unscaled; the snapshot must span the store, not its
    logical rect — else it covers a corner of the page."""
    window = QWidget()
    window.resize(80, 60)
    window.setStyleSheet("background: #ffffff;")
    window.show()
    backdrop = gui_widgets.BackdropBlur(window)
    backdrop.refresh()
    try:
        device = QSize(400, 300)
        canvas = QImage(device, QImage.Format.Format_RGB32)
        canvas.fill(QColor(0, 0, 0))
        painter = QPainter(canvas)
        backdrop._paint(painter, QSizeF(device))
        painter.end()
        for x, y in (
            (5, 5),
            (device.width() - 5, 5),
            (5, device.height() - 5),
            (device.width() - 5, device.height() - 5),
        ):
            assert canvas.pixelColor(x, y).red() > 40
    finally:
        backdrop.hide()
        window.close()


def test_undoing_a_removal_clears_the_peer_keep_it_worked_with(review) -> None:
    """The peer's keep is half of the pair's decision, even when it came first.

    Keeping one copy before marking the other for removal left the pair
    looking undecided after the second Mark for removal: the removal was
    released but the keep it paired with stayed, so only one of the two
    videos went back to the reviewer.
    """
    win, _repo, ids = review
    win.apply_decision(ids[1], "keep")
    card = win.cards[ids[0]][0]
    # The keep completed the pair, so the removal is the peer's half of it
    assert win.decisions[ids[0]] == "remove"
    assert win.decisions[ids[1]] == "keep"

    card.remove_button.click()  # a second click on the active button undoes
    assert not win.decisions.get(ids[0])
    assert not win.decisions.get(ids[1])


def test_undoing_a_keep_clears_the_peer_removal_it_worked_with(review) -> None:
    win, _repo, ids = review
    win.apply_decision(ids[1], "remove")
    card = win.cards[ids[0]][0]
    assert win.decisions[ids[0]] == "keep"
    assert win.decisions[ids[1]] == "remove"

    card.keep_button.click()  # a second click on the active button undoes
    assert not win.decisions.get(ids[0])
    assert not win.decisions.get(ids[1])

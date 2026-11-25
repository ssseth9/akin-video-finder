"""Two copies that rank equally get no keeper, and both cards say so.

The keeper key is a total order only when its elements differ. A pair whose
key ties element for element — or whose bytes are equal — gives the app no
basis to prefer one copy, so it labels both cards instead of naming a keeper:
the first key in iteration order would turn an accident of the peer list into
a recommendation.
"""

import os
import time
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402

IDENTICAL = "Identical"
SAME_EVIDENCE = "Same evidence"
KEEPER = "Recommended"


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _build(
    tmp_path, monkeypatch, classification: str, sizes: tuple[int, int]
) -> tuple[gui_app.MainWindow, DatabaseRepository, list[int]]:
    """A MainWindow over one reviewable pair, with contact sheets stubbed out."""
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)

    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))
    repo = DatabaseRepository(tmp_path / "v.db")
    scan_id = repo.create_scan("test")
    root_id = repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
    ids = []
    for i, size in enumerate(sizes):
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"x" * size)
        ids.append(
            repo.create_file(
                root_id,
                str(video),
                str(video).lower(),
                video.name,
                ".mp4",
                size,
                1000 + i,
                scan_id,
            )
        )
    repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", classification, 1.0, 0.01, 12)
    repo.commit()

    win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
    return win, repo, ids


@pytest.fixture()
def make_window(tmp_path, qapp, monkeypatch):
    """Factory for one window per test, torn down afterwards."""
    built: list[tuple[gui_app.MainWindow, DatabaseRepository]] = []

    def _make(classification: str = "EXACT", sizes: tuple[int, int] = (1_000_000, 1_000_000)):
        win, repo, ids = _build(tmp_path, monkeypatch, classification, sizes)
        built.append((win, repo))
        return win, ids

    yield _make
    for win, repo in built:
        win._sheet_sweep.stop()
        win._sheet_sweep_idle.stop()
        win.sheet_loader.watchdog.stop()
        win.close()
        win.deleteLater()
        repo.close()


def _cards(win, ids: list[int]) -> list[gui_app.FileCard]:
    """The Result-grid card for each file of the pair, in id order."""
    cards = []
    for fid in ids:
        cards.extend(win.cards.get(fid, []))
    assert len(cards) == 2
    return cards


def _label_texts(card: gui_app.FileCard) -> list[str]:
    return [label.text() for label in card.findChildren(QLabel)]


def test_exact_pair_labels_both_cards(make_window, qapp):
    win, ids = make_window("EXACT", (1_000_000, 1_000_000))
    _pump(qapp, 0.2)
    cards = _cards(win, ids)

    # Identical bytes: neither card claims the recommendation
    assert [c.is_keeper for c in cards] == [False, False]
    assert [c.tie_label for c in cards] == [IDENTICAL, IDENTICAL]
    for card in cards:
        texts = _label_texts(card)
        assert IDENTICAL in texts
        # The keeper row must not appear on either side of a tie
        assert not any(KEEPER in text for text in texts)


def test_tied_evidence_does_not_claim_identical(make_window, qapp):
    # A perceptual pair with equal metrics: the key ties, but the bytes are
    # not proven equal, so the label must not say "identical"
    win, ids = make_window("VERY_LIKELY", (1_000_000, 1_000_000))
    _pump(qapp, 0.2)
    cards = _cards(win, ids)

    assert [c.is_keeper for c in cards] == [False, False]
    assert [c.tie_label for c in cards] == [SAME_EVIDENCE, SAME_EVIDENCE]
    for card in cards:
        texts = _label_texts(card)
        assert SAME_EVIDENCE in texts
        assert not any("Identical" in text for text in texts)


def test_unequal_pair_still_names_one_keeper(make_window, qapp):
    # Regression guard: a pair the key can order keeps its badge, and only
    # the keeper's card carries it
    win, ids = make_window("VERY_LIKELY", (1, 999))
    _pump(qapp, 0.2)
    cards = _cards(win, ids)

    assert sorted(c.is_keeper for c in cards) == [False, True]
    assert all(c.tie_label is None for c in cards)
    keeper_texts = [t for card in cards if card.is_keeper for t in _label_texts(card)]
    assert any(KEEPER in text for text in keeper_texts)
    loser_texts = [t for card in cards if not card.is_keeper for t in _label_texts(card)]
    assert not any(KEEPER in text for text in loser_texts)


def test_undoing_a_keep_brings_the_keeper_recommendation_back(make_window, qapp):
    """Keeping the losing copy hides the keeper's badge — and undoing restores it.

    The keeper's card is repainted while the clicked card still carries its
    keep, so a repaint that read the pair as decided left the keeper looking
    unranked after the undo, as if the pair had never been ranked at all.
    """
    win, ids = make_window("VERY_LIKELY", (1, 999))
    _pump(qapp, 0.2)
    cards = _cards(win, ids)
    keeper = next(card for card in cards if card.is_keeper)
    loser = next(card for card in cards if not card.is_keeper)
    recommended = f"\u2605 {KEEPER}"
    assert keeper.recommended_badge.text() == recommended

    loser.keep_button.click()
    assert win.decisions[loser.file_row["id"]] == "keep"
    assert keeper.recommended_badge.text() == ""
    assert keeper.remove_badge.text() == "Remove"

    loser.keep_button.click()
    assert not win.decisions.get(loser.file_row["id"])
    assert not win.decisions.get(keeper.file_row["id"])
    assert keeper.remove_badge.text() == ""
    assert keeper.recommended_badge.text() == recommended


def test_compare_dialog_labels_both_sides(make_window, qapp):
    # The Compare window ranks through the same method, so it must not name
    # a keeper for an identical pair either
    win, _ids = make_window("EXACT", (1_000_000, 1_000_000))
    dialog = gui_app.CompareDialog(win)
    try:
        dialog.show()
        _pump(qapp, 0.4)
        cards = dialog._dialog_cards
        assert len(cards) == 2
        assert [c.is_keeper for c in cards] == [False, False]
        assert [c.tie_label for c in cards] == [IDENTICAL, IDENTICAL]
        for card in cards:
            assert IDENTICAL in _label_texts(card)
    finally:
        dialog.close()
        dialog.deleteLater()


def test_pair_keeper_returns_no_keeper_on_a_tie(make_window):
    win, ids = make_window("VERY_LIKELY", (1_000_000, 1_000_000))
    fa, fb = win.files[ids[0]], win.files[ids[1]]

    keeper_id, reasons, label = win._pair_keeper([(fa, None), (fb, None)], "VERY_LIKELY")
    assert keeper_id is None
    assert reasons == {}
    assert label == SAME_EVIDENCE


def test_pair_keeper_treats_exact_as_identical(make_window):
    # The bytes are equal, so the probes cannot make one copy preferable
    win, ids = make_window("EXACT", (1_000_000, 1_000_000))
    fa, fb = win.files[ids[0]], win.files[ids[1]]
    fa = dict(fa, size=1_000_000)
    fb = dict(fb, size=2_000_000)

    keeper_id, reasons, label = win._pair_keeper([(fa, None), (fb, None)], "EXACT")
    assert keeper_id is None
    assert reasons == {}
    assert label == IDENTICAL


def test_pair_keeper_orders_an_unequal_pair(make_window):
    win, ids = make_window("VERY_LIKELY", (1, 999))
    fa, fb = win.files[ids[0]], win.files[ids[1]]

    keeper_id, reasons, label = win._pair_keeper([(fa, None), (fb, None)], "VERY_LIKELY")
    assert keeper_id == ids[1]  # the larger copy, since size is a key element
    assert list(reasons) == [ids[0]]
    assert label is None


def test_reason_lines_become_the_card_tooltip(make_window, qapp):
    """The chips restated the metrics; the card keeps the lines on hover.

    A reason chip wrapped to two or three lines depending on the length of
    the numbers in it, which pushed that card's buttons below its pair's.
    The metric chips above already carry the comparison, so the lines moved
    to the tooltip — except "longer, but weaker quality", which the chips
    cannot state.
    """
    win, ids = make_window("VERY_LIKELY", (1_000_000, 2_000_000))
    fa = win.files[ids[0]]
    reasons = ["lower resolution (1280x720 vs 1920x1080)", "older codec (wmv3 vs h264)"]

    card = gui_app.FileCard(win, fa, None, [(fa, None)], False, reasons)
    try:
        texts = _label_texts(card)
        assert not any("lower resolution" in text for text in texts)
        assert card.toolTip() == "\n".join(reasons)
    finally:
        card.deleteLater()

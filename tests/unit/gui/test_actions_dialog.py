"""Actions for marked files: the empty state and the header/footer."""

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGroupBox,
    QLabel,
    QPushButton,
    QTableWidget,
)

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(gui_app, "generate_contact_sheet", lambda *a, **k: False)
    cfg = Config(roots=[RootConfig(path=str(tmp_path))])
    cfg = replace(cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports")))
    repo = DatabaseRepository(tmp_path / "v.db")
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
    repo.create_pair_comparison(scan_id, ids[0], ids[1], "v1", "POSSIBLE", 0.8, 0.1, 12)
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


def test_empty_actions_offers_the_way_back(window) -> None:
    win, _repo, _ids = window
    dialog = gui_app.ActionsDialog(win)
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "Nothing marked for removal" in texts
    buttons = [b.text() for b in dialog.findChildren(QPushButton)]
    assert "Back to Results" in buttons


def test_the_header_and_footer_use_the_marked_count(window, monkeypatch) -> None:
    win, _repo, _ids = window
    plan = SimpleNamespace(
        removals=[SimpleNamespace(path="C:/v1.mp4", size=1_000_000_000)],
        total_bytes=1_000_000_000,
        skipped=[],
    )
    monkeypatch.setattr(gui_app.ActionService, "plan_removals", lambda self, scan_id: plan)
    monkeypatch.setattr(gui_app.ActionService, "pending_quarantine", lambda self: [])
    monkeypatch.setattr(gui_app.ActionService, "pending_actions", lambda self: [])
    dialog = gui_app.ActionsDialog(win)
    texts = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "1 file totaling" in texts
    assert "marked for removal" in texts
    buttons = [b.text() for b in dialog.findChildren(QPushButton)]
    assert "Move 1 file…" in buttons
    assert "Delete 1 file…" in buttons
    assert buttons.index("Move 1 file…") < buttons.index("Delete 1 file…")
    by_text = {b.text(): b for b in dialog.findChildren(QPushButton)}
    assert by_text["Move 1 file…"].property("role") == "secondary"
    assert by_text["Delete 1 file…"].property("role") == "danger"
    assert "1 file totaling" in texts
    assert "1.00 GB" in texts
    table = dialog.findChild(QTableWidget)
    assert table is not None
    file_header = table.horizontalHeaderItem(0)
    assert file_header is not None
    assert file_header.textAlignment() & Qt.AlignmentFlag.AlignLeft
    assert "Choose a destination folder" not in texts
    assert "Sends files to the Recycle Bin" not in texts
    assert "still match the last scan" not in texts


def test_the_file_list_fills_the_dialog_even_when_short(window, monkeypatch) -> None:
    """A tall dialog gives its spare height to the list, not to empty page.

    The table was fitted to its rows and capped, so it floated above a large
    empty area inside the dialog whenever the window was taller than the list.
    """
    win, _repo, _ids = window
    plan = SimpleNamespace(
        removals=[SimpleNamespace(path="C:/v1.mp4", size=1_000_000_000)],
        total_bytes=1_000_000_000,
        skipped=[],
    )
    monkeypatch.setattr(gui_app.ActionService, "plan_removals", lambda self, scan_id: plan)
    monkeypatch.setattr(gui_app.ActionService, "pending_quarantine", lambda self: [])
    monkeypatch.setattr(gui_app.ActionService, "pending_actions", lambda self: [])
    dialog = gui_app.ActionsDialog(win)
    dialog.resize(1000, 900)
    dialog.show()
    QApplication.processEvents()
    table = dialog.findChild(QTableWidget)
    assert table is not None
    assert table.height() > 520


def test_a_long_file_list_scrolls_against_the_right_edge(window, monkeypatch) -> None:
    """No page strip between the list and its scrollbar.

    The body's right margin used to stay on the table, so the scrollbar was
    drawn 16 px in from the dialog edge with empty page beside it.
    """
    win, _repo, _ids = window
    plan = SimpleNamespace(
        removals=[
            SimpleNamespace(path=f"C:/v{i}.mp4", size=1_000_000_000) for i in range(40)
        ],
        total_bytes=40_000_000_000,
        skipped=[],
    )
    monkeypatch.setattr(gui_app.ActionService, "plan_removals", lambda self, scan_id: plan)
    monkeypatch.setattr(gui_app.ActionService, "pending_quarantine", lambda self: [])
    monkeypatch.setattr(gui_app.ActionService, "pending_actions", lambda self: [])
    dialog = gui_app.ActionsDialog(win)
    dialog.resize(1000, 900)
    dialog.show()
    QApplication.processEvents()
    table = dialog.findChild(QTableWidget)
    assert table is not None
    assert table.verticalScrollBar().isVisible()
    right = table.mapTo(dialog, table.rect().topRight()).x()
    assert right >= dialog.width() - 2


def test_a_skipped_file_keeps_its_reason_and_drops_the_all_match_line(window, monkeypatch):
    win, _repo, _ids = window
    plan = SimpleNamespace(
        removals=[SimpleNamespace(path="C:/v1.mp4", size=1_000_000_000)],
        total_bytes=1_000_000_000,
        skipped=[SimpleNamespace(reason="size changed since the scan", path="C:/v1.mp4")],
    )
    monkeypatch.setattr(gui_app.ActionService, "plan_removals", lambda self, scan_id: plan)
    monkeypatch.setattr(gui_app.ActionService, "pending_quarantine", lambda self: [])
    monkeypatch.setattr(gui_app.ActionService, "pending_actions", lambda self: [])
    dialog = gui_app.ActionsDialog(win)
    texts = " ".join(label.text() for label in dialog.findChildren(QLabel))
    group_titles = [box.title() for box in dialog.findChildren(QGroupBox)]
    assert "1 file skipped by the safety check" in group_titles
    assert "size changed since the scan" in texts
    assert "still match the last scan" not in texts


def test_rebuilding_to_the_empty_state_removes_the_old_body(window, monkeypatch):
    """The old body must leave the dialog when its plan empties.

    The old clear loop only deleted ``item.widget()``, so after Move/Delete
    the old title, notes and buttons stayed parented to the dialog and kept
    painting over the empty state.
    """
    win, _repo, _ids = window
    plan = SimpleNamespace(
        removals=[
            SimpleNamespace(path="C:/v0.mp4", size=1_000_000_000),
            SimpleNamespace(path="C:/v1.mp4", size=2_000_000_000),
        ],
        total_bytes=3_000_000_000,
        skipped=[],
    )
    monkeypatch.setattr(gui_app.ActionService, "plan_removals", lambda self, scan_id: plan)
    monkeypatch.setattr(gui_app.ActionService, "pending_quarantine", lambda self: [])
    monkeypatch.setattr(gui_app.ActionService, "pending_actions", lambda self: [])
    dialog = gui_app.ActionsDialog(win)
    assert any(
        label.text().startswith("2 files totaling") and "3.00 GB" in label.text()
        for label in dialog.findChildren(QLabel)
    )

    empty = SimpleNamespace(removals=[], total_bytes=0, skipped=[])
    monkeypatch.setattr(gui_app.ActionService, "plan_removals", lambda self, scan_id: empty)
    dialog._rebuild()

    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "Nothing marked for removal" in texts
    assert not any("still match the last scan" in text for text in texts)
    assert not any(text.startswith("2 files totaling") for text in texts)
    buttons = [b.text() for b in dialog.findChildren(QPushButton)]
    assert "Delete 2 files…" not in buttons
    assert dialog.close_button.accessibleName() == "Close actions"
    assert not dialog.close_button.isHidden()

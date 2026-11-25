"""The folder table must say what each row is.

Roots, excluded folders and name patterns shared one list where the only
distinguishers were a checkbox and grey italics, the row-number gutter ate
a column, and a pattern row displayed a permanently checked, disabled
"Exclude" box — a control that looked broken rather than not applicable.
The rows also gained a leading Kind column, which shifted every index; the
save path is what would silently mis-map them.
"""

import os
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.configuration.models import Config, RootConfig  # noqa: E402
from akin_video_finder.persistence.repository import DatabaseRepository  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path, qapp):
    created = []

    def _make(roots=None, excludes=()):
        cfg = Config(roots=[RootConfig(path=r) for r in (roots or [str(tmp_path)])])
        cfg = replace(cfg, exclude_patterns=list(excludes))
        cfg = replace(
            cfg, reporting=replace(cfg.reporting, output_directory=str(tmp_path / "reports"))
        )
        repo = DatabaseRepository(tmp_path / "v.db")
        win = gui_app.MainWindow(cfg, repo, tmp_path / "config.toml")
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


def _kinds(win) -> list[str]:
    return [
        win.folders_table.item(row, win._COL_KIND).text()
        for row in range(win.folders_table.rowCount())
    ]


class TestKindColumn:
    def test_root_row_is_a_scan_folder(self, window, tmp_path):
        win = window([str(tmp_path)])
        assert _kinds(win) == ["Scan folder"]
        assert win.folders_table.item(0, win._COL_PATH).text() == str(tmp_path)

    def test_excluded_row_is_marked_excluded(self, window, tmp_path):
        win = window(excludes=["C:/library/Excluded/*"])
        assert _kinds(win) == ["Scan folder", "Excluded"]

    def test_pattern_row_is_marked_pattern(self, window, tmp_path):
        win = window(excludes=["C:/library/Excluded/*", "_AI"])
        assert _kinds(win) == ["Scan folder", "Excluded", "Pattern"]
        assert win.folders_table.item(2, win._COL_PATH).text() == "_AI"

    def test_pattern_row_has_no_checkboxes(self, window, tmp_path):
        win = window(excludes=["_AI"])
        sub = win.folders_table.item(1, win._COL_SUB)
        skip = win.folders_table.item(1, win._COL_EXCL)
        # The cells are blank: a dash and "Always" repeated what the kind
        # and the tooltip already say
        assert sub.text() == ""
        assert skip.text() == ""
        assert not (sub.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        assert not (skip.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        # No subtitle under the chip, so the row is folder height
        assert win.folders_table.rowHeight(1) == 26

    def test_headers_use_the_review_language(self, window, tmp_path):
        win = window()
        headers = [
            win.folders_table.horizontalHeaderItem(col).text()
            for col in range(win.folders_table.columnCount())
        ]
        assert headers == ["Type", "Folder or pattern", "Subfolders", "Skip", ""]

    def test_the_header_is_slimmer_than_a_row(self, window, tmp_path):
        win = window()
        win.show()
        QApplication.processEvents()
        header = win.folders_table.horizontalHeader()
        assert header.height() < win.folders_table.rowHeight(0)

    def test_row_number_gutter_is_gone(self, window, tmp_path):
        win = window()
        assert win.folders_table.verticalHeader().isHidden()

    def test_rows_are_not_selectable(self, window, tmp_path):
        # A selected row painted a lighter band and recoloured the Type label.
        win = window()
        assert win.folders_table.selectionMode() == QAbstractItemView.SelectionMode.NoSelection

    def test_checkbox_clicks_do_not_select_the_row(self, window, tmp_path):
        # The checkbox owns the click; no row highlight follows it.
        win = window()
        win.show()
        QApplication.processEvents()
        table = win.folders_table
        item = table.item(0, win._COL_SUB)
        QTest.mouseClick(
            table.viewport(), Qt.MouseButton.LeftButton, pos=table.visualItemRect(item).center()
        )
        QApplication.processEvents()
        assert table.selectedIndexes() == []

    def test_cells_do_not_light_up_on_hover(self, window, tmp_path):
        # The Windows 11 style painted a blue hover panel behind cells whose
        # delegate falls through to the base paint; the item rules blank it.
        win = window()
        sheet = win.folders_table.styleSheet().replace(" ", "")
        assert (
            "QTableWidget::item,QTableWidget::item:hover,"
            "QTableWidget::item:selected{background:transparent;}" in sheet
        )


class TestFittedHeight:
    def test_the_body_ends_with_the_last_row(self, window, tmp_path):
        # Spare pixels under the last row made the body taller than the
        # check cells, so their top margin read smaller than the bottom one.
        win = window()
        win.show()
        QApplication.processEvents()
        table = win.folders_table
        rows = sum(table.rowHeight(r) for r in range(table.rowCount()))
        assert table.viewport().height() == rows
        assert table.height() == table.horizontalHeader().height() + rows + 2 * table.frameWidth()


class TestCheckboxCells:
    def test_the_box_has_equal_margins_in_the_cell(self):
        # QRect.center() truncates the half pixel: a 26 px cell centred the
        # 16 px box at y=4, leaving 4 above and 6 below.
        from PySide6.QtCore import QRect

        from akin_video_finder.gui.delegates import CheckDelegate

        box = CheckDelegate._box_for(QRect(0, 0, 81, 26))
        assert box.top() == 5.0
        assert 26 - box.bottom() == 5.0

    def test_checked_state_accepts_both_qt_spellings(self):
        # index.data() returns 2 for one item and CheckState.Checked for
        # another; comparing to only one made checked boxes paint unchecked.
        from akin_video_finder.gui.delegates import CheckDelegate

        assert CheckDelegate._checked(Qt.CheckState.Checked)
        assert CheckDelegate._checked(Qt.CheckState.Checked.value)
        assert not CheckDelegate._checked(Qt.CheckState.Unchecked)
        assert not CheckDelegate._checked(Qt.CheckState.Unchecked.value)

    def test_clicking_the_subfolders_box_toggles_it(self, window, tmp_path):
        # The themed delegate owns both painting and the click; the platform
        # item checkbox drew a black tick on the dark row.
        win = window()
        win.show()
        QApplication.processEvents()
        table = win.folders_table
        item = table.item(0, win._COL_SUB)
        assert item.checkState().value == Qt.CheckState.Checked.value
        rect = table.visualItemRect(item)
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        QApplication.processEvents()
        assert item.checkState().value == Qt.CheckState.Unchecked.value

    def test_clicking_skip_twice_marks_and_unmarks_the_folder(self, window, tmp_path):
        win = window()
        win.show()
        QApplication.processEvents()
        table = win.folders_table
        skip = table.item(0, win._COL_EXCL)
        sub = table.item(0, win._COL_SUB)
        QTest.mouseClick(
            table.viewport(), Qt.MouseButton.LeftButton, pos=table.visualItemRect(skip).center()
        )
        QApplication.processEvents()
        assert skip.checkState().value == Qt.CheckState.Checked.value
        assert sub.text() == ""
        assert not (sub.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        QTest.mouseClick(
            table.viewport(), Qt.MouseButton.LeftButton, pos=table.visualItemRect(skip).center()
        )
        QApplication.processEvents()
        assert skip.checkState().value == Qt.CheckState.Unchecked.value
        assert sub.text() == ""
        assert sub.flags() & Qt.ItemFlag.ItemIsUserCheckable

    def test_an_excluded_row_leaves_the_subfolders_cell_blank(self, window, tmp_path):
        win = window()
        win._append_folder_row("C:/library/Skip", excluded=True)
        win.show()
        QApplication.processEvents()
        table = win.folders_table
        item = table.item(1, win._COL_SUB)
        assert item.text() == ""
        assert item.data(Qt.ItemDataRole.CheckStateRole) is None
        assert not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        rect = table.visualItemRect(item)
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        QApplication.processEvents()
        assert item.text() == ""
        assert item.data(Qt.ItemDataRole.CheckStateRole) is None


class TestRowRemoval:
    def test_every_row_carries_its_own_remove_cell(self, window, tmp_path):
        win = window(excludes=["C:/library/Excluded/*", "_AI"])
        for row in range(win.folders_table.rowCount()):
            cell = win.folders_table.item(row, win._COL_DEL)
            assert cell is not None
            assert cell.data(Qt.ItemDataRole.AccessibleTextRole) == "Remove from list"
            assert cell.data(Qt.ItemDataRole.AccessibleDescriptionRole)
        excluded = str(Path("C:/library/Excluded"))
        assert win.folders_table.item(1, win._COL_DEL).toolTip() == (
            f"Remove {excluded} from the list"
        )

    def test_clicking_the_cross_removes_that_row(self, window, tmp_path):
        win = window()
        win._append_folder_row(str(tmp_path / "second"), excluded=False)
        win._save_settings(quiet=True)
        win.show()
        QApplication.processEvents()
        table = win.folders_table
        cell = table.item(1, win._COL_DEL)
        QTest.mouseClick(
            table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=table.visualItemRect(cell).center(),
        )
        QApplication.processEvents()
        assert table.rowCount() == 1
        assert [r.path for r in win.config.roots] == [str(tmp_path)]

    def test_delete_key_removes_the_last_clicked_row(self, window, tmp_path):
        win = window()
        win._append_folder_row(str(tmp_path / "second"), excluded=False)
        win._save_settings(quiet=True)
        win.show()
        win.activateWindow()
        QApplication.processEvents()
        table = win.folders_table
        # Selection is off, so the key uses the row clicked last
        QTest.mouseClick(
            table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=table.visualItemRect(table.item(1, win._COL_PATH)).center(),
        )
        table.setFocus()
        QApplication.processEvents()
        QTest.keyClick(table, Qt.Key.Key_Delete)
        QApplication.processEvents()
        assert table.rowCount() == 1
        assert [r.path for r in win.config.roots] == [str(tmp_path)]


class TestSaveMapping:
    def test_added_excluded_row_is_saved_as_a_pattern(self, window, tmp_path):
        win = window()
        win._append_folder_row("C:/library/Skip", excluded=True)

        win._save_settings(quiet=True)

        assert "C:/library/Skip/*" in win.config.exclude_patterns
        assert [r.path for r in win.config.roots] == [str(tmp_path)]

    def test_unticking_exclude_makes_the_row_a_root(self, window, tmp_path):
        win = window()
        win._append_folder_row("C:/library/Skip", excluded=True)
        # Same toggle the user clicks
        win.folders_table.item(1, win._COL_EXCL).setCheckState(Qt.CheckState.Unchecked)

        win._save_settings(quiet=True)

        assert str(Path("C:/library/Skip")) in [r.path for r in win.config.roots]
        assert "C:/library/Skip/*" not in win.config.exclude_patterns
        # The kind follows the box: the row is a scan folder again, not a
        # leftover "Excluded" label over a root
        assert win.folders_table.item(1, win._COL_KIND).text() == "Scan folder"

    def test_the_kind_label_follows_the_skip_box(self, window, tmp_path):
        win = window()
        kind = win.folders_table.item(0, win._COL_KIND)
        skip = win.folders_table.item(0, win._COL_EXCL)
        assert kind.text() == "Scan folder"

        skip.setCheckState(Qt.CheckState.Checked)

        assert kind.text() == "Excluded"
        assert "skipped" in kind.toolTip()

        skip.setCheckState(Qt.CheckState.Unchecked)

        assert kind.text() == "Scan folder"
        assert "indexed" in kind.toolTip()

    def test_pattern_rows_survive_a_save(self, window, tmp_path):
        win = window(excludes=["_AI", "*.tmp"])
        win._save_settings(quiet=True)
        assert win._non_dir_patterns == ["_AI", "*.tmp"]
        assert [r.path for r in win.config.roots] == [str(tmp_path)]


class TestDatabaseStatus:
    def test_reports_a_selected_root_that_was_never_scanned(self, window, tmp_path):
        win = window()
        win._update_db_status()
        text = win.db_status.text()
        assert "from 0 folders" in text
        assert "1 folder selected for the next scan" in text

    def test_agrees_when_scanned_and_selected_match(self, window, tmp_path):
        win = window()
        repo = win.repository
        repo.get_or_create_root(str(tmp_path), str(tmp_path).lower())
        repo.commit()

        win._update_db_status()

        text = win.db_status.text()
        assert "from 1 folder" in text
        assert "1 folder selected for the next scan" in text

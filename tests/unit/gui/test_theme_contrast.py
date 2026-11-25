"""The review's contrast table is a test, not a hope.

The first pass measured the match-percentage floor at 3.60:1 on dark and
3.07–3.41:1 on light, and several hints sat below 4.5:1. Every token that
carries body text is checked here against the surfaces it is painted on, in
both themes; the hue ramp is checked at its endpoints.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import akin_video_finder.gui.app as gui_app  # noqa: E402
from akin_video_finder.gui import theme  # noqa: E402
from akin_video_finder.gui.theme import TOKENS, contrast_ratio  # noqa: E402


@pytest.fixture()
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("theme_index", [0, 1])
def test_body_text_meets_aa_on_every_surface(theme_index: int) -> None:
    surfaces = ("page", "surface", "surface_raised", "surface_sunken")
    for name in ("text", "text_secondary", "text_muted"):
        foreground = TOKENS[name][theme_index]
        for surface_name in surfaces:
            background = TOKENS[surface_name][theme_index]
            assert contrast_ratio(foreground, background) >= 4.5, (
                name,
                surface_name,
                theme_index,
            )


@pytest.mark.parametrize("theme_index", [0, 1])
def test_log_timestamp_and_detail_meet_aa(theme_index: int) -> None:
    for name in ("log_stamp", "log_detail"):
        assert contrast_ratio(TOKENS[name][theme_index], TOKENS["page"][theme_index]) >= 4.5
        assert contrast_ratio(TOKENS[name][theme_index], TOKENS["surface"][theme_index]) >= 4.5


@pytest.mark.parametrize("theme_index", [0, 1])
def test_primary_button_text_meets_aa(theme_index: int) -> None:
    for background in ("accent", "accent_hover"):
        assert (
            contrast_ratio(
                TOKENS["accent_fg"][theme_index],
                TOKENS[background][theme_index],
            )
            >= 4.5
        )


def test_tab_labels_meet_aa(qapp) -> None:
    for theme_index in (0, 1):
        assert (
            contrast_ratio(
                TOKENS["tab_text"][theme_index],
                TOKENS["tab_strip"][theme_index],
            )
            >= 4.5
        )


@pytest.mark.parametrize("dark", [False, True])
def test_the_match_ramp_endpoints_are_readable(monkeypatch, qapp, dark: bool) -> None:
    monkeypatch.setattr(theme, "dark_theme", lambda: dark)
    floor = 0.50
    page = TOKENS["page"][1 if dark else 0]
    for fraction in (floor, 0.75, 1.0):
        color = gui_app._match_color(fraction, floor)
        assert contrast_ratio(color, page) >= 4.5, (fraction, dark, color)

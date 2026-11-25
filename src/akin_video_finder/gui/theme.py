"""Semantic colour, type and control tokens for the review GUI.

The GUI grew up around a dark palette, and light-theme support was bolted on
through ``_ui_color(light, dark)`` call sites.  Some surfaces kept literal dark
values, so a light palette produced dark chrome around light pages.  This module
is the single place where the light/dark pair for a semantic role lives.

The tokens are deliberately not derived from ``QPalette`` roles: several are
tints (warning washes, kept/remove edges, the log's path split) that the palette
does not carry, and the contrast of a tint is a design property, not a platform
one.  ``ensure_contrast`` is the one exception: the match-percentage hue ramp is
a continuous data scale, so its lightness is adjusted until it is readable on
the active page rather than pinned to a hand-picked value per theme.
"""

from __future__ import annotations

import contextlib

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QPushButton

# (light, dark) pairs.  Keep names semantic; components must not know a hex
# value exists.  Values are the review's foundation table, adjusted where a
# contrast check on the real surface required it.
TOKENS: dict[str, tuple[str, str]] = {
    "page": ("#f3f3f3", "#2d2d2d"),
    "surface": ("#ffffff", "#333333"),
    "surface_raised": ("#f7f8f9", "#3a3b3d"),
    "surface_sunken": ("#eceff2", "#26272a"),
    "overlay": ("rgba(0,0,0,0.04)", "rgba(255,255,255,0.05)"),
    "border": ("#d6dbe0", "#4a4d52"),
    "divider": ("#e1e5e9", "#3f4247"),
    "text": ("#1f2328", "#e6e8ea"),
    "text_secondary": ("#4a525b", "#c9d1d9"),
    "text_muted": ("#5c6470", "#aab2bc"),
    "text_faint": ("#6b737b", "#8b949e"),
    "accent": ("#0b6bcb", "#0b6bcb"),
    "accent_hover": ("#095aa8", "#095aa8"),
    "accent_fg": ("#ffffff", "#ffffff"),
    "accent_soft": ("rgba(11,107,203,0.10)", "rgba(79,193,255,0.12)"),
    "accent_text": ("#0b5cad", "#4fc1ff"),
    "accent_outline": ("rgba(11,107,203,0.28)", "rgba(79,193,255,0.28)"),
    "success": ("#1a7f37", "#56d364"),
    "success_text": ("#1a7f37", "#7ee2a0"),
    "success_outline": ("rgba(26,127,55,0.28)", "rgba(63,185,80,0.28)"),
    "success_fill": ("#3fb950", "#3fb950"),
    "success_soft": ("rgba(63,185,80,0.12)", "rgba(63,185,80,0.15)"),
    "warning": ("#9a6700", "#f2cc60"),
    "warning_fill": ("#e3b341", "#e3b341"),
    "warning_soft": ("rgba(227,179,65,0.16)", "rgba(227,179,65,0.13)"),
    "warning_outline": ("rgba(154,103,0,0.28)", "rgba(227,179,65,0.28)"),
    "danger": ("#b42318", "#ff9b94"),
    "danger_fill": ("#da3633", "#da3633"),
    "danger_soft": ("rgba(218,54,51,0.10)", "rgba(255,123,114,0.12)"),
    "info": ("#0969da", "#4fc1ff"),
    "info_soft": ("rgba(9,105,218,0.10)", "rgba(79,193,255,0.12)"),
    "info_outline": ("rgba(9,105,218,0.28)", "rgba(79,193,255,0.28)"),
    "focus": ("#0b6bcb", "#4fc1ff"),
    "sheet": ("#111111", "#111111"),
    "kept_bg": ("rgba(63,185,80,0.10)", "rgba(63,185,80,0.10)"),
    "kept_edge": ("#3fb950", "#3fb950"),
    "remove_bg": ("rgba(218,54,51,0.08)", "rgba(255,123,114,0.09)"),
    "remove_edge": ("#da3633", "#ff7b72"),
    "ignored_bg": ("rgba(128,128,128,0.08)", "rgba(255,255,255,0.04)"),
    "kind_scan": ("#0b5cad", "#9ccfff"),
    "kind_pattern": ("#6f42c1", "#d2a8ff"),
    "dropzone_border": ("#8a929c", "#8a929c"),
    "dropzone_text": ("#4a525b", "#d7dce1"),
    "progress_track": ("#e6e8ea", "#232323"),
    "progress_chunk": ("#0b6bcb", "#4fc1ff"),
    "log_stamp": ("#5b6470", "#aab2bc"),
    "log_detail": ("#4f5864", "#b8c0c8"),
    "log_muted": ("#6b737b", "#8b949e"),
    "log_text": ("#334155", "#cbd5e1"),
    "log_banner": ("#0f172a", "#f8fafc"),
    "log_metric": ("#047857", "#34d399"),
    "log_path_dir": ("#64748b", "#6e7787"),
    "log_path_file": ("#0f172a", "#e2e8f0"),
    "log_path_ext": ("#475569", "#94a3b8"),
    "log_warn": ("#8a5a00", "#f2cc60"),
    "log_error": ("#a11b1b", "#ff9b94"),
    "badge_neutral_bg": ("rgba(128,128,128,0.12)", "rgba(255,255,255,0.07)"),
    "badge_neutral_text": ("#4a525b", "#c9d1d9"),
    "badge_neutral_outline": ("rgba(128,128,128,0.28)", "rgba(255,255,255,0.18)"),
    "badge_fill_outline": ("rgba(0,0,0,0.18)", "rgba(0,0,0,0.28)"),
    "tab_strip": ("#f3f3f3", "#2d2d2d"),
    "tab_selected": ("#ffffff", "#3a3b3d"),
    "tab_text": ("#4a525b", "#aab2bc"),
}

# Point sizes are not used: the review's scale is in pixels.  Qt's QSS accepts
# integer px sizes only, so the 11.5px hint row from the mockup rounds to 11.
TYPE = {
    "page": 20,
    "section": 15,
    "card": 13,
    "body": 12,
    "hint": 11,
    "chip": 10,
}

SPACING = 4
RADIUS = 8


def dark_theme() -> bool:
    """Whether the active application palette is dark."""
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128  # type: ignore[attr-defined,no-any-return]


def ui_color(light: str, dark: str) -> str:
    """Pick the light or dark value for the active theme."""
    return dark if dark_theme() else light


def token(name: str) -> str:
    """Resolve a semantic token for the active theme."""
    try:
        light, dark = TOKENS[name]
    except KeyError as exc:  # pragma: no cover - a programmer error
        raise KeyError(f"unknown theme token: {name}") from exc
    return ui_color(light, dark)


def _rgb(color: QColor) -> tuple[float, float, float]:
    return color.redF(), color.greenF(), color.blueF()


def _channel_luminance(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(color: QColor | str) -> float:
    """WCAG relative luminance of a colour."""
    c = QColor(color) if isinstance(color, str) else color
    if not c.isValid():
        raise ValueError(f"invalid colour: {color!r}")
    r, g, b = _rgb(c)
    return (
        0.2126 * _channel_luminance(r)
        + 0.7152 * _channel_luminance(g)
        + 0.0722 * _channel_luminance(b)
    )


def contrast_ratio(foreground: QColor | str, background: QColor | str) -> float:
    """WCAG 2.x contrast ratio between two opaque colours."""
    fg = relative_luminance(foreground)
    bg = relative_luminance(background)
    lighter, darker = max(fg, bg), min(fg, bg)
    return (lighter + 0.05) / (darker + 0.05)


def ensure_contrast(color: QColor, background: QColor, target: float = 4.5) -> QColor:
    """Return a hue-preserving colour with at least ``target`` contrast.

    The search is deliberately small and deterministic: the HSL lightness is
    moved in 1% steps in whichever direction raises contrast, keeping hue and
    saturation.  A colour that already passes is returned unchanged, so a hue
    ramp does not drift in the middle of its range.
    """
    if contrast_ratio(color, background) >= target:
        return color
    hue, saturation, lightness = color.hslHueF(), color.hslSaturationF(), color.lightnessF()
    if hue < 0:
        hue = 0.0
    bg_luminance = relative_luminance(background)
    # On a dark surface lighten; on a light surface darken.  Trying both
    # directions costs little and covers mid-grey surfaces correctly.
    directions = [1.0, -1.0] if bg_luminance < 0.5 else [-1.0, 1.0]
    best_ratio = contrast_ratio(color, background)
    for direction in directions:
        for step in range(1, 101):
            value = lightness + direction * step / 100.0
            if value < 0.0 or value > 1.0:
                continue
            candidate = QColor.fromHslF(hue, saturation, value)
            ratio = contrast_ratio(candidate, background)
            if ratio > best_ratio:
                best_ratio = ratio
            if ratio >= target:
                return candidate
    # White/black are the only guaranteed endpoints for a pathological
    # background; keeping the hue cannot help once both directions are spent.
    return QColor("#ffffff") if bg_luminance < 0.5 else QColor("#000000")


def font_px(size: int, weight: int = 400, family: str | None = None) -> QFont:
    """A font at the review's pixel scale with the UI family and weight."""
    font = QFont(family or "Segoe UI Variable Text")
    families = [family] if family else ["Segoe UI Variable Text", "Segoe UI"]
    with_families = getattr(font, "setFamilies", None)
    if with_families is not None:
        with_families(families)
    font.setPixelSize(size)
    weights = {
        400: QFont.Weight.Normal,
        500: QFont.Weight.Medium,
        600: QFont.Weight.DemiBold,
        700: QFont.Weight.Bold,
    }
    font.setWeight(weights.get(weight, QFont.Weight.Normal))
    return font


def tabular_font(font: QFont) -> QFont:
    """Request tabular figures; silently no-op on a Qt/font without the feature."""
    with contextlib.suppress(AttributeError, TypeError, ValueError):
        font.setFeature(QFont.Tag("tnum"), 1)
    return font


def button_style(role: str) -> str:
    """QSS for one button role, including a visible keyboard focus ring."""
    text = token("text")
    secondary = token("text_secondary")
    border = token("border")
    accent = token("accent")
    accent_hover = token("accent_hover")
    accent_fg = token("accent_fg")
    raised = token("surface_raised")
    danger = token("danger")
    danger_soft = token("danger_soft")
    focus = token("focus")
    base = "border-radius:6px;padding:6px 14px;"
    if role == "primary":
        return (
            f"QPushButton{{{base}background:{accent};color:{accent_fg};"
            f"border:1px solid {accent};font-weight:600;}}"
            f"QPushButton:hover{{background:{accent_hover};border-color:{accent_hover};}}"
            f"QPushButton:pressed{{background:#074a8c;border-color:#074a8c;}}"
            f"QPushButton:focus{{border:2px solid {focus};}}"
            f"QPushButton:disabled{{background:{border};color:{secondary};border-color:{border};}}"
        )
    if role == "secondary":
        return (
            f"QPushButton{{{base}background:{raised};color:{text};border:1px solid {border};}}"
            f"QPushButton:hover{{background:{token('surface')};border-color:{accent};}}"
            f"QPushButton:focus{{border:2px solid {focus};}}"
            f"QPushButton:disabled{{color:{token('text_faint')};}}"
        )
    if role == "danger":
        return (
            f"QPushButton{{{base}background:transparent;color:{danger};"
            f"border:1px solid {danger};font-weight:600;}}"
            f"QPushButton:hover{{background:{danger_soft};}}"
            f"QPushButton:focus{{border:2px solid {focus};}}"
            f"QPushButton:disabled{{color:{token('text_faint')};border-color:{border};}}"
        )
    if role == "link":
        return (
            f"QPushButton{{background:transparent;color:{accent};border:none;"
            f"padding:2px 4px;text-decoration:underline;}}"
            f"QPushButton:hover{{color:{accent_hover};}}"
            f"QPushButton:focus{{border:2px solid {focus};border-radius:4px;}}"
        )
    if role == "danger_ghost":
        return (
            f"QPushButton{{{base}background:transparent;color:{danger};"
            f"border:1px solid transparent;}}"
            f"QPushButton:hover{{background:{danger_soft};border-color:{danger};}}"
            f"QPushButton:focus{{border:2px solid {focus};}}"
            f"QPushButton:disabled{{color:{token('text_faint')};}}"
        )
    # ghost
    return (
        f"QPushButton{{{base}background:transparent;color:{secondary};"
        f"border:1px solid transparent;}}"
        f"QPushButton:hover{{background:{raised};color:{text};}}"
        f"QPushButton:focus{{border:2px solid {focus};}}"
        f"QPushButton:disabled{{color:{token('text_faint')};}}"
    )


def apply_button_role(button: QPushButton, role: str) -> None:
    """Style a button by semantic role; unknown roles fall back to ghost."""
    button.setStyleSheet(button_style(role))
    button.setProperty("role", role)


def badge_style(kind: str) -> str:
    """QSS for a status/class badge label."""
    base = "border-radius:3px;padding:1px 7px;font-size:10px;font-weight:600;"
    if kind == "recommended":
        return (
            f"QLabel{{{base}background:transparent;"
            f"color:{token('success_text')};border:1px solid {token('success_outline')};}}"
        )
    if kind == "kept":
        return (
            f"QLabel{{{base}background:{token('success_fill')};color:#052e16;"
            f"border:1px solid {token('badge_fill_outline')};}}"
        )
    if kind == "remove":
        return (
            f"QLabel{{{base}background:{token('danger_fill')};color:#ffffff;"
            f"border:1px solid {token('badge_fill_outline')};}}"
        )
    if kind == "warning":
        return (
            f"QLabel{{{base}background:{token('warning_soft')};"
            f"color:{token('warning')};border:1px solid {token('warning_outline')};}}"
        )
    if kind == "info":
        return (
            f"QLabel{{{base}background:{token('info_soft')};"
            f"color:{token('info')};border:1px solid {token('info_outline')};}}"
        )
    if kind == "accent":
        return (
            f"QLabel{{{base}background:{token('accent_soft')};"
            f"color:{token('accent_text')};border:1px solid {token('accent_outline')};}}"
        )
    return (
        f"QLabel{{{base}background:{token('badge_neutral_bg')};"
        f"color:{token('badge_neutral_text')};border:1px solid {token('badge_neutral_outline')};}}"
    )

"""PySide6 review GUI for Akin Video Finder.

Replaces the HTML review report: browses exact groups, perceptual
groups, and ungrouped review pairs with cached contact-sheet images,
keeper analysis, peer-relative metric coloring, and keep/remove
decision controls persisted to the review_decisions table.
"""

import contextlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, override

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFontDatabase,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPalette,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from akin_video_finder import __version__
from akin_video_finder.application.actions import ActionService, pending_remove_ids
from akin_video_finder.application.analysis import (
    _VIDEO_CODEC_RANK,
    _describe,
    _member_key,
)
from akin_video_finder.application.exact_match import CLASS_EXACT
from akin_video_finder.application.maintenance import CleanupService
from akin_video_finder.application.scan_service import ScanService, fingerprint_matches
from akin_video_finder.configuration.models import ActionsConfig, Config, MatchingConfig
from akin_video_finder.core.models import ProgressEvent, ScanStatus
from akin_video_finder.core.path_utils import get_path_key
from akin_video_finder.gui import theme as ui_theme
from akin_video_finder.gui.delegates import (
    ActionLinkDelegate,
    BadgeDelegate,
    CheckDelegate,
    DeleteCellDelegate,
    PatternPathDelegate,
    TwoLineDelegate,
)
from akin_video_finder.gui.icons import icon as ui_icon
from akin_video_finder.gui.scan_sound import play_scan_complete
from akin_video_finder.gui.theme import (
    apply_button_role,
    button_style,
    font_px,
    tabular_font,
    token,
)
from akin_video_finder.gui.widgets import (
    BackdropBlur,
    Badge,
    Card,
    CountChip,
    EmptyState,
    Keycap,
    KeyedButton,
    PageHeader,
    WrappedLabel,
)
from akin_video_finder.infrastructure.console_signals import ignore_ctrl_c
from akin_video_finder.persistence.repository import DatabaseRepository
from akin_video_finder.reporting.sheets import generate_contact_sheet, sheet_name

logger = logging.getLogger(__name__)

# Contact sheets render at 1280x540 (4x3 grid of 320x180 tiles);
# cards expand to fill the row but never upscale beyond native size
SHEET_ASPECT = 540 / 1280
SHEET_NATIVE_WIDTH = 1280
# Cap the image's fixed height — a transient huge card width during
# layout settling must not lock in a giant empty image area
SHEET_MAX_HEIGHT = 480

# A FileCard's maximum width: the sheet plus its margins and border. Two
# of them, the Compare panels' gap and the dialog's own side margins are
# what "both sheets side by side at native size" needs from the monitor.
CARD_MAX_WIDTH = SHEET_NATIVE_WIDTH + 20
_COMPARE_PAIR_WIDTH = 2 * CARD_MAX_WIDTH + 12 + 16

# Similarity-strictness slider endpoints (0 = relaxed, 100 = strict);
# each MatchingConfig threshold is interpolated linearly between them.
# The midpoint lands on the MatchingConfig defaults.
_STRICTNESS_LOOSE: dict[str, Any] = {
    "frame_match_max_distance": 0.15,
    "very_likely_threshold": 0.85,
    "very_likely_mean_distance": 0.12,
    "possible_threshold": 0.55,
    "possible_min_matched_frames": 4,
    "signature_min_shared_frames": 1,
    "signature_max_hamming": 14,
}
_STRICTNESS_TIGHT: dict[str, Any] = {
    "frame_match_max_distance": 0.06,
    "very_likely_threshold": 0.95,
    "very_likely_mean_distance": 0.05,
    "possible_threshold": 0.85,
    "possible_min_matched_frames": 10,
    "signature_min_shared_frames": 3,
    "signature_max_hamming": 6,
}

# Human wording for each classification. The pair header shows this as plain
# text; the status dot and the match chip take their hue from the match
# fraction instead, so a 100 % pair is green whichever named band it is in.
_CLASS_LABELS: dict[str, str] = {
    "EXACT": "Exact copy",
    "VERY_LIKELY": "Very likely duplicate",
    "POSSIBLE": "Possible duplicate",
}

# Instance attribute types (set in __init__ / setup_ui)
_result_layout: QVBoxLayout | None = None
_pair_boxes: list[Any] = []
duration_slider: QSlider | None = None
strictness_slider: QSlider | None = None


def _strictness_fields(value: int) -> dict[str, Any]:
    """Interpolate the matching thresholds for a 0-100 strictness value.

    Piecewise, anchored on the MatchingConfig defaults at 50: the two ends
    keep the measured loose/tight values, and returning the slider to the
    middle reproduces the untouched defaults. A straight line between the
    ends did not — its midpoint was 0.105 / 0.085 / 7 frames where the
    defaults are 0.10 / 0.08 / 6, so merely visiting the slider rewrote the
    thresholds the first scan ran with.
    """
    t = value / 100.0
    middle = 0.5
    fields: dict[str, Any] = {}
    defaults = MatchingConfig()
    for key, lo in _STRICTNESS_LOOSE.items():
        hi = _STRICTNESS_TIGHT[key]
        mid = getattr(defaults, key)
        if t <= middle:
            v = lo + (t / middle) * (mid - lo)
        else:
            v = mid + ((t - middle) / (1.0 - middle)) * (hi - mid)
        fields[key] = round(v) if isinstance(lo, int) else round(v, 3)
    return fields


def _dark_theme() -> bool:
    return ui_theme.dark_theme()


def _ui_color(light: str, dark: str) -> str:
    """Pick a text color readable on the active theme."""
    return ui_theme.ui_color(light, dark)


def _with_alpha(color: str, alpha: float) -> str:
    """The same RGB with a QSS alpha, for a wash or an outline."""
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.2f})"


def _match_color(fraction: float, floor: float) -> str:
    """Hue for a match percentage: amber at the review floor, green at 100 %.

    The percentage text rendered in one grey, so an 83 % pair and a 100 %
    pair looked alike in a list sorted by exactly that number. Hue carries the
    value instead — the active theme's warning amber at the lowest score a
    review shows, its success green at a certain one — so the eye sorts the
    page before it reads a single figure. The pair's class badge stays on the
    semantic palette; only this number is a continuous scale.

    The ramp deliberately stops at amber rather than continuing into red.
    A low confidence is weak evidence, not an error: red is the state palette's
    danger colour, and a page of possible duplicates must not look like a page
    of failures.  The hue is taken from the theme's own warning and success
    endpoints so "amber" and "green" keep meaning the same thing here as they
    do everywhere else.

    The ramp is a data scale, not a state colour, so it is exempt from the
    semantic palette's red=danger rule.  Its lightness is clamped against the
    page surface because the review measured the old floor hue at 3.60:1 dark
    and 3.07–3.41:1 light; contrast is a readability property, not a design
    taste.
    """
    span = max(1e-6, 1.0 - floor)
    t = min(1.0, max(0.0, (fraction - floor) / span))
    amber_hue = QColor(token("warning")).hslHueF()
    green_hue = QColor(token("success")).hslHueF()
    hue = (amber_hue + (green_hue - amber_hue) * t) % 1.0
    raw = QColor.fromHslF(hue, 0.70, 0.60 if _dark_theme() else 0.35)
    page = QColor(token("page"))
    return ui_theme.ensure_contrast(raw, page, 4.5).name()


def _level_color(level: str) -> str:
    """Text colour for a log level, resolved to one concrete value.

    ``_LOG_STYLES`` uses ``None`` for "leave the palette alone", which is
    what most levels want — the row is then built without a colour at all.
    The branch that does need a colour has to turn that ``None`` into
    something, so this falls back to the default text colour.
    """
    light, dark = _LOG_STYLES[level][:2]
    default_light, default_dark = _LOG_MESSAGE_COLORS["text"]
    return _ui_color(light or default_light, dark or default_dark)


def _count(n: int, singular: str) -> str:
    """Format a count with its unit: "1 pair", "2 pairs"."""
    return f"{n:,} {singular if n == 1 else singular + 's'}"


def _savings_text(minimum: int, maximum: int) -> str:
    """The recoverable figure, a range unless certainty and estimate agree.

    The high end treats every shown pair as a duplicate; the low end counts
    only the 100 % ones. A single number read as a promise even when the page
    held pairs scored in the fifties, and a reviewer deciding whether the
    cleanup is worth starting deserves to see how much of it rests on the weak
    candidates.

    A zero low end is the one case where the range is not useful: "~0.0–14.7"
    reads like a broken estimate.  It renders as "Up to 14.7" because there is
    no certain floor to hide; a non-zero range keeps both numbers and spells
    the interval out ("From 1.0 to 2.0").
    """
    if maximum <= 0:
        return ""
    low = _size_text(minimum)
    high = _size_text(maximum)
    if minimum <= 0:
        return f"Up to {high} recoverable"
    if low == high:
        return f"~{high} recoverable"
    # Both ends usually share a unit; state it once — "From 1.0 to 2.0 GB"
    low_number, _, low_unit = low.rpartition(" ")
    if low_unit and low.endswith(low_unit) and high.endswith(f" {low_unit}"):
        return f"From {low_number} to {high} recoverable"
    return f"From {low} to {high} recoverable"


def _rate_quantity(value: float, unit: str, suffix: str) -> str:
    """A quantity with its unit, pluralised: "45 files/s", "1 pair/min"."""
    shown = f"{value:,.0f}" if value >= 10 else f"{value:.1f}".removesuffix(".0")
    return f"{shown} {unit if shown == '1' else unit + 's'}{suffix}"


def _rate_phrase(rate: float, unit: str) -> str:
    """A rate with its unit, in the unit that reads best.

    A fast phase reads per second — "45 files/s", "38,955 pairs/s". A slow
    one reads per minute, because "0.1 pairs/s" is a fraction you have to
    convert before you can tell whether it is moving or stuck, while "6
    pairs/min" is the same number and says six copies a minute.
    """
    if rate >= 1.0:
        return _rate_quantity(rate, unit, "/s")
    return _rate_quantity(rate * 60.0, unit, "/min")


def _rate_text(rate: float, unit: str) -> str:
    """The rate phrase, or "" when it would read as "0 units/min".

    A stage slower than one unit every twenty minutes gets no rate at all:
    a zero in the progress bar reads as a scan that has stopped, when what
    it means is that this phase is slow.
    """
    return _rate_phrase(rate, unit) if rate * 60.0 >= _MIN_RATE_PER_MINUTE else ""


def _join_and(parts: list[str]) -> str:
    """Join phrases the way prose does: "a", "a and b", "a, b and c"."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _eta_text(seconds: float) -> str:
    """The rest of a phase, roughly: "~38 s left", "~1 h 33 min left".

    Coarse on purpose. It comes from the same average rate as the bar, over
    work whose cost per unit moves by an order of magnitude — a cached
    fingerprint against a cold one, a pair of adjacent checksums against a
    pair that needs a decode — so a to-the-second figure would be wrong and
    would jitter on every event. Minutes and hours are the units a person
    plans a scan with, and a number that only moves when the work really
    changed its pace is worth more than one that moves constantly.
    """
    if seconds < 45:
        return f"~{max(1, round(seconds))} s left"
    minutes = seconds / 60
    if minutes < 90:
        return f"~{round(minutes)} min left"
    hours = minutes / 60
    if hours < 36:
        whole = int(hours)
        rest = round(minutes - whole * 60)
        if rest == 60:  # 1.999 h would read "~1 h 60 min left"
            whole, rest = whole + 1, 0
        return f"~{whole} h left" if rest == 0 else f"~{whole} h {rest} min left"
    return f"~{round(hours / 24)} days left"


def _phase_stalled(state: dict[str, Any], now: float) -> bool:
    """Has the phase's counter stood still longer than its pace can explain?

    ``max_interval`` is the largest gap seen between counter moves, not the
    last one: a worker pool finishes files in bursts, so the gap inside a
    burst is near zero while the gap between bursts is the real wait. A fast
    phase therefore falls silent after ten seconds, while a phase made of
    five-minute units is allowed ten minutes before the rate and the estimate
    are withdrawn.
    """
    idle = now - state["last_progress_at"]
    return idle > max(_STALL_MIN_SECONDS, _STALL_INTERVAL_FACTOR * state["max_interval"])


# ----- scan log ---------------------------------------------------------
#
# The log used to be raw text: one line per progress event, everything the
# same dim colour, capped at 5,000 blocks. A 6,000-file scan emits tens of
# thousands of per-file lines, so the cap silently ate the *start* of the
# log — the phase headers and early milestones — leaving a wall of paths
# with no shape. Records are now kept intact and rendered through filters —
# all of them by default; unticking Files drops the per-file rows when the
# wall of paths is more than you want.

_LOG_STAMP_FORMAT = "%H:%M:%S.%f"  # trimmed to milliseconds on insert
_LOG_GAP = "  "  # between the timestamp and the line, in the copied text
_LOG_PHASE_WIDTH = 22  # one past "Confirming lookalikes" — the longest label
# Shown by default — the timeline gives each one the rail mark and the
# highlighted path of its phase. 50,000 covers a couple of full scans before
# the oldest rows start dropping.
_SCAN_LOG_MAX_RECORDS = 50_000
# Rows the widget renders. Qt's block cap drops the oldest blocks, so this
# is what the footer has to own up to; 20,000 covers a couple of long scans
_SCAN_LOG_MAX_ROWS = 20_000
# The rate is the phase's average from its first event to its last counter
# move, not a trailing window. A unit here can take minutes — a 4K
# fingerprint, a shift probe — and a ten-second window measured one such unit
# as "one per ten seconds" right after it finished, then fell to zero while
# the next one ran: the remaining time swung by a factor of four on the
# reference library. Stopping at the last move also keeps a worker pool's
# bursts out of the denominator, so four files finishing together is four
# files over the time they took, not a four-file spike. The baseline is the
# first event's count, because the counter opens at the cache's answer —
# 2,004 of 2,013 fingerprints were already on disk — and that head start is
# not speed.
_STALL_MIN_SECONDS = 10.0
# A phase whose counter has not moved for this many of the largest gaps seen
# between its counter moves is stalled. The average rate would otherwise keep
# promising a finish from work that is not happening; the rate and the
# estimate drop out until it moves. The largest gap, not the last one, is
# what a worker pool's burst pattern needs: files that finish together leave
# near-zero gaps inside the burst and the real wait between bursts.
_STALL_INTERVAL_FACTOR = 2.0
# Below one unit every twenty minutes the per-minute reading would round to
# "0 units/min", which reads as a stuck scan rather than a slow one
_MIN_RATE_PER_MINUTE = 0.05
# The remaining-time reading waits for this much measured work. One event's
# average is noise — the first pair of a phase can be a 40 ms checksum or a
# nine-second decode — and an estimate that swings from "~2 s left" to
# "~4 h left" inside a second is worse than no estimate
_ETA_MIN_SECONDS = 3.0
# How often the bar's file name may change. A fast phase emits one event per
# file — 200 a second while fingerprinting — and a name repainted at that rate
# is an unreadable blur that costs a repaint each time. A sixth of a second
# changes faster than a name can be read and slowly enough to stay cheap
_CURRENT_NAME_SECONDS = 0.15
# How often the bar's reading is recomputed while a phase sits on one unit.
# Progress events arrive when a unit finishes; a 4K fingerprint or a shift
# probe can run for minutes, and the reading has to age with the clock rather
# than freeze at the last fast units' "~2 s left"
_BAR_REFRESH_SECONDS = 0.5
# Pseudo-phases: real progress events, but nothing worth a header row
_LOG_SKIP_PHASES = frozenset({"initialization", "completed"})
# What the scanner calls each phase, in the words the log shows. The names
# in the events are the pipeline's own vocabulary — signing, fingerprinting,
# blocking — which describe the machinery rather than what it is doing; a
# user reading the log watches videos being found, read and compared.
_PHASE_LABELS: dict[str, str] = {
    "discovery": "Finding videos",
    "probing": "Reading details",
    "hashing": "Finding exact copies",
    "signing": "Reading video frames",
    "blocking": "Finding lookalikes",
    "verifying": "Confirming lookalikes",
    "offset": "Checking for shifted copies",
    "fingerprinting": "Reading more frames",
    "audio": "Comparing audio",
    "comparing": "Comparing videos",
}


def _phase_label(phase: str) -> str:
    """A phase's name for humans; unknown phases keep a tidied raw name."""
    return _PHASE_LABELS.get(phase, phase.replace("_", " ").capitalize())


def _phase_heading(phase: str) -> str:
    """A phase heading: sentence case, the bold row names the phase once.

    The heading used to be the stage's ALL-CAPS code. A reader had to learn
    that DETAILS meant "Reading details" and EXACT meant "Finding exact
    copies"; the phase name belongs here, and the rail's node carries the
    stage without a second vocabulary.
    """
    return _phase_label(phase)


def _phase_column(phase: str) -> str:
    """The phase label padded to the result column.

    A label longer than the column keeps its own length and gains one space:
    "Checking for shifted copies" is four characters past
    ``_LOG_PHASE_WIDTH``, and without that space its count ran straight into
    it — "Checking for shifted copies40 pairs" is not a sentence anyone can
    read, and the splitter cannot tell the two apart either.
    """
    label = _phase_label(phase)
    padded = f"{label:<{_LOG_PHASE_WIDTH}}"
    return padded if len(label) < _LOG_PHASE_WIDTH else padded + " "


def _bar_message(message: str, root: str | None) -> str:
    """A progress message for the bar, with a root shown by its folder name.

    Discovery is the one message that carries a path, and the bar is where a
    path costs more than it gives: its format string elides, so
    ``P:\\_portable\\_AI\\vids`` arrives without the folders that tell two roots
    apart while the volume survives — and the bar already sits under a scan
    running on known roots. The line below keeps names for the same reason
    (``context/scan-progress.md``). The scan log keeps the whole message: it is
    the record, and its rows carry paths too.

    ``root`` is the structured source of the path, so the message is never
    parsed to find it. A drive root has no folder name to fold to, and folding
    it would leave nothing behind, so the message stays whole.
    """
    root = root or ""
    folder = Path(root).name
    if not folder or folder == root or root not in message:
        return message
    return message.replace(root, folder)


_SCAN_STARTED_ROW = "Scan started"


# What each phase counts, for its "6,449 files in 25.4 s" row
_PHASE_UNITS: dict[str, str] = {
    "discovery": "file",
    "probing": "file",
    "hashing": "file",
    "signing": "file",
    "fingerprinting": "file",
    "blocking": "pair",
    "verifying": "pair",
    "offset": "pair",
    "comparing": "pair",
    "audio": "window",
}
# Colour and weight per level: (light theme, dark theme, bold). None keeps
# the palette's own text colour, so ordinary lines match the rest of the UI.
_LOG_STYLES: dict[str, tuple[str | None, str | None, bool]] = {
    "detail": (*ui_theme.TOKENS["log_detail"], False),
    "info": (None, None, False),
    # The banner is bold but not blue: the rail's node carries the stage, and
    # a second colour beside it put two colours on the row that says the least.
    # warn and error keep theirs — there the colour is the message.
    "heading": (None, None, True),
    "phase": (None, None, False),
    "summary": (None, None, False),
    "warn": (*ui_theme.TOKENS["log_warn"], True),
    "error": (*ui_theme.TOKENS["log_error"], False),
}
_LOG_STAMP_COLOR = ui_theme.TOKENS["log_stamp"]
# The grey the uncoloured rows borrow for their connector and their text.
_LOG_MUTED = ui_theme.TOKENS["log_muted"]
# The event column's leading mark per level: a stage opens with a node and
# everything under it branches off the same line, so a stage reads as a
# branch of the timeline rather than as a run of unrelated lines. A failure
# keeps the branch mark too — the red text is the signal, and a `!` column
# down the page read as a second, competing one. One cell wide in the
# platform's fixed font — the mockup's closing corner "⎿" advances 13 px
# against 8 and shears the column, so "├" stands in.
_LOG_EVENT_MARKS: dict[str, str] = {
    "heading": "●",
    "detail": "├",
    "phase": "├",
    "info": "│",
    "summary": "│",
    "warn": "├",
    "error": "├",
}
# The mark's colour: the accent for a stage's node, the muted grey for every
# branch. A failure's mark is grey like any other — its colour lives in its
# text, where the eye already goes.
_LOG_EVENT_MARK_COLORS: dict[str, tuple[str, str]] = {
    "heading": ui_theme.TOKENS["accent_text"],
    "detail": _LOG_MUTED,
    "phase": _LOG_MUTED,
    "info": _LOG_MUTED,
    "summary": _LOG_MUTED,
    "warn": _LOG_MUTED,
    "error": _LOG_MUTED,
}
# The message column's colours, from the mockup: a path is split into its
# folders, its name and its extension, and a metric is the one green thing
# on the row, so the numbers are what the eye lands on.
_LOG_MESSAGE_COLORS: dict[str, tuple[str, str]] = {
    "text": ui_theme.TOKENS["log_text"],
    "banner": ui_theme.TOKENS["log_banner"],
    "metric": ui_theme.TOKENS["log_metric"],
    "path_dir": ui_theme.TOKENS["log_path_dir"],
    "path_file": ui_theme.TOKENS["log_path_file"],
    "path_ext": ui_theme.TOKENS["log_path_ext"],
}
# The separator a pair's two paths are joined with, so each half can be
# highlighted on its own.
_PAIR_SEPARATOR = "  vs  "
# A string that opens like a path: a drive letter, a UNC share, or a POSIX
# root. Only these get the folder/name/extension tinting — a diagnostic
# line ("files: 6,450 found, …") is not a path and must not be split as one.
_PATH_START = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
# A per-file failure names the stage that produced it. The scan reports its
# failures in the summary, long after the phase's banner has scrolled away,
# so without this the row loses the one fact that says what was running when
# the file failed.
_ERROR_STAGE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Hash failed for", "hashing"),
    ("Signature failed for", "signing"),
    ("Head window failed for", "offset"),
    ("Fingerprint failed for", "fingerprinting"),
    ("Audio signature failed for", "audio"),
    ("Audio fingerprint failed for", "audio"),
    ("Missing fingerprint for pair", "comparing"),
)


def _stage_for_message(text: str) -> str | None:
    """The stage a ``<what> failed for <path>`` message came from, if known."""
    stripped = text.lstrip()
    for prefix, stage in _ERROR_STAGE_PREFIXES:
        if stripped.startswith(prefix):
            return stage
    return None


# The column gap as rich text. _LOG_GAP is two spaces for the copied and
# saved text; Qt collapses bare whitespace in HTML, so the rendered columns
# have to survive as entities or every row loses a column of alignment.
_LOG_GAP_HTML = "&nbsp;&nbsp;"


def _duration(seconds: float) -> str:
    """Human duration: "840 ms", "26.9 s", "3 m 12 s"."""
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes} m {secs} s"


def _pair_key(first: str, second: str) -> tuple[str, str]:
    """Order-independent key for a pair of identities (paths or contents)."""
    return (first, second) if first <= second else (second, first)


# A drive letter or UNC share — the shapes error strings put paths in
_PATH_IN_TEXT = re.compile(r'(?:[A-Za-z]:|\\\\|(?<=\s)/)[^<>|"\r\n]*')
_SPACE_RUN = re.compile(r" {2,}")


def _path_in(text: str) -> str | None:
    """The first existing path named inside ``text``, if any.

    Error strings append detail after the path ("… video.mp4 (0/12 usable
    frames)"), so the match is trimmed back a word at a time until what is
    left names something on disk.
    """
    for match in _PATH_IN_TEXT.finditer(text):
        candidate = match.group(0).rstrip(" ,;:)")
        while candidate and not Path(candidate).exists():
            cut = candidate.rfind(" ")
            if cut <= 2:  # "P:" alone — nothing left to trim to
                break
            candidate = candidate[:cut].rstrip(" ,;:)")
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _log_html(text: str) -> str:
    """Escape for rich text, keeping runs of spaces (the alignment) intact."""
    return _SPACE_RUN.sub(lambda m: "&nbsp;" * len(m.group(0)), escape(text))


def _log_one_path_html(text: str) -> str:
    """One path, split at its last separator and its last dot."""
    cut = max(text.rfind("/"), text.rfind("\\"))
    folder, name = (text[: cut + 1], text[cut + 1 :]) if cut >= 0 else ("", text)
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    folder_color = _ui_color(*_LOG_MESSAGE_COLORS["path_dir"])
    file_color = _ui_color(*_LOG_MESSAGE_COLORS["path_file"])
    ext_color = _ui_color(*_LOG_MESSAGE_COLORS["path_ext"])
    return (
        f'<span style="color:{folder_color}">{_log_html(folder)}</span>'
        f'<span style="color:{file_color}">{_log_html(stem)}</span>'
        f'<span style="color:{ext_color}">{_log_html(dot + extension)}</span>'
    )


def _log_path_html(text: str) -> str:
    """A path — or a pair of them — with its parts tinted.

    The folders recede, the file name is the brightest thing on the row and
    the extension sits between, so a row of long paths still shows where
    each one is and what it is called without reading the whole string.
    """
    parts: list[str] = []
    for index, piece in enumerate(text.split(_PAIR_SEPARATOR)):
        if index:
            parts.append(_log_html(_PAIR_SEPARATOR))
        parts.append(_log_one_path_html(piece))
    return "".join(parts)


def _log_header_text() -> str:
    """The column header, laid out in the same character columns as a row."""
    return f"{'TIMESTAMP':<12}{_LOG_GAP}EVENT / FILE PATH"


# A drive letter at the start of a token in a line — "e:\vids", "p:/video".
# Windows writes drive letters in caps, but a scan can find "e:\vids" and
# "E:\vids" for the same volume, and a log that mixes them reads as two
# different disks.
_DRIVE_LETTER = re.compile(r"(?<![0-9A-Za-z])([a-z]):(?=[\\/])")


def _tidy_drive_letters(text: str) -> str:
    """Upper-case the drive letters of the paths named in a line."""
    return _DRIVE_LETTER.sub(lambda m: m.group(1).upper() + ":", text)


@dataclass(frozen=True)
class _LogEntry:
    """One scan-log row: what it says, how it reads, what it points at."""

    stamp: str
    text: str
    level: str = "info"
    path: str | None = None
    # The phase a row belongs to, kept for filtering and for naming the
    # origin of a per-file failure. The rail shows the phase once, so this
    # is metadata now, not a printed column.
    stage: str | None = None


def _event_text(entry: _LogEntry) -> str:
    """The row's text without the phase label the heading already says.

    A phase row is written as "<label>  <count> in <duration>" and a file
    row as "<label>  <path>". The heading above names the phase once, so the
    row drops the repeated label and shows only what it adds.
    """
    prefix = _phase_column(entry.stage) if entry.stage else ""
    if prefix and entry.text.startswith(prefix):
        return entry.text[len(prefix) :].lstrip()
    return entry.text.lstrip()


class _ScanLogView(QPlainTextEdit):
    """Read-only log that reports clicked rows back to the window."""

    row_activated = Signal(int)  # block number of the clicked row

    def __init__(self) -> None:
        super().__init__()
        # A scan log is read as a column: a path that wraps pushes its own
        # timestamp out of line and makes the next row look like part of the
        # previous one. Rows stay one line wide; the log scrolls sideways.
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Set by the window: rows naming a file switch to a hand cursor
        self.row_clickable: Callable[[int], bool] = lambda _block: False
        self.viewport().setMouseTracking(True)

    @override
    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            point = event.position().toPoint()
            cursor = self.cursorForPosition(point)
            block_rect = self.blockBoundingGeometry(cursor.block()).translated(
                self.contentOffset()
            )
            # A click below the last row lands on the nearest block too;
            # only a point inside the row it names opens anything
            if block_rect.contains(point):
                self.row_activated.emit(cursor.blockNumber())
        super().mousePressEvent(event)

    @override
    def keyPressEvent(self, event: Any) -> None:
        # The arrow keys already move the cursor from row to row; Enter
        # opens the row it sits on, so the reveal is not mouse-only.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.row_activated.emit(self.textCursor().blockNumber())
            event.accept()
            return
        super().keyPressEvent(event)

    @override
    def mouseMoveEvent(self, event: Any) -> None:
        point = event.position().toPoint()
        clickable = self.row_clickable(self.cursorForPosition(point).blockNumber())
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.IBeamCursor
        )
        super().mouseMoveEvent(event)


def _relative_age(stamp: str | None) -> str:
    """Human age of an ISO-8601 timestamp, e.g. "12 min ago"."""
    if not stamp:
        return "unknown"
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - when).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)} min ago"
    hours = minutes / 60
    if hours < 36:
        return f"{round(hours)} h ago"
    return f"{round(hours / 24)} days ago"


# Scan phases mapped to a share of the overall progress bar, roughly
# proportional to the time each typically takes (dense fingerprint
# extraction dominates a cold scan). Unknown phases hold the bar.
_PHASE_WEIGHTS: dict[str, int] = {
    "initialization": 1,
    "discovery": 4,
    "probing": 10,
    "hashing": 8,
    "signing": 20,
    "blocking": 2,
    "verifying": 5,
    "offset": 3,
    "fingerprinting": 32,
    "audio": 5,
    "comparing": 10,
}
_PHASE_STARTS: dict[str, int] = {
    phase: sum(list(_PHASE_WEIGHTS.values())[:i]) for i, phase in enumerate(_PHASE_WEIGHTS)
}


def _resource_path(*parts: str) -> Path:
    """A file shipped with the app, wherever it is running from.

    PyInstaller unpacks bundled data under ``sys._MEIPASS``; a source
    checkout keeps the same tree at the repository root.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    base = Path(bundle) if bundle else Path(__file__).resolve().parents[3]
    return base.joinpath(*parts)


def _app_icon() -> QIcon:
    """The window, taskbar and title-bar icon, in every size the file holds."""
    return QIcon(str(_resource_path("resources", "icons", "akin-video-finder.ico")))


def _build_id() -> str:
    """Git short hash for the title bar, or '' when unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parents[3],
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _file_metrics(file_row: dict[str, Any], probe: dict[str, Any] | None) -> dict[str, float]:
    """Sortable metric values for a file."""
    duration = float(probe["container_duration"] or 0.0) if probe else 0.0
    width = int(probe["primary_video_width"] or 0) if probe else 0
    height = int(probe["primary_video_height"] or 0) if probe else 0
    bitrate = float(probe["container_bitrate"] or 0.0) if probe else 0.0
    codec = str(probe["primary_video_codec"] or "").lower() if probe else ""
    return {
        "size": float(file_row["size"]),
        "duration": duration,
        "resolution": float(width * height),
        "bitrate": bitrate,
        "codec": float(_VIDEO_CODEC_RANK.get(codec, 0)),
    }


def _clear_layout(layout: QLayout) -> None:
    """Remove every item from a layout, nested layouts included.

    ``takeAt`` hands back a layout item, and for a nested row it wraps another
    layout rather than a widget — ``item.widget()`` is None there, so a
    one-level loop left the header row and the note cards parented to the
    dialog and still painting after the body was rebuilt. Detaching the widget
    immediately (rather than only calling ``deleteLater``) is what stops it
    from being drawn until the event loop catches up.
    """
    while (item := layout.takeAt(0)) is not None:
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            _clear_layout(child)
            child.deleteLater()


def _reveal_in_explorer(path: Path) -> bool:
    """Reveal a file in Explorer (its folder when it is gone); True if opened.

    The command line is built by hand on Windows because explorer parses its
    own: ``explorer /select,"C:\\dir\\a b.mp4"`` is one switch glued to one
    quoted path, while the argv form ``["explorer", "/select,", "C:\\dir\\a
    b.mp4"]`` gets quoted *around the switch too* — explorer then reads a
    quoted file name, fails to find it, and opens Documents. The log-row
    reveal used the argv form, so a double-click on a per-file failure did
    nothing; both callers share this helper now.
    """
    if sys.platform == "win32":
        if path.is_file():
            subprocess.Popen(f'explorer /select,"{path}"')
            return True
        folder = path if path.is_dir() else path.parent
        if folder.is_dir():
            subprocess.Popen(["explorer", str(folder)])
            return True
        return False
    folder = path.parent if path.is_file() else (path if path.is_dir() else path.parent)
    if folder.is_dir():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        return True
    return False


def _format_size(size: int | float) -> str:
    """A file size in the unit a person reads: GB, MB or KB."""
    value = float(size)
    if value <= 0:
        return "?"
    if value >= 1e9:
        return f"{value / 1e9:,.2f} GB"
    if value >= 1e6:
        return f"{value / 1e6:,.0f} MB"
    return f"{value / 1e3:,.0f} KB"


def _size_text(value: int | float) -> str:
    """A sum of file sizes with one decimal, in its own unit.

    The chips print a single file's size at a glance, but a savings total or a
    delete confirmation is a figure someone acts on: a 4.1 MB pair rendered as
    "0.0 GB" by the old GB-only format. One decimal keeps 4.1 MB and 1.5 GB
    readable, and the unit follows the magnitude instead of the context.
    """
    size = float(value)
    if size >= 1e9:
        return f"{size / 1e9:,.1f} GB"
    if size >= 1e6:
        return f"{size / 1e6:,.1f} MB"
    if size >= 1e3:
        return f"{size / 1e3:,.0f} KB"
    return f"{size:,.0f} B"


def _format_duration(seconds: float) -> str:
    """Fixed HH:MM:SS.mmm — the precision the keeper ranking compares.

    Human ``m:ss`` reads better but hides the sub-second differences that make
    one copy the keeper; the review's answer is one uniform, exact format, not
    a mixed human/precise pair.
    """
    if seconds <= 0:
        return "?"
    total_ms = int(round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    whole_seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{millis:03d}"


_VIDEO_CODEC_LABELS: dict[str, str] = {
    "h264": "H.264",
    "avc": "H.264",
    "h265": "HEVC",
    "hevc": "HEVC",
    "mpeg4": "MPEG-4",
    "mpeg2video": "MPEG-2",
    "vp8": "VP8",
    "vp9": "VP9",
    "av1": "AV1",
    "vc1": "VC-1",
    "wmv3": "WMV3",
    "theora": "Theora",
}


def _format_codec(codec: str) -> str:
    """A codec's display name; unknown codecs keep their raw spelling."""
    raw = codec.strip()
    if not raw:
        return "?"
    return _VIDEO_CODEC_LABELS.get(raw.lower(), raw.upper())


def _format_bitrate(bitrate: float) -> str:
    """Bitrate in Mbps, or kbps below one Mbps."""
    if bitrate <= 0:
        return "?"
    if bitrate >= 1e6:
        return f"{bitrate / 1e6:,.2f} Mbps"
    return f"{bitrate / 1e3:,.0f} kbps"


def _metric_text(file_row: dict[str, Any], probe: dict[str, Any] | None) -> dict[str, str]:
    """Display text for each metric, in the review's reading order."""
    duration = float(probe["container_duration"] or 0.0) if probe else 0.0
    width = int(probe["primary_video_width"] or 0) if probe else 0
    height = int(probe["primary_video_height"] or 0) if probe else 0
    bitrate = float(probe["container_bitrate"] or 0.0) if probe else 0.0
    codec = str(probe["primary_video_codec"] or "") if probe else ""
    return {
        "resolution": f"{width:,} × {height:,}" if width and height else "?",
        "bitrate": _format_bitrate(bitrate),
        "codec": _format_codec(codec),
        "duration": _format_duration(duration),
        "size": _format_size(file_row["size"]),
    }


class SheetLoader(QObject):
    """Background contact-sheet renderer; emits file_id + sheet path.

    Renders run on **one** worker thread at a time. Concurrent renders used
    to deadlock inside the subprocess machinery — several Python threads were
    finalizing ``Popen`` objects at once and the stacks were parked in
    ``CloseHandle`` — so a preview session could sit silent for minutes with
    every worker blocked. One renderer keeps the ``Popen`` lifecycle on a
    single thread; a watchdog still bounds a stuck render, and replaces the
    worker instead of leaving the queue dead behind it.

    Workers never touch Qt: results go onto a ``queue.Queue`` that a timer on
    the GUI thread drains. That is what keeps a late render from emitting
    into a window that no longer exists.
    """

    sheet_ready = Signal(int, str)  # file_id, absolute sheet path
    sheet_failed = Signal(int)  # file_id — show a placeholder instead
    pending_changed = Signal(int)  # live queue depth for the status bar

    _RENDER_DEADLINE_S = 90.0

    def __init__(self, sheets_dir: Path, ffmpeg_path: str, workers: int = 4) -> None:
        super().__init__()
        self.sheets_dir = sheets_dir
        self.ffmpeg_path = ffmpeg_path
        # Priority queue instead of a FIFO pool — the most recently
        # requested (currently on-screen) card renders first, and
        # workers skip entries the watchdog already expired
        self._sheet_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._sheet_seq = 0
        # ``workers`` still records whether the caller wants rendering at
        # all (tests pass 0 to drive the queue by hand); more than one
        # renderer is not allowed, see the class docstring
        self._max_workers = 1 if workers else 0
        self._worker: threading.Thread | None = None
        self._worker_gen = 0
        self._stopping = False
        self._results: queue.Queue[tuple] = queue.Queue()
        # Failures carry a retry budget, not a permanent blacklist —
        # RSS kills and timeouts are usually transient (contention on
        # busy scans), so a healthy file must recover on a later request.
        # (attempts, last-failure-monotonic); the cap decays so a file
        # that keeps failing doesn't stay suppressed forever
        self.failed: dict[Path, tuple[int, float]] = {}
        self._max_attempts = 3
        self._fail_cooldown_s = 300.0
        # In-flight + queued jobs: (file_id, deadline, seq, priority).
        # deadline is None while queued — it starts when a worker pulls
        # the job, so a long warm-up queue can't expire jobs that never
        # got a worker slot. The watchdog resolves any in-flight job
        # that stays pending too long so no card is stuck forever
        self.pending: dict[Path, tuple[int, float | None, int, int]] = {}
        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self._expire_pending)
        self.watchdog.start(15_000)
        self._pump = QTimer(self)
        self._pump.timeout.connect(self._drain_results)
        self._pump.start(100)
        sheets_dir.mkdir(parents=True, exist_ok=True)
        # The render thread starts on the first request — a window that
        # never shows a card should not keep one parked on a queue

    def cached_sheet(self, file_row: dict[str, Any]) -> str | None:
        """Path of this file's sheet if it is already rendered on disk."""
        sheet_path = self.sheets_dir / sheet_name(file_row)
        return str(sheet_path) if sheet_path.exists() else None

    def request(self, file_row: dict[str, Any], duration_s: float, *, priority: int = 0) -> None:
        """Queue a contact-sheet render.

        priority: 0 = interactive (on-screen cards), 1 = prefetch
        (upcoming compare pairs), 2 = background warm-up. Higher-
        priority jobs run first; a higher-priority request supersedes
        the same sheet's queued job.
        """
        sheet_path = self.sheets_dir / sheet_name(file_row)
        logger.debug(
            f"sheet request {file_row['original_path']} "
            f"(cached={sheet_path.exists()} pending={sheet_path in self.pending})"
        )
        if sheet_path.exists():
            self.sheet_ready.emit(file_row["id"], str(sheet_path))
            return
        attempts, last_fail = self.failed.get(sheet_path, (0, 0.0))
        if attempts >= self._max_attempts and time.monotonic() - last_fail < self._fail_cooldown_s:
            self.sheet_failed.emit(file_row["id"])
            return
        if file_row.get("presence") != "present":
            # Recorded as missing — no render possible, but don't spend
            # retry budget: after a restore the file is present again and
            # the next request must be allowed to render
            self.sheet_failed.emit(file_row["id"])
            return
        entry = self.pending.get(sheet_path)
        if entry is not None:
            _fid, deadline, _seq, old_prio = entry
            if deadline is not None or priority >= old_prio:
                # In flight, or already queued at same/higher priority —
                # background tiers never re-prioritize each other
                return
        # Otherwise a higher-priority request supersedes a queued job:
        # the new seq marks the old queue entry stale, so a card the
        # user is looking at jumps the whole warm-up backlog
        self._sheet_seq += 1
        seq = self._sheet_seq
        # The seq is a generation token: only the job that owns the
        # pending entry may resolve it, so an expired-then-requeued
        # render can't clobber its successor's entry or preview
        self.pending[sheet_path] = (file_row["id"], None, seq, priority)
        self.pending_changed.emit(len(self.pending))
        # Tier ordering: interactive pops newest-first (latest card the
        # user looked at wins); background tiers FIFO so warm-up renders
        # in review order, not bottom-up
        tie = -seq if priority == 0 else seq
        self._sheet_queue.put((priority, tie, seq, file_row, duration_s, sheet_path))
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        """Start the render thread if it is missing or was replaced.

        A worker that is merely slow is left alone; the watchdog is what
        decides a worker is stuck and bumps ``_worker_gen`` so the old
        thread exits after its render returns instead of pulling the queue.
        """
        if self._stopping or not self._max_workers:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker_gen += 1
        thread = threading.Thread(target=self._sheet_worker, args=(self._worker_gen,), daemon=True)
        thread.start()
        self._worker = thread

    def _sheet_worker(self, generation: int) -> None:
        while not self._stopping and generation == self._worker_gen:
            job = self._sheet_queue.get()
            _prio, _tie, seq, file_row, duration_s, sheet_path = job
            if self._stopping or generation != self._worker_gen:
                # Replaced or shutting down: hand the job to whoever is
                # current rather than rendering from a retired thread
                if not self._stopping:
                    self._sheet_queue.put(job)
                return
            logger.debug(
                f"worker pulled {sheet_path.name} "
                f"(queued={self._sheet_queue.qsize()} pending={len(self.pending)})"
            )
            entry = self.pending.get(sheet_path)
            # Stale — already rendered, expired by the watchdog, or
            # superseded by a newer job for the same path
            if entry is None or entry[2] != seq:
                continue
            # Deadline starts now that the job has a worker — queued
            # time doesn't count against the render budget
            self.pending[sheet_path] = (
                entry[0],
                time.monotonic() + self._RENDER_DEADLINE_S,
                seq,
                entry[3],
            )
            self._render(file_row, duration_s, sheet_path, seq)

    def _render(
        self,
        file_row: dict[str, Any],
        duration_s: float,
        sheet_path: Path,
        seq: int,
    ) -> None:
        t0 = time.monotonic()
        src = Path(file_row["original_path"])
        # Check the drive anchor first — an unavailable volume fails
        # instantly instead of blocking the worker on a dead-mount stat
        if not Path(src.anchor).exists() or not src.is_file():
            ok = False
        else:
            try:
                ok = generate_contact_sheet(
                    Path(file_row["original_path"]),
                    duration_s,
                    sheet_path,
                    ffmpeg_path=self.ffmpeg_path,
                    # Healthy files render in ~1-3s, but a 4-tile batch
                    # on 2160p x265 legitimately needs ~30-40s — 15s
                    # timed out healthy warm-up sheets and made cards
                    # retry on view. Dead inputs fail fast on decode
                    # errors, so the cap only ever bounds true hangs
                    timeout_seconds=60,
                )
            except Exception:
                # Always resolve the card, even if the renderer itself blew up
                logger.exception(f"Contact sheet render crashed for {file_row['original_path']}")
                ok = False
        logger.info(
            f"sheet {'ok' if ok else 'FAILED'} {time.monotonic() - t0:.1f}s "
            f"{file_row['original_path']}"
        )
        entry = self.pending.get(sheet_path)
        live = entry is not None and entry[2] == seq
        if live:
            # Resolve on the worker so a re-request cannot queue behind a
            # job whose result is still waiting for the GUI pump
            self.pending.pop(sheet_path, None)
        # Qt is not touched from here: the pump emits on the GUI thread
        self._results.put(("resolved", file_row["id"], str(sheet_path), ok, live))
        if live:
            self._results.put(("pending", len(self.pending)))

    def _drain_results(self) -> None:
        """Emit render results on the GUI thread, one pump tick at a time."""
        while True:
            try:
                message = self._results.get_nowait()
            except queue.Empty:
                return
            if self._stopping:
                continue
            if message[0] == "pending":
                self.pending_changed.emit(message[1])
                continue
            _kind, file_id, sheet_path, ok, live = message
            if ok:
                self.sheet_ready.emit(file_id, sheet_path)
                continue
            if not live:
                # A superseded failure must not overwrite what the newer job
                # produces; a superseded success above is still a valid sheet
                continue
            path = Path(sheet_path)
            attempts, _last = self.failed.get(path, (0, 0.0))
            self.failed[path] = (attempts + 1, time.monotonic())
            self.sheet_failed.emit(file_id)

    def _expire_pending(self) -> None:
        if self._stopping:
            return
        now = time.monotonic()
        stalled = False
        for path, (file_id, deadline, _seq, _prio) in list(self.pending.items()):
            if deadline is not None and now >= deadline:
                self.pending.pop(path, None)
                # Expiry means the queue was too slow, not that the file
                # is bad — the next request retries instead of the path
                # being blacklisted for the session
                logger.warning(
                    f"Contact sheet render exceeded {self._RENDER_DEADLINE_S:.0f}s "
                    f"for file id {file_id}"
                )
                self.sheet_failed.emit(file_id)
                stalled = True
        if stalled:
            # The thread is probably parked in the subprocess machinery.
            # It cannot be cancelled, but it can be retired: dropping the
            # handle lets ``_ensure_worker`` bump the generation, which is
            # what makes the old thread exit instead of pulling the queue
            logger.warning("Contact sheet worker stalled — starting a replacement")
            self._worker = None
            self._ensure_worker()
        self.pending_changed.emit(len(self.pending))

    def shutdown(self) -> None:
        """Stop emitting and drop queued work without touching Qt threads.

        Called from ``closeEvent``. The render thread is a daemon and may
        still be inside a subprocess call; it checks ``_stopping`` at the
        top of its loop and after the render, so it exits without emitting.
        """
        self._stopping = True
        self._pump.stop()
        self.watchdog.stop()
        self.pending.clear()
        self._results = queue.Queue()
        while True:
            try:
                self._sheet_queue.get_nowait()
            except queue.Empty:
                break


class CardGrid(QWidget):
    """Grid that fits as many card columns as the width allows.

    Cards keep pair-card size (~half the viewport); extra members wrap
    to the next row instead of shrinking. Columns are recomputed on
    resize, so cards stay usable at any window width.
    """

    CARD_MIN_WIDTH = 520

    def __init__(self) -> None:
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self._cards: list[QWidget] = []
        self._cols = -1

    def add_card(self, card: QWidget) -> None:
        self._cards.append(card)
        self._relayout()

    def _relayout(self) -> None:
        cols = max(1, min(len(self._cards), self.width() // self.CARD_MIN_WIDTH))
        if cols == self._cols and self.grid.count() == len(self._cards):
            return
        self._cols = cols
        for i, card in enumerate(self._cards):
            self.grid.addWidget(card, i // cols, i % cols)
        for i in range(self.grid.columnCount()):
            self.grid.setColumnStretch(i, 1 if i < cols else 0)

    @override
    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._relayout()


class FitWidthWidget(QWidget):
    """Scroll-area container reporting width 0 from sizeHint().

    Without this, a wide child (long group title, many cards) inflates
    the container past the viewport and the area scrolls horizontally —
    or worse, the layout never settles. Review views scroll vertically
    only; children wrap/shrink to fit the viewport width.
    """

    @override
    def sizeHint(self) -> QSize:
        lay = self.layout()
        height = lay.sizeHint().height() if lay else 0
        return QSize(0, max(0, height))


class PairBox(QGroupBox):
    """A review pair's frame, carrying the comparison it shows.

    ``_pair_boxes`` used to be a list of anonymous frames, so the Compare
    dialog had no way to know which pair a scroll position pointed at: the
    boxes are built from the shown pairs but skip any whose file row is
    missing, so box *i* is not necessarily ``_shown_pairs()[i]``. Carrying
    the comparison on the frame makes "the pair in view" a lookup instead
    of a guess.
    """

    header_label: QLabel

    def __init__(self, comparison: dict[str, Any]) -> None:
        super().__init__()
        self.comparison = comparison


class ReviewToast(QFrame):
    """The short confirmation after one pair is ignored, with its way back.

    Ignoring is permanent in the database, so the row disappears at once.
    The toast keeps the last dismissal reversible for a few seconds without
    deferring the write: Undo deletes the row again and lets the review
    rebuild from the candidates it already holds.  It floats over the page
    rather than taking a row in the layout, so the list does not shift when
    it comes and goes.
    """

    undo_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("reviewToast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QFrame#reviewToast{{background:{token('surface_sunken')};"
            f"border:1px solid {token('border')};border-radius:7px;}}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 8, 6)
        row.setSpacing(9)
        tick = QLabel("✓")
        tick.setFont(font_px(11, 700))
        tick.setStyleSheet(f"color:{token('success_text')};border:none;")
        row.addWidget(tick)
        self._message = QLabel()
        self._message.setFont(font_px(12, 400))
        self._message.setStyleSheet(f"color:{token('text')};border:none;")
        row.addWidget(self._message)
        row.addSpacing(8)
        undo = QPushButton("Undo")
        undo.setFont(font_px(12, 600))
        undo.setCursor(Qt.CursorShape.PointingHandCursor)
        undo.setStyleSheet(
            f"QPushButton{{background:transparent;color:{token('accent_text')};"
            "border:none;padding:2px 4px;}"
            f"QPushButton:hover{{text-decoration:underline;}}"
            f"QPushButton:focus{{border:2px solid {token('focus')};border-radius:4px;}}"
        )
        undo.clicked.connect(self.undo_requested.emit)
        row.addWidget(undo)
        row.addWidget(Keycap("U"))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide_toast)
        self.hide()
        parent.installEventFilter(self)

    def show_message(self, text: str) -> None:
        self._message.setText(text)
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start(8000)

    def hide_toast(self) -> None:
        self._timer.stop()
        self.hide()

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        x = max(0, (parent.width() - self.width()) // 2)
        y = max(0, parent.height() - self.height() - 16)
        self.move(x, y)

    @override
    def eventFilter(self, source: QObject, event: QEvent) -> bool:
        if source is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._reposition()
        return super().eventFilter(source, event)


def _first_visible_box(spans: list[tuple[int, int]], offset: int, height: int) -> int | None:
    """Index of the box that fills most of a viewport at ``offset``.

    ``spans`` are ``(top, bottom)`` in the scroll content's coordinates.
    The box with the largest visible height wins, so a sliver of the
    previous box left at the top of the viewport does not outrank the pair
    that fills the screen. Ties go to the topmost box, which is the one a
    reader reaches first. With no intersection the viewport is either in
    the gap between two boxes — the next box is the one about to be read —
    or past the last one, where nothing else can be shown. The first box
    at or below the offset answers both, and the last box when there is
    none.
    """
    if not spans:
        return None
    bottom = offset + height
    best_index: int | None = None
    best_visible = 0
    for i, (top, end) in enumerate(spans):
        visible = min(end, bottom) - max(top, offset)
        if visible > best_visible:
            best_visible = visible
            best_index = i
    if best_index is not None:
        return best_index
    for i, (top, _end) in enumerate(spans):
        if top >= offset:
            return i
    return len(spans) - 1


class ClickableLabel(QLabel):
    """Label that emits clicked() on mouse press."""

    clicked = Signal()

    @override
    def mousePressEvent(self, _event: Any) -> None:
        self.clicked.emit()


class ElidedLabel(QLabel):
    """One line of text that elides, with the full text as its tooltip.

    A path is an identifier, not prose. Wrapped, a long one took three
    lines while its pair's took one, pushing that card's chips and buttons
    down two rows and leaving the two sides of the comparison visibly out
    of step. Eliding keeps the row one line high on every card, and the
    tooltip carries the whole string for when the middle matters too.
    """

    # Class-level default: a polish event can arrive before __init__ finishes
    _full_text = ""

    def __init__(self, text: str = "") -> None:
        super().__init__()
        # The elided text is only as wide as the label; without Ignored the
        # full text's width would become the card's minimum width
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    @override
    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._apply_elide()

    def full_text(self) -> str:
        """The whole string, before elision."""
        return self._full_text

    @override
    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    @override
    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        # A stylesheet font arrives after construction, and eliding with the
        # old metrics would leave text too long for the label
        if event.type() == QEvent.Type.FontChange:
            self._apply_elide()

    def _elide_text(self, text: str, width: int) -> str:
        return self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, max(0, width))

    def _apply_elide(self) -> None:
        super().setText(self._elide_text(self._full_text, self.width()))


def _path_root(path: str) -> str:
    """The part of a path that identifies the volume: "E:\\", "\\\\srv\\share\\", "/"."""
    if path.startswith("\\\\"):
        parts = path.split("\\")
        return "\\".join(parts[:4]) + "\\" if len(parts) > 4 else path
    for sep in ("\\", "/"):
        if sep in path:
            head, _sep, _rest = path.partition(sep)
            return head + sep
    return ""


class PathLabel(ElidedLabel):
    """Elides a path's folders, never the file name.

    Plain middle-elision splits the string in half, which cuts the *start*
    of the file name — exactly what tells two files in the same folder
    apart. This keeps the volume and the whole name and turns the folders
    into a single ellipsis: "E:\\…\\clip.mp4". A name that cannot fit is
    elided in its own middle, so the extension survives.
    """

    @override
    def _elide_text(self, text: str, width: int) -> str:
        metrics = self.fontMetrics()
        if width <= 0 or metrics.horizontalAdvance(text) <= width:
            return text
        _head, sep, name = text.rpartition("\\")
        if not sep:
            _head, sep, name = text.rpartition("/")
        if not sep:
            return super()._elide_text(text, width)
        # The volume stays; the folders between it and the name become one
        # ellipsis. A root with no separator of its own gets dropped.
        root = _path_root(text)
        prefix = root if root and root.endswith(sep) else ""
        candidate = f"{prefix}…{sep}{name}"
        if metrics.horizontalAdvance(candidate) <= width:
            return candidate
        budget = width - metrics.horizontalAdvance(f"…{sep}")
        if budget > 0:
            elided = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, budget)
            return f"…{sep}{elided}"
        return super()._elide_text(text, width)


class ClickablePathLabel(PathLabel):
    """A path row that opens the file's folder, with a link cursor."""

    clicked = Signal()

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    @override
    def mousePressEvent(self, _event: Any) -> None:
        self.clicked.emit()


class ClickableElidedLabel(ElidedLabel):
    """A one-line elided label that opens the file's folder."""

    clicked = Signal()

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    @override
    def mousePressEvent(self, _event: Any) -> None:
        self.clicked.emit()


class SheetView(ClickableLabel):
    """A contact sheet with a play affordance over its top-right corner.

    The sheet already opens the player on click; the overlay is the visible
    affordance that replaced the full-width button.  It is a real QPushButton
    so keyboard users can tab to it instead of losing the action.
    """

    def __init__(self) -> None:
        super().__init__("rendering…")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setScaledContents(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"background:{token('sheet')};color:{token('text_muted')};border-radius:6px;"
        )
        self.setMinimumHeight(120)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # An icon-only control has no text to announce; the name says what
        # the click does, the description (set per card) says which file.
        self.setAccessibleName("Contact sheet")
        self.setAccessibleDescription("Click to open the video in the player")
        self.play_button = QPushButton(self)
        self.play_button.setFixedSize(30, 30)
        self.play_button.setIcon(ui_icon("play", token("accent_fg"), 14))
        self.play_button.setIconSize(QSize(14, 14))
        self.play_button.setToolTip("Open in player")
        self.play_button.setAccessibleName("Open in player")
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.play_button.setStyleSheet(
            "QPushButton {"
            "background:rgba(0,0,0,0.58);"
            "border:1px solid rgba(255,255,255,0.75);"
            "border-radius:15px;"
            "}"
            "QPushButton:hover {background:rgba(0,0,0,0.78);}"
            f"QPushButton:focus {{border:2px solid {token('focus')};}}"
        )

    @override
    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.play_button.move(max(0, self.width() - 38), 8)
        self.play_button.raise_()


class DropZone(QWidget):
    """Accepts dragged folders; Alt-drop marks them as exclusions."""

    paths_dropped = Signal(list)  # list[str]
    excludes_dropped = Signal(list)  # list[str]
    browse_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setMinimumHeight(132)
        self.setObjectName("dropZone")
        # A plain QWidget subclass does not paint its stylesheet background by
        # default, so the dashed rectangle never appeared.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            f"QWidget#dropZone{{border:2px dashed {token('dropzone_border')};"
            f"border-radius:8px;background:{token('surface_sunken')};}}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(0)
        outer.addStretch(1)
        icon_box = QLabel()
        icon_box.setFixedSize(34, 34)
        icon_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_box.setStyleSheet(f"background:{token('surface_raised')};border-radius:8px;")
        icon_box.setPixmap(ui_icon("folder", token("text_muted"), 18).pixmap(18, 18))
        outer.addWidget(icon_box, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addSpacing(8)
        title = QLabel("Drop folders here")
        title.setFont(font_px(13, 600))
        title.setStyleSheet(f"color:{token('dropzone_text')}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
        subtitle = QLabel(
            f'<a href="browse" style="color:{token("accent")};'
            'text-decoration:none">or browse for a folder</a>'
        )
        subtitle.setTextFormat(Qt.TextFormat.RichText)
        subtitle.setOpenExternalLinks(False)
        subtitle.setFont(font_px(11, 400))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.linkActivated.connect(lambda _href: self.browse_requested.emit())
        outer.addWidget(subtitle, 0, Qt.AlignmentFlag.AlignHCenter)
        alt_widget = QWidget()
        alt_row = QHBoxLayout(alt_widget)
        alt_row.setContentsMargins(0, 0, 0, 0)
        alt_row.setSpacing(6)
        alt_row.addStretch(1)
        alt_row.addWidget(Keycap("Alt"))
        alt_caption = QLabel("while dropping excludes")
        alt_caption.setFont(font_px(11, 400))
        alt_caption.setStyleSheet(f"color:{token('text_muted')}")
        alt_row.addWidget(alt_caption)
        alt_row.addStretch(1)
        outer.addSpacing(4)
        outer.addWidget(alt_widget, 0)
        outer.addStretch(1)

    @override
    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    @override
    def dropEvent(self, event: Any) -> None:
        dirs = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and Path(url.toLocalFile()).is_dir()
        ]
        if not dirs:
            return
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            self.excludes_dropped.emit(dirs)
        else:
            self.paths_dropped.emit(dirs)


class FileCard(QFrame):
    """One file: sheet image, path, metrics, keeper/decision controls."""

    def __init__(
        self,
        window: "MainWindow",
        file_row: dict[str, Any],
        probe: dict[str, Any] | None,
        peers: Sequence[tuple[dict[str, Any], dict[str, Any] | None]],
        is_keeper: bool,
        reasons: list[str],
        tie_label: str | None = None,
        index: int | None = None,
    ) -> None:
        super().__init__()
        self.main_window = window
        self.file_row = file_row
        self.peer_ids = [row["id"] for row, _p in peers if row["id"] != file_row["id"]]
        self.is_keeper = is_keeper
        self.tie_label = tie_label
        self.index = index
        self.sheet_path: str | None = None
        self._sheet_requested = False
        self._decision: str | None = None
        self.setObjectName("fileCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            f"QFrame#fileCard{{background:{token('surface')};"
            f"border:1px solid {token('border')};border-radius:8px;}}"
        )
        # Five metric chips include a millisecond duration, so the card needs
        # enough room for them without clipping; the review grid's minimum is
        # wider than this, and Compare lets each side grow to the sheet's
        # native width.
        self.setMinimumWidth(420)
        self.setMaximumWidth(CARD_MAX_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(7)

        self.image_label = SheetView()
        self.image_label.clicked.connect(self._open_file)
        self.image_label.play_button.clicked.connect(self._open_file)
        # Name the file on the sheet and its play overlay: two cards side by
        # side otherwise offer two identical "Open in player" buttons.
        self._file_name = Path(file_row["original_path"]).name
        self.image_label.setAccessibleDescription(f"Click to open {self._file_name} in the player")
        self.image_label.play_button.setAccessibleDescription(self._file_name)
        layout.addWidget(self.image_label)

        # The path is an identifier, not prose: the name owns the first line,
        # the folder the second, and both are clickable to reveal the file in
        # Explorer.  No separate link — the path itself is the affordance.
        path_block = QFrame()
        path_block.setObjectName("pathBlock")
        path_block.setStyleSheet(
            f"QFrame#pathBlock{{background:{token('surface_raised')};border-radius:5px;}}"
        )
        path_layout = QVBoxLayout(path_block)
        path_layout.setContentsMargins(7, 4, 7, 4)
        path_layout.setSpacing(0)
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(6)
        self.name_label = ClickableElidedLabel(Path(file_row["original_path"]).name)
        self.name_label.setFont(font_px(13, 600))
        self.name_label.setStyleSheet(f"color:{token('text')}")
        self.name_label.clicked.connect(self._reveal_file)
        name_row.addWidget(self.name_label, 1)
        path_layout.addLayout(name_row)
        self.folder_label = ClickablePathLabel(str(Path(file_row["original_path"]).parent))
        self.folder_label.setFont(font_px(11, 400))
        self.folder_label.setStyleSheet(f"color:{token('text_muted')}")
        self.folder_label.clicked.connect(self._reveal_file)
        self.folder_label.setToolTip(file_row["original_path"])
        self.path_label = self.folder_label
        path_layout.addWidget(self.folder_label)
        layout.addWidget(path_block)

        metrics = _file_metrics(file_row, probe)
        texts = _metric_text(file_row, probe)
        peer_metrics = [_file_metrics(row, p) for row, p in peers]
        # One fixed-height strip holds the metrics and their verdict badge:
        # they are the card's at-a-glance line and read left to right. The
        # fixed height keeps a badge appearing on one copy from moving its
        # buttons out of line with the peer's.
        self.metrics_strip = QWidget()
        self.metrics_strip.setFixedHeight(20)
        metrics_row = QHBoxLayout(self.metrics_strip)
        metrics_row.setContentsMargins(0, 0, 0, 0)
        metrics_row.setSpacing(4)
        chip_base = "padding:1px 6px;border-radius:3px;font-size:10px;"
        chip_neutral = (
            f"{chip_base}background:{token('badge_neutral_bg')};color:{token('text_secondary')};"
        )
        chip_best = (
            f"{chip_base}background:{token('success_soft')};"
            f"color:{token('success')};font-weight:600;"
        )
        for key, text in texts.items():
            values = [pm[key] for pm in peer_metrics]
            # Size is never "better": a larger copy can be a bloated encode.
            best = key != "size" and len(set(values)) > 1 and metrics[key] == max(values)
            chip = QLabel(f"▲ {text}" if best else text)
            chip.setObjectName("metricChip")
            chip.setFont(tabular_font(font_px(10, 600 if best else 400)))
            chip.setStyleSheet(chip_best if best else chip_neutral)
            # Each chip keeps its own width: stretched chips read as
            # separate columns, and the minimum width below is measured
            # from this strip so the row cannot be squeezed
            chip.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            metrics_row.addWidget(chip)
        metrics_row.addStretch(1)
        self.recommended_badge = Badge("★ Recommended", "recommended")
        self.tie_badge = Badge(tie_label or "", "neutral")
        self.kept_badge = Badge("✓ Kept", "kept")
        self.remove_badge = Badge("Remove", "remove")
        badges = (
            self.recommended_badge,
            self.tie_badge,
            self.kept_badge,
            self.remove_badge,
        )
        for badge in badges:
            metrics_row.addWidget(badge)
            badge.hide()
        layout.addWidget(self.metrics_strip)
        widest_badge = max(badge.sizeHint().width() for badge in badges)
        self.setMinimumWidth(
            max(420, metrics_row.minimumSize().width() + widest_badge + 8)
        )

        if reasons:
            self.setToolTip("\n".join(reasons))
        if file_row.get("is_symlink"):
            link = QLabel(f"<i>symlink → {file_row.get('link_target') or '?'}</i>")
            link.setFont(font_px(11, 400))
            link.setWordWrap(True)
            link.setStyleSheet(f"color:{token('text_muted')};")
            layout.addWidget(link)

        # Stretch above the buttons so they align at the card bottom
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        # The pair's 1/2 shortcut keeps this copy, so the keycap sits on the
        # control it triggers rather than beside the file name.
        if index is not None:
            self.keep_button: QPushButton = KeyedButton("Keep", str(index), "left", "secondary")
        else:
            self.keep_button = QPushButton("Keep")
        self.remove_button = QPushButton("Mark for removal")
        self.keep_button.setCheckable(True)
        self.remove_button.setCheckable(True)
        apply_button_role(self.keep_button, "secondary")
        apply_button_role(self.remove_button, "secondary")
        keep_base = button_style("secondary")
        if isinstance(self.keep_button, KeyedButton):
            # The button's own layout owns the padding, like KeyedButton does
            keep_base = keep_base.replace("padding:6px 14px;", "padding:0px;")
        self.keep_button.setStyleSheet(
            keep_base + f"QPushButton:checked{{background:{token('success_soft')};"
            f"border-color:{token('success')};color:{token('success')};}}"
        )
        self.remove_button.setStyleSheet(
            button_style("secondary") + f"QPushButton:checked{{background:{token('danger_soft')};"
            f"border-color:{token('danger')};color:{token('danger')};}}"
        )
        # "Keep" alone names one of four identical buttons on a pair, so the
        # name carries the file and the description says what the pair does.
        self.keep_button.setAccessibleName(f"Keep {self._file_name}")
        self.keep_button.setAccessibleDescription("Keeps this copy and marks the other for removal")
        self.remove_button.setAccessibleName(f"Mark {self._file_name} for removal")
        self.remove_button.setAccessibleDescription(
            "Marks this copy for removal and keeps the other copy"
        )
        for button in (self.keep_button, self.remove_button):
            buttons.addWidget(button, 1)
            # A double-click used to send two clicked signals, so the second
            # one undid the decision the first had just made. Consuming the
            # dblclick event leaves the deliberate second click (undo) alone.
            button.installEventFilter(self)
        self.keep_button.clicked.connect(lambda: self._toggle("keep"))
        self.remove_button.clicked.connect(lambda: self._toggle("remove"))
        layout.addLayout(buttons)

        self.set_decision(window.decisions.get(file_row["id"]) or None)

    def ensure_sheet(self) -> None:
        """Request the contact sheet once — safe to call repeatedly."""
        if self._sheet_requested:
            return
        self._sheet_requested = True
        try:
            self.main_window.sheet_loader.request(
                self.file_row, self.main_window._duration(self.file_row["id"])
            )
        except Exception:
            # A failure here would leave the card on 'rendering…'
            # forever — log it and show the placeholder instead
            logger.exception(f"Sheet request failed for {self.file_row.get('original_path')}")
            self.set_sheet_failed()

    @override
    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        self.ensure_sheet()

    def _open_file(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.file_row["original_path"]))

    def _reveal_file(self) -> None:
        """Open the file's folder with that row selected.

        The sheet's click already plays the video; the path row answers the
        other question — where on disk this copy lives. Explorer's ``/select``
        switch lands on the folder *with the file highlighted*, which is what
        a reviewer wants next (move, rename, look at its neighbours). A file
        that is gone opens its folder instead of selecting nothing.
        """
        _reveal_in_explorer(Path(self.file_row["original_path"]))

    def _toggle(self, decision: str) -> None:
        """A second click on the active button undoes the decision.

        The explicit Undo button is gone: the two decision buttons fill the
        row, and clicking the one already in effect clears it.
        """
        self._decide(None if self._decision == decision else decision)

    def _decide(self, decision: str | None) -> None:
        mw = self.main_window
        file_id = self.file_row["id"]
        # Either direction overrides what this card last propagated: a flip
        # from Keep to Remove must release the removals it made, and back.
        # Both owner records are cleared for this file either way, so a
        # second click cannot leave a stale pair behind.
        mw.release_auto_marks(file_id)
        mw.release_auto_keeps(file_id)
        if decision is None and self._decision is not None:
            # Undoing a pair decision clears the counterpart too, even when
            # the peer carried it before this card was clicked: a kept copy
            # set before "Mark for removal" is still the other half of that
            # decision, so leaving it behind would make the pair look decided.
            # A counterpart another keeper still depends on stays put.
            counter = "remove" if self._decision == "keep" else "keep"
            owners = mw.auto_marks if counter == "remove" else mw.auto_keeps
            for peer_id in self.peer_ids:
                if mw.decisions.get(peer_id) != counter:
                    continue
                if mw._owned(peer_id, owners):
                    continue
                mw.apply_decision(peer_id, None)
        if decision == "keep":
            # Keeping one copy marks its duplicates for removal; remembered in
            # auto_marks so clearing this undoes them. A peer already kept is
            # taken over the same way the remove direction takes over a kept
            # peer: a pair reads one kept and one removed whichever button
            # starts it, so a 3-copy set cannot leave two keeps in one pair.
            flipped: list[str] = []
            for peer_id in self.peer_ids:
                if mw.mark_peer_removed(file_id, peer_id, take_over=True):
                    name = (mw.files.get(peer_id) or {}).get("name") or "the other copy"
                    flipped.append(name)
            if flipped:
                mw._show_status_message(
                    f"Marked {', '.join(flipped)} for removal — a pair keeps one copy.",
                    4000,
                )
        elif decision == "remove":
            # The mirror image: removing one copy keeps the other, so the
            # pair always reads as one kept and one removed. If the peer is
            # already marked remove, the invariant wins: this click moves the
            # removal here and keeps the earlier copy, because a pair that
            # lost both files would never carry a keeper.
            flipped: list[str] = []
            for peer_id in self.peer_ids:
                if mw.mark_peer_kept(file_id, peer_id, take_over=True):
                    name = (mw.files.get(peer_id) or {}).get("name") or "the other copy"
                    flipped.append(name)
            if flipped:
                mw._show_status_message(f"Kept {', '.join(flipped)} — a pair keeps one copy.", 4000)
        mw.apply_decision(file_id, decision)

    def set_decision(self, decision: str | None) -> None:
        self._decision = decision
        self.keep_button.setChecked(decision == "keep")
        self.remove_button.setChecked(decision == "remove")
        keep_text = "Kept" if decision == "keep" else "Keep"
        if isinstance(self.keep_button, KeyedButton):
            self.keep_button.set_label_text(keep_text)
            # The child label carries its own colour, so QSS :checked
            # cannot recolour it
            self.keep_button.set_label_color(
                token("success") if decision == "keep" else token("text")
            )
        else:
            self.keep_button.setText(keep_text)
        # set_label_text renames the button from its face; keep the file in
        # the name so the four decision buttons of a pair stay distinguishable
        self.keep_button.setAccessibleName(
            f"Kept: {self._file_name}" if decision == "keep" else f"Keep {self._file_name}"
        )
        pair_decided = decision is not None or any(
            self.main_window.decisions.get(peer_id) for peer_id in self.peer_ids
        )
        recommended_visible = bool(self.is_keeper) and not self.tie_label and not pair_decided
        tie_visible = bool(self.tie_label) and not pair_decided
        kept_visible = decision == "keep"
        remove_visible = decision == "remove"
        self.recommended_badge.setText("★ Recommended" if recommended_visible else "")
        self.recommended_badge.setVisible(recommended_visible)
        self.tie_badge.setText(self.tie_label or "" if tie_visible else "")
        self.tie_badge.setVisible(tie_visible)
        self.kept_badge.setText("✓ Kept" if kept_visible else "")
        self.kept_badge.setVisible(kept_visible)
        self.remove_badge.setText("Remove" if remove_visible else "")
        self.remove_badge.setVisible(remove_visible)
        edge = (
            token("kept_edge")
            if decision == "keep"
            else token("remove_edge")
            if decision == "remove"
            else ""
        )
        border = f"border-left:3px solid {edge};" if edge else ""
        self.setStyleSheet(
            f"QFrame#fileCard{{background:{token('surface')};"
            f"border:1px solid {token('border')};{border}border-radius:8px;}}"
        )

    @override
    def eventFilter(self, watched: Any, event: Any) -> bool:
        """Swallow the double-click's second press on a decision button.

        A double-click delivered press/release/press/dblclick/release, so
        ``clicked`` fired twice and the card's own "second click undoes"
        rule cancelled the decision the reviewer had just made. Qt sends
        ``MouseButtonDblClick`` instead of that second press; consuming it
        means the button never goes down again, and a deliberate second
        click keeps working.
        """
        if event.type() == QEvent.Type.MouseButtonDblClick and watched in (
            self.keep_button,
            self.remove_button,
        ):
            event.accept()
            return True
        return super().eventFilter(watched, event)

    @override
    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        # Defer to the next event-loop pass so the label sees the final
        # card geometry, then lock it to the 1280:540 sheet aspect
        QTimer.singleShot(0, self._sync_image_height)

    def _sync_image_height(self) -> None:
        try:
            card_layout = self.layout()
        except RuntimeError:
            return  # singleShot fired after the card was deleted
        if card_layout is None:
            return
        m = card_layout.contentsMargins()
        width = max(0, self.width() - m.left() - m.right())
        self.image_label.setFixedHeight(min(int(width * SHEET_ASPECT), SHEET_MAX_HEIGHT))

    def set_sheet_from_cache(self, path: str) -> None:
        """Show an already-rendered sheet, before this card's first paint.

        Without this the first paint draws the placeholder even on a
        cache hit — a one-frame black flash on every Compare pair change,
        at the placeholder's height rather than the sheet's.
        """
        self._sheet_requested = True
        self.set_sheet(path)

    def set_sheet(self, path: str) -> None:
        if path == self.sheet_path:
            return  # already showing this sheet — decoding it again is waste
        pixmap = QPixmap(path)
        if pixmap.isNull():
            # Corrupt/truncated cache file — drop it so a later paint
            # re-renders, and don't leave the card on 'rendering…'.
            # sheet_path stays unset, so that retry isn't skipped above.
            Path(path).unlink(missing_ok=True)
            self.set_sheet_failed()
            return
        self.sheet_path = path
        self.image_label.setPixmap(pixmap)
        self.image_label.setText("")

    def set_sheet_failed(self) -> None:
        self.image_label.setText("no preview")


class PairDetailsDialog(QDialog):
    """The raw evidence behind a pair, kept out of the review surface.

    The pair header and the Compare evidence strip say the sentence a reviewer
    needs; the distance, mode, coverage and exact metrics live here so the
    surface does not turn into a diagnostic dump.
    """

    def __init__(
        self,
        window: "MainWindow",
        comparison: dict[str, Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pair details")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        title = QLabel("Pair details")
        title.setFont(font_px(15, 600))
        title.setStyleSheet(f"color:{token('text')}")
        layout.addWidget(title)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(6)

        def row(label: str, value: str) -> None:
            key = QLabel(label)
            key.setFont(font_px(11, 400))
            key.setStyleSheet(f"color:{token('text_muted')}")
            val = QLabel(value or "—")
            val.setFont(font_px(12, 400))
            val.setStyleSheet(f"color:{token('text')}")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(key, val)

        row("Mean frame distance", f"{float(comparison.get('mean_frame_distance') or 0.0):.4f}")
        row("Match", f"{float(comparison.get('match_fraction') or 0.0) * 100:.1f} %")
        comparison_class = str(comparison.get("classification") or "")
        row(
            "Classification",
            # The raw enum leaked into the only human-facing row in the
            # dialog; the wording is the same one the pair header uses
            _CLASS_LABELS.get(comparison_class, comparison_class or "—"),
        )
        evidence: dict[str, Any] = {}
        if comparison.get("evidence"):
            try:
                evidence = json.loads(comparison["evidence"])
            except (TypeError, ValueError):
                evidence = {}
        if evidence:
            row("Mode", str(evidence.get("mode") or "—"))
            row(
                "Frames matched",
                f"{evidence.get('matching_frames', '—')} of {evidence.get('frames_compared', '—')}",
            )
            coverage_a = float(evidence.get("coverage_a", 1.0))
            coverage_b = float(evidence.get("coverage_b", 1.0))
            row(
                "Fingerprint coverage",
                f"A {coverage_a * 100:.1f} % · B {coverage_b * 100:.1f} %",
            )
            offset = evidence.get("offset_seconds")
            row("Offset", f"{float(offset):+.3f} s" if offset is not None else "—")
            audio = evidence.get("audio_distance")
            row("Audio distance", f"{float(audio):.4f}" if audio is not None else "—")
        for label, fid in (
            ("File A", comparison.get("file_a_id")),
            ("File B", comparison.get("file_b_id")),
        ):
            file_row = window.files.get(fid) if fid is not None else None
            probe = window.probes.get(fid) if fid is not None else None
            if not file_row:
                row(label, "—")
                continue
            row(label, str(file_row.get("original_path") or "—"))
            size_bytes = int(file_row.get("size") or 0)
            duration_s = float(probe["container_duration"] or 0.0) if probe else 0.0
            width = int(probe["primary_video_width"] or 0) if probe else 0
            height = int(probe["primary_video_height"] or 0) if probe else 0
            bitrate = float(probe["container_bitrate"] or 0.0) if probe else 0.0
            row(
                f"{label} metrics",
                " · ".join(
                    (
                        f"{size_bytes:,} bytes",
                        f"{duration_s:.2f} s" if duration_s else "?",
                        f"{width} × {height}" if width and height else "?",
                        f"{bitrate:,.0f} bps" if bitrate else "?",
                        _format_codec(str(probe["primary_video_codec"] or "")) if probe else "?",
                    )
                ),
            )
        layout.addLayout(form)
        layout.addStretch(1)
        close = QPushButton("Close")
        apply_button_role(close, "secondary")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class CompareDialog(QDialog):
    """Side-by-side pair triage — sheets at readable size, one keystroke
    per decision (←/→ move, 1/2 keep, C clear, I ignore, Esc close).
    Decisions write through the main window so Result stays in sync; a
    single rebuild happens on close."""

    def __init__(self, window: "MainWindow", start_pair: dict[str, Any] | None = None) -> None:
        super().__init__(window)
        self.mw = window
        # The pair the review page had in view when Compare was opened —
        # the dialog starts there instead of at the first pair, so a
        # reviewer scrolling to a specific pair does not lose it
        self._start_pair = start_pair
        self._dialog_cards: list[FileCard] = []
        # The blurred copy of the page behind this window; built on first show
        self._backdrop: BackdropBlur | None = None
        # Settled sheet height of the pair on screen. Changing the pair
        # does not resize the window, so the next pair reuses it instead
        # of re-deriving the layout through two deferred passes
        self._image_height = 0
        self._scan_id = window.scan_id
        # Mirror the Result list exactly — same sort and hide-decided
        # filter — so pair 1 here is the pair the user sees first there
        self.pairs = window._visible_pairs()
        # The count the empty state reports: the pair list this dialog opened
        # with, before Hide reviewed narrows it. A pair hidden by that filter
        # was still checked, and saying "0 pairs" would be wrong.
        self._pair_total = len(self.pairs)
        if getattr(window, "_result_hide_decided", False):
            self.pairs = [
                c
                for c in self.pairs
                if not (
                    window.decisions.get(c["file_a_id"]) or window.decisions.get(c["file_b_id"])
                )
            ]
        if getattr(window, "_result_sort", "match") == "size":
            self.pairs.sort(
                key=lambda c: (
                    -(
                        window.files[c["file_a_id"]].get("size", 0)
                        + window.files[c["file_b_id"]].get("size", 0)
                    )
                )
            )
        self.index = self._index_of_pair(start_pair)
        self.dirty = False
        self._file_ids: list[int] = []
        self.setWindowTitle("Compare pairs")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)

        self._stack = QStackedWidget()
        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)

        header_widget = QWidget()
        self._header_widget = header_widget
        header_row = QHBoxLayout(header_widget)
        header_row.setContentsMargins(10, 6, 10, 0)
        header_row.setSpacing(8)
        self.header = QLabel()
        self.header.setFont(font_px(14, 600))
        self.header.setStyleSheet(f"color:{token('text')}")
        header_row.addWidget(self.header)
        self.class_label = QLabel()
        self.class_label.setFont(font_px(12, 600))
        self.class_label.setStyleSheet(f"color:{token('text_secondary')}")
        header_row.addWidget(self.class_label)
        self.pct_label = QLabel()
        self.pct_label.setFont(tabular_font(font_px(12, 600)))
        header_row.addWidget(self.pct_label)
        header_row.addStretch(1)
        self.close_button = KeyedButton("Close", "Esc", "right", "secondary")
        self.close_button.setToolTip("Close (Esc)")
        self.close_button.setAccessibleName("Close compare")
        self.close_button.clicked.connect(self.reject)
        header_row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        content_layout.addWidget(header_widget)

        # Each side hosts a real FileCard — identical layout, sheet
        # loading, metric diffing, and decision buttons as the list
        self._holders: list[QWidget] = []
        # Kept on the dialog: _fit_to_screen needs the gap between the two
        # holders to derive the card width the window will give them.
        self._panels = QHBoxLayout()
        self._panels.setSpacing(12)
        panels = self._panels
        for _ in range(2):
            holder = QWidget()
            col = QVBoxLayout(holder)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            col.setAlignment(Qt.AlignmentFlag.AlignTop)
            panels.addWidget(holder, 1)
            self._holders.append(holder)
        content_layout.addLayout(panels)

        self.evidence_row = QFrame()
        self.evidence_row.setObjectName("evidenceRow")
        self.evidence_row.setStyleSheet(
            f"QFrame#evidenceRow{{background:{token('surface_raised')};border-radius:6px;}}"
        )
        evidence_layout = QHBoxLayout(self.evidence_row)
        evidence_layout.setContentsMargins(10, 6, 8, 6)
        evidence_layout.setSpacing(8)
        self.evidence_title = QLabel("Why this pair")
        self.evidence_title.setFont(font_px(11, 600))
        self.evidence_title.setStyleSheet(f"color:{token('text_secondary')}")
        evidence_layout.addWidget(self.evidence_title)
        self.evidence_label = QLabel()
        self.evidence_label.setFont(font_px(11, 400))
        self.evidence_label.setStyleSheet(f"color:{token('text_secondary')}")
        evidence_layout.addWidget(self.evidence_label, 1)
        self.details_button = QPushButton("Details")
        apply_button_role(self.details_button, "link")
        self.details_button.setAccessibleName("Show pair details")
        self.details_button.setToolTip("Raw metrics and evidence for this pair")
        self.details_button.clicked.connect(self._show_current_details)
        evidence_layout.addWidget(self.details_button)
        content_layout.addWidget(self.evidence_row)

        self._footer_widget = QWidget()
        footer_outer = QVBoxLayout(self._footer_widget)
        footer_outer.setContentsMargins(0, 0, 0, 0)
        footer_outer.setSpacing(2)
        footer = QHBoxLayout()
        footer.setContentsMargins(10, 4, 10, 0)
        self.prev_button = KeyedButton("Prev", "←", "left", "secondary")
        self.prev_button.setAccessibleName("Previous pair")
        self.prev_button.clicked.connect(lambda: self._move(-1))
        self.next_button = KeyedButton("Next", "→", "right", "secondary")
        self.next_button.setAccessibleName("Next pair")
        self.next_button.clicked.connect(lambda: self._move(1))
        self.undo_button = KeyedButton("Undo decision", "C", "right", "ghost")
        self.undo_button.setAccessibleName("Undo decision")
        self.undo_button.clicked.connect(self._clear)
        self.ignore_button = KeyedButton("Ignore pair", "I", "right", "ghost")
        self.ignore_button.setAccessibleName("Ignore pair")
        self.ignore_button.clicked.connect(self._ignore)
        footer.addWidget(self.prev_button)
        footer.addStretch(1)
        footer.addWidget(self.undo_button)
        footer.addWidget(self.ignore_button)
        footer.addStretch(1)
        footer.addWidget(self.next_button)
        footer_outer.addLayout(footer)
        content_layout.addWidget(self._footer_widget)
        content_layout.addStretch(1)

        self._empty = EmptyState(
            "check",
            "All pairs reviewed",
            "",
            icon_color=token("success"),
        )
        self._empty.add_action("Review ignored pairs", "secondary", self._review_ignored_from_empty)
        self._stack.addWidget(self._content)
        self._stack.addWidget(self._empty)
        layout.addWidget(self._stack, 1)

        # Window-scoped shortcuts fire regardless of which child widget
        # has focus — plain keyPressEvent loses arrow keys to button
        # focus navigation. Space is deliberately not bound: it activates
        # the focused control, the way it does everywhere else, and →/←
        # already page through the pairs.
        for key, slot in (
            (Qt.Key.Key_Right, lambda: self._move(1)),
            (Qt.Key.Key_Left, lambda: self._move(-1)),
            (Qt.Key.Key_1, lambda: self._keep(0)),
            (Qt.Key.Key_2, lambda: self._keep(1)),
            (Qt.Key.Key_C, self._clear),
            (Qt.Key.Key_I, self._ignore),
        ):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

        self._show()
        self._fit_to_screen()
        # Start where the reviewer acts: a dialog with no focus anchor is
        # unreachable for assistive technology, and Next is the main action.
        if self.pairs:
            self.next_button.setFocus()

    def _screen_available(self) -> QRect | None:
        """The usable area of the one screen the dialog opens on.

        ``window.screen()`` follows the main window, which is where the
        reviewer clicked Compare. The virtual desktop is deliberately not
        used: two monitors add their widths up, and a dialog fitted to that
        would open across the bezel, larger than either screen.
        """
        screen = self.mw.screen() or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _fit_to_screen(self) -> None:
        """Open at the pair's size: the monitor's width, the content's height.

        The sheets are the evidence, and they scale with the card's width.
        The old 1500 px cap shrank each sheet to a thumbnail while plenty of
        screen sat unused. The width now fills the available width of the
        monitor the dialog opens on, up to the two-native-sheet width, so a
        wide monitor shows the largest pair it can and an ultrawide keeps the
        pair together instead of spreading it across the screen. The height
        is measured from the laid-out content: a wider card is a taller
        sheet. The window is centred vertically with nothing cropped.
        """
        available = self._screen_available()
        if available is None:
            self.resize(_COMPARE_PAIR_WIDTH, 760)
            return
        width = min(_COMPARE_PAIR_WIDTH, available.width())
        margins = self.layout().contentsMargins()
        # Set the sheet heights arithmetically instead of asking the cards
        # after a layout pass: a hidden dialog's layout does not hand child
        # widgets their geometry, so the width the cards would report is
        # still the placeholder's. Sizes are computed from the width this
        # window will have once shown.
        card_width = min(
            CARD_MAX_WIDTH,
            max(
                0,
                (width - margins.left() - margins.right() - self._panels.spacing())
                // 2,
            ),
        )
        for card in self._dialog_cards:
            card_margins = card.layout().contentsMargins()
            inner = card_width - card_margins.left() - card_margins.right()
            card.image_label.setFixedHeight(
                min(int(inner * SHEET_ASPECT), SHEET_MAX_HEIGHT)
            )
        needed = self._content_height() + margins.top() + margins.bottom()
        height = min(max(needed, 560), available.height())
        self.resize(width, height)
        self.move(
            available.x() + (available.width() - width) // 2,
            available.y() + (available.height() - height) // 2,
        )

    def _content_height(self) -> int:
        """Height the header, cards, evidence strip and footer need.

        Measured from the parts' own size hints: a hidden dialog's layouts
        cache the nested size hint from before the cards had a sheet — the
        holder reported 0 px until the window was shown — so the stacked
        layout's own ``sizeHint`` understates the pair's height.
        """
        layout = self._content.layout()
        margins = layout.contentsMargins()
        heights = [self._header_widget.sizeHint().height()]
        if self._dialog_cards:
            heights.append(
                max(card.sizeHint().height() for card in self._dialog_cards)
            )
        if not self.evidence_row.isHidden():
            heights.append(self.evidence_row.sizeHint().height())
        heights.append(self._footer_widget.sizeHint().height())
        return (
            sum(heights)
            + layout.spacing() * (len(heights) - 1)
            + margins.top()
            + margins.bottom()
        )

    @override
    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if self._backdrop is None:
            self._backdrop = BackdropBlur(self.mw)
        self._backdrop.refresh()

    @override
    def hideEvent(self, event: Any) -> None:
        if self._backdrop is not None:
            self._backdrop.hide()
        super().hideEvent(event)

    @override
    def changeEvent(self, event: Any) -> None:
        if event.type() == QEvent.Type.WindowStateChange and self._backdrop is not None:
            if self.isMinimized():
                self._backdrop.hide()
            elif self.isVisible():
                self._backdrop.refresh()
        super().changeEvent(event)

    def _stale(self) -> bool:
        """A scan finishing mid-dialog rebuilds files/cards — the pair
        snapshot belongs to the old scan; close rather than write
        decisions against the new scan's ids."""
        if self.mw.scan_id != self._scan_id:
            self.reject()
            return True
        return False

    def _index_of_pair(self, comparison: dict[str, Any] | None) -> int:
        """Position of ``comparison`` in this dialog's own list, or 0.

        The dialog filters and sorts differently from the review page
        (Hide reviewed drops pairs the page still shows), so the pair in
        view can legitimately be absent. Starting at the first pair is the
        honest fallback: better than guessing a neighbour.
        """
        if comparison is None:
            return 0
        wanted = {comparison.get("file_a_id"), comparison.get("file_b_id")}
        for i, c in enumerate(self.pairs):
            if {c.get("file_a_id"), c.get("file_b_id")} == wanted:
                return i
        return 0

    def _release_cards(self) -> None:
        """Deregister and delete the two current cards — mw.cards must
        not keep wrappers for dead C++ objects."""
        heights = []
        for card in self._dialog_cards:
            with contextlib.suppress(RuntimeError):
                heights.append(card.image_label.height())
        if heights:
            self._image_height = max(heights)  # the pair's settled sheet height
        self._dialog_cards = []
        for holder in self._holders:
            layout = holder.layout()
            while layout is not None and (item := layout.takeAt(0)):
                if w := item.widget():
                    # Only FileCards are registered — key badges are
                    # plain QLabels with no file_row
                    if isinstance(w, FileCard):
                        bucket = self.mw.cards.get(w.file_row["id"])
                        if bucket and w in bucket:
                            bucket.remove(w)
                    w.deleteLater()

    def _show(self) -> None:
        # A pair swap rebuilds both cards, so the control that had focus is
        # gone by the time the new ones exist; remember where it was and put
        # focus back on the same side and role.
        focus_slot = self._card_focus_slot()
        self._release_cards()
        if self._stale():
            return
        if not self.pairs:
            self._file_ids = []
            marked = sum(1 for decision in self.mw.decisions.values() if decision == "remove")
            self._empty.body_label.setText(
                f"{_count(self._pair_total, 'pair')} checked · "
                f"{_count(marked, 'file')} marked for removal"
            )
            self._stack.setCurrentWidget(self._empty)
            return
        self._stack.setCurrentWidget(self._content)
        self.index = max(0, min(self.index, len(self.pairs) - 1))
        # Prefetch BEFORE this pair's cards request their sheets on first
        # paint — the queue is newest-first, so older (prefetch) seqs lose
        # the priority race and current-pair renders go first
        self._prefetch()
        c = self.pairs[self.index]
        fraction = float(c["match_fraction"] or 0.0)
        self.header.setText(f"Pair {self.index + 1} of {len(self.pairs)}")
        class_label = _CLASS_LABELS.get(
            str(c.get("classification") or ""),
            str(c.get("classification") or "Match"),
        )
        self.class_label.setText(class_label)
        self.pct_label.setText(f"{fraction * 100:.2f}% match")
        self.pct_label.setStyleSheet(
            f"color:{_match_color(fraction, self.mw.config.matching.possible_threshold)}"
        )
        rows = [
            self.mw.files.get(c["file_a_id"]),
            self.mw.files.get(c["file_b_id"]),
        ]
        if not all(rows):
            # File vanished between scans — drop the pair and move on
            self.pairs.pop(self.index)
            self._show()
            return
        assert rows[0] is not None and rows[1] is not None
        fa = rows[0]
        fb = rows[1]
        evidence = self.mw._evidence_sentence(c)
        self.evidence_label.setText(evidence)
        self.evidence_row.setVisible(bool(evidence))
        self._file_ids = [fa["id"], fb["id"]]
        peers = [
            (fa, self.mw.probes.get(fa["id"]) or {}),
            (fb, self.mw.probes.get(fb["id"]) or {}),
        ]
        self._dialog_cards = []
        keeper_id, _reasons, tie_label = self.mw._pair_keeper(
            peers,
            c["classification"],
        )
        for i, (f, probe) in enumerate(peers):
            # Full peers list like the Result grid — passing only the
            # other file leaves a single-element metric comparison and
            # the chips render permanently neutral
            card = FileCard(
                self.mw,
                f,
                probe,
                peers,
                f["id"] == keeper_id,
                [],
                tie_label,
                index=i + 1,
            )
            # Same window as the previous pair, so the sheet slot keeps
            # its height — a card born at its minimum height painted the
            # placeholder for a frame at the wrong size on every swap
            if self._image_height:
                card.image_label.setFixedHeight(self._image_height)
            # Take a cached sheet now rather than on first paint: the
            # paint would draw the placeholder before the (synchronous)
            # cache hit resolved, flashing black on every pair change
            cached = self.mw.sheet_loader.cached_sheet(f)
            if cached:
                card.set_sheet_from_cache(cached)
            self._dialog_cards.append(card)
            # Registering in mw.cards lets sheet_ready and
            # apply_decision updates reach the dialog's cards too
            self.mw.cards.setdefault(f["id"], []).append(card)
            holder_layout = self._holders[i].layout()
            if holder_layout is not None:
                holder_layout.addWidget(card, 0, Qt.AlignmentFlag.AlignTop)  # type: ignore[call-arg]
        self._set_focus_order()
        self._restore_card_focus(focus_slot)

    _CARD_FOCUS_ROLES = ("play", "keep", "remove")

    def _card_focus_slot(self) -> tuple[int, str] | None:
        """Which card control has focus, as ``(side, role)``."""
        focused = QApplication.focusWidget()
        for side, card in enumerate(getattr(self, "_dialog_cards", [])):
            for role in self._CARD_FOCUS_ROLES:
                if focused is self._card_control(card, role):
                    return side, role
        return None

    def _restore_card_focus(self, slot: tuple[int, str] | None) -> None:
        """Put focus back on the same control of the rebuilt cards."""
        if slot is None or not self._dialog_cards:
            return
        side = min(slot[0], len(self._dialog_cards) - 1)
        self._card_control(self._dialog_cards[side], slot[1]).setFocus()

    @staticmethod
    def _card_control(card: "FileCard", role: str) -> QWidget:
        if role == "keep":
            return card.keep_button
        if role == "remove":
            return card.remove_button
        return card.image_label.play_button

    def _set_focus_order(self) -> None:
        """Chain Tab through the dialog in visual order.

        Header, then each card's controls, then the evidence link and the
        footer.  Every button in here used to be ``NoFocus`` and the keyboard
        was served by window shortcuts alone, which left assistive
        technology with no place to start reading the dialog.
        """
        chain: list[QWidget] = [self.close_button]
        for card in self._dialog_cards:
            chain.extend(self._card_control(card, role) for role in self._CARD_FOCUS_ROLES)
        chain.extend(
            (
                self.details_button,
                self.prev_button,
                self.undo_button,
                self.ignore_button,
                self.next_button,
            )
        )
        for first, second in zip(chain, chain[1:], strict=False):
            QWidget.setTabOrder(first, second)

    def _review_ignored_from_empty(self) -> None:
        self.done(0)
        QTimer.singleShot(0, self.mw._show_dismissed_dialog)

    def _show_current_details(self) -> None:
        if self.pairs and self._file_ids:
            index = max(0, min(self.index, len(self.pairs) - 1))
            PairDetailsDialog(self.mw, self.pairs[index], self).exec()

    def _prefetch(self, ahead: int = 6) -> None:
        """Warm the cache for upcoming pairs — by the time the user
        advances, the sheets are already rendered on disk.

        Only files that still need rendering are requested. Requesting a
        cached sheet makes the loader emit ``sheet_ready`` for an image
        the cards already show, and every one of those emits re-decodes
        the image on the UI thread — measured 74 ms of stall per pair
        change on a 12-file prefetch window.
        """
        loader = self.mw.sheet_loader
        for c in self.pairs[self.index + 1 : self.index + ahead + 1]:
            for fid in (c["file_a_id"], c["file_b_id"]):
                f = self.mw.files.get(fid)
                if f and loader.cached_sheet(f) is None:
                    loader.request(f, self.mw._duration(fid), priority=1)

    def _move(self, delta: int) -> None:
        if self.pairs:
            self.index = (self.index + delta) % len(self.pairs)
            self._show()

    def _keep(self, side: int) -> None:
        if side >= len(self._file_ids) or self._stale():
            return
        mw = self.mw
        fid, peer = self._file_ids[side], self._file_ids[1 - side]
        # Either the keep or the removal it replaced may have auto-marked
        # the other side; that owes nothing once this side is decided
        mw.release_auto_marks(fid)
        mw.release_auto_keeps(fid)
        flipped = mw.mark_peer_removed(fid, peer, take_over=True)
        mw.apply_decision(fid, "keep")
        if flipped:
            name = (mw.files.get(peer) or {}).get("name") or "the other copy"
            mw._show_status_message(
                f"Marked {name} for removal — a pair keeps one copy.", 4000
            )
        self.dirty = True
        self._move(1)

    def _clear(self) -> None:
        if self._stale():
            return
        mw = self.mw
        for fid in self._file_ids:
            mw.release_auto_marks(fid)
            mw.release_auto_keeps(fid)
            mw.apply_decision(fid, None)
        self.dirty = True

    def _ignore(self) -> None:
        if not self.pairs or self._stale():
            return
        mw = self.mw
        c = self.pairs[self.index]
        key = tuple(
            sorted(
                (
                    mw.files[c["file_a_id"]]["normalized_path_key"],
                    mw.files[c["file_b_id"]]["normalized_path_key"],
                )
            )
        )
        mw.repository.dismiss_pair(key[0], key[1])
        mw.repository.commit()
        mw.note_pair_dismissed(c)
        with contextlib.suppress(ValueError):
            mw.all_pairs.remove(c)
        self.pairs.pop(self.index)
        self.dirty = True
        self._show()

    @override
    def done(self, result: int) -> None:
        # Release cards on every close — even a browse-only session
        # leaves live wrappers in mw.cards otherwise
        self._release_cards()
        super().done(result)
        if self.dirty:
            self.mw.reload_data()


def _thin_scrollbar_style() -> str:
    """QSS for a slim vertical scrollbar that sits on the widget's edge."""
    return (
        "QScrollBar:vertical{width:8px;background:transparent;margin:0;}"
        f"QScrollBar::handle:vertical{{background:{token('border')};"
        "border-radius:4px;min-height:30px;}"
        "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{"
        "height:0;border:none;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{"
        "background:transparent;}"
    )


class ProtectedFoldersDialog(QDialog):
    """Manage folders that may be scanned but never moved or deleted."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.main_window = window
        self.paths = list(window.config.actions.protected_paths)
        self.setWindowTitle("Protected folders")
        self.resize(680, 460)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(10)
        title = QLabel("Protected folders")
        title.setFont(font_px(20, 600))
        title.setStyleSheet(f"color:{token('text')}")
        outer.addWidget(title)
        intro = QLabel(
            "Files under these folders can be scanned and reviewed, but actions "
            "will never move or delete them. A whole drive is protected by "
            "listing its root."
        )
        intro.setWordWrap(True)
        intro.setFont(font_px(11, 400))
        intro.setStyleSheet(f"color:{token('text_muted')}")
        outer.addWidget(intro)
        self.list_widget = QListWidget()
        self.list_widget.setAccessibleName("Protected folders")
        outer.addWidget(self.list_widget, 1)
        edit_row = QHBoxLayout()
        edit_row.setSpacing(8)
        add = QPushButton("Add folder…")
        apply_button_role(add, "secondary")
        add.clicked.connect(self._add_folder)
        edit_row.addWidget(add)
        remove = QPushButton("Remove selected")
        apply_button_role(remove, "ghost")
        remove.clicked.connect(self._remove_selected)
        edit_row.addWidget(remove)
        edit_row.addStretch(1)
        outer.addLayout(edit_row)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        cancel = QPushButton("Cancel")
        apply_button_role(cancel, "ghost")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("Save")
        apply_button_role(save, "primary")
        save.clicked.connect(self.accept)
        actions.addWidget(save)
        outer.addLayout(actions)
        self._reload()

    def _reload(self) -> None:
        self.list_widget.clear()
        self.list_widget.addItems(self.paths)

    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Protect a folder")
        if not path:
            return
        key = os.path.normcase(str(Path(path).resolve(strict=False)))
        existing = {
            os.path.normcase(str(Path(item).resolve(strict=False))) for item in self.paths
        }
        if key not in existing:
            self.paths.append(path)
            self._reload()

    def _remove_selected(self) -> None:
        row = self.list_widget.currentRow()
        if row >= 0:
            self.paths.pop(row)
            self._reload()
            self.list_widget.setCurrentRow(min(row, len(self.paths) - 1))


class ActionsDialog(QDialog):
    """Review marked files: move them to a chosen folder, or delete
    (Recycle Bin first); also manages prior quarantined/moved items."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.main_window = window
        self.service = ActionService(window.repository, window.config.actions.protected_paths)
        self._last_move_dir: str | None = None
        self.setWindowTitle("Actions for marked files")
        self.resize(1100, 640)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(16, 14, 16, 6)
        header_row.setSpacing(8)
        self._title_label = QLabel()
        self._title_label.setFont(font_px(20, 600))
        self._title_label.setStyleSheet(f"color:{token('text')}")
        self._title_label.setTextFormat(Qt.TextFormat.RichText)
        header_row.addWidget(self._title_label)
        header_row.addStretch(1)
        outer.addLayout(header_row)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(_thin_scrollbar_style())
        container = QWidget()
        # The file list is the dialog's content: it gets the whole body width
        # and the whole spare height, so its own scrollbar sits on the right
        # edge instead of behind a strip of page. Rows that are not the list
        # (warnings, skipped files, prior actions) carry their own right inset.
        self.content_layout = QVBoxLayout(container)
        self.content_layout.setContentsMargins(16, 0, 0, 12)
        self.content_layout.setSpacing(8)
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)
        self.buttons_layout = QHBoxLayout()
        self.buttons_layout.setContentsMargins(16, 4, 16, 12)
        self.buttons_layout.setSpacing(12)
        outer.addLayout(self.buttons_layout)
        self._rebuild()

    def _rebuild(self) -> None:
        _clear_layout(self.content_layout)

        plan = self.service.plan_removals(self.main_window.scan_id)
        pending = self.service.pending_quarantine()
        actions = self.service.pending_actions()
        lost_sets = self.main_window.fully_removed_sets({str(r.path) for r in plan.removals})

        if plan.removals:
            self._title_label.setText(
                f"{_count(len(plan.removals), 'file')} totaling "
                f"<span style='color:{token('success_text')}'>"
                f"{_format_size(plan.total_bytes)}</span> marked for removal"
            )
        else:
            self._title_label.setText("Actions for marked files")

        if not plan.removals and not plan.skipped and not pending and not actions:
            empty = EmptyState(
                "shield",
                "Nothing marked for removal",
                "Mark copies in Results, then come back here to move or delete them.",
            )
            empty.add_action("Back to Results", "primary", self._back_to_results)
            self._add_indented(empty)
            self.content_layout.addStretch(1)
            self._build_footer(plan, pending)
            return

        if plan.removals:
            if lost_sets:
                # Only decisions written before the pair invariant was
                # enforced can reach this state; the review can no longer
                # select both copies of a pair by itself
                names = [
                    (self.main_window.files.get(fid) or {}).get("name", f"file {fid}")
                    for group in lost_sets
                    for fid in sorted(group)
                ]
                warning = QLabel(
                    f"This removes every copy of {_count(len(lost_sets), 'video')} — "
                    "nothing would be left."
                )
                warning.setWordWrap(True)
                warning.setFont(font_px(12, 600))
                warning.setStyleSheet(
                    f"color:{token('danger')};background:{token('danger_soft')};"
                    f"border:1px solid {token('danger')};border-radius:6px;"
                    "padding:8px 10px;"
                )
                warning.setToolTip("\n".join(names))
                self._add_indented(warning)

            table = QTableWidget(len(plan.removals) + 1, 2)
            table.setHorizontalHeaderLabels(["File", "Size"])
            file_header = table.horizontalHeaderItem(0)
            if file_header is not None:
                file_header.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
            size_header = table.horizontalHeaderItem(1)
            if size_header is not None:
                size_header.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            table.setShowGrid(False)
            table.setWordWrap(False)
            table.setItemDelegateForColumn(0, TwoLineDelegate())
            table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.setStyleSheet(
                "QTableWidget{background:transparent;border:none;}"
                f"QHeaderView::section{{background:transparent;color:{token('text_muted')};"
                "border:none;border-bottom:1px solid "
                f"{token('divider')};padding:4px 6px;font-weight:600;}}"
                + _thin_scrollbar_style()
            )
            header_view = table.horizontalHeader()
            header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            gap_row = 16
            table.verticalHeader().setMinimumSectionSize(gap_row)
            table.setRowHeight(0, gap_row)
            for row, removal in enumerate(plan.removals, start=1):
                path = Path(removal.path)
                cell = QTableWidgetItem(path.name)
                cell.setData(Qt.ItemDataRole.UserRole + 1, path.name)
                cell.setData(Qt.ItemDataRole.UserRole + 2, str(path.parent))
                cell.setToolTip(str(removal.path))
                table.setItem(row, 0, cell)
                size_item = QTableWidgetItem(_format_size(removal.size))
                size_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                size_item.setForeground(QColor(token("text_secondary")))
                table.setItem(row, 1, size_item)
                table.setRowHeight(row, 40)
            # The list owns the dialog's spare height: no fitted cap, so a
            # tall window shows more rows and a long list scrolls in place,
            # with its scrollbar on the dialog's right edge.
            table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            table_row = QHBoxLayout()
            table_row.setContentsMargins(0, 0, 0, 0)
            table_row.addWidget(table)
            self.content_layout.addLayout(table_row, 1)
        else:
            empty = EmptyState(
                "shield",
                "Nothing marked for removal",
                "Mark copies in Results, then come back here to move or delete them.",
            )
            empty.add_action("Back to Results", "primary", self._back_to_results)
            self._add_indented(empty)

        if plan.skipped:
            box = QGroupBox(f"{_count(len(plan.skipped), 'file')} skipped by the safety check")
            skip_layout = QVBoxLayout(box)
            bad = token("danger")
            for s in plan.skipped:
                lbl = QLabel(f"<span style='color:{bad}'>{s.reason}</span> — {s.path}")
                lbl.setWordWrap(True)
                skip_layout.addWidget(lbl)
            self._add_indented(box)

        if actions:
            box = QGroupBox("Moved / quarantined")
            actions_layout = QVBoxLayout(box)
            for a in actions:
                action_row = QHBoxLayout()
                status_color = {
                    "executed": token("success"),
                    "restored": token("text_muted"),
                    "deleted": token("text_muted"),
                    "failed": token("danger"),
                }.get(a["status"], token("text_muted"))
                label = QLabel(
                    f"<span style='color:{status_color}'>{a['status']}</span> "
                    f"{Path(a['source_path']).name}"
                )
                label.setToolTip(f"{a['source_path']} → {a['quarantine_path']}")
                action_row.addWidget(label, 1)
                if a["status"] == "executed":
                    restore = QPushButton("Restore")
                    apply_button_role(restore, "secondary")
                    restore.clicked.connect(lambda _c=False, aid=a["id"]: self._restore(aid))
                    action_row.addWidget(restore)
                actions_layout.addLayout(action_row)
            self._add_indented(box)

        if pending:
            pending_size = sum(Path(a["quarantine_path"]).stat().st_size for a in pending)
            empty_quarantine = QPushButton(
                f"Delete quarantined files permanently — "
                f"{_count(len(pending), 'file')} ({_size_text(pending_size)})"
            )
            apply_button_role(empty_quarantine, "danger_ghost")
            empty_quarantine.clicked.connect(
                lambda: self._empty_quarantine(len(pending), pending_size)
            )
            self._add_indented(empty_quarantine)

        # The table's stretch owns the spare height; a stretch after it
        # would split that height and leave the list short again.
        if not plan.removals:
            self.content_layout.addStretch(1)
        self._build_footer(plan, pending)

    def _add_indented(self, widget: QWidget) -> None:
        """Add a body row with the page's right inset; the file list keeps none."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 16, 0)
        row.addWidget(widget)
        self.content_layout.addLayout(row)

    def _build_footer(self, plan: Any, pending: list[dict[str, Any]]) -> None:
        _clear_layout(self.buttons_layout)
        self.close_button = KeyedButton("Close", "Esc", "right", "secondary")
        self.close_button.setToolTip("Close (Esc)")
        self.close_button.setAccessibleName("Close actions")
        self.close_button.clicked.connect(self.accept)
        self.buttons_layout.addWidget(self.close_button)
        self.buttons_layout.addStretch(1)
        if plan.removals:
            move = QPushButton(f"Move {_count(len(plan.removals), 'file')}…")
            apply_button_role(move, "secondary")
            move.setDefault(True)
            move.clicked.connect(lambda: self._move(plan))
            self.buttons_layout.addWidget(move)
            move.setFocus()
            delete = QPushButton(f"Delete {_count(len(plan.removals), 'file')}…")
            apply_button_role(delete, "danger")
            delete.clicked.connect(lambda: self._delete(plan))
            self.buttons_layout.addWidget(delete)

    def _back_to_results(self) -> None:
        self.accept()
        self.main_window.tabs.setCurrentWidget(self.main_window._result_container)

    def _move(self, plan: Any) -> None:
        dest = QFileDialog.getExistingDirectory(
            self, "Move marked files to…", self._last_move_dir or ""
        )
        if not dest:
            return
        self._last_move_dir = dest
        result = self.service.move_files(plan, Path(dest), self.main_window.scan_id)
        QMessageBox.information(
            self,
            "Move complete",
            f"Moved: {result['moved']}\nFailed: {result['failed']}\nSkipped: {result['skipped']}",
        )
        self.main_window.reload_data()
        self._rebuild()

    def _delete(self, plan: Any) -> None:
        lost_sets = self.main_window.fully_removed_sets({str(r.path) for r in plan.removals})
        lost = (
            f"\n\nThis includes every copy of {_count(len(lost_sets), 'video')};"
            " nothing would be left."
            if lost_sets
            else ""
        )
        answer = QMessageBox.warning(
            self,
            "Delete marked files",
            f"Delete {_count(len(plan.removals), 'file')} ({_size_text(plan.total_bytes)})?"
            f"{lost}\n\n"
            "They go to the Recycle Bin where possible; files the bin "
            "refuses move to quarantine and stay restorable.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        result = self.service.delete_files(plan, self.main_window.scan_id)
        QMessageBox.information(
            self,
            "Delete complete",
            f"Recycled: {result['trashed']}\nMoved to quarantine: "
            f"{result['quarantined']}\nFailed: {result['failed']}\n"
            f"Skipped: {result['skipped']}",
        )
        self.main_window.reload_data()
        self._rebuild()

    def _restore(self, action_id: int) -> None:
        if not self.service.restore_action(action_id):
            QMessageBox.warning(self, "Restore failed", "Could not restore this file.")
        self.main_window.reload_data()
        self._rebuild()

    def _empty_quarantine(self, count: int, size: int) -> None:
        answer = QMessageBox.warning(
            self,
            "Delete permanently",
            f"Permanently delete {_count(count, 'file')} ({_size_text(size)}) from quarantine?\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        result = self.service.empty_quarantine()
        QMessageBox.information(
            self,
            "Quarantine emptied",
            f"Deleted: {result['deleted']}\nFailed: {result['failed']}",
        )
        self._rebuild()


class DismissedDialog(QDialog):
    """Manage pairs dismissed as false positives — undo one, or undo a batch."""

    # Undoing rows one at a time shifts every row below it, so a large bulk
    # undo is cheaper to pay for by re-reading the table than by 3,000
    # removeRow calls. Under this many, removing them in place is free.
    _BULK_RELOAD_THRESHOLD = 40

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.main_window = window
        self._row_items: list[dict[str, Any]] = []
        self.setWindowTitle("Ignored pairs")
        self.resize(1100, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Ignored pairs")
        title.setFont(font_px(20, 600))
        title.setStyleSheet(f"color:{token('text')}")
        header.addWidget(title)
        self._count_chip = CountChip(0)
        header.addWidget(self._count_chip)
        header.addStretch(1)
        outer.addLayout(header)
        intro = QLabel(
            "Ignored pairs are hidden from results and future scans. "
            "Select one or more to show them again."
        )
        intro.setWordWrap(True)
        intro.setFont(font_px(11, 400))
        intro.setStyleSheet(f"color:{token('text_muted')}")
        outer.addWidget(intro)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter ignored pairs")
        self._filter.setAccessibleName("Filter ignored pairs")
        self._filter.setClearButtonEnabled(True)
        self._filter.setMaximumWidth(340)
        self._filter.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter)
        self._filter_count = QLabel("")
        self._filter_count.setFont(font_px(11, 400))
        self._filter_count.setStyleSheet(f"color:{token('text_muted')}")
        filter_row.addWidget(self._filter_count)
        filter_row.addStretch(1)
        outer.addLayout(filter_row)
        # The table stays, with its headers, because a filter that matches
        # nothing is a different answer from "you have never ignored a pair"
        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setFont(font_px(12, 400))
        self._hint.setStyleSheet(f"color:{token('text_muted')}")
        outer.addWidget(self._hint)
        # A real table renders ~1,500 rows instantly where stacked row
        # widgets made the dialog sluggish to open — and filters for free.
        # The two-line cells are delegates, not widgets, for the same reason.
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Select", "File A", "File B", "Ignored", "Action"])
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setMouseTracking(True)
        self.table.viewport().installEventFilter(self)
        # The per-row action is a link cell, and the rows carry no widgets;
        # the table itself answers Enter (act on the current cell) and Space
        # (toggle its checkbox) so the row action is not mouse-only.
        self.table.installEventFilter(self)
        self.table.setAccessibleName("Ignored pairs")
        self.table.setItemDelegateForColumn(1, TwoLineDelegate())
        self.table.setItemDelegateForColumn(2, TwoLineDelegate())
        self.table.setItemDelegateForColumn(3, TwoLineDelegate())
        self.table.setItemDelegateForColumn(4, ActionLinkDelegate())
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        outer.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._undo_selected = QPushButton("Show selected")
        apply_button_role(self._undo_selected, "secondary")
        self._undo_selected.setEnabled(False)
        self._undo_selected.clicked.connect(self._undismiss_selection)
        actions.addWidget(self._undo_selected)
        self._undo_shown = QPushButton("Show all…")
        apply_button_role(self._undo_shown, "ghost")
        self._undo_shown.setToolTip(
            "Show every row the filter currently shows — the way back from Ignore all shown."
        )
        self._undo_shown.clicked.connect(self._undismiss_shown)
        actions.addWidget(self._undo_shown)
        actions.addStretch(1)
        close = QPushButton("Close")
        apply_button_role(close, "ghost")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        outer.addLayout(actions)
        self._rebuild()

    def _rebuild(self) -> None:
        # Thousands of rows are normal here, so nothing per row may be a
        # widget: a QPushButton each cost 170 ms to build and another 300 ms
        # to lay out, and the action is a link cell now.
        items = self.main_window.repository.get_dismissed_items()
        keys = [key for item in items for key in item["member_keys"]]
        display = self.main_window.repository.get_display_paths_for_keys(keys)
        self._row_items = []
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.setRowCount(len(items))
        for r, item in enumerate(items):
            key_a, key_b = item["key_a"], item["key_b"]
            display_a = display.get(key_a, key_a)
            display_b = display.get(key_b, key_b)
            name_a, name_b = Path(display_a).name, Path(display_b).name
            folder_a, folder_b = str(Path(display_a).parent), str(Path(display_b).parent)
            joined = f"{display_a}\n{display_b}"
            select = QTableWidgetItem()
            select.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
            )
            select.setCheckState(Qt.CheckState.Unchecked)
            select.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            # A checkbox has no text to announce; name what checking it does
            select.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"Show {name_a} and {name_b} again",
            )
            self.table.setItem(r, 0, select)
            a_cell = QTableWidgetItem(name_a)
            a_cell.setData(Qt.ItemDataRole.UserRole + 1, name_a)
            a_cell.setData(Qt.ItemDataRole.UserRole + 2, folder_a)
            a_cell.setToolTip(display_a)
            self.table.setItem(r, 1, a_cell)
            second = name_b
            if len(item["member_keys"]) > 2:
                second += f"  ⇄  +{len(item['member_keys']) - 2} more"
            b_cell = QTableWidgetItem(second)
            b_cell.setData(Qt.ItemDataRole.UserRole + 1, name_b)
            b_cell.setData(
                Qt.ItemDataRole.UserRole + 2,
                folder_b
                + (
                    f"  ·  +{len(item['member_keys']) - 2} more"
                    if len(item["member_keys"]) > 2
                    else ""
                ),
            )
            b_cell.setToolTip(display_b)
            self.table.setItem(r, 2, b_cell)
            stamp = datetime.fromisoformat(item["created_at"]).astimezone()
            when = stamp.strftime("%d %b %Y %H:%M")
            date_cell = QTableWidgetItem(when)
            date_cell.setData(Qt.ItemDataRole.UserRole + 1, stamp.strftime("%d %b %Y"))
            date_cell.setData(Qt.ItemDataRole.UserRole + 2, stamp.strftime("%H:%M"))
            date_cell.setToolTip(f"{when}\n\nClick to filter by this time")
            self.table.setItem(r, 3, date_cell)
            action = QTableWidgetItem("Show again")
            action.setData(Qt.ItemDataRole.AccessibleTextRole, "Show again")
            action.setToolTip(f"{joined}\n\nClick to show this pair again")
            self.table.setItem(r, 4, action)
            for c in (1, 2):
                cell = self.table.item(r, c)
                if cell is not None:
                    cell.setToolTip(joined)
            self.table.setRowHeight(r, 40)
            # The date is searchable exactly as shown — with the year — and
            # in ISO form. After one "Ignore all shown" every row shares a
            # timestamp, and this is how that batch is told apart.
            item["_search"] = (
                f"{joined}\n{when}\n{stamp.isoformat()}\n{item.get('created_at', '')}"
            ).lower()
            self._row_items.append(item)
        self.table.blockSignals(False)
        self._count_chip.set_count(len(items))
        self._apply_filter(self._filter.text())
        self._update_bulk_buttons()

    def _apply_filter(self, text: str) -> None:
        needle = text.lower()
        shown = 0
        for r, item in enumerate(self._row_items):
            hidden = bool(needle) and needle not in item["_search"]
            self.table.setRowHidden(r, hidden)
            if not hidden:
                shown += 1
        total = len(self._row_items)
        if getattr(self, "_filter_count", None) is not None:
            self._filter_count.setText(f"{shown:,} shown" if total else "")
        if getattr(self, "_hint", None) is not None:
            if not total:
                self._hint.setText("No ignored pairs — pairs you ignore in Results appear here.")
            elif not shown:
                self._hint.setText("No matches.")
            else:
                self._hint.setText("")
            self._hint.setVisible(bool(self._hint.text()))
            # An empty table with only its headers reads as a loading screen
            self.table.setVisible(total > 0)
            self._filter.setVisible(total > 0)
        self._update_bulk_buttons()

    def _visible_items(self) -> list[dict[str, Any]]:
        """The rows the filter currently shows — what a bulk undo would take."""
        needle = self._filter.text().lower()
        if not needle:
            return list(self._row_items)
        return [item for item in self._row_items if needle in item["_search"]]

    def _selected_rows(self) -> list[int]:
        rows: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                rows.append(row)
        return rows

    def _update_bulk_buttons(self) -> None:
        selected = len(self._selected_rows())
        self._undo_selected.setEnabled(selected > 0)
        self._undo_selected.setText(
            f"Show {_count(selected, 'pair')}" if selected else "Show selected"
        )
        shown = len(self._visible_items())
        self._undo_shown.setEnabled(shown > 0)
        # "Show all 1 pair…" reads as a choice; "Show all 1 pairs…" reads
        # as a bug, and this dialog can hold exactly one row
        self._undo_shown.setText(f"Show all {_count(shown, 'pair')}…" if shown else "Show all…")

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        row = item.row()
        if item.checkState() == Qt.CheckState.Checked:
            tint = QColor(token("accent"))
            tint.setAlpha(34)
        else:
            tint = None
        for col in range(1, 5):
            cell = self.table.item(row, col)
            if cell is not None:
                cell.setData(Qt.ItemDataRole.BackgroundRole, tint)
        self._update_bulk_buttons()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.table and event.type() == QEvent.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            row, column = self.table.currentRow(), self.table.currentColumn()
            if row >= 0 and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # Enter acts on the current cell exactly like a click on it
                self._on_cell_clicked(row, column)
                return True
            if row >= 0 and event.key() == Qt.Key.Key_Space:
                if column == 0:
                    item = self.table.item(row, 0)
                    if item is not None:
                        checked = item.checkState() == Qt.CheckState.Checked
                        item.setCheckState(
                            Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked
                        )
                    return True
                if column == 4:
                    self._on_cell_clicked(row, column)
                    return True
        if watched is self.table.viewport() and event.type() == QEvent.Type.MouseMove:
            assert isinstance(event, QMouseEvent)
            index = self.table.indexAt(event.position().toPoint())
            if index.isValid() and index.column() == 4:
                self.table.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.table.viewport().unsetCursor()
        elif watched is self.table.viewport() and event.type() == QEvent.Type.Leave:
            self.table.viewport().unsetCursor()
        return super().eventFilter(watched, event)

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if not 0 <= row < len(self._row_items):
            return
        if column == 4:
            self._undismiss_many([self._row_items[row]])
        elif column == 3:
            # The pairs of one bulk dismiss share a timestamp, so filtering by
            # it is how one batch is told from another — one click beats typing
            cell = self.table.item(row, 3)
            if cell is not None:
                self._filter.setText(cell.text())

    def _undismiss_selection(self) -> None:
        rows = [r for r in self._selected_rows() if r < len(self._row_items)]
        if rows:
            self._undismiss_many([self._row_items[r] for r in rows])

    def _undismiss_shown(self) -> None:
        items = self._visible_items()
        if not items:
            return
        answer = QMessageBox.question(
            self,
            "Show all shown",
            f"Show {_count(len(items), 'pair')} again?\n\n"
            "They will be reported again by the next scan of these folders.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._undismiss_many(items)

    def _undismiss_many(self, items: list[dict[str, Any]]) -> None:
        """Undo these dismissals with one review rebuild, however many there are."""
        mw = self.main_window
        repo = mw.repository
        withdrawn: list[tuple[str, str, tuple[str, str] | None]] = []
        done: list[dict[str, Any]] = []
        for item in items:
            # Only pairs remain — groups were removed from the model
            if item["kind"] != "pair":
                continue
            key_a, key_b = item["key_a"], item["key_b"]
            # What each dismissal implied has to be read before the row goes:
            # afterwards nothing ties those paths to those contents
            content = mw.content_pair_for_paths(key_a, key_b)
            repo.undismiss_pair(key_a, key_b)
            withdrawn.append((key_a, key_b, content))
            done.append(item)
        repo.commit()
        # Every row is deleted before any content pair is withdrawn, so two
        # dismissals of the same contents both count as gone
        mw.withdraw_dismissals(withdrawn)
        self._drop_rows({id(item) for item in done})

    def _drop_rows(self, dropped: set[int]) -> None:
        rows = [r for r, item in enumerate(self._row_items) if id(item) in dropped]
        if not rows:
            return
        if len(rows) > self._BULK_RELOAD_THRESHOLD:
            self._rebuild()
            return
        for r in sorted(rows, reverse=True):
            self.table.removeRow(r)
            self._row_items.pop(r)
        self._count_chip.set_count(len(self._row_items))
        self._apply_filter(self._filter.text())


class ScanWorker(QThread):
    """Runs ScanService in a background thread with its own repository."""

    progress = Signal(object)  # ProgressEvent
    finished_scan = Signal(dict)

    def __init__(self, config: Config, db_path: Path) -> None:
        super().__init__()
        self.config = config
        self.db_path = db_path
        self.service: ScanService | None = None
        self._cancel_requested = False

    def run(self) -> None:
        repository = DatabaseRepository(self.db_path)
        try:
            self.service = ScanService(
                self.config, repository, progress_callback=self.progress.emit
            )
            # A Stop clicked before run() reached this point used to be lost
            # because cancel() had no service to forward to.
            if self._cancel_requested:
                self.service.cancel()
            result = self.service.scan()
            repository.commit()
            self.finished_scan.emit(result)
        except Exception:
            logger.exception("Scan failed")
            self.finished_scan.emit({"status": "error"})
        finally:
            repository.close()

    def cancel(self) -> None:
        self._cancel_requested = True
        if self.service:
            self.service.cancel()


class MainWindow(QMainWindow):
    """Review browser: exact groups, perceptual groups, pairs, scan tab."""

    # Folder-table columns. Named because a shifted index would silently
    # write a checkbox into the path column (and drop the folder on save).
    _COL_KIND = 0
    _COL_PATH = 1
    _COL_SUB = 2
    _COL_EXCL = 3
    _COL_DEL = 4

    def __init__(self, config: Config, repository: DatabaseRepository, config_path: Path) -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.repository = repository
        self.scan_id = 0
        self.decisions: dict[int, str] = {}
        self._loading_settings = False
        # One file can appear in several pairs — every occurrence is its
        # own FileCard, so map id → all instances
        self.cards: dict[int, list[FileCard]] = {}
        # Result-view state must exist before the first scan: the tabs and
        # toolbars are built at startup, when there is nothing to review
        self.all_pairs: list[dict[str, Any]] = []
        self.files: dict[int, dict[str, Any]] = {}
        self.probes: dict[int, dict[str, Any] | None] = {}
        # Ignore state, filled by reload_data: pairs of paths the user
        # dismissed, the same decisions as pairs of content keys, and the
        # content key of each file in a pair
        self.dismissed: set[tuple[str, str]] = set()
        self.dismissed_content: set[tuple[str, str]] = set()
        self.content_keys: dict[int, str] = {}
        # Files already asked about for a content key, whether or not one
        # came back — see _content_pair_of
        self._content_key_checked: set[int] = set()
        self._pair_boxes: list[Any] = []
        self._pairs_hidden = 0
        # Pending remove marks, as the toolbar's Marked files button counts
        # them — kept here so _sync_review_controls can gate that button
        self._marked_count = 0
        self._result_sort = "match"
        self._result_hide_decided = False
        self.scan_worker: ScanWorker | None = None
        # Scan-log state lives here, not in the widget: the text alone could
        # not be filtered, searched or saved, and the block cap ate the start
        # of a long scan's log. Every row is a record; the widget shows the
        # subset the filters allow.
        self._scan_log_records: list[_LogEntry] = []
        self._log_rendered: list[_LogEntry] = []
        self._log_matching = 0
        self._phase_log: dict[str, Any] | None = None
        self._scan_started = 0.0
        # Progress state belongs to the window, not to the first scan — the
        # bar and the log are built at startup, before any scan exists
        self._last_phase: str | None = None
        self._progress_peak = 0.0
        self._progress_shown = 0.0
        # The last counted event's (phase, done, total): the timer repaints its
        # reading while no event arrives (see _BAR_REFRESH_SECONDS)
        self._bar_counts: tuple[str, int, int] | None = None
        self._bar_refresh_at = 0.0
        # Set by the Stop button and cleared when the scan ends. Between the
        # click and the boundary the engine reaches, the bar would otherwise
        # keep counting and predicting work that is about to be abandoned
        # (`context/scan-progress.md`, "A stop is a state, not a click")
        self._stopping = False
        # Set once the window is closing: handlers that would rebuild pages
        # or emit into deleted widgets return early
        self._closing = False
        # The name on the bar's second line, and when it last changed: see
        # _CURRENT_NAME_SECONDS for why it is not repainted on every event
        self._current_file_text = ""
        self._current_file_at = 0.0
        build = _build_id()
        title = f"Akin Video Finder v{__version__}"
        if build:
            title += f" ({build})"
        self.setWindowTitle(title)
        # Same icon for the title bar, the taskbar and the Alt-Tab switcher;
        # the .ico carries every size so each one is drawn crisp rather than
        # scaled from a single bitmap
        self.setWindowIcon(_app_icon())
        self.resize(1500, 900)

        sheets_dir = Path(config.reporting.output_directory) / "review" / "sheets"
        self.sheet_loader = SheetLoader(
            sheets_dir,
            config.matching.ffmpeg_path,
            # Sheet renders are light decode (single frames) — the seeks
            # parallelize fine across drives, so a larger pool roughly
            # doubles preview throughput on multi-drive libraries
            workers=min(6, config.matching.signature_workers),
        )
        self.sheet_loader.sheet_ready.connect(self._on_sheet_ready)
        self.sheet_loader.sheet_failed.connect(self._on_sheet_failed)
        self._preview_status = QLabel("")
        # The review's status strip is the bottom bar; the native QStatusBar
        # would be a second, empty chrome row below it.
        self.statusBar().setVisible(False)
        # Preview-phase state, set by _on_scan_finished for the sheets the
        # scan renders. Zero outside a scan, so a lazy render resolving on
        # its own can never drive the scan progress bar or steal the tab.
        self._preview_total = 0
        self._rendering_previews = False
        # A completed scan with previews to render chimes when the queue
        # drains, not while the sheets are still being drawn
        self._chime_when_previews_done = False
        self.sheet_loader.pending_changed.connect(self._on_pending_changed)
        # Debounced viewport sweep — requests sheets for cards that are
        # actually on screen. paintEvent alone proved unreliable: Qt can
        # suppress it when children fully cover the widget, leaving cards
        # stuck on 'rendering…' with no request ever reaching the pool
        self._sheet_sweep = QTimer(self)
        self._sheet_sweep.setSingleShot(True)
        self._sheet_sweep.setInterval(80)
        self._sheet_sweep.timeout.connect(self._request_visible_sheets)
        # Slow periodic re-sweep — catches missed triggers (tab shown after
        # the debounce fired, window resized into more columns) and keeps
        # cards from waiting on any single event arriving
        self._sheet_sweep_idle = QTimer(self)
        self._sheet_sweep_idle.setInterval(1500)
        self._sheet_sweep_idle.timeout.connect(self._request_visible_sheets)
        self._sheet_sweep_idle.start()

        central = QWidget()
        central_layout = QVBoxLayout(central)
        # Breathing room between the title bar and the tab strip: flush
        # against the frame the tabs read as part of the title bar
        central_layout.setContentsMargins(0, 8, 0, 0)
        central_layout.setSpacing(0)
        self.tabs = QTabWidget()
        # Full-bleed pages: no pane frame or inner stack margin — each page
        # supplies its own padding
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border:none; padding:0; margin:0; top:0 }"
            "QTabWidget::tab-bar { left:8px }"
            "QTabBar::tab { background:transparent; border:none;"
            f"  color:{token('tab_text')};padding:6px 16px }}"
            f"QTabBar::tab:selected {{ background:{token('surface')};"
            f"  color:{token('text')};font-weight:600;"
            f"  border-bottom:2px solid {token('accent')};"
            "  border-top-left-radius:6px; border-top-right-radius:6px }"
            f"QTabBar::tab:hover:!selected {{ background:{token('overlay')};"
            f"  color:{token('text')} }}"
        )
        central_layout.addWidget(self.tabs, 1)
        for stack in self.tabs.findChildren(QStackedWidget):
            sl = stack.layout()
            if sl is not None:
                sl.setContentsMargins(0, 0, 0, 0)
            # The page background follows the window, not the button role:
            # using Button here is what left a dark strip around a light page.
            stack.setAutoFillBackground(True)
            stack.setBackgroundRole(QPalette.ColorRole.Window)
        # Bottom strip (status / scan / progress) filled by _build_scan_tab
        bottom = QWidget()
        bottom.setAutoFillBackground(True)
        bottom.setBackgroundRole(QPalette.ColorRole.Window)
        self._bottom_layout = QVBoxLayout(bottom)
        self._bottom_layout.setContentsMargins(12, 4, 12, 8)
        self._bottom_layout.setSpacing(6)
        central_layout.addWidget(bottom)
        self.setCentralWidget(central)
        self.settings_tab = self._build_settings_tab()
        self.tabs.addTab(self.settings_tab, "Tools")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._build_scan_tab()
        self.tabs.setCurrentIndex(0)  # land on the Scan home tab
        self.reload_data()
        self._sync_scan_controls()

    def _export_report(self) -> None:
        if not self.scan_id:
            QMessageBox.information(self, "No scan", "Run a scan first.")
            return
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export report",
            str(Path(self.config.reporting.output_directory) / "full-scan-report"),
            "JSON (*.json);;CSV (*.csv)",
        )
        if not path:
            return
        fmt = "csv" if path.lower().endswith(".csv") or "CSV" in selected else "json"
        if not path.lower().endswith((".json", ".csv")):
            path += f".{fmt}"
        from akin_video_finder.reporting.exporter import export_report

        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(
                export_report(self.repository, self.scan_id, fmt, self.config), encoding="utf-8"
            )
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        except Exception as e:
            # An export is a file on disk that either exists or does not; a
            # malformed evidence row raised out of the handler with no
            # message at all, so the click looked ignored
            logger.exception("Report export failed")
            QMessageBox.warning(self, "Export failed", f"{type(e).__name__}: {e}")
            return
        QMessageBox.information(self, "Report exported", path)

    def _show_actions_dialog(self) -> None:
        dialog = ActionsDialog(self)
        dialog.exec()

    def _pair_in_view(self) -> dict[str, Any] | None:
        """The pair whose frame is at the top of the visible review area.

        Opening Compare without this always started at pair 1, so a
        reviewer who had scrolled to a specific pair had to find it again
        by hand. The scroll area is looked up on the Result container
        rather than remembered, because the page is rebuilt on every
        filter change and a stored reference would go stale.
        """
        container = getattr(self, "_result_container", None)
        if container is None:
            return None
        scroll = container.findChild(QScrollArea)
        if scroll is None:
            return None
        content = scroll.widget()
        if content is None:
            return None
        spans: list[tuple[int, int]] = []
        boxes: list[PairBox] = []
        for box in self._pair_boxes:
            if not isinstance(box, PairBox):
                continue
            with contextlib.suppress(RuntimeError):
                top = box.mapTo(content, QPoint(0, 0)).y()
                spans.append((top, top + box.height()))
                boxes.append(box)
        index = _first_visible_box(
            spans, scroll.verticalScrollBar().value(), scroll.viewport().height()
        )
        if index is None:
            return None
        return boxes[index].comparison

    def _show_compare(self) -> None:
        if not self._visible_pairs():
            return
        dialog = CompareDialog(self, start_pair=self._pair_in_view())
        # The dialog applies sort + hide-decided on top — open it only
        # when pairs actually remain
        if dialog.pairs:
            dialog.exec()

    def _show_dismissed_dialog(self) -> None:
        dialog = DismissedDialog(self)
        dialog.exec()
        self.reload_data()

    # ----- data loading -------------------------------------------------

    def reload_data(self) -> None:
        scan = self.repository.get_latest_scan()
        if not scan:
            # The status line already says "No scan yet" — a second message
            # beside it would repeat the same fact.
            # Render the page anyway: an empty database used to leave the
            # Result tab completely blank, which reads as a broken screen
            self.all_pairs = []
            self.dismissed = set()
            self._update_tools_counts()
            self._rebuild_review_tabs()
            self._update_last_scan_summary()
            # The folder count still followed the config, so a list that had
            # just been emptied kept saying "1 selected for next scan"
            self._update_db_status()
            return
        self.scan_id = scan["id"]
        self.decisions = self.repository.get_decisions(self.scan_id)
        self.auto_marks: dict[int, set[int]] = {}
        self.auto_keeps: dict[int, set[int]] = {}
        # Dismissed false-positive pairs and groups — permanent,
        # scan-independent
        self.dismissed = self.repository.get_dismissed_pairs()
        self._update_tools_counts()
        # The same decisions seen through content instead of paths: a pair
        # ignored in one copy of the library must stay ignored in the next
        # copy of it, where the paths differ but the videos do not
        self.dismissed_content = self.repository.get_dismissed_content_pairs()
        configured_keys = {get_path_key(r.path) for r in self.config.roots}
        self.files = {
            f["id"]: f
            for root in self.repository.get_all_roots()
            if root["normalized_path_key"] in configured_keys
            for f in self.repository.get_files_for_root(root["id"])
        }

        # Only pairs the matcher actually compared. A transitive link
        # (A–B and B–C) does not invent A–C.
        candidates = [
            c
            for c in self.repository.get_pair_comparisons(self.scan_id)
            if c["classification"] in ("POSSIBLE", "VERY_LIKELY", "EXACT")
            and c["file_a_id"] in self.files
            and c["file_b_id"] in self.files
            and self.files[c["file_a_id"]].get("presence", "present") == "present"
            and self.files[c["file_b_id"]].get("presence", "present") == "present"
        ]
        # Content keys are only worth reading when some dismissal was made
        # for contents these files might be copies of
        pair_ids = {fid for c in candidates for fid in (c["file_a_id"], c["file_b_id"])}
        self.content_keys = (
            self.repository.get_content_keys(pair_ids) if self.dismissed_content else {}
        )
        self._content_key_checked = set(self.content_keys)
        self.all_pairs = [c for c in candidates if not self._pair_dismissed(c)]
        self.all_pairs.sort(key=lambda c: -(c["match_fraction"] or 0))
        self._complete_decided_pairs()
        # Kept unfiltered so a dismissal change can re-filter without
        # re-reading the scan — see reload_dismissals
        self._candidates = candidates

        # Probe metadata is only ever read for files that appear in a pair
        # (_duration, card metrics). It used to be loaded for the whole
        # inventory — one query per file at startup and after every reload,
        # nearly all of it never looked at.
        pair_file_ids = {fid for c in self.all_pairs for fid in (c["file_a_id"], c["file_b_id"])}
        self.probes = {fid: self.repository.get_probe_metadata(fid) for fid in pair_file_ids}

        self.cards = {}
        self._rebuild_review_tabs()
        self._update_db_status()
        self._update_last_scan_summary()
        # No preview work here. Sheets are rendered by the scan itself
        # (_on_scan_finished), so a restart or a reload finds them cached;
        # anything still missing is fetched by the card that needs it
        # rather than a background batch the review may never scroll to.

    def reload_dismissals(self) -> None:
        """Re-filter the pairs after a dismissal change, not the whole scan.

        Ignoring or un-ignoring a pair cannot change which files the scan
        found or how it compared them, only which pairs the review shows —
        so this re-reads the two dismissal sets and re-filters the candidate
        list the last ``reload_data`` kept. On the reference library that is
        about 0.1 s per burst of clicks instead of 0.23 s, and the pause was
        the thing that made un-ignoring a few hundred pairs a chore.
        """
        if getattr(self, "_candidates", None) is None:
            # Nothing has been loaded yet (or the database has no scan), so
            # there is no candidate list to re-filter
            self.reload_data()
            return
        self.dismissed = self.repository.get_dismissed_pairs()
        # Content keys are only worth reading when some dismissal was made
        # for contents these files might be copies of
        self.dismissed_content = self.repository.get_dismissed_content_pairs()
        pair_ids = {fid for c in self._candidates for fid in (c["file_a_id"], c["file_b_id"])}
        self.content_keys = (
            self.repository.get_content_keys(pair_ids) if self.dismissed_content else {}
        )
        self._content_key_checked = set(self.content_keys)
        self.all_pairs = [c for c in self._candidates if not self._pair_dismissed(c)]
        self.all_pairs.sort(key=lambda c: -(c["match_fraction"] or 0))
        self._complete_decided_pairs()
        self._update_tools_counts()
        self._rebuild_review_tabs()
        self._update_last_scan_summary()

    def _update_db_status(self) -> None:
        """Say what the database holds — and how it differs from the list."""
        files = self.repository.get_file_count()
        known = len(self.repository.get_all_roots())
        selected = len(self.config.roots)
        text = (
            f"{_count(files, 'file')} from {_count(known, 'folder')} · "
            f"{_count(selected, 'folder')} selected for the next scan"
        )
        self.db_status.setText(text)
        self.db_status.setToolTip(
            "Folders scanned counts every folder that has been scanned into "
            "this database. Removing a folder from the list stops it being "
            "scanned; its files stay in the database and are still compared, "
            "but pairs involving them are not shown in the review."
        )

    def _update_last_scan_summary(self) -> None:
        """Say when the last scan ran, what it found, and whether it still holds.

        This is the idle content of the one status line. The freshness note
        sits on the same line's right side, marked by a small info dot; a
        transient message takes that side first and the note returns when the
        message clears.
        """
        label = getattr(self, "_last_scan_label", None)
        if label is None:
            return
        notice = getattr(self, "_last_scan_notice", None)
        scan = self.repository.get_latest_scan()
        if not scan:
            label.setToolTip("")
            label.setText("No scan yet — run a scan to find duplicates.")
            self._set_stale_note_visible(False)
            return
        counts = self.repository.get_comparison_counts(scan["id"])
        pairs = sum(counts.get(k, 0) for k in ("EXACT", "VERY_LIKELY", "POSSIBLE"))
        files = scan.get("present_files") or scan.get("total_files") or 0
        status = str(scan.get("status") or "")
        age = f"Last scan {_relative_age(scan.get('start_time'))}"
        if status and status != ScanStatus.COMPLETED.value:
            age += f" ({status})"
        label.setText(f"{age} · {_count(files, 'file')} · {_count(pairs, 'pair')}")
        if not fingerprint_matches(scan.get("config_fingerprint"), self.config):
            label.setToolTip(
                "The scan folders, file types or matching thresholds have "
                "changed since this scan ran. Run a scan again to refresh "
                "the results."
            )
            message = getattr(self, "status_message_label", None)
            self._set_stale_note_visible(not (message is not None and message.text()))
            if notice is not None:
                notice.setToolTip(label.toolTip())
        else:
            label.setToolTip("")
            self._set_stale_note_visible(False)

    def _duration(self, file_id: int) -> float:
        probe = self.probes.get(file_id)
        return float(probe["container_duration"]) if probe and probe["container_duration"] else 0.0

    def _is_decided(self, file_id: int) -> bool:
        """Is there a keep/remove decision for this file?"""
        return bool(self.decisions.get(file_id))

    def _pair_decided(self, comparison: dict[str, Any]) -> bool:
        """Has the reviewer marked either file of this pair?"""
        return self._is_decided(comparison["file_a_id"]) or self._is_decided(
            comparison["file_b_id"]
        )

    def _content_pair_of(
        self, file_a: int, file_b: int, *, refresh: bool = False
    ) -> tuple[str, str] | None:
        """Content-pair key for two files, reading keys it does not hold yet.

        The key map is read once per reload, and only when a dismissal was
        made for some content — a library with nothing ignored pays nothing.
        A pair met later (a dismissal just written, a mirror copy of a pair
        already ignored) is looked up on demand.

        A file with no signature is asked about once, not once per pair: the
        lookup omits it rather than returning a placeholder, so testing the
        map alone re-queried every such file on every comparison — one query
        per pair, per reload, forever. ``_content_key_checked`` remembers
        what has been asked, and is reseeded by every reload so a file that
        gained a signature in a later scan is looked up again.
        """
        if refresh:
            # Something was just written — trust nothing that was cached
            missing = [fid for fid in (file_a, file_b) if fid not in self.content_keys]
        elif self.dismissed_content:
            missing = [
                fid
                for fid in (file_a, file_b)
                if fid not in self.content_keys and fid not in self._content_key_checked
            ]
        else:
            missing = []
        if missing:
            self.content_keys.update(self.repository.get_content_keys(missing))
            self._content_key_checked.update(missing)
        key_a = self.content_keys.get(file_a)
        key_b = self.content_keys.get(file_b)
        if key_a is None or key_b is None:
            return None  # one of them has no signature yet
        return _pair_key(key_a, key_b)

    def _pair_dismissed(self, comparison: dict[str, Any]) -> bool:
        """Has this pair been ignored — here, or wherever its files also live?

        Ignoring a pair records two paths. The same two videos under a
        second copy of the library (a mirror drive, a backup root) are a
        different pair of paths, and used to come back as if nothing had
        been decided; their content keys match, so the decision travels with
        the videos instead of with the disk they are on.
        """
        a = comparison["file_a_id"]
        b = comparison["file_b_id"]
        paths = _pair_key(
            self.files[a]["normalized_path_key"], self.files[b]["normalized_path_key"]
        )
        if paths in self.dismissed:
            return True
        content = self._content_pair_of(a, b)
        return content is not None and content in self.dismissed_content

    def note_pair_dismissed(self, comparison: dict[str, Any]) -> None:
        """Record a just-written dismissal in the in-memory ignore sets.

        The database is the record; these sets only have to agree with it
        until the next reload, so the pair vanishes from the review the
        moment it is ignored rather than one refresh later — and so does its
        copy in another root.
        """
        a = comparison["file_a_id"]
        b = comparison["file_b_id"]
        self.dismissed.add(
            _pair_key(self.files[a]["normalized_path_key"], self.files[b]["normalized_path_key"])
        )
        content = self._content_pair_of(a, b, refresh=True)
        if content is not None:
            self.dismissed_content.add(content)
        # The Tools page carries the ignored count; an ignore here must move
        # it now, not at the next full reload.
        self._update_tools_counts()

    def content_pair_for_paths(self, key_a: str, key_b: str) -> tuple[str, str] | None:
        """The content pair two dismissed paths imply, or None if unknown.

        The Ignored pairs dialog lists paths, not file ids, so it cannot reach
        ``_content_pair_of``. Read it *before* the dismissal is deleted —
        afterwards there is nothing left tying those two paths to those two
        contents.
        """
        keys = self.repository.get_content_keys_for_path_keys((key_a, key_b))
        content_a = keys.get(key_a)
        content_b = keys.get(key_b)
        if content_a is None or content_b is None:
            return None
        return _pair_key(content_a, content_b)

    def withdraw_dismissal(self, key_a: str, key_b: str, content: tuple[str, str] | None) -> None:
        """Undo one dismissal in memory — see :meth:`withdraw_dismissals`."""
        self.withdraw_dismissals([(key_a, key_b, content)])

    def withdraw_dismissals(self, withdrawn: list[tuple[str, str, tuple[str, str] | None]]) -> None:
        """Undo dismissals in memory, without re-reading the scan.

        A path pair can be dropped outright — its row left the table. Its
        content pair cannot: several dismissed paths resolve to the same two
        contents (the same videos in a mirror root), so it is withdrawn only
        when no remaining dismissal still implies it. When it does go, every
        root's copy of that pair returns at once, which is why the review is
        re-filtered rather than one box re-shown.

        This is what makes un-ignoring cheap: the candidate list, the content
        keys and the probe map are the ones the last reload kept, so the reads
        are one per content pair and nothing else. A batch rebuilds the review
        once rather than once per row — undoing a few thousand dismissals in
        one go is exactly the case the dialog could not do.

        Callers must delete the rows *before* calling this: the content check
        asks what is left in the table, and a row that has not gone yet would
        keep its own content pair alive.
        """
        if getattr(self, "_candidates", None) is None:
            # Nothing loaded yet — fall back to the reload that does it all
            self.reload_dismissals()
            return
        for key_a, key_b, content in withdrawn:
            self.dismissed.discard(tuple(sorted((key_a, key_b))))
            if content is not None and not self.repository.content_pair_still_dismissed(*content):
                self.dismissed_content.discard(content)
        self.all_pairs = [c for c in self._candidates if not self._pair_dismissed(c)]
        self.all_pairs.sort(key=lambda c: -(c["match_fraction"] or 0))
        self._complete_decided_pairs()
        self._update_tools_counts()
        self._rebuild_review_tabs()
        self._update_last_scan_summary()

    def _visible_pairs(self) -> list[dict[str, Any]]:
        """Every pair the last scan compared and did not ignore.

        The length slider is a setting for the next scan, not a filter over
        these rows: moving it must not make pairs the scan already found
        disappear. Keeping one accessor means the review, Compare and the
        scan summary still describe the same list.
        """
        return list(self.all_pairs)

    def _shown_pairs(self) -> list[dict[str, Any]]:
        """Pairs the Result page currently shows — Hide decided alone.

        Kept in one place so the savings figure and the page cannot disagree
        about which pairs they are describing.
        """
        hide_decided = getattr(self, "_result_hide_decided", False)
        return [c for c in self._visible_pairs() if not hide_decided or not self._pair_decided(c)]

    def _pair_keeper(
        self,
        peers: Sequence[tuple[dict[str, Any], dict[str, Any] | None]],
        classification: str | None = None,
    ) -> tuple[int | None, dict[int, list[str]], str | None]:
        """Rank the two files of a single pair.

        Returns the recommended keeper's file id, per-file reasons for the
        copy that lost, and — when the ranking cannot separate the copies —
        a label to show on both cards instead of naming a keeper.

        Ranking uses the same keeper key as the rest of the app, so advice
        stays consistent with the metric chips. A key that ties element for
        element means the app has no basis to prefer either copy, so the id
        is ``None``: the first key in iteration order would present an
        accident of the peer list as a judgement. An ``EXACT`` pair ties by
        definition — the bytes are equal, so whatever the probes say, the
        pair is one file twice and either copy can be kept.
        """
        if len(peers) < 2:
            return None, {}, None
        files = {f["id"]: f for f, _p in peers}
        probes = {f["id"]: p for f, p in peers}
        max_d = max((self._duration(fid) for fid in files), default=0.0)
        keys = {
            fid: _member_key(files[fid], probes[fid], max_d, self.config.keeper) for fid in files
        }
        if classification == CLASS_EXACT:
            return None, {}, "Identical"
        first = next(iter(keys.values()))
        if all(key == first for key in keys.values()):
            return None, {}, "Same evidence"
        keeper_id = max(keys, key=lambda k: keys[k])
        reasons = {
            fid: _describe(files[fid], probes[fid], probes[keeper_id], max_d)
            for fid in files
            if fid != keeper_id
        }
        return keeper_id, reasons, None

    # ----- tab construction ----------------------------------------------

    def _rebuild_review_tabs(self) -> None:
        # Rebuild the Result page: one flat list of pairs, no groups.
        while item := self._result_layout.takeAt(0):
            if widget := item.widget():
                # deleteLater is deferred. Until the event loop runs, the old
                # page is still a visible child at its last geometry, so a
                # narrow window can show both toolbars side by side.
                widget.hide()
                widget.deleteLater()

        self._pair_boxes = []
        self._pair_headers: list[QLabel] = []
        self._last_ignore: tuple[str, str, tuple[str, str] | None] | None = None
        # Card instances are rebuilt below — dead wrappers would raise
        # RuntimeError in sheet/decision updates and kill the whole
        # signal loop for their file id
        self.cards = {}

        sort_key = getattr(self, "_result_sort", "match")
        hide_decided = getattr(self, "_result_hide_decided", False)

        container = FitWidthWidget()
        layout = QVBoxLayout(container)
        # The mockup insets the pair cards from the page edge and keeps one
        # card's gap to the next; without this the header band ran edge to
        # edge and read as a second toolbar rather than a card.
        layout.setContentsMargins(12, 10, 12, 14)
        layout.setSpacing(10)
        self._pair_list_layout = layout
        pairs = self._shown_pairs()
        if sort_key == "size":
            pairs.sort(
                key=lambda c: (
                    -(
                        self.files[c["file_a_id"]].get("size", 0)
                        + self.files[c["file_b_id"]].get("size", 0)
                    )
                )
            )
        self._pairs_hidden = len(self.all_pairs) - len(pairs)
        if not pairs:
            layout.addWidget(self._empty_result_hint())
        self._add_pair_boxes(layout, pairs)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        # Toolbar above the scroll area holds review actions
        page = QWidget()
        # One subtle outline per card; the pair frame is the outer rounded
        # container and the two file cards sit inside it with their own fill.
        page.setStyleSheet(
            "QGroupBox { border:1px solid " + token("divider") + "; background:"
            f"{token('overlay')};"
            " border-radius:9px; margin-top:0; padding:0 }"
            "QScrollBar:vertical { width:8px; background:transparent; margin:0 }"
            f"QScrollBar::handle:vertical {{ background:{token('border')}; min-height:30px;"
            "  border-radius:4px }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            "  height:0; border:none }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            "  background:transparent }"
        )
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QVBoxLayout()
        toolbar.setContentsMargins(10, 8, 8, 6)
        toolbar.setSpacing(6)
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        sort_label = QLabel("Sort by")
        sort_label.setFont(font_px(11, 400))
        sort_label.setStyleSheet(f"color:{token('text_muted')}")
        top_row.addWidget(sort_label)
        self._sort_combo = QComboBox()
        self._sort_combo.addItem("Best match", "match")
        self._sort_combo.addItem("Largest files", "size")
        # The visible label is the control's name: a buddy also gives the
        # combo an accessible name, so it is not announced as a bare
        # "Best match, combo box" with no setting attached to it.
        sort_label.setBuddy(self._sort_combo)
        self._sort_combo.setAccessibleName("Sort by")
        self._sort_combo.setAccessibleDescription("Orders the pairs by confidence or by size")
        self._sort_combo.setStyleSheet(
            f"QComboBox{{background:{token('surface_raised')};color:{token('text')};"
            f"border:1px solid {token('border')};border-radius:5px;padding:2px 6px;}}"
            f"QComboBox QAbstractItemView{{background:{token('surface_raised')};"
            f"color:{token('text')};selection-background-color:{token('accent')};"
            f"selection-color:{token('accent_text')};}}"
        )
        idx = self._sort_combo.findData(sort_key)
        self._sort_combo.setCurrentIndex(max(0, idx))
        self._sort_combo.currentIndexChanged.connect(self._on_result_view_changed)
        top_row.addWidget(self._sort_combo)
        self._hide_decided_box = QCheckBox("Hide reviewed")
        self._hide_decided_box.setChecked(hide_decided)
        self._hide_decided_box.toggled.connect(self._on_result_view_changed)
        top_row.addWidget(self._hide_decided_box)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet(f"color:{token('divider')};")
        top_row.addWidget(divider)
        self._review_progress_label = QLabel()
        self._review_progress_label.setFont(tabular_font(font_px(11, 400)))
        self._review_progress_label.setStyleSheet(f"color:{token('text_muted')}")
        top_row.addWidget(self._review_progress_label)
        top_row.addStretch(1)
        toolbar.addLayout(top_row)
        self._savings_dot = QLabel()
        self._savings_dot.setFixedSize(6, 6)
        self._savings_dot.setStyleSheet(f"background:{token('success')};border-radius:3px;")
        bottom_row.addWidget(self._savings_dot)
        self._savings_label = QLabel()
        self._savings_label.setFont(tabular_font(font_px(11, 400)))
        self._savings_label.setStyleSheet(f"color:{token('success_text')}")
        self._savings_label.setToolTip(
            "Rough estimate: what deleting the redundant copies would free if "
            "each set of duplicates keeps one copy — the one the app "
            "recommends. Files are counted once, so a film held in four "
            "copies counts three copies, not the six pairs it forms. The low "
            "end of the range counts only the pairs matched at 100 %; the "
            "high end treats every shown pair as a real duplicate."
        )
        bottom_row.addWidget(self._savings_label)
        self._savings_info = QLabel()
        self._savings_info.setPixmap(ui_icon("info", token("text_muted"), 14).pixmap(14, 14))
        self._savings_info.setToolTip(self._savings_label.toolTip())
        bottom_row.addWidget(self._savings_info)
        bottom_row.addStretch(1)
        self._compare_button = QPushButton("Compare pairs")
        self._compare_button.setToolTip(
            "Side-by-side triage — arrow keys to move, 1/2 to keep, I to ignore"
        )
        self._compare_button.clicked.connect(self._show_compare)
        apply_button_role(self._compare_button, "secondary")
        bottom_row.addWidget(self._compare_button)
        self._quarantine_button = QPushButton()
        self._quarantine_button.clicked.connect(self._show_actions_dialog)
        apply_button_role(self._quarantine_button, "secondary")
        bottom_row.addWidget(self._quarantine_button)
        overflow = QToolButton()
        overflow.setText("⋯")
        overflow.setAccessibleName("More review actions")
        overflow.setToolTip("Bulk actions and ignored pairs")
        overflow.setCursor(Qt.CursorShape.PointingHandCursor)
        overflow.setFixedSize(28, 27)
        overflow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        overflow.setArrowType(Qt.ArrowType.NoArrow)
        overflow.setStyleSheet(
            f"QToolButton{{background:transparent;color:{token('text_secondary')};"
            "border:1px solid transparent;border-radius:6px;font-size:16px;padding:0;}"
            f"QToolButton:hover{{background:{token('surface_raised')};color:{token('text')};}}"
            f"QToolButton:focus{{border:2px solid {token('focus')};}}"
            f"QToolButton::menu-indicator{{image:none;width:0;}}"
        )
        menu = QMenu(overflow)
        menu.setStyleSheet(
            f"QMenu{{background:{token('surface_raised')};color:{token('text')};"
            f"border:1px solid {token('border')};padding:5px;}}"
            "QMenu::item{padding:6px 22px 6px 10px;border-radius:4px;}"
            f"QMenu::item:selected{{background:{token('surface')};}}"
            f"QMenu::separator{{height:1px;background:{token('divider')};margin:4px 6px;}}"
        )
        self._dismiss_all_action = QAction("Ignore all shown…", menu)
        self._dismiss_all_action.setToolTip("Permanently ignore every pair currently shown")
        self._dismiss_all_action.triggered.connect(self._dismiss_all_shown)
        menu.addAction(self._dismiss_all_action)
        menu.addSeparator()
        ignored = menu.addAction("Review ignored pairs…")
        ignored.triggered.connect(self._show_dismissed_dialog)
        overflow.setMenu(menu)
        bottom_row.addWidget(overflow)
        toolbar.addLayout(bottom_row)
        page_layout.addLayout(toolbar)
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setTextVisible(False)
        progress.setFixedHeight(2)
        progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        progress.setStyleSheet(
            "QProgressBar{background:"
            f"{token('overlay')};"
            "border:none;border-radius:1px;}"
            f"QProgressBar::chunk{{background:{token('accent')};border-radius:1px;}}"
        )
        page_layout.addWidget(progress)
        page_layout.addWidget(scroll, 1)
        self._result_layout.addWidget(page)
        self._review_progress = progress
        self._review_toast = ReviewToast(page)
        self._review_toast.undo_requested.connect(self._undo_last_ignore)
        undo_shortcut = QShortcut(QKeySequence(Qt.Key.Key_U), page)
        undo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        undo_shortcut.activated.connect(self._undo_last_ignore)
        self._undo_shortcut = undo_shortcut
        self._update_quarantine_button()
        self._update_savings(pairs)
        self._update_review_progress()
        self._update_results_tab_text()
        scroll.verticalScrollBar().valueChanged.connect(lambda _v: self._sheet_sweep.start())
        self._sheet_sweep.start()

    def _empty_result_hint(self) -> QWidget:
        """Explain an empty Result page and give the way back.

        The page used to answer every empty case but the genuine first run
        with "Nothing to review — run a scan", including after a scan that
        found nothing, after every pair was ignored, and when "Hide reviewed"
        hid them all. Each case now names what is actually empty and offers
        the action that changes it — otherwise the reviewer is left
        re-running a finished scan.
        """
        candidates = getattr(self, "_candidates", [])
        if not self.scan_id:
            state = EmptyState(
                "magnifier",
                "Nothing to review",
                "Run a scan to look for duplicate videos in your folders.",
            )
            state.add_action("Go to Scan", "primary", self._go_to_scan)
            return state
        if not candidates:
            state = EmptyState(
                "magnifier",
                "No duplicates found",
                f"{_count(len(self.files), 'file')} checked — nothing looks like another copy.",
            )
            state.add_action("Go to Scan", "ghost", self._go_to_scan)
            return state
        if not self.all_pairs:
            ignored = len(candidates)
            title = "1 pair is ignored" if ignored == 1 else f"All {ignored:,} pairs are ignored"
            state = EmptyState(
                "magnifier",
                title,
                "Ignored pairs stay out of this scan and future scans. "
                "Bring one back from the Ignored pairs list.",
            )
            state.add_action("Review ignored pairs", "secondary", self._show_dismissed_dialog)
            state.add_action("Go to Scan", "ghost", self._go_to_scan)
            return state
        visible = self._visible_pairs()
        body = (
            f"All {_count(len(visible), 'pair')} are hidden by the current filters. "
            "Show reviewed pairs to bring them back."
        )
        state = EmptyState("magnifier", "No pairs shown", body)
        state.add_action("Show reviewed pairs", "secondary", self._show_all_pairs)
        state.add_action("Go to Scan", "ghost", self._go_to_scan)
        return state

    def _go_to_scan(self) -> None:
        self.tabs.setCurrentWidget(self._scan_tab)

    def _show_all_pairs(self) -> None:
        if getattr(self, "_hide_decided_box", None) is not None:
            self._hide_decided_box.setChecked(False)

    def _request_visible_sheets(self) -> None:
        """Request sheets for cards intersecting the visible viewport."""
        if self.tabs.currentWidget() is not self._result_container:
            return
        sa = self._result_container.findChild(QScrollArea)
        if sa is None:
            return
        content = sa.widget()
        if content is None:
            return
        area = QRect(
            QPoint(sa.horizontalScrollBar().value(), sa.verticalScrollBar().value()),
            sa.viewport().size(),
        )
        asked = 0
        for card in content.findChildren(FileCard):
            if card._sheet_requested:
                continue
            r = QRect(card.mapTo(content, QPoint(0, 0)), card.size())
            if r.intersects(area):
                card.ensure_sheet()
                asked += 1
        if asked:
            logger.debug(f"sweep requested {asked} visible sheets")

    def _prefetch_result_sheets(self) -> None:
        """Queue sheet renders for every file appearing in results."""
        ids: set[int] = set()
        for c in self.all_pairs:
            ids.update((c["file_a_id"], c["file_b_id"]))
        for fid in ids:
            f = self.files.get(fid)
            if f:
                self.sheet_loader.request(f, self._duration(fid), priority=2)

    def schedule_result_reload(self) -> None:
        """Coalesce repeated un-ignores into one reload — the dialog
        stays instant per click, the Result rebuild happens once.

        The reload is the dismissal-only one: neither call site can have
        changed a file or a comparison, and re-reading those was most of
        the pause between clicks.
        """
        if getattr(self, "_reload_scheduled", False):
            return
        self._reload_scheduled = True
        QTimer.singleShot(300, self._run_scheduled_reload)

    def _run_scheduled_reload(self) -> None:
        self._reload_scheduled = False
        self.reload_dismissals()

    def _on_result_view_changed(self) -> None:
        self._result_sort = self._sort_combo.currentData()
        self._result_hide_decided = self._hide_decided_box.isChecked()
        self._rebuild_review_tabs()

    def _update_review_progress(self) -> None:
        """Say how much of the review is decided, without a section header.

        The Results tab already carries the list's name and count, so the
        page's own heading repeated both and cost a third of a pair's height.
        The filter row now carries the progress instead: a quiet "n of m
        reviewed" and a hairline bar. Reviewed means either half of the pair
        has a decision, the same test Hide reviewed uses, so the number and
        the filter cannot disagree about what "reviewed" means.
        """
        label = getattr(self, "_review_progress_label", None)
        if label is None:
            return
        pairs = self._visible_pairs()
        reviewed = sum(1 for c in pairs if self._pair_decided(c))
        total = len(pairs)
        label.setText(f"{reviewed} of {total} reviewed" if total else "No pairs")
        progress = getattr(self, "_review_progress", None)
        if progress is not None:
            progress.setValue(round(reviewed / total * 100) if total else 0)

    def _update_results_tab_text(self) -> None:
        """Say the shown-pair count on the tab itself.

        The count is what the user came for, so an empty review is obvious
        before the tab is opened. Ignoring a pair also updates this: removing
        one pair used to leave the tab reading the pre-ignore total until the
        next full reload.
        """
        result_idx = self.tabs.indexOf(self._result_container)
        if result_idx < 0:
            return
        shown = len(self._pair_boxes)
        self.tabs.setTabText(result_idx, f"Results ({shown})" if shown else "Results")

    def _renumber_pair_headers(self) -> None:
        """Keep "Pair N of M" true after one pair is ignored in place."""
        total = len(self._pair_headers)
        for index, label in enumerate(self._pair_headers, start=1):
            with contextlib.suppress(RuntimeError):
                label.setText(f"Pair {index} of {total}")

    def _refresh_savings(self) -> None:
        """Recompute the total after a pair leaves the list without a rebuild.

        Ignoring a single pair removes its box and nothing else, so the
        toolbar figure used to keep the ignored pair's space until the next
        full reload — the pair vanished and the number stayed. Recomputing it
        is one union-find pass over the pairs the page already holds.
        """
        label = getattr(self, "_savings_label", None)
        if label is None:
            return
        self._update_savings(self._shown_pairs())

    def _duplicate_sets(self, pairs: list[dict[str, Any]]) -> list[set[int]]:
        """Group the files the shown pairs connect into sets of duplicates.

        A film kept in four copies is six pairs, and its four files are one
        set reachable from one another through those pairs. Union-find,
        because a set arrives as a chain (A–B, B–C) and the second pair only
        joins the first's set once the first is followed.
        """
        parent: dict[int, int] = {}

        def find(fid: int) -> int:
            root = fid
            while parent[root] != root:
                root = parent[root]
            while parent[fid] != root:
                parent[fid], fid = root, parent[fid]
            return root

        for c in pairs:
            for fid in (c["file_a_id"], c["file_b_id"]):
                parent.setdefault(fid, fid)
            root_a, root_b = find(c["file_a_id"]), find(c["file_b_id"])
            if root_a != root_b:
                parent[root_a] = root_b
        sets: dict[int, set[int]] = {}
        for fid in parent:
            sets.setdefault(find(fid), set()).add(fid)
        return list(sets.values())

    def _set_savings(self, members: set[int]) -> int:
        """Bytes freed by a set of duplicates that keeps one copy.

        The copy kept is the one the keeper ranking recommends — the same
        advice the cards carry — so the figure and the badge agree. Sorted
        input so an exact tie between two copies always resolves the same
        way; the same library must not produce two different totals.
        """
        if len(members) < 2:
            return 0
        sizes: dict[int, int] = {
            fid: int(self.files.get(fid, {}).get("size", 0)) for fid in members
        }
        max_d = max((self._duration(fid) for fid in members), default=0.0)
        keeper = max(
            sorted(members),
            key=lambda fid: _member_key(
                self.files.get(fid, {}), self.probes.get(fid), max_d, self.config.keeper
            ),
        )
        return sum(sizes.values()) - sizes.get(keeper, 0)

    def _update_savings(self, pairs: list[dict[str, Any]]) -> None:
        """Space recoverable if every set of duplicates keeps one copy.

        A file is counted once, not once per pair: four copies of a film
        form six pairs, and the per-pair sum this replaced claimed the same
        three redundant copies twice. Each set now contributes everything
        except the copy the keeper ranking recommends, so the figure states
        what deleting the redundant copies would actually free. The high end
        treats every shown pair as real; the low end keeps only the 100 %
        matches, so a page of weak candidates cannot state a guess as fact.
        """
        certain = [c for c in pairs if float(c.get("match_fraction") or 0.0) >= 1.0]
        minimum = sum(self._set_savings(group) for group in self._duplicate_sets(certain))
        maximum = sum(self._set_savings(group) for group in self._duplicate_sets(pairs))
        text = _savings_text(minimum, maximum)
        self._savings_label.setText(text)
        # The icon explains a figure; with no figure it was a lone glyph
        # sitting beside an empty label
        info = getattr(self, "_savings_info", None)
        if info is not None:
            info.setVisible(bool(text))
        dot = getattr(self, "_savings_dot", None)
        if dot is not None:
            dot.setVisible(bool(text))

    def _update_quarantine_button(self) -> None:
        # Same rule the dialog's plan uses — one helper, so the toolbar
        # count and the dialog's list cannot disagree. Counting raw
        # decisions here once promised sixteen files the plan refused;
        # see pending_remove_ids for the failure mode.
        present = {
            fid for fid, row in self.files.items() if row.get("presence", "present") == "present"
        }
        marked = len(pending_remove_ids(self.decisions, present))
        self._marked_count = marked
        self._quarantine_button.setText(f"Marked files {marked}" if marked else "Marked files")
        self._update_review_progress()
        self._sync_review_controls()

    def fully_removed_sets(self, only_paths: set[str] | None = None) -> list[set[int]]:
        """Duplicate sets whose every remaining copy is marked for removal.

        The pair invariant now flips an earlier removal to Keep when the
        second copy is marked, so this should be unreachable from the review.
        It still guards decisions written by an older build (or a hand-edited
        database) before the Actions dialog deletes every copy of a video.
        Uses every pair, not the filtered page: hiding reviewed pairs must not
        change whether a set still has a keeper. ``only_paths`` narrows the
        answer to the files the plan will actually touch, so a set whose other
        copy the safety check skipped is not called out as fully removed.
        """
        present = {
            fid for fid, row in self.files.items() if row.get("presence", "present") == "present"
        }
        pending = set(pending_remove_ids(self.decisions, present))
        if only_paths is not None:
            pending = {
                fid
                for fid in pending
                if (self.files.get(fid) or {}).get("original_path") in only_paths
            }
        if not pending:
            return []
        return [
            group
            for group in self._duplicate_sets(self.all_pairs)
            if len(group) > 1 and group <= pending
        ]

    def _sync_review_controls(self) -> None:
        """Gate the review toolbar on what the page actually holds.

        With no pair at all, Sort by, Hide reviewed, Compare pairs and the
        bulk ignore were live controls over an empty list: sorting re-ordered
        nothing and both dialogs opened empty. A filter that hides every pair
        keeps Sort by and Hide reviewed live — they are the way back to the
        pairs the page still holds. Marked files follows its own count: a mark
        can outlive the pair that made it, so its dialog stays reachable while
        the pair list is empty.
        """
        if getattr(self, "_sort_combo", None) is None:
            return
        has_pairs = bool(self.all_pairs)
        has_shown = bool(self._pair_boxes)
        self._sort_combo.setEnabled(has_pairs)
        self._hide_decided_box.setEnabled(has_pairs)
        self._compare_button.setEnabled(has_shown)
        self._dismiss_all_action.setEnabled(has_shown)
        self._dismiss_all_action.setText(
            f"Ignore all shown… ({_count(len(self._pair_boxes), 'pair')})"
            if has_shown
            else "Ignore all shown…"
        )
        self._quarantine_button.setEnabled(self._marked_count > 0)

    def _evidence_sentence(self, comparison: dict[str, Any]) -> str:
        """The one-line evidence summary shown under a pair header."""
        raw = comparison.get("evidence")
        if not raw:
            return ""
        try:
            evidence = json.loads(raw)
        except (TypeError, ValueError):
            return ""
        parts: list[str] = []
        matched = evidence.get("matching_frames")
        compared = evidence.get("frames_compared")
        if matched is not None and compared is not None:
            parts.append(f"{matched} of {compared} sampled frames matched")
        mode = str(evidence.get("mode") or "")
        if mode == "trim-offset-trim":
            parts.append("trimmed copy")
        elif mode == "trim-offset-speed":
            parts.append("speed-adjusted copy")
        elif mode == "offset-probe":
            parts.append("shift detected")
        coverage = min(
            float(evidence.get("coverage_a", 1.0)),
            float(evidence.get("coverage_b", 1.0)),
        )
        if coverage < 0.95:
            parts.append(f"partial fingerprint ({coverage * 100:.0f}%)")
        audio = evidence.get("audio_distance")
        if audio is not None and float(audio) <= self.config.matching.audio_divergence_threshold:
            parts.append("audio similar")
        return " · ".join(parts)

    def _show_pair_details(self, comparison: dict[str, Any]) -> None:
        PairDetailsDialog(self, comparison, self).exec()

    def _add_pair_boxes(self, layout: QVBoxLayout, pairs: list[dict[str, Any]]) -> None:
        floor = self.config.matching.possible_threshold
        for index, c in enumerate(pairs, start=1):
            fa, fb = self.files.get(c["file_a_id"]), self.files.get(c["file_b_id"])
            if not fa or not fb:
                continue
            box = PairBox(c)
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.setSpacing(0)
            header = QWidget()
            header.setObjectName("pairHeader")
            header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            header.setStyleSheet(
                f"QWidget#pairHeader{{background:transparent;"
                f"border-bottom:1px solid {token('divider')};}}"
            )
            top = QHBoxLayout(header)
            top.setContentsMargins(12, 7, 8, 7)
            top.setSpacing(8)
            pair_label = QLabel(f"Pair {index} of {len(pairs)}")
            pair_label.setFont(tabular_font(font_px(11, 400)))
            pair_label.setStyleSheet(f"color:{token('text_faint')}")
            top.addWidget(pair_label)
            self._pair_headers.append(pair_label)
            box.header_label = pair_label
            class_label = _CLASS_LABELS.get(
                str(c.get("classification") or ""),
                str(c.get("classification") or "Match"),
            )
            fraction = float(c.get("match_fraction") or 0.0)
            confidence = _match_color(fraction, floor)
            dot_halo = QLabel()
            dot_halo.setObjectName("confidenceDotHalo")
            dot_halo.setFixedSize(13, 13)
            dot_halo.setStyleSheet(
                "border-radius:6px;background:" + _with_alpha(confidence, 0.13) + ";"
            )
            dot = QLabel(dot_halo)
            dot.setObjectName("confidenceDot")
            dot.setFixedSize(7, 7)
            dot.move(3, 3)
            dot.setStyleSheet("border-radius:3px;background:" + confidence + ";")
            top.addWidget(dot_halo)
            class_text = QLabel(class_label)
            class_text.setFont(font_px(12, 600))
            class_text.setStyleSheet(f"color:{token('text_secondary')}")
            top.addWidget(class_text)
            percentage = QLabel(f"{fraction * 100:.2f}% match")
            percentage.setObjectName("confidencePill")
            percentage.setFont(tabular_font(font_px(10, 600)))
            percentage.setStyleSheet(
                "QLabel{"
                f"background:{_with_alpha(confidence, 0.14)};"
                f"color:{confidence};"
                f"border:1px solid {_with_alpha(confidence, 0.30)};"
                "border-radius:9px;padding:2px 7px;}"
            )
            # The pair bar's 30 px buttons set the row height; left to the
            # layout the label stretched to that height and the 20 px pill
            # looked like a lozenge.  Size it to its own content and centre it.
            percentage.setFixedHeight(20)
            top.addWidget(percentage, 0, Qt.AlignmentFlag.AlignVCenter)
            evidence_text = self._evidence_sentence(c)
            if evidence_text:
                evidence = ElidedLabel(evidence_text)
                evidence.setFont(font_px(11, 400))
                evidence.setStyleSheet(f"color:{token('text_muted')};")
                evidence.setMinimumWidth(120)
                top.addWidget(evidence, 1)
            else:
                top.addStretch(1)
            details = QPushButton("Details")
            details.setToolTip("Show the raw evidence and exact metrics for this pair")
            details.clicked.connect(lambda _c=False, comp=c: self._show_pair_details(comp))
            apply_button_role(details, "ghost")
            top.addWidget(details)
            dismiss = QPushButton("Ignore pair")
            dismiss.setToolTip(
                "Hide this pair permanently — Undo for a few seconds, or bring it "
                "back from Ignored pairs"
            )
            dismiss.clicked.connect(lambda _c=False, comp=c, b=box: self._dismiss_pair(comp, b))
            apply_button_role(dismiss, "secondary")
            top.addWidget(dismiss, 0, Qt.AlignmentFlag.AlignRight)
            box_layout.addWidget(header)
            grid = CardGrid()
            peers = [(fa, self.probes.get(fa["id"])), (fb, self.probes.get(fb["id"]))]
            keeper_id, reasons, tie_label = self._pair_keeper(peers, c.get("classification"))
            for f, probe in peers:
                card = FileCard(
                    self,
                    f,
                    probe,
                    peers,
                    f["id"] == keeper_id,
                    reasons.get(f["id"], []),
                    tie_label,
                )
                self.cards.setdefault(f["id"], []).append(card)
                grid.add_card(card)
            grid_row = QHBoxLayout()
            grid_row.setContentsMargins(10, 10, 10, 10)
            grid_row.addWidget(grid)
            box_layout.addLayout(grid_row)
            layout.addWidget(box)
            self._pair_boxes.append(box)

    def _dismiss_pair(self, comparison: dict[str, Any], box: PairBox) -> None:
        """Permanently mark a possible-match pair as a false positive.

        The write goes in at once and the row leaves the page; the toast is
        the short way back. It is not a deferred write: the row's content pair
        is read before the dismissal so Undo can withdraw the *content* half
        as well as the path row, the same call the Ignored-pairs dialog uses.
        """
        key = tuple(
            sorted(
                (
                    self.files[comparison["file_a_id"]]["normalized_path_key"],
                    self.files[comparison["file_b_id"]]["normalized_path_key"],
                )
            )
        )
        content = self.content_pair_for_paths(key[0], key[1])
        self.repository.dismiss_pair(key[0], key[1])
        self.repository.commit()
        self.note_pair_dismissed(comparison)
        self._last_ignore = (key[0], key[1], content)
        self.all_pairs.remove(comparison)
        self._pair_boxes.remove(box)
        with contextlib.suppress(ValueError, AttributeError):
            self._pair_headers.remove(box.header_label)
        box.hide()
        box.deleteLater()
        # The ignored pair is not hidden by a filter, it is gone. Counting it
        # as hidden made the section say "1 hidden" for a list nobody was
        # filtering, and the tab kept the old total
        self._pairs_hidden = len(self.all_pairs) - len(self._shown_pairs())
        self._update_review_progress()
        self._update_results_tab_text()
        self._renumber_pair_headers()
        # The pair is gone from the page, so the toolbar figure has to follow
        # it now — a full rebuild would refresh it, but only much later
        self._refresh_savings()
        self._sync_review_controls()
        if not self._pair_boxes:
            # The page has no rebuild in flight when the final row leaves.
            # Insert the "all ignored" state into the layout it was built
            # with, so the reviewer is not left looking at an empty page
            # while the toast is the only thing on screen.
            list_layout = getattr(self, "_pair_list_layout", None)
            if list_layout is not None:
                list_layout.insertWidget(0, self._empty_result_hint())
        toast = getattr(self, "_review_toast", None)
        if toast is not None:
            toast.show_message("Pair ignored")

    def _undo_last_ignore(self) -> None:
        """Bring back the pair the toast just dismissed.

        Deletes the dismissal rows before withdrawing them in memory — the
        content-pair check asks what is still stored. The review rebuild this
        causes is the same cheap in-place path the Ignored-pairs dialog uses,
        so the row returns in full rather than as a re-shown dead widget.
        """
        pending = getattr(self, "_last_ignore", None)
        if pending is None:
            return
        key_a, key_b, content = pending
        self._last_ignore = None
        toast = getattr(self, "_review_toast", None)
        if toast is not None:
            toast.hide_toast()
        self.repository.undismiss_pair(key_a, key_b)
        self.repository.commit()
        self.withdraw_dismissal(key_a, key_b, content)

    def _dismiss_all_shown(self) -> None:
        """Permanently ignore every pair currently visible in the Result view.

        Exists for classes of false positive that no signal separates — e.g.
        silent same-parameter generated clips. Clearing them one pair at a
        time is the only other option; this respects the active sort and
        "Hide reviewed".
        """
        hide_decided = getattr(self, "_result_hide_decided", False)
        pairs = [c for c in self._visible_pairs() if not hide_decided or not self._pair_decided(c)]
        if not pairs:
            return
        answer = QMessageBox.question(
            self,
            "Ignore all shown",
            f"Ignore {len(pairs)} pair(s) permanently?\n\n"
            "They will not appear in this scan or any future scan. "
            "You can undo this from Ignored pairs.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        keys = [
            (
                self.files[c["file_a_id"]]["normalized_path_key"],
                self.files[c["file_b_id"]]["normalized_path_key"],
            )
            for c in pairs
            if c["file_a_id"] in self.files and c["file_b_id"] in self.files
        ]
        written = self.repository.dismiss_pairs(keys)
        self.repository.commit()
        logger.info(f"dismissed {written} pair(s) from the Result view")
        self.schedule_result_reload()

    # ----- scan tab --------------------------------------------------------

    def _build_scan_tab(self) -> None:
        tab = QWidget()
        self._scan_tab = tab
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(24, 18, 24, 18)
        page_layout.setSpacing(16)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        self.scan_now_button = QPushButton("Scan now")
        self.scan_now_button.setIcon(ui_icon("magnifier", token("accent_fg"), 16))
        self.scan_now_button.setIconSize(QSize(16, 16))
        apply_button_role(self.scan_now_button, "primary")
        self.scan_now_button.clicked.connect(self._on_scan_toggle)
        layout.addWidget(
            PageHeader(
                "Find duplicate videos",
                "Choose what to scan and how closely to match.",
                self.scan_now_button,
            )
        )

        home = QHBoxLayout()
        home.setSpacing(16)

        left_card = Card(padding=14, spacing=10)
        left = left_card.body
        where_label = QLabel("Folders to scan")
        where_label.setFont(font_px(13, 600))
        where_label.setStyleSheet(f"color:{token('text')}")
        left.addWidget(where_label)
        # The drop target absorbs the spare height so the folder card's bottom
        # sits level with the matching card; its contents stay centred.
        drop = DropZone()
        drop.paths_dropped.connect(self._add_root_paths)
        drop.excludes_dropped.connect(self._add_exclude_paths)
        drop.browse_requested.connect(self._add_root_row)
        left.addWidget(drop, 1)
        self._loading_settings = True
        # Excluded folders live in exclude_patterns as "dir/*"; other
        # (file-glob) patterns in the TOML stay untouched
        self._non_dir_patterns = [p for p in self.config.exclude_patterns if not p.endswith("/*")]
        dir_excludes = [p[:-2] for p in self.config.exclude_patterns if p.endswith("/*")]
        self.folders_table = QTableWidget(
            len(self.config.roots) + len(dir_excludes) + len(self._non_dir_patterns), 5
        )
        self.folders_table.setHorizontalHeaderLabels(
            ["Type", "Folder or pattern", "Subfolders", "Skip", ""]
        )
        header = self.folders_table.horizontalHeader()
        header.setSectionResizeMode(self._COL_KIND, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_PATH, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._COL_SUB, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_EXCL, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._COL_DEL, QHeaderView.ResizeMode.Fixed)
        self.folders_table.setColumnWidth(self._COL_DEL, 32)
        # The default header was nearly as tall as a row; a slimmer strip
        # keeps the titles without pushing the list down
        header.setFixedHeight(20)
        self.folders_table.setItemDelegateForColumn(self._COL_KIND, BadgeDelegate())
        self.folders_table.setItemDelegateForColumn(self._COL_PATH, PatternPathDelegate())
        self.folders_table.setItemDelegateForColumn(self._COL_SUB, CheckDelegate())
        self.folders_table.setItemDelegateForColumn(self._COL_EXCL, CheckDelegate())
        self.folders_table.setItemDelegateForColumn(self._COL_DEL, DeleteCellDelegate())
        # Checkbox headers should sit over their boxes, not float left of them
        for col in (self._COL_SUB, self._COL_EXCL):
            header_item = self.folders_table.horizontalHeaderItem(col)
            if header_item is not None:
                header_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        # The row-number gutter carried no information and stole width from
        # the path; the Type column says what each row actually is
        self.folders_table.verticalHeader().setVisible(False)
        # Elide in the middle: the leaf folder is what identifies the row,
        # and right-elision cut exactly that part first. Word wrap has to be
        # off for elision to win at narrow widths — with it on, the column
        # wrapped instead and every cell read "C:…" (checked at 900 px)
        self.folders_table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.folders_table.setWordWrap(False)
        row = 0
        for root in self.config.roots:
            self._set_folder_row(row, root.path, recursive=root.recursive, excluded=False)
            row += 1
        for path in dir_excludes:
            self._set_folder_row(row, path, recursive=True, excluded=True)
            row += 1
        for pattern in self._non_dir_patterns:
            self._set_pattern_row(row, pattern)
            row += 1
        self.folders_table.itemChanged.connect(self._on_folder_item_changed)
        self.folders_table.cellClicked.connect(self._on_folder_cell_clicked)
        self.folders_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # The table is driven by its checkboxes, not a row selection: a
        # selected row painted a lighter band behind its controls, and even a
        # card-coloured highlight tinted the Type label. Nothing is
        # selectable, so a click never recolours a cell; the Delete key acts
        # on the row clicked last (see _on_folder_cell_clicked).
        self.folders_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._last_folder_row = -1
        self.folders_table.setShowGrid(False)
        # The delete cross washes red under the pointer; hover needs the
        # view's mouse tracking before the delegate sees State_MouseOver
        self.folders_table.setMouseTracking(True)
        # Delete removes the selected rows, the keyboard twin of the row's X
        self._folders_delete_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Delete), self.folders_table
        )
        self._folders_delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._folders_delete_shortcut.activated.connect(self._remove_selected_folder_rows)
        self.folders_table.setStyleSheet(
            f"QTableWidget{{background:transparent;border:1px solid {token('border')};"
            "border-radius:6px;}"
            # Windows 11's style paints a blue hover panel behind any cell
            # whose delegate falls through to the base paint — the path and
            # the blank check cells. The table is click-driven, so a cell
            # must not light up under the pointer.
            "QTableWidget::item,QTableWidget::item:hover,"
            "QTableWidget::item:selected{background:transparent;}"
            f"QHeaderView::section{{background:{token('surface_raised')};"
            f"color:{token('text_muted')};border:none;border-bottom:1px solid "
            f"{token('border')};padding:2px 6px;font-weight:600;}}"
            f"QTableCornerButton::section{{background:{token('surface_raised')};border:none;}}"
        )
        left.addWidget(self.folders_table)
        self._resize_folders_table()
        # Two empty boxes with no explanation read as a broken screen
        self._folders_hint = QLabel("No folders yet — drop one above, or use + Add folder.")
        self._folders_hint.setFont(font_px(11, 400))
        self._folders_hint.setStyleSheet(f"color:{token('text_muted')};padding:4px 2px")
        left.addWidget(self._folders_hint)
        self._refresh_folders_hint()
        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        add = QPushButton("+ Add folder")
        apply_button_role(add, "secondary")
        add.clicked.connect(self._add_root_row)
        add_excl = QPushButton("+ Exclude folder")
        apply_button_role(add_excl, "secondary")
        add_excl.clicked.connect(self._add_exclude_row)
        add_pat = QPushButton("+ Exclude pattern")
        apply_button_role(add_pat, "secondary")
        add_pat.setToolTip(
            "Exclude files or folders by name pattern — e.g. *.tmp skips "
            "files, a folder name skips that folder and its contents"
        )
        add_pat.clicked.connect(self._add_pattern_row)
        folder_row.addWidget(add)
        folder_row.addWidget(add_excl)
        folder_row.addWidget(add_pat)
        folder_row.addStretch(1)
        left.addLayout(folder_row)
        # Library and next-scan scope belong with the folder list, not with
        # the last run: this is the row the selection is made in.
        self.db_status = QLabel()
        self.db_status.setFont(tabular_font(font_px(11, 400)))
        self.db_status.setStyleSheet(f"color:{token('text_muted')};padding:2px 2px 0")
        self.db_status.setWordWrap(True)
        left.addWidget(self.db_status)
        self._loading_settings = False
        home.addWidget(left_card, 3)

        right_card = Card(padding=14, spacing=10)
        right = right_card.body
        what_label = QLabel("How closely to match")
        what_label.setFont(font_px(13, 600))
        what_label.setStyleSheet(f"color:{token('text')}")
        right.addWidget(what_label)
        # A preset sets the strictness slider only. The duration difference
        # limit is a separate gate that applies whatever preset is chosen, so
        # it lives outside the custom block and never dims with it.
        self._profile_radios: dict[int, QRadioButton] = {}
        # Clicking a preset writes its value into the slider; that write must
        # not be mistaken for the user moving the slider, which selects Custom.
        self._selecting_preset = False
        # Each option lives in its own row widget, so Qt's parent-scoped
        # auto-exclusivity cannot see them as one set; an explicit group keeps
        # the four options behaving as radios instead of check boxes.
        self._profile_group = QButtonGroup(self)
        self._profile_group.setExclusive(True)
        for value, title, desc, recommended in (
            (75, "Only near-identical videos", "Fewest results, highest confidence", False),
            (50, "Similar copies", "Balanced preset for most libraries", True),
            (25, "Anything similar", "Most results, more false positives", False),
        ):
            option_row = QWidget()
            row_layout = QVBoxLayout(option_row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            top.setSpacing(6)
            radio = QRadioButton(title)
            radio.setFont(font_px(12, 400))
            self._profile_group.addButton(radio)
            radio.toggled.connect(
                lambda checked, v=value: self._apply_preset(v) if checked else None
            )
            top.addWidget(radio)
            if recommended:
                top.addWidget(Badge("★ Recommended", "recommended"))
            top.addStretch(1)
            row_layout.addLayout(top)
            hint = QLabel(desc)
            hint.setContentsMargins(20, 0, 0, 0)
            hint.setFont(font_px(11, 400))
            hint.setStyleSheet(f"color:{token('text_muted')}")
            row_layout.addWidget(hint)
            self._profile_radios[value] = radio
            right.addWidget(option_row)
        custom_row = QWidget()
        custom_layout = QVBoxLayout(custom_row)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        custom_layout.setSpacing(0)
        self._custom_radio = QRadioButton("Custom")
        self._custom_radio.setFont(font_px(12, 400))
        self._profile_group.addButton(self._custom_radio)
        custom_layout.addWidget(self._custom_radio)
        custom_hint = QLabel("Set strictness yourself.")
        custom_hint.setContentsMargins(20, 0, 0, 0)
        custom_hint.setFont(font_px(11, 400))
        custom_hint.setStyleSheet(f"color:{token('text_muted')}")
        custom_layout.addWidget(custom_hint)
        right.addWidget(custom_row)
        self._custom_panel = QFrame()
        self._custom_panel.setObjectName("customPanel")
        # The indentation ties this control to the Custom radio above it: it
        # is that option's detail, not a sibling card.
        panel_layout = QVBoxLayout(self._custom_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(6)
        custom_wrap = QWidget()
        custom_wrap_layout = QHBoxLayout(custom_wrap)
        custom_wrap_layout.setContentsMargins(20, 0, 0, 0)
        custom_wrap_layout.setSpacing(0)
        custom_wrap_layout.addWidget(self._custom_panel)
        right.addWidget(custom_wrap)
        # The duration difference limit is a section of its own: it applies
        # to the next scan whatever strictness is selected. The rule and the
        # card-title heading below set it apart from the radio group's block.
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background:{token('border')};border:none")
        self._duration_panel = QWidget()
        duration_layout = QVBoxLayout(self._duration_panel)
        duration_layout.setContentsMargins(0, 0, 0, 0)
        duration_layout.setSpacing(6)
        # One section owns the rule and the block, so the rule's gap above,
        # the heading's gap below it and the card's bottom padding all come
        # out the same: the layout's 10 px plus 8 above the rule, 14 under
        # it, and 4 under the block on top of the card's 14 px padding.
        length_section = QWidget()
        length_layout = QVBoxLayout(length_section)
        length_layout.setContentsMargins(0, 8, 0, 4)
        length_layout.setSpacing(14)
        length_layout.addWidget(divider)
        length_layout.addWidget(self._duration_panel)
        right.addWidget(length_section)
        # Disabling the frame stops its controls, but the labels keep the
        # explicit colours set on them and still read as live. The effect
        # dims the whole area so a preset visibly owns the values.
        self._custom_veil = QGraphicsOpacityEffect(self._custom_panel)
        self._custom_panel.setGraphicsEffect(self._custom_veil)
        self._custom_radio.toggled.connect(self._set_custom_area_active)
        self._set_custom_area_active(self._custom_radio.isChecked())
        muted_small = f"color:{token('text_muted')};font-size:11px"

        def _end_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setFont(font_px(10, 400))
            label.setStyleSheet(muted_small)
            return label

        def _value_label() -> QLabel:
            label = QLabel()
            label.setFont(tabular_font(font_px(11, 600)))
            label.setStyleSheet(f"color:{token('text')}")
            return label

        def _header_row(name: str, value: QLabel, control: QWidget) -> QHBoxLayout:
            """The control's name, then what it is set to right beside it."""
            row = QHBoxLayout()
            row.setContentsMargins(0, 4, 0, 0)
            row.setSpacing(8)
            label = QLabel(name)
            label.setFont(font_px(11, 600))
            label.setStyleSheet(f"color:{token('text')}")
            # A slider has no text of its own; the pairing names it for a
            # screen reader instead of leaving it as "slider, 50".
            label.setBuddy(control)
            row.addWidget(label)
            row.addWidget(value)
            row.addStretch(1)
            return row

        def _slider_row(slider: QSlider, left_text: str, right_text: str) -> QHBoxLayout:
            """loose ———●——— strict, or loose ———●——— equal."""
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(_end_label(left_text))
            row.addWidget(slider)
            row.addWidget(_end_label(right_text))
            return row

        self.strictness_slider = QSlider(Qt.Orientation.Horizontal)
        self.strictness_slider.setRange(0, 100)
        self.strictness_slider.setValue(self.config.matching.strictness)
        self.strictness_slider.setToolTip(
            "How alike two videos must be to count as duplicates.\n"
            "Lower reports more variants (and more false positives); higher "
            "reports only near-identical copies."
        )
        self.strictness_slider.setAccessibleName("Strictness")
        self.strictness_slider.setAccessibleDescription(
            "How alike two videos must be to count as duplicates"
        )
        self.strictness_value = _value_label()
        self.strictness_value.setToolTip("Where the slider sits between loose and strict")
        self.strictness_slider.valueChanged.connect(
            lambda v: self.strictness_value.setText(f"{v}%")
        )
        self.strictness_slider.valueChanged.connect(lambda _v: self._autosave_settings())
        panel_layout.addLayout(
            _header_row("Strictness", self.strictness_value, self.strictness_slider)
        )
        panel_layout.addLayout(_slider_row(self.strictness_slider, "Loose", "Strict"))

        # Same loose–strict feel as strictness: the left end allows bigger
        # duration differences, so the slider is inverted. 0% ("Equal") is
        # the strict right end. The gate stops at 25%, and one slider unit is
        # 0.5 percentage points, so the track keeps half-point steps.
        self.duration_slider = QSlider(Qt.Orientation.Horizontal)
        self.duration_slider.setRange(0, 50)
        self.duration_slider.setSingleStep(1)
        self.duration_slider.setInvertedAppearance(True)
        ratio = max(self.config.matching.min_duration_ratio, 0.75)
        # A stored tolerance under 75% (over 25% difference) is outside the
        # slider now. Clamping keeps the gate the label shows and the gate
        # the scan uses the same; the next save writes the new floor.
        if ratio != self.config.matching.min_duration_ratio:
            self.config = replace(
                self.config,
                matching=replace(self.config.matching, min_duration_ratio=ratio),
            )
        self.duration_slider.setValue(round((1.0 - ratio) * 200))
        self.duration_slider.setToolTip("The most the two durations may differ.")
        self.duration_slider.setAccessibleName("Duration difference limit")
        self.duration_slider.setAccessibleDescription(
            "The most the two durations may differ"
        )
        self.duration_value = _value_label()
        self.duration_value.setFont(font_px(12, 400))
        self.duration_value.setStyleSheet(f"color:{token('text')}")
        self.duration_value.setToolTip("The most the two durations may differ")

        def _show_duration(value: int) -> None:
            percent = value / 2
            self.duration_value.setText("Equal" if value == 0 else f"Up to {percent:g}%")

        self.duration_slider.valueChanged.connect(_show_duration)
        self.duration_slider.valueChanged.connect(
            lambda _v: (self._autosave_settings(), self._update_last_scan_summary())  # type: ignore[func-returns-value]
        )
        _show_duration(self.duration_slider.value())
        duration_title = QLabel("Duration difference limit")
        duration_title.setFont(font_px(13, 600))
        duration_title.setStyleSheet(f"color:{token('text')}")
        duration_title.setBuddy(self.duration_slider)
        duration_layout.addWidget(duration_title)
        # The value and the track sit on the Custom hint's left edge, like
        # the strictness control: the heading names the section, the block
        # below it is the section's content.
        duration_body = QVBoxLayout()
        duration_body.setContentsMargins(20, 0, 0, 0)
        duration_body.setSpacing(6)
        duration_body.addWidget(self.duration_value)
        duration_body.addLayout(_slider_row(self.duration_slider, "Loose", "Equal"))
        duration_layout.addLayout(duration_body)
        self._sync_profile_radio()
        # Moving the slider is a custom setting by definition — even when it
        # lands exactly on a preset's value. Selecting a preset is what applies
        # one; a slider-driven radio switch would disable the panel mid-drag.
        self.strictness_slider.valueChanged.connect(self._on_strictness_changed)

        right.addStretch(1)
        home.addWidget(right_card, 2)
        layout.addLayout(home)
        self._update_db_status()

        page_layout.addWidget(content)
        page_layout.addStretch(1)
        scroll.setWidget(page)
        outer.addWidget(scroll)

        # The scan controls live in the global strip, not on the Scan page:
        # primary Scan in the page header, Stop in the running panel.
        self._build_bottom_bar()

        # Scan is the landing tab — home screen first, settings second
        self.tabs.insertTab(0, tab, "Scan")
        # The scan log and Results are top-level tabs so review gets full
        # height; the log sits before Results because a running scan is
        # watched there long before there is anything to review
        self._result_container: QWidget = QWidget()
        self._result_layout = QVBoxLayout(self._result_container)
        self._result_layout.setContentsMargins(0, 0, 0, 0)
        self._log_tab = QWidget()
        log_layout = QVBoxLayout(self._log_tab)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_layout.addLayout(self._build_log_toolbar())
        self.scan_log = _ScanLogView()
        self.scan_log.setReadOnly(True)
        # A fixed-width font is what makes the timestamp and event columns
        # line up — the platform's own fixed font, not a hardcoded family
        # that may not be installed
        log_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        log_font.setPixelSize(12)
        self.scan_log.setFont(log_font)
        # Qt's rich-text engine honours line-height in the document style on
        # platforms that support it; where it does not, the fixed font's own
        # leading is the fallback and the rows stay readable.
        self.scan_log.document().setDefaultStyleSheet("p { line-height: 160%; }")
        # Qt drops the oldest blocks past this — together with the record
        # list this bounds a session-long log without losing the milestones
        self.scan_log.setMaximumBlockCount(_SCAN_LOG_MAX_ROWS)
        self.scan_log.setPlaceholderText("Nothing logged yet — run a scan to start.")
        self.scan_log.setAccessibleName("Scan log")
        self.scan_log.setAccessibleDescription(
            "Click a row that names a file, or press Enter on it, to show it in Explorer"
        )
        # No widget tooltip here: a tooltip on the log is anchored to the whole
        # viewport, so it opened anywhere over the log — blank space included —
        # and stayed put while the pointer rested, covering the rows it was
        # meant to explain. The hand cursor over a clickable row is the
        # affordance.
        self.scan_log.row_activated.connect(self._on_log_row_activated)
        self.scan_log.row_clickable = self._log_row_has_target
        self.scan_log.setFrameShape(QFrame.Shape.NoFrame)
        # The column header lives outside the editor, not in it: a header
        # block inside would make block 0 a row that maps to no record, and
        # every lookup — the click, the cursor shape, the copy — would have
        # to skip it. Outside, block n is still record n.
        border = token("divider")
        self._log_header = QLabel()
        self._log_header.setTextFormat(Qt.TextFormat.RichText)
        self._log_header.setText(_log_header_text().replace(" ", "&nbsp;"))
        self._log_header.setFont(log_font)
        self._log_header.setStyleSheet(
            f"color:{token('text_muted')};border-bottom:1px solid {border};padding-bottom:4px"
        )
        self._log_table = QFrame()
        self._log_table.setObjectName("logTable")
        self._log_table.setStyleSheet(
            f"QFrame#logTable{{border:1px solid {border};border-radius:6px}}"
        )
        table_layout = QVBoxLayout(self._log_table)
        table_layout.setContentsMargins(10, 8, 10, 8)
        table_layout.setSpacing(6)
        table_layout.addWidget(self._log_header)
        table_layout.addWidget(self.scan_log, 1)
        log_layout.addWidget(self._log_table, 1)
        self._log_footer = QLabel()
        self._log_footer.setFont(font_px(11, 400))
        self._log_footer.setStyleSheet(f"color:{token('text_muted')}")
        log_layout.addWidget(self._log_footer)
        self.tabs.insertTab(1, self._log_tab, "Scan log")
        self.tabs.insertTab(2, self._result_container, "Results")

    def _on_tab_changed(self, _index: int | None = None) -> None:
        self._sheet_sweep.start()

    def _build_bottom_bar(self) -> None:
        """The one status line: last run when idle, progress while scanning.

        The strip always says something useful. Idle it carries the last-scan
        summary and, when the saved settings no longer match that scan, the
        freshness note. A running scan swaps in the three-column panel —
        phase and file on the left, thin bar and percent in the middle, Stop
        on the right. Transient messages take the strip's right side and the
        freshness note returns when they clear.
        """
        self._bottom_rule = QFrame()
        self._bottom_rule.setFixedHeight(1)
        self._bottom_rule.setStyleSheet(f"background:{token('divider')};border:none")
        self._bottom_layout.addWidget(self._bottom_rule)

        self._status_strip = QWidget()
        strip = QHBoxLayout(self._status_strip)
        strip.setContentsMargins(0, 2, 0, 0)
        strip.setSpacing(12)
        self._last_scan_label = QLabel()
        self._last_scan_label.setFont(tabular_font(font_px(12, 400)))
        self._last_scan_label.setStyleSheet(f"color:{token('text_secondary')}")
        strip.addWidget(self._last_scan_label)
        strip.addStretch(1)
        self.status_message_label = QLabel("")
        self.status_message_label.setFont(font_px(11, 400))
        self.status_message_label.setStyleSheet(f"color:{token('text_muted')}")
        strip.addWidget(self.status_message_label)
        self._last_scan_dot = QLabel()
        self._last_scan_dot.setFixedSize(6, 6)
        self._last_scan_dot.setStyleSheet(
            f"background:{token('info')};border-radius:3px;border:none"
        )
        self._last_scan_dot.hide()
        strip.addWidget(self._last_scan_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._last_scan_notice = QLabel("Settings changed since this scan")
        self._last_scan_notice.setFont(font_px(11, 400))
        self._last_scan_notice.setStyleSheet(f"color:{token('info')}")
        self._last_scan_notice.hide()
        strip.addWidget(self._last_scan_notice, 0, Qt.AlignmentFlag.AlignVCenter)

        self._progress_panel = QWidget()
        panel = QHBoxLayout(self._progress_panel)
        panel.setContentsMargins(0, 2, 0, 0)
        panel.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(0)
        self.progress_phase_label = QLabel("")
        self.progress_phase_label.setFont(font_px(12, 600))
        self.progress_phase_label.setStyleSheet(f"color:{token('text')}")
        left.addWidget(self.progress_phase_label)
        self.current_file_label = ElidedLabel()
        self.current_file_label.setFont(font_px(11, 400))
        self.current_file_label.setStyleSheet(f"color:{token('text_muted')}")
        self.current_file_label.setVisible(False)
        left.addWidget(self.current_file_label)
        panel.addLayout(left, 2)

        centre = QVBoxLayout()
        centre.setSpacing(2)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(8)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setStyleSheet(
            f"QProgressBar{{background:{token('progress_track')};"
            "border:none;border-radius:3px;}"
            f"QProgressBar::chunk{{background:{token('progress_chunk')};"
            "border-radius:3px;}"
        )
        bar_row.addWidget(self.progress_bar, 1)
        self.progress_percent_label = QLabel("")
        self.progress_percent_label.setFont(tabular_font(font_px(11, 600)))
        self.progress_percent_label.setStyleSheet(f"color:{token('text')}")
        bar_row.addWidget(self.progress_percent_label)
        centre.addLayout(bar_row)
        self.progress_meta_label = QLabel("")
        self.progress_meta_label.setFont(font_px(11, 400))
        self.progress_meta_label.setStyleSheet(f"color:{token('text_muted')}")
        centre.addWidget(self.progress_meta_label)
        panel.addLayout(centre, 5)

        self.scan_button = QPushButton("■ Stop")
        self.scan_button.setFixedWidth(104)
        apply_button_role(self.scan_button, "danger")
        self.scan_button.setToolTip("Stops the scan — everything scanned so far stays cached")
        self.scan_button.clicked.connect(self._on_scan_toggle)
        panel.addWidget(self.scan_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._bottom_layout.addWidget(self._status_strip)
        self._bottom_layout.addWidget(self._progress_panel)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._reset_status_message)
        self._progress_panel.hide()
        self._set_progress_idle()
        self._update_last_scan_summary()

    def _show_status_message(self, text: str, timeout_ms: int = 5000) -> None:
        """Show a transient message in the bottom strip instead of QStatusBar."""
        label = getattr(self, "status_message_label", None)
        if label is None:
            return
        label.setText(text)
        self._set_stale_note_visible(False)
        self._status_timer.start(timeout_ms)

    def _reset_status_message(self) -> None:
        label = getattr(self, "status_message_label", None)
        if label is not None:
            label.setText("")
        # The freshness note belongs to the right side when no message holds
        # it: re-read the latest scan so it comes back if the run is stale.
        self._update_last_scan_summary()

    def _set_stale_note_visible(self, visible: bool) -> None:
        dot = getattr(self, "_last_scan_dot", None)
        if dot is not None:
            dot.setVisible(visible)
        notice = getattr(self, "_last_scan_notice", None)
        if notice is not None:
            notice.setVisible(visible)

    def _resize_folders_table(self) -> None:
        """Size the folder table to its rows, capped so a long list scrolls."""
        table = getattr(self, "folders_table", None)
        if table is None:
            return
        header = table.horizontalHeader().height()
        rows_height = sum(table.rowHeight(row) for row in range(table.rowCount()))
        frame = table.frameWidth() * 2
        # The fitted height is the rows and nothing else: a few spare pixels
        # under the last row left the check cells floating above the body's
        # bottom edge, so their top and bottom margins read unequal.
        height = header + rows_height + frame
        table.setFixedHeight(min(max(height, header + 28), 260))

    def _build_log_toolbar(self) -> QHBoxLayout:
        """Filters and export for the scan log."""
        row = QHBoxLayout()
        self._log_files_box = QCheckBox("File rows")
        self._log_files_box.setToolTip(
            "Show every file and pair as it is processed. Off keeps the "
            "phases, their timings and the totals — a full scan logs tens "
            "of thousands of file lines."
        )
        # On by default. The per-file rows used to be hidden because they
        # were an undifferentiated wall of paths; the rail gives each one
        # the highlighted path of the row above, so they read as that
        # phase's detail. Untick it for the phases and totals alone.
        self._log_files_box.setChecked(True)
        self._log_files_box.toggled.connect(self._render_log)
        row.addWidget(self._log_files_box)
        self._log_errors_box = QCheckBox("Errors only")
        self._log_errors_box.setToolTip("Show warnings and errors only")
        self._log_errors_box.toggled.connect(self._render_log)
        row.addWidget(self._log_errors_box)
        self._log_filter = QLineEdit()
        placeholder = "Filter by path, phase, or message"
        self._log_filter.setPlaceholderText(placeholder)
        # Width the box to its placeholder instead of stretching it: the
        # placeholder is the legend for what it matches. A stretch after the
        # field keeps the actions at the right edge of the row.
        self._log_filter.setFixedWidth(
            self._log_filter.fontMetrics().horizontalAdvance(placeholder) + 44
        )
        self._log_filter.setAccessibleName("Filter scan log")
        self._log_filter.setAccessibleDescription("Narrows the log by path, phase, or message")
        self._log_filter.setClearButtonEnabled(True)
        self._log_filter.textChanged.connect(self._render_log)
        row.addWidget(self._log_filter)
        # The field ends where its legend does; the stretch keeps the actions
        # at the right end of the row, where they sat before the field stretched.
        row.addStretch(1)
        copy = QPushButton("Copy log")
        apply_button_role(copy, "secondary")
        copy.setToolTip("Copy the lines shown here to the clipboard")
        copy.clicked.connect(self._copy_log)
        row.addWidget(copy)
        save = QPushButton("Save log…")
        apply_button_role(save, "secondary")
        save.setToolTip(
            "Save the whole log to a text file — including the lines the filters are hiding"
        )
        save.clicked.connect(self._save_log)
        row.addWidget(save)
        return row

    def _apply_preset(self, value: int) -> None:
        """Write a preset's strictness into the slider without selecting Custom."""
        self._selecting_preset = True
        try:
            self.strictness_slider.setValue(value)
        finally:
            self._selecting_preset = False

    def _on_strictness_changed(self, _value: int) -> None:
        """A moved slider is a custom setting, not a preset selection."""
        if self._selecting_preset:
            return
        self._custom_radio.setChecked(True)

    def _sync_profile_radio(self) -> None:
        """Check the profile radio matching the strictness slider value."""
        value = self.strictness_slider.value()
        radio = self._profile_radios.get(value)
        (radio or self._custom_radio).setChecked(True)
        self.strictness_value.setText(f"{value}%")

    def _set_custom_area_active(self, active: bool) -> None:
        """Hand the custom strictness area to the user only while Custom is picked.

        A preset owns the strictness value, so the area reads as a preview:
        its controls stop responding and the whole panel dims. Length
        difference limit sits in its own section and is never dimmed by this.
        Disabling the frame alone greyed only the slider, whose style reacts
        to the disabled state, while the labels kept their own colours.
        """
        self._custom_panel.setEnabled(active)
        self._custom_veil.setOpacity(1.0 if active else 0.45)

    def _set_folder_row(self, row: int, path: str, *, recursive: bool, excluded: bool) -> None:
        kind = QTableWidgetItem("Excluded" if excluded else "Scan folder")
        kind.setFlags(kind.flags() & ~Qt.ItemFlag.ItemIsEditable)
        kind.setToolTip(
            "Excluded folder — skipped while scanning"
            if excluded
            else "Scan folder — files here are indexed and reviewed"
        )
        folder = QTableWidgetItem(str(Path(path)))
        # The column elides; without the tooltip a truncated path is
        # unrecoverable — the user cannot tell which folder a row is
        folder.setToolTip(str(Path(path)))
        shown = str(Path(path))
        if excluded:
            sub = QTableWidgetItem()
            sub.setFlags(
                sub.flags() & ~Qt.ItemFlag.ItemIsEditable & ~Qt.ItemFlag.ItemIsUserCheckable
            )
            sub.setData(Qt.ItemDataRole.AccessibleTextRole, "Subfolders not applicable")
        else:
            sub = QTableWidgetItem()
            sub.setFlags(sub.flags() & ~Qt.ItemFlag.ItemIsEditable)
            sub.setCheckState(Qt.CheckState.Checked if recursive else Qt.CheckState.Unchecked)
            sub.setData(Qt.ItemDataRole.AccessibleTextRole, "Include subfolders")
        # A check cell has no text to announce; its name says what is
        # checked and the row's folder says which row.
        sub.setData(Qt.ItemDataRole.AccessibleDescriptionRole, shown)
        excl = QTableWidgetItem()
        excl.setFlags(excl.flags() & ~Qt.ItemFlag.ItemIsEditable)
        excl.setCheckState(Qt.CheckState.Checked if excluded else Qt.CheckState.Unchecked)
        excl.setData(Qt.ItemDataRole.AccessibleTextRole, "Skip this folder")
        excl.setData(Qt.ItemDataRole.AccessibleDescriptionRole, shown)
        self.folders_table.setItem(row, self._COL_KIND, kind)
        self.folders_table.setItem(row, self._COL_PATH, folder)
        self.folders_table.setItem(row, self._COL_SUB, sub)
        self.folders_table.setItem(row, self._COL_EXCL, excl)
        self._set_delete_cell(row, shown)
        self.folders_table.setRowHeight(row, 26)
        self._style_folder_row(row)

    def _set_delete_cell(self, row: int, what: str) -> None:
        """The row's own remove action, painted by DeleteCellDelegate."""
        cell = QTableWidgetItem()
        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        cell.setToolTip(f"Remove {what} from the list")
        # A drawn cross has no text; the roles name it for a screen reader
        cell.setData(Qt.ItemDataRole.AccessibleTextRole, "Remove from list")
        cell.setData(Qt.ItemDataRole.AccessibleDescriptionRole, what)
        self.folders_table.setItem(row, self._COL_DEL, cell)

    def _set_pattern_row(self, row: int, pattern: str) -> None:
        """Name-pattern row — the chip and the kind, nothing else to toggle."""
        kind = QTableWidgetItem("Pattern")
        kind.setFlags(kind.flags() & ~Qt.ItemFlag.ItemIsEditable)
        kind.setToolTip("Name pattern — matching files and folders are skipped")
        item = QTableWidgetItem(pattern)
        item.setData(Qt.ItemDataRole.UserRole, "pattern")
        # The chip elides a long pattern; the tooltip carries the whole one
        item.setToolTip(f"{pattern}\nName pattern — matching files and folders are skipped.")
        # Empty placeholders: the toggles do not apply to a pattern (it
        # excludes by definition), and a dash or "Always" said it twice more
        sub = QTableWidgetItem()
        sub.setFlags(sub.flags() & ~Qt.ItemFlag.ItemIsEditable & ~Qt.ItemFlag.ItemIsUserCheckable)
        excl = QTableWidgetItem()
        excl.setFlags(excl.flags() & ~Qt.ItemFlag.ItemIsEditable & ~Qt.ItemFlag.ItemIsUserCheckable)
        self.folders_table.setItem(row, self._COL_KIND, kind)
        self.folders_table.setItem(row, self._COL_PATH, item)
        self.folders_table.setItem(row, self._COL_SUB, sub)
        self.folders_table.setItem(row, self._COL_EXCL, excl)
        self._set_delete_cell(row, pattern)
        self.folders_table.setRowHeight(row, 26)
        self._style_folder_row(row)

    def _row_is_pattern(self, row: int) -> bool:
        item = self.folders_table.item(row, self._COL_PATH)
        return bool(item and item.data(Qt.ItemDataRole.UserRole) == "pattern")

    def _style_folder_row(self, row: int) -> None:
        """Grey excluded rows, disable their subfolder checkbox, sync the kind."""
        if self._row_is_pattern(row):
            # Pattern rows carry no toggles — restyling would turn the
            # "not applicable" dash back into a checkbox
            return
        excl = self.folders_table.item(row, self._COL_EXCL)
        excluded = bool(excl and excl.checkState() == Qt.CheckState.Checked)
        # Skip is what makes a row an exclusion, so the kind follows it live:
        # the label used to keep whatever the row was born as
        kind = self.folders_table.item(row, self._COL_KIND)
        if kind is not None:
            text = "Excluded" if excluded else "Scan folder"
            if kind.text() != text:
                kind.setText(text)
            kind.setToolTip(
                "Excluded folder — skipped while scanning"
                if excluded
                else "Scan folder — files here are indexed and reviewed"
            )
        folder = self.folders_table.item(row, self._COL_PATH)
        sub = self.folders_table.item(row, self._COL_SUB)
        if folder:
            font = folder.font()
            font.setItalic(excluded)
            folder.setFont(font)
            folder.setForeground(QColor(token("text_muted")) if excluded else QColor(token("text")))
        if sub:
            if excluded:
                sub.setFlags(sub.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                # Clear the role, not just the state: a leftover check state
                # makes the platform delegate paint a checkbox in the cell.
                # The cell stays blank — a dash was an "inactive control"
                # that still read as something to click.
                sub.setData(Qt.ItemDataRole.CheckStateRole, None)
                sub.setText("")
                sub.setData(Qt.ItemDataRole.AccessibleTextRole, "Subfolders not applicable")
            else:
                sub.setText("")
                sub.setData(Qt.ItemDataRole.ForegroundRole, None)
                sub.setFlags(sub.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                sub.setCheckState(Qt.CheckState.Checked)
                sub.setData(Qt.ItemDataRole.AccessibleTextRole, "Include subfolders")

    def _on_folder_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == self._COL_KIND:
            # The kind mirrors the Skip cell; its repaint is not a setting
            return
        if item.column() == self._COL_EXCL:
            self._style_folder_row(item.row())
        self._autosave_settings()

    def _on_folder_cell_clicked(self, row: int, column: int) -> None:
        # Selection is off, so Delete needs its own memory of where the user
        # was: the row clicked last is the row the key removes.
        self._last_folder_row = row
        if column == self._COL_DEL:
            self._remove_folder_row(row)

    def _append_folder_row(self, path: str, *, excluded: bool) -> bool:
        # Compare and show the native form so a dropped row and a browsed
        # row of the same folder do not read as two different paths
        shown = str(Path(path))
        existing = {
            item.text()
            for r in range(self.folders_table.rowCount())
            if (item := self.folders_table.item(r, self._COL_PATH)) is not None
        }
        if shown in existing:
            return False
        row = self.folders_table.rowCount()
        self.folders_table.insertRow(row)
        self._set_folder_row(row, shown, recursive=True, excluded=excluded)
        self._resize_folders_table()
        self._refresh_folders_hint()
        return True

    def _refresh_folders_hint(self) -> None:
        """Show the empty-list hint only while there is nothing to list."""
        hint = getattr(self, "_folders_hint", None)
        if hint is not None:
            hint.setVisible(self.folders_table.rowCount() == 0)

    def _add_root_paths(self, paths: list[str]) -> None:
        """Add dropped folders as scan roots, skipping duplicates."""
        self._loading_settings = True
        added = False
        duplicates: list[str] = []
        for path in paths:
            if self._append_folder_row(path, excluded=False):
                added = True
            else:
                duplicates.append(path)
        self._loading_settings = False
        if added:
            self._save_settings(quiet=True)
        if duplicates:
            self._show_duplicate_message(duplicates)

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(16)
        content = QWidget()
        content.setMaximumWidth(900)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(
            PageHeader(
                "Tools",
                "Reports, ignored pairs, and database maintenance.",
            )
        )

        buttons: list[QPushButton] = []

        def action_card(
            icon_name: str,
            title: str,
            description: str,
            button_text: str,
            role: str,
            callback: Any,
            title_extra: QWidget | None = None,
        ) -> Card:
            card = Card(padding=14, spacing=6)
            row = QHBoxLayout()
            row.setSpacing(12)
            icon_label = QLabel()
            icon_label.setPixmap(ui_icon(icon_name, token("text_muted"), 24).pixmap(24, 24))
            icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
            row.addWidget(icon_label)
            text = QVBoxLayout()
            text.setSpacing(2)
            title_row = QHBoxLayout()
            title_row.setSpacing(8)
            title_label = QLabel(title)
            title_label.setFont(font_px(13, 600))
            title_label.setStyleSheet(f"color:{token('text')}")
            title_row.addWidget(title_label)
            if title_extra is not None:
                title_row.addWidget(title_extra)
            title_row.addStretch(1)
            text.addLayout(title_row)
            description_label = WrappedLabel(description)
            description_label.setFont(font_px(11, 400))
            description_label.setStyleSheet(f"color:{token('text_muted')}")
            text.addWidget(description_label)
            row.addLayout(text, 1)
            button = QPushButton(button_text)
            apply_button_role(button, role)
            buttons.append(button)
            button.clicked.connect(callback)
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
            card.body.addLayout(row)
            return card

        layout.addWidget(
            action_card(
                "export",
                "Export report",
                "Save the latest scan as JSON or CSV for a spreadsheet or archive.",
                "Export report…",
                "secondary",
                self._export_report,
            )
        )
        self._ignored_count_chip = CountChip(len(getattr(self, "dismissed", ())))
        layout.addWidget(
            action_card(
                "eye",
                "Ignored pairs",
                "Review pairs you've hidden and show them again. Nothing is deleted from disk.",
                "Review pairs…",
                "secondary",
                self._show_dismissed_dialog,
                title_extra=self._ignored_count_chip,
            )
        )
        self._protected_count_chip = CountChip(len(self.config.actions.protected_paths))
        layout.addWidget(
            action_card(
                "shield",
                "Protected folders",
                "Folders that may be scanned but never moved or deleted by actions.",
                "Edit…",
                "secondary",
                self._edit_protected_folders,
                title_extra=self._protected_count_chip,
            )
        )
        layout.addWidget(
            action_card(
                "trash",
                "Clean up records",
                "Remove database entries for files that no longer exist. No files are touched.",
                "Clean up…",
                "danger",
                self._cleanup_records,
            )
        )
        # One width for all three actions: the cards are identical, so a
        # narrow button on one row read as a different weight of action.
        # The longest label decides it, and every label fits.
        button_width = max(button.sizeHint().width() for button in buttons)
        for button in buttons:
            button.setFixedWidth(button_width)
        layout.addStretch(1)
        outer.addWidget(content, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        return tab

    def _update_tools_counts(self) -> None:
        chip = getattr(self, "_ignored_count_chip", None)
        if chip is not None:
            chip.set_count(len(getattr(self, "dismissed", ())))
        protected_chip = getattr(self, "_protected_count_chip", None)
        if protected_chip is not None:
            protected_chip.set_count(len(self.config.actions.protected_paths))

    def _edit_protected_folders(self) -> None:
        from akin_video_finder.configuration.loader import save_config

        dialog = ProtectedFoldersDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            new_config = replace(
                self.config,
                actions=ActionsConfig(protected_paths=dialog.paths),
            )
            save_config(new_config, self.config_path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.config = new_config
        self._update_tools_counts()
        self._show_status_message("Protected folders saved.", 3000)

    def _cleanup_records(self) -> None:
        """Offer to delete rows whose file is no longer on disk.

        The inventory is keyed by path, so a file deleted outside the app
        leaves a row behind that no scan ever revisits. Whether a row is
        dead is a question only the filesystem can answer — a scan's
        "missing" flag also covers files an exclude pattern hides — so the
        check runs here, when the user asks, and a root that is not
        available is skipped instead of emptied.
        """
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            plan = CleanupService(self.repository).plan()
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Clean up failed", str(e))
            return
        QApplication.restoreOverrideCursor()

        lines = [
            f"Checked {_count(plan.examined, 'stored path')} against the disk.",
            f"Still on disk: {plan.on_disk:,}.",
        ]
        if plan.app_action:
            lines.append(f"Kept, the app moved or deleted them: {plan.app_action:,}.")
        if plan.unavailable_roots:
            lines.append("Skipped, not available: " + ", ".join(plan.unavailable_roots))
        if not plan.candidates:
            QMessageBox.information(self, "Nothing to clean up", "\n".join(lines))
            return
        lines.append(f"Rows to remove: {plan.deletable:,} — their files are not there any more.")
        lines.append(
            "Removing them drops their cached probes, hashes, signatures and pair "
            "history. No file is touched."
        )
        answer = QMessageBox.question(
            self,
            "Clean up records",
            "\n\n".join(lines),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted = CleanupService(self.repository).apply(plan)
        except Exception as e:
            QMessageBox.critical(self, "Clean up failed", str(e))
            return
        self._log(
            f"Cleaned up {_count(deleted, 'record')}: their files are not on disk any more",
            level="summary",
        )
        self.reload_data()

    def _add_root_row(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose scan root")
        if not path:
            return
        self._loading_settings = True
        added = self._append_folder_row(path, excluded=False)
        self._loading_settings = False
        if not added:
            self._show_duplicate_message([path])
            return
        self._save_settings(quiet=True)

    def _remove_folder_row(self, row: int) -> None:
        """Remove one row from its own X; the save follows the list."""
        if not 0 <= row < self.folders_table.rowCount():
            return
        self._last_folder_row = -1
        self.folders_table.removeRow(row)
        self._resize_folders_table()
        self._refresh_folders_hint()
        self._save_settings(quiet=True)

    def _remove_selected_folder_rows(self) -> None:
        """Delete-key path: remove the row clicked last.

        The table no longer selects rows, so ``selectedIndexes`` and
        ``currentRow`` normally come back empty; the click handler keeps
        the row for this key.
        """
        rows = sorted({i.row() for i in self.folders_table.selectedIndexes()}, reverse=True)
        if not rows and self.folders_table.currentRow() >= 0:
            rows = [self.folders_table.currentRow()]
        if not rows and 0 <= self._last_folder_row < self.folders_table.rowCount():
            rows = [self._last_folder_row]
        if not rows:
            return
        self._last_folder_row = -1
        for row in rows:
            self.folders_table.removeRow(row)
        self._resize_folders_table()
        self._refresh_folders_hint()
        self._save_settings(quiet=True)

    def _add_exclude_row(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose folder to exclude")
        if path:
            self._add_exclude_paths([path])

    def _add_exclude_paths(self, paths: list[str]) -> None:
        """Add excluded folders — stored as 'dir/*' exclude_patterns."""
        self._loading_settings = True
        added = False
        duplicates: list[str] = []
        for path in paths:
            if self._append_folder_row(path, excluded=True):
                added = True
            else:
                duplicates.append(path)
        self._loading_settings = False
        if added:
            self._save_settings(quiet=True)
        if duplicates:
            self._show_duplicate_message(duplicates)

    def _add_pattern_row(self) -> None:
        pattern, ok = QInputDialog.getText(
            self,
            "Add name pattern",
            "Pattern matching file or folder names to skip\n(e.g. *.tmp, *_sample.*):",
        )
        pattern = pattern.strip()
        if not ok or not pattern:
            return
        existing = {
            item.text()
            for r in range(self.folders_table.rowCount())
            if (item := self.folders_table.item(r, self._COL_PATH)) is not None
        }
        if pattern in existing:
            # A duplicate used to be dropped without a word, so the dialog
            # closed and nothing in the list changed
            self._show_duplicate_message([pattern])
            return
        self._loading_settings = True
        row = self.folders_table.rowCount()
        self.folders_table.insertRow(row)
        self._set_pattern_row(row, pattern)
        self._resize_folders_table()
        self._loading_settings = False
        self._save_settings(quiet=True)

    def _show_duplicate_message(self, paths: list[str]) -> None:
        """Say that an add did nothing because the entry is already listed."""
        shown = ", ".join(Path(p).name or p for p in paths)
        message = (
            f"Already listed: {shown}"
            if len(paths) == 1
            else f"{_count(len(paths), 'folder')} already listed: {shown}"
        )
        self._show_status_message(message, 5000)

    def _autosave_settings(self) -> None:
        if self._loading_settings:
            return
        self._save_settings(quiet=True)

    def _save_settings(self, quiet: bool = False) -> None:
        from akin_video_finder.configuration.loader import save_config
        from akin_video_finder.configuration.models import RootConfig

        roots = []
        folder_patterns = []
        name_patterns = []
        for row in range(self.folders_table.rowCount()):
            path_item = self.folders_table.item(row, self._COL_PATH)
            excl_item = self.folders_table.item(row, self._COL_EXCL)
            path = path_item.text().strip() if path_item else ""
            if not path:
                continue
            if self._row_is_pattern(row):
                name_patterns.append(path)
                continue
            if excl_item and excl_item.checkState() == Qt.CheckState.Checked:
                # Stored in the config's forward-slash form regardless of
                # the native display form the row uses
                folder_patterns.append(f"{Path(path).as_posix().rstrip('/')}/*")
                continue
            chk_item = self.folders_table.item(row, self._COL_SUB)
            recursive = chk_item.checkState() == Qt.CheckState.Checked if chk_item else True
            # The root is stored exactly as the row shows it, so the list
            # and the config cannot disagree about the same folder
            roots.append(RootConfig(path=path, recursive=recursive))
        self._non_dir_patterns = name_patterns
        exclude_patterns = name_patterns + folder_patterns
        strictness = self.strictness_slider.value()
        matching = self.config.matching
        if strictness != matching.strictness:
            # Slider moved: derive all thresholds from it. When untouched,
            # leave hand-tuned [matching] values alone
            matching = replace(matching, strictness=strictness, **_strictness_fields(strictness))
        # The duration slider is a setting for the next scan: the matcher
        # skips pairs beyond it instead of comparing them. [reporting] keeps
        # a mirror because the loader mirrors them on read (see loader.py).
        min_duration_ratio = 1.0 - self.duration_slider.value() / 200.0
        matching = replace(matching, min_duration_ratio=min_duration_ratio)
        try:
            new_config = replace(
                self.config,
                roots=roots,
                exclude_patterns=exclude_patterns,
                matching=matching,
                reporting=replace(
                    self.config.reporting,
                    review_min_duration_ratio=min_duration_ratio,
                ),
                logging=self.config.logging,
            )
            save_config(new_config, self.config_path)
        except (OSError, ValueError) as e:
            if quiet:
                self._show_status_message(f"Settings not saved: {e}", 5000)
            else:
                QMessageBox.warning(self, "Save failed", str(e))
            return
        old_keys = {get_path_key(r.path) for r in self.config.roots}
        new_keys = {get_path_key(r.path) for r in roots}
        self.config = new_config
        if old_keys != new_keys:
            # Root changes affect which files appear in the review tabs
            self.reload_data()
        self._sync_scan_controls()
        if quiet:
            self._show_status_message("Settings saved.", 3000)
        else:
            QMessageBox.information(
                self,
                "Saved",
                f"Settings saved to {self.config_path}\nThey take effect on the next scan.",
            )

    def _on_scan_toggle(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            self._cancel_scan()
        elif getattr(self, "_rendering_previews", False):
            # "Stop" during the preview phase = skip the remaining
            # renders: clearing pending stales every queued job (worker
            # pull → entry gone → skip); in-flight jobs still finish
            self.sheet_loader.pending.clear()
            self._on_pending_changed(0)
        else:
            self._start_scan()

    def _sync_scan_controls(self) -> None:
        """Gate both Scan buttons on whether a folder is selected.

        An empty root list is a real state — a fresh install has no folders
        yet, and the last one may be removed before another is added. Starting
        a scan that walks nothing used to report "no duplicates" for a library
        that was never looked at; the buttons now say what is missing instead.
        """
        self._has_roots = bool(self.config.roots)
        running = bool(self.scan_worker and self.scan_worker.isRunning())
        previewing = bool(getattr(self, "_rendering_previews", False))
        now = getattr(self, "scan_now_button", None)
        if now is not None:
            now.setEnabled(self._has_roots and not running and not previewing)
            now.setToolTip("Add a folder to scan first." if not self._has_roots else "")
        stop = getattr(self, "scan_button", None)
        if stop is not None:
            # While a scan or its preview phase runs this button is the Stop
            # control, so only the "stopping already" state disables it
            stop.setEnabled(self._has_roots and not self._stopping)
            if not self._has_roots:
                stop.setToolTip("Add a folder to scan first.")

    def _set_progress_idle(self) -> None:
        """Return the bottom bar to the last-scan summary and hide the panel."""
        if getattr(self, "_progress_timer", None) is not None:
            self._progress_timer.stop()
        self._stopping = False
        button = getattr(self, "scan_button", None)
        if button is not None:
            button.setText("■ Stop")
        panel = getattr(self, "_progress_panel", None)
        strip = getattr(self, "_status_strip", None)
        if panel is not None:
            panel.hide()
        if strip is not None:
            strip.show()
        bar = getattr(self, "progress_bar", None)
        if bar is not None:
            bar.setMaximum(1000)
            bar.setValue(0)
            bar.setFormat("%p%")
            bar.setVisible(False)
        if getattr(self, "progress_phase_label", None) is not None:
            self.progress_phase_label.setText("")
            self.progress_meta_label.setText("")
            self.progress_percent_label.setText("")
        self._clear_current_file()
        if getattr(self, "current_file_label", None) is not None:
            self.current_file_label.setVisible(False)
        self._sync_scan_controls()

    def _start_scan(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            return
        if not self.config.roots:
            # Nothing to walk: the status strip says what to do instead of
            # letting the scan create a row that reports zero files
            self._show_status_message("Add a folder to scan first.", 5000)
            self._sync_scan_controls()
            return
        self._stopping = False
        self.scan_button.setEnabled(True)
        self.scan_button.setText("■ Stop")
        self.scan_button.setToolTip("Stops the scan — everything scanned so far stays cached")
        self._last_phase = None
        self._progress_peak = 0.0
        self._progress_shown = 0.0
        self._bar_counts = None
        self._bar_refresh_at = 0.0
        if getattr(self, "_status_strip", None) is not None:
            self._status_strip.hide()
        if getattr(self, "_progress_panel", None) is not None:
            self._progress_panel.show()
        self.progress_phase_label.setText("Starting…")
        self.progress_meta_label.setText("")
        self.progress_percent_label.setText("0%")
        self.scan_now_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(1000)  # tenths for smooth motion
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self._clear_current_file()
        self.current_file_label.setVisible(True)
        if not hasattr(self, "_progress_timer"):
            self._progress_timer = QTimer(self)
            self._progress_timer.setInterval(40)
            self._progress_timer.timeout.connect(self._tick_progress)
        self._progress_timer.start()
        self._scan_started = time.monotonic()
        self._phase_log = None
        self._begin_scan_log()
        self.tabs.setCurrentWidget(self._log_tab)
        self.scan_worker = ScanWorker(self.config, Path(self.config.database.path))
        self.scan_worker.progress.connect(self._on_scan_progress)
        self.scan_worker.finished_scan.connect(self._on_scan_finished)
        self.scan_worker.start()

    def _cancel_scan(self) -> None:
        """Ask the engine to stop, and say where the stop will land.

        The cancel token is tested between files, batches and phases, so the
        unit in flight finishes first — the fingerprint of a long 4K film is a
        single ffmpeg run, seconds of work on a warm disk and more on a cold
        one. Until this said so, three things read as the click having been
        ignored — the bar kept counting toward a phase about to be abandoned,
        the button still said "Stop", and a second click added a second line to
        the log (`context/scan-progress.md`, "A stop is a state, not a click").
        """
        if self.scan_worker is None or self._stopping:
            return
        self.scan_worker.cancel()
        self._stopping = True
        self.scan_button.setText("■  Stopping…")
        self.scan_button.setToolTip(
            "Finishing the file in progress — everything scanned so far stays cached"
        )
        self.scan_button.setEnabled(False)
        self._log(
            "Stop requested — the file in progress finishes first",
            level="info",
            stage="scan",
        )

    # ----- scan log --------------------------------------------------------

    def _log(
        self,
        message: str,
        *,
        level: str = "info",
        path: str | None = None,
        stage: str | None = None,
    ) -> None:
        """Record one scan-log row and show it if the filters allow.

        The record is kept whether or not it is visible — flipping the
        Files box on has to reveal the per-file rows of the scan that has
        already run, not just of the next one.
        """
        entry = _LogEntry(
            stamp=datetime.now().strftime(_LOG_STAMP_FORMAT)[:-3],
            # One volume, one spelling: a scan may hand back "e:\vids" and
            # "E:\vids" for the same folder, and a log that shows both looks
            # like it is talking about two disks
            text=_tidy_drive_letters(message),
            level=level,
            path=path,
            stage=stage,
        )
        records = self._scan_log_records
        records.append(entry)
        if len(records) > _SCAN_LOG_MAX_RECORDS:
            excess = len(records) - _SCAN_LOG_MAX_RECORDS
            for dropped in records[:excess]:
                if self._log_matches(dropped):
                    self._log_matching = max(0, self._log_matching - 1)
            del records[:excess]
        if not self._log_matches(entry):
            # Still worth a footer refresh: the hidden count just moved
            self._refresh_log_footer()
            return
        self._log_matching += 1
        if hasattr(self, "scan_log"):
            self._append_log_row(entry)
        self._refresh_log_footer()

    def _log_matches(self, entry: _LogEntry) -> bool:
        """Would this row pass the current filters?"""
        files_box = getattr(self, "_log_files_box", None)
        if entry.level == "detail" and (files_box is None or not files_box.isChecked()):
            return False
        errors_box = getattr(self, "_log_errors_box", None)
        if (
            errors_box is not None
            and errors_box.isChecked()
            and entry.level not in ("warn", "error")
        ):
            return False
        edit = getattr(self, "_log_filter", None)
        needle = edit.text().strip().lower() if edit is not None else ""
        if not needle:
            return True
        haystack = entry.text.lower()
        if entry.stage:
            haystack += " " + _phase_label(entry.stage).lower()
        return needle in haystack

    def _log_row_html(self, entry: _LogEntry) -> str:
        """One row as timestamp + rail/event.

        The stage no longer has a pill column: the rail's node and `├` marks
        already say a row belongs to the phase above it, and a green stage
        pill sitting beside an error read as a success state.  The phase
        label is kept in the entry so the path/phase/message filter can find
        it even though it is not printed.
        """
        stamp = _ui_color(*_LOG_STAMP_COLOR)
        return (
            f'<span style="color:{stamp}">{entry.stamp}</span>'
            f"{_LOG_GAP_HTML}"
            f"{self._log_event_html(entry)}"
        )

    def _log_event_html(self, entry: _LogEntry) -> str:
        """The event column: the level's mark, then the row's text.

        The mockup's three message styles: a banner is the brightest text on
        the row, a metric is the one green thing, and a path is split so its
        folders recede and its file name stands out.
        """
        mark = _LOG_EVENT_MARKS.get(entry.level, "│")
        mark_color = _ui_color(*_LOG_EVENT_MARK_COLORS.get(entry.level, _LOG_MUTED))
        text = _event_text(entry)
        if entry.level == "heading":
            color = _ui_color(*_LOG_MESSAGE_COLORS["banner"])
            body = f'<span style="color:{color}">{_log_html(text)}</span>'
        elif entry.level == "phase":
            color = _ui_color(*_LOG_MESSAGE_COLORS["metric"])
            body = f'<span style="color:{color}">{_log_html(text)}</span>'
        elif entry.level == "detail" and _PATH_START.match(text):
            body = _log_path_html(text)
        elif entry.level == "detail":
            color = _ui_color(*_LOG_MESSAGE_COLORS["text"])
            body = f'<span style="color:{color}">{_log_html(text)}</span>'
        elif entry.level in ("warn", "error"):
            color = _level_color(entry.level)
            body = f'<span style="color:{color}">{_log_html(text)}</span>'
        else:
            color = _ui_color(*_LOG_MESSAGE_COLORS["text"])
            body = f'<span style="color:{color}">{_log_html(text)}</span>'
        return f'<span style="color:{mark_color}">{mark}</span> {body}'

    def _append_log_row(self, entry: _LogEntry) -> None:
        bar = self.scan_log.verticalScrollBar()
        # Someone reading further up must not be yanked back down by rows
        # arriving behind their back
        follow = bar.value() >= bar.maximum() - 4
        kept = bar.value()
        self.scan_log.appendHtml(self._log_row_html(entry))
        self._log_rendered.append(entry)
        if len(self._log_rendered) > _SCAN_LOG_MAX_ROWS:
            del self._log_rendered[: len(self._log_rendered) - _SCAN_LOG_MAX_ROWS]
        if not follow:
            bar.setValue(min(kept, bar.maximum()))

    def _render_log(self, *_args: Any) -> None:
        """Rebuild the visible rows — the filters changed."""
        if not hasattr(self, "scan_log"):
            return
        bar = self.scan_log.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        visible = [e for e in self._scan_log_records if self._log_matches(e)]
        self._log_matching = len(visible)
        self._log_rendered = visible[-_SCAN_LOG_MAX_ROWS:]
        self.scan_log.clear()
        if self._log_rendered:
            # One append for the batch, not one per row: appendHtml parses
            # its argument each time, and 0.12 ms a row turns a filter
            # change into a 2.4 s freeze at 20,000 rows (measured). The
            # <p> wrapper keeps one block per row, so block n is still
            # record n.
            self.scan_log.appendHtml(
                "".join(f"<p>{self._log_row_html(e)}</p>" for e in self._log_rendered)
            )
        bar.setValue(bar.maximum() if follow else min(bar.value(), bar.maximum()))
        self._refresh_log_footer()

    def _refresh_log_footer(self) -> None:
        """Say how much of the log is on screen, and why the rest is not."""
        if not hasattr(self, "_log_footer"):
            return
        total = len(self._scan_log_records)
        matching = self._log_matching
        shown = len(self._log_rendered)
        if not total:
            self._log_footer.setText("")
            return
        filter_text = ""
        if getattr(self, "_log_filter", None) is not None:
            filter_text = self._log_filter.text().strip()
        errors_only = bool(
            getattr(self, "_log_errors_box", None) and self._log_errors_box.isChecked()
        )
        files_hidden = bool(
            getattr(self, "_log_files_box", None) and not self._log_files_box.isChecked()
        )
        if filter_text:
            detail = filter_text
        elif errors_only:
            detail = "Errors only"
        elif files_hidden:
            detail = "File rows hidden"
        else:
            detail = "No filter"
        if matching < total or detail != "No filter" or shown < matching:
            text = f"{shown:,} of {total:,} lines shown · {detail}"
        else:
            text = f"{shown:,} lines shown · No filter"
        # The log refreshes this for every hidden row too — keep it cheap
        if text != self._log_footer.text():
            self._log_footer.setText(text)

    def _log_record_for_block(self, block: int) -> _LogEntry | None:
        # Qt reuses its first block on the first append — the offset keeps
        # block numbers mapped to records either way
        index = block - (self.scan_log.blockCount() - len(self._log_rendered))
        if 0 <= index < len(self._log_rendered):
            return self._log_rendered[index]
        return None

    def _log_row_has_target(self, block: int) -> bool:
        entry = self._log_record_for_block(block)
        return entry is not None and bool(entry.path)

    def _on_log_row_activated(self, block: int) -> None:
        entry = self._log_record_for_block(block)
        if entry is not None and entry.path:
            self._reveal_log_path(entry.path)

    def _reveal_log_path(self, path: str) -> None:
        """Show a logged file — or, when it is gone, the folder it was in."""
        if not _reveal_in_explorer(Path(path)):
            self._show_status_message(f"Not found: {path}", 5000)

    def _log_lines(self, entries: list[_LogEntry]) -> str:
        return "\n".join(f"{e.stamp}{_LOG_GAP}{e.text}" for e in entries)

    def _copy_log(self) -> None:
        QApplication.clipboard().setText(self._log_lines(self._log_rendered))
        self._show_status_message(
            f"Copied {_count(len(self._log_rendered), 'line')} to the clipboard", 3000
        )

    def _save_log(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        default = Path(self.config.reporting.output_directory) / f"scan-log-{stamp}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "Save scan log", str(default), "Text (*.txt)")
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path += ".txt"
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(self._log_lines(self._scan_log_records) + "\n", encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self._show_status_message(f"Scan log saved to {path}", 5000)

    def _begin_scan_log(self) -> None:
        """Start the log over: a second scan opens on its own rows.

        The previous run's lines used to be kept, on the theory that a scan
        which dies and gets restarted is exactly when they matter. What that
        produced was a rescan whose log opened on a page of rows about the
        library as it was *before* the rescan, with the new run's first
        banners somewhere below the fold. The record of a finished scan is its
        report file and "Save log"; this widget is the live view of the scan
        that is running.
        """
        self._scan_log_records.clear()
        self._log_rendered.clear()
        self._log_matching = 0
        if hasattr(self, "scan_log"):
            self.scan_log.clear()
        self._log(_SCAN_STARTED_ROW, level="heading", stage="scan")

    def _close_phase_log(self) -> None:
        """Finish the running phase row: how much it did, how long it took."""
        state = self._phase_log
        self._phase_log = None
        if state is None:
            return
        elapsed = _duration(time.monotonic() - state["started"])
        done = state["done"]
        unit = _PHASE_UNITS.get(state["phase"], "file")
        note = f"{_count(done, unit)} in " if done else ""
        # Same column as the rows above it: two spaces here put "Finding
        # lookalikes  585,728 pairs" four columns left of them
        label = _phase_column(state["phase"])
        self._log(f"{label}{note}{elapsed}", level="phase", stage=state["phase"])

    def _tick_progress(self) -> None:
        """Ease the bar toward the last event target (~25fps) plus a
        slow creep while waiting — motion reads as progress instead of
        discrete jumps."""
        target = self._progress_peak
        if self._progress_shown < target:
            self._progress_shown = min(
                target,
                self._progress_shown + max(0.2, (target - self._progress_shown) * 0.12),
            )
        elif not self._stopping and self._progress_shown < min(97.0, target + 4.0):
            # The creep exists to make a phase that is thinking look alive.
            # Once Stop is requested there is nothing left to keep alive, and
            # a bar still creeping toward a number nobody will reach is the
            # same lie the ETA would be
            self._progress_shown += 0.05
        self.progress_bar.setValue(round(self._progress_shown * 10))
        label = getattr(self, "progress_percent_label", None)
        if label is not None:
            label.setText(f"{self._progress_shown:.0f}%")
        # The reading is checked against the clock, not only when an event
        # arrives: a phase stuck on one slow unit emits none, and its last
        # "~2 s left" must not stay frozen on the bar for as long as the unit
        # takes
        now = time.monotonic()
        if now - self._bar_refresh_at >= _BAR_REFRESH_SECONDS:
            self._bar_refresh_at = now
            self._refresh_bar_text()
        if target >= 100.0 and self._progress_shown >= 100.0:
            self._progress_timer.stop()

    def _on_scan_progress(self, event: ProgressEvent) -> None:
        if self._closing:
            return
        if event.phase != self._last_phase:
            self._close_phase_log()
            self._last_phase = event.phase
            # A phase that names no files — blocking, verifying — would
            # otherwise leave the previous phase's file name on the bar
            self._clear_current_file()
            # Until the new phase's own reading arrives the timer must not
            # repaint the previous phase's numbers
            self._bar_counts = None
            if event.phase in _LOG_SKIP_PHASES:
                self._phase_log = None
            else:
                self._log(_phase_heading(event.phase), level="heading", stage=event.phase)
                opened = time.monotonic()
                self._phase_log = {
                    "phase": event.phase,
                    "started": opened,
                    "start_done": event.completed_count,
                    "done": event.completed_count,
                    # The rate's baseline is the count at the phase's first
                    # event, because the counter opens at the cache's answer;
                    # the progress fields feed the stall guard
                    "last_progress_count": event.completed_count,
                    "last_progress_at": opened,
                    "max_interval": 0.0,
                }
        frac = event.completed_count / event.total_count if event.total_count else 0.0
        overall = (
            _PHASE_STARTS.get(event.phase, self._progress_peak)
            + _PHASE_WEIGHTS.get(event.phase, 0) * frac
        )
        if event.phase == "completed":
            overall = 100.0
        # Monotonic — a phase may emit several counted passes (visual
        # then audio fingerprints) without the bar regressing
        self._progress_peak = max(self._progress_peak, overall)
        if event.total_count:
            self._bar_counts = (event.phase, event.completed_count, event.total_count)
            self.progress_bar.setFormat(f"{self._bar_text(*self._bar_counts)}   %p%")
        else:
            # No counter to repaint: the message (if any) is the whole story
            self._bar_counts = None
            if event.message:
                text = _bar_message(event.message, event.current_root)
                self.progress_bar.setFormat(f"{_phase_label(event.phase)}   {text}   %p%")
        self._update_current_file(event)
        self.progress_phase_label.setText(_phase_label(event.phase))
        if event.total_count:
            self.progress_meta_label.setText(
                self._bar_meta_text(event.phase, event.completed_count, event.total_count)
            )
        elif event.message:
            self.progress_meta_label.setText(_bar_message(event.message, event.current_root))
        else:
            self.progress_meta_label.setText("")
        self._log_progress_row(event)

    def _bar_text(self, phase: str, completed: int, total: int) -> str:
        """What the progress bar says while a phase runs: phase, counts, rate,
        and roughly how long the rest of the phase needs.

        The live reading belongs here rather than in the log. A log row every
        five seconds repeating the bar's own count tripled the log's length
        and gave it nothing you could read it for afterwards; the bar updates
        in place, and the phase's result row carries the totals once it ends.

        Takes the last counted event's numbers rather than the event itself:
        the timer repaints the same reading while no event arrives, and the
        stall guard withdraws the rate and the estimate once the wait is
        longer than the phase's own gaps can explain.

        A request to stop replaces the rate and the estimate with the word
        "stopping": both of them describe work that will not be finished, and
        the counts themselves stay because they are still true.
        """
        pieces = [
            _phase_label(phase),
            f"{completed:,}/{total:,}",
        ]
        if completed < total:
            now = time.monotonic()
            if self._stopping:
                # A rate measures how fast the work goes and an estimate
                # promises when it ends; a stop withdraws both. What is left
                # is a state, and it is the only true thing to say
                pieces.append("stopping")
                return "   ".join(pieces)
            unit = _PHASE_UNITS.get(phase, "file")
            rate = self._phase_rate(completed, now)
            phrase = _rate_text(rate, unit)
            if phrase:
                pieces.append(phrase)
            left = self._phase_eta(completed, total, rate)
            if left:
                pieces.append(left)
        return "   ".join(pieces)

    def _bar_meta_text(self, phase: str, completed: int, total: int) -> str:
        """The progress panel's under-bar line: counts, rate, estimate."""
        pieces = [f"{completed:,} of {total:,}"]
        if completed < total:
            if self._stopping:
                pieces.append("stopping")
                return " · ".join(pieces)
            now = time.monotonic()
            unit = _PHASE_UNITS.get(phase, "file")
            rate = self._phase_rate(completed, now)
            phrase = _rate_text(rate, unit)
            if phrase:
                pieces.append(phrase)
            left = self._phase_eta(completed, total, rate)
            if left:
                pieces.append(left)
        return " · ".join(pieces)

    def _refresh_bar_text(self) -> None:
        """Repaint the bar's reading while a phase sits on one unit.

        Progress events arrive when a unit finishes. A 4K fingerprint or a
        shift probe can run for minutes, and without this the reading the last
        fast units produced — "~2 s left" — stays on the bar long after it
        stopped being true. The same arithmetic run against the clock checks
        the phase's largest observed gap and drops the reading once the wait
        exceeds it, so the bar admits the stall instead of freezing.
        """
        counts = self._bar_counts
        if counts is None:
            return
        phase, completed, total = counts
        if completed >= total:
            return
        self.progress_bar.setFormat(f"{self._bar_text(phase, completed, total)}   %p%")
        if getattr(self, "progress_meta_label", None) is not None:
            self.progress_meta_label.setText(self._bar_meta_text(phase, completed, total))

    def _update_current_file(self, event: ProgressEvent) -> None:
        """Name the file — or both files of a pair — the phase is on right now.

        Names only. On this line a path elides to "…\\clip.mp4", so the folders
        are what the reader loses while the name is what the reader wants; the
        whole path is the tooltip, and the scan log's per-file rows keep it
        too. A pair names both halves, because a comparison is about two files
        and "a.mp4  vs" leaves the obvious question open.
        """
        if not event.current_path:
            return
        names = [Path(event.current_path).name]
        if event.peer_path:
            names.append(Path(event.peer_path).name)
        text = "  vs  ".join(names)
        if text == self._current_file_text:
            return
        now = time.monotonic()
        if now - self._current_file_at < _CURRENT_NAME_SECONDS:
            return
        self._current_file_text = text
        self._current_file_at = now
        self.current_file_label.setText(text)
        # setText above sets the tooltip to the name; the path is what the
        # tooltip is for
        self.current_file_label.setToolTip(
            "\n".join(p for p in (event.current_path, event.peer_path) if p)
        )

    def _clear_current_file(self) -> None:
        """Blank the name line: nothing runs, or the phase names no files."""
        self._current_file_text = ""
        self._current_file_at = 0.0
        label = getattr(self, "current_file_label", None)
        if label is None:
            return
        label.setText("")
        label.setToolTip("")

    def _phase_rate(self, completed: int, now: float) -> float:
        """Units per second over the whole phase, up to its last counter move.

        The average from the phase's first event to its last counter move, not
        a trailing window: a unit here can take minutes, and a window shorter
        than the unit measures it as "one per window" right after it finishes,
        then falls to zero while the next one runs. Stopping the average at
        the last move also keeps a worker pool's bursts out of it: four files
        finishing together is still four files over the time they took, not a
        four-file spike. The baseline is the count at the first event, because
        the counter opens at the cache's answer — the files the cache answered
        before the phase began are not speed.

        A counter that has stood still for longer than the phase's own largest
        observed gap can explain returns 0, which drops the rate and the
        estimate until work resumes; the average would otherwise keep
        promising a finish from a stalled phase.
        """
        state = self._phase_log
        if state is None:
            return 0.0
        if completed > state["last_progress_count"]:
            gap = now - state["last_progress_at"]
            state["max_interval"] = max(state["max_interval"], gap)
            state["last_progress_at"] = now
            state["last_progress_count"] = completed
        if _phase_stalled(state, now):
            return 0.0
        done = completed - state["start_done"]
        span = state["last_progress_at"] - state["started"]
        if done <= 0 or span <= 0:
            return 0.0
        return done / max(0.001, span)

    def _phase_eta(self, completed: int, total: int, rate: float) -> str:
        """Roughly how long the rest of this phase needs, or "" when the number
        would be a guess wearing a unit.

        Silent twice: while the measured span is shorter than
        ``_ETA_MIN_SECONDS``, and when there is no rate to divide by — the
        same condition that leaves the rate off the bar. It deliberately never
        estimates the whole scan: the phase weights behind the percentage are
        typical shares, not measurements, so a stage that skipped most of its
        work (a warm fingerprint cache, a shift-test queue an earlier scan
        drained) would turn a total estimate into a figure wrong by hours,
        while the phase's own remaining work is measured directly.
        """
        state = self._phase_log
        if state is None or rate <= 0.0 or completed >= total:
            return ""
        if state["last_progress_at"] - state["started"] < _ETA_MIN_SECONDS:
            return ""
        if rate * 60.0 < _MIN_RATE_PER_MINUTE:
            return ""
        return _eta_text((total - completed) / rate)

    def _log_progress_row(self, event: ProgressEvent) -> None:
        """Decide what this event contributes to the scan log.

        One row per file is what buried the log: on a 6,000-file library it
        produced tens of thousands of lines and the block cap threw away
        the phase headers that gave them meaning. The rows survive the cap
        now and each one is drawn as its phase's detail, so they are shown
        by default and the Files box hides them when they get too many.
        Nothing else is written while a phase runs — the count and rate are
        the progress bar's job, the phase banner says what is running, and
        its result row says how it went.
        """
        state = self._phase_log
        if state is not None:
            state["done"] = event.completed_count
        prefix = _phase_column(event.phase)
        if event.current_path:
            text = event.current_path
            if event.peer_path:
                # "Comparing videos  A" left the obvious question open —
                # with what? A pair is the unit of work here, so both
                # halves of it are named
                text = f"{text}  vs  {event.peer_path}"
            self._log(prefix + text, level="detail", path=event.current_path, stage=event.phase)
        if state is None or event.phase in _LOG_SKIP_PHASES:
            return
        if not event.total_count and event.message:
            # No counter to report — the message is the whole story
            # ("Scanning root: P:\\Videos", "Processed 6,449 files …")
            self._log(prefix + event.message, stage=event.phase)

    def _on_scan_finished(self, result: dict) -> None:
        if self._closing:
            return
        # Cancel/error paths emit no 'completed' event — without this the
        # timer keeps ticking and the creep fabricates progress past the
        # point where the scan actually died
        if hasattr(self, "_progress_timer"):
            self._progress_timer.stop()
        self._bar_counts = None
        # However the scan ended — finished, stopped, failed — there is no
        # longer a stop in flight, and the CTA goes back to being the CTA
        self._stopping = False
        self.scan_button.setText("▶  Scan")
        self.scan_button.setToolTip("")
        self._sync_scan_controls()
        self._close_phase_log()
        # Reload before summarising: the summary counts the pairs the review
        # will show (dismissed ones never get there), and whether each one
        # already has a decision — neither is knowable before the reload
        self.reload_data()
        self._log_scan_summary(result)
        # Previews are rendered by the scan, not by the review: request
        # every pair file now so the tab opens with finished sheets, and
        # hold the scan open until the queue drains. Only a completed scan
        # renders previews — a cancelled or failed run has no trustworthy
        # pair list, so its results open straight away.
        completed = result.get("status") == ScanStatus.COMPLETED.value
        if completed:
            self._prefetch_result_sheets()
        self._preview_total = len(self.sheet_loader.pending)
        self._rendering_previews = self._preview_total > 0
        if self._rendering_previews:
            self._chime_when_previews_done = completed
            self._log(f"Preview: rendering {self._preview_total} contact sheets")
            self._progress_panel.show()
            self._status_strip.hide()
            self.scan_now_button.setEnabled(False)
            self.progress_phase_label.setText("Rendering previews")
            self.progress_meta_label.setText(f"{self._preview_total:,} contact sheets")
            self.progress_percent_label.setText("0%")
            self.scan_button.setText("■ Stop")
            self.scan_button.setToolTip("Skip the remaining previews")
            self.progress_bar.setMaximum(self._preview_total)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("rendering previews %v/%m")
        else:
            self._chime_when_previews_done = False
            self._set_progress_idle()
            self.tabs.setCurrentWidget(self._result_container)
            if completed:
                self._play_scan_complete_chime()

    def _log_scan_summary(self, result: dict) -> None:
        """Say what the scan did, in the words a user of the app would use.

        Four questions, in the order people ask them: did it finish, what
        was scanned, how much is left for me to review, did anything go
        wrong. Two of those used to be answered with the matcher's own
        tally — "837 possible duplicates" counted pairs the reviewer had
        already decided, and "8,876 of 9,723 pairs turned out not to match"
        answered a question nobody asked. The pair counts come from the
        review's own list now — decisions and the view's filters included —
        and the machinery the table used to print (cache hits, hashes,
        signatures, rejections) sits in one details block for whoever is
        diagnosing rather than reviewing.
        """

        def num(key: str) -> int:
            return int(result.get(key) or 0)

        started = getattr(self, "_scan_started", 0.0)
        verb = {
            ScanStatus.COMPLETED.value: "finished",
            ScanStatus.INTERRUPTED.value: "stopped",
            ScanStatus.FAILED.value: "failed",
        }.get(str(result.get("status") or ""), "ended")
        headline = f"Scan {verb}"
        if started:
            headline += f" in {_duration(time.monotonic() - started)}"
        # Phase styling: the end of a long scan has to be as findable as its
        # beginning, and the summary used to be unformatted text in the noise
        self._log(headline, level="heading", stage="scan")

        checked, missing = num("total_files"), num("missing_files")
        if checked:
            # "Missing" is a file the database knows that is not on disk any
            # more, so a zero is the normal case and says nothing
            tail = f", {missing:,} not found on disk" if missing else ""
            self._log(f"{_count(checked, 'video')} checked{tail}", level="summary", stage="scan")

        pairs = getattr(self, "all_pairs", [])
        # The log and the Result tab count the same list: the length
        # slider gates the next scan, so it no longer hides rows here.
        undecided = [c for c in pairs if not self._pair_decided(c)]
        decided = sum(1 for c in pairs if self._pair_decided(c))
        found = self._pair_class_sentence(pairs)
        parts = self._pair_class_parts(undecided)
        if len(parts) == 1:
            # "23 pairs left to review: 23 possible duplicates" said it twice
            self._log(f"{parts[0]} left to review", level="summary", stage="scan")
        elif parts:
            self._log(
                f"{_count(len(undecided), 'pair')} left to review: {_join_and(parts)}",
                level="summary",
                stage="scan",
            )
        elif pairs:
            self._log("Every pair has a decision", level="summary", stage="scan")
        elif num("exact_pairs") or num("very_likely") or num("possible"):
            # Everything this scan matched was dismissed earlier
            self._log("No pairs left to review", level="summary", stage="scan")
        else:
            self._log("No duplicates found", level="summary", stage="scan")

        # The errors come straight after their count: five detail rows in
        # between made "6 errors" read as a label for the details block
        errors = result.get("errors") or []
        self._log(
            _count(len(errors), "error") if errors else "No errors", level="summary", stage="scan"
        )
        for message in errors or []:
            self._log(
                f"  {message}",
                level="error",
                path=_path_in(message),
                stage=_stage_for_message(message),
            )

        # Everything the old table said, for diagnosing a scan that behaved
        # oddly — behind the Files box, like the per-file rows
        if checked:
            self._log(
                f"  files: {checked:,} found, {num('present_files'):,} present, "
                f"{missing:,} missing",
                level="detail",
                stage="scan",
            )
        self._log(
            f"  reused: {num('cache_hits'):,} probes, {num('hash_cache_hits'):,} hashes, "
            f"{num('signature_cache_hits'):,} signatures, "
            f"{num('fingerprint_cache_hits'):,} fingerprints",
            level="detail",
            stage="scan",
        )
        self._log(
            f"  computed: {num('files_hashed'):,} hashed, {num('files_signed'):,} signed, "
            f"{num('files_fingerprinted'):,} fingerprinted, "
            f"{num('files_head_bagged'):,} head-scanned",
            level="detail",
            stage="scan",
        )
        if pairs or found:
            self._log(
                f"  matches: {found or 'none'} ({decided:,} already decided)",
                level="detail",
                stage="scan",
            )
        self._log(
            f"  pairs: {num('candidate_pairs'):,} candidates, {num('comparisons'):,} compared, "
            f"{num('rejected'):,} rejected",
            level="detail",
            stage="scan",
        )
        # The gate runs before any pair is compared, so its count is missing
        # from every other number here: without this line a scan that skipped
        # half the index's proposals looks like a scan that found less.
        pruned = num("duration_pruned")
        if pruned:
            pct = self.config.matching.min_duration_ratio * 100
            self._log(
                f"  gate: {pruned:,} candidate pairs skipped before comparison "
                f"(shorter copy under {pct:.0f}% of the longer one)",
                level="detail",
                stage="scan",
            )
        # The offset probe is the only phase that decodes 180s from *both*
        # files of a pair, so its line says how much of the candidate set was
        # tested. Pairs an earlier scan already judged are named apart from the
        # ones tested now: "10 of 12" alone cannot tell progress from a repeat.
        probes = num("offset_probes")
        cached = num("offset_probes_cached")
        if probes or cached:
            hits = num("offset_probe_hits")
            rechecked = num("offset_probes_reconfirmed")
            candidates = num("offset_probes_candidates") or probes + cached
            known = f" ({cached:,} already known)" if cached else ""
            again = f", {rechecked:,} known shifts re-checked" if rechecked else ""
            self._log(
                f"  probe: {probes:,} of {candidates:,} candidate pairs tested{known}{again}, "
                f"{hits:,} found a shift",
                level="detail",
                stage="scan",
            )

    @staticmethod
    def _pair_class_parts(pairs: list[dict[str, Any]]) -> list[str]:
        """The pair classes present, each counted: "9 very likely duplicates"."""
        counts = Counter(c["classification"] for c in pairs)
        return [
            _count(counts[key], label)
            for key, label in (
                ("EXACT", "exact duplicate"),
                ("VERY_LIKELY", "very likely duplicate"),
                ("POSSIBLE", "possible duplicate"),
            )
            if counts.get(key)
        ]

    @classmethod
    def _pair_class_sentence(cls, pairs: list[dict[str, Any]]) -> str:
        """ "9 very likely duplicates and 28 possible duplicates"."""
        return _join_and(cls._pair_class_parts(pairs))

    def _on_pending_changed(self, n: int) -> None:
        if self._closing:
            return
        total = getattr(self, "_preview_total", 0)
        if not total:
            return
        if n:
            self.progress_bar.setValue(total - n)
            done = total - n
            if getattr(self, "progress_percent_label", None) is not None:
                self.progress_percent_label.setText(f"{done / total * 100:.0f}%")
                self.progress_meta_label.setText(f"{done:,} of {total:,} contact sheets")
        else:
            self._preview_total = 0
            self._rendering_previews = False
            self.scan_button.setText("■ Stop")
            self.scan_button.setToolTip("")
            self._set_progress_idle()
            # Everything is really done now — previews included
            self.tabs.setCurrentWidget(self._result_container)
            if getattr(self, "_chime_when_previews_done", False):
                self._chime_when_previews_done = False
                self._play_scan_complete_chime()

    def _play_scan_complete_chime(self) -> None:
        """Sound the end of a completed run; a hidden window is headless."""
        if not self.isVisible():
            return
        play_scan_complete(_resource_path("resources", "sounds", "scan-complete.wav"))

    # ----- decisions & sheets ----------------------------------------------

    def mark_peer_removed(
        self, keeper_id: int, peer_id: int, *, take_over: bool = False
    ) -> bool:
        """Mark a keeper's peer for removal, recording ownership in
        auto_marks — unless the mark is manual (no auto_mark owner),
        which no keeper may later erase by clearing.

        ``take_over`` is the mirror of ``mark_peer_kept``'s: a peer that is
        itself marked keep becomes the removed copy instead, releasing the
        removals it had caused. That is what makes a second "Keep" leave one
        kept and one removed rather than two kept copies. Returns True when
        the peer was flipped.
        """
        current = self.decisions.get(peer_id)
        if current == "keep" and not take_over:
            return False
        was_manual = current == "remove" and not self._owned(peer_id, self.auto_marks)
        if current == "keep":
            # It stops being a keeper, so the removals it owned go with it
            self.release_auto_marks(peer_id)
            self._forget_owner(peer_id)
        self.apply_decision(peer_id, "remove", auto=True, propagate=False)
        if not was_manual:
            self.auto_marks.setdefault(keeper_id, set()).add(peer_id)
        return current == "keep"

    def release_auto_marks(self, keeper_id: int) -> None:
        """Undo a keeper's auto-marks — but only for peers no other
        surviving keeper also depends on.

        A cleared peer may itself have kept copies in its other pairs, so
        those releases are undone with it: leaving them behind would keep
        a half-decided pair on screen after the mark that justified it is
        gone.
        """
        for peer_id in self.auto_marks.pop(keeper_id, set()):
            if self.decisions.get(peer_id) != "remove":
                continue
            still_owned = any(
                peer_id in marks and self.decisions.get(owner) == "keep"
                for owner, marks in self.auto_marks.items()
            )
            if not still_owned:
                self.release_auto_keeps(peer_id)
                self.apply_decision(peer_id, None)

    def mark_peer_kept(self, remover_id: int, peer_id: int, *, take_over: bool = False) -> bool:
        """Mark a removed copy's peer as kept, recording ownership in
        auto_keeps — unless the keep is manual (no auto_keep owner),
        which no removal may later erase by clearing.  This is the mirror
        of mark_peer_removed, so a pair ends up one kept and one removed
        whichever button starts it.

        ``take_over`` applies the invariant to a peer that is itself marked
        remove: it becomes the kept copy instead, releasing the keeps it had
        caused. That is what makes a second "Mark for removal" keep the first
        copy rather than leaving both selected — a pair must not be able to
        lose both its files by accident. Returns True when the peer was
        flipped.
        """
        current = self.decisions.get(peer_id)
        if current == "remove" and not take_over:
            return False
        was_manual = current == "keep" and not self._owned(peer_id, self.auto_keeps)
        if current == "remove":
            # It stops being a remover, so the keeps it owned go with it
            self.release_auto_keeps(peer_id)
            self._forget_owner(peer_id)
        self.apply_decision(peer_id, "keep", auto=True, propagate=False)
        if not was_manual:
            self.auto_keeps.setdefault(remover_id, set()).add(peer_id)
        return current == "remove"

    def release_auto_keeps(self, remover_id: int) -> None:
        """Undo a removed file's auto-keeps — but only for peers no other
        surviving removal also depends on.

        Mirror of ``release_auto_marks``: a cleared keep may itself have
        marked copies for removal in its other pairs, and those go with it.
        """
        for peer_id in self.auto_keeps.pop(remover_id, set()):
            if self.decisions.get(peer_id) != "keep":
                continue
            still_owned = any(
                peer_id in marks and self.decisions.get(owner) == "remove"
                for owner, marks in self.auto_keeps.items()
            )
            if not still_owned:
                self.release_auto_marks(peer_id)
                self.apply_decision(peer_id, None)

    @staticmethod
    def _owned(peer_id: int, owners: dict[int, set[int]]) -> bool:
        return any(peer_id in marks for marks in owners.values())

    def _forget_owner(self, file_id: int) -> None:
        """Drop a file from every ownership set after it changes direction.

        The set that owned the old decision is stale once the file is on the
        other side of the pair; leaving it behind lets a later release clear a
        decision that is still needed by a different keeper or remover.
        """
        for marks in (*self.auto_marks.values(), *self.auto_keeps.values()):
            marks.discard(file_id)

    def apply_decision(
        self,
        file_id: int,
        decision: str | None,
        *,
        auto: bool = False,
        propagate: bool = True,
    ) -> None:
        if decision:
            self.repository.set_decision(self.scan_id, file_id, decision)
        else:
            self.repository.clear_decision(self.scan_id, file_id)
        self.repository.commit()
        self.decisions[file_id] = decision or ""
        if not auto:
            # A decision the reviewer made themselves stops being an
            # owner's to undo, so a later release cannot erase it
            self._forget_owner(file_id)
        self._update_quarantine_button()
        self._update_decision_cards(file_id)
        if decision and propagate:
            self._propagate_decision(file_id)

    def _propagate_decision(self, file_id: int) -> None:
        """Mirror a decision into every pair the file is part of.

        A pair reads one kept and one removed, so a blank peer of a decided
        file takes the opposite decision.  A copy decided in one pair used
        to leave its other pairs with one side marked and the other blank —
        the reviewer saw a pair that looked half-clicked.  Peers that
        already carry a decision are left alone: the reviewer's choice on
        that pair wins.  The walk continues through them, so a chain of
        pairs stays consistent end to end.
        """
        opposite = {"keep": "remove", "remove": "keep"}
        neighbours: dict[int, set[int]] = {}
        for comparison in self.all_pairs:
            a, b = comparison["file_a_id"], comparison["file_b_id"]
            neighbours.setdefault(a, set()).add(b)
            neighbours.setdefault(b, set()).add(a)
        seen: set[int] = set()
        pending = [file_id]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            decision = self.decisions.get(current)
            if decision not in opposite:
                continue
            want = opposite[decision]
            for peer_id in neighbours.get(current, ()):
                if peer_id in seen:
                    continue
                if self.decisions.get(peer_id):
                    pending.append(peer_id)
                    continue
                self.apply_decision(peer_id, want, auto=True, propagate=False)
                owners = self.auto_marks if want == "remove" else self.auto_keeps
                owners.setdefault(current, set()).add(peer_id)
                pending.append(peer_id)

    def _complete_decided_pairs(self) -> None:
        """Give every half-decided pair its missing half.

        A pair has exactly two legal readings: neither copy decided, or
        both.  One ✓ Kept card beside a pair of unchecked buttons is not a
        state the reviewer can produce any more, but it can still be
        *loaded*: a decision written before cross-pair completion existed,
        or made while this pair was hidden (ignored, or one file missing),
        reaches the review with its peer blank.

        Fill the blank side with the opposite decision, and record the
        decided copy as the owner so undoing it in this session releases
        the fill with it.  A fill can complete the next pair along, so the
        pass repeats until nothing changes; only blanks are filled, so the
        set of decisions grows and the loop ends.
        """
        opposite = {"keep": "remove", "remove": "keep"}
        filled = 0
        changed = True
        while changed:
            changed = False
            for comparison in self.all_pairs:
                a, b = comparison["file_a_id"], comparison["file_b_id"]
                decision_a = self.decisions.get(a)
                decision_b = self.decisions.get(b)
                if bool(decision_a) == bool(decision_b):
                    continue
                decided, blank = (a, b) if decision_a else (b, a)
                want = opposite[decision_a or decision_b]
                self.repository.set_decision(self.scan_id, blank, want)
                self.decisions[blank] = want
                owners = self.auto_marks if want == "remove" else self.auto_keeps
                owners.setdefault(decided, set()).add(blank)
                filled += 1
                changed = True
        if filled:
            self.repository.commit()

    def _update_decision_cards(self, file_id: int) -> None:
        """Repaint this file's cards and every peer card they sit beside.

        A card's badges read the decisions of *both* halves of its pair, and
        a release repaints the peer before the clicked file's own decision
        is cleared. Updating only the file that changed therefore left the
        keeper's *Recommended* badge hidden after an undo — the released
        peer had already been drawn while the pair still looked decided.
        """
        touched = {file_id}
        for card in self.cards.get(file_id, ()):
            touched.update(getattr(card, "peer_ids", ()))
        for fid in touched:
            decision = self.decisions.get(fid) or None
            for card in self.cards.get(fid, ()):
                # Dialog cards are deleted on navigation — a stale wrapper
                # must not abort mid-decision (repo is already committed)
                with contextlib.suppress(RuntimeError):
                    card.set_decision(decision)

    def _on_sheet_ready(self, file_id: int, sheet_path: str) -> None:
        if self._closing:
            return
        # Update every visible instance — a file may appear in multiple
        # pairs/groups; resolving only the last-built card left the rest
        # stuck on 'rendering…'
        for card in self.cards.get(file_id, ()):
            # Stale card refs (deleted C++ objects) must not abort the loop
            with contextlib.suppress(RuntimeError):
                card.set_sheet(sheet_path)

    def _on_sheet_failed(self, file_id: int) -> None:
        if self._closing:
            return
        for card in self.cards.get(file_id, ()):
            with contextlib.suppress(RuntimeError):
                card.set_sheet_failed()

    def closeEvent(self, event: Any) -> None:
        """Stop the background work before the widgets go away.

        Closing mid-scan used to leave the scan thread and its executor
        running into interpreter shutdown ("cannot schedule new futures after
        interpreter shutdown"), and a preview finishing after the window was
        destroyed raised "Signal source has been deleted". Both are stopped
        here: the sheet loader drops its queue and stops emitting, and a
        running scan is cancelled and given a few seconds to reach a boundary.
        """
        if self._closing:
            event.accept()
            return
        self._closing = True
        for name in ("_sheet_sweep", "_sheet_sweep_idle", "_status_timer", "_progress_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
        self.sheet_loader.shutdown()
        worker = self.scan_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            # Test doubles do not always expose QThread.wait; a real worker
            # gets the chance to reach a boundary before the window goes
            wait = getattr(worker, "wait", None)
            if callable(wait) and not wait(15_000):
                logger.warning("Scan did not stop within 15s; closing anyway")
        event.accept()


def run_gui(config: Config, config_path: Path) -> int:
    """Launch the review GUI for the configured database."""
    import sys

    if sys.platform == "win32":
        # python.exe has no app manifest, so Windows guesses DPI awareness.
        # Declare per-monitor V2 before QApplication exists — crisp text
        # when the window moves between displays at different scaling.
        import contextlib
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    # Ctrl+C is a copy shortcut in a window, and this window's Stop button is
    # the only stop. Launched from a console, a stray press would raise
    # KeyboardInterrupt out of the Qt event loop and take the window — and the
    # scan inside it — down mid-write. Ignore it before the loop exists.
    ignore_ctrl_c()
    app = QApplication(sys.argv[:1])
    # Application-wide, so dialogs and the taskbar group carry it too
    app.setWindowIcon(_app_icon())
    repository = DatabaseRepository(Path(config.database.path))
    # Scans left at 'running' belong to a killed process — no worker can
    # still be alive in a fresh GUI, so mark them interrupted up front
    repository.mark_interrupted_scans()
    window = MainWindow(config, repository, config_path)
    window.showMaximized()
    code = app.exec()
    repository.close()
    return code

    repository.close()
    return code

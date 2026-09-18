"""
Bert -- equipment ticket board.

Drag cards to reprioritise, edit fields inline, mark work complete, undo from
the activity feed. Every change is attributed to the name in Settings, and
edits are batched so the server can process them in one go.

    pip install PySide6 httpx
    python bert.py --api http://127.0.0.1:8788
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import pathlib
import re
import sys
import time
import urllib.parse
import uuid
from datetime import date, datetime, timezone

import httpx

# The title format is defined once, in the parser the sync uses. Importing it
# keeps the editor's validity check and Ernie's own reading of a thread in
# agreement.
import ernie_extract as ex
import ernie_version
from PySide6.QtCore import (QUrl, QDate, 
    QEvent, QMimeData, QPoint, QPointF, QRect, QRectF, QSize,
    QStringListModel, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import (QDesktopServices, 
    QBrush, QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QPainter,
    QPalette, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QCompleter, QDateEdit, QDialog,
    QDialogButtonBox,
    QFormLayout, QGridLayout,
    QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QVBoxLayout,
    QWidget,
)

SETTINGS = pathlib.Path.home() / ".bert.json"
# Beside the script rather than in the settings directory: it ships with the
# code, and a checkout without it should still start.
def _asset(name):
    """Where a bundled file is, frozen or not.
    """
    root = pathlib.Path(getattr(sys, "_MEIPASS", "")
                        or pathlib.Path(__file__).resolve().parent)
    return root / "assets" / name


LOGO = _asset("bert_logo.png")
# The face Bert makes about a version mismatch.
UPDATE_FACE = _asset("bert_update.png")
# Set only by --pretend-version / --pretend-ernie, which exist so the update
# dialog can be looked at.
PRETEND_MINE = ""
PRETEND_ERNIE = ""
PRETEND_NEWEST = ""
PRETEND_REQUIRED = ""
# **Whether this window is the whole application.** `ernie_app` sets it before
# building the window; a Bert started by `run.sh` or `bert.cmd` leaves it
# False.
SUPERVISED = False
POLL_MS = 5_000       # a poll that changes nothing now costs <1ms to render
DEGRADED_S, BLOCKED_S = 5, 15
SHARED_STALE_S = 180   # three missed sync cycles: their changes aren't arriving
# The customer list is pulled hourly, so a few hours late means nothing and
# six means the pull has stopped -- a bad token, or Jira unreachable.
ROSTER_STALE_S = 6 * 3600

# What a `completed` event's new_value says when the closing happened in
# Discord rather than here.
CLOSED_IN_DISCORD = "discord"

NEW_TICKET = "__new__"
MIRROR_STALE_S = 180   # three cycles, asked of Ernie's own reading: past
                       # this the sync has stopped and the board is older
                       # than it looks
REFRESH_GLYPH = "\u21bb"
SPIN_MS = 33           # the glyph turns while a manual refresh waits;
SPIN_STEP = 11         # a full turn in about a second
AWAIT_GIVEUP_S = 90    # a manual refresh waits for the next read of Discord,
                       # which is the only thing that moves the number.
TOAST_MS = 6_000      # a ceiling: the toast normally clears the
                      # moment the board comes back without the card

#Lefthand Card Rail/Board
DRAG_THRESHOLD = 5
RAIL_ZONE_MIN = 34                               
RAIL_ZONE_GAP = 7                            
RAIL_HEAD_GAP = 8          # air above a band's name, so it groups downward
DROP_ZONE_MIN = 72                     
RANK_STEP = 1000.0          
EDGE_SCROLL_ZONE = 64
EDGE_SCROLL_MAX = 22
EDGE_SCROLL_MS = 16
RAIL_WIDTH = 208           # what it opens at, not what it stays
RAIL_MIN_W = 120
RAIL_MAX_W = 460
RAIL_ROW_CHROME = 26
CARD_HEAD_SPACING = 8      # between the columns of a card's top row
CARD_CLIENT_MIN_W = 44     # a client name never shrinks past this, it elides
CARD_FOOT_SPACING = 16     # between the columns of a card's bottom row
CLIENT_BOX_MIN_W = 190     # "Bravo Environmental (7)" without eliding it
CARD_ISSUE_MIN_W = 124

CARD_MIN_W = 140
RAIL_REDRAW_MS = 140
RAIL_BAR_H = 2
BOARD_PAD = 16              
BOARD_MAX = 800             

#Recent Activity Feed                                           
FEED_HEIGHT = 160         
FEED_FOLDED = 30           # the floor for a folded feed; the real
                           # height is measured off its caption
FEED_ROWS = 4              
FEED_MAX_ROWS = 8          
BOARD_MIN_H = 180          # the board never drags away to nothing
SPLIT_GRIP = 6             # the handle between the board and the feed
RAIL_FOLDED_W = 30         # the spine either side folds down to
UNFOLD_GRAB = 12           # drag a spine this far and it opens
STATS_MIN_W = 180          # narrower and the month bars stop comparing
STATS_MAX_W = 380          # wider is a report, not a margin
STATS_WIDTH = 244          # what it opens at, not what it stays
STATS_ROW_CHROME = 64      # the age column and the padding beside it

STATS_GUTTER = 10
STATS_OLD_D = 90           # a quarter open is a different kind of old
STATS_MAX_AGE_S = 60       # these move when a ticket closes, not per poll

# How many retag pairs are drawn before the rest are summed into one line.
STATS_MOVES_SHOWN = 6
GLYPH_MOVE = "\u2192"  # the arrow between two tags: PROD -> OPS

# What the figures panel can be asked about, shortest first.
STATS_WINDOWS = (("7 days", 7), ("2 weeks", 14), ("3 weeks", 21),
                 ("4 weeks", 28), ("3 months", 91), ("6 months", 182),
                 ("9 months", 273), ("1 year", 365))
STATS_WINDOW_DEFAULT = 28
GLYPH_LEFT = "\u00ab"
GLYPH_RIGHT = "\u00bb"
# The feed folds downward rather than sideways, so its button says so.
GLYPH_DOWN = "\u25be"
GLYPH_UP = "\u25b4"

# Qt's QWIDGETSIZE_MAX, which PySide6 does not export. Undoes a
# setFixedHeight, which sets minimum and maximum together.
UNCAPPED = 16777215
FEED_TIME_W = 60           # the timestamp column
FEED_MORE_W = 14           # the chevron, in its own column so it survives
                           # Between the last column and the scrollbar.

FEED_GUTTER = 10

# How wide a row may get.
FEED_ROW_MAX_W = 1300
# What a closed row is clipped to at the narrowest. The widths at each
# site (46 for the thread, 44 for the detail, 40 for a new name) are
# scaled up together from here, so their proportions survive.
FEED_BASE_CHARS = 46 + 44
FEED_FONT_PX = 12          # the feed line, set in the row's stylesheet
# Measured rather than guessed at: averageCharWidth() is a crude number
# that ignores which glyphs actually turn up. A real line does not.
FEED_SAMPLE = ("Bella Fiore edited PROD: Steel City Water - 30Aug26 - "
               "SSD0311 firmware rollback")
FEED_STATUS_W = 118        
FEED_UNDO_W = 88           
FEED_ROW_PAD = 4           # above and below a row's contents, so the Undo
                           # button clears the hairline under it
FEED_LIMIT = 200           

# The customer list is pulled from Jira on the sync's own hourly heartbeat, so
# asking Ernie for it more often than this only ever gets the same answer back.
ROSTER_MAX_AGE_S = 900

BANDS = ["unassigned", "critical", "high", "medium", "low"]
BAND_LABEL = {b: b.capitalize() for b in BANDS}
# Not "Unassigned", which described the data rather than the ask. These
# are the tickets nobody has picked up.
BAND_LABEL["unassigned"] = "Needs Attention"
CAUTION = "⚠"          # the same sign warning_row() uses

# Build state, return state and equipment direction were replaced by work
# items; nothing edits them any more.
STATES = [("needs_created", "Needs created"),
          ("created", "Created"),
          ("not_needed", "Not needed")]
DIRECTIONS = [("", "\u2014"), ("leaving", "Leaving"), ("coming_back", "Coming back")]

# A stylesheet padding rule replaces the native one outright rather than
# adding to it, so every button that sets its own padding is a smaller target
# than a default Qt one.
BTN_HIT = "font-size:11px; padding:6px 14px; "

# A card is the big shape and takes the larger radius; a rail row is 26px
# tall, so more than a hint of a curve on one eats its own corner.
CARD_RADIUS = 5
ROW_RADIUS = 4
BTN_RADIUS = 5

# What a search box spends before any placeholder is drawn: field() sets
# 6px of padding either side over a 1px border, and the last two are slack so
# a hint that fits does not end flush against the frame.
SEARCH_HINT_PAD = 16


# --------------------------------------------------------------------------
# Color
# --------------------------------------------------------------------------

LIGHT = {
    # Every value is solved against the floors `check_palette.py` holds
    # rather than chosen by eye -- CARD_MIN, CONTROL_MIN, EDGE_MIN -- so a
    # change here is checked rather than judged. The tag stripe in particular
    # doubles as that tag's label in the figures panel, where it is text.

    "ink": "#262626", "muted": "#5F5F5F", "line": "#C6C6C6",
    "surface": "#FFFFFF", "canvas": "#EEEEEE", "panel": "#EBEBEB",
    # The activity bar, under the sections either side of the board.
    "feed": "#E8E8E8",
    # Raised controls -- the Qt Button role.
    "beside": "#F4F4F4",
    # What a button, a field or a work-item bubble is drawn on: a step under
    # the card, in both themes.
    "control": "#E2E2E2",
    # The floor
    "well": "#E5E5E5",
    # Badge fills, a step under the card rather than over it.
    "amber_bg": "#F9EEDA", "amber_fg": "#79510D",
    "red_bg": "#FAEBEB", "red_fg": "#A12626", "red_edge": "#D54E4E",
    "ok_fg": "#1C6033", "ok_bg": "#D9F2E2", "accent": "#245B99",
    "info_bg": "#E8F0F8", "info_fg": "#24568F",
    "grey_fg": "#5F5F5F",
    # A tag carrying a fact rather than a warning -- an equipment number, a
    # ticket count. Quiet on purpose: there are several per card and they are
    # reference, not news.
    "chip_bg": "#E5E5E5",
    "on_accent": "#FFFFFF", "hover_bg": "#E7F0F9",
    "neutral": ("#959595", "#FBFBFB", "#454545"),
    # (stripe, fill, ink). The fill is a whisper of the tag on a near-white
    # card; the **stripe** carries it, and is dark rather than pale because it
    # has to stand EDGE_MIN off that fill -- which is also what lets the
    # figures panel draw it as the tag's label.
    "queue": {
        "PROD": ("#9E6D25", "#FDFAF7", "#583D16"),
        "OPS":  ("#59831F", "#F8FCF3", "#324912"),
        "ENG":  ("#3278CF", "#F8FAFD", "#1D4372"),
        "CS":   ("#9858D8", "#FBFAFD", "#5B2490"),
    },
    # The band header's strip: above the column, under the cards.
    "band_tint": {
        "unassigned": "#EAEAEA", "critical": "#EAEAEA",
        "high": "#EAEAEA", "medium": "#EAEAEA",
        "low": "#EAEAEA",
    },
    # (fill, outline). Unassigned keeps its red wash, quieter than critical's,
    # the way dark has always had it. `low` is the one band with no colour of
    # its own and stands off by lightness alone.
    "band_card": {
        "unassigned": ("#FDF9F9", "#D54E4E"), "critical": ("#FDF9F9", "#D54E4E"),
        "high": ("#FCF9F6", "#AE7A29"), "medium": ("#F8FAFD", "#4685D3"),
        "low": ("#FAFAFA", "#AEAEAE"),
    },
    # Heading ink, one per band, the dark end of the colour it is washed in.
    "band_text": {
        "unassigned": "#A12626", "critical": "#A12626",
        "high": "#654818", "medium": "#1E4F81",
        "low": "#545454",
    },
}

# The neutral ramp is lifted from the PortalBear prototype, which had already
# been tuned against a real screen.
DARK = {
    "ink": "#E6E9EC", "muted": "#98A2AD", "line": "#333B45",
    "surface": "#1B2027", "canvas": "#14181D",
    # The sections around the work sit at the canvas here, not a step under
    # it.
    "panel": "#14181D",
    # The activity bar.
    "feed": "#14181D",
    "beside": "#222831",
    "control": "#1B2027",
    # Below the canvas here, as it is in light -- in dark that means darker
    # still, which is the one direction #222831 could not go.
    "well": "#0E1115",
    "amber_bg": "#2E2718", "amber_fg": "#EFC15E",
    "red_bg": "#301D1C", "red_fg": "#F5AAA2", "red_edge": "#E08078",
    "ok_fg": "#A8DC8B", "ok_bg": "#1E2A1C", "accent": "#7FA9DA",
    "info_bg": "#232E3B", "info_fg": "#A3C8F0",
    "grey_fg": "#A8B2BD",
    # The light one is a near-white, which on this ground stopped being quiet
    # and became the brightest thing on the card.
    "chip_bg": "#262D36",
    "on_accent": "#12202E", "hover_bg": "#2A3A4E",
    "neutral": ("#6E7883", "#262D36", "#B6C0CB"),
    "queue": {
        "PROD": ("#EF9F27", "#33291A", "#EFC15E"),
        "OPS":  ("#97C459", "#232E1D", "#A8DC8B"),
        "ENG":  ("#6F9BD1", "#212B38", "#A3C8F0"),
        "CS":   ("#B08BD4", "#2A2334", "#D3BCE8"),
    },
    "band_tint": {
        "unassigned": "#2E1E1D", "critical": "#2E1E1D", "high": "#2C2318",
        "medium": "#1F2833", "low": "#212730",
    },
    "band_card": {
        "unassigned": ("#301D1C", "#E08078"), "critical": ("#3A2422", "#E08078"),
        "high": ("#382C18", "#D9A441"), "medium": ("#24303E", "#5A82B0"),
        "low": ("#262D36", "#3E4753"),
    },
    "band_text": {
        "unassigned": "#F5AAA2", "critical": "#F5AAA2", "high": "#EFC15E",
        "medium": "#A3C8F0", "low": "#A8B2BD",
    },
}



# --------------------------------------------------------------------------
# Other Themes
# --------------------------------------------------------------------------
MIDNIGHT = {
    **DARK,
    "ink": "#E6E8F0", "muted": "#9CA1B1", "line": "#313A50",
    "surface": "#0C203C", "canvas": "#021833", "panel": "#021833",
    "feed": "#021833", "beside": "#162745", "control": "#0C203C",
    "well": "#071023", "chip_bg": "#1B2C4A",
}

PLUM = {
    **DARK,
    "ink": "#EFE6EB", "muted": "#AF9CA5", "line": "#4D3240",
    "surface": "#361327", "canvas": "#2D0B1F", "panel": "#2D0B1F",
    "feed": "#2D0B1F", "beside": "#3F1C30", "control": "#361327",
    "well": "#200816", "chip_bg": "#442035",
}

SLATE = {
    **DARK,
    "ink": "#E0EBEA", "muted": "#8DA6A5", "line": "#184040",
    "surface": "#012424", "canvas": "#031B1B", "panel": "#031B1B",
    "feed": "#031B1B", "beside": "#002D2D", "control": "#012424",
    "well": "#001414", "chip_bg": "#033232",
}

PALETTES = {"light": LIGHT, "dark": DARK, "midnight": MIDNIGHT,
            "plum": PLUM, "slate": SLATE}

# Which of them have pale ink.
DARK_GROUNDS = frozenset({"dark", "midnight", "plum", "slate"})

# "system" is not a palette, it is a question -- so it is added here rather
# than living in PALETTES and having to be excluded from every walk.
THEMES = ("system",) + tuple(PALETTES)
THEME_LABEL = {"system": "Follow the desktop", "light": "Light",
               "dark": "Dark", "midnight": "Midnight \u2014 dark blue",
               "plum": "Plum \u2014 dark wine",
               "slate": "Slate \u2014 dark teal"}

# What a board with no setting yet opens as
THEME_DEFAULT = "dark"

# How the editor asks for a title. `guided` shows the tag, client, date and
# description and keeps the title as a preview; `typing` shows the title box
# and nothing else, for the people who have been typing these into Discord
# for years.
ENTRY_MODES = ("guided", "typing")
ENTRY_LABEL = {"guided": "Guided \u2014 pick the tag, client and date",
               "typing": "Typing \u2014 one box, as in Discord"}
ENTRY_DEFAULT = "guided"


class Theme:
    """The active palette, reached by name.
    """

    _p = LIGHT

    def __init__(self):
        self.name = "light"

    def use(self, name: str) -> None:
        # An unknown name is light rather than a KeyError: it arrives from a
        # settings file, which a future build may have written.
        self.name = name if name in PALETTES else "light"
        self._p = PALETTES[self.name]

    @property
    def dark(self) -> bool:
        return self.name in DARK_GROUNDS

    def __getattr__(self, key):
        # _p resolves off the class, so this never recurses looking for it.
        try:
            return self._p[key.lower()]
        except KeyError:
            raise AttributeError(key) from None


T = Theme()

# PySide keeps no reference of its own to a top-level window. One built inside
# a method and left to a local goes when that method returns, taking the
# window with it -- so every Bert window that is meant to stay on screen is
# held here.
_OPEN = []


def dark_titlebar(win, on=None) -> bool:
    """Ask Windows to draw this window's title bar dark.

    The attribute is 19 before Windows 10 build 18985 and 20 after, and the
    wrong one comes back as an error rather than raising -- which is why the
    loop below tries both, and why it must go on trying both.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        flag = ctypes.c_int(1 if (T.dark if on is None else on) else 0)
        for attr in (20, 19):
            ok = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(win.winId())), ctypes.c_int(attr),
                ctypes.byref(flag), ctypes.sizeof(flag))
            if ok == 0:
                return True
    except Exception:
        pass          # a nicety; never worth failing to open a window over
    return False


def desktop_is_dark() -> bool:
    """What the desktop is set to, when Qt is willing to say.
    """
    hints = QApplication.styleHints() if QApplication.instance() else None
    scheme = getattr(hints, "colorScheme", None)
    if scheme is None:
        return False
    try:
        return scheme() == Qt.ColorScheme.Dark
    except (AttributeError, TypeError):
        return False


def resolve_theme(choice: str) -> str:
    """A stored setting to the palette to actually load.
    """
    if choice in PALETTES:
        return choice
    return "dark" if desktop_is_dark() else "light"


def apply_theme(choice: str) -> None:
    """Load a palette and hand Qt a matching one for what it draws itself.
    """
    T.use(resolve_theme(choice))
    app = QApplication.instance()
    if app is None:
        return

    # From the style's own palette, not a blank one.
    pal = QPalette(app.style().standardPalette())
    ink, surface, canvas = QColor(T.INK), QColor(T.SURFACE), QColor(T.CANVAS)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText,
                 QPalette.ToolTipText):
        pal.setColor(role, ink)
    pal.setColor(QPalette.Window, canvas)
    pal.setColor(QPalette.Base, surface)
    pal.setColor(QPalette.AlternateBase, QColor(T.BESIDE))
    pal.setColor(QPalette.ToolTipBase, surface)
    pal.setColor(QPalette.Button, QColor(T.BESIDE))
    pal.setColor(QPalette.Highlight, QColor(T.ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor(T.ON_ACCENT))
    pal.setColor(QPalette.PlaceholderText, QColor(T.MUTED))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(T.MUTED))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(T.MUTED))
    app.setPalette(pal)

    # Tooltips need saying twice.
    app.setStyleSheet(
        f"QToolTip {{ color:{T.INK}; background-color:{T.SURFACE};"
        f" border:1px solid {T.LINE}; padding:4px 6px; }}")

# Long enough to find the card the jump landed on
FLASH_MS = 600

# Issues that mean the thread itself couldn't be read properly.
BLOCKING = {"title_none", "title_unparseable", "title_prefix_only",
            "title_loose", "title_nonstandard"}


def card_skin(data, editing=False):
    """Fill and outline for one ticket, wherever it is drawn.

    A ticket wears its **tag** -- PROD, OPS, ENG, CS -- not its priority. The
    tag is what the ticket is; the priority is where it sits, which the band
    already says, and saying it twice spent the whole colour budget on the
    half nobody was reading.

    Needs attention outranks the tag: red, being the one state asking for
    somebody rather than describing the work. An unreadable thread keeps its
    fill and is outlined at 2px, so it reads as outlined rather than coloured.

    The card and its rail row have to agree, and used to say it separately.
    """
    stripe, tint, _ = T.QUEUE.get(data.get("queue") or "", T.NEUTRAL)
    edge = stripe
    if (data.get("priority") or "") == "unassigned":
        tint, edge = T.BAND_CARD["unassigned"]
    if needs_triage(data):
        return tint, T.RED_EDGE, 2
    if editing:
        return tint, T.ACCENT, 1
    return tint, edge, 1


def wal_standing(wal: dict | None) -> str:
    """What to say about the write-ahead log: "", "watch" or "act".

    A healthy WAL fills and empties every few seconds, so it never gets near
    the size of the database. One that keeps growing means a reader is
    holding a snapshot and it cannot be checkpointed -- and the part nobody
    sees is that writers then start timing out, so a ticket can look closed
    on the board and never reach its thread.

    Larger than its own database is the line. Past twice the size is the
    difference between something to keep an eye on and something to do
    something about, and the something is restarting the stack.

    Pure, and fails quiet: an Ernie too old to report `wal` says nothing,
    which must never become a warning about a field that is simply absent.
    """
    if not wal:
        return ""
    size = wal.get("db_bytes") or 0
    bytes_ = wal.get("wal_bytes") or 0
    if not size or bytes_ <= size:
        return ""
    return "act" if bytes_ > size * 2 else "watch"


def build_standing(mine, theirs, floor, newest="", required=""):
    """Whether this Bert is behind, and how far. Answers (state, sentence).

    blocked is older than the floor Ernie publishes and goes read-only;
    behind is merely not newest; ok is the rest. Fails open at every step it
    cannot answer.

    In the exe `theirs` is always equal -- one process, one module -- so
    `newest`, off the pinned note, is the only thing that can be news.
    """
    if not theirs and not newest:
        return "ok", ""

    # `required` is the floor the release note set, and the only one that
    # comes from outside this process: MIN_BERT is this build's own, against
    # this build's own VERSION, and check_version.py keeps it at or below --
    # so in one process the blocked branch is unreachable by construction.
    #
    # Refused here as well as in the parser. That one runs where the note is
    # read; this runs where the board is taken away, and a floor ahead of a
    # build nobody can download has no recovery in the field. So it is obeyed
    # only when a build satisfying it is known to exist -- not knowing what
    # can be downloaded is the same case, and resolves the same way.
    if required and newest and not ernie_version.is_older(newest, required):
        if not floor or ernie_version.is_older(floor, required):
            floor = required

    if floor and ernie_version.is_older(mine, floor):
        return "blocked", (
            f"This copy of Bert is {mine}. This board needs {floor} or newer, "
            f"so changes are paused until it is updated. The board is still "
            f"here to read.")
    # The sentence names its source, because the two send you to different
    # places: an Ernie further ahead is somebody else's machine on a build you
    # could get, and a release note is the build that is actually sitting in
    # the shared folder waiting to be downloaded.
    target, whose = theirs, "Ernie is on"
    if newest and (not target or ernie_version.is_older(target, newest)):
        target, whose = newest, "the current build is"
    if target and ernie_version.is_older(mine, target):
        # Said as the good news it is. "Bert found a new update" over a
        # board that is working perfectly reads as an alarm, and an alarm
        # that turns out to be nothing is how somebody learns to click
        # through the next one without reading it.
        return "behind", (
            f"This one is fine and nothing is paused \u2014 you are on {mine} "
            f"and {whose} {target}. Worth updating when you get a chance.")
    return "ok", ""


class UpdateDialog(QDialog):
    """Bert, having noticed.

    A dialog rather than the banner the outage warning uses, because this one
    has to be read once and acted on -- a strip along the top is for a state
    somebody is living with, and being on the wrong build is a thing to go and
    fix. It says the two numbers, because "there is an update" is not
    actionable and "you are on 0.9.0, Ernie is on 0.9.3" is.
    """

    def __init__(self, parent, state, detail, url=""):
        super().__init__(parent)
        self.setWindowTitle("Update")
        dark_titlebar(self)
        self.setStyleSheet(f"QDialog {{ background:{T.CANVAS}; }}")

        row = QHBoxLayout(self)
        row.setContentsMargins(18, 18, 18, 14)
        row.setSpacing(16)

        face = QLabel()
        if UPDATE_FACE.exists():
            face.setPixmap(QPixmap(str(UPDATE_FACE)))
        face.setAlignment(Qt.AlignTop)
        face.setStyleSheet("background:transparent;")
        row.addWidget(face)

        said = QVBoxLayout()
        said.setSpacing(8)
        head = QLabel("Wait, Bert found a new update")
        f = QFont()
        f.setPointSize(12)
        f.setWeight(QFont.DemiBold)
        head.setFont(f)
        head.setStyleSheet(f"color:{T.INK}; background:transparent;")
        said.addWidget(head)

        # The host is read off the address, never written here: the download
        # can move without a rebuild, so a name typed in becomes a lie the day
        # it does -- and the kind nobody notices, because the button still
        # works.
        if url:
            # Named, because a bare "Opens ..." leaves the reader to work
            # out which of the two buttons it is about -- and the other one
            # dismisses the dialog, which is the wrong guess to invite.
            detail = (f"{detail} (Get the new build opens "
                      f"{urllib.parse.urlparse(url).netloc} in your browser.)")

        body = QLabel(detail)
        body.setWordWrap(True)
        body.setMinimumWidth(300)
        body.setStyleSheet(f"color:{T.MUTED}; font-size:12px;"
                           f" background:transparent;")
        said.addWidget(body)
        said.addStretch(1)

        # **Only on the one that is not a problem.** A blocked board cannot
        # be dismissed -- the dialog is not what is stopping anybody, the
        # floor is, and offering to hide it would promise something it
        # cannot do.
        self.quiet = None
        if state == "behind":
            self.quiet = QCheckBox("Don't tell me about this one again")
            # The indicator is stated, not left to the style. Fusion draws it
            # from the palette's own roles, and against this dialog's ground
            # that came out as a label with no box beside it at all -- a
            # checkbox nobody can see is a checkbox nobody ticks.
            self.quiet.setStyleSheet(
                f"QCheckBox {{ color:{T.MUTED}; font-size:11px;"
                f" background:transparent; }}"
                f"QCheckBox::indicator {{ width:13px; height:13px;"
                f" border:1px solid {T.LINE}; border-radius:3px;"
                f" background:{T.CONTROL}; }}"
                f"QCheckBox::indicator:hover {{ border-color:{T.ACCENT}; }}"
                f"QCheckBox::indicator:checked {{ background:{T.ACCENT};"
                f" border-color:{T.ACCENT}; }}")
            self.quiet.setCursor(Qt.PointingHandCursor)
            said.addWidget(self.quiet)

        # Under the text, which is where it was asked for -- and it is the
        # only button, because there is nothing here to decline. A blocked
        # board is blocked whatever this says.
        ok = QPushButton("Fine" if state == "blocked" else "Alright")
        ok.setStyleSheet(btn_css())
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self.accept)
        under = QHBoxLayout()
        under.addStretch(1)

        # **Only when there is somewhere to go.** Ernie publishes where a new
        # Bert comes from; with no `BERT_UPDATE_URL` set there is no build to
        # fetch and no button, which is the whole reason it is published
        # rather than built in. It says *get*, not *update now*: it opens a
        # page. A button called "Update now" that only opens a browser is the
        # unsent mark saying "pushing" over something that was never going to
        # be pushed.
        if url:
            go = QPushButton("Get the new build")
            go.setStyleSheet(btn_css())
            go.setCursor(Qt.PointingHandCursor)
            go.setToolTip(url)
            go.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
            # First on the blocked one, because that is the person who has to
            # do something; second on the other, where carrying on is a fair
            # answer.
            if state == "blocked":
                under.addWidget(go)
                under.addWidget(ok)
            else:
                under.addWidget(ok)
                under.addWidget(go)
        else:
            under.addWidget(ok)
        said.addLayout(under)

        row.addLayout(said, 1)

    def muted(self) -> bool:
        """Whether to stop mentioning *this* build of Ernie."""
        return bool(self.quiet and self.quiet.isChecked())


def needs_triage(c) -> bool:
    """Whether a card still reads as unreadable.
    """
    if not set(c.get("issues") or []) & BLOCKING:
        return False
    return not (c.get("client_override") or "").strip()


def needs_attention(cards):
    """The cards asking for a person, in the order the eye reads down a board.

    `needs_triage` stays the only definition of the set; this is the ordering
    alone, so the control cannot drift from the red edges it points at.
    BANDS then rank is what the board already draws, so cycling never jumps
    backwards up the screen.
    """
    place = {b: i for i, b in enumerate(BANDS)}
    flagged = [c for c in cards or [] if needs_triage(c)]
    flagged.sort(key=lambda c: (place.get(c.get("priority") or "", len(BANDS)),
                                c.get("rank") or 0.0))
    return [c["thread_id"] for c in flagged]


def next_attention(order, current):
    """The card after `current`, wrapping past the last one to the first.

    A `current` that has gone -- retitled, closed, or filtered out since the
    last click -- starts again at the top rather than losing the cycle.
    """
    if not order:
        return None
    if current in order:
        return order[(order.index(current) + 1) % len(order)]
    return order[0]


# A date anywhere in the raw title, for telling "never typed" from
# "typed and refused". The parser's own token, so the two agree.
_DATE_RX = re.compile(ex._DATE_TOKEN, re.IGNORECASE)


def compose_title(queue, client, date, summary) -> str:
    """The four fields as the one string Discord holds.

    Only for a title there is nothing to preserve in -- a new ticket, or one
    the fields cannot express. A title that already parses is spliced instead
    (`ex.replace_field`), because composing renormalises whatever it passes
    and every change is a real thread rename.
    """
    out = f"{(queue or '').strip()}:"
    for part in ((client or "").strip(),
                 title_stamp(date) if date else "",
                 (summary or "").strip()):
        if part:
            out += f" {part} -"
    return out.rstrip(" -")


class DateBox(QDateEdit):
    """A date field that can say it has no date, and still opens on today.

    Qt has no empty date, so `NO_DATE` stands for one and
    `setSpecialValueText` shows "none". That leaves the calendar opening on
    the floor, so reaching for the control -- click, arrow key, popup --
    wakes an empty field to today first. Nothing is written until then, so a
    title with no date goes on saying so.
    """

    # The drop-down keeps its hit area and loses its arrow; paintEvent draws
    # a calendar there instead, since an arrow promises a list.
    ARROW_W = 30
    # Inside the field's 1px border and 5px radius, or the glyph rides the
    # rounded corner.
    EDGE = 3

    def paintEvent(self, e):
        super().paintEvent(e)
        pen = QPen(QColor(T.MUTED))
        pen.setWidth(1)
        pa = QPainter(self)
        pa.setRenderHint(QPainter.Antialiasing, True)
        pa.setPen(pen)
        # A calendar: a page, a torn-off header, and two rings above it.
        w, h = 13, 13
        x = self.width() - self.EDGE - self.ARROW_W + (self.ARROW_W - w) // 2
        y = (self.height() - h) // 2 + 1
        pa.drawRoundedRect(x, y, w, h, 2, 2)
        pa.drawLine(x + 1, y + 4, x + w - 1, y + 4)
        pa.drawLine(x + 3, y - 1, x + 3, y + 1)
        pa.drawLine(x + w - 3, y - 1, x + w - 3, y + 1)
        pa.end()

    def _wake(self):
        if self.date() == NO_DATE:
            self.setDate(QDate.currentDate())
            return True
        return False

    def mousePressEvent(self, e):
        # Before the click lands, so the popup opens on today.
        self._wake()
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
            if self._wake():
                return          # the wake *was* the step; do not also step
        super().keyPressEvent(e)


def title_takes_client(title: str) -> bool:
    """Whether picking a client can reach this title at all.

    The client is whatever sits between the tag and the date, so there is
    only a segment to change once a date marks where it ends. Below that a
    pick is saved as a `client_override` and the title does not move, so the
    editor says so rather than appearing to do nothing.
    """
    return ex.parse_title(title or "").confidence in ("strict", "loose")


def title_problems(c) -> list:
    """What is wrong with this card's title, in words a person can act on.

    Read off the parsed fields, not the issue code: `title_loose` says the
    shape was wrong without saying which part is missing, and queue,
    client_raw and thread_date each either exist or do not.

    A list, because a title can be wrong in several ways at once. Pure over
    the payload, so the wording is checked without a QApplication.
    """
    if (c.get("confidence") or "") == "strict":
        return []
    raw = (c.get("name") or "").strip()
    # Nothing at all is one fact, not three: every other message here names
    # one thing to go and fix.
    if not raw and not any(c.get(k) for k in
                           ("queue", "client_raw", "thread_date", "summary")):
        return ["No title"]

    # Every pattern is anchored on the tag, so without one nothing else
    # parses and every field reads as absent whether it is or not. Put a tag
    # on the front and parse again: the probe says what would still be wrong
    # once a real one is there.
    if not (c.get("queue") or ex.PREFIX_ONLY.match(raw)):
        t = ex.parse_title(f"{ex.QUEUES_OFFERED[0]}: {raw}")
        # Into the half below, never back to the top: PREFIX_ONLY needs a
        # character after the tag, so an empty title probes to `PROD: `,
        # which has no queue either, and recursing would not terminate.
        return ["No tag"] + _title_problems_after_tag(dict(
            c, queue=t.queue or ex.QUEUES_OFFERED[0], client_raw=t.client_raw,
            thread_date=t.date.isoformat() if t.date else None,
            summary=t.summary, confidence=t.confidence), raw)

    return _title_problems_after_tag(c, raw)


def _title_problems_after_tag(c, raw) -> list:
    """Everything that can be judged once a tag is known to be there.

    Separate from `title_problems` so the tagless probe is one step rather
    than a recursive call.
    """
    # The probe can land on a title that parses perfectly -- one missing only
    # its tag -- and then there is nothing else to say.
    if (c.get("confidence") or "") == "strict":
        return []
    out = []
    dated = bool(c.get("thread_date") or "")

    # The date anchors the client -- the client is whatever sits between the
    # tag and the date -- so with no date there is no client slot to be
    # empty. `PROD: Thrasher - Trade show TOF` parses with the whole of it as
    # the summary, and calling that a missing client would be reporting a
    # field the parser could not reach rather than one that is absent.
    if dated and not (c.get("client_raw") or ""):
        out.append("No client name")
    if not dated:
        # A date refused is a correction; a date never typed is an
        # addition. Different jobs, so different words.
        out.append("Date not readable" if _DATE_RX.search(raw) else "No date")
    if dated and not (c.get("summary") or ""):
        # The only reason a title with a tag, client and date still fails
        # strict; "non-standard format" would send somebody to the
        # separators.
        out.append("No description")
    # Everything is present and it still did not parse strictly, so the shape
    # is the complaint: the separators or the date's spelling.
    return out or ["Non-standard title format"]

def who_is_behind(their_v, our_v) -> str:
    """Which of the two machines has to update, said plainly.

    The version arrives as whatever was in the payload rather than as a
    number -- a malformed one is still worth reporting -- so this has to
    produce a sentence for values it cannot compare.
    """
    try:
        theirs, ours = int(their_v), int(our_v)
    except (TypeError, ValueError):
        return "one of the two boards needs updating"
    if theirs > ours:
        return "this machine is the older one, so update it here"
    if theirs < ours:
        return "the other machine is the older one, so they need to update"
    return "one of the two boards needs updating"


def unsent_mark(c):
    """The mark on a card holding a change that has not left this machine.

    Returns (glyph, colour, why), or None when the card owes nothing.

    Both debts `Bert._owed()` counts, and they must agree with it: queued
    events, and cards moved since the board was last published. A given-up row
    is a third state and must never read as waiting.
    """
    if c.get("stuck"):
        # Amber stays: this one is a caution, and amber is what a caution
        # looks like everywhere else on the board. And it must not say
        # "pushing", which is the one thing that is not happening.
        return "Not sent", T.AMBER_FG, ("Ernie gave up sending this. It will "
                                        "not retry on its own.")
    unsent, unshared = c.get("unsent") or 0, c.get("unshared")
    if not unsent and not unshared:
        return None
    if unsent and unshared:
        why = "not posted to the thread yet, and not on the shared board yet"
    elif unsent:
        why = "waiting to post to the thread"
    else:
        why = "waiting to reach the shared board"
    # The ink, not the accent: it reads roughly twice as well on every card
    # fill, and a card already wears its tag's colour, so a mark spending
    # none leaves colour meaning something.
    #
    # One sentence for all three waiting cases -- #ernie-state is a Discord
    # channel too -- with which of them it is left to the tooltip.
    return "Pushing to Discord…", T.INK, f"Changed here — {why}"


MIME = "application/x-bert-card"

# Only the fields the editor still sends a base snapshot for; work items are a
# list and merge on their own, so they never appear in this warning.
FIELD_LABELS = {"client_override": "the client", "title": "the thread title"}


_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# Qt's date control cannot be empty, so its floor stands for "no date" and
# the field says so with setSpecialValueText. Any real ticket date is above
# it by a century.
NO_DATE = QDate(1900, 1, 1)


def title_stamp(d):
    """The date form thread titles actually use, e.g. 25Aug26.

    Built by hand rather than with strftime: Windows raises on %y for any year
    before 1900, and a half-typed date in the title box parses to years like
    226, which crashed the validity check mid-keystroke.
    """
    return f"{d.day:02d}{_MONTH_ABBR[d.month - 1]}{d.year % 100:02d}"

VALUE_LABEL = {v: lab for v, lab in STATES + DIRECTIONS}
VALUE_LABEL[""] = "(empty)"


def period_name(start, bucket):
    """What to call one bar, in a column 244px wide.

    Three shapes, because the bucket follows the window: `9 Sep` for a day,
    `w/c 1 Sep` for a rolling seven days, and the month's own short name for
    a month. Never raises and never invents a date it was not given -- a
    string it cannot read comes back unchanged, which is a label somebody can
    at least recognise as wrong.
    """
    text = str(start or "")
    try:
        y, m, d = (int(x) for x in text.split("-")[:3])
        stamp = _MONTH_ABBR[m - 1]
    except (ValueError, IndexError, TypeError):
        return text
    if bucket == "month":
        return month_name(f"{y:04d}-{m:02d}")
    if bucket == "week":
        return f"w/c {d} {stamp}"
    return f"{d} {stamp}"


def month_name(month: str) -> str:
    """"2026-08" as "Aug", and "Jan 27" where the year turns over.

    The panel is 244px wide and the label sits beside a count, so the year is
    spelled only where leaving it out would put two Januaries side by side.
    """
    try:
        y, m = month.split("-")
        name = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[int(m) - 1]
    except (ValueError, IndexError, AttributeError):
        return str(month)
    return f"{name} {y[2:]}" if m == "01" else name


def tip_css() -> str:
    """The tooltip rule, for a widget that carries a stylesheet of its own.

    `apply_theme` states this on the application, and that is enough for a
    widget with no sheet. It is not enough for one that has its own: Qt
    resolves a tooltip against the nearest stylesheet in the widget's chain,
    so a container that names only itself leaves its rows' tooltips to
    whatever the platform draws -- which on a dark desktop is dark, and
    unreadable against the ink a light board asks for. Every scoped container
    below states it too.
    """
    return (f"QToolTip {{ color:{T.INK}; background-color:{T.SURFACE};"
            f" border:1px solid {T.LINE}; padding:4px 6px; }}")


def btn_css(chrome=False) -> str:
    """A plain button, drawn from the palette rather than left to the style.

    `BTN_HIT` sets the hit area and the type size and stops there, so Fusion
    supplied the rest -- and what Fusion supplies is a vertical grey gradient
    with a hard 1px bevel. Measured down a card, the Edit button ran from
    #F9FAFB to #C0C2C7 over 32 pixels: a gradient with more range in it than
    anything else on the board, on the one control that appears twice per
    card. It read as an old dialog dropped onto a clean surface, and it did
    the same to dark.

    Flat, on the surface tone, with the accent arriving only on hover. The
    border is the hairline every other edge on the board uses.
    """
    # The raised-control tone, not the surface: the surface *is* the card a
    # card's buttons are drawn on, so a button wearing it had only its border
    # to say it was a button. `chrome` picks the other ground -- see `field`.
    return (f"QPushButton {{ {BTN_HIT} background:"
            f"{T.BESIDE if chrome else T.CONTROL};"
            f" border:1px solid {T.LINE}; border-radius:{BTN_RADIUS}px;"
            f" color:{T.INK}; }}"
            f"QPushButton:hover {{ background:{rgba(T.ACCENT, 0.10)};"
            f" border-color:{rgba(T.ACCENT, 0.45)}; }}"
            f"QPushButton:pressed {{ background:{rgba(T.ACCENT, 0.18)}; }}"
            f"QPushButton:disabled {{ color:{T.MUTED};"
            f" background:transparent; border-color:{T.LINE}; }}")


def rgba(hex_colour, alpha):
    """A washed-out version of a palette colour, for hairlines."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def field(chrome=False) -> str:
    """Type into these. A function, not a constant: a constant would be built
    once at import, in whichever palette happened to be loaded first.

    `chrome` says the box is on the toolbar rather than on a card, and the two
    are different grounds. `control` is defined as a step *under* the card, so
    on chrome it has to clear `well` from the other side -- and that pinned
    `well` under it, which is what kept light's bar the heaviest thing in the
    window. `beside` is the Qt Button role and already the raised tone in both
    themes, so a control on chrome takes that instead.
    """
    return (f"background:{T.BESIDE if chrome else T.CONTROL};"
            f" border:1px solid {rgba(T.INK, 0.28)};"
            f" border-radius:5px; padding:4px 6px; color:{T.INK};")


def clip(text, width):
    """Shorten to width, ending on an ellipsis rather than mid-word rubbish."""
    text = (text or "").strip()
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def strip_lead(text, lead):
    """Drop a leading word the line has already said, so it isn't said twice.

    The feed line says "added a work item to X"; new_value says
    'added "the thing"'. Without this the row reads "added ... added".
    """
    return text[len(lead):].lstrip() if text.startswith(lead) else text


def show_value(v):
    return VALUE_LABEL.get(v or "", v)


def ago(secs):
    """A duration in seconds, said briefly."""
    if secs is None:
        return "a while"
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h"


def moments_ago(ts):
    """Minute-grained, for conflicts -- Card._ago only resolves to days."""
    if not ts:
        return ""
    try:
        then = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    if secs < 60:
        return "a moment ago"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


class Conflict(Exception):
    """A 409 from the API, carrying the structured body Bert renders."""

    def __init__(self, detail):
        self.detail = detail if isinstance(detail, dict) else {
            "code": "error", "message": str(detail)}
        super().__init__(self.detail.get("message", "Conflict"))

    @property
    def code(self):
        return self.detail.get("code", "error")


class ConflictDialog(QDialog):
    """Show what changed underneath before anyone overwrites anything."""

    def __init__(self, parent, detail):
        super().__init__(parent)
        self.setWindowTitle("Someone else changed this card")
        self.setMinimumWidth(520)
        dark_titlebar(self)
        self.choice = None

        lay = QVBoxLayout(self)

        who = detail.get("by") or "Someone else"
        head = QLabel(f"<b>{who}</b> changed this card while you had it open.")
        head.setWordWrap(True)
        lay.addWidget(head)

        when = detail.get("at")
        if when:
            sub = QLabel(moments_ago(when))
            sub.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
            lay.addWidget(sub)

        grid = QFormLayout()
        grid.setSpacing(6)
        for ch in detail.get("changes", []):
            box = QVBoxLayout()
            theirs = QLabel(f"Theirs:  {show_value(ch.get('theirs'))}")
            theirs.setStyleSheet(f"color:{T.AMBER_FG}; font-size:12px;")
            mine = QLabel(f"Mine:    {show_value(ch.get('mine'))}")
            mine.setStyleSheet(f"color:{T.ACCENT}; font-size:12px;")
            box.addWidget(theirs)
            box.addWidget(mine)
            holder = QWidget()
            holder.setLayout(box)
            grid.addRow(f"{ch.get('label', ch.get('field'))}", holder)
        lay.addLayout(grid)

        note = QLabel("Overwriting replaces their value with yours. Keeping "
                      "theirs discards what you typed.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
        lay.addWidget(note)

        row = QHBoxLayout()
        row.addStretch()
        keep = QPushButton("Keep theirs")
        keep.clicked.connect(lambda: self._pick("keep"))
        row.addWidget(keep)
        over = QPushButton("Overwrite with mine")
        over.setStyleSheet(f"background:{T.ACCENT}; color:{T.ON_ACCENT}; border:none;"
                           f" padding:5px 14px;")
        over.clicked.connect(lambda: self._pick("overwrite"))
        row.addWidget(over)
        lay.addLayout(row)

    def _pick(self, what):
        self.choice = what
        self.accept()


def load_settings() -> dict:
    if SETTINGS.exists():
        try:
            return json.loads(SETTINGS.read_text())
        except json.JSONDecodeError:
            pass
    return {}


class SettingsDialog(QDialog):
    """Name, theme, and which build this is.

    The build goes here because this is the one window somebody already opens
    to answer a question about their own copy, and because in the
    one-backend-two-Berts setup the two halves are two checkouts: the person
    on the far end can be running a Bert from last week against somebody
    else's Ernie, and until now neither end could say so.
    """

    # A third way out, besides OK and Cancel: "show me". Picking a theme
    # closes the dialog with this, the window is rebuilt in that palette, and
    # the dialog opens again on top of it -- so from the outside the board
    # simply changes under an open Settings window.
    PREVIEW = 2

    def __init__(self, parent, current, health=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(360)
        dark_titlebar(self)
        self.who = QLineEdit(current.get("name") or
                             f"{current.get('first_name', '')} "
                             f"{current.get('last_name', '')}".strip())
        self.who.setPlaceholderText("First Last")
        self.theme = Combo()
        for key in THEMES:
            self.theme.addItem(THEME_LABEL[key], key)
        stored = current.get("theme", THEME_DEFAULT)
        self.theme.setCurrentIndex(
            THEMES.index(stored) if stored in THEMES else 0)
        # Connected *after* the index is set, or building the dialog would
        # fire it and ask for a preview of the theme already on screen.
        #
        # And only when the choice would actually look different: picking
        # "Follow the desktop" on a machine whose desktop is already dark
        # changes what is stored and changes nothing to look at, so there is
        # nothing to preview and no reason to make the dialog blink.
        self.theme.currentIndexChanged.connect(self._preview)
        self.entry = Combo()
        for key in ENTRY_MODES:
            self.entry.addItem(ENTRY_LABEL[key], key)
        mode = current.get("entry", ENTRY_DEFAULT)
        self.entry.setCurrentIndex(
            ENTRY_MODES.index(mode) if mode in ENTRY_MODES else 0)
        form = QFormLayout()
        form.addRow("Your name", self.who)
        form.addRow("Theme", self.theme)
        form.addRow("Ticket entry", self.entry)
        note = QLabel("Your name is added to thread updates so the team can see "
                      "who made each change. Changes are blocked until it's "
                      "set.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")

        # Both ends, always, rather than one line when they agree: a reader
        # who sees a single version has to know it stands for two things.
        # Ernie's comes off /health, so it is genuinely what answered, not
        # what this copy of the source happens to say.
        theirs = ((health or {}).get("build") or {}).get("version")
        their_commit = ((health or {}).get("build") or {}).get("commit")
        said = (f"{theirs} ({their_commit})" if theirs and their_commit
                else theirs or "not reported")
        build = QLabel(f"Bert {ernie_version.describe()}\nErnie {said}")
        build.setTextInteractionFlags(Qt.TextSelectableByMouse)
        build.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
        form.addRow("Version", build)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(bb)

    def _preview(self, *_):
        if resolve_theme(self.theme.currentData()) != T.name:
            self.done(self.PREVIEW)

    def values(self):
        return {"name": self.who.text().strip(),
                "theme": self.theme.currentData(),
                "entry": self.entry.currentData()}


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=8.0)

    def board(self):
        return self.client.get(f"{self.base}/cards").json()

    def events(self, limit=FEED_LIMIT):
        return self.client.get(f"{self.base}/events", params={"limit": limit}).json()

    def health(self):
        return self.client.get(f"{self.base}/health").json()

    def roster(self):
        return self.client.get(f"{self.base}/clients/roster").json()

    def stats(self, days=STATS_WINDOW_DEFAULT):
        return self.client.get(f"{self.base}/stats",
                               params={"days": days}).json()

    def new_ticket(self, fields, actor):
        return self._post("/tickets", {**fields, "actor": actor})

    def _post(self, path, payload):
        payload.setdefault("key", str(uuid.uuid4()))
        r = self.client.post(f"{self.base}{path}", json=payload)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            if r.status_code == 409:
                raise Conflict(detail)
            raise RuntimeError(detail if isinstance(detail, str)
                               else detail.get("message", str(detail)))
        return r.json()

    def move(self, tid, priority, after, before, actor):
        return self._post(f"/cards/{tid}/move", {
            "priority": priority, "after_id": after,
            "before_id": before, "actor": actor})

    def edit(self, tid, fields, actor, base=None, force=False):
        return self._post(f"/cards/{tid}/edit",
                          {**fields, "actor": actor, "base": base, "force": force})

    def work_done(self, tid, item_id, actor):
        return self._post(f"/cards/{tid}/work/{item_id}/done", {"actor": actor})

    def complete(self, tid, actor):
        return self._post(f"/cards/{tid}/complete", {"actor": actor})

    def reopen(self, tid, actor):
        return self._post(f"/cards/{tid}/reopen", {"actor": actor})

    def undo(self, eid, actor, force=False):
        return self._post(f"/events/{eid}/undo", {"actor": actor, "force": force})


class Poller(QThread):
    loaded = Signal(dict)
    failed = Signal(str)

    def __init__(self, api, want_roster=False, want_stats=False,
                 stats_days=STATS_WINDOW_DEFAULT):
        super().__init__()
        self.api = api
        # The figures move when a ticket closes or ages a day, not every five
        # seconds, so they ride the slow lane with the roster rather than the
        # poll the board depends on.
        self.want_stats = want_stats
        self.stats_days = stats_days
        # The customer list is 65 rows that change about hourly, so it does
        # not ride the five-second poll. It comes back on the polls that ask,
        # and it comes back here rather than on demand because fetching it
        # when an editor opens would block the window on an HTTP call.
        self.want_roster = want_roster

    def run(self):
        try:
            p = {"board": self.api.board(),
                 "events": self.api.events(),
                 "health": self.api.health()}
            if self.want_roster:
                # A stack with no Jira configured serves an empty list, and an
                # older Ernie has no such route at all. Neither is a reason to
                # fail the poll the board depends on.
                try:
                    p["roster"] = self.api.roster().get("clients") or []
                except Exception:
                    p["roster"] = []
            if self.want_stats:
                # An older Ernie has no such route. That is not a reason to
                # fail the poll the board depends on.
                try:
                    p["stats"] = self.api.stats(self.stats_days)
                except Exception:
                    p["stats"] = None
            self.loaded.emit(p)
        except Exception as e:
            self.failed.emit(str(e))


def warning_row(text, fg, size=11):
    """A caution sign beside a wrapped message.
    """
    row = QWidget()
    row.setStyleSheet("background:transparent;")
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)

    sign = QLabel("\u26a0")
    sign.setStyleSheet(f"color:{fg}; font-size:{size + 1}px;"
                       f" background:transparent; min-height:{size + 7}px;")
    sign.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    sign.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)
    h.addWidget(sign)

    body = QLabel(text)
    body.setWordWrap(True)
    body.setStyleSheet(f"color:{fg}; font-size:{size}px; background:transparent;")
    h.addWidget(body, 1)
    row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    return row


def chrome_button(glyph, tip):
    """A square button carrying one symbol: refresh, settings.
    """
    b = QPushButton(glyph)
    b.setFixedSize(30, 28)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip(tip)
    # 14px leaves the glyph box room inside a 28px button -- the lesson the
    # caution sign taught, where the two were the same height and it clipped.
    b.setStyleSheet("font-size:14px;")
    return b


def fold_button(glyph, tip):
    """The control that folds a section away, and there are three of them.

    Written once because it was written three times: the running order, the
    figures and the activity feed are the window's three foldable sections,
    and a reader who has learnt one of these has learnt all three. They had
    drifted already -- two of them differed only in the alpha of the hover
    tint, 0.12 against 0.14, which is nobody's intention and nothing anybody
    would notice going wrong.

    The glyph is the section's own, because each folds toward a different
    edge: the rail toward the left, the figures toward the right, the feed
    toward the bottom.
    """
    b = QPushButton(glyph)
    b.setFixedSize(24, 24)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip(tip)
    b.setStyleSheet(
        f"QPushButton {{ border:1px solid {T.LINE}; border-radius:5px;"
        f" background:{T.CONTROL}; color:{T.MUTED}; font-size:11px; }}"
        f"QPushButton:hover {{ background:{rgba(T.ACCENT, 0.12)};"
        f" color:{T.ACCENT}; }}")
    return b


def tick_icon(colour, side=12):
    """A check mark.
    """
    scale = 2                       # drawn at 2x so it stays sharp when scaled
    pm = QPixmap(side * scale, side * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(colour), 2.0 * scale)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    n = side * scale
    p.drawPolyline([QPoint(int(n * 0.18), int(n * 0.52)),
                    QPoint(int(n * 0.42), int(n * 0.76)),
                    QPoint(int(n * 0.84), int(n * 0.24))])
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


def spin_icon(angle, colour=None, side=14):
    """The refresh arrow, at one point in its turn.

    Drawn rather than typed, and for a sharper reason than the tick was: a
    rotated character is a glyph under a transform, and a font engine that
    will not draw one draws nothing at all -- no glyph, no error, just a
    button with no icon on it. An arc has no such opinion, and the turn
    becomes where the arc starts rather than a transform over the painter.

    Colour is resolved on the call, never in the signature: a default
    argument is evaluated when this file is imported, which is before any
    theme has been chosen.
    """
    scale = 2                       # drawn at 2x so it stays sharp when scaled
    n = side * scale
    pm = QPixmap(n, n)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    ink = QColor(colour or T.INK)
    stroke = 1.8 * scale
    pen = QPen(ink, stroke)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)

    # Inset by the stroke and the head, so neither is clipped at the edge.
    m = n * 0.19
    box = QRectF(m, m, n - 2 * m, n - 2 * m)
    # Qt counts sixteenths of a degree, anticlockwise from 3 o'clock. Sweeping
    # negative draws it clockwise, which is the way the glyph's arrow pointed.
    sweep = -260
    p.drawArc(box, int(-angle * 16), sweep * 16)

    # The head sits at the open end, pointing along the tangent there.
    cx, cy, r = n / 2, n / 2, (n - 2 * m) / 2
    theta = math.radians(-angle + sweep)
    px, py = cx + r * math.cos(theta), cy - r * math.sin(theta)
    tx, ty = math.sin(theta), math.cos(theta)       # tangent, clockwise
    nx, ny = math.cos(theta), -math.sin(theta)      # outward normal
    h, w = n * 0.17, n * 0.13
    p.setPen(Qt.NoPen)
    p.setBrush(ink)
    p.drawPolygon(QPolygonF([
        QPointF(px + tx * h, py + ty * h),
        QPointF(px - tx * h * 0.3 + nx * w, py - ty * h * 0.3 + ny * w),
        QPointF(px - tx * h * 0.3 - nx * w, py - ty * h * 0.3 - ny * w),
    ]))
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


def chip(text, bg, fg, dashed=False):
    lab = QLabel(text)
    lab.setStyleSheet(
        f"background:{bg}; color:{fg}; border:1px "
        f"{'dashed' if dashed else 'solid'} {fg}44; border-radius:5px;"
        f"padding:1px 6px; font-size:11px;")
    lab.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
    return lab


def age_css():
    """How the age reads on a card. A function, like `btn_css()`, because the
    colour comes off the palette in force and a string frozen at import would
    be whichever theme the module happened to be imported under."""
    return f"color:{T.MUTED}; font-size:11px; background:transparent;"


def elided_chip(text, bg, fg, room):
    """A chip cut to the room it is given, with the whole of it on hover.

    `chip()` is a QLabel, and an unwrapped QLabel cannot be made narrower
    than its own text -- it reports that width as a floor and shoves whatever
    comes after it off the end of the row. That is the feed's rule arrived at
    on a card: **the text is what gives way, never the controls.**

    Cut against the font it actually draws in, the way a rail row and the
    client name are, rather than at a character count -- these sit in a
    stylesheet with padding and a border, so the chrome is measured off the
    hint rather than assumed.
    """
    lab = chip(text, bg, fg)
    lab.ensurePolished()
    fm = QFontMetrics(lab.font())
    chrome = lab.sizeHint().width() - fm.horizontalAdvance(text)
    if lab.sizeHint().width() > room:
        cut = fm.elidedText(text, Qt.ElideRight, max(room - chrome, 0))
        lab.setText(cut)
        # Only when something was actually hidden: a tooltip repeating the
        # words under it is noise on every card that has one.
        if cut != text:
            lab.setToolTip(text)
    return lab


def ticket_url(base, key):
    """Where a PIP key lives, or "" if it cannot be said.
    """
    if not base or not key:
        return ""
    return f"{base.rstrip('/')}/browse/{key}"


class LinkChip(QLabel):
    """A chip that opens something in the browser.
    """

    def __init__(self, text, url, tip=""):
        super().__init__(text)
        self.url = url
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tip or url)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        self._paint(False)

    def _paint(self, hover):
        self.setStyleSheet(
            f"background:{T.CHIP_BG}; color:{T.ACCENT};"
            f" border:1px solid {T.ACCENT}55; border-radius:5px;"
            f" padding:1px 6px; font-size:11px;"
            f"{' text-decoration:underline;' if hover else ''}")

    def enterEvent(self, e):
        self._paint(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._paint(False)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        # On release rather than press, so a click begun and dragged away
        # does not fire -- the behaviour every other clickable thing has.
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            QDesktopServices.openUrl(QUrl(self.url))
        super().mouseReleaseEvent(e)


class ClickableWidget(QWidget):
    """A plain widget that reports left clicks -- used for band headers."""
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class ClickLabel(QLabel):
    """A label that reports left clicks -- used for the attention count."""
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class FeedRow(ClickableWidget):
    """One line of the activity feed, with a hairline under it.
    """

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setPen(QColor(T.LINE))
        y = self.height() - 1
        p.drawLine(0, y, self.width(), y)


class ClickableLabel(QLabel):
    """Double-click jumps straight into edit mode on that field.

    Reports no minimum width: a plain QLabel claims its whole text as a
    floor and shoves the card's fixed corner off the end. Measured with a
    real customer -- 'Municipal Authority of Westmoreland County' wants
    408px inside a 300px card.
    """
    doubleClicked = Signal()

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()


class Combo(QComboBox):
    """A combo box that never changes value on the wheel.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Default is WheelFocus, which would let a stray wheel turn take focus.
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, e):
        e.ignore()


# How wanted a match is, most wanted first. Tiered rather than one number so
# a hit on the customer's own name always outranks a hit on a note about them:
# 'Abay Construction *Working under Trekk*' contains Trekk, and typing "trek"
# must offer Trekk first and Abay second, not the other way round.
_M_EXACT, _M_PREFIX, _M_NAME, _M_ALIAS, _M_SUMMARY = 3.0, 2.5, 2.2, 2.0, 1.8
CLIENT_FUZZY_MIN = 0.72    # below this a near miss is just a different word
CLIENT_HITS = 8            # a popup you scan, not a list you read
SEP = chr(0xB7)            # middle dot, as the shared-board label uses


def client_squash(text: str) -> str:
    """A client name with the punctuation taken out.

    Every miss measured on the real board was punctuation, not letters: the
    apostrophe in Duke's, the dot in Inspect.AI, the hyphen in Eight-Eleven.
    Somebody typing "dukes" is not making a mistake worth correcting, they are
    typing the name without the apostrophe, and the box should find it.
    """
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def client_label(c) -> str:
    """How one client reads in the list.

    Two live customers can shorten to the same word -- IPI is both PIP-2136
    and PIP-3927 -- so those carry their Jira summary and nothing else does.
    """
    short = (c.get("short_name") or "").strip()
    return (f"{short}  " + SEP + f"  {c['name']}"
            if c.get("ambiguous") else short)


# A word shorter than this is not compared loosely: `SCI`, `RJN`, `GFT` are
# whole customer names, and at three letters almost anything resembles almost
# anything. The exact, prefix and contains tiers already catch those.
FUZZY_WORD_MIN = 4


def client_words(text: str) -> list:
    """A name as its words, squashed, long enough to be worth comparing.

    Names are typed in part far more often than in full -- the first word and
    then a guess, or the first word and then the wrong thing entirely.
    """
    return [w for w in (client_squash(x) for x in str(text or "").split())
            if len(w) >= FUZZY_WORD_MIN]


def client_fuzzy(q: str, typed: str, keys: list, raws: list) -> float:
    """How much what was typed resembles one client, at its best.
    """
    best = max((difflib.SequenceMatcher(None, q, k).ratio()
                for k in keys if k), default=0.0)
    qw = client_words(typed)
    kw = [w for raw in raws for w in client_words(raw)]
    if qw and kw:
        best = max(best, max(difflib.SequenceMatcher(None, a, b).ratio()
                             for a in qw for b in kw))
    return best


def client_matches(typed, roster, limit=CLIENT_HITS):
    """The clients worth offering for what has been typed so far.

    Searches the customer's name, their Jira summary, and every spelling the
    board has ever used for them -- the alias table already knows the
    misspellings, so a name that was typed wrong last year finds the right
    customer today.
    """
    q = client_squash(typed)
    if not q:
        return []
    out = []
    for c in roster or []:
        short = client_squash(c.get("short_name"))
        names = [client_squash(a) for a in (c.get("aliases") or [])]
        summary = client_squash(c.get("name"))
        if short == q:
            score = _M_EXACT
        elif short.startswith(q):
            score = _M_PREFIX
        elif q in short:
            score = _M_NAME
        elif any(q in a for a in names):
            score = _M_ALIAS
        elif q in summary:
            score = _M_SUMMARY
        else:
            # Only now is it worth the cost, and only against what the client
            # is actually called -- fuzzy against a whole summary matches
            # anything long enough.
            best = client_fuzzy(q, typed, [short] + names,
                                [c.get("short_name") or ""]
                                + list(c.get("aliases") or []))
            if best < CLIENT_FUZZY_MIN:
                continue
            score = best
        out.append((score, (c.get("short_name") or "").lower(), c))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in out[:limit]]


def client_known(typed, roster) -> bool:
    """Whether the roster has heard of this name, punctuation aside.

    Aliases count. `Dukes Root Control` is a misspelling the board has used
    nine times and the alias table already points it at PIP-8605, so somebody
    typing it is naming a customer -- badly, but nameably. It is a name that
    resolves, which is the question here.
    """
    q = client_squash(typed)
    if not q:
        return False
    for c in roster or []:
        if q == client_squash(c.get("short_name")):
            return True
        if any(q == client_squash(a) for a in (c.get("aliases") or [])):
            return True
    return False


def client_stands_for(typed, short) -> bool:
    """Whether what was typed is this customer's name rather than a slip at it.
    """
    a, b = client_squash(typed), client_squash(short)
    if not a or not b:
        return False
    if a == b or b.startswith(a):
        return True
    # Whole words, in order: "trekk design" for "Trekk Design Group".
    want = [w for w in re.split(r"[^a-z0-9]+", (typed or "").lower()) if w]
    have = [w for w in re.split(r"[^a-z0-9]+", (short or "").lower()) if w]
    i = 0
    for w in have:
        if i < len(want) and want[i] == w:
            i += 1
    return i == len(want) and bool(want)


def client_resolve(typed, roster, opened_with="") -> str:
    """The name to save instead of what was typed, or "" to keep it as typed.
    """
    typed = (typed or "").strip()
    if not typed:
        return ""
    if client_squash(typed) == client_squash(opened_with):
        return ""
    # The customer's own name stands; an alias does not.
    #
    # Correcting through an alias is the safest case there is: an alias names
    # one client outright, so there is nothing to guess.
    if any(client_stands_for(typed, c.get("short_name")) for c in roster or []):
        return ""
    # Two, so "exactly one" can be told apart from "more than one".
    hits = client_matches(typed, roster, limit=2)
    if len(hits) != 1:
        return ""
    name = (hits[0].get("short_name") or "").strip()
    if not name or client_squash(name) == client_squash(typed):
        return ""
    return name


def client_note(typed, roster, opened_with="") -> str:
    """The caution under the Client box, or nothing.

    Only about what somebody **just typed**. A card already carrying a name
    the roster has never heard of -- a retired customer, or one from before
    the roster existed -- is not a mistake anybody is making now, and warning
    every time that card is opened is nagging rather than helping.

    A caution and not a refusal. A customer exists before Jira hears about
    them, and the box is pick-or-type for that reason; this only says which
    of the two just happened, so a slip is caught at the moment it is made
    rather than at the moment somebody reads the board.
    """
    typed = (typed or "").strip()
    if not typed:
        return ""
    if client_squash(typed) == client_squash(opened_with):
        return ""
    # One candidate and the save will take it, so the box says so *before*
    # the save rather than after.
    fixed = client_resolve(typed, roster, opened_with)
    if fixed:
        return f"will be saved as {fixed}"
    if client_known(typed, roster):
        return ""
    return "not a customer Jira knows \u2014 it will be typed as-is"


class ClientCombo(Combo):
    """The customer list, picked rather than typed.

    Editable on purpose. The list is what Jira knows about, and a customer
    exists before Jira hears about them -- so this offers the roster and still
    takes anything, the way QUEUES_OFFERED is narrower than QUEUES without
    stopping a card from carrying a tag nobody offers any more.

    It answers to text()/setText() so the editor's save() and is_dirty() read
    it exactly as they read the box it replaced; those two are the same
    statement twice and have to stay that way.
    """

    def __init__(self, roster, current="", parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)

        self.addItem("", "")
        offered = set()
        for c in roster or []:
            short = (c.get("short_name") or "").strip()
            if not short:
                continue
            # Two live customers can shorten to the same label -- 'IPI : El
            # Paso' and 'IPI : *REP*' both read as IPI -- and a list with the
            # same word twice is worse than the typos this replaces. The full
            # summary goes on the line for those, and the short name is still
            # what lands in the title.
            label = client_label(c)
            self.addItem(label, short)
            self.setItemData(self.count() - 1, c.get("name"), Qt.ToolTipRole)
            offered.add(short.lower())

        # A client the roster does not offer -- retired, paused, or never
        # listed -- stays on the card it is already on, the same rule the
        # queue dropdown follows for a retired tag.
        #
        # Shown, not offered: put in the box rather than added to the list.
        # Bert cannot tell a retired client from a typo -- both are strings
        # the roster has never heard of -- so it declines to dress either as
        # a choice.
        self._unlisted = bool(current) and current.lower() not in offered

        # The popup is filled by client_matches rather than filtered by the
        # completer, because the completer can only match the strings in the
        # list -- and half of what people type is not in it. 'dukes' is not a
        # substring of "Duke's Root Control", 'inspect ai' is not one of
        # 'Inspect.AI', and 'monaloh' appears only in MBE's Jira summary.
        # Unfiltered means the popup shows exactly what was put in it, in the
        # order it was put in.
        self._roster = list(roster or [])
        self._short_of = {client_label(c): (c.get("short_name") or "")
                          for c in self._roster}
        comp = QCompleter([], self)
        comp.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.activated[str].connect(self._took_completion)
        self.setCompleter(comp)
        self._comp = comp
        # textEdited, so rebuilding the list is never mistaken for typing.
        self.lineEdit().textEdited.connect(self._offer)

        if self._unlisted:
            # Nothing to select, so put the text in the box directly. text()
            # reads the line edit, so save() and is_dirty() see it either way.
            self.setCurrentIndex(0)
            self.setEditText(current)
        else:
            self.setCurrentIndex(
                max(self.findData(current), 0) if current else 0)
        self.setEditText(current)
        # Picking the disambiguated line must put the short name in the box,
        # not the whole line including the summary.
        self.activated.connect(self._took_pick)

    def _offer(self, text):
        """Put the clients worth considering into the popup, best first."""
        hits = client_matches(text, self._roster)
        self._comp.setModel(
            QStringListModel([client_label(c) for c in hits], self._comp))
        if hits:
            self._comp.complete()

    def _took_completion(self, label):
        """A pick from the popup leaves the short name, not the whole line."""
        self.setEditText(self._short_of.get(label, label))

    def _took_pick(self, index):
        self.setEditText(self.itemData(index) or "")

    def text(self) -> str:
        return self.currentText()

    def setText(self, value: str) -> None:
        self.setEditText(value or "")


def plain_cursors(parent):
    """Give the controls on a card their own cursors.
    """
    for w in parent.findChildren(QLineEdit):
        w.setCursor(Qt.IBeamCursor)
    for cls in (QPushButton, QComboBox, QCheckBox):
        for w in parent.findChildren(cls):
            w.setCursor(Qt.PointingHandCursor)


class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto the next line.
    """

    def __init__(self, parent=None, spacing=5):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    # -- the five QLayout has to be given ----------------------------------
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations()

    # -- wrapping ----------------------------------------------------------
    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._run(QRect(0, 0, width, 0), place=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._run(rect, place=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _run(self, rect, place):
        """Lay the items out, or -- with place=False -- only measure them."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if line_h and x + hint.width() > area.right() + 1:
                x, y = area.x(), y + line_h + self.spacing()
                line_h = 0
            if place:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class Bubble(QFrame):
    """One work item.

    A ticked one stays on the card, in green. It used to vanish -- the API
    filtered on done_at IS NULL -- and the bubble was the only record that the
    work had happened, so ticking the last one left a card saying nothing
    about what had been done on it.

    In the editor it goes quieter and takes a dashed border, the way an empty
    band's slot does: there to be read, and removed if it should not be there,
    rather than worked on.
    """

    acted = Signal(str)          # this bubble's key
    reopened = Signal(str)       # a finished one, double-clicked

    def __init__(self, key, body, editing, done=False):
        super().__init__()
        self.key = key
        self.body = body
        self.done = done
        self.setObjectName("bubble")
        if done:
            fill = "transparent" if editing else T.OK_BG
            edge = "dashed" if editing else "solid"
            self.setStyleSheet(
                f"#bubble {{ background:{fill};"
                f" border:1px {edge} {rgba(T.OK_FG, 0.55)};"
                f" border-radius:11px; }}")
            ink = T.OK_FG
        else:
            # A step under the card, not a tint of it: the card underneath
            # wears its queue's colour, and a pale blue bubble all but
            # disappeared on a blue ENG card. Neutral and one level down
            # reads on every fill in both palettes, which a tint cannot.
            self.setStyleSheet(
                f"#bubble {{ background:{T.CONTROL};"
                f" border:1px solid {rgba(T.INK, 0.16)}; border-radius:11px; }}")
            ink = T.INK

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 2, 3, 2)
        h.setSpacing(4)

        lab = QLabel(body)
        lab.setStyleSheet(f"color:{ink}; font-size:11px;"
                          f" background:transparent; border:none;")
        h.addWidget(lab)

        self.btn = QPushButton("\u2715" if editing else "\u2713")
        # Smaller than the buttons elsewhere on the card, but the square is the
        # hit area and only the glyph inside it has to stay quiet.
        self.btn.setFixedSize(22, 22)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setToolTip("Remove this item" if editing else "Mark this done")
        hover, ink = (T.RED_BG, T.RED_FG) if editing else (T.HOVER_BG, T.OK_FG)
        self.btn.setStyleSheet(
            f"QPushButton {{ border:none; border-radius:11px; font-size:11px;"
            f" background:transparent; color:{T.MUTED}; }}"
            f"QPushButton:hover {{ background:{hover}; color:{ink}; }}"
            f"QPushButton:disabled {{ color:{T.LINE}; background:transparent; }}")
        self.btn.clicked.connect(lambda: self.acted.emit(self.key))
        h.addWidget(self.btn)

        if done:
            # Double-click, not single: a stray click must not put finished
            # work back, and the x beside it is one click away. The second
            # click is the confirmation -- a dialog would tax every tick to
            # guard against the rare wrong one.
            self.setToolTip("Double-click to put this back to outstanding")

        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)

    def mouseDoubleClickEvent(self, e):
        if self.done and e.button() == Qt.LeftButton:
            self.reopened.emit(self.key)
        super().mouseDoubleClickEvent(e)


class FeedLine(QLabel):
    """One activity row's text.

    It claims the width of the line it holds, so the chevron after it sits
    against the end of the text rather than across the row -- but reports no
    minimum, so the layout can squeeze it when the window is narrow. An
    ordinary QLabel cannot be made narrower than its text, which pushed the
    row's controls off the edge; QSizePolicy.Ignored fixes that but discards
    the width it wants as well, leaving the label nothing at all.
    """

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())


class TagEdit(QLineEdit):
    """The box new work items get typed into.

    Backspace in an empty box takes the last bubble back off, the way every
    other tag field behaves. Without it, fixing something entered a second ago
    means reaching for the mouse.
    """

    backspaced = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Backspace, Qt.Key_Delete) and not self.text():
            self.backspaced.emit()
            return
        super().keyPressEvent(e)


class WorkBar(QWidget):
    """The work items on a card, as bubbles.

    Editing is local: nothing is sent until Save, so the bar only remembers
    what got typed and what got crossed off and hands both to Card.save().
    That keeps a four-bubble edit inside the one batched write.
    """

    ticked = Signal(str)         # view mode only: the item_id to close out

    def __init__(self, items, editing):
        super().__init__()
        self.editing = editing
        self._rows = [{"key": i["item_id"], "item_id": i["item_id"],
                       "body": i["body"], "done": i.get("done", False)}
                      for i in items]
        self._removed = []       # item_ids of stored bubbles crossed off
        self._undone = []        # item_ids put back to outstanding
        self._new = 0            # counter behind the keys of unsaved bubbles

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(5)
        self.holder = QWidget()
        # Both of these sit on a tinted card and must let it through; without
        # it they paint the default window grey in a block behind the bubbles.
        self.setStyleSheet("background:transparent;")
        self.holder.setStyleSheet("background:transparent;")
        self.flow = FlowLayout(self.holder)
        outer.addWidget(self.holder)

        self.entry = None
        if editing:
            self.entry = TagEdit()
            self.entry.setPlaceholderText("Type an item and press Enter")
            # A darker frame than the fields above it, and a prompt in the
            # ordinary muted text colour rather than Qt's near-invisible grey:
            self.entry.setStyleSheet(
                f"QLineEdit {{ background:{T.SURFACE}; border-radius:5px;"
                f" padding:4px 6px; font-size:11px;"
                f" border:1px solid {rgba(T.INK, 0.38)}; }}"
                f"QLineEdit:hover {{ border:1px solid {T.ACCENT}; }}"
                f"QLineEdit:focus {{ border:1px solid {T.ACCENT}; }}")
            pal = self.entry.palette()
            pal.setColor(QPalette.PlaceholderText, QColor(T.MUTED))
            self.entry.setPalette(pal)
            self.entry.setCursor(Qt.IBeamCursor)
            self.entry.returnPressed.connect(self.commit_typed)
            self.entry.backspaced.connect(self._drop_last)
            outer.addWidget(self.entry)

        self._draw()

    # -- what Card.save() asks for -----------------------------------------
    def added(self):
        """Bubbles typed this session, including one still sitting in the box.

        Forgetting to press Enter before hitting Save is the obvious way to
        lose a work item, so the half-entered one counts.
        """
        pending = self.entry.text().strip() if self.entry else ""
        typed = [r["body"] for r in self._rows if r["item_id"] is None]
        return typed + ([pending] if pending else [])

    def removed(self):
        return list(self._removed)

    def undone(self):
        """Finished bubbles double-clicked back to outstanding.

        Held here rather than sent on the click, because the editor's whole
        contract is that nothing leaves until Save -- so Cancel takes this
        back too, the way it takes back a typed bubble.
        """
        return list(self._undone)

    # -- editing -----------------------------------------------------------
    def commit_typed(self):
        text = self.entry.text().strip()
        if not text:
            return
        self._new += 1
        self._rows.append({"key": f"new-{self._new}", "item_id": None,
                           "body": text, "done": False})
        self.entry.clear()
        self._draw()

    def _reopen(self, key):
        """A finished bubble, double-clicked: outstanding again."""
        for row in self._rows:
            if row["key"] == key and row.get("done"):
                row["done"] = False
                if row["item_id"] and row["item_id"] not in self._undone:
                    self._undone.append(row["item_id"])
                self._draw()
                return

    def _drop_last(self):
        if self._rows:
            self._forget(self._rows[-1]["key"])

    def _acted(self, key):
        if not self.editing:
            self.ticked.emit(key)     # in view mode the key is the item_id
            return
        self._forget(key)

    def _forget(self, key):
        row = next((r for r in self._rows if r["key"] == key), None)
        if row is None:
            return
        if row["item_id"]:
            self._removed.append(row["item_id"])
        self._rows.remove(row)
        self._draw()

    # -- drawing -----------------------------------------------------------
    def _draw(self):
        while self.flow.count():
            w = self.flow.takeAt(0).widget()
            if w is not None:
                # Unparent before deleteLater.
                w.hide()
                w.setParent(None)
                w.deleteLater()
        for row in self._rows:
            b = Bubble(row["key"], row["body"], self.editing,
                       row.get("done", False))
            b.acted.connect(self._acted)
            b.reopened.connect(self._reopen)
            self.flow.addWidget(b)
        # An empty holder still claims a row's worth of height, which reads as
        # a gap nobody put there.
        self.holder.setVisible(bool(self._rows))
        self.holder.updateGeometry()
        self.updateGeometry()
        if self.entry is not None:
            self.entry.setFocus()

    def set_enabled(self, ok):
        """Grey the ticks out while the board can't write."""
        for b in self.holder.findChildren(Bubble):
            if b.btn is not None:      # a finished bubble has no tick
                b.btn.setEnabled(ok)


# Longest first. The toolbar picks the longest that fits the box it is drawn
# in, the way a rail row picks how much of a client name it can show.
SEARCH_HINTS = ("Search client, equipment, summary",
                "Search client, equipment",
                "Search tickets",
                "Search")


def status_forms(text):
    """Every way of writing a toolbar status, longest first.

    The two indicators are the only things on that bar whose words can be
    given up: everything else is a control, and the board's own count is a
    number. So they shorten rather than being cut -- and they shorten by
    dropping the *noun*, never the state, because the state is the whole
    point of them. "shared board" is already established by the time somebody
    has read it once, and both labels carry the full sentence in a tooltip.

    A clip would have taken the other end: `shared board · up to da` was what
    the bar actually did at 1000px, which is the reading that matters cut off
    in favour of the words that are the same every time.
    """
    forms = [text]
    if text.startswith("shared board · "):
        forms.append("shared · " + text.split("· ", 1)[1])
    elif text.startswith("synced "):
        forms.append(text[len("synced "):])
    elif text.endswith(" attention"):
        # The count is the part that cannot go: it is red, it is clickable,
        # and the tooltip carries the sentence. So the words go first and the
        # number last, which is the rule the other two follow.
        stem = text[:-len(" attention")]
        forms.append(stem + " you")
        forms.append(stem.rsplit(" need", 1)[0])
    return forms


def attention_text(n, total=None) -> str:
    """`1 needs attention`, `2 need attention`, and nothing at nought.
    """
    if not n:
        return ""
    count = f"{n} of {total}" if total and total != n else str(n)
    return f"{count} need{'s' if n == 1 else ''} attention"


def a_few(n, one: str, many: str) -> str:
    """`1 change`, `3 changes` -- the noun and its verb, agreeing.
    """
    return f"{n} {one if n == 1 else many}"


# The equipment the board can be narrowed to, and what each covers.
#
# The names are what people say; `eq_type` is what `ernie_extract` reads off
# a title, and the two are not the same word. Bot covers SSD and LED because
# production calls both bots; splitting LED out is one line here.
#
# A thread can carry several pieces, so a card matching *any* selected type
# is shown rather than needing all of them.
EQUIPMENT_FILTERS = (
    ("Bot", ("SSD", "LED")),
    ("E-Reels", ("EReel",)),
    ("ODE", ("ODE",)),
    ("OLK", ("OLK",)),
)


def equipment_types(chosen) -> set:
    """The `eq_type` values behind a set of chip labels."""
    return {t for name, types in EQUIPMENT_FILTERS if name in chosen
            for t in types}


def equipment_counts(cards) -> dict:
    """How many open tickets carry each kind, across the whole board.
    """
    out = {name: 0 for name, _ in EQUIPMENT_FILTERS}
    for c in cards:
        if c.get("completed_at"):
            continue
        types = {e.get("eq_type") for e in (c.get("equipment") or [])}
        for name, wanted in EQUIPMENT_FILTERS:
            if types & set(wanted):
                out[name] += 1
    return out


CLIENT_ALL = ""            # the dropdown's first entry: no client filter
CLIENT_NONE = "__no_client__"    # cards whose title names nobody


def client_key(name, roster) -> str:
    """The one label a client's many spellings should be counted under.
    """
    name = (name or "").strip()
    if not name:
        return CLIENT_NONE
    q = client_squash(name)
    for c in roster or []:
        if q == client_squash(c.get("short_name")):
            return c.get("short_name") or name
        if any(q == client_squash(a) for a in (c.get("aliases") or [])):
            return c.get("short_name") or name
    return name


def client_counts(cards, roster):
    """Every client on the board and how many open tickets they have.
    """
    counts = {}
    for c in cards or []:
        if c.get("completed_at"):
            continue
        k = client_key(c.get("client_override") or c.get("client_raw"), roster)
        counts[k] = counts.get(k, 0) + 1
    named = sorted(((k, n) for k, n in counts.items() if k != CLIENT_NONE),
                   key=lambda kv: kv[0].lower())
    # Nobody's client last: it is a real answer and somebody may want to see
    # exactly those, but it is not a customer and should not sort among them.
    if CLIENT_NONE in counts:
        named.append((CLIENT_NONE, counts[CLIENT_NONE]))
    return named


def client_typos(cards, roster):
    """The spellings on the board that are provably wrong, and how many.

    Provably wrong, not merely unfamiliar: a spelling counts only if the
    roster knows who it means and it is not a deliberate shortening, so
    `Bravo` never goes red and `bravon` does. A name Jira has never heard of
    is left alone -- a customer can exist before Jira hears about them.
    """
    out = {}
    for c in cards or []:
        if c.get("completed_at"):
            continue
        raw = (c.get("client_override") or c.get("client_raw") or "").strip()
        if not raw:
            continue
        key = client_key(raw, roster)
        # Unknown to the roster: its own entry already, and not an error.
        if key == raw:
            continue
        if client_stands_for(raw, key):
            continue                       # a shortening, typed on purpose
        out[raw] = out.get(raw, 0) + 1
    return sorted(out.items(), key=lambda kv: kv[0].lower())


def client_filter_label(key, count) -> str:
    """`Thrasher (8)`, the way the queue boxes and the chips already read."""
    if key == CLIENT_NONE:
        return f"No client ({count})"
    return f"{key} ({count})"


def queue_counts(cards):
    """How many open tickets wear each tag.

    Over every card the board holds, **not** the filtered view: a number that
    moved with the search would be answering "how many did you find", which
    the board is already showing, and unchecking PROD would change the figure
    beside OPS, which is nonsense. This says what there is.

    A card with no tag, or one carrying a retired queue, is counted under no
    heading -- `T.QUEUE` holds exactly the offered ones and only those have a
    box to put a number on.
    """
    out = {q: 0 for q in T.QUEUE}
    for c in cards:
        q = c.get("queue") or ""
        if q in out:
            out[q] += 1
    return out


def queue_label(name, count):
    """`OPS` until the board has loaded, `OPS (5)` after.

    Used by both filter rows, so the two cannot drift into writing the same
    thing two ways. The brackets are load-bearing: `Bot 3` reads as the name
    of a third bot, which is exactly what the equipment row said before
    somebody read it back.
    """
    return name if count is None else f"{name} ({count})"


class EquipChip(QPushButton):
    """One equipment filter, as a pill that fills when it is on.

    **Deliberately not a checkbox**, though the queue filters beside it are.
    Those default to all-checked and you *uncheck* to narrow; these default
    to none-on, and turning one on narrows to it -- opposite conventions, and
    two rows of identical-looking controls behaving oppositely is the kind of
    thing nobody works out from looking. A pill is visibly a different
    control, so the different rule reads as intended rather than as a bug.

    None on means the board is unnarrowed, which is what makes "click ODE to
    see the ODEs" work in one click rather than three unchecks.
    """

    def __init__(self, name):
        super().__init__(name)
        # Kept, rather than read back off the button's own text: the text
        # grows a count and picking the name out of it again is a parser for
        # a string this file wrote a line earlier.
        self.name = name
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.count = None
        f = QFont()
        f.setPointSize(9)
        f.setWeight(QFont.DemiBold)
        self.setFont(f)
        self._paint()

    def _paint(self):
        # On, it is the accent; off, it is a raised control on chrome. The
        # filter row is `well`, not a card, so this takes `beside` -- see
        # `field`, where the same distinction is written down.
        self.setStyleSheet(
            f"QPushButton {{ color:{T.MUTED}; background:{T.BESIDE};"
            f" border:1px solid {T.LINE}; border-radius:10px;"
            f" padding:3px 10px; }}"
            f"QPushButton:hover {{ border-color:{T.ACCENT}; }}"
            f"QPushButton:checked {{ color:{T.ON_ACCENT};"
            f" background:{T.ACCENT}; border-color:{T.ACCENT}; }}")

    def set_count(self, n):
        """How many open tickets carry this kind. Guarded like QueueBox's.

        `render()` runs on every poll and every drag, and `updateGeometry` on
        five controls relays the row each time. The number moves when a
        ticket is made, closed or retitled; nothing else needs the layout
        touched.
        """
        if n == self.count:
            return
        self.count = n
        self.setText(queue_label(self.name, n))
        self.setToolTip(
            f"{n} open ticket{'' if n == 1 else 's'} carrying "
            f"{self.name}. Counted across the whole board, so "
            f"it does not move with the search."
            "\n\n"
            f"A ticket with several pieces is counted under each, and one "
            f"with none is under no chip at all -- so these do not add up to "
            f"the board.")
        self.updateGeometry()


class QueueBox(QCheckBox):
    """A queue filter that wears its queue's own colours.
    """

    SIDE = 15                 # the box; the hit area is the whole widget

    def __init__(self, queue):
        super().__init__(queue)
        self.stripe, self.tint, self.ink = T.QUEUE.get(queue, T.NEUTRAL)
        self.count = None
        self.setCursor(Qt.PointingHandCursor)
        f = QFont()
        f.setPointSize(9)
        f.setWeight(QFont.DemiBold)
        self.setFont(f)

    def label(self):
        return queue_label(self.text(), self.count)

    def set_count(self, n):
        """How many open tickets wear this tag.

        Guarded, because `render()` runs on every poll and every drag, and
        `updateGeometry` on four boxes relays the toolbar each time. The
        number changes when a ticket is made, closed or retagged; nothing
        else needs the layout touched.
        """
        if n == self.count:
            return
        self.count = n
        self.setToolTip(f"{n} open ticket{'' if n == 1 else 's'} tagged "
                        f"{self.text()}. Counted across the whole board, so "
                        f"it does not move with the search.")
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        return QSize(self.SIDE + 7 + fm.horizontalAdvance(self.label()) + 10,
                     max(self.SIDE, fm.height()) + 10)

    def hitButton(self, pos):
        # The whole control is the target.
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()

        box = QRect(2, (self.height() - self.SIDE) // 2, self.SIDE, self.SIDE)
        p.setPen(QPen(QColor(self.ink if on else T.LINE), 1))
        p.setBrush(QColor(self.tint) if on else QColor(T.SURFACE))
        p.drawRoundedRect(box, 3, 3)

        if on:
            # Two strokes rather than a tick character: a glyph would come from
            # whatever font had one, at whatever size it felt like.
            pen = QPen(QColor(self.ink), 1.8)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            x, y, w, h = box.x(), box.y(), box.width(), box.height()
            p.drawPolyline([QPoint(x + int(w * 0.24), y + int(h * 0.52)),
                            QPoint(x + int(w * 0.43), y + int(h * 0.71)),
                            QPoint(x + int(w * 0.77), y + int(h * 0.29))])

        p.setPen(QColor(self.ink if on else T.MUTED))
        p.setFont(self.font())
        p.drawText(QRect(box.right() + 7, 0,
                         self.width() - box.right() - 7, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self.label())


class Card(QFrame):
    def __init__(self, data, board, room=0):
        super().__init__()
        self.data = data
        self.board = board
        # The width this card will be given, so the client name can be cut to
        # it -- told its room the way a rail row is, rather than guessing.
        # 0 means ask the widget, which is right once it has been laid out.
        self.room = room
        self.thread_id = data["thread_id"]
        # A ticket being started has no thread yet, so it has no card either:
        # it is this widget and a row the API is about to be told to make.
        self.is_new = bool(data.get("is_new"))
        self.editing = False
        self._press = None
        self.problem = needs_triage(data)
        self.edit_btn = self.done_btn = self.work = None

        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(12, 10, 12, 10)
        self.body.setSpacing(7)
        self._paint()
        self._build_view()

    def _paint(self):
        """Colour the card by how urgent it is, and stripe it by whose it is.
        """
        stripe = T.QUEUE.get(self.data.get("queue") or "", T.NEUTRAL)[0]
        fill, edge, px = card_skin(self.data, self.editing)
        # The left edge carries the queue, unless something louder has taken
        # the card over: triage first, then an open editor.
        left = T.RED_EDGE if self.problem else T.ACCENT if self.editing else stripe
        self.setStyleSheet(
            f"Card {{ background:{fill}; border:{px}px solid {edge};"
            f" border-left:{4 if self.problem else 3}px solid {left};"
            f" border-radius:{CARD_RADIUS}px; }}")

    def flash(self):
        """Say which card the jump landed on, briefly.

        Three unreadable cards look alike, so arriving without being told
        which one is disorienting. Deliberately not a selection state: that
        would be a second kind of highlight for the board to explain, and
        this has nothing to say once it has been seen.

        `self` as the timer's context, so a card rebuilt by a poll inside the
        window takes its pending repaint with it rather than reaching a
        deleted widget.
        """
        fill, _, _ = card_skin(self.data, self.editing)
        self.setStyleSheet(
            f"Card {{ background:{fill}; border:2px solid {T.RED_FG};"
            f" border-left:4px solid {T.RED_FG};"
            f" border-radius:{CARD_RADIUS}px; }}")
        QTimer.singleShot(FLASH_MS, self, self._paint)

    def _clear(self):
        # These belong to the view and are about to be deleted. Dropping the
        # references keeps set_writable from reaching a deleted C++ object.
        self.edit_btn = self.done_btn = self.work = None

        def drop(w):
            # Unparent before deleteLater.
            w.hide()
            w.setParent(None)
            w.deleteLater()

        while self.body.count():
            it = self.body.takeAt(0)
            if it.widget():
                drop(it.widget())
            elif it.layout():
                while it.layout().count():
                    sub = it.layout().takeAt(0)
                    if sub.widget():
                        drop(sub.widget())

    # -- read mode ---------------------------------------------------------

    def _build_view(self):
        d = self.data
        cbg, cfg = T.QUEUE.get(d.get("queue") or "", T.NEUTRAL)[1:]

        head = QHBoxLayout()
        head.setSpacing(CARD_HEAD_SPACING)
        tag = chip(d.get("queue") or "\u2014", cbg, cfg)
        head.addWidget(tag)

        who = (d.get("client_override") or d.get("client_raw")
               or "Unknown client")
        client = ClickableLabel(who)
        f = QFont()
        f.setPointSize(11)
        f.setWeight(QFont.DemiBold)
        client.setFont(f)
        client.setStyleSheet(f"color:{T.RED_FG if self.problem else T.INK};"
                             f" background:transparent;")
        client.doubleClicked.connect(self.enter_edit)

        # PIP tickets raised in the thread -- the build and return requests the
        # interface bot posts -- not Bert's own tickets, which is what a card
        # already is. Shown only past one: production runs 83 threads on one
        # PIP and 111 on two, so the distinction is real there.
        pips = d.get("ticket_count") or 0

        # Built before they are placed, so the client can be told what is
        # actually left instead of claiming the row and shoving them off it.
        edited = chip("edited", T.CHIP_BG, T.MUTED) if d.get("client_override") else None
        after = []
        # The build ticket, as a link to it. Only when there is one *and*
        # somewhere for it to go.
        key = d.get("build_ticket")
        url = ticket_url((self.board.health or {}).get("jira_url"), key)
        if url:
            after.append(LinkChip(
                f"Build {key}", url,
                tip=f"Open {key} in Jira." + "\n\n"
                    "Uses your own Jira sign-in; Ernie never reads it."))

        if pips > 1:
            after.append(chip(f"{pips} PIPs", T.CHIP_BG, T.MUTED))

        # Last in the row, so it sits in the card's top corner: this is the
        # one thing on the card about the change rather than about the ticket.
        mark = unsent_mark(d)
        if mark:
            text, colour, why = mark
            # A chip, like the ones beside it.
            said = chip(text, T.CHIP_BG, colour)
            said.setToolTip(why)
            after.append(said)

        # Cut to the room it actually has, measured against the font it draws
        # in, the way a rail row cuts both of its lines.
        room = self._client_room([tag] + ([edited] if edited else []) + after)
        cut = QFontMetrics(f).elidedText(who, Qt.ElideRight, room)
        client.setText(cut)
        # The whole name on hover, but only when it was actually cut --
        # a tooltip repeating the line it sits on is noise on every card.
        tip = [who, "Double-click to edit"] if cut != who else ["Double-click to edit"]
        client.setToolTip("\n".join(tip))

        head.addWidget(client)
        if edited:
            head.addWidget(edited)
        head.addStretch()
        for w in after:
            head.addWidget(w)
        self.body.addLayout(head)

        if self.problem:
            # What is wrong, not that something is. Joined rather than one
            # row each: a card is a summary, and three rows of red would out-
            # shout the ticket they are about.
            self.body.addWidget(warning_row(
                " \u00b7 ".join(title_problems(d)), T.RED_FG))

        if d.get("equipment"):
            row = QHBoxLayout()
            row.setSpacing(5)
            for e in d["equipment"][:6]:
                if e["state"] == "resolved":
                    row.addWidget(chip(e["raw"], T.CHIP_BG, T.MUTED))
                elif e["state"] == "pending":
                    row.addWidget(chip(e["raw"], T.AMBER_BG, T.AMBER_FG, dashed=True))
                else:
                    row.addWidget(chip(e["raw"], T.RED_BG, T.RED_FG))
            row.addStretch()
            self.body.addLayout(row)

        # Only what still needs doing. A finished bubble is history, and the
        # card is a list of what is left -- the editor is where the history
        # is, and where it can be put back.
        items = [i for i in (d.get("work_items") or []) if not i.get("done")]
        if items:
            self.work = WorkBar(items, editing=False)
            self.work.ticked.connect(self._tick_off)
            self.body.addWidget(self.work)
        else:
            # Nothing typed yet, so fall back to what the thread title says
            # this is about rather than leaving a hole in the card.
            work = ClickableLabel(d.get("summary") or d.get("name") or "")
            work.setWordWrap(True)
            work.setStyleSheet(f"color:{T.MUTED}; font-size:12px;"
                               f" background:transparent;")
            work.setToolTip("Double-click to edit")
            work.doubleClicked.connect(self.enter_edit)
            self.body.addWidget(work)

        foot = QHBoxLayout()
        foot.setSpacing(CARD_FOOT_SPACING)

        # The buttons are built before they are placed, so the chips can be
        # told what is actually left instead of claiming the row and pushing
        # them off it.
        self.edit_btn = QPushButton("Edit")
        self.edit_btn.setStyleSheet(btn_css())
        self.edit_btn.clicked.connect(self.enter_edit)

        # Close thread, not Complete.
        #
        # Qt puts a button's icon on the left, always.
        self.done_btn = QPushButton("Close thread ")
        self.done_btn.setLayoutDirection(Qt.RightToLeft)
        self.done_btn.setStyleSheet(btn_css())
        self.done_btn.setIcon(tick_icon(T.OK_FG))
        self.done_btn.setIconSize(QSize(12, 12))
        self.done_btn.clicked.connect(lambda: self.board.complete(self.thread_id))

        # Not while there is work left on it: a card is a list of what is
        # still to do.
        #
        # **Only the count is set here; `set_writable` disables the button.**
        # setEnabled(False) at this point does nothing: `set_writable` runs at
        # the end of this function and hands it straight back.
        self._work_left = len(items)
        if items:
            n = self._work_left
            self.done_btn.setToolTip(
                f"{n} thing{'' if n == 1 else 's'} still to do on this "
                f"ticket. Tick them off, or remove them in the editor, "
                f"then close the thread.")

        age, chips = self._fit_foot(d)
        if age:
            foot.addWidget(age)
        foot.addStretch()
        for w in chips:
            foot.addWidget(w)
        foot.addWidget(self.edit_btn)
        foot.addWidget(self.done_btn)
        self.body.addLayout(foot)
        self.set_writable(self.board.writable())
        plain_cursors(self)

    # -- edit mode ---------------------------------------------------------

    def _fit_foot(self, d):
        """The footer's columns, fitted to the card before any of them is placed.

        **The controls never give way.** What does, in order:

        1. The age gives up its words -- `Last reply 3d` becomes `3d`. A word
           dropped whole still reads; half a word does not.
        2. A second chip is dropped, its text moving into the one that stays.
        3. The age goes altogether: it is context, and an amber chip is the
           card asking for somebody.
        4. Only then is the survivor cut, to `CARD_ISSUE_MIN_W` at worst.

        Nothing is dropped in silence -- whatever is not on the row is in the
        tooltip of what is.
        """
        texts = [i.replace("_", " ") for i in (d.get("issues") or [])[:2]
                 if i not in BLOCKING]

        room0 = self.room or self.width()
        m = self.body.contentsMargins()
        room0 -= m.left() + m.right()
        for b in (self.edit_btn, self.done_btn):
            b.ensurePolished()
            room0 -= b.sizeHint().width()

        def room_for(age_w, n):
            # Every gap in the row, the stretch's own included.
            fixed = 2 + (1 if age_w else 0)
            return room0 - age_w - CARD_FOOT_SPACING * (fixed + n)

        def widths(ts):
            out = []
            for t in ts:
                c = chip(t, T.AMBER_BG, T.AMBER_FG)
                c.ensurePolished()
                out.append(c.sizeHint().width())
            return out

        # Longest first, so the first that fits is the most it can say.
        forms = self._age_forms(d.get("last_human_at"))
        want = widths(texts)

        chosen = forms[-1] if forms else None
        keep = texts
        for form in forms or [None]:
            if sum(want) <= room_for(self._age_width(form), len(texts)):
                chosen = form
                break
        else:
            # Nothing fits whole, so the age is already down to its number.
            if (len(texts) == 2
                    and room_for(self._age_width(chosen), 2) < CARD_ISSUE_MIN_W * 2):
                keep = texts[:1]
            if keep and room_for(self._age_width(chosen),
                                 len(keep)) < CARD_ISSUE_MIN_W:
                # The age goes last but it goes before the chip does. It is
                # context -- how long this has been quiet -- and an amber chip
                # is the card asking for somebody.
                chosen = None
                if room_for(0, len(keep)) < CARD_ISSUE_MIN_W:
                    keep = []

        age = self._age_label(chosen, d.get("last_human_at"))
        made = []
        for i, t in enumerate(keep):
            c = elided_chip(t, T.AMBER_BG, T.AMBER_FG,
                            self._shares(keep, room_for(
                                self._age_width(chosen), len(keep)))[i])
            # Whatever is not on the row is still readable on what is.
            if i == 0 and len(keep) < len(texts):
                c.setToolTip("\n".join(texts))
            made.append(c)
        return age, made

    @staticmethod
    def _shares(texts, room):
        """How much of `room` each chip gets.
        """
        made = [chip(t, T.AMBER_BG, T.AMBER_FG) for t in texts]
        for w in made:
            w.ensurePolished()
        want = [w.sizeHint().width() for w in made]
        out = [None] * len(want)
        while True:
            open_ = [i for i, v in enumerate(out) if v is None]
            if not open_:
                return out
            share = max(room // len(open_), 0)
            fits = [i for i in open_ if want[i] <= share]
            if not fits:
                for i in open_:
                    out[i] = share
                return out
            for i in fits:
                out[i] = want[i]
                room -= want[i]

    def _client_room(self, fixed):
        """What the card's top row has left for the client name.

        Measured rather than assumed. A rail row can take a constant off its
        width because every row is the same shape; a card's head is not -- the
        queue tag, an "edited" chip, a PIP count, the build-ticket link and
        the unsent mark are each there or not, and each as wide as its own
        text. So the chrome
        is asked how big it is.

        sizeHint() after ensurePolished(), because the padding these carry
        comes from a stylesheet and is not in the hint until the style has
        been applied to them.
        """
        room = self.room or self.width()
        m = self.body.contentsMargins()
        for w in fixed:
            w.ensurePolished()
            room -= w.sizeHint().width()
        room -= m.left() + m.right()
        room -= CARD_HEAD_SPACING * (len(fixed) + 1)
        return max(room, CARD_CLIENT_MIN_W)

    def enter_edit(self):
        if not self.board.writable() or self.editing:
            return
        if self.board.editor_is_busy(self.thread_id):
            return
        self.editing = True
        self.board.editing_card = self.thread_id
        self._clear()
        self._warn_label = None          # _clear() just deleted any previous one
        self._paint()

        d = self.data
        # What the server held when this editor opened.
        self._edit_base = {f: (d.get(f) or "") for f in
                           ("client_override",)}
        # The thread title lives on Discord, not on the card, so it travels
        # under its own key and is compared against the card's "name".
        self._edit_base["title"] = d.get("name") or ""
        self._title_touched = False
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)

        # Picked from Jira's customer list rather than typed. The board runs
        # 120 spellings of 43 customers -- five ways of writing Inspect.AI --
        # because every one of them was typed into a title by hand.
        self.f_client = ClientCombo(
            self.board.roster,
            d.get("client_override") or d.get("client_raw") or "")
        # What the card already carried, so the caution below can tell a name
        # somebody has just typed from one the card arrived with.
        self._client_opened_with = self.f_client.text()
        self.client_state = QLabel()
        self.client_state.setWordWrap(True)
        self.client_state.setStyleSheet(
            f"color:{T.AMBER_FG}; font-size:11px; padding:1px 0 3px 0;"
            f" background:transparent;")
        self.client_state.hide()
        client_box = QVBoxLayout()
        client_box.setContentsMargins(0, 0, 0, 0)
        client_box.setSpacing(2)
        client_box.addWidget(self.f_client)
        client_box.addWidget(self.client_state)
        client_holder = QWidget()
        # No stylesheet. An unscoped `background:transparent` applies to the
        # widget *and everything under it* -- and `Combo` carries no sheet of
        # its own, so the rule reached the box and took its fill away. The
        # field beside it survives the same line only because a QLineEdit is
        # given field() directly and its own rule wins. A plain QWidget paints
        # nothing without WA_StyledBackground anyway, so the rule was doing
        # no work and one piece of damage.
        client_holder.setLayout(client_box)
        self.f_client.currentTextChanged.connect(self._say_client)

        self.f_work = WorkBar(d.get("work_items") or [], editing=True)

        self.f_title = QLineEdit(d.get("name") or "")
        self.f_title.setStyleSheet(field())
        self.title_state = QLabel()
        self.title_state.setWordWrap(True)
        # Room under the caution sign the two unparseable branches put in
        # here, for the same reason warning_row() exists.
        self.title_state.setStyleSheet("font-size:11px; padding:1px 0 3px 0;"
                                       " background:transparent;")
        # What the thread is called now, shown only when the fields cannot
        # express it and guided is therefore building a replacement. Two
        # lines then: what it is, and what it will be. Read-only, because the
        # old one is a fact rather than a field.
        self.title_was = QLabel()
        self.title_was.setWordWrap(True)
        self.title_was.setStyleSheet("background:transparent; font-size:12px;")
        self.title_was.hide()

        # The mark goes beside the title rather than into it: a tick inside
        # the box would be part of the string, and the string is what gets
        # saved as the thread's name.
        self.title_mark = QLabel()
        self.title_mark.setStyleSheet("background:transparent;")
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title_row.addWidget(self.title_mark)
        title_row.addWidget(self.f_title, 1)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        title_box.addWidget(self.title_was)
        title_box.addLayout(title_row)
        title_box.addWidget(self.title_state)
        title_holder = QWidget()
        # Nor here, for the same reason -- it was masked only by the line
        # edit's own sheet, which is a latent version of the bug above.
        title_holder.setLayout(title_box)

        self.f_queue = Combo()
        self.f_queue.addItem("—", "")
        for q in ex.QUEUES_OFFERED:
            self.f_queue.addItem(q, q)
        # A card already carrying a retired queue keeps it. Without this the
        # findData below misses, falls back to index 0, and saving the card
        # for any other reason quietly clears the tag -- the retired queue is
        # not offered to anybody, but it is not taken off the one card that
        # has it either.
        here = d.get("queue") or ""
        if here and self.f_queue.findData(here) < 0:
            self.f_queue.addItem(here, here)
        self.f_queue.setCurrentIndex(max(self.f_queue.findData(here), 0))

        # A real date control, not another text box. The date is what places
        # the client -- with none, the parser cannot tell a client from a
        # summary -- so typing it freely would move the parsing problem here
        # rather than remove it.
        self.f_date = DateBox()
        self.f_date.setCalendarPopup(True)
        self.f_date.setDisplayFormat("d MMM yyyy")
        # The floor stands for "no date" -- see DateBox, which is what keeps
        # that from meaning the calendar opens in 1900.
        self.f_date.setMinimumDate(NO_DATE)
        self.f_date.setSpecialValueText("\u2014 none \u2014")
        self.f_date.setStyleSheet(
            field()
            + f"QDateEdit::drop-down {{ border:none; background:transparent;"
              f" width:{DateBox.ARROW_W}px; }}"
              f"QDateEdit::down-arrow {{ image:none; width:0; height:0; }}")
        d_date = ex.parse_title(d.get("name") or "").date
        self.f_date.setDate(QDate(d_date.year, d_date.month, d_date.day)
                            if d_date else NO_DATE)

        # Seeded from whatever the title already says. For an unreadable one
        # that is the whole remainder -- `ENG: Retired bots` puts "Retired
        # bots" here -- so building a replacement starts from what somebody
        # wrote rather than from nothing. Seeding a field does not compose,
        # so opening a card still cannot make it dirty.
        _t0 = ex.parse_title(d.get("name") or "")
        self.f_desc = QLineEdit(
            _t0.summary or ("" if _t0.confidence in ("strict", "loose")
                            else (d.get("name") or "").strip()))
        self.f_desc.setStyleSheet(field())

        # textEdited fires only for typing, so rebuilding the suggestion below
        # doesn't count as the person taking the title over.
        # Which of the two this person asked for. Both modes hide controls
        # rather than changing what saving does, which keeps them one editor
        # with a preference on top rather than two to keep in step.
        #
        # Read before anything asks: `_check_title()` runs during the wiring
        # below and needs to know which mode it is in.
        self._entry = (self.board.settings.get("entry") or ENTRY_DEFAULT)

        self.f_title.textEdited.connect(self._title_edited)
        self.f_title.textChanged.connect(self._check_title)
        self.f_queue.currentIndexChanged.connect(self._queue_picked)
        self.f_client.currentTextChanged.connect(self._suggest_title)
        self.f_date.dateChanged.connect(self._date_picked)
        self.f_desc.textEdited.connect(self._desc_typed)
        self._check_title()

        form.addRow("Thread title", title_holder)
        form.addRow("Tag", self.f_queue)
        form.addRow("Client", client_holder)
        form.addRow("Date", self.f_date)
        form.addRow("Description", self.f_desc)
        form.addRow("Work items", self.f_work)

        # Kept, because hiding a row is easy and putting it back needs to know
        # what was hidden. The mode can change under an open editor.
        self._guided_rows = [(form.labelForField(w), w) for w in
                             (self.f_queue, client_holder, self.f_date,
                              self.f_desc)]
        self._show_guided_rows(self._entry == "guided")
        # Work items stay in both: they are not part of the title, and the
        # people who type titles still tick bubbles.

        # Only when starting one. Ernie opens the thread, so it posts a line
        # saying who it was for -- this is the message they would have typed
        # into it themselves, and is optional because plenty of tickets are
        # their title and nothing more.
        self.f_first = None
        if self.is_new:
            self.f_first = QLineEdit()
            self.f_first.setStyleSheet(field())
            self.f_first.setPlaceholderText(
                "Optional -- posted into the thread after Ernie says who "
                "started it")
            form.addRow("First message", self.f_first)
        # The labels QFormLayout makes for itself paint their palette
        # background.
        for i in range(form.rowCount()):
            lab = form.itemAt(i, QFormLayout.LabelRole)
            if lab is not None and lab.widget() is not None:
                lab.widget().setStyleSheet("background:transparent;")
        self.body.addLayout(form)

        note = QLabel(
            "Saving opens a thread in Discord with this title. Ernie posts a "
            "line saying you started it, then your first message if you "
            "wrote one."
            if self.is_new else
            "Saving posts one update to the thread, however many fields you "
            "change. Picking a client rewrites the title, and changing the "
            "title renames the Discord thread.")
        note.setStyleSheet(f"color:{T.MUTED}; font-size:11px;"
                           f" background:transparent;")
        self.body.addWidget(note)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(btn_css())
        cancel.clicked.connect(self.exit_edit)
        row.addWidget(cancel)
        save = QPushButton("Create ticket" if self.is_new else "Save")
        save.setStyleSheet(BTN_HIT + f"background:{T.ACCENT}; color:{T.ON_ACCENT};"
                                     f" border:none;")
        save.clicked.connect(self.save)
        row.addWidget(save)
        self.body.addLayout(row)
        plain_cursors(self)

    def _title_edited(self, _text):
        self._title_touched = True

    def _say_client(self, _text=None):
        """Show or hide the caution under the Client box."""
        lab = getattr(self, "client_state", None)
        if lab is None:
            return
        lines = [client_note(self.f_client.text(), self.board.roster,
                             getattr(self, "_client_opened_with", ""))]
        # A pick that cannot reach the title has to say so. Silence reads as
        # the dropdown being broken -- the card updates, because the name is
        # saved as an override, and the thread title sits there unchanged.
        # Only once there is something to place, and only while the title is
        # still the editor's to rebuild.
        if (self.f_client.text().strip()
                and not getattr(self, "_title_touched", True)
                and not title_takes_client(self.f_title.text())):
            lines.append("The title has nowhere to put a client yet \u2014 give it "
                         "a tag and a date and it will follow.")
        note = "\n".join(x for x in lines if x)
        lab.setText(note)
        lab.setVisible(bool(note))

    def _show_guided_rows(self, on):
        """Show or hide the four fields the guided mode adds."""
        for lab, w in getattr(self, "_guided_rows", []):
            if lab is not None:
                lab.setVisible(on)
            w.setVisible(on)

    def set_entry_mode(self, mode):
        """Switch an already-open editor between typing and guided.

        Settings can be opened mid-edit, and `render()` spares the bands an
        editor lives in, so the change has to reach it directly. Cheap
        because both modes are one editor: nothing about saving depends on
        which is showing.
        """
        if mode == getattr(self, "_entry", None) or not self.editing:
            return
        self._entry = mode
        self._show_guided_rows(mode == "guided")
        self._check_title()

    def _set_title(self, text):
        """Write the title without the write coming back as an edit."""
        if text == self.f_title.text():
            return
        self._syncing = True
        try:
            self.f_title.setText(text)
        finally:
            self._syncing = False

    def _put(self, field, value):
        """Put one field into the title, touching nothing else."""
        if getattr(self, "_syncing", False):
            return
        cur = self.f_title.text()
        if ex.parse_title(cur).confidence in ("strict", "loose"):
            self._set_title(ex.replace_field(cur, field, value))
            return
        # Nothing to splice into, and nothing worth preserving either, so
        # the fields build a replacement. Safe only here: composing a title
        # that *does* parse renormalises it, which is a rename nobody asked
        # for.
        self._set_title(self._composed())

    def _composed(self) -> str:
        """The four fields as the title they describe."""
        qd = self.f_date.date() if hasattr(self, "f_date") else None
        return compose_title(
            self.f_queue.currentData() if hasattr(self, "f_queue") else "",
            self.f_client.text().strip() if hasattr(self, "f_client") else "",
            None if qd is None or qd == NO_DATE
            else date(qd.year(), qd.month(), qd.day()),
            self.f_desc.text().strip() if hasattr(self, "f_desc") else "")

    def _fields_from_title(self, t):
        """The other direction: what the title says, in the fields.

        A field with focus is skipped -- pushing a half-parsed answer back at
        somebody mid-type fights them.
        """
        self._syncing = True
        try:
            if hasattr(self, "f_queue") and not self.f_queue.hasFocus():
                want = t.queue or ""
                if self.f_queue.currentData() != want:
                    self.f_queue.blockSignals(True)
                    self.f_queue.setCurrentIndex(
                        max(self.f_queue.findData(want), 0))
                    self.f_queue.blockSignals(False)
            if hasattr(self, "f_client") and not self.f_client.hasFocus():
                want = t.client_raw or ""
                if self.f_client.text() != want:
                    self.f_client.blockSignals(True)
                    self.f_client.setText(want)
                    self.f_client.blockSignals(False)
            if hasattr(self, "f_date") and not self.f_date.hasFocus():
                want = (QDate(t.date.year, t.date.month, t.date.day)
                        if t.date else NO_DATE)
                if self.f_date.date() != want:
                    self.f_date.blockSignals(True)
                    self.f_date.setDate(want)
                    self.f_date.blockSignals(False)
            if hasattr(self, "f_desc") and not self.f_desc.hasFocus():
                want = t.summary or ""
                if self.f_desc.text() != want:
                    self.f_desc.blockSignals(True)
                    self.f_desc.setText(want)
                    self.f_desc.blockSignals(False)
        finally:
            self._syncing = False

    def _date_picked(self, qd):
        if qd == NO_DATE:
            return          # a title has no way to say "no date" but blank
        self._put("date", title_stamp(date(qd.year(), qd.month(), qd.day())))

    def _desc_typed(self, text):
        self._put("summary", text.strip())

    def _suggest_title(self, _text=None):
        """Keep the title in step with the client."""
        self._put("client", self.f_client.text().strip())

    def _queue_picked(self, _index):
        """Put the chosen queue into the title, keeping whatever else is there."""
        q = self.f_queue.currentData()
        cur = self.f_title.text().strip()
        t = ex.parse_title(cur)
        if not q:
            # The dash is an entry, and an entry is something you can pick,
            # so picking it takes the tag off rather than returning here.
            #
            # That leaves a title nothing can parse, which is allowed: the
            # card says "No tag" and the fields are still there to put one
            # back. Refusing the visible thing is what is not allowed.
            if t.confidence in ("strict", "loose"):
                self._put("queue", "")
            else:
                m = ex.PREFIX_ONLY.match(cur)
                self._set_title(m.group("rest").strip() if m else cur)
            return
        if t.confidence in ("strict", "loose"):
            self._put("queue", q)
            return
        # No date, so there are no segments to splice -- but there is still
        # text somebody typed, and the tag goes on the front of it. This used
        # to lay out `TAG: Client - today - what it's about` over the top,
        # which threw away whatever was there: picking a tag on
        # `Thrasher - Trade show TOF` lost the lot. Losing what somebody typed
        # is the worse failure, and the card already says what is still
        # missing.
        m = ex.PREFIX_ONLY.match(cur)
        rest = m.group("rest") if m else cur
        self._set_title(f"{q}: {rest}".rstrip())

    def _check_title(self, _text=None):
        """Title -> fields, and the state of the title box itself.

        Runs on every keystroke in the title box.
        """
        # The Client note depends on the title as well as the box.
        self._say_client()
        t = ex.parse_title(self.f_title.text().strip())
        # The fields follow the title, including a prefix typed by hand.
        if not getattr(self, "_syncing", False):
            self._fields_from_title(t)
        # Guided never hands the box back, even for a title the fields cannot
        # express -- those are the ones somebody most needs help rebuilding.
        # It shows both instead: what the thread is called now, and what it
        # will be called. No frame either, since a frame invites typing the
        # control would refuse.
        if getattr(self, "_entry", ENTRY_DEFAULT) == "guided":
            if not self.f_title.isReadOnly():
                self.f_title.setReadOnly(True)
                self.f_title.setFrame(False)
                self.f_title.setStyleSheet(
                    f"background:transparent; border:none; padding:0;"
                    f" color:{T.INK}; font-size:13px;")
                self.f_title.setToolTip(
                    "Built from the fields below. Change one and this "
                    "follows.")
            was = ((getattr(self, "_edit_base", None) or {}).get("title") or "")
            broken = bool(was) and ex.parse_title(was).confidence not in (
                "strict", "loose")
            if broken:
                shown = (was.replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;"))
                self.title_was.setText(
                    f"<span style='color:{T.RED_FG}'>\u2717</span> "
                    f"<span style='color:{T.MUTED}'>Now: {shown}</span>")
            self.title_was.setVisible(broken)
        else:
            # Stated rather than left as whatever guided did, which is one
            # line away from inheriting it.
            self.title_was.hide()
            if self.f_title.isReadOnly():
                self.f_title.setReadOnly(False)
                self.f_title.setFrame(True)
                self.f_title.setStyleSheet(field())
                self.f_title.setToolTip("")

        guided = getattr(self, "_entry", ENTRY_DEFAULT) == "guided"
        ok = t.confidence in ("strict", "loose")
        mark, colour = ("✓", T.OK_FG) if ok else (
            ("⚠", T.AMBER_FG) if t.confidence == "prefix_only"
            else ("⚠", T.RED_FG))
        if hasattr(self, "title_mark"):
            self.title_mark.setText(
                f"<span style='color:{colour}; font-size:14px'>{mark}</span>")

        # Guided already shows the parts as fields, so repeating them under
        # the title is the same sentence twice -- and the quieter copy is the
        # one the eye reads second and trusts less. It keeps only what the
        # fields cannot say, which is what is wrong and what the shape should
        # have been. Typing has no fields, so the breakdown there is the only
        # account of what Ernie made of the string.
        if ok:
            self.title_state.setText("" if guided else
                f"<span style='color:{T.MUTED}'>{t.queue} &middot; {t.client_raw} "
                f"&middot; {title_stamp(t.date)} &middot; {t.summary or ''}</span>")
        else:
            # The same words the card uses. There was a second vocabulary
            # here -- "no date Ernie can read", "doesn't match" -- saying the
            # same things differently, which is how two accounts of one
            # condition start disagreeing. And it named a half: the parser is
            # `ernie_extract.parse_title`, which Bert imports and Ernie runs,
            # so whose date-reading it is was never a question worth putting
            # to the reader.
            said = " &middot; ".join(title_problems({
                "name": self.f_title.text().strip(), "queue": t.queue,
                "client_raw": t.client_raw, "summary": t.summary,
                "thread_date": t.date.isoformat() if t.date else None,
                "confidence": t.confidence}))
            colour = T.AMBER_FG if t.confidence == "prefix_only" else T.RED_FG
            # The shape, but only where there is nothing else showing it. In
            # guided the four fields are the shape; in typing this line is
            # all there is.
            hint = ("" if guided else
                    f" <span style='color:{T.MUTED}'>&mdash; TAG: Client - "
                    f"25Aug26 - what it's about</span>")
            self.title_state.setText(
                f"<span style='color:{colour}'>{said}</span>{hint}")
        self.title_state.setVisible(bool(self.title_state.text()))

    def warn_changed(self, msg):
        """Live notice, while the editor is open, that the card moved."""
        if not self.editing:
            return
        if getattr(self, "_warn_label", None) is None:
            self._warn_label = QWidget()
            self._warn_label.setObjectName("editWarn")
            self._warn_label.setStyleSheet(
                f"#editWarn {{ background:{T.AMBER_BG}; padding:6px;"
                f" border:1px solid {T.AMBER_FG}44; }}")
            lay = QVBoxLayout(self._warn_label)
            lay.setContentsMargins(6, 5, 6, 5)
            self.body.insertWidget(0, self._warn_label)
        while self._warn_label.layout().count():
            w = self._warn_label.layout().takeAt(0).widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self._warn_label.layout().addWidget(warning_row(msg, T.AMBER_FG))
        self._warn_label.show()

    def exit_edit(self):
        self.editing = False
        self.board.editing_card = None
        if self.is_new:
            # There is nothing behind it to show. Dropping the widget also
            # releases the board, which has been holding every poll while
            # this editor was open.
            self.hide()
            self.setParent(None)
            self.deleteLater()
            self.board.apply_pending()
            return
        self._clear()
        self._paint()
        self._build_view()
        # Come back to this card. The rebuild above has already halved its
        # height, so whatever the view was looking at inside the editor is
        # gone; without this the scrollbar keeps its number and lands on
        # some other ticket entirely.
        self.board._focus_card = self.thread_id
        # Redrawing is safe again now the editor is gone.
        self.board.apply_pending()
        # apply_pending only renders when there is something to draw. With a
        # quiet board nothing calls put_back, so nothing would consume the
        # request.
        if self.board._focus_card:
            self.board._focus_card = None
            QTimer.singleShot(0, lambda: self.board.reveal(self.thread_id))

    def _override(self) -> str:
        """What the Client box means as a client_override.

        An override says "the parsed client is wrong, use this instead", and
        `needs_triage` reads one as somebody vouching for an unreadable card
        -- so writing one that nobody decided clears the red edge off a
        ticket that still needs it. Three answers:

          title already says it   nothing to override
          box untouched           whatever the card already had
          box changed or emptied  a decision, so write it

        Compared against the title in the box, not the card's client_raw,
        which is the old title's client until the next sync.
        """
        typed = self.f_client.text().strip()
        if not typed:
            return ""
        t = ex.parse_title(self.f_title.text().strip())
        if ex.normalise_client(typed) == ex.normalise_client(t.client_raw or ""):
            return ""
        # The box is seeded from the card, so a name still sitting in it is
        # not a decision made here -- and an override that writes itself
        # re-seeds the box it came from and can never clear. Same guard
        # `client_note` uses, on the half that writes.
        if client_squash(typed) == client_squash(
                getattr(self, "_client_opened_with", "") or ""):
            base = getattr(self, "_edit_base", None) or {}
            return base.get("client_override") or ""
        return typed

    def is_dirty(self):
        """Whether this editor is holding anything worth asking about.

        Exactly what save() would send, compared against what the card
        held when the editor opened. Somebody who clicked Edit on the
        wrong ticket and clicked away has nothing to decide, and should
        not be asked to decide it.
        """
        if self.is_new:
            # A blank template is not a draft. What makes it one is anything
            # typed into it -- and the title starts filled in, so it is only
            # a change from the template that counts.
            base = getattr(self, "_edit_base", None) or {}
            return bool(
                self.f_title.text().strip() != (base.get("title") or "")
                or self.f_work.added()
                or (self.f_first and self.f_first.text().strip()))
        base = getattr(self, "_edit_base", None) or {}
        # `_title_to_send`, not the box, because this has to be exactly what
        # save() would send -- a corrected client is part of that. It cannot
        # make an untouched card dirty: `client_resolve` returns nothing for
        # a name the editor opened with.
        if self._title_to_send() != (base.get("title") or ""):
            return True
        if self._override() != (base.get("client_override") or ""):
            return True
        return bool(self.f_work.added() or self.f_work.removed()
                    or self.f_work.undone())

    def _title_to_send(self) -> str:
        """The title as it will be saved, with a mistyped client corrected.

        The client box drives the title, so a name corrected here has to go
        into the **title** -- that is what `save` sends and what the thread is
        renamed to. Rewriting the box alone would change nothing, because
        `_suggest_title` stops rebuilding the moment somebody types in the
        title themselves.

        Only the client segment moves, and only on a title that parses: a
        title the fields cannot hold is the one thing the box is there to
        repair, and rebuilding it from parts would throw away whatever the
        person was in the middle of writing.
        """
        title = self.f_title.text().strip()
        fixed = client_resolve(self.f_client.text(), self.board.roster,
                               getattr(self, "_client_opened_with", ""))
        if not fixed:
            return title
        t = ex.parse_title(title)
        if t.confidence not in ("strict", "loose"):
            return title
        # Only if the title is still carrying the name that was corrected --
        # somebody who typed a different client straight into the title meant
        # that one, and the box is not what they were editing.
        if ex.normalise_client(t.client_raw or "") != ex.normalise_client(
                self.f_client.text().strip()):
            return title
        return f"{t.queue}: {fixed} - {title_stamp(t.date)} - {t.summary or ''}"

    def save(self) -> bool:
        """True if the write landed. Closing Bert waits on the answer."""
        # Refused here rather than discovered in the outbox. A thread has to
        # have a name -- Discord rejects an empty one -- so this would have
        # gone out, come back 4xx, burned its five attempts and settled on
        # the card as "Not sent", for something that was answerable the
        # moment it was typed.
        if not self._title_to_send().strip():
            QMessageBox.warning(
                self, "No title",
                "A thread needs a name, and Discord will not take an empty "
                "one.\n\nThe shape is: TAG: Client - 25Aug26 - what it's "
                "about.")
            self.f_title.setFocus()
            return False
        if self.is_new:
            return self.board.create_ticket(self, {
                "title": self._title_to_send(),
                "priority": self.data["priority"],
                "work_add": self.f_work.added(),
                "first_message": (self.f_first.text().strip()
                                  if self.f_first else ""),
            })
        fields = {
            "title": self._title_to_send(),
            "client_override": self._override(),
            # The bubbles travel as what changed, not as a list to diff: two
            # people adding different items then merge instead of colliding.
            "work_add": self.f_work.added(),
            "work_remove": self.f_work.removed(),
            "work_undone": self.f_work.undone(),
        }
        base = getattr(self, "_edit_base", None)
        # The write first, and the editor closes only if there is nothing left
        # to keep. It used to go back to view mode before the write, so a save
        # that failed redrew the card from server data and everything typed
        # was gone -- the error box explained the failure over the top of work
        # that had already been thrown away.
        ok = self.board.save_edits(self.thread_id, fields, base)
        if ok:
            self.exit_edit()
        return ok

    def _tick_off(self, item_id):
        self.board.finish_item(self.thread_id, item_id)

    def set_writable(self, ok):
        """The one place that decides whether a card's buttons work.

        Closing needs two things: a board that can write at all, and a ticket
        with nothing left on it. Both live here, because this runs after
        `_build_view` has placed the buttons and again whenever the
        connection changes -- so a rule applied anywhere else is undone the
        next time it runs.
        """
        if self.done_btn is not None:        # None while the editor is open
            self.done_btn.setEnabled(ok and not getattr(self, "_work_left", 0))
            self.edit_btn.setEnabled(ok)
        if self.work is not None:
            self.work.set_enabled(ok)
        self.setCursor(Qt.OpenHandCursor if ok else Qt.ArrowCursor)

    # -- drag --------------------------------------------------------------

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self.editing:
            self._press = e.position().toPoint()

    def mouseMoveEvent(self, e):
        if self._press is None or self.editing or not self.board.writable():
            return
        if (e.position().toPoint() - self._press).manhattanLength() < DRAG_THRESHOLD:
            return
        mime = QMimeData()
        mime.setData(MIME, self.thread_id.encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        shot = QPixmap(self.size())
        shot.fill(Qt.transparent)
        self.render(shot)
        drag.setPixmap(shot)
        drag.setHotSpot(self._press)
        self.board.begin_drag()
        try:
            drag.exec(Qt.MoveAction)
        finally:
            # Before end_drag(), which redraws the board and may well delete
            # this very widget.
            self._press = None
            self.board.end_drag()

    def mouseReleaseEvent(self, e):
        self._press = None

    @staticmethod
    def _age_days(ts):
        """Whole days since `ts`, or None where there is no usable one.

        None and 0 are different answers though both draw nothing: no
        timestamp means nobody has ever posted in the thread, 0 means somebody
        posted today. The sandbox is full of the first, because the seeder
        writes every message as the bot.

        A naive timestamp is read as UTC rather than refused -- everything
        writing one here already is. **Without that it was not a wrong
        number, it was a dead card**: naive minus aware raises TypeError, not
        ValueError, so it escaped `_build_view` and the card failed to draw.
        The same mismatch CLAUDE.md records as a SQL trap, one language up.
        """
        # A string or it is nothing: the column is TEXT, so this is the shape
        # it always arrives in, and asking rather than catching keeps a real
        # mistake further up from being swallowed here as "no age".
        if not isinstance(ts, str) or not ts:
            return None
        try:
            then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None                      # not a timestamp at all
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).days

    @staticmethod
    def _ago(ts):
        """How long since a person last said anything in the thread.

        Nothing at all for today. Most of the board is today most of the time,
        so "today" was a word on almost every card that told you what you
        would have assumed anyway -- and it read as information, which cost it
        a glance each time. The number is worth having exactly when it is not
        today.

        A negative reads as nothing too. It means a message is stamped in the
        future, which is a clock disagreeing rather than an age, and "-1d" on
        a card is a bug report nobody can act on.
        """
        d = Card._age_days(ts)
        return "" if d is None or d <= 0 else f"{d}d"

    @classmethod
    def _age_forms(cls, ts):
        """What the age can say, longest first, or nothing at all.

        Two forms rather than one, for the reason `status_forms()` has them:
        a row too tight for the whole label should drop a **word**, not cut
        one. `Last reply 3d` is 143px and `3d` is 22, and the short form is
        exactly what the card said before it was labelled -- so giving the
        words up costs the number nothing.
        """
        d = cls._age_days(ts)
        if d is None or d <= 0:
            return []
        # "Last reply", not "last updated": this is the newest message from a
        # person, where `cards.updated_at` is this machine's own clock about
        # its own row -- two different facts, and the wrong word would send a
        # reader to the wrong one. The bare number read as the *ticket's* age,
        # which is the more obvious thing to put on a card and is not what it
        # is; the words are what stop that, and the tooltip is then free to
        # carry only the two rules it cannot show.
        return [f"Last reply {d}d", f"{d}d"]

    @staticmethod
    def _age_width(text):
        """What a form costs the row. 0 for no age, which takes no column."""
        if not text:
            return 0
        w = QLabel(text)
        w.setStyleSheet(age_css())
        w.ensurePolished()
        return w.sizeHint().width()

    @classmethod
    def _age_label(cls, text, ts):
        """The age as it sits on the card, or None when there is nothing to say.

        Built only when it has a number: an empty label still takes a column.
        It sits in the footer, whose left end is empty, rather than the head,
        where every column is paid for by the client name -- which is what
        makes the words affordable.

        The tooltip says what the number counts: `3d` reads as the ticket's
        age, which is not what this is.
        """
        if not text:
            return None
        d = cls._age_days(ts)
        w = QLabel(text)
        w.setStyleSheet(age_css())
        day = "day" if d == 1 else "days"
        w.setToolTip(f"A person last posted in this thread {d} {day} ago." + "\n\n"
                     "Messages from bots don't count, and nothing is shown "
                     "when somebody has posted today.")
        return w



class Band(QWidget):
    def __init__(self, priority, board):
        super().__init__()
        self.priority = priority
        self.board = board
        self.cards = []
        self._sig = None
        self.collapsed = False
        self.setAcceptDrops(True)
        self.setMaximumWidth(BOARD_MAX)

        tint = T.BAND_TINT[priority]
        accent = T.BAND_TEXT[priority]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- header: clickable, and styled the same way for every band --------
        self.header = ClickableWidget()
        self.header.clicked.connect(self.toggle)
        self.header.setCursor(Qt.PointingHandCursor)
        # A QWidget *subclass* paints its own background, so a stylesheet
        # background is ignored until this is set.
        self.header.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(8, 5, 8, 5)
        h.setSpacing(8)

        self.caret = QLabel("▾")
        # Once a QLabel carries a stylesheet it paints its palette background,
        # which is the window colour.
        self.caret.setStyleSheet(
            f"color:{accent}; font-size:11px; background:transparent;")
        # On the one band that is asking for something. In the header rather
        # than in BAND_LABEL, because the label is also read out in the
        # activity feed, where a warning sign on a reorder would be shouting.
        if priority == "unassigned":
            sign = QLabel(CAUTION)
            sign.setStyleSheet(
                f"color:{accent}; font-size:12px; background:transparent;")
            h.addWidget(sign)
        self.title = QLabel(BAND_LABEL[priority])
        self.count = QLabel("0")

        f = QFont()
        f.setPointSize(11)
        f.setWeight(QFont.DemiBold)
        self.title.setFont(f)
        self.title.setStyleSheet(f"color:{accent}; background:transparent;")
        self.count.setStyleSheet(
            f"color:{accent}; font-size:11px; background:transparent;")

        self.header.setObjectName("bandHeader")
        # Scope to the header itself. The bar is where the band's colour goes:
        # 4px of the real thing down the left, the heading's ink beside it, and
        # a strip behind them that is barely tinted at all. Filling the strip
        # instead put a second colour behind every ticket in the run, which is
        # what a card's own fill is for.
        self.header.setStyleSheet(
            f"#bandHeader {{ background:{tint};"
            f" border-left:4px solid {accent}; border-radius:4px; }}")

        # Only the band that has something to say gets a label. An empty one
        # was made for the other four and then never added to anything, and a
        # QWidget with no parent is a top-level window: four of them per board,
        # and four more every time a theme change rebuilds it.
        self.hint = None
        if priority == "unassigned":
            self.hint = QLabel("new — drag into a priority")
            self.hint.setStyleSheet(
                f"color:{accent}; font-size:11px; background:transparent;")

        h.addWidget(self.caret)
        h.addWidget(self.title)
        h.addWidget(self.count)
        if self.hint is not None:
            h.addSpacing(12)    # the hint is a caption, not part of the count
            h.addWidget(self.hint)
        h.addStretch(1)

        # One per band, because the band is the answer to "where does this
        # go" and pressing the one you mean has already given it. Needs
        # Attention keeps one too: every thread opened in Discord lands there
        # anyway, so a ticket with no home yet is the ordinary case, not an
        # exception.
        self.add_btn = QPushButton("+ New Ticket")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setToolTip(f"Start a ticket in {BAND_LABEL[priority]}")
        self.add_btn.setStyleSheet(
            f"QPushButton {{ {BTN_HIT} border:1px solid {rgba(accent, 0.45)};"
            f" border-radius:5px; color:{accent}; background:transparent;"
            f" font-size:11px; }}"
            f"QPushButton:hover {{ background:{rgba(accent, 0.14)}; }}"
            f"QPushButton:disabled {{ color:{T.MUTED};"
            f" border-color:{T.LINE}; }}")
        self.add_btn.clicked.connect(
            lambda: self.board.start_ticket(self.priority))
        h.addWidget(self.add_btn)
        outer.addWidget(self.header)

        # -- the container the cards sit in -----------------------------------
        # Untinted. The band's colour belongs on its header, which is the thing
        # that names the band; washing the whole run of cards in it as well put
        # a second colour behind every ticket, so a PROD card in Medium was
        # amber on blue and a PROD card in High was amber on amber. The tickets
        # carry the colour now, and this is what they sit on.
        self.panel = QWidget()
        self.panel.setObjectName("bandPanel")
        self.panel.setStyleSheet("#bandPanel { background:transparent; }")
        self.lay = QVBoxLayout(self.panel)
        # Cards stacked at 8px read as one block with lines through it. The
        # extra two are what let each one be seen as a card.
        self.lay.setContentsMargins(10, 10, 10, 12)
        self.lay.setSpacing(10)
        outer.addWidget(self.panel)

        # Shown only while a drag is running and this band is empty. Without
        # it an empty band is a blank strip that gives no sign it will take the
        # card, and the drop goes to whichever neighbour has cards in it.
        # Parented to the panel from the start. It is only put into the layout
        # once a drag needs it, and until then an unparented widget is a
        # window of its own -- one per band, sitting there hidden, for the
        # whole life of the board.
        self.empty_hint = QLabel("drop here", self.panel)
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setMinimumHeight(DROP_ZONE_MIN)
        self.empty_hint.setStyleSheet(
            f"color:{accent}; font-size:11px; background:transparent;"
            f" border:2px dashed {rgba(accent, 0.45)}; border-radius:4px;")
        self.empty_hint.hide()

        self.marker = QFrame(self.panel)
        self.marker.setFixedHeight(3)
        self.marker.setStyleSheet(f"background:{accent};")
        self.marker.hide()

    def toggle(self):
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, yes):
        """Fold the cards away; the header keeps showing the count."""
        self.collapsed = yes
        self.panel.setVisible(not yes)
        self.caret.setText("▸" if yes else "▾")

    def card_width(self):
        """The width a card in this band will be given."""
        m = self.lay.contentsMargins()
        return max(self.panel.width() - m.left() - m.right(), CARD_MIN_W)

    def set_cards(self, cards):
        # Rebuilding a card is ~4ms, so redrawing every band on every poll costs
        # a fifth of a second of frozen UI on a 50-card board -- for identical
        # content. Only tear down when something actually changed.
        #
        # The width counts as a change: a card cuts its client name to the room
        # it has, so a narrower window is a different picture of the same
        # cards. The rail carries its row width in here for the same reason.
        room = self.card_width()
        sig = json.dumps([cards, room], sort_keys=True, default=str)
        if sig == self._sig:
            self.cards = cards
            self._apply_drag_height()
            return
        self._sig = sig

        while self.lay.count():
            it = self.lay.takeAt(0)
            w = it.widget()
            if w is not None and w not in (self.marker, self.empty_hint):
                # Hide before unparent, and keep it that way. setParent(None)
                # on a visible widget makes it a visible top-level window
                # until the event loop deletes it -- rebuilding the feed once
                # put 151 blank windows on the desktop for 1.2s each.
                w.hide()
                w.setParent(None)
                w.deleteLater()
        # takeAt() above pulled the marker out along with the cards. Leaving it
        # shown paints a stale accent line at whatever row it last occupied,
        # because a refresh can land mid-drag. dragMoveEvent re-inserts it.
        self.marker.hide()
        self.cards = cards
        self.count.setText(str(len(cards)))
        for c in cards:
            self.lay.addWidget(Card(c, self.board, room))
        self._apply_drag_height()

    def _apply_drag_height(self):
        """Give an empty band a real target while a drag is running.

        The height goes on the panel rather than the band, so the whole of it
        is drop area instead of most of it being header.
        """
        wants_hint = self.board.dragging and not self.cards
        if self.board.dragging:
            self.setVisible(True)
            self.panel.setMinimumHeight(DROP_ZONE_MIN if not self.cards else 0)
        else:
            self.panel.setMinimumHeight(0)
            # Every band is drawn, empty or not, and whatever a filter has
            # left in it. A band is a drop target and carries its own
            # "+ New Ticket", so hiding one takes away the only way to drag a
            # card into that priority or start one there.
            self.setVisible(True)

        if wants_hint:
            if self.lay.indexOf(self.empty_hint) < 0:
                self.lay.addWidget(self.empty_hint)
            self.empty_hint.show()
        else:
            self.empty_hint.hide()
            if self.lay.indexOf(self.empty_hint) >= 0:
                self.lay.removeWidget(self.empty_hint)

    def _drop_index(self, y, dragged_id=None):
        """
        Index into the card list (excluding the dragged card) where the drop
        lands.
        """
        index = 0
        for i in range(self.lay.count()):
            w = self.lay.itemAt(i).widget()
            if not isinstance(w, Card) or w.thread_id == dragged_id:
                continue
            # y arrives in Band coordinates; the cards sit inside the panel.
            if y > self.panel.y() + w.y() + w.height() / 2:
                index += 1
        return index

    def _marker_slot(self, index, dragged_id=None):
        """Layout row for the insertion marker, given a card index."""
        seen = 0
        for i in range(self.lay.count()):
            w = self.lay.itemAt(i).widget()
            if not isinstance(w, Card) or w.thread_id == dragged_id:
                continue
            if seen == index:
                return i
            seen += 1
        return self.lay.count()

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            # Dropping into a folded band would put the card somewhere the
            # person can't see, so open it as the drag arrives.
            if self.collapsed:
                self.set_collapsed(False)
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasFormat(MIME):
            return
        # Detach first, THEN measure.
        self.lay.removeWidget(self.marker)
        if not self.cards:
            # The 'drop here' panel is the whole band; an insertion line under
            # it would be answering a question nobody asked.
            self.marker.hide()
            e.acceptProposedAction()
            return
        dragged = bytes(e.mimeData().data(MIME)).decode()
        index = self._drop_index(e.position().toPoint().y(), dragged)
        # insertWidget() reparents the marker before inserting it, which drops
        # it back out of this layout and shrinks the count under a slot that was
        # measured a moment ago.
        slot = min(self._marker_slot(index, dragged), self.lay.count())
        self.lay.insertWidget(slot, self.marker)
        self.marker.show()
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.marker.hide()
        self.lay.removeWidget(self.marker)

    def dropEvent(self, e):
        tid = bytes(e.mimeData().data(MIME)).decode()
        self.lay.removeWidget(self.marker)
        self.marker.hide()

        order = [c["thread_id"] for c in self.cards if c["thread_id"] != tid]
        pos = min(self._drop_index(e.position().toPoint().y(), tid), len(order))
        after = order[pos - 1] if pos > 0 else None
        before = order[pos] if pos < len(order) else None

        # Dropping back where it already was is a no-op, not a write.
        current = [c["thread_id"] for c in self.cards]
        if (self.priority == self.board.priority_of(tid)
                and current[pos:pos + 1] == [tid]):
            e.acceptProposedAction()
            return

        self.board.move_card(tid, self.priority, after, before)
        e.acceptProposedAction()


class RailRow(QFrame):
    """One row in the side rail: who it's for, and what the job is.

    The client alone was enough to find a card by while a client had one
    open, and stopped being enough the moment one had two. The summary goes
    underneath in the muted ink, smaller, because it answers "which one"
    rather than "whose".

    A thread whose title cannot be read has no summary to show and simply
    gets no second line -- its raw title is already on the first one, which
    is all there is to say about it.
    """

    def __init__(self, data, board, room=180):
        super().__init__()
        self.data = data
        self.board = board
        self.thread_id = data["thread_id"]
        self._press = None

        stripe = T.QUEUE.get(data.get("queue") or "", T.NEUTRAL)[0]
        fill, edge, px = card_skin(data)
        # A triage row gives its whole outline over to the red rather than
        # keeping a queue stripe, which at 26px tall is most of its edge.
        left = "" if needs_triage(data) else f" border-left:3px solid {stripe};"
        self.setStyleSheet(f"RailRow {{ background:{fill};"
                           f" border:{px}px solid {edge};{left}"
                           f" border-radius:{ROW_RADIUS}px; }}" + tip_css())
        self.setCursor(Qt.OpenHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(0)

        who = (data.get("client_override") or data.get("client_raw")
               or data.get("name") or "\u2014")
        f = QFont()
        f.setPointSize(9)
        f.setWeight(QFont.DemiBold)
        name = QLabel(QFontMetrics(f).elidedText(who, Qt.ElideRight, room))
        name.setFont(f)
        name.setStyleSheet(f"color:{T.INK}; background:transparent;")
        lay.addWidget(name)

        # Only when the first line is actually a client. Without one it is
        # already showing the raw title, and the summary is a slice of that
        # same string -- "OPS: outdated escalatio..." over "outdated
        # escalation list" is one fact taking two lines. The second line is
        # here to answer "which job for this client", so with no client
        # there is no question for it to answer.
        named = bool(data.get("client_override") or data.get("client_raw"))
        detail = (data.get("summary") or "").strip() if named else ""
        if detail:
            sf = QFont()
            sf.setPointSize(8)
            sub = QLabel(
                QFontMetrics(sf).elidedText(detail, Qt.ElideRight, room))
            sub.setFont(sf)
            sub.setStyleSheet(f"color:{T.MUTED}; background:transparent;")
            lay.addWidget(sub)

        # The band in words, and both lines untruncated, one hover away.
        tip = [who] + ([detail] if detail else [])
        tip.append(BAND_LABEL[data["priority"]])
        self.setToolTip("\n".join(tip))

    # A press that travels becomes a drag; one that doesn't is a click.
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseMoveEvent(self, e):
        if self._press is None or not self.board.writable():
            return
        if (e.position().toPoint() - self._press).manhattanLength() < DRAG_THRESHOLD:
            return
        mime = QMimeData()
        mime.setData(MIME, self.thread_id.encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        shot = QPixmap(self.size())
        shot.fill(Qt.transparent)
        self.render(shot)
        drag.setPixmap(shot)
        drag.setHotSpot(self._press)
        self.board.begin_drag()
        try:
            drag.exec(Qt.MoveAction)
        finally:
            # Before end_drag(), which redraws the board and may well delete
            # this very widget.
            self._press = None
            self.board.end_drag()

    def mouseReleaseEvent(self, e):
        if (self._press is not None
                and (e.position().toPoint() - self._press).manhattanLength()
                < DRAG_THRESHOLD):
            self.board.reveal(self.thread_id)
        self._press = None


class RailBandHead(QWidget):
    """A band's name in the running order, with a rule running off it.

    A bare coloured bar said a band started here but never which -- you
    counted down from the top. The word says it outright, in the neutral ink:
    spending a third colour on a fact the card's tag and the board's header
    already carry is what the tag rule exists to stop. The rule carries the
    eye across, and is a hairline rather than a bar.

    Clicking it collapses the band. A long board puts High and Low a screen
    apart, and folding what is between them is the difference between a drag
    you can make in one movement and one you cannot -- so a collapsed band
    stays collapsed *during* a drag, which is the whole point of it. It
    remains a place to drop: the header takes a card and the band names
    itself, exactly as an empty band's slot does.
    """

    def __init__(self, priority, count, collapsed, rail=None):
        super().__init__()
        self.priority = priority
        self.collapsed = collapsed
        self.rail = rail
        self.setStyleSheet("background:transparent;")
        self.setCursor(Qt.PointingHandCursor)
        shown = BAND_LABEL[priority]
        self.setToolTip(f"{'Show' if collapsed else 'Hide'} {shown}")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, RAIL_HEAD_GAP, 0, 3)
        row.setSpacing(6)

        self.caret = QLabel("▸" if collapsed else "▾")
        row.addWidget(self.caret)

        self.name = QLabel(shown)
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        row.addWidget(self.name)

        # Collapsed, the count is the only thing left saying there is work in
        # here. An empty band says so rather than leaving the rule to explain
        # a header with nothing under it.
        self.note = QLabel(f"{count}" if collapsed else "" if count else "empty")
        row.addWidget(self.note)

        self.rule = QFrame()
        self.rule.setFixedHeight(RAIL_BAR_H)
        self.rule.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self.rule, 1)
        self._paint(False)

    def _paint(self, hot):
        # Hot uses the accent the drop marker already uses, so a target and an
        # insertion line are recognisably the same promise.
        ink = T.ACCENT if hot else T.MUTED
        line = T.ACCENT if hot else T.LINE
        self.caret.setStyleSheet(f"color:{ink}; font-size:9px;"
                                 f" background:transparent;")
        self.name.setStyleSheet(f"color:{ink}; font-size:10px;"
                                f" letter-spacing:0.5px; background:transparent;")
        self.note.setStyleSheet(f"color:{T.MUTED if hot else T.LINE};"
                                f" font-size:10px; background:transparent;")
        self.rule.setStyleSheet(f"background:{line}; border:none;")

    def set_hot(self, hot):
        """Light up while a drag is over a collapsed band, as its slot would."""
        self._paint(hot and self.collapsed)

    def mouseReleaseEvent(self, e):
        if self.rail is not None and e.button() == Qt.LeftButton:
            self.rail.toggle_band(self.priority)


class RailZone(QWidget):
    """The place an empty band keeps in the running order, during a drag.
    """

    def __init__(self, priority):
        super().__init__()
        self.priority = priority

        # The dashed box is inset, so the rows above and below are pushed clear
        # of it rather than sitting against its border
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, RAIL_ZONE_GAP, 0, RAIL_ZONE_GAP)
        lay.setSpacing(0)
        self.box = QLabel(BAND_LABEL[priority].lower())
        self.box.setAlignment(Qt.AlignCenter)
        self.box.setMinimumHeight(RAIL_ZONE_MIN)
        lay.addWidget(self.box)
        self._paint(False)

    def _paint(self, hot):
        tint, edge = T.BAND_CARD[self.priority]
        ink = T.BAND_TEXT[self.priority]
        self.box.setStyleSheet(
            f"background:{tint if hot else 'transparent'}; color:{ink};"
            f" font-size:10px; border:2px dashed {edge if hot else rgba(ink, 0.4)};"
            f" border-radius:4px;")

    def set_hot(self, hot):
        """Fill in while the pointer is over it, so the target is unambiguous."""
        self._paint(hot)


class Rail(QWidget):
    """Every open ticket on one screen, in board order.
    """

    def __init__(self, board):
        super().__init__()
        self.board = board
        self.cards = []
        self._sig = None
        self.folded = False
        self._spacer = None
        # Which bands are folded away, remembered like the rail's own width.
        self.collapsed = set(board.settings.get("rail_collapsed") or [])
        self.setAcceptDrops(True)
        # A range, not a fixed width -- a fixed child gives the splitter
        # handle nothing to move. set_folded() fixes it, because folded is
        # the button's business rather than the handle's.
        self.setMinimumWidth(RAIL_MIN_W)
        self.setMaximumWidth(RAIL_MAX_W)
        self.resize(RAIL_WIDTH, self.height())
        # Scoped to the rail itself: an unscoped background rule cascades to
        # every descendant and to the tooltips they own, which paints a
        # tooltip's text the same colour as its box.
        #
        # WA_StyledBackground because a plain QWidget subclass ignores a
        # stylesheet background unless it opts in -- without it the rule
        # below does nothing at all, silently.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"Rail {{ background:{T.PANEL}; }}" + tip_css())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 4, 8)
        outer.setSpacing(6)

        # Not "Priority" -- the bands are the priorities, so that name reads
        # like a filter.
        self.head = QLabel("Running order")
        f = QFont()
        f.setPointSize(10)
        f.setWeight(QFont.DemiBold)
        self.head.setFont(f)
        self.head.setStyleSheet(f"color:{T.INK}; background:transparent;")

        self.fold_btn = fold_button(GLYPH_LEFT, "Hide the running order")
        self.fold_btn.clicked.connect(self.toggle_fold)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        top.addWidget(self.head)
        top.addStretch()
        top.addWidget(self.fold_btn)
        outer.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("background:transparent;")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(4)
        self.scroll.setWidget(inner)
        outer.addWidget(self.scroll, 1)

        self.hint = QLabel(self.RAIL_HINT)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color:{T.MUTED}; font-size:10px;"
                                f" background:transparent;")
        outer.addWidget(self.hint)

        self.marker = QFrame(inner)
        self.marker.setFixedHeight(3)
        self.marker.setStyleSheet(f"background:{T.ACCENT};")
        self.marker.hide()

    def toggle_fold(self):
        self.set_folded(not self.folded)

    def set_folded(self, yes, settle=True):
        """Fold down to a spine so the board gets the whole window."""
        self.folded = yes
        self.head.setVisible(not yes)
        self.scroll.setVisible(not yes)
        self.hint.setVisible(not yes)
        if yes:
            # A range, not a fixed width. Fixed, the pane could not be
            # moved at all -- and the handle is still sitting right there
            # against the spine, so the answer to dragging it was nothing
            # happening. The minimum is the spine; the maximum is what it
            # would have unfolded to, so the handle can pull it back out and
            # `_unfold_by_drag` turns that into an unfold.
            self.setMinimumWidth(RAIL_FOLDED_W)
            self.setMaximumWidth(RAIL_MAX_W)
        else:
            # Handed back to the splitter, which puts it where it was.
            self.setMinimumWidth(RAIL_MIN_W)
            self.setMaximumWidth(RAIL_MAX_W)
        # Both ways. Narrowing the widget does not narrow the pane it sits in:
        # the splitter keeps the width it last allotted, so folding left a
        # spine, then four hundred pixels of empty floor, then a handle
        # stranded in the middle of it -- and the board got none of the room
        # the fold was supposed to give it.
        # `settle` is False when the *handle* did this: the drag is already
        # the width somebody wants, and re-placing the panes would snap it
        # out from under the pointer mid-gesture.
        if settle:
            self.board._rail_sized = False
            self.board._stats_sized = False
            self.board._place_sides()
        self.layout().setContentsMargins(*((3, 10, 3, 8) if yes
                                           else (10, 10, 4, 8)))
        self.fold_btn.setText(GLYPH_RIGHT if yes else GLYPH_LEFT)
        self.fold_btn.setToolTip("Show the running order" if yes
                                 else "Hide the running order")

        # Folded, the list that was absorbing the spare height is hidden, and
        # the button drifts to the middle of the spine.
        lay = self.layout()
        if yes and self._spacer is None:
            lay.addStretch(1)
            self._spacer = lay.itemAt(lay.count() - 1)
        elif not yes and self._spacer is not None:
            lay.removeItem(self._spacer)
            self._spacer = None

    def row_width(self):
        """The pixels a row has for its text, at the width the rail is now.

        Pixels rather than a character count. The two lines were clipped at
        24 and 28 characters, which is a count and not a measurement, so
        dragging the rail wider gave the text more room and not one more
        letter of it -- and any per-character estimate to replace it is a
        guess about a proportional font. QFontMetrics.elidedText knows
        exactly, so the row asks it.
        """
        return max(self.width() - RAIL_ROW_CHROME, 40)

    RAIL_HINT = "Drag to reorder, click to jump"

    def set_cards(self, cards):
        # Same signature check the bands use.
        # The clip widths are part of what a row draws, so a rail that has been
        # dragged is a different picture of the same cards and has to be rebuilt.
        room = self.row_width()
        # Which bands are folded is part of the picture, the same way the
        # clip width is: without it here, collapsing a band changes nothing
        # on screen until the cards themselves happen to change.
        sig = json.dumps([cards, self.board.dragging, room,
                          sorted(self.collapsed)],
                         sort_keys=True, default=str)
        if sig == self._sig:
            self.cards = cards
            return
        self._sig = sig
        self.cards = cards

        while self.lay.count():
            it = self.lay.takeAt(0)
            w = it.widget()
            if w is not None and w is not self.marker:
                # Unparent first -- see Band.set_cards.
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.marker.hide()

        # Walk the bands rather than the cards, so a band with nothing in it
        # still gets its turn.
        for band in BANDS:
            group = [c for c in self.cards if c["priority"] == band]
            # Every band is named, empty or not, and the first one included.
            # Drawing one only when it holds something rearranges the list
            # under the pointer the moment a drag makes the empty ones
            # reappear -- and an empty band is still a drop target carrying
            # its own "+ New Ticket". The head already knows how to say
            # "empty".
            shut = band in self.collapsed
            self.lay.addWidget(RailBandHead(band, len(group), shut, self))
            if shut:
                # Deliberately still folded mid-drag. Shortening the distance
                # between High and Low is what somebody collapsed Medium for,
                # and opening it under them would undo that at the moment it
                # matters. The header takes the drop instead.
                continue
            for c in group:
                self.lay.addWidget(RailRow(c, self.board, room))
            # Drag-only, unlike the head above it: the zone is a target, and
            # four of them stacked up at rest is a rail of empty boxes with
            # the running order pushed off the bottom.
            if not group and self.board.dragging:
                self.lay.addWidget(RailZone(band))
        self.lay.addStretch()

    def toggle_band(self, priority):
        """Fold a band away, or bring it back."""
        if priority in self.collapsed:
            self.collapsed.discard(priority)
        else:
            self.collapsed.add(priority)
        self.board.remember_collapsed(self.collapsed)
        self.set_cards(self.cards)

    def _heads(self):
        return [self.lay.itemAt(i).widget() for i in range(self.lay.count())
                if isinstance(self.lay.itemAt(i).widget(), RailBandHead)]

    def _shut_head_at(self, y):
        """The collapsed band's header under the pointer, if there is one.

        Only a collapsed one: an open band's header is a label sitting above
        rows that can speak for themselves, and taking the drop there would
        send a card to the bottom of the band when the row it was dropped
        beside said otherwise.
        """
        for h in self._heads():
            if not h.collapsed:
                continue
            top = h.mapTo(self, QPoint(0, 0)).y()
            if top <= y <= top + h.height():
                return h
        return None

    def _zones(self):
        return [self.lay.itemAt(i).widget() for i in range(self.lay.count())
                if isinstance(self.lay.itemAt(i).widget(), RailZone)]

    def _zone_at(self, y):
        """The empty band's slot under the pointer, if it is over one."""
        for z in self._zones():
            top = z.mapTo(self, QPoint(0, 0)).y()
            if top <= y <= top + z.height():
                return z
        return None

    def _drop_at(self, y, dragged):
        """Where a drop at this height lands, as (band, after_id, before_id).
        """
        rows = [w for w in self._rows() if w.thread_id != dragged]
        if not rows:
            return BANDS[0], None, None

        # The row under the pointer, or the nearest one when it is in a gap.
        best = best_top = None
        best_gap = None
        for w in rows:
            top = w.mapTo(self, QPoint(0, 0)).y()
            gap = 0 if top <= y <= top + w.height() else min(
                abs(y - top), abs(y - (top + w.height())))
            if best_gap is None or gap < best_gap:
                best, best_top, best_gap = w, top, gap

        i = rows.index(best)
        band = best.data["priority"]
        if y < best_top + best.height() / 2:
            # Above its middle: this card goes in front of that one. Only take
            # a neighbour from the same band -- the row before it may belong to
            # the band above, and is not this card's neighbour at all.
            before = best.thread_id
            prev = rows[i - 1] if i else None
            after = prev.thread_id if prev and prev.data["priority"] == band else None
        else:
            after = best.thread_id
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            before = nxt.thread_id if nxt and nxt.data["priority"] == band else None
        return band, after, before

    def _rows(self):
        out = []
        for i in range(self.lay.count()):
            w = self.lay.itemAt(i).widget()
            if isinstance(w, RailRow):
                out.append(w)
        return out

    def _drop_index(self, y, dragged):
        index = 0
        for w in self._rows():
            if w.thread_id == dragged:
                continue
            # y is in Rail coordinates; the rows live inside the scroll area.
            if y > w.mapTo(self, QPoint(0, 0)).y() + w.height() / 2:
                index += 1
        return index

    def _marker_slot(self, index, dragged):
        seen = 0
        for i in range(self.lay.count()):
            w = self.lay.itemAt(i).widget()
            if not isinstance(w, RailRow) or w.thread_id == dragged:
                continue
            if seen == index:
                return i
            seen += 1
        return max(self.lay.count() - 1, 0)      # above the trailing stretch

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasFormat(MIME):
            return
        y = e.position().toPoint().y()

        # Over an empty band's slot, that slot *is* the answer -- an insertion
        # line between two rows would be saying something else.
        hot = self._zone_at(y)
        for z in self._zones():
            z.set_hot(z is hot)

        # A collapsed band answers for itself the same way an empty one does:
        # the header is the target, and an insertion line between two rows of
        # some other band would be saying something else.
        shut = self._shut_head_at(y) if hot is None else None
        for h in self._heads():
            h.set_hot(h is shut)

        if hot is not None or shut is not None:
            self.marker.hide()
            self.lay.removeWidget(self.marker)
            e.acceptProposedAction()
            return

        self.lay.removeWidget(self.marker)
        dragged = bytes(e.mimeData().data(MIME)).decode()
        index = self._drop_index(y, dragged)
        slot = min(self._marker_slot(index, dragged), self.lay.count())
        self.lay.insertWidget(slot, self.marker)
        self.marker.show()
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.marker.hide()
        self.lay.removeWidget(self.marker)
        for z in self._zones():
            z.set_hot(False)
        for h in self._heads():
            h.set_hot(False)

    def dropEvent(self, e):
        tid = bytes(e.mimeData().data(MIME)).decode()
        self.marker.hide()
        self.lay.removeWidget(self.marker)
        for z in self._zones():
            z.set_hot(False)
        for h in self._heads():
            h.set_hot(False)

        # Dropped on a collapsed band's header: no neighbours to read, and
        # none needed -- the header names the band, and a neighbourless move
        # lands at the end of it.
        shut = self._shut_head_at(e.position().toPoint().y())
        if shut is not None:
            self.board.move_card(tid, shut.priority, None, None)
            e.acceptProposedAction()
            return

        # Dropped on an empty band's slot: it has no neighbours to read the
        # band off, and doesn't need any -- the slot names it.
        zone = self._zone_at(e.position().toPoint().y())
        if zone is not None:
            self.board.move_card(tid, zone.priority, None, None)
            e.acceptProposedAction()
            return

        band, after, before = self._drop_at(e.position().toPoint().y(), tid)
        self.board.move_card(tid, band, after, before)
        e.acceptProposedAction()


class Stats(QWidget):
    """The board over time, in the space to the right of it.

    Each figure earns its place by answering something the board cannot, and
    the first one that turns out to be wrong takes the others' credibility
    with it. There were four; "no ticket raised" was dropped after Julian
    read it.

    Sized like the running order, for the same reasons.
    """

    def __init__(self, board):
        super().__init__()
        self.board = board
        self.folded = False
        self._spacer = None
        self._sig = None
        self.setMinimumWidth(STATS_MIN_W)
        self.setMaximumWidth(STATS_MAX_W)
        self.resize(STATS_WIDTH, self.height())
        # Scoped, like Rail's: unscoped it cascades into every child and
        # their tooltips. And a plain QWidget subclass ignores a stylesheet
        # background unless it opts in -- without this the rule below did
        # nothing, unnoticed while this and the window behind it matched.
        self.setAttribute(Qt.WA_StyledBackground, True)
        # The canvas, the same as the running order. It was put on the floor
        # on the reasoning that the rail and the board are worked in and the
        # figures are only read -- which is true and is not what the eye does
        # with it: a panel the same value as the space around it stops being
        # a panel, and the three sections stopped matching each other for a
        # distinction nobody was asking the layout to draw. Every section
        # with content in it is the canvas; the floor is what they stand on.
        self.setStyleSheet(f"Stats {{ background:{T.PANEL}; }}" + tip_css())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 10, 10, 8)
        outer.setSpacing(6)

        self.head = QLabel("Stats")
        f = QFont()
        f.setPointSize(10)
        f.setWeight(QFont.DemiBold)
        self.head.setFont(f)
        self.head.setStyleSheet(f"color:{T.INK}; background:transparent;")

        self.fold_btn = fold_button(GLYPH_RIGHT, "Hide the figures")
        self.fold_btn.clicked.connect(self.toggle_fold)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        # The button first, because this panel's spine is the edge nearest the
        # board -- the mirror of the rail, whose spine is the far left.
        top.addWidget(self.fold_btn)
        top.addWidget(self.head)
        top.addStretch(1)
        outer.addLayout(top)

        # Outside the body on purpose. `set_stats` tears the body down and
        # builds it again whenever the numbers change, which is every time
        # the poll brings a different answer -- a combo rebuilt under
        # somebody's pointer loses its popup mid-choice, and would have to
        # have its value put back from settings on every redraw. This one is
        # built once and never touched again.
        self.window_box = Combo()
        self.window_box.addItems([label for label, _ in STATS_WINDOWS])
        self.window_box.setCursor(Qt.PointingHandCursor)
        self.window_box.setToolTip(
            "How far back Created and Closed count. Open is the backlog now "
            "and does not move with it.")
        want = board.settings.get("stats_days") or STATS_WINDOW_DEFAULT
        self.window_box.setCurrentIndex(
            next((i for i, (_, d) in enumerate(STATS_WINDOWS) if d == want), 3))
        self.window_box.currentIndexChanged.connect(self._window_changed)
        outer.addWidget(self.window_box)

        self.body = QVBoxLayout()
        # A gutter down the right, and only the right. Every figure on this
        # panel is right-aligned, so they all end at one edge -- and with no
        # margin that edge is where the scrollbar starts.
        #
        # It goes on the *body*, not the panel: the scroll area is what the
        # bar belongs to, so padding outside it moves the bar along with the
        # content and leaves the gap where it was. `FEED_GUTTER` is the same
        # rule on the feed.
        self.body.setContentsMargins(0, 0, STATS_GUTTER, 0)
        self.body.setSpacing(9)
        self.holder = QWidget()
        self.holder.setLayout(self.body)
        self.holder.setStyleSheet("background:transparent;")

        # The panel scrolls, the way the running order does. In a plain
        # layout Qt does not clip an overflowing block, it squashes every
        # block proportionally -- so one block too many draws a six-row table
        # as a single line, with nothing reporting a failure.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("background:transparent;")
        self.scroll.setWidget(self.holder)
        outer.addWidget(self.scroll, 1)

        self.hint = QLabel("waiting for Ernie")
        self.hint.setStyleSheet(f"color:{T.MUTED}; font-size:11px;"
                                f" background:transparent;")
        outer.addWidget(self.hint)

    # -- drawing -----------------------------------------------------------

    def _heading(self, text):
        lab = QLabel(text.upper())
        lab.setStyleSheet(f"color:{T.MUTED}; font-size:10px;"
                          f" background:transparent;")
        return lab

    def _line(self, text, colour=None, tip=None):
        lab = QLabel(text)
        lab.setStyleSheet(f"color:{colour or T.INK}; font-size:12px;"
                          f" background:transparent;")
        if tip:
            lab.setToolTip(tip)
        return lab

    def _bar(self, fraction, colour):
        """A proportional strip.

        Drawn rather than spelled with block characters: a block is a
        different width in every font, so the bars would not line up with
        each other and the comparison is the whole point of them.
        """
        holder = QWidget()
        holder.setFixedHeight(5)
        holder.setStyleSheet("background:transparent;")
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        fill = QWidget()
        fill.setStyleSheet(f"background:{colour}; border-radius:2px;")
        share = max(1, min(100, int(round(fraction * 100))))
        lay.addWidget(fill, share)
        lay.addStretch(100 - share)
        return holder

    def days(self):
        """The window the selector is on, in days."""
        i = max(0, min(self.window_box.currentIndex(), len(STATS_WINDOWS) - 1))
        return STATS_WINDOWS[i][1]

    def _window_changed(self, *_):
        """Kept, and asked for again straight away.

        The figures ride the slow lane -- they move when a ticket closes, not
        every five seconds -- but a dropdown that takes up to a minute to
        change the numbers under it reads as broken. Clearing the stamp makes
        the next poll fetch them, which is the same 5s round trip everything
        else on this board runs at.
        """
        self.board.settings["stats_days"] = self.days()
        self.board.save_settings()
        self.board.stats_at = 0
        self.board.refresh()

    def _tally(self, tally):
        """Open, created and closed, per tag, as a table.

        A table rather than three lists, because the three numbers are read
        together -- "three open, eleven in, twenty out" is a sentence about
        PROD, and the same figures on three separate rows is three facts to
        hold at once. It fits a 244px panel: a tag, then three columns.
        """
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)

        for col, (text, tip) in enumerate((
                ("open", "Open right now. This is a backlog, not a count for "
                         "the window -- it does not move when the window does."),
                ("new", f"Threads opened in the last {self.days()} days."),
                ("done", f"Tickets closed in the last {self.days()} days."))):
            lab = self._line(text, T.MUTED, tip=tip)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(lab, 0, col + 1)

        # In the toolbar's order, which is the palette's -- the filter
        # checkboxes are built by walking T.QUEUE, so PROD OPS ENG CS is the
        # order somebody has already read once across the top of the window.
        # QUEUES_OFFERED is the parser's order and is not the same.
        served = tally.get("queues") or []
        order = ([q for q in T.QUEUE if q in served]
                 + [q for q in served if q not in T.QUEUE])

        row = 1
        for q in order:
            stripe = T.QUEUE.get(q, T.NEUTRAL)[0]
            name = self._line(q, stripe)
            grid.addWidget(name, row, 0)
            for col, key in enumerate(("open", "created", "closed")):
                n = (tally.get(key) or {}).get(q, 0)
                # A nought is quiet: on a board where one tag does most of
                # the work, the rest of the table is noughts and reading them
                # as loud as the numbers makes the shape harder to see.
                cell = self._line(str(n), T.INK if n else T.MUTED)
                cell.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                grid.addWidget(cell, row, col + 1)
            row += 1

        # A painted strip, not QFrame.HLine: a framed line takes its colour
        # from the palette's Mid role and ignores a stylesheet background, so
        # it came out as a gap. The board's drop marker is a widget with a
        # background for the same reason.
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background:{T.LINE}; border:none;")
        grid.addWidget(rule, row, 0, 1, 4)
        row += 1

        totals = tally.get("totals") or {}
        grid.addWidget(self._line("all", T.MUTED), row, 0)
        for col, key in enumerate(("open", "created", "closed")):
            cell = self._line(str(totals.get(key, 0)))
            cell.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(cell, row, col + 1)

        grid.setColumnStretch(0, 1)
        holder = QWidget()
        holder.setLayout(grid)
        holder.setStyleSheet("background:transparent;")
        return holder

    def _moves(self, data):
        """Where tickets went when somebody retagged them.

        `PROD -> OPS  21`. The pair is the fact, so both ends are named.
        Which pairs count is Ernie's decision, so these rows are the whole of
        what was counted and cannot fail to make their own total. The bar is
        the destination's colour, against the biggest pair.
        """
        moves = (data or {}).get("moves") or []
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        if not moves:
            box.addWidget(self._line("nothing retagged", T.MUTED))
        top = max([m["count"] for m in moves] or [1]) or 1
        for m in moves[:STATS_MOVES_SHOWN]:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(self._line(
                f"{m['from']} {GLYPH_MOVE} {m['to']}", T.MUTED))
            row.addStretch(1)
            row.addWidget(self._line(str(m["count"])))
            box.addLayout(row)
            box.addWidget(self._bar(m["count"] / top,
                                    T.QUEUE.get(m["to"], T.NEUTRAL)[0]))
        rest = moves[STATS_MOVES_SHOWN:]
        if rest:
            # Summed rather than dropped, so the rows still add up to the
            # total below them -- a figure that does not add up is the first
            # one somebody stops believing.
            box.addWidget(self._line(
                a_few(sum(m["count"] for m in rest), "other move",
                      "other moves"),
                T.MUTED,
                tip="\n".join(f"{m['from']} {GLYPH_MOVE} {m['to']}  "
                              f"{m['count']}" for m in rest)))
        holder = QWidget()
        holder.setLayout(box)
        holder.setStyleSheet("background:transparent;")
        return holder

    def clear(self):
        while self.body.count():
            it = self.body.takeAt(0)
            w = it.widget()
            if w is not None:
                # Hide before unparenting: an unparented visible widget is a
                # top-level window until the event loop gets round to it.
                w.hide()
                w.setParent(None)
                w.deleteLater()
            elif it.layout() is not None:
                inner = it.layout()
                while inner.count():
                    sub = inner.takeAt(0)
                    sw = sub.widget()
                    if sw is not None:
                        sw.hide()
                        sw.setParent(None)
                        sw.deleteLater()

    def set_stats(self, data):
        """Draw the four figures, and only when they have changed."""
        sig = json.dumps(data, sort_keys=True, default=str)
        if sig == self._sig:
            return
        self._sig = sig
        self.clear()
        if not data:
            self.hint.setText("waiting for Ernie")
            self.hint.setVisible(not self.folded)
            return
        self.hint.hide()

        # The gutter comes out of the room a client name has, or the longest
        # ones would be cut to a width that no longer exists and sit under
        # the bar anyway.
        room = max(self.width() - STATS_ROW_CHROME - STATS_GUTTER, 60)
        fm = QFontMetrics(self.font())

        # First, because it is the block somebody came to the panel for: how
        # much there is, how much arrived, how much left, and of what.
        tally = data.get("tally")
        if tally:
            self.body.addWidget(self._heading("Tickets"))
            self.body.addWidget(self._tally(tally))

        # Retagging, straight after the tally: both are flows between the
        # same four tags over the same window, and the tally's own `new` and
        # `done` columns cannot show a ticket that arrived as one tag and
        # left as another. Julian asked how much PROD becomes OPS.
        tag_moves = data.get("tag_moves")
        if tag_moves is not None:
            self.body.addWidget(self._heading("Retagged"))
            self.body.addWidget(self._moves(tag_moves))

        # 1. Completed across the window. The trend is the point: one number
        #    throws away the shape, and the shape is the news. It follows the
        #    selector -- a timeframe control that does nothing to the biggest
        #    block on the panel is one nobody believes, which is how this was
        #    reported.
        done = data.get("completed") or {}
        bars = done.get("periods") or []
        self.body.addWidget(self._heading("Completed"))
        if not bars:
            self.body.addWidget(self._line("nothing closed yet", T.MUTED))
        else:
            # Against the tallest bar, not against the total: the question a
            # row of bars answers is which period was busier than which.
            top = max(b["count"] for b in bars) or 1
            for b in bars:
                label = QHBoxLayout()
                label.setContentsMargins(0, 0, 0, 0)
                label.addWidget(self._line(
                    period_name(b["start"], done.get("bucket")), T.MUTED))
                label.addStretch(1)
                label.addWidget(self._line(str(b["count"])))
                self.body.addLayout(label)
                self.body.addWidget(self._bar(b["count"] / top, T.ACCENT))

        # 2. Open longest. The list that changes what somebody does today.
        self.body.addWidget(self._heading("Open longest"))
        ageing = data.get("ageing") or []
        if not ageing:
            self.body.addWidget(self._line("nothing open", T.MUTED))
        for a in ageing:
            who = a.get("client_raw") or a.get("name") or a["thread_id"]
            days = a.get("days") or 0
            row = ClickableWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            age = self._line(f"{days}d",
                             T.RED_FG if days >= STATS_OLD_D else T.AMBER_FG)
            age.setFixedWidth(fm.horizontalAdvance("999d") + 2)
            lay.addWidget(age)
            lay.addWidget(self._line(
                fm.elidedText(str(who), Qt.ElideRight, room),
                tip=a.get("name") or ""))
            lay.addStretch(1)
            row.setCursor(Qt.PointingHandCursor)
            row.clicked.connect(
                lambda _=None, t=a["thread_id"]: self.board.reveal(t))
            self.body.addWidget(row)

        # 3. How long one takes. The spread matters more than the average, so
        #    the slowest comes with it -- and the middle is quoted rather than
        #    the mean, which one very old ticket drags a long way.
        took = data.get("time_to_complete")
        self.body.addWidget(self._heading("Time to close"))
        if not took:
            self.body.addWidget(self._line("nothing closed yet", T.MUTED))
        else:
            self.body.addWidget(self._line(
                f"{took['median_days']} days, typically",
                tip=f"The middle of {took['count']} closed tickets. The "
                    f"average is {took['average_days']} days, which one very "
                    f"old ticket can drag a long way."))
            self.body.addWidget(self._line(
                f"slowest {took['slowest_days']} days", T.MUTED))

        # The blocks stack from the top and the leftover goes underneath
        # them. The panel scrolls now, so `setWidgetResizable` stretches this
        # widget to the viewport -- and with nothing to absorb the surplus a
        # QVBoxLayout hands it out *between* the items, which spread the rows
        # down the panel like a menu. It only shows when the content is
        # shorter than the panel, which is why it survived the renders that
        # had every block full.
        self.body.addStretch(1)


    # -- folding, the way the rail folds -----------------------------------

    def toggle_fold(self):
        self.set_folded(not self.folded)

    def set_folded(self, yes, settle=True):
        """Fold down to a spine, so the board gets the width back."""
        self.folded = yes
        self.head.setVisible(not yes)
        self.window_box.setVisible(not yes)
        self.scroll.setVisible(not yes)
        self.hint.setVisible(not yes and self._sig is None)
        if yes:
            # A range, not a fixed width. Fixed, the pane could not be
            # moved at all -- and the handle is still sitting right there
            # against the spine, so the answer to dragging it was nothing
            # happening. The minimum is the spine; the maximum is what it
            # would have unfolded to, so the handle can pull it back out and
            # `_unfold_by_drag` turns that into an unfold.
            self.setMinimumWidth(RAIL_FOLDED_W)
            self.setMaximumWidth(STATS_MAX_W)
        else:
            # Handed back to the splitter, which puts it where it was.
            self.setMinimumWidth(STATS_MIN_W)
            self.setMaximumWidth(STATS_MAX_W)
        # Both ways, for the reason Rail.set_folded gives.
        if settle:
            self.board._rail_sized = False
            self.board._stats_sized = False
            self.board._place_sides()
        self.layout().setContentsMargins(*((3, 10, 3, 8) if yes
                                           else (4, 10, 10, 8)))
        self.fold_btn.setText(GLYPH_LEFT if yes else GLYPH_RIGHT)
        self.fold_btn.setToolTip("Show the figures" if yes
                                 else "Hide the data")

        # Folded, the block that was absorbing the spare height is hidden and
        # the button drifts to the middle of the spine.
        lay = self.layout()
        if yes and self._spacer is None:
            lay.addStretch(1)
            self._spacer = lay.itemAt(lay.count() - 1)
        elif not yes and self._spacer is not None:
            lay.removeItem(self._spacer)
            self._spacer = None


class Bert(QMainWindow):
    def __init__(self, api_base):
        super().__init__()
        self.api = Api(api_base)
        self.settings = load_settings()
        self.fail_since = None
        self.dragging = False
        self._pending = None        # a poll held back by a drag or an editor
        self.editing_card = None
        self._shown = []            # the cards the filters left on screen
        self._attention_at = None   # where the jump has got to, by thread id
        self._attention_full = ""   # the count's longest form, for _fit_toolbar
        self.poller = None
        # The customer roster, refreshed far more slowly than the board.
        self.roster = []
        self.roster_at = 0.0
        self.connected = True
        self.last_sync = None
        self.health = {}            # the last /health payload
        # A rebuild render() had to skip because an editor was open, so
        # closing it draws what was missed rather than waiting for a poll.
        self._bands_stale = False
        # A card the next settled layout should land on, set by an
        # editor closing. Outranks the scroll anchor for that one
        # render, then clears itself.
        self._focus_card = None
        self.sharing = None         # its sharing block, or None if solo
        self.health_at = 0.0        # when that payload arrived, so both ages
                                    # off it go on counting between polls
        dark_titlebar(self)
        # Following the desktop has to mean following it, not reading it once
        # at startup: a laptop that darkens at sunset would otherwise leave
        # Bert the only bright window on the screen until it was restarted.
        hints = QApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self.desktop_theme_changed)
        # Where this build stands against the one answering. Read on every
        # poll; the dialog is shown once, the first time it is not "ok".
        self.update_state = "ok"
        self.update_said = ""
        self._update_told = False
        self._swapping_theme = False   # closing to reopen, not to quit
        self._gone = False             # this window's widgets are deleted
        self.awaiting = False       # a manual refresh, waiting on the next
        self.await_run = None       # read of Discord. await_run is the read it
        self.await_since = 0.0      # started from, to tell a new one landing
                                    # from the same one ageing
        self.filters = {q: True for q in T.QUEUE}
        # Which equipment chips are on. **Empty means unnarrowed**, which is
        # the opposite of `filters` above and is why they are not checkboxes:
        # turning one on narrows to it, rather than turning one off hiding it.
        self.equip: set[str] = set()
        # "" is every client. One at a time, unlike the equipment chips:
        # a ticket has several pieces of equipment and exactly one customer,
        # so "any of these" is a question nobody asks here.
        # ("all", ""), ("client", key) or ("raw", spelling). A tuple because
        # the last one filters on the exact string a card carries rather than
        # the customer it resolves to -- which is the whole point of showing
        # a misspelling separately.
        self.client_pick = (CLIENT_ALL, "")
        self.cards = []
        self.feed = []
        # Which rows are open, by event_id. The feed is rebuilt from
        # scratch on every poll, so this has to live outside the widgets
        # or an opened row would shut again on the next one.
        self.feed_open = set()
        self.completing = set()     # closed here, still drawn on the board

        self.setWindowTitle("Bert")
        if LOGO.exists():
            self.setWindowIcon(QIcon(str(LOGO)))
        self.resize(1000, 840)
        # Narrower than this and the feed's fixed columns start eating
        # the line, and the board columns are too tight to drop into.
        self.setMinimumWidth(1000)
        # The floor, so every section on it reads as a section. Painted the
        # canvas colour, the board column, the rail, the figures and the
        # feed all met the space around them at the same value and the
        # window read as one flat field -- most obviously with a panel
        # folded away, where the space it left looked like more board.
        self.setStyleSheet(f"QMainWindow {{ background:{T.WELL}; }}")

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.hide()
        outer.addWidget(self.banner)

        # Its own strip, so it cannot be painted over by the update banner --
        # both can be true at once. Amber, not red: nothing is asking for a
        # person. Across the window rather than on the figures panel because a
        # screenshot is how an invented figure would escape into a meeting.
        self.demo = QLabel()
        self.demo.setAlignment(Qt.AlignCenter)
        self.demo.hide()
        outer.addWidget(self.demo)

        # A write-ahead log that has stopped checkpointing, which is the one
        # failure here that takes the whole board down and says nothing on
        # the way. Its own strip for the reason the others have one: a board
        # can be showing demo data, be a build behind, and have a runaway WAL
        # all at once, and none of those may paint over another.
        self.wal = QLabel()
        self.wal.setAlignment(Qt.AlignCenter)
        self.wal.hide()
        outer.addWidget(self.wal)

        # Its own strip rather than a second use of the banner: a save must not
        # paint over "can't reach Ernie" and then hide it on the way out.
        self.toast = QLabel()
        self.toast.setAlignment(Qt.AlignCenter)
        self.toast.setStyleSheet(
            f"background:{T.INFO_BG}; color:{T.INFO_FG}; padding:7px; font-size:12px;")
        self.toast.hide()
        outer.addWidget(self.toast)
        self.toast_timer = QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self._clear_toast)

        outer.addWidget(self._toolbar())
        # **Its own row, because the first one has no room.** At the window's
        # minimum width the toolbar already asks for about 45px more than it
        # has -- `_fit_toolbar` exists entirely to shorten two labels until it
        # fits -- so five more controls in there would be five more things for
        # it to squeeze, and the search box would lose the width again.
        outer.addWidget(self._filter_row())

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        # The area around the column: the floor, like the space beside a
        # folded panel, so the column reads as a section standing on it.
        #
        # Named and styled, never set through the palette -- a palette set on
        # a viewport is overwritten by the application palette `apply_theme`
        # installs, silently, leaving the strip the same value as the board.
        # The object name is what keeps the rule off the cards: a bare
        # `background:` on a scroll area cascades into every band and card
        # inside it.
        vp = self.scroll.viewport()
        vp.setObjectName("boardBack")
        vp.setAttribute(Qt.WA_StyledBackground, True)
        vp.setStyleSheet(f"#boardBack {{ background:{T.WELL}; }}")
        # Widget smaller than the viewport: centre it. The column is capped
        # at BOARD_MAX, so on anything wide there is spare room in here, and
        # pinning it left meant folding the running order did not give the
        # board anything -- it slid across to where the rail had been and left
        # the same gap on the other side. Centred, folding either side widens
        # the space around the column evenly and the tickets stay where the
        # eye already is.
        self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        board = QWidget()
        board.setObjectName("boardColumn")
        # Opted in, or the rule below is decoration: a plain QWidget paints no
        # stylesheet background without it. It never had, so what looked like
        # the column's colour was the viewport showing through -- which is why
        # the strip beside the column could not be told apart from it.
        board.setAttribute(Qt.WA_StyledBackground, True)
        # The column keeps the canvas colour and stops where the bands stop
        # A rule down both sides, now that the column floats rather than
        # sitting against the rail: one edge drawn and the other not reads as
        # a mistake once there is floor on both sides of it.
        board.setStyleSheet(f"#boardColumn {{ background:{T.CANVAS};"
                            f" border-left:1px solid {T.LINE};"
                            f" border-right:1px solid {T.LINE}; }}")
        self.board_lay = QVBoxLayout(board)
        self.board_lay.setContentsMargins(BOARD_PAD, 10, BOARD_PAD, 24)
        self.board_lay.setSpacing(16)

        self.bands = {}
        for b in BANDS:
            self.bands[b] = Band(b, self)
            self.board_lay.addWidget(self.bands[b])
        self.board_lay.addStretch()
        # + the column's own margins, so the edge sits just clear of the cards.
        board.setMaximumWidth(BOARD_MAX + BOARD_PAD * 2)
        self.scroll.setWidget(board)

        # The cap goes on the *scroll area*, not on the column inside it, so
        # the area's scrollbar comes to the cap with it. Left to fill the
        # pane, the bar stays out at the pane's own edge -- with the running
        # order folded it sits well right of the board it belongs to, hard
        # against the figures panel. The rule `feed_scroll` already follows.
        #
        # Added by stretch factor, never by an alignment flag: a scroll area
        # added with one takes its own sizeHint, which is small, cap or no
        # cap. With a factor it reaches the cap and the spacers centre it.
        self.board_holder = QWidget()
        self.board_holder.setObjectName("boardHolder")
        self.board_holder.setAttribute(Qt.WA_StyledBackground, True)
        self.board_holder.setStyleSheet(
            f"#boardHolder {{ background:{T.WELL}; }}")
        hold = QHBoxLayout(self.board_holder)
        hold.setContentsMargins(0, 0, 0, 0)
        hold.setSpacing(0)
        # Two spacers whose widths are worked out rather than two stretches
        # splitting what is left evenly. Even is centred only while the two
        # side panels match: drag the running order wide and the figures
        # narrow and the pane's own middle is not the window's, so a column
        # centred inside the pane sits off-centre on screen. `_centre_board()`
        # works them out against the whole splitter.
        #
        # Neither may carry a stretch factor, or the three of them split the
        # pane between themselves and the area never reaches its cap.
        self.board_pad_l = QWidget()
        self.board_pad_r = QWidget()
        for pad in (self.board_pad_l, self.board_pad_r):
            pad.setStyleSheet("background:transparent;")
            pad.setFixedWidth(0)
        hold.addWidget(self.board_pad_l)
        hold.addWidget(self.scroll, 1)
        hold.addWidget(self.board_pad_r)
        # Its own bar rides inside the cap, so the cap has to allow for it or
        # the column loses that much width whenever the board scrolls.
        self.scroll.setMaximumWidth(
            BOARD_MAX + BOARD_PAD * 2
            + self.scroll.verticalScrollBar().sizeHint().width())

        self.rail = Rail(self)
        # Same trade as the feed, along the other axis: a title clipped in
        # the running order can be read by widening it, and somebody who
        # wants the board can take the width back. The fold button is still
        # there for getting it out of the way entirely.
        self.rail_split = QSplitter(Qt.Horizontal)
        self.rail_split.setChildrenCollapsible(False)
        self.rail_split.setHandleWidth(SPLIT_GRIP)
        self.rail_split.addWidget(self.rail)
        self.rail_split.addWidget(self.board_holder)
        self.stats_panel = Stats(self)
        self.rail_split.addWidget(self.stats_panel)
        self.rail_split.setStretchFactor(0, 0)  # the board takes the slack
        self.rail_split.setStretchFactor(1, 1)
        self.rail_split.setStretchFactor(2, 0)  # and so does the far side
        self.rail_split.splitterMoved.connect(self._unfold_by_drag)
        # After the unfold, which changes the pane widths this measures
        # against, and before the two that record them.
        self.rail_split.splitterMoved.connect(
            lambda *_: self._centre_board())
        # After the unfold, or they see a folded panel and record nothing.
        self.rail_split.splitterMoved.connect(self._remember_rail_width)
        self.rail_split.splitterMoved.connect(self._remember_stats_width)
        # Rebuilding thirty rows on every pixel of a drag is a stutter, so the
        # re-clip waits for the handle to settle. render() is cheap for the
        # bands either side of it: their signature has not changed.
        self.rail_redraw = QTimer(self)
        self.rail_redraw.setSingleShot(True)
        self.rail_redraw.setInterval(RAIL_REDRAW_MS)
        self.rail_redraw.timeout.connect(self.render)
        self.rail_split.splitterMoved.connect(lambda *_: self.rail_redraw.start())
        top = self.rail_split
        top.setMinimumHeight(BOARD_MIN_H)

        # The feed used to be a fixed height nobody could argue with.
        # A splitter, so the board and the history can be traded off
        # against each other -- some days the feed is the thing you are
        # reading. Neither half may carry a fixed height or the handle
        # has nothing to move.
        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(SPLIT_GRIP)
        self.split.addWidget(top)
        self.split.addWidget(self._feed_panel())
        self.split.setStretchFactor(0, 1)   # the board takes the slack
        self.split.setStretchFactor(1, 0)
        self.split.splitterMoved.connect(self._remember_feed_height)
        self.split.splitterMoved.connect(self._unfold_feed_by_drag)
        outer.addWidget(self.split, 1)

        self.edge_timer = QTimer(self)
        self.edge_timer.setInterval(EDGE_SCROLL_MS)
        self.edge_timer.timeout.connect(self._edge_scroll)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_MS)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick_freshness)
        self.clock.start(1000)
        # Turns the refresh glyph, and only while a manual refresh is waiting.
        self.spin_angle = 0
        self.spin_timer = QTimer(self)
        self.spin_timer.setInterval(SPIN_MS)
        self.spin_timer.timeout.connect(self._spin)

        QTimer.singleShot(0, self.refresh)
        if not self.name():
            QTimer.singleShot(300, self.open_settings)

    def _filter_row(self):
        """Client, then equipment, then the way back out of both.

        **Client first**, the broader question: *whose* tickets before
        *which kind*. The other way round asks you to pick equipment before
        saying who you are looking at.

        Chips for equipment, a dropdown for the client: four kinds fit across
        a row and 35 client names do not. Chips rather than checkboxes, and
        none-on meaning everything: see `EquipChip`.

        The row filters the board **and the running order**, which is what
        the queue checkboxes above it already do -- the two lists are the
        same board said twice, and one showing five tickets while the other
        shows thirty-four reads as a fault in both.
        """
        row = QWidget()
        row.setStyleSheet(
            f"background:{T.WELL}; border-bottom:1px solid {T.LINE};")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(6)


        # **A dropdown, where equipment gets chips.** Four kinds fit across a
        # row; 35 client names are a list you scan. Beside the chips rather
        # than at the far edge -- the two narrow the same board the same way.
        caption = QLabel("Client")
        caption.setStyleSheet(f"color:{T.MUTED}; font-size:11px;"
                              f" background:transparent;")
        lay.addWidget(caption)
        lay.addSpacing(4)

        self.client_box = Combo()
        self.client_box.setMinimumWidth(CLIENT_BOX_MIN_W)
        self.client_box.setStyleSheet(btn_css(chrome=True))
        self.client_box.setCursor(Qt.PointingHandCursor)
        self.client_box.currentIndexChanged.connect(self._client_picked)
        self._client_sig = None
        lay.addWidget(self.client_box)

        caption = QLabel("Equipment")
        caption.setStyleSheet(f"color:{T.MUTED}; font-size:11px;"
                              f" background:transparent;")
        lay.addWidget(caption)
        lay.addSpacing(4)

        self.chips = {}
        for name, _ in EQUIPMENT_FILTERS:
            chip = EquipChip(name)
            chip.toggled.connect(
                lambda on, k=name: (self.equip.add(k) if on
                                    else self.equip.discard(k), self.render()))
            lay.addWidget(chip)
            self.chips[name] = chip


        # After both, and it clears both. Only when something is on, because
        # a control that does nothing is noise.
        #
        # Last in the row so that turning a chip on moves nothing -- between
        # the chips and the client box it shifted that dropdown out from
        # under the pointer of somebody about to use it. "Stop narrowing"
        # reading over the whole row is also truer than having it speak for
        # the chips alone.
        self.clear_equip = QPushButton("Show all")
        self.clear_equip.setStyleSheet(btn_css())
        self.clear_equip.setCursor(Qt.PointingHandCursor)
        self.clear_equip.clicked.connect(self._clear_filters)
        self.clear_equip.hide()
        lay.addSpacing(10)
        lay.addWidget(self.clear_equip)

        lay.addStretch()
        return row

    def _client_picked(self, _index):
        pick = self.client_box.currentData()
        if pick is None:
            return
        pick = tuple(pick)
        if pick == tuple(self.client_pick):
            return
        self.client_pick = pick
        self.render()

    def _fill_clients(self):
        """Rebuild the list, but only when it would actually read differently.

        `render()` runs on every poll and every drag, and a combo rebuilt
        under somebody's pointer loses the popup they had open -- the same
        reason the figures panel builds its window selector once and leaves
        it outside the body it throws away. The signature is the entries and
        their counts, so a ticket closing changes it and a redraw does not.
        """
        entries = client_counts(self.cards, self.board_roster())
        typos = client_typos(self.cards, self.board_roster())
        sig = json.dumps([entries, typos, list(self.client_pick)],
                         sort_keys=True)
        if sig == getattr(self, "_client_sig", None):
            return
        self._client_sig = sig

        box = self.client_box
        box.blockSignals(True)
        box.clear()
        total = sum(n for _, n in entries)
        box.addItem(f"All clients ({total})", (CLIENT_ALL, ""))

        # Customers and the misspellings of them, in one alphabetical list.
        # Interleaved rather than gathered at the end, because a slip sorts
        # beside the name it is a slip at -- `bravon` lands under Bravo
        # Environmental, where somebody looking for Bravo will meet it.
        merged = ([(k, n, "client") for k, n in entries]
                  + [(raw, n, "raw") for raw, n in typos])
        merged.sort(key=lambda t: (t[0] == CLIENT_NONE, t[0].lower()))
        for name, n, kind in merged:
            box.addItem(client_filter_label(name, n), (kind, name))
            if kind != "raw":
                continue
            # **Red, and still pickable.** Excluding it would hide the one
            # entry that is asking to be dealt with; colouring it says the
            # board knows it is wrong without pretending it is not there.
            i = box.count() - 1
            box.setItemData(i, QBrush(QColor(T.RED_FG)), Qt.ForegroundRole)
            box.setItemData(
                i, f"{name} is a misspelling the roster resolves to "
                   f"{client_key(name, self.board_roster())}.\n\n"
                   f"Pick it to find the {n} ticket"
                   f"{'' if n == 1 else 's'} still carrying it.",
                Qt.ToolTipRole)
        # A client whose last open ticket just closed is gone from the list,
        # and a filter pinned to nobody shows nothing with no way back. Fall
        # to the whole board rather than leaving the board empty.
        at = self._client_index(self.client_pick)
        if at < 0:
            self.client_pick = (CLIENT_ALL, "")
            at = 0
        box.setCurrentIndex(at)
        box.blockSignals(False)

    def _client_index(self, pick) -> int:
        """Where this pick sits in the list, or -1.

        Scanned rather than `findData`, which compares the QVariants Qt made
        of these and does not match a Python tuple back against one.
        """
        want = tuple(pick or ())
        for i in range(self.client_box.count()):
            if tuple(self.client_box.itemData(i) or ()) == want:
                return i
        return -1

    def board_roster(self):
        """The roster, or nothing. A board with no Jira still filters."""
        return getattr(self, "roster", None) or []

    def _clear_filters(self):
        """Back to the whole board, without five separate clicks."""
        for chip in self.chips.values():
            chip.blockSignals(True)
            chip.setChecked(False)
            chip.blockSignals(False)
        self.equip.clear()
        self.client_pick = (CLIENT_ALL, "")
        self.render()

    def _toolbar(self):
        bar = self.bar = QWidget()
        # The floor, like the space around the sections: this is chrome, not
        # a section. Painted in the brightest token it was the first thing the
        # eye landed on in light mode; painted the canvas colour it merged
        # with the columns under it.
        bar.setStyleSheet(f"background:{T.WELL}; border-bottom:1px solid {T.LINE};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(10)

        mark = QPixmap(str(LOGO))
        if not mark.isNull():
            logo = QLabel()
            # Scaled for the display it lands on, so it isn't a blur on a
            # HiDPI screen -- the file is 128px for exactly that reason.
            dpr = self.devicePixelRatioF() or 1.0
            side = int(26 * dpr)
            shown = mark.scaled(side, side, Qt.KeepAspectRatio,
                                Qt.SmoothTransformation)
            shown.setDevicePixelRatio(dpr)
            logo.setPixmap(shown)
            logo.setStyleSheet("background:transparent;")
            lay.addWidget(logo)

        title = QLabel("Bert")
        f = QFont()
        f.setPointSize(14)
        f.setWeight(QFont.DemiBold)
        title.setFont(f)
        title.setStyleSheet(f"color:{T.INK}; background:transparent;")
        lay.addWidget(title)

        # The count is the control: it is already the one thing on the bar
        # that names the cards needing a person, so hanging the jump off
        # anything else would be a second way to say the same thing. Not on a
        # band header, because a card keeps its red edge wherever it is
        # dragged -- two of the three on the board the day this was asked for
        # were sitting in Medium.
        self.count = ClickLabel("")
        self.count.setStyleSheet(f"color:{T.MUTED}; font-size:12px;")
        self.count.clicked.connect(self.jump_attention)
        lay.addWidget(self.count)
        lay.addSpacing(10)

        self.search = QLineEdit()
        self.search.setPlaceholderText(SEARCH_HINTS[0])
        # A range, not a fixed width. The toolbar asks for more than a
        # narrow window has, so something must give: less of the search
        # placeholder showing costs nothing, the filters being sat on top
        # of costs the filters.
        self.search.setMinimumWidth(140)
        self.search.setMaximumWidth(230)
        # The same frame as the client and title boxes. It had no styling at
        # all, so it fell back to Qt's own, which against a dark toolbar is
        # near enough invisible to look like there is no box there.
        self.search.setStyleSheet(field(chrome=True))
        # Qt's placeholder grey is faint for the same reason the work item
        # entry sets this: the ordinary muted text colour is legible.
        pal = self.search.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(T.MUTED))
        self.search.setPalette(pal)
        self.search.setCursor(Qt.IBeamCursor)
        self.search.textChanged.connect(self.render)
        # A stretch factor, so the surplus on a wide window reaches the box
        # rather than going entirely to the spacer before the indicators. Its
        # maximum is what stops it running away with a full-screen board;
        # without this it sat near its minimum at 1200px with 165px spare,
        # and showed a shortened placeholder for no reason.
        lay.addWidget(self.search, 1)

        # So the first filter can never end up against the box before it.
        lay.addSpacing(8)
        self.qboxes = {}
        for q in T.QUEUE:
            cb = QueueBox(q)
            cb.setChecked(True)
            cb.stateChanged.connect(
                lambda s, k=q: (self.filters.__setitem__(k, bool(s)), self.render()))
            lay.addWidget(cb)
            self.qboxes[q] = cb

        lay.addStretch()

        self.fresh = QLabel("")
        self.fresh.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
        lay.addWidget(self.fresh)

        # Second freshness, and a different question. self.fresh says how long
        # since Ernie last read Discord; this says how long since Ernie and the
        # other person's board agreed. On a solo setup it stays hidden.
        self.shared = QLabel("")
        self.shared.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
        self.shared.hide()
        lay.addSpacing(10)
        lay.addWidget(self.shared)

        # Third freshness, and the quietest. The client list is pulled hourly
        # and changes rarely, so this says nothing at all until the pull has
        # clearly stopped -- the failure it exists for is silent otherwise:
        # a token that expired, and a list that goes on looking fine.
        self.roster_age = QLabel("")
        self.roster_age.hide()
        lay.addSpacing(10)
        lay.addWidget(self.roster_age)

        # Before the two buttons rather than between them. It is empty for
        # anybody who has set their name, so all it did there was hold the
        # refresh button and the gear apart for no visible reason.
        self.who = QLabel("")
        lay.addWidget(self.who)

        self.refresh_btn = chrome_button(REFRESH_GLYPH, "Refresh")
        # Through a lambda: clicked passes its checked flag as the first
        # argument, which would arrive as manual=False and undo the point.
        self.refresh_btn.clicked.connect(lambda: self.refresh(manual=True))
        lay.addWidget(self.refresh_btn)

        gear = chrome_button("\u2699", "Settings")
        gear.clicked.connect(self.open_settings)
        lay.addWidget(gear)
        return bar

    def _feed_panel(self):
        self.feed_folded = False
        self._feed_row_h = 0
        self._feed_wants = 0        # what it would choose for itself
        self._feed_sized = False    # whether the handle has been placed
        self._rail_sized = False    # and the same for the rail
        self._stats_sized = False   # and for the figures beside it
        self.stats = None
        self.stats_at = 0.0
        w = QWidget()
        w.setObjectName("feedPanel")
        # Scoped, so the caption and the rows don't each paint their own block
        # of it the way a bare selector would.
        # The canvas, like the board column and the two side panels: the feed
        # is a section with content in it, so it sits on the floor rather than
        # being part of it.
        w.setStyleSheet(f"#feedPanel {{ background:{T.FEED};"
                        f" border-top:1px solid {T.LINE}; }}")
        # No fixed height: the splitter owns it. A minimum only, so the
        # handle cannot be dragged down over the caption.
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 6, 16, 8)
        lay.setSpacing(4)

        # Clickable header, the same gesture the bands use to fold.
        head = ClickableWidget()
        head.setCursor(Qt.PointingHandCursor)
        head.setStyleSheet("background:transparent;")
        head.setToolTip("Hide the activity feed")
        hh = QHBoxLayout(head)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(6)
        # The same button the other two foldable sections carry. The header
        # stays clickable underneath, but a label that merely happens to be
        # clickable does not tell anybody the section folds.
        self.feed_fold_btn = fold_button(GLYPH_DOWN, "Hide the activity feed")
        self.feed_fold_btn.clicked.connect(self.toggle_feed)
        lab = QLabel("Recent activity")
        lab.setStyleSheet(f"color:{T.MUTED}; font-size:11px;"
                          f" background:transparent;")
        hh.addWidget(self.feed_fold_btn)
        hh.addWidget(lab)
        hh.addStretch()
        head.clicked.connect(self.toggle_feed)
        self.feed_head = head
        lay.addWidget(head)

        self.feed_body = QWidget()
        self.feed_body.setStyleSheet("background:transparent;")
        body = QVBoxLayout(self.feed_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # The feed carries the whole history now rather than the last four, so
        # it has to scroll. Without this the panel grows to fit every row and
        # eats the board it sits under.
        self.feed_scroll = QScrollArea()
        self.feed_scroll.setWidgetResizable(True)
        self.feed_scroll.setFrameShape(QFrame.NoFrame)
        self.feed_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Left, so the feed lines up with the board above it rather than
        # floating in the middle of a wide window.
        self.feed_scroll.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.feed_scroll.setStyleSheet("background:transparent;")
        self.feed_scroll.viewport().setStyleSheet("background:transparent;")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        self.feed_lay = QVBoxLayout(inner)
        self.feed_lay.setContentsMargins(0, 0, FEED_GUTTER, 0)
        # No gap between rows: each one draws a hairline at its own bottom
        # edge, and that is the separator. A layout gap on top of it is a
        # second one -- and because it falls above a row's contents but below
        # the rule above them, everything in the row read as sitting low in
        # the band between the two rules. Measured: 40px rule to rule, with
        # the text 14px below the one above and 10px above its own.
        self.feed_lay.setSpacing(0)
        self.feed_scroll.setWidget(inner)
        # On the scroll area, not the rows: every row then fills one viewport
        # so the Undo buttons stay in a column, and the scrollbar comes to the
        # cap with it instead of sitting out at the window edge.
        self.feed_scroll.setMaximumWidth(FEED_ROW_MAX_W)
        # No alignment flag: aligning it makes it take its own sizeHint, which
        # for a scroll area is small -- measured at 432px, cap or no cap. Left
        # to fill, it takes the width it is given up to the cap and sits at the
        # left of it anyway.
        body.addWidget(self.feed_scroll)
        lay.addWidget(self.feed_body)

        self.feed_panel = w
        return w

    def toggle_feed(self):
        """Fold the feed down to its caption, giving the board the height."""
        self.feed_folded = not self.feed_folded
        # Unfolding restores the height rather than whatever the fold left
        # behind, so folding and unfolding is not a way to lose your layout.
        if not self.feed_folded:
            self._feed_sized = False
        self.feed_body.setVisible(not self.feed_folded)
        self._say_feed_fold()
        self._fit_feed()
        # The button's business, so the button re-places the splitter. Folding
        # the widget alone left the pane exactly as tall as it was.
        self._place_feed()

    def _say_feed_fold(self):
        """The glyph and both tooltips, wherever the fold came from."""
        tip = ("Show the activity feed" if self.feed_folded
               else "Hide the activity feed")
        self.feed_fold_btn.setText(GLYPH_UP if self.feed_folded else GLYPH_DOWN)
        self.feed_fold_btn.setToolTip(tip)
        self.feed_head.setToolTip(tip)

    def resizeEvent(self, e):
        """A resized window is a different picture of the same cards.

        Cards cut their client name to the width they are given, so the board
        has to be rebuilt when that width changes -- but not on every pixel of
        a drag, which is thirty cards torn down and rebuilt per frame. It goes
        through the same timer the rail handle uses, which exists to wait for
        a geometry change to settle.
        """
        super().resizeEvent(e)
        redraw = getattr(self, "rail_redraw", None)
        if redraw is not None:      # resize fires while the window is built
            redraw.start()
        # Not through the timer. The board is thirty widgets and waits for the
        # drag to settle; the toolbar is two labels and a placeholder, and
        # leaving those cut for the length of a drag is the thing being fixed.
        self._fit_toolbar()
        # Same reasoning: two widths, and the column visibly sliding off
        # centre for the length of a window drag is what this stops.
        self._centre_board()

    def _unfold_by_drag(self, *_):
        """A spine dragged away from the edge opens that panel.

        Folding used to pin the pane with `setFixedWidth`, on the grounds
        that folding is the button's business -- true of *folding*, and it
        left the handle sitting against the spine doing nothing at all, which
        is not a rule anybody can see. Reported as not being able to drag the
        sides back open, which is exactly what it was.

        The button still folds and unfolds. This only turns a drag that has
        clearly left the spine into the same unfold, and passes `settle=False`
        so the width the drag is choosing survives it.
        """
        sizes = self.rail_split.sizes()
        for i, panel in ((0, self.rail), (2, self.stats_panel)):
            if panel.folded and sizes[i] > RAIL_FOLDED_W + UNFOLD_GRAB:
                panel.set_folded(False, settle=False)

    def save_settings(self) -> None:
        """Write the layout somebody chose, and never make a fuss about it.

        Four places were doing this identically -- the rail width, the
        collapsed bands, the figures width and the feed height -- each with
        its own `try` and its own comment saying the same thing. A layout is
        not worth an error box, so a failure here is swallowed on purpose:
        the worst case is that the window opens the way it did last time.
        """
        try:
            SETTINGS.write_text(json.dumps(self.settings, indent=2))
        except OSError:
            pass

    def _remember_rail_width(self, *_):
        """Kept the way the feed height is, and for the same reason: a
        layout somebody chose should survive the next launch."""
        if self.rail.folded:
            return
        w = self.rail.width()
        if w and w != self.settings.get("rail_width"):
            self.settings["rail_width"] = w
            self.save_settings()

    def remember_collapsed(self, bands):
        """Kept the way the rail width is: a layout somebody chose should
        survive the next launch."""
        self.settings["rail_collapsed"] = sorted(bands)
        self.save_settings()

    def _remember_stats_width(self, *_):
        """Kept the way the rail's width is, and for the same reason."""
        if self.stats_panel.folded:
            return
        w = self.stats_panel.width()
        if w and w != self.settings.get("stats_width"):
            self.settings["stats_width"] = w
            self.save_settings()

    def _centre_board(self):
        """Keep the column in the middle of the **window**, not of its pane.

        The pane is only centred while the side panels match -- fold the
        figures and it starts 400px in. Measured against the splitter, which
        spans the whole row. Staying centred costs width, and the floor is the
        column's own minimum: better off centre than too narrow to read.
        """
        hold = getattr(self, "board_holder", None)
        if hold is None or not hold.width():
            return
        room = hold.width()
        span = self.rail_split.width()
        x = hold.mapTo(self.rail_split, QPoint(0, 0)).x()
        col = min(self.scroll.maximumWidth(), room)

        # The widest a column can be and still sit centred *here*: it has to
        # start at or after where this pane starts, and end at or before
        # where it ends. Lopsided panels make one of those two the binding
        # one, and the column has to give up the difference -- filling the
        # pane is exactly what leaves it off centre, because the pane is not
        # centred.
        fits = min(span - 2 * x, 2 * (x + room) - span)
        floor = self.scroll.widget().minimumSizeHint().width()
        if col > fits >= floor:
            col = fits

        want = (span - col) // 2
        left = max(0, min(room - col, want - x))
        self.board_pad_l.setFixedWidth(left)
        self.board_pad_r.setFixedWidth(max(0, room - col - left))

    def _place_sides(self):
        """Put both handles where they were left, once per unfold.

        One function for the pair, because they share a splitter and
        setSizes() takes every pane at once. Placing one of them with a
        two-element list -- which is what this did before the figures were
        added beside the board -- leaves the third pane to whatever Qt makes
        of a short list, and in practice neither width was applied at all:
        the rail sat at its content width, ignoring the one somebody had
        dragged.

        Once, from whatever was dragged last time or the default, and then
        left alone. Re-applying on every poll would drag the handle back out
        from under whoever was moving it.
        """
        if self._rail_sized and self._stats_sized:
            return
        rail = (RAIL_FOLDED_W if self.rail.folded else
                max(RAIL_MIN_W, min(self.settings.get("rail_width")
                                    or RAIL_WIDTH, RAIL_MAX_W)))
        figures = (RAIL_FOLDED_W if self.stats_panel.folded else
                   max(STATS_MIN_W, min(self.settings.get("stats_width")
                                        or STATS_WIDTH, STATS_MAX_W)))
        total = self.rail_split.width()
        # The board keeps a card's worth of room whatever the sides ask for.
        if total <= rail + figures + CARD_MIN_W:
            return
        self.rail_split.setSizes([rail, total - rail - figures, figures])
        self._rail_sized = True
        self._stats_sized = True
        self._centre_board()

    def _panel_height(self, view):
        """The panel that holds a feed viewport this tall."""
        m = self.feed_panel.layout().contentsMargins()
        return (m.top() + m.bottom() + self.feed_panel.layout().spacing()
                + self.feed_head.sizeHint().height() + view)

    def _folded_height(self):
        """What the caption actually needs, measured rather than counted.

        `FEED_FOLDED` was 30, chosen when the control on this header was a
        caret in an 11px label -- about 14px, which fitted inside the panel's
        6 and 8 of margin with two to spare. The fold button that replaced it
        is 24px and does not, so the panel was pinned eight pixels shorter
        than its own contents and the button was clipped along the top.
        Reported as the collapse button being cut off, at every window size,
        which is what a fixed height does: it is wrong by the same amount
        everywhere.

        "A hand-counted fixed height clips in silence" is the first line of
        `_fit_feed`'s reasoning about the *rows*. The caption is the same
        problem one layout up, and the answer is the same one: ask.
        """
        m = self.feed_panel.layout().contentsMargins()
        return max(FEED_FOLDED,
                   m.top() + m.bottom() + self.feed_head.sizeHint().height())

    def _remember_feed_height(self, *_):
        """Kept, so the handle does not have to be found again every
        time Bert opens. Written on the drag rather than on close,
        because a crash should not cost somebody their layout."""
        if self.feed_folded:
            return
        h = self.feed_panel.height()
        if h and h != self.settings.get("feed_height"):
            self.settings["feed_height"] = h
            self.save_settings()

    def _feed_scale(self):
        """How much more of a line a closed row may show at this width.

        The clip was a fixed number of characters, written for a window that
        might be narrow, so a full-screen board threw away most of the room it
        had and clipped lines with half the row still empty.

        Capped at halfway across the window rather than at the space available:
        a single line run the whole width of a wide screen is further than the
        eye tracks comfortably, and the row is a summary -- the whole of it is
        one click away. Never below 1, so a narrow board is untouched.
        """
        # The feed line is 12px, set on the row; self.fontMetrics() is the
        # window's font and reports a wider character, which cancelled the
        # whole calculation out and left a full-screen board clipping at the
        # narrow width anyway.
        f = QFont(self.font())
        f.setPixelSize(FEED_FONT_PX)
        fm = QFontMetrics(f)
        per = max(fm.horizontalAdvance(FEED_SAMPLE) / len(FEED_SAMPLE), 1.0)
        gaps = 4 * max(self.feed_lay.spacing(), 0)
        # Against the row's own width, which is capped, rather than the
        # viewport -- otherwise on a wide screen the line would be sized for
        # room the row is never given.
        across = min(self.feed_scroll.viewport().width(), FEED_ROW_MAX_W)
        room = (across - FEED_TIME_W - FEED_MORE_W - FEED_STATUS_W
                - FEED_UNDO_W - FEED_GUTTER - gaps)
        room = min(room, self.width() // 2)
        return max(1.0, room / per / FEED_BASE_CHARS)

    def _toggle_feed_row(self, eid):
        """Open or close one row. Kept by event_id, not on the widget, because
        the next poll throws every widget away and builds them again."""
        self.feed_open.symmetric_difference_update({eid})
        self._render_feed()

    def _fit_feed(self):
        """Give the panel the height its rows actually need.

        A hand-counted fixed height clips in silence. The rows have a minimum
        of their own, so a panel a few pixels short squeezes them and then cuts
        the bottom ones off -- entries vanishing one at a time as the feed
        fills, and coming back whenever a shorter row makes the stack fit. The
        row height depends on the font, the display scaling and whether a row
        carries an Undo button, so it is measured, not predicted.
        """
        if self.feed_folded:
            # A range, not a fixed height -- the rule the two side panels
            # already follow. `setFixedHeight` pins the *widget*, and the
            # handle is still sitting right there against the caption, so
            # dragging it did nothing and nothing on screen said why.
            #
            # The height itself is placed by `_place_feed`, from the fold
            # gesture rather than from here: this runs on every poll, and
            # putting the handle back on each one would snap it out from
            # under anybody dragging it open.
            self.feed_panel.setMinimumHeight(self._folded_height())
            self.feed_panel.setMaximumHeight(UNCAPPED)
            return
        # The rows went in a moment ago and the layout has not recomputed yet,
        # so ask it to before believing anything it says about its size.
        self.feed_lay.invalidate()
        self.feed_lay.activate()

        rows = [self.feed_lay.itemAt(i).widget()
                for i in range(self.feed_lay.count())]
        rows = [r for r in rows if r is not None]
        # An opened row is meant to be taller, so it must not set the height
        # everything else is held to -- and _feed_row_h only ever grows, so one
        # click would have left every row four lines deep for the session.
        shut = [r for r in rows if not getattr(r, "expanded", False)]
        tallest = max((r.sizeHint().height() for r in shut), default=0)
        # Held steady, and only ever upward. A row carrying an Undo button is
        # taller than one that has lost it, so a height measured fresh every
        # render drops the moment the last undoable row ages out of its
        # window -- and the whole list slides up while somebody is reading it.
        #
        # Only safe because closed rows never wrap: a wrapped label reports a
        # height at a width of its own, and this number only ever grows.
        # check_feed holds them to one line.
        self._feed_row_h = max(getattr(self, "_feed_row_h", 0), tallest)

        # What a closed row leaves under its one line of text, so an opened
        # one can leave the same. Measured off the rows themselves rather than
        # a font metric: the height they are held to is whatever the tallest
        # closed row wanted, and that is the thing being matched.
        line = max((r.text_label.height() for r in shut
                    if getattr(r, "text_label", None) is not None), default=0)
        slack = max(self._feed_row_h - line, 0)

        # Closed rows are all one height. A row that loses its Undo button is
        # shorter than one that has it, so without this every undo shifts
        # everything below it up by a few pixels while you are still looking
        # at it. An opened row takes whatever its text needs.
        if self._feed_row_h:
            for r in rows:
                if getattr(r, "expanded", False):
                    # sizeHint() under-reports a wrapped label -- it does not
                    # know the width the layout is about to give it, so an
                    # open row measured that way comes out short and clips the
                    # very text it was opened to show. Ask the label what it
                    # needs at the width it actually has, and let the row take
                    # its height from that.
                    lab = getattr(r, "text_label", None)
                    need = 0
                    if lab is not None:
                        need = lab.heightForWidth(max(lab.width(), 1))
                        lab.setMinimumHeight(need)
                    # Never shorter than it was closed, and it keeps the
                    # slack a closed row carries. A row with no Undo button is
                    # smaller than the height they are all held to, so its
                    # natural size pulls everything below it upward; and a
                    # closed row's height comes from the taller Undo column
                    # with the label top-aligned, so there is room under the
                    # words an exactly-sized open row would lose. Opening one
                    # either changes nothing or adds the lines it needs.
                    r.setMinimumHeight(max(self._feed_row_h, need + slack))
                    r.setMaximumHeight(UNCAPPED)
                else:
                    r.setFixedHeight(self._feed_row_h)

        gap = self.feed_lay.spacing()

        def stack(n):
            return n * self._feed_row_h + max(0, n - 1) * gap

        # How tall the panel wants to be if nobody has said otherwise. The
        # height itself belongs to the splitter now -- setting it here would
        # take it back off whoever dragged the handle, on the next poll.
        shown = min(max(len(rows), FEED_ROWS), FEED_MAX_ROWS)
        self._feed_wants = self._panel_height(stack(shown)) if self._feed_row_h else 0
        self.feed_panel.setMinimumHeight(self._panel_height(stack(1)))
        self.feed_panel.setMaximumHeight(UNCAPPED)

        # Placed once, from whatever was dragged last time or the default, and
        # then left alone -- re-applying it on every poll would drag the handle
        # back under the person moving it.
        self._place_sides()
        if not self._feed_sized and self._feed_wants:
            self._place_feed()

    def _place_feed(self):
        """Put the handle where the fold says it belongs.

        **Narrowing the widget does not narrow the pane it sits in**, and the
        feed was the last panel still learning that. `setFixedHeight` took the
        panel down to its caption and the splitter went on holding the pane at
        whatever it last allotted, so folding left the heading, then three
        hundred pixels of empty floor, then a handle stranded above it -- and
        the board got none of the room the fold was for. Reported as clicking
        the header collapsing "the content in it" rather than the section,
        which is exactly what it was doing.

        The same fix the figures panel already carries, in the same words: the
        fold has to re-place the splitter, both ways.
        """
        total = self.split.height()
        if not total:
            return                      # asked before the window has a size
        if self.feed_folded:
            spine = self._folded_height()
            self.split.setSizes([total - spine, spine])
            return
        want = self.settings.get("feed_height") or self._feed_wants
        if want and total > want + BOARD_MIN_H:
            self.split.setSizes([total - want, want])
            self._feed_sized = True

    def _unfold_feed_by_drag(self, *_):
        """A caption dragged away from the bottom opens the feed.

        The counterpart of `_unfold_by_drag` for the other axis, and it
        exists for the same reason: the button folds, but a handle sitting
        against a folded panel has to do something, or the rule is one nobody
        can see. It does not re-place the pane -- the drag is already the
        height somebody is choosing.
        """
        if not self.feed_folded:
            return
        sizes = self.split.sizes()
        if len(sizes) > 1 and sizes[1] > self._folded_height() + UNFOLD_GRAB:
            self.feed_folded = False
            self.feed_body.setVisible(True)
            self._say_feed_fold()
            self._fit_feed()

    # -- identity ----------------------------------------------------------

    def name(self):
        s = self.settings
        if s.get("name"):
            return s["name"].strip()
        # A settings file written before the field became one box.
        return f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()

    def writable(self):
        # A blocked build is read-only for the same reason a lost connection
        # is: what it would write cannot be trusted to land properly.
        return (bool(self.name()) and self.connected
                and self.update_state != "blocked")

    def open_settings(self, pending=None):
        """Settings, with the theme previewing as it is picked.

        A theme is the one setting nobody can judge from its name. Previewing
        rebuilds the whole window, because that is the only restyle there is
        -- there is no single sheet to swap, and no way to be sure a live one
        missed none of the seventy-six.

        Nothing is stored until OK, so Cancel has something true to go back
        to; `pending` carries what was typed across the rebuild.
        """
        seed = {**self.settings, **(pending or {})}
        dlg = SettingsDialog(self, seed, self.health)
        if pending:
            # Where the pointer was. Reopening on a different widget would
            # give the whole thing away.
            dlg.theme.setFocus()
        code = dlg.exec()
        vals = dlg.values()

        if code == SettingsDialog.PREVIEW:
            fresh = self.rebuild_in_new_theme(vals["theme"])
            # Through a timer, so the rebuild's own layout events have run
            # before a modal loop starts on top of them.
            QTimer.singleShot(0, lambda: fresh.open_settings(vals))
            return

        if code == QDialog.Accepted:
            self.settings.update(vals)
            # Don't leave the old pair behind to be read back later.
            self.settings.pop("first_name", None)
            self.settings.pop("last_name", None)
            self.save_settings()
            # An editor open right now was built in the old mode, and the
            # render below spares the bands it lives in.
            if self.editing_card:
                w = self._card_widget(self.editing_card)
                if w is not None and hasattr(w, "set_entry_mode"):
                    w.set_entry_mode(vals.get("entry") or ENTRY_DEFAULT)
            # Only if the board is not already showing it -- after a preview
            # it is, and rebuilding again would be a second flicker for
            # nothing.
            if resolve_theme(vals["theme"]) != T.name:
                self.rebuild_in_new_theme()
                return
            self.render()
            return

        # Cancelled. A preview may have left the board in a theme that was
        # never stored, so put it back to whatever is.
        if resolve_theme(self.settings.get("theme", THEME_DEFAULT)) != T.name:
            self.rebuild_in_new_theme()

    def desktop_theme_changed(self, *_):
        """The desktop flipped. Only this board's business if it was following.

        An explicit light or dark is a decision, and the desktop does not get
        to overrule it -- that is the whole difference between the two.
        """
        if self.settings.get("theme", THEME_DEFAULT) != "system":
            return
        if T.name == resolve_theme("system"):
            return                  # already showing what the desktop asks for
        self.rebuild_in_new_theme()

    def rebuild_in_new_theme(self, choice=None):
        """Build the window again in the other palette. Answers the new one.

        `choice` is for previewing: it restyles to a theme that has not been
        stored anywhere, so the fresh window still loads whatever is on disk
        and Cancel has something true to go back to.

        Every stylesheet here is written where its widget is made, which is
        what keeps each one next to the thing it explains -- and the price is
        that there is no one sheet to swap. Restyling in place would mean
        finding all seventy-six of them again and being sure none was missed,
        and a single miss is a white panel in a dark board. Building the
        window once more cannot miss any. It costs the scroll position and one
        poll, on a setting nobody changes twice in a day.
        """
        apply_theme(choice or self.settings.get("theme", THEME_DEFAULT))
        fresh = Bert(self.api.base)
        _OPEN.append(fresh)
        fresh.search.setText(self.search.text())     # a typed search survives
        fresh.setGeometry(self.geometry())
        fresh.showMaximized() if self.isMaximized() else fresh.show()

        # Shown before the old one closes, so the last-window-closed quit
        # never fires; and the timers stopped by hand, because a closed window
        # is not a deleted one and its poll would go on running behind this.
        self._swapping_theme = True
        # **Deferred work has to be told, because it cannot be stopped.** The
        # timers below are stoppable; the `QTimer.singleShot`s that `render()`
        # and `_render_feed()` post to put the scrollbars back are not, and
        # they fire on the next turn of the loop -- by which time this window
        # is closed and its layouts and scrollbars are deleted C++ objects.
        # Two tracebacks per rebuild, harmless and printed every time.
        # Rebuilding used to be a thing nobody did twice in a day; previewing
        # a theme does it on every pick, so it is worth saying so.
        self._gone = True
        for t in (self.timer, self.clock, self.spin_timer, self.edge_timer):
            t.stop()
        self.close()
        if self in _OPEN:
            _OPEN.remove(self)
        self.deleteLater()
        return fresh

    # -- polling -----------------------------------------------------------

    def refresh(self, manual=False):
        if self.dragging:
            return                      # never yank the board out from under a drag
        if manual:
            if self.awaiting:
                return              # already waiting on the next read
            # The button cannot make a sync happen -- ernie_sync runs its own
            # cycle in its own process, and this only re-asks Ernie, which Bert
            # does every POLL_MS anyway. So the press waits for the next read
            # of Discord to land, and says so until it does. Flashing
            # something and putting the same number back read as a dead button.
            self.awaiting = True
            self.await_run = self.health.get("synced_at")
            self.await_since = time.time()
            self._show_busy(True)
            self._tick_freshness()
        # One poll at a time. A manual press during an automatic one still gets
        # its answer: that poll is already on its way back.
        if self.poller is not None and self.poller.isRunning():
            return
        self.poller = Poller(
            self.api, want_roster=time.time() - self.roster_at > ROSTER_MAX_AGE_S,
            want_stats=time.time() - self.stats_at > STATS_MAX_AGE_S,
            stats_days=self.stats_panel.days())
        self.poller.loaded.connect(self.on_loaded)
        self.poller.failed.connect(self.on_failed)
        self.poller.start()

    def _check_build(self):
        """Where this build stands against the one that just answered.

        Off the poll rather than at startup, because at startup there is
        nothing to compare against yet -- `/health` is the only thing that
        knows what Ernie is, and Bert has not asked it.
        """
        build = dict((self.health or {}).get("build") or {})
        # Staged from the command line, for looking at the thing that only
        # appears when two builds disagree -- which is otherwise unreachable
        # on a machine where both halves come out of the same checkout. It
        # feeds the real decision rather than faking the dialog: what is
        # under test is `build_standing`, not the picture.
        if PRETEND_ERNIE:
            build["version"] = PRETEND_ERNIE
        if PRETEND_NEWEST:
            build["newest"] = PRETEND_NEWEST
        if PRETEND_REQUIRED:
            build["required"] = PRETEND_REQUIRED
        build.setdefault("version", None)
        was = self.update_state
        self.update_state, self.update_said = build_standing(
            PRETEND_MINE or ernie_version.VERSION,
            build.get("version"), build.get("min_bert"),
            build.get("newest") or "", build.get("required") or "")

        if self.update_state != was and was == "blocked":
            self.banner.hide()          # updated underneath us, or moved on
        if self.update_state == "blocked":
            self.banner.setText(self.update_said)
            self.banner.setStyleSheet(
                f"background:{T.RED_BG}; color:{T.RED_FG}; padding:7px;"
                f" font-size:12px;")
            self.banner.show()
        if self.update_state != was:
            self.render()               # the buttons follow writable()

        # Once a session. A dialog on every poll would be its own outage.
        if self.update_state == "ok" or self._update_told:
            return
        # And once a *build*, if somebody asked for that. Keyed on the
        # version of Ernie they were told about rather than on their own:
        # "stop mentioning 0.9.9" should stop mentioning 0.9.9, and should
        # say something again when 0.9.10 turns up. There is no muting a
        # blocked board -- that is the floor talking, not this.
        theirs = build.get("version")
        if (self.update_state == "behind"
                and theirs and self.settings.get("update_muted") == theirs):
            self._update_told = True
            return

        # Set before the dialog, or a poll landing while it is open opens a
        # second one behind it.
        self._update_told = True
        # Scheme-checked here as well as in Ernie. Bert is what actually
        # hands this to the desktop, and a value nobody validated on the way
        # in is a value somebody trusts on the way out.
        url = build.get("update_url") or ""
        if urllib.parse.urlparse(url).scheme.lower() not in ("http", "https"):
            url = ""
        dlg = UpdateDialog(self, self.update_state, self.update_said, url)
        dlg.exec()
        if dlg.muted() and theirs:
            self.settings["update_muted"] = theirs
            self.save_settings()

    def _check_awaited(self):
        """Stop waiting once Discord has actually been read again.

        A newly finished run, not merely a new answer from Ernie: the age only
        moves when ernie_sync completes a cycle, so clearing on anything else
        would put the same number back and read as the press being ignored.
        """
        if not self.awaiting:
            return
        synced = self.health.get("synced_at")
        if ((synced and synced != self.await_run)
                or time.time() - self.await_since > AWAIT_GIVEUP_S):
            self._stop_awaiting()

    def _stop_awaiting(self):
        self.awaiting = False
        self._show_busy(False)

    def _show_busy(self, busy):
        """The button, while it waits on a read of Discord.

        Held down for the darker fill Qt already draws for a press -- its own
        rendering, so it matches whatever the desktop does -- and the glyph
        swapped for a turning one. It stays enabled: a second press is ignored
        above rather than by greying out the only thing saying anything.
        """
        self.refresh_btn.setDown(busy)
        if busy:
            self.refresh_btn.setText("")
            self.spin_timer.start()
        else:
            self.spin_timer.stop()
            self.refresh_btn.setIcon(QIcon())
            self.refresh_btn.setText(REFRESH_GLYPH)

    def _spin(self):
        self.spin_angle = (self.spin_angle + SPIN_STEP) % 360
        self.refresh_btn.setIcon(spin_icon(self.spin_angle))

    def on_loaded(self, p):
        self.fail_since = None
        self.connected = True
        self.last_sync = time.time()
        self.banner.hide()
        incoming = p["board"]["cards"]
        self.feed = p["events"]["events"]
        # Held with the moment it arrived, so the age can go on counting up
        # between polls instead of freezing at whatever the last poll said.
        self.health = p.get("health") or {}
        self.sharing = self.health.get("sharing")
        self.health_at = time.time()
        self._check_build()
        if "stats" in p:
            # An empty answer still counts as one: without stamping it, a
            # stack with an older Ernie would ask again on every poll.
            self.stats = p["stats"]
            self.stats_at = time.time()
            self.stats_panel.set_stats(self.stats)
        if "roster" in p:
            # An empty answer still counts as an answer: without stamping it,
            # a stack with no Jira asks again on every single poll.
            self.roster = p["roster"]
            self.roster_at = time.time()
        self._tick_freshness()          # both labels come off this payload
        # Before the editor and drag guards below, not after: those return
        # early, and a board holding a parked payload is still a board showing
        # invented figures.
        self._tick_invented()
        self._tick_wal()

        # The card outlives the click by a poll or two; drop the toast the
        # moment it is genuinely off the board rather than on a timer.
        if self.completing and not self.completing.intersection(
                c["thread_id"] for c in incoming):
            self._clear_toast()

        # An open editor must not be redrawn out from under someone mid-sentence,
        # but they should still hear that the card moved. Warn, don't redraw --
        # and hold the payload rather than drop it. Dropping it froze the whole
        # board for as long as somebody was typing, so a ticket raised in
        # Discord meanwhile did not arrive until whichever poll happened to
        # follow the editor closing.
        if self.editing_card:
            self._flag_edited_underneath(incoming)
            self._pending = p
            return

        # A poll that set off before the drag began still lands in the middle
        # of one. Rendering here rebuilds the bands, which deletes the very
        # Card widget Qt is dragging -- and Qt takes the process down with it,
        # because the drag it is running holds that widget as its source. Hold
        # the payload until the drag lets go.
        if self.dragging:
            self._pending = p
            return

        # Nothing is holding the board, so whatever was held is older than
        # what just arrived. Clearing it stops a later release replaying a
        # stale board over this one.
        self._pending = None
        self.cards = incoming
        self.render()

    def _tick_invented(self):
        """Say so when the figures include a past nobody lived through."""
        made = ((self.health or {}).get("invented") or {}).get("cards") or 0
        if not made:
            self.demo.hide()
            return
        self.demo.setText(
            f"Demo data — {made} invented tickets are counted in the "
            f"figures. Nothing here is real history.")
        self.demo.setToolTip(
            "Added by tools/fake_stats_data.py, so the figures panel could "
            "be looked at on a board with no past."
            "\n\nRemove them with:\n"
            "    python tools/fake_stats_data.py --db <your.db> --clear")
        self.demo.setStyleSheet(
            f"background:{T.AMBER_BG}; color:{T.AMBER_FG}; padding:7px;"
            f" font-size:12px;")
        self.demo.show()

    def _tick_wal(self):
        """Say when the write-ahead log has stopped checkpointing.

        A reader holding a snapshot stops the WAL being checkpointed, and the
        symptom nobody sees is writers timing out: a Complete that never
        reaches its thread, a change log posting the same line repeatedly.

        Bigger than its own database is the line, because a WAL that
        checkpoints never gets near it. Amber there, red past twice the size
        -- the difference between watching it and restarting the stack.
        """
        w = (self.health or {}).get("wal") or {}
        standing = wal_standing(w)
        if not standing:
            self.wal.hide()
            return
        wal, size = w.get("wal_bytes") or 0, w.get("db_bytes") or 0
        mb = wal / 1048576
        bad = standing == "act"
        self.wal.setText(
            f"The database's write-ahead log has grown to {mb:.1f} MB, "
            f"larger than the database. Changes may stop reaching Discord.")
        self.wal.setToolTip(
            "A WAL that cannot be checkpointed keeps growing, and writers "
            "start timing out -- so a ticket can look closed here and never "
            "reach its thread." + chr(10) + chr(10) +
            "Something is holding a read connection open. Restarting the "
            "stack clears it." + chr(10) + chr(10) +
            f"WAL {mb:.1f} MB against a database of "
            f"{size / 1048576:.1f} MB.")
        self.wal.setStyleSheet(
            f"background:{T.RED_BG if bad else T.AMBER_BG};"
            f" color:{T.RED_FG if bad else T.AMBER_FG}; padding:7px;"
            f" font-size:12px;")
        self.wal.show()

    def _card_widget(self, tid):
        for band in self.bands.values():
            for i in range(band.lay.count()):
                w = band.lay.itemAt(i).widget()
                if isinstance(w, Card) and w.thread_id == tid:
                    return w
        return None

    def _flag_edited_underneath(self, incoming):
        # A ticket being started has no thread yet, so it is not on the board
        # and the poll can only ever answer that it is missing. Warning that
        # it "has left the board" is both wrong and alarming -- it arrived
        # within a poll of pressing + New Ticket, about a blank form.
        if self.editing_card == NEW_TICKET:
            return
        w = self._card_widget(self.editing_card)
        base = getattr(w, "_edit_base", None) if w else None
        if not base:
            return
        fresh = next((c for c in incoming
                      if c["thread_id"] == self.editing_card), None)
        if fresh is None:
            w.warn_changed("This ticket has left the board -- it may have been "
                           "closed or archived. Saving will probably fail.")
            return
        if fresh.get("completed_at"):
            who = fresh.get("completed_by") or "Someone"
            w.warn_changed(f"{who} has closed this ticket while you were "
                           f"editing. Your changes can't be saved until it's "
                           f"reopened.")
            return
        def held(f):
            # the thread title arrives on the card payload as "name"
            return (fresh.get("name") if f == "title" else fresh.get(f)) or ""

        moved = [f for f, was in base.items() if held(f) != was]
        if moved:
            what = ", ".join(FIELD_LABELS.get(f, f) for f in moved)
            w.warn_changed(f"Someone changed {what} while you were editing. "
                           f"Saving will ask you before overwriting.")

    def on_failed(self, err):
        # No way to hear a sync land while Ernie is unreachable, and the
        # banner is already saying what is wrong.
        self._stop_awaiting()
        t = time.time()
        if self.fail_since is None:
            self.fail_since = t
        down = t - self.fail_since
        if down < DEGRADED_S:
            return
        self.banner.show()
        if down < BLOCKED_S:
            self.banner.setText("Reconnecting to Ernie\u2026")
            self.banner.setStyleSheet(
                f"background:{T.AMBER_BG}; color:{T.AMBER_FG}; padding:7px; font-size:12px;")
        else:
            self.connected = False
            self.banner.setText("Can't reach Ernie. Showing the last known board "
                                "\u2014 changes are paused until it's back.")
            self.banner.setStyleSheet(
                f"background:{T.RED_BG}; color:{T.RED_FG}; padding:7px; font-size:12px;")
            self.render()

    def notify(self, text):
        """Say that a click landed, for work that outlives the click."""
        self.toast.setText(text)
        self.toast.show()
        # The write below runs on the GUI thread, so without an explicit repaint
        # the strip only appears once the work it announces has finished.
        self.toast.repaint()
        self.toast_timer.start(TOAST_MS)

    def _clear_toast(self):
        self.toast_timer.stop()
        self.completing.clear()
        self.toast.hide()

    @staticmethod
    def _owed(health):
        """What closing on top of the stack would strand, as (thread, board).

        Two different debts. `queued` is events waiting out their undo window
        before Ernie posts them to the customer thread. `waiting_to_send` is
        cards that have moved since the shared board was last published --
        and a reorder, or any band move that is not in or out of critical,
        only ever appears in the second: they are silent by design and carry
        no dispatch_after at all. Counting the first alone meant reordering
        the board and closing straight away asked nothing, and the running
        order never left the machine.
        """
        q = health.get("queued") or {}
        share = health.get("sharing") or {}
        return (q.get("count") or 0), (share.get("waiting_to_send") or 0)

    def closeEvent(self, ev):
        """
        Closing Bert doesn't lose a change -- the outbox is a separate process
        and posts it whether Bert is open or not. The mistake this is here to
        catch is closing Bert and then shutting the whole stack down on top of
        something that hasn't gone out yet.
        """
        # A theme swap closes this window and opens another one. Nothing is
        # being shut down, so there is nothing to warn about.
        if self._swapping_theme:
            return super().closeEvent(ev)

        # A third debt, and a different kind from the other two. Those reach
        # Discord whether Bert is open or not, which is why their warning is
        # about shutting the *stack* down -- but an editor nobody has saved is
        # gone the moment this window shuts, and it is the only one of the
        # three that is lost with the stack already down. So it is asked
        # first, and before `connected` is looked at.
        if not self._editor_may_close():
            return ev.ignore()

        n, unshared = self._owed(self.health)
        if not self.connected or not (n or unshared):
            return super().closeEvent(ev)

        q = self.health.get("queued") or {}
        due = ""
        if q.get("due_at"):
            try:
                secs = (datetime.fromisoformat(q["due_at"])
                        - datetime.now(timezone.utc)).total_seconds()
                # Not under a supervisor: there the next sentence says the
                # close is what sends them, and a countdown beside it reads as
                # a deadline to beat rather than as something about to be
                # skipped.
                if secs > 0 and not SUPERVISED:
                    due = f" The first goes out in about {int(secs)}s."
            except (ValueError, TypeError):
                pass

        lines = []
        if n:
            thing = "change hasn't" if n == 1 else f"{n} changes haven't"
            lines.append(
                f"{thing.capitalize()} been posted to the thread yet.{due}")
        if unshared:
            card = "card has" if unshared == 1 else "cards have"
            lines.append(f"{unshared} {card} moved since the shared board was"
                         f" last published.")
        if SUPERVISED:
            # One process, so closing this window closes the sync and the
            # outbox with it -- and `shut_down` brings everything owed forward
            # rather than leaving it behind. Nothing is lost, so the warning is
            # not about loss: it is that the undo window is **spent**. A change
            # made ten seconds ago goes to the customer thread as it stands,
            # and the chance to take it back goes with it.
            ask = QMessageBox.question(
                self, "Closing posts these now",
                "\n".join(lines) + f"\n\n"
                f"Closing sends them straight away instead of waiting out the "
                f"undo window — so anything you might still want to take "
                f"back goes out as it is. It takes a moment.\n\n"
                f"Close Ernie?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ask == QMessageBox.Yes:
                return super().closeEvent(ev)
            return ev.ignore()

        ask = QMessageBox.question(
            self, "Not everything has reached Discord",
            "\n".join(lines) + f"\n\n"
            f"Closing Bert is fine on its own — Ernie posts them whether Bert "
            f"is open or not. But if you're shutting everything down, leave "
            f"the rest running another minute or they won't go out at all.\n\n"
            f"Close Bert?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ask == QMessageBox.Yes:
            return super().closeEvent(ev)
        ev.ignore()

    def _editor_may_close(self):
        """Ask about an unsaved editor. False means stay open.

        The same three-way the second click on Edit offers, and for the same
        reason: refusing to close would leave somebody to find the editor
        themselves, and closing without asking is what this exists to stop.
        Keeping it open is the default, because it is the one that loses
        nothing.
        """
        editor = (self._card_widget(self.editing_card)
                  if self.editing_card else None)
        if editor is None or not getattr(editor, "editing", False):
            return True
        if not editor.is_dirty():
            return True

        starting = self.editing_card == NEW_TICKET
        held = (editor.f_title.text().strip() or "an untitled ticket")             if starting else (editor.data.get("name") or self.editing_card)

        box = QMessageBox(self)
        box.setWindowTitle("A ticket you haven't created yet" if starting
                           else "Unsaved changes")
        box.setText(held)
        box.setInformativeText(
            "Closing Bert loses it, and nothing has been created yet."
            if starting else
            "Closing Bert loses the changes -- they have not been saved.")
        save = box.addButton("Create it and close" if starting
                             else "Save and close", QMessageBox.AcceptRole)
        box.addButton("Close and lose it", QMessageBox.DestructiveRole)
        stay = box.addButton("Keep writing" if starting else "Keep editing",
                             QMessageBox.RejectRole)
        box.setDefaultButton(stay)
        box.exec()

        if box.clickedButton() is stay:
            return False
        if box.clickedButton() is save:
            # save() puts the card back in view mode *before* the write, so
            # the editor being shut says nothing about whether it landed --
            # it has to answer for itself. A failed save with the window
            # already gone takes the error box with it.
            return bool(editor.save())
        return True

    def _tick_roster(self):
        """Whether the customer list is still being pulled.

        Hidden unless it has clearly stopped. The list changes rarely, so its
        age is not news -- what is news is a pull that has been failing, which
        otherwise shows up nowhere: ernie_sync catches it, writes a line to
        the log and carries on, and the dropdown goes on offering whatever it
        last knew.
        """
        r = (self.health or {}).get("clients")
        if not r:
            self.roster_age.hide()       # no Jira here, nothing to report
            return
        since = r.get("seconds_since_sync")
        if since is not None:
            since += int(time.time() - self.health_at)
        if since is None or since <= ROSTER_STALE_S:
            self.roster_age.hide()
            return
        self.roster_age.setText(f"client list · {ago(since)} old")
        self.roster_age.setStyleSheet(f"color:{T.AMBER_FG}; font-size:11px;")
        self.roster_age.setToolTip(
            "Ernie hasn't pulled the customer list from Jira in a while, so "
            "the Client dropdown may be missing recent changes. It is pulled "
            "hourly -- if this stays, check the JIRA_ keys in the env file "
            "and logs/sync.log.")
        self.roster_age.show()

    def _tick_sharing(self):
        """
        How the shared board is doing, which is a different question from how
        this Bert is doing. Hidden entirely unless a board is actually shared,
        so nothing changes for one person on one machine.
        """
        # Asked before the "is anything shared" guard, because a machine that
        # could read none of the channel has applied none of it and so has no
        # state_sync rows to be sharing by. That machine is precisely the one
        # that needs telling, and the guard below would have hidden the only
        # thing on screen that explains why its board is empty.
        skew = self.health.get("format_skew")
        if skew:
            n = skew.get("cards") or 0
            self._say_shared(
                "shared board · can't read the other board", T.RED_FG,
                f"{a_few(n, 'card', 'cards')} in #ernie-state "
                f"{'is' if n == 1 else 'are'} written in format "
                f"v{skew.get('their_v')} and this machine speaks "
                f"v{skew.get('our_v')}, so "
                f"{'it is' if n == 1 else 'they are'} being skipped -- and "
                f"nothing done here is reaching the other board either. "
                f"Waiting will not fix it: "
                f"{who_is_behind(skew.get('their_v'), skew.get('our_v'))}.")
            return

        s = self.sharing
        if not s:
            self.shared.hide()
            return

        waiting = s.get("waiting_to_send") or 0
        agreed = s.get("seconds_since_agreed")
        if agreed is not None:
            agreed += int(time.time() - self.health_at)

        if agreed is None:
            # Publishing proves nothing about the other direction, and the
            # publish is what created these rows. Until a pull has actually
            # read the channel there is no contact to report, and saying the
            # boards match here is how this indicator used to lie.
            text, colour = "shared board · no contact yet", T.AMBER_FG
            tip = ("This board has published to the shared copy in "
                   "#ernie-state but hasn't read it back yet, so changes made "
                   "on the other machine aren't showing. It clears on the "
                   "next sync cycle -- if it doesn't, the sync loop isn't "
                   "running.")
        elif agreed > SHARED_STALE_S:
            # Long enough that the sync loop is probably not running -- the
            # board on screen may be missing whatever they have done since.
            text = f"shared board · no contact for {ago(agreed)}"
            colour = T.AMBER_FG
            tip = ("Ernie hasn't compared this board against the shared copy "
                   "in #ernie-state recently, so anything done on the other "
                   "machine won't be showing. Check their stack is running.")
        elif waiting:
            text = f"shared board · {waiting} to send"
            colour = T.AMBER_FG
            tip = (f"{a_few(waiting, 'change', 'changes')} made here that "
                   f"the shared copy in #ernie-state hasn't been told about "
                   f"yet. {'It goes' if waiting == 1 else 'They go'} out on "
                   f"the next cycle.")
        else:
            text, colour = "shared board · up to date", T.MUTED
            tip = ("This board matches the shared copy in #ernie-state, which "
                   "is what a second machine reads and writes -- so anyone "
                   "else running Bert is seeing what you see. Last compared "
                   f"{ago(agreed)} ago.")

        self._say_shared(text, colour, tip)

    def _tick_freshness(self):
        """How old the board is.

        This counted from the last time Bert asked Ernie, which Bert does
        every POLL_MS -- so it read "updated just now" permanently, and went
        on saying it with the sync loop dead and the mirror hours behind.
        Ernie's last finished read of Discord is the age worth showing: it is
        the one that moves, and the one that can be bad news.
        """
        self._check_awaited()
        self._tick_sharing()
        self._tick_roster()
        if self.last_sync is None:
            self._say_fresh("never updated", False,
                            "Bert hasn't reached Ernie yet.")
            return

        since = self.health.get("seconds_since_sync")
        if since is not None:
            since += int(time.time() - self.health_at)

        if self.awaiting:
            self._say_fresh("refreshing\u2026", False,
                            "Waiting for Ernie's next read of Discord. "
                            + (f"The board is {ago(since)} old."
                               if since is not None
                               else "Nothing has been read from Discord yet."))
            return

        if since is None:
            self._say_fresh("never synced", True,
                            "Ernie has no finished sync run, so nothing has "
                            "been read from Discord yet. Check the sync loop "
                            "is running.")
            return

        if since > MIRROR_STALE_S:
            self._say_fresh(f"synced {ago(since)} ago", True,
                            "Ernie hasn't read Discord in a while, so new "
                            "tickets and edits there aren't showing. Check "
                            "the sync loop is running.")
        else:
            self._say_fresh("synced just now" if since < 5
                            else f"synced {ago(since)} ago", False,
                            f"Ernie last read Discord {ago(since)} ago.")

    def _say_fresh(self, text, amber, tip):
        self._fresh_full = text
        self.fresh.setStyleSheet(
            f"color:{T.AMBER_FG if amber else T.MUTED}; font-size:11px;")
        self.fresh.setToolTip(tip)
        self._fit_toolbar()

    def _say_shared(self, text, colour, tip):
        self._shared_full = text
        self.shared.setStyleSheet(f"color:{colour}; font-size:11px;")
        self.shared.setToolTip(tip)
        self.shared.show()
        self._fit_toolbar()

    def _fit_toolbar(self):
        """Give up words before the bar starts cutting them.

        At the window's own minimum width the bar asks for about 45px more
        than it has, and Qt spends that by squeezing whatever can be squeezed:
        the search placeholder came out `Search client, equip` and the shared
        indicator `shared board · up to da`, which is a status cut off
        exactly where it starts saying something. Nothing here is elided for
        the same reason -- an ellipsis on the end of that line loses the same
        half.

        So the two labels step down through `status_forms` together, and the
        placeholder picks the longest of `SEARCH_HINTS` that fits the box it
        is actually in. The test is the layout's own `totalMinimumSize`: what
        it says it cannot go below, against what it has been given.
        """
        bar = getattr(self, "bar", None)
        search = getattr(self, "search", None)
        # resizeEvent fires while the window is still being built, before
        # either of these exists.
        if bar is None or search is None:
            return

        # The labels first, and the placeholder against what is left. In the
        # other order the box is measured before the shortening has given it
        # its room back, so it is told it has 140px, picks a short hint, and
        # is then handed 175 -- which came out as a *narrower* window showing
        # a *longer* placeholder, measured at 1002px against 940px.
        lay = bar.layout()
        fresh = status_forms(getattr(self, "_fresh_full", "") or "")
        shared = status_forms(getattr(self, "_shared_full", "") or "")
        # The attention count is a third label with words to give up, so it
        # steps down with the other two rather than being assumed to fit: the
        # bar already asks for about 45px more than it has at the window's
        # minimum width, and this one is on every board that has a red card.
        seen = status_forms(getattr(self, "_attention_full", "") or "")
        for step in range(max(len(fresh), len(shared), len(seen))):
            self.fresh.setText(fresh[min(step, len(fresh) - 1)])
            self.shared.setText(shared[min(step, len(shared) - 1)])
            self.count.setText(seen[min(step, len(seen) - 1)])
            lay.activate()
            if lay.totalMinimumSize().width() <= bar.width():
                break

        fm = QFontMetrics(search.font())
        room = search.width() - SEARCH_HINT_PAD
        for hint in SEARCH_HINTS:
            if fm.horizontalAdvance(hint) <= room or hint is SEARCH_HINTS[-1]:
                search.setPlaceholderText(hint)
                break

    # -- writes ------------------------------------------------------------

    def _guard(self):
        if not self.name():
            QMessageBox.information(self, "Set your name",
                                    "Add your name in Settings before making "
                                    "changes.")
            self.open_settings()
            return False
        return self.connected

    def move_card(self, tid, priority, after, before):
        if not self._guard():
            return
        # Show the move now.
        self._reorder_local(tid, priority, after, before)
        # This runs from dropEvent, which is inside drag.exec().
        if not self.dragging:
            self.render()
        try:
            self.api.move(tid, priority, after, before, self.name())
        except Exception as e:
            QMessageBox.warning(self, "Couldn't move that card", str(e))
        self.refresh()

    def _reorder_local(self, tid, priority, after, before):
        """Mirror the server's fractional rank so the board can redraw at once."""
        by_id = {c["thread_id"]: c for c in self.cards}
        card = by_id.get(tid)
        if card is None:
            return

        # Same rule the server uses: the neighbours we sent are only the ones
        # this person could see, so resolve the gap against the whole band or a
        # filtered drop lands on top of something hidden.
        band = sorted((c for c in self.cards
                       if c["priority"] == priority and c["thread_id"] != tid),
                      key=lambda c: c["rank"])
        ids = [c["thread_id"] for c in band]
        ranks = [c["rank"] for c in band]

        lo = hi = None
        if after in ids:
            i = ids.index(after)
            lo = ranks[i]
            hi = ranks[i + 1] if i + 1 < len(ranks) else None
        elif before in ids:
            j = ids.index(before)
            hi = ranks[j]
            lo = ranks[j - 1] if j > 0 else None

        if lo is not None and hi is not None:
            card["rank"] = (lo + hi) / 2
        elif lo is not None:
            card["rank"] = lo + RANK_STEP
        elif hi is not None:
            card["rank"] = hi - RANK_STEP
        else:
            card["rank"] = (ranks[-1] + RANK_STEP) if ranks else RANK_STEP
        card["priority"] = priority
        self.cards.sort(key=lambda c: (BANDS.index(c["priority"])
                                       if c["priority"] in BANDS else 99,
                                       c["rank"]))

    def save_edits(self, tid, fields, base=None) -> bool:
        """Write the edit. False if it did not land, so a caller can wait.

        Closing Bert is one caller: "Save and close" must not close on top of
        a save that failed, or the error box goes with the window and nobody
        reads why. The editor is the other -- it stays open, with the typing
        in it, so the write can be tried again.
        """
        if not self._guard():
            return False
        ok = True
        try:
            self.api.edit(tid, fields, self.name(), base=base)
        except Conflict as e:
            ok = self._edit_conflict(tid, fields, base, e)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't save", str(e))
            ok = False
        self.refresh()
        return ok

    def _edit_conflict(self, tid, fields, base, e) -> bool:
        """Settle a write the server refused. True if nothing is left to keep.

        Not quite "did it land": choosing to discard settles it too. What the
        callers are really asking is whether the editor may be closed, and
        after "keep theirs" or "discard my changes" the answer is yes even
        though nothing was written. Backing out of the question -- closing the
        dialog, or a retry that fails -- leaves the typing where it is.
        """
        if e.code == "completed":
            box = QMessageBox(self)
            box.setWindowTitle("Already closed")
            box.setText(e.detail.get("message", "This ticket is closed."))
            box.setInformativeText("Your changes were not saved. Reopen the "
                                   "ticket if you still need to change it.")
            reopen = box.addButton("Reopen and retry", QMessageBox.AcceptRole)
            box.addButton("Discard my changes", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is reopen:
                try:
                    self.api.reopen(tid, self.name())
                    self.api.edit(tid, fields, self.name(), base=base, force=True)
                except Exception as err:
                    QMessageBox.warning(self, "Couldn't save", str(err))
                    return False
                return True
            return True         # they chose to discard, which settles it

        if e.code != "stale":
            QMessageBox.warning(self, "Couldn't save", str(e))
            return False

        dlg = ConflictDialog(self, e.detail)
        dlg.exec()
        if dlg.choice == "overwrite":
            try:
                self.api.edit(tid, fields, self.name(), base=base, force=True)
            except Exception as err:
                QMessageBox.warning(self, "Couldn't save", str(err))
                return False
            return True
        # "Keep theirs" discards what was typed and says so on the button.
        # Closing the dialog any other way answers nothing, so nothing is lost.
        return dlg.choice == "keep"

    def finish_item(self, tid, item_id):
        """Tick one work item off from the card, without opening the editor."""
        if not self._guard():
            return
        try:
            self.api.work_done(tid, item_id, self.name())
        except Conflict as e:
            d = e.detail
            QMessageBox.information(
                self, "Already done",
                f"{d.get('message', 'Someone already ticked that off.')}\n\n"
                f"{moments_ago(d.get('at'))}".strip())
        except Exception as e:
            QMessageBox.warning(self, "Couldn't tick that off", str(e))
        self.refresh()

    def complete(self, tid):
        if not self._guard():
            return
        self.notify("Closing the thread\u2026")
        try:
            self.api.complete(tid, self.name())
        except Conflict as e:
            self._clear_toast()
            d = e.detail
            if d.get("code") == "work_outstanding":
                # Reachable with the button disabled, which is why it is
                # handled rather than trusted away: the board is up to five
                # seconds behind the server.
                left = d.get("items") or []
                lines = [d.get("message", "This ticket still has work on it.")]
                lines += [""] + [f"\u2022  {b}" for b in left]
                lines += ["", d.get("hint", "")]
                QMessageBox.information(self, "Still to do",
                                        "\n".join(lines).strip())
                self.refresh()
                return
            QMessageBox.information(
                self, "Already closed",
                f"{d.get('message', 'Someone already closed this.')}\n\n"
                f"{moments_ago(d.get('at'))}".strip())
        except Exception as e:
            self._clear_toast()
            QMessageBox.warning(self, "Couldn't close that thread", str(e))
        else:
            self.completing.add(tid)
            # Take it off the board now, even with an editor open: an open
            # editor parks every poll, so otherwise a card closed meanwhile
            # sits there looking untouched until the editor closes.
            #
            # Hidden rather than rebuilt. The card being closed is never the
            # one being edited -- that one has no Complete button -- so
            # nothing being typed into is touched, and the next real render
            # replaces the lot anyway.
            if self.editing_card:
                self._hide_closed_card(tid)
        self.refresh()

    def _hide_closed_card(self, tid):
        """Drop a card off both lists without rebuilding either.

        Only ever reached with an editor open, which is the one time
        `render()` cannot run. Walks the layouts rather than `findChildren`,
        the way `_hold_scroll` does -- not for the order here, but because a
        layout is what the panel actually draws and a stray parented widget
        would answer to the other.
        """
        for band in self.bands.values():
            hit = False
            for i in range(band.lay.count()):
                w = band.lay.itemAt(i).widget()
                if isinstance(w, Card) and w.thread_id == tid:
                    w.hide()
                    hit = True
            if hit:
                # The heading counts what is on the band, and a card that has
                # gone is not on it. `isHidden` rather than `isVisible`: a
                # folded band's cards are all invisible and none of them are
                # closed.
                band.count.setText(str(sum(
                    1 for i in range(band.lay.count())
                    if isinstance(band.lay.itemAt(i).widget(), Card)
                    and not band.lay.itemAt(i).widget().isHidden())))

        # And the running order, which is the same board said twice -- 33
        # cards on one and 34 on the other is the kind of disagreement that
        # makes somebody stop trusting both.
        for i in range(self.rail.lay.count()):
            w = self.rail.lay.itemAt(i).widget()
            if isinstance(w, RailRow) and w.thread_id == tid:
                w.hide()

    def _undo(self, eid, force=False):
        r = self.api.undo(eid, self.name(), force=force)
        if r.get("correction_posted"):
            QMessageBox.information(
                self, "Undone",
                "That update was already posted to Discord, so Ernie added "
                "a correction in the thread.")
        return r

    def undo(self, eid):
        if not self._guard():
            return
        try:
            self._undo(eid)
        except Conflict as e:
            if e.code == "other_actor":
                d = e.detail
                ask = QMessageBox.question(
                    self, "Undo someone else's change?",
                    f"{d.get('message')}\n\n{d.get('detail') or ''}\n"
                    f"{moments_ago(d.get('at'))}\n\nUndo it anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ask == QMessageBox.Yes:
                    try:
                        self._undo(eid, force=True)
                    except Exception as err:
                        QMessageBox.warning(self, "Couldn't undo", str(err))
            else:
                QMessageBox.information(
                    self, "Can't undo that",
                    e.detail.get("message", str(e)))
        except Exception as e:
            QMessageBox.warning(self, "Couldn't undo", str(e))
        self.refresh()

    def editor_is_busy(self, tid):
        """True if another card's editor is in the way and stays there.

        One at a time: two open editors mean two unsaved drafts, and a
        refresh can only warn the single card it is tracking that it
        changed underneath.

        It used to say so and stop, which left somebody to find the other
        card themselves and deal with it before they could get on. It
        offers to finish it now -- and an editor with nothing typed in it
        is closed without asking at all, because there is no decision to
        put to anybody.
        """
        busy = self.editing_card
        # NEW_TICKET is a sentinel, not an identity: two tickets being started
        # are two different tickets. Letting it match itself here meant a
        # second + walked straight past the one-editor rule -- two placeholder
        # cards, two open editors, and one editing_card naming both, so
        # _card_widget answered with the first while somebody typed into the
        # second. Everything that asks "which card is being edited" then had
        # the wrong one: clicking Edit on a real ticket offered to save a draft
        # other than the one on the screen.
        if not busy or (busy == tid and tid != NEW_TICKET):
            return False
        w = self._card_widget(busy)
        if w is None or not getattr(w, "editing", False):
            self.editing_card = None      # its editor is gone; don't lock up
            return False

        if not w.is_dirty():
            w.exit_edit()
            return False

        self.reveal(busy)
        # Not "a new ticket in Medium": the band was decided by which + was
        # pressed, so saying it again on all three buttons is the longest
        # thing in the box and the part that was never in question. What the
        # buttons have to keep apart is the two tickets, and "it" against
        # "a new ticket" already does that -- the one being closed is named in
        # the body above them.
        going = self._short_name(tid)
        held = w.data.get("name") or busy
        if busy == NEW_TICKET:
            held = w.f_title.text().strip() or "an untitled ticket"

        # A ticket being started is not a card with unsaved edits: there is
        # nothing behind it, so "unsaved changes" and "Save" are both the
        # wrong words, and discarding loses the whole thing rather than an
        # edit to something that will still be there.
        starting = busy == NEW_TICKET
        # Both of them being tickets nobody has created yet needs its own
        # wording: "opening" one reads as though there were something there to
        # open, when what is being offered is starting a second.
        another = starting and tid == NEW_TICKET
        box = QMessageBox(self)
        if starting:
            box.setWindowTitle("A ticket you haven't created yet")
            box.setText(f"You're partway through starting a ticket:\n\n{held}")
            box.setInformativeText(
                f"{'Starting' if another else 'Opening'} {going} will close "
                f"it, and nothing has been created yet -- it would be lost.")
        else:
            # The title says what happened, the text says which ticket, and
            # the line under it says what the button will do. Saying
            # "unsaved changes" in the title and again in the body, and
            # "that editor" for a thing already named twice above, was
            # three sentences to carry one fact.
            box.setWindowTitle("Unsaved changes")
            box.setText(held)
            box.setInformativeText(f"Opening {going} will close it.")
        # Each button says what happens to both tickets. "this ticket" was
        # the one word that could not be used here: the ticket being closed
        # is not the one just clicked on.
        verb = "start" if another else "open"
        save = box.addButton(
            f"Create it, then {verb} {going}" if starting
            else f"Save and open {going}", QMessageBox.AcceptRole)
        drop = box.addButton(
            f"Discard it and {verb} {going}" if starting
            else f"Discard and open {going}", QMessageBox.DestructiveRole)
        stay = box.addButton("Keep writing" if starting else "Keep editing",
                             QMessageBox.RejectRole)
        # Staying is the one that loses nothing, so it is what Escape does.
        box.setDefaultButton(stay)
        box.exec()

        if box.clickedButton() is save:
            # save() closes the editor itself when the write lands, and
            # refresh() is a thread, so the board is not redrawn under the
            # card about to be opened. If it did not land the editor is still
            # open with the typing in it, and opening the other one would put
            # two on screen -- which is the thing this guard exists to stop.
            return not w.save()
        if box.clickedButton() is drop:
            w.exit_edit()
            return False
        return True

    def start_ticket(self, priority):
        """Open an editor for a ticket that does not exist yet.

        Straight into the editor rather than asking for a title first: the
        title is the thread's name and has a shape to keep, and the tag, the
        client and the work items are all in there anyway.
        """
        if not self._guard():
            return
        if self.editor_is_busy(NEW_TICKET):
            return

        today = datetime.now(timezone.utc).date()
        blank = {
            "thread_id": NEW_TICKET, "is_new": True, "priority": priority,
            "rank": 0.0, "queue": "PROD", "confidence": "strict",
            "name": f"PROD: Client - {title_stamp(today)} - what it's about",
            "client_raw": None, "client_override": None, "summary": None,
            "issues": [], "work_items": [], "equipment": [],
            "ticket_count": 0, "last_human_at": None, "completed_at": None,
            "unsent": 0, "stuck": 0, "unshared": False,
        }
        band = self.bands.get(priority)
        if band is None:
            return
        # A folded band hides its panel and keeps its header, so the button
        # that starts a ticket stays clickable while the place the card goes
        # does not exist on screen. The card would be inserted out of sight
        # with its editor, and editing_card holds the poll off meanwhile, so
        # the board reads as frozen with nothing on it to explain why. Open
        # it, for the reason a drag arriving opens one.
        if band.collapsed:
            band.set_collapsed(False)
        card = Card(blank, self)
        band.lay.insertWidget(0, card)
        band.setVisible(True)
        self.editing_card = NEW_TICKET     # holds the poll off while it is open
        card.enter_edit()

    def create_ticket(self, card, fields) -> bool:
        """Ask Ernie for the thread. It shows on the board straight away.

        False if nothing was asked for, so closing Bert can refuse to close
        over it -- an untitled draft still has everything typed into it.
        """
        if not (fields.get("title") or "").strip():
            QMessageBox.information(self, "A ticket needs a title",
                                    "Give it a title before creating it.")
            return False
        ok = True
        try:
            self.api.new_ticket(fields, self.name())
        except Exception as e:
            QMessageBox.warning(self, "Couldn't start that ticket", str(e))
            ok = False
        else:
            # Only once Ernie has it. Dropping the placeholder first meant a
            # request that failed took the whole ticket with it, and there is
            # nothing behind a new one to fall back to.
            card.exit_edit()      # drops the placeholder, releases the board
        self.refresh()
        return ok

    def _short_name(self, tid):
        """A ticket in a few words, for a sentence about two of them."""
        if tid == NEW_TICKET:
            return "a new ticket"
        c = next((x for x in self.cards if x["thread_id"] == tid), None)
        if not c:
            return "the other ticket"
        return clip(c.get("client_override") or c.get("client_raw")
                    or c.get("name") or "that ticket", 28)

    def reveal(self, tid):
        """Scroll the board to a card, opening its band if it's folded away."""
        band = self.bands.get(self.priority_of(tid))
        if band is not None and band.collapsed:
            band.set_collapsed(False)
            # ensureWidgetVisible needs the post-expand geometry, not the
            # geometry from before the panel came back.
            self.scroll.widget().layout().activate()
        w = self._card_widget(tid)
        if w is not None:
            self.scroll.ensureWidgetVisible(w, 0, 60)

    def jump_attention(self):
        """Scroll to the next card needing a person, wrapping at the end.

        Nothing happens while an editor is open: the poll already parks its
        payload for exactly this reason, and scrolling the board away from a
        half-typed ticket is the same discourtesy by another route. The
        tooltip says so rather than the click failing silently.

        It cycles what is on screen, which is what the label counts -- the
        narrowing is in the words (`2 of 3`) rather than in a card the control
        skips without saying.
        """
        if self.editing_card:
            return
        tid = next_attention(needs_attention(self._shown), self._attention_at)
        if tid is None:
            return
        self._attention_at = tid
        self.reveal(tid)
        w = self._card_widget(tid)
        if w is not None:
            w.flash()

    def priority_of(self, tid):
        for c in self.cards:
            if c["thread_id"] == tid:
                return c["priority"]
        return None

    def begin_drag(self):
        self.dragging = True
        for b in self.bands.values():
            b.set_cards(b.cards)
        # The rail needs it too: its empty bands only exist while dragging.
        self.rail.set_cards(self.rail.cards)
        self.edge_timer.start()

    def end_drag(self):
        self.edge_timer.stop()
        self.dragging = False
        for b in self.bands.values():
            b.marker.hide()
        self.render()
        self.apply_pending()

    def apply_pending(self):
        """Draw a poll that landed while the board was held.

        Both holds park the payload here -- a drag in flight, and an open
        editor. on_loaded re-checks both, so a release while the other one is
        still on simply parks it again rather than redrawing under it.
        """
        pending, self._pending = self._pending, None
        if pending is not None:
            self.on_loaded(pending)
        elif self._bands_stale:
            # No poll to draw, but something changed shape while the editor
            # was open -- a resize is the ordinary one. Without this the board
            # keeps the width it had until whichever poll happens next.
            self.render()

    def _edge_scroll(self):
        """Scroll the board while a card is held near the top or bottom edge.

        Fifty tickets don't fit on screen, and a band you can't see is a band
        you can't drop into -- the drag has nowhere to go. Holding the card at
        an edge walks the board along under it.
        """
        if not self.dragging:
            self.edge_timer.stop()
            return

        # Whichever list the pointer is over -- the rail scrolls at fifty
        # tickets too, so it needs the same treatment as the board.
        for area in (self.rail.scroll, self.scroll):
            vp = area.viewport()
            p = vp.mapFromGlobal(QCursor.pos())
            # Ignore the pointer once it's wandered off, so a drag taken
            # somewhere else doesn't leave the view scrolling on its own.
            if not (0 <= p.x() <= vp.width()):
                continue
            if not (-2 * EDGE_SCROLL_ZONE <= p.y()
                    <= vp.height() + 2 * EDGE_SCROLL_ZONE):
                continue

            bar = area.verticalScrollBar()
            if p.y() < EDGE_SCROLL_ZONE:
                bar.setValue(bar.value()
                             - self._edge_step(EDGE_SCROLL_ZONE - max(p.y(), 0)))
            elif p.y() > vp.height() - EDGE_SCROLL_ZONE:
                bar.setValue(bar.value()
                             + self._edge_step(EDGE_SCROLL_ZONE
                                               - max(vp.height() - p.y(), 0)))
            return

    @staticmethod
    def _edge_step(depth):
        """Ease in: a nudge at the edge of the zone, full speed at the very edge."""
        depth = min(max(depth, 0), EDGE_SCROLL_ZONE)
        return max(1, round(EDGE_SCROLL_MAX * (depth / EDGE_SCROLL_ZONE) ** 2))

    # -- rendering ---------------------------------------------------------

    def _hold_scroll(self):
        """Put both lists back where they were looking after a rebuild.

        The place is a card, not a scrollbar number: everything above the view
        can change height between rebuilds. The pixel is the fallback for when
        that card has gone.
        """
        if self.dragging:
            # _edge_scroll owns the scrollbars while a card is in the air, and
            # putting them back mid-drag fights it.
            return

        def in_order(area):
            """The scrolling items, top to bottom.

            findChildren answers in the order Qt happens to hold them, which
            is not the order they are drawn in -- and picking "the topmost"
            out of that gave whichever card came first in the tree.
            """
            out = []
            if area is self.scroll:
                for band in (self.bands[p] for p in BANDS if p in self.bands):
                    for i in range(band.lay.count()):
                        w = band.lay.itemAt(i).widget()
                        if isinstance(w, Card):
                            out.append(w)
            else:
                for i in range(self.rail.lay.count()):
                    w = self.rail.lay.itemAt(i).widget()
                    if isinstance(w, RailRow):
                        out.append(w)
            return out

        def anchor(area):
            """The item the eye is on: the one covering the top of the view."""
            vp = area.viewport()
            for w in in_order(area):
                y = w.mapTo(vp, QPoint(0, 0)).y()
                if y + w.height() > 0:
                    return w.thread_id, y
            return None

        kept = []
        for area in (self.scroll, self.rail.scroll):
            bar = area.verticalScrollBar()
            kept.append((area, bar, bar.value(), anchor(area)))

        def measure():
            """The geometry, with the scroll position taken out of it.

            A card's offset inside the scrolled widget does not move when the
            bar does, so this can be read as often as we like without our own
            correction disturbing the reading.
            """
            out = []
            for area, bar, _was, held in kept:
                y = None
                if held is not None:
                    y = next((w.mapTo(area.widget(), QPoint(0, 0)).y()
                              for w in in_order(area)
                              if w.thread_id == held[0]), None)
                out.append((bar.maximum(), y))
            return tuple(out)

        def put_back(tries=6, seen=None):
            if self._gone:
                return              # this window has been swapped out
            # A rebuild posts its layout requests rather than doing the work
            # there and then, so a position read before they are delivered is
            # the old one -- measured: the board still reported its old
            # maximum on the first pass and grew by 144px on the next. Drain
            # them twice, because activating a band posts fresh requests to
            # the cards inside it.
            for _ in range(2):
                QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
                for band in self.bands.values():
                    band.lay.activate()
                self.rail.lay.activate()

            now = measure()
            if now != seen and tries > 1:
                # Still settling. Wait for it rather than correct against it:
                # a correction worked out from geometry that is still moving
                # is wrong, and the next pass taking it back is a visible
                # jump. Measured on a resize -- which rebuilds the board
                # through the same timer the rail handle uses -- the bar went
                # +496px and returned 75ms later, twice per drag.
                QTimer.singleShot(16, lambda: put_back(tries - 1, now))
                return

            # Settled, so each bar is moved once and lands where it belongs.
            for area, bar, was, held in kept:
                vp = area.viewport()
                w = None if held is None else next(
                    (x for x in in_order(area) if x.thread_id == held[0]), None)
                if w is None:
                    # The card the view was on has gone -- completed, or
                    # filtered out by a search. The number is all that is left
                    # of the place, clamped because a board that got shorter
                    # has a smaller maximum than the value we took off it.
                    bar.setValue(min(was, bar.maximum()))
                    continue
                moved = w.mapTo(vp, QPoint(0, 0)).y() - held[1]
                if moved:
                    bar.setValue(
                        max(0, min(bar.value() + moved, bar.maximum())))

            # An editor that has just closed asks to be looked at, and that
            # outranks the anchor. Closing one halves the card -- measured at
            # 276px down to 138 on a ticket with three work items -- and while
            # it was open the view was somewhere inside it, so the pixel the
            # anchor restores now points at whatever fell into that space.
            # Reported as saving an edit and having to go and find the card.
            #
            # Last, because it has to beat the correction above rather than be
            # undone by it.
            if self._focus_card:
                tid, self._focus_card = self._focus_card, None
                self.reveal(tid)

        # Not yet: the layout hasn't settled, so maximum() is still the old one.
        QTimer.singleShot(0, put_back)

    def render(self):
        self._hold_scroll()
        term = self.search.text().strip().lower()

        # Before any of the filtering below, deliberately -- see queue_counts.
        for q, n in queue_counts(self.cards).items():
            self.qboxes[q].set_count(n)
        for name, n in equipment_counts(self.cards).items():
            self.chips[name].set_count(n)
        self._fill_clients()
        # Either half narrowing is enough to offer the way back.
        self.clear_equip.setVisible(
            bool(self.equip) or tuple(self.client_pick)[0] != CLIENT_ALL)

        wanted_types = equipment_types(self.equip)

        def keep(c):
            if not self.filters.get(c.get("queue") or "", True):
                return False
            # Any of the chosen kinds, not all: a ticket about a bot and a
            # reel belongs under both chips. One with no equipment matches
            # nothing and is hidden -- most of the board, legitimately, which
            # is what the counts on the chips are for.
            if wanted_types:
                mine = {e.get("eq_type") for e in (c.get("equipment") or [])}
                if not (mine & wanted_types):
                    return False
            # One customer per ticket, so this is an equality rather than the
            # any-of the chips above do. Keyed on the resolved client, which
            # is what makes `bravon` and `Bravo Environmental` one answer.
            kind, want = tuple(self.client_pick) or (CLIENT_ALL, "")
            if kind == "client":
                if client_key(c.get("client_override") or c.get("client_raw"),
                              self.board_roster()) != want:
                    return False
            elif kind == "raw":
                # The exact spelling, not the customer: picking `bravon` is
                # asking which tickets still say `bravon`.
                if (c.get("client_override")
                        or c.get("client_raw") or "").strip() != want:
                    return False
            if term:
                hay = " ".join(str(c.get(k) or "") for k in
                               ("name", "client_raw", "client_override",
                                "summary")).lower()
                hay += " ".join(e["raw"] for e in (c.get("equipment") or [])).lower()
                hay += " ".join(w["body"] for w in
                                (c.get("work_items") or [])).lower()
                return term in hay
            return True

        shown = self._shown = [c for c in self.cards if keep(c)]
        # Only the part worth acting on. The open count was a number nobody
        # did anything with -- the board itself says how much there is.
        problems = sum(1 for c in shown if needs_triage(c))
        everywhere = sum(1 for c in self.cards if needs_triage(c))
        self._attention_full = attention_text(problems, everywhere)
        # Colour on the widget rather than inline HTML, so `_fit_toolbar` can
        # shorten the words without having to rebuild the markup around them.
        self.count.setStyleSheet(
            f"color:{T.RED_FG if problems else T.MUTED}; font-size:12px;")
        self.count.setCursor(Qt.PointingHandCursor if problems
                             else Qt.ArrowCursor)
        self.count.setToolTip(
            "Finish or close the ticket being edited first."
            if problems and self.editing_card else
            "Go to the next ticket whose title can't be read." if problems
            else "")
        self._fit_toolbar()

        # Straight down the order the server sent: rank is the only order.
        # An open editor is never rebuilt under somebody, and only the bands
        # are spared, since that is where an editor lives -- the signatures
        # are left stale too, so `_bands_stale` redraws once it closes.
        self._bands_stale = bool(self.editing_card)
        ordered = []
        for band, w in self.bands.items():
            group = [c for c in shown if c["priority"] == band]
            if not self.editing_card:
                w.set_cards(group)
            ordered.extend(group)

        self.rail.set_cards(ordered)

        ok = self.writable()
        for w in self.bands.values():
            for i in range(w.lay.count()):
                c = w.lay.itemAt(i).widget()
                if isinstance(c, Card):
                    c.set_writable(ok)

        if not self.name():
            self.who.setText("Set your name to make changes")
            self.who.setStyleSheet(f"color:{T.AMBER_FG}; font-size:12px;")
        else:
            # Your own name told you nothing you didn't know. It only earns
            # toolbar space when it's missing, which blocks every write.
            self.who.setText("")

        self._render_feed()

    def _render_feed(self):
        # Every row is rebuilt on every poll and after every undo.
        keep = self.feed_scroll.verticalScrollBar().value()

        while self.feed_lay.count():
            w = self.feed_lay.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        # An undo of something already posted queues a correction naming the
        # event it retracts, so a row can say it is mid-revoke rather than
        # just going quiet between the click and the message going out.
        revoking = {e.get("new_value") for e in self.feed
                    if e["verb"] == "undo_correction" and not e.get("posted_at")}

        # Once for the whole feed, not per row: it is a property of the window.
        scale = self._feed_scale()

        for e in self.feed:
            eid = e["event_id"]
            opened = eid in self.feed_open
            short = self._feed_text(e, scale=scale)
            whole = self._feed_text(e, full=True)
            # Only a row with something behind it is worth a click. Most are
            # short enough to say everything already, and giving those an
            # affordance teaches people to click rows that never change.
            more = short != whole

            # Every row, whether or not there is more of it to read.
            row = FeedRow()
            h = QHBoxLayout(row)
            # Room above and below, so the Undo button is not sitting on the
            # hairline under the row.
            h.setContentsMargins(0, FEED_ROW_PAD, 0, FEED_ROW_PAD + 1)
            h.setSpacing(8)

            when = QLabel(self._clock(e["occurred_at"]))
            when.setFixedWidth(FEED_TIME_W)
            when.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
            h.addWidget(when, 0, Qt.AlignVCenter)

            txt = FeedLine(whole if opened else short)
            # Wrapping only when open.
            txt.setWordWrap(opened)
            # Given the spare width rather than a stretch beside it
            txt.setStyleSheet(
                f"color:{T.INK}; font-size:{FEED_FONT_PX}px;")
            if opened:
                # Wrapped, so it wants the width to wrap into.
                h.addWidget(txt, 1, Qt.AlignVCenter)
            else:
                # Its natural width, so the chevron sits against the end of
                # the text; FeedLine is what lets it be squeezed below that
                # when the window is narrow, which keeps the controls on
                # screen.
                h.addWidget(txt, 0, Qt.AlignVCenter)

            # Its own column, so a narrow window clipping the text cannot also
            # take away the only sign that there is more of it to read.
            chevron = QLabel("\u25be" if opened else "\u25b8" if more else "")
            chevron.setFixedWidth(FEED_MORE_W)
            chevron.setStyleSheet(f"color:{T.MUTED}; font-size:{FEED_FONT_PX}px;")
            h.addWidget(chevron, 0, Qt.AlignVCenter)
            # Everything left over goes here, between the line and the
            # controls, rather than between the line and its own chevron.
            h.addStretch(1)

            row.text_label = txt
            row.expanded = opened
            if more:
                row.setCursor(Qt.PointingHandCursor)
                row.setToolTip("Click to close" if opened
                               else "Click to read the whole line")
                row.clicked.connect(
                    lambda k=eid: self._toggle_feed_row(k))

            # Its own column, right-aligned and always the same width.
            status = self._feed_status(e, e["event_id"] in revoking)
            status_col = QWidget()
            sc = QHBoxLayout(status_col)
            sc.setContentsMargins(0, 0, 0, 0)
            sc.addStretch()
            if status:
                sc.addWidget(chip(*status))
            status_col.setFixedWidth(FEED_STATUS_W)
            h.addWidget(status_col, 0, Qt.AlignVCenter)

            undo_col = QWidget()
            uc = QHBoxLayout(undo_col)
            uc.setContentsMargins(0, 0, 0, 0)
            uc.addStretch()
            undo_col.setFixedWidth(FEED_UNDO_W)
            h.addWidget(undo_col, 0, Qt.AlignVCenter)

            # `renamed` belongs here: the API has always undone one, and
            # inside the window it is free, because nothing has left the
            # machine yet. check_feed reads this tuple off both ends, so the
            # two cannot drift apart again.
            undoable = e["verb"] in ("completed", "priority_changed", "edited",
                                     "work_done", "renamed")
            if undoable and not e["undone_at"]:
                b = QPushButton("\u21b6  Undo")
                # The same button does two different things either side of the
                # undo window, and looked identical doing them.
                if not e.get("posted_at"):
                    tip = "Nothing has been posted yet \u2014 undoing is silent."
                elif e["verb"] == "renamed":
                    tip = ("Already renamed in Discord \u2014 undoing renames it "
                           "back, which spends one of the two renames allowed "
                           "every ten minutes.")
                else:
                    tip = "Already in the thread \u2014 undoing posts a correction."
                b.setToolTip(tip)
                b.setCursor(Qt.PointingHandCursor)
                b.setStyleSheet(
                    f"QPushButton {{ {BTN_HIT}"
                    f" border:1px solid {T.ACCENT}; border-radius:5px;"
                    f" color:{T.ACCENT}; background:{T.CONTROL}; }}"
                    f"QPushButton:hover {{"
                    f" background:{rgba(T.ACCENT, 0.12)}; }}"
                    f"QPushButton:disabled {{ color:{T.MUTED}; border-color:{T.LINE}; }}")
                b.setEnabled(self.writable())
                b.clicked.connect(lambda _, i=e["event_id"]: self.undo(i))
                uc.addWidget(b)
            self.feed_lay.addWidget(row)
            row.show()

        self.feed_lay.addStretch()
        self._fit_feed()

        # After a rebuild the layout hasn't settled
        bar = self.feed_scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: None if self._gone
                          else bar.setValue(min(keep, bar.maximum())))

    @staticmethod
    def _feed_status(e, revoking):
        """
        Where a change has got to, as (text, background, foreground).
        """
        if revoking:
            return ("attempting to revoke…", T.AMBER_BG, T.AMBER_FG)
        if e.get("undone_at"):
            return ("undone", T.CHIP_BG, T.MUTED)
        if e.get("posted_at"):
            return ("in the thread", T.OK_BG, T.OK_FG)
        if e.get("claimed_at"):
            return ("posting…", T.AMBER_BG, T.AMBER_FG)
        if e.get("dispatch_after"):
            try:
                due = datetime.fromisoformat(e["dispatch_after"])
                left = int((due - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                return None
            return ((f"sending in {left}s" if left > 0 else "sending…"),
                    T.INFO_BG, T.INFO_FG)
        return None

    @staticmethod
    def _feed_text(e, full=False, scale=1.0):
        """One line of the activity feed.
        """
        who = e.get("actor_name") or "Ernie"
        # scale > 1 on a wide window: the same line, allowed more of itself
        # before it is cut. Never below the width it was written for, so a
        # narrow board reads exactly as it did.
        cut = ((lambda t, w: (t or "").strip()) if full
               else (lambda t, w: clip(t, max(int(w * scale), w))))
        what = cut(e.get("thread_name") or "", 46)
        thread = f"<span style='color:{T.MUTED}'>{what}</span>"

        old, new = e.get("old_value"), e.get("new_value")

        # Closed by somebody archiving the thread rather than by anyone here.
        if e["verb"] == "completed" and new == CLOSED_IN_DISCORD:
            # The name when the audit log gave one, and no name rather than a
            # wrong one when it did not.
            named = (e.get("actor_name") or "").strip()
            dot = f"<span style='color:{T.LINE}'> &middot; </span>"
            if named:
                return f"<b>{named}</b> closed {thread}{dot}<b>in Discord</b>"
            return f"{thread}{dot}<b>closed in Discord</b>"

        if e["verb"] == "priority_changed" and old in BANDS and new in BANDS:
            def band(b):
                return (f"<b style='color:{T.BAND_TEXT[b]}'>"
                        f"{BAND_LABEL[b]}</b>")
            # A middot before the bands: thread names end in a date, and
            # "5June26 High" ran together into one thing to read.
            return (f"<b>{who}</b> moved {thread}"
                    f"<span style='color:{T.LINE}'> &middot; </span>"
                    f"{band(old)} <span style='color:{T.MUTED}'>&#8594;</span> "
                    f"{band(new)}")

        if e["verb"] == "work_done" and (new or "").strip():
            # Which item, not just which thread.
            item = cut(new, 44)
            return (f"<b>{who}</b> finished {thread}"
                    f"<span style='color:{T.LINE}'> &middot; </span>"
                    f"<b>{item}</b>")

        if e["verb"] == "reordered":
            was, now = ex.reorder_spot(old), ex.reorder_spot(new)
            if was and now:
                # Rows from before the band was recorded still have both
                # places; they just can't say which band, so they don't.
                band = now[0] or was[0]
                lead = ""
                if band in BANDS:
                    lead = (f"<b style='color:{T.BAND_TEXT[band]}'>"
                            f"{BAND_LABEL[band]}</b> ")
                return (f"<b>{who}</b> reordered {thread}"
                        f"<span style='color:{T.LINE}'> &middot; </span>"
                        f"{lead}<b>{ex.ordinal(was[1])}</b> "
                        f"<span style='color:{T.MUTED}'>&#8594;</span> "
                        f"<b>{ex.ordinal(now[1])}</b>")

        if e["verb"] == "undo_correction":
            return f"<b>{who}</b> retracted an update to {thread}"

        if e["verb"] == "renamed" and (new or "").strip():
            return (f"<b>{who}</b> renamed {thread}"
                    f"<span style='color:{T.LINE}'> &middot; </span>"
                    f"<b>{cut(new, 40)}</b>")

        if e["verb"] == "edited":
            # An edit is batched -- four fields and three bubbles are one
            # event -- so "edited" was all the feed could say about any of it.
            # old_value carries the shape (which fields, how many bubbles) and
            # new_value the prose, so the line can say which of the two it was.
            added, removed, fields = Bert._edit_shape(e.get("old_value"))
            detail = (new or "").strip()
            if added and not removed and not fields:
                head = ("added a work item to" if added == 1
                        else f"added {added} work items to")
                detail = strip_lead(detail, "added ")
            elif removed and not added and not fields:
                head = ("removed a work item from" if removed == 1
                        else f"removed {removed} work items from")
                detail = strip_lead(detail, "removed ")
            elif (added or removed) and not fields:
                head = "changed the work on"
            else:
                head = "edited"
            if not detail:
                return f"<b>{who}</b> {head} {thread}"
            return (f"<b>{who}</b> {head} {thread}"
                    f"<span style='color:{T.LINE}'> &middot; </span>"
                    f"<b>{cut(detail, 44)}</b>")

        return f"<b>{who}</b> {e['verb'].replace('_', ' ')} {thread}"

    @staticmethod
    def _edit_shape(old):
        """What an edited event actually changed: bubbles, fields, or both.

        Read off old_value, which is the previous values of whatever moved,
        plus a __work__ entry naming the bubbles added and removed. Anything
        unreadable counts as nothing, so the line falls back to "edited"
        rather than the feed failing over a row it can't parse.
        """
        try:
            d = json.loads(old) if old else {}
        except (TypeError, ValueError):
            return 0, 0, 0
        if not isinstance(d, dict):
            return 0, 0, 0
        work = d.get("__work__") or {}
        if not isinstance(work, dict):
            work = {}
        return (len(work.get("added") or []),
                len(work.get("removed") or []),
                len([k for k in d if k != "__work__"]))

    @staticmethod
    def _clock(ts):
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except (ValueError, AttributeError, TypeError):
            return ""
        now = datetime.now().astimezone()
        fmt = "%#I:%M %p" if sys.platform == "win32" else "%-I:%M %p"
        if t.date() == now.date():
            return t.strftime(fmt)
        if (now.date() - t.date()).days == 1:
            return "Yesterday"
        return t.strftime("%b %d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8787")
    ap.add_argument("--pretend-version", default="", metavar="X",
                    help="testing only: claim this Bert is build X")
    ap.add_argument("--pretend-ernie", default="", metavar="X",
                    help="testing only: pretend Ernie answered as build X")
    ap.add_argument("--pretend-newest", default="", metavar="X",
                    help="testing only: pretend the release note says X")
    ap.add_argument("--pretend-required", default="", metavar="X",
                    help="testing only: pretend the release note demands X")
    a = ap.parse_args()
    global PRETEND_MINE, PRETEND_ERNIE, PRETEND_NEWEST, PRETEND_REQUIRED
    PRETEND_MINE, PRETEND_ERNIE = a.pretend_version, a.pretend_ernie
    PRETEND_NEWEST, PRETEND_REQUIRED = a.pretend_newest, a.pretend_required
    app = QApplication(sys.argv)
    # Fusion draws the same way on every desktop, which is what makes one
    # QPalette enough to carry the dark theme through Qt's own widgets.
    app.setStyle("Fusion")
    apply_theme(load_settings().get("theme", THEME_DEFAULT))
    w = Bert(a.api)
    _OPEN.append(w)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

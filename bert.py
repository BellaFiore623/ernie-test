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
import uuid
from datetime import datetime, timezone

import httpx

# The title format is defined once, in the parser the sync uses. Importing it
# keeps the editor's validity check and Ernie's own reading of a thread in
# agreement.
import ernie_extract as ex
import ernie_version
from PySide6.QtCore import (
    QEvent, QMimeData, QPoint, QPointF, QRect, QRectF, QSize,
    QStringListModel, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QPainter,
    QPalette, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox,
    QFormLayout, QGridLayout,
    QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QVBoxLayout,
    QWidget,
)

SETTINGS = pathlib.Path.home() / ".bert.json"
# Beside the script rather than in the settings directory: it ships with the
# code, and a checkout without it should still start.
LOGO = pathlib.Path(__file__).parent / "assets" / "bert_logo.png"
POLL_MS = 5_000       # a poll that changes nothing now costs <1ms to render
DEGRADED_S, BLOCKED_S = 5, 15
SHARED_STALE_S = 180   # three missed sync cycles: their changes aren't arriving
# The customer list is pulled hourly, so a few hours late means nothing and
# six means the pull has stopped -- a bad token, or Jira unreachable. Only
# then is there anything to say: an indicator that is always on is furniture.
ROSTER_STALE_S = 6 * 3600
# What a ticket being started stands under until Ernie has made its thread.
# It is not a thread id and never becomes one: the real card arrives from the
# next poll with an id of its own.
# What a `completed` event's new_value says when the closing happened in
# Discord rather than here. Matched, not imported: Bert talks to Ernie over
# HTTP and imports none of it. tests/check_closures.py holds the three copies
# together.
CLOSED_IN_DISCORD = "discord"

NEW_TICKET = "__new__"
MIRROR_STALE_S = 180   # the same three cycles, asked of Ernie's own reading:
                       # past this the sync loop has stopped and the board is
                       # older than it looks
REFRESH_GLYPH = "\u21bb"
SPIN_MS = 33           # the glyph turns while a manual refresh waits, so the
SPIN_STEP = 11         # wait reads from across the room and not only in the
                       # wording beside it. A full turn in about a second.
AWAIT_GIVEUP_S = 90    # a manual refresh waits for the next read of Discord,
                       # which is the only thing that moves the number. Past a
                       # cycle and a half the sync loop isn't running, and the
                       # amber age says more than a spinner does.
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
RAIL_MIN_W = 120           # narrower than this and a client name is gone
RAIL_MAX_W = 460           # wider is a second board, not a running order
# Margins, border and the queue stripe, taken off before working out how
# much of a line fits across the rest of a row.
RAIL_ROW_CHROME = 26
CARD_HEAD_SPACING = 8      # between the columns of a card's top row
CARD_CLIENT_MIN_W = 44     # a client name never shrinks past this, it elides
CARD_MIN_W = 140           # below this a card is not a card, whatever the window
RAIL_REDRAW_MS = 140       # after the handle settles, not during
RAIL_BAR_H = 2             # the rule beside a band's name in the running order
BOARD_PAD = 16              
BOARD_MAX = 800             
                           
#Recent Activity Feed                                           
FEED_HEIGHT = 160         
FEED_FOLDED = 30           
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
STATS_OLD_D = 90           # a quarter open is a different kind of old
STATS_MAX_AGE_S = 60       # these move when a ticket closes, not per poll
# What the figures panel can be asked about, shortest first. Weeks up to a
# month and then quarters, because that is how the work is actually talked
# about -- "the last three weeks" is a sentence somebody says and "the last
# 19 days" is not.
STATS_WINDOWS = (("7 days", 7), ("2 weeks", 14), ("3 weeks", 21),
                 ("4 weeks", 28), ("3 months", 91), ("6 months", 182),
                 ("9 months", 273), ("1 year", 365))
STATS_WINDOW_DEFAULT = 28
GLYPH_LEFT = "\u00ab"
GLYPH_RIGHT = "\u00bb"
# Qt's QWIDGETSIZE_MAX, which PySide6 does not export. Undoes a
# setFixedHeight, which sets minimum and maximum together.
UNCAPPED = 16777215
FEED_TIME_W = 60           # the timestamp column
FEED_MORE_W = 14           # the chevron, in its own column so it survives
# Between the last column and the scrollbar. The Undo button sat hard
# against it, which reads as the row having been cut off rather than
# ending.
FEED_GUTTER = 10
# How wide a row may get. The controls are right-aligned in fixed
# columns, so on a full-screen board they drifted a thousand pixels
# from the line they belong to and it stopped being obvious which
# button went with which entry. Capped, the gap cannot grow past
# what the eye can carry, and the columns stay a column.
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
# are the tickets nobody has picked up, and that is the one thing on the
# board wanting a person rather than reporting a state.
BAND_LABEL["unassigned"] = "Needs Attention"
CAUTION = "⚠"          # the same sign warning_row() uses

# Build state, return state and equipment direction were replaced by work
# items; nothing edits them any more. These stay so the activity feed and the
# conflict dialog can still put words to an old event that names one.
STATES = [("needs_created", "Needs created"),
          ("created", "Created"),
          ("not_needed", "Not needed")]
DIRECTIONS = [("", "\u2014"), ("leaving", "Leaving"), ("coming_back", "Coming back")]

# A stylesheet padding rule replaces the native one outright rather than
# adding to it, so every button that sets its own padding is a smaller target
# than a default Qt one. These sit on cards that take a drag, and a press a
# pixel outside the button grabs the card instead -- a silent miss. One
# generous value, used everywhere something is pressable. No colour in it, so
# it is the same in both themes.
BTN_HIT = "font-size:11px; padding:6px 14px; "

# Softened corners, the one purely cosmetic number here. A card is the big
# shape on the board and takes the larger radius; a rail row is 26px tall and
# anything more than a hint of a curve on one eats its own corner.
CARD_RADIUS = 5
ROW_RADIUS = 4
BTN_RADIUS = 5

# What a search box spends before any placeholder is drawn: field() sets
# 6px of padding either side over a 1px border, and the last two are slack so
# a hint that fits does not end flush against the frame.
SEARCH_HINT_PAD = 16


# --------------------------------------------------------------------------
# Colour
#
# Two palettes with the same keys, and nothing below reads a colour any other
# way. Adding one means adding it to both, which is the point: a hex written
# straight into a stylesheet is a value that only works in one theme, and
# there were a hundred and seventy of them here.
# --------------------------------------------------------------------------

LIGHT = {
    # A grey workspace with work surfaces that are *lighter*, not white. The
    # last pass put the cards at 0.91 luminance on a 0.79 ground, which is a
    # near-white slab taking up most of the window -- reported as glare
    # coming off the cards themselves once the chrome around them had already
    # come down. So the cards came down to meet the room rather than the room
    # rising to meet the cards.
    #
    # Five levels, darkest first, each a step and none of them a leap:
    #
    #   well     the outer chrome -- toolbar, the floor beside the column
    #   feed     the activity bar, which recedes furthest of the sections
    #   panel    the running order and the figures
    #   canvas   the workspace, which is the board column and nothing else
    #   surface  the cards
    #
    # The card stands 1.12x over the workspace -- a step, where it used to be
    # a jump -- and the *border* still carries the tag at 3.8x its own fill.
    # That relationship is the one doing the work; the fill only has to be
    # told from the room, not shouted across it.
    "ink": "#20252B", "muted": "#464C53", "line": "#B9C0C8",
    "surface": "#E3E6EA", "canvas": "#D5DAE0", "panel": "#D1D6DC",
    # The activity bar sits under the sections either side of the board, so
    # the history recedes when nobody is reading it. Measured, it is 1.04
    # against `panel` -- near the edge of what an eye picks up, and kept
    # because the direction is right even where the size of it is marginal.
    "feed": "#CDD2D8",
    # Raised controls -- the Qt Button role. Under the surface in light and
    # over it in dark, because "raised" is toward the light end in one and
    # the dark end in the other.
    "beside": "#DCE0E5",
    # What a button, a field or a work-item bubble is drawn on: a step under
    # the card, in both themes. The Undo button was a white object on a
    # darker bar, which is the same glare the cards had.
    "control": "#D5D9DE",
    "well": "#C9CED5",
    # Badge fills, a step *under* the card rather than over it -- on a card
    # this tone a lighter badge reads as a hole rather than a chip.
    "amber_bg": "#E6D8C3", "amber_fg": "#6E4806",
    "red_bg": "#EDD3D3", "red_fg": "#8E2828", "red_edge": "#C43C3C",
    "ok_fg": "#255629", "ok_bg": "#C9E4CD", "accent": "#26619E",
    "info_bg": "#CCDBEA", "info_fg": "#1B3A5C",
    "grey_fg": "#4E545A",
    # A tag carrying a fact rather than a warning -- an equipment number, a
    # ticket count. Quiet on purpose: there are several per card and they are
    # reference, not news.
    "chip_bg": "#DADEE3",
    "on_accent": "#FFFFFF", "hover_bg": "#C0D2E5",
    "neutral": ("#727272", "#E6E6E6", "#33373A"),
    # (stripe, fill, ink). The fills are mixed **onto the card base** rather
    # than onto white, which is what keeps them a tint of the room instead of
    # a pastel block dropped into it. Chroma 15-26 against 6-12 before: on a
    # near-white card that much colour glared, and on this one it reads as
    # the tag. The stripe is the tag colour taken down in lightness and not
    # in saturation -- same hue, same cast, dark enough to hold an edge.
    "queue": {
        "PROD": ("#A1660C", "#EDE5D7", "#5A320A"),
        "OPS":  ("#5B7C2C", "#DEE9CF", "#27500A"),
        "ENG":  ("#3C75BA", "#DEE6F0", "#1B3A5C"),
        "CS":   ("#8F5AC2", "#EAE3F2", "#3D2154"),
    },
    # The band header's strip: above the column, under the cards. Between the
    # two on purpose -- level with the cards and the header reads as part of
    # the run rather than the thing naming it.
    "band_tint": {
        "unassigned": "#EADFDF", "critical": "#EADFDF", "high": "#E5E1D9",
        "medium": "#DDE2E8", "low": "#E2E2E2",
    },
    # (fill, outline). The outline is what says which band it is and none of
    # them moved -- those are the severity marks.
    #
    # Unassigned carries a red wash, which it did not before. The argument
    # for the plain neutral was that a blank card reads as one nobody has
    # picked up -- true, and it left the largest band on the board as the
    # brightest, flattest thing in the window, which is the opposite of what
    # three passes at this palette were for. Dark has always washed it: its
    # unassigned is #301D1C, quieter than its critical at #3A2422, and light
    # mirrors that relationship rather than making the two alike -- chroma 11
    # against 15. The two most urgent states looking similar is already
    # accepted here; the status embed makes the same trade for the same
    # reason.
    "band_card": {
        "unassigned": ("#EFE4E4", "#C43C3C"), "critical": ("#F1E2E2", "#C43C3C"),
        "high": ("#ECE5D9", "#D19434"), "medium": ("#DFE7EF", "#7099CB"),
        "low": ("#E6E6E6", "#B7BCC2"),
    },
    # Heading ink, one per band, the dark end of the colour it is washed in.
    "band_text": {
        "unassigned": "#8E2828", "critical": "#8E2828", "high": "#6E4806",
        "medium": "#22588F", "low": "#454B51",
    },

}

# The neutral ramp is lifted from the PortalBear prototype, which had already
# been tuned against a real screen. The hues stay Bert's own, lifted until
# they read on a dark ground: a fill chosen to sit under black text is not a
# fill any more once the text on it is pale, it is a smudge. So the fills go
# deep and the inks come up, which is the opposite move to the light palette
# and the reason this could never have been a filter over the other one.
DARK = {
    "ink": "#E6E9EC", "muted": "#98A2AD", "line": "#333B45",
    "surface": "#1B2027", "canvas": "#14181D",
    # The sections around the work sit at the canvas here, not a step under
    # it. Dark's bottom end has no room for a fourth level: the whole of it
    # from the floor to the workspace is a contrast ratio of 1.06, and a step
    # inside that measures 1.04 against the canvas -- a difference nobody can
    # see, spent on a distinction light needs and dark does not. Dark gets its
    # depth from the border-to-fill relationship instead, which is 5-7x.
    "panel": "#14181D",
    # The activity bar. Light drops it under the sections either side; dark's
    # bottom end has nowhere left to go -- floor to workspace is a ratio of
    # 1.06 in total -- so it sits where they do.
    "feed": "#14181D",
    "beside": "#222831",
    # A step under every card here too, which is where dark already had it --
    # this is the value its buttons, fields and bubbles were already using, so
    # naming the role changes nothing on this side. `beside` could not do the
    # job: measured against dark's seven card fills it is lighter than some
    # and darker than others, 1.02-1.07, which is a control that appears and
    # disappears depending on the ticket's tag.
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

# What Settings offers. "system" is read from the desktop when it is applied
# and again whenever the desktop says it has changed, so a machine that
# darkens at sunset takes the board with it. An explicit light or dark is a
# decision and the desktop does not overrule it.
THEMES = ("system", "light", "dark")
THEME_LABEL = {"system": "Follow the desktop", "light": "Light", "dark": "Dark"}


class Theme:
    """The active palette, reached by name.

    Attribute access rather than a dict lookup, so the call sites read the way
    the constants they replaced did -- T.INK, not COLOURS["ink"] -- and so a
    colour a palette is missing is an AttributeError the first time the board
    draws rather than a KeyError somewhere down a later repaint.
    """

    _p = LIGHT

    def __init__(self):
        self.name = "light"

    def use(self, name: str) -> None:
        self.name = "dark" if name == "dark" else "light"
        self._p = DARK if self.name == "dark" else LIGHT

    @property
    def dark(self) -> bool:
        return self.name == "dark"

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

    The strip with the minimise and close buttons is drawn by the desktop, not
    by Qt, so no stylesheet and no QPalette reaches it -- a dark board under a
    bright white frame. DWM will darken it on request, which is the same
    switch every native app uses.

    Windows only, and quietly nothing anywhere else. The attribute was 19
    before Windows 10 build 18985 and 20 after, and asking with the wrong one
    is a returned error rather than a raise, so both are offered.
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

    colorScheme() landed in Qt 6.5; on anything older there is no answer to
    give and light is the safer guess, since that is what every stylesheet
    here was written against.
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
    """A stored setting to the palette to actually load."""
    if choice in ("light", "dark"):
        return choice
    return "dark" if desktop_is_dark() else "light"


def apply_theme(choice: str) -> None:
    """Load a palette and hand Qt a matching one for what it draws itself.

    The stylesheets below cover Bert's own widgets. Everything Qt renders on
    its own -- menus, tooltips, scrollbars, the popup list on a combo box,
    every QMessageBox -- reads QPalette instead, and would otherwise stay
    bright white in the middle of a dark board.
    """
    T.use(resolve_theme(choice))
    app = QApplication.instance()
    if app is None:
        return

    # From the style's own palette, not a blank one. A default-constructed
    # QPalette leaves every role this does not name at Qt's fallback, which is
    # largely black -- and setPalette() then installs that over the whole
    # application. It is why the rail's tooltips came out black: nothing here
    # names the role a tooltip actually paints its background from.
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

    # Tooltips need saying twice. Qt draws them itself and, on Windows, ignores
    # ToolTipBase/ToolTipText above -- they came out the system's pale yellow
    # in the middle of a dark board. Stating it as a rule also settles it
    # against any container stylesheet that would otherwise cascade into one,
    # which is how the rail's rows came to have black-on-black tooltips.
    app.setStyleSheet(
        f"QToolTip {{ color:{T.INK}; background-color:{T.SURFACE};"
        f" border:1px solid {T.LINE}; padding:4px 6px; }}")

# Issues that mean the thread itself couldn't be read properly.
BLOCKING = {"title_none", "title_unparseable", "title_prefix_only",
            "title_loose", "title_nonstandard"}


def card_skin(data, editing=False):
    """Fill and outline for one ticket, wherever it is drawn.

    A ticket wears its **tag** -- PROD, OPS, ENG, CS -- not its priority. The
    tag is what the ticket is; the priority is where it currently sits, which
    the band it is in already says, and saying it twice spent the board's
    whole colour budget on the half nobody was reading.

    Needs attention is the exception, and outranks the tag: red, because it is
    the one state that is asking for somebody rather than describing the work.

    An unreadable thread keeps whatever fill it has and is outlined at 2px, so
    it reads as outlined rather than merely coloured.

    The card and its row in the side rail have to agree about this, and used
    to say it separately -- which is how they came to fill it red in two
    places at once.
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


def needs_triage(c) -> bool:
    """Whether a card still reads as unreadable.
    """
    if not set(c.get("issues") or []) & BLOCKING:
        return False
    return not (c.get("client_override") or "").strip()

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

    Two debts, and they are the two Bert._owed() counts before warning about
    a close: events queued behind their undo window, and cards that have moved
    since the shared board was last published. A reorder, and every band move
    that is not in or out of critical, is silent by design -- no
    dispatch_after at all -- and appears only in the second, so reading the
    first alone would leave a card somebody had just dragged looking as though
    it had already gone out. The mark and the close warning have to agree, or
    one of them is lying.

    A row the outbox has given up on says something else. It will not be
    tried again, so a mark that reads as "in a moment" would be telling the
    reader to wait for something that is not coming -- which is exactly why
    /health reports `stuck` apart from `queued` rather than folding it in.

    **Words, not a glyph.** It was `*` and `!`, on the reasoning that the
    glyph is what tells the two states apart and it spends no colour. Both
    halves are true and neither made `*` mean anything: an asterisk in the
    corner of a card is a footnote mark with nothing to point at, and the
    sentence explaining it was in a tooltip nobody hovers on a card they are
    not already asking about. Saying it costs the width of a chip, and the
    card already wears chips -- the tag, the PIP count, "edited" -- so this is
    the shape the eye is reading there anyway.
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
    # The ink, not the accent. Measured against every card fill in both
    # palettes, the accent averages 4.6:1 in light and 5.9:1 in dark; the ink
    # is 13.7:1 and 11.8:1. It is also the cheaper choice: a card already
    # wears its tag's colour, and a mark that spends none leaves colour
    # meaning something.
    #
    # One sentence for all three cases. #ernie-state is a Discord channel too,
    # so a card waiting only on the shared board is still waiting on Discord
    # and saying so twice differently would be drawing a distinction the
    # reader cannot act on either way. Which of the three it is stays in the
    # tooltip, where it belongs.
    return "Pushing to Discord…", T.INK, f"Changed here — {why}"


MIME = "application/x-bert-card"

# Only the fields the editor still sends a base snapshot for; work items are a
# list and merge on their own, so they never appear in this warning.
FIELD_LABELS = {"client_override": "the client", "title": "the thread title"}


_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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


def btn_css() -> str:
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
    # to say it was a button.
    return (f"QPushButton {{ {BTN_HIT} background:{T.CONTROL};"
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


def field() -> str:
    """Type into these. A function, not a constant: a constant would be built
    once at import, in whichever palette happened to be loaded first."""
    return (f"background:{T.CONTROL}; border:1px solid {rgba(T.INK, 0.28)};"
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
        stored = current.get("theme", "system")
        self.theme.setCurrentIndex(
            THEMES.index(stored) if stored in THEMES else 0)
        form = QFormLayout()
        form.addRow("Your name", self.who)
        form.addRow("Theme", self.theme)
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

    def values(self):
        return {"name": self.who.text().strip(),
                "theme": self.theme.currentData()}


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


class ClickableWidget(QWidget):
    """A plain widget that reports left clicks -- used for band headers."""
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class FeedRow(ClickableWidget):
    """One line of the activity feed, with a hairline under it.

    The status chip and Undo are right-aligned in fixed columns, which is what
    makes them a column you can run down and click -- but the line they belong
    to ends a long way to the left of them, and it was not clear which button
    went with which entry. FEED_ROW_MAX_W caps how far apart they can get; the
    rule closes the rest of the distance by making the pair read as one row.

    Drawn, not added to the layout. A separator widget would be another item
    for _fit_feed to walk, measure, and hold to a row height, and it would
    have to be kept out of every count the panel height is worked out from.
    A line costs nothing in that accounting.
    """

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setPen(QColor(T.LINE))
        y = self.height() - 1
        p.drawLine(0, y, self.width(), y)


class ClickableLabel(QLabel):
    """Double-click jumps straight into edit mode on that field.

    Reports no minimum width, for the reason FeedLine does: an ordinary
    QLabel cannot be made narrower than its text, so it claims the whole
    client name as a floor and shoves everything after it off the end. The
    card's corner is fixed columns -- the PIP count, the age, the mark
    saying a change has not gone out -- and they were the ones that
    disappeared. Measured with a real customer: 'Municipal Authority of
    Westmoreland County' wants 408px inside a 300px card, and the age and
    the mark both landed past the edge, cut away in silence.

    The text is what gives way. That is the same call the feed rows make,
    and for the same reason -- a clipped name is still a name, while a
    control you cannot see is gone.
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


def client_matches(typed, roster, limit=CLIENT_HITS):
    """The clients worth offering for what has been typed so far.

    Searches the customer's name, their Jira summary, and every spelling the
    board has ever used for them -- the alias table already knows the
    misspellings, so a name that was typed wrong last year finds the right
    customer today.

    Nothing here decides anything. reconcile_aliases refuses to merge on
    resemblance because 'falmouth ma' and 'falmouth me' are 0.91 similar and
    are different places; that rule is about a matcher writing an alias with
    nobody watching. This one only puts candidates in front of a person, who
    picks -- which is the safe half of the same idea.
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
            best = max((difflib.SequenceMatcher(None, q, k).ratio()
                        for k in [short] + names if k), default=0.0)
            if best < CLIENT_FUZZY_MIN:
                continue
            score = best
        out.append((score, (c.get("short_name") or "").lower(), c))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in out[:limit]]


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

        # A client already on the card that Jira does not offer -- retired,
        # paused, or never in the list -- stays on the card. Same reason the
        # queue dropdown keeps a retired tag: not offering it to anybody is
        # not the same as taking it off the one ticket that has it.
        if current and current.lower() not in offered:
            self.addItem(current, current)

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

        self.setCurrentIndex(max(self.findData(current), 0) if current else 0)
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
    return forms


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


def queue_label(queue, count):
    """`OPS` until the board has loaded, `OPS (5)` after."""
    return queue if count is None else f"{queue} ({count})"


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
        # already is. "1 tickets" on a ticket read as nonsense twice over: the
        # count was always plural, and the noun collided with the card itself.
        #
        # Shown only past one. A thread usually exists because somebody raised
        # a PIP, so one is the case you would assume; two or more is the thing
        # worth a glance. Production runs 83 threads on one and 111 on two, so
        # this is a real distinction there -- and in the sandbox, where every
        # thread has exactly one, it correctly says nothing at all.
        pips = d.get("ticket_count") or 0
        ago = QLabel(self._ago(d.get("last_human_at")))
        ago.setStyleSheet(f"color:{T.MUTED}; font-size:11px; background:transparent;")

        # Built before they are placed, so the client can be told what is
        # actually left instead of claiming the row and shoving them off it.
        edited = chip("edited", T.CHIP_BG, T.MUTED) if d.get("client_override") else None
        after = [chip(f"{pips} PIPs", T.CHIP_BG, T.MUTED)] if pips > 1 else []
        after.append(ago)

        # Last in the row, so it sits in the card's top corner: this is the
        # one thing on the card about the change rather than about the ticket.
        mark = unsent_mark(d)
        if mark:
            text, colour, why = mark
            # A chip, like the ones beside it. Drawn in its own ink over the
            # plain chip ground rather than a fill of its own: it is a state,
            # not a warning, and the amber one carries the only colour here.
            said = chip(text, T.CHIP_BG, colour)
            said.setToolTip(why)
            after.append(said)

        # Cut to the room it actually has, measured against the font it draws
        # in, the way a rail row cuts both of its lines. A count of characters
        # would be a guess about a proportional font, and the head's chrome
        # is not even a fixed set of columns. The whole name is one hover away.
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
            self.body.addWidget(warning_row(
                "Couldn't read this thread's title \u2014 check the client and "
                "details, then edit to fix.", T.RED_FG))

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
        foot.setSpacing(16)
        foot.addStretch()
        for issue in (d.get("issues") or [])[:2]:
            if issue not in BLOCKING:
                foot.addWidget(chip(issue.replace("_", " "), T.AMBER_BG, T.AMBER_FG))

        self.edit_btn = QPushButton("Edit")
        self.edit_btn.setStyleSheet(btn_css())
        self.edit_btn.clicked.connect(self.enter_edit)
        foot.addWidget(self.edit_btn)

        # Qt puts a button's icon on the left, always.
        self.done_btn = QPushButton("Complete ")
        self.done_btn.setLayoutDirection(Qt.RightToLeft)
        self.done_btn.setStyleSheet(btn_css())
        self.done_btn.setIcon(tick_icon(T.OK_FG))
        self.done_btn.setIconSize(QSize(12, 12))
        self.done_btn.clicked.connect(lambda: self.board.complete(self.thread_id))
        foot.addWidget(self.done_btn)
        self.body.addLayout(foot)
        self.set_writable(self.board.writable())
        plain_cursors(self)

    # -- edit mode ---------------------------------------------------------

    def _client_room(self, fixed):
        """What the card's top row has left for the client name.

        Measured rather than assumed. A rail row can take a constant off its
        width because every row is the same shape; a card's head is not -- the
        queue tag, an "edited" chip, a PIP count, the age and the unsent mark
        are each there or not, and each as wide as its own text. So the chrome
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
        self.f_work = WorkBar(d.get("work_items") or [], editing=True)

        self.f_title = QLineEdit(d.get("name") or "")
        self.f_title.setStyleSheet(field())
        self.title_state = QLabel()
        self.title_state.setWordWrap(True)
        # Room under the caution sign the two unparseable branches put in
        # here, for the same reason warning_row() exists.
        self.title_state.setStyleSheet("font-size:11px; padding:1px 0 3px 0;"
                                       " background:transparent;")
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        title_box.addWidget(self.f_title)
        title_box.addWidget(self.title_state)
        title_holder = QWidget()
        title_holder.setStyleSheet("background:transparent;")
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

        # textEdited fires only for typing, so rebuilding the suggestion below
        # doesn't count as the person taking the title over.
        self.f_title.textEdited.connect(self._title_edited)
        self.f_title.textChanged.connect(self._check_title)
        self.f_queue.currentIndexChanged.connect(self._queue_picked)
        self.f_client.currentTextChanged.connect(self._suggest_title)
        self._check_title()

        form.addRow("Thread title", title_holder)
        form.addRow("Tag", self.f_queue)
        form.addRow("Client", self.f_client)
        form.addRow("Work items", self.f_work)

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

    def _suggest_title(self, _text=None):
        """Keep the title in step with the client, until someone types in it.
        """
        if getattr(self, "_title_touched", True):
            return
        # Rebuild from whatever the box holds now, not from the stored title:
        # otherwise a queue just chosen from the dropdown gets overwritten the
        # moment the client is edited.
        t = ex.parse_title(self.f_title.text().strip())
        if t.confidence not in ("strict", "loose"):
            return                  # nothing dependable to rebuild from
        client = self.f_client.text().strip() or t.client_raw or ""
        self.f_title.setText(
            f"{t.queue}: {client} - {title_stamp(t.date)} - {t.summary or ''}")

    def _queue_picked(self, _index):
        """Put the chosen queue into the title, keeping whatever else is there."""
        q = self.f_queue.currentData()
        if not q:
            return
        t = ex.parse_title(self.f_title.text().strip())
        if t.confidence in ("strict", "loose"):
            self.f_title.setText(
                f"{q}: {t.client_raw} - {title_stamp(t.date)} - {t.summary or ''}")
        elif t.confidence == "prefix_only":
            self.f_title.setText(f"{q}: {t.summary or ''}".strip())
        else:
            # Nothing parseable to keep, so lay out the standard shape from the
            # fields.
            client = self.f_client.text().strip() or "Client"
            today = datetime.now(timezone.utc).date()
            self.f_title.setText(
                f"{q}: {client} - {title_stamp(today)} - what it's about")

    def _check_title(self, _text=None):
        t = ex.parse_title(self.f_title.text().strip())
        # Keep the dropdown showing whatever the title actually says, including
        # when the person types a different prefix by hand.
        if hasattr(self, "f_queue"):
            want = t.queue or ""
            if self.f_queue.currentData() != want:
                self.f_queue.blockSignals(True)
                self.f_queue.setCurrentIndex(max(self.f_queue.findData(want), 0))
                self.f_queue.blockSignals(False)
        if t.confidence in ("strict", "loose"):
            self.title_state.setText(
                f"<span style='color:{T.OK_FG}'>✓</span> "
                f"<span style='color:{T.MUTED}'>{t.queue} &middot; {t.client_raw} "
                f"&middot; {title_stamp(t.date)} &middot; {t.summary or ''}</span>")
        elif t.confidence == "prefix_only":
            self.title_state.setText(
                f"<span style='color:{T.AMBER_FG}'>⚠ no date Ernie can read</span> "
                f"<span style='color:{T.MUTED}'>&mdash; tag {t.queue} is fine, "
                f"the rest won't parse</span>")
        else:
            self.title_state.setText(
                f"<span style='color:{T.RED_FG}'>⚠ doesn't match</span> "
                f"<span style='color:{T.MUTED}'>TAG: Client - 25Aug26 - "
                f"what it's about</span>")

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
        # Redrawing is safe again now the editor is gone.
        self.board.apply_pending()

    def _override(self) -> str:
        """What the Client box means as a client_override.

        An override says "the parsed client is wrong, use this instead". When
        the title already says what the box says -- which it does whenever the
        client was picked from the list, because picking rewrites the title --
        there is nothing to override, and saying so anyway would be a lie with
        consequences: needs_triage() reads a client_override as somebody
        vouching for an unreadable card, and would clear the red edge off
        every ticket anyone had merely opened.

        Compared against the title in the box rather than the card's
        client_raw, which is the *old* title's client until the next sync.
        """
        typed = self.f_client.text().strip()
        if not typed:
            return ""
        t = ex.parse_title(self.f_title.text().strip())
        if ex.normalise_client(typed) == ex.normalise_client(t.client_raw or ""):
            return ""
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
        if self.f_title.text().strip() != (base.get("title") or ""):
            return True
        if self._override() != (base.get("client_override") or ""):
            return True
        return bool(self.f_work.added() or self.f_work.removed()
                    or self.f_work.undone())

    def save(self) -> bool:
        """True if the write landed. Closing Bert waits on the answer."""
        if self.is_new:
            return self.board.create_ticket(self, {
                "title": self.f_title.text().strip(),
                "priority": self.data["priority"],
                "work_add": self.f_work.added(),
                "first_message": (self.f_first.text().strip()
                                  if self.f_first else ""),
            })
        fields = {
            "title": self.f_title.text().strip(),
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
        if self.done_btn is not None:        # None while the editor is open
            self.done_btn.setEnabled(ok)
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
    def _ago(ts):
        """How long since a person last said anything in the thread.

        Nothing at all for today. Most of the board is today most of the time,
        so "today" was a word on almost every card that told you what you
        would have assumed anyway -- and it read as information, which cost it
        a glance each time. The number is worth having exactly when it is not
        today.
        """
        if not ts:
            return ""
        try:
            then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ""
        d = (datetime.now(timezone.utc) - then).days
        return "" if d == 0 else "1d" if d == 1 else f"{d}d"


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
                # Hide, then unparent. deleteLater() only queues the deletion,
                # and a card still parented to the panel keeps painting at the
                # geometry it had -- so a rebuild mid-drag left the old rows on
                # screen underneath the new ones.
                #
                # But setParent(None) on a *visible* widget makes it a visible
                # top-level window, and it stays one until the event loop gets
                # round to deleting it. Rebuilding the feed threw away 151 rows
                # and put 151 blank windows on the desktop for 1.2s each,
                # titled "python3" because that is what Qt calls the
                # application. Hiding first costs nothing and is the whole fix.
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
            # A hidden band takes its "+ New Ticket" with it, and the band
            # is the answer to "where does this go" -- so with everything in
            # Needs Attention there was no way to start a ticket in Critical
            # at all, which is the one case where you most want to. Measured:
            # four of the five buttons did not exist.
            self.setVisible(bool(self.cards) or not self.board.filtering()
                            or self.priority == "unassigned")

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

    This used to be a bare coloured bar, which said a band started here and
    never said which -- you counted down from the top to work it out. The word
    says it outright, and says it in the neutral ink: a card already wears its
    tag, the board already tints its band headers, and spending a third colour
    on the same fact is what the tag rule exists to stop. The rule is what
    carries the eye across; it is a hairline, not a bar.

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
        # Scoped to the rail itself. Unscoped, a background rule cascades to
        # every descendant *and* to the tooltip a descendant owns, so hovering
        # a row produced a box the right size for its three lines, painted the
        # canvas colour, with the text the same colour as the box. Measured
        # against a plain widget in the same process: readable there, solid
        # black here.
        # A plain QWidget subclass ignores a stylesheet background
        # unless it says so: Qt only paints one for widgets that opt
        # in. Without this the rule below did nothing at all -- proved
        # by setting it to magenta and seeing the window through it --
        # and it went unnoticed for as long as this and the window
        # behind it were the same colour.
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

        self.fold_btn = QPushButton("\u00ab")
        self.fold_btn.setFixedSize(24, 24)
        self.fold_btn.setCursor(Qt.PointingHandCursor)
        self.fold_btn.setToolTip("Hide the running order")
        self.fold_btn.setStyleSheet(
            f"QPushButton {{ border:1px solid {T.LINE}; border-radius:5px;"
            f" background:{T.CONTROL}; color:{T.MUTED}; font-size:11px; }}"
            f"QPushButton:hover {{ background:{rgba(T.ACCENT, 0.12)};"
            f" color:{T.ACCENT}; }}")
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

        self.hint = QLabel("Drag to reorder, click to jump")
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
        self.fold_btn.setText("\u00bb" if yes else "\u00ab")
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
            # An empty band is still named. It used to appear only while a
            # drag was running, so a board with everything in one place showed
            # a single heading at rest and four more the instant a card was
            # picked up -- the list rearranging itself under you at the moment
            # you were aiming at it, and nothing to aim at before that. The
            # head already knows how to say "empty".
            if not group and not self.board.dragging and self.board.filtering():
                continue
            # Every band is named, the first one included: the word is a
            # label rather than a separator, and "Needs Attention" at the top
            # is the one people most need to see.
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

    The board says what is on the plate now. None of it says whether that is
    getting better or worse, how long a ticket takes, or which ones have been
    open since April. Each figure earns its place by answering something the
    board cannot: a page of statistics nobody acts on is furniture, and the
    first one that turns out to be wrong takes the credibility of the others
    with it. There were four; "no ticket raised" was dropped after Julian
    read it, which is the same standard the other three are kept to.

    Sized like the running order and for the same reasons -- a range rather
    than a fixed width, so the splitter handle has something to move; folded
    by its own button rather than by the handle; and the width remembered.
    The rail plus a full-width board is 1040px, so on anything wider this
    grows into empty space rather than out of the board.
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
        # Scoped, like Rail's. Unscoped it cascades into every child and into
        # the tooltips those children own.
        # A plain QWidget subclass ignores a stylesheet background
        # unless it says so: Qt only paints one for widgets that opt
        # in. Without this the rule below did nothing at all -- proved
        # by setting it to magenta and seeing the window through it --
        # and it went unnoticed for as long as this and the window
        # behind it were the same colour.
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

        self.fold_btn = QPushButton(GLYPH_RIGHT)
        self.fold_btn.setFixedSize(24, 24)
        self.fold_btn.setCursor(Qt.PointingHandCursor)
        self.fold_btn.setToolTip("Hide the data")
        self.fold_btn.setStyleSheet(
            f"QPushButton {{ border:1px solid {T.LINE}; border-radius:5px;"
            f" background:{T.CONTROL}; color:{T.MUTED}; font-size:11px; }}"
            f"QPushButton:hover {{ background:{rgba(T.ACCENT, 0.14)};"
            f" color:{T.ACCENT}; }}")
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
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(9)
        self.holder = QWidget()
        self.holder.setLayout(self.body)
        self.holder.setStyleSheet("background:transparent;")

        # The panel scrolls, the way the running order does. It used to be a
        # widget in a plain layout with a stretch under it, which was fine
        # while three blocks fitted -- add a fourth and Qt does not clip the
        # overflow, it *squashes every block proportionally*: measured, the
        # tally asked for 134px and was given 11, so a table of six rows was
        # drawn as one line and "time to close" was cut off the bottom edge.
        # Nothing said anything, because nothing had failed.
        #
        # More blocks are coming as people say what they want here, so this
        # is the shape that survives that rather than a height to keep an eye
        # on.
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

        room = max(self.width() - STATS_ROW_CHROME, 60)
        fm = QFontMetrics(self.font())

        # First, because it is the block somebody came to the panel for: how
        # much there is, how much arrived, how much left, and of what.
        tally = data.get("tally")
        if tally:
            self.body.addWidget(self._heading("Tickets"))
            self.body.addWidget(self._tally(tally))

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
        self.fold_btn.setToolTip("Show the data" if yes
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
        self._swapping_theme = False   # closing to reopen, not to quit
        self.awaiting = False       # a manual refresh, waiting on the next
        self.await_run = None       # read of Discord. await_run is the read it
        self.await_since = 0.0      # started from, to tell a new one landing
                                    # from the same one ageing
        self.filters = {q: True for q in T.QUEUE}
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

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        # The area around the column: the floor, like the space beside a
        # folded panel, so the column reads as a section standing on it.
        #
        # Named and styled, not set through the palette. A palette set here is
        # propagated over by the application palette apply_theme installs, so
        # this quietly stayed the canvas colour and the strip beside the board
        # was the same value as the board itself -- measured on a real window,
        # #14181D where #0E1115 was asked for. The name is what keeps the rule
        # off the cards: a bare `background:` on a scroll area cascades into
        # every band and card inside it.
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

        # The column is centred by capping the *scroll area* and letting two
        # spacers take the rest, not by centring the column inside a
        # full-width scroll area. The difference is where the scrollbar ends
        # up: left to fill the pane, the area keeps its bar at the pane's own
        # edge, so with the running order folded the board sat 116px in from
        # the left and its scrollbar sat 160px out to the right of it, hard
        # against the figures panel and reading as though it belonged to
        # them. This is the rule `feed_scroll` already follows for the same
        # reason -- the cap goes on the scroll area so its bar comes to the
        # cap with it.
        #
        # A stretch factor, **not** an alignment flag: `addWidget(scroll,
        # alignment=...)` makes a scroll area take its own sizeHint, which is
        # small, cap or no cap. With a factor it expands to the cap and the
        # spacers split what is left, evenly, which is the centring.
        self.board_holder = QWidget()
        self.board_holder.setObjectName("boardHolder")
        self.board_holder.setAttribute(Qt.WA_StyledBackground, True)
        self.board_holder.setStyleSheet(
            f"#boardHolder {{ background:{T.WELL}; }}")
        hold = QHBoxLayout(self.board_holder)
        hold.setContentsMargins(0, 0, 0, 0)
        hold.setSpacing(0)
        # Two spacers whose widths are worked out rather than two stretches
        # that split what is left evenly. Even is only centred while the two
        # side panels are the same width: drag the running order wide and the
        # figures narrow and the pane itself is off-centre, so a column
        # centred *inside the pane* sits off-centre in the window. Both are
        # widened by `_centre_board()` against the whole splitter.
        #
        # They also cannot carry stretch factors: with one each they split
        # the pane three ways with the area and it never reached its cap --
        # measured, the column stayed at its 463px minimum on a 1500px window.
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

        self.count = QLabel("")
        self.count.setStyleSheet(f"color:{T.MUTED}; font-size:12px;")
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
        self.search.setStyleSheet(field())
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
        self.feed_caret = QLabel("\u25be")
        self.feed_caret.setStyleSheet(f"color:{T.MUTED}; font-size:11px;"
                                      f" background:transparent;")
        lab = QLabel("Recent activity")
        lab.setStyleSheet(f"color:{T.MUTED}; font-size:11px;"
                          f" background:transparent;")
        hh.addWidget(self.feed_caret)
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
        # The cap lives on the scroll area, not on the rows and not on the
        # widget inside it. Two things follow, and both are the point:
        #
        # Every row fills one viewport, so they are all a single width and the
        # Undo buttons stay in a column. A row capped on its own takes the
        # width of its own text instead, and they landed at 761 and 845 for
        # two rows of the same feed.
        #
        # And the scrollbar belongs to the scroll area, so it comes to the cap
        # with it and closes the feed off, rather than sitting out at the
        # window edge with a field of nothing between it and the last button.
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
        self.feed_caret.setText("\u25b8" if self.feed_folded else "\u25be")
        self.feed_head.setToolTip("Show the activity feed" if self.feed_folded
                                  else "Hide the activity feed")
        self._fit_feed()

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

        The pane is only centred while the two side panels happen to match.
        Drag the running order out and fold the figures down and the board's
        pane starts 400px from the left and ends at the window edge -- so
        splitting that pane evenly leaves the tickets sitting well right of
        centre, which is what somebody looking at the screen sees.

        Worked out against the splitter, which spans the whole row: where the
        column *should* start is `(width - column) / 2`, and the left spacer
        is however far that is from where the pane begins.

        **Staying centred costs width, and that is the trade.** A column that
        fills its pane cannot be centred, because the pane is not -- so with
        the panels lopsided it comes in to the widest that *can* be, which is
        whichever of its two edges runs out first. Measured at 1500px with
        the running order at its 460 maximum and the figures at their 180
        minimum: the widest centred column is 568 against the 846 it would
        otherwise take. At 1920 the same arrangement centres at the full 846
        and costs nothing.

        The floor is the column's own minimum -- the width below which a card
        stops being a card. Under that it gives up no more room and simply
        sits as near the middle as the pane allows, which on a narrow window
        with both panels wide is hard against the near edge. Better a board
        off centre than a board too narrow to read.
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
            # Fixed on purpose: a folded feed is not something to drag open,
            # the caret does that.
            self.feed_panel.setFixedHeight(FEED_FOLDED)
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
        # taller than one that has lost it, so a height measured fresh every render
        # drops the moment the last undoable row ages out of its window -- and
        # the whole list slides up a few pixels while somebody is reading it.
        #
        # This is the number that ran away when closed rows were allowed to
        # wrap, and it only could because a wrapped label reported eight lines.
        # Closed rows are one line now and check_feed holds them to it, so
        # every value going in here is bounded by one line plus a button.
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
                    # Never shorter than it was closed. A row with no Undo
                    # button is smaller than the height they are all held to,
                    # so letting it take its natural size pulled everything
                    # below it upward -- opening a line to read four more
                    # characters moved the list under the pointer. Opening
                    # either changes nothing or adds the lines it needs.
                    #
                    # Plus the slack a closed row carries. A closed row is
                    # _feed_row_h tall around one line of text -- the height
                    # comes from the taller Undo column beside it, and the
                    # label is top-aligned, so there is room under the words.
                    # An open row set to exactly what its label needs has
                    # none, and its last line sits that much closer to the
                    # row below than every other line on the board does. It
                    # reads as the row squeezing into the gap rather than the
                    # list making space for it.
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
            want = self.settings.get("feed_height") or self._feed_wants
            total = self.split.height()
            if total > want + BOARD_MIN_H:
                self.split.setSizes([total - want, want])
                self._feed_sized = True

    # -- identity ----------------------------------------------------------

    def name(self):
        s = self.settings
        if s.get("name"):
            return s["name"].strip()
        # A settings file written before the field became one box.
        return f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()

    def writable(self):
        return bool(self.name()) and self.connected

    def open_settings(self):
        was = self.settings.get("theme", "system")
        dlg = SettingsDialog(self, self.settings, self.health)
        if dlg.exec() == QDialog.Accepted:
            self.settings.update(dlg.values())
            # Don't leave the old pair behind to be read back later.
            self.settings.pop("first_name", None)
            self.settings.pop("last_name", None)
            SETTINGS.write_text(json.dumps(self.settings, indent=2))
            if self.settings.get("theme", "system") != was:
                self.rebuild_in_new_theme()
                return
            self.render()

    def desktop_theme_changed(self, *_):
        """The desktop flipped. Only this board's business if it was following.

        An explicit light or dark is a decision, and the desktop does not get
        to overrule it -- that is the whole difference between the two.
        """
        if self.settings.get("theme", "system") != "system":
            return
        if T.name == resolve_theme("system"):
            return                  # already showing what the desktop asks for
        self.rebuild_in_new_theme()

    def rebuild_in_new_theme(self):
        """Build the window again in the other palette.

        Every stylesheet here is written where its widget is made, which is
        what keeps each one next to the thing it explains -- and the price is
        that there is no one sheet to swap. Restyling in place would mean
        finding all seventy-six of them again and being sure none was missed,
        and a single miss is a white panel in a dark board. Building the
        window once more cannot miss any. It costs the scroll position and one
        poll, on a setting nobody changes twice in a day.
        """
        apply_theme(self.settings.get("theme", "system"))
        fresh = Bert(self.api.base)
        _OPEN.append(fresh)
        fresh.search.setText(self.search.text())     # a typed search survives
        fresh.setGeometry(self.geometry())
        fresh.showMaximized() if self.isMaximized() else fresh.show()

        # Shown before the old one closes, so the last-window-closed quit
        # never fires; and the timers stopped by hand, because a closed window
        # is not a deleted one and its poll would go on running behind this.
        self._swapping_theme = True
        for t in (self.timer, self.clock, self.spin_timer, self.edge_timer):
            t.stop()
        self.close()
        if self in _OPEN:
            _OPEN.remove(self)
        self.deleteLater()

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
                if secs > 0:
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
                f"{n} card(s) in #ernie-state are written in format "
                f"v{skew.get('their_v')} and this machine speaks "
                f"v{skew.get('our_v')}, so they are being skipped -- and "
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
            tip = (f"{waiting} change(s) made here that the shared copy in "
                   "#ernie-state hasn't been told about yet. They go out on "
                   "the next cycle.")
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
        for step in range(max(len(fresh), len(shared))):
            self.fresh.setText(fresh[min(step, len(fresh) - 1)])
            self.shared.setText(shared[min(step, len(shared) - 1)])
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
        self.notify("Closing the ticket\u2026")
        try:
            self.api.complete(tid, self.name())
        except Conflict as e:
            self._clear_toast()
            d = e.detail
            QMessageBox.information(
                self, "Already closed",
                f"{d.get('message', 'Someone already closed this.')}\n\n"
                f"{moments_ago(d.get('at'))}".strip())
        except Exception as e:
            self._clear_toast()
            QMessageBox.warning(self, "Couldn't complete that card", str(e))
        else:
            self.completing.add(tid)
        self.refresh()

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

    def priority_of(self, tid):
        for c in self.cards:
            if c["thread_id"] == tid:
                return c["priority"]
        return None

    def filtering(self) -> bool:
        """Is the board showing less than it holds?

        An empty band is worth naming when the band is genuinely empty: it is
        somewhere to drop a card, somewhere to start one, and the shape of the
        order is easier to read when every step of it is on screen. It is
        noise when a *search* has emptied it -- somebody narrowing the view
        did that deliberately, and five headings over one result fights the
        narrowing rather than helping it.
        """
        return bool(self.search.text().strip()
                    or not all(self.filters.values()))

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

        render() tears every card down and builds it again whenever the data
        changes, so the scrollbar loses its place. Ticking one work bubble off
        a card halfway down a fifty-ticket board threw the view somewhere else
        entirely -- the same thing the activity feed did after an undo, and
        fixed the same way.

        The pixel alone is not enough, which is what it used to keep. All the
        height above the view belongs to other cards, and any of it can change
        between rebuilds: sixteen cards above gaining a line each moved the
        view a card and a half while the scrollbar read exactly the same
        number. So the card at the top of the view is noted and put back at
        the same height, and the pixel is only the fallback for when that card
        has gone -- completed, or filtered out by a search.

        Bands have no scroll area of their own; the two lists that scroll are
        the board column and the rail, which is the pair _edge_scroll walks
        for the same reason.
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

        # Not yet: the layout hasn't settled, so maximum() is still the old one.
        QTimer.singleShot(0, put_back)

    def render(self):
        self._hold_scroll()
        term = self.search.text().strip().lower()

        # Before any of the filtering below, deliberately -- see queue_counts.
        for q, n in queue_counts(self.cards).items():
            self.qboxes[q].set_count(n)

        def keep(c):
            if not self.filters.get(c.get("queue") or "", True):
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

        shown = [c for c in self.cards if keep(c)]
        # Only the part worth acting on. The open count was a number nobody
        # did anything with -- the board itself says how much there is.
        problems = sum(1 for c in shown if needs_triage(c))
        self.count.setText(
            f"<span style='color:{T.RED_FG}'>{problems} need attention</span>"
            if problems else "")

        # Straight down the order the server sent. Unassigned used to float its
        # unreadable threads here, which put them at the top of the board while
        # every other view -- the state channel, the numbers on the card
        # messages -- still read them in rank order, and left a drop between
        # two visible cards computing a rank against neighbours that were not
        # its neighbours. They are ranked to the top for real now, in
        # ensure_card, so there is one order and this draws it.
        # An open editor is never rebuilt under somebody. The poll already
        # parks its payload for exactly this reason -- but render() is reached
        # from places no poll goes, a resize and the end of a drag, and those
        # tore the editor down anyway. Maximising the window while writing a
        # new ticket destroyed it outright: the placeholder is not in
        # self.cards, so nothing rebuilt it, and editing_card was left naming
        # a widget that no longer existed, which holds every later poll and
        # leaves the board frozen with nothing on it to say why.
        #
        # Only the bands are spared, because that is where an editor lives.
        # The rail holds none and re-clips to the new width happily -- and the
        # band signatures are deliberately left alone, so whatever changed is
        # drawn by the render that follows the editor closing.
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

            # Every row, whether or not there is more of it to read: the rule
            # is what groups a line with its buttons, and a feed where only
            # some entries had one would group them wrongly. Only a row with
            # something behind it gets the cursor and the click, below.
            row = FeedRow()
            h = QHBoxLayout(row)
            # Room above and below, so the Undo button is not sitting on the
            # hairline under the row. One more at the bottom than the top,
            # because the hairline is drawn in the row's own last pixel: pad
            # both sides equally and the contents centre against a box that
            # is really a pixel shorter, which reads as sitting low.
            h.setContentsMargins(0, FEED_ROW_PAD, 0, FEED_ROW_PAD + 1)
            h.setSpacing(8)

            when = QLabel(self._clock(e["occurred_at"]))
            when.setFixedWidth(FEED_TIME_W)
            when.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
            h.addWidget(when, 0, Qt.AlignVCenter)

            txt = FeedLine(whole if opened else short)
            # Wrapping only when open. A closed row is a one-line summary and
            # has to stay exactly one line tall, because _fit_feed takes the
            # height every row is held to from these -- and a wrapped QLabel
            # reports its sizeHint at a heuristic width of its own, not the
            # width the layout will give it. Measured: 112px against 14 for
            # the same line. That became the row height for the whole feed,
            # the panel grew to fit it, and _feed_row_h only ever grows, so
            # every redraw ratcheted it further.
            txt.setWordWrap(opened)
            # Given the spare width rather than a stretch beside it: the label
            # used to take its one-line size hint and get cut off by whatever
            # was left over.
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
            # The characters themselves, not HTML entities: a QLabel only
            # reads rich text when it can see a tag, so "&#9656;" with
            # nothing around it was drawn literally -- and then clipped
            # to "&#" by the width of its column.
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

            undoable = e["verb"] in ("completed", "priority_changed", "edited",
                                     "work_done")
            if undoable and not e["undone_at"]:
                b = QPushButton("\u21b6  Undo")
                # The same button does two different things either side of the
                # undo window, and looked identical doing them.
                b.setToolTip(
                    "Already in the thread \u2014 undoing posts a correction."
                    if e.get("posted_at") else
                    "Nothing has been posted yet \u2014 undoing is silent.")
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
        QTimer.singleShot(0, lambda: bar.setValue(min(keep, bar.maximum())))

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

        `full` returns it with nothing cut out, which is what an opened row
        shows. Comparing the two is also how a row knows whether it has
        anything worth opening for -- clipping is exactly what hides content,
        so if the two are equal there is nothing behind the row.
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
        # It names no one on purpose: the thread object does not say who
        # archived it, and the audit log that would needs a permission the
        # bot has not got. "Ernie closed it" -- which is what the fallback
        # below would have said -- is the one reading that is definitely
        # wrong, because Ernie is the only party that certainly did not.
        if e["verb"] == "completed" and new == CLOSED_IN_DISCORD:
            # The name when the audit log gave one, and no name rather than a
            # wrong one when it did not. This branch used to drop the actor on
            # the floor -- written when a Discord closure could never carry a
            # name, and not revisited when View Audit Log made it possible. The
            # row had the name the whole time and the line threw it away.
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
    a = ap.parse_args()
    app = QApplication(sys.argv)
    # Fusion draws the same way on every desktop, which is what makes one
    # QPalette enough to carry the dark theme through Qt's own widgets.
    app.setStyle("Fusion")
    apply_theme(load_settings().get("theme", "system"))
    w = Bert(a.api)
    _OPEN.append(w)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

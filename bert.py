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
import json
import math
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

# The title format is defined once, in the parser the sync uses. Importing it
# keeps the editor's validity check and Ernie's own reading of a thread in
# agreement.
import ernie_extract as ex
from PySide6.QtCore import (
    QMimeData, QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread, QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QPainter,
    QPalette, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

SETTINGS = pathlib.Path.home() / ".bert.json"
# Beside the script rather than in the settings directory: it ships with the
# code, and a checkout without it should still start.
LOGO = pathlib.Path(__file__).parent / "assets" / "bert_logo.png"
POLL_MS = 5_000       # a poll that changes nothing now costs <1ms to render
DEGRADED_S, BLOCKED_S = 5, 15
SHARED_STALE_S = 180   # three missed sync cycles: their changes aren't arriving
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
DROP_ZONE_MIN = 72                     
RANK_STEP = 1000.0          
EDGE_SCROLL_ZONE = 64
EDGE_SCROLL_MAX = 22
EDGE_SCROLL_MS = 16
RAIL_WIDTH = 208
BOARD_PAD = 16              
BOARD_MAX = 800             
                           
#Recent Activity Feed                                           
FEED_HEIGHT = 160         
FEED_FOLDED = 30           
FEED_ROWS = 4              
FEED_MAX_ROWS = 8          
# Qt's QWIDGETSIZE_MAX, which PySide6 does not export. Undoes a
# setFixedHeight, which sets minimum and maximum together.
UNCAPPED = 16777215
FEED_TIME_W = 60           # the timestamp column
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
FEED_LIMIT = 200           

BANDS = ["unassigned", "critical", "high", "medium", "low"]
BAND_LABEL = {b: b.capitalize() for b in BANDS}

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


# --------------------------------------------------------------------------
# Colour
#
# Two palettes with the same keys, and nothing below reads a colour any other
# way. Adding one means adding it to both, which is the point: a hex written
# straight into a stylesheet is a value that only works in one theme, and
# there were a hundred and seventy of them here.
# --------------------------------------------------------------------------

LIGHT = {
    "ink": "#1F2124", "muted": "#6B7075", "line": "#DFE1E4",
    "surface": "#FFFFFF", "canvas": "#F5F6F7",
    # Behind and to the right of the board column, a shade under the canvas.
    "beside": "#EAECEE",
    "amber_bg": "#FCF3E2", "amber_fg": "#8A5A08",
    "red_bg": "#FBEBEB", "red_fg": "#9B2C2C", "red_edge": "#D14343",
    "ok_fg": "#2E6B34", "ok_bg": "#EAF2EA", "accent": "#2B6CB0",
    "info_bg": "#E6EDF7", "info_fg": "#1B3A5C",
    "grey_fg": "#555B60",
    # A tag carrying a fact rather than a warning -- an equipment number, a
    # ticket count. Quiet on purpose: there are several per card and they are
    # reference, not news.
    "chip_bg": "#EEEFF1",
    # Text on an accent-filled button, and the wash under a hovered bubble.
    "on_accent": "#FFFFFF", "hover_bg": "#D6E4F5",
    "neutral": ("#9AA0A6", "#EEEFF1", "#3C4043"),
    "queue": {
        "PROD": ("#EF9F27", "#FAEEDA", "#633806"),
        "OPS":  ("#97C459", "#EAF3DE", "#27500A"),
        "ENG":  ("#6F9BD1", "#E6EDF7", "#1B3A5C"),
        "CS":   ("#B08BD4", "#F0E9F7", "#3D2154"),
    },
    # A wash behind each band's cards. Unassigned is the one neutral in the
    # ramp on purpose: it is not a priority, it is the absence of one, and
    # wearing a near-critical red said the opposite of that across the room.
    "band_tint": {
        "unassigned": "#E8EBEF", "critical": "#F6E4E4", "high": "#FBF1DF",
        "medium": "#EAF1FA", "low": "#EFF1F2",
    },
    # The card, a shade deeper than its wash -- except unassigned, which is
    # the plain surface, the other way about. A blank card reads as one
    # nobody has picked up, and it leaves red to mean one thing on this
    # board: a card that needs a person. Those keep their outline over it.
    "band_card": {
        "unassigned": ("#FFFFFF", "#C6CCD3"), "critical": ("#F7DCDC", "#D14343"),
        "high": ("#FCEBD1", "#E0A03C"), "medium": ("#E3EDF9", "#7FA8D8"),
        "low": ("#EDEFF1", "#C2C7CC"),
    },
    # Heading ink, one per band, the dark end of the colour it is washed in.
    "band_text": {
        "unassigned": "#57616B", "critical": "#9B2C2C", "high": "#8A5A08",
        "medium": "#2B6CB0", "low": "#555B60",
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
    "beside": "#222831",
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
        "unassigned": "#232A32", "critical": "#2E1E1D", "high": "#2C2318",
        "medium": "#1F2833", "low": "#212730",
    },
    "band_card": {
        "unassigned": ("#1B2027", "#3E4753"), "critical": ("#3A2422", "#E08078"),
        "high": ("#382C18", "#D9A441"), "medium": ("#24303E", "#5A82B0"),
        "low": ("#262D36", "#3E4753"),
    },
    "band_text": {
        "unassigned": "#9AA6B3", "critical": "#F5AAA2", "high": "#EFC15E",
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

    pal = QPalette()
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

# Issues that mean the thread itself couldn't be read properly.
BLOCKING = {"title_none", "title_unparseable", "title_prefix_only",
            "title_loose", "title_nonstandard"}


def card_skin(data, editing=False):
    """Fill and outline for one ticket, wherever it is drawn.

    An unreadable thread is outlined, not filled: it keeps its band's colour,
    so an unassigned one still reads as unassigned and the red says only that
    a person is needed. 2px, so it reads as outlined next to a critical card
    that is merely red.

    The card and its row in the side rail have to agree about this, and used
    to say it separately -- which is how they came to fill it red in two
    places at once.
    """
    tint, edge = T.BAND_CARD.get(data.get("priority") or "", T.BAND_CARD["low"])
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


def rgba(hex_colour, alpha):
    """A washed-out version of a palette colour, for hairlines."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def field() -> str:
    """Type into these. A function, not a constant: a constant would be built
    once at import, in whichever palette happened to be loaded first."""
    return (f"background:{T.SURFACE}; border:1px solid {rgba(T.INK, 0.28)};"
            f" border-radius:3px; padding:4px 6px; color:{T.INK};")


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
    def __init__(self, parent, current):
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
                      "set. Following the desktop tracks it as it changes, so "
                      "a machine that darkens at sunset takes Bert with it.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
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

    def __init__(self, api):
        super().__init__()
        self.api = api

    def run(self):
        try:
            self.loaded.emit({"board": self.api.board(),
                              "events": self.api.events(),
                              "health": self.api.health()})
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
        f"{'dashed' if dashed else 'solid'} {fg}44; border-radius:3px;"
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


class ClickableLabel(QLabel):
    """Double-click jumps straight into edit mode on that field."""
    doubleClicked = Signal()

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
    """

    acted = Signal(str)          # this bubble's key

    def __init__(self, key, body, editing):
        super().__init__()
        self.key = key
        self.body = body
        self.setObjectName("bubble")
        # White, not a tint: the card underneath is now its queue's colour, and
        # a pale blue bubble all but disappeared on a blue ENG card.
        self.setStyleSheet(
            f"#bubble {{ background:{T.SURFACE};"
            f" border:1px solid {rgba(T.INK, 0.16)}; border-radius:11px; }}")

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 2, 3, 2)
        h.setSpacing(4)

        lab = QLabel(body)
        lab.setStyleSheet(f"color:{T.INK}; font-size:11px;"
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

        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)


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
                       "body": i["body"]} for i in items]
        self._removed = []       # item_ids of stored bubbles crossed off
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
                f"QLineEdit {{ background:{T.SURFACE}; border-radius:3px;"
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

    # -- editing -----------------------------------------------------------
    def commit_typed(self):
        text = self.entry.text().strip()
        if not text:
            return
        self._new += 1
        self._rows.append({"key": f"new-{self._new}", "item_id": None,
                           "body": text})
        self.entry.clear()
        self._draw()

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
                w.setParent(None)
                w.deleteLater()
        for row in self._rows:
            b = Bubble(row["key"], row["body"], self.editing)
            b.acted.connect(self._acted)
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
            b.btn.setEnabled(ok)


class QueueBox(QCheckBox):
    """A queue filter that wears its queue's own colours.
    """

    SIDE = 15                 # the box; the hit area is the whole widget

    def __init__(self, queue):
        super().__init__(queue)
        self.stripe, self.tint, self.ink = T.QUEUE.get(queue, T.NEUTRAL)
        self.setCursor(Qt.PointingHandCursor)
        f = QFont()
        f.setPointSize(9)
        f.setWeight(QFont.DemiBold)
        self.setFont(f)

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        return QSize(self.SIDE + 7 + fm.horizontalAdvance(self.text()) + 10,
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
                   Qt.AlignVCenter | Qt.AlignLeft, self.text())


class Card(QFrame):
    def __init__(self, data, board):
        super().__init__()
        self.data = data
        self.board = board
        self.thread_id = data["thread_id"]
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
            f" border-left:{4 if self.problem else 3}px solid {left}; }}")

    def _clear(self):
        # These belong to the view and are about to be deleted. Dropping the
        # references keeps set_writable from reaching a deleted C++ object.
        self.edit_btn = self.done_btn = self.work = None

        def drop(w):
            # Unparent before deleteLater.
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
        head.setSpacing(8)
        head.addWidget(chip(d.get("queue") or "\u2014", cbg, cfg))

        client = ClickableLabel(d.get("client_override")
                                or d.get("client_raw") or "Unknown client")
        f = QFont()
        f.setPointSize(11)
        f.setWeight(QFont.DemiBold)
        client.setFont(f)
        client.setStyleSheet(f"color:{T.RED_FG if self.problem else T.INK};"
                             f" background:transparent;")
        client.setToolTip("Double-click to edit")
        client.doubleClicked.connect(self.enter_edit)
        head.addWidget(client)
        if d.get("client_override"):
            head.addWidget(chip("edited", T.CHIP_BG, T.MUTED))
        head.addStretch()

        if d.get("ticket_count"):
            head.addWidget(chip(f"{d['ticket_count']} tickets", T.CHIP_BG, T.MUTED))
        ago = QLabel(self._ago(d.get("last_human_at")))
        ago.setStyleSheet(f"color:{T.MUTED}; font-size:11px; background:transparent;")
        head.addWidget(ago)
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

        items = d.get("work_items") or []
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
        self.edit_btn.setStyleSheet(BTN_HIT)
        self.edit_btn.clicked.connect(self.enter_edit)
        foot.addWidget(self.edit_btn)

        # Qt puts a button's icon on the left, always.
        self.done_btn = QPushButton("Complete ")
        self.done_btn.setLayoutDirection(Qt.RightToLeft)
        self.done_btn.setStyleSheet(BTN_HIT)
        self.done_btn.setIcon(tick_icon(T.OK_FG))
        self.done_btn.setIconSize(QSize(12, 12))
        self.done_btn.clicked.connect(lambda: self.board.complete(self.thread_id))
        foot.addWidget(self.done_btn)
        self.body.addLayout(foot)
        self.set_writable(self.board.writable())
        plain_cursors(self)

    # -- edit mode ---------------------------------------------------------

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

        self.f_client = QLineEdit(d.get("client_override") or d.get("client_raw") or "")
        self.f_client.setStyleSheet(field())
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
        self.f_client.textChanged.connect(self._suggest_title)
        self._check_title()

        form.addRow("Thread title", title_holder)
        form.addRow("Tag", self.f_queue)
        form.addRow("Client", self.f_client)
        form.addRow("Work items", self.f_work)
        # The labels QFormLayout makes for itself paint their palette
        # background.
        for i in range(form.rowCount()):
            lab = form.itemAt(i, QFormLayout.LabelRole)
            if lab is not None and lab.widget() is not None:
                lab.widget().setStyleSheet("background:transparent;")
        self.body.addLayout(form)

        note = QLabel("Saving posts one update to the thread, however many "
                      "fields you change. Changing the title renames the "
                      "Discord thread.")
        note.setStyleSheet(f"color:{T.MUTED}; font-size:11px;"
                           f" background:transparent;")
        self.body.addWidget(note)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(BTN_HIT)
        cancel.clicked.connect(self.exit_edit)
        row.addWidget(cancel)
        save = QPushButton("Save")
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
                w.setParent(None)
                w.deleteLater()
        self._warn_label.layout().addWidget(warning_row(msg, T.AMBER_FG))
        self._warn_label.show()

    def exit_edit(self):
        self.editing = False
        self.board.editing_card = None
        self._clear()
        self._paint()
        self._build_view()
        # Redrawing is safe again now the editor is gone.
        self.board.apply_pending()

    def save(self):
        fields = {
            "title": self.f_title.text().strip(),
            "client_override": self.f_client.text().strip(),
            # The bubbles travel as what changed, not as a list to diff: two
            # people adding different items then merge instead of colliding.
            "work_add": self.f_work.added(),
            "work_remove": self.f_work.removed(),
        }
        base = getattr(self, "_edit_base", None)
        # Put the card back in view mode before the write.
        self.exit_edit()
        self.board.save_edits(self.thread_id, fields, base)

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
        if not ts:
            return ""
        try:
            then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ""
        d = (datetime.now(timezone.utc) - then).days
        return "today" if d == 0 else "1d" if d == 1 else f"{d}d"


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
        # Scope to the header itself.
        self.header.setStyleSheet(
            f"#bandHeader {{ background:{tint};"
            f" border-left:3px solid {accent}; }}")

        if priority == "unassigned":
            self.hint = QLabel("new — drag into a priority")
            self.hint.setStyleSheet(
                f"color:{accent}; font-size:11px; background:transparent;")
        else:
            self.hint = QLabel("")

        h.addWidget(self.caret)
        h.addWidget(self.title)
        h.addWidget(self.count)
        if self.hint.text():
            h.addSpacing(12)    # the hint is a caption, not part of the count
            h.addWidget(self.hint)
        h.addStretch(1)
        outer.addWidget(self.header)

        # -- the tinted container the cards sit in ----------------------------
        self.panel = QWidget()
        self.panel.setObjectName("bandPanel")
        self.panel.setStyleSheet(f"#bandPanel {{ background:{tint}; }}")
        self.lay = QVBoxLayout(self.panel)
        self.lay.setContentsMargins(8, 8, 8, 8)
        self.lay.setSpacing(8)
        outer.addWidget(self.panel)

        # Shown only while a drag is running and this band is empty. Without
        # it an empty band is a blank strip that gives no sign it will take the
        # card, and the drop goes to whichever neighbour has cards in it.
        self.empty_hint = QLabel("drop here")
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

    def set_cards(self, cards):
        # Rebuilding a card is ~4ms, so redrawing every band on every poll costs
        # a fifth of a second of frozen UI on a 50-card board -- for identical
        # content. Only tear down when something actually changed.
        sig = json.dumps(cards, sort_keys=True, default=str)
        if sig == self._sig:
            self.cards = cards
            self._apply_drag_height()
            return
        self._sig = sig

        while self.lay.count():
            it = self.lay.takeAt(0)
            w = it.widget()
            if w is not None and w not in (self.marker, self.empty_hint):
                # Unparent first. deleteLater() only queues the deletion, and
                # a card that is still a child of the panel keeps painting at
                # the geometry it had -- so a rebuild mid-drag leaves the old
                # rows on screen underneath the new ones.
                w.setParent(None)
                w.deleteLater()
        # takeAt() above pulled the marker out along with the cards. Leaving it
        # shown paints a stale accent line at whatever row it last occupied,
        # because a refresh can land mid-drag. dragMoveEvent re-inserts it.
        self.marker.hide()
        self.cards = cards
        self.count.setText(str(len(cards)))
        for c in cards:
            self.lay.addWidget(Card(c, self.board))
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
            self.setVisible(bool(self.cards) or self.priority == "unassigned")

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
    """One line in the side rail: who it's for, in the colour of its band.
    """

    def __init__(self, data, board):
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
                           f" border:{px}px solid {edge};{left} }}")
        self.setCursor(Qt.OpenHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(0)

        who = (data.get("client_override") or data.get("client_raw")
               or data.get("name") or "\u2014")
        name = QLabel(who[:24])
        f = QFont()
        f.setPointSize(9)
        f.setWeight(QFont.DemiBold)
        name.setFont(f)
        name.setStyleSheet(f"color:{T.INK}; background:transparent;")
        lay.addWidget(name)

        # The band in words, and the untruncated name, one hover away.
        self.setToolTip(f"{who}\n{BAND_LABEL[data['priority']]}")

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
        self.setAcceptDrops(True)
        self.setFixedWidth(RAIL_WIDTH)
        self.setStyleSheet(f"background:{T.CANVAS};")

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
            f"QPushButton {{ border:1px solid {T.LINE}; border-radius:3px;"
            f" background:{T.SURFACE}; color:{T.MUTED}; font-size:11px; }}"
            f"QPushButton:hover {{ background:#EAF1FA; color:{T.ACCENT}; }}")
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

    def set_folded(self, yes):
        """Fold down to a spine so the board gets the whole window."""
        self.folded = yes
        self.head.setVisible(not yes)
        self.scroll.setVisible(not yes)
        self.hint.setVisible(not yes)
        self.setFixedWidth(30 if yes else RAIL_WIDTH)
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

    def set_cards(self, cards):
        # Same signature check the bands use.
        sig = json.dumps([cards, self.board.dragging], sort_keys=True, default=str)
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
                w.setParent(None)
                w.deleteLater()
        self.marker.hide()

        # Walk the bands rather than the cards, so a band with nothing in it
        # still gets its turn.
        first = True
        for band in BANDS:
            group = [c for c in self.cards if c["priority"] == band]
            if not group and not self.board.dragging:
                continue
            if not first:
                # Where the next band starts.
                line = QFrame()
                line.setFixedHeight(2)
                line.setStyleSheet(
                    f"background:{rgba(T.BAND_TEXT[band], 0.55)}; border:none;")
                self.lay.addWidget(line)
            first = False
            for c in group:
                self.lay.addWidget(RailRow(c, self.board))
            if not group:
                self.lay.addWidget(RailZone(band))
        self.lay.addStretch()

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
        if hot is not None:
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

    def dropEvent(self, e):
        tid = bytes(e.mimeData().data(MIME)).decode()
        self.marker.hide()
        self.lay.removeWidget(self.marker)
        for z in self._zones():
            z.set_hot(False)

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
        self.connected = True
        self.last_sync = None
        self.health = {}            # the last /health payload
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
        self.setStyleSheet(f"QMainWindow {{ background:{T.CANVAS}; }}")

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
        # The area around the column, set through the palette rather than a
        # stylesheet: a bare `background:` on the scroll area cascades into
        # every band and card inside it.
        vp = self.scroll.viewport()
        vp.setAutoFillBackground(True)
        pal = vp.palette()
        pal.setColor(QPalette.Window, QColor(T.BESIDE))
        vp.setPalette(pal)
        # Widget smaller than the viewport: pin it left, don't centre it.
        self.scroll.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        board = QWidget()
        board.setObjectName("boardColumn")
        # The column keeps the canvas colour and stops where the bands stop
        board.setStyleSheet(f"#boardColumn {{ background:{T.CANVAS};"
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

        self.rail = Rail(self)
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addWidget(self.rail)
        middle.addWidget(self.scroll, 1)
        outer.addLayout(middle, 1)
        outer.addWidget(self._feed_panel())

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
        bar = QWidget()
        bar.setStyleSheet(f"background:{T.SURFACE}; border-bottom:1px solid {T.LINE};")
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
        self.search.setPlaceholderText("Search client, equipment, summary")
        self.search.setFixedWidth(230)
        self.search.textChanged.connect(self.render)
        lay.addWidget(self.search)

        for q in T.QUEUE:
            cb = QueueBox(q)
            cb.setChecked(True)
            cb.stateChanged.connect(
                lambda s, k=q: (self.filters.__setitem__(k, bool(s)), self.render()))
            lay.addWidget(cb)

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

        self.refresh_btn = chrome_button(REFRESH_GLYPH, "Refresh")
        # Through a lambda: clicked passes its checked flag as the first
        # argument, which would arrive as manual=False and undo the point.
        self.refresh_btn.clicked.connect(lambda: self.refresh(manual=True))
        lay.addWidget(self.refresh_btn)

        self.who = QLabel("")
        lay.addWidget(self.who)
        gear = chrome_button("\u2699", "Settings")
        gear.clicked.connect(self.open_settings)
        lay.addWidget(gear)
        return bar

    def _feed_panel(self):
        self.feed_folded = False
        self._feed_row_h = 0
        w = QWidget()
        w.setObjectName("feedPanel")
        # Scoped, so the caption and the rows don't each paint their own block
        # of it the way a bare selector would.
        w.setStyleSheet(f"#feedPanel {{ background:{T.SURFACE};"
                        f" border-top:1px solid {T.LINE}; }}")
        w.setFixedHeight(FEED_HEIGHT)
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
        self.feed_scroll.setStyleSheet("background:transparent;")
        self.feed_scroll.viewport().setStyleSheet("background:transparent;")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        self.feed_lay = QVBoxLayout(inner)
        self.feed_lay.setContentsMargins(0, 0, 0, 0)
        self.feed_lay.setSpacing(3)
        self.feed_scroll.setWidget(inner)
        body.addWidget(self.feed_scroll)
        lay.addWidget(self.feed_body)

        self.feed_panel = w
        return w

    def toggle_feed(self):
        """Fold the feed down to its caption, giving the board the height."""
        self.feed_folded = not self.feed_folded
        self.feed_body.setVisible(not self.feed_folded)
        self.feed_caret.setText("\u25b8" if self.feed_folded else "\u25be")
        self.feed_head.setToolTip("Show the activity feed" if self.feed_folded
                                  else "Hide the activity feed")
        self._fit_feed()

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
        gaps = 3 * max(self.feed_lay.spacing(), 0)
        room = (self.feed_scroll.viewport().width()
                - FEED_TIME_W - FEED_STATUS_W - FEED_UNDO_W - gaps)
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
                    r.setMinimumHeight(max(self._feed_row_h, need))
                    r.setMaximumHeight(UNCAPPED)
                else:
                    r.setFixedHeight(self._feed_row_h)

        gap = self.feed_lay.spacing()

        def stack(n):
            return n * self._feed_row_h + max(0, n - 1) * gap

        # Hold FEED_ROWS open even when fewer have come in, so the panel and
        # the board above it don't jump as rows arrive, and stop growing at
        # FEED_MAX_ROWS
        shown = min(max(len(rows), FEED_ROWS), FEED_MAX_ROWS)
        view = stack(shown) if self._feed_row_h else 0
        self.feed_scroll.setFixedHeight(view)

        m = self.feed_panel.layout().contentsMargins()
        self.feed_panel.setFixedHeight(
            m.top() + m.bottom() + self.feed_panel.layout().spacing()
            + self.feed_head.sizeHint().height() + view)

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
        dlg = SettingsDialog(self, self.settings)
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
        self.poller = Poller(self.api)
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

    def closeEvent(self, ev):
        """
        Closing Bert doesn't lose a change -- the outbox is a separate process
        and posts it whether Bert is open or not. The mistake this is here to
        catch is closing Bert and then shutting the whole stack down on top of
        something that hasn't gone out yet.
        """
        q = self.health.get("queued") or {}
        n = q.get("count") or 0
        # A theme swap closes this window and opens another one. Nothing is
        # being shut down, so there is nothing to warn about.
        if self._swapping_theme or not n or not self.connected:
            return super().closeEvent(ev)

        due = ""
        if q.get("due_at"):
            try:
                secs = (datetime.fromisoformat(q["due_at"])
                        - datetime.now(timezone.utc)).total_seconds()
                if secs > 0:
                    due = f" The first goes out in about {int(secs)}s."
            except (ValueError, TypeError):
                pass

        thing = "change hasn't" if n == 1 else f"{n} changes haven't"
        ask = QMessageBox.question(
            self, "Not everything has reached Discord",
            f"{thing.capitalize()} been posted to the thread yet.{due}\n\n"
            f"Closing Bert is fine on its own — Ernie posts them whether Bert "
            f"is open or not. But if you're shutting everything down, leave "
            f"the rest running another minute or they won't go out at all.\n\n"
            f"Close Bert?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ask == QMessageBox.Yes:
            return super().closeEvent(ev)
        ev.ignore()

    def _tick_sharing(self):
        """
        How the shared board is doing, which is a different question from how
        this Bert is doing. Hidden entirely unless a board is actually shared,
        so nothing changes for one person on one machine.
        """
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

        self.shared.setText(text)
        self.shared.setStyleSheet(f"color:{colour}; font-size:11px;")
        self.shared.setToolTip(tip)
        self.shared.show()

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
        self.fresh.setText(text)
        self.fresh.setStyleSheet(
            f"color:{T.AMBER_FG if amber else T.MUTED}; font-size:11px;")
        self.fresh.setToolTip(tip)

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

    def save_edits(self, tid, fields, base=None):
        if not self._guard():
            return
        try:
            self.api.edit(tid, fields, self.name(), base=base)
        except Conflict as e:
            self._edit_conflict(tid, fields, base, e)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't save", str(e))
        self.refresh()

    def _edit_conflict(self, tid, fields, base, e):
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
            return

        if e.code != "stale":
            QMessageBox.warning(self, "Couldn't save", str(e))
            return

        dlg = ConflictDialog(self, e.detail)
        dlg.exec()
        if dlg.choice == "overwrite":
            try:
                self.api.edit(tid, fields, self.name(), base=base, force=True)
            except Exception as err:
                QMessageBox.warning(self, "Couldn't save", str(err))

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
        """True if some other card already has an editor open.

        One at a time: two open editors mean two unsaved drafts, and a refresh
        can only warn the single card it is tracking that it changed underneath.
        """
        busy = self.editing_card
        if not busy or busy == tid:
            return False
        w = self._card_widget(busy)
        if w is None or not getattr(w, "editing", False):
            self.editing_card = None      # its editor is gone; don't lock up
            return False
        self.reveal(busy)
        QMessageBox.information(
            self, "One ticket at a time",
            f"You're still editing:\n\n{w.data.get('name') or busy}\n\n"
            f"Save or cancel that one first.")
        return True

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

        Bands have no scroll area of their own; the two lists that scroll are
        the board column and the rail, which is the pair _edge_scroll walks
        for the same reason.
        """
        if self.dragging:
            # _edge_scroll owns the scrollbars while a card is in the air, and
            # putting them back mid-drag fights it.
            return
        kept = [(bar, bar.value()) for bar in
                (self.scroll.verticalScrollBar(),
                 self.rail.scroll.verticalScrollBar())]

        def put_back():
            for bar, was in kept:
                # A board that got shorter -- the last bubble ticked off a
                # card, say -- has a smaller maximum than the value we took.
                bar.setValue(min(was, bar.maximum()))

        # Not yet: the layout hasn't settled, so maximum() is still the old one.
        QTimer.singleShot(0, put_back)

    def render(self):
        self._hold_scroll()
        term = self.search.text().strip().lower()

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
        ordered = []
        for band, w in self.bands.items():
            group = [c for c in shown if c["priority"] == band]
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

            row = ClickableWidget() if more else QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)

            when = QLabel(self._clock(e["occurred_at"]))
            when.setFixedWidth(FEED_TIME_W)
            when.setStyleSheet(f"color:{T.MUTED}; font-size:11px;")
            h.addWidget(when, 0, Qt.AlignTop)

            body = whole if opened else short
            if more:
                body += (f"<span style='color:{T.MUTED}'>&nbsp;"
                         f"{'&#9662;' if opened else '&#9656;'}</span>")
            txt = QLabel(body)
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
            h.addWidget(txt, 1, Qt.AlignTop)

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
            h.addWidget(status_col, 0, Qt.AlignTop)

            undo_col = QWidget()
            uc = QHBoxLayout(undo_col)
            uc.setContentsMargins(0, 0, 0, 0)
            uc.addStretch()
            undo_col.setFixedWidth(FEED_UNDO_W)
            h.addWidget(undo_col, 0, Qt.AlignTop)

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
                    f" border:1px solid {T.ACCENT}; border-radius:3px;"
                    f" color:{T.ACCENT}; background:{T.SURFACE}; }}"
                    f"QPushButton:hover {{ background:#EAF1FA; }}"
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

        if (e["verb"] == "reordered"
                and (old or "").isdigit() and (new or "").isdigit()):
            return (f"<b>{who}</b> reordered {thread}"
                    f"<span style='color:{T.LINE}'> &middot; </span>"
                    f"<b>{ex.ordinal(old)}</b> "
                    f"<span style='color:{T.MUTED}'>&#8594;</span> "
                    f"<b>{ex.ordinal(new)}</b>")

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

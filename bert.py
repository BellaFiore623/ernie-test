"""
Bert -- equipment ticket board.

Drag cards to reprioritise, edit fields inline, mark work complete, undo from
the activity feed. Every change is attributed to the name in Settings, and
edits are batched so saving four fields posts one message, not four.

    pip install PySide6 httpx
    python bert.py --api http://127.0.0.1:8788
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

# The title format is defined once, in the parser the sync uses. Importing it
# keeps the editor's validity check and Ernie's own reading of a thread in
# agreement; it's pure functions over strings, no I/O.
import ernie_extract as ex
from PySide6.QtCore import (
    QMimeData, QPoint, QRect, QSize, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QPainter,
    QPalette, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

SETTINGS = pathlib.Path.home() / ".bert.json"
# Beside the script rather than in the settings directory: it ships with the
# code, and a checkout without it should still start.
LOGO = pathlib.Path(__file__).with_name("bert_logo.png")
POLL_MS = 5_000       # a poll that changes nothing now costs <1ms to render
DEGRADED_S, BLOCKED_S = 5, 15
TOAST_MS = 6_000      # a ceiling, not a duration: the toast normally clears the
                      # moment the board comes back without the card
DRAG_THRESHOLD = 5
RAIL_ZONE_MIN = 34          # the same thing in the rail, where space is
                            # tighter and a row is only ~26px to begin with
RAIL_ZONE_GAP = 7           # air above and below it, so the rows either side
                            # are visibly pushed clear rather than touching
DROP_ZONE_MIN = 72          # an empty band's drop area during a drag. Tall
                            # enough to hit with the pointer, which is what
                            # counts: the pointer lands wherever it was inside
                            # the card when the drag started, not on the card's
                            # top edge.
RANK_STEP = 1000.0          # must match ernie_api, so the optimistic rank
                            # Bert draws is the one the server writes
EDGE_SCROLL_ZONE = 64       # holding a card this close to the edge scrolls
EDGE_SCROLL_MAX = 22        # px per tick right at the edge
EDGE_SCROLL_MS = 16
RAIL_WIDTH = 208            # the side rail: wide enough for a client name
BOARD_PAD = 16              # gutter either side of the bands in the column
BOARD_MAX = 800             # a ticket stops growing here, however wide the
                            # window gets. A card running the full width of a
                            # large monitor puts its client name and its
                            # buttons a foot apart, and the tint has to stop
                            # where the cards do or the band is a slab of
                            # colour with nothing in most of it. Left aligned,
                            # so the board stays a column next to the rail.
FEED_HEIGHT = 160           # only until the first render measures the rows
FEED_FOLDED = 30            # just the caption, when it's folded away
FEED_ROWS = 4               # entries shown, and the height held open for them

BANDS = ["unassigned", "critical", "high", "medium", "low"]
BAND_LABEL = {b: b.capitalize() for b in BANDS}

# Build state, return state and equipment direction were replaced by work
# items; nothing edits them any more. These stay so the activity feed and the
# conflict dialog can still put words to an old event that names one.
STATES = [("needs_created", "Needs created"),
          ("created", "Created"),
          ("not_needed", "Not needed")]
DIRECTIONS = [("", "\u2014"), ("leaving", "Leaving"), ("coming_back", "Coming back")]

QUEUE = {
    "PROD": ("#EF9F27", "#FAEEDA", "#633806"),
    "OPS":  ("#97C459", "#EAF3DE", "#27500A"),
    "ENG":  ("#6F9BD1", "#E6EDF7", "#1B3A5C"),
    "CS":   ("#B08BD4", "#F0E9F7", "#3D2154"),
}
NEUTRAL = ("#9AA0A6", "#EEEFF1", "#3C4043")

INK, MUTED, LINE = "#1F2124", "#6B7075", "#DFE1E4"
SURFACE, CANVAS = "#FFFFFF", "#F5F6F7"
# Behind and to the right of the board column. A shade under the canvas, so a
# wide window reads as a column of work with room beside it rather than one
# undifferentiated field -- and so there is somewhere obvious for a second
# panel to go later.
BESIDE = "#EAECEE"
AMBER_BG, AMBER_FG = "#FCF3E2", "#8A5A08"
RED_BG, RED_FG, RED_EDGE = "#FBEBEB", "#9B2C2C", "#D14343"
OK_FG, ACCENT = "#2E6B34", "#2B6CB0"
INFO_BG, INFO_FG = "#E6EDF7", "#1B3A5C"
UNASSIGNED_TINT = "#EEF2F8"

# A stylesheet padding rule replaces the native one outright rather than adding
# to it, so every button that set its own padding was a smaller target than a
# default Qt button. These sit on cards that take a drag, and a press a pixel
# outside the button grabs the card instead of clicking -- so the whole miss is
# silent. Keep one generous value and use it everywhere something is pressable.
BTN_HIT = "font-size:11px; padding:6px 14px; "


# A light wash behind each band's cards. The cards themselves stay white, so
# the tint reads as the container the tickets are sitting in.
BAND_TINT = {
    "unassigned": "#F7EEEE",
    "critical":   "#F6E4E4",
    "high":       "#FBF1DF",
    "medium":     "#EAF1FA",
    "low":        "#EFF1F2",
}
# The card itself, a shade deeper than the wash it sits on so it still reads
# as a card on a band rather than a hole in one. Hot to cold down the ramp:
# unsorted, on fire, warm, cool, cold -- so the band a card belongs to is
# readable without finding its header. The queue keeps the left stripe.
BAND_CARD = {
    "unassigned": ("#FBEEEE", "#C08A8A"),
    "critical":   ("#F7DCDC", "#D14343"),
    "high":       ("#FCEBD1", "#E0A03C"),
    "medium":     ("#E3EDF9", "#7FA8D8"),
    "low":        ("#EDEFF1", "#C2C7CC"),
}

# Heading ink, one per band, each the dark end of the colour that band is
# washed in -- so a header, its wash and its cards are all one thing. Medium
# keeps the blue it always had. Used for the caret, title, count and the rule
# down the left of the header, and for the band name in the side rail.
GREY_FG = "#555B60"          # the dark end of low's grey, readable on the wash
BAND_TEXT = dict.fromkeys(BANDS, ACCENT)
BAND_TEXT["unassigned"] = RED_FG
BAND_TEXT["critical"] = RED_FG
BAND_TEXT["high"] = AMBER_FG
BAND_TEXT["low"] = GREY_FG

# Issues that mean the thread itself couldn't be read properly.
BLOCKING = {"title_none", "title_unparseable", "title_prefix_only",
            "title_loose", "title_nonstandard"}


def needs_triage(c) -> bool:
    """Whether a card still reads as unreadable.

    The red warning tells the human to fix it by editing the card, but the
    issue itself is derived from the Discord title and no edit here can clear
    it -- so the card stayed red no matter what they did. A client typed in by
    hand is that acknowledgement. The parsed title stays wrong until someone
    renames the thread, which is correct: Discord is the source of truth.
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


# Type into these. The card behind them carries its band's colour now, and a
# box with no fill of its own just becomes part of it -- and the title box in
# particular sits inside a holder that was told to be transparent, which its
# children inherited. State the fill and the frame rather than hoping.
FIELD = (f"background:{SURFACE}; border:1px solid {rgba(INK, 0.28)};"
         f" border-radius:3px; padding:4px 6px; color:{INK};")


def show_value(v):
    return VALUE_LABEL.get(v or "", v)


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
        self.choice = None

        lay = QVBoxLayout(self)

        who = detail.get("by") or "Someone else"
        head = QLabel(f"<b>{who}</b> changed this card while you had it open.")
        head.setWordWrap(True)
        lay.addWidget(head)

        when = detail.get("at")
        if when:
            sub = QLabel(moments_ago(when))
            sub.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            lay.addWidget(sub)

        grid = QFormLayout()
        grid.setSpacing(6)
        for ch in detail.get("changes", []):
            box = QVBoxLayout()
            theirs = QLabel(f"Theirs:  {show_value(ch.get('theirs'))}")
            theirs.setStyleSheet(f"color:{AMBER_FG}; font-size:12px;")
            mine = QLabel(f"Mine:    {show_value(ch.get('mine'))}")
            mine.setStyleSheet(f"color:{ACCENT}; font-size:12px;")
            box.addWidget(theirs)
            box.addWidget(mine)
            holder = QWidget()
            holder.setLayout(box)
            grid.addRow(f"{ch.get('label', ch.get('field'))}", holder)
        lay.addLayout(grid)

        note = QLabel("Overwriting replaces their value with yours. Keeping "
                      "theirs discards what you typed.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        lay.addWidget(note)

        row = QHBoxLayout()
        row.addStretch()
        keep = QPushButton("Keep theirs")
        keep.clicked.connect(lambda: self._pick("keep"))
        row.addWidget(keep)
        over = QPushButton("Overwrite with mine")
        over.setStyleSheet(f"background:{ACCENT}; color:white; border:none;"
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
        # One box. The two halves were never used apart -- every read of them
        # joined them straight back together to hand Ernie one string.
        self.who = QLineEdit(current.get("name") or
                             f"{current.get('first_name', '')} "
                             f"{current.get('last_name', '')}".strip())
        self.who.setPlaceholderText("First Last")
        form = QFormLayout()
        form.addRow("Your name", self.who)
        note = QLabel("Your name is added to thread updates so the team can see "
                      "who made each change. Changes are blocked until it's set.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(bb)

    def values(self):
        return {"name": self.who.text().strip()}


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=8.0)

    def board(self):
        return self.client.get(f"{self.base}/cards").json()

    def events(self, limit=8):
        return self.client.get(f"{self.base}/events", params={"limit": limit}).json()

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
                              "events": self.api.events()})
        except Exception as e:
            self.failed.emit(str(e))


def warning_row(text, fg, size=11):
    """A caution sign beside a wrapped message.

    The sign is its own label rather than the first character of the text.
    U+26A0 comes from the emoji fallback font, and its glyph box is exactly as
    tall as an 11px line -- no slack at all -- so at anything but 100% display
    scaling the rounding clips the top of the triangle. On its own it can be
    given the room it needs without stretching the line the message sits on.
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

    The editor sits inside the board's scroll area, so a wheel turn over an
    open card should move the board. Qt's default is for the box under the
    pointer to eat the wheel and step its own selection instead -- silently
    editing a field nobody clicked. Ignoring the event lets it fall through
    to the scroll area; the popup list still scrolls on its own.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Default is WheelFocus, which would let a stray wheel turn take focus.
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, e):
        e.ignore()


def plain_cursors(parent):
    """Give the controls on a card their own cursors.

    Qt hands a widget's cursor down to any child that has not set one, so the
    card's grab cursor lands on every button and field sitting on it. A button
    under an open hand reads as "drag me", not "click me".
    """
    for w in parent.findChildren(QLineEdit):
        w.setCursor(Qt.IBeamCursor)
    for cls in (QPushButton, QComboBox, QCheckBox):
        for w in parent.findChildren(cls):
            w.setCursor(Qt.PointingHandCursor)


class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto the next line.

    Qt ships nothing that does this. Work items are typed by hand and run from
    two words to a short sentence, so a row of them has to be able to spill
    onto a second line instead of squeezing every bubble to nothing.
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

    The button on the right is the point of the widget: a tick while the card
    is just sitting there -- that item got done -- and a cross while the editor
    is open, which says it shouldn't have been on the list at all.
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
            f"#bubble {{ background:{SURFACE};"
            f" border:1px solid {rgba(INK, 0.16)}; border-radius:11px; }}")

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 2, 3, 2)
        h.setSpacing(4)

        lab = QLabel(body)
        lab.setStyleSheet(f"color:{INK}; font-size:11px;"
                          f" background:transparent; border:none;")
        h.addWidget(lab)

        self.btn = QPushButton("\u2715" if editing else "\u2713")
        # Smaller than the buttons elsewhere on the card, but the square is the
        # hit area and only the glyph inside it has to stay quiet.
        self.btn.setFixedSize(22, 22)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setToolTip("Remove this item" if editing else "Mark this done")
        hover, ink = (RED_BG, RED_FG) if editing else ("#D6E4F5", OK_FG)
        self.btn.setStyleSheet(
            f"QPushButton {{ border:none; border-radius:11px; font-size:11px;"
            f" background:transparent; color:{MUTED}; }}"
            f"QPushButton:hover {{ background:{hover}; color:{ink}; }}"
            f"QPushButton:disabled {{ color:{LINE}; background:transparent; }}")
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
            # this box is the only way to add an item, so it has to read as
            # something you click, not as a caption.
            # Deliberately without a `color` of its own: a stylesheet colour
            # overrides the palette, and the palette is the only portable way
            # to set the placeholder's.
            self.entry.setStyleSheet(
                f"QLineEdit {{ background:{SURFACE}; border-radius:3px;"
                f" padding:4px 6px; font-size:11px;"
                f" border:1px solid {rgba(INK, 0.38)}; }}"
                f"QLineEdit:hover {{ border:1px solid {ACCENT}; }}"
                f"QLineEdit:focus {{ border:1px solid {ACCENT}; }}")
            pal = self.entry.palette()
            pal.setColor(QPalette.PlaceholderText, QColor(MUTED))
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
                # Unparent before deleteLater: deletion is deferred to the next
                # trip round the event loop, and until then a bubble that is
                # still a child of the holder goes on being painted where it
                # last sat -- a ghost of the item just removed. Hold the widget
                # in a local first: the layout item drops it the moment it is
                # unparented, so asking twice gets None the second time.
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

    Painted rather than styled: a stylesheet can only put a tick in a checkbox
    by loading an image from disk, and the tick is the part that has to be the
    tag's dark ink. So the whole control is drawn here -- pale fill, dark tick,
    dark label -- straight off the same three colours the tag chip uses.
    """

    SIDE = 15                 # the box; the hit area is the whole widget

    def __init__(self, queue):
        super().__init__(queue)
        self.stripe, self.tint, self.ink = QUEUE.get(queue, NEUTRAL)
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
        # The whole control is the target. Qt's default click region comes from
        # the style's own indicator and label rects, and those no longer line up
        # with anything now that paintEvent draws the control itself -- clicks
        # near the box quietly did nothing.
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()

        box = QRect(2, (self.height() - self.SIDE) // 2, self.SIDE, self.SIDE)
        p.setPen(QPen(QColor(self.ink if on else LINE), 1))
        p.setBrush(QColor(self.tint) if on else QColor(SURFACE))
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

        p.setPen(QColor(self.ink if on else MUTED))
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

        Background is the priority band, so a card carries its band with it --
        across a drag, and down a long board where the header has scrolled off.
        The queue keeps the left stripe and the tag chip, so a card still says
        both things at once.
        """
        stripe = QUEUE.get(self.data.get("queue") or "", NEUTRAL)[0]
        tint, edge = BAND_CARD.get(self.data.get("priority") or "",
                                   BAND_CARD["low"])
        if self.problem:
            # An unreadable thread outranks its priority: 2px, so it reads as
            # outlined next to a critical card that is merely red.
            self.setStyleSheet(
                f"Card {{ background:{RED_BG}; border:2px solid {RED_EDGE};"
                f" border-left:4px solid {RED_EDGE}; }}")
        elif self.editing:
            self.setStyleSheet(
                f"Card {{ background:{tint}; border:1px solid {ACCENT};"
                f" border-left:3px solid {ACCENT}; }}")
        else:
            self.setStyleSheet(
                f"Card {{ background:{tint}; border:1px solid {edge};"
                f" border-left:3px solid {stripe}; }}")

    def _clear(self):
        # These belong to the view and are about to be deleted. Dropping the
        # references keeps set_writable from reaching a deleted C++ object.
        self.edit_btn = self.done_btn = self.work = None

        def drop(w):
            # Unparent before deleteLater. Deletion waits for the next trip
            # round the event loop, and until then a widget that is still a
            # child of the card goes on painting where it last sat -- the old
            # view showing through the editor for a frame.
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
        cbg, cfg = QUEUE.get(d.get("queue") or "", NEUTRAL)[1:]

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(chip(d.get("queue") or "\u2014", cbg, cfg))

        client = ClickableLabel(d.get("client_override")
                                or d.get("client_raw") or "Unknown client")
        f = QFont()
        f.setPointSize(11)
        f.setWeight(QFont.DemiBold)
        client.setFont(f)
        client.setStyleSheet(f"color:{RED_FG if self.problem else INK};"
                             f" background:transparent;")
        client.setToolTip("Double-click to edit")
        client.doubleClicked.connect(self.enter_edit)
        head.addWidget(client)
        if d.get("client_override"):
            head.addWidget(chip("edited", "#EEEFF1", MUTED))
        head.addStretch()

        if d.get("ticket_count"):
            head.addWidget(chip(f"{d['ticket_count']} tickets", "#EEEFF1", MUTED))
        ago = QLabel(self._ago(d.get("last_human_at")))
        ago.setStyleSheet(f"color:{MUTED}; font-size:11px; background:transparent;")
        head.addWidget(ago)
        self.body.addLayout(head)

        if self.problem:
            self.body.addWidget(warning_row(
                "Couldn't read this thread's title \u2014 check the client and "
                "details, then edit to fix.", RED_FG))

        if d.get("equipment"):
            row = QHBoxLayout()
            row.setSpacing(5)
            for e in d["equipment"][:6]:
                if e["state"] == "resolved":
                    row.addWidget(chip(e["raw"], "#F2F3F4", MUTED))
                elif e["state"] == "pending":
                    row.addWidget(chip(e["raw"], AMBER_BG, AMBER_FG, dashed=True))
                else:
                    row.addWidget(chip(e["raw"], RED_BG, RED_FG))
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
            work.setStyleSheet(f"color:{MUTED}; font-size:12px;"
                               f" background:transparent;")
            work.setToolTip("Double-click to edit")
            work.doubleClicked.connect(self.enter_edit)
            self.body.addWidget(work)

        foot = QHBoxLayout()
        foot.setSpacing(16)
        foot.addStretch()
        for issue in (d.get("issues") or [])[:2]:
            if issue not in BLOCKING:
                foot.addWidget(chip(issue.replace("_", " "), AMBER_BG, AMBER_FG))

        self.edit_btn = QPushButton("Edit")
        self.edit_btn.setStyleSheet(BTN_HIT)
        self.edit_btn.clicked.connect(self.enter_edit)
        foot.addWidget(self.edit_btn)

        self.done_btn = QPushButton("Complete")
        self.done_btn.setStyleSheet(BTN_HIT)
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
        # What the server held when this editor opened. Saving sends it back so
        # the server can tell a field this person changed from one they merely
        # had on screen, and refuse to silently clobber somebody else's work.
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
        self.f_client.setStyleSheet(FIELD)
        self.f_work = WorkBar(d.get("work_items") or [], editing=True)

        self.f_title = QLineEdit(d.get("name") or "")
        self.f_title.setStyleSheet(FIELD)
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
        for q in ex.QUEUES:
            self.f_queue.addItem(q, q)
        self.f_queue.setCurrentIndex(
            max(self.f_queue.findData(d.get("queue") or ""), 0))

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
        # background, which is a pale block on a card that now carries its
        # queue's colour.
        for i in range(form.rowCount()):
            lab = form.itemAt(i, QFormLayout.LabelRole)
            if lab is not None and lab.widget() is not None:
                lab.widget().setStyleSheet("background:transparent;")
        self.body.addLayout(form)

        note = QLabel("Saving posts one update to the thread, however many "
                      "fields you change. Changing the title renames the "
                      "Discord thread.")
        note.setStyleSheet(f"color:{MUTED}; font-size:11px;"
                           f" background:transparent;")
        self.body.addWidget(note)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(BTN_HIT)
        cancel.clicked.connect(self.exit_edit)
        row.addWidget(cancel)
        save = QPushButton("Save")
        save.setStyleSheet(BTN_HIT + f"background:{ACCENT}; color:white;"
                                     f" border:none;")
        save.clicked.connect(self.save)
        row.addWidget(save)
        self.body.addLayout(row)
        plain_cursors(self)

    def _title_edited(self, _text):
        self._title_touched = True

    def _suggest_title(self, _text=None):
        """Keep the title in step with the client, until someone types in it.

        Work items deliberately have no say here. They are notes about what is
        happening on a thread -- added, ticked off and dropped all day -- and
        the thread title is not a running commentary on them. Renaming a
        Discord thread every time somebody wrote down a detail was noise for
        everyone watching the thread.
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
            # fields. Today's date is a starting point, not a claim -- it sits
            # in an editable box the person still has to save.
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
                f"<span style='color:{OK_FG}'>✓</span> "
                f"<span style='color:{MUTED}'>{t.queue} &middot; {t.client_raw} "
                f"&middot; {title_stamp(t.date)} &middot; {t.summary or ''}</span>")
        elif t.confidence == "prefix_only":
            self.title_state.setText(
                f"<span style='color:{AMBER_FG}'>⚠ no date Ernie can read</span> "
                f"<span style='color:{MUTED}'>&mdash; tag {t.queue} is fine, "
                f"the rest won't parse</span>")
        else:
            self.title_state.setText(
                f"<span style='color:{RED_FG}'>⚠ doesn't match</span> "
                f"<span style='color:{MUTED}'>TAG: Client - 25Aug26 - "
                f"what it's about</span>")

    def warn_changed(self, msg):
        """Live notice, while the editor is open, that the card moved."""
        if not self.editing:
            return
        if getattr(self, "_warn_label", None) is None:
            self._warn_label = QWidget()
            self._warn_label.setObjectName("editWarn")
            self._warn_label.setStyleSheet(
                f"#editWarn {{ background:{AMBER_BG}; padding:6px;"
                f" border:1px solid {AMBER_FG}44; }}")
            lay = QVBoxLayout(self._warn_label)
            lay.setContentsMargins(6, 5, 6, 5)
            self.body.insertWidget(0, self._warn_label)
        while self._warn_label.layout().count():
            w = self._warn_label.layout().takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._warn_label.layout().addWidget(warning_row(msg, AMBER_FG))
        self._warn_label.show()

    def exit_edit(self):
        self.editing = False
        self.board.editing_card = None
        self._clear()
        self._paint()
        self._build_view()

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
        # Put the card back in view mode before the write. Leaving it in the
        # form left its buttons deleted, and a refresh that skipped the rebuild
        # -- which a title-only edit always does, since the title is not a card
        # field -- then called set_writable on them.
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

        tint = BAND_TINT[priority]
        accent = BAND_TEXT[priority]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- header: clickable, and styled the same way for every band --------
        self.header = ClickableWidget()
        self.header.clicked.connect(self.toggle)
        self.header.setCursor(Qt.PointingHandCursor)
        # A QWidget *subclass* paints its own background, so a stylesheet
        # background is ignored until this is set. Plain QWidget doesn't need
        # it, which is why the header lost its tint when it became clickable.
        self.header.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(8, 5, 8, 5)
        h.setSpacing(8)

        self.caret = QLabel("▾")
        # Once a QLabel carries a stylesheet it paints its palette background,
        # which is the window colour -- each of these was punching a pale
        # rectangle through the band's tint. Keep them transparent.
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
        # Scope to the header itself. A bare selector cascades into every child
        # QLabel, so the count and the hint each drew their own accent border
        # and tinted block and ran into the title.
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
        lands. Counting cards rather than layout rows, and skipping the card
        being dragged, keeps this aligned with the neighbour list -- otherwise
        every drop below the card's own row is off by one and swaps break.
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
        # Detach first, THEN measure. Computing an index with the marker still
        # in the layout and removing it afterwards shrinks the count under it.
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
        # measured a moment ago. A slot one past the end then makes Qt warn and
        # append anyway, so clamp it.
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

    The band used to be spelled out under every name -- fifty rows reading
    "critical, high, high, high, medium" down a narrow column, which is a lot
    of words for five distinct values. The row wears its band's colour instead,
    the same one the card has on the board, and the name gets the whole line.
    """

    def __init__(self, data, board):
        super().__init__()
        self.data = data
        self.board = board
        self.thread_id = data["thread_id"]
        self._press = None

        stripe = QUEUE.get(data.get("queue") or "", NEUTRAL)[0]
        tint, edge = BAND_CARD.get(data.get("priority") or "", BAND_CARD["low"])
        if needs_triage(data):
            # Outlined, like its card, so an unreadable thread stays the
            # loudest thing in the rail now that whole bands are red-ish.
            self.setStyleSheet(f"RailRow {{ background:{RED_BG};"
                               f" border:2px solid {RED_EDGE}; }}")
        else:
            self.setStyleSheet(f"RailRow {{ background:{tint};"
                               f" border:1px solid {edge};"
                               f" border-left:3px solid {stripe}; }}")
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
        name.setStyleSheet(f"color:{INK}; background:transparent;")
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

    Without one there is nothing to aim at: the rail is a flat list of rows,
    so a band with no tickets has no rows, no height, and no way to be dropped
    into -- the gap between two rules is a hairline. This is that band's slot,
    and it says which band it is, since the rows no longer do.
    """

    def __init__(self, priority):
        super().__init__()
        self.priority = priority

        # The dashed box is inset, so the rows above and below are pushed clear
        # of it rather than sitting against its border -- a slot the width of
        # the rail with 4px of air either side reads as overlapping them.
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, RAIL_ZONE_GAP, 0, RAIL_ZONE_GAP)
        lay.setSpacing(0)
        self.box = QLabel(BAND_LABEL[priority].lower())
        self.box.setAlignment(Qt.AlignCenter)
        self.box.setMinimumHeight(RAIL_ZONE_MIN)
        lay.addWidget(self.box)
        self._paint(False)

    def _paint(self, hot):
        tint, edge = BAND_CARD[self.priority]
        ink = BAND_TEXT[self.priority]
        self.box.setStyleSheet(
            f"background:{tint if hot else 'transparent'}; color:{ink};"
            f" font-size:10px; border:2px dashed {edge if hot else rgba(ink, 0.4)};"
            f" border-radius:4px;")

    def set_hot(self, hot):
        """Fill in while the pointer is over it, so the target is unambiguous."""
        self._paint(hot)


class Rail(QWidget):
    """Every open ticket on one screen, in board order.

    Fifty cards don't fit on the board at once, which turns a move into a
    scrolling exercise -- you can't see where the card is and where it's going
    at the same time. Here both ends are always visible. Click a row to jump
    the board to it, or drag the row itself.
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
        self.setStyleSheet(f"background:{CANVAS};")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 4, 8)
        outer.setSpacing(6)

        # Not "Priority" -- the bands are the priorities, so that name reads
        # like a filter. This is the order the work gets worked, which is also
        # the reason to drag anything in here.
        self.head = QLabel("Running order")
        f = QFont()
        f.setPointSize(10)
        f.setWeight(QFont.DemiBold)
        self.head.setFont(f)
        self.head.setStyleSheet(f"color:{INK}; background:transparent;")

        self.fold_btn = QPushButton("\u00ab")
        self.fold_btn.setFixedSize(24, 24)
        self.fold_btn.setCursor(Qt.PointingHandCursor)
        self.fold_btn.setToolTip("Hide the running order")
        self.fold_btn.setStyleSheet(
            f"QPushButton {{ border:1px solid {LINE}; border-radius:3px;"
            f" background:{SURFACE}; color:{MUTED}; font-size:11px; }}"
            f"QPushButton:hover {{ background:#EAF1FA; color:{ACCENT}; }}")
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
        self.hint.setStyleSheet(f"color:{MUTED}; font-size:10px;"
                                f" background:transparent;")
        outer.addWidget(self.hint)

        self.marker = QFrame(inner)
        self.marker.setFixedHeight(3)
        self.marker.setStyleSheet(f"background:{ACCENT};")
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
        # the button drifts to the middle of the spine. Hold it at the top so
        # it doesn't move when you fold and unfold.
        lay = self.layout()
        if yes and self._spacer is None:
            lay.addStretch(1)
            self._spacer = lay.itemAt(lay.count() - 1)
        elif not yes and self._spacer is not None:
            lay.removeItem(self._spacer)
            self._spacer = None

    def set_cards(self, cards):
        # Same signature check the bands use: redrawing 50 rows on every poll
        # for identical content is wasted work. The drag state is part of it --
        # empty bands take a slot in the rail while a drag is running.
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
                # Unparent first -- see Band.set_cards. This is the one that
                # showed: the rail rebuilds as a drag starts, to make room for
                # the empty bands' slots, and every old row stayed painted
                # where it was with the new ones drawn over the top.
                w.setParent(None)
                w.deleteLater()
        self.marker.hide()

        # Walk the bands rather than the cards, so a band with nothing in it
        # still gets its turn. Card order inside a band is left as the board
        # sent it -- unassigned floats its unreadable threads to the top.
        first = True
        for band in BANDS:
            group = [c for c in self.cards if c["priority"] == band]
            if not group and not self.board.dragging:
                continue
            if not first:
                # Where the next band starts. This carries more than it did,
                # now that the rows no longer spell their band out, so it is a
                # solid rule in the new band's ink rather than a hairline.
                line = QFrame()
                line.setFixedHeight(2)
                line.setStyleSheet(
                    f"background:{rgba(BAND_TEXT[band], 0.55)}; border:none;")
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

        Read off the row the pointer is actually over and which half of it,
        rather than off "the row above the gap". That older rule made the top
        of a band unreachable: the space above a band's first row still
        resolved to the band before it, so a card dropped there joined the
        bottom of the previous band, and a card dropped a few pixels lower
        went in second. Half a row is a real target; the seam between two was
        not.
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
        self._pending = None        # a poll that landed mid-drag, held back
        self.editing_card = None
        self.poller = None
        self.connected = True
        self.last_sync = None
        self.filters = {q: True for q in QUEUE}
        self.cards = []
        self.feed = []
        self.completing = set()     # closed here, still drawn on the board

        self.setWindowTitle("Bert")
        if LOGO.exists():
            self.setWindowIcon(QIcon(str(LOGO)))
        self.resize(1000, 840)
        self.setStyleSheet(f"QMainWindow {{ background:{CANVAS}; }}")

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
            f"background:{INFO_BG}; color:{INFO_FG}; padding:7px; font-size:12px;")
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
        pal.setColor(QPalette.Window, QColor(BESIDE))
        vp.setPalette(pal)
        # Widget smaller than the viewport: pin it left, don't centre it.
        self.scroll.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        board = QWidget()
        board.setObjectName("boardColumn")
        # The column keeps the canvas colour and stops where the bands stop,
        # with an edge to say so. Scoped to the id: a bare selector would hand
        # the same background to every band and card under it.
        board.setStyleSheet(f"#boardColumn {{ background:{CANVAS};"
                            f" border-right:1px solid {LINE}; }}")
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

        QTimer.singleShot(0, self.refresh)
        if not self.name():
            QTimer.singleShot(300, self.open_settings)

    def _toolbar(self):
        bar = QWidget()
        bar.setStyleSheet(f"background:{SURFACE}; border-bottom:1px solid {LINE};")
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
        title.setStyleSheet(f"color:{INK}; background:transparent;")
        lay.addWidget(title)

        self.count = QLabel("")
        self.count.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        lay.addWidget(self.count)
        lay.addSpacing(10)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search client, equipment, summary")
        self.search.setFixedWidth(230)
        self.search.textChanged.connect(self.render)
        lay.addWidget(self.search)

        for q in QUEUE:
            cb = QueueBox(q)
            cb.setChecked(True)
            cb.stateChanged.connect(
                lambda s, k=q: (self.filters.__setitem__(k, bool(s)), self.render()))
            lay.addWidget(cb)

        lay.addStretch()

        self.fresh = QLabel("")
        self.fresh.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        lay.addWidget(self.fresh)

        self.refresh_btn = QPushButton("\u21bb  Refresh")
        self.refresh_btn.setStyleSheet(BTN_HIT)
        self.refresh_btn.clicked.connect(self.refresh)
        lay.addWidget(self.refresh_btn)

        self.who = QLabel("")
        lay.addWidget(self.who)
        gear = QPushButton("\u2699")
        gear.setFixedSize(30, 28)
        gear.setCursor(Qt.PointingHandCursor)
        gear.setToolTip("Settings")
        # 14px: the cog's glyph box is 19px tall against a 28px button, so it
        # has the room the caution sign didn't.
        gear.setStyleSheet("font-size:14px;")
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
        w.setStyleSheet(f"#feedPanel {{ background:{SURFACE};"
                        f" border-top:1px solid {LINE}; }}")
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
        self.feed_caret.setStyleSheet(f"color:{MUTED}; font-size:11px;"
                                      f" background:transparent;")
        lab = QLabel("Recent activity")
        lab.setStyleSheet(f"color:{MUTED}; font-size:11px;"
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
        self.feed_lay = QVBoxLayout()
        self.feed_lay.setSpacing(3)
        body.addLayout(self.feed_lay)
        body.addStretch()
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
        body = self.feed_body.layout()
        body.invalidate()
        body.activate()

        # Hold the full four rows open even when fewer have come in, at the
        # height of the tallest row actually on screen -- a row with an Undo
        # button is taller than one without. Sizing to just what is there sees
        # the panel, and the whole board above it, jump every time the mix of
        # rows changes.
        rows = [self.feed_lay.itemAt(i).widget()
                for i in range(self.feed_lay.count())]
        tallest = max((r.sizeHint().height() for r in rows if r is not None),
                      default=0)
        # Remembered, so a moment with an empty feed -- a fresh database, or
        # every row undone -- doesn't collapse the panel and bounce the board.
        self._feed_row_h = max(getattr(self, "_feed_row_h", 0), tallest)
        reserve = (FEED_ROWS * self._feed_row_h
                   + (FEED_ROWS - 1) * self.feed_lay.spacing()
                   if self._feed_row_h else 0)

        m = self.feed_panel.layout().contentsMargins()
        self.feed_panel.setFixedHeight(
            m.top() + m.bottom() + self.feed_panel.layout().spacing()
            + self.feed_head.sizeHint().height()
            + max(body.sizeHint().height(), reserve))

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
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec() == QDialog.Accepted:
            self.settings.update(dlg.values())
            # Don't leave the old pair behind to be read back later.
            self.settings.pop("first_name", None)
            self.settings.pop("last_name", None)
            SETTINGS.write_text(json.dumps(self.settings, indent=2))
            self.render()

    # -- polling -----------------------------------------------------------

    def refresh(self):
        if self.dragging:
            return                      # never yank the board out from under a drag
        # One poll at a time. Reassigning self.poller while the last one is
        # still running drops its only reference and Qt tears the thread down
        # underneath itself -- "QThread: Destroyed while thread is running".
        if self.poller is not None and self.poller.isRunning():
            return
        self.refresh_btn.setText("\u21bb  ...")
        self.poller = Poller(self.api)
        self.poller.loaded.connect(self.on_loaded)
        self.poller.failed.connect(self.on_failed)
        self.poller.start()

    def on_loaded(self, p):
        self.fail_since = None
        self.connected = True
        self.last_sync = time.time()
        self.banner.hide()
        self.refresh_btn.setText("\u21bb  Refresh")
        incoming = p["board"]["cards"]
        self.feed = p["events"]["events"]

        # The card outlives the click by a poll or two; drop the toast the
        # moment it is genuinely off the board rather than on a timer.
        if self.completing and not self.completing.intersection(
                c["thread_id"] for c in incoming):
            self._clear_toast()

        # An open editor must not be redrawn out from under someone mid-sentence,
        # but they should still hear that the card moved. Warn, don't redraw.
        if self.editing_card:
            self._flag_edited_underneath(incoming)
            return

        # A poll that set off before the drag began still lands in the middle
        # of one. Rendering here rebuilds the bands, which deletes the very
        # Card widget Qt is dragging -- and Qt takes the process down with it,
        # because the drag it is running holds that widget as its source. Hold
        # the payload until the drag lets go.
        if self.dragging:
            self._pending = p
            return

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
        self.refresh_btn.setText("\u21bb  Refresh")
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
                f"background:{AMBER_BG}; color:{AMBER_FG}; padding:7px; font-size:12px;")
        else:
            self.connected = False
            self.banner.setText("Can't reach Ernie. Showing the last known board "
                                "\u2014 changes are paused until it's back.")
            self.banner.setStyleSheet(
                f"background:{RED_BG}; color:{RED_FG}; padding:7px; font-size:12px;")
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

    def _tick_freshness(self):
        if self.last_sync is None:
            self.fresh.setText("never updated")
            return
        s = int(time.time() - self.last_sync)
        if s < 5:
            txt = "updated just now"
        elif s < 60:
            txt = f"updated {s}s ago"
        elif s < 3600:
            txt = f"updated {s // 60}m ago"
        else:
            txt = f"updated {s // 3600}h ago"
        self.fresh.setText(txt)
        self.fresh.setStyleSheet(
            f"color:{AMBER_FG if s > 90 else MUTED}; font-size:11px;")

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
        # Show the move now. The POST plus a full board reload is slow enough
        # that the card otherwise sits in its old slot looking like the drag
        # didn't take. The refresh below replaces these ranks with the server's.
        self._reorder_local(tid, priority, after, before)
        # This runs from dropEvent, which is inside drag.exec(). Redrawing here
        # would delete the widget being dragged out from under Qt; end_drag()
        # repaints the board the instant the drag returns.
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
        # Whatever the poll brought in while the drag was running, applied now
        # that redrawing is safe again.
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

    def render(self):
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
            f"<span style='color:{RED_FG}'>{problems} need attention</span>"
            if problems else "")

        # Collect the bands in the order they're drawn, because that is not the
        # order the server sent: unassigned floats its unreadable threads to the
        # top. The rail has to be the same list top to bottom -- it looked wrong
        # side by side, and its drop targets are read off adjacency, so a
        # different order there means the wrong neighbours get sent.
        ordered = []
        for band, w in self.bands.items():
            group = [c for c in shown if c["priority"] == band]
            if band == "unassigned":
                # Unreadable threads float to the top so they get triaged first.
                group.sort(key=lambda c: (not needs_triage(c), c["rank"]))
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
            self.who.setStyleSheet(f"color:{AMBER_FG}; font-size:12px;")
        else:
            # Your own name told you nothing you didn't know. It only earns
            # toolbar space when it's missing, which blocks every write.
            self.who.setText("")

        self._render_feed()

    def _render_feed(self):
        while self.feed_lay.count():
            w = self.feed_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        for e in self.feed[:FEED_ROWS]:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)

            when = QLabel(self._clock(e["occurred_at"]))
            when.setFixedWidth(60)
            when.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            h.addWidget(when)

            txt = QLabel(self._feed_text(e))
            txt.setStyleSheet(f"color:{INK}; font-size:12px;")
            h.addWidget(txt)
            h.addStretch()

            undoable = e["verb"] in ("completed", "priority_changed", "edited",
                                     "work_done")
            if undoable and not e["undone_at"]:
                b = QPushButton("\u21b6  Undo")
                b.setCursor(Qt.PointingHandCursor)
                b.setStyleSheet(
                    f"QPushButton {{ {BTN_HIT}"
                    f" border:1px solid {ACCENT}; border-radius:3px;"
                    f" color:{ACCENT}; background:{SURFACE}; }}"
                    f"QPushButton:hover {{ background:#EAF1FA; }}"
                    f"QPushButton:disabled {{ color:{MUTED}; border-color:{LINE}; }}")
                b.setEnabled(self.writable())
                b.clicked.connect(lambda _, i=e["event_id"]: self.undo(i))
                h.addWidget(b)
            elif e["undone_at"]:
                h.addWidget(chip("undone", "#EEEFF1", MUTED))

            self.feed_lay.addWidget(row)
            # Qt reports a widget that has not been shown yet as zero-sized,
            # and _fit_feed() measures these a moment from now -- without this
            # it sizes the panel for an empty feed and squashes every row.
            row.show()

        self._fit_feed()

    @staticmethod
    def _feed_text(e):
        """One line of the activity feed.

        A priority change carries the two bands it went between -- "priority
        changed" on its own said that something moved but not where to, which
        is the only part worth reading. Each band is in its own colour, the
        same one it wears on the board.
        """
        who = e.get("actor_name") or "Ernie"
        what = (e.get("thread_name") or "")[:46]
        thread = f"<span style='color:{MUTED}'>{what}</span>"

        old, new = e.get("old_value"), e.get("new_value")
        if e["verb"] == "priority_changed" and old in BANDS and new in BANDS:
            def band(b):
                return (f"<b style='color:{BAND_TEXT[b]}'>"
                        f"{BAND_LABEL[b]}</b>")
            # A middot before the bands: thread names end in a date, and
            # "5June26 High" ran together into one thing to read.
            return (f"<b>{who}</b> moved {thread}"
                    f"<span style='color:{LINE}'> &middot; </span>"
                    f"{band(old)} <span style='color:{MUTED}'>&#8594;</span> "
                    f"{band(new)}")

        if e["verb"] == "work_done" and (new or "").strip():
            # Which item, not just which thread -- the text is right there on
            # the event, and "work done" alone was the same gap the priority
            # rows had. Clipped so a long item can't push the Undo button off
            # the end of the row.
            item = new.strip()
            if len(item) > 44:
                item = item[:43].rstrip() + "…"
            return (f"<b>{who}</b> finished {thread}"
                    f"<span style='color:{LINE}'> &middot; </span>"
                    f"<b>{item}</b>")

        return f"<b>{who}</b> {e['verb'].replace('_', ' ')} {thread}"

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
    app.setStyle("Fusion")
    pal = app.palette()
    pal.setColor(QPalette.Window, QColor(CANVAS))
    app.setPalette(pal)
    w = Bert(a.api)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

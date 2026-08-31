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
from PySide6.QtCore import QMimeData, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QDrag, QFont, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

SETTINGS = pathlib.Path.home() / ".bert.json"
POLL_MS = 5_000       # a poll that changes nothing now costs <1ms to render
DEGRADED_S, BLOCKED_S = 5, 15
DRAG_THRESHOLD = 5
DROP_ZONE_MIN = 44          # empty bands stay this tall during a drag
EDGE_SCROLL_ZONE = 64       # holding a card this close to the edge scrolls
EDGE_SCROLL_MAX = 22        # px per tick right at the edge
EDGE_SCROLL_MS = 16

BANDS = ["unassigned", "critical", "high", "medium", "low"]
BAND_LABEL = {b: b.capitalize() for b in BANDS}

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
AMBER_BG, AMBER_FG = "#FCF3E2", "#8A5A08"
RED_BG, RED_FG, RED_EDGE = "#FBEBEB", "#9B2C2C", "#D14343"
OK_FG, ACCENT = "#2E6B34", "#2B6CB0"
UNASSIGNED_TINT = "#EEF2F8"

# A light wash behind each band's cards. The cards themselves stay white, so
# the tint reads as the container the tickets are sitting in.
BAND_TINT = {
    "unassigned": "#F7EEEE",
    "critical":   "#F6E4E4",
    "high":       "#FBF1DF",
    "medium":     "#EAF1FA",
    "low":        "#EFF1F2",
}
# Unassigned reads red because it is the triage queue; the real priorities
# read blue.
BAND_TEXT = dict.fromkeys(BANDS, ACCENT)
# Unassigned and Critical read red so the heading matches the wash behind them.
BAND_TEXT["unassigned"] = RED_FG
BAND_TEXT["critical"] = RED_FG

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

FIELD_LABELS = {"client_override": "the client", "action_item": "the work item",
                "build_state": "the build ticket", "return_state": "the return ticket",
                "direction": "the equipment direction", "title": "the thread title"}


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
        self.first = QLineEdit(current.get("first_name", ""))
        self.last = QLineEdit(current.get("last_name", ""))
        form = QFormLayout()
        form.addRow("First name", self.first)
        form.addRow("Last name", self.last)
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
        return {"first_name": self.first.text().strip(),
                "last_name": self.last.text().strip()}


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


class Card(QFrame):
    def __init__(self, data, board):
        super().__init__()
        self.data = data
        self.board = board
        self.thread_id = data["thread_id"]
        self.editing = False
        self._press = None
        self.problem = needs_triage(data)
        self.edit_btn = self.done_btn = None

        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(12, 10, 12, 10)
        self.body.setSpacing(7)
        self._paint()
        self._build_view()

    def _paint(self):
        stripe = QUEUE.get(self.data.get("queue") or "", NEUTRAL)[0]
        if self.problem:
            self.setStyleSheet(
                f"Card {{ background:{RED_BG}; border:1px solid {RED_EDGE};"
                f" border-left:3px solid {RED_EDGE}; }}")
        elif self.editing:
            self.setStyleSheet(
                f"Card {{ background:{SURFACE}; border:1px solid {ACCENT};"
                f" border-left:3px solid {stripe}; }}")
        else:
            self.setStyleSheet(
                f"Card {{ background:{SURFACE}; border:1px solid {LINE};"
                f" border-left:3px solid {stripe}; }}")

    def _clear(self):
        # These belong to the view and are about to be deleted. Dropping the
        # references keeps set_writable from reaching a deleted C++ object.
        self.edit_btn = self.done_btn = None
        while self.body.count():
            it = self.body.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                while it.layout().count():
                    sub = it.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

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
        client.setStyleSheet(f"color:{RED_FG if self.problem else INK};")
        client.setToolTip("Double-click to edit")
        client.doubleClicked.connect(self.enter_edit)
        head.addWidget(client)
        if d.get("client_override"):
            head.addWidget(chip("edited", "#EEEFF1", MUTED))
        head.addStretch()

        if d.get("ticket_count"):
            head.addWidget(chip(f"{d['ticket_count']} tickets", "#EEEFF1", MUTED))
        ago = QLabel(self._ago(d.get("last_human_at")))
        ago.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        head.addWidget(ago)
        self.body.addLayout(head)

        if self.problem:
            warn = QLabel("\u26a0  Couldn't read this thread's title \u2014 "
                          "check the client and details, then edit to fix.")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color:{RED_FG}; font-size:11px;")
            self.body.addWidget(warn)

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

        work = ClickableLabel(d.get("action_item")
                              or d.get("summary") or d.get("name") or "")
        work.setWordWrap(True)
        work.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        work.setToolTip("Double-click to edit")
        work.doubleClicked.connect(self.enter_edit)
        self.body.addWidget(work)

        foot = QHBoxLayout()
        foot.setSpacing(16)
        for label, key in (("Build", "build_state"), ("Return", "return_state")):
            foot.addWidget(self._state_label(label, d.get(key)))
        if d.get("direction"):
            foot.addWidget(chip(dict(DIRECTIONS).get(d["direction"], ""),
                                "#EEEFF1", MUTED))
        foot.addStretch()
        for issue in (d.get("issues") or [])[:2]:
            if issue not in BLOCKING:
                foot.addWidget(chip(issue.replace("_", " "), AMBER_BG, AMBER_FG))

        self.edit_btn = QPushButton("Edit")
        self.edit_btn.setStyleSheet("font-size:11px; padding:3px 10px;")
        self.edit_btn.clicked.connect(self.enter_edit)
        foot.addWidget(self.edit_btn)

        self.done_btn = QPushButton("Complete")
        self.done_btn.setStyleSheet("font-size:11px; padding:3px 10px;")
        self.done_btn.clicked.connect(lambda: self.board.complete(self.thread_id))
        foot.addWidget(self.done_btn)
        self.body.addLayout(foot)
        self.set_writable(self.board.writable())

    # -- edit mode ---------------------------------------------------------

    def enter_edit(self):
        if not self.board.writable() or self.editing:
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
                           ("client_override", "action_item", "build_state",
                            "return_state", "direction")}
        # The thread title lives on Discord, not on the card, so it travels
        # under its own key and is compared against the card's "name".
        self._edit_base["title"] = d.get("name") or ""
        self._title_touched = False
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)

        self.f_client = QLineEdit(d.get("client_override") or d.get("client_raw") or "")
        self.f_work = QLineEdit(d.get("action_item") or d.get("summary") or "")

        self.f_build = QComboBox()
        self.f_return = QComboBox()
        for combo, cur in ((self.f_build, d.get("build_state")),
                           (self.f_return, d.get("return_state"))):
            for val, lab in STATES:
                combo.addItem(lab, val)
            i = combo.findData(cur or "needs_created")
            combo.setCurrentIndex(max(i, 0))

        self.f_dir = QComboBox()
        for val, lab in DIRECTIONS:
            self.f_dir.addItem(lab, val)
        self.f_dir.setCurrentIndex(max(self.f_dir.findData(d.get("direction") or ""), 0))

        self.f_title = QLineEdit(d.get("name") or "")
        self.title_state = QLabel()
        self.title_state.setWordWrap(True)
        self.title_state.setStyleSheet("font-size:11px;")
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        title_box.addWidget(self.f_title)
        title_box.addWidget(self.title_state)
        title_holder = QWidget()
        title_holder.setLayout(title_box)

        self.f_queue = QComboBox()
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
        self.f_work.textChanged.connect(self._suggest_title)
        self._check_title()

        form.addRow("Thread title", title_holder)
        form.addRow("Queue", self.f_queue)
        form.addRow("Client", self.f_client)
        form.addRow("Current work item", self.f_work)
        form.addRow("Build ticket", self.f_build)
        form.addRow("Return ticket", self.f_return)
        form.addRow("Equipment", self.f_dir)
        self.body.addLayout(form)

        note = QLabel("Saving posts one update to the thread, however many "
                      "fields you change. Changing the title renames the "
                      "Discord thread.")
        note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        self.body.addWidget(note)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet("font-size:11px; padding:4px 12px;")
        cancel.clicked.connect(self.exit_edit)
        row.addWidget(cancel)
        save = QPushButton("Save")
        save.setStyleSheet(f"font-size:11px; padding:4px 14px; "
                           f"background:{ACCENT}; color:white; border:none;")
        save.clicked.connect(self.save)
        row.addWidget(save)
        self.body.addLayout(row)

    def _title_edited(self, _text):
        self._title_touched = True

    def _suggest_title(self, _text=None):
        """Keep the title in step with the fields, until someone types in it."""
        if getattr(self, "_title_touched", True):
            return
        # Rebuild from whatever the box holds now, not from the stored title:
        # otherwise a queue just chosen from the dropdown gets overwritten the
        # moment the client is edited.
        t = ex.parse_title(self.f_title.text().strip())
        if t.confidence not in ("strict", "loose"):
            return                  # nothing dependable to rebuild from
        client = self.f_client.text().strip() or t.client_raw or ""
        summary = self.f_work.text().strip() or t.summary or ""
        self.f_title.setText(
            f"{t.queue}: {client} - {title_stamp(t.date)} - {summary}")

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
            work = self.f_work.text().strip() or "what it's about"
            today = datetime.now(timezone.utc).date()
            self.f_title.setText(f"{q}: {client} - {title_stamp(today)} - {work}")

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
                f"<span style='color:{MUTED}'>&mdash; queue {t.queue} is fine, "
                f"the rest won't parse</span>")
        else:
            self.title_state.setText(
                f"<span style='color:{RED_FG}'>⚠ doesn't match</span> "
                f"<span style='color:{MUTED}'>QUEUE: Client - 25Aug26 - "
                f"what it's about</span>")

    def warn_changed(self, msg):
        """Live notice, while the editor is open, that the card moved."""
        if not self.editing:
            return
        if getattr(self, "_warn_label", None) is None:
            self._warn_label = QLabel()
            self._warn_label.setWordWrap(True)
            self._warn_label.setStyleSheet(
                f"background:{AMBER_BG}; color:{AMBER_FG}; padding:6px;"
                f" font-size:11px; border:1px solid {AMBER_FG}44;")
            self.body.insertWidget(0, self._warn_label)
        self._warn_label.setText("⚠  " + msg)
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
            "action_item": self.f_work.text().strip(),
            "build_state": self.f_build.currentData(),
            "return_state": self.f_return.currentData(),
            "direction": self.f_dir.currentData(),
        }
        base = getattr(self, "_edit_base", None)
        # Put the card back in view mode before the write. Leaving it in the
        # form left its buttons deleted, and a refresh that skipped the rebuild
        # -- which a title-only edit always does, since the title is not a card
        # field -- then called set_writable on them.
        self.exit_edit()
        self.board.save_edits(self.thread_id, fields, base)

    def set_writable(self, ok):
        if self.done_btn is not None:        # None while the editor is open
            self.done_btn.setEnabled(ok)
            self.edit_btn.setEnabled(ok)
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
        drag.exec(Qt.MoveAction)
        self.board.end_drag()
        self._press = None

    def mouseReleaseEvent(self, e):
        self._press = None

    def _state_label(self, name, value):
        text = dict(STATES).get(value or "needs_created", "Needs created")
        colour = {"created": OK_FG, "not_needed": MUTED}.get(value or "", AMBER_FG)
        # A created ticket gets a tick in its own queue's colour, so PROD, OPS,
        # ENG and CS stay tellable apart without reading the chip.
        tick = ""
        if value == "created":
            q = QUEUE.get(self.data.get("queue") or "", NEUTRAL)[0]
            tick = f"<span style='color:{q}'>✓</span> "
        lab = QLabel(f"{name} {tick}<b style='color:{colour}'>{text}</b>")
        lab.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        return lab

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
            if w is not None and w is not self.marker:
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
        # During a drag every band stays visible and tall enough to drop into,
        # otherwise an empty band is a 20px sliver you can't hit.
        if self.board.dragging:
            self.setVisible(True)
            self.setMinimumHeight(DROP_ZONE_MIN if not self.cards else 0)
        else:
            self.setMinimumHeight(0)
            self.setVisible(bool(self.cards) or self.priority == "unassigned")

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


class Bert(QMainWindow):
    def __init__(self, api_base):
        super().__init__()
        self.api = Api(api_base)
        self.settings = load_settings()
        self.fail_since = None
        self.dragging = False
        self.editing_card = None
        self.poller = None
        self.connected = True
        self.last_sync = None
        self.filters = {q: True for q in QUEUE}
        self.cards = []
        self.feed = []

        self.setWindowTitle("Bert")
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
        outer.addWidget(self._toolbar())

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(f"background:{CANVAS};")
        board = QWidget()
        self.board_lay = QVBoxLayout(board)
        self.board_lay.setContentsMargins(16, 10, 16, 24)
        self.board_lay.setSpacing(16)

        self.bands = {}
        for b in BANDS:
            self.bands[b] = Band(b, self)
            self.board_lay.addWidget(self.bands[b])
        self.board_lay.addStretch()
        self.scroll.setWidget(board)
        outer.addWidget(self.scroll, 1)
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

        title = QLabel("Needs done")
        f = QFont()
        f.setPointSize(14)
        f.setWeight(QFont.DemiBold)
        title.setFont(f)
        title.setStyleSheet(f"color:{INK};")
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
            cb = QCheckBox(q)
            cb.setChecked(True)
            cb.stateChanged.connect(
                lambda s, k=q: (self.filters.__setitem__(k, bool(s)), self.render()))
            lay.addWidget(cb)

        lay.addStretch()

        self.fresh = QLabel("")
        self.fresh.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        lay.addWidget(self.fresh)

        self.refresh_btn = QPushButton("\u21bb  Refresh")
        self.refresh_btn.setStyleSheet("font-size:11px; padding:4px 11px;")
        self.refresh_btn.clicked.connect(self.refresh)
        lay.addWidget(self.refresh_btn)

        self.who = QLabel("")
        lay.addWidget(self.who)
        gear = QPushButton("Settings")
        gear.clicked.connect(self.open_settings)
        lay.addWidget(gear)
        return bar

    def _feed_panel(self):
        w = QWidget()
        w.setStyleSheet(f"background:{SURFACE}; border-top:1px solid {LINE};")
        w.setFixedHeight(136)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(4)
        lab = QLabel("Recent activity")
        lab.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        lay.addWidget(lab)
        self.feed_lay = QVBoxLayout()
        self.feed_lay.setSpacing(3)
        lay.addLayout(self.feed_lay)
        lay.addStretch()
        return w

    # -- identity ----------------------------------------------------------

    def name(self):
        s = self.settings
        return f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()

    def writable(self):
        return bool(self.name()) and self.connected

    def open_settings(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec() == QDialog.Accepted:
            self.settings.update(dlg.values())
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

        # An open editor must not be redrawn out from under someone mid-sentence,
        # but they should still hear that the card moved. Warn, don't redraw.
        if self.editing_card:
            self._flag_edited_underneath(incoming)
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
                                    "Add your first and last name in Settings "
                                    "before making changes.")
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
        a, b = by_id.get(after), by_id.get(before)
        if a is not None and b is not None:
            card["rank"] = (a["rank"] + b["rank"]) / 2
        elif a is not None:
            card["rank"] = a["rank"] + 1.0
        elif b is not None:
            card["rank"] = b["rank"] - 1.0
        else:
            card["rank"] = 0.0
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

    def complete(self, tid):
        if not self._guard():
            return
        try:
            self.api.complete(tid, self.name())
        except Conflict as e:
            d = e.detail
            QMessageBox.information(
                self, "Already closed",
                f"{d.get('message', 'Someone already closed this.')}\n\n"
                f"{moments_ago(d.get('at'))}".strip())
        except Exception as e:
            QMessageBox.warning(self, "Couldn't complete that card", str(e))
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

    def priority_of(self, tid):
        for c in self.cards:
            if c["thread_id"] == tid:
                return c["priority"]
        return None

    def begin_drag(self):
        self.dragging = True
        for b in self.bands.values():
            b.set_cards(b.cards)
        self.edge_timer.start()

    def end_drag(self):
        self.edge_timer.stop()
        self.dragging = False
        for b in self.bands.values():
            b.marker.hide()
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

        vp = self.scroll.viewport()
        p = vp.mapFromGlobal(QCursor.pos())
        # Ignore the pointer once it's wandered off the board, so a drag taken
        # somewhere else entirely doesn't leave the view scrolling on its own.
        if not (0 <= p.x() <= vp.width()):
            return
        if not (-2 * EDGE_SCROLL_ZONE <= p.y() <= vp.height() + 2 * EDGE_SCROLL_ZONE):
            return

        bar = self.scroll.verticalScrollBar()
        if p.y() < EDGE_SCROLL_ZONE:
            bar.setValue(bar.value() - self._edge_step(EDGE_SCROLL_ZONE - max(p.y(), 0)))
        elif p.y() > vp.height() - EDGE_SCROLL_ZONE:
            bar.setValue(bar.value()
                         + self._edge_step(EDGE_SCROLL_ZONE
                                           - max(vp.height() - p.y(), 0)))

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
                                "summary", "action_item")).lower()
                hay += " ".join(e["raw"] for e in (c.get("equipment") or [])).lower()
                return term in hay
            return True

        shown = [c for c in self.cards if keep(c)]
        problems = sum(1 for c in shown if needs_triage(c))
        attention = (f"  &middot;  <span style='color:{RED_FG}'>"
                     f"{problems} need attention</span>") if problems else ""
        self.count.setText(f"{len(shown)} open{attention}")

        for band, w in self.bands.items():
            group = [c for c in shown if c["priority"] == band]
            if band == "unassigned":
                # Unreadable threads float to the top so they get triaged first.
                group.sort(key=lambda c: (not needs_triage(c), c["rank"]))
            w.set_cards(group)

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
            self.who.setText(self.name())
            self.who.setStyleSheet(f"color:{MUTED}; font-size:12px;")

        self._render_feed()

    def _render_feed(self):
        while self.feed_lay.count():
            it = self.feed_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        for e in self.feed[:4]:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)

            when = QLabel(self._clock(e["occurred_at"]))
            when.setFixedWidth(60)
            when.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            h.addWidget(when)

            verb = e["verb"].replace("_", " ")
            what = (e.get("thread_name") or "")[:46]
            txt = QLabel(f"<b>{e.get('actor_name') or 'Ernie'}</b> {verb} "
                         f"<span style='color:{MUTED}'>{what}</span>")
            txt.setStyleSheet(f"color:{INK}; font-size:12px;")
            h.addWidget(txt)
            h.addStretch()

            undoable = e["verb"] in ("completed", "priority_changed", "edited")
            if undoable and not e["undone_at"]:
                b = QPushButton("\u21b6  Undo")
                b.setCursor(Qt.PointingHandCursor)
                b.setStyleSheet(
                    f"QPushButton {{ font-size:11px; padding:3px 12px;"
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

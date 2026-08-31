"""
Bert -- equipment ticket board.

Talks to Ernie's HTTP API. Drag cards to reprioritise, mark work complete,
undo from the activity feed. Blocks all changes if Ernie is unreachable or
if you haven't set your name.

    pip install PySide6 httpx
    python bert.py
    python bert.py --api http://ernie.local:8787
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
from PySide6.QtCore import QMimeData, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

SETTINGS = pathlib.Path.home() / ".bert.json"
POLL_MS = 15_000
DEGRADED_S, BLOCKED_S = 5, 15
DRAG_THRESHOLD = 5

BANDS = ["unassigned", "critical", "high", "medium", "low"]
BAND_LABEL = {b: b.capitalize() for b in BANDS}

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
RED_BG, RED_FG = "#FBEBEB", "#9B2C2C"
OK_FG, ACCENT = "#2E6B34", "#2B6CB0"

MIME = "application/x-bert-card"


# --------------------------------------------------------------------------
# Settings + API
# --------------------------------------------------------------------------

def load_settings() -> dict:
    if SETTINGS.exists():
        try:
            return json.loads(SETTINGS.read_text())
        except json.JSONDecodeError:
            pass
    return {}


class SettingsDialog(QDialog):
    def __init__(self, parent, current: dict):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(360)
        self.first = QLineEdit(current.get("first_name", ""))
        self.last = QLineEdit(current.get("last_name", ""))

        form = QFormLayout()
        form.addRow("First name", self.first)
        form.addRow("Last name", self.last)

        note = QLabel("Your name is added to thread updates so the team can "
                      "see who made each change. You can't make changes until "
                      "this is set.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED}; font-size:11px;")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(bb)

    def values(self) -> dict:
        return {"first_name": self.first.text().strip(),
                "last_name": self.last.text().strip()}


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=8.0)

    def board(self):
        return self.client.get(f"{self.base}/cards").json()

    def events(self, limit=8):
        return self.client.get(f"{self.base}/events",
                               params={"limit": limit}).json()

    def health(self):
        return self.client.get(f"{self.base}/health").json()

    def _post(self, path: str, payload: dict):
        payload.setdefault("key", str(uuid.uuid4()))
        r = self.client.post(f"{self.base}{path}", json=payload)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(detail)
        return r.json()

    def move(self, tid, priority, after_id, before_id, actor):
        return self._post(f"/cards/{tid}/move", {
            "priority": priority, "after_id": after_id,
            "before_id": before_id, "actor": actor})

    def complete(self, tid, actor):
        return self._post(f"/cards/{tid}/complete", {"actor": actor})

    def undo(self, event_id, actor):
        return self._post(f"/events/{event_id}/undo", {"actor": actor})


class Poller(QThread):
    loaded = Signal(dict)
    failed = Signal(str)

    def __init__(self, api: Api):
        super().__init__()
        self.api = api

    def run(self):
        try:
            self.loaded.emit({"board": self.api.board(),
                              "events": self.api.events(),
                              "health": self.api.health()})
        except Exception as e:
            self.failed.emit(str(e))


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

def chip(text, bg, fg, dashed=False) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet(
        f"background:{bg}; color:{fg}; border:1px "
        f"{'dashed' if dashed else 'solid'} {fg}44; border-radius:3px;"
        f"padding:1px 6px; font-size:11px;")
    lab.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
    return lab


class Card(QFrame):
    """One thread. Draggable; a press that doesn't move is a click, not a drag."""

    def __init__(self, data: dict, board: "Bert"):
        super().__init__()
        self.data = data
        self.board = board
        self.thread_id = data["thread_id"]
        self._press: QPoint | None = None

        stripe, cbg, cfg = QUEUE.get(data.get("queue") or "", NEUTRAL)
        self.setStyleSheet(
            f"Card {{ background:{SURFACE}; border:1px solid {LINE};"
            f" border-left:3px solid {stripe}; }}")
        self.setCursor(Qt.OpenHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(chip(data.get("queue") or "\u2014", cbg, cfg))
        client = QLabel(data.get("client_raw") or "Unknown client")
        f = QFont()
        f.setPointSize(11)
        f.setWeight(QFont.DemiBold)
        client.setFont(f)
        client.setStyleSheet(f"color:{INK};")
        head.addWidget(client)
        head.addStretch()
        if data.get("ticket_count"):
            head.addWidget(chip(f"{data['ticket_count']} tickets", "#EEEFF1", MUTED))
        ago = QLabel(self._ago(data.get("last_human_at")))
        ago.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        head.addWidget(ago)
        lay.addLayout(head)

        if data.get("equipment"):
            row = QHBoxLayout()
            row.setSpacing(5)
            for e in data["equipment"][:6]:
                if e["state"] == "resolved":
                    row.addWidget(chip(e["raw"], "#F2F3F4", MUTED))
                elif e["state"] == "pending":
                    row.addWidget(chip(e["raw"], AMBER_BG, AMBER_FG, dashed=True))
                else:
                    row.addWidget(chip(e["raw"], RED_BG, RED_FG))
            row.addStretch()
            lay.addLayout(row)

        s = QLabel(data.get("summary") or data.get("name") or "")
        s.setWordWrap(True)
        s.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        lay.addWidget(s)

        foot = QHBoxLayout()
        foot.setSpacing(16)
        for label, key in (("Build", "build_state"), ("Return", "return_state")):
            foot.addWidget(self._state(label, data.get(key)))
        foot.addStretch()
        for issue in (data.get("issues") or [])[:2]:
            foot.addWidget(chip(issue.replace("_", " "), AMBER_BG, AMBER_FG))

        self.done_btn = QPushButton("Complete")
        self.done_btn.setStyleSheet("font-size:11px; padding:3px 10px;")
        self.done_btn.clicked.connect(lambda: board.complete(self.thread_id))
        foot.addWidget(self.done_btn)
        lay.addLayout(foot)

    def set_writable(self, ok: bool):
        self.done_btn.setEnabled(ok)
        self.setCursor(Qt.OpenHandCursor if ok else Qt.ArrowCursor)

    # drag -----------------------------------------------------------------

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

        self.board.begin_drag(self.data["priority"])
        drag.exec(Qt.MoveAction)
        self.board.end_drag()
        self._press = None

    def mouseReleaseEvent(self, e):
        self._press = None

    @staticmethod
    def _state(name, value) -> QLabel:
        text = {"created": "Created", "not_needed": "Not needed"}.get(
            value or "", "Needs created")
        colour = {"created": OK_FG, "not_needed": MUTED}.get(value or "", AMBER_FG)
        lab = QLabel(f"{name} <b style='color:{colour}'>{text}</b>")
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
    """A priority band. Accepts drops and shows where the card will land."""

    def __init__(self, priority: str, board: "Bert"):
        super().__init__()
        self.priority = priority
        self.board = board
        self.cards: list[dict] = []
        self.setAcceptDrops(True)

        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(8)

        self.header = QWidget()
        h = QHBoxLayout(self.header)
        h.setContentsMargins(0, 4, 0, 2)
        h.setSpacing(8)
        self.title = QLabel(BAND_LABEL[priority])
        self.title.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        self.count = QLabel("0")
        self.count.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setStyleSheet(f"color:{LINE};")
        h.addWidget(self.title)
        h.addWidget(self.count)
        h.addWidget(rule, 1)
        self.lay.addWidget(self.header)

        self.marker = QFrame(self)
        self.marker.setFixedHeight(2)
        self.marker.setStyleSheet(f"background:{ACCENT};")
        self.marker.hide()

    def set_cards(self, cards: list[dict]):
        while self.lay.count() > 1:
            it = self.lay.takeAt(1)
            w = it.widget()
            if w is not None and w is not self.marker:
                w.deleteLater()
        self.cards = cards
        self.count.setText(str(len(cards)))
        for c in cards:
            self.lay.addWidget(Card(c, self.board))
        self.setVisible(bool(cards) or self.board.dragging is not None)

    def _slot(self, y: int) -> int:
        idx = 0
        for i in range(1, self.lay.count()):
            w = self.lay.itemAt(i).widget()
            if not isinstance(w, Card):
                continue
            if y > w.y() + w.height() / 2:
                idx = i
        return idx + 1

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasFormat(MIME):
            return
        slot = self._slot(e.position().toPoint().y())
        self.lay.removeWidget(self.marker)
        self.lay.insertWidget(slot, self.marker)
        self.marker.show()
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.marker.hide()

    def dropEvent(self, e):
        tid = bytes(e.mimeData().data(MIME)).decode()
        slot = self._slot(e.position().toPoint().y())
        self.marker.hide()

        order = [c["thread_id"] for c in self.cards if c["thread_id"] != tid]
        pos = min(max(slot - 1, 0), len(order))
        after = order[pos - 1] if pos > 0 else None
        before = order[pos] if pos < len(order) else None

        self.board.move_card(tid, self.priority, after, before)
        e.acceptProposedAction()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class Bert(QMainWindow):
    def __init__(self, api_base: str):
        super().__init__()
        self.api = Api(api_base)
        self.settings = load_settings()
        self.fail_since: float | None = None
        self.dragging: str | None = None
        self.connected = True
        self.filters = {q: True for q in QUEUE}
        self.cards: list[dict] = []
        self.feed: list[dict] = []

        self.setWindowTitle("Bert")
        self.resize(980, 820)
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

        self.empty = QLabel("Nothing open. New threads land in Unassigned "
                            "as soon as Ernie sees them.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet(f"color:{MUTED}; padding:60px; font-size:13px;")
        self.empty.hide()
        self.board_lay.addWidget(self.empty)

        self.bands = {}
        for b in BANDS:
            self.bands[b] = Band(b, self)
            self.board_lay.addWidget(self.bands[b])
        self.board_lay.addStretch()

        self.scroll.setWidget(board)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(self._feed_panel())

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_MS)
        QTimer.singleShot(0, self.refresh)
        if not self.name():
            QTimer.singleShot(300, self.open_settings)

    # -- chrome ------------------------------------------------------------

    def _toolbar(self) -> QWidget:
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
        lay.addSpacing(12)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search client, equipment, summary")
        self.search.setFixedWidth(250)
        self.search.textChanged.connect(self.render)
        lay.addWidget(self.search)

        for q in QUEUE:
            cb = QCheckBox(q)
            cb.setChecked(True)
            cb.stateChanged.connect(
                lambda s, k=q: (self.filters.__setitem__(k, bool(s)), self.render()))
            lay.addWidget(cb)

        lay.addStretch()
        self.who = QLabel("")
        lay.addWidget(self.who)
        gear = QPushButton("Settings")
        gear.clicked.connect(self.open_settings)
        lay.addWidget(gear)
        return bar

    def _feed_panel(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background:{SURFACE}; border-top:1px solid {LINE};")
        w.setFixedHeight(132)
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

    def name(self) -> str:
        s = self.settings
        return f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()

    def writable(self) -> bool:
        return bool(self.name()) and self.connected

    def open_settings(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec() == QDialog.Accepted:
            self.settings.update(dlg.values())
            SETTINGS.write_text(json.dumps(self.settings, indent=2))
            self.render()

    # -- polling -----------------------------------------------------------

    def refresh(self):
        self.poller = Poller(self.api)
        self.poller.loaded.connect(self.on_loaded)
        self.poller.failed.connect(self.on_failed)
        self.poller.start()

    def on_loaded(self, p):
        self.fail_since = None
        self.connected = True
        self.banner.hide()
        self.cards = p["board"]["cards"]
        self.feed = p["events"]["events"]
        self.render()

    def on_failed(self, err):
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
            self.banner.setText(
                "Can't reach Ernie. Showing the last known board \u2014 "
                "changes are paused until the connection is back.")
            self.banner.setStyleSheet(
                f"background:{RED_BG}; color:{RED_FG}; padding:7px; font-size:12px;")
            self.render()

    # -- writes ------------------------------------------------------------

    def _guard(self) -> bool:
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
        try:
            self.api.move(tid, priority, after, before, self.name())
        except Exception as e:
            QMessageBox.warning(self, "Couldn't move that card", str(e))
        self.refresh()

    def complete(self, tid):
        if not self._guard():
            return
        try:
            self.api.complete(tid, self.name())
        except Exception as e:
            QMessageBox.warning(self, "Couldn't complete that card", str(e))
        self.refresh()

    def undo(self, event_id):
        if not self._guard():
            return
        try:
            r = self.api.undo(event_id, self.name())
            if r.get("correction_posted"):
                QMessageBox.information(
                    self, "Undone",
                    "That update was already posted to Discord, so Ernie "
                    "added a correction in the thread.")
        except Exception as e:
            QMessageBox.warning(self, "Couldn't undo", str(e))
        self.refresh()

    # -- drag state --------------------------------------------------------

    def begin_drag(self, priority):
        self.dragging = priority
        for b in self.bands.values():
            b.setVisible(True)

    def end_drag(self):
        self.dragging = None
        for b in self.bands.values():
            b.marker.hide()
        self.render()

    # -- rendering ---------------------------------------------------------

    def render(self):
        term = self.search.text().strip().lower()

        def keep(c):
            if not self.filters.get(c.get("queue") or "", True):
                return False
            if term:
                hay = " ".join(str(c.get(k) or "") for k in
                               ("name", "client_raw", "summary")).lower()
                hay += " ".join(e["raw"] for e in (c.get("equipment") or [])).lower()
                return term in hay
            return True

        shown = [c for c in self.cards if keep(c)]
        self.count.setText(f"{len(shown)} open")
        self.empty.setVisible(not shown)

        for band, w in self.bands.items():
            w.set_cards([c for c in shown if c["priority"] == band])

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
            when.setFixedWidth(58)
            when.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            h.addWidget(when)

            verb = e["verb"].replace("_", " ")
            what = (e.get("thread_name") or "")[:52]
            txt = QLabel(f"<b>{e.get('actor_name') or 'Ernie'}</b> {verb} "
                         f"<span style='color:{MUTED}'>{what}</span>")
            txt.setStyleSheet(f"color:{INK}; font-size:12px;")
            h.addWidget(txt)
            h.addStretch()

            if e["verb"] in ("completed", "priority_changed") and not e["undone_at"]:
                b = QPushButton("Undo")
                b.setStyleSheet("font-size:11px; padding:2px 9px;")
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

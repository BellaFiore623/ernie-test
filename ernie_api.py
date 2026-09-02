"""
Ernie's HTTP API -- the boundary Bert talks to.

Read-only for now. Writes (rank, statuses, complete/undo) come next, and go
through the events table with idempotency keys.

    pip install fastapi uvicorn
    python ernie_api.py                 # http://127.0.0.1:8787
    python ernie_api.py --host 0.0.0.0  # reachable from other machines

Interactive docs at /docs -- poke every endpoint from a browser, no client
needed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel

import ernie_extract as ex

DB = "ernie.db"
UNDO_WINDOW_S = 60      # how long before Ernie posts to the thread
RANK_STEP = 1000.0
PRIORITY_ORDER = ("unassigned", "critical", "high", "medium", "low")
WORK_ITEM_MAX = 200     # a bubble, not a paragraph -- it has to fit on a card

app = FastAPI(title="Ernie", version="0.1")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    return con


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur]


def rw() -> sqlite3.Connection:
    """Read-write connection. Only the write endpoints use this."""
    con = sqlite3.connect(DB, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_actor(name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Set your name in Settings before making changes.")
    return name


def replay(con, key: str | None):
    """Return the stored result for an idempotency key we've already seen."""
    if not key:
        return None
    row = con.execute("SELECT result_json FROM write_keys WHERE idempotency_key=?",
                      (key,)).fetchone()
    return json.loads(row["result_json"]) if row else None


def remember(con, key: str | None, result: dict) -> dict:
    if key:
        con.execute(
            "INSERT OR REPLACE INTO write_keys (idempotency_key, result_json, created_at)"
            " VALUES (?,?,?)", (key, json.dumps(result), now_iso()))
    return result


def log_event(con, *, thread_id, verb, actor, old=None, new=None, post=False) -> str:
    """
    Record an event. post=True queues a Discord message, delayed by the undo
    window so an undo inside that window cancels it before it ever goes out.
    """
    eid = str(uuid.uuid4())
    ts = datetime.now(timezone.utc)
    dispatch = (ts + timedelta(seconds=UNDO_WINDOW_S)).isoformat() if post else None
    con.execute(
        """INSERT INTO events (event_id, occurred_at, actor_name, thread_id, verb,
                               old_value, new_value, dispatch_after)
           VALUES (?,?,?,?,?,?,?,?)""",
        (eid, ts.isoformat(), actor, thread_id, verb, old, new, dispatch))
    return eid


class MoveBody(BaseModel):
    priority: str
    after_id: Optional[str] = None      # card it lands below
    before_id: Optional[str] = None     # card it lands above
    actor: str
    key: Optional[str] = None


class StatusBody(BaseModel):
    build_state: Optional[str] = None
    return_state: Optional[str] = None
    direction: Optional[str] = None
    action_item: Optional[str] = None
    actor: str
    key: Optional[str] = None


class ActorBody(BaseModel):
    actor: str
    key: Optional[str] = None
    force: bool = False             # proceed past a soft conflict


class EditBody(BaseModel):
    """A batch of field edits saved together as one change."""
    client_override: Optional[str] = None
    action_item: Optional[str] = None
    build_state: Optional[str] = None
    return_state: Optional[str] = None
    direction: Optional[str] = None
    title: Optional[str] = None     # the Discord thread name; renames the thread
    # Work items are a list, so the editor sends what it did rather than the
    # whole list: texts typed in, and the ids of bubbles x-ed out.
    work_add: list[str] = []
    work_remove: list[str] = []
    actor: str
    key: Optional[str] = None
    # What the editor was showing when it opened. Lets the server tell a field
    # this person actually changed from one they merely had on screen, so two
    # people editing different fields of the same card don't collide.
    base: Optional[dict] = None
    force: bool = False             # save anyway, overwriting the other person


# Bert stopped sending the last four when work items replaced them, so an edit
# now only ever carries client_override. They stay in the tuple because undo
# reads it to decide what an old 'edited' event is allowed to put back, and the
# feed still has those events in it.
EDITABLE = ("client_override", "action_item", "build_state",
            "return_state", "direction")

FIELD_LABEL = {
    "client_override": "client",
    "action_item": "current work item",
    "build_state": "build ticket",
    "return_state": "return ticket",
    "direction": "equipment",
}

VALUE_LABEL = {
    "needs_created": "needs created", "created": "created",
    "not_needed": "not needed", "leaving": "leaving",
    "coming_back": "coming back",
}


# Columns this build depends on. The API opens the database directly rather
# than through load.connect(), so schema.sql is never applied here and a
# database that missed a migration fails at request time with an opaque
# IndexError. Checked once at startup instead.
REQUIRED_COLUMNS = {
    "cards": ["client_override"],
    "events": ["claimed_at"],
    "work_items": ["item_id", "done_at"],
}


def check_schema() -> None:
    con = db()
    missing = []
    for table, cols in REQUIRED_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        missing += [f"{table}.{c}" for c in cols if c not in have]
    con.close()
    if missing:
        sys.exit(f"{DB} is behind this build -- missing {', '.join(missing)}.\n"
                 f"Run the matching migrate_*.py against it first.")


def conflict(code: str, message: str, **extra):
    """409 with a body Bert can render, rather than a bare string."""
    raise HTTPException(409, {"code": code, "message": message, **extra})


def load_card(con, thread_id: str):
    card = con.execute("SELECT * FROM cards WHERE thread_id=?",
                       (thread_id,)).fetchone()
    if not card:
        raise HTTPException(404, "no such card")
    return card


def last_touch(con, thread_id: str) -> dict:
    """Who last changed this card, so a conflict can name a person."""
    e = con.execute(
        """SELECT actor_name, occurred_at, verb, new_value FROM events
           WHERE thread_id=? AND undone_at IS NULL
           ORDER BY occurred_at DESC LIMIT 1""", (thread_id,)).fetchone()
    if not e:
        return {}
    return {"by": e["actor_name"] or "Someone", "at": e["occurred_at"],
            "verb": e["verb"], "detail": e["new_value"]}


REVISION_LABEL = {
    "priority_changed": "moved it to another priority band",
    "completed":        "closed the ticket",
    "renamed":          "renamed the thread",
    "work_done":        "ticked a work item off",
    "edited":           "edited the card",
}


def describe_revision(e) -> str:
    """The later change, in the words a person would use for it."""
    if e["verb"].startswith("set_"):
        field = e["verb"][4:]
        return f"changed the {FIELD_LABEL.get(field, field)}"
    return REVISION_LABEL.get(e["verb"], "changed it")


def event_scope(e) -> set[str]:
    """
    What an event actually touched, as opaque keys. Two events collide when
    these sets intersect.

    Per value rather than per card, because most pairs don't collide at all:
    closing a ticket is no reason to refuse an undo of a rename.
    """
    verb = e["verb"]
    if verb == "priority_changed":
        return {"priority"}
    if verb == "reordered":
        # Undo never restores rank, so a later reorder is not in its way.
        return {"rank"}
    if verb == "completed":
        return {"completed"}
    if verb == "renamed":
        return {"title"}
    if verb == "work_done":
        return {f"work:{e['old_value']}"}       # old_value is the item id
    if verb.startswith("set_"):
        return {f"field:{verb[4:]}"}
    if verb == "edited":
        # old_value is the JSON of what the batch overwrote, so its keys are
        # exactly the fields that moved, and __work__ the bubbles that did.
        try:
            previous = json.loads(e["old_value"] or "{}")
        except json.JSONDecodeError:
            return set()
        work = previous.pop("__work__", None) or {}
        scope = {f"field:{f}" for f in previous}
        for iid in (work.get("added") or []) + (work.get("removed") or []):
            scope.add(f"work:{iid}")
        return scope
    return set()            # undo_correction, and any verb added since


def revised_since(con, e):
    """
    The first later event that touched what this one touched, or None.

    Undo is a restore, not a merge: it writes the old value straight back over
    whatever is there now. If somebody has moved that same value on since,
    restoring it throws their change away without either of them seeing it go.
    """
    scope = event_scope(e)
    if not scope:
        return None
    later = con.execute(
        """SELECT * FROM events
           WHERE thread_id=? AND undone_at IS NULL AND event_id<>?
             AND datetime(occurred_at) >= datetime(?)
           ORDER BY occurred_at""",
        (e["thread_id"], e["event_id"], e["occurred_at"])).fetchall()
    for row in later:
        # datetime() truncates to the second, so that filter is deliberately
        # loose; the exact order is settled here on the full ISO timestamp.
        if row["occurred_at"] <= e["occurred_at"]:
            continue
        if event_scope(row) & scope:
            return row
    return None


def open_items(con, thread_id: str) -> list[dict]:
    """The bubbles a card is currently showing: not ticked off, not removed."""
    return rows(con.execute(
        """SELECT item_id, body, created_at, created_by FROM work_items
           WHERE thread_id=? AND done_at IS NULL AND removed_at IS NULL
           ORDER BY position""", (thread_id,)))


def guard_open(card, doing: str = "change"):
    """Refuse to touch a card somebody has already closed."""
    if card["completed_at"]:
        who = card["completed_by"] or "Someone"
        conflict("completed", f"{who} has already closed this ticket.",
                 by=who, at=card["completed_at"], doing=doing,
                 hint="Reopen it first if you still need to change it.")


# --------------------------------------------------------------------------

@app.get("/")
def root():
    return {"service": "ernie", "docs": "/docs"}


@app.get("/health")
def health():
    """Is Ernie alive, and how stale is the mirror?"""
    con = db()
    last = con.execute(
        """SELECT started_at, finished_at, threads_seen, messages_new, error
           FROM sync_runs ORDER BY run_id DESC LIMIT 1""").fetchone()
    board = con.execute(
        "SELECT COUNT(*) FROM cards WHERE completed_at IS NULL").fetchone()[0]
    con.close()

    stale = None
    if last and last["finished_at"]:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(last["finished_at"])
        stale = int(delta.total_seconds())

    return {
        "ok": bool(last and not last["error"]),
        "last_sync": dict(last) if last else None,
        "seconds_since_sync": stale,
        "board_size": board,
    }


@app.get("/cards")
def cards(
    queue: Optional[str] = Query(None, description="OPS | PROD | ENG | CS"),
    client: Optional[str] = None,
    include_completed: bool = False,
):
    """The board. Sorted by priority band, then manual rank within it."""
    con = db()
    sql = """
        SELECT c.thread_id, c.priority, c.rank, c.build_state, c.return_state,
               c.direction, c.action_item, c.client_override, c.updated_at,
               c.completed_at, c.completed_by,
               v.name, v.queue, v.client_raw, v.client_key, v.thread_date,
               v.summary, v.confidence, v.archived,
               (SELECT COUNT(*) FROM tickets t
                WHERE t.thread_id = c.thread_id) AS ticket_count,
               (SELECT MAX(m.created_at) FROM messages m
                WHERE m.thread_id = c.thread_id AND m.is_bot = 0) AS last_human_at
        FROM cards c
        JOIN v_thread_current v ON v.thread_id = c.thread_id
        WHERE 1=1
    """
    args: list = []
    if not include_completed:
        sql += " AND c.completed_at IS NULL"
    if queue:
        sql += " AND v.queue = ?"
        args.append(queue.upper())
    if client:
        sql += " AND v.client_key LIKE ?"
        args.append(f"%{client.lower()}%")

    out = rows(con.execute(sql, args))

    # A title the board has changed but Discord hasn't confirmed yet. Showing
    # the old one means the queue tag stays "--" and the card stays red for the
    # couple of minutes it takes the rename to post and the sync to read it
    # back. One query for the lot rather than one per card.
    pending = {}
    for r in con.execute(
            """SELECT thread_id, new_value FROM events
               WHERE verb='renamed' AND undone_at IS NULL AND posted_at IS NULL
               ORDER BY occurred_at"""):
        pending[r["thread_id"]] = r["new_value"]      # newest wins

    # One query for every card's bubbles rather than one per card; the board
    # is redrawn on every poll and this sits in that path.
    items: dict[str, list] = {}
    for r in con.execute(
            """SELECT thread_id, item_id, body FROM work_items
               WHERE done_at IS NULL AND removed_at IS NULL
               ORDER BY thread_id, position"""):
        items.setdefault(r["thread_id"], []).append(
            {"item_id": r["item_id"], "body": r["body"]})

    # equipment and open issues per card
    for c in out:
        c["work_items"] = items.get(c["thread_id"], [])
        c["title_pending"] = False
        proposed = pending.get(c["thread_id"])
        if proposed:
            t = ex.parse_title(proposed)
            c["name"] = proposed
            c["queue"] = t.queue
            c["client_raw"] = t.client_raw
            c["client_key"] = ex.normalise_client(t.client_raw or "") or None
            c["thread_date"] = t.date.isoformat() if t.date else None
            c["summary"] = t.summary
            c["confidence"] = t.confidence
            c["title_pending"] = True

        c["equipment"] = rows(con.execute(
            "SELECT eq_type, eq_number, state, raw FROM thread_equipment "
            "WHERE thread_id=?", (c["thread_id"],)))
        issues = con.execute(
            "SELECT issues_json FROM ticket_proposals WHERE thread_id=?",
            (c["thread_id"],)).fetchall()
        merged: list[str] = []
        for i in issues:
            merged += json.loads(i["issues_json"])
        if c["confidence"] in ("loose", "prefix_only", "none"):
            merged.append(f"title_{c['confidence']}")
        c["issues"] = sorted(set(merged))
    con.close()

    out.sort(key=lambda c: (PRIORITY_ORDER.index(c["priority"])
                            if c["priority"] in PRIORITY_ORDER else 99,
                            c["rank"]))
    return {"count": len(out), "cards": out,
            "as_of": datetime.now(timezone.utc).isoformat()}


@app.get("/cards/{thread_id}")
def card_detail(thread_id: str):
    """Everything Bert needs for the expanded card."""
    con = db()
    card = con.execute(
        """SELECT c.*, v.name, v.queue, v.client_raw, v.client_key,
                  v.thread_date, v.summary, v.confidence, v.archived, v.parent_id
           FROM cards c JOIN v_thread_current v ON v.thread_id = c.thread_id
           WHERE c.thread_id = ?""", (thread_id,)).fetchone()
    if not card:
        raise HTTPException(404, "no such card")

    d = dict(card)
    d["work_items"] = open_items(con, thread_id)
    d["equipment"] = rows(con.execute(
        "SELECT eq_type, eq_number, state, raw FROM thread_equipment WHERE thread_id=?",
        (thread_id,)))
    d["tickets"] = rows(con.execute(
        """SELECT pip_key, kind, created_at, assignee, equipment_master, client_cr
           FROM tickets WHERE thread_id=? ORDER BY created_at""", (thread_id,)))
    d["proposals"] = rows(con.execute(
        """SELECT message_id, kind, proposed_at, equipment_master, equipment_label,
                  client_cr, client_label, equipment_type, template, assignee,
                  reporter, reported_problem, has_buttons, issues_json
           FROM ticket_proposals WHERE thread_id=? ORDER BY proposed_at""",
        (thread_id,)))
    for p in d["proposals"]:
        p["issues"] = json.loads(p.pop("issues_json"))

    d["title_history"] = rows(con.execute(
        """SELECT observed_at, name, queue, confidence FROM thread_titles
           WHERE thread_id=? ORDER BY observed_at DESC""", (thread_id,)))
    d["discord_url"] = f"https://discord.com/channels/{card['parent_id']}/{thread_id}"
    con.close()
    return d


@app.get("/cards/{thread_id}/messages")
def card_messages(thread_id: str, limit: int = 200, include_bots: bool = True):
    """Latest revision of each message, oldest first. Deleted ones excluded."""
    con = db()
    sql = """
        SELECT m.message_id, m.author_name, m.is_bot, m.created_at,
               r.content, r.edited_at, r.embeds_json
        FROM messages m
        JOIN message_revisions r ON r.message_id = m.message_id
        WHERE m.thread_id = ? AND m.deleted_at IS NULL
          AND r.observed_at = (SELECT MAX(observed_at) FROM message_revisions
                               WHERE message_id = m.message_id)
    """
    if not include_bots:
        sql += " AND m.is_bot = 0"
    sql += " ORDER BY m.created_at LIMIT ?"

    out = rows(con.execute(sql, (thread_id, limit)))
    con.close()
    for m in out:
        m["embeds"] = json.loads(m.pop("embeds_json") or "[]")
    return {"count": len(out), "messages": out}


@app.get("/events")
def events(since: Optional[str] = None, limit: int = 50):
    """Activity feed. Pass `since` (ISO timestamp) to poll for changes."""
    con = db()
    sql = """SELECT e.*, v.name AS thread_name
             FROM events e
             LEFT JOIN v_thread_current v ON v.thread_id = e.thread_id
             WHERE 1=1"""
    args: list = []
    if since:
        sql += " AND e.occurred_at > ?"
        args.append(since)
    sql += " ORDER BY e.occurred_at DESC LIMIT ?"
    args.append(limit)

    out = rows(con.execute(sql, args))
    con.close()
    return {"count": len(out), "events": out, "now": datetime.now(timezone.utc).isoformat()}


@app.get("/clients")
def clients():
    """Distinct client keys seen on the board, for the filter dropdown."""
    con = db()
    out = rows(con.execute(
        """SELECT v.client_key, COUNT(*) AS n,
                  MAX(v.client_raw) AS example
           FROM cards c JOIN v_thread_current v ON v.thread_id = c.thread_id
           WHERE c.completed_at IS NULL AND v.client_key IS NOT NULL
           GROUP BY v.client_key ORDER BY n DESC"""))
    con.close()
    return {"count": len(out), "clients": out}




# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

@app.post("/cards/{thread_id}/move")
def move_card(thread_id: str, body: MoveBody):
    """
    Reorder or reprioritise. Send the neighbours, not a rank -- the server
    computes the midpoint so two people dragging at once can't clash.
    """
    actor = require_actor(body.actor)
    if body.priority not in PRIORITY_ORDER:
        raise HTTPException(400, f"priority must be one of {PRIORITY_ORDER}")

    con = rw()
    try:
        cached = replay(con, body.key)
        if cached:
            return cached

        card = load_card(con, thread_id)
        guard_open(card, "move")

        # Bert can only send neighbours it can see. With a queue filter or a
        # search on, the card on the far side of the gap may be hidden, and the
        # midpoint between two visible cards can land straight on top of a
        # hidden one -- equal ranks, and an arbitrary order the moment the
        # filter comes off. So anchor to the neighbour the person actually
        # dropped against and take the other side from the whole band. With no
        # filter on, the visible neighbour is the real one and this is the
        # midpoint it always was.
        band = con.execute(
            """SELECT thread_id, rank FROM cards
               WHERE priority=? AND thread_id<>? AND completed_at IS NULL
               ORDER BY rank""", (body.priority, thread_id)).fetchall()
        ids = [r["thread_id"] for r in band]
        ranks = [r["rank"] for r in band]

        lo = hi = None
        if body.after_id in ids:
            i = ids.index(body.after_id)
            lo = ranks[i]
            hi = ranks[i + 1] if i + 1 < len(ranks) else None
        elif body.before_id in ids:
            j = ids.index(body.before_id)
            hi = ranks[j]
            lo = ranks[j - 1] if j > 0 else None

        if lo is not None and hi is not None:
            new_rank = (lo + hi) / 2
        elif lo is not None:
            new_rank = lo + RANK_STEP
        elif hi is not None:
            new_rank = hi - RANK_STEP
        else:
            new_rank = (ranks[-1] + RANK_STEP) if ranks else RANK_STEP

        con.execute(
            "UPDATE cards SET priority=?, rank=?, updated_at=? WHERE thread_id=?",
            (body.priority, new_rank, now_iso(), thread_id))

        if card["priority"] != body.priority:
            # In or out of critical is the one band change the thread should
            # hear about. The rest is board housekeeping -- posting every
            # nudge between high and medium would be noise in a customer
            # thread, and nobody reading it could act on it.
            loud = "critical" in (card["priority"], body.priority)
            log_event(con, thread_id=thread_id, verb="priority_changed", actor=actor,
                      old=card["priority"], new=body.priority, post=loud)
        else:
            log_event(con, thread_id=thread_id, verb="reordered", actor=actor)

        # Renormalise if the gap is collapsing toward float precision limits.
        if lo is not None and hi is not None and abs(hi - lo) < 0.001:
            band = con.execute(
                "SELECT thread_id FROM cards WHERE priority=? ORDER BY rank",
                (body.priority,)).fetchall()
            for i, r in enumerate(band, 1):
                con.execute("UPDATE cards SET rank=? WHERE thread_id=?",
                            (i * RANK_STEP, r["thread_id"]))

        result = {"thread_id": thread_id, "priority": body.priority, "rank": new_rank}
        remember(con, body.key, result)
        con.commit()
        return result
    finally:
        con.close()


@app.post("/cards/{thread_id}/status")
def set_status(thread_id: str, body: StatusBody):
    """Update build/return state, direction, or the current action item."""
    actor = require_actor(body.actor)
    con = rw()
    try:
        cached = replay(con, body.key)
        if cached:
            return cached

        card = load_card(con, thread_id)
        guard_open(card, "update")

        valid = {"needs_created", "created", "not_needed"}
        changes = {}
        for f in ("build_state", "return_state", "direction", "action_item"):
            v = getattr(body, f)
            if v is None or v == card[f]:
                continue
            if f.endswith("_state") and v not in valid:
                raise HTTPException(400, f"{f} must be one of {sorted(valid)}")
            changes[f] = v

        for f, v in changes.items():
            con.execute(f"UPDATE cards SET {f}=?, updated_at=? WHERE thread_id=?",
                        (v, now_iso(), thread_id))
            log_event(con, thread_id=thread_id, verb=f"set_{f}", actor=actor,
                      old=card[f], new=v)

        result = {"thread_id": thread_id, "changed": changes}
        remember(con, body.key, result)
        con.commit()
        return result
    finally:
        con.close()


@app.post("/cards/{thread_id}/complete")
def complete(thread_id: str, body: ActorBody):
    """Mark done. Queues a thread message, held for the undo window."""
    actor = require_actor(body.actor)
    con = rw()
    try:
        cached = replay(con, body.key)
        if cached:
            return cached

        card = load_card(con, thread_id)
        guard_open(card, "close")

        ts = now_iso()
        con.execute(
            "UPDATE cards SET completed_at=?, completed_by=?, updated_at=? "
            "WHERE thread_id=?", (ts, actor, ts, thread_id))
        eid = log_event(con, thread_id=thread_id, verb="completed", actor=actor,
                        post=True)

        result = {"thread_id": thread_id, "event_id": eid,
                  "undo_until": (datetime.now(timezone.utc)
                                 + timedelta(seconds=UNDO_WINDOW_S)).isoformat()}
        remember(con, body.key, result)
        con.commit()
        return result
    finally:
        con.close()


@app.post("/cards/{thread_id}/reopen")
def reopen(thread_id: str, body: ActorBody):
    actor = require_actor(body.actor)
    con = rw()
    try:
        cached = replay(con, body.key)
        if cached:
            return cached

        card = load_card(con, thread_id)
        if not card["completed_at"]:
            conflict("not_completed", "This ticket isn't closed.",
                     **last_touch(con, thread_id))

        con.execute("UPDATE cards SET completed_at=NULL, completed_by=NULL, "
                    "updated_at=? WHERE thread_id=?", (now_iso(), thread_id))
        eid = log_event(con, thread_id=thread_id, verb="reopened", actor=actor,
                        post=True)
        result = {"thread_id": thread_id, "event_id": eid}
        remember(con, body.key, result)
        con.commit()
        return result
    finally:
        con.close()


@app.post("/cards/{thread_id}/work/{item_id}/done")
def finish_work_item(thread_id: str, item_id: str, body: ActorBody):
    """
    Tick a work item off from the card, without opening the editor.

    Its own event, not a batched one: this is a single deliberate click, and
    the thread should hear what got finished rather than "the card changed".
    """
    actor = require_actor(body.actor)
    con = rw()
    try:
        cached = replay(con, body.key)
        if cached:
            return cached

        card = load_card(con, thread_id)
        guard_open(card, "tick off a work item")

        item = con.execute(
            "SELECT * FROM work_items WHERE item_id=? AND thread_id=?",
            (item_id, thread_id)).fetchone()
        if not item or item["removed_at"]:
            raise HTTPException(404, "no such work item")
        if item["done_at"]:
            who = item["done_by"] or "Someone"
            conflict("already_done", f"{who} already ticked that one off.",
                     by=who, at=item["done_at"])

        con.execute("UPDATE work_items SET done_at=?, done_by=? WHERE item_id=?",
                    (now_iso(), actor, item_id))
        con.execute("UPDATE cards SET updated_at=? WHERE thread_id=?",
                    (now_iso(), thread_id))

        # old_value is the id so undo can find the row again; new_value is the
        # text, because that is what the thread message says.
        eid = log_event(con, thread_id=thread_id, verb="work_done", actor=actor,
                        old=item_id, new=item["body"], post=True)

        result = {"thread_id": thread_id, "item_id": item_id,
                  "done": True, "event_id": eid, "summary": item["body"]}
        remember(con, body.key, result)
        con.commit()
        return result
    finally:
        con.close()


@app.post("/events/{event_id}/undo")
def undo(event_id: str, body: ActorBody):
    """
    Reverse an event. Inside the undo window nothing was posted to Discord, so
    this is clean. After it, the message is already out and Ernie posts a
    correction instead.
    """
    actor = require_actor(body.actor)
    con = rw()
    try:
        cached = replay(con, body.key)
        if cached:
            return cached

        e = con.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not e:
            raise HTTPException(404, "no such event")
        if e["undone_at"]:
            who = e["undone_by"] or "Someone"
            conflict("already_undone", f"{who} has already undone this.",
                     by=who, at=e["undone_at"])

        # The outbox claims a row before it starts talking to Discord. If this
        # one is claimed but not yet marked posted, the message is in flight and
        # we cannot tell whether it needs a correction -- so don't guess.
        if e["claimed_at"] and not e["posted_at"]:
            conflict("posting",
                     "Ernie is posting this to Discord right now. "
                     "Try the undo again in a few seconds.",
                     at=e["claimed_at"])

        # A hard stop, unlike other_actor below: force is for taking over
        # somebody else's change, not for silently discarding one that was
        # made after it. The way out is to make the change again by hand.
        newer = revised_since(con, e)
        if newer:
            who = newer["actor_name"] or "Someone"
            # Usually somebody else, but you can outrun your own undo too:
            # two drags and then undo of the first is the same collision.
            subject = "You have" if who == actor else f"{who} has"
            conflict("revised",
                     f"{subject} {describe_revision(newer)} since. "
                     f"Undoing now would discard that.",
                     by=who, at=newer["occurred_at"], verb=newer["verb"],
                     detail=newer["new_value"])

        if (e["actor_name"] or "") != actor and not body.force:
            conflict("other_actor",
                     f"That change was made by {e['actor_name'] or 'someone else'}.",
                     by=e["actor_name"], at=e["occurred_at"], verb=e["verb"],
                     detail=e["new_value"],
                     hint="Undo it anyway?")

        if e["verb"] == "completed":
            con.execute("UPDATE cards SET completed_at=NULL, completed_by=NULL, "
                        "updated_at=? WHERE thread_id=?", (now_iso(), e["thread_id"]))
        elif e["verb"] == "priority_changed":
            con.execute("UPDATE cards SET priority=?, updated_at=? WHERE thread_id=?",
                        (e["old_value"], now_iso(), e["thread_id"]))
        elif e["verb"] == "renamed":
            # Nothing in cards to put back. Inside the undo window the rename
            # never went out, so cancelling the event is the whole job; once it
            # has, the only way back is another rename.
            if e["posted_at"] and e["old_value"]:
                log_event(con, thread_id=e["thread_id"], verb="renamed",
                          actor=actor, old=e["new_value"], new=e["old_value"],
                          post=True)
        elif e["verb"] == "edited":
            # A batched edit stores the previous values as JSON. Without this
            # branch the event was marked undone and the card never moved back,
            # which is the one undo Bert offers most often.
            try:
                previous = json.loads(e["old_value"] or "{}")
            except json.JSONDecodeError:
                previous = {}
            restore = {k: v for k, v in previous.items() if k in EDITABLE}
            if restore:
                sets = ", ".join(f"{k}=?" for k in restore)
                con.execute(
                    f"UPDATE cards SET {sets}, updated_at=? WHERE thread_id=?",
                    (*restore.values(), now_iso(), e["thread_id"]))
            # Bubbles the same save added come back off the card, and ones it
            # removed go back on.
            work = previous.get("__work__") or {}
            for iid in work.get("added") or []:
                con.execute("UPDATE work_items SET removed_at=?, removed_by=? "
                            "WHERE item_id=?", (now_iso(), actor, iid))
            for iid in work.get("removed") or []:
                con.execute("UPDATE work_items SET removed_at=NULL, "
                            "removed_by=NULL WHERE item_id=?", (iid,))
        elif e["verb"] == "work_done":
            con.execute("UPDATE work_items SET done_at=NULL, done_by=NULL "
                        "WHERE item_id=?", (e["old_value"],))
        elif e["verb"].startswith("set_"):
            col = e["verb"][4:]
            if col not in EDITABLE:
                raise HTTPException(400, f"can't undo {e['verb']}")
            con.execute(f"UPDATE cards SET {col}=?, updated_at=? WHERE thread_id=?",
                        (e["old_value"], now_iso(), e["thread_id"]))

        con.execute("UPDATE events SET undone_at=?, undone_by=? WHERE event_id=?",
                    (now_iso(), actor, event_id))

        # Only an actual message needs retracting. A rename posts none of its
        # own -- Discord announces it -- so a "disregard the last message"
        # correction would be pointing at nothing.
        already_posted = bool(e["posted_at"] and e["discord_message_id"])
        if already_posted:
            log_event(con, thread_id=e["thread_id"], verb="undo_correction",
                      actor=actor, old=e["verb"], post=True)

        result = {"event_id": event_id, "undone": True,
                  "correction_posted": already_posted}
        remember(con, body.key, result)
        con.commit()
        return result
    finally:
        con.close()




@app.post("/cards/{thread_id}/edit")
def edit_card(thread_id: str, body: EditBody):
    """
    Save several field changes at once.

    Deliberately one event and one thread message, however many fields moved.
    Editing four things shouldn't post four times.
    """
    actor = require_actor(body.actor)
    con = rw()
    try:
        cached = replay(con, body.key)
        if cached:
            return cached

        card = load_card(con, thread_id)
        guard_open(card, "edit")

        valid_state = {"needs_created", "created", "not_needed"}
        valid_dir = {"leaving", "coming_back", None, ""}

        base = body.base or {}

        # The title is not a cards column -- it belongs to Discord, and the
        # mirror only ever observes it. So a title change is queued as a rename
        # for the outbox to carry out; the next sync reads the result back.
        row = con.execute("SELECT name FROM v_thread_current WHERE thread_id=?",
                          (thread_id,)).fetchone()
        current_title = (row["name"] if row else "") or ""
        new_title = (body.title or "").strip()
        title_base = (base.get("title") or "") if base else current_title
        wants_rename = bool(new_title) and new_title != title_base
        if wants_rename and len(new_title) > 100:
            raise HTTPException(400, "Discord thread names are limited to "
                                     "100 characters.")

        # Which fields did this person actually change? With a base snapshot
        # that's "differs from what the dialog opened with" -- a field left
        # alone is not a change even though the dialog submits every field.
        # Without one, fall back to "differs from the stored value".
        intended = {}
        for f in EDITABLE:
            v = getattr(body, f)
            if v is None:
                continue
            v = v.strip() if isinstance(v, str) else v
            if f.endswith("_state") and v not in valid_state:
                raise HTTPException(400, f"{f} must be one of {sorted(valid_state)}")
            if f == "direction" and v not in valid_dir:
                raise HTTPException(400, "direction must be leaving or coming_back")
            reference = (base.get(f) or "") if base else (card[f] or "")
            if v != reference:
                intended[f] = v

        # A clash is a field this person changed that somebody else also moved
        # since the dialog opened. Fields only they touched merge cleanly.
        clashes = [
            {"field": f, "label": FIELD_LABEL[f],
             "was": base.get(f) or "", "theirs": card[f] or "", "mine": v}
            for f, v in intended.items()
            if base and (card[f] or "") != (base.get(f) or "")
        ]
        if wants_rename and base and current_title != title_base:
            clashes.append({"field": "title", "label": "thread title",
                            "was": title_base, "theirs": current_title,
                            "mine": new_title})
        if clashes and not body.force:
            conflict("stale",
                     "Someone else changed this card while you had it open.",
                     changes=clashes, **last_touch(con, thread_id))

        changes = {f: v for f, v in intended.items() if v != (card[f] or "")}
        renaming = wants_rename and new_title != current_title

        # Bubbles take no part in the clash check above, and don't need to: two
        # people adding different items both get what they typed, and removing
        # one somebody else already removed is a no-op. A list merges where a
        # single field has to pick a winner.
        ts = now_iso()
        added, removed = [], []
        typed = [t.strip() for t in body.work_add if t and t.strip()]
        if any(len(t) > WORK_ITEM_MAX for t in typed):
            raise HTTPException(400, f"A work item is limited to "
                                     f"{WORK_ITEM_MAX} characters.")
        if typed:
            pos = con.execute("SELECT COALESCE(MAX(position), 0) FROM work_items"
                              " WHERE thread_id=?", (thread_id,)).fetchone()[0]
            for text in typed:
                pos += 1
                iid = str(uuid.uuid4())
                con.execute(
                    """INSERT INTO work_items (item_id, thread_id, body,
                                               position, created_at, created_by)
                       VALUES (?,?,?,?,?,?)""",
                    (iid, thread_id, text, pos, ts, actor))
                added.append({"item_id": iid, "body": text})

        for iid in body.work_remove:
            r = con.execute(
                """SELECT body FROM work_items WHERE item_id=? AND thread_id=?
                   AND removed_at IS NULL AND done_at IS NULL""",
                (iid, thread_id)).fetchone()
            if not r:
                continue                  # already gone; nothing to report
            con.execute("UPDATE work_items SET removed_at=?, removed_by=? "
                        "WHERE item_id=?", (ts, actor, iid))
            removed.append({"item_id": iid, "body": r["body"]})

        work = {"added": added, "removed": removed}
        touched = bool(changes or added or removed)

        if not touched and not renaming:
            return {"thread_id": thread_id, "changed": {}, "event_id": None,
                    "work": work}

        rename_event = None
        if renaming:
            rename_event = log_event(con, thread_id=thread_id, verb="renamed",
                                     actor=actor, old=current_title,
                                     new=new_title, post=True)

        if not touched:
            result = {"thread_id": thread_id, "changed": {}, "work": work,
                      "event_id": rename_event, "renamed": new_title,
                      "summary": f"title: {current_title} -> {new_title}"}
            remember(con, body.key, result)
            con.commit()
            return result

        if changes:
            sets = ", ".join(f"{f}=?" for f in changes)
            con.execute(f"UPDATE cards SET {sets}, updated_at=? WHERE thread_id=?",
                        (*changes.values(), ts, thread_id))
        else:
            con.execute("UPDATE cards SET updated_at=? WHERE thread_id=?",
                        (ts, thread_id))

        parts = []
        for f, v in changes.items():
            old = card[f] or "(empty)"
            parts.append(f"{FIELD_LABEL[f]}: "
                         f"{VALUE_LABEL.get(old, old)} -> {VALUE_LABEL.get(v, v)}")
        if added:
            parts.append("added " + ", ".join(f'"{a["body"]}"' for a in added))
        if removed:
            parts.append("dropped " + ", ".join(f'"{r["body"]}"' for r in removed))
        summary = "; ".join(parts)

        # __work__ is not a column name, so undo's "is this an editable field"
        # filter passes over it and the work-item branch picks it up instead.
        previous = {f: card[f] for f in changes}
        if added or removed:
            previous["__work__"] = {"added": [a["item_id"] for a in added],
                                    "removed": [r["item_id"] for r in removed]}

        eid = log_event(con, thread_id=thread_id, verb="edited", actor=actor,
                        old=json.dumps(previous), new=summary, post=True)

        result = {"thread_id": thread_id, "changed": changes, "work": work,
                  "event_id": eid, "summary": summary,
                  "renamed": new_title if renaming else None,
                  "rename_event": rename_event}
        remember(con, body.key, result)
        con.commit()
        return result
    finally:
        con.close()


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()

    DB = a.db
    check_schema()
    uvicorn.run(app, host=a.host, port=a.port)

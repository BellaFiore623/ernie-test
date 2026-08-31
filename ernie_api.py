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
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel

DB = "ernie.db"
UNDO_WINDOW_S = 60      # how long before Ernie posts to the thread
RANK_STEP = 1000.0
PRIORITY_ORDER = ("unassigned", "critical", "high", "medium", "low")

app = FastAPI(title="Ernie", version="0.1")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur]


def rw() -> sqlite3.Connection:
    """Read-write connection. Only the write endpoints use this."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
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
               c.direction, c.action_item, c.completed_at, c.completed_by,
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

    # equipment and open issues per card
    for c in out:
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
    return {"count": len(out), "cards": out}


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

        card = con.execute("SELECT priority, rank FROM cards WHERE thread_id=?",
                           (thread_id,)).fetchone()
        if not card:
            raise HTTPException(404, "no such card")

        def rank_of(tid):
            r = con.execute("SELECT rank FROM cards WHERE thread_id=?", (tid,)).fetchone()
            return r["rank"] if r else None

        lo = rank_of(body.after_id) if body.after_id else None
        hi = rank_of(body.before_id) if body.before_id else None

        if lo is not None and hi is not None:
            new_rank = (lo + hi) / 2
        elif lo is not None:
            new_rank = lo + RANK_STEP
        elif hi is not None:
            new_rank = hi - RANK_STEP
        else:
            top = con.execute("SELECT MAX(rank) m FROM cards WHERE priority=?",
                              (body.priority,)).fetchone()
            new_rank = (top["m"] or 0) + RANK_STEP

        con.execute(
            "UPDATE cards SET priority=?, rank=?, updated_at=? WHERE thread_id=?",
            (body.priority, new_rank, now_iso(), thread_id))

        if card["priority"] != body.priority:
            log_event(con, thread_id=thread_id, verb="priority_changed", actor=actor,
                      old=card["priority"], new=body.priority)
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

        card = con.execute("SELECT * FROM cards WHERE thread_id=?",
                           (thread_id,)).fetchone()
        if not card:
            raise HTTPException(404, "no such card")

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

        card = con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                           (thread_id,)).fetchone()
        if not card:
            raise HTTPException(404, "no such card")
        if card["completed_at"]:
            raise HTTPException(409, "already completed")

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
        e = con.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not e:
            raise HTTPException(404, "no such event")
        if e["undone_at"]:
            raise HTTPException(409, "already undone")

        if e["verb"] == "completed":
            con.execute("UPDATE cards SET completed_at=NULL, completed_by=NULL, "
                        "updated_at=? WHERE thread_id=?", (now_iso(), e["thread_id"]))
        elif e["verb"] == "priority_changed":
            con.execute("UPDATE cards SET priority=?, updated_at=? WHERE thread_id=?",
                        (e["old_value"], now_iso(), e["thread_id"]))
        elif e["verb"].startswith("set_"):
            col = e["verb"][4:]
            con.execute(f"UPDATE cards SET {col}=?, updated_at=? WHERE thread_id=?",
                        (e["old_value"], now_iso(), e["thread_id"]))

        con.execute("UPDATE events SET undone_at=?, undone_by=? WHERE event_id=?",
                    (now_iso(), actor, event_id))

        already_posted = bool(e["posted_at"])
        if already_posted:
            log_event(con, thread_id=e["thread_id"], verb="undo_correction",
                      actor=actor, old=e["verb"], post=True)

        con.commit()
        return {"event_id": event_id, "undone": True,
                "correction_posted": already_posted}
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
    uvicorn.run(app, host=a.host, port=a.port)

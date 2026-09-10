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
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel

import ernie_extract as ex
import ernie_version

DB = "ernie.db"
UNDO_WINDOW_S = 60      # how long before Ernie posts to the thread
RANK_STEP = 1000.0
# What a `completed` event's new_value says when the closing happened in
# Discord rather than in Bert. Written by ernie_sync.reconcile_closures and
# matched here rather than imported: importing the sync would pull httpx and
# a Discord client into the API process for one string. The two are held
# together by tests/check_closures.py, the way OUTBOX_MAX_ATTEMPTS is held to
# ernie_outbox.MAX_ATTEMPTS.
CLOSED_IN_DISCORD = "discord"
PRIORITY_ORDER = ("unassigned", "critical", "high", "medium", "low")

# The outbox stops trying after this many failures, and v_outbox_due says
# the same number. /health has to agree with both or it reports work as
# pending that nothing will ever pick up. tests/check_state.py holds the
# three together.
OUTBOX_MAX_ATTEMPTS = 5
WORK_ITEM_MAX = 200     # a bubble, not a paragraph -- it has to fit on a card

# The real one, not documentation metadata. This read 0.1 for the whole
# life of the project, which is worse than saying nothing: /openapi.json
# and /docs both quote it.
app = FastAPI(title="Ernie", version=ernie_version.VERSION)


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


class NewTicketBody(BaseModel):
    actor: str
    title: str
    priority: str = "unassigned"
    work_add: list[str] = []
    # Optional. The thread gets a note saying who started it either way; this
    # is the message they would have typed into it themselves.
    first_message: Optional[str] = None
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
    # Ticked bubbles put back to outstanding. A different act from removing
    # one: removing says the item should not be on the card at all, this says
    # it is not finished after all.
    work_undone: list[str] = []
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
    "clients": ["short_name", "offered"],
    "events": ["claimed_at", "sent_steps"],
    "work_items": ["item_id", "done_at"],
    # Not read here, but the sync writes them every cycle and would fail one
    # thread at a time. Better to say so once, at startup, with the fix.
    "threads": ["owner_id"],
    "messages": ["author_display", "type"],
    "new_threads": ["rank", "sent_steps"],
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


def band_order(con, priority: str, exclude: str | None = None):
    """Every card in a band, in rank order, drafts included.

    The board draws a ticket with no thread yet alongside the real ones, so a
    drop lands against one as readily as against a card -- and reading only
    `cards` here meant the neighbour Bert sent was not in the list, the
    midpoint fell through to "end of band", and the card went somewhere
    nobody aimed at. Both tables, one order.
    """
    return rows(con.execute(
        """SELECT thread_id, rank FROM cards
           WHERE priority=:p AND thread_id<>:x AND completed_at IS NULL
           UNION ALL
           SELECT draft_id AS thread_id, rank FROM new_threads
           WHERE priority=:p AND draft_id<>:x AND posted_at IS NULL
             AND complete_on_arrival = 0 AND rank IS NOT NULL
           ORDER BY rank""", {"p": priority, "x": exclude or ""}))


def band_top(con, priority: str) -> float:
    """One step above the band's lowest, which is where a new ticket goes.

    Counted over drafts as well as cards, or two tickets started in the same
    band in a row would be given the same rank and tie.
    """
    band = band_order(con, priority)
    return (band[0]["rank"] if band else RANK_STEP) - RANK_STEP


def load_draft(con, thread_id: str):
    """The new_threads row a draft_id names, if it is still waiting."""
    return con.execute(
        """SELECT * FROM new_threads WHERE draft_id=? AND posted_at IS NULL
           AND complete_on_arrival = 0""", (thread_id,)).fetchone()


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
    # The newest row is the running cycle for the few seconds one takes, and
    # its finished_at is NULL until it lands. Staleness is about the last run
    # that actually finished: reading it off the newest row reported nothing
    # at all once a minute, for as long as each cycle took, which Bert drew as
    # "never synced".
    done = con.execute(
        """SELECT finished_at FROM sync_runs WHERE finished_at IS NOT NULL
           ORDER BY run_id DESC LIMIT 1""").fetchone()
    board = con.execute(
        "SELECT COUNT(*) FROM cards WHERE completed_at IS NULL").fetchone()[0]

    # Only when this machine is actually sharing a board. A solo setup has no
    # state_sync rows -- and an older database has no such table at all, so
    # ask before selecting from it rather than 500ing on /health.
    sharing = None
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                   "AND name='state_sync'").fetchone():
        # agreed_at arrived after the table did, so ask for it the same way --
        # a database that has not had migrate_state_agreed_at.py run against
        # it should report no contact yet, not 500.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(state_sync)")}
        # The pull writes agreed_at; the publish writes synced_at. Contact is
        # the pull: their changes only reach this board down that direction,
        # and synced_at keeps advancing on a machine whose sync has stopped,
        # which is exactly the case worth reporting.
        col = "agreed_at" if "agreed_at" in cols else "NULL"
        agreed = con.execute(
            f"SELECT COUNT(*) AS n, MAX({col}) AS last FROM state_sync").fetchone()
        if agreed["n"]:
            # datetime() on both sides, and both written by this machine, so
            # the other person's clock has no say in it.
            waiting = con.execute(
                """SELECT COUNT(*) FROM cards c JOIN state_sync s USING (thread_id)
                   WHERE datetime(c.updated_at) > datetime(s.synced_at)""").fetchone()[0]
            since = None
            if agreed["last"]:
                since = int((datetime.now(timezone.utc)
                             - datetime.fromisoformat(agreed["last"])).total_seconds())
            sharing = {"cards": agreed["n"], "seconds_since_agreed": since,
                       "waiting_to_send": waiting}
    # The channel holding cards in a wire format this build cannot read.
    # Its own block rather than folded into sharing: sharing answers whether
    # contact is happening, and this is contact happening and being useless.
    # Asked for the same way state_sync is -- a database that has not had the
    # migration run should report nothing here rather than 500 on /health.
    format_skew = None
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                   "AND name='state_format_skew'").fetchone():
        sk = con.execute("SELECT * FROM state_format_skew WHERE id=1").fetchone()
        if sk:
            format_skew = {
                "their_v": sk["their_v"], "our_v": sk["our_v"],
                "cards": sk["cards"],
                "seconds_since_seen": int(
                    (datetime.now(timezone.utc)
                     - datetime.fromisoformat(sk["seen_at"])).total_seconds()),
            }

    # Still owed to Discord: queued behind the undo window, or being retried.
    # Bert asks so it can say so before somebody shuts the stack down on top
    # of a change that hasn't gone out.
    #
    # attempts < OUTBOX_MAX_ATTEMPTS, matching v_outbox_due. Without it a row
    # the outbox has given up on was counted here for ever, so Bert warned
    # about unsent changes on a board nobody had touched for ten minutes and
    # waiting made no difference -- which is the one thing the warning is
    # supposed to tell you to do.
    owed = con.execute(
        """SELECT COUNT(*) AS n, MIN(dispatch_after) AS soonest FROM events
           WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
             AND undone_at IS NULL AND attempts < ?""",
        (OUTBOX_MAX_ATTEMPTS,)).fetchone()
    # Given up on, and reported separately: leaving the stack running will not
    # send these, so a warning that says "wait a minute" would be wrong about
    # them -- but they must not be silently dropped either.
    # The customer roster's age. None on a machine with no Jira configured --
    # the table is empty there, and an indicator for something switched off is
    # noise. The list changes rarely, so what matters is not how old it is but
    # whether the pull has stopped: hours, not minutes.
    roster = None
    rr = con.execute("SELECT COUNT(*) AS n, SUM(offered) AS offered, "
                     "MAX(synced_at) AS at FROM clients").fetchone()
    if rr and rr["n"]:
        since = None
        if rr["at"]:
            since = int((datetime.now(timezone.utc)
                         - datetime.fromisoformat(rr["at"])).total_seconds())
        roster = {"count": rr["n"], "offered": rr["offered"] or 0,
                  "synced_at": rr["at"], "seconds_since_sync": since}

    stuck = con.execute(
        """SELECT COUNT(*) AS n FROM events
           WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
             AND undone_at IS NULL AND attempts >= ?""",
        (OUTBOX_MAX_ATTEMPTS,)).fetchone()
    con.close()

    stale = None
    if done:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(done["finished_at"])
        stale = int(delta.total_seconds())

    return {
        "ok": bool(last and not last["error"]),
        # Which build is answering. Bert shows it, and the machine on the
        # other end of #ernie-state has no other way to ask.
        "build": ernie_version.payload(),
        "last_sync": dict(last) if last else None,
        "seconds_since_sync": stale,
        # Which read of Discord that age belongs to, so Bert can tell a new
        # one landing from the same one ageing, and whether one is running now.
        "synced_at": done["finished_at"] if done else None,
        "syncing": bool(last and not last["finished_at"]),
        "board_size": board,
        "sharing": sharing,
        # Reported even when sharing is None: a machine that could read none
        # of the channel has applied nothing, so it has no state_sync rows to
        # be "sharing" by -- which is exactly the machine that needs telling.
        "format_skew": format_skew,
        "clients": roster,
        "queued": {"count": owed["n"], "due_at": owed["soonest"]},
        "stuck": {"count": stuck["n"]},
    }


@app.get("/stats")
def stats(ageing: int = 5, days: int = 28):
    """What the board looks like over time, rather than right now.

    Four numbers, and each earns its place by answering something the board
    itself cannot. Deliberately not a survey: a page of statistics nobody acts
    on is furniture, and the first one that turns out to be wrong takes the
    others' credibility with it.

    Nothing here is derived from `events`. It is all read off cards and
    threads, which are mirrored from Discord -- so it says something true on a
    board that has never been driven from Bert, where the feed is empty and
    every completion reads as "imported".
    """
    con = db()
    try:
        # 1. Completed over the window. The trend is the point: a bare "this
        #    month" throws away the shape, and the shape is what somebody
        #    wants. It follows the selector, because a timeframe control that
        #    visibly does nothing to the biggest block on the panel is a
        #    control nobody believes -- reported exactly that way.
        #
        #    The bucket comes from the window rather than being fixed, or
        #    seven days is one bar and a year is 365. Between four and twelve
        #    is a shape the eye reads; the thresholds are what put every
        #    offered window inside that.
        span = max(1, int(days))
        # Fourteen, not ten: at ten, a two-week window fell to weekly and
        # drew *two bars*, which is not a trend -- it is two numbers with a
        # picture round them. Daily to a fortnight, weekly to two months,
        # monthly beyond, which puts every offered window between three bars
        # and thirteen.
        bucket = "day" if span <= 14 else "week" if span <= 56 else "month"
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=span - 1)
        # A month bucket is snapped back to the first of its month, and the
        # query widened to match. Left rolling, the earliest bar was a part
        # month counted against whole ones -- June the 14th to the 30th
        # beside all of July -- which reads as a quiet month rather than as
        # half of one. The exact figure for the window is the tally's job;
        # this is a shape to compare along, and the things being compared
        # have to be the same size. Weeks stay rolling, because a seven-day
        # slice is not a named thing anybody compares to a calendar.
        if bucket == "month":
            start = start.replace(day=1)
        daily = {r["day"]: r["n"] for r in con.execute(
            """SELECT date(completed_at) AS day, COUNT(*) AS n
               FROM cards
               WHERE completed_at IS NOT NULL AND date(completed_at) >= :from
               GROUP BY day""", {"from": start.isoformat()})}

        # Built forward from that start rather than off the rows, so a quiet
        # week is a gap in the trend instead of a bar that simply is not
        # there. A missing bucket reads as "no data"; a nought reads as
        # "nothing closed", and they are not the same news.
        periods, cursor = [], start
        while cursor <= today:
            if bucket == "month":
                nxt = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
            elif bucket == "week":
                nxt = cursor + timedelta(days=7)
            else:
                nxt = cursor + timedelta(days=1)
            n = sum(v for k, v in daily.items()
                    if cursor.isoformat() <= k < nxt.isoformat())
            periods.append({"start": cursor.isoformat(), "count": n})
            cursor = nxt
        completed = {"bucket": bucket, "periods": periods}

        # 2. The open ones that have been open longest. This is the list that
        #    changes what somebody does today, and nothing else shows it.
        oldest = [dict(r) for r in con.execute(
            """SELECT c.thread_id, v.name, v.client_raw, c.priority,
                      CAST(julianday('now') - julianday(t.created_at) AS INT)
                        AS days
               FROM cards c
               JOIN threads t USING (thread_id)
               LEFT JOIN v_thread_current v ON v.thread_id = c.thread_id
               WHERE c.completed_at IS NULL
               ORDER BY days DESC LIMIT ?""", (ageing,))]

        # 3. How long one takes, end to end. The spread matters more than the
        #    average, so the slowest comes too.
        spans = [r[0] for r in con.execute(
            """SELECT julianday(c.completed_at) - julianday(t.created_at)
               FROM cards c JOIN threads t USING (thread_id)
               WHERE c.completed_at IS NOT NULL""") if r[0] is not None]
        spans.sort()
        took = None
        if spans:
            mid = len(spans) // 2
            median = (spans[mid] if len(spans) % 2
                      else (spans[mid - 1] + spans[mid]) / 2)
            took = {"count": len(spans),
                    "average_days": round(sum(spans) / len(spans), 1),
                    "median_days": round(median, 1),
                    "slowest_days": round(spans[-1], 1)}

        # 4. Open tickets with no build or return raised against them. Only
        #    230 of 889 threads ever get one, so this is invisible today and
        #    is the one number here somebody can act on directly.
        # There was a fourth: open tickets with no Build Request or Return
        # raised against them. It was dropped after Julian read the panel --
        # a figure nobody acts on is furniture, and it is the same standard
        # the other three earn their place by. The query goes with it rather
        # than being left to run every refresh for a field nothing reads.
        # 4. How much there is, how much arrived and how much left, split by
        #    the tag. Three questions in one block because they are read
        #    together -- "twelve open, nine in, seven out" is a sentence, and
        #    the same three numbers on three separate screens is not.
        #
        #    **Open is a level; created and closed are flows.** Open is what
        #    is on the plate *now* and does not move with the window, which
        #    is why it is labelled so in the panel. Windowing it would answer
        #    "opened in the last four weeks and still open", which is a
        #    different and much less useful question -- the backlog somebody
        #    is carrying does not start at the beginning of the window.
        since = f"-{max(1, int(days))} days"
        # datetime() on both sides. Python writes ISO8601 with a T and
        # SQLite's datetime('now') uses a space, so a raw string compare is
        # always false -- it silently broke the outbox once.
        rows = con.execute(
            """SELECT COALESCE(v.queue, '') AS queue,
                      SUM(c.completed_at IS NULL) AS open,
                      SUM(datetime(t.created_at) >= datetime('now', :since))
                        AS created,
                      SUM(c.completed_at IS NOT NULL
                          AND datetime(c.completed_at)
                              >= datetime('now', :since)) AS closed
               FROM cards c
               JOIN threads t USING (thread_id)
               LEFT JOIN v_thread_current v ON v.thread_id = c.thread_id
               GROUP BY queue""", {"since": since}).fetchall()

        tally = {"days": int(days), "queues": [],
                 "open": {}, "created": {}, "closed": {},
                 "totals": {"open": 0, "created": 0, "closed": 0}}
        for r in rows:
            # A tag nothing offers any more still names the cards it is on --
            # same rule as the dropdown. It goes under one heading rather
            # than its own, or a queue retired years ago gets a column of
            # zeroes for ever; but it is never dropped, because then the rows
            # would not add up to the total and a figure that does not add up
            # is the first one somebody stops believing.
            q = r["queue"] if r["queue"] in ex.QUEUES_OFFERED else "Other"
            for k in ("open", "created", "closed"):
                tally[k][q] = tally[k].get(q, 0) + (r[k] or 0)
                tally["totals"][k] += r[k] or 0
        # Every offered tag gets a row even at nought, so the shape of the
        # list does not change under somebody reading it; Other appears only
        # when it has something in it.
        tally["queues"] = list(ex.QUEUES_OFFERED) + (
            ["Other"] if any(t.get("Other") for t in
                             (tally["open"], tally["created"], tally["closed"]))
            else [])
        for q in tally["queues"]:
            for k in ("open", "created", "closed"):
                tally[k].setdefault(q, 0)

        # 5. Tickets that changed tag while they were open -- PROD to OPS,
        #    mostly, which is the pair Julian asked about: work that starts as
        #    production and turns into operations.
        #
        #    **`thread_titles` already records this, so nothing new is
        #    stored.** The tag *is* the title's prefix, the table is
        #    append-only, and every revision carries the queue the parser read
        #    off it -- so a tag change is already a row, with the time on it.
        #    The two routes considered instead were a tag history written into
        #    `#ernie-state` (which could only start counting from today) and
        #    the other bot's log channel; neither is needed.
        #
        #    Discord's rename system messages are in the mirror too and were
        #    the obvious source. They were measured and rejected: 573 of
        #    production's messages have content that parses as a title and 550
        #    of those were written by people, and `messages` does not store
        #    Discord's message `type`, so a rename cannot be told from
        #    somebody pasting a title into the chat. A figure that invents
        #    transitions is worse than no figure.
        #
        #    Cards only, because the panel is about the board -- and
        #    `#customer-support` is mirrored for history with
        #    `generate_cards = 0`, so its threads are not tickets anybody
        #    tracks.
        #
        #    The honest limit, and it is the same one the state-channel route
        #    would have had: this counts changes Ernie was watching for. A
        #    database whose threads have one title row each has nothing to
        #    report, which is production until it runs this build.
        moves = [{"from": r["was"], "to": r["queue"], "count": r["n"]}
                 for r in con.execute(
            """SELECT was, queue, COUNT(*) AS n
                 FROM (SELECT ti.thread_id, ti.observed_at, ti.queue,
                              LAG(ti.queue) OVER (PARTITION BY ti.thread_id
                                                  ORDER BY ti.observed_at)
                                AS was
                         FROM thread_titles ti
                         JOIN cards c USING (thread_id)
                        WHERE ti.queue IS NOT NULL AND ti.queue <> '')
                WHERE was IS NOT NULL AND was <> queue
                  AND datetime(observed_at) >= datetime('now', :since)
                GROUP BY was, queue
                ORDER BY n DESC, was, queue""", {"since": since})]

        return {"completed": completed, "ageing": oldest,
                "time_to_complete": took, "tally": tally,
                "tag_moves": {"days": int(days), "moves": moves,
                              "total": sum(m["count"] for m in moves)}}
    finally:
        con.close()


@app.get("/cards")
def cards(
    queue: Optional[str] = Query(None, description="OPS | PROD | ENG | CS"),
    client: Optional[str] = None,
    include_completed: bool = False,
):
    """The board. Sorted by priority band, then manual rank within it."""
    # try/finally like its neighbours: without it a request that raised
    # part-way left its connection open, and on Windows that is a file
    # handle nothing gives back.
    con = db()
    try:
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
        # Ticked ones come too, flagged rather than filtered. A bubble that
        # vanished the moment it was ticked took the only record of the work with
        # it -- the card went quiet and said nothing about what had been done on
        # it. Removed ones stay gone: an x in the editor says "this should not be
        # here", which is a different statement from "this is finished".
        items: dict[str, list] = {}
        for r in con.execute(
                """SELECT thread_id, item_id, body, done_at FROM work_items
                   WHERE removed_at IS NULL
                   ORDER BY thread_id, position"""):
            items.setdefault(r["thread_id"], []).append(
                {"item_id": r["item_id"], "body": r["body"],
                 "done": r["done_at"] is not None})

        # What this machine still owes on each card, so a card can say it holds a
        # change that has not left here yet.
        #
        # The same two debts Bert._owed() counts for the close warning, and they
        # have to stay the same two: a card wearing no mark under a warning that
        # says changes are unsent would be worse than no mark at all.
        unsent: dict[str, int] = {}
        for r in con.execute(
                """SELECT thread_id, COUNT(*) AS n FROM events
                   WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
                     AND undone_at IS NULL AND attempts < ?
                   GROUP BY thread_id""", (OUTBOX_MAX_ATTEMPTS,)):
            unsent[r["thread_id"]] = r["n"]

        # Given up on, and counted apart for the reason /health keeps them apart:
        # the outbox will not pick these up again, so a mark that means "wait a
        # moment" would be telling the reader to do the one thing that cannot
        # help. Not hidden either -- just said differently.
        stuck: dict[str, int] = {}
        for r in con.execute(
                """SELECT thread_id, COUNT(*) AS n FROM events
                   WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
                     AND undone_at IS NULL AND attempts >= ?
                   GROUP BY thread_id""", (OUTBOX_MAX_ATTEMPTS,)):
            stuck[r["thread_id"]] = r["n"]

        # The second debt. A reorder, and every band move that is not in or out of
        # critical, is silent by design and carries no dispatch_after at all -- it
        # appears here and nowhere else. Same comparison /health makes, both sides
        # written by this machine, so the other laptop's clock has no say in it.
        unshared: set[str] = set()
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='state_sync'").fetchone():
            unshared = {r["thread_id"] for r in con.execute(
                """SELECT c.thread_id FROM cards c JOIN state_sync s USING (thread_id)
                   WHERE datetime(c.updated_at) > datetime(s.synced_at)""")}

        # equipment and open issues per card
        for c in out:
            c["work_items"] = items.get(c["thread_id"], [])
            c["unsent"] = unsent.get(c["thread_id"], 0)
            c["stuck"] = stuck.get(c["thread_id"], 0)
            c["unshared"] = c["thread_id"] in unshared
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
        # Tickets started here whose thread does not exist yet. They belong on the
        # board straight away -- somebody just made one -- and they carry the
        # unsent mark for the same reason every other queued change does. Not
        # cards yet, so nothing that keys on a thread_id will find them: Bert
        # shows them and leaves them alone until the outbox has made the thread.
        for d in con.execute(
                """SELECT draft_id, title, priority, rank, work_json,
                          attempts, created_at
                   FROM new_threads WHERE posted_at IS NULL
                     -- Closed already, so it goes the moment the button is
                     -- pressed rather than lingering until the thread exists.
                     AND complete_on_arrival = 0
                   ORDER BY created_at"""):
            t = ex.parse_title(d["title"])
            out.append({
                "thread_id": d["draft_id"], "pending": True,
                # Its own rank, not a placeholder: 0.0 sorted it against the
                # band's real ranks, which can be negative, so a ticket the
                # board promised to put at the top of High could arrive below
                # everything in it.
                "priority": d["priority"], "rank": d["rank"] or 0.0,
                "name": d["title"], "queue": t.queue, "client_raw": t.client_raw,
                "client_key": ex.normalise_client(t.client_raw or "") or None,
                "thread_date": t.date.isoformat() if t.date else None,
                "summary": t.summary, "confidence": t.confidence,
                "archived": 0, "completed_at": None, "completed_by": None,
                "client_override": None, "updated_at": d["created_at"],
                "ticket_count": 0, "last_human_at": None, "title_pending": False,
                "equipment": [], "issues": [],
                "work_items": [{"item_id": None, "body": b, "done": False}
                               for b in json.loads(d["work_json"])],
                "unsent": 0 if d["attempts"] >= OUTBOX_MAX_ATTEMPTS else 1,
                "stuck": 1 if d["attempts"] >= OUTBOX_MAX_ATTEMPTS else 0,
                "unshared": False,
            })


        out.sort(key=lambda c: (PRIORITY_ORDER.index(c["priority"])
                                if c["priority"] in PRIORITY_ORDER else 99,
                                c["rank"]))
        return {"count": len(out), "cards": out,
                "as_of": datetime.now(timezone.utc).isoformat()}
    finally:
        con.close()


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


@app.get("/clients/roster")
def client_roster():
    """
    The customer list from Jira, for the editor to offer.

    Different question from /clients above, which answers "what is on my
    board" for the filter. This one answers "who are our customers", which is
    the list you pick a name out of when you are naming a thread.

    Offered clients only -- a summary marked *INACTIVE*, *PENDING* or *PAUSED*
    stays in the table and keeps naming the cards that already carry it, but
    is not put forward for new ones.

    `name` rides along with every row because `short_name` is not always
    unique: 'IPI : El Paso' and 'IPI : *REP*' are two live customers that both
    shorten to IPI, and the summary is the only thing that tells them apart.
    Rows that need it are flagged, so the editor does not have to work it out.
    """
    con = db()
    out = rows(con.execute(
        """SELECT c.client_id, c.name, c.short_name,
                  (SELECT COUNT(*) FROM cards k
                     JOIN v_thread_current v ON v.thread_id = k.thread_id
                     JOIN client_aliases a ON a.raw_key = v.client_key
                    WHERE k.completed_at IS NULL
                      AND a.client_id = c.client_id) AS n
           FROM clients c
          WHERE c.offered = 1 AND c.short_name IS NOT NULL AND c.short_name <> ''
          ORDER BY c.short_name COLLATE NOCASE"""))
    seen = Counter((c["short_name"] or "").lower() for c in out)

    # Every spelling the board has ever used for each client. Bert searches
    # these as well as the name, so somebody who types what a title said last
    # year still finds the customer -- the alias table already knows the
    # misspellings, and there is no reason to make the editor rediscover them.
    aliases: dict[str, list[str]] = {}
    for r in con.execute(
            """SELECT a.client_id, v.client_raw FROM client_aliases a
               JOIN v_thread_current v ON v.client_key = a.raw_key
               WHERE v.client_raw IS NOT NULL AND v.client_raw <> ''"""):
        aliases.setdefault(r["client_id"], [])
        if r["client_raw"] not in aliases[r["client_id"]]:
            aliases[r["client_id"]].append(r["client_raw"])

    for c in out:
        c["ambiguous"] = seen[(c["short_name"] or "").lower()] > 1
        c["aliases"] = aliases.get(c["client_id"], [])
    stamp = con.execute("SELECT MAX(synced_at) AS at FROM clients").fetchone()
    con.close()
    return {"count": len(out), "clients": out,
            "synced_at": stamp["at"] if stamp else None}




# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

@app.post("/tickets")
def new_ticket(body: NewTicketBody):
    """Start a ticket that has no Discord thread yet.

    Bert cannot make the thread -- only the outbox writes to Discord -- so
    this records what to make and hands it over. The board shows it in the
    meantime, wearing the unsent mark, which is what it is.
    """
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "A ticket needs a title.")
    if body.priority not in PRIORITY_ORDER:
        raise HTTPException(400, f"Unknown priority {body.priority!r}")

    con = rw()
    try:
        done = replay(con, body.key)
        if done is not None:
            return done

        # Whichever channel the board's cards come from. There is normally one;
        # if somebody watches two, the first is as good an answer as exists and
        # is better than refusing.
        chan = con.execute(
            "SELECT channel_id FROM watched_channels WHERE generate_cards = 1 "
            "ORDER BY channel_id LIMIT 1").fetchone()
        if not chan:
            raise HTTPException(400, "No channel is set to generate cards, so "
                                     "there is nowhere to put a new ticket.")

        typed = [t.strip() for t in body.work_add if t and t.strip()]
        if any(len(t) > WORK_ITEM_MAX for t in typed):
            raise HTTPException(400, f"A work item is limited to "
                                     f"{WORK_ITEM_MAX} characters.")

        draft = str(uuid.uuid4())
        # To the top of the band whose + was pressed, which is where Bert has
        # been showing it since. It is a real rank rather than a placeholder,
        # so the board draws it where it will actually land and a drag has
        # something to move.
        rank = band_top(con, body.priority)
        con.execute(
            """INSERT INTO new_threads (draft_id, channel_id, title, priority,
                                        rank, work_json, first_message, actor,
                                        created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (draft, chan["channel_id"], title, body.priority, rank,
             json.dumps(typed), (body.first_message or "").strip() or None,
             body.actor, now_iso()))
        result = {"draft_id": draft, "title": title, "priority": body.priority,
                  "rank": rank, "work_items": typed}
        remember(con, body.key, result)
        con.commit()
        return result
    finally:
        con.close()


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

        # A ticket whose thread Discord has not made yet is on the board and
        # can be dragged like anything else, and looking only in `cards`
        # answered "no such card" -- true, and useless in the same way
        # completing one was: the person moved it, and the wait for the thread
        # is Ernie's problem rather than theirs. The draft carries the band and
        # the rank, so the move is recorded there and make_threads brings the
        # card in where it was left.
        draft = None
        card = con.execute("SELECT * FROM cards WHERE thread_id=?",
                           (thread_id,)).fetchone()
        if card is None:
            draft = load_draft(con, thread_id)
            if draft is None:
                raise HTTPException(404, "no such card")
        else:
            guard_open(card, "move")

        # Bert can only send neighbours it can see. With a queue filter or a
        # search on, the card on the far side of the gap may be hidden, and the
        # midpoint between two visible cards can land straight on top of a
        # hidden one -- equal ranks, and an arbitrary order the moment the
        # filter comes off. So anchor to the neighbour the person actually
        # dropped against and take the other side from the whole band. With no
        # filter on, the visible neighbour is the real one and this is the
        # midpoint it always was.
        band = band_order(con, body.priority, exclude=thread_id)
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

        if draft is not None:
            # Nothing is logged. The ticket does not exist yet, so there is no
            # history to record and nothing in a thread to announce -- a draft
            # dragged into Critical before it is made is the same as having
            # pressed the + in Critical, which writes no event either.
            con.execute(
                "UPDATE new_threads SET priority=?, rank=? WHERE draft_id=?",
                (body.priority, new_rank, thread_id))
            result = {"thread_id": thread_id, "priority": body.priority,
                      "rank": new_rank, "pending": True}
            remember(con, body.key, result)
            con.commit()
            return result

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
            # Where it sat, and where it now sits, counted against the cards it
            # is ordered among -- `ranks` already leaves this card out, so
            # both are the position it takes rather than one it shares.
            #
            # rank is a fraction and means nothing to a reader: "1000 -> 1500"
            # says only that something moved. The feed and the change log want
            # the place in the band, which is what the person was looking at.
            was = sum(1 for r in ranks if r < card["rank"]) + 1
            now = sum(1 for r in ranks if r < new_rank) + 1
            # A drag that lands a card back where it started is not a change
            # anybody needs a line about. It still writes the new rank, so the
            # boards agree; it just doesn't announce a move that didn't happen.
            if was != now:
                # Band and place together, so the row says "High 5th" rather
                # than a bare number the reader has to place themselves. A
                # reorder never leaves its band, so both carry the same one.
                log_event(con, thread_id=thread_id, verb="reordered",
                          actor=actor, old=f"{body.priority}:{was}",
                          new=f"{body.priority}:{now}")

        # Renormalise if the gap is collapsing toward float precision limits.
        if lo is not None and hi is not None and abs(hi - lo) < 0.001:
            # Drafts are spread with the cards, or a respacing would leave
            # them at ranks the cards have just moved out from under.
            for i, r in enumerate(band_order(con, body.priority), 1):
                con.execute("UPDATE cards SET rank=? WHERE thread_id=?",
                            (i * RANK_STEP, r["thread_id"]))
                con.execute("UPDATE new_threads SET rank=? WHERE draft_id=? "
                            "AND posted_at IS NULL",
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

        # A ticket still waiting for its thread has no cards row, so the
        # lookup below would answer "no such card" -- true, and useless. The
        # person meant to close it; the wait is Ernie's problem, not theirs.
        # The flag is acted on by make_threads the moment the thread exists,
        # so nothing is lost and nothing has to be remembered.
        draft = con.execute(
            "SELECT draft_id FROM new_threads WHERE draft_id=? "
            "AND posted_at IS NULL AND complete_on_arrival = 0",
            (thread_id,)).fetchone()
        if draft:
            con.execute(
                "UPDATE new_threads SET complete_on_arrival=1, completed_by=? "
                "WHERE draft_id=?", (actor, thread_id))
            # No event yet, so no undo yet: there is nothing in the thread to
            # take back until the thread is there. One is written when it is.
            result = {"thread_id": thread_id, "event_id": None,
                      "pending": True, "undo_until": None}
            remember(con, body.key, result)
            con.commit()
            return result

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
        # Nothing here made it happen, so there is nothing here to take back:
        # the thread exists in Discord whatever this row says. Bert offers no
        # button for it; this is so a stray call can't blank a card either.
        if e["verb"] == "started":
            conflict("not_undoable",
                     "That thread was opened in Discord. Undo can't unmake it.")
        if e["verb"] == "completed" and e["new_value"] == CLOSED_IN_DISCORD:
            # Undo would clear completed_at and the very next sync would see
            # the thread still archived and close the card again -- a card
            # that comes back on the board for five seconds and leaves, for
            # ever. Reopen is the verb that actually settles it: it posts to
            # the thread, and posting to an archived thread unarchives it, so
            # Discord and the board agree afterwards.
            conflict("not_undoable",
                     "That ticket was closed in Discord. Undo can't reach it "
                     "-- reopen it instead, which unarchives the thread.")
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
            # And ones it put back to outstanding are ticked off again. The
            # timestamp is now rather than the original: undo restores the
            # state, and there is no record of when it was first finished.
            for iid in work.get("reopened") or []:
                con.execute("UPDATE work_items SET done_at=?, done_by=? "
                            "WHERE item_id=?", (now_iso(), actor, iid))
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
            # new_value names the event being retracted, so the feed can tell
            # exactly which row is mid-revoke rather than inferring it from
            # timing. The outbox renders a fixed sentence for this verb and
            # reads neither value.
            log_event(con, thread_id=e["thread_id"], verb="undo_correction",
                      actor=actor, old=e["verb"], new=event_id, post=True)

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

        reopened = []
        for iid in body.work_undone:
            r = con.execute(
                """SELECT body FROM work_items WHERE item_id=? AND thread_id=?
                   AND removed_at IS NULL AND done_at IS NOT NULL""",
                (iid, thread_id)).fetchone()
            if not r:
                continue                  # not ticked, or already gone
            con.execute("UPDATE work_items SET done_at=NULL, done_by=NULL "
                        "WHERE item_id=?", (iid,))
            reopened.append({"item_id": iid, "body": r["body"]})

        # A finished bubble can be removed too. The x says "this should not
        # be on the card at all", which is as true of something ticked off as
        # of something outstanding -- and the editor is the only place either
        # can be said, so refusing it there left no way to say it.
        for iid in body.work_remove:
            r = con.execute(
                """SELECT body FROM work_items WHERE item_id=? AND thread_id=?
                   AND removed_at IS NULL""",
                (iid, thread_id)).fetchone()
            if not r:
                continue                  # already gone; nothing to report
            con.execute("UPDATE work_items SET removed_at=?, removed_by=? "
                        "WHERE item_id=?", (ts, actor, iid))
            removed.append({"item_id": iid, "body": r["body"]})

        work = {"added": added, "removed": removed, "reopened": reopened}
        # Reopening counts. Without it here the work_items row was updated
        # and then the function returned before the commit, so the bubble came
        # back ticked and no event was written -- the change simply did not
        # happen, silently.
        touched = bool(changes or added or removed or reopened)

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
        if reopened:
            parts.append("reopened "
                         + ", ".join(f'"{r["body"]}"' for r in reopened))
        summary = "; ".join(parts)

        # __work__ is not a column name, so undo's "is this an editable field"
        # filter passes over it and the work-item branch picks it up instead.
        previous = {f: card[f] for f in changes}
        if added or removed or reopened:
            previous["__work__"] = {"added": [a["item_id"] for a in added],
                                    "removed": [r["item_id"] for r in removed],
                                    "reopened": [r["item_id"] for r in reopened]}

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
    ap.add_argument("--version", action="version",
                    version=ernie_version.describe())
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()

    DB = a.db
    print(f"ernie_api {ernie_version.describe()}")
    check_schema()
    uvicorn.run(app, host=a.host, port=a.port)

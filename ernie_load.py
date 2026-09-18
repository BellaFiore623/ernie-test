"""
Load a Discord dump into Ernie's SQLite database.

Idempotent: run it repeatedly on successive dumps and it will insert new
threads and messages, record title changes and message edits as new
revisions, and leave Bert's own state (cards, rank, statuses) untouched.

    python ernie_load.py dump/threads.json --db ernie.db \
        --channel 1486095486011310080

Add --rebuild-derived to recompute proposals/tickets/equipment from the
mirror without re-reading Discord (safe; derived tables are disposable).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

import ernie_extract as ex

SCHEMA = pathlib.Path(__file__).with_name("schema.sql")
RANK_STEP = 1000.0
# How long a closed card's thread must stay open before it counts as somebody
# reopening it. The outbox unarchives a thread to post into it and re-archives
# straight after, so for a second or two every closure looks exactly like a
# reopen from the outside -- and did, once the guard stopped treating Ernie's
# own message as the cause. Seen in the two-stack test as "closed this thread
# in Discord" at 20:24:35 and "reopened, so it's back on the Bert board" at
# 20:24:40, with nobody touching it.
#
# One observation is not evidence, which is `reconcile_closures`' own rule one
# table along: absence is the question, never the answer. Ernie's window is
# seconds and never survives this; a real reopen always does, at the cost of
# registering half a minute later.
REOPEN_SETTLE_S = 30
WITNESSED_WITHIN_S = 600   # a thread Ernie watched appear was created moments
                           # before it was first seen. One that predates the
                           # mirror was not, and "somebody started this" about a
                           # thread from four months ago is not news -- it is a
                           # first sync writing a line per thread into the feed
                           # and the change log.


# The name this switch was born with, still honoured. It was `ANNOUNCE_CLOSURES`
# while closures were all it gated; reopens made it wrong, and a switch whose
# name describes half of what it does is one somebody will set for the half
# they read.
#
# Renamed 2026-09-17, while the only two machines carrying it were ours. The
# old name is still read because an installed env lives at
# `%LOCALAPPDATA%\Ernie\ernie.env` and a reinstall does not overwrite it --
# so dropping it outright would turn a rename into both boards going quiet
# about closures, saying nothing, which is the exact failure most of the rules
# in this project exist to prevent. It warns once and keeps working.
OLD_ANNOUNCE_NAME = "ANNOUNCE_CLOSURES"
_warned_old_announce = False


def announce_thread_changes() -> bool:
    """Whether this machine tells a thread what happened to it in Discord.

    Covers both halves of the same fact: somebody archived the thread, and
    somebody unarchived it. Off unless `ANNOUNCE_THREAD_CHANGES` is set, and
    only one machine may set it -- every stack runs its own sync, every stack
    sees the same flip, and every stack writes its own event. Those rows cost
    nothing while they never post, and tell the thread twice the moment they
    do.

    It lives here rather than in `ernie_sync` because `load_thread` needs it
    and the import runs the other way. `ernie_sync.announce_thread_changes` is
    this function.
    """
    global _warned_old_announce
    on = os.environ.get("ANNOUNCE_THREAD_CHANGES", "").strip().lower()
    if not on:
        on = os.environ.get(OLD_ANNOUNCE_NAME, "").strip().lower()
        if on and not _warned_old_announce:
            # Once per process. This is read per thread on every pass, and an
            # alarm repeated a hundred times an hour is one nobody reads.
            _warned_old_announce = True
            print(f"  !! {OLD_ANNOUNCE_NAME} has been renamed to "
                  f"ANNOUNCE_THREAD_CHANGES, because it gates reopens too. "
                  f"Still honoured; rename the line in your env file.",
                  file=sys.stderr)
    return on in ("1", "true", "yes", "on")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Columns added to a table that already exists somewhere.
#
# `schema.sql` is all CREATE TABLE IF NOT EXISTS: it creates tables and can
# never alter one, so a database made before a column was added never grows
# it. From a checkout that is what `migrations/` is for -- a script run by
# hand -- and a shipped exe has no shell to run one in, so the sync dies with
# `no such column` and the API refuses to start, on a machine whose only
# repair tool is the thing that will not start.
#
# So the application adds them itself, on every open, idempotently. Only
# columns added *after* the first shipped build belong here: everything
# before it is in `schema.sql`, which is what any installed database was
# created from.
ADDED_COLUMNS = (
    ("release_seen", "minimum", "TEXT NOT NULL DEFAULT ''"),
    ("threads", "seen_open_at", "TEXT"),
    ("changelog_sent", "swallowed", "INTEGER NOT NULL DEFAULT 0"),
    # Defaulting to 0 is right for every row already there: a board that has
    # been logging on its own has logged them, and one that has not is about
    # to have `catch_up` swallow the lot.
    ("events", "replayed", "INTEGER NOT NULL DEFAULT 0"),
)


def add_missing_columns(con) -> list[str]:
    """Bring an older database up to the current shape. Answers what it did."""
    done = []
    for table, col, decl in ADDED_COLUMNS:
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        # No table at all is not this function's problem: schema.sql has just
        # run, so an absent one is a table this build genuinely does not have.
        if have and col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            done.append(f"{table}.{col}")
            if (table, col) == ("changelog_sent", "swallowed"):
                _mark_swallowed_history(con)
    con.commit()
    return done


def _mark_swallowed_history(con) -> None:
    """Say which of the old NULL-message_id rows were swallowed on purpose.

    Runs once, on the pass that adds the column: before it existed the two
    meanings were not recorded, so they have to be told apart from what is.

    `changelog_state.started_at` is the line, and it is exact. `catch_up()`
    runs before `mark_initialised()` and only at switch-on, so its rows are
    stamped earlier; nothing can `claim()` before the log is initialised. No
    `changelog_state` row means nothing was ever swallowed here.

    `datetime()` on both sides is the house rule and costs nothing: it
    truncates to the second, and no real claim can land in that second --
    the first drain after switch-on has an empty `pending()`.
    """
    con.execute(
        """UPDATE changelog_sent SET swallowed = 1
           WHERE message_id IS NULL
             AND datetime(sent_at) <= datetime(
                   (SELECT started_at FROM changelog_state WHERE id = 1))""")


def connect(path: str, timeout: float = 15.0) -> sqlite3.Connection:
    """
    Open the database.

    timeout makes a blocked writer wait rather than raising 'database is
    locked' immediately. It only helps if the other writer commits promptly,
    which is why the sync passes commit per thread instead of per cycle.
    """
    con = sqlite3.connect(path, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    # After the schema, because a table has to exist before it can be altered.
    add_missing_columns(con)
    con.execute("PRAGMA busy_timeout = 15000")
    con.execute("PRAGMA synchronous = NORMAL")
    return con


# --------------------------------------------------------------------------
# Mirror
# --------------------------------------------------------------------------

def settled_open(since: str, now_ts: str) -> bool:
    """Has the thread been open long enough to mean it?"""
    try:
        a = datetime.fromisoformat(since)
        b = datetime.fromisoformat(now_ts)
    except (TypeError, ValueError):
        return False
    return (b - a).total_seconds() >= REOPEN_SETTLE_S


def note_reopen(con: sqlite3.Connection, tid: str, ts: str, stats: dict) -> None:
    """Record the reopen and put the card back.

    Told, or recorded and silent. A reopen is the other half of a closure --
    something that happened to this thread in Discord, seen by every stack
    watching it -- so it is behind the same one-machine switch, for the same
    reason: two boards announcing it tell the customer thread twice. NULL
    still reopens the card everywhere; what the switch decides is who says so.
    """
    stats["reopened"] = stats.get("reopened", 0) + 1
    con.execute(
        """INSERT OR IGNORE INTO events
           (event_id, occurred_at, thread_id, verb, old_value, new_value,
            dispatch_after)
           VALUES (?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), ts, tid, "thread_reopened", "archived",
         "active", ts if announce_thread_changes() else None),
    )
    con.execute(
        "UPDATE cards SET completed_at=NULL, completed_by=NULL, updated_at=? "
        "WHERE thread_id=?", (ts, tid))


def load_thread(con: sqlite3.Connection, entry: dict, stats: dict) -> str:
    t = entry["thread"]
    tid = t["id"]
    meta = t.get("thread_metadata") or {}
    ts = now()

    # Detect an archive/unarchive flip before we overwrite the flag. A reopen
    # means someone revived finished work, and Ernie should say so in-thread.
    was = con.execute(
        "SELECT archived, last_synced_at, seen_open_at FROM threads "
        "WHERE thread_id=?", (tid,)).fetchone()
    now_archived = int(bool(meta.get("archived")))

    if was is not None and now_archived and was["seen_open_at"]:
        # Shut again, so whatever we were watching was not a reopen. Almost
        # always this is the outbox finishing a post.
        con.execute("UPDATE threads SET seen_open_at=NULL WHERE thread_id=?",
                    (tid,))
    elif (was is not None and not now_archived and was["seen_open_at"]
            and settled_open(was["seen_open_at"], ts)):
        # Still open, and has been for longer than a post takes. Now it is a
        # reopen -- but only while the card is actually closed; one reopened
        # by an earlier pass has nothing left to do.
        done = con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                           (tid,)).fetchone()
        con.execute("UPDATE threads SET seen_open_at=NULL WHERE thread_id=?",
                    (tid,))
        if done is not None and done["completed_at"]:
            note_reopen(con, tid, ts, stats)

    if was is not None and was["archived"] == 1 and now_archived == 0:
        # A bot posting into an archived thread unarchives it as a side
        # effect -- a keepalive ping, or Ernie's own correction. Neither is
        # somebody reopening the ticket.
        #
        # The test was "is the newest message a bot", which swallowed every
        # reopen that mattered: Ernie posts "closed this thread in Bert" and
        # *then* archives, so its own message is the newest in every thread it
        # has closed. Zero `thread_reopened` in five months, which read as
        # nobody ever reopening one.
        #
        # What makes a bot message the *cause* is arriving since we last
        # looked -- one already sitting there explains nothing. And never
        # Ernie's own: `reconcile_closures` stamps `last_synced_at` as it
        # closes, so the announcement lands after that stamp and would swallow
        # the reopen. `events.discord_message_id` already records what the
        # outbox posted, so this needs no new column and no network.
        caused = con.execute(
            """SELECT is_bot FROM messages
                WHERE thread_id=? AND datetime(created_at) > datetime(?)
                  AND message_id NOT IN (SELECT discord_message_id FROM events
                                          WHERE discord_message_id IS NOT NULL)
                ORDER BY created_at DESC LIMIT 1""",
            (tid, was["last_synced_at"])).fetchone()
        if caused is not None and caused["is_bot"]:
            # The flag goes too: the thread is open, so Ernie is no longer
            # the reason it was shut, and leaving it set hides a later
            # closure from reconcile_closures.
            con.execute("UPDATE threads SET archived=0, archived_by_ernie=0 "
                        "WHERE thread_id=?", (tid,))
            return tid
        con.execute("UPDATE threads SET archived_by_ernie=0 WHERE thread_id=?",
                    (tid,))
        # Note the moment and wait. See REOPEN_SETTLE_S: the outbox has to
        # unarchive a thread to post into it, so a thread caught open right
        # now is as likely to be Ernie mid-sentence as anybody reopening it.
        con.execute("UPDATE threads SET seen_open_at=? WHERE thread_id=?",
                    (ts, tid))

    con.execute(
        """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                first_seen_at, last_synced_at, archived, locked,
                                owner_id)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(thread_id) DO UPDATE SET
               last_synced_at = excluded.last_synced_at,
               archived       = excluded.archived,
               locked         = excluded.locked,
               -- only ever fills a gap: Discord stops sending owner_id on
               -- some archived threads, and a NULL must not erase what an
               -- earlier cycle already learned.
               owner_id       = COALESCE(threads.owner_id, excluded.owner_id)""",
        (tid, t.get("parent_id", ""), t.get("guild_id", ""),
         t.get("created_at") or meta.get("create_timestamp") or ts,
         ts, ts, int(bool(meta.get("archived"))), int(bool(meta.get("locked"))),
         t.get("owner_id")),
    )

    # Title revision, only when it differs from the last one we saw.
    prev = con.execute(
        """SELECT name FROM thread_titles WHERE thread_id=?
           ORDER BY observed_at DESC LIMIT 1""", (tid,)).fetchone()

    if prev is None or prev["name"] != t.get("name", ""):
        if prev is not None:
            stats["titles_changed"] += 1
        record_title(con, tid, t.get("name", ""), ts)
    return tid


def record_title(con: sqlite3.Connection, tid: str, name: str,
                 ts: str | None = None) -> None:
    """Write one title revision, parsed.

    The only place that decides what a title row holds. The outbox writes one
    too, for a thread it has just created, and wrote only the name -- so the
    card came up with no queue and no client until a sync cycle later filled
    them in: grey, "unknown client", for a title that reads perfectly well.
    Both callers come through here so there is one answer rather than two.
    """
    ts = ts or now()
    t = ex.parse_title(name or "")
    con.execute(
        """INSERT OR REPLACE INTO thread_titles
           (thread_id, observed_at, name, queue, client_raw, client_key,
            thread_date, summary, confidence)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (tid, ts, name or "", t.queue, t.client_raw,
         ex.normalise_client(t.client_raw or "") or None,
         t.date.isoformat() if t.date else None, t.summary, t.confidence),
    )


def load_messages(con: sqlite3.Connection, tid: str, msgs: list, stats: dict) -> None:
    ts = now()
    for m in msgs:
        mid = m["id"]
        author = m.get("author") or {}

        cur = con.execute(
            """INSERT OR IGNORE INTO messages
               (message_id, thread_id, author_id, author_name, author_display,
                is_bot, type, created_at, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (mid, tid, author.get("id", ""), author.get("username"),
             author.get("global_name"),
             int(bool(author.get("bot"))), m.get("type"),
             m.get("timestamp", ""), ts),
        )
        # `cur.rowcount`, not `con.total_changes`: the connection's total is
        # cumulative from the moment it was opened, so it is truthy after the
        # first write of the process and stays that way -- every message seen
        # counted as a message that was new.
        if cur.rowcount:
            stats["messages_new"] += 1
        elif m.get("type") is not None:
            # A row that is already here gets its `type` filled in, and only
            # that. It is what makes a re-read a backfill: the type cannot be
            # derived from anything stored, so a row written before the column
            # existed can only be given one by fetching it again, and OR
            # IGNORE alone would leave it NULL however often it was read.
            #
            # Nothing else is touched, because the mirror is append-only and a
            # name reading differently on a second fetch is Discord being
            # mutable rather than us being wrong. Its own statement rather
            # than an upsert clause: an upsert that *updates* still reports
            # `rowcount` 1, and that counter is what says a message is new.
            con.execute(
                "UPDATE messages SET type=? WHERE message_id=? AND type IS NULL",
                (m.get("type"), mid))

        # Revision only when the body actually changed.
        prev = con.execute(
            """SELECT content, edited_at FROM message_revisions
               WHERE message_id=? ORDER BY observed_at DESC LIMIT 1""",
            (mid,)).fetchone()

        content = m.get("content") or ""
        edited = m.get("edited_timestamp")
        if prev is None or prev["content"] != content or prev["edited_at"] != edited:
            if prev is not None:
                stats["edits_found"] += 1
            con.execute(
                """INSERT OR REPLACE INTO message_revisions
                   (message_id, observed_at, edited_at, content,
                    embeds_json, components_json, attachments_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (mid, ts, edited, content,
                 json.dumps(m.get("embeds") or []),
                 json.dumps(m.get("components") or []),
                 json.dumps(m.get("attachments") or [])),
            )

    if msgs:
        con.execute("UPDATE threads SET last_seen_message_id=? WHERE thread_id=?",
                    (max(m["id"] for m in msgs), tid))


# --------------------------------------------------------------------------
# Derived
# --------------------------------------------------------------------------

def load_derived(con: sqlite3.Connection, rec: ex.ThreadRecord) -> None:
    con.execute("DELETE FROM ticket_proposals WHERE thread_id=?", (rec.thread_id,))
    con.execute("DELETE FROM thread_equipment WHERE thread_id=?", (rec.thread_id,))

    for p in rec.proposals:
        con.execute(
            """INSERT OR REPLACE INTO ticket_proposals
               (message_id, thread_id, kind, proposed_at, equipment_master,
                equipment_label, client_cr, client_label, client_key,
                equipment_type, template, assignee, reporter, reported_problem,
                has_buttons, issues_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p.message_id, rec.thread_id, p.kind, p.timestamp, p.equipment_master,
             p.equipment_label, p.client_cr, p.client_label, p.client_key,
             p.equipment_type, p.template, p.assignee, p.reporter,
             p.reported_problem, int(p.has_buttons), json.dumps(p.issues)),
        )

    for c in rec.created:
        con.execute(
            """INSERT OR REPLACE INTO tickets
               (pip_key, thread_id, message_id, kind, created_at,
                assignee, equipment_master, client_cr)
               VALUES (?,?,?,?,?,?,?,?)""",
            (c.key, rec.thread_id, c.message_id, c.kind, c.timestamp,
             c.assignee, c.equipment_master, c.client_cr),
        )

    for e in rec.equipment:
        con.execute(
            """INSERT OR REPLACE INTO thread_equipment
               (thread_id, eq_type, eq_number, state, raw)
               VALUES (?,?,?,?,?)""",
            (rec.thread_id, e.type, e.number, e.state, e.raw),
        )


def ensure_card(con: sqlite3.Connection, rec: ex.ThreadRecord) -> None:
    """
    Create a card the first time we see a thread. Never overwrites.

    Only channels flagged generate_cards produce cards -- customer-support is
    mirrored for history but stays off the board.
    """
    gen = con.execute(
        "SELECT generate_cards FROM watched_channels WHERE channel_id=?",
        (rec.parent_id,)).fetchone()
    if not gen or not gen[0]:
        return

    if con.execute("SELECT 1 FROM cards WHERE thread_id=?",
                   (rec.thread_id,)).fetchone():
        return

    # A thread nobody can read is ranked to the top of unassigned rather than
    # the bottom, because it is the one that needs a person soonest and the
    # bottom of a nineteen-card band is where it goes unlooked-at. This is a
    # real rank and not a trick of Bert's rendering: the board, the state
    # channel and the numbers on the card messages all read the same order,
    # and dragging one down leaves it down -- a later re-sync does not haul it
    # back up.
    if rec.title_unreadable:
        edge = con.execute(
            "SELECT MIN(rank) AS m FROM cards WHERE priority='unassigned'"
        ).fetchone()
        rank = (RANK_STEP if edge["m"] is None else edge["m"]) - RANK_STEP
    else:
        edge = con.execute(
            "SELECT MAX(rank) AS m FROM cards WHERE priority='unassigned'"
        ).fetchone()
        rank = (edge["m"] or 0) + RANK_STEP

    build = "created" if any(t.kind == "build" for t in rec.created) else "needs_created"
    ret = "created" if any(t.kind == "return" for t in rec.created) else "needs_created"

    # Threads already archived when Ernie first saw them finished before Bert
    # existed. Record them so history and search work, but keep them off the
    # board -- Bert shows active work only.
    completed_at = rec.last_ts if rec.archived else None
    completed_by = "imported" if rec.archived else None

    con.execute(
        """INSERT INTO cards (thread_id, priority, rank, build_state,
                              return_state, completed_at, completed_by, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (rec.thread_id, "unassigned", rank, build, ret,
         completed_at, completed_by, now()),
    )
    note_started(con, rec)


def witnessed_start(con: sqlite3.Connection, thread_id: str) -> bool:
    """Whether Ernie actually saw this thread appear, rather than inherited it."""
    r = con.execute(
        "SELECT created_at, first_seen_at FROM threads WHERE thread_id=?",
        (thread_id,)).fetchone()
    if not r:
        return False
    try:
        gap = (datetime.fromisoformat(r["first_seen_at"])
               - datetime.fromisoformat(r["created_at"])).total_seconds()
    except (TypeError, ValueError):
        return False
    return gap <= WITNESSED_WITHIN_S


def creator(con: sqlite3.Connection, rec: ex.ThreadRecord):
    """Who opened the thread, named the way Discord shows them.

    owner_id is the authority on who it was; the name comes from any message
    they left, because the mirror already stores one for every author and
    asking Discord again would be a request per thread for something already
    on disk. global_name is the display name -- "Tyler", not "tyler_mazza" --
    and falls back to the username, which is always set.
    """
    for sql, args in (
        ("""SELECT author_display, author_name, is_bot FROM messages
             WHERE thread_id=? AND author_id=? AND deleted_at IS NULL
             ORDER BY created_at LIMIT 1""", (rec.thread_id, rec.owner_id or "")),
        # No owner_id, or they never posted under it: the thread opens with
        # its author's message, so the first one is the next best answer.
        ("""SELECT author_display, author_name, is_bot FROM messages
             WHERE thread_id=? AND deleted_at IS NULL
             ORDER BY created_at LIMIT 1""", (rec.thread_id,)),
    ):
        r = con.execute(sql, args).fetchone()
        if r:
            return (r["author_display"] or r["author_name"]), bool(r["is_bot"])
    return None, False


def note_started(con: sqlite3.Connection, rec: ex.ThreadRecord) -> None:
    """"Tyler started PROD: Penn Hills ..." in the activity feed.

    dispatch_after is NULL: this already happened in Discord, and posting it
    back would be telling the thread about itself. A thread a bot opened gets
    no line -- the seeder makes two dozen at a time, and none of them are
    somebody starting work.
    """
    who, is_bot = creator(con, rec)
    if not who or is_bot or not witnessed_start(con, rec.thread_id):
        return
    made = con.execute("SELECT created_at FROM threads WHERE thread_id=?",
                       (rec.thread_id,)).fetchone()["created_at"]
    con.execute(
        """INSERT INTO events (event_id, occurred_at, actor_name, thread_id,
                               verb, new_value, dispatch_after)
           VALUES (?,?,?,?,'started',?,NULL)""",
        (str(uuid.uuid4()), made, who, rec.thread_id, rec.name),
    )


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def run(dump_path: str, db_path: str, channel: str | None, cards: bool) -> None:
    dump = json.load(open(dump_path))
    con = connect(db_path)
    stats = {"threads_seen": 0, "messages_new": 0,
             "edits_found": 0, "titles_changed": 0}

    run_id = con.execute(
        "INSERT INTO sync_runs (started_at) VALUES (?)", (now(),)).lastrowid

    for entry in dump:
        if channel and entry["thread"].get("parent_id") != channel:
            continue
        stats["threads_seen"] += 1
        tid = load_thread(con, entry, stats)
        load_messages(con, tid, entry.get("messages") or [], stats)
        rec = ex.extract_thread(entry)
        load_derived(con, rec)
        if cards:
            ensure_card(con, rec)

    con.execute(
        """UPDATE sync_runs SET finished_at=?, threads_seen=?, messages_new=?,
                                edits_found=?, titles_changed=? WHERE run_id=?""",
        (now(), stats["threads_seen"], stats["messages_new"],
         stats["edits_found"], stats["titles_changed"], run_id),
    )
    con.commit()

    print(f"threads seen     {stats['threads_seen']}")
    print(f"messages new     {stats['messages_new']}")
    print(f"titles changed   {stats['titles_changed']}")
    print(f"edits found      {stats['edits_found']}")

    q = lambda s: con.execute(s).fetchone()[0]
    print(f"\nmirror: {q('SELECT COUNT(*) FROM threads')} threads, "
          f"{q('SELECT COUNT(*) FROM messages')} messages, "
          f"{q('SELECT COUNT(*) FROM message_revisions')} revisions")
    print(f"derived: {q('SELECT COUNT(*) FROM ticket_proposals')} proposals, "
          f"{q('SELECT COUNT(*) FROM tickets')} tickets, "
          f"{q('SELECT COUNT(*) FROM thread_equipment')} equipment refs")
    if cards:
        print(f"state: {q('SELECT COUNT(*) FROM cards')} cards")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--channel", help="only load threads under this parent_id")
    ap.add_argument("--no-cards", action="store_true",
                    help="mirror only; don't create Bert cards")
    a = ap.parse_args()
    run(a.dump, a.db, a.channel, cards=not a.no_cards)

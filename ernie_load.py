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
import pathlib
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

import ernie_extract as ex

SCHEMA = pathlib.Path(__file__).with_name("schema.sql")
RANK_STEP = 1000.0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    return con


# --------------------------------------------------------------------------
# Mirror
# --------------------------------------------------------------------------

def load_thread(con: sqlite3.Connection, entry: dict, stats: dict) -> str:
    t = entry["thread"]
    tid = t["id"]
    meta = t.get("thread_metadata") or {}
    ts = now()

    # Detect an archive/unarchive flip before we overwrite the flag. A reopen
    # means someone revived finished work, and Ernie should say so in-thread.
    was = con.execute("SELECT archived FROM threads WHERE thread_id=?",
                      (tid,)).fetchone()
    now_archived = int(bool(meta.get("archived")))
    if was is not None and was["archived"] == 1 and now_archived == 0:
        # A keepalive bot posting into an archived thread unarchives it as a
        # side effect. That is not a reopen. Only count it if the newest
        # message came from a human.
        newest = con.execute(
            """SELECT is_bot FROM messages WHERE thread_id=?
               ORDER BY created_at DESC LIMIT 1""", (tid,)).fetchone()
        if newest is not None and newest["is_bot"]:
            con.execute("UPDATE threads SET archived=0 WHERE thread_id=?", (tid,))
            return tid
        stats["reopened"] = stats.get("reopened", 0) + 1
        con.execute(
            """INSERT OR IGNORE INTO events
               (event_id, occurred_at, thread_id, verb, old_value, new_value,
                dispatch_after)
               VALUES (?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), ts, tid, "thread_reopened", "archived", "active", ts),
        )
        con.execute(
            "UPDATE cards SET completed_at=NULL, completed_by=NULL, updated_at=? "
            "WHERE thread_id=?", (ts, tid))

    con.execute(
        """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                first_seen_at, last_synced_at, archived, locked)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(thread_id) DO UPDATE SET
               last_synced_at = excluded.last_synced_at,
               archived       = excluded.archived,
               locked         = excluded.locked""",
        (tid, t.get("parent_id", ""), t.get("guild_id", ""),
         t.get("created_at") or meta.get("create_timestamp") or ts,
         ts, ts, int(bool(meta.get("archived"))), int(bool(meta.get("locked")))),
    )

    # Title revision, only when it differs from the last one we saw.
    title = ex.parse_title(t.get("name", ""))
    prev = con.execute(
        """SELECT name FROM thread_titles WHERE thread_id=?
           ORDER BY observed_at DESC LIMIT 1""", (tid,)).fetchone()

    if prev is None or prev["name"] != t.get("name", ""):
        if prev is not None:
            stats["titles_changed"] += 1
        con.execute(
            """INSERT OR REPLACE INTO thread_titles
               (thread_id, observed_at, name, queue, client_raw, client_key,
                thread_date, summary, confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tid, ts, t.get("name", ""), title.queue, title.client_raw,
             ex.normalise_client(title.client_raw or "") or None,
             title.date.isoformat() if title.date else None,
             title.summary, title.confidence),
        )
    return tid


def load_messages(con: sqlite3.Connection, tid: str, msgs: list, stats: dict) -> None:
    ts = now()
    for m in msgs:
        mid = m["id"]
        author = m.get("author") or {}

        con.execute(
            """INSERT OR IGNORE INTO messages
               (message_id, thread_id, author_id, author_name, is_bot,
                created_at, first_seen_at)
               VALUES (?,?,?,?,?,?,?)""",
            (mid, tid, author.get("id", ""), author.get("username"),
             int(bool(author.get("bot"))), m.get("timestamp", ""), ts),
        )
        if con.total_changes:
            stats["messages_new"] += 1

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

    top = con.execute(
        "SELECT MAX(rank) AS m FROM cards WHERE priority='unassigned'").fetchone()
    rank = (top["m"] or 0) + RANK_STEP

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

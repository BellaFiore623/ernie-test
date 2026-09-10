"""
Discord's message type, kept so a rename can be told from a paste.

A tag change is a title change, and `thread_titles` already records those --
which is where the retag figure comes from and why it needed no new storage.
This is the other half: **the exact record of a rename that happened before
Ernie was watching.** Discord posts a system message into the thread for one,
type 4 (CHANNEL_NAME_CHANGE), carrying the new name as its content and the
person who did it as its author. Those messages are already in the mirror.

Without the type they cannot be picked out. Measured against production, 573
messages have content that parses as a title and 550 of those were written by
people -- so a rename and somebody pasting a title into the chat look exactly
alike, and a figure built on the guess would invent transitions.

Nothing reads this yet. It is stored because it can only be captured as it
goes past: the type is not derivable from anything in the mirror, so a
message read without it has to be fetched again to get it.
"""

import io
import pathlib

from support import PARENT, Board, Check, iso

import ernie_api as api
import ernie_load as load


ROOT = pathlib.Path(__file__).resolve().parent.parent

RENAME = 4          # CHANNEL_NAME_CHANGE, confirmed against the sandbox
PINNED = 6          # CHANNEL_PINNED_MESSAGE, what pinning the status posts


def a_message(mid, tid, content="hello", mtype=0, author="tyler_mazza"):
    """One message the way Discord sends it."""
    return {"id": mid, "type": mtype, "content": content,
            "timestamp": iso(-3600),
            "author": {"id": "u1", "username": author,
                       "global_name": "Tyler", "bot": False},
            "embeds": [], "components": [], "attachments": []}


def a_thread(b, tid="thread-x"):
    b.con.execute(
        """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                first_seen_at, last_synced_at)
           VALUES (?,?,?,?,?,?)""",
        (tid, PARENT, "guild", iso(-7200), iso(-7200), iso()))
    b.con.commit()
    return tid


def check_the_type_is_kept() -> bool:
    """
    What Discord sends is what is stored, rename and all.

    Type 4 is the whole reason for the column: it is a rename, its content is
    the new name, and its author is whoever did it. Confirmed against the
    sandbox rather than taken from the documentation -- three renamed threads,
    every one carrying a type 4 whose content was the new title.
    """
    c = Check("the type is kept")

    with Board() as b:
        tid = a_thread(b)
        stats = {"messages_new": 0, "edits_found": 0}
        load.load_messages(b.con, tid, [
            a_message("m1", tid, "just talking"),
            a_message("m2", tid, "OPS: Trafford Borough - 02Sep26 - x",
                      mtype=RENAME),
            a_message("m3", tid, "", mtype=PINNED),
        ], stats)
        b.con.commit()

        rows = {r["message_id"]: r["type"] for r in b.con.execute(
            "SELECT message_id, type FROM messages")}
        c.equal(rows.get("m1"), 0, "an ordinary message is type 0")
        c.equal(rows.get("m2"), RENAME, "a rename is type 4")
        c.equal(rows.get("m3"), PINNED, "and a pin is type 6")
        c.equal(stats["messages_new"], 3, "three new messages, counted once each")

        # The point of keeping it: the rename is findable, and the thing that
        # looks exactly like it is not confused with it.
        found = b.con.execute(
            "SELECT content FROM message_revisions r JOIN messages m USING "
            "(message_id) WHERE m.type = ?", (RENAME,)).fetchall()
        c.equal(len(found), 1, "exactly one message is a rename")
        c.ok(str(found[0]["content"]).startswith("OPS: "),
             "and its content is the name the thread was given")

    return c.report()


def check_a_paste_is_not_a_rename() -> bool:
    """
    The failure this column exists to prevent.

    Somebody pasting a title into the chat produces a message whose content
    is a title and whose author is a person -- which is what 550 of
    production's 573 title-shaped messages are. It is type 0, and that is the
    only thing separating it from the rename above.
    """
    c = Check("a paste is not a rename")

    with Board() as b:
        tid = a_thread(b)
        stats = {"messages_new": 0, "edits_found": 0}
        title = "OPS: Trafford Borough - 02Sep26 - x"
        load.load_messages(b.con, tid, [
            a_message("m1", tid, title, mtype=RENAME),
            a_message("m2", tid, title),              # somebody pasted it
        ], stats)
        b.con.commit()

        n = b.con.execute("SELECT COUNT(*) n FROM messages "
                          "WHERE type = ?", (RENAME,)).fetchone()["n"]
        c.equal(n, 1, "identical content, and only one of them is a rename")

    return c.report()


def check_a_re_read_fills_in_a_row_that_has_none() -> bool:
    """
    Which is what makes a later backfill possible at all.

    Every message already in the mirror was written before this column
    existed, so it has no type -- and the type cannot be derived from
    anything stored, only fetched again. `INSERT OR IGNORE` would have meant
    those rows stayed NULL for ever however many times they were re-read, so
    the history before today would have been lost by a keyword.

    Only the type. Everything else is left as found: the mirror is
    append-only, and a name or a timestamp reading differently on a second
    fetch is Discord being mutable rather than us being wrong.
    """
    c = Check("a re-read fills in a row that has none")

    with Board() as b:
        tid = a_thread(b)
        # A row as it would have been written before the column existed.
        seen = iso(-7200)
        b.con.execute(
            """INSERT INTO messages (message_id, thread_id, author_id,
                                     author_name, author_display, is_bot,
                                     created_at, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("m1", tid, "u1", "tyler_mazza", "Tyler", 0, seen, seen))
        b.con.commit()

        stats = {"messages_new": 0, "edits_found": 0}
        load.load_messages(b.con, tid, [
            a_message("m1", tid, "OPS: Trafford Borough - 02Sep26 - x",
                      mtype=RENAME)], stats)
        b.con.commit()

        row = b.con.execute(
            "SELECT type, first_seen_at, author_name FROM messages "
            "WHERE message_id='m1'").fetchone()
        c.equal(row["type"], RENAME, "the type is filled in on the re-read")
        c.equal(row["first_seen_at"], seen,
                "and when it was first seen is left where it was")
        c.equal(stats["messages_new"], 0,
                "a row that was already there is not counted as new")

        # And a second pass over a row that now has one changes nothing.
        load.load_messages(b.con, tid, [
            a_message("m1", tid, "OPS: Trafford Borough - 02Sep26 - x",
                      mtype=RENAME)], stats)
        b.con.commit()
        c.equal(b.con.execute("SELECT type FROM messages WHERE message_id='m1'")
                .fetchone()["type"], RENAME, "and it stays what it was")

    return c.report()


def check_a_database_without_it_is_told_at_startup() -> bool:
    """
    The same guard `client_override` and `owner_id` already sit behind.

    The sync writes this column every cycle, so a database that missed the
    migration fails one thread at a time in a log nobody is reading. It is
    cheaper to say so once, at startup, with the fix.
    """
    c = Check("a database without it is told at startup")

    c.ok("type" in api.REQUIRED_COLUMNS.get("messages", []),
         "check_schema asks for messages.type")

    mig = ROOT / "migrations" / "migrate_message_type.py"
    c.ok(mig.exists(), "and there is a migration for a database already out there")
    body = io.open(mig, encoding="utf-8").read() if mig.exists() else ""
    c.ok("PRAGMA table_info(messages)" in body,
         "which is safe to re-run, the way every other one is")

    schema = io.open(ROOT / "schema.sql", encoding="utf-8").read()
    c.ok("ix_messages_type" in schema,
         "and a fresh database gets the column and its index from schema.sql")

    return c.report()


CHECKS = (check_the_type_is_kept,
          check_a_paste_is_not_a_rename,
          check_a_re_read_fills_in_a_row_that_has_none,
          check_a_database_without_it_is_told_at_startup)

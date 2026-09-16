"""
Reopening a ticket by unarchiving its thread.

Ernie has always detected the archived -> active flip, reopened the card and
posted *"This thread was reopened, so it's back on the Bert board."* The
event had fired **zero times in five months** across both boards, which
looked like nobody ever reopening a ticket and was really the guard eating
them.

The guard exists for a true thing: a bot posting into an archived thread
unarchives it as a side effect -- a keepalive ping, or Ernie's own
correction going back into a thread it closed -- and neither is a person
reopening the work. But it asked *"is the newest message a bot"*, and Ernie
posts "closed this thread in Bert" and **then** archives, so its own message
is the newest one in every thread it has ever closed. Every reopen of a
ticket Ernie closed was read as a bot ping and dropped: the mirror recorded
the thread open, the card stayed closed, and nothing was said.

The question that actually distinguishes them is whether a bot message
arrived **since we last looked**. One already sitting there while the thread
was archived explains nothing about why it is open now.
"""

import contextlib
import os
import sqlite3

from support import Board, Check, PARENT, iso

import ernie_extract as ex
import ernie_load as load


@contextlib.contextmanager
def announcing(on: bool):
    """ANNOUNCE_CLOSURES set or not, restored afterwards."""
    before = os.environ.get("ANNOUNCE_CLOSURES")
    if on:
        os.environ["ANNOUNCE_CLOSURES"] = "1"
    else:
        os.environ.pop("ANNOUNCE_CLOSURES", None)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("ANNOUNCE_CLOSURES", None)
        else:
            os.environ["ANNOUNCE_CLOSURES"] = before


def archived_thread(b, *, last_message_at, is_bot, synced_at):
    """A thread the mirror believes is archived, with one message in it."""
    b.con.execute(
        "INSERT OR IGNORE INTO watched_channels (channel_id, generate_cards) "
        "VALUES (?,1)", (PARENT,))
    tid = b.card("PROD: Thrasher - 26Aug26 - 3k tether job", "high")
    b.con.execute(
        "UPDATE threads SET archived=1, archived_by_ernie=1, last_synced_at=? "
        "WHERE thread_id=?", (synced_at, tid))
    b.con.execute(
        """INSERT INTO messages (message_id, thread_id, author_id, is_bot,
                                 created_at, first_seen_at)
           VALUES ('m-1',?,'u-1',?,?,?)""",
        (tid, 1 if is_bot else 0, last_message_at, last_message_at))
    b.con.execute("UPDATE cards SET completed_at=?, completed_by='Bella Fiore' "
                  "WHERE thread_id=?", (synced_at, tid))
    b.con.commit()
    return tid


def now_open(tid):
    """What Discord sends for a thread that is no longer archived."""
    return {"thread": {"id": tid, "parent_id": PARENT, "guild_id": "g",
                       "name": "PROD: Thrasher - 26Aug26 - 3k tether job",
                       "thread_metadata": {"archived": False}}}


def reopened(b, tid):
    return b.con.execute(
        "SELECT COUNT(*) FROM events WHERE thread_id=? AND "
        "verb='thread_reopened'", (tid,)).fetchone()[0]


def check_a_thread_ernie_closed_can_be_reopened() -> bool:
    """The reported case, and the one that could never work.

    Closed in Bert, so the newest message is Ernie's own "closed this thread
    in Bert" -- posted before the archive, and therefore older than the last
    sync. Unarchiving from Discord's thread menu adds no message at all.
    """
    c = Check("a thread Ernie closed can be reopened")

    with Board() as b:
        tid = archived_thread(b, last_message_at=iso(-600), is_bot=True,
                              synced_at=iso(-300))
        with announcing(True):
            load.load_thread(b.con, now_open(tid), {})
        b.con.commit()

        c.equal(reopened(b, tid), 1,
                "the reopen is seen -- Ernie's own closing message is older "
                "than the last sync, so it explains nothing")
        c.equal(b.con.execute("SELECT completed_at FROM cards WHERE "
                              "thread_id=?", (tid,)).fetchone()["completed_at"],
                None, "and the card comes back to the board")
        c.equal(b.con.execute("SELECT archived_by_ernie FROM threads WHERE "
                              "thread_id=?", (tid,)).fetchone()[0], 0,
                "the flag comes off, so a later real closure is not hidden "
                "from reconcile_closures")

        e = b.con.execute(
            "SELECT dispatch_after FROM events WHERE thread_id=? AND "
            "verb='thread_reopened'", (tid,)).fetchone()
        c.ok(e["dispatch_after"] is not None,
             "and the thread is told, which is what was missing")

    return c.report()


def check_a_bot_ping_is_still_not_a_reopen() -> bool:
    """The thing the guard is for, which must keep working.

    A keepalive bot pings live threads every three days, and posting into an
    archived one unarchives it. Nobody reopened anything.
    """
    c = Check("a bot ping is still not a reopen")

    with Board() as b:
        # The bot message arrives *after* the last sync: it is what changed.
        tid = archived_thread(b, last_message_at=iso(-60), is_bot=True,
                              synced_at=iso(-300))
        load.load_thread(b.con, now_open(tid), {})
        b.con.commit()

        c.equal(reopened(b, tid), 0, "no reopen is recorded")
        c.ok(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                           (tid,)).fetchone()["completed_at"] is not None,
             "and the card stays closed")
        c.equal(b.con.execute("SELECT archived FROM threads WHERE thread_id=?",
                              (tid,)).fetchone()[0], 0,
                "though the mirror still records what Discord says")
        c.equal(b.con.execute("SELECT archived_by_ernie FROM threads WHERE "
                              "thread_id=?", (tid,)).fetchone()[0], 0,
                "and the flag comes off here too -- the thread is open, so "
                "Ernie is no longer the reason it was shut")

    return c.report()


def check_a_person_posting_reopens_it() -> bool:
    """Posting into a closed ticket is reopening it, and always counted."""
    c = Check("a person posting reopens it")

    with Board() as b:
        tid = archived_thread(b, last_message_at=iso(-60), is_bot=False,
                              synced_at=iso(-300))
        load.load_thread(b.con, now_open(tid), {})
        b.con.commit()

        c.equal(reopened(b, tid), 1, "a human message since the last sync is "
                                     "a reopen whatever else is true")
        c.equal(b.con.execute("SELECT completed_at FROM cards WHERE "
                              "thread_id=?", (tid,)).fetchone()["completed_at"],
                None, "and the card is back")

    return c.report()


def check_the_guard_asks_about_the_window_not_the_last_message() -> bool:
    """The distinction, stated as the two cases that differ only in timing.

    One bot message, one thread, one unarchive. The only thing that changes
    is whether the message predates the last sync -- and that has to be the
    whole of the answer, or the guard is back to reading Ernie's own
    closing message as a keepalive ping.
    """
    c = Check("the guard asks about the window, not the last message")

    seen = {}
    for label, when in (("before the last sync", iso(-600)),
                        ("after the last sync", iso(-60))):
        with Board() as b:
            tid = archived_thread(b, last_message_at=when, is_bot=True,
                                  synced_at=iso(-300))
            load.load_thread(b.con, now_open(tid), {})
            b.con.commit()
            seen[label] = reopened(b, tid)

    c.equal(seen["before the last sync"], 1,
            "a bot message already there is not why the thread is open")
    c.equal(seen["after the last sync"], 0,
            "one that arrived since is exactly why, and is not a reopen")

    return c.report()


def check_the_reopen_message_is_behind_the_one_machine_switch() -> bool:
    """A reopen is the other half of a closure, and is gated with it.

    Every stack runs its own sync and every stack sees the same flip, so two
    of them announcing it tell the customer thread twice -- the problem
    ANNOUNCE_CLOSURES exists for. `thread_reopened` had an unconditional
    dispatch since it was written and simply never fired, so nobody met it.

    What the switch decides is who *says so*. The card comes back on every
    board either way, because that is board state rather than an
    announcement.
    """
    c = Check("the reopen message is behind the one-machine switch")

    for on in (True, False):
        with Board() as b:
            tid = archived_thread(b, last_message_at=iso(-600), is_bot=True,
                                  synced_at=iso(-300))
            with announcing(on):
                load.load_thread(b.con, now_open(tid), {})
            b.con.commit()

            row = b.con.execute(
                "SELECT dispatch_after FROM events WHERE thread_id=? AND "
                "verb='thread_reopened'", (tid,)).fetchone()
            c.equal(reopened(b, tid), 1,
                    f"switched {'on' if on else 'off'}, the reopen is still "
                    f"recorded")
            c.equal(b.con.execute("SELECT completed_at FROM cards WHERE "
                                  "thread_id=?", (tid,)).fetchone()[0], None,
                    "and the card still comes back -- that is board state, "
                    "not an announcement")
            if on:
                c.ok(row["dispatch_after"] is not None,
                     "and the thread is told")
            else:
                c.equal(row["dispatch_after"], None,
                        "and nothing is queued, so a second stack watching "
                        "the same flip stays quiet")

    return c.report()


CHECKS = (check_a_thread_ernie_closed_can_be_reopened,
          check_the_reopen_message_is_behind_the_one_machine_switch,
          check_a_bot_ping_is_still_not_a_reopen,
          check_a_person_posting_reopens_it,
          check_the_guard_asks_about_the_window_not_the_last_message)

"""
The write-ahead log, the change log, and the tool that looks at both.

Three things that came out of one afternoon. A reader somewhere held a WAL
snapshot, so SQLite could not checkpoint; the log grew to **33 MB against a
4.68 MB database**; writers started timing out; and the symptoms were a
ticket closed in Bert whose thread was never told for four and a half
minutes, and one change-log line posted **seven times**.

Each of these defends a different part of that:

- the WAL is reported, so the failure stops being silent until the board
  stops;
- the change log claims a line before it posts it, so a write that fails
  after the message has gone cannot hand the same event back next pass;
- `tools/q.py` opens read-only, because the tool somebody points at a live
  database while diagnosing it should not be able to take a lock the board
  needs.
"""

import pathlib
import sqlite3

from support import Board, Check, PARENT

import bert
import ernie_api as api
import ernie_changelog as cl


ROOT = pathlib.Path(__file__).resolve().parent.parent


# -- the WAL is reported ----------------------------------------------------

def check_the_wal_is_measured_and_reported() -> bool:
    """`/health` carries it, because nothing else was going to say."""
    c = Check("the WAL is measured and reported")

    with Board() as b:
        api.DB = b.path
        w = api.wal_state()
        c.ok(w is not None, "wal_state answers for a real database")
        if w:
            for key in ("db_bytes", "wal_bytes", "ratio"):
                c.ok(key in w, f"it carries {key}")
            c.ok(w["db_bytes"] > 0, "the database has a size")

        h = api.health()
        c.ok("wal" in h, "and /health carries it on every poll, "
                         "which is how Bert can ever see it")

    api.DB = "no-such-file.db"
    c.equal(api.wal_state(), None,
            "a database that is not there answers None rather than raising -- "
            "/health must not fail over a file size")

    return c.report()


def check_what_bert_says_about_it() -> bool:
    """The decision, pure, so it can be exercised without a QApplication.

    `build_standing` is the precedent: a widget built with no QApplication
    aborts the process rather than raising, so the judgement lives in a
    function and the widget only draws it.
    """
    c = Check("what Bert says about the WAL")

    mb = 1048576
    c.equal(bert.wal_standing({"db_bytes": 5 * mb, "wal_bytes": 1 * mb}), "",
            "a WAL smaller than its database is the ordinary state and is "
            "silent -- a healthy one fills and empties every few seconds")
    c.equal(bert.wal_standing({"db_bytes": 5 * mb, "wal_bytes": 5 * mb}), "",
            "equal is still not past it")
    c.equal(bert.wal_standing({"db_bytes": 5 * mb, "wal_bytes": 6 * mb}),
            "watch", "larger than the database is worth saying")
    c.equal(bert.wal_standing({"db_bytes": 5 * mb, "wal_bytes": 33 * mb}),
            "act", "and the size this actually reached asks for a person")

    # Fails quiet, the rule every field off /health follows.
    for absent in (None, {}, {"db_bytes": 0, "wal_bytes": 99 * mb}):
        c.equal(bert.wal_standing(absent), "",
                f"{absent!r} says nothing rather than warning about a field "
                f"an older Ernie does not send")

    return c.report()


# -- the change log claims before it posts ----------------------------------

class Channel:
    """Discord for the change log: records posts, and can be told to fail."""

    def __init__(self, fail=False):
        self.posts, self.fail = [], fail

    def write(self, method, path, **kw):
        if self.fail:
            raise RuntimeError("channel is unhappy")
        self.posts.append(kw.get("content", ""))
        return {"id": f"msg-{len(self.posts)}"}


def one_settled_event(b):
    """A completed event old enough to have settled, so the log wants it."""
    tid = b.card("PROD: bravon - 25Aug26 - SSD0145", "high")
    b.con.execute(
        """INSERT INTO events (event_id, occurred_at, actor_name, thread_id,
                               verb, dispatch_after, posted_at)
           VALUES ('ev-1', datetime('now','-1 day'), 'Bella Fiore', ?,
                   'completed', datetime('now','-1 day'),
                   datetime('now','-1 day'))""", (tid,))
    b.con.execute("INSERT OR IGNORE INTO changelog_state (id, started_at) "
                  "VALUES (1, datetime('now','-2 days'))")
    b.con.commit()
    return tid


def check_a_line_is_claimed_before_it_is_posted() -> bool:
    """The bug: post first, record second, and a failed record reposts.

    `mark()` raised because the database was locked, *after* the message had
    reached Discord. Nothing was written, so `pending()` handed the same
    event back every pass -- seven copies of one completion in a channel
    whose whole job is to be a durable record.
    """
    c = Check("a line is claimed before it is posted")

    with Board() as b:
        one_settled_event(b)
        d = Channel()

        c.equal(len(cl.pending(b.con)), 1, "the event is waiting to be logged")
        cl.drain(d, "chan-1", b.con)
        c.equal(len(d.posts), 1, "it is posted once")
        c.equal(len(cl.pending(b.con)), 0, "and is no longer pending")

        # The failure that caused it: the message is out, the recording is
        # gone. The claim is what has to survive that.
        b.con.execute("UPDATE changelog_sent SET message_id=NULL "
                      "WHERE event_id='ev-1'")
        b.con.commit()
        c.equal(len(cl.pending(b.con)), 0,
                "a claim with no message id still keeps the event out of "
                "pending, which is the whole of the fix")

        cl.drain(d, "chan-1", b.con)
        c.equal(len(d.posts), 1,
                "so a second pass posts nothing -- this is the seven copies "
                "not happening")

    return c.report()


def check_a_post_that_fails_gives_the_claim_back() -> bool:
    """Or the fix for duplicates would quietly become lost lines instead."""
    c = Check("a post that fails gives the claim back")

    with Board() as b:
        one_settled_event(b)
        dead = Channel(fail=True)

        r = cl.drain(dead, "chan-1", b.con)
        c.equal(r["sent"], 0, "nothing was sent")
        c.equal(r["failed"], 1, "and it is reported as failed")
        c.equal(len(cl.pending(b.con)), 1,
                "the event is pending again, so the line is not lost")
        c.equal(b.con.execute("SELECT COUNT(*) FROM changelog_sent")
                .fetchone()[0], 0, "and no claim is left behind")

        live = Channel()
        cl.drain(live, "chan-1", b.con)
        c.equal(len(live.posts), 1, "it goes out once the channel is well")
        c.equal(len(cl.pending(b.con)), 0, "and is recorded")

    return c.report()


def check_an_unrecorded_line_is_never_struck_through() -> bool:
    """A claim with no message id has nothing to edit, and must not guess."""
    c = Check("an unrecorded line is never struck through")

    with Board() as b:
        one_settled_event(b)
        cl.drain(Channel(), "chan-1", b.con)
        b.con.execute("UPDATE changelog_sent SET message_id=NULL")
        b.con.execute("UPDATE events SET undone_at=datetime('now'), "
                      "undone_by='Bella Fiore' WHERE event_id='ev-1'")
        b.con.commit()

        c.equal(len(cl.retracted(b.con)), 0,
                "retracted() skips it rather than trying to PATCH a message "
                "whose id nobody has")
        c.equal(len(cl.unresolved(b.con)), 1,
                "and it is reported instead, because a claim with no id may "
                "or may not have been posted and retrying is what caused the "
                "duplicates")

    return c.report()


# -- the tool that looks at a live database ---------------------------------

def check_the_sql_tool_cannot_write_by_accident() -> bool:
    """`tools/q.py` gets pointed at live databases. That is what it is for.

    It opened read-write with no busy timeout, so a question about the board
    took a lock the board needed -- which is a poor property for the thing
    somebody reaches for while diagnosing exactly that kind of problem.
    """
    c = Check("the SQL tool cannot write by accident")

    src = (ROOT / "tools/q.py").read_text(encoding="utf-8")
    c.ok("mode=ro" in src, "it opens read-only")
    c.ok("--write" in src, "with an explicit way to ask for otherwise")
    c.ok("busy_timeout" in src,
         "and waits for a busy database rather than failing at it")
    c.ok("con.close()" in src, "and closes what it opened")

    # Proof rather than inspection: read-only is SQLite's refusal, not ours.
    with Board() as b:
        con = sqlite3.connect(f"file:{b.path}?mode=ro", uri=True)
        try:
            con.execute("UPDATE cards SET rank = rank")
            c.ok(False, "a read-only connection refuses a write")
        except sqlite3.OperationalError as e:
            c.ok("readonly" in str(e).replace(" ", ""),
                 f"a read-only connection refuses a write ({e})")
        finally:
            con.close()

    return c.report()


# -- a publish pass is bounded ----------------------------------------------

class StateChannel:
    """A state channel that remembers what was written to it.

    It has to: the cap is about a board catching up across passes, and a
    channel with no memory re-posts everything every time, which is not the
    thing being measured.
    """

    def __init__(self):
        self.writes = []
        self.msgs = {}                       # message_id -> content

    def write(self, method, path, **kw):
        self.writes.append(method)
        mid = path.rsplit("/", 1)[-1]
        if method == "POST":
            mid = str(1000000 + len(self.msgs) + 1)
        self.msgs[mid] = kw.get("content", "")
        return {"id": mid,
                "timestamp": "2026-09-16T19:00:00.000000+00:00"}

    def get(self, path, **kw):
        if kw.get("before"):
            return []
        return [{"id": mid, "content": body}
                for mid, body in reversed(list(self.msgs.items()))]


def check_a_publish_pass_is_bounded() -> bool:
    """One pass may not run for minutes, because the drain waits behind it.

    A card message carries its position in the band, and `positions()` is
    computed across the whole board -- so closing one ticket shifts
    everything below it and genuinely changes the prose of dozens of
    messages. That is not a bug; writing all of them in one pass is. Measured
    on a 49-card sandbox: 40 edits, **4m46s**, at Discord's rate limit, with
    the outbox on one thread and a Complete sitting behind the lot.

    Everything else here that does per-item work against Discord has a
    budget -- CLOSURE_CHECKS 20, RESCAN_PER_CYCLE 12, the change log's BATCH.
    This had none.
    """
    c = Check("a publish pass is bounded")

    import ernie_state as st

    c.ok(isinstance(st.PUBLISH_MAX, int) and st.PUBLISH_MAX > 0,
         f"there is a budget ({st.PUBLISH_MAX} card messages a pass)")

    with Board() as b:
        b.con.execute(
            "INSERT OR IGNORE INTO watched_channels (channel_id, "
            "generate_cards) VALUES (?,1)", (PARENT,))
        n = st.PUBLISH_MAX * 2 + 3
        for i in range(n):
            b.card(f"PROD: C{i:03d} - 01Jan26 - x", "high", rank=1000.0 + i)
        b.con.commit()

        d = StateChannel()
        r = st.publish(d, "chan-1", b.path)
        wrote = r["posted"] + r["edited"]
        c.equal(wrote, st.PUBLISH_MAX,
                f"a pass writes at most the budget ({wrote})")
        c.equal(r.get("left"), n - st.PUBLISH_MAX,
                "and says how many it did not get to, so a pass that wrote "
                "ten of forty does not read as one that finished")

        # The rest arrive on later passes rather than never: a written card
        # is unchanged next time, so the budget walks down the board.
        second = st.publish(d, "chan-1", b.path)
        c.equal(second["posted"] + second["edited"], st.PUBLISH_MAX,
                "the next pass takes the next ten")
        c.ok(second.get("left", 0) < r.get("left", 0),
             f"and fewer are left each time "
             f"({r.get('left')} -> {second.get('left')})")

        third = st.publish(d, "chan-1", b.path)
        c.equal(third.get("left", 0), 0,
                "so a board this size is in step after three passes")
        c.equal(third["posted"] + third["edited"], n - st.PUBLISH_MAX * 2,
                "the last pass writes only what is left, not a full budget")

    return c.report()


def check_a_quiet_board_still_writes_nothing() -> bool:
    """The cap must not turn "nothing to do" into "ten things to do"."""
    c = Check("a quiet board still writes nothing")

    import ernie_state as st

    with Board() as b:
        b.con.execute(
            "INSERT OR IGNORE INTO watched_channels (channel_id, "
            "generate_cards) VALUES (?,1)", (PARENT,))
        for i in range(4):
            b.card(f"PROD: D{i} - 01Jan26 - x", "high", rank=1000.0 + i)
        b.con.commit()

        d = StateChannel()
        st.publish(d, "chan-1", b.path)
        before = len(d.writes)
        again = st.publish(d, "chan-1", b.path)
        c.equal(len(d.writes), before,
                "a second pass over an unchanged board writes nothing")
        c.equal(again["posted"] + again["edited"], 0, "and reports nothing")
        c.equal(again.get("left", 0), 0, "with nothing left over")

    return c.report()


CHECKS = (check_the_wal_is_measured_and_reported,
          check_what_bert_says_about_it,
          check_a_line_is_claimed_before_it_is_posted,
          check_a_post_that_fails_gives_the_claim_back,
          check_an_unrecorded_line_is_never_struck_through,
          check_the_sql_tool_cannot_write_by_accident,
          check_a_publish_pass_is_bounded,
          check_a_quiet_board_still_writes_nothing)

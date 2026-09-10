"""
A retry must not do again what already reached Discord.

Reported as changes showing up in Discord while the card went on saying
"Pushing to Discord..." -- and the details were not to hand, which is exactly
what made it hard to place. It reproduces cleanly, and it is worse than the
symptom.

Posting one event takes up to four writes -- unarchive, rename, message,
archive -- and creating a ticket takes three: the thread, a note saying who
started it, then their opening message. Only the *last* write was ever
recorded, so a failure anywhere threw away the record of everything before
it. The retry started from the top.

Two of those writes are harmless twice and two are not. Archiving a thread
that is already archived is the same as archiving it once. Renaming is 2 per
10 minutes on a shared budget and posts a system message every time; posting
a message is a message; and opening a thread is a whole second ticket.

Measured before the fix: an archive that failed twice put **three identical
"marked this complete" messages** into one customer thread, and an opening
message that failed twice left **three real threads** in the customer channel
for one ticket -- the board keeping the third, and the sync free to pick the
other two up later as fresh unassigned cards.

`sent_steps` is the record, written and committed the moment each irreversible
write lands.
"""

import sqlite3
import uuid

from support import PARENT, Board, Check, iso

import ernie_outbox as O


class Flaky:
    """Discord, with one kind of write failing on demand."""

    def __init__(self, fail=None):
        self.calls = []
        self.guild_id = "guild"
        self.fail = fail            # a predicate over (verb, path, kw)
        self.threads = 0

    def write(self, verb, path, **kw):
        if self.fail and self.fail(verb, path, kw):
            self.calls.append((verb, path, kw, "failed"))
            raise RuntimeError("boom -- Discord said no")
        self.calls.append((verb, path, kw, "ok"))
        if path.endswith("/threads"):
            self.threads += 1
            return {"id": f"thread-new-{self.threads}"}
        return {"id": f"msg-{len(self.calls)}"}

    def ok(self, verb, ending=None):
        """The successful calls of a shape, for counting."""
        return [(p, kw) for v, p, kw, how in self.calls
                if how == "ok" and v == verb
                and (ending is None or p.endswith(ending))]


def pushing(con, tid):
    """What the card's unsent mark reads off -- the API's own query."""
    return con.execute(
        """SELECT COUNT(*) FROM events
           WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
             AND undone_at IS NULL AND attempts < 5 AND thread_id=?""",
        (tid,)).fetchone()[0]


def check_a_message_is_posted_once_however_often_the_rest_fails() -> bool:
    """
    Completing posts a message and then archives the thread.

    The archive failing is an ordinary thing -- a 500, a permission that has
    just changed, a thread somebody deleted -- and it left the message posted
    with nothing recording that. Three attempts, three messages.
    """
    c = Check("a message is posted once however often the rest fails")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        eid = b.event(tid, verb="completed", old=None, new="done",
                      dispatch_after=iso(-60))
        d = Flaky(fail=lambda v, p, kw: kw.get("archived") is True)

        for _ in range(2):
            ev = b.con.execute("SELECT * FROM events WHERE event_id=?",
                               (eid,)).fetchone()
            c.equal(O.post_one(b.con, d, ev), "failed", "the archive fails")
            b.con.commit()

        c.equal(len(d.ok("POST", "/messages")), 1,
                "and the message went out once, not once per attempt")
        c.ok(pushing(b.con, tid),
             "the card still says it is pushing, which is true -- the thread "
             "is not archived yet")

        d.fail = None
        ev = b.con.execute("SELECT * FROM events WHERE event_id=?",
                           (eid,)).fetchone()
        c.equal(O.post_one(b.con, d, ev), "sent", "and it lands on the third")
        b.con.commit()
        c.equal(len(d.ok("POST", "/messages")), 1,
                "still one message in the thread")
        c.ok(not pushing(b.con, tid), "and the mark clears")

        row = b.con.execute("SELECT discord_message_id FROM events "
                            "WHERE event_id=?", (eid,)).fetchone()
        c.ok(row["discord_message_id"],
             "the message id survives the retries, so undo can reply to it")

    return c.report()


def check_a_thread_is_renamed_once() -> bool:
    """
    A rename is 2 per 10 minutes, shared, and posts a system message.

    So doing it again because a later write failed spends a budget the board
    has very little of, and puts a second "renamed the thread" line into a
    customer thread that has already had one.
    """
    c = Check("a thread is renamed once")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        # Archived, so there is an unarchive before the rename and a real
        # sequence to interrupt.
        b.con.execute("UPDATE threads SET archived=1 WHERE thread_id=?", (tid,))
        eid = b.event(tid, verb="renamed", old="PROD: A - 01Jan26 - x",
                      new="OPS: A - 01Jan26 - x", dispatch_after=iso(-60))
        b.con.commit()

        # The rename lands; the write after it fails. Then everything works.
        state = {"n": 0}

        def fail(v, p, kw):
            if "name" in kw:
                state["n"] += 1
                return False        # the rename itself always succeeds
            return state["n"] == 1 and kw.get("archived") is False

        d = Flaky(fail=fail)
        for _ in range(2):
            ev = b.con.execute("SELECT * FROM events WHERE event_id=?",
                               (eid,)).fetchone()
            O.post_one(b.con, d, ev)
            b.con.commit()

        renames = [kw for _, kw in d.ok("PATCH") if "name" in kw]
        c.equal(len(renames), 1,
                "one rename reached Discord across a failure and a retry")

    return c.report()


def a_draft(b):
    did = str(uuid.uuid4())
    b.con.execute(
        """INSERT INTO new_threads (draft_id, channel_id, title, priority,
                                    actor, first_message, work_json,
                                    created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (did, PARENT, "PROD: A - 01Jan26 - a new one", "medium", "Tester",
         "here is what it is about", '["check the reel", "pack it"]',
         iso(-10)))
    b.con.commit()
    return did


def check_one_ticket_makes_one_thread() -> bool:
    """
    The worst of them, because the extra threads are real tickets.

    The thread's id was written only after both messages had gone out, so a
    message that failed threw it away -- and the retry opened another thread
    for the same ticket. The board keeps whichever one finished; the sync sees
    the others on its next pass and makes cards for them, so a hiccup while
    starting a ticket quietly puts two more on the board.
    """
    c = Check("one ticket makes one thread")

    with Board() as b:
        a_draft(b)
        d = Flaky(fail=lambda v, p, kw: p.endswith("/messages"))

        for _ in range(2):
            c.equal(O.make_threads(b.con, d)["failed"], 1,
                    "the opening message fails")
        c.equal(d.threads, 1,
                "and exactly one thread was created, not one per attempt")

        d.fail = None
        c.equal(O.make_threads(b.con, d)["made"], 1, "the third pass lands it")
        c.equal(d.threads, 1, "still one thread")
        c.equal(len(d.ok("POST", "/messages")), 2,
                "one note and one opening message, each posted once")

    return c.report()


def check_a_ticket_reaches_the_board_before_its_messages_do() -> bool:
    """
    A card that exists is worth more than a tidy order of writes.

    The rows were written after both messages, so a message that failed left
    the ticket off the board entirely until a later attempt succeeded --
    somebody watching would see the ticket they had just started simply not be
    there. The thread exists by then, which is the only thing a card needs.
    """
    c = Check("a ticket reaches the board before its messages do")

    with Board() as b:
        a_draft(b)
        d = Flaky(fail=lambda v, p, kw: p.endswith("/messages"))
        O.make_threads(b.con, d)

        row = b.con.execute("SELECT thread_id, priority FROM cards").fetchone()
        c.ok(row is not None, "the card is on the board after the failure")
        if row is None:
            return c.report()
        c.equal(row["priority"], "medium", "in the band the + was pressed in")
        c.equal(b.con.execute("SELECT COUNT(*) n FROM work_items")
                .fetchone()["n"], 2, "with its two work items")

        # And the retry does not give it every bubble twice.
        O.make_threads(b.con, d)
        c.equal(b.con.execute("SELECT COUNT(*) n FROM work_items")
                .fetchone()["n"], 2, "still two after a retry, not four")
        c.equal(b.con.execute("SELECT COUNT(*) n FROM thread_titles "
                              "WHERE thread_id=?",
                              (row["thread_id"],)).fetchone()["n"], 1,
                "and one title row, not one per attempt")

    return c.report()


def check_the_steps_are_recorded_as_they_land() -> bool:
    """
    The record has to be committed *between* the writes, not after them.

    Everything else here is a consequence of that. A pass that noted its steps
    in memory and wrote them at the end would lose them in exactly the case
    they exist for.
    """
    c = Check("the steps are recorded as they land")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        eid = b.event(tid, verb="completed", old=None, new="done",
                      dispatch_after=iso(-60))
        d = Flaky(fail=lambda v, p, kw: kw.get("archived") is True)
        ev = b.con.execute("SELECT * FROM events WHERE event_id=?",
                           (eid,)).fetchone()
        O.post_one(b.con, d, ev)

        # Read on a second connection, so only committed rows are visible.
        other = sqlite3.connect(b.path)
        other.row_factory = sqlite3.Row
        steps = other.execute("SELECT sent_steps FROM events WHERE event_id=?",
                              (eid,)).fetchone()["sent_steps"]
        other.close()
        c.ok(steps and "message" in steps,
             f"the message step is committed, not held in memory ({steps!r})")

        c.equal(O.steps_done({"sent_steps": "message,renamed"}),
                {"message", "renamed"}, "and steps_done reads it back")
        c.equal(O.steps_done({"sent_steps": None}), set(),
                "an untouched row has done nothing")

    return c.report()


CHECKS = (check_a_message_is_posted_once_however_often_the_rest_fails,
          check_a_thread_is_renamed_once,
          check_one_ticket_makes_one_thread,
          check_a_ticket_reaches_the_board_before_its_messages_do,
          check_the_steps_are_recorded_as_they_land)

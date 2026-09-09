"""
The ticket's status, kept as a message in its own thread.

The board knows what a ticket still needs and the thread is where the work is
discussed, and the two only met by somebody opening Bert. This puts what is
still to do where the conversation is.

Two rules carry the whole thing. The message is **edited in place**, because
an edit recovers from its rate limit in 0.67s and notifies nobody -- so the
status can follow every tick without the thread becoming a notification feed,
and a pass must therefore rewrite only when the rendering would differ. And it
covers **new threads only**: a first sync inherits every thread there has ever
been -- 889 in production -- and posting into all of them at once is not a
thing to do to a channel people are working in.
"""

import pathlib

from support import Board, Check, FakeDiscord, PARENT, iso

import ernie_state as S
import ernie_status as st


NL = chr(10)


def a_card(b, name="PROD: Penn Hills - 09Sep26 - EReel-1220 respool",
           priority="high", witnessed=True, archived=False):
    """One card whose thread Ernie either watched appear, or merely inherited."""
    tid = b.card(name, priority)
    seen = iso(-30) if witnessed else iso()
    made = iso(-60) if witnessed else iso(-90 * 24 * 3600)
    b.con.execute(
        "UPDATE threads SET created_at=?, first_seen_at=?, archived=? "
        "WHERE thread_id=?", (made, seen, int(archived), tid))
    b.con.commit()
    return tid


def work(b, tid, body, done=False):
    b.con.execute(
        """INSERT INTO work_items (item_id, thread_id, body, position,
                                   created_at, created_by, done_at, done_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (f"{tid}-{body[:6]}", tid, body, 1.0, iso(-20), "Bella Fiore",
         iso(-10) if done else None, "Bella Fiore" if done else None))
    b.con.commit()


def card_for(b, tid):
    return next(c for c in S.load_board(b.path) if c.thread_id == tid)


def check_the_message_says_what_is_left() -> bool:
    """
    What is still to do, first, because that is the question being asked.

    Not the ticket's name: that is the thread's own name, shown directly above
    this in every client, so repeating it puts the same string on screen twice
    within an inch. Finished items are struck through, which is already what a
    ticked item looks like in the state channel and in the change log.
    """
    c = Check("the message says what is left to do")

    with Board() as b:
        tid = a_card(b)
        work(b, tid, "replace cable")
        work(b, tid, "collect reel", done=True)
        # Who last touched it comes off the feed, the same way the state
        # channel names an actor -- so there has to be something in it.
        b.event(tid, actor="Bella Fiore")
        body = st.render(card_for(b, tid))

        c.ok("replace cable" in body, "an outstanding item is named")
        c.ok("~~collect reel~~" in body, "and a finished one is struck through")
        c.ok("**To do**" in body and "**Done**" in body,
             "under headings, so the two do not run together")
        c.ok("Penn Hills" not in body,
             "the ticket's name is not repeated -- the thread is already called it")
        c.ok("High" in body, "the band it sits in is there")
        c.ok("Last updated" in body, "and when it last moved")
        c.ok("Bella Fiore" in body, "and who moved it")

    return c.report()


def check_an_empty_ticket_says_so() -> bool:
    """
    A third of open tickets carry no work items at all.

    Every thread gets a status message whether or not there is anything on it:
    the trigger is that a card exists, which is what puts the message near the
    top of the thread instead of fifty replies down. So it has to read
    properly with nothing to list -- a message that stops after the band looks
    like one that failed to load.
    """
    c = Check("a ticket with nothing on it still reads properly")

    with Board() as b:
        tid = a_card(b)
        body = st.render(card_for(b, tid))
        c.ok("No work items yet." in body, "it says so outright")
        c.ok("**To do**" not in body, "with no empty heading above it")

        # And once everything is ticked off, while the ticket is still open.
        work(b, tid, "replace cable", done=True)
        body = st.render(card_for(b, tid))
        c.ok("Nothing left to do." in body, "a finished list says that instead")
        c.ok("~~replace cable~~" in body, "and still shows what was done")

    return c.report()


def check_a_closed_ticket_does_not_say_it_twice() -> bool:
    """The header already carries it; repeating it underneath is padding."""
    c = Check("a closed ticket says it once")

    with Board() as b:
        tid = a_card(b)
        work(b, tid, "replace cable", done=True)
        b.con.execute("UPDATE cards SET completed_at=?, completed_by=? "
                      "WHERE thread_id=?", (iso(-5), "Julian", tid))
        b.con.commit()
        body = st.render(card_for(b, tid))

        c.ok("closed by Julian" in body, "the header says it is closed, and by whom")
        c.ok("Nothing left to do." not in body, "and does not then say it again")
        c.ok("High" not in body,
             "nor a band, which is where it sat rather than where it is")

    return c.report()


def check_nothing_in_it_moves_on_its_own() -> bool:
    """
    The body has to be stable, or the message rewrites itself for ever.

    The timestamp is Discord's own markup, so the reader's client renders "2
    hours ago" and keeps it current while the source text stays fixed at the
    moment the card changed. A written-out "2h ago" would differ on every
    pass -- which is the trap ernie_state.without_stamp() exists to work
    around.
    """
    c = Check("nothing in the message moves on its own")

    with Board() as b:
        tid = a_card(b)
        work(b, tid, "replace cable")
        card = card_for(b, tid)
        c.equal(st.render(card), st.render(card),
                "rendering the same card twice gives the same text")
        c.ok("<t:" in st.render(card),
             "the time is Discord's markup, rendered by the reader's client")
        c.ok(" ago" not in st.render(card),
             "and not written out, which would change under it")

    return c.report()


def check_only_threads_ernie_watched_open() -> bool:
    """
    The rule that stops this posting into 889 threads at once.

    A first sync makes a card for every thread there has ever been. Ernie
    watched none of those appear, and a status message in each is a channel
    full of bot posts nobody asked for. witnessed_start() is the same
    predicate the `started` feed line uses.
    """
    c = Check("only threads Ernie watched open get one")

    with Board() as b:
        fresh = a_card(b, "PROD: Penn Hills - 09Sep26 - EReel-1220 respool")
        old = a_card(b, "OPS: Munhall - 26Aug26 - 1k reel", witnessed=False)
        shut = a_card(b, "CS: Latrobe - 30Aug26 - camera head", archived=True)

        keep = {card.thread_id for card in
                st.wanted(b.con, S.load_board(b.path))}
        c.ok(fresh in keep, "a thread Ernie watched appear gets one")
        c.ok(old not in keep, "one it merely inherited does not")
        c.ok(shut not in keep,
             "nor an archived one, which Discord refuses a post to anyway")

    return c.report()


def check_it_posts_once_then_edits() -> bool:
    """
    One message per thread, for the life of the ticket.

    Posting a new one per change would turn the thread into a notification
    feed, which is the thing the state channel was designed around: an edit
    announces nothing and recovers from its rate limit in 0.67s.
    """
    c = Check("it posts once, then edits that message")

    with Board() as b:
        tid = a_card(b)
        work(b, tid, "replace cable")
        d = FakeDiscord()

        first = st.publish(d, b.con, b.path)
        c.equal(first["posted"], 1, "the first pass posts")
        c.equal(first["edited"], 0, "and edits nothing")
        c.ok(any(v == "POST" and "/messages" in p for v, p, _ in d.calls),
             "as a message in the thread")
        c.ok(any(v == "PUT" and "/pins/" in p for v, p, _ in d.calls),
             "and pins it, since a bot cannot be a thread's first message")

        row = b.con.execute("SELECT * FROM thread_status WHERE thread_id=?",
                            (tid,)).fetchone()
        c.ok(row is not None and row["message_id"],
             "the message id is kept, so a restart edits rather than reposts")

        # Nothing has changed, so nothing should be written.
        d2 = FakeDiscord()
        again = st.publish(d2, b.con, b.path)
        c.equal(again, {"posted": 0, "edited": 0, "failed": 0},
                "a pass over an unchanged board writes nothing at all")
        c.equal(d2.calls, [], "and does not touch Discord")

        # Tick the item off: the message should follow, in place.
        b.con.execute("UPDATE work_items SET done_at=?, done_by=? "
                      "WHERE thread_id=?", (iso(), "Julian", tid))
        b.con.execute("UPDATE cards SET updated_at=? WHERE thread_id=?",
                      (iso(), tid))
        b.con.commit()
        d3 = FakeDiscord()
        third = st.publish(d3, b.con, b.path)
        c.equal(third["edited"], 1, "a change edits the message")
        c.equal(third["posted"], 0, "rather than posting a second one")
        c.ok(all(v != "POST" for v, _, _ in d3.calls),
             "nothing new is posted into the thread")
        after = b.con.execute("SELECT body FROM thread_status WHERE thread_id=?",
                              (tid,)).fetchone()
        c.ok("~~replace cable~~" in after["body"],
             "and what it now says is what was written")

    return c.report()


def check_an_inherited_thread_is_never_posted_to() -> bool:
    """Belt and braces on the rule that matters most: no backfill, ever."""
    c = Check("an inherited thread is never posted to")

    with Board() as b:
        a_card(b, "OPS: Munhall - 26Aug26 - 1k reel", witnessed=False)
        d = FakeDiscord()
        counts = st.publish(d, b.con, b.path)
        c.equal(counts["posted"], 0, "nothing is posted")
        c.equal(d.calls, [], "and Discord is not called at all")
        c.equal(b.con.execute("SELECT COUNT(*) FROM thread_status")
                .fetchone()[0], 0, "and nothing is recorded")

    return c.report()


CHECKS = (check_the_message_says_what_is_left,
          check_an_empty_ticket_says_so,
          check_a_closed_ticket_does_not_say_it_twice,
          check_nothing_in_it_moves_on_its_own,
          check_only_threads_ernie_watched_open,
          check_it_posts_once_then_edits,
          check_an_inherited_thread_is_never_posted_to)

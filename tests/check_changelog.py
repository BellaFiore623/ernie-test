"""
The change log only says true things.

Two bugs live here. A silent change -- every band move that isn't in or out of
critical, and every reorder -- carries no dispatch_after, and reading that as
"finished" logged the bulk of the board's activity the instant it happened,
ahead of the changes that do wait out their undo window. And undo has no
deadline, so waiting the window out is not enough on its own: a line already
posted and undone later has to be corrected where it stands.
"""

from support import Board, Check, FakeDiscord, iso

import ernie_changelog as cl


def check_settled() -> bool:
    c = Check("settled()")

    def ev(**kw):
        row = {"occurred_at": iso(), "undone_at": None, "posted_at": None,
               "dispatch_after": None}
        row.update(kw)
        return row

    # The bug: these used to settle instantly, with no window at all.
    c.ok(not cl.settled(ev(occurred_at=iso(-5))),
         "a silent move made seconds ago is not settled")
    c.ok(not cl.settled(ev(occurred_at=iso(-30))),
         "still not settled halfway through the window")
    c.ok(cl.settled(ev(occurred_at=iso(-(cl.UNDO_WINDOW_S + 30)))),
         "settled once the window has passed")

    # Posting events keep exactly the behaviour they had.
    c.ok(not cl.settled(ev(occurred_at=iso(-5), dispatch_after=iso(55))),
         "a loud move inside its dispatch window is not settled")
    c.ok(cl.settled(ev(occurred_at=iso(-90), dispatch_after=iso(-30))),
         "a loud move past its dispatch window is settled")
    c.ok(cl.settled(ev(occurred_at=iso(-5), dispatch_after=iso(55),
                       posted_at=iso(-1))),
         "already posted is settled -- undo posts a correction from here")

    c.ok(cl.settled(ev(occurred_at=iso(-2), undone_at=iso(-1))),
         "undone is an outcome, whenever it happened")
    c.ok(cl.settled(ev(occurred_at="not a date")),
         "an unparseable timestamp does not wedge the log forever")

    return c.report()


def check_retractions() -> bool:
    c = Check("retractions")

    with Board() as b:
        tid = b.card("OPS: Baldwin - 02Sep26 - Gooseneck 10in out of stock")

        # All three lines went up 20s ago. undone_at is what separates them.
        live = b.event(tid)
        struck = b.event(tid, undone_at=iso(-10))
        early = b.event(tid, undone_at=iso(-30))
        for eid in (live, struck, early):
            b.logged(eid, sent_at=iso(-20))

        hits = {r["event_id"] for r in cl.retracted(b.con)}
        c.ok(struck in hits, "a line undone after it was posted is picked up")
        c.ok(live not in hits, "a change that still stands is left alone")
        c.ok(early not in hits,
             "an undo before the line went up was already rendered struck")

        # The edit re-stamps sent_at, which is the whole guard against
        # striking the same line on every pass forever.
        cl.mark(b.con, struck, "log-msg")
        b.con.commit()
        c.ok(struck not in {r["event_id"] for r in cl.retracted(b.con)},
             "re-stamping sent_at takes it back out of the list")

        row = b.con.execute(
            """SELECT e.*, v.name AS thread_name FROM events e
               LEFT JOIN v_thread_current v ON v.thread_id = e.thread_id
               WHERE e.event_id=?""", (struck,)).fetchone()
        line = cl.render(row)
        c.ok(line.count("~~") == 2 and "undone by" in line,
             "and the line renders struck through")

    return c.report()


def check_drain_order() -> bool:
    c = Check("drain() order")

    with Board() as b:
        tid = b.card("OPS: Munhall - 26Aug26 - 1k reel")

        # One correction owed, one new line due, one still inside its window.
        struck = b.event(tid, occurred_at=iso(-600), undone_at=iso(-10))
        b.logged(struck, sent_at=iso(-20))
        b.event(tid, occurred_at=iso(-(cl.UNDO_WINDOW_S + 30)))
        b.event(tid, occurred_at=iso(-5))

        b.con.execute("INSERT INTO changelog_state (id, started_at) VALUES (1,?)",
                      (iso(-3600),))
        b.con.commit()

        d = FakeDiscord()
        r = cl.tick(d, "chan", b.con)

        c.equal(r["struck"], 1, "one line corrected")
        c.equal(r["sent"], 1, "one line posted")
        c.equal(d.verbs(), ["PATCH", "POST"],
                "the correction goes out before the new line")
        c.ok(not any("~~" in body for v, _, body in d.calls if v == "POST"),
             "the new line is not struck through")

    return c.report()


CHECKS = (check_settled, check_retractions, check_drain_order)

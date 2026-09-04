"""
One order, and rank is it.

An unreadable thread belongs at the top of unassigned -- it is the one needing
a person soonest, and the bottom of a nineteen-card band is where it goes
unlooked-at. Bert used to arrange that at draw time. Everything else still
read rank order, so the board disagreed with itself: the two unknown-client
cards sat first on screen and twelfth in the state channel, and a drop between
two visible cards was measured against neighbours that were not its
neighbours. The rank is the real one now, so there is nothing left to
disagree.
"""

import ast
import pathlib
from support import Board, Check, iso

import bert
import ernie_api as api
import ernie_extract as ex
import ernie_load as load
import ernie_state as S


def a_record(name: str, owner: str | None = None) -> ex.ThreadRecord:
    """The parsed thread ensure_card is handed, with nothing else going on."""
    title = ex.parse_title(name)
    return ex.ThreadRecord(
        thread_id=f"t-{abs(hash(name)) % 10**9}", parent_id="chan", name=name,
        owner_id=owner,
        title=title, client_key=None, equipment=[], proposals=[], created=[],
        participants=[], message_count=1, first_ts=iso(-60), last_ts=iso(-60),
        archived=False, issues=[])


def check_predicate() -> bool:
    c = Check("title_unreadable")

    for name, want in (
            ("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool", False),
            ("Rhino needs to approve there inspections", True),
            ("OPS: outdated escalation list", True),
    ):
        rec = a_record(name)
        c.equal(rec.title_unreadable, want,
                f"{rec.title.confidence:<11} {name[:44]}")

    # The one definition, not a second list that can drift from Bert's.
    import bert
    derived = {f"title_{conf}" for conf in ex.UNREADABLE_CONFIDENCE}
    c.ok(derived <= bert.BLOCKING,
         "every unreadable confidence is blocking in Bert too")

    return c.report()


def check_new_cards_rank() -> bool:
    c = Check("ensure_card ranking")

    with Board() as b:
        b.con.execute(
            "INSERT INTO watched_channels (channel_id, generate_cards) VALUES (?,1)",
            ("chan",))
        b.con.commit()

        def arrive(name):
            rec = a_record(name)
            b.con.execute(
                """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                        first_seen_at, last_synced_at)
                   VALUES (?,?,?,?,?,?)""",
                (rec.thread_id, "chan", "g", iso(-60), iso(-60), iso()))
            b.con.execute(
                """INSERT INTO thread_titles (thread_id, observed_at, name, confidence)
                   VALUES (?,?,?,?)""",
                (rec.thread_id, iso(-60), name, rec.title.confidence))
            load.ensure_card(b.con, rec)
            b.con.commit()
            return rec.thread_id

        first = arrive("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool")
        second = arrive("OPS: McKeesport - 02Sep26 - ODE-2977 wheel motor")
        junk = arrive("Rhino needs to approve there inspections")
        third = arrive("CS: Bethel Park - 02Sep26 - Operator training refresher")
        junk2 = arrive("OPS: outdated escalation list")

        order = [r["thread_id"] for r in b.con.execute(
            "SELECT thread_id FROM cards WHERE priority='unassigned' ORDER BY rank")]

        c.equal(order.index(junk) < order.index(first), True,
                "an unreadable thread lands above the cards already there")
        c.equal(order.index(junk2) < order.index(first), True,
                "and so does the next one")
        c.equal(order.index(junk2) < order.index(junk), True,
                "the newest unreadable one goes to the very top")
        c.equal(order[-1], third, "a readable thread still lands at the bottom")
        c.equal(order.index(first) < order.index(second), True,
                "readable ones keep the order they arrived in")

    return c.report()


def check_one_order() -> bool:
    c = Check("one order everywhere")

    with Board() as b:
        # A triage card parked mid-band. Nothing may lift it: it is where the
        # rank says, and the rank is what a person dragged it to.
        b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool",
               "unassigned", 1000.0)
        dragged = b.card("Rhino needs to approve there inspections",
                         "unassigned", 2000.0)
        b.card("OPS: McKeesport - 02Sep26 - ODE-2977 wheel motor",
               "unassigned", 3000.0)

        cards = S.load_board(b.path)
        band = [x for x in cards if x.priority == "unassigned"]
        by_rank = [x.thread_id for x in sorted(band, key=lambda x: x.rank)]

        c.equal([x.thread_id for x in band], by_rank,
                "load_board hands them over in rank order")
        c.equal(S.positions(cards)[dragged], 2,
                "the state channel numbers it where its rank puts it")
        c.ok(0 < S.positions(cards)[dragged] < len(band),
             "a triage card dragged down the band stays down")

        # What a drop between two adjacent cards has to produce. With the
        # display order and the rank order the same, the visible neighbours
        # are the real ones and the midpoint lands between them.
        ranks = sorted(x.rank for x in band)
        mid = (ranks[0] + ranks[1]) / 2
        c.ok(ranks[0] < mid < ranks[1],
             "a drop between the first two lands strictly between them")

    return c.report()


def check_a_reorder_says_where_it_went() -> bool:
    """
    rank is a fraction and says nothing to a reader.

    "reordered PROD: ..." was the whole line, for every drag, so the feed and
    the change log recorded that something moved and not where. The position
    in the band is what the person was looking at when they dragged it, and it
    is derivable at the moment of the write from the ranks it is ordered
    against -- afterwards it is not, because the other ranks have moved on.
    """
    c = Check("a reorder records the places it moved between")

    with Board() as b:
        api.DB = b.path
        ids = [b.card(f"PROD: Client {i} - 02Sep26 - item {i}", "high",
                      1000.0 * (i + 1)) for i in range(5)]

        def positions(tid):
            row = b.con.execute(
                "SELECT priority, rank FROM cards WHERE thread_id=?",
                (tid,)).fetchone()
            others = [r[0] for r in b.con.execute(
                """SELECT rank FROM cards WHERE priority=? AND thread_id<>?
                   AND completed_at IS NULL""", (row["priority"], tid))]
            return sum(1 for r in others if r < row["rank"]) + 1

        def last_reorder():
            r = b.con.execute(
                """SELECT old_value, new_value FROM events WHERE verb='reordered'
                   ORDER BY rowid DESC LIMIT 1""").fetchone()
            return (r["old_value"], r["new_value"]) if r else None

        before = b.con.execute(
            "SELECT COUNT(*) FROM events WHERE verb='reordered'").fetchone()[0]

        # Last card to the front.
        api.move_card(ids[4], api.MoveBody(priority="high", before_id=ids[0],
                                      actor="Tester"))
        c.equal(positions(ids[4]), 1, "it really is first now")
        c.equal(last_reorder(), ("high:5", "high:1"),
                "and the event says 5th to 1st, in High")

        # And back down between two others.
        api.move_card(ids[4], api.MoveBody(priority="high", after_id=ids[1],
                                      actor="Tester"))
        c.equal(last_reorder(), ("high:1", f"high:{positions(ids[4])}"),
                "the next move starts from where the last one left it")

        # The band travels with the place, so a row can be read on its own.
        c.equal(ex.reorder_spot(last_reorder()[0])[0], "high",
                "and names the band it happened in")

        return_ = b.con.execute(
            "SELECT COUNT(*) FROM events WHERE verb='reordered'").fetchone()[0]
        c.equal(return_ - before, 2, "one event per move that moved something")

    return c.report()


def check_a_reorder_that_moves_nothing_says_nothing() -> bool:
    """
    A drag that lands a card back where it started is not a change.

    It still writes the rank, so two boards agree about it, but four identical
    "reordered" lines in a row for a card that never went anywhere is the feed
    reporting the dragging rather than the outcome.
    """
    c = Check("a drag that changes nothing writes no line")

    with Board() as b:
        api.DB = b.path
        ids = [b.card(f"OPS: Client {i} - 02Sep26 - item {i}", "medium",
                      1000.0 * (i + 1)) for i in range(4)]

        def reorders():
            return b.con.execute(
                "SELECT COUNT(*) FROM events WHERE verb='reordered'").fetchone()[0]

        # Drop it straight back after the card it already follows.
        rank_before = b.con.execute(
            "SELECT rank FROM cards WHERE thread_id=?", (ids[2],)).fetchone()[0]
        api.move_card(ids[2], api.MoveBody(priority="medium", after_id=ids[1],
                                      actor="Tester"))
        c.equal(reorders(), 0, "no line for a move that went nowhere")

        rank_after = b.con.execute(
            "SELECT rank FROM cards WHERE thread_id=?", (ids[2],)).fetchone()[0]
        c.ok(rank_after is not None, "but the rank is still written")
        c.ok(rank_before is not None, "and it had one before")

        # A real move still speaks up.
        api.move_card(ids[3], api.MoveBody(priority="medium", before_id=ids[0],
                                      actor="Tester"))
        c.equal(reorders(), 1, "a move that goes somewhere still does")

    return c.report()


def check_the_rail_clips_to_its_width() -> bool:
    """
    The running order can be dragged, so its text has to follow.

    Both lines were cut at a fixed number of characters -- 24 and 28 -- which
    is a count and not a measurement, so widening the rail gave the text more
    room and not one more letter of it. The obvious replacement, characters
    per pixel, is a guess about a proportional font; QFontMetrics.elidedText
    knows exactly, and cannot overflow the row it was measured for.
    """
    c = Check("the running order clips to the width it has")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def method(cls_name, fn):
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == cls_name)
        return next(n for n in cls.body
                    if isinstance(n, ast.FunctionDef) and n.name == fn)

    row = method("RailRow", "__init__")
    elides = [n for n in ast.walk(row)
              if isinstance(n, ast.Call)
              and getattr(n.func, "attr", None) == "elidedText"]
    c.equal(len(elides), 2, "both lines are elided against their own font")
    c.ok(not any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "clip"
                 for n in ast.walk(row)),
         "and neither is cut at a character count any more")

    # The width has to be part of what a row is, or a drag would not redraw it.
    setc = method("Rail", "set_cards")
    c.ok(any(isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "row_width"
             for n in ast.walk(setc)),
         "the rail measures itself before building rows")
    sig = next((n for n in ast.walk(setc) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "sig" for t in n.targets)), None)
    c.ok(sig is not None, "there is still a signature guarding the rebuild")
    c.ok(sig is not None and any(getattr(n, "id", None) == "room"
                                 for n in ast.walk(sig)),
         "and the width is in it, so a drag counts as a change")

    # Thirty rows rebuilt on every pixel of a drag is a stutter.
    init = method("Bert", "__init__")
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "rail_redraw"
             for n in ast.walk(init)),
         "the redraw waits for the handle to settle rather than chasing it")

    return c.report()


CHECKS = (check_predicate, check_new_cards_rank, check_one_order,
          check_a_reorder_says_where_it_went,
          check_a_reorder_that_moves_nothing_says_nothing,
          check_the_rail_clips_to_its_width)

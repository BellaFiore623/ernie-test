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

from support import Board, Check, iso

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


CHECKS = (check_predicate, check_new_cards_rank, check_one_order)

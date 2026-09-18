"""Three boards on one channel, and whether pairwise reasoning is enough.

`check_two_boards` has the harness; this reuses it with a third machine.

Everything in the merge is pairwise -- `resolve()` compares one board against
one base, and `apply_card` merges one payload -- and two boards converge
trivially because the channel is the only other party. Three is where that can
fail: a machine pulls a channel that has *already* resolved somebody else's
change, and its own base is older than both.

The properties asked for here are the ones a shared board is worthless without:
it stops writing, it ends in one state, and which machine happened to publish
first does not decide what that state is.
"""

import itertools

from support import Check

from check_two_boards import CHANNEL, Laptop, eq

import ernie_api as api
import ernie_state as S


def board(n, name, priority="medium"):
    """The same ticket on n machines.

    The ids match because each fixture counts its own cards and these are
    created in the same order -- asserted, not assumed, because everything
    here is keyed on the thread id.
    """
    machines = [Laptop(f"m{i}") for i in range(n)]
    tids = [m.board.card(name, priority, 1000.0) for m in machines]
    assert len(set(tids)) == 1, tids
    for m in machines:
        m.con.commit()
    return machines, tids[0]


def bubbles(m, tid):
    return sorted(r[0] for r in m.con.execute(
        "SELECT body FROM work_items WHERE thread_id=? AND removed_at IS NULL",
        (tid,)).fetchall())


def settle(machines, rounds=6):
    """Publish and pull round the ring until nothing writes.

    Returns the round it went quiet on, or None if it never did -- which is the
    failure worth catching. A board that keeps writing is two machines handing
    the same change back and forth, and it would show up in production as an
    edit budget spent on nothing.
    """
    for i in range(rounds):
        wrote = 0
        for m in machines:
            r = m.publish()
            wrote += r["posted"] + r["edited"]
            m.pull()
        if wrote == 0:
            return i
    return None


def check_three_add_a_bubble_each() -> bool:
    c = Check("three people each add a work item")
    CHANNEL.clear()
    ms, tid = board(3, "PROD: Trio - 18Sep26 - a thing")
    try:
        ms[0].publish()
        for m in ms[1:]:
            m.pull()
        for m, body in zip(ms, ("order the cable", "chase the RMA", "book the van")):
            m.api(api.edit_card, tid,
                  api.EditBody(actor=m.who, work_add=[body]))
        took = settle(ms)
        eq(c, "the board stops writing", took is not None, True)
        want = ["book the van", "chase the RMA", "order the cable"]
        for m in ms:
            eq(c, f"{m.who} has all three bubbles", bubbles(m, tid), want)
        eq(c, "and nothing was overruled",
           sum(len(m.events(tid, "overruled")) for m in ms), 0)
    finally:
        for m in ms:
            m.close()
    return c.report()


def check_three_move_the_same_card() -> bool:
    c = Check("three move the same card")
    CHANNEL.clear()
    ms, tid = board(3, "PROD: Trio - 18Sep26 - a thing")
    try:
        ms[0].publish()
        for m in ms[1:]:
            m.pull()
        for m, band in zip(ms, ("low", "high", "critical")):
            m.api(api.move_card, tid,
                  api.MoveBody(actor=m.who, priority=band))
        took = settle(ms)
        eq(c, "the board stops writing", took is not None, True)
        bands = {m.who: m.card(tid)["priority"] for m in ms}
        eq(c, "all three agree on one band", len(set(bands.values())), 1)
        overruled = sum(len(m.events(tid, "overruled")) for m in ms)
        eq(c, "and the two that lost each say so", overruled, 2)
    finally:
        for m in ms:
            m.close()
    return c.report()


def check_order_does_not_decide() -> bool:
    """Which machine publishes first must not decide where the board ends up.

    This is the property a shared board is worthless without, and the one that
    only three machines can really put pressure on: with two there is a single
    interleaving worth trying, with three there are six. If any of them ends
    somewhere different, the board is deciding by accident of timing -- which is
    exactly what resolving against a stored base rather than a clock is meant to
    rule out.
    """
    c = Check("the publish order does not decide")
    endings = {}
    for order in itertools.permutations(range(3)):
        CHANNEL.clear()
        ms, tid = board(3, "PROD: Trio - 18Sep26 - a thing")
        try:
            ms[0].publish()
            for m in ms[1:]:
                m.pull()
            for m, body in zip(ms, ("aaa", "bbb", "ccc")):
                m.api(api.edit_card, tid,
                      api.EditBody(actor=m.who, work_add=[body]))
            for i in order:              # they publish in this order
                ms[i].publish()
                for m in ms:
                    m.pull()
            settle(ms)
            endings[order] = tuple(bubbles(ms[0], tid))
        finally:
            for m in ms:
                m.close()
    eq(c, "all six publish orders end the same way",
       len(set(endings.values())), 1)
    eq(c, "and that ending is all three bubbles",
       sorted(set(endings.values())), [("aaa", "bbb", "ccc")])
    return c.report()


def check_a_third_machine_joins_late() -> bool:
    c = Check("a third machine joins late")
    CHANNEL.clear()
    ms, tid = board(3, "PROD: Trio - 18Sep26 - a thing")
    first, second, joiner = ms
    try:
        # a and b work for a while; c has never pulled or published.
        first.publish(); second.pull()
        first.api(api.edit_card, tid,
                  api.EditBody(actor="first", work_add=["from first"]))
        second.api(api.move_card, tid,
                   api.MoveBody(actor="second", priority="high"))
        first.publish(); second.pull(); second.publish(); first.pull()

        rows = joiner.con.execute(
            "SELECT COUNT(*) FROM state_sync").fetchone()[0]
        eq(c, "the joiner has agreed nothing yet", rows, 0)

        r = joiner.pull()     # adopting
        eq(c, "its first pull adopts rather than fighting",
           (len(r["conflicts"]), len(r["applied"])), (0, 1))
        eq(c, "it takes the band", joiner.card(tid)["priority"], "high")
        eq(c, "and the bubble", bubbles(joiner, tid), ["from first"])

        settle(ms)
        eq(c, "and all three agree afterwards",
           len({m.card(tid)["priority"] for m in ms}), 1)
        eq(c, "on the bubbles too",
           len({tuple(bubbles(m, tid)) for m in ms}), 1)
    finally:
        for m in ms:
            m.close()
    return c.report()


CHECKS = (check_three_add_a_bubble_each,
          check_three_move_the_same_card,
          check_order_does_not_decide,
          check_a_third_machine_joins_late)

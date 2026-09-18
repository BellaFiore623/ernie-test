"""Two boards, sharing one channel: phase 5 without two laptops.

Two databases built from `schema.sql` and a dict standing in for
`#ernie-state`. Everything between them is real: `ernie_api`'s write endpoints,
`ernie_state.publish`, `ernie_state.reconcile`. Only Discord is faked, and only
at the two seams that reach it.

**Both seams, not one.** `publish` reads the channel through `fetch_channel`
before it decides anything -- that is how it leaves alone a card the other
board has moved -- so stubbing only `fetch_state` leaves it reasoning against
an empty channel and every count it reports is meaningless. That mistake made a
perfectly good closure look broken for a while when this was written.

Qt is out of reach: an editor is a widget and a widget needs a QApplication.
Where a case turns on the editor, this drives the decision Bert actually makes
off the payload, through `check_poll_hold`'s stand-in.

One of phase 5's four cases is missing on purpose. Two people adding a
different work item to one ticket keeps one and tombstones the other;
`plans/work-item-merge.md` has the reproduction and the fix.
"""

import os
import pathlib
import sys
import uuid

from support import Board, Check, FakeDiscord

import bert
import ernie_api as api
import ernie_load as load
import ernie_state as S
import ernie_sync

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The stand-in channel has no rate-limit bucket, so the pace between card
# writes buys nothing here and costs a great deal: `publish` sleeps
# `WRITE_PACE` per card, which is 2.2s, and this module publishes about a
# dozen times. It took the suite from 36s to 67s before this line. Zeroed for
# the module rather than per check, because every check here publishes -- the
# same call `check_changelog` makes, for the same reason.
S.WRITE_PACE = 0


CHANNEL = {}          # thread_id -> {message_id, payload, content}
class Laptop:
    """One machine: its own database, its own API target."""

    def __init__(self, who):
        self.who = who
        self.board = Board()
        self.path = self.board.path
        self.con = self.board.con

    def api(self, fn, *a, **kw):
        """Run an API call against this machine's database."""
        was, api.DB = api.DB, self.path
        try:
            return fn(*a, **kw)
        finally:
            api.DB = was

    def publish(self):
        """Push our board into the channel, the way the outbox does.

        Both seams have to be stubbed, not just one. `publish` reads the
        channel through `fetch_channel` before deciding anything -- that is
        how it leaves a card the other board has moved alone -- so a harness
        that stubs only `fetch_state` has it reasoning against an empty
        channel, and every count it reports is meaningless.
        """
        d = FakeDiscord()
        saved = S.fetch_channel
        S.fetch_channel = lambda _d, _cid: (
            {k: dict(v) for k, v in CHANNEL.items()}, [])
        try:
            r = S.publish(d, "chan", self.path)
        finally:
            S.fetch_channel = saved
        for verb, path, content in d.calls:
            p = S.parse(content)
            if not p:
                continue
            CHANNEL[p["thread"]] = {"message_id": path.rsplit("/", 1)[-1],
                                    "payload": p, "content": content}
        return r

    def pull(self):
        """Read the channel into our board, the way the sync does."""
        saved = S.fetch_state
        S.fetch_state = lambda d, cid: {k: dict(v) for k, v in CHANNEL.items()}
        try:
            return S.reconcile(None, "chan", self.path)
        finally:
            S.fetch_state = saved

    def card(self, tid):
        return self.con.execute(
            "SELECT * FROM cards WHERE thread_id=?", (tid,)).fetchone()

    def events(self, tid, verb=None):
        sql = "SELECT * FROM events WHERE thread_id=?"
        args = [tid]
        if verb:
            sql += " AND verb=?"
            args.append(verb)
        return self.con.execute(sql + " ORDER BY rowid", args).fetchall()

    def payload(self):
        """What /cards would send Bert.

        The arguments are passed explicitly: they are FastAPI `Query` defaults,
        so calling the function directly without them hands the query object
        itself to the SQL.
        """
        return self.api(api.cards, queue=None, client=None,
                        include_completed=False)["cards"]

    def close(self):
        self.board.close()


def same_thread(a, b, name, priority="medium", rank=1000.0):
    """The same ticket on both machines, as a shared board really has it.

    Both fixtures number their cards per instance, so creating in the same
    order on each gives the same `thread_id` -- which is what matters, because
    everything here is keyed on it. Asserted rather than assumed.
    """
    ta = a.board.card(name, priority, rank)
    tb = b.board.card(name, priority, rank)
    assert ta == tb, f"the two boards disagree on the id: {ta} vs {tb}"
    a.con.commit()
    b.con.commit()
    return ta


def eq(c, label, got, want):
    """`Check.equal` with the label first, which is how these read.

    The harness this came from put the label first and `Check.equal` puts it
    last. Adapting once beats reordering every call and getting one wrong.
    """
    c.equal(got, want, label)


def check_both_move_the_same_card() -> bool:
    CHANNEL.clear()
    a, b = Laptop("Bella"), Laptop("Chris")
    c = Check("both boards move the same card")
    try:
        tid = same_thread(a, b, "PROD: Apex - 12Sep26 - a thing", "medium")
        a.publish(); b.pull()          # agree on medium first

        # Neither has seen the other yet.
        a.api(api.move_card, tid, api.MoveBody(actor="Bella Fiore", priority="low"))
        b.api(api.move_card, tid, api.MoveBody(actor="Chris", priority="high"))

        a.publish()                    # hers reaches the channel first
        r = b.pull()

        eq(c, "Chris's board reports one conflict", len(r["conflicts"]), 1)
        eq(c, "and it names the field that lost",
              r["conflicts"][0].get("discarded"), ["priority"])
        eq(c, "his card ends on her value", b.card(tid)["priority"], "low")

        moved = b.events(tid, "priority_changed")[-1]
        eq(c, "the applied line reads from the shared value, not his own",
              (moved["old_value"], moved["new_value"]), ("medium", "low"))
        over = b.events(tid, "overruled")
        eq(c, "one row says a change of his was discarded", len(over), 1)
        eq(c, "carrying what it was", "high" in (over[0]["old_value"] or ""), True)
        eq(c, "and it is never posted to the thread",
              over[0]["dispatch_after"], None)

        # Now his board publishes and hers pulls: they must converge, not
        # ping-pong. This is the half that used to send a change back.
        b.publish()
        r2 = a.pull()
        eq(c, "her board settles rather than bouncing it back",
              (len(r2["applied"]), len(r2["conflicts"])), (0, 0))
        eq(c, "and both boards agree",
              (a.card(tid)["priority"], b.card(tid)["priority"]), ("low", "low"))
    finally:
        a.close(); b.close()
    return c.report()


def check_close_while_the_other_edits() -> bool:
    CHANNEL.clear()
    a, b = Laptop("Bella"), Laptop("Chris")
    c = Check("one closes while the other edits")
    try:
        tid = same_thread(a, b, "PROD: Rhino - 09Sep26 - a thing", "high")
        other = same_thread(a, b, "PROD: Delta - 10Sep26 - another", "high")
        a.publish(); b.pull()

        a.api(api.complete, tid, api.ActorBody(actor="Bella Fiore"))
        a.publish()
        r = b.pull()
        eq(c, "his board applies the closure", len(r["applied"]), 1)
        eq(c, "and the card is closed there",
              bool(b.card(tid)["completed_at"]), True)

        left = {c["thread_id"] for c in b.payload()}
        eq(c, "it is gone from what /cards sends Bert", tid in left, False)
        eq(c, "and the other ticket is still there", other in left, True)

        # The editor half. A widget needs a QApplication, so this exercises the
        # decisions Bert makes off the payload rather than the widgets.
        sys.path.insert(0, str(pathlib.Path(bert.__file__).parent / "tests"))
        from check_poll_hold import FakeBert

        w = FakeBert()
        w.on_loaded({"board": {"cards": [
            {"thread_id": tid, "priority": "high", "rank": 1000.0},
            {"thread_id": other, "priority": "high", "rank": 2000.0}]},
            "events": {"events": []}, "health": {}})
        w.editing_card = other          # he is typing into the OTHER card
        w.dropped.clear()
        w.on_loaded({"board": {"cards": [
            {"thread_id": other, "priority": "high", "rank": 2000.0}]},
            "events": {"events": []}, "health": {}})
        eq(c, "the closed card leaves his board with the editor open",
              w.dropped, [tid])
        eq(c, "without a rebuild, so his typing is untouched", w.rendered, 1)

        # And if he had that very card open, the save is refused by name.
        try:
            b.api(api.edit_card, tid,
                  api.EditBody(actor="Chris", client_override="typed anyway"))
            eq(c, "editing a closed ticket is refused", False, True)
        except Exception as e:
            d = getattr(e, "detail", {})
            eq(c, "editing a closed ticket is refused by name",
                  d.get("code") if isinstance(d, dict) else None, "completed")
            eq(c, "naming who closed it",
                  (d.get("by") if isinstance(d, dict) else None), "Bella Fiore")
    finally:
        a.close(); b.close()
    return c.report()


def check_count_the_announcements() -> bool:
    CHANNEL.clear()
    a, b = Laptop("Bella"), Laptop("Chris")
    before = os.environ.get("ANNOUNCE_THREAD_CHANGES")
    c = Check("only one machine tells the thread")
    try:
        tid = same_thread(a, b, "PROD: Orion - 08Sep26 - a thing", "high")

        # The closure row the way reconcile_closures writes it: `dispatch_after`
        # is now() on the announcing machine and NULL everywhere else, which is
        # the whole of the one-machine rule.
        import uuid
        for machine, announce in ((a, True), (b, False)):
            if announce:
                os.environ["ANNOUNCE_THREAD_CHANGES"] = "1"
            else:
                os.environ.pop("ANNOUNCE_THREAD_CHANGES", None)
            say = load.announce_thread_changes()
            machine.con.execute(
                """INSERT INTO events (event_id, occurred_at, actor_name,
                                       thread_id, verb, new_value, dispatch_after)
                   VALUES (?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), "2026-09-18T12:00:00+00:00", "Tyler", tid,
                 "completed", ernie_sync.CLOSED_IN_DISCORD,
                 "2026-09-18T12:00:00+00:00" if say else None))
            machine.con.execute(
                "UPDATE cards SET completed_at=?, completed_by=? WHERE thread_id=?",
                ("2026-09-18T12:00:00+00:00", "Tyler", tid))
            machine.con.commit()

        queued = 0
        for machine in (a, b):
            queued += sum(1 for e in machine.events(tid, "completed")
                          if e["dispatch_after"])
        eq(c, "exactly one machine would tell the thread", queued, 1)
    finally:
        if before is None:
            os.environ.pop("ANNOUNCE_THREAD_CHANGES", None)
        else:
            os.environ["ANNOUNCE_THREAD_CHANGES"] = before
        a.close(); b.close()
    return c.report()


def check_both_edit_the_same_ticket() -> bool:
    CHANNEL.clear()
    a, b = Laptop("Bella"), Laptop("Chris")
    c = Check("both add a different work item")
    try:
        tid = same_thread(a, b, "PROD: Vega - 11Sep26 - a thing", "medium")
        a.publish(); b.pull()

        # Work items are the part of an edit that crosses the channel.
        a.api(api.edit_card, tid, api.EditBody(
            actor="Bella Fiore", work_add=["order the cable"]))
        b.api(api.edit_card, tid, api.EditBody(
            actor="Chris", work_add=["chase the RMA"]))

        a.publish(); b.pull()
        b.publish(); a.pull()
        a.publish(); b.pull()          # one more, so both have settled

        def bubbles(m):
            return sorted(r["body"] for r in m.con.execute(
                "SELECT body FROM work_items WHERE thread_id=? "
                "AND removed_at IS NULL", (tid,)))

        want = ["chase the RMA", "order the cable"]
        eq(c, "her board carries both bubbles", bubbles(a), want)
        eq(c, "his board carries both bubbles", bubbles(b), want)
        eq(c, "neither edit was overruled",
              len(a.events(tid, "overruled")) + len(b.events(tid, "overruled")), 0)


        # The directions this rewrite could have broken instead, each checked
        # against a board that has actually agreed on the item first.
        #
        # A removal still has to travel. The old code removed anything absent
        # from the payload, which is right here and wrong above; the new rule is
        # "absent from theirs *and present in the base*", so this must still go.
        iid = b.con.execute(
            "SELECT item_id FROM work_items WHERE thread_id=? AND body=?",
            (tid, "order the cable")).fetchone()[0]
        a.api(api.edit_card, tid,
              api.EditBody(actor="Bella Fiore", work_remove=[iid]))
        a.publish(); b.pull()
        eq(c, "a removal still reaches the other board",
           "order the cable" in bubbles(b), False)

        # And a removal of ours is not undone by their stale copy, which is what
        # the old "restored" branch did on every pull until they published.
        mine = b.con.execute(
            "SELECT item_id FROM work_items WHERE thread_id=? AND body=?",
            (tid, "chase the RMA")).fetchone()[0]
        b.api(api.edit_card, tid, api.EditBody(actor="Chris", work_remove=[mine]))
        b.pull()
        eq(c, "and ours is not put back by their stale copy",
           "chase the RMA" in bubbles(b), False)
        b.publish(); a.pull()
        eq(c, "it reaches her board once he publishes",
           "chase the RMA" in bubbles(a), False)

        # The part that does NOT cross, which is worth knowing.
        a.api(api.edit_card, tid, api.EditBody(
            actor="Bella Fiore", client_override="Apex Ltd"))
        a.publish(); b.pull()
        eq(c, "a client_override does not cross the channel at all",
              b.card(tid)["client_override"], None)
    finally:
        a.close(); b.close()
    return c.report()

CHECKS = (check_both_move_the_same_card,
          check_both_edit_the_same_ticket,
          check_close_while_the_other_edits,
          check_count_the_announcements)

"""
Closing a ticket, and the word for it.

Two rules, both asked for after a round of use:

**A ticket with work still on it cannot be closed from Bert.** A card is a
list of what is left to do, so closing one with bubbles on it says the ticket
is finished while the card says it is not. There is always a way through, and
both are one click in the editor: tick the bubble off, or take it off the
card with the X.

It holds in Bert only, and deliberately. Archiving a thread in Discord closes
its card whatever the work items say, because Discord is the source of truth
-- 21 of production's closures arrived exactly that way. The rule is "Bert
will not let you", never "it cannot happen", and nothing here should be read
as claiming the stronger thing.

**Complete is the word for a work item; close is the word for a ticket.** One
bubble and one tick against the whole ticket leaving the board: two acts on
two different things, which shared a word and read as one.
"""

import inspect
import json

from support import Board, Check, PARENT

import bert
import ernie_api as api
import ernie_outbox as outbox


def board_with_work(b, bodies, done=(), removed=()):
    """One open card carrying the work items named. Returns its thread_id."""
    b.con.execute(
        "INSERT OR IGNORE INTO watched_channels (channel_id, generate_cards) "
        "VALUES (?,1)", (PARENT,))
    tid = b.card("PROD: Trekk - 04aug26 - SSD0008", "high")
    for i, body in enumerate(bodies):
        b.con.execute(
            """INSERT INTO work_items (item_id, thread_id, body, position,
                                       created_at, created_by, done_at,
                                       removed_at)
               VALUES (?,?,?,?,datetime('now'),'Bella',?,?)""",
            (f"item-{i}", tid, body, i,
             "2026-09-16T10:00:00+00:00" if body in done else None,
             "2026-09-16T10:00:00+00:00" if body in removed else None))
    b.con.commit()
    return tid


def closing(tid, key):
    return api.complete(tid, api.ActorBody(actor="Bella Fiore", key=key))


def check_a_ticket_with_work_left_cannot_be_closed() -> bool:
    """The rule itself, and the shape of the refusal Bert renders."""
    c = Check("a ticket with work left cannot be closed")

    with Board() as b:
        api.DB = b.path
        tid = board_with_work(b, ["replace tail fiber", "bench test"])
        try:
            closing(tid, "k-1")
        except api.HTTPException as e:
            c.equal(e.status_code, 409, "it is refused")
            d = e.detail
            c.equal(d.get("code"), "work_outstanding",
                    "with a code Bert can branch on")
            c.ok("2 things" in d.get("message", ""),
                 f"and a message that counts them ({d.get('message')!r})")
            c.equal(d.get("items"), ["replace tail fiber", "bench test"],
                    "naming them, so the dialog can list what is in the way")
            c.ok("editor" in (d.get("hint") or ""),
                 "and pointing at where they can be ticked or removed")
        else:
            c.ok(False, "closing a ticket with work left is refused")

        c.equal(b.con.execute("SELECT completed_at FROM cards WHERE "
                              "thread_id=?", (tid,)).fetchone()["completed_at"],
                None, "and the card is still open afterwards")
        c.equal(b.con.execute("SELECT COUNT(*) FROM events WHERE "
                              "verb='completed'").fetchone()[0], 0,
                "with nothing queued for the thread")

    return c.report()


def check_one_item_is_counted_as_one() -> bool:
    """A message that says "1 things" is a message nobody wrote on purpose."""
    c = Check("one item is counted as one")

    with Board() as b:
        api.DB = b.path
        tid = board_with_work(b, ["bench test"])
        try:
            closing(tid, "k-2")
        except api.HTTPException as e:
            c.ok("1 thing to do" in e.detail.get("message", ""),
                 f"singular reads as singular ({e.detail.get('message')!r})")
        else:
            c.ok(False, "one outstanding item still refuses")

    return c.report()


def check_both_ways_out_actually_let_it_close() -> bool:
    """Ticking and removing are the two escapes, and a rule with none is a trap.

    Worth holding together rather than trusting: a guard that counted every
    row rather than the open ones would leave a ticket uncloseable for ever
    once anything had been written on it.
    """
    c = Check("both ways out actually let it close")

    with Board() as b:
        api.DB = b.path
        tid = board_with_work(b, ["ticked"], done=["ticked"])
        closing(tid, "k-3")
        c.ok(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                           (tid,)).fetchone()["completed_at"] is not None,
             "a ticked item does not block")

    with Board() as b:
        api.DB = b.path
        tid = board_with_work(b, ["taken off"], removed=["taken off"])
        closing(tid, "k-4")
        c.ok(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                           (tid,)).fetchone()["completed_at"] is not None,
             "and neither does one removed with the X")

    with Board() as b:
        api.DB = b.path
        tid = board_with_work(b, [])
        closing(tid, "k-5")
        c.ok(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                           (tid,)).fetchone()["completed_at"] is not None,
             "a ticket with no work items closes as it always did")

    return c.report()


def check_a_draft_is_held_to_the_same_rule() -> bool:
    """A ticket waiting for its thread has work items too, typed into the form.

    Its rows are the JSON list on `new_threads` rather than `work_items`, so
    this is a second code path to the same rule and the one most likely to be
    missed.
    """
    c = Check("a draft is held to the same rule")

    with Board() as b:
        api.DB = b.path
        b.con.execute(
            "INSERT OR IGNORE INTO watched_channels (channel_id, "
            "generate_cards) VALUES (?,1)", (PARENT,))
        b.con.execute(
            """INSERT INTO new_threads (draft_id, channel_id, title, priority,
                                        rank, work_json, actor, created_at)
               VALUES ('draft-w',?,'PROD: A - 01Jan26 - x','high',1.0,?,
                       'Bella', datetime('now'))""",
            (PARENT, json.dumps(["wire it up"])))
        b.con.commit()

        try:
            closing("draft-w", "k-6")
        except api.HTTPException as e:
            c.equal(e.detail.get("code"), "work_outstanding",
                    "a draft with something typed on it is refused too")
            c.equal(e.detail.get("items"), ["wire it up"], "naming it")
        else:
            c.ok(False, "a draft with work left is refused")

        c.equal(b.con.execute("SELECT complete_on_arrival FROM new_threads "
                              "WHERE draft_id='draft-w'").fetchone()[0], 0,
                "and it is not quietly marked to close when the thread lands")

    with Board() as b:
        api.DB = b.path
        b.con.execute(
            "INSERT OR IGNORE INTO watched_channels (channel_id, "
            "generate_cards) VALUES (?,1)", (PARENT,))
        b.con.execute(
            """INSERT INTO new_threads (draft_id, channel_id, title, priority,
                                        rank, work_json, actor, created_at)
               VALUES ('draft-e',?,'PROD: A - 01Jan26 - x','high',1.0,'[]',
                       'Bella', datetime('now'))""", (PARENT,))
        b.con.commit()
        closing("draft-e", "k-7")
        c.equal(b.con.execute("SELECT complete_on_arrival FROM new_threads "
                              "WHERE draft_id='draft-e'").fetchone()[0], 1,
                "a draft with nothing on it still closes on arrival")

    return c.report()


def check_bert_disables_the_button_rather_than_hiding_the_rule() -> bool:
    """The client half: the control is dead, and it says why on hover.

    Read off the source, because a widget built with no QApplication aborts
    the process rather than raising -- the rule these checks have always
    followed.
    """
    c = Check("Bert disables the button rather than hiding the rule")

    src = inspect.getsource(bert.Card._build_view)

    c.ok("setEnabled(False)" in src,
         "the close button is disabled when something is outstanding")
    c.ok("setToolTip" in src.split("done_btn = ")[1][:1500],
         "and carries the reason, since a dead control that says nothing "
         "is the thing being avoided")

    # The list it branches on is the one the card is already drawing, so the
    # disabled button and the bubbles above it can never disagree.
    c.ok("if items:" in src,
         "keyed on the same list the card draws its bubbles from")

    return c.report()


def check_complete_is_the_word_for_a_work_item_and_close_for_a_ticket() -> bool:
    """The rename, at both ends.

    The button and the thread message are the two places a person reads the
    word, and they were the two places it was wrong.
    """
    c = Check("complete is for a work item, close is for a ticket")

    src = inspect.getsource(bert.Card._build_view)
    label = src.split("self.done_btn = QPushButton(")[1].split(")")[0]
    c.ok("Close thread" in label,
         f"the card's button says what it does to Discord ({label!r})")
    c.ok("Complete" not in label, "and no longer says Complete")

    def said(new_value, actor="Bella Fiore"):
        return outbox.render({"verb": "completed", "new_value": new_value,
                              "actor_name": actor, "old_value": None})

    in_bert = said(None)
    c.ok("closed this thread in Bert" in in_bert,
         f"the thread is told it was closed ({in_bert!r})")
    c.ok("complete" not in in_bert.lower(),
         "and never that it was completed, which is what a bubble is")

    in_discord = said(outbox.CLOSED_IN_DISCORD)
    c.ok("closed this thread in Discord" in in_discord,
         f"the Discord wording is the same sentence ({in_discord!r})")
    c.equal(in_bert.replace("Bert", "X"), in_discord.replace("Discord", "X"),
            "the two differ only in where it happened")

    # A work item keeps the word, which is the half being protected.
    done = outbox.render({"verb": "work_done", "new_value": "bench test",
                          "actor_name": "Bella Fiore", "old_value": None})
    c.ok("finished" in done or "bench test" in done,
         f"and a work item still reads as work ({done!r})")

    return c.report()


CHECKS = (check_a_ticket_with_work_left_cannot_be_closed,
          check_one_item_is_counted_as_one,
          check_both_ways_out_actually_let_it_close,
          check_a_draft_is_held_to_the_same_rule,
          check_bert_disables_the_button_rather_than_hiding_the_rule,
          check_complete_is_the_word_for_a_work_item_and_close_for_a_ticket)

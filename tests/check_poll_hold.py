"""
A poll the board can't draw yet is held, not dropped.

Two things freeze the board: a drag in flight, because rendering deletes the
very widget Qt is dragging, and an open editor, because nobody should have
their half-typed card rebuilt under them. The drag has always parked its
payload and drawn it on release. The editor threw it away, so for as long as
somebody was typing the board was frozen -- a ticket raised in Discord
meanwhile did not appear until whichever poll happened to follow the editor
closing. Both hold the same way now, and both draw what they held.
"""

import ast
import pathlib

from support import Check

import bert


class Nothing:
    """Any attribute is a no-op call. Stands in for the Qt widgets."""

    def __getattr__(self, _):
        return lambda *a, **k: None


class FakeBert:
    """Enough of Bert for on_loaded, which is what is under test.

    The real one is a QMainWindow and wants a display and a live API. Only
    the state on_loaded reads has to be real; render() records that it ran.
    """

    def __init__(self, *, editing=None, dragging=False):
        self.editing_card = editing
        self.dragging = dragging
        self._pending = None
        self.cards = []
        self.completing = set()
        self.rendered = 0
        self.warned = 0
        self.banner = Nothing()
        self.refresh_btn = Nothing()

    def render(self):
        self.rendered += 1

    def _tick_freshness(self):
        pass

    def _done_checking(self):
        pass

    def _clear_toast(self):
        pass

    def _flag_edited_underneath(self, incoming):
        self.warned += 1

    # The two under test, taken off the real class rather than restated.
    on_loaded = bert.Bert.on_loaded
    apply_pending = bert.Bert.apply_pending


def poll(*thread_ids) -> dict:
    """A board payload carrying exactly these cards."""
    return {
        "board": {"cards": [{"thread_id": t, "priority": "unassigned",
                             "rank": float(i)}
                            for i, t in enumerate(thread_ids)]},
        "events": {"events": []},
        "health": {},
    }


def on(b) -> list:
    return [c["thread_id"] for c in b.cards]


def check_free_board() -> bool:
    c = Check("a board holding nothing draws every poll")

    b = FakeBert()
    b.on_loaded(poll("a", "b"))
    c.equal(on(b), ["a", "b"], "the cards arrive")
    c.equal(b.rendered, 1, "and are drawn once")
    c.ok(b._pending is None, "nothing is left held")

    return c.report()


def check_editor_holds() -> bool:
    c = Check("an open editor holds the poll")

    b = FakeBert(editing="being-typed-in")
    b.on_loaded(poll("a", "new-ticket"))
    c.equal(b.rendered, 0, "nothing is redrawn under the editor")
    c.equal(b.warned, 1, "but the editor is told the card moved")
    c.ok(b._pending is not None, "the payload is held, not dropped")

    # The regression: this used to be a bare return, so the new ticket was
    # gone for good and only the next poll after closing could bring it back.
    b.editing_card = None
    b.apply_pending()
    c.equal(on(b), ["a", "new-ticket"], "closing the editor draws what it held")
    c.equal(b.rendered, 1, "drawn once, on release")
    c.ok(b._pending is None, "and the hold is let go")

    return c.report()


def check_drag_still_holds() -> bool:
    c = Check("a drag holds it the same way")

    b = FakeBert(dragging=True)
    b.on_loaded(poll("a"))
    c.equal(b.rendered, 0, "the dragged widget is not deleted under Qt")
    c.ok(b._pending is not None, "the payload is held")

    b.dragging = False
    b.apply_pending()
    c.equal(on(b), ["a"], "and drawn when the card is let go")

    return c.report()


def check_other_hold_reparks() -> bool:
    c = Check("releasing one hold while the other is still on")

    # Editor open and a drag running: whichever ends first must not draw over
    # the one still going. on_loaded re-checks both, so it simply parks again.
    b = FakeBert(editing="being-typed-in", dragging=True)
    b.on_loaded(poll("a"))
    b.dragging = False              # drag released, editor still open
    b.apply_pending()
    c.equal(b.rendered, 0, "still nothing drawn under the open editor")
    c.ok(b._pending is not None, "the payload is parked again, not lost")

    b.editing_card = None
    b.apply_pending()
    c.equal(on(b), ["a"], "and drawn once the editor closes too")

    return c.report()


def check_stale_hold_dropped() -> bool:
    c = Check("a held poll never outlives a newer one")

    b = FakeBert(editing="being-typed-in")
    b.on_loaded(poll("old"))
    c.ok(b._pending is not None, "the older board is held")

    # The editor closed without anything draining it -- editor_is_busy clears
    # editing_card on its own when the widget has gone. The next live poll
    # must take the hold with it.
    b.editing_card = None
    b.on_loaded(poll("old", "newer"))
    c.equal(on(b), ["old", "newer"], "the newer board is drawn")
    c.ok(b._pending is None, "and the older one is dropped, not queued")

    b.apply_pending()
    c.equal(on(b), ["old", "newer"], "so nothing can replay it over the top")

    return c.report()


def _method(name):
    """The AST of one Bert method. Reading the source beats building a window:
    the checks deliberately never make a QApplication."""
    tree = ast.parse(pathlib.Path(bert.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    return next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def check_render_keeps_your_place() -> bool:
    """
    Ticking a work bubble off threw the view up or down the board.

    render() tears every card down and builds it again whenever the data
    changes, so the scrollbar loses its place -- the widgets it was measuring
    against stop existing for a moment. Tick one bubble on a card halfway down
    a fifty-ticket board and you ended up somewhere else entirely. The activity
    feed had the same bug after an undo and fixed it the same way.
    """
    c = Check("render keeps your place in the list")

    render = _method("render")
    first = render.body[0]
    c.ok(isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
         and getattr(first.value.func, "attr", None) == "_hold_scroll",
         "render holds the scroll before it touches anything")

    hold = _method("_hold_scroll")
    src = ast.dump(hold)

    # Both lists that scroll, which is the pair _edge_scroll walks.
    c.ok("verticalScrollBar" in src, "it reads the scrollbars")
    for attr in ("scroll", "rail"):
        c.ok(f"'{attr}'" in src, f"it covers self.{attr}")

    # A drag owns the scrollbar; putting it back mid-drag fights _edge_scroll.
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "dragging"
             for n in ast.walk(hold)),
         "and leaves the scrollbars alone while a card is in the air")

    # The restore has to wait for the layout, and clamp: a board that just got
    # shorter has a smaller maximum than the value we took off it.
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "singleShot"
             for n in ast.walk(hold)),
         "it puts them back after the layout settles, not during")
    c.ok(any(isinstance(n, ast.Name) and n.id == "min" for n in ast.walk(hold)),
         "clamped to the new maximum, so a shorter board doesn't overshoot")

    return c.report()


def check_the_place_is_a_card_not_a_number() -> bool:
    """
    The scrollbar number is not the place; the card at the top of the view is.

    Every pixel above the view belongs to some other card, and any of it can
    change between one rebuild and the next. Measured on a real board: sixteen
    cards above the view each gained four bubbles, the scrollbar read exactly
    the number it had before, and a different card was under the cursor -- the
    view had moved a card and a half without the number moving at all. That is
    what somebody hitting Edit saw as the board jumping up or down under them.

    So the card covering the top of the view is noted by thread_id and put
    back at the same height, and the number is only the fallback for when that
    card has gone -- completed, or filtered out by a search.
    """
    c = Check("the place held is a card, not a scrollbar number")

    hold = _method("_hold_scroll")

    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "thread_id"
             for n in ast.walk(hold)),
         "it notes which card the view was on, not just where the bar was")

    # Visual order, walked off the layouts. findChildren answers in the order
    # Qt happens to hold the widgets, which is not the order they are drawn
    # in, so "the topmost" came back as whichever card was first in the tree.
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "itemAt"
             for n in ast.walk(hold)),
         "walking the layouts, so top to bottom means what it says")
    c.ok(not any(isinstance(n, ast.Attribute) and n.attr == "findChildren"
                 for n in ast.walk(hold)),
         "and not findChildren, which answers in tree order")

    # A rebuild posts its layout requests rather than doing the work there and
    # then, so a position read before they are delivered is the old one.
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "sendPostedEvents"
             for n in ast.walk(hold)),
         "the geometry is settled before anything is measured")

    # And nothing is moved until the geometry has stopped moving. The
    # correction is worked out from where the anchor has landed, so a pass
    # run against a layout that is still settling computes the wrong one --
    # and the pass after it taking that back is what somebody sees as the
    # scrollbar glitching. Measured on a resize, which rebuilds the board
    # through the same timer the rail handle uses: +496px, back 75ms later,
    # twice for every drag of the window edge.
    inner = next((n for n in ast.walk(hold) if isinstance(n, ast.FunctionDef)
                  and any(isinstance(x, ast.Name) and x.id == n.name
                          for x in ast.walk(n))), None)
    c.ok(inner is not None, "the correction can run again after itself")

    if inner is not None:
        # The bail-out: while this reading differs from the one before it,
        # come back later instead of correcting.
        wait = next((i for i, n in enumerate(inner.body)
                     if isinstance(n, ast.If)
                     and any(isinstance(x, ast.Return) for x in ast.walk(n))
                     and any(isinstance(x, ast.Name) and x.id == inner.name
                             for x in ast.walk(n))), None)
        c.ok(wait is not None,
             "it waits for the geometry rather than correcting against it")

        if wait is not None:
            before = [x for n in inner.body[:wait] for x in ast.walk(n)]
            c.ok(not any(isinstance(x, ast.Attribute) and x.attr == "setValue"
                         for x in before),
                 "and touches no scrollbar until it has stopped moving")
            guard = inner.body[wait]
            c.ok(any(isinstance(x, ast.Name) and x.id == "tries"
                     for x in ast.walk(guard.test)),
                 "bounded, so a layout that never settles cannot loop")

    return c.report()


class Warned:
    """A card that records what it was warned about."""

    def __init__(self, base=None):
        self._edit_base = base if base is not None else {"title": "PROD: x"}
        self.warnings = []

    def warn_changed(self, msg):
        self.warnings.append(msg)


class Editing:
    """Enough of Bert for _flag_edited_underneath, which is what is tested.

    The method only reads editing_card and asks for the widget, so it can be
    called with a stand-in self -- no QApplication, no display.
    """

    def __init__(self, tid, card):
        self.editing_card = tid
        self._card = card

    def _card_widget(self, _tid):
        return self._card


def check_a_ticket_with_no_thread_has_not_left_the_board() -> bool:
    """
    Pressing + New Ticket warned that the ticket had left the board.

    _flag_edited_underneath looks the card being edited up in the incoming
    poll and says it has gone if it is not there. A ticket being started has
    no thread yet, so it is not on the board and never can be found -- the
    warning arrived within a poll of pressing the button, about a blank form,
    and said saving would probably fail. It came back on every poll after
    that, because every poll asks the same question.

    The check is on both sides: the new ticket is exempt, and a real card that
    genuinely has gone still says so.
    """
    c = Check("a ticket with no thread yet has not left the board")

    flag = bert.Bert._flag_edited_underneath

    # A ticket being started, against a board that of course does not hold it.
    card = Warned()
    flag(Editing(bert.NEW_TICKET, card), [])
    c.equal(card.warnings, [], "starting a ticket warns about nothing")

    # And with other cards in the payload, which is the real case.
    card = Warned()
    flag(Editing(bert.NEW_TICKET, card), [{"thread_id": "123", "name": "PROD: x"}])
    c.equal(card.warnings, [], "nor when the board has other cards on it")

    # The protection itself is untouched: a card that has really gone says so.
    card = Warned()
    flag(Editing("999", card), [{"thread_id": "123", "name": "PROD: x"}])
    c.equal(len(card.warnings), 1,
            "a real card that has gone still says it has left the board")
    c.ok(any("left the board" in m for m in card.warnings),
         "and says it in those words")

    # An editor that has typed nothing yet has no base, and is left alone.
    card = Warned(base={})
    flag(Editing("999", card), [])
    c.equal(card.warnings, [], "an editor with no base is not warned at all")

    return c.report()


CHECKS = (check_free_board, check_editor_holds, check_drag_still_holds,
          check_other_hold_reparks, check_stale_hold_dropped,
          check_render_keeps_your_place,
          check_the_place_is_a_card_not_a_number,
          check_a_ticket_with_no_thread_has_not_left_the_board)

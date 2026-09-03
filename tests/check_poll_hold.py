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

    def _tick_sharing(self):
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


CHECKS = (check_free_board, check_editor_holds, check_drag_still_holds,
          check_other_hold_reparks, check_stale_hold_dropped)

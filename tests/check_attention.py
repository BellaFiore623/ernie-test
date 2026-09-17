"""
Jumping to the next ticket that needs a person.

Two things wear the name "needs attention" and they are different sets. The
`unassigned` band, which `BAND_LABEL` renames Needs Attention, held 23 cards
the day this was asked for and means only that nobody has dragged them into a
priority yet. The red edge is `needs_triage()`: a blocking issue about the
title, and no `client_override` vouching for it. Three cards.

This control is about the second set, which is why it hangs off the toolbar
count rather than the band header. A card keeps its red edge wherever it is
dragged, and on the day it was asked for two of the three were sitting in
Medium -- so a control on that header would have sent somebody who clicked
"Needs Attention" to a card in Medium and taught the ambiguity to everybody
who used it.
"""

from __future__ import annotations

import ast
import pathlib

from support import Check

import bert

ROOT = pathlib.Path(__file__).resolve().parent.parent


def card(tid, priority="unassigned", rank=1000.0, issues=("title_none",),
         override=""):
    return {"thread_id": tid, "priority": priority, "rank": rank,
            "issues": list(issues), "client_override": override}


def check_the_set_is_needs_triage_and_nothing_else() -> bool:
    """One definition of the set, not two.

    The control points at the red edges, so it has to read the same predicate
    the edge does. A second copy would drift the first time `BLOCKING` changed
    and nothing would say so -- the cards would still be red and the jump
    would quietly skip one.
    """
    c = Check("the set is exactly needs_triage's")

    cards = [
        card("readable", issues=()),
        card("unreadable"),
        card("vouched-for", override="Bravo Environmental"),
        card("other-issue", issues=("equipment_pending",)),
    ]
    got = set(bert.needs_attention(cards))
    want = {x["thread_id"] for x in cards if bert.needs_triage(x)}
    c.equal(got, want, "the same cards the red edge is drawn on")
    c.equal(got, {"unreadable"}, "and that is the one unreadable card")

    # Read off the source as well: agreeing today is not the same as being
    # held together, and this is the check that would go red on a second copy.
    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "needs_attention")
    body = ast.get_source_segment(src, fn) or ""
    c.ok("needs_triage(" in body,
         "needs_attention calls the predicate rather than restating it")
    c.ok("BLOCKING" not in body,
         "and does not reach past it to the issue names")

    return c.report()


def check_the_order_is_the_order_the_eye_reads() -> bool:
    """BANDS then rank, so cycling never jumps backwards up the screen."""
    c = Check("the cycling order is the board's own")

    cards = [
        card("low-1", "low", 100.0),
        card("unassigned-2", "unassigned", 900.0),
        card("high-1", "high", 100.0),
        card("unassigned-1", "unassigned", 100.0),
        card("medium-1", "medium", 500.0),
        card("critical-1", "critical", 700.0),
    ]
    c.equal(bert.needs_attention(cards),
            ["unassigned-1", "unassigned-2", "critical-1", "high-1",
             "medium-1", "low-1"],
            "band order first, then rank inside a band")

    c.equal(bert.needs_attention([]), [], "an empty board has nothing to visit")
    c.equal(bert.needs_attention(None), [], "and neither does no board at all")

    # A band the constant does not know sorts last rather than raising: the
    # payload comes off the wire and this must never be what breaks a board.
    odd = bert.needs_attention([card("a", "high"), card("b", "nonsense")])
    c.equal(odd, ["a", "b"], "an unknown band sorts last instead of raising")

    return c.report()


def check_it_wraps_and_survives_a_card_going_away() -> bool:
    """Click past the last one and come back to the first.

    The card it was last on can vanish between clicks -- retitled, closed, or
    filtered out -- and the cycle must not be lost with it.
    """
    c = Check("the jump wraps, and survives its card going")

    order = ["a", "b", "c"]
    c.equal(bert.next_attention(order, None), "a", "nothing current starts at the top")
    c.equal(bert.next_attention(order, "a"), "b", "then the next one down")
    c.equal(bert.next_attention(order, "b"), "c", "and the next")
    c.equal(bert.next_attention(order, "c"), "a", "past the last, back to the first")
    c.equal(bert.next_attention(order, "gone"), "a",
            "a card that has been retitled since starts again at the top")
    c.equal(bert.next_attention([], "a"), None, "nothing to go to is None")
    c.equal(bert.next_attention([], None), None, "and so is nothing at all")

    c.equal(bert.next_attention(["only"], "only"), "only",
            "one card wraps to itself rather than going nowhere")

    return c.report()


def check_the_label_says_when_a_filter_is_hiding_some() -> bool:
    """The control cycles what is on screen, so the number says when that is
    not all of it. Cycling silently past a card a chip has hidden is the
    disappearing-band problem by another route.
    """
    c = Check("the count reports the narrowing")

    c.equal(bert.attention_text(0), "", "nothing at nought, so no control")
    c.equal(bert.attention_text(1), "1 needs attention", "the verb agrees")
    c.equal(bert.attention_text(3), "3 need attention", "and agrees in the plural")
    c.equal(bert.attention_text(3, 3), "3 need attention",
            "nothing to report when the filters hide none of them")
    c.equal(bert.attention_text(2, 3), "2 of 3 need attention",
            "and says so when they hide one")
    c.equal(bert.attention_text(1, 3), "1 of 3 needs attention",
            "the verb still agrees with what is on screen")
    c.equal(bert.attention_text(0, 3), "",
            "all of them hidden is no control, not '0 of 3'")

    return c.report()


def check_the_count_gives_up_words_before_the_bar_cuts_them() -> bool:
    """A third label on a bar that already asks for more than it has.

    `_fit_toolbar` shortens the two freshness labels at the window's minimum
    width; this one has to join that negotiation rather than be assumed to
    fit. The number is what cannot go -- it is red, it is the control, and the
    tooltip carries the sentence -- so the words go first.
    """
    c = Check("the count shortens rather than being cut")

    c.equal(bert.status_forms("3 need attention"),
            ["3 need attention", "3 need you", "3"],
            "words first, the number last")
    c.equal(bert.status_forms("1 needs attention"),
            ["1 needs attention", "1 needs you", "1"],
            "and the singular shortens the same way")
    c.equal(bert.status_forms("2 of 3 need attention"),
            ["2 of 3 need attention", "2 of 3 need you", "2 of 3"],
            "the narrowing survives to the shortest form")
    c.equal(bert.status_forms(""), [""], "nothing stays nothing")

    return c.report()


def check_an_open_editor_stops_the_jump() -> bool:
    """Scrolling the board away from a half-typed ticket is the same
    discourtesy the poll already parks its payload to avoid.

    Read off the source: the guard is the first thing in the function, and a
    check that made a QApplication to click the label would be the only one in
    this suite that did.
    """
    c = Check("an open editor stops the jump")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
              and n.name == "jump_attention")

    first = fn.body[1] if isinstance(fn.body[0], ast.Expr) else fn.body[0]
    c.ok(isinstance(first, ast.If),
         "the first thing it does is ask a question")
    c.ok("editing_card" in (ast.get_source_segment(src, first) or ""),
         "and the question is whether an editor is open")
    c.ok(isinstance(first.body[0], ast.Return),
         "which returns rather than carrying on")

    # And the reason is on the control, not only in the code: a click that
    # does nothing with nothing said is a broken button.
    render = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "render")
    body = ast.get_source_segment(src, render) or ""
    c.ok("setToolTip" in body and "editing_card" in body,
         "the tooltip says why while an editor is open")

    return c.report()


def check_the_control_is_the_count_itself() -> bool:
    """Not a band header, and not a second widget saying the same thing."""
    c = Check("the control is the count in the toolbar")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    bar = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name == "_toolbar")
    body = ast.get_source_segment(src, bar) or ""

    c.ok("ClickLabel" in body, "the count is a label that reports clicks")
    c.ok("self.count.clicked.connect(self.jump_attention)" in body,
         "and clicking it jumps")

    head = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                 and n.name == "BandHead"), None)
    if head is not None:
        c.ok("jump_attention" not in (ast.get_source_segment(src, head) or ""),
             "the band header does not offer it, the two sets differing")

    # Nought cards draws no control: the label is empty, so there is nothing
    # to click and nothing on the bar. The rule the roster freshness follows.
    c.equal(bert.attention_text(0), "",
            "nought cards is an empty label, so no control at all")

    return c.report()


CHECKS = (check_the_set_is_needs_triage_and_nothing_else,
          check_the_order_is_the_order_the_eye_reads,
          check_it_wraps_and_survives_a_card_going_away,
          check_the_label_says_when_a_filter_is_hiding_some,
          check_the_count_gives_up_words_before_the_bar_cuts_them,
          check_an_open_editor_stops_the_jump,
          check_the_control_is_the_count_itself)


if __name__ == "__main__":
    raise SystemExit(0 if all([fn() for fn in CHECKS]) else 1)

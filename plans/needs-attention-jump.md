# Jump to the next card that needs a person

Status: planned, not started. Asked for 2026-09-16.

## What is wanted

A control that takes you to the card needing attention nearest the top of the
board. Click it again for the next one down, and again past the last one to
come back to the first.

## The thing that decides the design

There are two meanings of "needs attention" on this board and they are not the
same set:

- The `unassigned` band, which `BAND_LABEL` renames "Needs Attention". It held
  23 cards on the day this was asked for. It means nobody has dragged them into
  a priority yet, which is the ordinary state of a new thread and not a
  problem.
- The red edge, which is `needs_triage()` in `bert.py` -- a blocking issue from
  `BLOCKING` (`bert.py:581`) and no `client_override` vouching for it. Three
  cards, and all five blocking issues are about the title: `title_none`,
  `title_unparseable`, `title_prefix_only`, `title_loose`, `title_nonstandard`.
  The client being unknown is a symptom rather than the test.

This control is about the second set. It cannot hang off the band header,
because a card keeps its red edge wherever it is dragged -- the title is still
unreadable in Medium. Measured on the live board the day it was asked for:

```
unassigned   OPS: SCI, Bravo, Inspect.AI inventory outreach
medium       ENG: Orange Cable Recall - Client list
medium       ENG: Retired bots
```

Two of three were not in the band named after the thing. Putting the control on
that header would send somebody who clicked "Needs Attention" to a card in
Medium, and teach the ambiguity to everybody who used it.

## Where it goes

The toolbar, beside the `shared board · ...` indicator, reading `3 need
attention`. It is a fact about the whole board, so it belongs on the
board-wide bar rather than inside any band.

Not drawn at all when the count is nought. An indicator that is always on is
furniture -- the rule the roster freshness note already follows -- and its
appearing is then itself worth noticing.

`_fit_toolbar()` (`bert.py:6852`) already shortens two labels until the bar
fits at the window's minimum width, and it asks for about 45px more than it
has before anything is added. A third label has to join that negotiation
rather than be assumed to fit: `3 need attention` -> `3 need you` -> `3`, the
same give-up-words-before-cutting-them rule `status_forms()` follows.

## What already exists

`reveal(tid)` (`bert.py:7321`) is the whole of the jumping. It opens a folded
band, calls `layout().activate()` so `ensureWidgetVisible` sees the
post-expand geometry, and scrolls with a 60px margin. It is already called
from three places, including a rail row click, so "click a thing, land on the
card" is an established gesture here rather than a new one.

Cycling order is `BANDS` (`bert.py:233`) then rank, which is the order the eye
reads down the screen. There is no separate ordering to invent.

## Decisions to settle before writing it

Which of these to take is the whole design; the code is small either way.

1. A filter hiding one of them. Cycling silently past a card that a chip has
   hidden is the disappearing-band problem again -- the reason empty bands are
   drawn rather than explained. Suggest cycling only what is on screen and
   saying `2 of 3`, so the number itself reports the narrowing.

2. Marking the card landed on. Three unreadable cards look alike, and arriving
   without being told which one is disorienting. A half-second outline in
   `T.RED_FG` on arrival, or nothing at all -- but not a permanent selection
   state, which would be a second kind of highlight for the board to explain.

3. An editor being open. The poll already parks its payload while
   `editing_card` is set, because rebuilding a band destroys the widget
   somebody is typing into. Scrolling the board away from a half-typed ticket
   is the same discourtesy by another route. Suggest the control does nothing
   while an editor is open, and says why on hover.

## The caveat, raised and answered

Two of the three cards looked unfixable: `ENG: Retired bots` and
`ENG: Orange Cable Recall - Client list` are holding threads rather than
customer tickets, with no date and no client, so they would wear the red edge
for ever and this control would cycle back to them every time.

A control that keeps pointing at something nobody can act on is a nag, and a
nag is a thing people learn to click past -- which costs the real one its
signal.

Answered 2026-09-16, and against the suggestion: Julian's position is that
every thread should carry the proper title format and the ENG ones are not an
exception. They stay flagged until somebody retitles them. So there is no
dismissal to build and no "not a ticket" state to invent -- the three cards
are three cards to fix, and the control is pointing at exactly what it should
be pointing at.

Which also means the count is expected to reach nought, and the control is
expected to disappear. That is the design working rather than a state nobody
thought about.

## Verification

- `needs_attention(cards)` as a pure function over the payload, so the cycling
  order and the wrap can be checked without a QApplication, which the checks
  never make.
- The set is exactly `needs_triage()`'s, held to it by a check rather than by
  a second copy of the predicate.
- Nought cards means no control in the toolbar at all.
- `_fit_toolbar` still fits at the window's minimum width with the third label
  present, which is the measurement that already exists for the other two.

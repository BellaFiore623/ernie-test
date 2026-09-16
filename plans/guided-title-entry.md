# Type it or pick it, and let both mean the same title

Status: planned, not started. Decided 2026-09-16.

## What is wanted

The title has to stay typeable, because people have been typing it into
Discord for years and that habit is not worth breaking. It also has to be
pickable, because the rules that make a title parse are exactly the rules
somebody typing at speed forgets.

So: both, on the same title, at the same time. Not a mode anybody chooses.
Type in the box and the fields follow; change a field and the box follows.
Somebody who prefers one never has to notice the other exists.

**Why no toggle.** Two modes is two code paths, a setting to explain, and a
question nobody wants to answer at the moment of switching -- what happens to
a title the fields cannot hold. Every bug found on 2026-09-16 lived where one
control was quietly serving two jobs; another mode is more of that.

**And the typed path is not the lesser one.** `title_problems` already tells
a card what is wrong with its title -- no tag, no client name, no date, date
not readable -- so somebody who ignores the fields entirely still gets caught
by the same warnings that catch a title typed in Discord. That is the reason
this can be two-way rather than guided-only: the free box has a backstop.

## What already exists

Three of the five fields, at `bert.py:3290`:

```
Thread title    free text          the authority today
Tag             Combo              guided
Client          ClientCombo        guided
Work items      WorkBar            separate already, not part of the title
First message   new tickets only
```

Missing: **Date** and **Description**. That is the whole of the new surface.

One direction of the binding exists too. `_suggest_title` (`:3365`) rebuilds
the title's client segment when the client changes, and `_check_title`
(`:3399`) already pushes the other way for the tag -- it keeps the Tag
dropdown showing whatever the title says, including a prefix typed by hand.
So title -> field is proven for one field and field -> title for another;
this is those two generalised.

## The rule that makes it safe

`_title_touched` (`:3212`, set at `:3342`). Once somebody types in the title
themselves, the fields stop rewriting it. That is what keeps the ten
production cards whose titles the fields cannot hold repairable -- the box
never stops being the authority when it has to be.

It wants one decision it does not need today: whether touching a *field*
after touching the title puts the fields back in charge. Suggest yes, because
the alternative is an editor that has quietly stopped listening to half its
own controls.

## The date is the anchor, and that is not a detail

Found on 2026-09-16: the client is whatever sits between the tag and the
date, so with no date there is no client slot -- `PROD: Thrasher - Trade show
TOF` parses with the whole of it as the summary and no client at all.
`title_takes_client` (`:837`) is that rule, and the note under the Client box
says so when a pick cannot reach the title.

Which means the Date field is the one that unlocks the others, and it has to
be a real date control rather than another text box. A typed date moves the
parsing problem rather than removing it, and the parser is where every
surprise of 2026-09-16 came from.

## Decisions to settle before writing it

1. **When does title -> fields run.** Per keystroke fights the person typing:
   a half-typed `PROD: Thra` would blank the Client box on the way past.
   Suggest on a pause or on focus leaving the box, and never while the field
   being updated has focus.

2. **What the fields show for a title they cannot represent.** Blank, with the
   note already written for the Client box extended to the others? Or the last
   thing they held? Blank is more honest and matches what the card now says
   about the same title.

3. **The queue dropdown is already destructive and the client one is not.**
   `_queue_picked` (`:3380`) rewrites an unparseable title from scratch --
   `f"{q}: {client} - {today} - what it's about"` -- discarding whatever was
   there. Picking a tag on `Thrasher - Trade show TOF` loses "Trade show
   TOF". The client dropdown refuses to touch the same title and says so.
   Both cannot be right. Suggest the client's behaviour wins and the queue
   stops discarding, because losing what somebody typed is the worse failure
   -- but it is a real change to a control people already use.

## What this does not fix

Titles arriving from Discord. 36 people open threads there by hand and that
is where most titles come from; nothing in Bert's editor reaches them. This
improves what Bert creates and repairs, which is the smaller half. The
warnings on the card remain the only thing that touches the rest, which is
another reason they had to be right first.

## Verification

- The binding is a pure function each way -- compose(fields) -> title and
  parse(title) -> fields -- so both are checked without a QApplication.
- Round trip: a title that parses strictly survives compose(parse(title))
  unchanged. This is the one that would catch `04aug26` quietly becoming
  `04Aug26`, which was measured at 29 production cards and is the reason
  composing was rejected as the *only* path.
- A title the fields cannot hold is not rewritten by touching a field.
- `title_takes_client` and `title_problems` keep agreeing: the titles a pick
  cannot reach are the ones the card is asking for a date on.

# Say what is wrong with a card, in words, and say all of it

Status: planned, not started. Asked for 2026-09-16 after Julian looked at the
board.

## What is wanted

Two things, from the same conversation:

1. Every thread should carry the proper title format, and the ENG holding
   threads are not an exception. They should be flagged until they are
   retitled. This settles the open question in
   `plans/needs-attention-jump.md`, and settles it against the suggestion made
   there -- the board does not need a way to say "this thread is not a
   ticket"; the threads need fixing.
2. A card should say what is wrong with it specifically -- no title, no
   client, and so on -- in the register of the notes the editor already shows,
   and it should be able to say more than one thing at once.

## What a card can say today

One line, `bert.py:2985`:

```python
texts = [i.replace("_", " ") for i in (d.get("issues") or [])[:2]
         if i not in BLOCKING]
```

Three separate problems in it.

Blocking issues are excluded. A red card therefore shows no chip at all
explaining the red: the edge is the only signal and it names nothing. All
three red cards on the board today are red for the same reason and none of
them says so.

The text is the issue code with its underscores swapped for spaces, so the
card reads `equipment master not found` and `client cr not found` -- machine
vocabulary put in front of a person. There is no label table anywhere; this
line is it.

It is capped at two, and `_fit_foot` can drop that to one. No card on the
board carries more than two today, so the cap costs nothing yet, but "could be
multiple" is the ask and the cap is the answer to it.

## What the codes actually mean

Worth writing down, because a label that misdescribes the condition is worse
than the raw code, and the three that can reach a card are not obvious from
their names. `parse_title` (`ernie_extract.py:143`) tries STRICT, then LOOSE,
then PREFIX_ONLY:

| code | what parsed | what a person should be told |
|---|---|---|
| `title_none` | nothing, not even the tag | no tag, client or date |
| `title_prefix_only` | the tag, and nothing else | no client and no date |
| `title_loose` | all of it, in a format off the standard | date or spacing is non-standard |

`ernie_api.py:1050` appends exactly one of those three, and only those three:
`title_{confidence}` for `loose`, `prefix_only` and `none`. So a card carries
at most one title issue and never two saying the same thing.

Note while doing this: `BLOCKING` (`bert.py:581`) lists five codes, and two of
them -- `title_unparseable` and `title_nonstandard` -- are the names
`extract_thread` uses at the thread level, which the API never puts on a card.
Harmless as belt and braces, misleading as a list of what a card can carry.
Confirm before trimming it.

## The fork to settle first

"Similar to the ones displayed when you hit edit" can mean two things, and
they lead to different work:

Either the card face gains the wording, which runs straight into the space
fight `_fit_foot` already exists to referee -- one chip put Complete at x=470
on a 463px card, two needed 850px. Adding blocking issues means three or four
chips competing for a row that already gives up the age to fit two.

Or the editor gains a block listing everything wrong with the card, in the
register `client_note()` already uses -- a sentence under a field, amber,
about a pending state rather than an error. There is room there, it is where
somebody is standing when they fix it, and it can list four things without
fighting anybody for the space.

The second is the one that can show all of them, and the first is the one you
see without clicking. They are not exclusive: a short chip on the face naming
the worst one, and the full list in the editor, is probably the answer -- but
it is two pieces of work and the wording has to agree between them, so decide
before starting rather than after.

## The part that is cheap either way

A label table, one place, mapping every issue code to a phrase a person would
say. It is needed by both halves of the fork and by the tooltip that already
carries whatever the row could not show. Nine codes plus one parameterised:

```
title_none                  no tag, client or date
title_prefix_only           no client or date
title_loose                 non-standard date or spacing
equipment_master_not_found  equipment not in the master list
equipment_master_missing    no equipment on the ticket
equipment_number_pending    equipment number not issued yet
client_cr_not_found         client CR not found
client_cr_missing           no client CR on the ticket
proposal_never_confirmed    build request never confirmed
unknown_equipment_type:X    unrecognised equipment type X
```

Wording above is a first draft and wants Julian's eye before it ships: these
are the words the board will use for the rest of its life, and he is the one
who reads them.

## Verification

- The label table covers every code the tree can emit, held by a check that
  walks `ernie_extract` for `issues.append` rather than by a list somebody
  keeps in step by hand.
- A red card names its reason. The three on the board today are the fixture.
- A card with no issues gains nothing, on the face or in the editor.
- `_fit_foot` still lands every control on the card at the 463px minimum, in
  both themes, with the new chip present -- the 240-render sweep that exists
  for this already.

# Work items merge per item, not per card

**Status: done 2026-09-18.** Found by running phase 5's third case across two
boards rather than by hand, and fixed the same day. Kept as the record of what
was wrong and of the four directions the fix had to not break --
`tests/check_two_boards.py` holds all of them now.

The table below is what was implemented. `work` came out of the conflict
comparison entirely: a card whose only difference is a bubble is no longer a
conflict, so `note_discarded` never fires for one, and `resolve()` hands the
remote list to `apply_card` to merge rather than picking a winner.

## The problem, in one sentence

Two people adding a different work item to the same ticket on two boards keeps
one of them and silently tombstones the other, which is the opposite of the
promise `CLAUDE.md` makes about `work_add`.

## What happens

Bella adds *order the cable*; Chris adds *chase the RMA*; neither has synced.

```
base work (both agreed)   []
channel after her publish ['order the cable']
his pull                  conflict -> the channel wins
his bubbles now           ['order the cable']
his tombstoned            ['chase the RMA']
```

*chase the RMA* is then gone from his board **and** from the channel, so it is
gone from hers too once she pulls. Nothing he did survives, and the only trace
is an `overruled` row on his machine -- which, per
`plans/changelog-per-machine.md`, the change log cannot even see.

## Why

The promise is real but it is about the wrong layer. `CLAUDE.md`:

> The editor sends `work_add` / `work_remove`, not the whole list, so two
> people adding different items merge instead of colliding.

That is true of `POST /cards/{id}/edit`: two editors against **one** Ernie do
merge, because each sends only its own delta. Across the state channel the
payload carries `work` as **a whole list**, and `resolve()` treats it as one
field -- so both boards moved it, the channel wins, and the losing list is
discarded entire.

`apply_card` then enforces the winning list exactly: an item present locally
and absent from the payload is tombstoned.

```python
for iid, li in local.items():
    if iid not in remote and not li["removed_at"]:
        con.execute("UPDATE work_items SET removed_at=?, removed_by=? ...")
```

Which is right when the payload is newer than everything local, and wrong when
it is a competing edit.

## The fix, and why it is available

**Per item, the same way the fields went per field**, one level deeper. Every
work item already has a stable `item_id`, the rows are never hard-deleted, and
**the base already carries the previous work list** -- which is the piece that
makes a real three-way possible rather than a guess:

| in base | in remote | in local | means | do |
|---|---|---|---|---|
| no | yes | no | they added it | take it |
| no | no | yes | we added it | keep it |
| yes | no | yes | they removed it | remove it |
| yes | yes | no | we removed it | leave it removed |
| yes | yes | yes | nobody touched it | nothing |

The `done` flag needs the same treatment per item, and a genuine collision --
both ticked the same bubble, or one ticked while the other removed -- is where
the channel still wins and `note_discarded` still has something to say.

Then `work` comes out of `FIELDS`, because it stops being one value: a card
whose only difference is work would no longer be a conflict at all, which is
what the reported case wants.

## Open questions

1. **Does `removed_at` need to carry which board removed it?** The table has
   `removed_by`, which is a name rather than a machine, and two machines can
   hold the same person's name. Probably not needed -- the base answers it --
   but worth confirming before relying on it.
2. **Position.** `apply_card` rewrites `position` from the payload's order. With
   two lists merged there is no single order, and taking the remote's and
   appending ours is the obvious answer rather than the right one. It is
   cosmetic, so it should not hold the rest up.
3. Whether a per-item conflict deserves its own `overruled` row per item or one
   for the card. One, on the reasoning already written down for
   `note_discarded`: a feed that says it three times for one pull is reporting
   the mechanism.

## Verification

`tests/check_two_boards.py` already has the case written and expects the merge;
it is the assertion that currently fails. The three cases beside it must stay
green, particularly the one that closes a ticket -- `completed` travels through
the same payload and must not be caught up in a change to how `work` resolves.

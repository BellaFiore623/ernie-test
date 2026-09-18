# Every machine logs its own changes

**Status: planned, not started.** Deferred deliberately until the two-machine
setup has been tested on production, because one of the two open questions
below can only be answered by watching two real boards disagree.

That test is now running, it has produced two real disagreements, and it has
turned up a concrete cost of the one-machine rule that was not on this page
when it was written -- see *The one event that is already per-machine*.

## The problem, in one sentence

`#ernie-logs` records the board's history, and it only records it while **one
nominated machine** is running — which reads to the person holding that
machine as a duty to leave a laptop on.

## Why it is one machine today

Not by design. By an accident of identity.

Both boards hold the whole history: their own changes, and replays of the
other's arriving through `#ernie-state`. When a change replays,
`ernie_state.py:729` writes it with a **fresh** id:

```python
eid = str(uuid.uuid4())
con.execute("""INSERT INTO events (event_id, occurred_at, actor_name, ...""")
```

So one real change exists as two unrelated rows on two machines, and nothing
connects them. `changelog_sent` is keyed per event id, which is exactly right
within a machine and useless across two: each sees its own row as unlogged.
Two loggers therefore post every line twice, and a doubled record is worse
than a late one, because a reader cannot tell by looking which entries are
real.

**Nor can a replay be recognised after the fact.** The `events` columns are
`event_id, occurred_at, actor_name, thread_id, verb, old_value, new_value,
undone_at, undone_by, dispatch_after, claimed_at, posted_at,
discord_message_id, attempts, last_error, sent_steps` — nothing says where the
row came from. `dispatch_after IS NULL` does not, because every silent local
change is NULL too: reorders, and band moves that are not in or out of
`critical`.

**And the timestamp cannot stand in for an identity either.** `apply_card`
takes the payload's own `at` through `sane_time()`, which **clamps it to now**
so a laptop running fast cannot park its changes at the top of the feed. A
clamped timestamp differs between machines, so "same thread, same verb, same
moment" is not reliably the same moment.

## The design

**Invert the rule. Every machine sets `CHANGELOG_CHANNEL_ID`, and each logs
only the changes it made itself.**

A change is made on exactly one machine, so it is logged exactly once, by the
machine that made it. Nobody's uptime affects anybody else's record, and
there is no machine to nominate.

Three edits:

1. **`schema.sql`** — `events.replayed INTEGER NOT NULL DEFAULT 0`, and the
   same line in `ernie_load.ADDED_COLUMNS`, which is what migrates an
   installed exe that has no shell to run a migration in.
2. **`ernie_state.py:729`** — the one INSERT that writes a replay sets
   `replayed = 1`. It is the only place a replay is created, which is what
   makes this a one-line change rather than a survey.
3. **`ernie_changelog.pending()`** (`ernie_changelog.py:139`) — add
   `AND e.replayed = 0` to the WHERE clause. `catch_up()` is left alone: it
   marks everything as already-logged on first run regardless, and a replay
   marked as logged is correct in both designs.

Nothing else moves. `changelog_sent` stays per-event and per-machine, which
is now exactly the right scope: a machine owns its own lines.

## What it costs

**Out-of-order arrival becomes normal rather than exceptional.** Two machines
post independently, so a line written at 09:00 on a laptop opened at 17:00
lands after lines from 16:00. That is already survivable — every line carries
its own timestamp from `discord_time(e['occurred_at'], 'f')` — but the
channel stops being a clean chronology, and the docs should say so rather
than leave somebody puzzling at it.

**A machine with no `CHANGELOG_CHANNEL_ID` becomes a hole.** Today a
non-logging machine still gets its changes recorded, via the other machine's
replay. Under this design its changes are logged by nobody, silently. So
"only one machine" becomes "all of them", and that has to be stated where
somebody setting up a second stack will read it — `TESTING.md`, the RTF, and
the comment in the env template, all three of which currently say the
opposite.

## The one event that is already per-machine, and is being lost

Found 2026-09-18 on production, reported as: Chris's board showed that a move
of his on `PROD: Rhino` had been overruled, and **the change log never
mentioned it** -- it carried only the winner's changes.

That is not a bug in the log. It is the one-machine rule meeting the one event
type the rule's premise does not cover.

The premise above is that **both boards hold the whole history**: a change
replays through `#ernie-state` and each machine ends up with its own row for
it, which is why two loggers would post every line twice. `overruled` breaks
that. It is written by `note_discarded()` only on the board whose change was
**thrown away**, and the winning board never learns a clash happened at all --
it cannot, because from its side nothing conflicted. Its value went up and
stayed up.

So the record of a discarded change exists on exactly one machine, and it is
never the machine running the logger unless the logger happens to be the one
that lost. In the sighting above the logger was on the winning machine, so the
line could not be written by anybody.

**This matters more than an ordinary missing line.** Every other event is a
change that happened, and the board's final state records it whether the log
does or not. This is the only one that records a change that *did not* happen
-- somebody's edit being dropped -- and it is the only thing in the system that
can quietly lose work. The feed on the losing machine is currently the whole
of the audit trail for it, which is one laptop and no further.

### What it changes about this plan

It removes the objection to a second logger for this verb, and it is worth
being exact about why: `overruled` cannot duplicate, because it never exists
on two machines. Every other verb can and does.

That suggests the per-machine design below should carry a **per-verb** floor
rather than an all-or-nothing switch -- a machine that is not the nominated
logger still logs the events that only it can have. Whether that is worth the
second key in the env, against simply finishing the per-machine design and
logging everything from everywhere, is the question to settle. The shape to
avoid is a third rule kept by hand: a "primary" flag that somebody has to
remember to set on exactly one of two machines is the same trap
`CHANGELOG_CHANNEL_ID` and `ANNOUNCE_THREAD_CHANGES` already are.

### Done in the meantime

Not the cross-machine half, which is this plan. But the wording, because it
was wrong wherever it *did* get logged: `describe()` had no branch for the
verb and fell through to the generic ending, reading `overruled on *PROD:
Rhino*` -- naming neither the field nor the value. It now reads
**`Bella Fiore overruled priority on PROD: Rhino -- 'high' was dropped`**, and
`note_discarded` keeps the field and the lost value in separate columns so the
feed's sentence and the log's do not have to strip each other's half out of
one string.

## Open questions — settle these before writing code

**1. Undo across machines. This is the real one.**

A posted line is struck through in place by editing the message, off
`changelog_sent.sent_at` against `events.undone_at`
(`ernie_changelog.retracted()`, `:157`). That works while one machine owns
both the row and the line.

Under this design: you make a change, your machine logs it and owns the
message. Julian then undoes it. **His** row gets `undone_at`; yours does not,
and he cannot edit a line he did not post. The strikethrough goes missing,
and the record then says a change stands that was taken back — which is worse
than the duplication this is meant to fix.

Three ways out, none obviously right yet:

- **Propagate the undo to the originating machine.** `#ernie-state` carries
  card state, not event history, so there is nothing there to carry it today.
  Would need a field, which is a `FORMAT_VERSION` bump.
- **Let any machine strike any line**, by finding it in the channel rather
  than from `changelog_sent`. Costs a read, and needs the line to carry
  something identifying — which is the stable-change-id problem again.
- **Log the undo as its own line** instead of editing the original. Loses the
  "made and taken back" reading that the strikethrough gives, and the current
  note in CLAUDE.md argues for that reading explicitly.

**2. Does a replay ever need logging after all?** A card the channel knows and
this machine has no thread for is skipped, not invented. If a change can reach
a board *only* as a replay — never having been a local event anywhere that
logs — it would go unrecorded. Worth proving impossible rather than assuming
it.

## Verification

- `tests/check_changelog.py` exists; extend it rather than adding a file.
- A replayed event is never in `pending()`, and a local one always is.
- A local event that was *also* replayed elsewhere is logged exactly once in
  a simulated two-database fixture — the check that would have caught the
  original duplication.
- `catch_up()` still marks replays as logged, so switching the log on does
  not post a back catalogue through the new filter.
- `ADDED_COLUMNS` migrates a database made before the column, idempotently,
  and `check_app.py` already walks that list.

## Scale, for context

Trivial today, which is why there is no hurry: production's `events` holds 26
rows and the sandbox 24, all of them local, because production has only ever
run one machine. The duplication this fixes has therefore **never actually
happened** — it is a property of the design waiting for the second stack,
which is the same shape as the two status embeds in one thread that the
weekend of two databases eventually produced.

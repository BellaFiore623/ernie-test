# Every machine logs its own changes

**Status: planned, not started.** Deferred deliberately until the two-machine
setup has been tested on production, because one of the two open questions
below can only be answered by watching two real boards disagree.

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

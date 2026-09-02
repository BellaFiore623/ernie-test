# Ernie + Bert

Internal tooling for tracking equipment tickets. Discord threads are the
single source of truth. Ernie mirrors them into SQLite and posts updates
back; Bert is a desktop board on top of Ernie's HTTP API.

## Files

| File | Role |
|---|---|
| `ernie_sync.py` | Discord → SQLite. Read-only against Discord. Runs as a loop. |
| `ernie_extract.py` | Parsing. Pure functions over dicts, no I/O, no network. |
| `ernie_load.py` | Writes extracted records into SQLite. Idempotent. |
| `ernie_api.py` | FastAPI over the database. Everything Bert talks to. |
| `ernie_outbox.py` | The **only** thing that posts to Discord. |
| `bert.py` | PySide6 client. |
| `schema.sql` | Applied on every `connect()`. All `CREATE ... IF NOT EXISTS`. |
| `seed_test_server.py` | Builds realistic test threads. Test guild only. |
| `wipe_test.py` | Deletes all threads in the test channel. Test guild only. |
| `ernie_state.py` | Board state in Discord: one message per card in `#ernie-state`. |
| `ernie_backup.py` | Online SQLite backup with rotation. |
| `q.py` | Ad-hoc SQL helper. |
| `run.sh` | Starts the whole stack. `./run.sh test bert` |
| `bert.cmd` | Double-clickable launcher for a tester who runs only Bert. |
| `stack.cmd` | Double-clickable launcher for a tester who runs their own stack. |
| `TESTING.md` | Hand this to the tester. Setup on their own laptop, start to finish. |

## Hard rules

- **Never test against production.** Always `--env ernie-test.env --db ernie-test.db`.
- **All Discord writes go through `Discord.write()`.** The guild guard lives
  there so no code path can skip it. Never add a direct `http.post` or
  `http.patch` to Discord anywhere else.
- `ALLOW_DISCORD_WRITES` must exactly equal `DISCORD_GUILD_ID` or nothing
  posts. Production's env file does not contain the line at all.
- **`if __name__ == "__main__":` stays at the very end of every file.**
  `uvicorn.run()` blocks, so anything appended below it never registers.
  This has already caused a "route not found" bug once.
- **Keep sync transactions short.** `ernie_sync` commits per thread. Bert
  writes to the same database, and a long transaction causes
  `database is locked`.
- **Never hard-delete from the mirror.** Discord is mutable, so
  `thread_titles` and `message_revisions` are append-only and deletions set
  `deleted_at`. Bert's own state (`cards`, `events`) is never overwritten by
  a re-sync.
- **Compare timestamps with `datetime()` on both sides in SQL.** Python
  writes ISO8601 with a `T`; SQLite's `datetime('now')` uses a space. Raw
  string comparison is always false. This silently broke the outbox once.
- Secrets live in `ernie.env` / `ernie-test.env`, both gitignored. Never put
  a token in a `.py` file.

## Data model notes

- Key everything on `thread_id`. Titles change (PROD ↔ OPS renames are
  normal), so nothing may be keyed on parsed title fields.
- Priority bands: `unassigned`, `critical`, `high`, `medium`, `low`. New
  threads land in `unassigned`; a human drags them out. Ties within a band
  are broken by a shared fractional `rank`.
- Work is a list, not a field: `work_items`, one row per bubble on the card.
  Rows are never deleted — a tick in view mode sets `done_at`, an ✕ in the
  editor sets `removed_at`, and undo needs both rows still there. The editor
  sends `work_add` / `work_remove`, not the whole list, so two people adding
  different items merge instead of colliding.
- `cards.action_item`, `build_state`, `return_state` and `direction` are
  retired — work items replaced all four. Nothing shows or edits them; they
  stay only so undo can reach an old `edited` event.
- Ticket embeds carry no priority signal — `Priority` is always "High" and
  `Labels` always "Operations" in real data. Priority is set by hand in Bert.
- `-- not found --` in an embed and `####` in an equipment ID are *pending*
  states, not errors. Amber, not red. Red is for genuinely unreadable titles.
- Ticket `Existing Return ticket(s) referenced in this thread (N)` has a
  varying count in the field name — match by prefix, not exact name.
- `#customer-threads` generates cards. `#customer-support` is mirrored for
  history only (`generate_cards = 0`).
- Archived means done: a keepalive bot pings live threads every three days,
  so nothing goes quiet by accident.

## Writes and undo

Every change writes one row to `events`. That single table backs the
activity feed, undo, and the outbox.

- `dispatch_after` = when Ernie may post. `NULL` means never post.
- Priority moves are silent except in or out of `critical`, which posts. Every
  other band change, and every reorder within a band, is board housekeeping:
  posting each nudge between high and medium is noise in a customer thread.
- Undo inside the window deletes nothing from Discord because nothing was
  ever sent. Undo after it posts a correction message instead.
- Edits are **batched**: saving four fields writes one event and posts one
  message. Do not split this into per-field events.
- Writes take an idempotency `key`; retries return the original result.

## The state channel

So two people on two machines share one board without either hosting the
other's API. Priority, rank, work items and completion live in
`#ernie-state` (`STATE_CHANNEL_ID`), one message per card, edited in place.

- **Not the thread title.** Measured in the sandbox: a message edit recovers
  from its rate limit in 0.67s and announces nothing; a thread rename allows
  two per ten minutes, comes back `scope: shared` so a second Ernie gets no
  budget of its own, and posts a system message into the customer thread
  every time. The standard rate-limit headers do not warn about the rename
  limit -- they read healthy right up to the 429.
- **One message per card, not one document.** Two people moving different
  cards edit different messages and never collide, and no card can outgrow
  the 2000-character content cap.
- Each message names its own `thread_id`, so the channel is self-describing
  and an interrupted publish resumes without duplicating.
- Everything above the fence is derived and never parsed back, so editing
  the prose in Discord cannot corrupt the board -- which is what lets it be
  chatty enough to read: band and position, the bubbles with ticked ones
  struck through, and who last touched it.
- One extra message carries a **`**Board**` summary** -- the whole running
  order, in the same band order Bert shows, so the channel can be read top to
  bottom when checking two boards agree. It holds no state, is skipped by
  `parse()`, and carries no timestamp of its own: one would differ on every
  publish and rewrite the message every cycle for nothing.
- A card message is rewritten when its state changes *or* when the prose
  would read differently, so changing `render()` re-renders the channel
  rather than leaving old cards in the old format forever.
- **The two directions live in different processes**, because
  `ernie_sync.py` is read-only against Discord and `ernie_outbox.py` is the
  only thing that writes there. Sync pulls the channel into SQLite; the
  outbox publishes SQLite into the channel.
- **Conflicts are resolved three-way against `state_sync`, never by
  comparing the two machines' clocks.** `state_sync` holds what this machine
  last agreed with the channel about, per card; against that base, the
  channel moved, we moved, both moved, or neither. Both moved means the
  channel wins -- it is the shared copy -- and the losing change is named in
  the feed rather than vanishing. A card with no base adopts the channel, so
  a board joining an existing session takes the shared state.
- Timestamps decide nothing. `cards.updated_at` is this machine's own clock
  and means "when this row changed here"; a remote `at` is only used to file
  the replayed event, clamped to now so a fast laptop can't park its changes
  at the top of the feed. `publish()` checks this machine against Discord's
  clock -- the one clock both share -- and says so past `SKEW_WARN_S`.
- Applying a remote change writes an event with **`dispatch_after` NULL**.
  The machine that made the change already queued its own message; giving
  the replay a dispatch would post the same update twice, once per board.
- A card the channel knows and this machine has no row for is skipped, not
  invented -- its thread simply hasn't synced here yet.
- Completed cards stay in the channel carrying `completed: true`, so closing
  one propagates. Cards closed before the channel ever saw them are not
  backfilled.

## Running

```bash
./run.sh test bert          # sandbox: sync + outbox + api + bert
./run.sh test bert lan      # same, API reachable from other machines
./run.sh prod               # production: sync + api only, no outbox
python ernie_sync.py --once --env ernie-test.env --db ernie-test.db
```

## Testing with somebody else

Two ways, and they are not the same setup. `TESTING.md` is the version to
hand over.

**One backend, two Berts.** You run `./run.sh test bert lan`, which binds the
API to every interface and prints the address; they run `bert.cmd`, which
asks for it once and remembers it. They need Python and a clone -- no token,
no database, no migration -- and the board updates at Bert's 5s poll. Same
network only. In *this* setup they must not run `run.sh`: a second sync and
outbox with no `STATE_CHANNEL_ID` is a second board, not a shared one.

**Two full stacks.** Both run everything, sharing only `#ernie-state`.
Works across networks and neither laptop has to stay up for the other, at the
cost of a token on both machines and a board that is up to a sync cycle
behind. `STATE_CHANNEL_ID` is what makes it one board; `ALLOW_DISCORD_WRITES`
must equal `DISCORD_GUILD_ID` or their changes never leave their machine. A
machine joining with no `state_sync` rows adopts the channel, so a fresh
clone comes up already matching the shared board.

`ernie_state.py --check` is the preflight for the second setup and what
`stack.cmd` runs before starting anything: it catches a missing
`STATE_CHANNEL_ID`, a channel the bot can't reach, and a clock out by more
than `SKEW_WARN_S`. Clock skew is read from the `Date` header of a live
response -- an existing message's timestamp says when it was written, not
what time it is, so measuring against one reports its age as skew.

Bert shows which it is: the `shared · …` indicator by the refresh button
appears only when `state_sync` has rows, and reports in step, waiting to
send, or out of contact.

The API has no authentication, so on `lan` anyone who can reach the port can
move cards and post to the thread. Trusted network, for as long as the test
lasts, never port-forwarded. `lan` is refused outright for `prod`.

Environment: Windows, Git Bash (MINGW64), Python 3.13, SQLite in WAL mode.

## Style

Match what's there: standard library first, `httpx` for HTTP, dataclasses for
records, plain functions over classes unless state demands it. Comments
explain *why*, not *what*. No new dependencies without a reason.

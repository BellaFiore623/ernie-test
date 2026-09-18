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
| `ernie_outbox.py` | The only thing that posts to Discord. |
| `bert.py` | PySide6 client. |
| `schema.sql` | Applied on every `connect()`. All `CREATE ... IF NOT EXISTS`. |
| `seed_test_server.py` | Builds realistic test threads. Test guild only. `--limit N` for a short board while iterating. |
| `wipe_test.py` | Deletes all threads in the test channel. Test guild only. |
| `ernie_state.py` | Board state in Discord: one message per card in `#ernie-state`. |
| `ernie_changelog.py` | Every change, appended to the log channel -- `#change-log` in the sandbox, `#ernie-logs` in production. Off unless configured. |
| `ernie_jira.py` | Customer list, Jira → SQLite. Read-only against Jira. Off unless configured. |
| `jira_client.py` | A standalone Jira CLI -- `test`, `search "JQL"`, fetch an issue. Nothing imports it; it is for looking at Jira by hand while working out a query. |
| `ernie_version.py` | The version number, and which build is answering. Imported by everything that says one. |
| `ernie_status.py` | The ticket's status, as a pinned message in its own thread. Rides with the outbox. |
| `ernie_app.py` | One process: sync, outbox, the API and Bert together. What the exe runs. `--headless` starts everything but Bert. |
| `build.py` | Builds the exe. `python build.py [--version X] [--clean] [--installer]`. |
| `ernie.spec` | PyInstaller's recipe. onedir, Qt trimmed, the hidden imports a walker cannot find. |
| `installer/ernie.nsi` | NSIS. Wraps `dist/Bert` in one per-user setup.exe. Carries no secret. The app is Bert; `AppDir` on disk stays Ernie. |
| `run.sh` | Starts the whole stack as four processes, for working from source. `./run.sh test bert` |
| `bert.cmd` | Double-clickable launcher for a tester who runs only Bert. |
| `stack.cmd` | Double-clickable launcher for a tester who runs their own stack. |
| `tools/q.py` | Ad-hoc SQL helper, **read-only unless `--write`**. `python tools/q.py "SELECT ..." ernie-test.db` |
| `tools/wal_watch.py` | Watches for the reader that pins the WAL, while it is happening. Holds one connection, passive-checkpoints on a beat, names what was running. The one tool here that must be read-write. |
| `tools/backfill_message_types.py` | Fetches Discord's message `type` for rows written before the column existed. Read-only against Discord, writes one column, resumable. |
| `tools/rebuild_title_history.py` | Writes each recovered rename as the title revision it was. No network. Cannot change what the board shows today. |
| `tools/backfill_closers.py` | Puts names on closures recorded before View Audit Log was granted. Read-only against Discord, fills two NULLs, `--dry-run`. |
| `tools/fake_stats_data.py` | Invents a past for the sandbox so the figures panel can be looked at. Refuses production; `--clear` undoes it. |
| `tools/ernie_backup.py` | Online SQLite backup with rotation. |
| `tools/dump_threads.py` | Raw API JSON to disk. Read-only, for seeing what Discord actually sent. |
| `tools/bashrc-snippet.sh` | Optional shell shortcuts. Nothing depends on it. |
| `assets/bert_logo.png` | Bert's mark. `bert.py` resolves it relative to itself. |
| `assets/bert_update.png` | The face Bert makes about a version mismatch. Shown by `UpdateDialog`. |
| `requirements.txt` | httpx, fastapi, uvicorn, pydantic; PySide6 for Bert only. |
| `ernie-test.env.example` | The env file's shape, with no values. Copied, not edited. |
| `backups/` | Where `tools/ernie_backup.py` writes, and where the `.pre-migrate-` / `.pre-reseed-` snapshots live under `snapshots/`. Gitignored; 55 MB of them were sitting in the root. |
| `migrations/` | One-off scripts already applied everywhere. Kept as a record; a fresh database never runs them. |
| `plans/` | Work decided on but not started, with the reasoning and the open questions. A plan here is a thing to pick up, not a thing that is done. |
| `docs/` | Why things are the way they are: `bert-ui.md`, `discord.md`, `clients.md`, `releases.md`. Not needed to make a change safely -- needed to understand one. |
| `tests/` | `python tests/run.py`. Standard library, no network, no database of yours -- the fixture builds one from `schema.sql` in a temp directory. |
| `README.md` | For somebody arriving at the repository. What it is, how to run it, why it is shaped this way. |
| `TESTING.md` | Hand this to the tester. Both setups, start to finish. |
| `SetupGuide.rtf` | The same ground as TESTING.md, in a form somebody can open without a code editor. |

## Hard rules

- **Never test against production.** Always `--env ernie-test.env --db ernie-test.db`.
- **All Discord writes go through `Discord.write()`.** The guild guard lives
  there so no code path can skip it. Never add a direct `http.post` or
  `http.patch` to Discord anywhere else.
- **`ALLOW_DISCORD_WRITES` must exactly equal `DISCORD_GUILD_ID` or nothing
  posts.** Production went live 2026-09-15 and the installed env carries the
  line; the master at `C:/Users/Edge/ernie/ernie.env` deliberately does not,
  so a copy handed to a second person arrives read-only and becomes a writer
  only when somebody uncomments it on purpose. The same shape as
  `CHANGELOG_CHANNEL_ID`: the one-machine decision expressed as a file rather
  than as something to remember.
- Every endpoint closes its connection in a `finally`, without exception.
  Seven did it only on the happy path -- `health`, `events`, `clients`,
  `client_roster`, `card_detail`, `card_messages`, `check_schema` -- so a
  request that raised part-way leaked one, and Bert polls `/health` and
  `/events` twelve times a minute. The consequence is not a slow leak, it
  is the board stopping: a reader holds a WAL snapshot, the snapshot blocks
  checkpointing, the WAL grows past the database, and writers start timing
  out. Seen live as `drain failed: database is locked` every five seconds
  against a 6.59 MB WAL on a 4.58 MB database, with five renames sitting
  unposted behind it and a card stuck saying *Pushing to Discord…*.
  The hazard was already written down one function along -- "without it a
  request that raised part-way left its connection open, and on Windows that
  is a file handle nothing gives back" -- and `cards` and `stats` were fixed
  when it was found. The other seven were not, which is the ordinary way a
  rule kept by hand is kept: once. `tests/check_app.py` walks every function
  that opens a connection and fails on any that can return without closing.
- **A decorator belongs to the function under it, and `/health` proves how
  quietly that fails.** `wal_state()` was inserted directly beneath the
  `@app.get("/health")` that belonged to `health()` and took it, so for four
  releases -- 0.9.5 to 0.9.8, both installed laptops -- `/health` answered
  with three numbers about the write-ahead log and `health()` was a function
  nobody called. Nothing looked broken: the route existed, returned 200 and
  served valid JSON, just the wrong function's. Everything Bert reads off it
  went quiet at once with nothing to point at -- the update check, the
  shared-board indicator, the unsent-changes warning, the sync age, the client
  roster, the Jira link on a card. **Including the WAL strip that commit was
  adding**, because Bert reads `health["wal"]` and the reply had no such key,
  which is why the strip "had never been drawn".
  Found four releases later by asking why no card showed a Jira link.
  `tests/check_app.py` holds two invariants, and the second is the one that
  generalises: `/health` maps to `health`, and every key Bert reads off the
  answer is in it -- read out of `bert.py` rather than listed, so a key it
  starts using is covered without the check being edited.
- `if __name__ == "__main__":` stays at the very end of every file.
  `uvicorn.run()` blocks, so anything appended below it never registers.
  This has already caused a "route not found" bug once.
- Keep sync transactions short. `ernie_sync` commits per thread. Bert
  writes to the same database, and a long transaction causes
  `database is locked`.
- **The WAL is reported, because the failure is silent until the board
  stops.** A reader that never lets go pins the write-ahead log, SQLite
  cannot checkpoint it, it grows without bound, and writers begin timing out
  -- and nothing says a word until something people are waiting on stops
  arriving. Twice now: 6.59 MB against 4.58 MB, and **33 MB against
  4.68 MB**, the second time costing a Complete four and a half minutes to
  reach its thread and posting one change-log line seven times.
  `ernie_api.wal_state()` puts it in `/health` on every poll and
  `bert.wal_standing()` is the pure decision -- silent while the WAL is
  smaller than its database, amber past that, red past twice it, and silent
  again for an Ernie too old to send the field.
  **It also needs an absolute floor, and the ratio alone gave a false red.**
  SQLite checkpoints itself at `wal_autocheckpoint` pages -- 1000 of 4096, so
  **3.9 MB of WAL is the ordinary working set on a database of any size**. The
  premise behind a pure ratio, that a healthy WAL never approaches its
  database, is true of a 16 MB mirror and false of a small one: production's
  second laptop holds only what it needs, 1.1 MB, so its normal 4.0 MB WAL was
  four times the file and the strip said *changes may stop reaching Discord* on
  a stack with no lock errors, nothing queued and nothing stuck. `WAL_FLOOR_BYTES`
  is 4 MB and both tests must pass. Every real sighting still warns -- 6.59 on
  4.58, 33 on 4.68, 48 on 16 -- which is what `check_wal.py` pins, along with
  the floor being at or above what the pragmas actually say, read off a fresh
  database rather than restated.
  It surfaced the day the strip could first be drawn at all: `/health` never
  delivered the `wal` key until 0.9.9 gave the route back to `health()`, so
  "the strip has never been drawn" was never the alarm staying quiet. Restarting the stack clears
  it; a checkpoint with nothing running took 33 MB to nothing instantly.
  **What holds it is `ernie_status.pin_pending`, found 2026-09-17.** The
  earlier probes -- the API, the sync, the outbox and Bert-shaped polling,
  each tested with a writer attempting `wal_checkpoint(TRUNCATE)` -- all came
  back clean because none of them was ever *pinning*. The loop iterated
  `con.execute(...)` directly, which is a lazy cursor holding a read
  transaction open for the whole loop, and the loop does an HTTP PUT per row.
  That stale snapshot is the reader, and the refused upgrade to a write is
  `database is locked` on the outbox's connection -- permanently, because the
  failed upgrade leaves the transaction open and the outbox is one thread and
  one connection. Status, drain, state channel and change log, all of it,
  until the process restarts.
  `.fetchall()` is the fix and the word is load-bearing. Reproduced against a
  copy of production's mirror with a second connection committing underneath:
  raises on the first pass before, eight clean passes and a flat 2 MB WAL
  after, against 450 concurrent commits. `tests/check_status.py` holds it
  with a Discord whose `write()` commits on a second connection, so the race
  happens every time rather than sometimes.
  It was latent for the life of the project: the query only returns rows when
  something is waiting to be pinned, and production had three status messages,
  all pinned months earlier. `STATUS_BACKFILL` made three unpinned rows a
  pass, which is the first traffic that ever reached it -- **48 MB of WAL
  against a 16 MB database and the outbox stalled for ten minutes**.
  The two earlier sightings are consistent with the same cause and not proven
  to be it: the sandbox run where *all 29 pins came back 403* held that cursor
  open across 29 HTTP calls, which pins the WAL without ever reaching the
  UPDATE -- growth with no lock errors, which is what was seen. `tools/q.py`
  stays read-only regardless; that was a good change for its own reasons. Both sightings were reconstructed hours afterwards from what
  was left behind, which is why neither named a reader: `tools/wal_watch.py`
  is the part that was missing, holding one connection and passive-
  checkpointing on a beat so a log that will not copy back is caught while
  whatever is holding it is still running. Start it beside the stack the next
  time the strip comes up, not after.
- **Never hard-delete from the mirror.** Discord is mutable, so
  `thread_titles` and `message_revisions` are append-only and deletions set
  `deleted_at`. Bert's own state (`cards`, `events`) is never overwritten by
  a re-sync.
- Compare timestamps with `datetime()` on both sides in SQL. Python
  writes ISO8601 with a `T`; SQLite's `datetime('now')` uses a space. Raw
  string comparison is always false. This silently broke the outbox once.
- Secrets live in `ernie.env` / `ernie-test.env`, both gitignored. Never put
  a token in a `.py` file.
- `ernie_sync.PRODUCTION_GUILD` is declared once and everything guarding
  imports it. `wipe_test.py` deletes threads, `seed_test_server.py` creates
  them, `ernie_state.py --check` writes a preflight, and
  `tools/fake_stats_data.py` invents history -- each refuses production, and
  each refusal is one comparison against that constant.
  It was wrong for the life of the project, holding a guild id that is not
  this company's, so all four compared production against a server nobody has
  ever used and let it through; `wipe_test.py` still carried the comment "set
  this to your real guild". Found the only way a wrong guard ever is -- by one
  firing on the wrong side, when the fake-data tool wrote 257 invented tickets
  into production's mirror. Recovered in full from the 28 Aug backup, and
  nothing reached Discord because production has no `ALLOW_DISCORD_WRITES` to
  reach it with.
  `tests/check_guards.py` holds the copies together and holds each guard to
  still asking the question -- a comparison that was deleted looks exactly
  like one that returns False, and these files have no other test surface.
  What no check can hold is that the value is *right*: the only thing to
  compare it against is production's own env file, which is deliberately not
  in the repo. **Verifying it is a person's job**, it takes one line, and it
  has to be redone if the company's server ever changes:
  `PRODUCTION_GUILD` must equal `DISCORD_GUILD_ID` in `ernie.env`.
- `run.sh` checks the env names the guild the mode claims. Both its
  filenames are relative -- `prod` reads `ernie.env` and opens `ernie.db` --
  and there are two checkouts on this machine: the sandbox, and production in
  its own folder. A stray `ernie.env` in the sandbox checkout is all it takes
  for `prod` to load the *sandbox* guild and open whatever `ernie.db` is
  lying beside it. One was, and so was a 14.8 MB copy of production's mirror:
  `./run.sh prod` from the sandbox would have written its 34 threads into a
  file holding production's 889. Neither file was wrong alone -- the env was
  a byte-identical copy of `ernie-test.env`, the database a snapshot somebody
  took to look at -- it was the names that made `prod` find them.
  Both directions are refused, because "never test against production" had
  nothing enforcing it either. `tests/check_guards.py` holds it.
- **A guard is tested against a copy, never by pulling the trigger.** Copy
  the database, point the tool at the copy, watch it refuse. Running a
  writing tool at production to find out whether it would stop is how the
  above was discovered, and it is not the way to do it twice.
- `tools/fake_stats_data.py` asks the database whose board it is, not
  what the file is called. It refused anything named `ernie.db`, which was
  right while the only two databases were `ernie.db` and `ernie-test.db` --
  and the exe made the name worthless, because an installed copy keeps its
  database at `%LOCALAPPDATA%\Ernie\ernie.db` whatever server it points at.
  A database with no threads yet cannot answer, and is refused rather than
  guessed at.

## Data model notes

- Key everything on `thread_id`. Titles change (PROD ↔ OPS renames are
  normal), so nothing may be keyed on parsed title fields.
- Priority bands: `unassigned`, `critical`, `high`, `medium`, `low`. New
  threads land in `unassigned`; a human drags them out. Ties within a band
  are broken by a shared fractional `rank`.
- `rank` is the order, and it is the only one. A thread nobody can read
  (`ex.UNREADABLE_CONFIDENCE`) is ranked to the *top* of unassigned by
  `ensure_card`, not the bottom, because it needs a person soonest. Bert used
  to arrange that at draw time instead: the card sat first on screen and
  twelfth in the state channel, and a drop between two visible cards was
  measured against neighbours that were not its neighbours. Nothing may sort
  a band by anything but `rank` -- if a card belongs somewhere, give it the
  rank that puts it there. Dragging one down leaves it down; a later rename
  to something unreadable does not haul it back up.
- Work is a list, not a field: `work_items`, one row per bubble on the card.
  The card shows only what is left; the editor keeps the finished ones.
  `/cards` sends them all, flagged -- it used to filter on `done_at IS NULL`,
  so a ticked bubble was never sent at all and there was no way to reach it.
  The card filters them off its face, because a card is a list of what still
  needs doing. In the editor a finished bubble is unfilled with a dashed green
  border, and a double-click puts it back to outstanding: double rather
  than single, because a stray click must not undo finished work, and the
  second click is the confirmation -- a dialog would tax every tick to guard
  against the rare wrong one. It rides in the batched save as `work_undone`,
  so Cancel takes it back like anything else typed there, and `touched` counts
  it or the row is updated and the function returns before the commit. The ✕
  works on a finished bubble too: removing says it should not be on the card
  at all, which is as true of something ticked off as of something
  outstanding. Both greens are `T.OK_BG`/`T.OK_FG`, which the palette already
  had.
  Rows are never deleted — a tick in view mode sets `done_at`, an ✕ in the
  editor sets `removed_at`, and undo needs both rows still there. The editor
  sends `work_add` / `work_remove`, not the whole list, so two people adding
  different items merge instead of colliding.
- **Complete is the word for a work item; close is the word for a ticket.**
  One bubble and one tick against the whole ticket leaving the board: two
  acts on two different things, and they shared a word. The card's button
  says **Close thread**, which also says the part that is easy to miss: it
  archives the Discord thread, not just the card. The thread is told
  `X closed this thread in Bert` — the same sentence as the Discord closure
  with the place swapped. The longer label costs the footer 77px, which
  `_fit_foot` pays out of the age and a second issue chip; measured across
  600 renders from the column's 463px minimum upward, nothing lands off the
  card and nothing is dropped without landing in a tooltip.
- **A ticket with work still on it cannot be closed from Bert.** A card is a
  list of what is left, so closing one with bubbles on it says the ticket is
  finished while the card says it is not. `guard_work_done` refuses it and
  names what is in the way; Bert disables the button and puts the reason in
  its tooltip, which reads because the bubbles are drawn directly above it.
  Both ways out are one click in the editor — tick it off, or take it off
  the card with the ✕ — and a guard that counted every row rather than the
  open ones would make a ticket uncloseable for ever, which is what
  `tests/check_close_rule.py` holds.
  **It holds in Bert only, and that is not a gap.** Archiving a thread in
  Discord closes its card whatever the work items say, because Discord is
  the source of truth: 21 of production's closures arrived exactly that way,
  against **0** ever pressed in Bert with work outstanding. So the rule
  forbids something nobody has done, and it is "Bert will not let you"
  rather than "it cannot happen".
- A retired queue is parsed, never offered. `QUEUES` is every prefix a
  title may legitimately start with and drives `_PREFIX`, so nothing may be
  taken out of it because it fell out of use: the titles already in the mirror
  would stop matching, fall to `UNREADABLE_CONFIDENCE`, and be ranked to the
  *top* of unassigned as cards nobody can read. `QUEUES_OFFERED` is what Bert's
  editor offers, and `T.QUEUE` must hold exactly those -- the filter checkboxes
  are built by walking the palette, so a tag with no colour also has no filter.
  `tests/check_palette.py` holds the two together. DATA was retired 2026-09
  with one thread in the sandbox and one in production still carrying it; the
  editor keeps a retired tag on a card that already has it rather than
  silently clearing it on the next save.
- `cards.action_item`, `build_state`, `return_state` and `direction` are
  retired — work items replaced all four. Nothing shows or edits them; they
  stay only so undo can reach an old `edited` event, which is why `describe`
  and the undo path still understand a `set_*` verb.
  `POST /cards/{thread_id}/status` is gone, though: it was the one thing
  that could still write those four, and an unauthenticated POST that mutates
  retired columns and logs events the feed cannot render is not worth
  carrying. Nothing had called it — Bert never referenced it, and neither
  database holds a single `set_*` event.
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
- **Unarchiving a thread reopens its card, and the guard asks what *caused*
  the unarchive.** A bot posting into an archived thread unarchives it as a
  side effect -- a keepalive ping, Ernie's own correction going back into a
  thread it closed -- and neither is a person reopening the work. The test
  was "is the newest message a bot", and Ernie posts "closed this thread in
  Bert" and **then** archives, so its own message is the newest one in every
  thread it has ever closed. `thread_reopened` was therefore unreachable for
  all of them: **zero events in five months** across both boards, which read
  as nobody ever reopening a ticket. Found when a thread closed in Bert and
  reopened from Discord's thread menu left the card closed with nothing
  said. What makes a bot message the cause is arriving **since the last
  sync of that thread**; one already sitting there while it was archived
  explains nothing. `tests/check_reopen.py` holds the two apart on timing
  alone.
- A thread archived in Discord closes its card, and Ernie has to go looking
  for it: an archived thread is not in the active listing, so
  `reconcile_closures()` asks Discord about each open card that has gone
  missing and closes only what comes back `archived: true`. Absence is the
  question, never the answer -- a listing short for any other reason would
  otherwise close the whole board in one pass.
- **A thread Ernie archived itself is not evidence of anything**, and
  `threads.archived_by_ernie` is what says which is which. The case is undo:
  Close in Bert completes the card and the outbox archives the thread, and
  undoing afterwards clears `completed_at` while the thread stays archived
  until the correction posts into it. In that window an open card sits on a
  missing thread, which is exactly the shape of a human closure -- so
  `reconcile_closures` closed it again, as a *Discord* closure, attributed to
  nobody and stamped with Ernie's own archive time. Silent until closures
  were announced, so it read as a card refusing to come back from an undo.
  The flag is cleared when the outbox unarchives to post, or the skip becomes
  a blind spot and a real closure never reaches the board.
- The thread is told who closed it **and who reopened it**, and only one
  machine may tell it. `ANNOUNCE_THREAD_CHANGES` gates both, because both are
  things that happened to the thread in Discord and every stack sees them.
  It was `ANNOUNCE_CLOSURES` until 2026-09-17, which named half of what it
  does: a switch whose name describes half its job is one somebody sets for
  the half they read. Renamed while the only two machines carrying it were
  ours -- an installed env is not overwritten by a reinstall, so the old name
  is still read and warns once rather than being dropped, which would have
  made a rename into both boards silently going quiet about closures.
  It is off unless set, for the reason
  `CHANGELOG_CHANNEL_ID` is: every stack notices the same archived thread and
  writes its own closure row, so two switched on tell the thread twice. A
  pass finding more than `ANNOUNCE_MAX` closed at once records them silently
  -- that is a machine catching up rather than one watching, and every
  announcement unarchives the thread, posts and re-archives it. What is held
  back is never the closure.
- `messages.type` is Discord's and is stored because it cannot be derived.
  Type 4 is a rename, which is the only exact record of a retag there is.

`messages.type`, the closure machinery and the audit-log attribution are in
`docs/discord.md`.

Two things wear the name "needs attention" and they are different sets: the
`unassigned` band, which is every card nobody has triaged yet, and the red
edge, which is `needs_triage()` and is about the title being unreadable. A
card keeps the red edge wherever it is dragged, so the two sets overlap
rather than nest -- on 2026-09-16, 23 in the band and 3 red, two of those
three sitting in Medium. `plans/needs-attention-jump.md` turns on the
distinction, and so does the jump built from it: **clicking the toolbar's
attention count goes to the next card whose title cannot be read**, wrapping
past the last. It hangs off that count rather than a band header for exactly
this reason -- two of those three were in Medium, so a control on the Needs
Attention header would have taught the ambiguity to everybody who used it.
`needs_attention()` orders `needs_triage()`'s set by band then rank and
`next_attention()` wraps; it cycles only what the filters leave on screen and
says `2 of 3` when that is not all of them, and does nothing while an editor
is open, because scrolling away from a half-typed ticket is what the poll
parks its payload to avoid. `tests/check_attention.py`.

## Writes and undo

Every change writes one row to `events`. That single table backs the
activity feed, undo, and the outbox.

Bert's layout used to live in this section too, which is how a heading about
posting to Discord came to own splitter handles and feed row heights. It is
in `docs/bert-ui.md` now.

- `dispatch_after` = when Ernie may post. `NULL` means never post.
- Priority moves are silent except in or out of `critical`, which posts. Every
  other band change, and every reorder within a band, is board housekeeping:
  posting each nudge between high and medium is noise in a customer thread.
- A `reordered` event carries the band and the card's position in it,
  before and after, as `band:position` in `old_value` / `new_value` --
  `high:5` -> `high:3`, which reads as "High 5th -> 3rd". Not the rank: that is
  a fraction and "1000 -> 1500" tells a reader only that something moved. Both
  values carry the band, though a reorder never leaves one, so either is
  readable on its own; `ex.reorder_spot()` parses them and returns a bare
  position for the rows written before the band was recorded. The
  positions have to be worked out where the move happens, against the ranks it
  is ordered among, because afterwards those ranks have moved on and it is no
  longer derivable. Rows written before this carry nothing and are shown
  without the places rather than being given invented ones. A drag that lands
  a card back where it started writes no event at all -- the rank is still
  saved so both boards agree, but four identical lines for a card that never
  went anywhere is the feed reporting the dragging rather than the outcome.
- Undo inside the window deletes nothing from Discord because nothing was
  ever sent. Undo after it posts a correction message instead.
- A rename is undoable, and Bert spent a long time not saying so. `undo`
  has handled one all along -- inside the window it cancels the event, which
  is the whole job because nothing left the machine, and after it queues a
  rename back -- but `renamed` was missing from the tuple in `_render_feed`
  that decides which verbs get a button. So a title change was the single
  thing on the feed that could not be taken back, and the two ends disagreed
  in silence: the server could do something the client never offered.
  Reported as there being no Undo beside a change still sending, which is
  exactly the moment it is free. `tests/check_feed.py` now reads the verbs
  off both ends, because one of them being right was never the problem.
  The tooltip says what it costs on the far side of the window, and for
  a rename that is more than the rest: putting a title back is another real
  rename, at two per ten minutes on a budget shared between both machines.
  The other verbs say "undoing posts a correction"; this one says which
  allowance it spends.
- Edits are batched: saving four fields writes one event and posts one
  message. Do not split this into per-field events. The feed still says which
  it was: `old_value` is the previous value of everything that moved plus a
  `__work__` entry naming the bubbles added and removed, so Bert reads the
  shape off it -- bubbles only, fields only, or both -- and `new_value` is the
  prose it shows. A row whose `old_value` won't parse falls back to "edited"
  rather than being dropped.
- A feed row opens only when clipping actually hid something. The line is
  rendered twice, clipped and whole, and the two being different is the test --
  no layout measuring, and no affordance on the rows that already say
  everything. Which rows are open is held on the window by `event_id`, not on
  the widgets: `_render_feed` throws every row away and builds it again on each
  poll, so a row opened to read would shut again within five seconds.

- A retry must not do again what already reached Discord. Posting one
  event takes up to four writes -- unarchive, rename, message, archive -- and
  creating a ticket takes three: the thread, a note saying who started it,
  then the opening message. Only the last was ever recorded, so a failure
  anywhere threw away the record of everything before it and the retry began
  at the top. Reported as changes showing up in Discord while the card went
  on saying *Pushing to Discord…*, which is exactly what it looks like from
  the board.
  Two of those writes are harmless twice and two are not. Archiving a thread
  that is already archived is the same as archiving it once. Renaming is 2
  per 10 minutes on a shared budget and posts a system message every time;
  posting a message is a message; and opening a thread is a whole second
  ticket. Measured before the fix: an archive that failed twice put three
  identical "marked this complete" messages into one customer thread, and an
  opening message that failed twice left three real threads in the
  customer channel for one ticket -- the board keeping the third, and the sync
  free to pick the other two up later as fresh unassigned cards.
  `events.sent_steps` and `new_threads.sent_steps` are the record, and
  `note_step()` commits between the writes, which is the whole of it: a
  pass that kept its steps in memory and wrote them at the end would lose them
  in precisely the case they exist for. `new_threads.thread_id` is written the
  instant the thread exists, so no retry can open a second one, and the card's
  rows go in before the two messages rather than after -- a message that
  fails should not keep a ticket somebody just started off the board. Those
  local writes are guarded on the card not existing yet, because the work
  items are a plain INSERT with a fresh uuid each and a second pass would give
  the ticket every bubble twice. `posted_at` keeps the stored
  `discord_message_id` through a `COALESCE`, or a retry that skipped the post
  would wipe the id undo replies to.
- The one way the mark can stick with nothing coming is a card too long to
  publish. `publish()` skips a card whose state message renders over
  Discord's 2000 characters, and nothing shortens it on a later pass -- so its
  `synced_at` never advances and it says *Pushing to Discord…* for ever. It
  goes to stderr with the other alarms and into the counts, rather than
  into stdout with the routine chatter, because the fix is a person removing
  work items. Not reachable on either board today: the longest card renders
  724 of 2000.
- Writes take an idempotency `key`; retries return the original result.
- The read path rode out Discord's 5xx and the write path did not.
  `Discord.get()` has always retried a 500 with a backoff, because a repeated
  read costs nothing; `write()` handled 429 and then raised, so a `503` came
  straight out. Found the only way it could be -- on a run of six thousand
  consecutive writes cloning production's threads into the sandbox, where
  Discord served two 503s in one afternoon and killed the run twice. Nothing
  in 1,877 assertions could have caught it, because the sandbox's whole board
  was thirty-four tidy threads and you never do enough writes in a row to
  meet a bad afternoon.
  It is opt-in, and the default is the safety. A 5xx does not say whether
  the request was processed, so retrying a POST can post twice -- which is
  exactly what `events.sent_steps` exists to prevent, and what once put three
  identical "marked this complete" messages into one customer thread. So the
  caller decides, and only where repeating is genuinely a no-op: archiving
  an archived thread, editing a message to the text it already holds, pinning
  a pinned message. Six call sites. Never a message post, never opening a
  thread, and never a rename -- two per ten minutes on a budget shared
  between both machines, with a system message each time, so a silent retry
  spends somebody else's allowance.
  What it was costing quietly: the outbox writes through the same method,
  so every transient 503 burned one of `MAX_ATTEMPTS`, and a handful spread
  over a day could strand a change for good. It surfaced as `stuck` in
  `/health`, which reads as "something is wrong with this card" rather than
  "Discord was briefly unwell."
  `tests/check_outbox_retry.py` holds the rule by walking every `write()`
  call in the tree: a POST carrying `retry_5xx` fails the check, and so does
  a rename.
- One editor at a time, and the second click offers to finish the first.

**Slate is hue 195, and it is the one that found the gamut limit.** It sits in
the 133-degree gap between OPS at 133 and Medium at 266, the widest empty
stretch on the wheel, and its worst tag is 10.8. But blue and wine take chroma
21 at L* 8 and round-trip with their lightness intact, while **teal at that
chroma clips** -- and a clipped channel moves L*, 1.55 of it, which took the
worst ratio drift from 0.05 to 0.433 and broke the very thing holding L* was
there to protect. So each of its tokens carries the most chroma sRGB will hold
at its own lightness, found by walking chroma *down* until the value
round-trips. Down rather than by halving: 8-bit rounding makes the fit
non-monotonic, and a binary search over it returned `chip_bg` at chroma 1.9
between neighbours at 9 and 15 -- a grey chip in a teal room. **Check the L*
drift on any new hue before trusting the ratios**, because a palette that
clips is one where holding lightness has quietly stopped holding anything.
  `editor_is_busy()` used to say no and stop, leaving somebody to find the
  other card themselves. It now offers Save / Discard / Keep editing, with
  *keep editing* as the default because it is the one that loses nothing. An
  editor with nothing typed in it is closed without asking: `Card.is_dirty()`
  compares exactly what `save()` would send against `_edit_base`, so clicking
  Edit on the wrong ticket and moving on costs no dialog. Every button names
  both tickets -- "this ticket" is the one phrase that cannot be used here,
  because the ticket being closed is not the one just clicked.
- Nothing rebuilds a band while an editor is open, whatever asked. The
  poll parks its payload for this reason, and always has -- but `render()` is
  reached from places no poll goes: a window resize goes through the same
  timer the rail handle uses, and the end of a drag calls it outright. Those
  rebuilt the bands regardless, so maximising the window while writing a new
  ticket destroyed it. For a card that exists it lost whatever had been
  typed, the widget being replaced from the data behind it; for a ticket being
  started it was worse, because the placeholder is not in `self.cards` -- so
  nothing rebuilt it at all and `editing_card` was left naming a widget that
  no longer existed, which holds every later poll and freezes the board with
  nothing on screen to say why. Only the bands are spared, because that is
  where an editor lives: the rail holds none and goes on re-clipping to the
  new width, which is the whole point of the resize. The band signatures are
  deliberately left un-updated, and `_bands_stale` carries the missed rebuild
  so `apply_pending()` draws it the moment the editor closes -- a parked poll
  redraws by its own route, but a resize parks nothing, and without the flag
  the board kept a layout for a window that was gone until whichever poll came
  next.
- A card closed while an editor is open still leaves the board. The poll
  parks its payload whenever `editing_card` is set, because rebuilding a band
  destroys the widget somebody is typing into -- so a ticket completed
  meanwhile was shut on the server with nothing on screen to say so. It sat
  there looking untouched, pressing Complete again did nothing because it was
  already closed, and it only went when the editor did. Reported as not being
  able to complete a ticket while another is being edited, which is exactly
  what it looks like from the board.
  `_hide_closed_card` hides it rather than rebuilding: the card being closed
  is never the one being edited -- that one is in edit mode and has no
  Complete button -- so nothing anybody is typing into is touched, and the
  next real render replaces the lot anyway, because the band's signature
  already differs once the payload stops carrying that card. The band's count
  and the matching rail row go with it: the running order is the same
  board said twice, and 33 on one against 34 on the other is the kind of
  disagreement that makes somebody stop trusting both. `isHidden` rather than
  `isVisible` for the recount -- a folded band's cards are all invisible and
  none of them are closed.
- Bert's close warning owes three debts, and the third is a different
  kind. The two below are about Discord, and neither is lost by closing --
  the outbox posts them whether Bert is open or not, which is why that warning
  is about shutting the *stack* down. An editor nobody has saved is the
  opposite: gone the moment the window shuts, the only one of the three that
  is lost with the stack already down, and for a long time the only one not
  asked about. `_editor_may_close()` asks it before `connected` is read,
  for that reason, and offers the same three-way the second click on Edit
  does, with *keep editing* as the default.
  `save_edits()` and `create_ticket()` answer `True`/`False` for themselves,
  because the editor being shut says nothing about whether a write landed.
- A write that did not land keeps what was typed. `Card.save()` used to
  put the card back in view mode *before* the write, so a failure was followed
  by `refresh()` redrawing the card from server data: everything typed was
  gone, and the error box explaining the failure sat on top of work already
  discarded. Worse for a ticket being started, where the placeholder is all
  there is -- the request failed and took the whole ticket with it. The write
  goes first now and the editor closes only once there is nothing left to
  keep, which is also why `create_ticket()` drops the placeholder in the
  `try`'s `else`. A failed save therefore leaves an editor open, so
  `editor_is_busy` returns `not w.save()`: opening the other card on top of it
  would put two editors on screen, which is the thing that guard exists to
  stop. `_edit_conflict()` answers what it *settled* rather than what it
  wrote -- "keep theirs" and "discard my changes" close the editor, because
  the person said so; a retry that fails, or a dialog closed without
  answering, leaves the typing where it is.
- Bert's close warning owes two different debts, and must count both.
  `queued` is events waiting out their undo window before Ernie posts them to
  the customer thread; `sharing.waiting_to_send` is cards that have moved since
  the shared board was last published. A reorder, and every band move that is
  not in or out of `critical`, is silent -- no `dispatch_after` at all -- so it
  appears only in the second. Counting the first alone meant reordering the
  board and closing straight away asked nothing, and the running order never
  left the machine. `Bert._owed()` reads both.
- The unsent mark says what it means, in words. It was `*` and `!`, on
  the reasoning that the glyph is what tells the two states apart and it
  spends no colour. Both halves were true and neither made `*` mean anything:
  an asterisk in the corner of a card is a footnote mark with nothing to point
  at, and the sentence explaining it lived in a tooltip nobody hovers on a card
  they are not already asking about. It is Pushing to Discord… and Not
  sent now, drawn as chips -- the card already wears chips for the tag, the
  PIP count and "edited", so it is the shape the eye is reading there anyway.
  One sentence covers all three waiting cases, because `#ernie-state` is a
  Discord channel too: a card waiting only on the shared board is still waiting
  on Discord, and which of the three it is stays in the tooltip. The given-up
  one must never say *pushing* -- nothing is being pushed, and telling somebody
  to wait for something that is not coming is the whole reason `/health`
  reports `stuck` apart from `queued`.
- And it is drawn in the ink, not the accent. Measured against every card
  fill in both palettes: the accent averages 4.6:1 in light and 5.9:1 in dark,
  the ink 13.7:1 and 11.8:1. It is also the cheaper choice -- a card already
  wears its tag's colour, and a mark spending none leaves colour meaning
  something, which is the same rule the band bars in the running order follow.
  Amber is kept for the given-up one alone, because a caution is the one thing
  on the card that is genuinely a warning.
- An outage is not the card's fault, and `attempts` must not say it is.
  A connection failure used to increment it, so five passes against an
  unreachable Discord abandoned the change for good and `/health` reported
  `stuck` -- which reads as something wrong with *that ticket* rather than
  with Discord. The drain beat made it sharp: five passes at 30 seconds
  was two and a half minutes, and at `FAST_SECONDS` it is twenty-five
  seconds of Discord being unreachable to strand every queued change on the
  board. The beat split is what turned a hazard into one worth fixing.
  Giving up is a statement about the *change* -- a message Discord will not
  accept, a thread that no longer exists -- and an outage says nothing about
  any particular row and the same thing about all of them. So an
  `httpx.TransportError`, which is exactly "no HTTP response happened",
  releases the claim and records `last_error` without counting: the row stays
  due and goes the moment Discord answers. Anything that *got* an answer
  still counts, a 4xx included, because Discord replied about this request.
  Measured against a copy with a client pointed at a dead port -- the way a
  guard is tested here, never by waiting for the real thing. Eight passes,
  `attempts` still 0, still due, nothing stuck; then a 200 and it posted.
- `/health` counts as owed only what the outbox will still try.
  `OUTBOX_MAX_ATTEMPTS` matches `ernie_outbox.MAX_ATTEMPTS` and the
  `attempts < 5` in `v_outbox_due`; without it a row nothing would ever pick up
  again was reported as pending for ever, so Bert warned about unsent changes
  on a board nobody had touched for ten minutes -- and the warning's advice,
  leave the stack running another minute, was the one thing that could not
  help. Given up is not hidden, it is reported separately as `stuck`.
- A thread opening in Discord writes a `started` event, so the feed can
  say where a card came from -- the one thing on it that nobody did in Bert.
  `dispatch_after` is NULL, because it happened in Discord already, and undo
  refuses it in both Bert and the API: there is nothing here to take back.
  The name is `threads.owner_id` resolved against `messages`, preferring
  Discord's `global_name` ("Tyler") to the username ("tyler_mazza"). A thread
  a bot opened gets no line -- the seeder makes two dozen at a time. Neither
  does one Ernie merely inherited: only a thread created within
  `WITNESSED_WITHIN_S` of being first seen counts, because a first sync makes
  a card for every thread there has ever been, and claiming to have watched
  those start would write a line per thread into the feed and the change log.

## Starting a ticket from the board

The editor's own behaviour -- the one-editor rule's dialogs, what they say,
and the folded-band trap -- is in `docs/bert-ui.md`.

A `+ New Ticket` on every band header, because the band is the answer to
"where does this go" and pressing the one you mean has already given it. Needs
Attention keeps one too: every thread opened in Discord lands there anyway, so
a ticket with no home yet is the ordinary case rather than an exception.

- A card cannot exist without a thread, and Bert cannot make one -- every
  write to Discord goes through `Discord.write()`, which lives in the outbox.
  So `POST /tickets` records what to make in `new_threads`, and the board
  shows it wearing the unsent mark until the outbox has made it. A ticket
  that exists here and not yet in Discord is exactly a change that has not
  left this machine, which is what that mark already says.
- The outbox writes the mirror rows itself rather than leaving them to the
  sync. Waiting would put the card on the board a cycle later and in
  `unassigned`, losing the band somebody chose by pressing the `+` in it. The
  sync reconciles both on its next pass; they are written the way it writes
  them.
- The outbox writes the card the way the sync would, or the board shows two
  different tickets. The rows it writes are read straight back by the API, so
  a shortcut there is visible immediately. It wrote the title row with the name
  alone and `confidence='pending'`, leaving queue and client NULL: a title that
  parses perfectly well -- `PROD: CHA Solutions - 08Sep26 - what it's about` --
  came up grey with "unknown client" the moment its thread existed, and stayed
  that way, because the sync writes a revision only when the *name* changes and
  the name never did. `load.record_title()` is now the one writer both go
  through. And it ranked the card to `MAX + RANK_STEP`, the bottom of the band,
  while Bert had been showing it at the top since the `+` was pressed -- rank is
  the order and the only one, so it has to say what the board says. It goes to
  `MIN - RANK_STEP` of the band whose `+` was pressed, the same idiom
  `ensure_card` uses to rank an unreadable thread to the top.
- A ticket can be closed before Discord has it. Completing looks up a
  `cards` row and a ticket still waiting in `new_threads` has none, so it
  answered `no such card` -- true, and useless: the person meant to close it,
  and the wait for the thread is Ernie's problem rather than theirs.
  `complete_on_arrival` records the intent, the card leaves the board the
  moment the button is pressed, and `make_threads` closes it as soon as the
  thread exists -- writing the `completed` event with a `dispatch_after`, so
  the thread says so the same way every other closure does and it becomes
  undoable from the feed at the first moment there is anything to undo. Until
  then the API answers `event_id: None`, because there is nothing in a thread
  to take back.
  Closing a card never cuts off what it still owes. `v_outbox_due` filters
  on dispatch, posted, undone, claimed and attempts, and never looks at
  `completed_at` -- measured, an edit queued behind its undo window is still
  due to post after the card is closed. So a change made seconds before
  closing still goes out.
- A ticket can be dragged before Discord has it, and it is the same
  sentence as closing one. A move looks up a `cards` row, a draft has none, and
  "no such card" is true and useless: the person moved it, the board had
  already drawn it in the new place, and the wait for the thread is Ernie's
  problem rather than theirs. The band and the rank are written to
  `new_threads` and `make_threads` brings the card in where it was left --
  which is why it takes `new_threads.rank` rather than recomputing the band's
  edge, or a ticket dragged down into Medium would arrive back at the top of
  it. Nothing is logged. The ticket does not exist, so there is no history
  to record and nothing in a thread to announce; dragging a draft into
  Critical is the same act as pressing the `+` in Critical, which writes no
  event either.
- A draft carries a real rank, because rank is the order and the only one.
  It went to the board at `0.0`, which is not a place -- ranks can be
  negative, and `ensure_card` puts an unreadable thread at `MIN - RANK_STEP`,
  so a ticket the board promised to put at the top of High could sort below
  everything in it. It is ranked when the `+` is pressed, against the band's
  cards and its other drafts, or two tickets started in one band in a row
  are given the same number and tie.
- A draft is a neighbour like any other. The board draws it among the
  cards, so a drop can land against one -- and `move_card` read the band out of
  `cards` alone, so the neighbour Bert named was not in the list, the midpoint
  fell through to "end of band", and the card went somewhere nobody aimed at.
  `band_order()` is the one place both tables are read as one order, and
  `band_top()` and the respacing go through it too.
- Ernie opens the thread and then says whose it is. There is no map from a
  Bert install to a Discord account, so the bot is the author and the name
  from `settings` goes in as plain text -- the same way every other name this
  posts does. The optional first message follows it.

- A ticket with no thread has not left the board. Every poll checks that
  the card under an open editor is still in the payload and warns if it has
  gone -- and `NEW_TICKET` is never in the payload, because the thread does not
  exist yet. So pressing `+ New Ticket` raised "this ticket has left the board
  ... saving will probably fail" within a poll, over a blank form, and again on
  every poll after it. `_flag_edited_underneath` exempts the sentinel and
  nothing else: a real card that has genuinely gone still says so, which is
  the half of it worth keeping.

## The status message in the thread

The board knows what a ticket still needs and the thread is where the work is
discussed, and the two only met by somebody opening Bert. `ernie_status.py`
puts what is left where the conversation is: band, what is still to do, what
has been done, when it last moved and who moved it.

- **New threads only by default, and a backfill is bounded and opt-in.**
  `witnessed_start()` is the gate: a first sync makes a card for every thread
  there has ever been, and posting into all of those is not a thing to do to
  a channel people are working in.
  The rule was right about the hazard and wrong about the number. It was
  decided against production's 889 inherited threads -- but an archived thread
  is skipped anyway, separately, so the set a backfill actually speaks into is
  the *open* ones. On 2026-09-17 that was 37, against a board carrying a
  status on **2 cards of 39**: the feature was doing its job on 5% of the
  live board, and it does not self-correct, because it only improves as old
  tickets close.
  **It needs 0.9.7.** Switched on in production on 2026-09-17 against 0.9.6
  and it stalled the outbox within three passes -- not through anything the
  backfill does, but because three unpinned rows a pass was the first traffic
  ever to reach the lazy cursor in `pin_pending`. The WAL rule above has the
  whole of it. Do not switch it on against a build without that fix.
  `STATUS_BACKFILL` switches it on and `BACKFILL_MAX` is 3 a pass, the same
  shape as `ANNOUNCE_MAX`, `PUBLISH_MAX`, `CLOSURE_CHECKS` and
  `RESCAN_PER_CYCLE`. The cost being budgeted is not API calls, it is 37
  notifications arriving in everybody's sidebar at once; simulated against a
  copy of production's mirror it covers all 37 in 13 passes, about 13 minutes.
  The budget is spent only on threads being spoken into for the first time --
  one that already has a status is maintained like any other, or the budget
  would be filled every pass by work already done and never reach the rest --
  and the counts carry `left` so three of thirty-seven does not read as a pass
  that finished.
  Unlike `CHANGELOG_CHANNEL_ID` and `ANNOUNCE_THREAD_CHANGES` it is safe on more
  than one machine, because `adopt()` looks in the thread before posting.
  Nothing filters by title prefix, `ENG:` included: titles change, and nothing
  may be keyed on parsed title fields.
- An archived thread is skipped. Discord refuses a post to one, and
  unarchiving to say "closed" would drag a finished ticket back into
  everybody's sidebar.
- One message, edited in place, and rewritten only when the rendering would
  differ. A quiet board must not edit every message every cycle.
- Pinning needs the bot's own **Pin Messages** permission, not Manage
  Messages. It is never fatal, and `thread_status.pinned` records the ones
  still outstanding so a later pass tries again.
- A board with no row looks in the thread before it posts, or N boards mean N
  embeds in one customer thread.

Why it is shaped this way, and what it carries: `docs/discord.md`.

## The figures beside the board

`Stats` is the panel on the right, and `/stats` is what fills it. The board
says what is on the plate now; none of it says whether that is getting better
or worse, how long a ticket takes, or which ones have been open since April.

- They ride the slow lane, `STATS_MAX_AGE_S`, because they move when a ticket
  closes rather than every five seconds. An Ernie too old to serve `/stats`
  answers `None` and the board carries on.
- Nothing is derived from `events`. It is what this machine happened to
  witness, and 325 of production's 350 completions still read `imported`, so
  a per-person figure would credit one fake name with most of the work.
  Everything comes off `cards` and `threads`, which are true on any board.
- Every row has to add up to its own total, which is what `Other` exists for.
  A figure that does not add up is the first one somebody stops believing.

The figures themselves, the windows, and the panel's layout: `docs/bert-ui.md`.

## The state channel

So two people on two machines share one board without either hosting the
other's API. Priority, rank, work items and completion live in
`#ernie-state` (`STATE_CHANNEL_ID`), one message per card, edited in place.

- One message per card, not one document, so two people moving different
  cards never collide and no card can outgrow the 2000-character cap.
- **The writes are paced, and two boards share one bucket.** `Discord.write`
  sleeps `PACING`, 0.1s, which is tuned for GETs against the 50/s global
  ceiling; editing messages in one channel is about **5 per 5s**, so a capped
  pass fired ten edits in a second and was rate limited from the sixth. With
  one board that was survivable and invisible -- `write()` rides out a 429
  asking 30s or less, so the pass just took longer, and the sandbox's 40 edits
  in **4m46s** were read as Discord being slow rather than as this. With two
  boards it breaks outright: they share the bot, so they share the bucket, the
  backoffs escalate past `RETRY_MAX_S`, `write()` raises and the publish dies
  -- every pass, on both machines, so neither board's changes reach the other.
  The rename budget already documented exactly this property (`scope: shared`,
  so a second Ernie gets no allowance of its own) and nobody applied it here.
  `WRITE_PACE` is 2.2s between card writes and `PUBLISH_MAX` is 6, so a pass
  is about thirteen seconds and two machines together put roughly one write a
  second into the channel. **1.1s and ten was still too fast**, which only
  showed once both boards were live and busy: one drag changes the position of
  dozens of cards, so every pass wrote a full budget from both machines at
  once and the 429s came back -- `edited 10, 17 still to go` with a publish
  failing either side of it. Found the day two stacks first shared a
  production channel, when a card moved on one board took six minutes to reach
  the other, and resized the same afternoon when the first fix proved to be
  half of one.
- **A publish pass is bounded, `PUBLISH_MAX` card messages at a time.** A
  card message carries its position in the band and `positions()` is computed
  across the whole board, so closing one ticket shifts everything below it
  and genuinely changes the prose of dozens of messages -- that part is the
  design working. Writing all of them in one pass is not: measured on a
  49-card sandbox, **40 edits and 4m46s** at Discord's rate limit, and the
  outbox is one thread, so `drain()` waited behind the lot and a Complete
  took four minutes to reach its thread. Every other per-item job here
  already has a budget -- `CLOSURE_CHECKS` 20, `RESCAN_PER_CYCLE` 12, the
  change log's `BATCH` -- and this had none. Cards are taken in board order
  and a written one is unchanged next pass, so the budget walks down the
  board rather than starving the bottom of it, and the counts carry `left`
  so a pass that wrote ten of forty does not read as one that finished.
- Conflicts resolve three-way against `state_sync`, never by comparing the
  two machines' clocks. Both moved means the channel wins, being the shared
  copy, and the losing change is named in the feed rather than vanishing.
- **Resolved per field, because the fields are independent decisions and the
  card is not.** Judging the whole payload at once made **an undo lose to a
  rank respace**. Production, 2026-09-18, the first day two boards shared one
  channel: Chris moved a card out of Needs Attention, Bella's machine applied
  it, she undid it -- and the next pull put it straight back and wrote a second
  identical event, leaving one undone and one standing with the card at his
  value. Her undo moved `priority`; his board had republished the card with a
  different `rank`, which is housekeeping and nobody's decision. Whole-payload
  comparison read that as "both moved".
  `FIELDS` is `priority`, `rank`, `completed`, `work` and `resolve()` walks
  them one at a time, so in that case hers stands for priority and his is
  taken for rank -- both changes survive, which is what a base is for.
  `apply_card` is handed the winning value per field and already compares every
  field against the row before writing it, so a field we won is left alone.
  **The base stays what the channel holds**, not what was resolved to: a field
  we won is not up there yet, and recording the resolved value would make our
  own change look settled and it would never publish.
- **A discarded change is said out loud, in both places.** The docstring
  promised the feed would name it and nothing did -- not the feed, not the log.
  `note_discarded()` writes an `overruled` event carrying what was lost, with
  `dispatch_after` NULL like everything else applied from the channel, and undo
  refuses it: the note is not the change, and undoing it would not bring the
  change back while the channel still holds theirs. `pull_state` prints
  conflicts to stderr with the other alarms, which it never did -- the line
  fired only on `applied` and `unknown`, so a conflict-resolved pull printed
  **nothing at all**. That is why one undo reverted twice showed a single
  `applied 1` in the log, and why the cause took a database query rather than a
  glance. `tests/check_state.py` holds all of it.
- Applying a remote change writes an event with `dispatch_after` NULL. The
  machine that made it already queued its own message; a dispatch here would
  post it twice.
- The pull and the push live in different processes, because `ernie_sync` is
  read-only against Discord and `ernie_outbox` is the only thing that writes.

The conflict cases, the summary message and the skew reporting:
`docs/discord.md`.

## The change log

A durable record of every change, in its own channel, for looking back at
rather than reading as it goes. Customer threads only hear the handful of
changes worth interrupting somebody for; this gets all of them.

- Inert unless `CHANGELOG_CHANNEL_ID` is set, and only one machine should set
  it: both boards hold the whole history, so two loggers write every line
  twice. It is uncommented in the installed env and left commented in the
  copy that gets handed out.
- The channel is `#change-log` in the sandbox and `#ernie-logs` in
  production. The name is nowhere in the code; the env carries an id.
- Switch it on before the writes. `catch_up()` marks everything not yet sent
  as logged on the first run, so whatever has piled up by then is swallowed
  rather than posted.
- Nothing is logged until it has settled, because an event inside its undo
  window may still be cancelled.
- **Paced like the state channel, and for the same reason.** `BATCH` is 20
  lines a pass into one channel, which at `PACING` alone is 20 writes in two
  seconds against a budget of about 5 per 5s. It never bit, because a quiet
  board logs one or two lines a pass and `drain()` breaks out of the loop on a
  failed write rather than hammering -- so it degraded into slowness and a
  retry next pass rather than into the state channel's dead publish. Found by
  looking for the same shape elsewhere the day the state channel's was fixed,
  which is the only reason it was found before a busy day did it.
  `ernie_state.WRITE_PACE` is the one value, imported rather than copied:
  it is a statement about a Discord channel, not about either feature.
- **A line is claimed before it is posted, never after.** The order was post,
  then record, then commit, under a comment saying "a failure halfway repeats
  nothing" -- true of a failure *between* lines and false of one inside a
  line. With the database locked, `mark()` raised after the message had
  already reached Discord, so `pending()` handed the same event back every
  pass: **one completion posted seven times** into a channel whose whole job
  is to be a durable record. `claim()` writes the row with a NULL
  `message_id` first, which is enough to take the event out of `pending()`;
  `release()` gives it back if the post fails, so the fix for duplicates does
  not quietly become lost lines. `retracted()` already required
  `message_id IS NOT NULL`, so a claimed-but-unrecorded line is skipped by
  the strike-through path, and `unresolved()` reports it rather than retrying
  -- retrying is what caused the duplicates. This is `post_one`'s claim,
  write, record, one channel along.

The one-machine rule is liftable and `plans/changelog-per-machine.md` says
how. The rest: `docs/discord.md`.

## The customer list

Client names were typed into thread titles by hand, and the board grew 120
distinct spellings of 43 customers -- five ways of writing Inspect.AI, two of
RavanAir, one title where the apostrophe in Duke's arrived as a replacement
character. `ernie_jira.py` pulls the real list off the Client CR issues in Jira
so the name is *picked* in Bert instead of typed.

- Inert unless `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_TOKEN` and
  `JIRA_CLIENT_JQL` are all set. Read-only against Jira: the only POST is the
  search endpoint, which is a read carrying a body.
- Picking a client writes the thread title, never `cards.client_override`.
  Writing one would clear the red triage edge off every ticket anyone had
  merely opened.
- Searching may be fuzzy; resolving may not. `reconcile_aliases` refuses to
  merge on resemblance, because `falmouth ma` and `falmouth me` are 0.91
  similar and are different places.
- A client the query stops returning is retired, not deleted, and a pull that
  returns nothing retires nobody.

The roster, the alias tiers and the typo correction: `docs/clients.md`.

## The version number

`ernie_version.VERSION` is the one, and everything that names a version
imports it. Bump it there and nowhere else.

- It is not `FORMAT_VERSION`. That describes the shape of a payload in
  `#ernie-state`; this moves when a build ships.
- `MIN_BERT` is the floor and must stay at or below `VERSION`, or a release
  locks every board out at once with no build in existence to fix it.
  `tests/check_version.py` holds the two in order.
- The newest build comes from a pinned `**Release** 0.9.3` note in
  `#ernie-state`, written by a person rather than the bot, because nobody can
  edit somebody else's message.

Cutting one, five steps, and the order is the whole of it:

```bash
git checkout main && git merge development     # build from main, never dev
python build.py --version 0.9.4                # bumps VERSION; no build needed yet
git commit -am "0.9.4"                         # the tag has to point at the bump
git tag v0.9.4
python build.py --installer                    # ~80s, from a clean tree
# upload dist/Bert-0.9.4-setup.exe to the Drive folder
# edit the pinned note in #ernie-state:  **Release** 0.9.4
```

The bump is committed before the tag, or the exe reports a version naming a
commit that does not contain it. Build from `main`, or the sha in the exe is
a commit `main` does not have. The upload and the note go together: a note
naming a build that is not in the folder sends everybody to an empty page.

The update check, the two tiers, the installer and going live:
`docs/releases.md`.

## One process, which is what the exe runs

`ernie_app.py`. Everybody runs everything and shares only `#ernie-state`, so
no machine has to stay powered on. `run.sh` is still how this is worked on
from source -- four processes, four logs, restart one without the others.

- Qt owns the main thread, which is not a preference: a QApplication has to
  be created there and its loop has to run there. So the two loops and
  uvicorn are the threads and Bert is what `main()` blocks on.
- The two loops never share a Discord client. The sync's is built with no
  `allow_writes_for` and the outbox's with it; one client between them would
  hand the read-only half a handle that can post.
- A named mutex, not a pid file. A shipped exe gets double-clicked twice, and
  without a lock that is two syncs on one database. A pid file outlives a
  crash and then lies; the kernel keeps this one true.
- Config and the database live in `%LOCALAPPDATA%\Ernie`, because beside the
  executable is either PyInstaller's temp directory, wiped on exit, or a
  Program Files path nobody can write.
- Closing the window stops the outbox, so it spends the undo window rather
  than waiting it out: it brings every undispatched event forward, drains,
  makes any threads still waiting, and publishes once more.
  **None of those steps may raise.** `shut_down` ran unguarded, so one failed
  write threw a traceback out of `main()` -- past the summary line, past the
  mutex release -- and somebody closing the window got a stack trace instead
  of the app going away. Found on the second laptop the day two stacks first
  shared a state channel: a 429 on the final board publish, which two
  machines editing the same messages makes ordinary rather than exotic.
  The **order** is what makes the failure cheap to carry on from: everything
  owed to a customer thread drains *before* the board is published, so the
  step most likely to fail is also the one that costs least -- the next start
  publishes again. `tests/check_app.py` holds both shapes, a failed publish
  and a failed drain.
- A blocked outbox says so rather than vanishing. Read-only is a legitimate
  way to run, so it is reported, never refused.
- **The outbox's two beats are about frequency, not blocking.** All five
  things run on one thread, so a long publish still stops the drain. The
  first pass is therefore a fast one -- it used to be full, and a restart
  spent 4m46s publishing before it posted anything anybody was waiting on --
  and a full pass drains again at the end rather than going to sleep.

The beat split, the installer, the program and board directories, and what
going live took: `docs/releases.md`.

## Running

```bash
./run.sh test bert          # sandbox: sync + outbox + api + bert
./run.sh test bert lan      # same, API reachable from other machines
./run.sh prod               # production: sync + api only, no outbox
./run.sh stop               # stop a stack this script started
python ernie_sync.py --once --env ernie-test.env --db ernie-test.db
```

`run.sh` tails the logs it started, by name, never `logs/*.log`. The
glob is a loop with a fuse in it. Redirect the script's own output into the
folder it is tailing -- `./run.sh test bert > logs/start.log`, which is the
obvious way to keep the startup banner -- and the glob picks that file up, so
`tail` reads what `tail` just wrote and writes it again. Nothing bounds it.
It ran: 392 GB of a banner followed by nul bytes, ending in `tail: error
writing 'standard output': No space left on device`, and C: down to 7.6 GB
free with production's SQLite living on that drive. The stack itself was
fine throughout and said nothing, because nothing had failed -- the loop is
between two programs neither of which was doing anything wrong. It stopped
growing only because the disk filled, and the `tail` was still running
eight hours later, so it would have resumed the moment anything freed space.
`start()` appends each log to `LOGS` and the tail takes that array, which
also drops the one-off logs -- `clone.log`, `prodsync.log`, `prune.log` --
that the glob was following and this run never wrote.

Closing the terminal window leaves the stack running. Ctrl+C reaches the
children through the process group and they stop tidily; closing the window
runs no trap at all, and sync and outbox go on with nothing on screen to say
so. Found in the sandbox after a day of it: six syncs writing to one
database and six outboxes publishing to one state channel, which is where
`outbox.log`'s 429s came from -- and a console window flashing past for every
process every start, which is what a report of "dozens of white windows" turned
out to be. `run.sh` now records the Windows pids (`/proc/<job>/winpid`, not
its own job numbers, which do not outlive it), refuses to start on top of a
stack that is still up, and `./run.sh stop` ends them with `taskkill`.

## How fast Discord reaches the board

Two hops: `ernie_sync` pulls Discord into SQLite, and Bert polls the API every
`POLL_MS` (5s). The sync used to be one 60-second cycle, so a new ticket was
up to 65 seconds old before it appeared. It is two beats now, and a
new ticket is on the board in 0.3-10s, about 5 on average.

- **Three beats.** `--fast` is the listing (5s), `--state-every` is the
  state-channel pull (20s), `--interval` is the rescan and the Jira roster
  (60s). The pull used to ride `--interval` on the argument that it and the
  rescan both use `/channels/{id}/messages` -- true of the route and wrong
  about the quantity, which is what a budget is spent in: the rescan is
  `RESCAN_PER_CYCLE` requests a pass and the pull is **one**, because every
  card in the channel fits in one page of 100 and production holds 59 for 42
  open cards.
  It matters because the pull is the *receiving* half of a shared board. A
  card moved on one laptop crossed to the other in about 50s average and 105s
  worst, and this was the largest single term in it -- bigger than the 30s
  publish beat that sent it and Bert's 5s poll that draws it put together. At
  20s the average is about 30s.
  **The send side was left alone on purpose.** `PUBLISH_MAX` 10 at
  `WRITE_PACE` 1.1 is already 11 seconds of writing per publish pass, so
  halving the 30s beat would have both machines writing into one channel more
  than half the time -- which is the rate-limit collapse of 2026-09-17 rebuilt
  deliberately. Latency on the receive side is one cheap GET; on the send side
  it is the thing that broke.
  `tests/check_sync_beats.py` holds the *position of the call*, which is the
  whole of the change: `pull_state` below the `if not full` return is a pull
  back on the 60s beat and looks identical from everywhere else.
- The split is by cost. A whole cycle is 13 GETs and about 4s, and 11 of
  those GETs are `rescan_edits`; a new ticket arrives through the thread
  listing, which is 1 GET and 0.30s and is effectively unmetered.
- The sleep is measured from the top of the pass, not the end, or a
  four-second pass makes a nine-second beat.
- A quiet fast pass writes no log line, and `prune_runs()` keeps `sync_runs`
  from growing twelve times as fast as it used to.

The measurements behind each of those: `docs/discord.md`.

## Testing with somebody else

Two ways, and they are not the same setup. `TESTING.md` is the version to
hand over.

One backend, two Berts. You run `./run.sh test bert lan`, which binds the
API to every interface and prints the address; they run `bert.cmd`, which
asks for it once and remembers it. They need Python and a clone -- no token,
no database, no migration -- and the board updates at Bert's 5s poll. Same
network only. In *this* setup they must not run `run.sh`: a second sync and
outbox with no `STATE_CHANNEL_ID` is a second board, not a shared one.

Two full stacks. Both run everything, sharing only `#ernie-state`.
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

Bert shows which it is: the `shared board · …` indicator by the refresh
button appears only when `state_sync` has rows, and reports up to date,
waiting to send, or out of contact. It says "shared board" rather than just
"shared" because the label has to answer *what* it is talking about on its
own -- read cold, `shared · in step` told nobody what was in step with what.

The API has no authentication, so on `lan` anyone who can reach the port can
move cards and post to the thread. Trusted network, for as long as the test
lasts, never port-forwarded. `lan` is refused outright for `prod`.

The sandbox's threads are Ernie's own, and production's are not. Every
thread `seed_test_server.py` makes is created by the bot, and a thread's
creator may archive it whatever its permissions say -- so every Complete
the sandbox had ever done took the easy path, and the production case had
never once been exercised. Production's are opened by 36 people: 40 of 40
sampled come back owned by somebody else, and archiving one of those needs
Manage Threads, which the production bot did not have until it was
granted. Unarchiving does not -- reopening needs only Send Messages -- so
Reopen would have worked while Complete failed, which is the confusing half
of it.
A bot cannot create a thread on somebody else's behalf, so the only way to
rehearse this is by hand: open a few threads in the sandbox as yourself.
Done 2026-09-11 with three, and all of it works on them -- Complete posts and
archives, Reopen posts and unarchives, and a rename really renames the thread
and leaves Discord's own type 4 behind. Worth redoing whenever the outbox's
write path changes, because the seeder cannot cover it.

The status message was rehearsed on the same three on 2026-09-17, when
`STATUS_BACKFILL` gave it something to reach them with -- they are inherited,
so nothing could have posted into them before. All three took a message, all
three **pinned**, one each way: an equipment line, a to-do, and a ticket with
neither. Then an edit in place on one of them, which is the path that runs for
the life of a ticket -- one message still, the new work item in it, and the
pass after it wrote nothing. Pinning was the part worth proving. Archiving
somebody else's thread needed Manage Threads and silently did not have it,
so the reasonable worry was that pinning in one would want something too;
it does not, and **Pin Messages** on the role is the whole of it.

Environment: Windows, Git Bash (MINGW64), Python 3.13, SQLite in WAL mode.

## Style

Match what's there: standard library first, `httpx` for HTTP, dataclasses for
records, plain functions over classes unless state demands it. Comments
explain *why*, not *what*. No new dependencies without a reason.

The palette itself -- both themes, the measurements that chose every value,
and the three rounds light mode took to get right -- is in `docs/bert-ui.md`.
These are the rules that outlive it:

No colour literals in Bert. Every colour comes off `T`, the active
palette -- `T.INK`, `T.BAND_CARD[band]` -- and a new one has to be added to
every palette. A hex typed into a stylesheet works in one theme and is wrong
in the others, silently, in whichever nobody happened to be looking at.
Settings offers light, dark, midnight, plum, slate, or following the desktop; changing it
rebuilds the window, because each widget styles itself where it is made and
there is no single sheet to swap -- and a stylesheet missed on a restyle is a
white panel in a dark board. `tests/check_palette.py` holds all three palettes
to the same keys and to the same floors, which is the invariant that keeps a
theme from crashing only for the person using it.

**`PALETTES` is the registry and the one place that knows a theme exists.**
`check_palette.py` walks it rather than keeping a list of its own, so a
palette added there is covered everywhere by existing -- adding plum took the
ink-on-ground check from 60 assertions to 80 without a line of it changing.
A list kept in the checks is a palette held in nine places and not the tenth,
which is the shape of failure they exist to catch. `THEMES` is
`("system",) + tuple(PALETTES)`, because "system" is a question rather than a
palette, and `DARK_GROUNDS` is which of them have pale ink -- `T.dark` asks
about the ground, never about which palette is loaded.

**Midnight and plum are `DARK` with their neutrals re-hued**, spread as `{**DARK, ...}` so
a token added to `DARK` arrives there too. Every L* is held to within 0.16,
which is what makes a new theme cheap to be sure about: contrast is a function
of luminance alone, so a hue that does not move lightness cannot break a ratio
-- worst drift from `DARK` across every pairing that carries meaning is 0.103.
The hazard is the other axis, and midnight is *better* at it than dark: a
ground with a hue of its own hides whichever tags share it, and `DARK`'s
ground at hue 266 sits 5.6 from ENG's fill at 268 -- the closest any tag comes
to vanishing in either theme. Midnight's 280 is past ENG and Medium and short
of CS at 307, so its worst tag stands at **10.8**. `low` and the no-tag fill
are deliberately the ground's own hue and stand off by lightness, so they are
not in that measurement. `resolve_theme` never infers midnight: a desktop can
say dark, not "dark, and blue".

**Plum is the same derivation at hue 345**, which the sweep put highest of any
ground: it sits between CS at 307 and the red bands at 26-29, the other wide
gap on the wheel. Worst tag **13.4** against midnight's 10.8 and dark's 5.6,
worst ratio drift from `DARK` 0.050. The one hue a dark ground may not take is
**90-120**, which scores 1.4 and 3.0 -- it collides with `high band` at 81 and
OPS at 133, so there is no olive or forest-green theme without moving a tag
first.

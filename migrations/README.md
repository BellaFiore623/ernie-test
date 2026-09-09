# Applied migrations

One-off scripts, each already run against both databases. They are kept as a
record of why a column exists, not because anything still needs them —
`schema.sql` builds a current database from nothing, so a fresh clone never
runs these.

Ordered by when they were applied:

| Script | What it did |
|---|---|
| `migrate_actor_name.py` | Recorded who made a change, not just that one happened. |
| `migrate_archived_by_ernie.py` | Told an archive Ernie performed from one a human did. |
| `migrate_client_override.py` | Let a parsed client name be corrected by hand. |
| `migrate_outbox_claim.py` | Gave the outbox a claim, so an undo can't race a message in flight. |
| `migrate_work_items.py` | Replaced four single-value fields with a list of work items. |
| `migrate_thread_owner.py` | Adds `threads.owner_id` and `messages.author_display`, so the feed can say who opened a thread and call them what Discord calls them. Neither is backfilled: owner_id arrives on the thread object every cycle, and messages are inserted once and never rewritten, so old rows keep the username the feed falls back to. |
| `migrate_state_agreed_at.py` | Split "we published" from "we heard from them". `synced_at` is stamped for every card by every publish, so Bert's `shared · in step` only ever measured whether the outbox was alive — it stayed green with the sync loop stopped. `agreed_at` is written by the pull alone. |
| `migrate_triage_rank.py` | Moved the unreadable unassigned cards to the top of the band for real. Bert floated them at draw time, so the board and the state channel disagreed about the running order and a drag inside unassigned landed against the wrong neighbours. |
| `migrate_client_roster.py` | Gave `clients` a `short_name` and an `offered` flag, so the Jira roster can be a dropdown: the summary carries the account note as well as the customer, and an `*INACTIVE*` one still has to name the cards already on it. |
| `migrate_new_threads.py` | Added `new_threads`, the queue a ticket started in Bert waits in until the outbox has opened its thread. |
| `migrate_pending_titles.py` | Re-read the title rows `make_threads` wrote without parsing them. They carried the name and `confidence='pending'`, so queue and client were NULL and the card came up grey with "unknown client" -- and stayed that way, because the sync only writes a revision when the *name* changes and the name never did. Six rows in the sandbox, none in production, which had never made a ticket. |
| `migrate_state_format_skew.py` | Added `state_format_skew`, the one row saying the state channel holds payloads this build cannot read. The skip itself was already happening; it went into a `--pull` printout nobody runs, so two people on different formats got two boards that quietly disagreed. |
| `migrate_thread_status.py` | Added `thread_status`, the message id and last-written body of each thread's status message. Nothing is backfilled: a row appears the first time Ernie posts into a thread it watched open. |
| `migrate_complete_on_arrival.py` | Let a ticket be closed before Discord had it. Completing looks up a `cards` row and a draft has none, so it answered "no such card"; the flag records the intent and `make_threads` acts on it once the thread exists. |
| `migrate_new_thread_rank.py` | Gave a waiting ticket a real `rank`, so it can be dragged before Discord has it. A move looks up a `cards` row and a draft has none, so it answered "no such card" -- and the rank it was sent at, 0.0, sorted it against ranks that can be negative, so a ticket the board promised to put at the top of a band could arrive below everything in it. Existing drafts are backfilled to the top of their band, which is where the board was drawing them. |
| `fix_outbox_view.py` | Compared timestamps with `datetime()` on both sides. Without it the outbox never sent anything: Python writes ISO8601 with a `T`, SQLite's `datetime('now')` uses a space, and the raw string compare was always false. |

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
| `migrate_state_agreed_at.py` | Split "we published" from "we heard from them". `synced_at` is stamped for every card by every publish, so Bert's `shared · in step` only ever measured whether the outbox was alive — it stayed green with the sync loop stopped. `agreed_at` is written by the pull alone. |
| `migrate_triage_rank.py` | Moved the unreadable unassigned cards to the top of the band for real. Bert floated them at draw time, so the board and the state channel disagreed about the running order and a drag inside unassigned landed against the wrong neighbours. |
| `fix_outbox_view.py` | Compared timestamps with `datetime()` on both sides. Without it the outbox never sent anything: Python writes ISO8601 with a `T`, SQLite's `datetime('now')` uses a space, and the raw string compare was always false. |

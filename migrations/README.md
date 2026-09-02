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
| `fix_outbox_view.py` | Compared timestamps with `datetime()` on both sides. Without it the outbox never sent anything: Python writes ISO8601 with a `T`, SQLite's `datetime('now')` uses a space, and the raw string compare was always false. |

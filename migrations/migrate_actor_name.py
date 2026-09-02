"""
Migrate an ernie.db created before the identity change.

The events table used to have actor_id referencing a people table. Identity
now comes from Bert's settings as plain text, so events needs actor_name.

Safe to run more than once -- it checks first and does nothing if already done.

    python migrate_actor_name.py --db ernie.db
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()

con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}

if "actor_name" in cols:
    print("already migrated, nothing to do")
    sys.exit()

if not cols:
    print("no events table yet -- just delete the db and let it rebuild")
    sys.exit()

n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
backup = f"{a.db}.pre-migrate-{datetime.now():%Y%m%d-%H%M}"
shutil.copy(a.db, backup)
print(f"backed up to {backup}  ({n} events)")

con.executescript("""
    ALTER TABLE events RENAME TO events_old;

    CREATE TABLE events (
        event_id        TEXT PRIMARY KEY,
        occurred_at     TEXT NOT NULL,
        actor_name      TEXT,
        thread_id       TEXT REFERENCES threads(thread_id),
        verb            TEXT NOT NULL,
        old_value       TEXT,
        new_value       TEXT,
        undone_at       TEXT,
        undone_by       TEXT,
        dispatch_after  TEXT,
        posted_at       TEXT,
        discord_message_id TEXT,
        attempts        INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT
    );

    INSERT INTO events (event_id, occurred_at, actor_name, thread_id, verb,
                        old_value, new_value, undone_at, undone_by,
                        dispatch_after, posted_at, discord_message_id,
                        attempts, last_error)
    SELECT event_id, occurred_at, actor_id, thread_id, verb,
           old_value, new_value, undone_at, undone_by,
           dispatch_after, posted_at, discord_message_id,
           attempts, last_error
    FROM events_old;

    DROP TABLE events_old;
    DROP TABLE IF EXISTS people;

    CREATE INDEX IF NOT EXISTS ix_events_feed ON events(occurred_at DESC);
    CREATE INDEX IF NOT EXISTS ix_events_thread ON events(thread_id, occurred_at DESC);

    DROP VIEW IF EXISTS v_outbox_due;
    CREATE VIEW v_outbox_due AS
    SELECT * FROM events
    WHERE dispatch_after IS NOT NULL
      AND posted_at IS NULL
      AND undone_at IS NULL
      AND dispatch_after <= datetime('now')
      AND attempts < 5
    ORDER BY occurred_at;
""")
con.commit()

kept = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
print(f"migrated: {kept} events carried over, people table dropped")
con.close()

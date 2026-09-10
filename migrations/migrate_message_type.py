"""Add messages.type, and the partial index over it. Safe to re-run.

Discord's message type. 4 is CHANNEL_NAME_CHANGE -- a thread rename, carrying
the new name as its content -- and it is the only exact record of a retag
there is. Existing rows are left NULL: the type is not in the mirror, so it
cannot be derived, only re-fetched. `load_messages` fills it in on any row
that has none, so a later re-read backfills without a second code path.
"""
import argparse, sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()

con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
if "type" in cols:
    print("already migrated")
else:
    con.execute("ALTER TABLE messages ADD COLUMN type INTEGER")
    print("added messages.type")
con.execute("""CREATE INDEX IF NOT EXISTS ix_messages_type
               ON messages(type, created_at)
               WHERE type IS NOT NULL AND type <> 0""")
con.commit()
blank = con.execute("SELECT COUNT(*) FROM messages WHERE type IS NULL").fetchone()[0]
print(f"{blank} message(s) with no type yet -- filled in as they are re-read")
con.close()

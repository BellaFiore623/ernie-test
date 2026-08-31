"""
Fix the outbox view on an existing database.

The view compared dispatch_after (Python ISO8601: '2026-08-28T19:18:29+00:00')
against datetime('now') (SQLite: '2026-08-28 19:18:44') as raw strings. 'T'
sorts after ' ', so the comparison was always false and no event would ever
have been posted. Wrapping both sides in datetime() normalises them.

    python fix_outbox_view.py --db ernie.db
"""
import argparse
import sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()

con = sqlite3.connect(a.db)
con.executescript("""
DROP VIEW IF EXISTS v_outbox_due;
CREATE VIEW v_outbox_due AS
SELECT * FROM events
WHERE dispatch_after IS NOT NULL
  AND posted_at IS NULL
  AND undone_at IS NULL
  AND datetime(dispatch_after) <= datetime('now')
  AND attempts < 5
ORDER BY occurred_at;
""")
con.commit()
n = con.execute("SELECT COUNT(*) FROM v_outbox_due").fetchone()[0]
print(f"view rebuilt -- {n} events now due")
con.close()

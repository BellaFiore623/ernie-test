"""
Ad-hoc SQL against an Ernie database.

    python q.py "SELECT COUNT(*) FROM cards"
    python q.py "SELECT * FROM events LIMIT 3" ernie-test.db
"""
import sqlite3
import sys

if len(sys.argv) < 2:
    sys.exit('usage: python q.py "SELECT ..." [db]')

db = sys.argv[2] if len(sys.argv) > 2 else "ernie.db"
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

try:
    rows = con.execute(sys.argv[1]).fetchall()
except sqlite3.Error as e:
    sys.exit(f"sql error: {e}")

con.commit()

if not rows:
    print(f"{con.total_changes} rows affected" if con.total_changes else "no rows")
else:
    cols = rows[0].keys()
    widths = [max(len(c), max(len(str(r[c])) for r in rows)) for c in cols]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print(f"\n{len(rows)} rows")

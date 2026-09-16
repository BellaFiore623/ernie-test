"""
Ad-hoc SQL against an Ernie database.

    python q.py "SELECT COUNT(*) FROM cards"
    python q.py "SELECT * FROM events LIMIT 3" ernie-test.db
    python q.py --write "UPDATE ..." ernie-test.db

**Read-only unless you ask for otherwise.** This gets pointed at live
databases while a stack is running -- that is what it is for -- and it used
to open read-write with no busy timeout, so a question about the board took
a lock the board needed. A tool for looking should not be able to write by
accident, and `mode=ro` means a typo that starts UPDATE is refused by SQLite
rather than applied.

The busy timeout is the other half: waiting five seconds for a writer to
finish is the right answer to a busy database, where failing instantly makes
this unusable exactly when somebody most wants to look.
"""
import sqlite3
import sys

args = [a for a in sys.argv[1:] if a != "--write"]
writing = "--write" in sys.argv

if not args:
    sys.exit('usage: python q.py [--write] "SELECT ..." [db]')

db = args[1] if len(args) > 1 else "ernie.db"
if writing:
    con = sqlite3.connect(db, timeout=15.0)
else:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15.0)
con.row_factory = sqlite3.Row
con.execute("PRAGMA busy_timeout = 15000")

try:
    rows = con.execute(args[0]).fetchall()
except sqlite3.Error as e:
    con.close()
    sys.exit(f"sql error: {e}"
             + ("" if writing else "\n(read-only -- pass --write to change "
                                   "anything)"))

if writing:
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

con.close()

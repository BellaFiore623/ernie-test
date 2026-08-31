"""Ad-hoc SQL against ernie.db.  python q.py "SELECT ..." """
import sqlite3, sys
con = sqlite3.connect("ernie.db")
con.row_factory = sqlite3.Row
rows = con.execute(sys.argv[1]).fetchall()
con.commit()
if rows:
    print(" | ".join(rows[0].keys()))
    for r in rows:
        print(" | ".join(str(v) for v in r))
else:
    print(f"{con.total_changes} rows affected")
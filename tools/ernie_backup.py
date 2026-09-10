"""
Back up ernie.db safely while the sync service is running.

Uses SQLite's online backup API -- a plain file copy is NOT safe in WAL mode
and can produce a corrupt snapshot.

    python ernie_backup.py                    # back up ./ernie.db
    python ernie_backup.py --keep 60          # keep 60 backups instead of 30
    python ernie_backup.py --verify           # check the newest backup opens
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys
from datetime import datetime


def backup(db: str, outdir: str, keep: int) -> pathlib.Path:
    src = pathlib.Path(db)
    if not src.exists():
        sys.exit(f"no such database: {src}")

    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    dest = out / f"ernie-{stamp}.db"

    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    with dst:
        con.backup(dst)          # consistent snapshot, safe while running
    dst.close()
    con.close()

    mb = dest.stat().st_size / 1_000_000
    print(f"wrote {dest}  ({mb:.1f} MB)")

    # Rotation, by count rather than by age -- the flag is a number of
    # backups, whatever the usage line used to say.
    #
    # The `-wal` and `-shm` beside a backup go with it. Globbing `*.db`
    # alone unlinked the database and left its two companions behind for
    # ever, which is how three-week-old orphans came to be sitting in here
    # with nothing to belong to.
    for f in sorted(out.glob("ernie-*.db"))[:-keep]:
        for part in (f, f.with_name(f.name + "-wal"),
                     f.with_name(f.name + "-shm")):
            if part.exists():
                part.unlink()
        print(f"  removed {f.name}")
    return dest


def verify(path: pathlib.Path) -> None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    counts = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("threads", "messages", "cards", "events")
    }
    con.close()
    print(f"integrity: {ok}")
    print("  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    if ok != "ok":
        sys.exit("BACKUP IS CORRUPT")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--outdir", default="backups")
    ap.add_argument("--keep", type=int, default=30,
                    help="how many backups to keep, oldest deleted first")
    ap.add_argument("--verify", action="store_true",
                    help="open the new backup and check it")
    a = ap.parse_args()

    dest = backup(a.db, a.outdir, a.keep)
    if a.verify:
        verify(dest)

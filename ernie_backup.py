"""
Back up ernie.db safely while the sync service is running.

Uses SQLite's online backup API -- a plain file copy is NOT safe in WAL mode
and can produce a corrupt snapshot.

    python ernie_backup.py                    # back up ./ernie.db
    python ernie_backup.py --keep 60          # keep 60 days instead of 30
    python ernie_backup.py --verify latest    # check the newest backup opens
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

    # rotation
    old = sorted(out.glob("ernie-*.db"))[:-keep]
    for f in old:
        f.unlink()
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
    ap.add_argument("--keep", type=int, default=30)
    ap.add_argument("--verify", action="store_true",
                    help="open the new backup and check it")
    a = ap.parse_args()

    dest = backup(a.db, a.outdir, a.keep)
    if a.verify:
        verify(dest)

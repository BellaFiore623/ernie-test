"""
Catch a runaway write-ahead log in the act.

A reader that never lets go pins the WAL, SQLite cannot checkpoint it, the
log grows without bound and writers start timing out. It has happened twice
on this sandbox -- 6.59 MB against 4.58 MB, then 33 MB against 4.68 MB, the
second time costing a Complete four and a half minutes to reach its thread
and posting one change-log line seven times. Both were reconstructed hours
later from what was left behind.

This is the part that was missing: something watching while it happens.
`/health` reports the size and Bert draws a strip, which tells a person the
WAL is large. Neither says *when it started* or *what was running*, and that
is what identifies the reader.

    python tools/wal_watch.py --db ernie-test.db
    python tools/wal_watch.py --db ernie-test.db --interval 5 --log logs/wal.log

**How it decides.** A passive checkpoint never blocks: it copies back what
it can and reports `(busy, frames_in_log, frames_copied)`. On a healthy
database those two numbers meet every few seconds. When frames pile up in
the log and cannot be copied back, some connection is holding a snapshot
older than them -- so `frames_in_log > frames_copied`, pass after pass, is
the signature. One pass proves nothing, because a checkpoint racing an
active writer is ordinary; `PINNED_PASSES` in a row is not.

It must hold **one** connection open for the life of the run. Opening and
closing per probe is what made the first attempt at this useless: SQLite
checkpoints automatically when the last connection goes, so the log was
empty every time it was measured and "healthy" meant nothing.

It is also the one tool here that has to be read-write, since checkpointing
is a write. It writes nothing else -- no schema, no rows.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

PINNED_PASSES = 3      # consecutive passes with frames stuck before it speaks


def now() -> str:
    return datetime.now(timezone.utc).isoformat()[:19]


def sizes(db: str) -> tuple[float, float]:
    """Database and WAL, in MB."""
    def mb(p):
        try:
            return os.path.getsize(p) / 1048576
        except OSError:
            return 0.0
    return mb(db), mb(db + "-wal")


def whats_running() -> str:
    """Every python process, so the report names what was up at the time.

    The reader is a process, and the only thing that identifies it after the
    fact is knowing which were alive when the frames first stuck.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             "ForEach-Object { '{0} {1}' -f $_.ProcessId, "
             "$_.CommandLine.Substring(0, [Math]::Min(60, "
             "$_.CommandLine.Length)) }"],
            capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or "(none)"
    except Exception as e:                       # noqa: BLE001
        return f"(could not list processes: {e})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie-test.db")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--log", help="append here as well as to stdout")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        sys.exit(f"no such database: {a.db}")

    out = open(a.log, "a", encoding="utf-8", buffering=1) if a.log else None

    def say(line: str, loud: bool = False) -> None:
        print(line, file=sys.stderr if loud else sys.stdout, flush=True)
        if out:
            out.write(line + "\n")

    con = sqlite3.connect(a.db, timeout=15.0)
    con.execute("PRAGMA busy_timeout = 15000")

    db_mb, wal_mb = sizes(a.db)
    say(f"[{now()}] watching {a.db} ({db_mb:.2f} MB), every {a.interval}s. "
        f"Pinned after {PINNED_PASSES} passes with frames stuck.")

    stuck = 0
    pinned_since = None
    try:
        while True:
            busy, in_log, copied = con.execute(
                "PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            db_mb, wal_mb = sizes(a.db)
            behind = max(in_log - copied, 0)

            if behind > 0 or busy:
                stuck += 1
            else:
                if pinned_since:
                    say(f"[{now()}] cleared -- the WAL is checkpointing again "
                        f"(was pinned from {pinned_since}, WAL {wal_mb:.2f} MB)")
                    pinned_since = None
                stuck = 0

            if stuck == PINNED_PASSES:
                pinned_since = now()
                say(f"[{now()}] PINNED -- {behind} frames cannot be copied "
                    f"back after {PINNED_PASSES} passes. WAL {wal_mb:.2f} MB "
                    f"against {db_mb:.2f} MB. A reader is holding a snapshot.",
                    loud=True)
                say("  running at this moment:", loud=True)
                for line in whats_running().splitlines():
                    say(f"    {line}", loud=True)

            # A quiet pass says nothing, the rule the sync's beats follow --
            # twelve times an hour of "nothing wrong" buries the one line
            # somebody opened the file to find.
            if a.once:
                say(f"[{now()}] busy={busy} in_log={in_log} copied={copied} "
                    f"WAL {wal_mb:.2f} MB against {db_mb:.2f} MB")
                return
            time.sleep(a.interval)
    except KeyboardInterrupt:
        say(f"[{now()}] stopped")
    finally:
        con.close()
        if out:
            out.close()


if __name__ == "__main__":
    main()

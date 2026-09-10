"""Fill the stats panel with plausible history, so it can be looked at.

The sandbox is reseeded from scratch, so it has no closures and no ageing --
the panel is honest and almost empty, which is no use for judging how it
reads. This invents a past for it.

    python tools/fake_stats_data.py --db ernie-test.db
    python tools/fake_stats_data.py --db ernie-test.db --clear

Two things it is careful about.

**It refuses production.** The figures there are real, and a fake month in
them would be believed.

**Everything it adds is removable.** The invented threads are all prefixed
`fake-`, so `--clear` takes exactly them and nothing else. They are also
written `archived`, which keeps ernie_status away from them: their threads do
not exist in Discord, and a status message posted at one would fail on every
outbox pass for ever.

The real seeded cards are not duplicated, only *backdated* -- their threads
keep their real ids, so the "open longest" rows still click through to a card
that is really there.

**Everything invented is closed**, and that is deliberate rather than an
oversight. An invented *open* ticket would sit on the board looking like a
real one, with no Discord thread behind it and nothing to click through to.
So the open column is the real seeded cards and only those; it is the two
flows -- created and closed -- that this fills in.
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

PREFIX = "fake-"

# A rising trend, because that is the shape production has (27, 61, 63, 67,
# 95 across five months) and a flat one tells you nothing about whether the
# panel reads well. Twelve months, because the figures panel offers a window
# out to a year and a selector whose longest setting shows the same thing as
# its second longest is a selector nobody trusts.
PER_MONTH = (9, 11, 14, 12, 17, 19, 14, 22, 26, 31, 44, 38)

# The tags, and roughly how the board is actually made up: PROD is most of
# it. Invented history that was all one tag made the per-tag breakdown look
# broken rather than empty -- every row but one at nought, on a panel built
# to compare them.
QUEUE_MIX = (["PROD"] * 6) + (["OPS"] * 3) + (["ENG"] * 2) + ["CS"]

# What each tag's work tends to be called, so a title reads like the board's
# rather than like a fixture.
SUMMARY = {
    "PROD": ("respool", "fiber snapped", "swap", "job", "reel service"),
    "OPS":  ("bot swap", "no amber light", "escalation", "site visit"),
    "ENG":  ("firmware", "what it's about", "sensor drift", "rev check"),
    "CS":   ("training", "onboarding", "follow up"),
}

# Most jobs close quickly and a few drag for months. This is the spread that
# makes the median worth quoting: the mean sits well above the middle.
SPANS = [1, 1, 2, 2, 3, 3, 4, 5, 6, 7, 9, 11, 14, 19, 26, 41, 63, 88]

CLIENTS = ("Ravanair", "St. Tammany", "Westmoreland County", "IPI",
           "Edge AI Services", "Trekk", "SCI", "Thrasher", "Kenosha",
           "Precision Trenchless")

# Days open, oldest first, applied to the real cards that are already there.
AGES = (152, 118, 110, 96, 95, 40, 21, 12, 5, 2)


def iso(when: datetime) -> str:
    return when.isoformat()


def clear(con) -> int:
    n = con.execute("SELECT COUNT(*) FROM cards WHERE thread_id LIKE ?",
                    (PREFIX + "%",)).fetchone()[0]
    for table in ("work_items", "thread_equipment", "thread_titles", "cards",
                  "messages", "threads"):
        try:
            con.execute(f"DELETE FROM {table} WHERE thread_id LIKE ?",
                        (PREFIX + "%",))
        except sqlite3.OperationalError:
            pass        # a table that has no thread_id, or no such table
    con.commit()
    return n


def backdate_real_cards(con) -> int:
    """Spread the ages of the cards that are really there.

    Their threads are real, so the panel's "open longest" rows click through
    to something. Nothing is invented here -- only moved.
    """
    rows = con.execute(
        """SELECT c.thread_id FROM cards c
           WHERE c.completed_at IS NULL AND c.thread_id NOT LIKE ?
           ORDER BY c.rank""", (PREFIX + "%",)).fetchall()
    now = datetime.now(timezone.utc)
    for row, days in zip(rows, AGES):
        when = now - timedelta(days=days, hours=3)
        # first_seen_at moves with it, and stays close behind. witnessed_start
        # asks whether Ernie saw the thread appear -- created_at alone would
        # leave a 152-day gap, the thread would read as inherited, and it
        # would quietly drop out of the status messages it already has.
        con.execute(
            "UPDATE threads SET created_at=?, first_seen_at=? WHERE thread_id=?",
            (iso(when), iso(when + timedelta(seconds=20)), row[0]))
    con.commit()
    return min(len(rows), len(AGES))


def add_history(con, rng: random.Random) -> int:
    """Closed tickets, month by month, going back six months."""
    now = datetime.now(timezone.utc)
    made = 0
    for back, count in enumerate(reversed(PER_MONTH)):
        for i in range(count):
            # Somewhere inside that month, and the span decides when it opened.
            closed = now - timedelta(days=back * 30 + rng.randint(1, 27),
                                     hours=rng.randint(0, 23))
            span = rng.choice(SPANS)
            opened = closed - timedelta(days=span, hours=rng.randint(0, 23))
            tid = f"{PREFIX}{back}-{i}"
            client = rng.choice(CLIENTS)
            queue = rng.choice(QUEUE_MIX)
            name = (f"{queue}: {client} - {opened.strftime('%d%b%y')} - "
                    f"EReel-{1000 + rng.randint(0, 400)} "
                    f"{rng.choice(SUMMARY[queue])}")
            con.execute(
                """INSERT OR REPLACE INTO threads
                   (thread_id, parent_id, guild_id, created_at, first_seen_at,
                    last_synced_at, archived)
                   VALUES (?,?,?,?,?,?,1)""",
                (tid, PREFIX + "channel", PREFIX + "guild", iso(opened),
                 iso(opened), iso(closed)))
            con.execute(
                """INSERT OR REPLACE INTO thread_titles
                   (thread_id, observed_at, name, queue, client_raw,
                    client_key, thread_date, summary, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (tid, iso(opened), name, queue, client, client.lower(),
                 opened.date().isoformat(), name.rsplit(" - ", 1)[-1],
                 "strict"))
            con.execute(
                """INSERT OR REPLACE INTO cards
                   (thread_id, priority, rank, updated_at, completed_at,
                    completed_by)
                   VALUES (?,?,?,?,?,?)""",
                (tid, "medium", 1000.0 + made, iso(closed), iso(closed),
                 "imported"))
            made += 1
    con.commit()
    return made


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie-test.db")
    ap.add_argument("--clear", action="store_true",
                    help="remove everything this added, and nothing else")
    ap.add_argument("--seed", type=int, default=7,
                    help="so two runs give the same board")
    a = ap.parse_args()

    if a.db.endswith("ernie.db"):
        sys.exit("REFUSING: that's production. Its figures are real, and a "
                 "fake month in them would be believed.")

    con = sqlite3.connect(a.db)
    con.execute("PRAGMA foreign_keys = ON")
    gone = clear(con)
    if a.clear:
        print(f"removed {gone} invented card(s)")
        con.close()
        return

    moved = backdate_real_cards(con)
    made = add_history(con, random.Random(a.seed))
    print(f"backdated {moved} real card(s) so they have an age")
    print(f"invented {made} closed ticket(s) across {len(PER_MONTH)} months")
    print(f"\nundo with:  python tools/fake_stats_data.py --db {a.db} --clear")
    con.close()


if __name__ == "__main__":
    main()

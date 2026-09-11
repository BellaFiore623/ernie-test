"""Fill the stats panel with plausible history, so it can be looked at.

The sandbox is reseeded from scratch, so it has no closures and no ageing --
the panel is honest and almost empty, which is no use for judging how it
reads. This invents a past for it.

    python tools/fake_stats_data.py --db ernie-test.db
    python tools/fake_stats_data.py --db ernie-test.db --clear

Two things it is careful about.

**It refuses production.** The figures there are real, and a fake month in
them would be believed.

**Everything it does is undoable, including the part it does not add.**
The invented threads are all prefixed `fake-`, so `--clear` takes exactly
them and nothing else -- and the *backdating* of real cards is written down
before it happens, in `fake_backdated`, so `--clear` puts those back too.
That table is the whole of the lesson from the day this ran against
production by mistake: the invented rows came out in one command, and the ten
real threads it had backdated needed a three-week-old backup, because
"removable" had quietly meant "the rows it inserted". They are also
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

# The one guild this must never touch. Copied from `ernie_sync` rather than
# imported: everything under tools/ runs standalone -- `python tools/x.py`
# puts tools/ on the path and not the repo root -- and this one does no
# networking, so pulling in the whole Discord client for one string is a poor
# trade. `tests/check_guards.py` holds the copy to the original, which is the
# part that matters: the value being wrong everywhere at once is how this
# guard came to be decorative in the first place.
PRODUCTION_GUILD = "924120427469623297"

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

# Some tickets are retagged partway through -- which is the figure Julian
# asked for, and it comes straight off `thread_titles` rather than out of
# anything stored for the purpose. So the invented history has to contain
# some, or the block reads as broken rather than as quiet.
#
# The mix is weighted the way the real one is described: production work that
# turns into operations, mostly, with a few going back the other way and a
# handful of engineering escalations. One ticket in five, which is enough to
# fill the block at a 7-day window without making a retag look routine.
RETAG_ODDS = 5
RETAG = {
    "PROD": (["OPS"] * 6) + ["ENG"] * 2 + ["CS"],
    "OPS":  (["PROD"] * 3) + ["ENG"],
    "ENG":  (["OPS"] * 2) + ["PROD"],
    "CS":   ["OPS", "PROD"],
}


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


def remember_before_backdating(con, tids) -> None:
    """Write down what the real threads said, so `--clear` can put it back.

    Nothing else here needs a record: the invented rows carry a prefix and
    can simply be deleted. These are *real* threads, and what this is about
    to overwrite -- when Discord says the thread was made, and when Ernie
    first saw it -- cannot be worked out again afterwards. `first_seen_at`
    especially: it is this machine's own history and exists nowhere else.
    """
    con.execute("""CREATE TABLE IF NOT EXISTS fake_backdated (
                       thread_id     TEXT PRIMARY KEY,
                       created_at    TEXT,
                       first_seen_at TEXT)""")
    for tid in tids:
        row = con.execute("SELECT created_at, first_seen_at FROM threads "
                          "WHERE thread_id=?", (tid,)).fetchone()
        if row is None:
            continue
        # INSERT OR IGNORE, not REPLACE: running this twice must not record
        # the *backdated* values over the real ones it wrote down first time.
        con.execute("INSERT OR IGNORE INTO fake_backdated "
                    "(thread_id, created_at, first_seen_at) VALUES (?,?,?)",
                    (tid, row[0], row[1]))
    con.commit()


def restore_backdated(con) -> int:
    """Put the real threads' dates back, and forget we ever moved them."""
    try:
        rows = con.execute("SELECT thread_id, created_at, first_seen_at "
                           "FROM fake_backdated").fetchall()
    except sqlite3.OperationalError:
        return 0            # nothing was ever backdated here
    for r in rows:
        con.execute("UPDATE threads SET created_at=?, first_seen_at=? "
                    "WHERE thread_id=?", (r[1], r[2], r[0]))
    con.execute("DROP TABLE fake_backdated")
    con.commit()
    return len(rows)


def backdate_real_cards(con) -> int:
    """Spread the ages of the cards that are really there.

    Their threads are real, so the panel's "open longest" rows click through
    to something. Nothing is invented here -- only moved, and what it is
    moved from is written down first so it can be moved back.
    """
    rows = con.execute(
        """SELECT c.thread_id FROM cards c
           WHERE c.completed_at IS NULL AND c.thread_id NOT LIKE ?
           ORDER BY c.rank""", (PREFIX + "%",)).fetchall()
    now = datetime.now(timezone.utc)
    remember_before_backdating(con, [r[0] for r in rows[:len(AGES)]])
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
    """Closed tickets, month by month, going back six months.

    Answers `(made, retagged)` -- the second is how many of them changed tag
    on the way, which is a separate line in the report because it is a
    separate block on the panel.
    """
    now = datetime.now(timezone.utc)
    made = retagged = 0
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
            # A retag is a second title row, and that is the whole of it --
            # append-only, so the first one stays and the pair *is* the
            # transition. Placed inside the ticket's own life rather than at
            # a round offset, or every move in a window would land on the
            # same day.
            if span >= 2 and rng.randrange(RETAG_ODDS) == 0:
                to = rng.choice(RETAG[queue])
                when = opened + timedelta(
                    days=rng.randint(1, max(1, span - 1)),
                    hours=rng.randint(0, 23))
                renamed = f"{to}: " + name.split(": ", 1)[1]
                con.execute(
                    """INSERT OR REPLACE INTO thread_titles
                       (thread_id, observed_at, name, queue, client_raw,
                        client_key, thread_date, summary, confidence)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (tid, iso(when), renamed, to, client, client.lower(),
                     opened.date().isoformat(),
                     renamed.rsplit(" - ", 1)[-1], "strict"))
                retagged += 1
            con.execute(
                """INSERT OR REPLACE INTO cards
                   (thread_id, priority, rank, updated_at, completed_at,
                    completed_by)
                   VALUES (?,?,?,?,?,?)""",
                (tid, "medium", 1000.0 + made, iso(closed), iso(closed),
                 "imported"))
            made += 1
    con.commit()
    return made, retagged


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie-test.db")
    ap.add_argument("--clear", action="store_true",
                    help="remove everything this added, and nothing else")
    ap.add_argument("--seed", type=int, default=7,
                    help="so two runs give the same board")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row

    # **Which guild the data belongs to, not what the file is called.**
    # This used to refuse anything named `ernie.db`, which was exactly right
    # while the only two databases in the world were `ernie.db` and
    # `ernie-test.db`. The exe made the name meaningless: an installed copy
    # keeps its database at %LOCALAPPDATA%\Ernie\ernie.db whatever server it
    # is pointed at, so the check refused a sandbox install -- and would have
    # allowed production under any other filename. The guild is the fact the
    # question is actually about, and the database already carries it.
    row = con.execute("SELECT guild_id, COUNT(*) AS n FROM threads "
                      "WHERE guild_id IS NOT NULL "
                      "GROUP BY guild_id ORDER BY n DESC LIMIT 1").fetchone()
    guild = row["guild_id"] if row else None

    if guild is None:
        # Nothing synced yet, so there is nothing to read the answer off.
        # Refusing is the safe way to be unsure: a database about to have
        # production pulled into it would end up with invented rows mixed
        # through real ones, and no way to tell which was which afterwards.
        sys.exit(f"Can't tell whose board {a.db} is -- it has no threads yet. "
                 f"Let it sync once, then run this again.")

    if guild == PRODUCTION_GUILD:
        sys.exit("REFUSING: that's production. Its figures are real, and a "
                 "fake month in them would be believed.")

    con.execute("PRAGMA foreign_keys = ON")
    gone = clear(con)
    if a.clear:
        put_back = restore_backdated(con)
        print(f"removed {gone} invented card(s)")
        if put_back:
            print(f"put {put_back} real thread(s) back to their own dates")
        con.close()
        return

    moved = backdate_real_cards(con)
    made, retagged = add_history(con, random.Random(a.seed))
    print(f"backdated {moved} real card(s) so they have an age")
    print(f"invented {made} closed ticket(s) across {len(PER_MONTH)} months")
    print(f"{retagged} of them changed tag partway through")
    print(f"\nundo with:  python tools/fake_stats_data.py --db {a.db} --clear")
    con.close()


if __name__ == "__main__":
    main()

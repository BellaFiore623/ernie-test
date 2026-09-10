"""
Put the renames Ernie never watched back into `thread_titles`.

Discord posts a system message into a thread every time it is renamed --
type 4, CHANNEL_NAME_CHANGE, carrying the new name as its content. Those
messages have always been in the mirror; what was missing was the `type` that
tells one from somebody pasting a title into the chat, and
`tools/backfill_message_types.py` fetched it. Production has 703 of them.

They are not a *title history* yet. `thread_titles` is what every reading of
the board goes through -- the retag figure included -- and production has one
row per thread, written at its first sync, so a board that has been renamed
for two years reads as never having changed. This walks the renames and
writes the revision each one was.

    python tools/rebuild_title_history.py --db ernie.db --dry-run
    python tools/rebuild_title_history.py --db ernie.db

**No network.** Everything it needs is already in the database.

**It cannot change what the board shows today.** A rename is written only if
it is strictly older than the thread's earliest existing title row, so the
newest revision -- the one `v_thread_current` reads and every card takes its
queue and client from -- is never the one we added. Renames dated after that
are the *sync's* business: it will see the current name on its next pass and
record it the way it records every other one. Measured against production,
that is 2 of 696; the other 694 are history nobody was watching.

**And it writes through `load.record_title`**, which is the one place that
decides what a title row holds. A row written any other way is a row parsed
some other way, and that has already caused one bug -- the outbox writing the
name alone left cards grey with "unknown client" for titles that read
perfectly well.

Re-runnable: `thread_titles` is keyed on (thread_id, observed_at), so a
second pass rewrites the same rows with the same values.
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ernie_load as load           # noqa: E402  (after the path insert)


RENAME = 4          # CHANNEL_NAME_CHANGE, confirmed against Discord


def renames(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every rename in the mirror, with the name it gave the thread.

    The newest revision of each, because a system message is not edited but
    the table holds revisions and asking for "the content" without saying
    which one is how a mirror gets read wrongly.
    """
    return con.execute(
        """SELECT m.thread_id, m.created_at AS at, r.content AS name,
                  (SELECT MIN(observed_at) FROM thread_titles ti
                    WHERE ti.thread_id = m.thread_id) AS earliest
             FROM messages m
             JOIN message_revisions r USING (message_id)
            WHERE m.type = ?
              AND m.deleted_at IS NULL
              AND r.observed_at = (SELECT MAX(observed_at)
                                     FROM message_revisions
                                    WHERE message_id = m.message_id)
            ORDER BY m.thread_id, m.created_at""", (RENAME,)).fetchall()


def rebuild(con: sqlite3.Connection, commit: bool = True) -> dict:
    """Write each rename as the title revision it was. Answers a tally.

    `commit` is what makes `--dry-run` a real preview rather than a promise:
    the rows go in, the effect is measured, and the whole thing is rolled
    back. A dry run that skips the writes can only report how many it would
    have made, which is the least interesting half of the question -- what
    somebody wants to know before running this on a live board is what it
    would do to the figures.
    """
    out = {"renames": 0, "written": 0, "already": 0,
           "not_history": 0, "empty": 0, "no_thread": 0}

    for r in renames(con):
        out["renames"] += 1
        name = (r["name"] or "").strip()
        if not name:
            # Discord always puts the new name in a rename, so this is a row
            # the mirror never got the content of rather than a nameless
            # rename. Nothing to write either way.
            out["empty"] += 1
            continue
        if r["earliest"] is None:
            # A thread with no title row at all is one the sync has never
            # made a card for. Inventing its history here would be inventing
            # its present too, since the row would be the newest.
            out["no_thread"] += 1
            continue
        # Only an exact row -- same thread, same moment -- counts as one we
        # already have. Matching on the *name* alone was tried and is wrong
        # in a way that matters: production's one row per thread carries the
        # name as of its first sync, which is usually the name the last
        # rename gave it. Skipping that rename because the name was familiar
        # would leave the revision dated the day Ernie first looked rather
        # than the day it happened -- so a thread that went OPS last April
        # would count as having gone OPS in August, inside windows it fell
        # outside. The duplicate name is not noise, it is the difference
        # between when it was renamed and when we first saw it, and the two
        # carry the same tag so no move is invented by having both.
        #
        # **Asked before the age question**, which changes no outcome and
        # every explanation. Once a pass has run, the oldest row a thread
        # holds is one this wrote, so every rename is at-or-after it: asked
        # the other way round, a second run reported all 694 as "newer than
        # what we hold -- the sync's job", which is a sentence about rows we
        # had put there ourselves a minute earlier.
        seen = con.execute(
            "SELECT 1 FROM thread_titles WHERE thread_id=? AND observed_at=?",
            (r["thread_id"], r["at"])).fetchone()
        if seen:
            out["already"] += 1
            continue
        if r["at"] >= r["earliest"]:
            # At or after what we already hold, so writing it could become
            # the newest revision and change the card. That is the sync's
            # job, and it will do it on its next pass.
            out["not_history"] += 1
            continue

        load.record_title(con, r["thread_id"], name, r["at"])
        if commit:
            con.commit()            # per row: the sync writes here too
        out["written"] += 1

    return out


def moves(con: sqlite3.Connection) -> dict:
    """Tag changes the title history now shows, for cards.

    The same reading `/stats` does, run here so the tool can say what it
    changed rather than leaving somebody to go and look.
    """
    rows = con.execute(
        """SELECT was, queue, COUNT(*) AS n
             FROM (SELECT ti.thread_id, ti.observed_at, ti.queue,
                          LAG(ti.queue) OVER (PARTITION BY ti.thread_id
                                              ORDER BY ti.observed_at) AS was
                     FROM thread_titles ti
                     JOIN cards c USING (thread_id)
                    WHERE ti.queue IS NOT NULL AND ti.queue <> '')
            WHERE was IS NOT NULL AND was <> queue
            GROUP BY was, queue
            ORDER BY n DESC, was, queue""").fetchall()
    return {(r["was"], r["queue"]): r["n"] for r in rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would be written, write nothing")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row

    cols = {c[1] for c in con.execute("PRAGMA table_info(messages)")}
    if "type" not in cols:
        sys.exit(f"{a.db} has no messages.type -- apply "
                 f"migrations/migrate_message_type.py first")
    typed = con.execute(
        "SELECT COUNT(*) FROM messages WHERE type IS NOT NULL").fetchone()[0]
    if not typed:
        sys.exit(f"{a.db} has the column but nothing in it -- run "
                 f"tools/backfill_message_types.py first, or there is no "
                 f"history here to rebuild")

    before = moves(con)
    if a.dry_run:
        # Written, measured, and taken back out, so the figures below are
        # what would really happen rather than an estimate of it.
        out = rebuild(con, commit=False)
        after = moves(con)
        con.rollback()
        if moves(con) != before:
            sys.exit("the preview did not roll back cleanly -- stopping")
    else:
        out = rebuild(con)
        after = moves(con)

    print(f"{out['renames']} rename(s) in the mirror")
    print(f"  {out['written']} written as title revisions"
          f"{' (dry run, nothing written)' if a.dry_run else ''}")
    print(f"  {out['already']} already recorded -- an earlier pass, or the "
          f"sync was watching")
    print(f"  {out['not_history']} newer than what we hold -- the sync's job")
    if out["empty"]:
        print(f"  {out['empty']} with no content in the mirror")
    if out["no_thread"]:
        print(f"  {out['no_thread']} on threads with no title row at all")

    print("\ntag changes visible to /stats, on cards"
          + (" (previewed, then rolled back):" if a.dry_run
             else ":"))
    for pair in sorted(set(before) | set(after),
                       key=lambda k: (-after.get(k, 0), k)):
        was, now = before.get(pair, 0), after.get(pair, 0)
        arrow = f"{pair[0]:5} -> {pair[1]:5}"
        print(f"   {arrow}  {was:4} -> {now:4}"
              if was != now else f"   {arrow}  {now:4}")
    print(f"   total: {sum(before.values())} -> {sum(after.values())}")
    con.close()


if __name__ == "__main__":
    main()

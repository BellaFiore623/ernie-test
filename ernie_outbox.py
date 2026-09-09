"""
Ernie's outbox -- the only thing that posts to Discord.

Reads events whose undo window has expired and posts them in-thread. Every
call goes through Discord.write(), so the guild guard applies automatically:
nothing posts unless ALLOW_DISCORD_WRITES names the configured guild.

    python ernie_outbox.py --once --env ernie-test.env --db ernie-test.db
    python ernie_outbox.py --env ernie-test.env --db ernie-test.db

Needs two permissions the read-only bot doesn't have:
  Send Messages in Threads  -- to post
  Manage Threads            -- to unarchive first, since you can't post into
                               an archived thread
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import ernie_changelog
import ernie_load as load
import ernie_state
import ernie_status
import ernie_version
from ernie_sync import Discord, GuildMismatch, load_env

POLL_SECONDS = 30
MAX_ATTEMPTS = 5
# Matches ernie_api.UNDO_WINDOW_S. Duplicated rather than imported, because
# importing the API here would pull FastAPI into the outbox for one integer --
# the same trade MAX_ATTEMPTS already makes. Only make_threads needs it, for a
# ticket closed before its thread existed: every other event arrives with a
# dispatch_after the API has already worked out. tests/check_state.py holds
# the two together.
UNDO_WINDOW_S = 60
CLAIM_STALE_S = 300    # a claim older than this belonged to a process that died


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Message text
# --------------------------------------------------------------------------

def describe(e) -> str:
    """
    What an event did, phrased to follow "undid".

    Named rather than pointed at, because "the last message" stops being true
    the moment anything else is posted in the thread -- another person's
    update, or Ernie's own next one.
    """
    verb, old, new = e["verb"], e["old_value"], e["new_value"]
    if verb == "completed":
        return "marking this complete"
    if verb == "priority_changed":
        if new == "critical":
            return "making this critical"
        if old == "critical":
            return f"taking this out of critical (to {new or 'unassigned'})"
        return f"the move to {new}"
    if verb == "work_done":
        return f"finishing “{new}”" if new else "finishing a work item"
    if verb == "edited":
        return f"the edit — {new}" if new else "the edit"
    if verb == "renamed":
        return "the rename"
    return "the previous update"


def render(event, original=None) -> str | None:
    """
    Turn an event row into thread text. Returning None means 'nothing to say'
    -- the event still gets marked posted so it doesn't retry forever.
    """
    who = event["actor_name"] or "Someone"
    verb = event["verb"]

    if verb == "completed":
        return f"**{who}** marked this complete in Bert."
    if verb in ("reopened", "thread_reopened"):
        if verb == "thread_reopened":
            return "This thread was reopened, so it's back on the Bert board."
        return f"**{who}** reopened this in Bert."
    if verb == "edited":
        # new_value already holds a rendered summary of every field that moved,
        # so a four-field edit is still one message.
        return f"**{who}** updated this in Bert \u2014 {event['new_value']}"
    if verb == "priority_changed":
        # Only queued for critical in either direction; see move_card. Say
        # which way it went, because "priority changed" on its own tells the
        # thread nothing it can act on.
        old, new = event["old_value"], event["new_value"]
        if new == "critical":
            return f"**{who}** made this **critical** in Bert."
        if old == "critical":
            return (f"**{who}** took this out of critical in Bert "
                    f"— it's {new or 'unassigned'} now.")
        return None
    if verb == "work_done":
        # new_value is the item's text, so the thread reads as a statement
        # about the work rather than about the board.
        return f"**{who}** finished: {event['new_value']}"
    if verb == "undo_correction":
        what = describe(original) if original else "the previous update"
        return f"Correction: **{who}** undid {what}."
    return None


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

# Verbs that should leave the thread archived once the message is posted, and
# verbs that should leave it open.
ARCHIVES = {"completed"}
UNARCHIVES = {"reopened", "undo_correction"}


def post_one(con, d: Discord, event) -> str:
    """
    Post a single event, then set the thread's archive state to match.

    Posting and archiving happen together, after the undo window. That way an
    undo inside the window means neither ever happened -- no message to
    retract, no thread to un-hide.
    """
    tid = event["thread_id"]
    verb = event["verb"]

    # Claim the row before saying anything to Discord. Posting first and
    # marking afterwards left a window where an undo saw posted_at still NULL,
    # decided nothing had gone out, and skipped the correction -- while the
    # message was already on its way. The claim also keeps two outbox processes
    # from both posting the same event.
    claimed = con.execute(
        """UPDATE events SET claimed_at=? WHERE event_id=?
           AND claimed_at IS NULL AND posted_at IS NULL AND undone_at IS NULL""",
        (now(), event["event_id"]))
    con.commit()
    if claimed.rowcount != 1:
        return "skipped"          # undone, or another worker got there first

    # A correction names the event it retracts, so it can say what was undone
    # and reply straight to the message that said it -- which stays right
    # however many messages land in between.
    original = reply_to = None
    if verb == "undo_correction" and event["new_value"]:
        original = con.execute("SELECT * FROM events WHERE event_id=?",
                               (event["new_value"],)).fetchone()
        if original:
            reply_to = original["discord_message_id"]

    text = render(event, original)
    # A rename is an action, not an announcement: Discord posts its own system
    # message when a thread name changes, so render() stays quiet for it.
    rename_to = event["new_value"] if verb == "renamed" else None

    if not text and not rename_to:
        con.execute("UPDATE events SET posted_at=? WHERE event_id=?",
                    (now(), event["event_id"]))
        return "skipped"

    try:
        # You can't post into an archived thread, so open it first regardless
        # of where it should end up.
        row = con.execute("SELECT archived FROM threads WHERE thread_id=?",
                          (tid,)).fetchone()
        if row and row["archived"]:
            d.write("PATCH", f"/channels/{tid}", archived=False)
            con.execute("UPDATE threads SET archived=0 WHERE thread_id=?", (tid,))

        if rename_to:
            d.write("PATCH", f"/channels/{tid}", name=rename_to)

        msg = {}
        if text:
            body = {"content": text}
            if reply_to:
                # fail_if_not_exists lets it post as a plain message if the
                # one it points at has been deleted, rather than 400ing and
                # retrying until it gives up.
                body["message_reference"] = {"message_id": reply_to,
                                             "fail_if_not_exists": False}
            msg = d.write("POST", f"/channels/{tid}/messages", **body)

        # Now put it where it belongs.
        if verb in ARCHIVES:
            d.write("PATCH", f"/channels/{tid}", archived=True)
            con.execute(
                "UPDATE threads SET archived=1, archived_by_ernie=1 WHERE thread_id=?",
                (tid,))
        elif verb in UNARCHIVES:
            con.execute(
                "UPDATE threads SET archived=0, archived_by_ernie=0 WHERE thread_id=?",
                (tid,))

        con.execute(
            "UPDATE events SET posted_at=?, discord_message_id=? WHERE event_id=?",
            (now(), msg.get("id"), event["event_id"]))
        return "sent"

    except GuildMismatch:
        raise                                  # config problem, not a bad row
    except Exception as e:
        # Release the claim so the row is eligible again on the next pass.
        con.execute(
            """UPDATE events SET claimed_at=NULL, attempts=attempts+1,
                                  last_error=? WHERE event_id=?""",
            (str(e)[:300], event["event_id"]))
        return "failed"


def release_stale_claims(con) -> int:
    """Free rows held by a worker that died mid-post."""
    cur = con.execute(
        """UPDATE events SET claimed_at=NULL
           WHERE claimed_at IS NOT NULL AND posted_at IS NULL
             AND datetime(claimed_at) < datetime('now', ?)""",
        (f"-{CLAIM_STALE_S} seconds",))
    con.commit()
    return cur.rowcount


def drain(con, d: Discord) -> dict:
    """Post everything that's due. Rows past MAX_ATTEMPTS are left alone."""
    release_stale_claims(con)
    due = con.execute("SELECT * FROM v_outbox_due").fetchall()
    counts = {"sent": 0, "skipped": 0, "failed": 0}

    for event in due:
        counts[post_one(con, d, event)] += 1
        con.commit()

    return counts


def make_threads(con, d: Discord) -> dict:
    """Create the Discord threads for tickets started in Bert.

    Bert cannot do this and neither can the API -- every write to Discord goes
    through Discord.write, which is here. Until this runs the ticket is a row
    in new_threads and a card on the board wearing the unsent mark.

    Three writes, in an order chosen so a failure leaves something legible:
    the thread, a note saying who started it, then their opening message if
    they wrote one. The note comes from Bert's own settings and is plain text,
    the same way every other name this posts is -- Ernie has no idea which
    Discord account belongs to which person, so the thread is opened by the
    bot and says whose it is.
    """
    counts = {"made": 0, "failed": 0}
    due = con.execute(
        "SELECT * FROM new_threads WHERE posted_at IS NULL AND attempts < ? "
        "ORDER BY created_at", (MAX_ATTEMPTS,)).fetchall()

    for row in due:
        try:
            made = d.write("POST", f"/channels/{row['channel_id']}/threads",
                           name=row["title"][:100], type=11,
                           auto_archive_duration=10080)
            tid = made["id"]

            who = (row["actor"] or "").strip()
            if who:
                d.write("POST", f"/channels/{tid}/messages",
                        content=f"Thread started by {who} from the board.")
            if row["first_message"]:
                d.write("POST", f"/channels/{tid}/messages",
                        content=row["first_message"])

            # Recorded here rather than waiting for the sync to notice it: the
            # card would otherwise arrive a cycle later and land in unassigned,
            # losing the band somebody chose by pressing the + in it. The sync
            # reconciles both rows on its next pass anyway -- they are written
            # the way it writes them.
            ts = now()
            con.execute(
                """INSERT OR IGNORE INTO threads (thread_id, parent_id, guild_id,
                                                  created_at, first_seen_at,
                                                  last_synced_at)
                   VALUES (?,?,?,?,?,?)""",
                (tid, row["channel_id"], d.guild_id, ts, ts, ts))
            # Parsed, through the one writer the sync uses. Writing just the
            # name left queue and client NULL until a sync cycle filled them
            # in, so a ticket whose title reads perfectly well came up grey
            # with "unknown client" the moment its thread existed.
            load.record_title(con, tid, row["title"], ts)
            # Where the board has been showing it. rank is the order and the
            # only one, so it has to say what the board says -- MAX + a step
            # put the card at the bottom of the band, and a ticket somebody
            # had just written slid away from them as soon as it became real.
            #
            # The draft carries its own rank now, because it can be dragged
            # while it waits: recomputing the band's edge here would take a
            # ticket somebody had moved down into Medium and put it back at
            # the top. A row written before that column existed has none, and
            # falls back to the edge it would have been given.
            rank = row["rank"]
            if rank is None:
                edge = con.execute(
                    "SELECT MIN(rank) AS m FROM cards WHERE priority=?",
                    (row["priority"],)).fetchone()
                rank = (load.RANK_STEP if edge["m"] is None
                        else edge["m"]) - load.RANK_STEP
            con.execute(
                """INSERT OR IGNORE INTO cards (thread_id, priority, rank,
                                                updated_at)
                   VALUES (?,?,?,?)""", (tid, row["priority"], rank, ts))
            for n, body in enumerate(json.loads(row["work_json"] or "[]")):
                con.execute(
                    """INSERT INTO work_items (item_id, thread_id, body, position,
                                               created_at, created_by)
                       VALUES (?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), tid, body, float(n + 1), ts, row["actor"]))

            if row["complete_on_arrival"]:
                # Closed while it was still a draft. Done here rather than
                # left to Bert, which has nothing to press by then -- the card
                # left the board when the button was pressed.
                who = row["completed_by"] or row["actor"]
                con.execute(
                    "UPDATE cards SET completed_at=?, completed_by=?, "
                    "updated_at=? WHERE thread_id=?", (ts, who, ts, tid))
                # With a dispatch, so the thread says it closed the same way
                # every other closure does. It is undoable from the feed from
                # here on, which is the first moment there is anything to undo.
                con.execute(
                    """INSERT INTO events (event_id, occurred_at, actor_name,
                                           thread_id, verb, dispatch_after)
                       VALUES (?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), ts, who, tid, "completed",
                     (datetime.now(timezone.utc)
                      + timedelta(seconds=UNDO_WINDOW_S)).isoformat()))
            con.execute("UPDATE new_threads SET thread_id=?, posted_at=? "
                        "WHERE draft_id=?", (tid, ts, row["draft_id"]))
            counts["made"] += 1
        except Exception as e:
            con.execute(
                "UPDATE new_threads SET attempts=attempts+1, last_error=? "
                "WHERE draft_id=?", (str(e)[:500], row["draft_id"]))
            counts["failed"] += 1
        con.commit()

    return counts


def pending(con) -> int:
    """Events still inside their undo window."""
    return con.execute(
        """SELECT COUNT(*) FROM events
           WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
             AND undone_at IS NULL AND dispatch_after > datetime('now')"""
    ).fetchone()[0]


def stuck(con) -> list:
    return con.execute(
        """SELECT event_id, verb, attempts, last_error FROM events
           WHERE posted_at IS NULL AND undone_at IS NULL AND attempts >= ?""",
        (MAX_ATTEMPTS,)).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version",
                    version=ernie_version.describe())
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--env", default="ernie.env")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be posted, send nothing")
    a = ap.parse_args()

    load_env(a.env)
    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("DISCORD_GUILD_ID")
    allow = os.environ.get("ALLOW_DISCORD_WRITES")
    if not token or not guild:
        sys.exit("DISCORD_TOKEN and DISCORD_GUILD_ID must be set")

    con = load.connect(a.db)

    if a.dry_run:
        due = con.execute("SELECT * FROM v_outbox_due").fetchall()
        print(f"{len(due)} due, {pending(con)} still in the undo window\n")
        for e in due:
            print(f"  -> thread {e['thread_id']}")
            print(f"     {render(e)}\n")
        return

    d = Discord(token, guild, allow_writes_for=allow)
    who = d.whoami()
    if not who["writes"]:
        sys.exit(f"Writes are blocked for guild {guild}. Set "
                 f"ALLOW_DISCORD_WRITES={guild} in {a.env} to enable posting.")
    print(f"ernie_outbox {ernie_version.describe()}")
    print(f"{who['bot']} -> {who['guild']} ({guild})  [POSTING]  db={a.db}")

    while True:
        try:
            c = drain(con, d)
            m = make_threads(con, d)
            if m["made"] or m["failed"]:
                print(f"[{now()[:19]}] new threads: {m['made']} made, "
                      f"{m['failed']} failed")
            if any(c.values()):
                print(f"[{now()[:19]}] sent={c['sent']} skipped={c['skipped']} "
                      f"failed={c['failed']} waiting={pending(con)}")
            for s in stuck(con):
                print(f"  STUCK {s['event_id'][:8]} {s['verb']} "
                      f"after {s['attempts']} tries: {s['last_error']}",
                      file=sys.stderr)
        except GuildMismatch as e:
            sys.exit(str(e))
        except Exception as e:
            print(f"[{now()[:19]}] drain failed: {e}", file=sys.stderr)

        # SQLite -> Discord, so it goes through the one process allowed to
        # write there. The pull in the other direction rides with the sync.
        state_channel = os.environ.get("STATE_CHANNEL_ID")
        if state_channel and d.writes_allowed:
            try:
                s = ernie_state.publish(d, state_channel, a.db)
                if s["posted"] or s["edited"]:
                    print(f"[{now()[:19]}] state: posted {s['posted']}, "
                          f"edited {s['edited']}")
            except Exception as e:
                print(f"[{now()[:19]}] state publish failed: {e}",
                      file=sys.stderr)

        # The ticket's own status, in its own thread. No channel to
        # configure: it goes to the threads the board already knows about,
        # and only the ones Ernie watched open -- so a machine that has just
        # inherited a server posts nothing.
        if d.writes_allowed:
            try:
                st = ernie_status.publish(d, con, a.db)
                if st["posted"] or st["edited"]:
                    print(f"[{now()[:19]}] status: posted {st['posted']}, "
                          f"edited {st['edited']}")
            except Exception as e:
                print(f"[{now()[:19]}] status failed: {e}", file=sys.stderr)

        # The durable record, if there is somewhere to keep it. Both boards
        # hold the whole history, so only one machine should set this -- two
        # would write every line twice.
        log_channel = os.environ.get("CHANGELOG_CHANNEL_ID")
        if log_channel and d.writes_allowed:
            try:
                c = ernie_changelog.tick(d, log_channel, con)
                if c["sent"] or c.get("struck"):
                    note = f"{c['sent']} logged"
                    if c.get("struck"):
                        note += f", {c['struck']} struck through"
                    print(f"[{now()[:19]}] changelog: {note}")
            except Exception as e:
                print(f"[{now()[:19]}] changelog failed: {e}", file=sys.stderr)

        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()

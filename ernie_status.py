"""
The ticket's status, kept as a message in its own thread.

The board knows what a ticket still needs; the thread is where the work is
actually discussed, and until now the two only met by somebody opening Bert.
This puts the answer where the conversation is: what is still to do, what has
been done, which band it sits in, and when it last moved.

Three things shape it.

**One message per thread, edited in place.** The same design as
`#ernie-state`, for the same measured reason: an edit recovers from its rate
limit in 0.67s and notifies nobody, while a thread rename allows two per ten
minutes and posts a system line into the thread every time. So the status can
follow every tick of a work item without the thread becoming a notification
feed. `thread_status.body` holds what was last written and a pass rewrites
only when the rendering would differ -- otherwise a quiet board would edit
every message every cycle for nothing.

**New threads only.** `witnessed_start()` asks whether Ernie saw the thread
appear rather than inheriting it, which is the same predicate the `started`
feed line uses. Without it the first sync of an existing server -- 889 threads
in production -- would post into every one of them at once.

**Nothing in it moves on its own.** The timestamp is Discord's own `<t:...:R>`
markup, so the reader's client renders "2 hours ago" and keeps it current
while the source text stays fixed at the moment the card changed. A written-out
"2h ago" would differ on every pass and rewrite the message forever, which is
the trap `ernie_state.without_stamp()` exists to work around.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import ernie_load as load
import ernie_state
import ernie_version
from ernie_state import discord_time
from ernie_sync import Discord, load_env

POLL_SECONDS = 30

# What a band is called to somebody reading a thread rather than the board.
# Not imported from bert.py: that pulls in PySide6, and the outbox runs where
# there is no display.
BAND_LABEL = {
    "unassigned": "Needs attention",
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- the message ------------------------------------------------------------

def render(card: ernie_state.Card) -> str:
    """What the status message says.

    Deliberately not the ticket's name: that is the thread's own name, shown
    directly above this in every client, and repeating it puts the same string
    on screen twice within an inch.
    """
    if card.completed:
        head = "**Ticket status** · closed"
        if card.completed_by:
            head += f" by {card.completed_by}"
    else:
        head = f"**Ticket status** · {BAND_LABEL.get(card.priority, card.priority)}"

    lines = [head]

    todo = [i for i in card.items if not i.done]
    done = [i for i in card.items if i.done]
    if not card.items:
        # Said outright rather than left off. Two thirds of open tickets carry
        # work items and a third do not, and a message that simply stops after
        # the band reads as though it failed to load.
        lines.append("No work items yet.")
    else:
        if todo:
            lines.append("**To do** — " + " · ".join(i.body for i in todo))
        if done:
            # Struck through, which is what a finished item already looks like
            # in the state channel and in the change log.
            lines.append("**Done** — "
                         + " · ".join("~~" + i.body + "~~" for i in done))
        if not todo and not card.completed:
            # Only worth saying while the ticket is still open. On a closed
            # one the header has already said it, and twice reads as padding.
            lines.append("Nothing left to do.")

    if card.updated_at:
        foot = f"Last updated {discord_time(card.updated_at)}"
        if card.actor:
            foot += f" by {card.actor}"
        lines.append(foot)

    return "\n".join(lines)


# -- what needs one ---------------------------------------------------------

def wanted(con, cards: list[ernie_state.Card]) -> list[ernie_state.Card]:
    """The cards whose threads should carry a status message.

    Two filters, and both are about not shouting into threads that are not
    ours to shout into. A thread Ernie merely inherited on a first sync gets
    nothing, ever. An archived one is skipped because Discord refuses a post
    to it -- and unarchiving to say "closed" would drag a finished ticket back
    into everybody's sidebar.
    """
    out = []
    for card in cards:
        if not load.witnessed_start(con, card.thread_id):
            continue
        r = con.execute("SELECT archived FROM threads WHERE thread_id=?",
                        (card.thread_id,)).fetchone()
        if r is None or r["archived"]:
            continue
        out.append(card)
    return out


def stored(con) -> dict:
    return {r["thread_id"]: r for r in
            con.execute("SELECT * FROM thread_status")}


def publish(d: Discord, con, db: str) -> dict:
    """One pass: post the missing ones, edit the ones that would read differently."""
    counts = {"posted": 0, "edited": 0, "failed": 0}
    have = stored(con)

    for card in wanted(con, ernie_state.load_board(db)):
        body = render(card)
        was = have.get(card.thread_id)
        if was is not None and was["body"] == body:
            continue
        try:
            if was is None:
                sent = d.write("POST", f"/channels/{card.thread_id}/messages",
                               content=body)
                con.execute(
                    """INSERT INTO thread_status (thread_id, message_id, body,
                                                  sent_at, pinned)
                       VALUES (?,?,?,?,0)""",
                    (card.thread_id, sent["id"], body, now()))
                counts["posted"] += 1
            else:
                d.write("PATCH",
                        f"/channels/{card.thread_id}/messages/{was['message_id']}",
                        content=body)
                con.execute(
                    """UPDATE thread_status SET body=?, sent_at=?
                        WHERE thread_id=?""", (body, now(), card.thread_id))
                counts["edited"] += 1
            con.commit()
        except Exception as e:
            counts["failed"] += 1
            print(f"  status: {card.thread_id} failed -- {e}", file=sys.stderr)

    counts["pinned"] = pin_pending(d, con)
    return counts


def pin_pending(d: Discord, con) -> int:
    """Pin the messages that are not pinned yet, and keep trying.

    "The first message" is what was wanted, and a bot cannot be the first
    message of a thread somebody else opened -- a pin is one click from the
    thread header, which is the same thing to a reader.

    It is tried on every pass rather than once at posting time, because it
    needs Manage Messages and the bot may not have it: measured against the
    sandbox, all 29 pins came back 403 while every message posted fine. Left
    at one attempt, granting the permission afterwards would have changed
    nothing. Never fatal -- a thread at the 50-pin cap still gets its status.
    """
    done = 0
    for r in con.execute("SELECT * FROM thread_status WHERE pinned = 0"):
        try:
            d.write("PUT", f"/channels/{r['thread_id']}/pins/{r['message_id']}")
        except Exception as e:
            print(f"  status: couldn't pin in {r['thread_id']}: "
                  f"{str(e).splitlines()[0]}", file=sys.stderr)
            continue
        con.execute("UPDATE thread_status SET pinned=1 WHERE thread_id=?",
                    (r["thread_id"],))
        con.commit()
        done += 1
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version",
                    version=ernie_version.describe())
    ap.add_argument("--env", default="ernie-test.env")
    ap.add_argument("--db", default="ernie-test.db")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what each thread's message would say, post nothing")
    a = ap.parse_args()

    load_env(a.env)
    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild:
        sys.exit("DISCORD_TOKEN and DISCORD_GUILD_ID must be set")
    con = load.connect(a.db)

    if a.dry_run:
        cards = ernie_state.load_board(a.db)
        keep = wanted(con, cards)
        print(f"{len(keep)} of {len(cards)} cards would carry a status message"
              f" ({len(cards) - len(keep)} inherited or archived)\n")
        for card in keep:
            print(f"-- {card.thread_id}")
            for line in render(card).splitlines():
                print(f"   {line}")
            print()
        return

    d = Discord(token, guild,
                allow_writes_for=os.environ.get("ALLOW_DISCORD_WRITES"))
    print(f"ernie_status {ernie_version.describe()}")
    counts = publish(d, con, a.db)
    print(f"posted {counts['posted']}, edited {counts['edited']}, "
          f"pinned {counts['pinned']}, failed {counts['failed']}")


if __name__ == "__main__":
    main()

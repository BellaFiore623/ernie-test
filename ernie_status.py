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
import json
import os
import sys
from datetime import datetime, timezone

import ernie_load as load
import ernie_state
import ernie_version
from ernie_state import discord_time
from ernie_sync import Discord, load_env

POLL_SECONDS = 30

# How many threads a backfill pass may speak into. The same shape as
# ANNOUNCE_MAX, PUBLISH_MAX and CLOSURE_CHECKS, and for the same reason: the
# cost is not the API calls, it is 37 notifications at once. Board order, and
# a thread that has one is skipped, so the budget walks down the board.
BACKFILL_MAX = 3

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

# The bar down the side of the embed, so a thread says which band it is in
# before a word of it is read. Copied from bert's DARK `band_text` rather than
# chosen here: inventing a colour would put the board and the thread out of
# step, and this file cannot import bert -- that pulls in PySide6, and the
# outbox runs where there is no display. `tests/check_status.py` holds the two
# together, the way check_palette.py holds the two themes together.
BAND_COLOUR = {
    "unassigned": 0xF5AAA2,
    "critical": 0xF5AAA2,
    "high": 0xEFC15E,
    "medium": 0xA3C8F0,
    "low": 0xA8B2BD,
}
CLOSED_COLOUR = 0xA8DC8B      # T.OK_FG: done is green everywhere else
FIELD_MAX = 1024              # Discord's cap on a field value


def backfilling() -> bool:
    """Whether this machine may give an inherited thread a status message.

    Off unless `STATUS_BACKFILL` is set, because a first sync inherits every
    thread there has ever been and posting into all of those is not a thing to
    do to a channel people are working in. What makes it worth switching on is
    that `wanted()` skips archived threads separately, so the set this reaches
    is the *open* ones -- 37 of production's 927 on 2026-09-17, against a
    board carrying a status on 2 cards of 39.

    Safe on more than one machine, unlike the change log and the closure
    announcements: `adopt()` looks in the thread before posting.
    """
    return (os.environ.get("STATUS_BACKFILL", "").strip().lower()
            in ("1", "true", "yes", "on"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- the message ------------------------------------------------------------

def clip(value: str) -> str:
    """Discord refuses a field value over 1024 characters."""
    return value if len(value) <= FIELD_MAX else value[:FIELD_MAX - 1] + "\u2026"


# Every status embed's title starts with this, and always has. It is what
# `adopt()` recognises one by, which is why it is a constant rather than
# written into the two f-strings below: a title that stopped starting with it
# would silently stop being findable, and every board would post its own again.
STATUS_TITLE = "Ticket status"


def render(card: ernie_state.Card, facts: dict | None = None) -> dict:
    """The status, as an embed.

    An embed rather than a message of text, for one thing text cannot do: the
    bar down its side carries the band, so the thread says how urgent it is
    before a word is read. These threads are already embed-shaped -- the
    ticket bot posts one per build and return -- so it reads as native rather
    than as a bot shouting.

    Deliberately not the ticket's name: that is the thread's own name, shown
    directly above this in every client, and repeating it puts the same string
    on screen twice within an inch.
    """
    if card.completed:
        title = "Ticket status \u00b7 closed"
        if card.completed_by:
            title += f" by {card.completed_by}"
        colour = CLOSED_COLOUR
    else:
        band = BAND_LABEL.get(card.priority, card.priority)
        title = f"Ticket status \u00b7 {band}"
        colour = BAND_COLOUR.get(card.priority, BAND_COLOUR["low"])

    embed = {"title": title, "color": colour, "fields": []}

    todo = [i for i in card.items if not i.done]
    done = [i for i in card.items if i.done]
    if not card.items:
        # Said outright rather than left off. A third of open tickets carry no
        # work items, and an embed that stops at its title reads as one that
        # failed to load.
        embed["description"] = "No work items yet."
    elif not todo and not card.completed:
        embed["description"] = "Nothing left to do."

    # Side by side on a desktop, stacked on a phone, which is what inline
    # means -- and one line per item, because a run of them separated by dots
    # stops being a list you can count.
    if todo:
        embed["fields"].append(
            {"name": "To do", "inline": True,
             "value": clip("\n".join(i.body for i in todo))})
    if done:
        # Struck through, which is what a finished item already looks like in
        # the state channel and in the change log.
        embed["fields"].append(
            {"name": "Done", "inline": True,
             "value": clip("\n".join("~~" + i.body + "~~" for i in done))})

    # What the thread is about, for the two thirds of threads that have a
    # ticket behind them and the third that do not. Inline, so they pack into
    # a row rather than running down the message, and after the work: what is
    # left to do is the question being asked, and this is the answer to
    # "which job is this".
    facts = facts or {}
    for name, value in (("Equipment", ", ".join(facts.get("equipment", []))),
                        ("Ticket", ", ".join(facts.get("tickets", []))),
                        ("Client CR", facts.get("client_cr") or ""),
                        ("Assignee", ", ".join(
                            dict.fromkeys(facts.get("assignees", []))))):
        if value:
            embed["fields"].append(
                {"name": name, "inline": True, "value": clip(value)})

    if card.updated_at:
        # Its own field rather than the footer: Discord renders <t:...:R> in a
        # description or a field value and *not* in footer text, and the
        # relative form is what keeps the stored body still while the reader's
        # clock moves.
        value = discord_time(card.updated_at)
        if card.actor:
            value += f" by {card.actor}"
        embed["fields"].append(
            {"name": "Last updated", "inline": False, "value": value})

    return embed


def ticket_facts(con) -> dict:
    """What Ernie knows about each thread besides its work, by thread_id.

    Only 230 of 889 production threads carry a Build Request embed -- those
    come from Python-Interface-Bot and Ernie has never posted one, nor should
    it: an embed that looks like a ticket with no PIP key behind it is worse
    than no embed. But the facts it parses out of the ones that exist can go
    in the status message, which *is* on every thread, so a reader has one
    place to look whether or not a ticket was ever raised.

    Nothing is invented. A thread with none of this gets none of these lines.
    """
    out = {}
    for r in con.execute(
            """SELECT thread_id, pip_key, kind, assignee, client_cr
               FROM tickets ORDER BY created_at"""):
        f = out.setdefault(r["thread_id"], {})
        f.setdefault("tickets", []).append(
            f"{r['pip_key']} ({r['kind']})" if r["kind"] else r["pip_key"])
        if r["assignee"]:
            f.setdefault("assignees", []).append(r["assignee"])
        if r["client_cr"]:
            f["client_cr"] = r["client_cr"]

    # The readable form, not the PIP key the ticket carries: EReel-1085 is
    # what the job is called out loud. Pending ones are left out -- a "####"
    # is the parser saying it could not read a number, and repeating that in
    # every thread is noise rather than news.
    for r in con.execute(
            "SELECT thread_id, raw FROM thread_equipment "
            "WHERE state = 'resolved' ORDER BY raw"):
        out.setdefault(r["thread_id"], {}).setdefault(
            "equipment", []).append(r["raw"])

    # A client CR is a key; the roster knows what it is called.
    names = {r["client_id"]: (r["short_name"] or r["name"]) for r in
             con.execute("SELECT client_id, name, short_name FROM clients")}
    for f in out.values():
        cr = f.get("client_cr")
        if cr and names.get(cr):
            f["client_cr"] = f"{cr} ({names[cr]})"
    return out


def as_body(embed: dict) -> str:
    """The embed as one string, for asking whether it would read differently.

    Sorted, so two renderings of the same card compare equal whatever order
    the keys happened to be built in.
    """
    return json.dumps(embed, sort_keys=True)


# -- what needs one ---------------------------------------------------------

def wanted(con, cards: list[ernie_state.Card], backfill: int = 0,
           have=()) -> tuple[list[ernie_state.Card], int]:
    """The cards whose threads should carry a status message, and how many
    inherited ones are still waiting.

    An archived thread is skipped absolutely: Discord refuses a post to one,
    and unarchiving to say "closed" would drag a finished ticket back into
    everybody's sidebar.

    An inherited thread gets nothing unless a backfill is switched on, and
    then only `backfill` a pass. The budget counts threads being spoken into
    for the first time and nothing else, or it would be filled every pass by
    work already done and never reach the rest. `left` is what is still
    without a message, so three of thirty-seven does not read as finished.
    """
    out, room, left = [], backfill, 0
    for card in cards:
        r = con.execute("SELECT archived FROM threads WHERE thread_id=?",
                        (card.thread_id,)).fetchone()
        if r is None or r["archived"]:
            continue
        if load.witnessed_start(con, card.thread_id) or card.thread_id in have:
            out.append(card)
            continue
        # Inherited, and nothing has been said in it yet.
        if room:
            out.append(card)
            room -= 1
        else:
            left += 1
    return out, left


def stored(con) -> dict:
    return {r["thread_id"]: r for r in
            con.execute("SELECT * FROM thread_status")}


def adopt(d: Discord, tid: str) -> str | None:
    """A status message already in this thread, whoever put it there.

    **`thread_status` is per database, and the thread is not.** Every board
    keeps its own `message_id` and had no way to learn about anybody else's,
    so a board with no row posted a fresh one -- and N boards meant N status
    embeds in one customer thread, each edited by the board that made it and
    ignored by the rest. Measured on the sandbox after a weekend of two
    databases on one machine: 10 of 34 threads carrying two or three, and
    four belonging to no database that still existed.

    `#ernie-state` never had this problem because **its messages are
    self-describing** -- each names its own `thread_id`, so a second board
    finds the existing one and edits it. This is that idea, one channel
    along: the thread is the key, and the message is found by looking.

    **The title is the marker, and it always was.** Every status embed this
    has ever posted begins `Ticket status`, so adoption works on messages
    written long before this function existed -- which is the whole point,
    since the ones needing adoption are exactly the ones already out there.

    **The oldest wins**, so two boards adopting independently converge on one
    message rather than each taking a different duplicate.

    Costs one GET, and only for a thread this board has no row for. `wanted()`
    has already filtered to threads Ernie *witnessed*, so a fresh install --
    which inherits every thread and witnesses none -- asks for nothing at all.
    """
    got = d.get(f"/channels/{tid}/messages", limit=50)
    if not got:
        return None
    me = (d.get("/users/@me") or {}).get("id")
    if not me:
        return None
    ours = [m for m in got
            if (m.get("author") or {}).get("id") == me
            and any((e.get("title") or "").startswith(STATUS_TITLE)
                    for e in m.get("embeds") or [])]
    # Snowflakes sort chronologically, which is the only order Discord gives
    # and the only one needed here.
    return min((m["id"] for m in ours), key=int, default=None)


def publish(d: Discord, con, db: str) -> dict:
    """One pass: post the missing ones, edit the ones that would read differently."""
    counts = {"posted": 0, "adopted": 0, "edited": 0, "failed": 0, "left": 0}
    have = stored(con)

    cards, counts["left"] = wanted(
        con, ernie_state.load_board(db),
        backfill=BACKFILL_MAX if backfilling() else 0, have=have)

    facts = ticket_facts(con)
    for card in cards:
        embed = render(card, facts.get(card.thread_id))
        body = as_body(embed)
        was = have.get(card.thread_id)
        if was is not None and was["body"] == body:
            continue
        try:
            if was is None:
                # Look before posting. A thread that already carries one of
                # ours belongs to a board that got here first, and a second
                # message is the failure this exists to prevent.
                found = adopt(d, card.thread_id)
                if found:
                    con.execute(
                        """INSERT INTO thread_status (thread_id, message_id,
                                                      body, sent_at, pinned)
                           VALUES (?,?,'',?,0)""",
                        (card.thread_id, found, now()))
                    con.commit()
                    # Empty body, so the edit below writes our rendering into
                    # it on this same pass rather than a cycle later.
                    was = {"message_id": found}
                    counts["adopted"] += 1
                else:
                    sent = d.write("POST",
                                   f"/channels/{card.thread_id}/messages",
                                   embeds=[embed])
                    con.execute(
                        """INSERT INTO thread_status (thread_id, message_id,
                                                      body, sent_at, pinned)
                           VALUES (?,?,?,?,0)""",
                        (card.thread_id, sent["id"], body, now()))
                    counts["posted"] += 1

            if was is not None:
                # content="" as well as the embed, so a message written by
                # an earlier build as plain text loses the text rather than
                # carrying both.
                d.write("PATCH",
                        f"/channels/{card.thread_id}/messages/{was['message_id']}",
                        content="", embeds=[embed], retry_5xx=True)
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

    It is tried on every pass rather than once at posting time, because the
    bot may not be allowed to pin yet: measured against the sandbox, all 29
    pins came back 403 while every message posted fine. Left at one attempt,
    granting the permission afterwards would have changed nothing -- as it
    happened, granting it and running one more pass was the whole fix.

    The permission is **Pin Messages**, its own toggle on the bot's role, and
    *not* Manage Messages: the bot already had that one -- bit 13 was set in
    its effective permissions, with no overwrite on the channel or its
    category -- and Discord still answered 50013. Worth knowing before
    debugging this again, because every symptom points at Manage Messages.

    Never fatal: a thread at the 50-pin cap still gets its status.
    """
    done = 0
    # fetchall, not a lazy cursor. Iterating con.execute() directly holds a
    # read transaction open across the HTTP PUT below; the sync commits
    # underneath it, so the UPDATE becomes a stale-snapshot upgrade, which
    # SQLite refuses outright and busy_timeout cannot wait out. The refusal
    # leaves that transaction open, wedging every later write on the outbox's
    # one connection and pinning the WAL. Stalled production 2026-09-17.
    for r in con.execute(
            "SELECT * FROM thread_status WHERE pinned = 0").fetchall():
        try:
            # Pinning a pinned message is the same as pinning it once.
            d.write("PUT", f"/channels/{r['thread_id']}/pins/{r['message_id']}",
                    retry_5xx=True)
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
        # The real budget and stored rows, so this answers what the next pass
        # would do rather than what it could do unbounded.
        keep, left = wanted(con, cards,
                            backfill=BACKFILL_MAX if backfilling() else 0,
                            have=stored(con))
        facts = ticket_facts(con)
        print(f"{len(keep)} of {len(cards)} cards would carry a status message"
              f" ({len(cards) - len(keep) - left} archived or already current,"
              f" {left} inherited and waiting; backfill {'on' if backfilling() else 'off'})\n")
        for card in keep:
            print(f"-- {card.thread_id}")
            e = render(card, facts.get(card.thread_id))
            print(f"   [{e['color']:#08x}] {e['title']}")
            if e.get("description"):
                print(f"   {e['description']}")
            for f in e["fields"]:
                first, *rest = f["value"].splitlines() or [""]
                print(f"   {f['name']:>12} | {first}")
                for line in rest:
                    print(f"   {'':>12} | {line}")
            print()
        return

    d = Discord(token, guild,
                allow_writes_for=os.environ.get("ALLOW_DISCORD_WRITES"))
    print(f"ernie_status {ernie_version.describe()}")
    counts = publish(d, con, a.db)
    note = (f"posted {counts['posted']}, edited {counts['edited']}, "
            f"pinned {counts['pinned']}, failed {counts['failed']}")
    # Same reason the outbox line carries it.
    if counts.get("left"):
        note += f", {counts['left']} still to go"
    print(note)


if __name__ == "__main__":
    main()

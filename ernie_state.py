"""
Board state, kept in Discord instead of only in SQLite.

One message per card in a state channel, edited in place. Editing a message
posts nothing, announces nothing and recovers from its rate limit in under a
second; renaming a thread announces itself to everyone in it and allows two
renames per ten minutes. That measurement is the whole reason the order lives
here rather than in the thread title -- see the probes in the scratchpad.

Each message carries a human line so the channel is readable at a glance, and
a fenced JSON payload that is the actual state. The channel is
self-describing: every message names its own thread_id, so nothing local has
to remember which message belongs to which card.

    python ernie_state.py --env ernie-test.env --db ernie-test.db
    python ernie_state.py --demo          # publish, move a card, read it back

Test guild only, like everything else that writes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid

import httpx
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import ernie_load as load
from ernie_sync import Discord, load_env

PRODUCTION_GUILD = "1481003073894744226"    # the constant wipe_test.py guards on
STATE_CHANNEL = "ernie-state"
FORMAT_VERSION = 1
CONTENT_MAX = 2000          # Discord's cap on message content
SKEW_WARN_S = 120           # clock difference worth saying out loud
SUMMARY_MARK = "**Board**"  # first characters of the human summary message
SUMMARY_HEARTBEAT_S = 600   # how stale "last checked" may get before a rewrite
# The order Bert shows the bands in. The summary reads the same way round
# as the board it describes, or comparing the two is needless work.
BAND_ORDER = ("unassigned", "critical", "high", "medium", "low")
FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class Item:
    item_id: str
    body: str
    done: bool
    by: str | None = None       # who ticked it, or who added it if untouched


@dataclass
class Card:
    thread_id: str
    name: str
    priority: str
    rank: float
    items: list[Item] = field(default_factory=list)
    completed: bool = False
    completed_by: str | None = None
    # Who last changed this card here. Attribution belongs to the card, not to
    # whichever process happens to be publishing -- otherwise every change
    # arrives on the other board credited to Ernie.
    actor: str | None = None

    def payload(self) -> dict:
        """The half of the message that is state rather than decoration."""
        return {
            "v": FORMAT_VERSION,
            "thread": self.thread_id,
            "priority": self.priority,
            "rank": self.rank,
            "completed": self.completed,
            "work": [{"id": i.item_id, "body": i.body, "done": i.done,
                      "by": i.by}
                     for i in self.items],
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def discord_time(iso: str, style: str = 'R') -> str:
    """
    An ISO timestamp as Discord's own markup, which the reader's client
    renders in their timezone and keeps current by itself. A plain string
    would have to be rewritten for the "ago" to stay true.
    """
    try:
        return f'<t:{int(datetime.fromisoformat(iso).timestamp())}:{style}>'
    except (ValueError, TypeError):
        return 'at an unknown time'


# -- the message ------------------------------------------------------------

def work_summary(items: list[Item]) -> str:
    """The bubbles, ticked ones marked, for reading rather than parsing."""
    if not items:
        return ""
    return " · ".join(("~~" + i.body + "~~") if i.done else i.body
                      for i in items)


def render(card: Card, position: int, actor: str = "ernie") -> str:
    """
    Some lines for people, then the payload for Ernie.

    Everything above the fence is derived and never parsed back, so a person
    editing the prose in Discord cannot corrupt the board -- which is what
    lets it be as chatty as it needs to be to read well.
    """
    who = card.actor or actor
    head = (f"**✓ completed** — {card.name}" if card.completed
            else f"**{card.priority.upper()} #{position}** — {card.name}")
    lines = [head]
    work = work_summary(card.items)
    if work:
        done = sum(1 for i in card.items if i.done)
        lines.append(f"_{done}/{len(card.items)} done_ — {work}")
    lines.append(f"_last touched by {who}_")
    body = json.dumps(dict(card.payload(), by=who, at=now_iso()),
                      ensure_ascii=False)
    return "\n".join(lines) + f"\n```json\n{body}\n```"


def parse(content: str) -> dict | None:
    """Pull the payload back out of a message, or None if it isn't one of ours."""
    m = FENCE.search(content or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return d if d.get("thread") else None


def prose_of(content: str) -> str:
    """The human half of a card message, without the fenced payload."""
    return FENCE.sub("", content or "").strip()


def state_only(p: dict) -> dict:
    """The payload without who touched it or when -- just the board state."""
    return {k: v for k, v in p.items() if k not in ("by", "at")}


def same_state(a: dict, b: dict) -> bool:
    """
    Ignoring who touched it and when.

    Without this every publish rewrites every message, which burns the edit
    budget and fills the channel with edits that changed nothing.
    """
    return state_only(a) == state_only(b)


def sane_time(at: str | None) -> str:
    """
    A remote timestamp to file an event under, clamped to now.

    The other machine's clock is not this one's. A card stamped an hour into
    the future would sit at the top of the activity feed until the clock
    caught up, above things that genuinely happened since.
    """
    here = now_iso()
    return here if not at or at > here else at


# -- the local board --------------------------------------------------------

def load_board(db: str) -> list[Card]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cards = []
        # Completed cards come too, or closing one would leave its message in
        # the channel still claiming a priority for a card nobody can see.
        for r in con.execute(
                """SELECT c.thread_id, c.priority, c.rank, c.completed_at,
                          c.completed_by, v.name,
                          (SELECT e.actor_name FROM events e
                            WHERE e.thread_id = c.thread_id
                              AND e.undone_at IS NULL AND e.actor_name IS NOT NULL
                            ORDER BY e.occurred_at DESC LIMIT 1) AS last_actor
                   FROM cards c
                   LEFT JOIN v_thread_current v ON v.thread_id = c.thread_id
                   ORDER BY c.priority, c.rank"""):
            items = [
                Item(i["item_id"], i["body"], bool(i["done_at"]),
                     i["done_by"] if i["done_at"] else i["created_by"])
                for i in con.execute(
                    """SELECT item_id, body, done_at, done_by, created_by
                       FROM work_items
                       WHERE thread_id=? AND removed_at IS NULL
                       ORDER BY position""", (r["thread_id"],))
            ]
            cards.append(Card(r["thread_id"], r["name"] or "(untitled)",
                              r["priority"], r["rank"], items,
                              completed=bool(r["completed_at"]),
                              completed_by=r["completed_by"],
                              actor=r["last_actor"]))
        return cards
    finally:
        con.close()


def positions(cards: list[Card]) -> dict[str, int]:
    """
    Ordinal within its band, which is what the human line shows.

    Closed cards are not in the running order, so they don't take a number
    and don't push the cards below them down one.
    """
    out, seen = {}, {}
    for c in sorted(cards, key=lambda c: (c.priority, c.rank)):
        if c.completed:
            out[c.thread_id] = 0
            continue
        seen[c.priority] = seen.get(c.priority, 0) + 1
        out[c.thread_id] = seen[c.priority]
    return out


# -- the channel ------------------------------------------------------------

def ensure_channel(d: Discord, guild: str, want: str) -> str:
    """
    Find the state channel by name or id, and make it if it isn't there.

    Creating one needs Manage Channels, which the bot doesn't have in the
    sandbox -- so say what to do about it rather than dying on a 403.
    """
    if want.isdigit():
        # An id addresses the channel directly, which also works for one the
        # guild listing doesn't return.
        ch = d.get(f"/channels/{want}")
        if not ch:
            sys.exit(f"Can't see channel {want}. The bot needs View Channel "
                     f"and Read Message History on it.")
        if ch.get("guild_id") != guild:
            sys.exit(f"Channel {want} is in guild {ch.get('guild_id')}, "
                     f"not {guild}. Refusing.")
        return ch["id"]

    channels = d.get(f"/guilds/{guild}/channels") or []
    for ch in channels:
        if ch.get("name") == want and ch.get("type") == 0:
            return ch["id"]
    try:
        made = d.write("POST", f"/guilds/{guild}/channels", name=want, type=0,
                       topic="Ernie's board state. Machine-written; edited "
                             "in place, never re-posted.")
        return made["id"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 403:
            raise
        have = ", ".join("#" + c["name"] for c in channels if c.get("type") == 0)
        sys.exit(f"No #{want}, and the bot can't create one (403: it needs "
                 f"Manage Channels).\nEither make #{want} by hand and give the "
                 f"bot access, or point this at a channel that exists:\n"
                 f"  --channel <name>   text channels here: {have}")


def clear(d: Discord, cid: str) -> int:
    """Delete the state messages in a channel, leaving anything else alone."""
    gone = 0
    for m in d.get(f"/channels/{cid}/messages", limit=100) or []:
        content = m.get("content", "")
        if parse(content) or content.startswith(SUMMARY_MARK):
            d.write("DELETE", f"/channels/{cid}/messages/{m['id']}")
            gone += 1
    return gone


def render_summary(cards: list[Card]) -> str:
    """
    The whole running order in one message, for people rather than for Ernie.

    Card messages are the state and are scattered up the channel in whatever
    order they were first posted; this is the only place the board can be read
    top to bottom, which is what you want when checking two boards agree.
    """
    live = [c for c in cards if not c.completed]
    pos = positions(cards)
    # Discord's own timestamp markup: the reader's client renders these in
    # their timezone and keeps the "ago" current by itself, so the message
    # doesn't have to be rewritten for the clock to stay right.
    lines = [f"{SUMMARY_MARK} — {len(live)} open, {len(cards) - len(live)} closed",
             "_the running order both boards should agree on_"]
    # "last checked" read as "the two boards were compared and agree", which
    # this message cannot say: it is written by whichever machine published,
    # from its own copy, and a board whose sync has stopped goes on stamping a
    # confident clock over a stale order. When it was written is what it
    # honestly knows. Whether contact is live is Bert's indicator, off agreed_at.
    lines.append(f"last published {discord_time(now_iso())}")

    for band in BAND_ORDER:
        in_band = sorted((c for c in live if c.priority == band),
                         key=lambda c: c.rank)
        if not in_band:
            continue
        lines.append("")
        lines.append(f"**{band.title()}**")
        for c in in_band:
            name = c.name if len(c.name) <= 52 else c.name[:51] + "…"
            row = f"`{pos[c.thread_id]}.` {name}"
            if c.items:
                row += f"  ·  {sum(1 for i in c.items if i.done)}/{len(c.items)}"
            if c.actor:
                row += f"  ·  {c.actor}"
            lines.append(row)

    # A long board has to lose its tail rather than the message being refused.
    out = "\n".join(lines)
    while len(out) > CONTENT_MAX - 40 and len(lines) > 3:
        lines.pop()
        out = "\n".join(lines) + "\n_…truncated_"
    return out


CHECKED = re.compile(r"last (?:published|checked) <t:(\d+):")

# Both spellings: a channel written before the rename still holds the old
# line, and it has to be stripped for the comparison too or every one of those
# messages reads as changed twice -- once to drop it, once to settle.
_STAMP_LINES = ("last published ", "last checked ")


def without_stamp(content: str) -> str:
    """The summary minus its clock, which changes on every publish."""
    return "\n".join(l for l in (content or "").splitlines()
                     if not l.startswith(_STAMP_LINES))


def publish_summary(d: Discord, cid: str, cards: list[Card],
                    existing: dict | None) -> str:
    """
    Post or edit the summary. Returns what happened, for the caller's count.

    The stamp is what tells a reader whether to trust what they are looking
    at, so it has to keep moving -- but rewriting the message every cycle just
    to advance a clock is noise. So: rewrite whenever the board itself
    changed, and otherwise only once the stamp has gone stale.
    """
    content = render_summary(cards)
    if existing is None:
        d.write("POST", f"/channels/{cid}/messages", content=content)
        return "posted"
    old = existing.get("content") or ""
    if without_stamp(old) == without_stamp(content):
        seen = CHECKED.search(old)
        if seen and time.time() - int(seen.group(1)) < SUMMARY_HEARTBEAT_S:
            return "unchanged"
    d.write("PATCH", f"/channels/{cid}/messages/{existing['id']}",
            content=content)
    return "edited"


def fetch_channel(d: Discord, cid: str) -> tuple[dict[str, dict], dict | None]:
    """Everything of ours in the channel: the cards, and the summary message."""
    cards, summary, before = {}, None, None
    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        page = d.get(f"/channels/{cid}/messages", **params)
        if not page:
            return cards, summary
        for m in page:
            content = m.get("content", "")
            p = parse(content)
            # Newest first, so the first message seen for a thread wins and a
            # stray older duplicate can't overwrite it.
            if p and p["thread"] not in cards:
                cards[p["thread"]] = {"message_id": m["id"], "payload": p,
                                      "content": content}
            elif p is None and content.startswith(SUMMARY_MARK) and summary is None:
                summary = m
        if len(page) < 100:
            return cards, summary
        before = page[-1]["id"]


def fetch_state(d: Discord, cid: str) -> dict[str, dict]:
    """The cards Discord is holding, keyed by thread_id."""
    return fetch_channel(d, cid)[0]


def clock_against_discord(d: Discord) -> float | None:
    """
    Seconds this machine's clock is ahead of Discord's, or None.

    Read from the Date header of a response Discord is sending right now. An
    existing message's timestamp says when it was written, not what the time
    is, so measuring against one reports its age as clock skew.
    """
    seen = {}
    hooks = d.http.event_hooks.get("response", [])
    d.http.event_hooks["response"] = list(hooks) + [
        lambda r: seen.setdefault("date", r.headers.get("date"))]
    try:
        d.get("/users/@me")
    finally:
        d.http.event_hooks["response"] = hooks
    if not seen.get("date"):
        return None
    try:
        theirs = parsedate_to_datetime(seen["date"])
    except (TypeError, ValueError):
        return None
    return (datetime.fromisoformat(now_iso()) - theirs).total_seconds()


def skew_seconds(msg: dict) -> float | None:
    """
    This machine's clock against Discord's, from a message Discord just
    stamped. Discord is the one clock both machines share, so it is the only
    thing either can be checked against.
    """
    stamp = msg.get("edited_timestamp") or msg.get("timestamp")
    if not stamp:
        return None
    try:
        theirs = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    # Through now_iso() so this machine has one notion of the time, rather
    # than the check reading a different clock from everything it checks.
    return (datetime.fromisoformat(now_iso()) - theirs).total_seconds()


def publish(d: Discord, cid: str, db: str, actor: str = "ernie",
            cards: list[Card] | None = None) -> dict:
    """
    Push the board into the channel: edit what moved, post what's new, leave
    the rest alone. Returns counts so a caller can see how little it did.

    Records what it published as the agreed base, which is what lets the pull
    tell "they moved it" from "we moved it" without comparing clocks.
    """
    if cards is None:
        cards = load_board(db)
    state, summary = fetch_channel(d, cid)
    pos = positions(cards)
    con = rw(db)
    counts = {"posted": 0, "edited": 0, "unchanged": 0}
    skew = None
    try:
        for c in cards:
            content = render(c, pos[c.thread_id], actor)
            if len(content) > CONTENT_MAX:
                print(f"  !! {c.thread_id} renders to {len(content)} chars, "
                      f"over Discord's {CONTENT_MAX}")
                continue
            known = state.get(c.thread_id)
            sent = None
            if known is None:
                if c.completed:
                    # Closed before the channel ever heard of it. Publishing
                    # the whole back catalogue on first run would be pages of
                    # history nobody is going to act on.
                    counts["unchanged"] += 1
                    continue
                sent = d.write("POST", f"/channels/{cid}/messages",
                               content=content)
                counts["posted"] += 1
            elif (same_state(known["payload"], c.payload())
                  and prose_of(known.get("content", "")) == prose_of(content)):
                counts["unchanged"] += 1
            else:
                sent = d.write("PATCH",
                               f"/channels/{cid}/messages/{known['message_id']}",
                               content=content)
                counts["edited"] += 1

            if sent:
                s = skew_seconds(sent)
                if s is not None and (skew is None or abs(s) > abs(skew)):
                    skew = s
            mid = (sent or {}).get("id") or (known or {}).get("message_id")
            save_base(con, c.thread_id, mid, c.payload())
            con.commit()
    finally:
        con.close()

    # Last, so it sits below the cards it describes on a first run.
    try:
        counts["summary"] = publish_summary(d, cid, cards, summary)
    except httpx.HTTPStatusError as e:
        # A board that can't render its own summary is still a working board.
        print(f"  !! summary not written: {e.response.status_code}",
              file=sys.stderr)

    if skew is not None and abs(skew) > SKEW_WARN_S:
        counts["clock_skew_s"] = round(skew, 1)
        print(f"  !! this machine's clock is {skew:+.0f}s off Discord's. "
              f"Event times in the feed will be wrong by about that much on "
              f"both boards. Fix the clock.", file=sys.stderr)
    return counts


# -- reading it back --------------------------------------------------------

def rw(db: str) -> sqlite3.Connection:
    # ernie_load.connect applies schema.sql, which is what creates state_sync.
    return load.connect(db)


def read_bases(con) -> dict[str, dict]:
    """What this machine last agreed with the channel about, per card."""
    out = {}
    for r in con.execute("SELECT thread_id, base_json FROM state_sync"):
        try:
            out[r["thread_id"]] = json.loads(r["base_json"])
        except json.JSONDecodeError:
            continue
    return out


def save_base(con, thread_id: str, message_id: str | None, payload: dict) -> None:
    con.execute(
        """INSERT INTO state_sync (thread_id, message_id, base_json, synced_at)
           VALUES (?,?,?,?)
           ON CONFLICT(thread_id) DO UPDATE SET
               message_id=excluded.message_id,
               base_json=excluded.base_json,
               synced_at=excluded.synced_at""",
        (thread_id, message_id, json.dumps(state_only(payload)), now_iso()))


def log_event(con, *, thread_id, verb, actor, old=None, new=None, at=None) -> str:
    """
    Put a replayed change into the local feed.

    dispatch_after stays NULL. The machine that made the change has already
    queued its own message for the thread, so giving this copy a dispatch
    would post the same update twice -- once from each board.

    occurred_at is the payload's timestamp, not now, so a change made while
    this machine was offline lands in the feed where it happened rather than
    at the top.
    """
    eid = str(uuid.uuid4())
    con.execute(
        """INSERT INTO events (event_id, occurred_at, actor_name, thread_id,
                               verb, old_value, new_value, dispatch_after)
           VALUES (?,?,?,?,?,?,?,NULL)""",
        (eid, at or now_iso(), actor, thread_id, verb, old, new))
    return eid


def apply_card(con: sqlite3.Connection, p: dict) -> list[str]:
    """
    Write one remote payload into the local mirror, and say what moved.

    updated_at is set to the payload's own timestamp rather than now, so the
    next cycle doesn't read this machine's write as a change of its own and
    push it straight back up.
    """
    # Clamped, so a laptop running fast can't file its changes in the future
    # and sit at the top of the feed above things that happened since.
    tid, at, by = p["thread"], sane_time(p.get("at")), p.get("by")
    actor = by or "the other board"
    changed = []

    row = con.execute(
        "SELECT priority, rank, completed_at FROM cards WHERE thread_id=?",
        (tid,)).fetchone()

    if row["priority"] != p["priority"]:
        changed.append(f"{row['priority']} -> {p['priority']}")
        log_event(con, thread_id=tid, verb="priority_changed", actor=actor,
                  old=row["priority"], new=p["priority"], at=at)
    elif row["rank"] != p["rank"]:
        changed.append(f"reordered in {p['priority']}")
        log_event(con, thread_id=tid, verb="reordered", actor=actor, at=at)
    if row["priority"] != p["priority"] or row["rank"] != p["rank"]:
        con.execute("UPDATE cards SET priority=?, rank=?, updated_at=? "
                    "WHERE thread_id=?",
                    (p["priority"], p["rank"], now_iso(), tid))

    closed = bool(p.get("completed"))
    if closed and not row["completed_at"]:
        con.execute("UPDATE cards SET completed_at=?, completed_by=? "
                    "WHERE thread_id=?", (at, by, tid))
        log_event(con, thread_id=tid, verb="completed", actor=actor, at=at)
        changed.append("closed")
    elif not closed and row["completed_at"]:
        # Reopened on the other machine, which only happens by undoing the
        # close there. Nothing to log: the event being undone is theirs.
        con.execute("UPDATE cards SET completed_at=NULL, completed_by=NULL "
                    "WHERE thread_id=?", (tid,))
        changed.append("reopened")

    added, dropped = [], []
    remote = {i["id"]: i for i in p.get("work") or []}
    local = {r["item_id"]: r for r in con.execute(
        """SELECT item_id, body, position, done_at, removed_at FROM work_items
           WHERE thread_id=?""", (tid,))}

    for pos, (iid, ri) in enumerate(remote.items()):
        li = local.get(iid)
        if li is None:
            con.execute(
                """INSERT INTO work_items (item_id, thread_id, body, position,
                                           created_at, created_by, done_at, done_by)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (iid, tid, ri["body"], float(pos), at, ri.get("by") or by,
                 at if ri["done"] else None,
                 (ri.get("by") or by) if ri["done"] else None))
            changed.append(f"+{ri['body']!r}")
            added.append(iid)
            continue
        if li["position"] != float(pos):
            con.execute("UPDATE work_items SET position=? WHERE item_id=?",
                        (float(pos), iid))
        if li["removed_at"]:
            # Never hard-deleted, so a bubble the other board still shows is
            # put back by clearing the tombstone rather than inserting again.
            con.execute("UPDATE work_items SET removed_at=NULL, removed_by=NULL "
                        "WHERE item_id=?", (iid,))
            changed.append(f"restored {ri['body']!r}")
        if ri["done"] and not li["done_at"]:
            con.execute("UPDATE work_items SET done_at=?, done_by=? WHERE item_id=?",
                        (at, ri.get("by") or by, iid))
            changed.append(f"ticked {ri['body']!r}")
            log_event(con, thread_id=tid, verb="work_done", actor=actor,
                      old=iid, new=ri["body"], at=at)
        elif not ri["done"] and li["done_at"]:
            con.execute("UPDATE work_items SET done_at=NULL, done_by=NULL "
                        "WHERE item_id=?", (iid,))
            changed.append(f"unticked {ri['body']!r}")

    for iid, li in local.items():
        if iid not in remote and not li["removed_at"]:
            con.execute("UPDATE work_items SET removed_at=?, removed_by=? "
                        "WHERE item_id=?", (at, by, iid))
            changed.append(f"-{li['body']!r}")
            dropped.append(iid)

    if added or dropped:
        log_event(con, thread_id=tid, verb="edited", actor=actor, at=at,
                  old=json.dumps({"__work__": {"added": added,
                                               "removed": dropped}}),
                  new="; ".join(c for c in changed
                                if c.startswith(("+", "-"))))

    # updated_at is this machine's own clock: it means "when this row last
    # changed here", and nothing decides a conflict by it any more -- that is
    # state_sync's job.
    con.execute("UPDATE cards SET updated_at=? WHERE thread_id=?",
                (now_iso(), tid))
    return changed


def reconcile(d: Discord, cid: str, db: str, dry_run: bool = False) -> dict:
    """
    Pull the channel into the local mirror.

    Three-way, against what this machine last agreed with the channel about
    (state_sync), never by comparing the two machines' clocks. Timestamps come
    off two real laptops: a clock a few minutes out would win or lose every
    tie in the same direction, silently, and a badly wrong one would either
    stamp on everything the other person does or ignore them entirely.

    Against that base, per card:

        channel moved, we didn't   -> apply theirs
        we moved, channel didn't   -> ours stands, publish() sends it
        both moved                 -> conflict; the channel wins, because it
                                      is the shared copy, and the change that
                                      loses is named in the feed rather than
                                      vanishing
        neither moved              -> settled

    A card with no base is one this machine has never reconciled, so it adopts
    the channel: a board joining an existing session takes the shared state.

    Every card that gets as far as being compared has its agreed_at stamped,
    whichever way the comparison went -- including the two quiet outcomes that
    write nothing else. That column is the only honest answer to "are their
    changes reaching us", because it moves solely when this loop has read the
    channel. publish() must never touch it: it advances even while the sync is
    stopped, which is the state worth reporting.
    """
    remote = fetch_state(d, cid)
    con = rw(db)
    bases = read_bases(con)
    local = {c.thread_id: c.payload() for c in load_board(db)}
    report = {"applied": [], "ahead": [], "conflicts": [], "unknown": [],
              "settled": 0}
    compared = []
    try:
        for tid, entry in remote.items():
            p = entry["payload"]
            if p.get("v") != FORMAT_VERSION:
                report["unknown"].append(f"{tid} (format v{p.get('v')})")
                continue
            if tid not in local:
                # The channel knows a card this machine has never synced -- the
                # thread itself hasn't arrived yet. Leave it for a later cycle
                # rather than inventing a card with no thread behind it.
                report["unknown"].append(f"{tid} (no local card)")
                continue

            theirs, ours = state_only(p), state_only(local[tid])
            base = bases.get(tid)
            they_moved = base is None or theirs != base
            we_moved = base is not None and ours != base

            compared.append(tid)

            if theirs == ours:
                # Agreed however they got there; record it so neither side
                # looks changed next time.
                if base != theirs and not dry_run:
                    save_base(con, tid, entry["message_id"], p)
                    con.commit()
                report["settled"] += 1
                continue
            if we_moved and not they_moved:
                report["ahead"].append(tid)
                continue

            changed = apply_card(con, p)
            where = "conflicts" if (we_moved and they_moved) else "applied"
            report[where].append({"thread": tid, "changed": changed,
                                  "by": p.get("by")})
            if dry_run:
                con.rollback()
            else:
                save_base(con, tid, entry["message_id"], p)
                con.commit()       # per card, so the write lock is never held long

        # One statement for the whole pass rather than a commit per card: on a
        # quiet board every card takes the settled path, and 27 write
        # transactions a minute to move 27 timestamps is lock Bert could have
        # had.
        if compared and not dry_run:
            con.executemany(
                "UPDATE state_sync SET agreed_at=? WHERE thread_id=?",
                [(now_iso(), tid) for tid in compared])
            con.commit()
    finally:
        con.close()
    return report


def connect(env: str) -> tuple[Discord, str]:
    load_env(env)
    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("DISCORD_GUILD_ID")
    allow = os.environ.get("ALLOW_DISCORD_WRITES")
    if not token or not guild:
        sys.exit("DISCORD_TOKEN and DISCORD_GUILD_ID must be set")
    if guild == PRODUCTION_GUILD:
        sys.exit("REFUSING: that's production.")
    d = Discord(token, guild, allow_writes_for=allow)
    who = d.whoami()
    if not who["writes"]:
        sys.exit(f"Writes are off. ALLOW_DISCORD_WRITES must equal {guild}.")
    print(f"{who['bot']} -> {who['guild']} ({guild})")
    return d, guild


def check(d: Discord, guild: str, want: str) -> int:
    """
    Is this machine set up to share a board? Returns a shell exit code.

    Every one of these is silent when it's wrong: the board looks healthy from
    here while nothing you do ever reaches the other person.
    """
    problems = []

    if not os.environ.get("STATE_CHANNEL_ID"):
        problems.append(
            "STATE_CHANNEL_ID is not set in the env file. Without it this\n"
            "     machine keeps a private board and shares nothing.")

    try:
        ensure_channel(d, guild, want)
        print(f"  state channel   {want} -- reachable")
    except SystemExit as e:
        problems.append(str(e))

    skew = clock_against_discord(d)
    if skew is None:
        print("  clock           couldn't be checked")
    elif abs(skew) > SKEW_WARN_S:
        problems.append(
            f"this machine's clock is {skew:+.0f}s off Discord's. Times in the\n"
            f"     activity feed will be wrong by about that much. Turn on\n"
            f"     'Set time automatically' and hit Sync now.")
    else:
        print(f"  clock           within {abs(skew):.0f}s of Discord")

    if not problems:
        print("\nReady to share a board.")
        return 0
    print("\nNot ready:")
    for p in problems:
        print(f"  -- {p}")
    return 1


def demo(d: Discord, guild: str, db: str, want: str) -> None:
    """
    The round trip, end to end: publish the board, move a card, publish again,
    then read the order back out of Discord and see it agree.
    """
    cid = ensure_channel(d, guild, want)
    print(f"state channel #{want} ({cid})\n")

    cards = load_board(db)
    print(f"local board: {len(cards)} open cards")

    t0 = time.monotonic()
    print("\n1. publishing the board")
    print(f"   {publish(d, cid, db, cards=cards)}  in {time.monotonic()-t0:.1f}s")

    print("\n2. publishing again, unchanged")
    t1 = time.monotonic()
    print(f"   {publish(d, cid, db, cards=cards)}  in {time.monotonic()-t1:.1f}s")
    print("   (nothing moved, so nothing was written)")

    band = sorted([c for c in cards if c.priority == cards[0].priority],
                  key=lambda c: c.rank)
    if len(band) >= 2:
        mover = band[-1]
        before = mover.rank
        mover.rank = band[0].rank - 1000.0        # drag it to the top
        print(f"\n3. dragging {mover.name[:48]!r} to the top of "
              f"{mover.priority}")
        t2 = time.monotonic()
        print(f"   {publish(d, cid, db, actor='Ana', cards=cards)}  "
              f"in {time.monotonic()-t2:.1f}s")
        print(f"   rank {before} -> {mover.rank}, one message edited")

    print("\n4. reading the board back out of Discord")
    state = fetch_state(d, cid)
    seen = sorted(state.values(), key=lambda s: (s["payload"]["priority"],
                                                 s["payload"]["rank"]))
    for s in seen[:8]:
        p = s["payload"]
        work = ", ".join(("x " if i["done"] else "") + i["body"]
                         for i in p["work"]) or "-"
        print(f"   {p['priority']:>10} {p['rank']:>9.1f}  {p['thread'][-6:]}  "
              f"{work[:44]}")
    if len(seen) > 8:
        print(f"   ... and {len(seen)-8} more")

    sizes = [len(render(c, 1)) for c in cards]
    print(f"\nmessage size: {min(sizes)}-{max(sizes)} chars "
          f"of Discord's {CONTENT_MAX}")
    print(f"whole round trip: {time.monotonic()-t0:.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="ernie-test.env")
    ap.add_argument("--db", default="ernie-test.db")
    ap.add_argument("--channel", default=None,
                    help="state channel, by name or id "
                         "(default: STATE_CHANNEL_ID from the env file)")
    ap.add_argument("--demo", action="store_true",
                    help="publish, move a card, read it back")
    ap.add_argument("--clear", action="store_true",
                    help="delete the state messages again")
    ap.add_argument("--check", action="store_true",
                    help="is this machine set up to share a board?")
    ap.add_argument("--pull", action="store_true",
                    help="read the channel back into the local mirror")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --pull, say what would change and change nothing")
    a = ap.parse_args()

    d, guild = connect(a.env)
    # connect() has loaded the env file by now, so the default can come
    # from there rather than being repeated on every command line.
    channel = a.channel or os.environ.get("STATE_CHANNEL_ID") or STATE_CHANNEL
    if a.check:
        sys.exit(check(d, guild, channel))
    if a.demo:
        demo(d, guild, a.db, channel)
    elif a.clear:
        cid = ensure_channel(d, guild, channel)
        print(f"deleted {clear(d, cid)} state messages from {channel}")
    elif a.pull:
        cid = ensure_channel(d, guild, channel)
        r = reconcile(d, cid, a.db, dry_run=a.dry_run)
        for hit in r["applied"]:
            who = hit["by"] or "someone"
            print(f"  {hit['thread'][-6:]}  {who}: " + "; ".join(hit["changed"]))
        for hit in r["conflicts"]:
            print(f"  CONFLICT {hit['thread'][-6:]}  both moved it; "
                  f"{hit['by'] or 'the channel'} wins: " + "; ".join(hit["changed"]))
        print(f"{'would apply' if a.dry_run else 'applied'} "
              f"{len(r['applied'])}, {len(r['conflicts'])} conflicts, "
              f"{r['settled']} already agreed, "
              f"{len(r['ahead'])} to publish from here, "
              f"{len(r['unknown'])} skipped")
        for u in r["unknown"]:
            print(f"  skipped {u}")
    else:
        cid = ensure_channel(d, guild, channel)
        print(publish(d, cid, a.db))


if __name__ == "__main__":
    main()

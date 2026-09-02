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

import httpx
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ernie_sync import Discord, load_env

PRODUCTION_GUILD = "1481003073894744226"    # the constant wipe_test.py guards on
STATE_CHANNEL = "ernie-state"
FORMAT_VERSION = 1
CONTENT_MAX = 2000          # Discord's cap on message content
FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class Item:
    item_id: str
    body: str
    done: bool


@dataclass
class Card:
    thread_id: str
    name: str
    priority: str
    rank: float
    items: list[Item] = field(default_factory=list)

    def payload(self) -> dict:
        """The half of the message that is state rather than decoration."""
        return {
            "v": FORMAT_VERSION,
            "thread": self.thread_id,
            "priority": self.priority,
            "rank": self.rank,
            "work": [{"id": i.item_id, "body": i.body, "done": i.done}
                     for i in self.items],
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- the message ------------------------------------------------------------

def render(card: Card, position: int, actor: str = "ernie") -> str:
    """
    A line for people, then the payload for Ernie.

    The human line is derived, never parsed back: everything that matters is
    in the JSON, so a person editing the prose can't corrupt the board.
    """
    head = f"**{card.priority} #{position}** — {card.name}"
    body = json.dumps(dict(card.payload(), by=actor, at=now_iso()),
                      ensure_ascii=False)
    return f"{head}\n```json\n{body}\n```"


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


def same_state(a: dict, b: dict) -> bool:
    """
    Ignoring who touched it and when.

    Without this every publish rewrites every message, which burns the edit
    budget and fills the channel with edits that changed nothing.
    """
    keep = lambda d: {k: v for k, v in d.items() if k not in ("by", "at")}
    return keep(a) == keep(b)


# -- the local board --------------------------------------------------------

def load_board(db: str) -> list[Card]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cards = []
        for r in con.execute(
                """SELECT c.thread_id, c.priority, c.rank, v.name
                   FROM cards c
                   LEFT JOIN v_thread_current v ON v.thread_id = c.thread_id
                   WHERE c.completed_at IS NULL
                   ORDER BY c.priority, c.rank"""):
            items = [
                Item(i["item_id"], i["body"], bool(i["done_at"]))
                for i in con.execute(
                    """SELECT item_id, body, done_at FROM work_items
                       WHERE thread_id=? AND removed_at IS NULL
                       ORDER BY position""", (r["thread_id"],))
            ]
            cards.append(Card(r["thread_id"], r["name"] or "(untitled)",
                              r["priority"], r["rank"], items))
        return cards
    finally:
        con.close()


def positions(cards: list[Card]) -> dict[str, int]:
    """Ordinal within its band, which is what the human line shows."""
    out, seen = {}, {}
    for c in sorted(cards, key=lambda c: (c.priority, c.rank)):
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
        if parse(m.get("content", "")):
            d.write("DELETE", f"/channels/{cid}/messages/{m['id']}")
            gone += 1
    return gone


def fetch_state(d: Discord, cid: str) -> dict[str, dict]:
    """What Discord currently believes, keyed by thread_id."""
    out = {}
    for m in d.get(f"/channels/{cid}/messages", limit=100) or []:
        p = parse(m.get("content", ""))
        if p:
            out[p["thread"]] = {"message_id": m["id"], "payload": p}
    return out


def publish(d: Discord, cid: str, cards: list[Card], actor: str = "ernie") -> dict:
    """
    Push the board into the channel: edit what moved, post what's new, leave
    the rest alone. Returns counts so a caller can see how little it did.
    """
    state = fetch_state(d, cid)
    pos = positions(cards)
    posted = edited = unchanged = 0
    for c in cards:
        content = render(c, pos[c.thread_id], actor)
        if len(content) > CONTENT_MAX:
            print(f"  !! {c.thread_id} renders to {len(content)} chars, "
                  f"over Discord's {CONTENT_MAX}")
            continue
        known = state.get(c.thread_id)
        if known is None:
            d.write("POST", f"/channels/{cid}/messages", content=content)
            posted += 1
        elif same_state(known["payload"], c.payload()):
            unchanged += 1
        else:
            d.write("PATCH", f"/channels/{cid}/messages/{known['message_id']}",
                    content=content)
            edited += 1
    return {"posted": posted, "edited": edited, "unchanged": unchanged}


# -- reading it back --------------------------------------------------------

def rw(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def apply_card(con: sqlite3.Connection, p: dict) -> list[str]:
    """
    Write one remote payload into the local mirror, and say what moved.

    updated_at is set to the payload's own timestamp rather than now, so the
    next cycle doesn't read this machine's write as a change of its own and
    push it straight back up.
    """
    tid, at, by = p["thread"], p["at"], p.get("by")
    changed = []

    row = con.execute("SELECT priority, rank FROM cards WHERE thread_id=?",
                      (tid,)).fetchone()
    if row["priority"] != p["priority"] or row["rank"] != p["rank"]:
        changed.append(f"{row['priority']} {row['rank']:.0f} -> "
                       f"{p['priority']} {p['rank']:.0f}")
        con.execute("UPDATE cards SET priority=?, rank=?, updated_at=? "
                    "WHERE thread_id=?", (p["priority"], p["rank"], at, tid))

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
                (iid, tid, ri["body"], float(pos), at, by,
                 at if ri["done"] else None, by if ri["done"] else None))
            changed.append(f"+{ri['body']!r}")
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
                        (at, by, iid))
            changed.append(f"ticked {ri['body']!r}")
        elif not ri["done"] and li["done_at"]:
            con.execute("UPDATE work_items SET done_at=NULL, done_by=NULL "
                        "WHERE item_id=?", (iid,))
            changed.append(f"unticked {ri['body']!r}")

    for iid, li in local.items():
        if iid not in remote and not li["removed_at"]:
            con.execute("UPDATE work_items SET removed_at=?, removed_by=? "
                        "WHERE item_id=?", (at, by, iid))
            changed.append(f"-{li['body']!r}")

    # Carry the payload's timestamp onto the card even when only a bubble
    # moved. Without it a work-item change leaves updated_at behind the
    # payload, and every later cycle re-examines a card that is already
    # settled.
    con.execute("UPDATE cards SET updated_at=? WHERE thread_id=?", (at, tid))
    return changed


def reconcile(d: Discord, cid: str, db: str, dry_run: bool = False) -> dict:
    """
    Pull the channel into the local mirror.

    The two boards meet in Discord, so the newer of the two wins per card: a
    payload written after this machine last touched that card is somebody
    else's move and gets applied; anything older is a local change that hasn't
    been published yet, and publish() will push it up.

    Both timestamps are ISO-8601 UTC written by Python, so they compare as
    strings -- but they come off two different laptops, so a clock that is
    minutes out will decide a tie the wrong way.
    """
    remote = fetch_state(d, cid)
    con = rw(db)
    report = {"applied": [], "ahead": [], "unknown": [], "same": 0, "settled": 0}
    try:
        for tid, entry in remote.items():
            p = entry["payload"]
            if p.get("v") != FORMAT_VERSION:
                report["unknown"].append(f"{tid} (format v{p.get('v')})")
                continue
            row = con.execute(
                "SELECT updated_at FROM cards WHERE thread_id=?", (tid,)).fetchone()
            if row is None:
                # The channel knows a card this machine has never synced -- the
                # thread itself hasn't arrived yet. Leave it for a later cycle
                # rather than inventing a card with no thread behind it.
                report["unknown"].append(f"{tid} (no local card)")
                continue
            local_at = row["updated_at"] or ""
            if p["at"] == local_at:
                report["settled"] += 1          # already agreed, nothing to do
                continue
            if p["at"] < local_at:
                # This machine has moved the card since the channel last heard
                # about it. publish() is what puts that right, not this.
                report["ahead"].append(tid)
                continue

            changed = apply_card(con, p)
            if changed:
                report["applied"].append({"thread": tid, "changed": changed,
                                          "by": p.get("by")})
            else:
                # Same state under a newer timestamp -- somebody republished
                # without moving anything. Take the timestamp so it settles.
                report["same"] += 1
            if dry_run:
                con.rollback()
            else:
                con.commit()       # per card, so the write lock is never held long
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
    print(f"   {publish(d, cid, cards)}  in {time.monotonic()-t0:.1f}s")

    print("\n2. publishing again, unchanged")
    t1 = time.monotonic()
    print(f"   {publish(d, cid, cards)}  in {time.monotonic()-t1:.1f}s")
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
        print(f"   {publish(d, cid, cards, actor='Ana')}  "
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
    ap.add_argument("--pull", action="store_true",
                    help="read the channel back into the local mirror")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --pull, say what would change and change nothing")
    a = ap.parse_args()

    d, guild = connect(a.env)
    # connect() has loaded the env file by now, so the default can come
    # from there rather than being repeated on every command line.
    channel = a.channel or os.environ.get("STATE_CHANNEL_ID") or STATE_CHANNEL
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
        print(f"{'would apply' if a.dry_run else 'applied'} "
              f"{len(r['applied'])}, {r['settled'] + r['same']} already agreed, "
              f"{len(r['ahead'])} to publish from here, "
              f"{len(r['unknown'])} skipped")
        for u in r["unknown"]:
            print(f"  skipped {u}")
    else:
        cid = ensure_channel(d, guild, channel)
        print(publish(d, cid, load_board(a.db)))


if __name__ == "__main__":
    main()

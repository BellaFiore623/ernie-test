"""
Ernie sync service -- keeps the SQLite mirror current from Discord.

Read-only against Discord. Never posts, never edits, never deletes anything
there. Replaces the one-shot dump script: fetches straight into the database
instead of via a JSON file.

Three passes per cycle:

  1. THREADS   list active threads in watched channels; record title changes
  2. FORWARD   fetch only messages newer than last_seen_message_id
  3. RESCAN    re-read the tail of recently-active threads to catch edits and
               deletions, which forward-only paging can never see

    export DISCORD_TOKEN=... DISCORD_GUILD_ID=...
    python ernie_sync.py --once            # single cycle
    python ernie_sync.py                   # loop forever
    python ernie_sync.py --backfill        # one-time archived history pull
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

import ernie_extract as ex
import ernie_load as load

API = "https://discord.com/api/v10"
PACING = 0.1         # sleep after each GET; Discord's global ceiling is 50/s
RESCAN_TAIL = 100      # messages re-read per thread when checking for edits
RESCAN_DAYS = 14       # only rescan threads active in this window
RESCAN_PER_CYCLE = 12  # threads rescanned per cycle; the rest wait their turn.
                       # Rescanning every thread every cycle was the bulk of the
                       # cycle and found an edit almost never. Rotating means an
                       # edit surfaces within ceil(threads/12) cycles instead of
                       # the next one -- new cards, which is what people watch,
                       # are not delayed at all.
CYCLE_SECONDS = 60    # a quiet cycle is ~14 GETs now, not ~101, so a
                      # shorter interval still costs Discord less per hour
                      # than the old 5-minute one did


def load_env(path: str = "ernie.env") -> None:
    """Load KEY=VALUE lines from an env file sitting next to this script."""
    p = pathlib.Path(__file__).with_name(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Discord client
# --------------------------------------------------------------------------

class GuildMismatch(RuntimeError):
    pass


class Discord:
    """
    Discord client with a write guard.

    Writes are refused unless ALLOW_DISCORD_WRITES names the exact guild in
    DISCORD_GUILD_ID. Naming the guild -- rather than setting a boolean --
    means a stray env var can never point write code at production: the two
    values have to agree, and they only agree in the config you wrote on
    purpose.
    """

    def __init__(self, token: str, guild_id: str, allow_writes_for: str | None = None):
        self.guild_id = guild_id
        self.writes_allowed = bool(allow_writes_for) and allow_writes_for == guild_id
        self.http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bot {token}",
                     "User-Agent": "ernie-sync/0.1"},
            timeout=30.0,
        )

    def write(self, method: str, path: str, **body):
        """Any non-GET call goes through here, and here checks the guard."""
        if not self.writes_allowed:
            raise GuildMismatch(
                f"Write blocked. ALLOW_DISCORD_WRITES must equal "
                f"DISCORD_GUILD_ID ({self.guild_id}) for Ernie to post. "
                f"Attempted: {method} {path}")
        r = self.http.request(method, path, json=body or None)
        r.raise_for_status()
        time.sleep(PACING)
        return r.json() if r.content else {}

    def whoami(self) -> dict:
        me = self.get("/users/@me") or {}
        g = self.get(f"/guilds/{self.guild_id}") or {}
        return {"bot": me.get("username"), "guild": g.get("name"),
                "guild_id": self.guild_id, "writes": self.writes_allowed}

    def get(self, path: str, **params):
        for attempt in range(5):
            r = self.http.get(path, params=params or None)
            if r.status_code == 429:
                wait = r.json().get("retry_after", 1.0)
                time.sleep(wait + 0.1)
                continue
            if r.status_code in (403, 404):
                return None
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            time.sleep(PACING)
            return r.json()
        raise RuntimeError(f"giving up on {path}")

    def active_threads(self, guild_id: str):
        d = self.get(f"/guilds/{guild_id}/threads/active")
        return (d or {}).get("threads", [])

    def archived_threads(self, channel_id: str):
        out, before = [], None
        while True:
            p = {"limit": 100}
            if before:
                p["before"] = before
            page = self.get(f"/channels/{channel_id}/threads/archived/public", **p)
            if not page or not page.get("threads"):
                return out
            out += page["threads"]
            if not page.get("has_more"):
                return out
            before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]

    def messages_after(self, channel_id: str, after: str | None):
        """Messages newer than `after`, oldest first. None = whole history."""
        out, cursor = [], after
        while True:
            p = {"limit": 100}
            if cursor:
                p["after"] = cursor
                page = self.get(f"/channels/{channel_id}/messages", **p)
                if not page:
                    return out
                page = list(reversed(page))          # `after` returns newest-first
                out += page
                if len(page) < 100:
                    return out
                cursor = page[-1]["id"]
            else:
                return self._all_messages(channel_id)

    def _all_messages(self, channel_id: str):
        out, before = [], None
        while True:
            p = {"limit": 100}
            if before:
                p["before"] = before
            page = self.get(f"/channels/{channel_id}/messages", **p)
            if not page:
                return list(reversed(out))
            out += page
            if len(page) < 100:
                return list(reversed(out))
            before = page[-1]["id"]

    def messages_tail(self, channel_id: str, limit: int = RESCAN_TAIL):
        """Most recent N messages, for edit/deletion detection."""
        page = self.get(f"/channels/{channel_id}/messages", limit=limit)
        return list(reversed(page)) if page else []


# --------------------------------------------------------------------------
# Passes
# --------------------------------------------------------------------------

def register_channels(con, d: Discord, guild_id: str, cards: str,
                      history: str) -> None:
    """Bring watched_channels in line with the env file.

    Channel ids are per-environment config, not schema. They used to be seeded
    from schema.sql, which meant every new database -- including a fresh test
    one -- came up watching production's channels.

    Listing a channel here adds or updates it. Dropping one does NOT unwatch
    it: delete the row by hand. A typo in an env file must not be able to
    quietly stop production syncing.
    """
    wanted = [(cid, gen)
              for ids, gen in ((cards, 1), (history, 0))
              for cid in (ids or "").replace(",", " ").split()]
    if not wanted:
        return

    for cid, gen in wanted:
        ch = d.get(f"/channels/{cid}")
        if ch is None:
            print(f"  channel {cid}: can't see it, leaving it unregistered",
                  file=sys.stderr)
            continue
        # Same guard as everywhere else: the env file names the guild, and a
        # channel from any other one has no business in this database.
        if ch.get("guild_id") != guild_id:
            print(f"  channel {cid} is in guild {ch.get('guild_id')}, not "
                  f"{guild_id}. Not registering it.", file=sys.stderr)
            continue
        con.execute(
            "INSERT INTO watched_channels "
            "       (channel_id, name, mirror, generate_cards) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "  name=excluded.name, mirror=1, "
            "  generate_cards=excluded.generate_cards",
            (cid, ch.get("name"), gen))
    con.commit()


def watched(con, cards_only=False):
    q = "SELECT channel_id, name, generate_cards FROM watched_channels WHERE mirror=1"
    if cards_only:
        q += " AND generate_cards=1"
    return {r["channel_id"]: dict(r) for r in con.execute(q)}


def sync_threads(con, d: Discord, guild_id: str, stats: dict) -> list[dict]:
    """Pass 1: list threads, filter to watched channels, record metadata."""
    chans = watched(con)
    threads = [t for t in d.active_threads(guild_id) if t.get("parent_id") in chans]
    stats["threads_seen"] = len(threads)

    for t in threads:
        load.load_thread(con, {"thread": t}, stats)
    con.commit()
    return threads


def sync_messages(con, d: Discord, threads: list[dict], stats: dict) -> None:
    """Pass 2: forward-only fetch of new messages."""
    for t in threads:
        tid = t["id"]
        row = con.execute(
            "SELECT last_seen_message_id FROM threads WHERE thread_id=?",
            (tid,)).fetchone()
        cursor = row["last_seen_message_id"] if row else None

        # The thread listing already carries Discord's own last_message_id, so
        # a thread with nothing past our cursor needs no request at all. On a
        # quiet board that is one saved GET per thread per cycle, which is most
        # of the cycle.
        if cursor is not None and t.get("last_message_id") == cursor:
            continue

        msgs = d.messages_after(tid, cursor)
        if msgs:
            load.load_messages(con, tid, msgs, stats)
        con.commit()      # short transactions: Bert must be able to write too


_rescan_at = 0          # rotation cursor for rescan_edits


def rescan_edits(con, d: Discord, stats: dict) -> None:
    """
    Pass 3: catch edits and deletions.

    Forward paging never revisits old messages, so an edit or a delete after
    ingestion is invisible without this. Only threads active in the last
    RESCAN_DAYS are checked, which keeps the cost bounded.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RESCAN_DAYS)).isoformat()
    rows = con.execute(
        """SELECT DISTINCT t.thread_id FROM threads t
           JOIN messages m ON m.thread_id = t.thread_id
           WHERE t.deleted_at IS NULL AND m.created_at > ?
           ORDER BY t.thread_id""", (cutoff,)).fetchall()

    global _rescan_at
    if RESCAN_PER_CYCLE and len(rows) > RESCAN_PER_CYCLE:
        start = _rescan_at % len(rows)
        rows = (rows + rows)[start:start + RESCAN_PER_CYCLE]
        _rescan_at = start + RESCAN_PER_CYCLE

    for r in rows:
        tid = r["thread_id"]
        live = d.messages_tail(tid)
        if not live:
            continue

        # Edits: load_messages inserts a revision only when content differs.
        load.load_messages(con, tid, live, stats)

        # Deletions: anything we hold inside the fetched ID range that Discord
        # no longer returns has been deleted.
        live_ids = {m["id"] for m in live}
        lo = min(live_ids)
        gone = con.execute(
            """SELECT message_id FROM messages
               WHERE thread_id=? AND message_id >= ? AND deleted_at IS NULL""",
            (tid, lo)).fetchall()
        for g in gone:
            if g["message_id"] not in live_ids:
                con.execute(
                    "UPDATE messages SET deleted_at=? WHERE message_id=?",
                    (now(), g["message_id"]))
                stats["deletions_found"] += 1
        con.commit()


def rebuild_derived(con, threads: list[dict]) -> None:
    """Recompute proposals/tickets/equipment and ensure cards exist."""
    for t in threads:
        tid = t["id"]
        msgs = [dict(m) for m in con.execute(
            """SELECT m.message_id AS id, m.created_at AS timestamp,
                      m.author_id, m.author_name, m.is_bot, r.content,
                      r.embeds_json, r.components_json
               FROM messages m
               JOIN message_revisions r ON r.message_id = m.message_id
               WHERE m.thread_id=? AND m.deleted_at IS NULL
                 AND r.observed_at = (SELECT MAX(observed_at)
                                      FROM message_revisions
                                      WHERE message_id = m.message_id)
               ORDER BY m.created_at""", (tid,))]

        # reshape stored rows back into Discord's message shape
        shaped = [{
            "id": m["id"],
            "timestamp": m["timestamp"],
            "content": m["content"] or "",
            "embeds": json.loads(m["embeds_json"] or "[]"),
            "components": json.loads(m["components_json"] or "[]"),
            "author": {"id": m["author_id"], "username": m["author_name"],
                       "bot": bool(m["is_bot"])},
        } for m in msgs]

        rec = ex.extract_thread({"thread": t, "messages": shaped})
        load.load_derived(con, rec)
        load.ensure_card(con, rec)
        con.commit()


def backfill(con, d: Discord, stats: dict) -> None:
    """One-time pull of archived threads for channels not yet backfilled."""
    for cid, c in watched(con).items():
        done = con.execute(
            "SELECT backfilled_at FROM watched_channels WHERE channel_id=?",
            (cid,)).fetchone()
        if done and done["backfilled_at"]:
            print(f"  {c['name']}: already backfilled, skipping")
            continue

        print(f"  {c['name']}: pulling archived threads...")
        threads = d.archived_threads(cid)
        print(f"    {len(threads)} archived threads")
        for t in threads:
            load.load_thread(con, {"thread": t}, stats)
            msgs = d.messages_after(t["id"], None)
            load.load_messages(con, t["id"], msgs, stats)
            con.commit()
        rebuild_derived(con, threads)
        con.execute("UPDATE watched_channels SET backfilled_at=? WHERE channel_id=?",
                    (now(), cid))
        con.commit()


# --------------------------------------------------------------------------
# Cycle
# --------------------------------------------------------------------------

def cycle(con, d: Discord, guild_id: str, do_backfill: bool = False) -> dict:
    stats = {"threads_seen": 0, "messages_new": 0, "edits_found": 0,
             "titles_changed": 0, "deletions_found": 0}
    run_id = con.execute("INSERT INTO sync_runs (started_at) VALUES (?)",
                         (now(),)).lastrowid
    try:
        if do_backfill:
            backfill(con, d, stats)

        threads = sync_threads(con, d, guild_id, stats)
        sync_messages(con, d, threads, stats)
        rescan_edits(con, d, stats)
        rebuild_derived(con, threads)

        con.execute(
            """UPDATE sync_runs SET finished_at=?, threads_seen=?, messages_new=?,
                                    edits_found=?, titles_changed=? WHERE run_id=?""",
            (now(), stats["threads_seen"], stats["messages_new"],
             stats["edits_found"], stats["titles_changed"], run_id))
        con.commit()
    except Exception as e:
        con.execute("UPDATE sync_runs SET finished_at=?, error=? WHERE run_id=?",
                    (now(), str(e)[:500], run_id))
        con.commit()
        raise
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--env", default="ernie.env",
                    help="which env file to load (ernie.env, ernie-test.env)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="also pull archived history for channels not yet done")
    ap.add_argument("--interval", type=int, default=CYCLE_SECONDS)
    a = ap.parse_args()

    load_env(a.env)

    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("DISCORD_GUILD_ID")
    allow = os.environ.get("ALLOW_DISCORD_WRITES")
    if not token or not guild:
        sys.exit("DISCORD_TOKEN and DISCORD_GUILD_ID must be set")

    con = load.connect(a.db)
    d = Discord(token, guild, allow_writes_for=allow)

    # Say out loud which server and which mode, every start. If this line ever
    # surprises you, stop before it does anything.
    who = d.whoami()
    if not who["guild"]:
        sys.exit(f"Can't see guild {guild}. Wrong token, or the bot isn't in "
                 f"that server.")
    mode = "WRITES ENABLED" if who["writes"] else "read-only"
    print(f"{who['bot']} -> {who['guild']} ({guild})  [{mode}]  db={a.db}")
    if allow and not who["writes"]:
        print(f"  note: ALLOW_DISCORD_WRITES={allow} does not match this "
              f"guild, so writes stay blocked", file=sys.stderr)

    register_channels(con, d, guild,
                      os.environ.get("CARD_CHANNEL_IDS", ""),
                      os.environ.get("HISTORY_CHANNEL_IDS", ""))
    # Second half of saying it out loud: which channels, not just which guild.
    for c in watched(con).values():
        kind = "cards" if c["generate_cards"] else "history only"
        print(f"  watching #{c['name']} ({c['channel_id']}) -- {kind}")

    first = True
    while True:
        t0 = time.time()
        try:
            s = cycle(con, d, guild, do_backfill=a.backfill and first)
            print(f"[{now()[:19]}] threads={s['threads_seen']} "
                  f"new={s['messages_new']} edits={s['edits_found']} "
                  f"titles={s['titles_changed']} deleted={s['deletions_found']} "
                  f"({time.time()-t0:.1f}s)")
        except Exception as e:
            print(f"[{now()[:19]}] cycle failed: {e}", file=sys.stderr)

        first = False
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()

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
import uuid
from datetime import datetime, timedelta, timezone

import httpx

import ernie_extract as ex
import ernie_load as load
import ernie_version

API = "https://discord.com/api/v10"
# What a `completed` event's new_value says when the closing happened in
# Discord rather than in Bert. Read by the API (undo refuses it) and by Bert
# (the feed says so instead of naming somebody).
CLOSED_IN_DISCORD = "discord"
PACING = 0.1         # sleep after each GET; Discord's global ceiling is 50/s
RESCAN_TAIL = 100      # messages re-read per thread when checking for edits
RESCAN_DAYS = 14       # only rescan threads active in this window
RESCAN_PER_CYCLE = 12  # threads rescanned per cycle; the rest wait their turn.
                       # Rescanning every thread every cycle was the bulk of the
                       # cycle and found an edit almost never. Rotating means an
                       # edit surfaces within ceil(threads/12) cycles instead of
                       # the next one -- new cards, which is what people watch,
                       # are not delayed at all.
CLOSURE_CHECKS = 20   # threads asked about per pass when cards go quiet
# Discord's audit-log action for a thread being edited, which is what an
# archive is. **111, not 112** -- 112 is THREAD_DELETE, and asking for it
# returns entries whose changes all read `new_value: None`, which looks
# enough like an archive to be believed. Confirmed against a real archive.
THREAD_UPDATE = 111
AUDIT_LOOKBACK = 100  # entries scanned for the thread we are asking about
RETRY_MAX_S = 30      # ride out a short 429 in write(); park anything longer
# Two beats, because the two halves of a cycle cost wildly different things
# and only one of them is what anybody is waiting for. Measured against the
# sandbox: a whole cycle is 13 GETs and ~4s, and **11 of those GETs and 3.5s
# of that time are the edit rescan** -- which found nothing in either sampled
# cycle, because an edit to an old message is rare. The part a new ticket
# actually arrives through is the thread listing: **1 GET, 0.30s**.
#
# So the listing runs on its own short beat and everything expensive stays
# where it was. Discord agrees this is free: the listing route answered
# `x-ratelimit-remaining: 999/1000` on eight back-to-back calls, while
# `/channels/{id}/messages` -- the rescan and the state pull -- is 5 per 5s
# and 429s on the sixth. The cheap route is the one being asked more often.
FAST_SECONDS = 5      # list threads, fetch what is new, recompute. 1 GET.
CYCLE_SECONDS = 60    # and everything else, on the beat it already had


# Where a frozen build keeps the things it has to be able to write: the env
# file the installer leaves, the databases, the logs. Beside the executable is
# either PyInstaller's temp extraction directory -- wiped on exit -- or a
# Program Files path the user cannot write to.
CONFIG_DIR = pathlib.Path(
    os.environ.get("LOCALAPPDATA")
    or (pathlib.Path.home() / ".config")) / "Ernie"

FROZEN = getattr(sys, "frozen", False)


def env_path(name: str) -> pathlib.Path | None:
    """Which file a `--env` argument actually means, or None.

    **The order depends on whether this is a frozen build, and that is the
    whole point.** The packaging note says to read `%LOCALAPPDATA%` first and
    keep the script directory as a fallback; taken literally that is a trap
    on a developer machine, because the installer's env names production and
    would then shadow the repository's `ernie-test.env` for anybody running
    `./run.sh test` afterwards. Testing against production is the one rule
    here with no exceptions in it, so it must not be reachable by installing
    the app.

    Frozen, there *is* no script directory worth reading, so the config
    directory is the only place looked at. From source the repository wins,
    and the config directory is the fallback for somebody who has put their
    keys there deliberately. Neither can surprise the other.
    """
    p = pathlib.Path(name).expanduser()
    if p.is_absolute():
        return p if p.is_file() else None
    here = pathlib.Path(__file__).resolve().parent
    order = ((CONFIG_DIR / p.name,) if FROZEN
             else (here / p, CONFIG_DIR / p.name))
    return next((c for c in order if c.is_file()), None)


def load_env(path: str = "ernie.env") -> pathlib.Path | None:
    """Load KEY=VALUE lines from an env file. Answers the file it read.

    Answering matters for a frozen build: "no DISCORD_TOKEN" is a sentence
    about a file, and the reader needs to know *which* file was looked for
    before they can put one there. Returning None rather than raising is
    still right -- most callers want the env if there is one and have their
    own words for its absence.
    """
    p = env_path(path)
    if p is None:
        return None
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    return p


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
        for attempt in range(3):
            r = self.http.request(method, path, json=body or None)
            if r.status_code == 429:
                wait = float(r.json().get("retry_after", 1.0))
                # Posting and editing come back in under a second, so riding
                # those out here saves every caller from handling them. A
                # thread rename that has spent its two-per-ten-minutes comes
                # back with ~600s instead: sleeping on that would stall the
                # outbox behind one card, so it goes to the caller to park.
                if wait <= RETRY_MAX_S and attempt < 2:
                    time.sleep(wait + 0.1)
                    continue
            r.raise_for_status()
            time.sleep(PACING)
            return r.json() if r.content else {}
        r.raise_for_status()
        return {}

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


def who_archived(d: Discord, guild_id: str, wanted: set) -> dict:
    """Who archived each of these threads, as far as the audit log knows.

    The thread object does not carry it -- only the audit log does, and only
    with **View Audit Log** on the bot's role. Without that this returns
    nothing and the closure is recorded with no name, which is what it did
    before the permission was granted; a revoked permission degrades the same
    way rather than failing the pass.

    One request, and only when there is something to attribute. The log is
    guild-wide, so a busy server can push an archive past `AUDIT_LOOKBACK`
    and out of reach: naming somebody is a nicety and never a reason to hold
    up the closure itself.

    Our own bot is skipped. It archives threads when somebody presses
    Complete in Bert -- that path never reaches here, because the card is
    already closed by then, but "ernie-test closed it" is exactly the
    attribution worth never making.
    """
    if not wanted:
        return {}
    r = d.get(f"/guilds/{guild_id}/audit-logs",
              action_type=THREAD_UPDATE, limit=AUDIT_LOOKBACK)
    if not r:
        return {}
    me = (d.get("/users/@me") or {}).get("id")
    users = {u["id"]: u for u in (r.get("users") or [])}
    found = {}
    for e in r.get("audit_log_entries") or []:
        tid = e.get("target_id")
        if tid not in wanted or tid in found:
            continue        # entries are newest first, so the first is the one
        if not any(c.get("key") == "archived" and c.get("new_value")
                   for c in (e.get("changes") or [])):
            continue
        if e.get("user_id") == me:
            found[tid] = None
            continue
        u = users.get(e.get("user_id")) or {}
        # global_name over username, the same preference the `started` line
        # uses: "Tyler" rather than "tyler_mazza".
        found[tid] = u.get("global_name") or u.get("username") or None
    return found


def reconcile_closures(con, d: Discord, active: set, stats: dict) -> None:
    """Cards whose thread has gone quiet: ask Discord whether it was closed.

    Archiving a thread is how work finishes, and Bert could not see it happen.
    The listing this loop runs on is `/guilds/{id}/threads/active`, and an
    archived thread is simply **not in it** -- so the row keeps whatever
    `archived` it had, the card is never completed, and a ticket somebody
    closed in Discord sits on the board for ever. Measured before this: a
    thread archived in Discord, then a full cycle -- `threads.archived` still
    0, `completed_at` still NULL, no event.

    **Absence is the question, never the answer.** A card missing from the
    listing only earns a `GET /channels/{id}`; the card is closed on what
    Discord says in the reply, not on the fact that it was missing. That
    matters because a listing short for any other reason -- a hiccup, a
    permission change, a channel dropping out of `watched` -- would otherwise
    close half the board in one pass. It costs nothing when nothing has
    happened, which is why it can run on the fast beat: no card is missing,
    so no request is made.

    The time is Discord's own `archive_timestamp`, not now: the event says
    when the work actually finished, which is the whole point of putting it
    in the feed. Who did it comes from the audit log, which needs **View
    Audit Log** on the bot's role -- see `who_archived`. Without it, or when
    the log no longer reaches back that far, the closure is recorded with no
    name rather than not recorded at all.
    """
    gone = [r["thread_id"] for r in con.execute(
        """SELECT c.thread_id FROM cards c
           JOIN threads t USING (thread_id)
           JOIN watched_channels w ON w.channel_id = t.parent_id
           WHERE c.completed_at IS NULL AND w.generate_cards = 1""")
        if r["thread_id"] not in active]
    if not gone:
        return

    closed = {}
    for tid in gone[:CLOSURE_CHECKS]:
        t = d.get(f"/channels/{tid}")
        if not t:
            # 403/404 come back as None. A thread we cannot read is not a
            # thread we may declare finished.
            continue
        meta = t.get("thread_metadata") or {}
        if not meta.get("archived"):
            continue
        closed[tid] = meta.get("archive_timestamp") or now()
    if not closed:
        return

    # One audit call for however many closed, and none at all if none did.
    by = who_archived(d, d.guild_id, set(closed))

    for tid, when in closed.items():
        who = by.get(tid)
        con.execute(
            "UPDATE threads SET archived=1, last_synced_at=? WHERE thread_id=?",
            (now(), tid))
        con.execute(
            "UPDATE cards SET completed_at=?, completed_by=?, updated_at=? "
            "WHERE thread_id=?", (when, who, now(), tid))
        # dispatch_after NULL: it happened in Discord already, and posting
        # "closed" back into the thread would be Ernie telling the room what
        # it just watched somebody do -- the same rule `started` follows.
        # new_value carries where it happened, so the feed can say so without
        # inventing a person to attribute it to.
        con.execute(
            """INSERT INTO events (event_id, occurred_at, actor_name, thread_id,
                                   verb, new_value, dispatch_after)
               VALUES (?,?,?,?,?,?,NULL)""",
            (str(uuid.uuid4()), when, who, tid, "completed", CLOSED_IN_DISCORD))
        stats["closed_in_discord"] = stats.get("closed_in_discord", 0) + 1
    con.commit()


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

SYNC_RUNS_KEPT = 5_000   # ~7 hours of fast passes, or a fortnight of full ones


def prune_runs(con) -> None:
    """Keep the audit table bounded.

    Every pass writes a `sync_runs` row and `/health` reads the newest
    finished one, which is what Bert draws as "synced 20s ago" -- so the fast
    pass has to write one or the board would report a staleness it does not
    have. Twelve times the passes is twelve times the rows, and nothing was
    ever deleting them. Trimmed on the full pass, so it is one statement a
    minute rather than one every five seconds.
    """
    con.execute(
        """DELETE FROM sync_runs WHERE run_id <=
             (SELECT MIN(run_id) FROM
                (SELECT run_id FROM sync_runs ORDER BY run_id DESC LIMIT ?))
             - 1""", (SYNC_RUNS_KEPT,))
    con.commit()


def cycle(con, d: Discord, guild_id: str, do_backfill: bool = False,
          full: bool = True) -> dict:
    """One pass over Discord.

    `full` is the difference between the two beats: without it this is the
    listing, whatever is new in the threads it named, and the local
    recompute -- the three things a new ticket has to go through, and 1 GET
    when the board is quiet. The edit rescan is the other 11.
    """
    stats = {"threads_seen": 0, "messages_new": 0, "edits_found": 0,
             "titles_changed": 0, "deletions_found": 0,
             "closed_in_discord": 0}
    run_id = con.execute("INSERT INTO sync_runs (started_at) VALUES (?)",
                         (now(),)).lastrowid
    try:
        if do_backfill:
            backfill(con, d, stats)

        threads = sync_threads(con, d, guild_id, stats)
        # Before the messages, so a thread that closed is settled in the same
        # pass that noticed it was missing rather than the next one.
        reconcile_closures(con, d, {t["id"] for t in threads}, stats)
        sync_messages(con, d, threads, stats)
        if full:
            # Rotating, so its cursor advances once a minute as it always
            # has: a fast pass calling it would spin the rotation twelve
            # times faster and spend 11 GETs a pass looking for something
            # that turns up about never.
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


def _pause(stop, seconds) -> bool:
    """Sleep between passes, unless asked to stop. True means stop now.

    `Event.wait` is what makes a thread's shutdown prompt: a plain sleep
    would hold the whole application closed for up to a full beat while it
    finished counting down.
    """
    seconds = max(0.0, seconds)
    if stop is None:
        time.sleep(seconds)
        return False
    return stop.wait(seconds)


def run(con, d: Discord, guild: str, db: str, *, fast: int = FAST_SECONDS,
        interval: int = CYCLE_SECONDS, backfill: bool = False,
        once: bool = False, stop=None) -> None:
    """The sync loop itself, so something other than a CLI can run it.

    Lifted out of `main()` whole rather than reimplemented. The packaging
    note calls this the one real refactor in its plan, and the reason is the
    slow beat: it carries more than `cycle()` -- the run pruning, the
    state-channel pull and the Jira roster all hang off it, each with its own
    argument for being on this loop rather than the outbox's. A supervisor
    that re-derived any of that would drift from the CLI the first time one
    of them changed.

    `stop` is a `threading.Event`; nothing else about the loop moved.
    """
    first = True
    next_full = 0.0        # the first pass is a full one
    while True:
        if stop is not None and stop.is_set():
            return
        t0 = time.time()
        full = t0 >= next_full
        if full:
            next_full = t0 + interval
        try:
            s = cycle(con, d, guild, do_backfill=backfill and first,
                      full=full)
            # A quiet fast pass says nothing. Twelve times the passes is
            # twelve times the log, and eleven of every twelve lines would
            # read "nothing happened" -- which buries the ones that matter in
            # the file somebody opens when something has gone wrong.
            noisy = (s["messages_new"] or s["edits_found"]
                     or s["titles_changed"] or s["deletions_found"]
                     or s["closed_in_discord"])
            if full or noisy:
                print(f"[{now()[:19]}] threads={s['threads_seen']} "
                      f"new={s['messages_new']} edits={s['edits_found']} "
                      f"titles={s['titles_changed']} "
                      f"deleted={s['deletions_found']} "
                      f"closed={s['closed_in_discord']} "
                      f"({time.time()-t0:.1f}s){'' if full else ' fast'}")
        except Exception as e:
            print(f"[{now()[:19]}] cycle failed: {e}", file=sys.stderr)

        if not full:
            # Everything below is on the slow beat by measurement: the state
            # pull and the roster are both `/channels/{id}/messages`-shaped
            # work against buckets far tighter than the thread listing's.
            if _pause(stop, fast - (time.time() - t0)):
                return
            first = False
            continue

        prune_runs(con)

        # Pulling the state channel is Discord -> SQLite like everything else
        # here, so it belongs in this loop. Pushing the other way does not:
        # the outbox is the only thing that writes to Discord, and it
        # publishes from its own loop.
        state_channel = os.environ.get("STATE_CHANNEL_ID")
        if state_channel:
            try:
                # Imported here because ernie_state imports this module, and a
                # top-level import either way round would be circular.
                import ernie_state
                r = ernie_state.reconcile(d, state_channel, db)
                if r["format_skew"]:
                    # Loud, every cycle it is true, and not folded in with the
                    # cards merely waiting on a thread: this one means the two
                    # boards have stopped agreeing and no amount of waiting
                    # will settle it.
                    them = r["format_skew"][-1]["v"]
                    print(f"[{now()[:19]}] state: !! {len(r['format_skew'])} "
                          f"card(s) in the channel are format v{them} and this "
                          f"machine speaks v{ernie_state.FORMAT_VERSION} -- one "
                          f"of the two boards needs updating", file=sys.stderr)
                if r["applied"] or r["unknown"]:
                    print(f"[{now()[:19]}] state: applied {len(r['applied'])}, "
                          f"{len(r['unknown'])} waiting on a thread")
                    for hit in r["applied"]:
                        print(f"    {hit['thread'][-6:]} {hit['by'] or '?'}: "
                              + "; ".join(hit["changed"]))
                # The published build, off a pinned note in the same
                # channel. Its own request rather than something read out of
                # the pull, because the pull is about cards and this is one
                # pin -- keeping them apart means neither can break the
                # other. One GET a minute, against a budget measured at
                # 0.60/s.
                def published():
                    row = con.execute(
                        "SELECT version FROM release_seen WHERE id=1"
                    ).fetchone()
                    return row["version"] if row else None

                before = published()
                ernie_state.note_release(
                    con, ernie_state.read_release(d, state_channel))
                con.commit()
                after = published()
                # Only a change speaks, the rule note_collisions follows: a
                # line every minute saying the build is still the build is
                # the one nobody reads when it finally says something else.
                if before != after:
                    print(f"[{now()[:19]}] release: "
                          + (f"the channel says {after} is current"
                             if after else "the release note is gone"))
            except Exception as e:
                print(f"[{now()[:19]}] state pull failed: {e}", file=sys.stderr)

        # The customer roster, Jira -> SQLite. Read-only against Jira, so it
        # belongs in this loop for the same reason the state pull does, and
        # for the same reason it does not belong in the outbox. Its own slow
        # heartbeat, though: the list changes about never, and asking on every
        # cycle would be 1440 searches a day to learn nothing.
        try:
            # Imported here because ernie_jira imports load_env from this
            # module, and a top-level import either way round would be
            # circular -- the same reason ernie_state is imported above.
            import ernie_jira
            cfg = ernie_jira.configured()
            if cfg and ernie_jira.due(con):
                cs = ernie_jira.run_once(con, cfg)
                print(f"[{now()[:19]}] clients: {cs['seen']} seen, "
                      f"{cs['offered']} offered, "
                      f"{len(cs['written'])} aliases written")
                # Only when the set has changed. A known collision -- IPI
                # has been two live customers since the roster arrived --
                # would otherwise print every hour for ever, and an alarm
                # that never stops is the one nobody reads when a new one
                # turns up. Clearing speaks too, so the log says when it
                # went away as well as when it came.
                if ernie_jira.note_collisions(con, cs["collisions"]):
                    if cs["collisions"]:
                        for c in cs["collisions"]:
                            print(f"    collision: {c['short_name']!r} <- "
                                  + ", ".join(x["client_id"]
                                              for x in c["clients"]),
                                  file=sys.stderr)
                    else:
                        print(f"[{now()[:19]}] clients: no short-name "
                              f"collisions any more")
        except Exception as e:
            print(f"[{now()[:19]}] client pull failed: {e}", file=sys.stderr)

        first = False
        if once:
            return
        # Against the top of this pass, not the end of it, so a slow pass
        # does not push the next one out by however long it took.
        if _pause(stop, fast - (time.time() - t0)):
            return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version",
                    version=ernie_version.describe())
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--env", default="ernie.env",
                    help="which env file to load (ernie.env, ernie-test.env)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="also pull archived history for channels not yet done")
    ap.add_argument("--fast", type=int, default=FAST_SECONDS,
                    help="seconds between listing passes -- the beat a new "
                         "ticket arrives on")
    ap.add_argument("--interval", type=int, default=CYCLE_SECONDS,
                    help="seconds between full passes: the edit rescan, the "
                         "state channel and the customer roster")
    a = ap.parse_args()

    # A fast beat longer than the full one is the two arguments swapped, and
    # zero is a loop with no sleep in it. Both are worth saying rather than
    # discovering from a rate limit.
    if a.fast < 1:
        sys.exit("--fast must be at least 1 second")
    if a.fast > a.interval:
        sys.exit(f"--fast {a.fast} is longer than --interval {a.interval}; "
                 f"the fast pass is the one that runs more often")

    load_env(a.env)

    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("DISCORD_GUILD_ID")
    allow = os.environ.get("ALLOW_DISCORD_WRITES")
    if not token or not guild:
        sys.exit("DISCORD_TOKEN and DISCORD_GUILD_ID must be set")

    con = load.connect(a.db)
    d = Discord(token, guild, allow_writes_for=allow)

    # Say out loud which server and which mode, every start. If this line ever
    # surprises you, stop before it does anything. The build goes first: it is
    # the question asked after the fact, off a log, when two boards disagree.
    print(f"ernie_sync {ernie_version.describe()}")
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

    run(con, d, guild, a.db, fast=a.fast, interval=a.interval,
        backfill=a.backfill, once=a.once)


if __name__ == "__main__":
    main()

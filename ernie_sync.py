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
ANNOUNCE_MAX = 3      # closures announced in one pass; past this it is catch-up
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
CYCLE_SECONDS = 60    # the rescan and the roster, on the beat they already had
# The state-channel pull, between the two. It is the receiving half of the
# shared board, so this interval is most of what somebody waiting on the other
# laptop's change is actually waiting for -- on the 60s pass it was the largest
# single term in the ~50s a card took to cross, bigger than the publish beat
# that sent it and the poll that draws it put together.
#
# Affordable because it is **one GET**: every card in the channel fits in one
# page of 100, and production holds 59 messages for 42 open cards. The budget
# is the part to watch. `/channels/{id}/messages` is 5 per ~5s and 429s on the
# sixth, and **two boards share it**, because they share the bot -- two
# machines here spend 6 pulls a minute on that route against the two rescans'
# ~24, which leaves it about where it already sat.
#
# Do not take it below the rescan's own burst without measuring: that fires
# RESCAN_PER_CYCLE requests back to back on the same route and already rides
# out a 429 doing it.
STATE_SECONDS = 20


# The one guild nothing here may touch. Every destructive or fabricating
# script guards on it: `wipe_test.py` deletes threads, `seed_test_server.py`
# creates them, `tools/fake_stats_data.py` invents history, and
# `ernie_state.py --check` refuses a preflight there.
#
# Declared once, here, because a constant copied into three files is three
# chances to fix one of them and believe the job is done.
# `tests/check_guards.py` holds the copy in `tools/` to this one, and holds
# every guard to still asking the question -- a comparison that was deleted
# looks exactly like one that returns False.
#
# Verifying the value is a person's job and takes one line: it must equal
# DISCORD_GUILD_ID in production's env file. Nothing in the repo can check
# that, because that file is deliberately not in the repo.
PRODUCTION_GUILD = "924120427469623297"

# Where a frozen build keeps what it has to be able to write: the env file
# the installer leaves, the databases, the logs. Beside the executable is
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

    def write(self, method: str, path: str, retry_5xx: bool = False, **body):
        """Any non-GET call goes through here, and here checks the guard.

        **`retry_5xx` is off by default, and the default is the safety.** A
        5xx does not say whether the request was processed, so retrying a POST
        can post twice -- the failure `events.sent_steps` exists to prevent,
        and which once put three identical "marked this complete" messages
        into one customer thread. Only the caller knows whether repeating is
        harmless, so the decision is theirs.

        **Opt in only where repeating is a genuine no-op**: archiving an
        archived thread, editing a message to the text it holds, pinning a
        pinned message. **Never** for posting a message, opening a thread, or
        renaming one -- a rename is two per ten minutes on a shared budget and
        posts a system message each time.
        """
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
            # Discord being briefly unwell, where the caller has said that
            # doing this twice is the same as doing it once.
            if retry_5xx and r.status_code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
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


# The one-machine switch, and it gates reopens as well as closures -- both
# are "what happened to this thread in Discord", and every stack sees both.
# Defined in ernie_load because `load_thread` needs it too and the import
# runs that way; named here for the readers that already say so.
announce_thread_changes = load.announce_thread_changes


def reconcile_closures(con, d: Discord, active: set, stats: dict) -> None:
    """Cards whose thread has gone quiet: ask Discord whether it was closed.

    Archiving is how work finishes, and an archived thread is simply not in
    `/guilds/{id}/threads/active` -- so without this the card is never
    completed and a ticket closed in Discord sits on the board for ever.

    **Absence is the question, never the answer.** A card missing from the
    listing earns a `GET /channels/{id}`, and is closed on what Discord says
    in the reply rather than on having been missing. A listing short for any
    other reason -- a hiccup, a permission change -- would otherwise close
    half the board in one pass. It costs nothing when nothing has happened,
    which is why it can run on the fast beat.

    The time is Discord's `archive_timestamp`, not now: the event says when
    the work finished. Who did it needs **View Audit Log** (see
    `who_archived`); without it the closure is recorded with no name rather
    than not recorded at all.
    """
    # A thread Ernie archived itself is not evidence that somebody closed it
    # in Discord, and `archived_by_ernie` has recorded which is which all
    # along -- nothing read it.
    #
    # The case is undo. Pressing Close in Bert completes the card and the
    # outbox archives the thread; undoing it afterwards clears
    # `completed_at` and puts the card back, but the thread stays archived
    # until the correction message posts into it, which is what unarchives
    # it. In that window the card is open and its thread is missing from the
    # listing, so this closed it again -- as a *Discord* closure, stamped
    # with Ernie's own archive_timestamp and attributed to nobody.
    #
    # Silent before closures were announced, which is why it went unnoticed:
    # it read as the card simply refusing to come back. Seen once it had a
    # voice, as a second message in the thread 73 seconds after the first
    # saying it had been closed in Discord when it had been closed in Bert
    # and then taken back.
    gone = [r["thread_id"] for r in con.execute(
        """SELECT c.thread_id FROM cards c
           JOIN threads t USING (thread_id)
           JOIN watched_channels w ON w.channel_id = t.parent_id
           WHERE c.completed_at IS NULL AND w.generate_cards = 1
             AND t.archived_by_ernie = 0""")
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

    # Announced only when this board watched it happen. Telling the thread
    # costs an unarchive, a message and a re-archive, so it lands in the
    # sidebar of everybody on it -- which is the point for one closure and
    # noise for twelve. At a 5s beat a pass finding more than a handful at
    # once is a machine catching up rather than one watching: a stack started
    # after a weekend, a channel coming back into `watched`, a permission
    # restored. A backlog announcing itself as news is the failure
    # `witnessed_start` exists to prevent one table along.
    on = announce_thread_changes()
    say = on and len(closed) <= ANNOUNCE_MAX
    if on and not say:
        print(f"  {len(closed)} closed at once -- recorded, not announced")

    for tid, when in closed.items():
        who = by.get(tid)
        # seen_open_at goes too -- see the outbox, which archives the
        # other way round. A clock left running on an archived thread
        # lets the next unarchive skip the wait.
        con.execute(
            "UPDATE threads SET archived=1, last_synced_at=?, "
            "seen_open_at=NULL WHERE thread_id=?", (now(), tid))
        con.execute(
            "UPDATE cards SET completed_at=?, completed_by=?, updated_at=? "
            "WHERE thread_id=?", (when, who, now(), tid))
        # new_value carries where it happened, so the feed can say so
        # without inventing a person to attribute it to, and the outbox can
        # phrase the thread message off the same field.
        #
        # The dispatch is what tells the thread, and it is immediate rather
        # than held for the undo window: undo refuses this verb outright and
        # points at reopen, so there is nothing to wait for. NULL leaves the
        # closure recorded and silent, which is what every machine but the
        # announcing one does.
        con.execute(
            """INSERT INTO events (event_id, occurred_at, actor_name, thread_id,
                                   verb, new_value, dispatch_after)
               VALUES (?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), when, who, tid, "completed", CLOSED_IN_DISCORD,
             now() if say else None))
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


def pull_state(con, d: Discord, db: str) -> None:
    """The state channel, Discord -> SQLite. One GET, on `STATE_SECONDS`.

    Discord -> SQLite like everything else in this module, which is why it
    lives here at all; pushing the other way is the outbox's job, because the
    outbox is the only thing that writes to Discord.

    Lifted out of `run` when it earned a beat of its own. It rode the slow
    pass with the rescan and the roster on the argument that all three are
    `/channels/{id}/messages`-shaped -- true of the route and wrong about the
    quantity, and the quantity is what a budget is spent in.

    The release note is deliberately left behind on the slow beat. It is a
    second request, and a pinned note naming the current build changes about
    never, so asking three times as often buys nothing.
    """
    state_channel = os.environ.get("STATE_CHANNEL_ID")
    if not state_channel:
        return
    try:
        # Imported here because ernie_state imports this module, and a
        # top-level import either way round would be circular.
        import ernie_state
        r = ernie_state.reconcile(d, state_channel, db)
        if r["format_skew"]:
            # Loud, every pass it is true, and not folded in with the cards
            # merely waiting on a thread: this one means the two boards have
            # stopped agreeing and no amount of waiting will settle it.
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
        # Its own line, and to stderr, because this is the one outcome where
        # somebody's change was thrown away. It printed nothing at all until
        # 2026-09-18: the test above asks only about `applied` and `unknown`,
        # so a conflict-resolved pull was indistinguishable from a quiet one --
        # which is how an undo came to be reverted twice with the log showing
        # a single "applied 1".
        if r["conflicts"]:
            print(f"[{now()[:19]}] state: !! {len(r['conflicts'])} card(s) "
                  f"had a change of ours overruled by the channel",
                  file=sys.stderr)
            for hit in r["conflicts"]:
                lost = ", ".join(hit.get("discarded") or []) or "?"
                print(f"    {hit['thread'][-6:]} lost {lost} to "
                      f"{hit['by'] or 'the other board'}", file=sys.stderr)
    except Exception as e:
        print(f"[{now()[:19]}] state pull failed: {e}", file=sys.stderr)


def run(con, d: Discord, guild: str, db: str, *, fast: int = FAST_SECONDS,
        interval: int = CYCLE_SECONDS, state_every: int = STATE_SECONDS,
        backfill: bool = False, once: bool = False, stop=None) -> None:
    """The sync loop itself, so something other than a CLI can run it.

    Lifted out of `main()` whole rather than reimplemented. The packaging
    note calls this the one real refactor in its plan, and the reason is the
    beats: this carries more than `cycle()` -- the run pruning and the Jira
    roster on the slow one, the state-channel pull on its own, each with its
    own argument for being on this loop rather than the outbox's. A supervisor
    that re-derived any of that would drift from the CLI the first time one
    of them changed.

    Three beats, not two: `fast` lists threads, `state_every` pulls the shared
    board, `interval` does the rescan and the roster. The middle one exists
    because it is one request and somebody is waiting on it; see
    `STATE_SECONDS`.

    `stop` is a `threading.Event`; nothing else about the loop moved.
    """
    first = True
    next_full = 0.0        # the first pass is a full one
    next_state = 0.0       # and pulls the state channel with it
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

        # Its own beat, between the listing's and the full pass's. See
        # STATE_SECONDS: it is one request, and it is the half of the shared
        # board that somebody is waiting on.
        if t0 >= next_state:
            next_state = t0 + state_every
            pull_state(con, d, db)

        if not full:
            # What is left below is on the slow beat by measurement: the
            # rescan is RESCAN_PER_CYCLE `/channels/{id}/messages` requests a
            # pass and the roster is a Jira search. The state pull was here
            # too until it was measured at one request.
            if _pause(stop, fast - (time.time() - t0)):
                return
            first = False
            continue

        prune_runs(con)

        state_channel = os.environ.get("STATE_CHANNEL_ID")
        if state_channel:
            try:
                import ernie_state
                # The published build, off a pinned note in the same
                # channel. Its own request rather than something read out of
                # the pull, because the pull is about cards and this is one
                # pin -- keeping them apart means neither can break the
                # other. One GET a minute, against a budget measured at
                # 0.60/s.
                def published():
                    row = con.execute(
                        "SELECT version, minimum FROM release_seen WHERE id=1"
                    ).fetchone()
                    if not row:
                        return None
                    return (row["version"], row["minimum"] or "")

                before = published()
                ernie_state.note_release(
                    con, ernie_state.read_release(d, state_channel))
                con.commit()
                after = published()
                # Only a change speaks, the rule note_collisions follows: a
                # line every minute saying the build is still the build is
                # the one nobody reads when it finally says something else.
                if before != after:
                    if not after:
                        said = "the release note is gone"
                    else:
                        v, floor = after
                        said = f"the channel says {v} is current"
                        # Worth its own half-sentence: this one takes boards
                        # away, and a line saying so is the only record that
                        # anybody chose to.
                        if floor:
                            said += f", and {floor} is the minimum"
                    print(f"[{now()[:19]}] release: {said}")
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
                    help="seconds between full passes: the edit rescan and "
                         "the customer roster")
    ap.add_argument("--state-every", type=int, default=STATE_SECONDS,
                    dest="state_every",
                    help="seconds between state-channel pulls -- the beat the "
                         "other board's changes arrive on")
    a = ap.parse_args()

    # A fast beat longer than the full one is the two arguments swapped, and
    # zero is a loop with no sleep in it. Both are worth saying rather than
    # discovering from a rate limit.
    if a.fast < 1:
        sys.exit("--fast must be at least 1 second")
    if a.fast > a.interval:
        sys.exit(f"--fast {a.fast} is longer than --interval {a.interval}; "
                 f"the fast pass is the one that runs more often")
    # The pull is checked at both ends, because it sits between the other two
    # and either side of it is a mistake worth naming. Below `--fast` it cannot
    # run more often than the loop turns; above `--interval` it is slower than
    # the pass it was taken off, which is the change undone rather than made.
    if a.state_every < a.fast:
        sys.exit(f"--state-every {a.state_every} is shorter than --fast "
                 f"{a.fast}; the loop only turns every {a.fast}s, so it "
                 f"cannot pull more often than that")
    if a.state_every > a.interval:
        sys.exit(f"--state-every {a.state_every} is longer than --interval "
                 f"{a.interval}; it used to ride the full pass, so that is "
                 f"slower than not having this argument at all")

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
        state_every=a.state_every, backfill=a.backfill, once=a.once)


if __name__ == "__main__":
    main()

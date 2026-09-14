"""Rebuild production's customer threads in the test server, from the mirror.

The sandbox's threads are invented, and invented threads are tidy: one client
spelling, one equipment kind, a handful of messages, no attachments and no
argument halfway through about which reel actually went out. Production is
none of those things, and every surprise this project has had came from the
difference. So this puts the real board in front of the real client.

**Production's Discord is never opened.** The mirror already holds all of it
-- titles, every revision, every message and its embeds -- pulled read-only by
the sync. This reads that file and writes to the test server, so the only
Discord it can reach is the one it is pointed at, and that one is guarded.

Two guards, because this writes: `PRODUCTION_GUILD` is refused outright the
way `wipe_test.py` and `seed_test_server.py` refuse it, and every write goes
through `ernie_sync.Discord.write()`, which refuses unless
`ALLOW_DISCORD_WRITES` names the guild. That is deliberately a different
client from the seeder's own -- a second bespoke HTTP client is a second place
the guard can be forgotten.

**What cannot come across, and it is worth knowing before reading the board:**

- **Nobody's name.** A bot cannot post as somebody else, so every message
  arrives authored by the bot. `is_bot` is therefore 1 on all of them, which
  means `last_human_at` is NULL and no copied card shows its `Last reply`
  age. The names are put in the text instead so the thread still reads as a
  conversation; `--no-names` posts the content verbatim.
- **Who opened the thread.** Same reason. Every copy is Ernie's own, so
  `witnessed_start` is false and no `started` line appears -- and `Complete`
  takes the easy path on a thread the bot owns, which is the one production
  case the sandbox has never been able to rehearse. That is written down in
  CLAUDE.md and this does not change it.
- **Attachments.** 329 messages carry one and the files are not in the
  mirror; the message comes across without it.
- **The original timestamps.** Discord stamps a message when it is posted, so
  a thread from April arrives dated today. The board's ordering comes from
  `rank`, not from dates, but the figures panel reads `created_at` and will
  show this lot as one enormous day.

Resumable, because it is a long run of writes and a failure halfway through
must not start again at the top: every thread and message it creates is
recorded in a ledger first, and a second run skips what the ledger already
names. That is the same reasoning `events.sent_steps` follows in the outbox.

    python tools/clone_prod_threads.py --dry-run
    python tools/clone_prod_threads.py --closed-within 60
"""

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ernie_sync import Discord, PRODUCTION_GUILD, load_env

# Discord's own caps, and the pace the seeder settled on.
CONTENT_CAP = 2000
PACING = 0.4
THREAD_PACING = 1.2      # thread creation is metered harder than posting

# Embed keys Discord accepts on the way in. The mirror stores what came out,
# which carries receive-only fields that a POST rejects.
EMBED_KEYS = {"title", "description", "url", "color", "fields",
              "footer", "author", "timestamp", "thumbnail", "image"}


def readable(path: str) -> sqlite3.Connection:
    """The mirror, opened so it cannot be written to even by mistake."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def chosen(con, closed_within: int, limit: int) -> list:
    """Open cards, plus ones closed recently enough to still be interesting.

    Oldest first, so the board fills in the order it originally happened and
    the ranks the sync gives it read the way production's do.
    """
    rows = con.execute(
        """SELECT c.thread_id, t.created_at, c.completed_at,
                  (SELECT name FROM thread_titles ti
                    WHERE ti.thread_id = c.thread_id
                    ORDER BY observed_at DESC LIMIT 1) AS name
             FROM cards c JOIN threads t ON t.thread_id = c.thread_id
            WHERE c.completed_at IS NULL
               OR julianday('now') - julianday(c.completed_at) <= ?
         ORDER BY t.created_at""", (closed_within,)).fetchall()
    rows = [r for r in rows if r["name"]]
    return rows[:limit] if limit else rows


def messages_of(con, thread_id: str) -> list:
    """Every message of a thread, in the order it was said.

    The newest revision of each, because the mirror is append-only and an
    edited message has more than one -- what belongs in the copy is what the
    message says now.
    """
    return con.execute(
        """SELECT m.message_id, m.author_display, m.author_name, m.is_bot,
                  r.content, r.embeds_json
             FROM messages m
             JOIN message_revisions r ON r.message_id = m.message_id
            WHERE m.thread_id = ?
              AND m.deleted_at IS NULL
              AND r.observed_at = (SELECT MAX(observed_at)
                                     FROM message_revisions r2
                                    WHERE r2.message_id = m.message_id)
         ORDER BY m.created_at""", (thread_id,)).fetchall()


def clean_embeds(raw: str) -> list:
    """The stored embeds, reduced to what Discord will accept back.

    Anything unparseable is dropped rather than guessed at: an embed is a
    nicety here, and a malformed one fails the whole message it rides on.
    """
    try:
        got = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for e in got if isinstance(got, list) else []:
        if not isinstance(e, dict):
            continue
        kept = {k: v for k, v in e.items() if k in EMBED_KEYS and v not in (None, "")}
        # A field with no name or value is rejected outright.
        if "fields" in kept:
            kept["fields"] = [f for f in kept["fields"]
                              if isinstance(f, dict) and f.get("name")
                              and f.get("value")][:25]
            if not kept["fields"]:
                kept.pop("fields")
        for key in ("thumbnail", "image"):
            if key in kept and not (isinstance(kept[key], dict)
                                    and kept[key].get("url")):
                kept.pop(key)
        if kept:
            out.append(kept)
    return out[:10]


def body_of(row, with_names: bool) -> dict:
    """What to post for one mirrored message, or {} if there is nothing to say.

    A bot cannot post as somebody else, so the name goes into the text -- the
    same thing Ernie already does when it opens a thread and then says whose
    it is. Without it a copied thread is a wall of anonymous lines and stops
    reading like the conversation it was.
    """
    content = (row["content"] or "").strip()
    embeds = clean_embeds(row["embeds_json"])
    if with_names and content and not row["is_bot"]:
        who = row["author_display"] or row["author_name"] or "someone"
        content = f"**{who}:** {content}"
    if len(content) > CONTENT_CAP:
        content = content[:CONTENT_CAP - 1] + "…"
    body = {}
    if content:
        body["content"] = content
    if embeds:
        body["embeds"] = embeds
    # Discord refuses a message that is neither. An attachment-only message
    # is the ordinary way this happens, and it is said rather than skipped so
    # the thread's shape survives.
    if not body:
        body["content"] = "_(an attachment, which the mirror does not hold)_"
    return body


class Ledger:
    """What has already been made, so a second run finishes rather than repeats.

    Written before the next write rather than after the last, and flushed
    every time -- a ledger kept in memory and saved at the end is lost in
    exactly the case it exists for.
    """

    def __init__(self, path: str):
        self.path = path
        self.data = {"threads": {}, "done": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, self.path)

    def thread_for(self, src):
        return self.data["threads"].get(src)

    def note_thread(self, src, new):
        self.data["threads"][src] = new
        self.save()

    def finished(self, src):
        return src in self.data["done"]

    def finish(self, src):
        self.data["done"].append(src)
        self.save()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default="prod-snapshot-20260910.db",
                    help="the mirror to read. Opened read-only.")
    ap.add_argument("--env", default="ernie-test.env")
    ap.add_argument("--closed-within", type=int, default=60, metavar="DAYS",
                    help="also bring closed cards this recent (default 60)")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="only the first N threads, for a look before the lot")
    ap.add_argument("--ledger", default="clone-ledger.json")
    ap.add_argument("--no-names", action="store_true",
                    help="post message text verbatim, without the speaker")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would be written and write nothing")
    a = ap.parse_args()

    if not os.path.exists(a.source):
        sys.exit(f"no such mirror: {a.source}")
    con = readable(a.source)
    threads = chosen(con, a.closed_within, a.limit)
    work = [(t, messages_of(con, t["thread_id"])) for t in threads]
    total_msgs = sum(len(m) for _, m in work)

    print(f"source {a.source} (read-only)")
    print(f"  {len(work)} threads, {total_msgs} messages")
    closed = sum(1 for t, _ in work if t["completed_at"])
    print(f"  of those, {closed} were closed in production and will be archived")
    est = (len(work) * THREAD_PACING + total_msgs * PACING) / 60
    print(f"  about {est:.0f} minutes of writes\n")

    if a.dry_run:
        for t, msgs in work[:8]:
            mark = " [closed]" if t["completed_at"] else ""
            print(f"  {t['name'][:68]}{mark}")
            print(f"     {len(msgs)} messages, "
                  f"{sum(1 for m in msgs if clean_embeds(m['embeds_json']))} with embeds")
        if len(work) > 8:
            print(f"  ... and {len(work) - 8} more")
        print("\ndry run: nothing was written")
        return

    load_env(a.env)
    token = os.environ.get("DISCORD_TOKEN")
    cid = os.environ.get("TEST_CHANNEL_ID")
    if not token or not cid:
        sys.exit(f"DISCORD_TOKEN and TEST_CHANNEL_ID must be set in {a.env}")

    # The guild the channel actually belongs to, asked of Discord rather than
    # taken from the env file -- the env names a guild, the channel is the
    # thing being written to, and a mismatch between them is exactly how a
    # tool ends up pointed somewhere nobody meant.
    probe = Discord(token, "", allow_writes_for=None)
    ch = probe.get(f"/channels/{cid}")
    if not ch:
        sys.exit(f"cannot read channel {cid} -- check the token and the id")
    guild = ch.get("guild_id")
    if guild == PRODUCTION_GUILD:
        sys.exit("REFUSING: that is the production guild. This writes threads; "
                 "point it at the test server.")

    d = Discord(token, guild,
                allow_writes_for=os.environ.get("ALLOW_DISCORD_WRITES"))
    print(f"writing into #{ch.get('name')} in guild {guild}")

    ledger = Ledger(a.ledger)
    made = posted = skipped = 0
    for n, (t, msgs) in enumerate(work, 1):
        src = t["thread_id"]
        if ledger.finished(src):
            skipped += 1
            continue

        tid = ledger.thread_for(src)
        if not tid:
            th = d.write("POST", f"/channels/{cid}/threads",
                         name=t["name"][:100], type=11,
                         auto_archive_duration=10080)
            tid = th["id"]
            # Recorded the instant it exists, so no retry can open a second.
            ledger.note_thread(src, tid)
            made += 1
            time.sleep(THREAD_PACING)

        for m in msgs:
            d.write("POST", f"/channels/{tid}/messages",
                    **body_of(m, not a.no_names))
            posted += 1
            time.sleep(PACING)

        if t["completed_at"]:
            d.write("PATCH", f"/channels/{tid}", archived=True)

        ledger.finish(src)
        print(f"  [{n}/{len(work)}] {t['name'][:60]}  ({len(msgs)} messages)")

    print(f"\nmade {made} threads, posted {posted} messages, "
          f"skipped {skipped} already done")
    print(f"ledger: {a.ledger}  (delete it to start over)")
    print("\nNext: python ernie_sync.py --once --backfill "
          f"--env {a.env} --db ernie-test.db")


if __name__ == "__main__":
    main()

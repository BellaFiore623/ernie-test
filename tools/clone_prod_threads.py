"""Rebuild production's customer threads in the test server, from the mirror.

The sandbox's invented threads are tidy; production's are not, and every
surprise this project has had came from the difference.

**Production's Discord is never opened** -- the mirror already holds it all,
and this only writes to the server it is pointed at. Two guards, because it
writes: `PRODUCTION_GUILD` is refused outright, and every write goes through
`ernie_sync.Discord.write()`.

What cannot come across: a bot cannot post as somebody else, so every message
and thread is Ernie's own (no `Last reply` age, no `started` line, and
`Complete` takes the easy path). Attachments are not in the mirror.
Timestamps are today's, so the figures panel reads the lot as one day.

**Components are not replayed, and that shifts a figure.** Python-Interface-Bot
posts its proposals as confirm-prompts with buttons; without them an
unconfirmed prompt arrives looking like a raised ticket -- 51 proposals in a
clone against production's 50. Left alone: a button that does nothing is
worse on a test board than an offset of one.

Resumable -- every thread and message is recorded in a ledger before it is
made, the way `events.sent_steps` works in the outbox.

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

# **Discord's own records, and only those.** A non-zero `type` does not mean
# a system message: **19 is a REPLY** and **20 and 21 are a bot answering a
# slash or context-menu command**, which is how Python-Interface-Bot posts
# its Build and Return embeds. Those are ordinary conversation and carry the
# content this whole exercise exists to copy.
#
# Read "type != 0" as "system" once, and it cost 357 real messages out of 388
# deleted -- 288 replies, 51 command answers, 18 context-menu answers --
# against 41 renames that genuinely needed removing. The check written to
# confirm the deletion counted what remained against the `type == 0` messages
# alone, so it shared the mistake and reported nothing real had gone.
#
# 4 is CHANNEL_NAME_CHANGE, which carries the new title as its content and
# becomes a fake paste if replayed. 6 is CHANNEL_PINNED_MESSAGE, which has no
# content at all. Those two are the whole list.
SYSTEM_TYPES = {4, 6}

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
                  COALESCE(m.type, 0) AS type, r.content, r.embeds_json
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
        self.data = {"threads": {}, "done": [], "posted": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        # A ledger written by an older run has no counts; an absent count is
        # nought, which is the right answer for a thread it never finished.
        self.data.setdefault("posted", {})

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

    def posted(self, src) -> int:
        """How many of this thread's messages already went out.

        The ledger used to record only "thread made" and "thread finished",
        so a failure part way through a hundred-message thread meant the
        resume began that thread again from its first line. Both deaths in
        the real run happened to land on thread creation, before any message
        -- which was luck, not design.
        """
        return int(self.data["posted"].get(src, 0))

    def note_posted(self, src, n):
        self.data["posted"][src] = n
        self.save()

    def finish(self, src):
        self.data["done"].append(src)
        # The count has done its work; the thread is done and will be skipped
        # whole from here.
        self.data["posted"].pop(src, None)
        self.save()


def prune_system(con, d, ledger, work, with_names: bool, dry: bool) -> dict:
    """Take back the system messages an earlier run posted as conversation.

    The clone used to replay everything, so Discord's own records -- renames
    above all -- arrived looking like somebody had typed a thread title into
    the chat. The extractor duly read equipment out of one and put a reel on
    a card production does not credit with one, which is how it was noticed:
    an equipment chip reading one higher than the real board.

    Matched by **content and counted**, not by content alone. A thread can
    hold a genuine message that says the same words as a rename -- that is
    the entire ambiguity -- so this removes at most as many as production
    says were system messages, oldest first, and leaves any surplus alone.
    Deleting somebody's real message to tidy up an artefact of ours would be
    a worse fault than the one being repaired.
    """
    counts = {"looked": 0, "deleted": 0, "missing": 0, "archived": 0}
    for t, msgs in work:
        src = t["thread_id"]
        tid = ledger.thread_for(src)
        if not tid:
            continue
        # **Discord refuses a delete in an archived thread**, with a 400
        # rather than anything that reads like the reason. Unarchiving to
        # tidy one up costs more than it fixes: a sync watching would see the
        # thread reopen, reopen its card, then close it again when the
        # archive went back -- and a reopen posts into the thread, so 132
        # closed tickets would each get a line about coming back to the
        # board. These are completed cards, off the board, and
        # `equipment_counts` reads open ones only, so the artefact there
        # costs nothing that is being looked at.
        if t["completed_at"]:
            counts["archived"] += 1
            continue
        wanted = {}
        for m in msgs:
            if m["type"] not in SYSTEM_TYPES:
                continue
            body = body_of(m, with_names).get("content", "")
            wanted[body] = wanted.get(body, 0) + 1
        if not wanted:
            continue
        counts["looked"] += sum(wanted.values())

        # Oldest first, so "at most this many" takes the earliest ones --
        # which is where a replayed record sits relative to a later reply.
        seen, before = [], None
        while True:
            page = d.get(f"/channels/{tid}/messages", limit=100,
                         **({"before": before} if before else {}))
            if not page:
                break
            seen.extend(page)
            if len(page) < 100:
                break
            before = page[-1]["id"]
        for m in reversed(seen):
            body = m.get("content") or ""
            if wanted.get(body):
                wanted[body] -= 1
                counts["deleted"] += 1
                if not dry:
                    # Deleting one that has already gone answers 404, which
                    # would raise; it is not worth riding out, so it is not
                    # opted in.
                    d.write("DELETE", f"/channels/{tid}/messages/{m['id']}")
                    time.sleep(PACING)
        counts["missing"] += sum(wanted.values())
    return counts


def restore_pruned(con, d, ledger, work, with_names: bool, dry: bool) -> dict:
    """Put back what `--prune-system` should never have taken.

    That pass read `type != 0` as "Discord's own record" and deleted 388
    messages from the open threads, of which **357 were conversation**: 288
    replies, 51 slash-command answers, 18 context-menu answers. Only the 41
    renames belonged in it. The embeds ride on the command answers, so all
    fifty parsed ticket proposals on open cards went with them, taking the
    equipment master, the client CR, the assignee and the amber issue chips.

    **It works from what is missing, not from a record of what was deleted.**
    Nothing wrote that record down, and a repair that trusted one would be
    trusting the same reasoning that caused the damage. So it compares the
    messages production holds -- every type except `SYSTEM_TYPES` -- against
    what the thread holds now, and posts only the shortfall. Counted, like
    the prune: a thread that legitimately says the same words twice keeps
    both, and one already whole gets nothing.

    That makes it safe to run twice, and safe to run on threads it never
    touched. **Archived threads are skipped for the same reason as before**:
    Discord refuses a write in one, and they were never pruned.

    What it cannot restore is position. The messages arrive at the end of
    the thread rather than where they were said, in their original order
    among themselves. Extraction does not care; a reader does. That was the
    trade taken against a ninety-minute reseed.
    """
    counts = {"looked": 0, "posted": 0, "whole": 0, "archived": 0}
    for t, msgs in work:
        src = t["thread_id"]
        tid = ledger.thread_for(src)
        if not tid:
            continue
        if t["completed_at"]:
            counts["archived"] += 1
            continue

        want = {}
        order = []
        for m in msgs:
            if m["type"] in SYSTEM_TYPES:
                continue
            body = body_of(m, with_names)
            key = body.get("content", "")
            want[key] = want.get(key, 0) + 1
            order.append((key, body))
        counts["looked"] += len(order)

        have, before = {}, None
        while True:
            page = d.get(f"/channels/{tid}/messages", limit=100,
                         **({"before": before} if before else {}))
            if not page:
                break
            for m in page:
                k = m.get("content") or ""
                have[k] = have.get(k, 0) + 1
            if len(page) < 100:
                break
            before = page[-1]["id"]

        missing = [(k, b) for k, b in order]
        short = []
        for k, b in missing:
            if have.get(k, 0) > 0:
                have[k] -= 1          # already there, account for it
            else:
                short.append(b)
        if not short:
            counts["whole"] += 1
            continue
        counts["posted"] += len(short)
        if dry:
            continue
        for body in short:
            # No retry_5xx: a 503 does not say whether the message landed,
            # and this pass exists because something was posted or deleted
            # on an assumption. A failure here leaves the rest for the next
            # run, which works from what is missing and will see it.
            d.write("POST", f"/channels/{tid}/messages", **body)
            time.sleep(PACING)
    return counts


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
    ap.add_argument("--restore-pruned", action="store_true",
                    help="re-post conversation an over-broad --prune-system "
                         "removed, rather than cloning anything")
    ap.add_argument("--prune-system", action="store_true",
                    help="delete system messages an earlier run replayed as "
                         "chat, rather than cloning anything")
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

    # The preview is about cloning. --prune-system is a different job and
    # needs the connection, so its dry run happens further down.
    if a.dry_run and not (a.prune_system or a.restore_pruned):
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

    if a.restore_pruned:
        got = restore_pruned(con, d, ledger, work, not a.no_names, a.dry_run)
        verb = "would post" if a.dry_run else "posted"
        print(f"  conversation production holds in the open threads: {got['looked']}")
        print(f"  {verb}: {got['posted']}")
        print(f"  threads already whole: {got['whole']}")
        print(f"  archived, never pruned: {got['archived']}")
        return

    if a.prune_system:
        got = prune_system(con, d, ledger, work, not a.no_names, a.dry_run)
        verb = "would delete" if a.dry_run else "deleted"
        print(f"  system messages production recorded: {got['looked']}")
        print(f"  {verb}: {got['deleted']}")
        if got["archived"]:
            print(f"  left alone in {got['archived']} archived threads "
                  f"-- Discord refuses a delete in one")
        if got["missing"]:
            print(f"  not found in the sandbox: {got['missing']} "
                  f"(already gone, or never posted)")
        return

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

        # Resume where this thread got to rather than at its first line.
        already = ledger.posted(src)
        for i, m in enumerate(msgs):
            # Discord's own records are not conversation and must not be
            # replayed as if they were. **Type 4 is a rename**, and it carries
            # the new title as its content -- posted as chat it becomes
            # somebody apparently pasting a thread title, which is exactly the
            # ambiguity `messages.type` is stored to end: 573 of production's
            # messages parse as a title and 550 of those were typed by people.
            # Replayed, 203 renames became 203 pastes, and the extractor read
            # equipment out of one of them and put a reel on a card that
            # production does not credit with one.
            #
            # Recreating them honestly is not available either: a rename would
            # have to be a real rename, at two per ten minutes.
            if m["type"] in SYSTEM_TYPES:
                ledger.note_posted(src, i + 1)
                continue
            if i < already:
                continue
            # Deliberately no retry_5xx: a 503 does not say whether the
            # message landed, and posting one twice is the thing the ledger
            # exists to stop. A failure here leaves the count where it is and
            # the next pass picks up from exactly this message.
            d.write("POST", f"/channels/{tid}/messages",
                    **body_of(m, not a.no_names))
            posted += 1
            ledger.note_posted(src, i + 1)
            time.sleep(PACING)

        if t["completed_at"]:
            # Archiving an archived thread is the same as archiving it once,
            # so this one may ride out a blip.
            d.write("PATCH", f"/channels/{tid}", archived=True, retry_5xx=True)

        ledger.finish(src)
        print(f"  [{n}/{len(work)}] {t['name'][:60]}  ({len(msgs)} messages)")

    print(f"\nmade {made} threads, posted {posted} messages, "
          f"skipped {skipped} already done")
    print(f"ledger: {a.ledger}  (delete it to start over)")
    print("\nNext: python ernie_sync.py --once --backfill "
          f"--env {a.env} --db ernie-test.db")


if __name__ == "__main__":
    main()

"""
Read-only Discord thread dumper.

Pulls active (and optionally archived) threads from a guild, plus every
message in each thread, and writes raw API JSON to disk. Makes no writes
to Discord whatsoever.

Setup:
    pip install httpx
    export DISCORD_TOKEN='your-bot-token'
    export DISCORD_GUILD_ID='123456789012345678'

Usage:
    python dump_threads.py                  # active threads only
    python dump_threads.py --archived       # also recently archived threads
    python dump_threads.py --list-guilds    # find your guild ID
"""

import argparse
import json
import os
import pathlib
import sys
import time

import httpx

API = "https://discord.com/api/v10"
OUT = pathlib.Path("dump")
PACING = 0.3  # seconds between requests; keeps us well under the limit


class Discord:
    def __init__(self, token: str):
        self.http = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "ernie-dump/0.1 (read-only survey)",
            },
            timeout=30.0,
        )

    def get(self, path: str, **params):
        """GET with 429 handling. Returns None on 403 (no access to channel)."""
        while True:
            r = self.http.get(path, params=params or None)

            if r.status_code == 429:
                wait = r.json().get("retry_after", 1.0)
                print(f"    rate limited, sleeping {wait:.1f}s", file=sys.stderr)
                time.sleep(wait + 0.1)
                continue

            if r.status_code == 403:
                return None  # bot lacks permission here; skip quietly

            r.raise_for_status()
            time.sleep(PACING)
            return r.json()

    # -- endpoints -------------------------------------------------------

    def guilds(self):
        return self.get("/users/@me/guilds")

    def channels(self, guild_id: str):
        return self.get(f"/guilds/{guild_id}/channels")

    def active_threads(self, guild_id: str):
        """Every non-archived thread in the guild, in a single call."""
        return self.get(f"/guilds/{guild_id}/threads/active")

    def archived_threads(self, channel_id: str, limit: int = 100):
        """Public archived threads under one channel, newest first."""
        out, before = [], None
        while True:
            params = {"limit": limit}
            if before:
                params["before"] = before
            page = self.get(f"/channels/{channel_id}/threads/archived/public", **params)
            if not page or not page.get("threads"):
                return out
            out.extend(page["threads"])
            if not page.get("has_more"):
                return out
            before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]

    def messages(self, channel_id: str):
        """
        All messages in a channel/thread, oldest first.

        Discord returns newest-first in pages of 100; `before` pages backwards
        through history using the oldest message ID seen so far.
        """
        out, before = [], None
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before
            page = self.get(f"/channels/{channel_id}/messages", **params)
            if not page:
                return list(reversed(out))
            out.extend(page)
            if len(page) < 100:
                return list(reversed(out))
            before = page[-1]["id"]


def summarize(thread, messages):
    """Print a one-line preview so you can eyeball the pull as it runs."""
    embeds = sum(len(m.get("embeds") or []) for m in messages)
    print(f"  {thread['name'][:60]:<62} {len(messages):>4} msg  {embeds:>3} embed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archived", action="store_true",
                    help="also pull recently archived threads")
    ap.add_argument("--list-guilds", action="store_true",
                    help="print guilds this bot is in, then exit")
    args = ap.parse_args()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN not set")

    d = Discord(token)

    if args.list_guilds:
        for g in d.guilds():
            print(f"{g['id']}  {g['name']}")
        return

    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not guild_id:
        sys.exit("DISCORD_GUILD_ID not set (run --list-guilds to find it)")

    OUT.mkdir(exist_ok=True)

    channels = d.channels(guild_id) or []
    (OUT / "channels.json").write_text(json.dumps(channels, indent=2))
    print(f"{len(channels)} channels")

    active = d.active_threads(guild_id)
    threads = list(active["threads"]) if active else []
    print(f"{len(threads)} active threads")

    if args.archived:
        # forum channels are type 15, standard text channels are type 0
        parents = [c for c in channels if c["type"] in (0, 15)]
        for c in parents:
            found = d.archived_threads(c["id"])
            if found:
                print(f"  +{len(found)} archived under #{c['name']}")
                threads.extend(found)

    print(f"\npulling messages for {len(threads)} threads\n")

    dump = []
    for t in threads:
        msgs = d.messages(t["id"])
        summarize(t, msgs)
        dump.append({"thread": t, "messages": msgs})

    path = OUT / "threads.json"
    path.write_text(json.dumps(dump, indent=2))

    total = sum(len(x["messages"]) for x in dump)
    print(f"\nwrote {len(dump)} threads / {total} messages to {path}")


if __name__ == "__main__":
    main()

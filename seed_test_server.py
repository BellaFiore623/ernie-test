"""
Seed a test Discord server with threads shaped like the real ones.

Writes to Discord -- run it ONLY against your own test guild. It refuses to
run if the guild id matches the production one.

Builds every edge case worth testing, including the ones that would take
months to appear naturally: '####' placeholders, '-- not found --' sentinels,
a PROD->OPS rename, an edited message, a deleted message, and an archived
thread you have to unarchive before posting.

    python seed_test_server.py            # reads ernie-test.env
    python seed_test_server.py --wipe     # archive everything first
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from ernie_sync import load_env

API = "https://discord.com/api/v10"
PACING = 0.4

# Guard: the real guild. Never seed into this.
PRODUCTION_GUILD = "1481003073894744226"


class Discord:
    def __init__(self, token: str):
        self.http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bot {token}",
                     "User-Agent": "ernie-seed/0.1"},
            timeout=30.0)

    def call(self, method: str, path: str, **json):
        while True:
            r = self.http.request(method, path, json=json or None)
            if r.status_code == 429:
                time.sleep(r.json().get("retry_after", 1) + 0.1)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
            time.sleep(PACING)
            return r.json() if r.content else {}

    def channel(self, cid):
        return self.call("GET", f"/channels/{cid}")

    def make_thread(self, cid, name):
        return self.call("POST", f"/channels/{cid}/threads",
                         name=name, type=11, auto_archive_duration=10080)

    def post(self, cid, content=None, embeds=None):
        body = {}
        if content:
            body["content"] = content
        if embeds:
            body["embeds"] = embeds
        return self.call("POST", f"/channels/{cid}/messages", **body)

    def edit(self, cid, mid, content):
        return self.call("PATCH", f"/channels/{cid}/messages/{mid}", content=content)

    def delete(self, cid, mid):
        return self.call("DELETE", f"/channels/{cid}/messages/{mid}")

    def rename(self, tid, name):
        return self.call("PATCH", f"/channels/{tid}", name=name)

    def archive(self, tid, on=True):
        return self.call("PATCH", f"/channels/{tid}", archived=on)


# The Build Request / Return Ticket panels that used to be built here, and the
# "Created PIP-...." confirmations that followed them, were Python-Interface-
# Bot's rather than Ernie's -- imitated so the parser had something real to
# read. They are gone from the seeded threads: Bert shows nothing from them
# yet, so a thread carrying one puts another bot's embed in front of the one
# message Ernie does own. `ernie_extract` still parses them, because
# production still has them and the rules about `-- not found --`, `####` and
# the varying "Existing Return ticket(s) (N)" count are all still live there.
# `tests/check_extract.py` is where those are exercised now.





# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------

LONG_THREAD = [
    "reel came back from site this morning",
    "fiber looks fine on the first 200ft",
    "got a kink at about 240, going to respool from there",
    "@ThreadGroup anyone got a spare drum?",
    "there's one on the shelf behind the bench",
    "grabbed it, thanks",
    "respooled, tension reads normal",
    "amber light came back on during the test run",
    "that's the third time this week on this unit",
    "pulling the head off to look at the connector",
    "connector pins look clean",
    "reseated it anyway",
    "ran it again, 400ft clean",
    "customer wants it back Thursday",
    "that's tight but doable",
    "packing it tonight",
    "hold on -- amber again at 380",
    "ok so it's not the connector",
    "swapping the controller board",
    "board swapped, running the full length now",
    "600ft, no amber",
    "letting it sit overnight before I call it",
    "still clean this morning",
    "boxing it up",
    "shipped, tracking sent to the customer",
] * 5        # 125 messages, past the rescan tail


CASES = [

    # **A long thread**, because three things behave differently past a
    # hundred messages and the sandbox's longest was five. `rescan_edits`
    # re-reads the last `RESCAN_TAIL` (100), so an edit further back than
    # that is out of its reach; the status message sits wherever it was
    # posted rather than near the top; and a card with this much history is
    # what the state channel's 2000-character cap is measured against.
    # Production's longest is 212.
    ("PROD: Westmoreland County - 02Sep26 - EReel-1204 respool and test", [
        (line, None) for line in LONG_THREAD
    ], None),
    # (title, [(content, embed) ...], extra)
    ("PROD: Edge AI Services - 03Aug26 - EReel-1060 fiber snapped", [
        ("@ThreadGroup we'll need to ship this today", None),
    ], None),

    # lowercase month
    ("PROD: Trekk - 04aug26 - SSD0008", [
        ("Bench check came back clean", None),
    ], None),

    # '####' placeholder -- pending, not an error
    ("OPS: Clinton MS - 24Aug26 - Order for Wheels and Domes", [
        ("Need ODE-#### spun up before Friday", None),
    ], None),

    # full month name
    ("PROD: PA Water Trade Show - 5June26 - Demo unit prep", [
        ("Booth setup is Thursday", None),
    ], None),

    # return ticket with the dynamic counted field
    ("OPS: Thrasher - 20Aug26 - EReel-1023 no amber light", [
        ("no amber light on the reel", None),
    ], None),

    # nested parens + markdown in the client name
    # This one carried nothing but the other bot's panel, so it was left with
    # no messages at all -- and an empty thread is not a case, it is a gap.
    ("OPS: SCI - 25Aug26 - Bot swap", [
        ("swapping the spare in while this one goes back", None),
    ], None),

    # queue prefixes beyond OPS/PROD
    ("ENG: Demo Portal - 26Jun26 - Update deliverable format", [
        ("Portal rewrite tracking", None),
    ], None),
    ("CS: Kenosha - 22Aug26 - Fogging on back camera", [
        ("Third report this month", None),
    ], None),

    # prefix only, no date -- a discussion thread, should not become a card
    ("OPS: outdated escalation list", [
        ("Who owns this now?", None),
    ], None),

    # malformed -- should land in the failure log
    ("Rhino needs to approve there inspections", [
        ("bumping this", None),
    ], None),

    # gets renamed PROD -> OPS after tickets exist
    ("PROD: Munhall - 26Aug26 - 1k reel", [
        ("Reel is staged", None),
    ], "rename"),

    # message gets edited after ingestion
    ("PROD: Reutzel - 27Aug26 - Dual laser replacement", [
        ("Original text, will be edited", None),
    ], "edit"),

    # message gets deleted after ingestion
    ("OPS: Diviney - 27Aug26 - 10 inch gooseneck", [
        ("This message gets deleted", None),
        ("This one stays", None),
    ], "delete"),

    # archived -- Ernie must unarchive before it can post
    ("PROD: IPI El Paso - 12Aug26 - SSD0210 damaged front camera", [
        ("Closing this out", None),
    ], "archive"),

    # ----------------------------------------------------------------------
    # Volume. The cases above are one of each edge; a board of fourteen is
    # too small to test two people working it -- nobody collides, no band
    # fills up, and the running order never gets long enough to scroll.
    # ----------------------------------------------------------------------

    ("PROD: Allegheny County - 28Aug26 - ODE-3114 gearbox rebuild", [
        ("Third rebuild on this unit", None),
    ], None),

    ("OPS: Beaver Falls - 29Aug26 - EReel-1188 spool jam", [
        ("Reel jams at about 40ft every time", None),
    ], None),

    ("PROD: Steel City Water - 30Aug26 - SSD0311 firmware rollback", [
        ("Customer wants the previous build back", None),
    ], None),

    ("CS: Latrobe - 30Aug26 - Camera head fogging after rain", [
        ("Same symptom as Kenosha", None),
    ], None),

    ("OPS: Greensburg - 31Aug26 - ODE-#### awaiting purchase order", [
        ("PO still not through", None),
    ], None),

    ("ENG: Reporting - 01Sep26 - Export drops trailing rows", [
        ("Repros on anything over 1k rows", None),
    ], None),

    ("PROD: Monroeville - 01Sep26 - Dual laser calibration", [
        ("Calibration jig is booked Thursday", None),
    ], None),

    ("OPS: Wilkinsburg - 01Sep26 - EReel-1204 no power", [
        ("Dead on arrival out of the crate", None),
    ], None),

    ("DATA: Fleet - 02Sep26 - Backfill equipment master ids", [
        ("Roughly 400 rows missing an EM", None),
    ], None),

    ("PROD: Ross Township - 02Sep26 - SSD0288 replacement chassis", [
        ("Chassis arrives Monday", None),
    ], None),

    ("CS: Bethel Park - 02Sep26 - Operator training refresher", [
        ("They've had three new starters", None),
    ], None),

    ("OPS: McKeesport - 02Sep26 - ODE-2977 wheel motor", [
        ("Left wheel motor stalls under load", None),
    ], None),

    ("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool", [
        ("Fiber respool plus a full bench check", None),
    ], None),

    ("OPS: Baldwin - 02Sep26 - Gooseneck 10in out of stock", [
        ("Substituting the 8in unless they object", None),
    ], None),

    # Two spellings of one customer, both carrying the same Client CR key --
    # tier 1 resolves them through PIP-8605 without comparing any strings.
    ("PROD: Dukes Root Control - 03Sep26 - EReel-1231 fiber respool", [
        ("Fiber went at the reel end again", None),
    ], None),

    ("OPS: Duke's Root Control - 03Sep26 - ODE-3140 wheel motor", [
        ("Right wheel motor is intermittent", None),
    ], None),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true",
                    help="archive existing threads before seeding")
    ap.add_argument("--env", default="ernie-test.env",
                    help="env file to read the token and channel from")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="seed only the first N cases. The full set is thirty "
                         "threads, which is a lot to read while iterating on "
                         "what a card or a message looks like.")
    a = ap.parse_args()

    # Every other script reads the env file; this one wanted the values
    # exported by hand, which is a papercut every time somebody reseeds.
    load_env(a.env)
    token = os.environ.get("DISCORD_TOKEN")
    cid = os.environ.get("TEST_CHANNEL_ID")
    if not token or not cid:
        sys.exit(f"DISCORD_TOKEN and TEST_CHANNEL_ID must be set, "
                 f"in {a.env} or the environment")

    # A short board while somebody is iterating on how a thread reads.
    cases = CASES[:a.limit] if a.limit else CASES
    d = Discord(token)
    ch = d.channel(cid)
    guild = ch.get("guild_id")

    if guild == PRODUCTION_GUILD:
        sys.exit("REFUSING: that's the production guild. Point this at your "
                 "test server.")

    print(f"seeding #{ch.get('name')} in guild {guild}\n")

    if a.wipe:
        active = d.call("GET", f"/guilds/{guild}/threads/active").get("threads", [])
        for t in active:
            if t.get("parent_id") == cid:
                d.archive(t["id"])
        print(f"archived {len(active)} existing threads\n")

    for title, messages, extra in cases:
        th = d.make_thread(cid, title)
        tid = th["id"]
        posted = []
        for content, embed in messages:
            m = d.post(tid, content=content,
                       embeds=[embed] if embed else None)
            posted.append(m["id"])

        note = ""
        if extra == "rename":
            d.rename(tid, title.replace("PROD:", "OPS:"))
            note = "  [renamed PROD->OPS]"
        elif extra == "edit":
            d.edit(tid, posted[0], "Edited text -- rescan should catch this")
            note = "  [message edited]"
        elif extra == "delete":
            d.delete(tid, posted[0])
            note = "  [message deleted]"
        elif extra == "archive":
            d.archive(tid)
            note = "  [archived]"

        print(f"  {title[:62]:<64}{note}")

    print(f"\n{len(cases)} threads seeded.\n"
          f"Point ernie.env at this guild and channel, use a separate db:\n"
          f"  python ernie_sync.py --once --db ernie-test.db")


if __name__ == "__main__":
    main()

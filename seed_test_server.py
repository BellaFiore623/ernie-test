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


# --------------------------------------------------------------------------
# Fake Python-Interface-Bot panels
# --------------------------------------------------------------------------

def build_embed(subject, equipment_master, client_cr, assignee="Brett Buttenfield"):
    return {
        "title": f"Build Request: {subject}",
        "color": 0x2ECC71,
        "fields": [
            {"name": "Equipment master", "value": equipment_master},
            {"name": "Client CR", "value": client_cr},
            {"name": "Priority", "value": "High", "inline": True},
            {"name": "Assignee", "value": assignee, "inline": True},
            {"name": "Due date", "value": "(none)", "inline": True},
            {"name": "Labels", "value": "Operations"},
        ],
    }


def return_embed(subject, equipment_master, client_cr, problem, eq_type,
                 template, referenced=None):
    fields = [
        {"name": "Equipment master", "value": equipment_master},
        {"name": "Client CR", "value": client_cr},
        {"name": "Reported problem", "value": problem},
        {"name": "Reporter", "value": "Hayden Ling", "inline": True},
        {"name": "Equipment type", "value": eq_type, "inline": True},
        {"name": "Template", "value": template, "inline": True},
        {"name": "Suggested labels", "value": "Damaged_Fiber"},
    ]
    if referenced:
        # dynamic field name -- the count varies, so exact-match lookups miss it
        fields.insert(0, {
            "name": f"Existing Return ticket(s) referenced in this thread ({len(referenced)})",
            "value": "\n".join(f"⚠️ {k} (Open) [Damaged_Fiber]" for k in referenced)})
    return {"title": f"Return Ticket: {subject}", "color": 0xE67E22, "fields": fields}


def created(pip, kind, em, cr):
    return (f"✅ Created **{pip}**: https://example.atlassian.net/browse/{pip}\n"
            f"Assigned to Brett Buttenfield\n"
            f"Linked to equipment master {em}\n"
            f"Linked to client CR {cr}\n"
            f"Posted {kind} link in thread.")


# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------

CASES = [
    # (title, [(content, embed) ...], extra)
    ("PROD: Edge AI Services - 03Aug26 - EReel-1060 fiber snapped", [
        ("@ThreadGroup we'll need to ship this today", None),
        (None, build_embed("Edge AI Services - EReel-1060 - 03Aug26",
                           "PIP-5167 (EReel-1060)",
                           "PIP-4863 (Edge AI Services : Laser (ST Client))")),
        (created("PIP-9467", "Build Request", "PIP-5167", "PIP-4863"), None),
    ], None),

    # lowercase month
    ("PROD: Trekk - 04aug26 - SSD0008", [
        ("Bench check came back clean", None),
    ], None),

    # '####' placeholder -- pending, not an error
    ("OPS: Clinton MS - 24Aug26 - Order for Wheels and Domes", [
        ("Need ODE-#### spun up before Friday", None),
        (None, build_embed("Clinton MS - ODE-#### - 24Aug26",
                           "-- not found --",
                           "PIP-7468 (Clinton, MS: **PURCHASE**)")),
    ], None),

    # full month name
    ("PROD: PA Water Trade Show - 5June26 - Demo unit prep", [
        ("Booth setup is Thursday", None),
    ], None),

    # return ticket with the dynamic counted field
    ("OPS: Thrasher - 20Aug26 - EReel-1023 no amber light", [
        ("no amber light on the reel", None),
        (None, return_embed("Thrasher - 20Aug26 : EReel-1023",
                            "PIP-5167 (EReel-1023)",
                            "PIP-4863 (Thrasher : Laser (ST Client))",
                            "EReel-1023 no amber light", "ereel",
                            "Return: EReel", referenced=["PIP-9468"])),
        (created("PIP-9469", "Return", "PIP-5167", "PIP-4863"), None),
    ], None),

    # nested parens + markdown in the client name
    ("OPS: SCI - 25Aug26 - Bot swap", [
        (None, build_embed("SCI - 25Aug26",
                           "PIP-7434 (ODE-2926)",
                           "PIP-4945 (SCI Infrastructure LLC. **PURCHASE** "
                           "(Should Have 3 Bots!))")),
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
        (None, build_embed("Munhall - 26Aug26", "-- not found --",
                           "PIP-7501 (Munhall Borough PA (MSSMA))")),
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
        (None, build_embed("Allegheny County - ODE-3114 - 28Aug26",
                           "PIP-7712 (ODE-3114)",
                           "PIP-4902 (Allegheny County DPW)")),
        (created("PIP-9501", "Build Request", "PIP-7712", "PIP-4902"), None),
    ], None),

    ("OPS: Beaver Falls - 29Aug26 - EReel-1188 spool jam", [
        ("Reel jams at about 40ft every time", None),
        (None, return_embed("Beaver Falls - 29Aug26 : EReel-1188",
                            "PIP-7719 (EReel-1188)",
                            "PIP-4915 (Beaver Falls Municipal Authority)",
                            "Spool jams under load", "ereel",
                            "Return: EReel", referenced=["PIP-9470", "PIP-9481"])),
    ], None),

    ("PROD: Steel City Water - 30Aug26 - SSD0311 firmware rollback", [
        ("Customer wants the previous build back", None),
    ], None),

    ("CS: Latrobe - 30Aug26 - Camera head fogging after rain", [
        ("Same symptom as Kenosha", None),
    ], None),

    ("OPS: Greensburg - 31Aug26 - ODE-#### awaiting purchase order", [
        ("PO still not through", None),
        (None, build_embed("Greensburg - ODE-#### - 31Aug26",
                           "-- not found --",
                           "PIP-7740 (City of Greensburg **PURCHASE**)")),
    ], None),

    ("ENG: Reporting - 01Sep26 - Export drops trailing rows", [
        ("Repros on anything over 1k rows", None),
    ], None),

    ("PROD: Monroeville - 01Sep26 - Dual laser calibration", [
        ("Calibration jig is booked Thursday", None),
        (None, build_embed("Monroeville - 01Sep26",
                           "PIP-7755 (SSD0402)",
                           "PIP-4931 (Monroeville Borough)")),
    ], None),

    ("OPS: Wilkinsburg - 01Sep26 - EReel-1204 no power", [
        ("Dead on arrival out of the crate", None),
        (None, return_embed("Wilkinsburg - 01Sep26 : EReel-1204",
                            "PIP-7761 (EReel-1204)",
                            "PIP-4940 (Wilkinsburg Sanitary Authority)",
                            "No power at all", "ereel", "Return: EReel")),
        (created("PIP-9512", "Return", "PIP-7761", "PIP-4940"), None),
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
        (None, build_embed("McKeesport - ODE-2977 - 02Sep26",
                           "PIP-7770 (ODE-2977)",
                           "PIP-4955 (McKeesport City : Laser (ST Client))")),
    ], None),

    ("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool", [
        ("Fiber respool plus a full bench check", None),
    ], None),

    ("OPS: Baldwin - 02Sep26 - Gooseneck 10in out of stock", [
        ("Substituting the 8in unless they object", None),
    ], None),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true",
                    help="archive existing threads before seeding")
    ap.add_argument("--env", default="ernie-test.env",
                    help="env file to read the token and channel from")
    a = ap.parse_args()

    # Every other script reads the env file; this one wanted the values
    # exported by hand, which is a papercut every time somebody reseeds.
    load_env(a.env)
    token = os.environ.get("DISCORD_TOKEN")
    cid = os.environ.get("TEST_CHANNEL_ID")
    if not token or not cid:
        sys.exit(f"DISCORD_TOKEN and TEST_CHANNEL_ID must be set, "
                 f"in {a.env} or the environment")

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

    for title, messages, extra in CASES:
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

    print(f"\n{len(CASES)} threads seeded.\n"
          f"Point ernie.env at this guild and channel, use a separate db:\n"
          f"  python ernie_sync.py --once --db ernie-test.db")


if __name__ == "__main__":
    main()

"""Delete the threads the seeder made, in the test channel. Test guild only.

**A thread somebody opened by hand is kept.** The seeder's threads are all
created by the bot, and a thread's creator may archive it whatever its
permissions say -- so a sandbox made only of them can never rehearse the
production case, where every thread belongs to one of 36 people. The only way
to have one is for a person to open it, and a reseed that deleted those threw
away the one fixture that cannot be rebuilt.

`--all` is the old behaviour, for when the channel genuinely wants clearing.
"""
import argparse, os, sys, time, httpx
from ernie_sync import PRODUCTION_GUILD, load_env

# Imported, not copied. This file used to carry its own value with the
# comment "set this to your real guild", and it never was -- so the
# guard on the one script here that deletes threads was decorative.

ap = argparse.ArgumentParser()
ap.add_argument("--all", action="store_true",
                help="delete threads a person opened too")
a = ap.parse_args()

load_env("ernie-test.env")
token = os.environ["DISCORD_TOKEN"]
cid = os.environ["TEST_CHANNEL_ID"]

h = httpx.Client(base_url="https://discord.com/api/v10",
                 headers={"Authorization": f"Bot {token}"}, timeout=30.0)

ch = h.get(f"/channels/{cid}").json()
guild = ch["guild_id"]
if guild == PRODUCTION_GUILD:
    sys.exit("REFUSING: that's production.")
print(f"wiping #{ch['name']} in {guild}")

me = h.get("/users/@me").json()["id"]
killed = kept = 0
for scope in ("active", "archived"):
    while True:
        if scope == "active":
            r = h.get(f"/guilds/{guild}/threads/active").json()
            threads = [t for t in r.get("threads", []) if t["parent_id"] == cid]
        else:
            r = h.get(f"/channels/{cid}/threads/archived/public",
                      params={"limit": 100}).json()
            threads = r.get("threads", [])
        if not threads:
            break
        # Whatever is left after the skips, or the loop never empties.
        doomed = [t for t in threads
                  if a.all or t.get("owner_id") == me]
        if not doomed:
            kept += len(threads)
            break
        for t in doomed:
            h.delete(f"/channels/{t['id']}")
            killed += 1
            print(f"  deleted {t['name'][:60]}")
            time.sleep(0.4)

print(f"\n{killed} threads deleted")
if kept:
    print(f"{kept} left alone -- opened by a person, and the seeder cannot "
          f"make another. --all deletes them too.")

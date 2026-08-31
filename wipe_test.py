"""Delete every thread in the test channel. Test guild only."""
import os, sys, time, httpx
from ernie_sync import load_env

PRODUCTION_GUILD = "1481003073894744226"   # set this to your real guild

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

killed = 0
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
        for t in threads:
            h.delete(f"/channels/{t['id']}")
            killed += 1
            print(f"  deleted {t['name'][:60]}")
            time.sleep(0.4)

print(f"\n{killed} threads deleted")

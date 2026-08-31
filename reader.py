import json, collections, re
d = json.load(open("dump/threads.json"))

# how many titles match the expected shape?
rx = re.compile(r'^(OPS|PROD):\s*(.+?)\s*-\s*(\d{1,2}[A-Za-z]{3}\d{2})\s*-\s*(.+)$')
hits = [x for x in d if rx.match(x["thread"]["name"])]
print(f"{len(hits)}/{len(d)} titles parse")

for x in d:
    if not rx.match(x["thread"]["name"]):
        print("  MISS:", x["thread"]["name"])

# what do the ticket embeds actually contain?
names = collections.Counter(
    f["name"]
    for x in d for m in x["messages"]
    for e in (m.get("embeds") or []) for f in (e.get("fields") or [])
)
print(names.most_common())

# Add this at the very end
input("\nPress Enter to exit...")
import json, collections, re
d = json.load(open("dump/threads.json"))
ch = {c["id"]: c for c in json.load(open("dump/channels.json"))}

rx = re.compile(r'^(OPS|PROD|ENG):\s*(.+?)\s*-\s*(\d{1,2}[A-Za-z]{3}\d{2})\s*-\s*(.+)$')

# where do threads live, and how many parse in each channel?
for pid, n in collections.Counter(x["thread"]["parent_id"] for x in d).most_common():
    hits = sum(1 for x in d
               if x["thread"]["parent_id"] == pid and rx.match(x["thread"]["name"]))
    c = ch.get(pid, {})
    print(f"{hits:>4}/{n:<5} #{c.get('name','?'):<28} type={c.get('type')}  {pid}")

# any embeds anywhere?
emb = [(x["thread"]["name"], m)
       for x in d for m in x["messages"] if m.get("embeds")]
print(f"\n{len(emb)} messages carry embeds")
if emb:
    print(json.dumps(emb[0][1]["embeds"][0], indent=2)[:1200])
    
# Add this at the very end
input("\nPress Enter to exit...")
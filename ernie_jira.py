"""
The customer list, pulled from Jira.

Read-only against Jira, the way ernie_sync is read-only against Discord: this
fetches Client CR issues and writes them into `clients`, and it never creates,
edits or transitions anything there.

It exists because client names are typed by hand into thread titles and drift.
Production carries 120 distinct spellings of 43 customers -- five ways of
writing Inspect.AI, two of RavanAir, and one title where the apostrophe in
Duke's came through as a replacement character. Jira already holds the list
those titles are all trying to name, keyed by the PIP-#### the tickets carry,
so the fix is to offer that list rather than to guess at the spellings after
the fact.

Two things come out of a run:

  clients          one row per Client CR, with the short name a thread title
                   should use
  client_aliases   every spelling already on the board, pointed at the client
                   it means, so existing cards resolve without being retitled

Inert unless JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN and JIRA_CLIENT_JQL are all
set, the way the change log is inert without its channel.

    python ernie_jira.py --check  --env ernie-test.env --db ernie-test.db
    python ernie_jira.py --once --report --env ernie-test.env --db ernie-test.db
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import sys
import time
from datetime import datetime, timezone

import httpx

import ernie_extract as ex
import ernie_load as load
import ernie_version
from ernie_sync import load_env

PACING = 0.1          # sleep after each call, as the Discord client does
PAGE = 100            # issues per search page
SYNC_EVERY_S = 3600   # the roster changes about never; don't ask every minute

# Fields worth asking for. `status` decides nothing -- the markers below are
# what was asked for -- but it costs nothing and is the obvious thing to reach
# for if the marker convention ever drifts from the workflow.
FIELDS = ["summary", "status", "issuetype", "parent"]

# What a client is, in Jira's words. The roster query has to select these and
# nothing else, and --check uses it to tell a query that is too narrow from a
# CR key that was never a client in the first place.
CLIENT_ISSUE_TYPE = "Customer Requirement"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Reading a Jira summary
# --------------------------------------------------------------------------

# A client the dropdown does not put forward. Match the *starred* form only:
# 'City of Superior WI : LENDING CALIB. BAR - Unpaused' contains the letters
# "paused" and is a live customer, while 'Long Beach : *Pending*' is not.
EXCLUDE = re.compile(r"\*+\s*(INACTIVE|PENDING|PAUSED)\s*\*+", re.I)

_PARENS = re.compile(r"\([^)]*\)")
_STARRED = re.compile(r"\*+[^*]+\*+")
# Only a *trailing* " - note", never a bare word mid-name. ex.normalise_client
# strips purchase|loaner|rental|demo wherever they appear, which is right for a
# comparison key and wrong here: it turns 'Edge AI Demo Team' into 'Edge AI
# Team' and eats a word that is part of the customer's name.
_TRAILING = re.compile(r"\s+[-–—]\s+.*$")
_TRIM = " -–—,"


def short_name(summary: str) -> str:
    """
    The name a thread title should call this client.

        'IPI : El Paso'                              -> 'IPI'
        'San Joaquin : (Replace Cable 1K -have ...)' -> 'San Joaquin'
        'RJN (and City of Baltimore) (ST Client)'    -> 'RJN'
        'GFT - *PURCHASE* (ST Client)'               -> 'GFT'

    The summary carries the account note as well as the name -- who they work
    under, what they bought, whether a bot is missing -- and none of that
    belongs in a thread title.
    """
    s, prev = summary, None
    # Jira nests them -- 'Reutzel ... (2 Bots) : (NEED Laser and GSN (ST
    # Client))' -- and one pass leaves a stray bracket behind.
    while prev != s:
        prev, s = s, _PARENS.sub(" ", s)
    s = s.split(":")[0]
    s = _STARRED.sub(" ", s)
    s = _TRAILING.sub("", s)
    s = re.sub(r"[()]", " ", s)
    return re.sub(r"\s+", " ", s).strip(_TRIM)


def is_offered(summary: str) -> bool:
    """
    Whether the dropdown puts this client forward.

    Tested against the summary as it came, before any cutting: 'Wilson
    Excavating: ACTIVE FOR 3RD PARTY CODING *INACTIVE*' carries its marker
    after the colon, and cutting first would keep the client on the list.
    """
    return not EXCLUDE.search(summary or "")


# --------------------------------------------------------------------------
# Jira client
# --------------------------------------------------------------------------

class Jira:
    """
    Read-only Jira client, shaped like ernie_sync.Discord.

    There is no write guard here because there are no writes: the only POST is
    the search endpoint, which is a read that happens to carry a body. Nothing
    in this file creates or edits a Jira issue, and nothing should.
    """

    def __init__(self, base_url: str, email: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url,
            auth=(email, token),             # Atlassian Cloud: email + API token
            headers={"Accept": "application/json",
                     "User-Agent": "ernie-jira/0.1"},
            timeout=30.0,
        )

    def _call(self, method: str, path: str, **kw):
        for attempt in range(5):
            r = self.http.request(method, path, **kw)
            if r.status_code == 429:
                # Jira answers in a Retry-After header rather than a body.
                time.sleep(float(r.headers.get("Retry-After", 1.0)) + 0.1)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(
                    f"Jira refused the credentials ({r.status_code}) for "
                    f"{method} {path}. Check JIRA_EMAIL and JIRA_TOKEN.")
            if r.status_code == 404:
                return None
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            time.sleep(PACING)
            return r.json() if r.content else {}
        r.raise_for_status()
        return None

    def myself(self) -> dict:
        return self._call("GET", "/rest/api/3/myself") or {}

    def is_client(self, key: str) -> bool:
        """Whether this key is a client, rather than merely a real issue.

        _call returns None on a 404. But existing is not the question: the
        sandbox's seeded threads carry CR keys that are real issues of the
        wrong kind -- PIP-4902 is a Build Request, PIP-4940 is a Bug -- and no
        widening of a client query would ever, or should ever, reach those.
        """
        d = self._call("GET", f"/rest/api/3/issue/{key}",
                       params={"fields": "issuetype"}) or {}
        t = ((d.get("fields") or {}).get("issuetype") or {}).get("name")
        return t == CLIENT_ISSUE_TYPE

    def search(self, jql: str) -> list[dict]:
        """Every issue the query matches, following the page tokens."""
        out, token = [], None
        while True:
            body = {"jql": jql, "fields": FIELDS, "maxResults": PAGE}
            if token:
                body["nextPageToken"] = token
            page = self._call("POST", "/rest/api/3/search/jql", json=body) or {}
            out += page.get("issues") or []
            token = page.get("nextPageToken")
            if not token or page.get("isLast"):
                return out


def client_rows(issues: list[dict]) -> list[dict]:
    """Jira issues -> the rows `clients` wants."""
    rows = []
    for i in issues:
        summary = ((i.get("fields") or {}).get("summary") or "").strip()
        if not summary:
            continue
        rows.append({
            "client_id": i.get("key"),
            "name": summary,
            "name_key": ex.normalise_client(summary),
            "short_name": short_name(summary),
            "offered": 1 if is_offered(summary) else 0,
        })
    return rows


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def sync_clients(con, rows: list[dict]) -> dict:
    """
    Upsert the roster. Idempotent, and a hand-written short name survives.

    Telling a hand-written one from a derived one is the whole difficulty.
    COALESCE could not: short_name is filled on the first insert, so it is
    never NULL again and was therefore never updated -- rename a client in
    Jira and the dropdown kept the old name for ever, which is the opposite
    of the intent. Re-deriving from the *previous* summary answers it: if the
    stored short name is exactly what that summary would have produced, then
    nobody has touched it and it may follow the rename.

    A client the pull no longer returns stops being offered rather than being
    deleted. Same rule as an *INACTIVE* one: the cards already carrying it
    keep their name, and it comes back if the query finds it again.
    """
    if not rows:
        # A pull that returned nothing is a failure, not an empty roster.
        # Retiring all 65 clients because Jira was briefly unreachable is not
        # a thing to do quietly.
        return {"seen": 0, "offered": 0, "retired": [], "renamed": [],
                "collisions": collisions(con)}

    before = {r["client_id"]: r for r in con.execute(
        "SELECT client_id, name, short_name FROM clients")}
    renamed = []
    rows = [dict(r) for r in rows]          # decided here, written below
    for r in rows:
        was = before.get(r["client_id"])
        if was is None:
            continue
        derived = short_name(was["name"] or "")
        if (was["short_name"] or "") != derived:
            r["short_name"] = was["short_name"]        # somebody set it by hand
        elif derived != r["short_name"]:
            renamed.append((r["client_id"], derived, r["short_name"]))

    stamp = now()
    for r in rows:
        con.execute(
            """INSERT INTO clients (client_id, name, name_key, short_name,
                                    offered, synced_at)
               VALUES (:client_id, :name, :name_key, :short_name, :offered,
                       :synced_at)
               ON CONFLICT(client_id) DO UPDATE SET
                   name       = excluded.name,
                   name_key   = excluded.name_key,
                   offered    = excluded.offered,
                   synced_at  = excluded.synced_at,
                   short_name = excluded.short_name
            """, dict(r, synced_at=stamp))
    # Anything the query no longer returns: retired, not removed.
    here = [r["client_id"] for r in rows]
    marks = ",".join("?" * len(here))
    retired = [x["client_id"] for x in con.execute(
        f"SELECT client_id FROM clients WHERE offered = 1 "
        f"AND client_id NOT IN ({marks})", here)]
    if retired:
        con.execute(f"UPDATE clients SET offered = 0 WHERE client_id NOT IN "
                    f"({marks})", here)
    con.commit()
    return {"seen": len(rows),
            "offered": sum(r["offered"] for r in rows),
            "retired": retired, "renamed": renamed,
            "collisions": collisions(con)}


def collisions(con) -> list[dict]:
    """
    Offered clients that would appear in the dropdown under the same label.

    Real: 'IPI : El Paso' (PIP-2136) and 'IPI : *REP*' (PIP-3927) both shorten
    to IPI, and both are live. Two identical rows in a list you pick from is
    worse than the typos this replaces, so they are reported rather than
    written and forgotten. The editor shows the full summary alongside, which
    is what tells them apart.
    """
    seen = collections.defaultdict(list)
    for r in con.execute("SELECT client_id, name, short_name FROM clients "
                         "WHERE offered = 1 AND short_name IS NOT NULL"):
        seen[(r["short_name"] or "").lower()].append(dict(r))
    return [{"short_name": k, "clients": v}
            for k, v in sorted(seen.items()) if len(v) > 1]


def collision_key(found: list) -> str:
    """The collision set as one comparable string, order-independent."""
    return "; ".join(sorted(
        f"{c['short_name']}={','.join(sorted(x['client_id'] for x in c['clients']))}"
        for c in found))


def note_collisions(con, found: list) -> bool:
    """Record the set, and say whether it is news.

    A collision is worth reporting the first time and not once an hour for
    ever after -- IPI has been two live customers since the roster arrived,
    a rule for it is owed, and repeating it hourly turns the one line that
    would matter into scenery. So the *set* is remembered and only a change
    speaks: a new collision shouts, and so does the last one clearing.
    """
    key = collision_key(found)
    was = con.execute("SELECT pairs FROM client_collisions WHERE id = 1").fetchone()
    if was is not None and was["pairs"] == key:
        return False
    con.execute(
        """INSERT INTO client_collisions (id, seen_at, pairs) VALUES (1,?,?)
           ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at,
                                         pairs=excluded.pairs""",
        (now(), key))
    con.commit()
    return True


def reconcile_aliases(con, actor: str = "auto") -> dict:
    """
    Point every client spelling already on the board at the client it means.

    Three tiers, and nothing fuzzy is ever merged:

      1. via the ticket's Client CR key -- the thread says PIP-8605, so its
         title spelling means PIP-8605, whatever it says. No string comparison
         is involved, which is why this tier collapses 'duke s root control'
         and 'dukes root control' onto one client without knowing they look
         alike.
      2. an exact match against a client's name or short name, for threads
         with no ticket.
      3. everything else is left alone and reported.

    Tier 3 is the point of the whole design. 'falmouth ma' and 'falmouth me'
    are 0.91 similar and are different places; 'fulton county north ga' and
    'fulton county south ga' likewise. A matcher confident enough to merge the
    Duke's spellings is confident enough to merge those, and losing one
    customer's tickets into another is not a trade worth making.
    """
    known = {r["client_id"] for r in con.execute("SELECT client_id FROM clients")}
    have = {r["raw_key"] for r in con.execute("SELECT raw_key FROM client_aliases")}

    # Tier 1: spelling -> the CR keys seen on threads carrying it.
    by_spelling = collections.defaultdict(set)
    for r in con.execute(
            """SELECT DISTINCT v.client_key AS k, p.client_cr AS cr
               FROM v_thread_current v
               JOIN ticket_proposals p ON p.thread_id = v.thread_id
               WHERE v.client_key IS NOT NULL AND v.client_key <> ''
                 AND p.client_cr IS NOT NULL"""):
        if r["cr"] in known:
            by_spelling[r["k"]].add(r["cr"])

    written, conflict = [], []
    for key, crs in sorted(by_spelling.items()):
        if len(crs) > 1:
            # 'dukes' is both Duke's Omaha and Duke's Root Control. Two real
            # customers, so this one waits for a person.
            conflict.append({"raw_key": key, "clients": sorted(crs)})
            continue
        if key not in have:
            _alias(con, key, next(iter(crs)), 1.0, "cr")
            written.append(key)

    # Tier 2: exact name match, for spellings no ticket vouches for. A name
    # two clients share is no better than no name at all, so it is dropped
    # rather than resolved to whichever was read last.
    lookup: dict[str, str | None] = {}
    for r in con.execute("SELECT client_id, name_key, short_name FROM clients"):
        for cand in (r["name_key"], ex.normalise_client(r["short_name"] or "")):
            if not cand:
                continue
            if cand in lookup and lookup[cand] != r["client_id"]:
                lookup[cand] = None
            else:
                lookup.setdefault(cand, r["client_id"])

    unresolved = []
    for r in con.execute("SELECT DISTINCT client_key AS k FROM v_thread_current "
                         "WHERE client_key IS NOT NULL AND client_key <> ''"):
        key = r["k"]
        if key in have or key in by_spelling:
            continue
        hit = lookup.get(key)
        if hit:
            _alias(con, key, hit, 1.0, actor)
            written.append(key)
        else:
            unresolved.append(key)

    con.commit()
    return {"written": written, "conflict": conflict, "unresolved": unresolved}


def _alias(con, raw_key: str, client_id: str, confidence: float, by: str) -> None:
    """Record a resolution. Never downgrades one already there."""
    con.execute(
        """INSERT INTO client_aliases (raw_key, client_id, confidence,
                                       resolved_by, resolved_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(raw_key) DO NOTHING""",
        (raw_key, client_id, confidence, by, now()))


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def configured() -> dict | None:
    """The four settings, or None if this machine isn't wired to Jira.

    The token also answers to ATLASSIAN_API_TOKEN, which is the name
    jira_client.py already reads. One token, set once, whichever of the two
    you reach for -- a second copy of a secret is a second thing to rotate and
    a second thing to get wrong.
    """
    got = {k: os.environ.get(k) for k in
           ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_CLIENT_JQL")}
    got["JIRA_TOKEN"] = got["JIRA_TOKEN"] or os.environ.get("ATLASSIAN_API_TOKEN")
    return got if all(got.values()) else None


def due(con, every_s: int = SYNC_EVERY_S) -> bool:
    """Whether the roster is old enough to ask Jira again."""
    r = con.execute("SELECT MAX(synced_at) AS at FROM clients").fetchone()
    if not r or not r["at"]:
        return True
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(r["at"])).total_seconds()
    except ValueError:
        return True
    return age >= every_s


def run_once(con, cfg: dict) -> dict:
    j = Jira(cfg["JIRA_BASE_URL"], cfg["JIRA_EMAIL"], cfg["JIRA_TOKEN"])
    rows = client_rows(j.search(cfg["JIRA_CLIENT_JQL"]))
    stats = sync_clients(con, rows)
    stats.update(reconcile_aliases(con))
    return stats


def collisions_from(rows: list[dict]) -> list[dict]:
    """collisions(), against rows not yet written."""
    seen = collections.defaultdict(list)
    for r in rows:
        if r["offered"]:
            seen[r["short_name"].lower()].append(r)
    return [{"short_name": k, "clients": v}
            for k, v in sorted(seen.items()) if len(v) > 1]


def check(con, cfg: dict) -> int:
    """Preflight, in the spirit of `ernie_state.py --check`."""
    print(f"base url : {cfg['JIRA_BASE_URL']}")
    j = Jira(cfg["JIRA_BASE_URL"], cfg["JIRA_EMAIL"], cfg["JIRA_TOKEN"])
    me = j.myself()
    if not me.get("accountId"):
        print("  cannot identify this account -- check JIRA_EMAIL/JIRA_TOKEN")
        return 1
    print(f"signed in: {me.get('displayName')} <{me.get('emailAddress')}>")

    rows = client_rows(j.search(cfg["JIRA_CLIENT_JQL"]))
    print(f"jql      : {cfg['JIRA_CLIENT_JQL']}")
    print(f"returned : {len(rows)} clients "
          f"({sum(r['offered'] for r in rows)} offered, "
          f"{sum(1 for r in rows if not r['offered'])} inactive/pending/paused)")

    # The acceptance test: every client the board already uses has to be in
    # the list, or the query is scoped too narrowly and those cards fall
    # through. The first page of this list alone would have missed 39 of 43.
    have = {r["client_id"] for r in rows}
    used = {r["client_cr"] for r in con.execute(
        "SELECT DISTINCT client_cr FROM ticket_proposals "
        "WHERE client_cr IS NOT NULL")}
    missing = sorted(used - have)
    print(f"coverage : {len(used) - len(missing)}/{len(used)} "
          f"of the client CRs on this board")

    # A key the query missed is only a finding if it is a client. The sandbox
    # is seeded with threads whose Client CR points at a real issue of the
    # wrong kind, so most of these can never be in a client roster however the
    # query is written. The ones that are Customer Requirements are the
    # finding: real clients this query is too narrow to reach.
    unreachable, not_clients = [], []
    for key in missing:
        (unreachable if j.is_client(key) else not_clients).append(key)

    if not_clients:
        print(f"  not clients at all ({len(not_clients)}), so no query could "
              f"reach them -- seeded threads carry CR keys that are Build "
              f"Requests, Tasks and Bugs:")
        print(f"    {', '.join(not_clients)}")
    if unreachable:
        print(f"  MISSING and real ({len(unreachable)}): "
              f"{', '.join(unreachable)}")
        print('  widen JIRA_CLIENT_JQL -- try '
              'project = PIP AND issuetype = "Customer Requirement"')
        return 1

    for c in collisions_from(rows):
        print(f"  collision: {c['short_name']!r} <- "
              + ", ".join(f"{x['client_id']} ({x['name']})" for x in c["clients"]))
    print("ok")
    return 0


def report(con) -> None:
    print("\nroster")
    for r in con.execute("SELECT client_id, short_name, name, offered "
                         "FROM clients ORDER BY offered DESC, short_name"):
        mark = "  " if r["offered"] else "--"
        extra = "" if r["short_name"] == r["name"] else f"   <- {r['name']}"
        print(f"  {mark} {r['client_id']:10} {r['short_name']}{extra}")

    print("\naliases")
    for r in con.execute(
            """SELECT a.raw_key, a.client_id, a.resolved_by, c.short_name
               FROM client_aliases a LEFT JOIN clients c
                 ON c.client_id = a.client_id ORDER BY a.raw_key"""):
        print(f"  {r['raw_key']:38} -> {r['client_id']:10} "
              f"{r['short_name'] or '?'}  [{r['resolved_by']}]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version",
                    version=ernie_version.describe())
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--env", default="ernie.env")
    ap.add_argument("--once", action="store_true", help="one pull, then stop")
    ap.add_argument("--check", action="store_true", help="preflight only")
    ap.add_argument("--report", action="store_true",
                    help="print the roster and the aliases afterwards")
    ap.add_argument("--short", metavar="PIP-1234=Name",
                    help="set one short name by hand; a later sync keeps it")
    a = ap.parse_args()

    load_env(a.env)
    con = load.connect(a.db)

    if a.short:
        key, _, name = a.short.partition("=")
        if not name.strip():
            sys.exit("--short wants PIP-1234=Name")
        con.execute("UPDATE clients SET short_name=? WHERE client_id=?",
                    (name.strip(), key.strip()))
        con.commit()
        print(f"{key.strip()} is now {name.strip()!r}")
        return

    cfg = configured()
    if not cfg:
        # Inert rather than fatal: a machine with no Jira credentials runs the
        # rest of the stack exactly as it did before.
        print("Jira is not configured (JIRA_BASE_URL, JIRA_EMAIL, "
              "JIRA_TOKEN or ATLASSIAN_API_TOKEN, JIRA_CLIENT_JQL) "
              "-- nothing to do")
        return

    if a.check:
        sys.exit(check(con, cfg))

    s = run_once(con, cfg)
    print(f"clients: {s['seen']} seen, {s['offered']} offered")
    print(f"aliases: {len(s['written'])} written, "
          f"{len(s['conflict'])} ambiguous, {len(s['unresolved'])} unresolved")
    for c in s["collisions"]:
        print(f"  collision: {c['short_name']!r} <- "
              + ", ".join(x["client_id"] for x in c["clients"]))
    for c in s["conflict"]:
        print(f"  ambiguous: {c['raw_key']!r} could be "
              + " or ".join(c["clients"]))
    if a.report:
        report(con)


if __name__ == "__main__":
    main()

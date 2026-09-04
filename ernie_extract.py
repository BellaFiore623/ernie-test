"""
Ernie extraction layer.

Turns raw Discord thread JSON into structured records. Pure functions over
dicts -- no network, no database, no Discord library. Every ambiguity is
reported rather than guessed at, so Bert can surface it for a human.

    from ernie_extract import extract_thread
    record = extract_thread(dump_entry)

Run directly to get a report over a dump:

    python ernie_extract.py dump/threads.json
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from datetime import date
from typing import Any, Iterator, Optional

# --------------------------------------------------------------------------
# Title parsing
# --------------------------------------------------------------------------

# Every queue a title may legitimately start with, including retired ones.
# This drives _PREFIX and therefore all three parse tiers, so a queue may
# never be dropped from here just because it is no longer used: the titles
# already in the mirror would stop matching, fall to UNREADABLE_CONFIDENCE,
# and be ranked to the *top* of unassigned as cards nobody can read.
QUEUES = ("OPS", "PROD", "ENG", "CS", "DATA")

# What Bert offers in the editor. Retired queues are parsed, never chosen --
# the same shape as the retired card columns: the read path keeps working and
# the write path stops offering it. DATA was retired 2026-09; one thread in
# the sandbox and one in production still carry it.
QUEUES_OFFERED = ("OPS", "PROD", "ENG", "CS")

RETIRED_QUEUES = tuple(q for q in QUEUES if q not in QUEUES_OFFERED)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# 20Aug26 / 04aug26 / 5June26 / 1Apr26
_DMY = re.compile(r"^(\d{1,2})\s*([A-Za-z]{3,9})\.?\s*(\d{2,4})$")
# 2026-7-13
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")

_DATE_TOKEN = r"(?:\d{1,2}\s*[A-Za-z]{3,9}\.?\s*\d{2,4}|\d{4}-\d{1,2}-\d{1,2})"
_PREFIX = rf"(?P<queue>{'|'.join(QUEUES)})\s*[-:]\s*"

# Tier 1: exactly the documented shape, hyphen separated.
STRICT = re.compile(
    rf"^{_PREFIX}(?P<client>.+?)\s*-\s*(?P<date>{_DATE_TOKEN})\s*-\s*(?P<summary>.+)$",
    re.IGNORECASE,
)

# Tier 2: date present somewhere, separators loose or absent.
LOOSE = re.compile(
    rf"^{_PREFIX}(?P<client>.+?)\s*[-:]?\s*(?P<date>{_DATE_TOKEN})\s*[-:]?\s*(?P<summary>.*)$",
    re.IGNORECASE,
)

# Tier 3: recognised queue, no parseable date at all.
PREFIX_ONLY = re.compile(rf"^{_PREFIX}(?P<rest>.+)$", re.IGNORECASE)


def parse_date(token: str) -> Optional[date]:
    """Parse the date forms actually observed in thread titles."""
    token = token.strip()

    m = _ISO.match(token)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    m = _DMY.match(token)
    if not m:
        return None
    day, name, year = m.group(1), m.group(2).lower()[:3], m.group(3)
    month = MONTHS.get(name)
    if month is None:
        return None
    y = int(year)
    if y < 100:
        y += 2000
    try:
        return date(y, month, int(day))
    except ValueError:
        return None


# The confidences that mean the title did not parse. Every title_* issue on a
# card reduces to this: extract_thread turns "none" and "loose" into
# title_unparseable and title_nonstandard, and the API derives title_{conf} for
# all three. Bert's BLOCKING set is the same statement in issue form, and both
# it and ernie_load read this rather than restating the list.
UNREADABLE_CONFIDENCE = ("none", "loose", "prefix_only")


@dataclasses.dataclass
class Title:
    queue: Optional[str] = None
    client_raw: Optional[str] = None
    date: Optional[date] = None
    summary: Optional[str] = None
    confidence: str = "none"        # strict | loose | prefix_only | none
    raw: str = ""


def parse_title(name: str) -> Title:
    name = (name or "").strip()

    for rx, conf in ((STRICT, "strict"), (LOOSE, "loose")):
        m = rx.match(name)
        if m:
            d = parse_date(m.group("date"))
            if d:
                return Title(
                    queue=m.group("queue").upper(),
                    client_raw=m.group("client").strip(" -:"),
                    date=d,
                    summary=(m.group("summary") or "").strip(" -:") or None,
                    confidence=conf,
                    raw=name,
                )

    m = PREFIX_ONLY.match(name)
    if m:
        return Title(
            queue=m.group("queue").upper(),
            summary=m.group("rest").strip(),
            confidence="prefix_only",
            raw=name,
        )

    return Title(raw=name)


# --------------------------------------------------------------------------
# Client name normalisation
# --------------------------------------------------------------------------

_MD = re.compile(r"\*{1,3}([^*]+)\*{1,3}")
_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|co|corp|company|borough|pa|incorporated)\b\.?", re.IGNORECASE
)
_ANNOT = re.compile(r"\b(purchase|loaner|rental|demo)\b", re.IGNORECASE)


def normalise_client(value: str) -> str:
    """
    Reduce a raw client string to a comparison key.

        'Clinton, MS: **PURCHASE**'                  -> 'clinton ms'
        'SCI Infrastructure LLC. **PURCHASE** (...)' -> 'sci infrastructure'
        'Munhall Borough PA (MSSMA) : Power Bank'    -> 'munhall mssma power bank'
    """
    if not value:
        return ""
    s = _MD.sub(r"\1", value)          # unwrap **bold**
    s = _ANNOT.sub(" ", s)
    s = _SUFFIX.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)     # drop punctuation
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


# --------------------------------------------------------------------------
# Equipment identifiers
# --------------------------------------------------------------------------

EQUIP = re.compile(r"\b(?P<type>EReel|ODE|SSD|LED|OLK)[\s-]?(?P<num>\d+|#{2,})\b", re.I)

EQUIP_TYPES = {"robot", "ereel", "ode", "olk", "lidar", "laser"}


@dataclasses.dataclass
class Equipment:
    type: str
    number: Optional[str]
    state: str          # resolved | pending | malformed
    raw: str

    @property
    def key(self) -> Optional[str]:
        return f"{self.type}-{self.number}" if self.state == "resolved" else None


def find_equipment(text: str) -> list[Equipment]:
    """Pull equipment IDs out of free text. '####' is pending, not an error."""
    out, seen = [], set()
    for m in EQUIP.finditer(text or ""):
        raw = m.group(0)
        if raw.lower() in seen:
            continue
        seen.add(raw.lower())
        num = m.group("num")
        out.append(
            Equipment(
                type=m.group("type").upper().replace("EREEL", "EReel"),
                number=None if "#" in num else num,
                state="pending" if "#" in num else "resolved",
                raw=raw,
            )
        )
    return out


# --------------------------------------------------------------------------
# Embeds
# --------------------------------------------------------------------------

NOT_FOUND = "-- not found --"

# 'PIP-7434 (ODE-2926)' / 'PIP-4945 (SCI Infrastructure LLC. **PURCHASE** (...))'
_PIP_PAREN = re.compile(r"^\s*(?P<key>PIP-\d+)\s*\((?P<label>.*)\)\s*$", re.DOTALL)
PIP_KEY = re.compile(r"\bPIP-\d+\b")


def field(embed: dict, name: str, prefix: bool = False) -> Optional[str]:
    """Look up an embed field by exact name, or by prefix for counted fields."""
    for f in embed.get("fields") or []:
        n = f.get("name", "")
        if (n.startswith(name) if prefix else n == name):
            return f.get("value")
    return None


def split_pip(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    'PIP-7434 (ODE-2926)' -> ('PIP-7434', 'ODE-2926')

    Takes the last closing paren so nested parens in client names survive.
    Returns (None, None) for the '-- not found --' sentinel.
    """
    if not value or value.strip() == NOT_FOUND:
        return None, None
    m = _PIP_PAREN.match(value.strip())
    if m:
        return m.group("key"), m.group("label").strip()
    m2 = PIP_KEY.search(value)
    return (m2.group(0) if m2 else None), None


@dataclasses.dataclass
class Ticket:
    kind: str                       # build | return
    message_id: str
    timestamp: str
    proposed_by: Optional[str]
    equipment_master: Optional[str]
    equipment_label: Optional[str]
    client_cr: Optional[str]
    client_label: Optional[str]
    client_key: Optional[str]
    equipment_type: Optional[str]
    template: Optional[str]
    reported_problem: Optional[str]
    reporter: Optional[str]
    assignee: Optional[str]
    labels: Optional[str]
    referenced: list[str]
    has_buttons: bool
    issues: list[str]


def parse_ticket_embed(msg: dict, embed: dict) -> Optional[Ticket]:
    title = embed.get("title") or ""
    if title.startswith("Build Request"):
        kind = "build"
    elif title.startswith("Return Ticket") or title.startswith("Edit Return Ticket"):
        kind = "return"
    else:
        return None                      # link preview, meme, unrelated embed

    em_raw = field(embed, "Equipment master")
    cr_raw = field(embed, "Client CR")
    em_key, em_label = split_pip(em_raw)
    cr_key, cr_label = split_pip(cr_raw)

    issues = []
    if em_raw and em_raw.strip() == NOT_FOUND:
        issues.append("equipment_master_not_found")
    if cr_raw and cr_raw.strip() == NOT_FOUND:
        issues.append("client_cr_not_found")
    if not em_raw:
        issues.append("equipment_master_missing")
    if not cr_raw:
        issues.append("client_cr_missing")

    eq_type = (field(embed, "Equipment type") or "").strip().lower() or None
    if eq_type and eq_type not in EQUIP_TYPES:
        issues.append(f"unknown_equipment_type:{eq_type}")

    referenced = []
    for f in embed.get("fields") or []:
        if f.get("name", "").startswith("Existing "):
            referenced += PIP_KEY.findall(f.get("value") or "")

    return Ticket(
        kind=kind,
        message_id=msg["id"],
        timestamp=msg.get("timestamp", ""),
        proposed_by=(msg.get("author") or {}).get("username"),
        equipment_master=em_key,
        equipment_label=em_label,
        client_cr=cr_key,
        client_label=cr_label,
        client_key=normalise_client(cr_label or ""),
        equipment_type=eq_type,
        template=field(embed, "Template"),
        reported_problem=field(embed, "Reported problem"),
        reporter=field(embed, "Reporter"),
        assignee=field(embed, "Assignee"),
        labels=field(embed, "Labels") or field(embed, "Labels", prefix=True),
        referenced=sorted(set(referenced)),
        has_buttons=bool(msg.get("components")),
        issues=issues,
    )


# --------------------------------------------------------------------------
# Creation confirmations -- the authoritative "ticket exists" signal
# --------------------------------------------------------------------------

CONFIRM = re.compile(
    r"Created\s+\*\*(?P<key>PIP-\d+)\*\*", re.IGNORECASE
)
_ASSIGNED = re.compile(r"Assigned to\s+(?P<who>.+)")
_LINK_EM = re.compile(r"Linked to equipment master\s+(?P<key>PIP-\d+)", re.I)
_LINK_CR = re.compile(r"Linked to client CR\s+(?P<key>PIP-\d+)", re.I)


@dataclasses.dataclass
class Created:
    key: str
    message_id: str
    timestamp: str
    assignee: Optional[str]
    equipment_master: Optional[str]
    client_cr: Optional[str]
    kind: Optional[str]


def parse_confirmation(msg: dict) -> Optional[Created]:
    text = msg.get("content") or ""
    m = CONFIRM.search(text)
    if not m:
        return None
    a = _ASSIGNED.search(text)
    em = _LINK_EM.search(text)
    cr = _LINK_CR.search(text)
    low = text.lower()
    kind = "build" if "build request" in low else "return" if "return" in low else None
    return Created(
        key=m.group("key"),
        message_id=msg["id"],
        timestamp=msg.get("timestamp", ""),
        assignee=a.group("who").strip() if a else None,
        equipment_master=em.group("key") if em else None,
        client_cr=cr.group("key") if cr else None,
        kind=kind,
    )


# --------------------------------------------------------------------------
# Thread-level assembly
# --------------------------------------------------------------------------

@dataclasses.dataclass
class ThreadRecord:
    thread_id: str
    parent_id: str
    name: str
    owner_id: Optional[str]
    title: Title
    client_key: Optional[str]
    equipment: list[Equipment]
    proposals: list[Ticket]
    created: list[Created]
    participants: list[str]
    message_count: int
    first_ts: Optional[str]
    last_ts: Optional[str]
    archived: bool
    issues: list[str]

    @property
    def needs_attention(self) -> bool:
        return bool(self.issues) or self.title.confidence in UNREADABLE_CONFIDENCE

    @property
    def title_unreadable(self) -> bool:
        """Nobody can read this title, so a human has to look at the thread.

        Narrower than needs_attention, which any issue trips -- a pending
        equipment number is a card worth a second glance, not one whose
        client is unknown.
        """
        return self.title.confidence in UNREADABLE_CONFIDENCE


def extract_thread(entry: dict) -> ThreadRecord:
    t = entry["thread"]
    msgs = entry.get("messages") or []
    meta = t.get("thread_metadata") or {}

    title = parse_title(t.get("name", ""))

    proposals, created = [], []
    for m in msgs:
        for e in m.get("embeds") or []:
            tk = parse_ticket_embed(m, e)
            if tk:
                proposals.append(tk)
        c = parse_confirmation(m)
        if c:
            created.append(c)

    # Equipment: the title first, then any ticket labels.
    equipment = find_equipment(title.summary or t.get("name", ""))
    seen = {e.raw.lower() for e in equipment}
    for p in proposals:
        for e in find_equipment(p.equipment_label or ""):
            if e.raw.lower() not in seen:
                seen.add(e.raw.lower())
                equipment.append(e)

    # Client key: prefer the ticket's Client CR label, fall back to the title.
    client_key = None
    for p in proposals:
        if p.client_key:
            client_key = p.client_key
            break
    if not client_key and title.client_raw:
        client_key = normalise_client(title.client_raw)

    issues = []
    if title.confidence == "none":
        issues.append("title_unparseable")
    elif title.confidence == "loose":
        issues.append("title_nonstandard")
    if any(e.state == "pending" for e in equipment):
        issues.append("equipment_number_pending")
    for p in proposals:
        issues += [f"{p.kind}:{i}" for i in p.issues]
    # A proposal with buttons and no matching confirmation is still a draft.
    confirmed = {c.key for c in created}
    if proposals and not confirmed:
        issues.append("proposal_never_confirmed")

    ts = [m.get("timestamp") for m in msgs if m.get("timestamp")]

    return ThreadRecord(
        thread_id=t["id"],
        parent_id=t.get("parent_id", ""),
        name=t.get("name", ""),
        owner_id=t.get("owner_id"),
        title=title,
        client_key=client_key,
        equipment=equipment,
        proposals=proposals,
        created=created,
        participants=sorted({(m.get("author") or {}).get("username", "") for m in msgs} - {""}),
        message_count=len(msgs),
        first_ts=min(ts) if ts else None,
        last_ts=max(ts) if ts else None,
        archived=bool(meta.get("archived")),
        issues=sorted(set(issues)),
    )


def extract_all(dump: list[dict], parent_id: Optional[str] = None) -> Iterator[ThreadRecord]:
    for entry in dump:
        if parent_id and entry["thread"].get("parent_id") != parent_id:
            continue
        yield extract_thread(entry)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _report(path: str, parent_id: Optional[str]) -> None:
    import collections

    dump = json.load(open(path))
    records = list(extract_all(dump, parent_id))
    n = len(records)
    print(f"{n} threads\n")

    conf = collections.Counter(r.title.confidence for r in records)
    print("title confidence")
    for k in ("strict", "loose", "prefix_only", "none"):
        if conf[k]:
            print(f"  {conf[k]:>4}  {k:<12} {conf[k]/n:>5.1%}")

    print(f"\ntickets: {sum(len(r.proposals) for r in records)} proposed, "
          f"{sum(len(r.created) for r in records)} confirmed")

    issues = collections.Counter(i for r in records for i in r.issues)
    if issues:
        print("\nissues")
        for k, v in issues.most_common():
            print(f"  {v:>4}  {k}")

    flagged = [r for r in records if r.needs_attention]
    print(f"\n{len(flagged)} of {n} threads would show a flag in Bert "
          f"({len(flagged)/n:.1%})\n")

    print("sample of flagged titles")
    for r in flagged[:12]:
        print(f"  [{r.title.confidence:<11}] {r.name[:66]}")
        if r.issues:
            print(f"                {', '.join(r.issues[:4])}")

    print("\ntop client keys")
    for k, v in collections.Counter(
        r.client_key for r in records if r.client_key
    ).most_common(12):
        print(f"  {v:>4}  {k}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python ernie_extract.py dump/threads.json [parent_channel_id]")
    _report(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)

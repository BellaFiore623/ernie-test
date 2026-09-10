"""
The customer list from Jira, and what it is allowed to change.

Client names were typed into thread titles by hand, and the board grew 120
spellings of 43 customers. ernie_jira pulls the real list and points the old
spellings at it. These checks defend the three ways that can go wrong: cutting
a name too hard, merging two customers who merely look alike, and clearing a
card's red edge as a side effect of naming its client.

Every string in here is a real one off the production board.
"""

from __future__ import annotations

import ast
import pathlib
import sys

from support import Board, Check, iso

import bert
import ernie_extract as ex
import ernie_api as api
import ernie_jira as J


ROOT = pathlib.Path(__file__).resolve().parent.parent


# -- building the bits the Board fixture doesn't ---------------------------

def spelling(b: Board, tid: str, client_key: str) -> None:
    """Give a card's title a parsed client, as the sync would."""
    b.con.execute("UPDATE thread_titles SET client_key=?, client_raw=? "
                  "WHERE thread_id=?", (client_key, client_key, tid))
    b.con.commit()


def ticket(b: Board, tid: str, client_cr: str) -> None:
    """Hang a ticket panel naming a Client CR off a card.

    The panel needs the message it came in on -- ticket_proposals keys on it.
    """
    mid = f"msg-{tid}-{client_cr}"
    b.con.execute(
        """INSERT INTO messages (message_id, thread_id, author_id, author_name,
                                 is_bot, created_at, first_seen_at)
           VALUES (?,?,?,?,?,?,?)""",
        (mid, tid, "bot-1", "Python-Interface-Bot", 1, iso(-600), iso(-600)))
    b.con.execute(
        """INSERT INTO ticket_proposals (message_id, thread_id, kind,
                                         proposed_at, client_cr)
           VALUES (?,?,?,?,?)""",
        (mid, tid, "build", iso(-600), client_cr))
    b.con.commit()


def roster(b: Board, *summaries: tuple[str, str]) -> None:
    """Put clients in, the way a Jira pull would."""
    J.sync_clients(b.con, J.client_rows(
        [{"key": k, "fields": {"summary": s}} for k, s in summaries]))


# -- reading a Jira summary ------------------------------------------------

def check_the_short_name_cuts_the_note_not_the_name():
    """A summary carries the account note as well as the customer.

    'IPI : El Paso' is the customer IPI. But 'Edge AI Demo Team' is a team
    whose name contains the word Demo, and ex.normalise_client's annotation
    list -- purchase|loaner|rental|demo, stripped wherever they appear --
    turns it into 'Edge AI Team'. That is why this does not reuse it.
    """
    c = Check("the short name cuts the note, not the name")
    for summary, want in [
            ("IPI : El Paso", "IPI"),
            ("San Joaquin : (Replace Cable 1K -have OLD KST rev)", "San Joaquin"),
            ("RJN (and City of Baltimore) (ST Client)", "RJN"),
            ("GFT - *PURCHASE* (ST Client)", "GFT"),
            ("Abay Construction *Working under Trekk*", "Abay Construction"),
            ("Thrasher : (ST Client)", "Thrasher"),
            ("Clinton, MS: **PURCHASE**", "Clinton, MS"),
            ("MBE (Monaloh Basin Engineers) - (Doing Service Work for Edge)", "MBE"),
            # Nested parens. One pass leaves a stray bracket behind, so the
            # strip loops until it stops changing anything.
            ("Reutzel Excavating **PURCHASE** (2 Bots) : "
             "(NEED Laser and GSN (ST Client))", "Reutzel Excavating"),
            # The one that says why the annotation list is not reused.
            ("Edge AI Demo Team", "Edge AI Demo Team"),
            # Nothing to cut.
            ("Duke's Root Control", "Duke's Root Control"),
            ("RK&K", "RK&K"),
    ]:
        c.equal(J.short_name(summary), want, summary[:44])
    return c.report()


def check_only_the_starred_marker_retires_a_client():
    """*PAUSED* takes a client off the list. The word paused does not.

    'City of Superior WI : LENDING CALIB. BAR - Unpaused' is a live customer
    that contains the letters. And 'Wilson Excavating: ACTIVE FOR 3RD PARTY
    CODING *INACTIVE*' carries its marker after the colon, so the test has to
    run on the summary as it came -- cutting first would keep it on the list.
    """
    c = Check("only the starred marker retires a client")
    for summary, offered in [
            ("City of Superior WI : LENDING CALIB. BAR - Unpaused", True),
            ("Abay Construction *Working under Trekk*", True),
            ("Northern Moraine", True),
            ("Long Beach : *Pending*", False),
            ("Drain Jetters R Us LLC: *PAUSED*", False),
            ("HydroEdge - *INACTIVE*", False),
            ("Wilson Excavating: ACTIVE FOR 3RD PARTY CODING *INACTIVE*", False),
    ]:
        c.equal(J.is_offered(summary), offered, summary[:50])
    return c.report()


# -- writing the roster ----------------------------------------------------

def check_a_resync_keeps_a_hand_written_short_name():
    """Derivation is a seed, not a ruling.

    'SCI Infrastructure LLC. **PURCHASE** (Should Have 3 Bots!)' derives to
    'SCI Infrastructure LLC.' and the board calls it SCI. Somebody fixes that
    once, and every later pull has to leave it alone.
    """
    c = Check("a re-sync keeps a hand-written short name")
    with Board() as b:
        summary = "SCI Infrastructure LLC. **PURCHASE** (Should Have 3 Bots!)"
        roster(b, ("PIP-4945", summary))
        c.equal(b.con.execute("SELECT short_name FROM clients").fetchone()[0],
                "SCI Infrastructure LLC.", "derived on the first pull")

        b.con.execute("UPDATE clients SET short_name='SCI'")
        b.con.commit()
        roster(b, ("PIP-4945", summary))
        row = b.con.execute("SELECT short_name, name FROM clients").fetchone()
        c.equal(row["short_name"], "SCI", "the hand-written one survives")
        c.equal(row["name"], summary, "the summary is still refreshed")
    return c.report()


def check_two_customers_never_share_a_line():
    """'IPI : El Paso' and 'IPI : *REP*' are both live, and both read as IPI.

    Two identical rows in a list you pick from is worse than the typos this
    replaces, so a collision is reported rather than written and forgotten.
    """
    c = Check("two customers never share a line")
    with Board() as b:
        roster(b, ("PIP-2136", "IPI : El Paso"), ("PIP-3927", "IPI : *REP*"),
               ("PIP-7450", "ATAC"))
        hits = J.collisions(b.con)
        c.equal(len(hits), 1, "one collision found")
        c.equal(sorted(x["client_id"] for x in hits[0]["clients"]),
                ["PIP-2136", "PIP-3927"], "and it names both of them")
    return c.report()


# -- reconciling what is already on the board ------------------------------

def check_one_ticket_key_collapses_every_spelling():
    """The whole point. Two titles, two spellings, one customer.

    The threads both carry PIP-8605 on a ticket, so neither spelling has to be
    compared against anything -- which is how 'duke s root control' and 'dukes
    root control' land on the same client without a fuzzy matcher.
    """
    c = Check("one ticket key collapses every spelling")
    with Board() as b:
        roster(b, ("PIP-8605", "Duke's Root Control"))
        for key in ("duke s root control", "dukes root control"):
            tid = b.card(f"PROD: {key} - 03Aug26 - EReel-1060 fault")
            spelling(b, tid, key)
            ticket(b, tid, "PIP-8605")

        J.reconcile_aliases(b.con)
        got = dict(b.con.execute(
            "SELECT raw_key, client_id FROM client_aliases").fetchall())
        c.equal(got, {"duke s root control": "PIP-8605",
                      "dukes root control": "PIP-8605"},
                "both spellings point at one client")
        c.equal({r["resolved_by"] for r in b.con.execute(
                    "SELECT resolved_by FROM client_aliases")}, {"cr"},
                "and say the ticket key is why")
    return c.report()


def check_names_that_merely_look_alike_are_left_alone():
    """'falmouth ma' and 'falmouth me' are 0.91 similar and different places.

    So are Fulton County North and South. A matcher confident enough to merge
    the Duke's spellings is confident enough to merge these, and losing one
    customer's tickets into another is not a trade worth making.
    """
    c = Check("names that merely look alike are left alone")
    with Board() as b:
        roster(b, ("PIP-9440", "Falmouth Maine"),
               ("PIP-6873", "Fulton County South"))
        for key in ("falmouth ma", "falmouth me", "fulton county north ga"):
            spelling(b, b.card(f"PROD: {key} - 03Aug26 - job"), key)

        out = J.reconcile_aliases(b.con)
        c.equal(b.con.execute("SELECT COUNT(*) FROM client_aliases").fetchone()[0],
                0, "nothing was resolved by looking alike")
        c.equal(sorted(out["unresolved"]),
                ["falmouth ma", "falmouth me", "fulton county north ga"],
                "all three are reported for a person instead")
    return c.report()


def check_a_spelling_two_customers_answer_to_waits_for_a_person():
    """'dukes' is Duke's Omaha and Duke's Root Control. Two real customers."""
    c = Check("a spelling two customers answer to waits for a person")
    with Board() as b:
        roster(b, ("PIP-8425", "Duke's Omaha : (MudMaster Hybrid)"),
               ("PIP-8605", "Duke's Root Control"))
        for cr in ("PIP-8425", "PIP-8605"):
            tid = b.card(f"PROD: Dukes - 03Aug26 - {cr}")
            spelling(b, tid, "dukes")
            ticket(b, tid, cr)

        out = J.reconcile_aliases(b.con)
        c.equal(b.con.execute("SELECT COUNT(*) FROM client_aliases").fetchone()[0],
                0, "no alias was guessed")
        c.equal([x["raw_key"] for x in out["conflict"]], ["dukes"],
                "the conflict is named")
    return c.report()


def check_reconciling_twice_changes_nothing():
    """It runs on every pull, so it has to be safe to run on every pull."""
    c = Check("reconciling twice changes nothing")
    with Board() as b:
        roster(b, ("PIP-7468", "Clinton, MS: **PURCHASE**"))
        tid = b.card("PROD: Clinton - 03Aug26 - job")
        spelling(b, tid, "clinton ms")
        ticket(b, tid, "PIP-7468")

        first = J.reconcile_aliases(b.con)
        b.con.execute("UPDATE client_aliases SET resolved_by='Tyler'")
        b.con.commit()
        second = J.reconcile_aliases(b.con)
        c.equal(len(first["written"]), 1, "the first pass resolves it")
        c.equal(second["written"], [], "the second writes nothing")
        c.equal(b.con.execute(
            "SELECT resolved_by FROM client_aliases").fetchone()[0], "Tyler",
            "and a person's resolution is not overwritten")
    return c.report()


def check_a_retired_client_still_names_its_cards():
    """Not offered is not deleted.

    'Prime Contractor Supply Corp *INACTIVE*' has four cards on the production
    board and 'HydroEdge - *INACTIVE*' has one. Taking them off the dropdown
    must not take the name off those five cards -- the same rule that keeps a
    retired queue on the one ticket carrying it.
    """
    c = Check("a retired client still names its cards")
    with Board() as b:
        roster(b, ("PIP-7079", "Prime Contractor Supply Corp *INACTIVE*"))
        tid = b.card("PROD: Prime - 03Aug26 - job")
        spelling(b, tid, "prime contractor supply")
        ticket(b, tid, "PIP-7079")
        J.reconcile_aliases(b.con)

        row = b.con.execute("SELECT offered, short_name FROM clients").fetchone()
        c.equal(row["offered"], 0, "it is not offered")
        c.equal(row["short_name"], "Prime Contractor Supply Corp",
                "but it still has a name")
        c.equal(b.con.execute(
            "SELECT client_id FROM client_aliases").fetchone()[0], "PIP-7079",
            "and the card still resolves to it")
    return c.report()


# -- what the editor is allowed to write -----------------------------------

class _Box:
    def __init__(self, value):
        self._v = value

    def text(self):
        return self._v


class _Editor:
    """Card's two text fields, without a QApplication to build one under."""

    def __init__(self, title, client):
        self.f_title = _Box(title)
        self.f_client = _Box(client)


def check_picking_a_client_does_not_vouch_for_the_card():
    """needs_triage() reads client_override as somebody vouching for a card.

    So writing one as a side effect of picking a name would clear the red edge
    off every unreadable ticket anyone merely opened the editor on. When the
    title already says what the box says -- which it does the moment a client
    is picked, because picking rewrites the title -- there is nothing to
    override and nothing is sent.
    """
    c = Check("picking a client does not vouch for the card")
    override = bert.Card._override

    e = _Editor("PROD: Thrasher - 03Aug26 - EReel-1060 fault", "Thrasher")
    c.equal(override(e), "", "the title agrees, so no override is sent")

    # The card is red because its title will not parse. Opening the editor,
    # seeing the client, and saving must leave it red.
    card = {"priority": "high", "queue": "PROD", "issues": ["title_none"],
            "client_override": override(_Editor("nonsense title", ""))}
    c.ok(bert.needs_triage(card), "an unreadable card stays red")

    # Typing a client the title does not mention is still the acknowledgement
    # it always was, and still clears the red.
    said = override(_Editor("nonsense title", "Thrasher"))
    c.equal(said, "Thrasher", "a client the title lacks is still an override")
    c.ok(not bert.needs_triage({"priority": "high", "queue": "PROD",
                                "issues": ["title_none"],
                                "client_override": said}),
         "and that one clears the red, as it always did")
    return c.report()


def check_the_editor_offers_the_roster_and_still_takes_anything():
    """A customer exists before Jira hears about them.

    QUEUES_OFFERED is narrower than QUEUES without stopping a card carrying a
    tag nobody offers; the client list works the same way. This reads the
    widget's construction rather than building one -- a Qt widget with no
    QApplication does not raise, it takes the process down.
    """
    c = Check("the editor offers the roster and still takes anything")
    c.ok(issubclass(bert.ClientCombo, bert.Combo),
         "it is a Combo, so the wheel cannot change it")
    for name in ("text", "setText"):
        c.ok(callable(getattr(bert.ClientCombo, name, None)),
             f"it answers to {name}(), as save() and is_dirty() expect")
    return c.report()


# -- being switched off ----------------------------------------------------

def check_no_jira_means_no_change():
    """A machine with no credentials runs the stack exactly as it did."""
    c = Check("no Jira means no change")
    import os
    keep = {k: os.environ.pop(k, None) for k in
            ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_CLIENT_JQL")}
    try:
        c.equal(J.configured(), None, "configured() says so")
        with Board() as b:
            c.ok(J.due(b.con), "an empty roster is always due")
            c.equal(b.con.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
                    0, "and nothing was written")
    finally:
        for k, v in keep.items():
            if v is not None:
                os.environ[k] = v
    return c.report()


def check_a_missing_key_is_judged_by_what_it_is():
    """--check must tell a narrow query from a key that was never a client.

    The sandbox's seeded threads carry Client CR keys that are real Jira
    issues of the wrong kind -- PIP-4902 is a Build Request, PIP-4940 a Bug,
    PIP-4931 a Task. No widening of a client query would ever reach those, or
    should. Testing that the key merely *exists* called all seven a failure
    and told the reader to widen a query that was already right.
    """
    c = Check("a key the query missed is judged by what it is")

    import ast
    import pathlib
    src = pathlib.Path(J.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "check"), None)
    body = (ast.get_source_segment(src, fn) or "") if fn else ""
    c.ok("is_client" in body,
         "the coverage report asks whether the key is a client")
    c.ok("issue_exists" not in body,
         "and not merely whether the issue exists")

    meth = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == "is_client"), None)
    mbody = (ast.get_source_segment(src, meth) or "") if meth else ""
    c.ok("CLIENT_ISSUE_TYPE" in mbody,
         "which it decides on the issue type, from one named constant")
    c.equal(J.CLIENT_ISSUE_TYPE, "Customer Requirement",
            "and that constant is what Jira calls a client")
    return c.report()


def _roster():
    """The handful of real clients these searches are about."""
    def c(cid, name, short, ambiguous=False, aliases=()):
        return {"client_id": cid, "name": name, "short_name": short,
                "ambiguous": ambiguous, "aliases": list(aliases)}
    return [
        c("PIP-8605", "Duke's Root Control", "Duke's Root Control",
          aliases=["Dukes Root Control"]),
        c("PIP-8425", "Duke's Omaha : (MudMaster Hybrid)", "Duke's Omaha"),
        c("PIP-2148", "Inspect.AI", "Inspect.AI", aliases=["Inspect AI"]),
        c("PIP-6700", "Eight-Eleven Co", "Eight-Eleven Co"),
        c("PIP-4863", "Thrasher : (ST Client)", "Thrasher"),
        c("PIP-6878", "Trekk Design Group (ST Client)", "Trekk"),
        # Its summary mentions Trekk, but it is not Trekk.
        c("PIP-2149", "Abay Construction *Working under Trekk*",
          "Abay Construction"),
        c("PIP-7979", "MBE (Monaloh Basin Engineers)", "MBE"),
        c("PIP-2136", "IPI : El Paso", "IPI", ambiguous=True),
        c("PIP-3927", "IPI : *REP*", "IPI", ambiguous=True),
    ]


def check_punctuation_never_hides_a_client():
    """Every miss measured on the real board was punctuation, not letters.

    "Duke's" has an apostrophe, 'Inspect.AI' a dot, 'Eight-Eleven' a hyphen.
    Somebody typing 'dukes' is not making a mistake worth correcting -- they
    are typing the name without the apostrophe, and a substring search finds
    nothing at all. Both of these are spellings that really appear in
    production titles.
    """
    c = Check("punctuation never hides a client")

    # Asserted on the squash itself, not only through a search: the alias and
    # fuzzy tiers can rescue these for their own reasons, and did -- taking
    # the squash out left every search still passing, which is a check
    # agreeing with the code rather than testing it.
    for raw, want in [("Duke's Root Control", "dukesrootcontrol"),
                      ("Inspect.AI", "inspectai"),
                      ("Eight-Eleven Co", "eightelevenco"),
                      ("Clinton, MS", "clintonms"),
                      ("  RK&K  ", "rkk")]:
        c.equal(bert.client_squash(raw), want, f"squash {raw!r}")

    # And a client with no alias to fall back on, so only the squash can
    # answer: the apostrophe is the only thing between the two strings.
    solo = [x for x in _roster() if x["client_id"] == "PIP-8425"]
    c.equal([x["short_name"] for x in bert.client_matches("dukesomaha", solo)],
            ["Duke's Omaha"], "an unaliased name found through its apostrophe")

    r = _roster()
    for typed, want in [("dukes", "Duke's Root Control"),
                        ("inspect ai", "Inspect.AI"),
                        ("eight eleven", "Eight-Eleven Co"),
                        ("root control", "Duke's Root Control")]:
        got = [x["short_name"] for x in bert.client_matches(typed, r)]
        c.ok(want in got, f"{typed!r} offers {want!r}  (got {got[:3]})")
    return c.report()


def check_a_mistyped_name_still_finds_its_client():
    """A letter wrong is the case the dropdown exists for."""
    c = Check("a mistyped name still finds its client")
    r = _roster()
    c.equal([x["short_name"] for x in bert.client_matches("thasher", r)],
            ["Thrasher"], "'thasher' offers Thrasher")
    c.equal(bert.client_matches("zzzzzz", r), [],
            "and nonsense offers nothing rather than the nearest thing")
    return c.report()


def check_the_customer_outranks_a_note_about_them():
    """'Abay Construction *Working under Trekk*' contains the word Trekk.

    So does Trekk Design Group, which is who you meant. A single score would
    put them in whatever order the roster happened to be in; the tiers put a
    hit on the customer's own name above a hit on somebody's summary.
    """
    c = Check("the customer outranks a note about them")
    got = [x["short_name"] for x in bert.client_matches("trek", _roster())]
    c.ok(got[:1] == ["Trekk"], f"Trekk is offered first  (got {got})")
    c.ok("Abay Construction" in got, "and Abay is still findable, just after")
    return c.report()


def check_an_old_spelling_finds_the_right_customer():
    """The alias table already knows the misspellings.

    'Dukes Root Control' is on nine production threads and 'Inspect AI' on
    six. Somebody typing what a title said last year should land on the
    customer, not on nothing -- the editor has no reason to rediscover what
    reconciliation already worked out.
    """
    c = Check("an old spelling finds the right customer")
    r = _roster()
    got = [x["short_name"] for x in bert.client_matches("monaloh", r)]
    c.equal(got, ["MBE"], "a name that appears only in the Jira summary")
    got = [x["short_name"] for x in bert.client_matches("Dukes Root Control", r)]
    c.ok(got[:1] == ["Duke's Root Control"],
         f"and a spelling only the alias table knows  (got {got[:2]})")
    return c.report()


def check_the_search_suggests_and_never_decides():
    """This is the half of fuzzy matching that is safe.

    reconcile_aliases refuses to merge on resemblance because 'falmouth ma'
    and 'falmouth me' are 0.91 similar and are different places. That rule is
    about a matcher writing an alias with nobody watching. Searching is the
    other half: it may offer anything it likes, because a person chooses. So
    an ambiguous query must offer *both* rather than pick one.
    """
    c = Check("the search suggests and never decides")
    got = [x["client_id"] for x in bert.client_matches("dukes", _roster())]
    c.ok("PIP-8605" in got and "PIP-8425" in got,
         "'dukes' offers both Duke's customers, and settles nothing")
    got = [x["client_id"] for x in bert.client_matches("ipi", _roster())]
    c.equal(sorted(got), ["PIP-2136", "PIP-3927"],
            "as does a name two live customers share")
    return c.report()


def check_the_roster_follows_jira_but_not_over_a_correction():
    """Three things a roster has to survive: an edit, a removal, a bad pull.

    A rename in Jira has to reach short_name, and a correction made by hand
    has to survive one. COALESCE could tell neither apart -- short_name is
    filled on the first insert, so it was never NULL again and therefore never
    updated: renaming a client in Jira left the dropdown on the old name for
    ever. Re-deriving from the previous summary answers it. If what is stored
    is exactly what that summary would have produced, nobody has touched it.
    """
    c = Check("the roster follows Jira, but not over a correction")

    def pull(b, *pairs):
        return J.sync_clients(b.con, J.client_rows(
            [{"key": k, "fields": {"summary": s}} for k, s in pairs]))

    def short(b, cid):
        r = b.con.execute("SELECT short_name, offered FROM clients "
                          "WHERE client_id=?", (cid,)).fetchone()
        return (r["short_name"], r["offered"]) if r else None

    with Board() as b:
        pull(b, ("PIP-1", "Trekk Design Group (ST Client)"),
                ("PIP-2", "SCI Infrastructure LLC. **PURCHASE**"))
        c.equal(short(b, "PIP-1"), ("Trekk Design Group", 1), "derived at first")

        r = pull(b, ("PIP-1", "Trekk Infrastructure (ST Client)"),
                    ("PIP-2", "SCI Infrastructure LLC. **PURCHASE**"))
        c.equal(short(b, "PIP-1")[0], "Trekk Infrastructure",
                "a rename in Jira reaches the short name")
        c.equal([x[0] for x in r["renamed"]], ["PIP-1"], "and is reported")

        b.con.execute("UPDATE clients SET short_name='SCI' WHERE client_id='PIP-2'")
        b.con.commit()
        pull(b, ("PIP-1", "Trekk Infrastructure (ST Client)"),
                ("PIP-2", "SCI Infrastructure LLC. **PURCHASE** (3 Bots)"))
        c.equal(short(b, "PIP-2")[0], "SCI",
                "a hand-written one survives a later edit in Jira")

        # Gone from the query: retired, not deleted. The cards carrying it
        # keep their name, and it comes back if the query finds it again.
        r = pull(b, ("PIP-1", "Trekk Infrastructure (ST Client)"))
        c.equal(short(b, "PIP-2"), ("SCI", 0), "one that left the query retires")
        c.equal(r["retired"], ["PIP-2"], "and is reported")
        r = pull(b, ("PIP-1", "Trekk Infrastructure (ST Client)"),
                    ("PIP-2", "SCI Infrastructure LLC. **PURCHASE** (3 Bots)"))
        c.equal(short(b, "PIP-2"), ("SCI", 1), "and comes back if it returns")

        # A pull that returned nothing is a failure, not an empty roster.
        r = pull(b)
        c.equal(short(b, "PIP-1"), ("Trekk Infrastructure", 1),
                "an empty pull retires nobody")
        c.equal(r["retired"], [], "and says it did nothing")
    return c.report()



def check_a_known_collision_stops_shouting() -> bool:
    """
    Worth saying once. Not worth saying every hour for ever.

    IPI has been two live customers since the roster arrived -- `IPI : El
    Paso` and `IPI : *REP*` -- and both are kept on purpose, because a list
    with the same word twice is still better than picking the wrong customer.
    A rule for telling them apart is owed and not yet written, so the report
    has to stay. What it must not be is scenery: an alarm that fires hourly
    for something already known is the one nobody reads when a *new*
    collision turns up.

    The set is remembered, and only a change speaks -- which means clearing
    speaks too, so the log says when it went away as well as when it came.
    """
    c = Check("a known collision stops shouting")

    IPI = [{"short_name": "ipi",
            "clients": [{"client_id": "PIP-2136"}, {"client_id": "PIP-3927"}]}]
    # The same set, listed the other way round: the same news, not new news.
    SWAPPED = [{"short_name": "ipi",
                "clients": [{"client_id": "PIP-3927"},
                            {"client_id": "PIP-2136"}]}]
    PLUS = IPI + [{"short_name": "sci",
                   "clients": [{"client_id": "PIP-1"}, {"client_id": "PIP-2"}]}]

    with Board() as b:
        c.ok(J.note_collisions(b.con, IPI), "the first one speaks")
        c.ok(not J.note_collisions(b.con, IPI), "the second time is quiet")
        c.ok(not J.note_collisions(b.con, SWAPPED),
             "and so is the same set in another order -- the order a query "
             "happens to return rows in is not news")
        c.ok(J.note_collisions(b.con, PLUS), "a new collision speaks")
        c.ok(not J.note_collisions(b.con, PLUS), "then goes quiet too")
        c.ok(J.note_collisions(b.con, IPI),
             "one of them clearing speaks, because that is news as well")
        c.ok(J.note_collisions(b.con, []), "and the last one clearing does")
        c.ok(not J.note_collisions(b.con, []),
             "after which there is nothing to say")

    # A database that has never pulled has no row, and that has to read as
    # "nothing reported yet" rather than as "no collisions".
    with Board() as b:
        c.equal(b.con.execute("SELECT COUNT(*) FROM client_collisions"
                              ).fetchone()[0], 0, "no row until a pull")
        c.ok(J.note_collisions(b.con, IPI),
             "so the first pull on a fresh database still speaks")

    return c.report()


def check_both_sides_of_a_collision_stay_offered() -> bool:
    """
    Keep both, which is what was asked for while the rule is owed.

    The detection only *reports*: nothing refuses to write the second row,
    nothing drops one from the dropdown, and the editor tells them apart by
    showing each one's Jira summary beside the short name. Picking the wrong
    customer is the failure this whole feature exists to stop, and silently
    hiding one of two live ones would be exactly that.
    """
    c = Check("both sides of a collision stay offered")

    with Board() as b:
        for cid, name in (("PIP-2136", "IPI : El Paso"),
                          ("PIP-3927", "IPI : *REP*")):
            b.con.execute(
                """INSERT INTO clients (client_id, name, name_key, short_name,
                                        offered, synced_at)
                   VALUES (?,?,?,?,1,?)""",
                (cid, name, name.lower(), "IPI", iso()))
        b.con.commit()

        found = J.collisions(b.con)
        c.equal(len(found), 1, "the collision is seen")
        c.equal(len(found[0]["clients"]), 2, "as two clients under one label")

        api.DB = b.path
        offered = [x for x in api.client_roster()["clients"]
                   if (x["short_name"] or "").lower() == "ipi"]
        c.equal(len(offered), 2, "and both are still offered to the editor")
        c.ok(all(x["ambiguous"] for x in offered),
             "each flagged, so the editor knows to show the summary")
        labels = {bert.client_label(x) for x in offered}
        c.equal(len(labels), 2,
                f"and the two read differently in the list ({labels})")
        c.ok(all("IPI" in l for l in labels), "both still called IPI")

    return c.report()



ROSTER = [
    {"short_name": "Trafford Borough", "name": "Trafford Borough", "aliases": []},
    {"short_name": "Duke's Root Control", "name": "Duke's Root Control",
     "aliases": ["Dukes Root Control", "duke s root control"]},
    {"short_name": "Duke's Omaha", "name": "Duke's Omaha", "aliases": []},
    {"short_name": "Inspect.AI", "name": "Inspect.AI", "aliases": ["Inspect AI"]},
    {"short_name": "SCI", "name": "SCI Infrastructure LLC.", "aliases": []},
    {"short_name": "Falmouth MA", "name": "Falmouth MA", "aliases": []},
    {"short_name": "Falmouth ME", "name": "Falmouth ME", "aliases": []},
]


def shorts(typed):
    return [c["short_name"] for c in bert.client_matches(typed, ROSTER)]


def check_a_name_typed_in_part_still_finds_its_client() -> bool:
    """
    The fuzzy tier compared whole against whole, and punished a partial name.

    `trafforf` against `traffordborough` is 0.61 -- under the 0.72 floor --
    and the miss is the half of the name that had not been typed yet, not the
    letters that were wrong. Against the *word* `trafford` it is 0.93. Typing
    on past the name hid the same way: somebody who gets `Trafford` right and
    then keeps going scored 0.48 on the whole string and 1.0 on the word.

    Reported from a real box: `Trafford NJKNKNKNLN` typed while aiming for
    Trafford Borough, and the list offered nothing at all.
    """
    c = Check("a name typed in part still finds its client")

    for typed in ("Trafford", "trafforf", "traford", "Trafford Bor",
                  "Trafford NJK", "Trafford NJKNKNKNLN"):
        c.ok("Trafford Borough" in shorts(typed),
             f"{typed!r} offers Trafford Borough")

    # Punctuation was always the common miss and still is.
    c.ok("Duke's Root Control" in shorts("dukes root control"),
         "an apostrophe left out still finds them")
    c.ok("Inspect.AI" in shorts("inspect ai"), "and a dot")

    return c.report()


def check_the_search_still_refuses_to_choose() -> bool:
    """
    Looser matching must not become deciding.

    `reconcile_aliases` refuses to merge on resemblance with nobody watching,
    and that rule is untouched -- this one only puts candidates in front of a
    person. The test of it is that an ambiguous query still returns *both*
    rather than picking, which is the invariant a wider net could quietly
    break by ranking one of them off the end of the list.
    """
    c = Check("the search offers rather than chooses")

    dukes = shorts("dukes")
    c.ok("Duke's Root Control" in dukes and "Duke's Omaha" in dukes,
         f"'dukes' offers both customers, and settles nothing ({dukes})")

    falmouth = shorts("falmouth")
    c.equal(sorted(x for x in falmouth if x.startswith("Falmouth")),
            ["Falmouth MA", "Falmouth ME"],
            "and so does 'falmouth' -- two different places, 0.91 similar")

    return c.report()


def check_nonsense_still_finds_nothing() -> bool:
    """A net wide enough to catch everything is not a search."""
    c = Check("nonsense still finds nothing")

    for typed in ("xyzzy", "zzzzzzzz", "qqqq", "12345"):
        c.equal(shorts(typed), [], f"{typed!r} offers nobody")

    # A three-letter name is never compared loosely: at that length almost
    # anything resembles almost anything, and the exact and prefix tiers
    # already catch the real ones.
    c.ok("SCI" in shorts("sci"), "a short name is still found exactly")
    c.ok("SCI" not in shorts("abc"),
         "but not by any other three letters")

    return c.report()



def check_an_unlisted_client_is_shown_not_offered() -> bool:
    """
    A typo on a card must not become a thing you can pick.

    The card's current client used to be added to the combo as an item when
    the roster had never heard of it -- meant for a retired customer, which
    is a real case: not offering one to anybody is not the same as taking it
    off the ticket that has it. But Bert cannot tell a retired client from a
    fat-fingered one; both are just a string the roster does not know. And an
    item is something you can *pick*, so a card whose title read `Trafford
    NJKNKNKNLN` put that in the dropdown beside the customers Jira knows
    about. Reported from exactly that.

    It is put in the box instead. `text()` reads the line edit, so `save()`
    and `is_dirty()` see it either way -- which is the property that makes
    showing it enough.

    Read off the source: a `ClientCombo` needs a QApplication, and the checks
    deliberately never make one.
    """
    c = Check("an unlisted client is shown, not offered")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    combo = next((n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.ClassDef) and n.name == "ClientCombo"),
                 None)
    c.ok(combo is not None, "there is a ClientCombo")
    if combo is None:
        return c.report()
    body = ast.get_source_segment(src, combo) or ""

    c.ok("addItem(current" not in body,
         "the card's own value is never added to the list")
    c.ok("setEditText(current)" in body,
         "it is put in the box instead, where it still saves")
    # The roster's own rows are of course still items -- that is the list.
    c.ok("self.addItem(label" in body,
         "and every client the roster does know is still offered")

    return c.report()



def check_a_name_the_roster_does_not_know_says_so() -> bool:
    """
    A caution at the moment the slip is made, not a refusal.

    Asked for after `Trafford NJKNKNKNLN` went into a title unremarked. The
    alternative on the table was making the title read-only and composing it
    from fields, which was measured against production and turned out to cost
    more than it saved: ten cards carry titles the fields cannot hold, and
    twenty-nine more would be silently renamed on the next save -- each a real
    Discord rename at two per ten minutes, for capitalisation nobody asked to
    change. So the title box stays and the client box speaks up instead.

    Never a block. A customer exists before Jira hears about them, which is
    why the box is pick-or-type at all; this only says which of the two just
    happened.
    """
    c = Check("a name the roster does not know says so")

    c.ok(bert.client_note("Trafford NJKNKNKNLN", ROSTER),
         "a typo is called out")
    c.equal(bert.client_note("Trafford Borough", ROSTER), "",
            "a real customer is not")
    c.equal(bert.client_note("trafford borough", ROSTER), "",
            "whatever the case")
    c.equal(bert.client_note("Duke's Root Control", ROSTER), "",
            "and whatever the punctuation")

    # An alias is a spelling the board has genuinely used, and the alias
    # table already points it at a client -- so it is a name that resolves,
    # badly spelled or not, and calling it unknown would be wrong.
    c.equal(bert.client_note("Dukes Root Control", ROSTER), "",
            "a spelling the board has used before resolves, so it is known")

    for blank in ("", "   ", None):
        c.equal(bert.client_note(blank, ROSTER), "",
                f"{blank!r} says nothing -- an empty client is the title "
                f"hint's business, not this one's")

    return c.report()


def check_it_does_not_nag_about_what_the_card_arrived_with() -> bool:
    """
    The caution is about what somebody just typed.

    A card carrying a retired customer, or one from before the roster
    existed, is not a mistake anybody is making now. Warning every time that
    card is opened is nagging, and a warning that is always on is furniture --
    the same reason the roster-staleness indicator stays quiet for six hours.
    """
    c = Check("it does not nag about what the card arrived with")

    c.equal(bert.client_note("HydroEdge", ROSTER, opened_with="HydroEdge"), "",
            "a name the card already had is left alone")
    c.ok(bert.client_note("HydroEdge", ROSTER, opened_with=""),
         "but the same name typed fresh is called out")
    c.equal(bert.client_note("HydroEdge ", ROSTER, opened_with="HydroEdge"), "",
            "and trailing space is not a change")
    c.ok(bert.client_note("HydroEdgex", ROSTER, opened_with="HydroEdge"),
         "while a letter added to it is")

    return c.report()


CHECKS = (check_a_name_typed_in_part_still_finds_its_client,
          check_a_name_the_roster_does_not_know_says_so,
          check_it_does_not_nag_about_what_the_card_arrived_with,
          check_an_unlisted_client_is_shown_not_offered,
          check_the_search_still_refuses_to_choose,
          check_nonsense_still_finds_nothing,
          check_a_known_collision_stops_shouting,
          check_both_sides_of_a_collision_stay_offered,
          
    check_the_short_name_cuts_the_note_not_the_name,
    check_only_the_starred_marker_retires_a_client,
    check_a_resync_keeps_a_hand_written_short_name,
    check_two_customers_never_share_a_line,
    check_one_ticket_key_collapses_every_spelling,
    check_names_that_merely_look_alike_are_left_alone,
    check_a_spelling_two_customers_answer_to_waits_for_a_person,
    check_reconciling_twice_changes_nothing,
    check_a_retired_client_still_names_its_cards,
    check_picking_a_client_does_not_vouch_for_the_card,
    check_the_editor_offers_the_roster_and_still_takes_anything,
    check_no_jira_means_no_change,
    check_a_missing_key_is_judged_by_what_it_is,
    check_punctuation_never_hides_a_client,
    check_a_mistyped_name_still_finds_its_client,
    check_the_customer_outranks_a_note_about_them,
    check_an_old_spelling_finds_the_right_customer,
    check_the_search_suggests_and_never_decides,
    check_the_roster_follows_jira_but_not_over_a_correction,
)


if __name__ == "__main__":
    sys.exit(0 if all(chk() for chk in CHECKS) else 1)

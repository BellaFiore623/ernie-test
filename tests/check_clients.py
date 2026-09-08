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

import sys

from support import Board, Check, iso

import bert
import ernie_extract as ex
import ernie_jira as J


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


CHECKS = (
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
)


if __name__ == "__main__":
    sys.exit(0 if all(chk() for chk in CHECKS) else 1)

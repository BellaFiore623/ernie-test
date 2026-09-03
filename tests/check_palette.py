"""
Unassigned carries no colour, because it is not a priority.

It used to be washed in a red a shade off critical's, so the top of the board
read as an emergency when all it meant was that nobody had sorted it yet --
and the one thing red should mean here, a card that needs a person, had to
compete with a whole band wearing it. Unassigned is white on grey now and the
red belongs to triage alone -- as an outline, not a fill, so a card nobody can
read still says which band it is sitting in.

Also checks every band has an entry in all three maps: Band indexes
BAND_TINT and BAND_TEXT directly, so a band missing from either is a
KeyError while the board is drawing itself, not a wrong colour.
"""

from support import Check

import bert


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hue_spread(h):
    """How far from grey a colour is: 0 is neutral, higher is a real hue."""
    r, g, b = rgb(h)
    return max(r, g, b) - min(r, g, b)


def check_every_band_is_painted() -> bool:
    c = Check("every band has a colour")

    for band in bert.BANDS:
        c.ok(band in bert.BAND_TINT, f"{band} has a wash")
        c.ok(band in bert.BAND_CARD, f"{band} has a card colour")
        c.ok(band in bert.BAND_TEXT, f"{band} has heading ink")
        c.ok(band in bert.BAND_LABEL, f"{band} has a label")

    return c.report()


def check_unassigned_is_neutral() -> bool:
    c = Check("unassigned is not an alarm")

    card, edge = bert.BAND_CARD["unassigned"]
    c.equal(card.upper(), "#FFFFFF", "the ticket itself is white")
    c.ok(hue_spread(bert.BAND_TINT["unassigned"]) <= 12,
         "and sits on a wash with no real hue in it")
    c.ok(hue_spread(edge) <= 16, "with a neutral edge")

    # The point of the change: it must not read as critical's quieter sibling.
    crit_card, _ = bert.BAND_CARD["critical"]
    c.ok(card.upper() != crit_card.upper(), "it is not critical's colour")
    c.ok(hue_spread(crit_card) > hue_spread(card),
         "critical is the one wearing a hue")
    c.ok(bert.BAND_TEXT["unassigned"] != bert.RED_FG,
         "and its heading is no longer red ink")

    return c.report()


def a_card(priority="unassigned", *, unreadable=False, override=None):
    return {"priority": priority,
            "issues": ["title_none"] if unreadable else [],
            "client_override": override}


def check_triage_is_outlined_not_filled() -> bool:
    c = Check("an unreadable card keeps its band's fill")

    white, _ = bert.BAND_CARD["unassigned"]
    fill, edge, px = bert.card_skin(a_card(unreadable=True))
    c.equal(fill, white, "an unassigned one stays white, so it still reads so")
    c.equal(edge, bert.RED_EDGE, "and says the rest with a red outline")
    c.equal(px, 2, "drawn thicker than an ordinary edge")

    # Filling it red was the old behaviour, in two places at once.
    c.ok(fill.upper() != bert.RED_BG.upper(), "the fill is not the red wash")

    # It reads the same way in every band, so a triage card dragged out of
    # unassigned still says which band it landed in.
    for band in bert.BANDS:
        f, e, _ = bert.card_skin(a_card(band, unreadable=True))
        c.equal(f, bert.BAND_CARD[band][0], f"{band} keeps its own fill")
        c.equal(e, bert.RED_EDGE, f"{band} still gets the red outline")

    return c.report()


def check_the_other_skins() -> bool:
    c = Check("what card_skin says the rest of the time")

    white, neutral = bert.BAND_CARD["unassigned"]
    c.equal(bert.card_skin(a_card()), (white, neutral, 1), "an ordinary card")
    c.equal(bert.card_skin(a_card(), editing=True), (white, bert.ACCENT, 1),
            "one with its editor open")

    # Triage outranks the editor: a card nobody can read is still that.
    _, edge, px = bert.card_skin(a_card(unreadable=True), editing=True)
    c.equal((edge, px), (bert.RED_EDGE, 2), "and triage wins over both")

    # A client typed in by hand is the acknowledgement, so the red clears.
    _, edge, _ = bert.card_skin(a_card(unreadable=True, override="Penn Hills"))
    c.ok(edge != bert.RED_EDGE, "a hand-typed client clears the outline")

    # Band indexing here is a .get with a fallback, unlike Band's -- a card
    # arriving with a priority this build has never heard of must still draw.
    c.equal(bert.card_skin(a_card("something-new")),
            bert.card_skin(a_card("low")), "an unknown band falls back to low")

    return c.report()


def check_red_is_left_to_triage() -> bool:
    c = Check("red means one thing")

    c.ok(hue_spread(bert.RED_EDGE) > 100, "the triage outline is properly red")

    _, unassigned_edge = bert.BAND_CARD["unassigned"]
    c.ok(unassigned_edge.upper() != bert.RED_EDGE.upper(),
         "which nothing in the unassigned band shares")
    c.ok(bert.BAND_TEXT["unassigned"] != bert.RED_EDGE,
         "nor its heading ink")

    return c.report()


CHECKS = (check_every_band_is_painted, check_unassigned_is_neutral,
          check_triage_is_outlined_not_filled, check_the_other_skins,
          check_red_is_left_to_triage)

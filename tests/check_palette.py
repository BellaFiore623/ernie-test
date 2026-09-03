"""
Two palettes, the same keys, and no colour reached any other way.

Unassigned carries no colour, because it is not a priority. It used to be
washed in a red a shade off critical's, so the top of the board read as an
emergency when all it meant was that nobody had sorted it yet -- and the one
thing red should mean here, a card that needs a person, had to compete with a
whole band wearing it. It is the plain surface on a neutral wash now, and the
red belongs to triage alone: an outline, not a fill, so a card nobody can read
still says which band it is sitting in.

Dark mode is why the colours became a table instead of a hundred and seventy
constants. The invariant that keeps it working is parity -- a key in one
palette and not the other is a crash the first time that widget draws in the
theme that is missing it, and only in that theme, which is the kind of bug
that ships.
"""

import ast
import pathlib

from support import Check

import bert


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lum(h):
    r, g, b = rgb(h)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def hue_spread(h):
    """How far from grey a colour is: 0 is neutral, higher is a real hue."""
    r, g, b = rgb(h)
    return max(r, g, b) - min(r, g, b)


def flatten(p):
    """Every key in a palette, including the ones nested a level down."""
    out = set()
    for k, v in p.items():
        out.add(k)
        if isinstance(v, dict):
            out |= {f"{k}.{i}" for i in v}
    return out


def swatches(p):
    """Every actual colour string in a palette."""
    out = []
    for v in p.values():
        if isinstance(v, dict):
            for i in v.values():
                out += list(i) if isinstance(i, tuple) else [i]
        elif isinstance(v, tuple):
            out += list(v)
        else:
            out.append(v)
    return [c for c in out if isinstance(c, str) and c.startswith("#")]


def in_theme(name, fn):
    """Run a check body under one palette, and put the module back after."""
    try:
        bert.T.use(name)
        return fn()
    finally:
        bert.T.use("light")


def check_palettes_agree() -> bool:
    c = Check("the two palettes have the same keys")

    light, dark = flatten(bert.LIGHT), flatten(bert.DARK)
    c.equal(sorted(light - dark), [], "nothing in light is missing from dark")
    c.equal(sorted(dark - light), [], "nothing in dark is missing from light")

    for band in bert.BANDS:
        for group in ("band_tint", "band_card", "band_text"):
            c.ok(band in bert.LIGHT[group] and band in bert.DARK[group],
                 f"{band} has a {group.replace('_', ' ')} in both")

    # Band and the feed index these directly, not with .get, so a band missing
    # from either map is a KeyError while the board is drawing itself.
    for band in bert.BANDS:
        c.ok(band in bert.BAND_LABEL, f"{band} has a label")

    return c.report()


def check_each_palette_is_the_right_end() -> bool:
    c = Check("dark is dark and light is light")

    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        ink, surface, canvas = palette["ink"], palette["surface"], palette["canvas"]
        if name == "light":
            c.ok(lum(surface) > 200, "light draws on a bright surface")
            c.ok(lum(ink) < 80, "in dark ink")
        else:
            c.ok(lum(surface) < 80, "dark draws on a dim surface")
            c.ok(lum(ink) > 180, "in pale ink")
        c.ok(abs(lum(ink) - lum(surface)) > 120,
             f"{name}: ink stands off its surface")
        c.ok(abs(lum(ink) - lum(canvas)) > 120,
             f"{name}: and off the canvas behind it")

    # Every colour is a real hex, in both. A typo here is a silently ignored
    # stylesheet rule, which Qt reports nowhere.
    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        bad = [s for s in swatches(palette) if len(s) != 7]
        c.equal(bad, [], f"{name}: every value is a #rrggbb")

    # A quiet tag -- an equipment number, a ticket count -- sits near the
    # surface it is on. The light grey read as quiet under black text and
    # became the brightest thing on the card once the card went dark.
    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        c.ok(abs(lum(palette["chip_bg"]) - lum(palette["surface"])) < 45,
             f"{name}: a plain tag stays close to the card under it")

    # Text on an accent fill has to survive it, in both.
    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        c.ok(abs(lum(palette["on_accent"]) - lum(palette["accent"])) > 80,
             f"{name}: label on an accent button is readable")

    return c.report()


def a_card(priority="unassigned", *, unreadable=False, override=None):
    return {"priority": priority,
            "issues": ["title_none"] if unreadable else [],
            "client_override": override}


def check_unassigned_is_neutral() -> bool:
    c = Check("unassigned is not an alarm")

    def body():
        card, edge = bert.T.BAND_CARD["unassigned"]
        # The plain surface, whichever theme that is -- white on light, the
        # card colour on dark. Not a hardcoded white, which was the old bug
        # in a new costume.
        c.equal(card.upper(), bert.T.SURFACE.upper(),
                f"{bert.T.name}: the ticket is the plain surface")
        crit, _ = bert.T.BAND_CARD["critical"]
        c.ok(hue_spread(crit) > hue_spread(card),
             f"{bert.T.name}: critical is the one wearing a hue")
        c.ok(hue_spread(bert.T.BAND_TINT["unassigned"])
             < hue_spread(bert.T.BAND_TINT["critical"]),
             f"{bert.T.name}: and its wash is the quieter of the two")
        c.ok(bert.T.BAND_TEXT["unassigned"] != bert.T.RED_FG,
             f"{bert.T.name}: its heading is not red ink")
        c.ok(edge != bert.T.RED_EDGE,
             f"{bert.T.name}: nor is its edge")
        return True

    in_theme("light", body)
    in_theme("dark", body)
    return c.report()


def check_triage_is_outlined_not_filled() -> bool:
    c = Check("an unreadable card keeps its band's fill")

    def body():
        fill, edge, px = bert.card_skin(a_card(unreadable=True))
        c.equal(fill, bert.T.SURFACE,
                f"{bert.T.name}: an unassigned one keeps the plain surface")
        c.equal(edge, bert.T.RED_EDGE, f"{bert.T.name}: red outline")
        c.equal(px, 2, f"{bert.T.name}: drawn thicker than an ordinary edge")
        c.ok(fill.upper() != bert.T.RED_BG.upper(),
             f"{bert.T.name}: the fill is not the red wash")

        # Every band, so a triage card dragged out of unassigned still says
        # which band it landed in.
        for band in bert.BANDS:
            f, e, _ = bert.card_skin(a_card(band, unreadable=True))
            c.equal(f, bert.T.BAND_CARD[band][0],
                    f"{bert.T.name}: {band} keeps its own fill")
            c.equal(e, bert.T.RED_EDGE,
                    f"{bert.T.name}: {band} still gets the outline")
        return True

    in_theme("light", body)
    in_theme("dark", body)
    return c.report()


def check_the_other_skins() -> bool:
    c = Check("what card_skin says the rest of the time")

    def body():
        surface, neutral = bert.T.BAND_CARD["unassigned"]
        c.equal(bert.card_skin(a_card()), (surface, neutral, 1),
                f"{bert.T.name}: an ordinary card")
        c.equal(bert.card_skin(a_card(), editing=True),
                (surface, bert.T.ACCENT, 1),
                f"{bert.T.name}: one with its editor open")

        # Triage outranks the editor: a card nobody can read is still that.
        _, edge, px = bert.card_skin(a_card(unreadable=True), editing=True)
        c.equal((edge, px), (bert.T.RED_EDGE, 2),
                f"{bert.T.name}: triage wins over both")

        # A client typed in by hand is the acknowledgement, so the red clears.
        _, edge, _ = bert.card_skin(a_card(unreadable=True, override="Penn"))
        c.ok(edge != bert.T.RED_EDGE,
             f"{bert.T.name}: a hand-typed client clears the outline")

        # .get with a fallback here, unlike Band's -- a card arriving with a
        # priority this build has never heard of must still draw.
        c.equal(bert.card_skin(a_card("something-new")),
                bert.card_skin(a_card("low")),
                f"{bert.T.name}: an unknown band falls back to low")
        return True

    in_theme("light", body)
    in_theme("dark", body)
    return c.report()


def check_choosing_a_theme() -> bool:
    c = Check("what Settings offers")

    c.equal(sorted(bert.THEMES), ["dark", "light", "system"], "three choices")
    c.equal(sorted(bert.THEME_LABEL), sorted(bert.THEMES),
            "each one has a label to show")

    c.equal(bert.resolve_theme("light"), "light", "light means light")
    c.equal(bert.resolve_theme("dark"), "dark", "dark means dark")
    # No QApplication in the checks, so there is no desktop to ask; the safe
    # answer is the palette every stylesheet here was written against.
    c.equal(bert.resolve_theme("system"), "light",
            "system falls back to light when nobody can say")
    c.equal(bert.resolve_theme("nonsense"), "light",
            "and so does a setting from some future build")

    bert.T.use("dark")
    c.ok(bert.T.dark, "T.dark says which one is loaded")
    c.equal(bert.T.SURFACE, bert.DARK["surface"], "and T reads that palette")
    bert.T.use("light")
    c.ok(not bert.T.dark, "and back")
    c.equal(bert.T.SURFACE, bert.LIGHT["surface"], "reading the other one")

    # A colour neither palette has is a mistake worth hearing about at once.
    try:
        bert.T.NOT_A_COLOUR
        c.ok(False, "an unknown colour should not resolve")
    except AttributeError:
        c.ok(True, "an unknown colour raises rather than returning None")

    return c.report()


def check_nothing_freezes_a_colour() -> bool:
    """No colour may be read at import time, before a theme is chosen.

    bert.py is imported, and only then is a palette applied -- so anything
    that reads T while the module is still being executed keeps whichever
    palette happened to be loaded first, for ever. It has happened twice: a
    composed stylesheet held as a constant, and the refresh glyph's colour
    sitting in a default argument, which left it drawing in light ink on a
    dark toolbar. Both were invisible to every other check here.
    """
    c = Check("no colour is frozen at import")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def reads_theme(node):
        return any(isinstance(n, ast.Name) and n.id == "T" for n in ast.walk(node))

    frozen = []
    for node in tree.body:                       # module level only
        if isinstance(node, ast.Assign) and reads_theme(node.value):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            frozen.append(f"line {node.lineno}: {', '.join(names) or 'assignment'}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
                if reads_theme(d):
                    frozen.append(f"line {node.lineno}: default arg of {node.name}()")

    c.equal(frozen, [], "nothing reads T while the module is being executed")

    # The one that got away, named so a reader knows what this is guarding.
    spin = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "spin_icon")
    colour = spin.args.defaults[0] if spin.args.defaults else None
    c.ok(isinstance(colour, ast.Constant) and colour.value is None,
         "spin_icon takes its colour on the call, not in the signature")

    return c.report()


class FakeBoard:
    """Enough of Bert to ask what it does when the desktop changes."""

    def __init__(self, choice):
        self.settings = {"theme": choice}
        self.rebuilt = 0

    def rebuild_in_new_theme(self):
        self.rebuilt += 1

    desktop_theme_changed = bert.Bert.desktop_theme_changed


def with_desktop(dark, fn):
    """Run something with the desktop reporting light or dark."""
    was = bert.desktop_is_dark
    try:
        bert.desktop_is_dark = lambda: dark
        return fn()
    finally:
        bert.desktop_is_dark = was


def check_following_the_desktop() -> bool:
    """The preference, on a machine set the other way from this one.

    Worth checking on its own because it is the one behaviour that cannot be
    seen where it is written: a light desktop reports light whatever the code
    does, so a reversed reading looks perfectly correct until somebody opens
    it on a dark machine.
    """
    c = Check("light or dark, and following")

    for dark in (True, False):
        want = "dark" if dark else "light"
        c.equal(with_desktop(dark, lambda: bert.resolve_theme("system")), want,
                f"a {want} desktop and 'system' gives {want}")
        # An explicit choice is a decision. The desktop does not overrule it,
        # in either direction -- which is the half that broke elsewhere.
        c.equal(with_desktop(dark, lambda: bert.resolve_theme("light")), "light",
                f"'light' stays light on a {want} desktop")
        c.equal(with_desktop(dark, lambda: bert.resolve_theme("dark")), "dark",
                f"'dark' stays dark on a {want} desktop")

    # No desktop to ask: the palette every stylesheet was written against.
    c.equal(bert.resolve_theme("system" if False else "nonsense"), "light",
            "an unreadable setting falls back to light")

    return c.report()


def check_the_desktop_changing_underneath() -> bool:
    c = Check("when the desktop changes while Bert is open")

    def ran(choice, showing, desktop_dark):
        b = FakeBoard(choice)
        bert.T.use(showing)
        with_desktop(desktop_dark, b.desktop_theme_changed)
        bert.T.use("light")
        return b.rebuilt

    c.equal(ran("system", "light", True), 1, "following: light board, dark desktop")
    c.equal(ran("system", "dark", False), 1, "following: dark board, light desktop")
    c.equal(ran("system", "dark", True), 0, "following, but already dark: nothing")
    c.equal(ran("system", "light", False), 0, "following, already light: nothing")

    # Somebody who picked a side keeps it when the sun goes down.
    c.equal(ran("light", "light", True), 0, "chose light: the desktop is ignored")
    c.equal(ran("dark", "dark", False), 0, "chose dark: likewise")

    return c.report()


CHECKS = (check_nothing_freezes_a_colour, check_palettes_agree,
          check_following_the_desktop, check_the_desktop_changing_underneath, check_each_palette_is_the_right_end,
          check_unassigned_is_neutral, check_triage_is_outlined_not_filled,
          check_the_other_skins, check_choosing_a_theme)

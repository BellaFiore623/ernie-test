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
import colorsys
import pathlib
import re

from support import Check

import bert
import ernie_extract as ex


ROOT = pathlib.Path(__file__).resolve().parent.parent

# The separator the toolbar's two indicators are written with. Spelled by
# code point so the check file's own encoding cannot be the thing that
# breaks it.
DOT = chr(0xB7)


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lum(h):
    r, g, b = rgb(h)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio. The offset is what makes two near-blacks
    comparable to two near-whites -- a raw luminance ratio says dark separates
    its cards 3x and light 1x, which is an artefact of dividing tiny numbers.
    """
    def rel(h):
        ch = [c / 255 for c in rgb(h)]
        ch = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    hi, lo = sorted((rel(a), rel(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def hue_of(h):
    """Where a colour sits on the wheel, in degrees. Meaningless for a grey."""
    r, g, b = (c / 255 for c in rgb(h))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


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


def a_card(priority="unassigned", *, queue="PROD", unreadable=False,
           override=None):
    return {"priority": priority, "queue": queue,
            "issues": ["title_none"] if unreadable else [],
            "client_override": override}


def check_a_ticket_wears_its_tag() -> bool:
    """
    The colour on a ticket is what it is, not where it sits.

    It used to be the priority band, which the band it is sitting in already
    says -- so the board spent its whole colour budget saying the same thing
    twice, and the tag, which is the part that tells you what the work
    actually is, got a chip and a hairline.
    """
    c = Check("a ticket wears its tag")

    def body():
        for q in bert.T.QUEUE:
            stripe, tint, _ = bert.T.QUEUE[q]
            fill, edge, px = bert.card_skin(a_card("high", queue=q))
            c.equal(fill, tint, f"{bert.T.name}: {q} is filled with its own colour")
            c.equal(edge, stripe, f"{bert.T.name}: {q} is edged with it too")
            c.equal(px, 1, f"{bert.T.name}: {q} at an ordinary weight")

        # The same ticket in another band is the same colour: the band is
        # where it sits, and the band says that itself.
        for band in ("critical", "high", "medium", "low"):
            c.equal(bert.card_skin(a_card(band, queue="OPS")),
                    bert.card_skin(a_card("high", queue="OPS")),
                    f"{bert.T.name}: OPS reads the same in {band}")

        # A tag with no colour -- retired, or from a later build -- is neutral
        # rather than a crash.
        fill, edge, _ = bert.card_skin(a_card("high", queue="DATA"))
        c.equal((fill, edge), (bert.T.NEUTRAL[1], bert.T.NEUTRAL[0]),
                f"{bert.T.name}: an unknown tag falls back to neutral")
        return True

    in_theme("light", body)
    in_theme("dark", body)
    return c.report()


def check_needs_attention_is_the_alarm() -> bool:
    """
    The one band that is asking for something rather than describing one.

    Unassigned was deliberately neutral while critical wore the red, because
    two red bands at the top of a board is an emergency that isn't one. Now
    that a ticket's colour is its tag, red is free to mean one thing: nobody
    has picked this up.
    """
    c = Check("needs attention is the alarm")

    c.equal(bert.BAND_LABEL["unassigned"], "Needs Attention",
            "the band says what it wants, not what it lacks")
    c.ok(bert.CAUTION, "and there is a sign to put beside it")

    def body():
        fill, edge, px = bert.card_skin(a_card("unassigned"))
        card, rim = bert.T.BAND_CARD["unassigned"]
        c.equal((fill, edge, px), (card, rim, 1),
                f"{bert.T.name}: it takes the band's colour, not its tag's")
        c.equal(edge, bert.T.RED_EDGE, f"{bert.T.name}: which is the red edge")
        c.equal(bert.T.BAND_TEXT["unassigned"], bert.T.RED_FG,
                f"{bert.T.name}: and the heading is red ink to match")

        # It outranks the tag, or a PROD ticket nobody has picked up would
        # read as ordinary PROD work.
        for q in bert.T.QUEUE:
            c.equal(bert.card_skin(a_card("unassigned", queue=q))[0], card,
                    f"{bert.T.name}: {q} is still red while it sits here")
        return True

    in_theme("light", body)
    in_theme("dark", body)
    return c.report()


def check_triage_is_outlined_not_filled() -> bool:
    c = Check("an unreadable card is outlined, whatever it is filled with")

    def body():
        for band in bert.BANDS:
            for q in ("PROD", "CS"):
                plain, _, _ = bert.card_skin(a_card(band, queue=q))
                fill, edge, px = bert.card_skin(
                    a_card(band, queue=q, unreadable=True))
                c.equal(fill, plain,
                        f"{bert.T.name}: {band}/{q} keeps the fill it had")
                c.equal(edge, bert.T.RED_EDGE,
                        f"{bert.T.name}: {band}/{q} gets the outline")
                c.equal(px, 2,
                        f"{bert.T.name}: {band}/{q} drawn thicker than ordinary")
        return True

    in_theme("light", body)
    in_theme("dark", body)
    return c.report()


def check_the_other_skins() -> bool:
    c = Check("what card_skin says the rest of the time")

    def body():
        stripe, tint, _ = bert.T.QUEUE["PROD"]
        c.equal(bert.card_skin(a_card("high")), (tint, stripe, 1),
                f"{bert.T.name}: an ordinary card")
        c.equal(bert.card_skin(a_card("high"), editing=True),
                (tint, bert.T.ACCENT, 1),
                f"{bert.T.name}: one with its editor open")

        # Triage outranks the editor: a card nobody can read is still that.
        _, edge, px = bert.card_skin(a_card("high", unreadable=True),
                                     editing=True)
        c.equal((edge, px), (bert.T.RED_EDGE, 2),
                f"{bert.T.name}: triage wins over both")

        # A client typed in by hand is the acknowledgement, so the red clears.
        _, edge, _ = bert.card_skin(
            a_card("high", unreadable=True, override="Penn"))
        c.ok(edge != bert.T.RED_EDGE,
             f"{bert.T.name}: a hand-typed client clears the outline")

        # A band this build has never heard of must still draw, and it is no
        # longer the band that decides the colour anyway.
        c.equal(bert.card_skin(a_card("something-new")),
                bert.card_skin(a_card("low")),
                f"{bert.T.name}: an unknown band draws like any other")
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


def check_the_queues_the_board_can_draw() -> bool:
    """
    The editor offered five tags and the palette had four colours for them.

    A DATA card came out neutral grey with no filter checkbox of its own,
    because the filters are built by walking the palette. Neither end was
    wrong on its own; they had simply drifted, and nothing held them together.

    DATA is retired now, so the fix is the four-colour palette and an editor
    that offers four -- but the parser still has to know all five, or the one
    DATA thread left in the mirror stops matching, falls to UNREADABLE, and is
    ranked to the top of unassigned as a card nobody can read.
    """
    c = Check("the queues the board can draw")

    # Membership, not order: the palette's order is the order the filter
    # chips sit in, and QUEUES_OFFERED's is the order of the dropdown. Each
    # is entitled to its own; having the same members is the invariant.
    for name in ("light", "dark"):
        bert.T.use(name)
        c.equal(set(bert.T.QUEUE), set(ex.QUEUES_OFFERED),
                f"{name}: a colour for every tag the editor offers, and no more")
    bert.T.use("light")

    # The filters are built by walking the palette, so parity above is what
    # gives every offered tag a checkbox.
    for q in ex.QUEUES_OFFERED:
        c.ok(q in bert.T.QUEUE, f"{q} can be filtered")

    # Retired queues are parsed, never offered.
    c.ok(ex.RETIRED_QUEUES, "there is at least one retired queue to speak of")
    for q in ex.RETIRED_QUEUES:
        c.ok(q in ex.QUEUES, f"{q} still parses")
        c.ok(q not in ex.QUEUES_OFFERED, f"{q} is not offered")
        c.ok(q not in bert.T.QUEUE, f"{q} has no colour, and falls back to neutral")

    # The whole point: a retired title still reads.
    t = ex.parse_title("DATA: Fleet - 02Sep26 - Backfill equipment master ids")
    c.equal(t.queue, "DATA", "a retired title parses rather than going unreadable")
    c.equal(t.client_raw, "Fleet", "and gives up its client like any other")
    c.equal(t.confidence, "strict", "at full confidence, not as something unreadable")

    return c.report()


def check_only_the_header_is_tinted() -> bool:
    """
    The band's colour names the band; it does not wash the tickets.

    Both the header and the panel the cards sit in were filled with
    BAND_TINT, so every ticket had a second colour behind it -- and once
    tickets took their tag's colour that meant a PROD card read as amber on
    blue in Medium and amber on amber in High. The header keeps it, because
    the header is the thing that says which band this is.

    Read off the source: building a Band needs a QApplication, and the checks
    deliberately never make one -- it does not raise, it aborts the process.
    """
    c = Check("only the band header carries the band's colour")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    head = next((l for l in src.splitlines() if "#bandHeader {{" in l), "")
    panel = next((l for l in src.splitlines() if "#bandPanel {" in l), "")

    c.ok(head, "the header still styles itself")
    c.ok("{tint}" in head, "and is filled with the band's tint")

    c.ok(panel, "the panel still styles itself")
    c.ok("{tint}" not in panel and "tint" not in panel,
         "but not with the tint -- the tickets carry the colour now")
    c.ok("transparent" in panel, "it lets the board through instead")

    # And the tint is not reaching the cards by some other route.
    uses = [l.strip() for l in src.splitlines() if "T.BAND_TINT" in l]
    c.equal(len(uses), 1, f"BAND_TINT is read in one place only ({uses})")

    return c.report()


def check_a_band_leaves_no_stray_windows() -> bool:
    """A QWidget with no parent is a top-level window, shown or not.

    Band made a QLabel("") for every band that had no hint to show and then
    never added it to anything, and built empty_hint unparented because the
    layout only wants it during a drag. Nine of them survived startup on a
    five-band board -- measured with QApplication.topLevelWidgets() -- and a
    theme change, which builds the window again, made nine more.

    Read off the source, like the tint check above: building a Band with no
    QApplication does not raise, it takes the process down.
    """
    c = Check("a band leaves no stray windows")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    band = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == "Band"), None)
    init = next((n for n in band.body if isinstance(n, ast.FunctionDef)
                 and n.name == "__init__"), None) if band else None
    c.ok(init is not None, "Band still has an __init__ to read")

    labels = [n for n in ast.walk(init)] if init else []
    calls = [n for n in labels if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "QLabel"]

    # Every label it makes is either given a parent or added to a layout.
    body = ast.get_source_segment(src, init) or ""
    c.ok('QLabel("")' not in body,
         "no empty label is made for a band with nothing to say")
    c.ok("self.hint = None" in body,
         "that band carries no hint at all")
    drop = [n for n in calls
            if n.args and getattr(n.args[0], "value", None) == "drop here"]
    c.ok(drop and len(drop[0].args) >= 2,
         "the drop hint is parented when it is made, not when it is used")
    return c.report()


def check_nothing_is_unparented_while_it_is_visible() -> bool:
    """setParent(None) on a visible widget makes it a visible window.

    The teardown here unparents before deleteLater on purpose -- a widget
    still parented to the panel keeps painting at the geometry it had, and a
    rebuild mid-drag left the old rows on screen under the new ones. But
    deleteLater only queues the deletion, so between the unparenting and the
    event loop catching up, every one of those widgets is a top-level window.

    Rebuilding the feed threw away 151 rows and put 151 blank windows on the
    desktop for about 1.2 seconds each, titled "python3" because that is the
    name Qt takes for the application. Counted with EnumWindows during a real
    ./run.sh test bert: 152 new windows, 151 of them that. Hiding first is the
    whole fix, and it has to hold at every one of these sites.
    """
    c = Check("nothing is unparented while it is still visible")

    lines = pathlib.Path(bert.__file__).read_text(encoding="utf-8").splitlines()
    sites = [n for n, l in enumerate(lines) if l.strip() == "w.setParent(None)"]
    c.ok(sites, f"there are still teardown sites to check ({len(sites)})")
    for n in sites:
        before = lines[n - 1].strip()
        c.ok(before == "w.hide()",
             f"bert.py:{n + 1} hides before unparenting  (line above is {before!r})")
    return c.report()


def check_a_tooltip_is_readable() -> bool:
    """A container's stylesheet must not cascade into its tooltips.

    Rail set `background: <canvas>` with no selector. In Qt that applies to
    the widget *and everything under it*, including the tooltip a child owns
    -- so hovering a row in the running order produced a box the right size
    for its three lines, painted near-black, with the text the same colour as
    the box. Measured against a plain QWidget in the same process: readable
    there, solid black here.

    Two things fix it and both are worth keeping. Scoping the rule to `Rail`
    stops the cascade. Stating QToolTip on the application settles it whatever
    else cascades -- and is needed anyway, because Qt draws tooltips itself
    and ignores the ToolTipBase/ToolTipText already in the palette, so they
    came out the system's pale yellow in the middle of a dark board.
    """
    c = Check("a tooltip is readable")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    rail = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == "Rail"), None)
    init = next((n for n in rail.body if isinstance(n, ast.FunctionDef)
                 and n.name == "__init__"), None) if rail else None
    body = (ast.get_source_segment(src, init) or "") if init else ""
    c.ok("Rail {{ background:" in body,
         "the rail's background rule names the rail")
    # The rail's own rule, not its children's. A leaf like the drop marker
    # can carry a bare background safely -- it has nothing under it and owns
    # no tooltip. A container cannot.
    c.ok('self.setStyleSheet(f"background:' not in body,
         "and the container's own rule is not left to fall on everything")

    theme = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "apply_theme"), None)
    tbody = (ast.get_source_segment(src, theme) or "") if theme else ""
    c.ok("QToolTip" in tbody, "the theme states what a tooltip looks like")
    c.ok("app.setStyleSheet" in tbody, "on the application, so it always wins")
    for token in ("T.INK", "T.SURFACE"):
        c.ok(token in tbody.split("QToolTip")[-1] if "QToolTip" in tbody else False,
             f"in {token}, not a hex typed into the rule")
    return c.report()


def check_a_finished_work_item_says_so() -> bool:
    """A ticked bubble leaves the card and waits in the editor.

    The card is the list of what is left, so a finished item comes off it.
    The editor is where the history is: there it shows unfilled with a dashed
    green border, and a double-click puts it back to outstanding. Double, not
    single -- a stray click must not undo finished work, and the second click
    is the confirmation, which is cheaper than a dialog on every tick.

    The x stays either way. Removing a bubble says it should not be on the
    card at all, which is as true of something ticked off as of something
    outstanding.
    """
    c = Check("a finished work item says so")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def meth(cls_name, fn):
        cls = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == cls_name), None)
        if cls is None:
            return None
        return next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                     and n.name == fn), None)

    view = meth("Card", "_build_view")
    vbody = (ast.get_source_segment(src, view) or "") if view else ""
    c.ok('if not i.get("done")' in vbody,
         "the card shows only what still needs doing")

    edit = meth("Card", "enter_edit")
    ebody = (ast.get_source_segment(src, edit) or "") if edit else ""
    c.ok('WorkBar(d.get("work_items") or [], editing=True)' in ebody,
         "and the editor is given all of them, finished ones included")

    init = meth("Bubble", "__init__")
    ibody = (ast.get_source_segment(src, init) or "") if init else ""
    c.ok(init is not None and any(a.arg == "done" for a in init.args.args),
         "a bubble knows whether it is finished")
    c.ok("T.OK_FG" in ibody, "and wears the palette's green, not a hex")
    c.ok('"dashed" if editing' in ibody and '"transparent" if editing' in ibody,
         "unfilled and dashed where it is shown")
    c.ok("self.btn" in ibody and "self.btn = None" not in ibody,
         "keeping its x, because removing one is a different act")

    dbl = meth("Bubble", "mouseDoubleClickEvent")
    dbody = (ast.get_source_segment(src, dbl) or "") if dbl else ""
    c.ok("self.done" in dbody and "reopened.emit" in dbody,
         "a double-click on a finished one puts it back")

    # Held until Save, like everything else the editor does.
    save = meth("Card", "save")
    sbody = (ast.get_source_segment(src, save) or "") if save else ""
    c.ok("work_undone" in sbody, "and it travels with the batched save")
    dirty = meth("Card", "is_dirty")
    c.ok("undone()" in (ast.get_source_segment(src, dirty) or "" if dirty else ""),
         "so closing the editor on one asks first")
    return c.report()


def check_starting_a_ticket_is_not_editing_one() -> bool:
    """A ticket being started has nothing behind it, and the words follow.

    "Unsaved changes" and "Save" are both wrong for something that does not
    exist: discarding loses the whole ticket, not an edit to a card that will
    still be there afterwards. The collision dialog says so, and the editor's
    button says Create rather than Save.

    Read off the source: the dialog needs a QApplication and a second open
    editor, and the checks deliberately build neither.
    """
    c = Check("starting a ticket is not editing one")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def meth(cls_name, fn):
        cls = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == cls_name), None)
        if cls is None:
            return ""
        f = next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == fn), None)
        return (ast.get_source_segment(src, f) or "") if f else ""

    c.ok("NEW_TICKET" in src, "a ticket with no thread stands under a sentinel")

    busy = meth("Bert", "editor_is_busy")
    c.ok("starting = busy == NEW_TICKET" in busy,
         "the collision dialog knows which case it is in")
    c.ok("Keep writing" in busy and "Keep editing" in busy,
         "and says keep writing rather than keep editing")
    c.ok("Create it, then" in busy,
         "offers to create it rather than save it")
    c.ok("would be lost" in busy,
         "and says plainly that discarding loses the whole thing")

    start = meth("Bert", "start_ticket")
    after = start.split("editor_is_busy(")[1][:100] if "editor_is_busy(" in start else ""
    c.ok("NEW_TICKET" in after,
         "starting one goes through the same one-editor rule")
    c.ok("self.editing_card = NEW_TICKET" in start,
         "and holds the poll off, so the board is not redrawn over it")

    exit_ = meth("Card", "exit_edit")
    c.ok("if self.is_new" in exit_ and "deleteLater" in exit_,
         "closing one throws the placeholder away, since nothing is behind it")

    save = meth("Card", "save")
    c.ok("create_ticket" in save,
         "and saving asks for a thread rather than editing a card")
    return c.report()


def check_a_scoped_container_states_its_tooltip() -> bool:
    """
    A widget with a stylesheet of its own owns its tooltips too.

    `apply_theme` states QToolTip on the application, which is enough for a
    widget carrying no sheet. It is not enough for one that has its own: Qt
    resolves a tooltip against the nearest stylesheet in the widget's chain,
    so a container that names only itself leaves its rows' tooltips to
    whatever the platform draws -- dark, on a dark desktop, against the ink a
    light board asks for. Reported from the running order, where the rows are
    the most hovered thing on the board.
    """
    c = Check("a container with its own stylesheet states its tooltip")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    c.ok("def tip_css(" in src, "the rule is written in one place")
    c.ok(src.count("tip_css()") >= 4,
         "and used, rather than being written out again at each site")

    tree = ast.parse(src)
    for cls_name in ("Rail", "RailRow", "Stats"):
        cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                    and n.name == cls_name), None)
        body = ast.get_source_segment(src, cls) or "" if cls else ""
        own = [ln for ln in body.splitlines() if "setStyleSheet" in ln]
        c.ok(own, f"{cls_name} carries a stylesheet of its own")
        # The call can span lines, so look at the statement it starts.
        block = body.split("setStyleSheet", 1)[1][:400] if own else ""
        c.ok("tip_css()" in block,
             f"{cls_name} states the tooltip rule with it")

    return c.report()



def check_a_card_stands_off_the_board_it_sits_on() -> bool:
    """
    Brightness separates a card from its board; colour only says which tag.

    Light mode was reported as overwhelming and the fills blamed, and the
    fills were not the fault: measured, they carried less chroma than dark's
    already. What light had was no brightness separation at all -- the
    weakest card sat at 1.01 against the well it is drawn in, so nothing but
    hue said where a card began, and hue was left carrying the whole
    structural load. Both themes now clear CARD_MIN, and the floor is a
    *ratio* rather than a difference in levels, because two near-blacks and
    two near-whites are not comparable any other way.
    """
    c = Check("a card stands off the board it sits on")

    # Below this a card reads as a tint of the board rather than a thing on
    # it. Dark's weakest is its unassigned card at 1.12 -- the one with no
    # colour of its own, which leans on its red outline -- and light's is
    # 1.17. The floor is under both, so it catches a palette going flat
    # rather than policing taste.
    CARD_MIN = 1.10

    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        # The canvas, because that is what a card is actually drawn on:
        # `#boardColumn` takes T.CANVAS and `#bandPanel` inside it is
        # transparent. The well is the floor *under* the column and is only
        # visible beside it -- measuring against that flattered both themes
        # by a step neither card ever sits next to. Read off a screenshot to
        # settle it: every pixel between two cards is the canvas.
        well = palette["canvas"]
        # Every fill a card can wear, taken the way card_skin takes them: the
        # tag's tint for an ordinary card, neutral for one with no tag, and
        # band_card's unassigned for one in Needs Attention. Checking only
        # band_card would have measured the one fill most cards never get.
        fills = {f"{q} tag": v[1] for q, v in palette["queue"].items()}
        fills["no tag"] = palette["neutral"][1]
        fills.update({f"{b} card": v[0]
                      for b, v in palette["band_card"].items()})
        for band, fill in fills.items():
            c.ok(contrast(fill, well) >= CARD_MIN,
                 f"{name}: a {band} card stands off the well "
                 f"({contrast(fill, well):.2f} >= {CARD_MIN})")
            # And it stands off it the same way in both themes. A card
            # *darker* than its board in one of them is the board reading
            # inside out, whatever the ratio says.
            c.ok(lum(fill) > lum(well),
                 f"{name}: a {band} card is brighter than the well, as in "
                 f"the other theme")

    return c.report()


def check_a_card_is_edged_in_its_own_tag() -> bool:
    """
    The border has to stand off the fill it encloses, or it is decoration.

    This is the half that was missing when light was reported as overwhelming.
    Its fills separated from the board about as well as dark's -- 1.19 against
    1.18-1.26 -- but its borders stood at 1.6-2.3 over their own fill where
    dark's stand at 5-7. The same stripe hex was being used on both, and a
    colour picked to blaze on a near-black card is a pastel on a near-white
    one. So light's stripes go down in lightness and **not** in saturation:
    same hue, same cast, dark enough to hold an edge.

    With the border carrying the tag, the fill no longer has to, which is what
    let the fills come down to a whisper.
    """
    c = Check("a card is edged in its own tag")

    # Dark's weakest is 4.96 and light's 4.44. Under this the edge stops
    # reading as a border and the card loses its outline.
    EDGE_MIN = 3.5

    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        for q, (stripe, fill, _) in palette["queue"].items():
            got = contrast(stripe, fill)
            c.ok(got >= EDGE_MIN, f"{name}: the {q} stripe stands off a {q} "
                                  f"card ({got:.2f} >= {EDGE_MIN})")

    # And it is the same colour in both themes, which is the rule that stops
    # "deeper for light" turning into "a different colour for light". Hue
    # only: the lightness is exactly what differs, on purpose.
    for q in bert.LIGHT["queue"]:
        a = hue_of(bert.LIGHT["queue"][q][0])
        b = hue_of(bert.DARK["queue"][q][0])
        gap = min(abs(a - b), 360 - abs(a - b))
        c.ok(gap <= 12, f"{q} is the same colour in both themes "
                        f"({a:.0f}deg and {b:.0f}deg)")

    return c.report()


def check_a_band_header_is_accented_not_filled() -> bool:
    """
    The band's colour belongs on its bar and its heading, not across its width.

    A header washed in the band's colour put a second colour behind every
    ticket in the run -- the thing `band_tint` was already cut back to the
    header alone for. It was still a fill: chroma 24 at the top of the light
    ramp, under cards whose own fills had come down to single figures, so the
    heading strip was the most coloured thing on the board and the tickets
    read as sitting inside it. The colour is in the 4px bar and the ink now.
    """
    c = Check("a band header is accented, not filled")

    # Measured against the theme's own cards rather than a flat number:
    # 17 levels of chroma on a near-black strip is not the same amount of
    # colour as 17 on a near-white one, so a shared cap would either let light
    # shout or call dark a failure for a tint nobody can see. The invariant
    # that holds in both is the one that was actually broken -- the strip
    # behind a run of cards was more coloured than the cards on it.
    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        loudest = max(hue_spread(v[1]) for v in palette["queue"].values())
        for band, tint in palette["band_tint"].items():
            got = hue_spread(tint)
            c.ok(got <= loudest,
                 f"{name}: the {band} header carries less colour than the "
                 f"cards under it ({got} <= {loudest})")
            # The heading's own ink is where the colour goes instead, so it
            # has to be the loud one.
            c.ok(hue_spread(palette["band_text"][band]) >= got
                 or hue_spread(palette["band_text"][band]) == 0,
                 f"{name}: and the {band} heading's ink carries more of the "
                 f"colour than the strip behind it")

    # The bar itself, read off the source: a header that stopped drawing one
    # would leave the band with no colour at all and no check would notice.
    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    band = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "Band")
    body = ast.get_source_segment(src, band) or ""
    head = body.split("#bandHeader")[1].split("}}")[0]
    c.ok("border-left" in head, "the header draws a bar down its left")
    c.ok("accent" in head, "in the band's own colour")

    return c.report()


def check_the_ink_follows_the_ground() -> bool:
    """
    Move the background and the text on it has to move too.

    Deepening light mode's neutrals to separate the cards took `muted` from
    4.7:1 to 4.2:1 on the canvas without touching it -- the readability cost
    of a change made somewhere else entirely, and invisible unless it is
    measured. 4.5:1 is the ordinary-text line.
    """
    c = Check("the ink follows the ground")

    TEXT_MIN = 4.5

    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        for ink in ("ink", "muted"):
            for ground in ("surface", "canvas", "well", "beside", "chip_bg"):
                got = contrast(palette[ink], palette[ground])
                c.ok(got >= TEXT_MIN,
                     f"{name}: {ink} on {ground} is {got:.1f}:1")
        # Ink on every fill a card can wear, which is the text people
        # actually read -- the tag tints included, not just band_card.
        fills = {f"{q} tag": v[1] for q, v in palette["queue"].items()}
        fills["no tag"] = palette["neutral"][1]
        fills.update({f"{b} card": v[0]
                      for b, v in palette["band_card"].items()})
        for band, fill in fills.items():
            got = contrast(palette["ink"], fill)
            c.ok(got >= TEXT_MIN,
                 f"{name}: ink on a {band} card is {got:.1f}:1")

    return c.report()



def check_a_filter_says_how_many_it_holds() -> bool:
    """
    `OPS (5)`, counted over the board rather than over what is on screen.

    Asked for after Julian read the toolbar: the checkboxes said which tags
    exist and nothing about how much was behind each. The count has to be the
    whole board's, and the two ways of getting that wrong are the reason this
    is a function rather than a comprehension inside `render()` -- counted
    after the filtering, unchecking PROD would change the number beside OPS,
    and with a search on it would answer "how many did you find", which the
    board is already showing.
    """
    c = Check("a filter says how many tickets it holds")

    board = [{"queue": "PROD"}, {"queue": "PROD"}, {"queue": "OPS"},
             {"queue": "ENG"}, {"queue": None}, {"queue": "DATA"},
             {"queue": ""}]
    got = bert.queue_counts(board)

    c.equal(got.get("PROD"), 2, "two PROD tickets are counted as two")
    c.equal(got.get("OPS"), 1, "and one OPS as one")
    c.equal(got.get("CS"), 0, "a tag with nothing on it says nothing, not blank")
    c.equal(set(got), set(bert.T.QUEUE),
            "every offered tag gets a number and no others do")
    # A retired queue parses and is never offered, so it has no box to put a
    # number on -- and must not raise on the way past.
    c.ok("DATA" not in got, "a retired queue is not given a heading of its own")

    # The whole board, whatever is on screen. This is the assertion the
    # feature is: the same cards filtered down must count the same.
    c.equal(bert.queue_counts([x for x in board if x["queue"] == "OPS"]),
            {"PROD": 0, "OPS": 1, "ENG": 0, "CS": 0},
            "counting a narrowed list is a different answer -- which is why "
            "render() must hand it self.cards and not the filtered ones")

    c.equal(bert.queue_label("OPS", 5), "OPS (5)", "the label carries it")
    c.equal(bert.queue_label("OPS", 0), "OPS (0)",
            "including nought, which is a fact rather than a gap")
    c.equal(bert.queue_label("OPS", None), "OPS",
            "and says nothing at all before the board has loaded, rather "
            "than claiming zero")

    # render() has to pass the unfiltered list, which is the half a pure
    # function cannot defend on its own.
    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    render = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "render")
    body = ast.get_source_segment(src, render) or ""
    c.ok("queue_counts(self.cards)" in body,
         "render counts the board it holds, not the list it is about to draw")
    head, _, tail = body.partition("queue_counts")
    c.ok("shown =" not in head,
         "and does it before the filtering, so the two cannot be confused")

    return c.report()



def check_a_status_gives_up_its_noun_not_its_state() -> bool:
    """
    The toolbar shortens rather than being cut, and cuts the right end.

    At the window's own minimum width the bar asked for about 45px more than
    it had, and Qt spent the difference on whatever could be squeezed:
    `Search client, equip` in the box and `shared board · up to da` beside
    it. The second is the one that matters -- a status cut off exactly where
    it starts saying something, keeping only the words that are the same
    every time. Eliding would have taken the same half.

    So the words that go are the noun. `shared board` is established the
    first time anybody reads it; `up to date`, `no contact`, `3 to send` is
    the part being looked at. Both labels carry the full sentence in a
    tooltip either way.
    """
    c = Check("a status gives up its noun, not its state")

    for full, short in (
            (f"shared board {DOT} up to date", f"shared {DOT} up to date"),
            (f"shared board {DOT} no contact yet", f"shared {DOT} no contact yet"),
            (f"shared board {DOT} 3 to send", f"shared {DOT} 3 to send"),
            ("synced 20s ago", "20s ago"),
            ("synced just now", "just now")):
        forms = bert.status_forms(full)
        c.equal(forms[0], full, f"{full!r} is written in full when it fits")
        c.ok(len(forms) > 1, f"{full!r} has something to give up")
        if len(forms) > 1:
            c.equal(forms[-1], short, f"and shortens to {short!r}")
            c.ok(full.split(DOT)[-1].strip() in forms[-1]
                 or full.split()[-1] in forms[-1],
                 f"keeping the reading, not the noun")

    # A label with no noun to drop is left alone rather than mangled.
    for odd in ("never synced", "", f"shared board{DOT}no space"):
        forms = bert.status_forms(odd)
        c.equal(forms[0], odd, f"{odd!r} is handed back unchanged")
        c.ok(all(isinstance(f, str) for f in forms),
             f"{odd!r} gives strings rather than raising")

    # The hints are longest-first, or the fitting loop takes the first that
    # fits and that is the shortest one.
    lens = [len(h) for h in bert.SEARCH_HINTS]
    c.equal(lens, sorted(lens, reverse=True),
            "the search hints are longest first, which the loop relies on")
    c.ok(len(bert.SEARCH_HINTS[-1]) < 12,
         "and the last is short enough to fit any box worth typing in")

    # The fit runs on resize directly, not through the board's redraw timer:
    # the board is thirty widgets and waits for a drag to settle, and leaving
    # the bar cut for the length of that drag is the thing being fixed.
    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    rs = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
              and n.name == "resizeEvent")
    c.ok("_fit_toolbar()" in (ast.get_source_segment(src, rs) or ""),
         "a resize refits the toolbar")

    # The loop has to walk every form. Checks never make a QApplication, so
    # the geometry this produces is measured by hand rather than here -- but
    # a loop that stops after the first candidate shortens nothing, and that
    # is the regression this catches. Put back, it goes red.
    fit_src = ast.get_source_segment(src, next(
        n for n in cls.body if isinstance(n, ast.FunctionDef)
        and n.name == "_fit_toolbar")) or ""
    c.ok("range(max(len(fresh), len(shared)))" in fit_src,
         "the fit tries every form, not just the longest")
    c.ok("totalMinimumSize" in fit_src,
         "and stops at the first that the layout says it can hold")

    # And the labels are only ever set through the two setters, so nothing
    # can write a status the fitting does not know the full text of.
    fit = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name == "_fit_toolbar")
    inside = ast.get_source_segment(src, fit) or ""
    body = src.split("class Bert", 1)[1]
    for lab in ("self.fresh.setText", "self.shared.setText"):
        c.equal(body.count(lab), inside.count(lab),
                f"{lab} happens only where the shortening decides it")

    return c.report()



def check_the_board_is_centred_and_keeps_its_scrollbar() -> bool:
    """
    The column floats in the middle of its pane, bar and all.

    It used to be pinned left, so folding the running order gave the board
    nothing: the column slid across to where the rail had been and left the
    same width of floor on the other side. Centred, folding either side opens
    the space around it evenly and the tickets stay where the eye already is.

    Two ways of centring it, and only one of them takes the scrollbar along.
    Centre the *column* inside a full-width scroll area and the area keeps its
    bar at the pane's own edge -- measured with the rail folded, the column
    sat 116px in from the left and its bar sat 160px out to the right of it,
    hard against the figures panel and reading as though it belonged to them.
    Capping the *scroll area* instead brings the bar to the cap with it, which
    is the rule `feed_scroll` already follows for exactly this.
    """
    c = Check("the board is centred and keeps its scrollbar")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    body = "\n".join(ast.get_source_segment(src, f) or "" for f in cls.body
                     if isinstance(f, ast.FunctionDef))

    c.ok("addWidget(self.board_holder)" in body,
         "the splitter takes the holder, so the pane is the full width and "
         "the floor fills it")
    c.ok("addWidget(self.scroll)" not in body,
         "and not the scroll area, which would put its bar at the pane edge")
    c.ok("hold.addWidget(self.scroll, 1)" in body,
         "the area is given a stretch factor")
    # An alignment flag is the trap: a scroll area added with one takes its
    # own sizeHint, which is small, cap or no cap. Same finding as the feed's.
    # Read off the call itself rather than the file's text -- the comment
    # above that line in bert.py says the word "alignment", and a text search
    # found its own warning and called it the bug.
    adds = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "addWidget"
            and any(isinstance(a, ast.Attribute) and a.attr == "scroll"
                    for a in n.args)]
    c.ok(adds, "the scroll area is added to a layout")
    c.ok(all(not a.keywords and len(a.args) == 2 for a in adds),
         "by factor rather than by an alignment flag, which would make it "
         "take its own sizeHint instead")
    # The spacers are widths somebody worked out, not stretches that split
    # what is left. Even only centres the column while the two side panels
    # match: drag the running order wide and fold the figures down and the
    # pane itself is off-centre, so an evenly split pane puts the tickets
    # right of the middle of the window -- which is what a person looking at
    # the screen sees.
    c.ok("self.board_pad_l" in body and "self.board_pad_r" in body,
         "the column sits between two spacers of its own")
    c.ok("hold.addStretch" not in body,
         "and not between two stretches, which can only ever split the pane "
         "evenly and so centre it in the wrong frame")
    pads = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "addWidget"
            and any(isinstance(a, ast.Attribute)
                    and a.attr in ("board_pad_l", "board_pad_r")
                    for a in n.args)]
    c.equal(len(pads), 2, "both spacers are in the layout")
    c.ok(all(len(a.args) == 1 and not a.keywords for a in pads),
         "carrying no stretch factor -- with one each they split the pane "
         "three ways with the area and it never reached its cap, measured as "
         "the column stuck at its 463px minimum on a 1500px window")

    # The cap has to allow for the bar, or the column loses that much width
    # the moment the board is long enough to scroll.
    cap = body.split("self.scroll.setMaximumWidth(", 1)[-1].split(")")[0]
    c.ok("verticalScrollBar" in cap,
         "the area's cap allows for its own scrollbar")
    c.ok("BOARD_MAX" in cap, "on top of the column's own width")

    return c.report()



def all_card_fills(palette):
    """Every fill a card can wear, the way card_skin reaches for them."""
    fills = {f"{q} tag": v[1] for q, v in palette["queue"].items()}
    fills["no tag"] = palette["neutral"][1]
    fills.update({f"{b} card": v[0] for b, v in palette["band_card"].items()})
    return fills


def check_a_control_sits_under_the_card_it_is_on() -> bool:
    """
    A button, a field and a work-item bubble are all drawn a step *under* the
    card they sit on -- in both themes, which is what makes it one rule.

    They used `surface`, which was right while a card was a tint and the
    surface was the near-white above it. Light's ramp then put the cards *at*
    the surface level, and the three of them quietly became invisible:
    measured against every card fill, `surface` is 1.00-1.01 in light. A
    bubble with nothing but its border, and a search box that is a rectangle
    of hairline -- and no check said anything, because every text pairing was
    still fine. It is the fill against its *ground* that had gone.

    `beside` was the obvious token and is the wrong one: in dark it is lighter
    than some card fills and darker than others, 1.02-1.07, so a control
    would appear and disappear depending on the ticket's tag.
    """
    c = Check("a control sits a step under the card it is on")

    # A control is told from its card by its fill or by its border, and at
    # least one of the two has to do real work. Both themes lean differently
    # and both clear this: light has 1.14 of fill and 1.73 of border, dark
    # 1.03 and 1.23 -- dark's fill alone is not enough on a triage card, and
    # its border is what makes the button a button there.
    CONTROL_MIN = 1.20

    for name, palette in (("light", bert.LIGHT), ("dark", bert.DARK)):
        ground, edge = palette["control"], palette["line"]
        for card, fill in all_card_fills(palette).items():
            got = max(contrast(ground, fill), contrast(edge, fill))
            c.ok(got >= CONTROL_MIN,
                 f"{name}: a control on a {card} is told from it "
                 f"({got:.2f} >= {CONTROL_MIN})")
            # The direction is the strict half, and it is the same in both:
            # a control is never brighter than the card it is drawn on, so
            # nothing on a card competes with the card for being the top
            # surface.
            c.ok(lum(ground) < lum(fill),
                 f"{name}: and sits under it, as it does in the other theme")

    # The three sites, off the source: a fourth one added later would want
    # the same token, and reaching for `surface` again is the mistake.
    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    for fn in ("btn_css", "field"):
        node = next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        body = ast.get_source_segment(src, node) or ""
        c.ok("T.CONTROL" in body, f"{fn}() draws on the control tone")
        c.ok("T.SURFACE" not in body,
             f"{fn}() does not reach for the surface, which is the card")

    bub = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef) and n.name == "Bubble")
    body = ast.get_source_segment(src, bub) or ""
    c.ok("T.CONTROL" in body, "an outstanding bubble does too")

    # And every *other* button, which is where this got away. The Undo button
    # carried `background:{T.SURFACE}` -- the card tone -- and sat on the
    # activity bar, which is darker than a card. A bright object on a dark
    # bar is the same glare the cards had, reported in the same breath.
    # A QPushButton rule is matched by the two lines it spans, because these
    # are composed f-strings and the property rarely shares a line with the
    # selector.
    lines = src.splitlines()
    on_surface = []
    for i, line in enumerate(lines):
        if "QPushButton" not in line:
            continue
        window = " ".join(lines[i:i + 4])
        if "background:{T.SURFACE}" in window:
            on_surface.append(i + 1)
    c.equal(on_surface, [],
            "no button is drawn on the card tone -- a control has its own, "
            "and `surface` is the thing controls sit *on*")

    return c.report()


def check_the_light_ramp_has_five_levels() -> bool:
    """
    A grey workspace with bright work surfaces, not one bright field.

    Light was reported as glaring three times. The first two answers went at
    the card fills, and the fills were never it: what was bright was *how much
    of the window* was. The sections and the workspace sat on one value with
    the cards a long way above it, so almost everything on screen was near the
    top of the range. Four levels now, and the cards are the only bright thing
    in the window.

    Dark keeps three, and the reason is measurable rather than an exception:
    the whole of its bottom end from floor to workspace is a contrast ratio of
    1.06, so a fourth step inside that is a difference nobody can see.
    """
    c = Check("the light ramp has five levels and dark the three it can hold")

    L = bert.LIGHT
    order = ["well", "feed", "panel", "canvas", "surface"]
    lums = [lum(L[k]) for k in order]
    c.equal(lums, sorted(lums),
            "light runs floor, activity bar, sections, workspace, cards -- "
            "darkest first")
    for a, b in zip(order, order[1:]):
        got = contrast(L[a], L[b])
        c.ok(got > 1.02, f"and {b} is a real step above {a} ({got:.3f})")

    # The workspace is the board column and nothing else; everything around
    # the work is a step under it.
    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    c.ok("#boardColumn {{ background:{T.CANVAS}" in src,
         "the board column is the workspace")
    for panel in ("Rail {{ background:{T.PANEL}",
                  "Stats {{ background:{T.PANEL}"):
        c.ok(panel in src, f"and {panel.split()[0]} is a section around it")
    # The activity bar goes one further down than the sections either side of
    # the board, so the history recedes when nobody is reading it.
    c.ok("#feedPanel {{ background:{T.FEED}" in src,
         "and the activity bar has a tone of its own, under those")
    c.ok(lum(L["feed"]) <= lum(L["panel"]),
         "which is below them rather than beside them")

    # The card is a step over the workspace, not a leap. Both halves matter:
    # under the floor it stops reading as a thing on a surface, and far over
    # it the board becomes a field of near-white with grey edging, which is
    # what this ramp was reported for twice.
    step = contrast(L["surface"], L["canvas"])
    c.ok(1.10 <= step <= 1.30,
         f"a card is a step over the workspace, not a jump ({step:.2f})")

    D = bert.DARK
    c.equal(D["panel"], D["canvas"],
            "dark's sections stay at its workspace level")
    c.equal(D["feed"], D["canvas"],
            "and so does its activity bar, for the same reason")
    c.ok(contrast(D["well"], D["canvas"]) < 1.10,
         f"because its floor and workspace are already only "
         f"{contrast(D['well'], D['canvas']):.3f} apart, and a step inside "
         f"that is not a difference anybody can see")

    return c.report()



def check_the_board_centres_on_the_window_not_its_pane() -> bool:
    """
    Centred against the splitter, which is the whole row, not against the
    pane the board happens to have been given.

    Reported as: drag one side more closed while the other is more open and
    the tickets stop being in the middle of the screen. They were centred in
    their pane the whole time -- and the pane is only centred while the two
    side panels are the same width. A wide running order and a folded figures
    panel puts the pane's own middle well right of the window's.

    It clamps, because the two are not always reconcilable: a rail wide
    enough and the column cannot reach the middle without going under it, and
    hard against the near edge is the best answer available.
    """
    c = Check("the board centres on the window, not on its pane")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    fn = next((n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name == "_centre_board"), None)
    c.ok(fn is not None, "there is one place that decides where it sits")
    if fn is None:
        return c.report()
    body = ast.get_source_segment(src, fn) or ""

    c.ok("self.rail_split.width()" in body,
         "the middle it aims for is the splitter's, which spans the row")
    c.ok("mapTo(self.rail_split" in body,
         "measured from where the pane actually begins in that row")
    c.ok("max(0," in body and "min(" in body,
         "and clamped, so an unreachable middle lands on the near edge "
         "rather than a negative width")

    # The part that is not obvious: a column filling its pane cannot be
    # centred, because the pane is not. It has to give the difference up.
    c.ok("fits" in body,
         "the column comes in to the widest that can sit centred here")
    c.ok("minimumSizeHint" in body,
         "with its own minimum as the floor, read off the widget rather "
         "than written down twice")
    c.ok("col > fits >= floor" in body,
         "so it narrows only when that is both needed and still a board -- "
         "under the floor it stops giving room up and sits off centre, "
         "because a board too narrow to read is the worse of the two")

    # Everything that can move either width has to ask again, or the column
    # is centred for the arrangement before last. Read each method's own
    # source rather than slicing the class text -- a name appears wherever it
    # is called as well as where it is defined, and slicing found a call.
    def method(name):
        node = next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                     and n.name == name), None)
        return (ast.get_source_segment(src, node) or "") if node else ""

    for caller, why in (("_place_sides", "placing the panes"),
                        ("resizeEvent", "a window resize")):
        c.ok("_centre_board" in method(caller), f"{why} re-centres it")

    # The handle is wired where the splitter is built rather than in a method
    # of its own, so this one is a connection instead of a call.
    wired = next((ast.get_source_segment(src, f) or "" for f in cls.body
                  if isinstance(f, ast.FunctionDef)
                  and "rail_split.splitterMoved"
                  in (ast.get_source_segment(src, f) or "")), "")
    c.ok("_centre_board" in wired,
         "a handle being dragged re-centres it")

    return c.report()



def check_no_colour_is_written_by_hand() -> bool:
    """
    Every colour comes off `T`, and until now nothing was checking.

    CLAUDE.md has said "no colour literals in Bert" for a long time and the
    rule was kept by hand, which is to say not kept: `#EAF1FA` was the hover
    on the Undo button, on the running order's fold button and on the
    figures' -- a pale blue that works in light and is a flare on a dark
    toolbar, in whichever theme nobody happened to be looking at. That is the
    exact failure the rule exists to prevent, and it sat there through three
    passes over this palette.

    Two things it must not flag. **Docstrings**, because the reasoning for a
    colour often quotes the measurement that chose it -- this file and
    `btn_css` both do. And **HTML entities**: `&#8594;` is a right arrow, and
    a looser pattern reads `#8594` as a colour.
    """
    c = Check("no colour is written by hand")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # The palettes are the one place a hex belongs.
    palette = set()
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") in ("LIGHT", "DARK") for t in n.targets):
            palette.update(range(n.lineno, n.end_lineno + 1))

    docstrings = set()
    for n in ast.walk(tree):
        body = getattr(n, "body", None)
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef)) and body:
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.update(range(first.lineno, first.end_lineno + 1))

    # Three or six digits and a boundary after, so `&#8594;` is an arrow.
    hexes = re.compile(r"#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})"
                       + chr(92) + "b")
    loose = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        if n.lineno in palette or n.lineno in docstrings:
            continue
        for hit in hexes.findall(n.value):
            loose.append(f"line {n.lineno}: {hit}")

    c.equal(loose, [],
            "every colour comes off T -- a hex typed into a stylesheet works "
            "in one theme and is silently wrong in the other")

    # And the palettes really are where they are assumed to be, or the two
    # exclusions above would quietly hide the whole file.
    c.ok(palette, "the palettes were found to exclude")
    c.ok(len(palette) < len(src.splitlines()) / 2,
         "and they are a small part of the file, not most of it")

    return c.report()



def check_a_count_agrees_with_its_verb() -> bool:
    """
    "1 need attention" sat beside the logo for as long as there was a count.

    Reported, and it is worth a check rather than a careful edit: the number
    is almost always more than one on a real board, so the singular is the
    case nobody sees until the day the board is nearly clear -- which is
    exactly the day somebody is looking at that number.
    """
    c = Check("a count agrees with its verb")

    c.equal(bert.attention_text(1), "1 needs attention", "one needs")
    c.equal(bert.attention_text(2), "2 need attention", "two need")
    c.equal(bert.attention_text(11), "11 need attention", "and eleven do")
    c.equal(bert.attention_text(0), "",
            "nought says nothing at all rather than '0 need attention'")

    c.equal(bert.a_few(1, "card", "cards"), "1 card", "one card")
    c.equal(bert.a_few(3, "card", "cards"), "3 cards", "three cards")
    c.equal(bert.a_few(0, "change", "changes"), "0 changes",
            "and nought takes the plural, as English does")

    # `card(s)` is fine in a log and grit in a sentence, so the tooltips that
    # a person actually reads should not carry it.
    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Docstrings excluded, the same way the colour-literal check excludes
    # them and for the same reason: the note explaining why a rule exists
    # quotes the thing the rule forbids.
    docs = set()
    for n in ast.walk(tree):
        body = getattr(n, "body", None)
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef)) and body:
            f = body[0]
            if (isinstance(f, ast.Expr) and isinstance(f.value, ast.Constant)
                    and isinstance(f.value.value, str)):
                docs.update(range(f.lineno, f.end_lineno + 1))
    grit = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if n.lineno in docs:
                continue
            if "(s)" in n.value or "(es)" in n.value:
                grit.append(f"line {n.lineno}: {n.value.strip()[:44]}")
    c.equal(grit, [],
            "no sentence Bert shows says card(s) -- the count is known when "
            "the sentence is built, so the word can agree with it")

    return c.report()


CHECKS = (check_nothing_freezes_a_colour,
          check_a_count_agrees_with_its_verb,
          check_no_colour_is_written_by_hand, check_palettes_agree,
          check_following_the_desktop, check_the_desktop_changing_underneath, check_each_palette_is_the_right_end,
          check_a_scoped_container_states_its_tooltip,
          check_a_ticket_wears_its_tag, check_needs_attention_is_the_alarm,
          check_triage_is_outlined_not_filled,
          check_the_other_skins, check_choosing_a_theme,
          check_the_queues_the_board_can_draw,
          check_only_the_header_is_tinted,
          check_a_band_leaves_no_stray_windows,
          check_nothing_is_unparented_while_it_is_visible,
          check_a_tooltip_is_readable,
          check_a_finished_work_item_says_so,
          check_starting_a_ticket_is_not_editing_one,
          check_a_card_stands_off_the_board_it_sits_on,
          check_a_card_is_edged_in_its_own_tag,
          check_a_control_sits_under_the_card_it_is_on,
          check_the_light_ramp_has_five_levels,
          check_a_filter_says_how_many_it_holds,
          check_a_status_gives_up_its_noun_not_its_state,
          check_the_board_is_centred_and_keeps_its_scrollbar,
          check_the_board_centres_on_the_window_not_its_pane,
          check_a_band_header_is_accented_not_filled,
          check_the_ink_follows_the_ground)

"""
One version number, in one place, that every part of this can say out loud.

Nothing identified a build before. `ernie_state.FORMAT_VERSION` is the shape
of a state-channel payload -- a different question, moving on its own
schedule -- and the version handed to FastAPI was documentation metadata that
read 0.1 for the whole life of the project, which is worse than silence
because /docs and /openapi.json both quote it.

It is load-bearing for the shape this is heading for: everybody runs the whole
stack and shares only #ernie-state, so "which build is that" is a question one
laptop has to be able to ask about another. `reconcile()` drops a payload whose
`v` does not match into report["unknown"] without a word, so two people on
different builds get two boards that quietly disagree.

The commit is part of it because the number alone cannot separate two machines
while everybody runs from source: both say the same three digits and one of
them is a week behind.
"""

import ast
import pathlib
import tempfile

from support import Board, Check, iso

import ernie_api as api
import bert
import ernie_version as V


ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRY_POINTS = ("ernie_sync.py", "ernie_api.py", "ernie_outbox.py",
                "ernie_state.py", "ernie_jira.py", "ernie_changelog.py")

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


def fake_clone(d, head, loose=None, packed=None):
    """A .git directory of a given shape, built without running git."""
    git = pathlib.Path(d) / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head, encoding="utf-8")
    if loose:
        (git / "refs" / "heads" / "main").write_text(loose, encoding="utf-8")
    if packed:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    return pathlib.Path(d)


def check_the_number_is_sayable() -> bool:
    """It exists, it is a version, and it says itself the same way twice."""
    c = Check("there is a version, and it can say itself")

    c.ok(isinstance(V.VERSION, str) and bool(V.VERSION), "VERSION is a string")
    bits = V.VERSION.split(".")
    c.equal(len(bits), 3, "three parts, so a release can be cut against it")
    c.ok(all(b.isdigit() for b in bits),
         "all digits, so two of them can be ordered later")

    # describe() is what a person reads and payload() is what a machine
    # compares. They must not drift into disagreeing about the version itself.
    c.ok(V.describe().startswith(V.VERSION), "describe() leads with the version")
    c.equal(V.payload()["version"], V.VERSION, "and payload() reports that one")
    c.equal(V.payload()["commit"], V.COMMIT, "with the same commit")

    return c.report()


def check_a_build_with_no_working_copy_still_answers() -> bool:
    """
    A frozen exe has no .git, and neither does a zip download.

    None is the ordinary answer there rather than a failure, so nothing may
    raise and describe() still has to return something a person can read: it
    is the line printed at startup and the one shown in Bert's settings.
    """
    c = Check("a build with no working copy still names itself")

    with tempfile.TemporaryDirectory() as d:
        c.ok(V._read_commit(pathlib.Path(d)) is None,
             "no .git at all reads as no commit, not an error")

    saved = V.COMMIT
    try:
        V.COMMIT = None
        c.equal(V.describe(), V.VERSION, "describe() falls back to the number")
        c.ok("None" not in V.describe(), "and does not say None out loud")
        c.equal(V.payload(),
                {"version": V.VERSION, "commit": None,
                 "min_bert": V.MIN_BERT},
                "payload() carries the gap rather than hiding it")
    finally:
        V.COMMIT = saved

    return c.report()


def check_the_commit_is_read_from_every_shape_of_clone() -> bool:
    """
    Read out of .git rather than shelled out to git, which need not be there.

    Three shapes are all ordinary and all had to be handled: a loose ref, refs
    packed away by gc or by an older clone, and a detached HEAD holding the
    sha itself.
    """
    c = Check("the commit is read from every shape of clone")

    with tempfile.TemporaryDirectory() as d:
        root = fake_clone(d, "ref: refs/heads/main" + chr(10),
                          loose=SHA + chr(10))
        c.equal(V._read_commit(root), SHA[:7], "a loose ref")

    with tempfile.TemporaryDirectory() as d:
        packed = ("# pack-refs with: peeled" + chr(10)
                  + OTHER + " refs/heads/main" + chr(10))
        root = fake_clone(d, "ref: refs/heads/main" + chr(10), packed=packed)
        c.equal(V._read_commit(root), OTHER[:7],
                "packed refs, where no file at refs/heads/main exists")

    with tempfile.TemporaryDirectory() as d:
        root = fake_clone(d, SHA + chr(10))
        c.equal(V._read_commit(root), SHA[:7], "a detached HEAD")

    # A ref resolving to nothing is a gap rather than a crash: it is what a
    # half-written .git looks like, and the version stays sayable through it.
    with tempfile.TemporaryDirectory() as d:
        root = fake_clone(d, "ref: refs/heads/main" + chr(10))
        c.ok(V._read_commit(root) is None, "a ref pointing at nothing")

    return c.report()


def check_nothing_else_names_a_version() -> bool:
    """
    One place to bump, or the numbers drift apart and both get believed.

    FastAPI's is the one that already went wrong: a literal that sat at 0.1
    while the project grew around it, quoted by /docs the whole time.
    """
    c = Check("nothing else names a version of its own")

    src = (ROOT / "ernie_api.py").read_text(encoding="utf-8")
    call = next((n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "FastAPI"), None)
    c.ok(call is not None, "the API still declares itself to FastAPI")
    if call is not None:
        kw = next((k for k in call.keywords if k.arg == "version"), None)
        c.ok(kw is not None, "with a version")
        c.ok(kw is not None and not isinstance(kw.value, ast.Constant),
             "and it is not a literal typed in a second time")
        c.ok(kw is not None and any(
            isinstance(n, ast.Name) and n.id == "ernie_version"
            for n in ast.walk(kw.value)),
            "it comes off ernie_version")

    # FORMAT_VERSION is a different question and stays one: it describes the
    # payload in #ernie-state and moves when that shape changes, not when a
    # build ships.
    state = (ROOT / "ernie_state.py").read_text(encoding="utf-8")
    fmt = next((n for n in ast.walk(ast.parse(state))
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "FORMAT_VERSION"
                        for t in n.targets)), None)
    c.ok(fmt is not None and isinstance(fmt.value, ast.Constant),
         "FORMAT_VERSION stays its own constant, not the build number")

    return c.report()


def check_every_entry_point_can_say_it() -> bool:
    """
    --version on each of them, because that is the question asked first.

    Six things here can be started, and the answer to "what are you running"
    must not depend on which one somebody happened to pick.
    """
    c = Check("every entry point answers --version")

    for name in ENTRY_POINTS:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        adds = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "add_argument"
                and n.args and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "--version"]
        c.equal(len(adds), 1, name + " takes --version")
        if adds:
            c.ok(any(isinstance(n, ast.Name) and n.id == "ernie_version"
                     for n in ast.walk(adds[0])),
                 name + " answers it from ernie_version")

    return c.report()


def check_health_says_which_build_answered() -> bool:
    """
    The only way one machine can ask another what it is running.

    Bert reads it for its settings window, and it is what a skew warning will
    have to be built on: there is no other channel for the question.
    """
    c = Check("/health says which build answered")

    with Board() as b:
        b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 respool", "high")
        b.con.execute(
            """INSERT INTO sync_runs (started_at, finished_at, threads_seen,
                                      messages_new)
               VALUES (?,?,?,?)""", (iso(-60), iso(-30), 1, 0))
        b.con.commit()
        api.DB = b.path
        h = api.health()

        c.ok("build" in h, "/health carries a build block")
        build = h.get("build") or {}
        c.equal(build.get("version"), V.VERSION, "naming this version")
        c.equal(build.get("commit"), V.COMMIT, "and this commit")
        # About the running build, not about the database it opened.
        c.ok("build" not in (h.get("last_sync") or {}),
             "reported once, at the top, not buried in the sync row")

    return c.report()


def check_a_bert_that_is_behind_is_told() -> bool:
    """
    Which build is answering, and whether this one may still write.

    It matters now because of the exe. While everybody runs from source a
    `git pull` is the update and the drift is visible; a handed-out build is
    frozen at the day it was made and there is no pull. And the failure it
    replaces is a misleading one -- `Api` calls `.json()` without checking the
    status, so an old Bert asking for a route its Ernie does not have gets
    `{"detail": "Not Found"}`, then a KeyError, then a failed poll, which
    surfaces as **"Can't reach Ernie."** A stale Bert looked like a dead
    server.

    **Two thresholds, not one.** `MIN_BERT` is the floor a release raises by
    hand when a build genuinely cannot be trusted; `VERSION` is merely the
    newest. Refusing anything that is not exactly `VERSION` was the obvious
    rule and is the wrong one here -- people run from source, `describe()`
    carries the commit, and every push would lock the other laptop out of a
    board it can read perfectly well.
    """
    c = Check("a Bert that is behind is told")

    def blocked_said():
        return bert.build_standing("0.9.0", "0.9.3", "0.9.2")[1]

    ok = bert.build_standing("0.9.3", "0.9.3", "0.9.0")
    c.equal(ok[0], "ok", "the same build as Ernie is fine")
    c.equal(ok[1], "", "and says nothing")

    behind = bert.build_standing("0.9.2", "0.9.3", "0.9.0")
    c.equal(behind[0], "behind", "an older one that clears the floor is behind")
    c.ok("0.9.2" in behind[1] and "0.9.3" in behind[1],
         f"and names both numbers, because 'there is an update' is not "
         f"actionable ({behind[1]!r})")
    # **It has to read as the good news it is.** "Bert found a new update"
    # over a board that is working perfectly reads as an alarm, and an alarm
    # that turns out to be nothing is how somebody learns to click through
    # the next one without reading it -- and the next one is the blocked one.
    c.ok("fine" in behind[1] and "paused" in behind[1],
         "and says the build is fine and nothing is paused")
    c.ok("paused until" in blocked_said(), "where the blocked one does not")

    blocked = bert.build_standing("0.9.0", "0.9.3", "0.9.2")
    c.equal(blocked[0], "blocked", "one under the floor is blocked")
    c.ok("0.9.2" in blocked[1], "and says which build it needs")

    c.equal(bert.build_standing("0.9.9", "0.9.3", "0.9.0")[0], "ok",
            "a Bert ahead of Ernie is not 'behind'")

    # **It fails open at every step.** A build check has no business taking a
    # working board away over a field an older Ernie does not send.
    c.equal(bert.build_standing("0.9.0", None, None)[0], "ok",
            "an Ernie that names no version says nothing about ours")
    c.equal(bert.build_standing("0.9.0", "0.9.3", None)[0], "behind",
            "and one that names no floor can never block")
    c.equal(bert.build_standing("0.9.0", "", "")[0], "ok",
            "empty strings are not a demand")

    # The comparison itself, and the trap in it.
    c.ok(V.is_older("0.9.9", "0.10.0"),
         "0.9.9 is older than 0.10.0 -- compared as numbers, not as text")
    c.ok(not V.is_older("0.9", "0.9.0"),
         "0.9 and 0.9.0 are the same build, not one behind the other")
    for bad in ("", "weird", None):
        V.is_older(bad, "0.9.0")        # must not raise
    c.ok(True, "an unreadable version is compared rather than thrown")

    return c.report()


def check_the_floor_cannot_lock_everybody_out() -> bool:
    """
    `MIN_BERT` above `VERSION` would block every build there is, including
    the newest one anybody could possibly install -- and it would do it from
    the server, to every board at once, with the only fix a build that does
    not exist yet. It is one line to get wrong in a release and there is no
    recovering from it in the field.
    """
    c = Check("the floor cannot lock everybody out")

    c.ok(not V.is_older(V.VERSION, V.MIN_BERT),
         f"MIN_BERT {V.MIN_BERT} is not ahead of VERSION {V.VERSION}")
    c.ok(V.payload().get("min_bert") == V.MIN_BERT,
         "and /health publishes it, or Bert has nothing to compare against")

    return c.report()


def check_a_frozen_build_can_still_name_its_commit() -> bool:
    """
    There is no `.git` inside an exe, so the sha has to be written in.

    Read *before* the git walk rather than after: a bundle carrying a stamp
    is not a clone, and there is nothing underneath it worth preferring.
    """
    c = Check("a frozen build can still name its commit")

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "build_commit.txt").write_text("deadbee1234\n")
        c.equal(V._read_commit(root), "deadbee",
                "the stamp is read, and cut to seven like every other route")

        # And it wins over a working copy, which is the ordering that matters.
        git = root / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "refs" / "heads" / "main").write_text("0000000aaaa\n")
        c.equal(V._read_commit(root), "deadbee",
                "and a stamp beats a .git that happens to be beside it")

    return c.report()


def check_only_the_harmless_one_can_be_silenced() -> bool:
    """
    "Don't tell me about this one again", and only about *this one*.

    Keyed on the version of Ernie somebody was told about, not on their own:
    "stop mentioning 0.9.9" should stop mentioning 0.9.9 and should speak up
    again when 0.9.10 turns up. Muting on Bert's own version would silence
    every future release at once, which is the same as not having the check.

    **A blocked board has no checkbox at all.** The dialog is not what is
    stopping anybody -- the floor is -- so offering to hide it would promise
    something it cannot do, and the board would stay read-only with nothing
    on screen saying why.
    """
    c = Check("only the harmless one can be silenced")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def body(name, cls=None):
        scope = tree
        if cls:
            scope = next((x for x in ast.walk(tree)
                          if isinstance(x, ast.ClassDef) and x.name == cls), None)
            if scope is None:
                return ""
        n = next((x for x in ast.walk(scope)
                  if isinstance(x, ast.FunctionDef) and x.name == name), None)
        return (ast.get_source_segment(src, n) or "") if n else ""

    made = body("__init__", "UpdateDialog")
    c.ok('if state == "behind"' in made and "QCheckBox" in made,
         "the checkbox exists only for the state that is not a problem")
    c.ok("indicator" in made,
         "and its indicator is stated, or Fusion draws a label with no box")

    wiring = body("_check_build")
    c.ok("update_muted" in wiring, "the answer is remembered")
    c.ok('build.get("version")' in wiring or "theirs" in wiring,
         "against Ernie's version, so a newer one speaks up again")
    c.ok('self.update_state == "behind"' in wiring
         and wiring.index('self.update_state == "behind"')
         < wiring.index("update_muted") + 200,
         "and the mute is only consulted for that state")
    c.ok("save_settings" in wiring, "and it survives a restart")

    return c.report()


def check_the_button_exists_only_when_there_is_somewhere_to_go() -> bool:
    """
    "Get the new build", and only when there is a build to get.

    A button called *Update now* that opens a browser is the unsent mark
    saying "pushing" over something that was never going to be pushed -- a
    control has to do what it says. This one opens a page and is named for
    that, and self-updating a running exe is a different project: Windows
    locks the binary, so it wants a helper or an installer, and it runs into
    the code-signing question rather than around it.

    **Ernie publishes the address; Bert does not hold one.** So moving from
    GitHub to Bitbucket or anywhere else is one line in an env file and a
    restart of Ernie -- nobody holding a built Bert has to be sent a new one.
    With the key unset there is no address, and with no address there is no
    button.

    **And the scheme is checked at both ends.** Bert is what hands the thing
    to the desktop; a value nobody validated on the way in is a value
    somebody trusts on the way out.
    """
    c = Check("the button exists only when there is somewhere to go")

    for raw, want in (("https://example.test/x", "https://example.test/x"),
                      ("http://example.test/x", "http://example.test/x"),
                      ("file:///C:/windows", ""),
                      ("javascript:alert(1)", ""),
                      ("ftp://example.test/x", ""),
                      ("  ", ""), ("", ""), (None, "")):
        c.equal(api.clean_url(raw), want, f"clean_url({raw!r})")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    made = next((x for x in ast.walk(tree)
                 if isinstance(x, ast.ClassDef) and x.name == "UpdateDialog"),
                None)
    body = ast.get_source_segment(src, made) or "" if made else ""
    c.ok("if url:" in body, "no address, no button")

    # The *labels*, off the AST, not the source text. Asked as "Update now"
    # is not in the file, this failed on the comment explaining why it must
    # not be -- the same trap the colour-literal check hit with docstrings
    # quoting the hexes they rejected.
    labels = [a.value for n in ast.walk(made)
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", "") == "QPushButton"
              for a in n.args if isinstance(a, ast.Constant)
              and isinstance(a.value, str)]
    c.ok("Get the new build" in labels,
         f"and it says what it does ({labels})")
    c.ok(not any("update now" in x.lower() for x in labels),
         "rather than promising an update it does not perform")

    wiring = next((x for x in ast.walk(tree)
                   if isinstance(x, ast.FunctionDef) and x.name == "_check_build"),
                  None)
    w = ast.get_source_segment(src, wiring) or "" if wiring else ""
    c.ok("urlparse" in w and "http" in w,
         "and Bert checks the scheme itself before opening anything")

    # The API has to be told where to read it from, or the key is dead.
    apisrc = (ROOT / "ernie_api.py").read_text(encoding="utf-8")
    c.ok("BERT_UPDATE_URL" in apisrc, "the API reads the key")
    c.ok('"--env"' in apisrc, "off the same --env every other entry point takes")
    c.ok("--env" in (ROOT / "run.sh").read_text(encoding="utf-8"),
         "and run.sh passes one to it")
    c.ok("BERT_UPDATE_URL" in (ROOT / "ernie-test.env.example").read_text(
        encoding="utf-8"), "with the key documented in the env example")

    return c.report()


CHECKS = (check_the_button_exists_only_when_there_is_somewhere_to_go,
          check_only_the_harmless_one_can_be_silenced,
          check_a_bert_that_is_behind_is_told,
          check_the_floor_cannot_lock_everybody_out,
          check_a_frozen_build_can_still_name_its_commit,
          check_the_number_is_sayable,
          check_a_build_with_no_working_copy_still_answers,
          check_the_commit_is_read_from_every_shape_of_clone,
          check_nothing_else_names_a_version,
          check_every_entry_point_can_say_it,
          check_health_says_which_build_answered)

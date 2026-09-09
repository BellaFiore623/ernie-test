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
        c.equal(V.payload(), {"version": V.VERSION, "commit": None},
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


CHECKS = (check_the_number_is_sayable,
          check_a_build_with_no_working_copy_still_answers,
          check_the_commit_is_read_from_every_shape_of_clone,
          check_nothing_else_names_a_version,
          check_every_entry_point_can_say_it,
          check_health_says_which_build_answered)

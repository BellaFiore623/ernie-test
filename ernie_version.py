"""The one version number, and which build is answering.

Nothing identified a build before this. `ernie_state.FORMAT_VERSION` describes
the shape of a state-channel payload, which is a different question moving for
different reasons and on its own schedule, and `FastAPI(version=...)` was
documentation metadata that read 0.1 for the whole life of the project.

It matters because of the shape this is heading for. Everybody runs the whole
stack and shares only `#ernie-state`, so "which build is that" is a question
one laptop has to be able to ask about another -- and the failure it guards is
a silent one: `reconcile()` drops a payload whose `v` does not match into
`report["unknown"]`, so two people on different builds get two boards that
quietly disagree and nothing on either says so.

The commit is here because the number alone cannot tell two machines apart
while everybody runs from source: both say the same three digits and one of
them is a week behind. It is read straight out of `.git` rather than shelled
out to `git`, which need not be installed and is absent from a zip download --
and from a frozen build, where there is no working copy at all and the stamp
will have to be written in at build time instead.
"""
from __future__ import annotations

import pathlib

# The number a person reads and a release is cut against. 1.0 is the one the
# README and TESTING guides get rewritten for, so the nines are the road to
# it and the packaging work is 0.9.x. Bump it here and nowhere else: every
# other place that names a version imports this one.
VERSION = "0.9.0"

# The oldest Bert this Ernie will work with. **Raised by hand, on a release
# that actually breaks something** -- not automatically, and not to match
# VERSION. Everything between this and VERSION is a Bert that is behind and
# still perfectly able to work, which is most of them: routes are added far
# more often than they change shape, and Bert already survives a missing
# `/stats` or `/clients/roster` without failing the poll the board depends
# on.
#
# The alternative was refusing anything that is not exactly VERSION, and it
# is the wrong rule for this project: `describe()` carries the commit, people
# run from source, and every push would lock the other laptop out of a board
# it can read perfectly well. This way "you cannot use Bert unless it is up
# to date" is true when somebody decides it needs to be.
MIN_BERT = "0.9.0"


def _read_commit(root: pathlib.Path | None = None) -> str | None:
    """The short commit of the working copy this file sits in.

    None for anything that is not one -- a zip download, or a frozen build --
    which is an ordinary answer rather than a failure, so every step is
    allowed to come up empty without raising.

    `root` is the directory to look in, and exists so the checks can point it
    at a .git they built themselves: the one this file lives in has whatever
    shape this clone happens to have, which is not the shape worth testing.
    """
    here = (root or pathlib.Path(__file__).resolve().parent)

    # A build written by the packager, for the case this function cannot
    # answer at all: a frozen exe has no working copy, so `.git` is not there
    # to read and the commit would be None for ever. The build step writes
    # the sha into this file and it is carried along with the rest of the
    # bundle -- one line, checked before the git walk rather than after,
    # because a bundle that has one is not a clone and there is nothing under
    # it worth preferring.
    stamp = here / "build_commit.txt"
    try:
        if stamp.is_file():
            return stamp.read_text(encoding="utf-8").strip()[:7] or None
    except OSError:
        pass

    git = here / ".git"
    try:
        if git.is_file():
            # A worktree or a submodule: .git is a file holding "gitdir: ...",
            # and the path in it may be relative to the file's own directory.
            where = git.read_text(encoding="utf-8").partition(":")[2].strip()
            if not where:
                return None
            git = pathlib.Path(where)
            if not git.is_absolute():
                git = (here / git).resolve()

        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:7] or None         # detached: HEAD is the sha itself

        ref = head.partition(":")[2].strip()
        loose = git / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()[:7] or None

        # Packed instead, which is how an older or garbage-collected clone
        # keeps its refs -- there is no file at refs/heads/<branch> at all.
        for line in (git / "packed-refs").read_text(
                encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha[:7] or None
    except OSError:
        pass
    return None


# Read once, at import. A process is the build it started as: rebasing under a
# running Ernie does not change the code it is executing, so re-reading would
# make it claim to be something it is not. It also keeps /health, which Bert
# polls every five seconds, off the disk.
COMMIT = _read_commit()


def describe() -> str:
    """The build, as a person should read it: `0.9.0` or `0.9.0 (a1b2c3d)`."""
    return f"{VERSION} ({COMMIT})" if COMMIT else VERSION


def as_tuple(v) -> tuple:
    """A version as numbers, for comparing. Unreadable parts sort as 0.

    Nothing here may raise. A build check that throws on a version it cannot
    parse would take the board down over the *label* on a build, which is the
    one thing this is not important enough to do.
    """
    out = []
    for part in str(v or "").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_older(a, b) -> bool:
    """Whether version `a` is behind version `b`.

    Padded to the same length, so 0.9 and 0.9.0 are the same build rather
    than one of them being behind.
    """
    x, y = as_tuple(a), as_tuple(b)
    n = max(len(x), len(y))
    return x + (0,) * (n - len(x)) < y + (0,) * (n - len(y))


def payload() -> dict:
    """The build, for something that has to compare it rather than read it.

    Kept apart from describe() on purpose: a machine asking whether another
    board is on the same build wants the fields, not a sentence it would have
    to take back apart.
    """
    return {"version": VERSION, "commit": COMMIT, "min_bert": MIN_BERT}


if __name__ == "__main__":
    print(describe())

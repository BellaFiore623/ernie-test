"""
Build the Windows application.

    python build.py                 # build
    python build.py --version 0.9.1 # bump the version and build

One command, because the two things that are easy to forget are the two that
matter afterwards: the version somebody sees in Settings, and the commit the
build can name. A frozen build has no `.git`, so `ernie_version._read_commit`
reads `build_commit.txt` beside the module instead -- and that file has to be
written by whatever does the building, which is this.

The stamp is removed again at the end. Left behind, a *source* run would read
it and claim to be whatever the last build was, which is the version of this
mistake that is hardest to notice: the number is plausible and stale.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
STAMP = HERE / "build_commit.txt"


def commit() -> str:
    """The sha this build is cut from, or a word saying there isn't one."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE,
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return out.stdout.strip()[:7]
    except (OSError, subprocess.SubprocessError):
        pass
    return "nogit"


def dirty() -> bool:
    """Whether the tree has changes the build would not be reproducible from."""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                             capture_output=True, text=True, timeout=15)
        return out.returncode == 0 and bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def set_version(new: str) -> None:
    """Bump `ernie_version.VERSION`, which is the one place it is written."""
    p = HERE / "ernie_version.py"
    s = p.read_text(encoding="utf-8")
    s2 = re.sub(r'^VERSION = "[^"]*"', f'VERSION = "{new}"', s,
                count=1, flags=re.M)
    if s2 == s:
        sys.exit("could not find VERSION in ernie_version.py")
    p.write_text(s2, encoding="utf-8", newline="\n")
    print(f"  VERSION -> {new}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", help="set VERSION before building")
    ap.add_argument("--clean", action="store_true",
                    help="throw away build/ and dist/ first")
    a = ap.parse_args()

    if a.version:
        set_version(a.version)

    sha = commit()
    if dirty():
        print(f"  note: the tree has uncommitted changes, so {sha} does not "
              f"describe this build exactly")

    STAMP.write_text(sha + "\n", encoding="utf-8", newline="\n")
    print(f"  build_commit.txt -> {sha}")

    if a.clean:
        for d in ("build", "dist"):
            shutil.rmtree(HERE / d, ignore_errors=True)
        print("  cleaned build/ and dist/")

    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "ernie.spec"],
            cwd=HERE)
    finally:
        # Always, even if the build failed: a stamp left in the tree makes a
        # source run claim to be a build that does not exist.
        STAMP.unlink(missing_ok=True)

    if r.returncode != 0:
        sys.exit(r.returncode)

    out = HERE / "dist" / "Ernie"
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"\n  built in {time.time() - t0:.0f}s")
    print(f"  {out}")
    print(f"  {size / 1048576:.0f} MB over "
          f"{sum(1 for _ in out.rglob('*') if _.is_file())} files")


if __name__ == "__main__":
    main()

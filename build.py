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
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
STAMP = HERE / "build_commit.txt"
NSI = HERE / "installer" / "ernie.nsi"
# Wherever it happens to be. NSIS is not on PATH by default and there is no
# reason to make somebody put it there for one command a release.
NSIS_CANDIDATES = (
    pathlib.Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    / "NSIS" / "makensis.exe",
    pathlib.Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    / "NSIS" / "makensis.exe",
)


def makensis() -> pathlib.Path | None:
    """Where NSIS is, or None."""
    found = shutil.which("makensis")
    if found:
        return pathlib.Path(found)
    return next((p for p in NSIS_CANDIDATES if p.exists()), None)


def version() -> str:
    """Whatever `ernie_version.VERSION` currently says.

    Read out of the file rather than imported, so `--version` earlier in the
    same run is what the installer is named after -- an import would have
    cached the old value at the top of this process.
    """
    s = (HERE / "ernie_version.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION = "([^"]*)"', s, re.M)
    return m.group(1) if m else "0.0.0"


def build_installer(v: str) -> pathlib.Path | None:
    """Wrap dist/Ernie in a single setup.exe. Answers the path, or None.

    The whole reason it exists: the build is 87 files, and "grab the new one
    and delete the old one" cannot be done with 87 files -- nor by hand at
    all, because Windows locks a running exe and the person deleting it is
    usually the person who has it open.
    """
    exe = makensis()
    if exe is None:
        print("  no NSIS found, so no installer was built")
        print("    winget install NSIS.NSIS")
        return None
    out = subprocess.run([str(exe), f"/DAppVersion={v}", str(NSI)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        # The last few lines carry the actual complaint; the rest is a banner.
        for line in out.stdout.strip().splitlines()[-12:]:
            print(f"    {line}")
        if out.stderr.strip():
            print(f"    {out.stderr.strip()}")
        print("  NSIS failed, so no installer was built")
        return None
    setup = HERE / "dist" / f"Ernie-{v}-setup.exe"
    return setup if setup.exists() else None


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
    ap.add_argument("--installer", action="store_true",
                    help="also wrap dist/Ernie in a single setup.exe")
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

    if a.installer:
        setup = build_installer(version())
        if setup:
            print(f"  {setup}")
            print(f"  {setup.stat().st_size / 1048576:.0f} MB installer")


if __name__ == "__main__":
    main()

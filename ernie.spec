# PyInstaller spec for the one-process build.
#
#     python build.py
#
# **onedir, not onefile.** A frozen PySide6 application is 150-250 MB before
# trimming, and onefile unpacks all of it to a temp directory on *every*
# launch -- so Bert would take seconds to open, every time. onedir starts at
# once, and an update can ship the files that changed rather than the lot.
#
# **What has to be carried.** `ernie_app` reaches sync, outbox, state,
# status, changelog, jira, extract, load, version and bert, and PyInstaller
# follows ordinary imports -- but three of those are imported *inside
# functions* to break circular imports, and one is reached only through
# uvicorn's own machinery. Those are the hiddenimports below: a missed one is
# not a build error, it is an ImportError the first time somebody completes a
# ticket.
#
# **The excludes are not cosmetic.** PySide6 ships Qt whole -- WebEngine,
# 3D, Charts, Multimedia, Quick -- and none of it is used here. Left in, the
# build is most of a gigabyte and every update ships it again.

import pathlib

from PyInstaller.utils.hooks import collect_submodules

HERE = pathlib.Path(SPECPATH)

# **The commit is only in the bundle if it is put there.** `build.py` writes
# the sha into build_commit.txt before calling PyInstaller and removes it
# afterwards, because a stamp left in the tree makes a *source* run claim to
# be a build -- so the file exists for exactly the length of this Analysis and
# has to be picked up here or it is written for nothing. Measured before this
# line: the first exe answered `/health` with `commit: null`.
#
# Conditional, because `pyinstaller ernie.spec` run by hand has no stamp, and
# a datas entry naming a file that is not there fails the build. No stamp is
# already a state `ernie_version` understands -- it falls through to .git and
# then to the bare number.
stamp = [("build_commit.txt", ".")] if (HERE / "build_commit.txt").exists() else []

hidden = [
    # Imported inside functions, so the walker never sees them: ernie_state
    # and ernie_jira from the sync loop, ernie_status and ernie_changelog
    # from the outbox's.
    "ernie_state", "ernie_status", "ernie_changelog", "ernie_jira",
    "ernie_extract", "ernie_load", "ernie_version", "ernie_api",
    "ernie_sync", "ernie_outbox", "bert",
    # uvicorn resolves these by name at runtime.
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
] + collect_submodules("uvicorn.loops") \
  + collect_submodules("uvicorn.protocols")

a = Analysis(
    ["ernie_app.py"],
    pathex=[str(HERE)],
    binaries=[],
    datas=[
        # The two images, and schema.sql -- which connect() applies on every
        # open, so without it a fresh machine has a database with no tables.
        ("assets/bert_logo.png", "assets"),
        ("assets/bert_update.png", "assets"),
        ("schema.sql", "."),
    ] + stamp,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Qt modules nothing here touches. Measured before and after.
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
        "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
        "PySide6.QtQml", "PySide6.QtBluetooth", "PySide6.QtNfc",
        "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtSensors",
        "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
        "PySide6.QtSql", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets",
        # And the toolchain, which a shipped build has no use for.
        "tkinter", "unittest", "pydoc", "doctest", "pytest", "PyInstaller",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Ernie",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX on a Qt build is a reliable way to be quarantined
    # **No console.** A black window opening behind Bert and sitting in the
    # taskbar for the life of the session is what a shipped application does
    # not do -- and closing it by mistake would take the sync, the outbox and
    # the board with it.
    #
    # It takes stdout and stderr with it: PyInstaller sets both to None, so
    # every print in the process becomes a no-op. `ernie_app.open_log()`
    # redirects them to %LOCALAPPDATA%\Ernie\logs\ernie.log before anything
    # writes, because the banner answers the three questions asked about
    # somebody else's machine after the fact -- which config, which database,
    # and whether it can post.
    console=False,
    # Windows takes only .ico here, and it wants several sizes in one
    # file: the taskbar, the Start menu, Explorer and Alt-Tab each
    # pick a different one, and what is missing gets scaled.
    icon=str(HERE / "assets" / "ernie.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Ernie",
)

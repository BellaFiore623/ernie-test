@echo off
rem  Ernie + Bert, the whole stack, for somebody running their own copy.
rem
rem  Double-click it. This is the two-stack setup: you have your own database
rem  and your own sync, and you share a board with the other person through
rem  the #ernie-state channel in Discord. Neither machine talks to the other.
rem
rem  If you don't have an ernie-test.env with a token in it, you want bert.cmd
rem  instead -- that one runs only Bert against somebody else's machine.
rem
rem  Sandbox only. There is no way to point this at production and no reason
rem  to want one.

setlocal
rem  %~dp0 ends in a backslash, which would escape the closing quote.
cd /d "%~dp0."

set "ENVFILE=ernie-test.env"
set "DB=ernie-test.db"
set "PORT=8788"

if not exist "%ENVFILE%" (
    echo.
    echo   There's no %ENVFILE% in this folder, so this machine can't run its
    echo   own stack. Either:
    echo.
    echo     - get %ENVFILE% from whoever set this up and put it here, or
    echo     - run bert.cmd instead, which needs no token and points at
    echo       their machine.
    echo.
    pause
    exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python isn't on PATH. Install Python 3.11 or newer, ticking
    echo   "Add python.exe to PATH", then run this again.
    pause
    exit /b 1
)

python -c "import PySide6, httpx, fastapi, uvicorn" 2>nul
if errorlevel 1 (
    echo.
    echo   Installing what this needs ^(PySide6, httpx, fastapi, uvicorn^)...
    python -m pip install --quiet PySide6 httpx fastapi uvicorn
    if errorlevel 1 (
        echo   That didn't work. Try it by hand:
        echo      python -m pip install PySide6 httpx fastapi uvicorn
        pause
        exit /b 1
    )
)

rem -- the things that are silent when they're wrong ------------------------
echo.
echo   Checking this machine...
python ernie_state.py --check --env "%ENVFILE%"
if errorlevel 1 (
    echo.
    echo   Sort that out first. Until you do, your board will look fine and
    echo   the other person will never see a thing.
    pause
    exit /b 1
)

rem -- background parts, each in its own window so errors stay readable -----
echo.
echo   Starting sync, outbox and API...
start "Ernie sync"   /min cmd /k python ernie_sync.py --env "%ENVFILE%" --db "%DB%"
start "Ernie outbox" /min cmd /k python ernie_outbox.py --env "%ENVFILE%" --db "%DB%"
start "Ernie api"    /min cmd /k python ernie_api.py --db "%DB%" --port %PORT%

set /a tries=0
:wait
python -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1',%PORT%))==0 else 1)" 2>nul
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% GEQ 40 (
    echo.
    echo   The API didn't come up. Look at the minimised "Ernie api" window
    echo   for why -- it stays open when it fails.
    pause
    exit /b 1
)
rem  ping, not timeout: timeout refuses to run at all when stdin isn't a
rem  console ("Input redirection is not supported"), which would turn this
rem  into a busy loop that gives up long before a slow machine is ready.
ping -n 2 127.0.0.1 >nul
goto wait

:ready
echo   API ready on http://127.0.0.1:%PORT%
echo.
echo   The first run takes a couple of minutes to build your database and
echo   pick up the shared board. Set your name in Settings before you change
echo   anything, or nothing will save.
echo.
python bert.py --api "http://127.0.0.1:%PORT%"

rem -- Bert closed. The rest is deliberately still running -----------------
echo.
echo   Bert has closed. Sync, outbox and API are still going, in three
echo   minimised windows -- that's on purpose: a change made in the last
echo   minute hasn't been posted to its thread yet, and the outbox needs to
echo   still be there to post it.
echo.
echo   Give it a minute, then close those three windows, or run:
echo      taskkill /FI "WINDOWTITLE eq Ernie *" /T /F
echo.
pause
endlocal

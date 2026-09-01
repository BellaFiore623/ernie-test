@echo off
rem  Bert, for somebody testing alongside whoever runs the stack.
rem
rem  Double-click it. The first run asks for the address of the machine
rem  running Ernie (they get it from `./run.sh test bert lan`) and remembers
rem  it; later runs go straight in. To point it somewhere else, either pass
rem  the address as an argument or delete %USERPROFILE%\.bert-host.
rem
rem  This starts Bert only. The database, the Discord token and the sync all
rem  live on the other machine.

setlocal
rem  %~dp0 ends in a backslash, which would escape the closing quote.
cd /d "%~dp0."
set "HOSTFILE=%USERPROFILE%\.bert-host"

rem -- an address on the command line wins, and is remembered ---------------
if not "%~1"=="" (
    set "TARGET=%~1"
    goto :have_target
)

if exist "%HOSTFILE%" (
    set /p TARGET=<"%HOSTFILE%"
)

if not defined TARGET goto :ask
if "%TARGET%"=="" goto :ask
goto :have_target

:ask
echo.
echo   Bert needs the address of the machine running Ernie.
echo   It looks like  192.168.1.20:8788  -- ask whoever started it.
echo.
set /p "TARGET=  address: "
if "%TARGET%"=="" (
    echo   Nothing entered. Run it again when you have the address.
    pause
    exit /b 1
)

:have_target
rem -- tidy what was typed or pasted: a stray space would go into the URL ---
:trim_left
if "%TARGET:~0,1%"==" " (
    set "TARGET=%TARGET:~1%"
    goto :trim_left
)
:trim_right
if "%TARGET:~-1%"==" " (
    set "TARGET=%TARGET:~0,-1%"
    goto :trim_right
)

rem -- accept a pasted http://... too, and store the bare host:port ---------
set "TARGET=%TARGET:http://=%"
set "TARGET=%TARGET:https://=%"
if "%TARGET:~-1%"=="/" set "TARGET=%TARGET:~0,-1%"
> "%HOSTFILE%" echo %TARGET%

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python isn't on PATH. Install Python 3.13, ticking
    echo   "Add python.exe to PATH", then run this again.
    pause
    exit /b 1
)

python -c "import PySide6, httpx" 2>nul
if errorlevel 1 (
    echo.
    echo   Installing what Bert needs ^(PySide6, httpx^)...
    python -m pip install --quiet PySide6 httpx
    if errorlevel 1 (
        echo   That didn't work. Try it by hand:
        echo      python -m pip install PySide6 httpx
        pause
        exit /b 1
    )
)

echo.
rem  The > must be escaped or cmd reads it as a redirect into a file.
echo   Bert  -^>  http://%TARGET%
echo   Set your name in Settings the first time, or nothing will save.
echo.
python bert.py --api "http://%TARGET%"

rem -- only stop to explain if it fell over --------------------------------
if errorlevel 1 (
    echo.
    echo   Bert exited with an error. If it says it can't reach Ernie, check
    echo   the address above and that their stack is still running.
    pause
)
endlocal

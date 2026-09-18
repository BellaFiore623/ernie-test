@echo off
rem  The installed Bert, pointed at the sandbox instead of production.
rem
rem  Double-click it. Nothing to edit, and nothing to type.
rem
rem  Why this exists rather than a shortcut: a Windows shortcut's Target does
rem  not reliably expand %LOCALAPPDATA%, and the installer writes its own
rem  shortcut with a fully literal path carrying the username of whichever
rem  machine it was built on. Editing that Target by hand gave a shortcut that
rem  would not launch at all -- no error, and nothing in the log, because the
rem  exe was never reached. A batch file expands the variable properly and can
rem  say what is wrong when something is missing.

setlocal
set "APP=%LOCALAPPDATA%\Programs\Ernie\Bert.exe"
set "CFG=%LOCALAPPDATA%\Ernie"

if not exist "%APP%" goto :no_app
if not exist "%CFG%\ernie-sandbox.env" goto :no_env

echo.
echo   Bert  -^>  the sandbox
echo   config  %CFG%\ernie-sandbox.env
echo   board   %CFG%\ernie-sandbox.db
echo.
echo   Production is the ordinary Bert shortcut. This one is separate on
echo   purpose, so switching back is picking a different icon.
echo.

rem  **Both arguments, always.** --db on its own defaults to ernie.db, which
rem  is production's mirror whatever --env says -- so --env alone would point
rem  the sandbox guild at production's board. 0.9.13 and later refuse that
rem  outright; earlier builds do not, which is why it is written out here
rem  rather than left to be remembered.
start "" "%APP%" --env ernie-sandbox.env --db ernie-sandbox.db
exit /b 0

:no_app
echo.
echo   Can't find Bert at:
echo      %APP%
echo.
echo   Install it first -- the setup.exe from the Drive folder. If it is
echo   installed somewhere else, this is the only line to change.
echo.
pause
exit /b 1

:no_env
echo.
echo   Bert is installed, but there is no sandbox config here:
echo      %CFG%\ernie-sandbox.env
echo.
echo   Ask for ernie-sandbox.env and put it in that folder. It carries a
echo   token, so it is sent by hand rather than kept in the repository --
echo   and it is a different token from production's.
echo.
echo   The folder holding it is not the folder holding Bert.exe: the config
echo   and the board live in %%LOCALAPPDATA%%\Ernie, the program in
echo   %%LOCALAPPDATA%%\Programs\Ernie.
echo.
pause
exit /b 1

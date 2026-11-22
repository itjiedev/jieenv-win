@echo off
rem Refresh service port: pick a random free port (>=8000) and save it to port.txt.
rem server.py / desktop.py read the port from port.txt at startup (fallback: 8000).
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT_FILE=port.txt"
set /a MIN_PORT=8000
set /a MAX_PORT=65535
set /a RANGE=MAX_PORT-MIN_PORT+1

echo Selecting a free port in range %MIN_PORT%-%MAX_PORT% ...

:retry
rem combine two independent random numbers to cover the full range; %% is set /a modulo
set /a "R1=%RANDOM%"
set /a "R2=%RANDOM%"
set /a "PORT=MIN_PORT+(R1*32768+R2)%%RANGE"

rem netstat check: skip this port if it is already in use (LISTENING / TIME_WAIT ...)
netstat -ano | findstr /c:":!PORT! " >nul 2>&1
if not errorlevel 1 (
    echo   port !PORT! is in use, retrying ...
    goto retry
)

rem write to port file (redirect prefix avoids trailing spaces)
>"!PORT_FILE!" echo !PORT!

echo.
echo Selected port: !PORT!
echo Saved to %PORT_FILE%. Takes effect on next start of server.py / desktop.py.
endlocal

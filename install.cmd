@echo off
setlocal
cd /d "%~dp0"

set "PY="
call :try "py -3"
if not defined PY call :try "python3"
if not defined PY call :try "python"
if not defined PY (
    >&2 echo install.cmd: Python 3 not found on PATH.
    >&2 echo Download Python 3 from https://www.python.org/downloads/ - in the installer tick "Add python.exe to PATH".
    exit /b 1
)

rem Only offer the interactive menu when stdin/stdout are real consoles;
rem unattended runs keep the previous unattended-install behaviour.
powershell -NoProfile -Command "exit ([int]([Console]::IsInputRedirected -or [Console]::IsOutputRedirected))" >nul 2>nul
if errorlevel 1 (
    %PY% scripts\install.py %*
) else (
    %PY% scripts\install.py --setup %*
)
exit /b %ERRORLEVEL%

:try
set "CAND=%~1"
%CAND% -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" >nul 2>nul
if not errorlevel 1 set "PY=%CAND%"
exit /b 0

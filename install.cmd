@echo off
setlocal
cd /d "%~dp0"

set "PY="
call :try "py -3"
if not defined PY call :try "python"
if not defined PY call :try "python3"
if not defined PY (
    >&2 echo install.cmd: Python 3 not found on PATH.
    >&2 echo Download Python 3 from https://www.python.org/downloads/ - in the installer tick "Add python.exe to PATH".
    exit /b 1
)

rem Only offer the interactive menu when stdin/stdout are real consoles;
rem unattended runs keep the previous unattended-install behaviour.
rem The probe must see this script's own handles: redirecting its stdout
rem would make IsOutputRedirected always true, so only stderr is nulled.
powershell -NoProfile -Command "exit ([int]([Console]::IsInputRedirected -or [Console]::IsOutputRedirected))" 2>nul
if errorlevel 1 (
    %PY% scripts\install.py %*
) else (
    %PY% scripts\install.py --setup %*
)
exit /b %ERRORLEVEL%

:try
rem Probe by actually running the interpreter: a `py` launcher with no
rem Python 3 installed exits with a launcher error and prints nothing to
rem stdout, and Store-alias stubs only print an advert - both fall through.
set "CAND=%~1"
set "MAJOR="
for /f "delims=" %%v in ('%CAND% -c "import sys; print(sys.version_info[0])" 2^>nul') do set "MAJOR=%%v"
if "%MAJOR%"=="3" set "PY=%CAND%"
exit /b 0

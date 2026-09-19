@echo off
setlocal
cd /d "%~dp0"
python scripts\install.py %*
exit /b %ERRORLEVEL%

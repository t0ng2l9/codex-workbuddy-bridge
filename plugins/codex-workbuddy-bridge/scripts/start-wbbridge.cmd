@echo off
rem Starts the local WorkBuddy -> Codex proxy. Usually not needed: codex-wbb.cmd
rem starts it on demand.
rem Keep this file ASCII/GBK + CRLF.
setlocal
call "%~dp0_findpy.cmd"
if not defined PYEXE (
    echo [wbb] Python not found. Install Python 3.10+ ^(tick "Add python.exe to PATH"^),
    echo       or set WBBRIDGE_PYTHON to a full python.exe path.
    pause
    exit /b 1
)
"%PYEXE%" "%~dp0..\bin\wbproxy.py" %*

@echo off
rem Watcher: keeps the proxy in sync with the plugin's enabled state.
rem Run with no arguments for a resident loop, or pass --status / --once /
rem --install-autostart / --remove-autostart.
rem Keep this file ASCII/GBK + CRLF.
setlocal
call "%~dp0_findpy.cmd"
if not defined PYEXE (
    echo [wbb] Python not found. Install Python 3.10+ ^(tick "Add python.exe to PATH"^),
    echo       or set WBBRIDGE_PYTHON to a full python.exe path.
    pause
    exit /b 1
)
if "%~1"=="" (
    echo [wbb] Watcher running. Press Ctrl+C to exit.
    "%PYEXE%" "%~dp0..\bin\watch_plugin.py"
) else (
    "%PYEXE%" "%~dp0..\bin\watch_plugin.py" %*
)

@echo off
rem Environment check: Python / cryptography / desktop app / credentials / Codex.
rem Installs nothing.
rem Keep this file ASCII/GBK + CRLF.
setlocal
call "%~dp0scripts\_findpy.cmd"
if not defined PYEXE (
    echo [wbb] Python not found. Install Python 3.10+ ^(tick "Add python.exe to PATH"^),
    echo       or set WBBRIDGE_PYTHON to a full python.exe path.
    pause
    exit /b 1
)
"%PYEXE%" "%~dp0bin\setup.py" doctor %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [wbb] Did not finish cleanly ^(exit %RC%^). Check the "!!" lines above.
)
pause
exit /b %RC%

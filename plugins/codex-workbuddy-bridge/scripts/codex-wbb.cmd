@echo off
rem Launches Codex against WorkBuddy: ensures the local proxy is up, then runs
rem Codex with the wbb profile.
rem
rem Usage is the same as codex:
rem     codex-wbb.cmd                     interactive TUI
rem     codex-wbb.cmd exec "refactor utils.py"
rem     codex-wbb.cmd -m hy3              switch model (hy3 / deepseek-v4.1-flash are free)
rem
rem Notes on why this script looks the way it does:
rem   1) Keep this file ASCII/GBK + CRLF. As UTF-8, non-ASCII text under code
rem      page 936 swallows line endings and merges the next line into a rem.
rem   2) Call curl/ping by full path under %SystemRoot%\System32, otherwise a
rem      git-bash GNU build on PATH wins and errors out. Do not use timeout.exe
rem      for waiting: it fails outright when stdin is redirected. Use ping -n 2.
rem   3) Parenthesized blocks with set: %VAR% inside a block is expanded when the
rem      block is parsed, so values set inside it are not visible until after.
rem   4) Prefer the npm global codex. Another codex earlier on PATH (e.g. one
rem      bundled with an IDE) may be incompatible with WorkBuddy; set
rem      WBBRIDGE_CODEX to force a specific one.
setlocal
call "%~dp0_findpy.cmd"
if not defined PYEXE (
    echo [wbb] Python not found. Install Python 3.10+ ^(tick "Add python.exe to PATH"^),
    echo       or set WBBRIDGE_PYTHON to a full python.exe path.
    exit /b 1
)
set "CURL=%SystemRoot%\System32\curl.exe"
set "PING=%SystemRoot%\System32\ping.exe"
set "PORT=8788"
if defined WBBRIDGE_PROXY_PORT set "PORT=%WBBRIDGE_PROXY_PORT%"

set "CODEX="
if defined WBBRIDGE_CODEX set "CODEX=%WBBRIDGE_CODEX%"
if not defined CODEX if exist "%APPDATA%\npm\codex.cmd" set "CODEX=%APPDATA%\npm\codex.cmd"
if not defined CODEX if exist "%APPDATA%\npm\codex.exe" set "CODEX=%APPDATA%\npm\codex.exe"
if not defined CODEX if exist "%LOCALAPPDATA%\pnpm\codex.cmd" set "CODEX=%LOCALAPPDATA%\pnpm\codex.cmd"
if not defined CODEX if exist "%ProgramFiles%\nodejs\codex.cmd" set "CODEX=%ProgramFiles%\nodejs\codex.cmd"
if not defined CODEX set "CODEX=codex"

"%CURL%" -s -o NUL --max-time 3 http://127.0.0.1:%PORT%/health
if not errorlevel 1 goto ready

echo [wbb] Local proxy is not running, starting it...
start "" /min "%PYEXE%" "%~dp0..\bin\wbproxy.py" --port %PORT%

set /a tries=0
:wait
"%PING%" -n 2 127.0.0.1 >NUL
"%CURL%" -s -o NUL --max-time 2 http://127.0.0.1:%PORT%/health
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% lss 20 goto wait

echo [wbb] Proxy did not come up within 20 seconds. Run start-wbbridge.cmd to see why.
echo [wbb] Common causes: the WorkBuddy desktop app is not installed, or not signed in.
exit /b 1

:ready
"%CODEX%" --profile wbb %*

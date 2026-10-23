@echo off
rem SessionStart hook entry point (calls bin\hook_session_start.py).
rem
rem Three hard constraints, all learned the hard way:
rem   1) Build the path with %CLAUDE_PLUGIN_ROOT%. The hook's cwd is the session
rem      directory, not the plugin root, so relative paths such as
rem      ./scripts/x.cmd or scripts\x.cmd always fail.
rem   2) Keep stdout empty. Codex parses hook stdout as JSON; any stray output
rem      marks the hook as Failed even when the work succeeded. Diagnostics go
rem      to a log file instead.
rem   3) Write the log under %TEMP%. The CLAUDE_PLUGIN_DATA directory may not
rem      exist on first run, and redirecting into a missing directory silently
rem      drops the output.
rem Keep this file ASCII/GBK + CRLF.
setlocal
call "%~dp0_findpy.cmd"
rem Prefer PYWEXE (pythonw) so the hook does not flash a console window.
if not defined PYWEXE if defined PYEXE set "PYWEXE=%PYEXE%"
if not defined PYWEXE exit /b 0
"%PYWEXE%" "%~dp0..\bin\hook_session_start.py" >> "%TEMP%\wbbridge-hook.log" 2>&1
rem Always exit 0: a failing hook must not block the user's session.
exit /b 0

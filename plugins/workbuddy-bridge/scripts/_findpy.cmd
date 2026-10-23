@echo off
rem Called by the other .cmd scripts; sets PYEXE to a usable python.exe.
rem Order: WBBRIDGE_PYTHON > common install paths > Codex bundled runtime > PATH
rem Keep this file ASCII/GBK + CRLF.
set "PYEXE="
if defined WBBRIDGE_PYTHON if exist "%WBBRIDGE_PYTHON%" set "PYEXE=%WBBRIDGE_PYTHON%"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python313\python.exe" set "PYEXE=%ProgramFiles%\Python313\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python312\python.exe" set "PYEXE=%ProgramFiles%\Python312\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python311\python.exe" set "PYEXE=%ProgramFiles%\Python311\python.exe"
rem Python bundled with the Codex desktop app (3.12 + cryptography)
if not defined PYEXE for /d %%d in ("%USERPROFILE%\.cache\codex-runtimes\*") do if not defined PYEXE if exist "%%d\dependencies\python\python.exe" set "PYEXE=%%d\dependencies\python\python.exe"
if not defined PYEXE for /d %%d in ("%USERPROFILE%\.codex\runtimes\*") do if not defined PYEXE if exist "%%d\python\python.exe" set "PYEXE=%%d\python\python.exe"
rem python on PATH; skip the WindowsApps "app execution alias" stub
if not defined PYEXE for /f "delims=" %%i in ('where python 2^>NUL') do if not defined PYEXE echo %%i | find /i "\WindowsApps\" >NUL || set "PYEXE=%%i"
rem
rem Also resolve PYWEXE: pythonw.exe next to PYEXE, so background processes get
rem no console window. A venv python.exe may be a uv stub that forwards without
rem creationflags, and the real interpreter then pops a visible console.
set "PYWDIR="
set "PYWEXE="
if defined PYEXE for %%d in ("%PYEXE%") do set "PYWDIR=%%~dpd"
if defined PYWDIR if exist "%PYWDIR%pythonw.exe" set "PYWEXE=%PYWDIR%pythonw.exe"
if not defined PYWEXE set "PYWEXE=%PYEXE%"
exit /b 0

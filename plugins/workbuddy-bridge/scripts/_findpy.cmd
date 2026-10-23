@echo off
rem 本脚本供其它 .cmd 脚本 call，返回一个能用的 python 解释器路径。
rem 优先级：WBBRIDGE_PYTHON > 常见安装路径 > Codex 自带运行时 > PATH
rem 本文件必须保存 GBK(cp936) + CRLF。
set "PYEXE="
if defined WBBRIDGE_PYTHON if exist "%WBBRIDGE_PYTHON%" set "PYEXE=%WBBRIDGE_PYTHON%"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python313\python.exe" set "PYEXE=%ProgramFiles%\Python313\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python312\python.exe" set "PYEXE=%ProgramFiles%\Python312\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python311\python.exe" set "PYEXE=%ProgramFiles%\Python311\python.exe"
rem Codex 桌面端自带的 Python（3.12 + cryptography），没装系统 Python 时靠它
if not defined PYEXE for /d %%d in ("%USERPROFILE%\.cache\codex-runtimes\*") do if not defined PYEXE if exist "%%d\dependencies\python\python.exe" set "PYEXE=%%d\dependencies\python\python.exe"
if not defined PYEXE for /d %%d in ("%USERPROFILE%\.codex\runtimes\*") do if not defined PYEXE if exist "%%d\python\python.exe" set "PYEXE=%%d\python\python.exe"
rem PATH 上的 python；跳过 WindowsApps 的「应用执行别名」存根
if not defined PYEXE for /f "delims=" %%i in ('where python 2^>NUL') do if not defined PYEXE echo %%i | find /i "\WindowsApps\" >NUL || set "PYEXE=%%i"
rem
rem 同时给出无控制台窗口的解释器 PYWEXE（与 PYEXE 同目录的 pythonw.exe）。
rem venv 的 python.exe 可能是 uv 存根，转发时不传 creationflags，真实解释器会弹可见控制台窗口；
rem pythonw.exe 没有窗口。
set "PYWDIR="
set "PYWEXE="
if defined PYEXE for %%d in ("%PYEXE%") do set "PYWDIR=%%~dpd"
if defined PYWDIR if exist "%PYWDIR%pythonw.exe" set "PYWEXE=%PYWDIR%pythonw.exe"
if not defined PYWEXE set "PYWEXE=%PYEXE%"
exit /b 0

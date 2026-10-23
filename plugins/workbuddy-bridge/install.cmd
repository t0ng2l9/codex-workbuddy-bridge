@echo off
rem 一键安装 workbuddy-bridge（幂等，可反复运行）
rem 本文件必须保持 GBK(cp936) + CRLF。
setlocal
call "%~dp0scripts\_findpy.cmd"
if not defined PYEXE (
    echo [wbb] 找不到 python 解释器。请安装 Python 3.10+，或设置 WBBRIDGE_PYTHON。
    pause
    exit /b 1
)
"%PYEXE%" "%~dp0bin\setup.py" install %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [wbb] 未成功完成（退出码 %RC%）。上面的 !! 行说明了原因。
)
pause
exit /b %RC%

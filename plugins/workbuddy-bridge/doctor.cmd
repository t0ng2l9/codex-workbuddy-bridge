@echo off
rem 环境体检：Python / 依赖 / 桌面端 / 凭据 / Codex 是否就绪
rem 本文件必须保持 GBK(cp936) + CRLF。
setlocal
call "%~dp0scripts\_findpy.cmd"
if not defined PYEXE (
    echo [wbb] 找不到 python 解释器。请安装 Python 3.10+，或设置 WBBRIDGE_PYTHON。
    pause
    exit /b 1
)
"%PYEXE%" "%~dp0bin\setup.py" doctor %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [wbb] 未成功完成（退出码 %RC%）。上面的 !! 行说明了原因。
)
pause
exit /b %RC%

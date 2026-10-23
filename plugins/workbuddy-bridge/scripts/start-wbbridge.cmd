@echo off
rem 启动 WorkBuddy -> Codex 本地代理（平时不需要，codex-wbb.cmd 会自动拉起）
rem 本文件必须保持 GBK(cp936) + CRLF。
setlocal
call "%~dp0_findpy.cmd"
if not defined PYEXE (
    echo [wbb] 找不到 python 解释器。请安装 Python 3.10+，或设置 WBBRIDGE_PYTHON。
    exit /b 1
)
"%PYEXE%" "%~dp0..\bin\wbproxy.py" %*

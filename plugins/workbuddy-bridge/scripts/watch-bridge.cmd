@echo off
rem 按 Codex 插件启用状态看护本地代理（插件激活则代理在跑，未激活则停掉）。
rem 不带参数 = 常驻看护；也可传 --status / --once / --install-autostart / --remove-autostart。
rem 本文件必须保持 GBK(cp936) + CRLF。
setlocal
call "%~dp0_findpy.cmd"
if not defined PYEXE (
    echo [wbb] 找不到 python 解释器。请安装 Python 3.10+，或设置 WBBRIDGE_PYTHON。
    exit /b 1
)
if "%~1"=="" (
    echo [wbb] 启动看护（Ctrl+C 退出）…
    "%PYEXE%" "%~dp0..\bin\watch_plugin.py"
) else (
    "%PYEXE%" "%~dp0..\bin\watch_plugin.py" %*
)

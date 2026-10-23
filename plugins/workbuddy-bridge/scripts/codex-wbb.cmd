@echo off
rem Codex + WorkBuddy 一键启动：确保本地代理在跑，再以 wbb profile 启动 Codex。
rem
rem 用法与 codex 相同：
rem     codex-wbb.cmd                     交互式 TUI
rem     codex-wbb.cmd exec "重构 utils.py"
rem     codex-wbb.cmd -m hy3              换模型（hy3 / deepseek-v4.1-flash 免费）
rem
rem 几处刻意的写法，别改：
rem   1) 本文件必须保持 GBK(cp936)：UTF-8 中文在 936 代码页下会吞掉换行、拆散语句。
rem   2) curl/ping 用 %SystemRoot% 全路径：否则 PATH 里的 git-bash 版会抢先匹配；
 rem      等待用 ping 而非 timeout.exe：timeout 在 stdin 被重定向时会直接报错。
rem   3) 全篇不用 ( ) 块包 set：括号内的 %VAR% 在解析期就展开，块内刚 set 的值取不到。
rem   4) codex 优先 npm 全局：PATH 里可能有别的 codex 抢先（例如某个 IDE 自带的那份
rem      与 WorkBuddy 不兼容）。要强制指定就设 WBBRIDGE_CODEX。
setlocal
call "%~dp0_findpy.cmd"
if not defined PYEXE (
    echo [wbb] 找不到 python 解释器。请安装 Python 3.10+，或设置 WBBRIDGE_PYTHON。
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

echo [wbb] 本地代理未运行，正在启动...
start "" /min "%PYEXE%" "%~dp0..\bin\wbproxy.py" --port %PORT%

set /a tries=0
:wait
"%PING%" -n 2 127.0.0.1 >NUL
"%CURL%" -s -o NUL --max-time 2 http://127.0.0.1:%PORT%/health
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% lss 20 goto wait

echo [wbb] 代理启动超时（20 秒）。请手动运行 start-wbbridge.cmd 查看原因。
echo [wbb] 常见原因：WorkBuddy AI 桌面端未安装，或尚未登录。
exit /b 1

:ready
"%CODEX%" --profile wbb %*

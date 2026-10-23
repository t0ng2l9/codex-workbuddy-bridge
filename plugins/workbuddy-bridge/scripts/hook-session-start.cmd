@echo off
rem SessionStart hook 入口：找 python，再跑 bin\hook_session_start.py。
rem
rem 三条硬约束（都实测出来的）：
rem   1) 必须用 %CLAUDE_PLUGIN_ROOT% 拼绝对路径。hook 的 cwd 是会话目录而非插件根，
rem      相对路径（./scripts/x.cmd、scripts\x.cmd）一律失败。
rem   2) 必须保持 stdout 空白。codex 会把 hook 的 stdout 当 JSON 解析，
rem      打了中文就会判 hook Failed（即使脚本其实干活成功了）。诊断写日志文件。
rem   3) 日志固定写 %TEMP%：CLAUDE_PLUGIN_DATA 目录在首次使用时并不存在，
rem      重定向到不存在的目录会静默吞掉整个脚本（踩过）。
rem 本文件必须保持 GBK(cp936) + CRLF。
setlocal
call "%~dp0_findpy.cmd"
rem 用 PYWEXE（pythonw）：hook 由 codex 拉起，用 python.exe 会多弹一个控制台窗口。
if not defined PYWEXE if defined PYEXE set "PYWEXE=%PYEXE%"
if not defined PYWEXE exit /b 0
"%PYWEXE%" "%~dp0..\bin\hook_session_start.py" >> "%TEMP%\wbbridge-hook.log" 2>&1
rem 无论成败都返回 0：hook 失败不该让用户的会话起不来
exit /b 0

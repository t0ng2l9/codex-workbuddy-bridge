"""以后台、无控制台窗口的方式启动本插件的 Python 进程。

为什么需要这个模块（实测结论，别改成 `sys.executable`）：

某些 Agent 环境的 venv 里 `Scripts\\python.exe` 只有 ~45KB，是 **uv 的 launcher 存根**，
它会重新 spawn `pyvenv.cfg` 的 `home` 指向的**真实**解释器，而创建进程时给的
`creationflags` **不会跟着传过去** —— 于是真实进程按默认方式启动，自己创建了一个
**可见的控制台窗口**（实测：存根进程 `MainWindowHandle=0`，它 spawn 出来的真实
解释器 `MainWindowHandle=1251688`，标题正是 `…\\venv\\Scripts\\python.exe`）。

正确的做法是**用 venv 自带的 `pythonw.exe` 存根**：

- 存根会激活 venv → `cryptography` 等依赖可用；
- 它是 **GUI 子系统**，转发时启动的是真实 `pythonw.exe`（不是 `python.exe`）
  → 全程不创建控制台。

**不要**改成直接调用 `pyvenv.cfg` 里 `home` 下的真实 `pythonw.exe` —— 那样会**丢掉 venv**
（真实解释器没有 venv 的 site-packages，实测 `ModuleNotFoundError: cryptography`，
进程起不来且日志里什么都没有）。

另外：只用 `DETACHED_PROCESS`（`0x8`），**不要与 `CREATE_NO_WINDOW` 按位或** ——
两者语义冲突，而且真正的问题在存根转发，加那个标志不解决。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

#: 新进程不继承父控制台，也不随父进程退出而终止。
DETACHED_PROCESS = 0x00000008

#: 后台进程的默认日志（各调用点通常会自己指定更贴合用途的日志）。
DEFAULT_LOG = Path(tempfile.gettempdir()) / "wbbridge-launch.log"


def no_console_python() -> str:
    """返回一个启动后**不会出现控制台窗口**、且**保留 venv** 的 python 解释器路径。

    优先 venv 里与当前解释器同目录的 `pythonw.exe` 存根；没有就退回当前解释器
    （可能带控制台，但至少能跑）。
    """
    try:
        cand = Path(sys.executable).with_name("pythonw.exe")
        if cand.is_file():
            return str(cand)
    except OSError:
        pass
    return sys.executable


def spawn_detached(args: list[str], log: Path | None = None, cwd: str | None = None):
    """后台启动一个不显示窗口、且不随父进程退出的进程。

    子进程 stdout/stderr 追加到 `log`（默认 `%TEMP%\\wbbridge-launch.log`），
    stdin 接 DEVNULL —— 否则子进程可能继承父进程的管道，父进程退出后写失败。
    """
    log = log or DEFAULT_LOG
    kwargs: dict = {"stdin": subprocess.DEVNULL, "cwd": cwd}
    if sys.platform == "win32":
        kwargs["creationflags"] = DETACHED_PROCESS
    try:
        fh = open(log, "ab")
    except OSError:
        fh = None
    try:
        if fh is not None:
            kwargs["stdout"] = fh
            kwargs["stderr"] = fh
        else:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        proc = subprocess.Popen([str(a) for a in args], **kwargs)
    finally:
        if fh is not None:
            fh.close()          # 子进程持有自己的句柄副本，关掉父进程这边的即可
    return proc


def python_note() -> str:
    """给 doctor / 排错用：说明后台将用哪个解释器。"""
    exe = no_console_python()
    if exe == sys.executable:
        return f"{exe}  [当前解释器：可能带控制台窗口]"
    return f"{exe}  [pythonw 存根：无控制台，且保留 venv]"

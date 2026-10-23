"""Start this plugin's Python processes in the background, with no console window.

Why this module exists (measured; do not "simplify" it to `sys.executable`):

In some agent environments the venv's `Scripts\\python.exe` is only ~45KB: it is a
**uv launcher stub**. It re-spawns the *real* interpreter named by `pyvenv.cfg`'s
`home`, and the `creationflags` given to the stub are **not forwarded**. The real
process therefore starts with defaults and creates its own **visible console
window** (measured: the stub has `MainWindowHandle=0`, the real interpreter it
spawned has `MainWindowHandle=1251688`, titled `...\\venv\\Scripts\\python.exe`).

The correct approach is to use the venv's own `pythonw.exe` stub:

- the stub activates the venv, so `cryptography` and friends are importable;
- it is a **GUI-subsystem** binary, so the real process it forwards to is
  `pythonw.exe` (not `python.exe`) and no console is ever created.

Do **not** call the real `pythonw.exe` under `pyvenv.cfg`'s `home` directly: that
loses the venv (the real interpreter has no venv site-packages; measured
`ModuleNotFoundError: cryptography`, process never starts and the log stays empty).

Also: use `DETACHED_PROCESS` (`0x8`) alone, **not** OR-ed with `CREATE_NO_WINDOW`.
The two flags conflict semantically, and the real problem is the stub forwarding,
which that flag does not fix.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

#: The new process neither inherits the parent console nor dies with the parent.
DETACHED_PROCESS = 0x00000008

#: Default log for background processes (callers usually pass a more specific one).
DEFAULT_LOG = Path(tempfile.gettempdir()) / "wbbridge-launch.log"


def no_console_python() -> str:
    """Return a python interpreter path that shows **no console window** and
    **keeps the venv**.

    Prefers the `pythonw.exe` stub sitting next to the current interpreter;
    falls back to the current interpreter (may show a console, but at least runs).
    """
    try:
        cand = Path(sys.executable).with_name("pythonw.exe")
        if cand.is_file():
            return str(cand)
    except OSError:
        pass
    return sys.executable


def spawn_detached(args: list[str], log: Path | None = None, cwd: str | None = None):
    """Start a background process with no window that outlives its parent.

    Child stdout/stderr are appended to `log` (default
    `%TEMP%\\wbbridge-launch.log`), and stdin is DEVNULL; otherwise the child may
    inherit the parent's pipes and fail to write after the parent exits.
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
            fh.close()          # the child holds its own handle; close ours
    return proc


def python_note() -> str:
    """For doctor / troubleshooting: which interpreter background work will use."""
    exe = no_console_python()
    if exe == sys.executable:
        return f"{exe}  [current interpreter: may show a console window]"
    return f"{exe}  [pythonw stub: no console, venv preserved]"

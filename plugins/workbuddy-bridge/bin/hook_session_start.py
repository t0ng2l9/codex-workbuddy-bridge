"""SessionStart hook: make sure the WorkBuddy local proxy is running.

Design notes:
  - exit 0 either way -- a failing hook must not stop the user's session;
  - if the proxy is already up, return immediately (one cheap port probe);
  - start it detached with its own log so it outlives the session.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PROXY = PLUGIN_ROOT / "bin" / "wbproxy.py"
sys.path.insert(0, str(PLUGIN_ROOT / "bin"))
import wbspawn  # noqa: E402  (console-free spawn)

DEFAULT_PORT = 8788


def port() -> int:
    try:
        return int(os.environ.get("WBBRIDGE_PROXY_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def is_up(p: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.4):
            return True
    except OSError:
        return False


def _utf8_output() -> None:
    """Output goes to a log file, not a console: force UTF-8 so neither
    PYTHONUTF8 nor the code page can affect it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _utf8_output()
    p = port()
    if is_up(p):
        return 0
    if not PROXY.is_file():
        print(f"[workbuddy] proxy script not found: {PROXY}", file=sys.stderr)
        return 0

    log = Path(tempfile.gettempdir()) / "wbbridge-proxy.log"
    try:
        # wbspawn picks an interpreter that brings no console window: a venv's
        # python.exe may be a uv stub that forwards without creationflags, so the
        # real process pops a visible console (measured).
        wbspawn.spawn_detached([wbspawn.no_console_python(), str(PROXY), "--port", str(p)],
                               log=log, cwd=str(PLUGIN_ROOT))
    except OSError as e:
        print(f"[workbuddy] failed to start the proxy: {e}", file=sys.stderr)
        return 0

    # Wait for it to come up, at most 20 seconds
    for _ in range(40):
        if is_up(p):
            print(f"[workbuddy] proxy ready at http://127.0.0.1:{p}/v1")
            return 0
        time.sleep(0.5)

    print(f"[workbuddy] proxy not ready within 20s, see log {log}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

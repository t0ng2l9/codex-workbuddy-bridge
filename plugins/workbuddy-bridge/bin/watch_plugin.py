"""Watch the local proxy based on the plugin's enabled state: active plugin -> proxy running.

Why this exists: a Codex hook fires once at **session start**, so it cannot handle
three cases -- the proxy dying mid-session, the machine rebooting before any
session starts, or not using Codex at all. This watchdog fills that gap; it reads
the state straight from Codex, so "enable/disable the plugin" means "start/stop
the proxy".

Active requires both:
  1. `enabled` is true for `[plugins."<name>@<market>"]` in config.toml (or the
     block exists and was never explicitly disabled)
  2. the plugin is actually installed (artifacts exist under the cache dir)

The proxy only manages **its own** process: before stopping anything it checks
that the command line contains this package's wbproxy.py, so unrelated programs
on the port are never killed.

Usage:
    python watch_plugin.py                    run the watchdog (5 s poll by default)
    python watch_plugin.py --once             reconcile once (for testing)
    python watch_plugin.py --status           print the current verdict, change nothing
    python watch_plugin.py --install-autostart    install logon autostart
    python watch_plugin.py --remove-autostart     remove logon autostart
"""
from __future__ import annotations

import argparse
import os

import wbspawn
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN = PLUGIN_ROOT / "bin"
PROXY = BIN / "wbproxy.py"

PLUGIN_NAME = "workbuddy-bridge"
MARKETPLACE = "wbbridge"
DEFAULT_PORT = 8788
TASK_NAME = "workbuddy-bridge-watch"

LOG = Path(tempfile.gettempdir()) / "wbbridge-watch.log"
LOCK = Path(tempfile.gettempdir()) / "wbbridge-watch.lock"


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log_err(*a) -> None:
    """State changes and errors, **always logged** (same split as wbproxy: only traces are silenced)."""
    line = " ".join(str(x) for x in a)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except OSError:
        pass


def port() -> int:
    try:
        return int(os.environ.get("WBBRIDGE_PROXY_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def config_path() -> Path:
    return Path(os.environ.get("WBBRIDGE_CODEX_CONFIG") or (codex_home() / "config.toml"))


# ── Rule 1: the plugin enable flag in config.toml ───────────
def plugin_enabled(cfg: Path | None = None) -> tuple[bool, str]:
    """Return (active, reason). A missing config or a missing plugin block counts as inactive."""
    cfg = cfg or config_path()
    if not cfg.is_file():
        return False, f"config not found: {cfg}"
    try:
        import tomllib
        with open(cfg, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as e:
        return False, f"failed to parse the config: {type(e).__name__}: {e}"

    plugins = data.get("plugins") or {}
    key = f"{PLUGIN_NAME}@{MARKETPLACE}"
    if key not in plugins:
        return False, f"no [{key}] in the config (plugin not installed or removed)"
    entry = plugins.get(key) or {}
    enabled = entry.get("enabled", True)      # block present without enabled -> treated as enabled (Codex default)
    if enabled:
        return True, f"[{key}] enabled=true"
    return False, f"[{key}] enabled=false (disabled in Codex)"


# ── Rule 2: the plugin is really on disk ────────────────────
def cache_root() -> Path:
    return codex_home() / "plugins" / "cache" / MARKETPLACE / PLUGIN_NAME


def plugin_installed() -> tuple[bool, str]:
    root = cache_root()
    if not root.is_dir():
        return False, f"plugin cache does not exist: {root}"
    versions = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda p: p.name)
    if not versions:
        return False, f"plugin cache is empty: {root}"
    return True, f"installed version {versions[-1].name}"


def is_active(cfg: Path | None = None) -> tuple[bool, str]:
    ok1, why1 = plugin_enabled(cfg)
    if not ok1:
        return False, why1
    ok2, why2 = plugin_installed()
    if not ok2:
        return False, why2
    return True, f"active ({why1}; {why2})"


# ── Proxy process: identify, start, stop ────────────────────
def _netstat_pids(p: int) -> list[str]:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.decode("cp936", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{p}"):
                pids.append(parts[4])
    return pids


def _exe_args(pid: str) -> str:
    ps = (f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
        return r.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def proxy_running(p: int | None = None) -> tuple[bool, str]:
    p = p or port()
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.5):
            return True, "port is listening"
    except OSError:
        return False, "port is not listening"


def our_pids(p: int | None = None) -> list[str]:
    """Only processes on our port whose command line contains this package's wbproxy -- touch nothing else."""
    p = p or port()
    out = []
    for pid in _netstat_pids(p):
        if "wbproxy" in _exe_args(pid):
            out.append(pid)
    return out


def start_proxy(p: int | None = None) -> bool:
    p = p or port()
    if not PROXY.is_file():
        log_err(f"start failed: cannot find {PROXY}")
        return False
    logf = Path(tempfile.gettempdir()) / "wbbridge-proxy.log"
    try:
        # See wbspawn: a venv python.exe is a uv stub that drops the flags, so the real
        # process pops up a console.
        wbspawn.spawn_detached([wbspawn.no_console_python(), str(PROXY), "--port", str(p)],
                               log=logf, cwd=str(PLUGIN_ROOT))
    except OSError as e:
        log_err(f"failed to start the proxy: {e}")
        return False
    for _ in range(40):
        time.sleep(0.5)
        if proxy_running(p)[0]:
            log_err(f"proxy started -> http://127.0.0.1:{p}/v1")
            return True
    log_err(f"proxy still not ready 20s after start (log {logf})")
    return False


def stop_proxy(p: int | None = None) -> bool:
    p = p or port()
    pids = our_pids(p)
    if not pids:
        return False
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log_err(f"failed to kill process {pid}: {e}")
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(0.4)
        if not proxy_running(p)[0]:
            log_err(f"plugin inactive -> proxy stopped (PID {', '.join(pids)})")
            return True
    log_err(f"timed out stopping the proxy (PID {', '.join(pids)})")
    return False


# ── Single instance ─────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    try:
        subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, timeout=30)
        return str(pid) in r.stdout.decode("cp936", "replace")
    except (OSError, subprocess.SubprocessError):
        return False


def acquire_lock() -> bool:
    """Take the single-instance lock.

    Created atomically with O_CREAT|O_EXCL -- an earlier version checked for
    existence and then wrote, so two processes starting at once both passed the
    check and two watchdogs ran side by side (measured). The lock records a pid
    and is only cleared when the original holder is dead.
    """
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            return True
        except FileExistsError:
            # Lock exists: is the holder still alive? If not, treat it as stale and retry once
            try:
                old_pid = int(LOCK.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                old_pid = 0
            if old_pid and _pid_alive(old_pid) and old_pid != os.getpid():
                return False
            try:
                LOCK.unlink(missing_ok=True)
            except OSError:
                return True          # cannot clear it: do not refuse to work over this
        except OSError:
            return True              # an unwritable lock file must not block the watchdog
    return False


# ── Reconcile ───────────────────────────────────────────────
def reconcile(cfg: Path | None = None, p: int | None = None, quiet: bool = False) -> str:
    """Bring the proxy state in line with the plugin state. Returns what it did."""
    p = p or port()
    active, why = is_active(cfg)
    running, _ = proxy_running(p)

    if active and not running:
        if not quiet:
            log_err(f"plugin active but proxy not running ({why}) -> starting proxy")
        start_proxy(p)
        return "started"
    if not active and running:
        pids = our_pids(p)
        if not pids:
            # Something is listening on the port but it is not our proxy: not our business, never kill it.
            return "foreign-port" if not quiet else "ok"
        if not quiet:
            log_err(f"plugin inactive ({why}) -> stopping proxy")
        stop_proxy(p)
        return "stopped"
    return "ok" if active else "idle"


def show_status(cfg: Path | None = None, p: int | None = None) -> int:
    p = p or port()
    active, why = is_active(cfg)
    running, rwhy = proxy_running(p)
    pids = our_pids(p) if running else []
    print(f"  plugin active : {active}   ({why})")
    print(f"  proxy running : {running} ({rwhy})")
    if pids:
        print(f"  our proxy PID : {', '.join(pids)}")
    print(f"  expected      : {'proxy should run' if active else 'proxy should stop'}"
          f"  ->  {'in sync' if active == running else 'out of sync, needs reconcile'}")
    return 0


# ── Logon autostart ─────────────────────────────────────────
# Use the Startup folder instead of a scheduled task: schtasks /Create normally
# needs admin rights (measured: "ERROR: Access is denied."), while the Startup
# folder is per-user, needs no elevation, and runs at logon.
# The .vbs wrapper hides the console window (pythonw has none either; belt and braces).

AUTOSTART_VBS = "workbuddy-bridge-watch.vbs"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_file() -> Path:
    return startup_dir() / AUTOSTART_VBS


def watcher_exe() -> str:
    """Prefer pythonw.exe (no console window)."""
    return wbspawn.no_console_python()


def _vbs(path_exe: str, path_script: str) -> str:
    """Build the autostart VBS.

    Quotes are assembled explicitly with Chr(34) instead of counting `"` inside a
    literal -- miscounting them in a literal yields an "unterminated string
    constant" compile error (measured).
    """
    return (
        "' Installed by workbuddy-bridge: start the proxy watchdog at logon (hidden).\r\n"
        "' Delete this file to cancel autostart.\r\n"
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'exe = "{path_exe}"\r\n'
        f'script = "{path_script}"\r\n'
        'q = Chr(34)\r\n'
        "cmd = q & exe & q & \" \" & q & script & q\r\n"
        "sh.Run cmd, 0, False\r\n"
    )


def install_autostart() -> int:
    d = startup_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log_err(f"cannot access the Startup folder {d}: {e}")
        print(f"  !! cannot access the Startup folder: {e}")
        return 1

    target = autostart_file()
    exe, script = watcher_exe(), str(Path(__file__).resolve())
    try:
        # UTF-16LE + BOM: WSH understands it, and it stays safe when the user name or path is non-ASCII
        target.write_text(_vbs(exe, script), encoding="utf-16")
    except OSError as e:
        log_err(f"failed to write the autostart file {target}: {e}")
        print(f"  !! failed to write the autostart file: {e}")
        return 1
    print(f"  logon autostart installed: {target}")
    print(f"    {exe} {script}")

    # Start one now instead of waiting for the next logon
    if acquire_lock():
        try:
            wbspawn.spawn_detached([wbspawn.no_console_python(), script])
            print("  watchdog started")
        except OSError as e:
            print(f"  !! failed to start the watchdog now (it will still start at logon): {e}")
    else:
        print("  watchdog already running")
    print(f"  watchdog log: {LOG}")
    return 0


def remove_autostart() -> int:
    target = autostart_file()
    if not target.is_file():
        print("  no autostart installed (file not found)")
        return 0
    try:
        target.unlink()
    except OSError as e:
        print(f"  !! failed to delete the autostart file: {e}")
        return 1
    print(f"  logon autostart removed: {target}")
    print("  Note: a running watchdog is not terminated; kill it with taskkill if needed.")
    return 0


def autostart_installed() -> bool:
    try:
        return autostart_file().is_file()
    except OSError:
        return False


def main() -> int:
    _utf8()
    ap = argparse.ArgumentParser(description="Watch the local proxy based on the Codex plugin state")
    ap.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds (default 5)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=None, help="use a specific config.toml (default $CODEX_HOME/config.toml)")
    ap.add_argument("--once", action="store_true", help="reconcile once")
    ap.add_argument("--status", action="store_true", help="print the verdict without changing anything")
    ap.add_argument("--install-autostart", action="store_true", help="install logon autostart")
    ap.add_argument("--remove-autostart", action="store_true", help="remove logon autostart")
    a = ap.parse_args()

    cfg = Path(a.config) if a.config else None
    p = a.port or port()

    if a.status:
        return show_status(cfg, p)
    if a.install_autostart:
        return install_autostart()
    if a.remove_autostart:
        return remove_autostart()
    if a.once:
        print("  " + reconcile(cfg, p))
        return 0

    if not acquire_lock():
        print(f"  a watchdog is already running ({LOCK}); use --once to re-check or delete the file first.")
        return 1

    log_err(f"watchdog started pid={os.getpid()} interval={a.interval}s config={cfg or config_path()}")
    try:
        while True:
            try:
                reconcile(cfg, p)
            except Exception as e:      # one bad round must not take the watchdog down
                log_err(f"reconcile error (round skipped): {type(e).__name__}: {e}")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        log_err("watchdog exiting (KeyboardInterrupt)")
    finally:
        LOCK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

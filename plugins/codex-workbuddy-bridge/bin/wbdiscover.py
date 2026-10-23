"""Path and runtime auto-discovery.

Makes this package work on **any machine**: where the WorkBuddy desktop app is
installed, what the credential file is called, whether Python and Codex are on
PATH -- all discovered at runtime, nothing hard-coded.

Every discovery function follows the same priority:
**environment variable override > automatic probe**.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Environment variable names (read by wbproxy / setup / launcher scripts) ──
ENV_APP = "WBBRIDGE_APP"
ENV_AUTH = "WBBRIDGE_AUTH_FILE"
ENV_PYTHON = "WBBRIDGE_PYTHON"
ENV_CODEX = "WBBRIDGE_CODEX"

APP_EXE_NAMES = ("WorkBuddyAI.exe", "CodeBuddy.exe", "WorkBuddy.exe")
APP_DIR_NAMES = ("WorkBuddyAI", "WorkBuddy AI", "CodeBuddy", "CodeBuddyAI")

# Credential file names: international build first, domestic build as fallback
# (both can exist at once, and they belong to different accounts)
AUTH_NAMES = ("workbuddy-desktop-ai.info", "workbuddy-desktop.info")


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def console_encodings() -> None:
    """Match stdout/stderr encoding to the console code page.

    Some environments (certain IDEs / agent hosts) set PYTHONUTF8=1, so Python
    emits UTF-8 while the Windows console defaults to cp936 -- non-ASCII text
    turns into mojibake. Reconfigure both streams to the **console output code
    page** instead, so:

      - the terminal displays correctly;
      - redirected output matches the .cmd files' own encoding (cmd writes in the
        OEM code page), with no half-cp936, half-UTF-8 mixed file.

    Deliberately does not call chcp -- that would break the cp936-encoded .cmd
    that is currently running.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except Exception:
        cp = 0
    enc = f"cp{cp}" if cp else "utf-8"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:
            pass


# ── WorkBuddy desktop executable ────────────────────────────
def _app_from_registry() -> str | None:
    """The NSIS installer writes an uninstall entry; its UninstallString points at
    the install directory."""
    if sys.platform != "win32":
        return None
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$p=@('HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*');"
        "Get-ItemProperty $p | "
        "Where-Object { $_.DisplayName -match 'WorkBuddy|CodeBuddy' } | "
        "Select-Object -First 4 -ExpandProperty UninstallString"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        # e.g.  "D:\...\WorkBuddyAI\Uninstall WorkBuddyAI.exe" /currentuser
        path = line.split('"')[0] if line.startswith('"') else line
        path = path.split(" /")[0].strip().strip('"')
        d = Path(path).parent
        for exe in APP_EXE_NAMES:
            cand = d / exe
            if cand.is_file():
                return str(cand)
    return None


def _app_from_process() -> str | None:
    """If the desktop app is running, just ask it."""
    if sys.platform != "win32":
        return None
    names = "' or name='".join(n.replace(".exe", ".exe") for n in APP_EXE_NAMES)
    try:
        r = subprocess.run(
            ["wmic", "process", "where", f"name='{names}'",
             "get", "ExecutablePath", "/format:csv"],
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if parts and parts[-1].lower().endswith(".exe") and Path(parts[-1]).is_file():
            return parts[-1]
    return None


def _app_from_common_paths() -> str | None:
    roots = [
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    ]
    seen: list[Path] = []
    for root in roots:
        if not root:
            continue
        for d in APP_DIR_NAMES:
            for sub in ("Programs", ""):
                base = Path(root) / sub / d if sub else Path(root) / d
                if base not in seen:
                    seen.append(base)
    # Same directory names on non-system drives (many people install to D:)
    for drive in "CDEFGH":
        for d in APP_DIR_NAMES:
            seen.append(Path(f"{drive}:/Programs/{d}"))
            seen.append(Path(f"{drive}:/Program Files/{d}"))
    for base in seen:
        for exe in APP_EXE_NAMES:
            cand = base / exe
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                continue
    return None


def find_app() -> str | None:
    """Return the WorkBuddy desktop executable path, or None if not found.

    An explicit WBBRIDGE_APP is taken at face value even if the path does not
    exist: a typo then shows up honestly as "not found" in the doctor output
    instead of silently falling back to auto-discovery and looking like it worked.
    """
    override = _env(ENV_APP)
    if override:
        return override
    for probe in (_app_from_registry, _app_from_process, _app_from_common_paths):
        got = probe()
        if got:
            return got
    return None


# ── Credential file ─────────────────────────────────────────
def _auth_dirs() -> list[Path]:
    la = os.environ.get("LOCALAPPDATA")
    out: list[Path] = []
    if la:
        out.append(Path(la) / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    # Some versions keep the data under the user profile
    out.append(Path.home() / ".codebuddy" / "auth")
    return out


def find_auth_files() -> list[str]:
    """List every usable credential file (international build first)."""
    found: list[str] = []
    for d in _auth_dirs():
        for name in AUTH_NAMES:
            p = d / name
            try:
                if p.is_file() and p.stat().st_size > 0:
                    found.append(str(p))
            except OSError:
                continue
    return found


def find_auth_file() -> str | None:
    override = _env(ENV_AUTH)
    if override:
        return override
    files = find_auth_files()
    return files[0] if files else None


# ── Python interpreter (used by the .cmd launchers) ─────────
def _python_candidates() -> list[str]:
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", "")
    home = Path.home()
    out = [
        os.path.join(la, "Programs", "Python", "Python313", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python311", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python310", "python.exe"),
        os.path.join(pf, "Python313", "python.exe"),
        os.path.join(pf, "Python312", "python.exe"),
        os.path.join(pf, "Python311", "python.exe"),
        os.path.join(pf, "Python310", "python.exe"),
    ]
    # The Codex desktop app ships a Python (3.12 + cryptography), so machines
    # without a system Python still work.
    # Path looks like ~/.cache/codex-runtimes/<runtime>/dependencies/python/python.exe
    for base, pat in ((home / ".cache" / "codex-runtimes", "*/dependencies/python/python.exe"),
                      (home / ".codex" / "runtimes", "*/python/python.exe")):
        try:
            out.extend(str(p) for p in sorted(base.glob(pat)))
        except OSError:
            pass
    return out


def find_python() -> str | None:
    """Return a usable python interpreter, preferring one with cryptography."""
    override = _env(ENV_PYTHON)
    if override and Path(override).is_file():
        return override
    cands = [c for c in _python_candidates() if Path(c).is_file()]
    # python / python3 on PATH
    for name in ("python.exe", "python3.exe"):
        p = shutil.which(name)
        # Skip the WindowsApps "app execution alias" stub: it is only a forwarder,
        # so without the Store build of Python it opens the Store, and importing
        # cryptography always fails.
        if p and "\\WindowsApps\\" not in p:
            cands.append(p)
    # Prefer one that can import cryptography
    for c in cands:
        if _has_crypto(c):
            return c
    return cands[0] if cands else None


def _has_crypto(python: str) -> bool:
    try:
        r = subprocess.run([python, "-c", "import cryptography; print(cryptography.__version__)"],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def has_crypto(python: str) -> tuple[bool, str]:
    """Return (has cryptography, version or error)."""
    try:
        r = subprocess.run([python, "-c",
                            "import sys, cryptography; "
                            "print(f'{sys.version_info.major}.{sys.version_info.minor}|{cryptography.__version__}')"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            return True, r.stdout.decode("utf-8", "replace").strip()
        return False, r.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:120]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:120]


# ── Codex executable ────────────────────────────────────────
def find_codex() -> str | None:
    """Return the path of the codex .cmd/.exe.

    In some environments the codex that comes first on PATH is an IDE-bundled
    build whose exec fails. So prefer the npm global install (the one the user
    installed), then fall back to PATH.
    """
    override = _env(ENV_CODEX)
    if override:
        p = shutil.which(override) or (override if Path(override).is_file() else None)
        if p:
            return p
    appdata = os.environ.get("APPDATA", "")
    for rel in ("npm/codex.cmd", "npm/codex.exe", "npm/codex"):
        p = Path(appdata) / rel
        if p.is_file():
            return str(p)
    for name in ("codex.cmd", "codex.exe", "codex"):
        p = shutil.which(name)
        if p:
            return p
    return None


# ── One-shot health check ───────────────────────────────────
def diagnose(port: int = 8788) -> list[tuple[str, bool, str]]:
    """Return [(check name, passed, detail)] for setup.py doctor to print."""
    out: list[tuple[str, bool, str]] = []

    # Python
    py = find_python()
    if py:
        ok, info = has_crypto(py)
        ver = info.split("|")[0] if "|" in info else "?"
        try:
            major, minor = (int(x) for x in ver.split(".")[:2])
            ver_ok = (major, minor) >= (3, 10)
        except ValueError:
            ver_ok = False
        out.append(("Python >=3.10", ver_ok, py if ver_ok else f"{py} (version {ver} is too old)"))
        out.append(("cryptography", ok, info if ok else f"missing -- {info}"))
    else:
        out.append(("Python >=3.10", False, "python not found; install Python 3.10+ and tick Add to PATH"))
        out.append(("cryptography", False, "skipped (no python)"))

    # WorkBuddy desktop app
    app = find_app()
    out.append(("WorkBuddy desktop app", bool(app), app or "not found; install the WorkBuddy AI desktop app (international build)"))

    # Credentials
    auths = find_auth_files()
    if auths:
        out.append(("credentials", True, auths[0] + (f" ({len(auths)-1} more available)" if len(auths) > 1 else "")))
    else:
        out.append(("credentials", False, "credential file not found; start the WorkBuddy desktop app and sign in"))

    # Codex
    cx = find_codex()
    out.append(("Codex CLI", bool(cx), cx or "codex not found; install with npm i -g @openai/codex"))

    # Port
    try:
        import socket
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            busy = "already in use (possibly this proxy already running)"
            out.append((f"port {port}", True, busy))
    except OSError:
        out.append((f"port {port}", True, "free"))

    return out


def main() -> int:
    import argparse
    console_encodings()
    ap = argparse.ArgumentParser(description="Path and runtime auto-discovery")
    ap.add_argument("what", nargs="?", default="all",
                    choices=["app", "auth", "python", "codex", "all", "doctor"])
    ap.add_argument("--port", type=int, default=8788)
    a = ap.parse_args()

    if a.what == "app":
        print(find_app() or "")
    elif a.what == "auth":
        print(find_auth_file() or "")
    elif a.what == "python":
        print(find_python() or "")
    elif a.what == "codex":
        print(find_codex() or "")
    elif a.what == "doctor":
        width = max(len(n) for n, _, _ in diagnose(a.port))
        for name, ok, detail in diagnose(a.port):
            print(f"  [{'OK ' if ok else '!! '}] {name:<{width}}  {detail}")
    else:
        print(f"app    : {find_app()}")
        print(f"auth   : {find_auth_file()}")
        print(f"python : {find_python()}")
        print(f"codex  : {find_codex()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

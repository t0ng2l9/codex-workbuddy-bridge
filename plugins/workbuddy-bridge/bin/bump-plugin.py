"""Stamp a cachebuster version onto the plugin manifest.

Codex materializes a plugin into `plugins/cache/<market>/<plugin>/<version>/` and
**will not re-read it while the version is unchanged**. So after editing the
plugin locally, run this first to move `version` to a monotonically increasing
semver:

    1.2.2  ->  1.2.2-20261013120000

It uses the semver prerelease segment, so the result still parses; the timestamp
is UTC, which keeps it monotonic.

Usage:
    python bump-plugin.py <plugin-dir>
    python bump-plugin.py <plugin-dir> --set 2.0.0     # explicit (for releases)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Strict semver, matching the official validator
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
# Already carries a timestamp suffix: replace it instead of stacking another
CACHEBUSTER_RE = re.compile(r"-\d{14}$")


def strip_cachebuster(version: str) -> str:
    return CACHEBUSTER_RE.sub("", version)


def console_encodings() -> None:
    """Match stdout/stderr encoding to the console code page (see wbdiscover)."""
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


def main() -> int:
    console_encodings()
    ap = argparse.ArgumentParser(description="Stamp a cachebuster version on a Codex plugin")
    ap.add_argument("plugin_path", help="Plugin root directory (contains .codex-plugin/plugin.json)")
    ap.add_argument("--set", metavar="VERSION",
                    help="Explicit version (for releases) instead of appending a timestamp")
    a = ap.parse_args()

    manifest_path = Path(a.plugin_path).expanduser() / ".codex-plugin" / "plugin.json"
    if not manifest_path.is_file():
        print(f"Plugin manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Cannot read plugin manifest: {e}", file=sys.stderr)
        return 1

    old = str(manifest.get("version") or "")
    if SEMVER_RE.fullmatch(old) is None:
        print(f"Existing version is not strict semver, refusing to change: {old!r}", file=sys.stderr)
        return 1

    if a.set:
        new = a.set
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        new = f"{strip_cachebuster(old)}-{stamp}"

    if SEMVER_RE.fullmatch(new) is None:
        print(f"New version is not strict semver: {new!r}", file=sys.stderr)
        return 1

    manifest["version"] = new
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    name = manifest.get("name", "?")
    print(f"{name}: {old} -> {new}")
    print("Now reinstall: codex plugin add " + f"{name}@<market>")
    return 0


if __name__ == "__main__":
    sys.exit(main())

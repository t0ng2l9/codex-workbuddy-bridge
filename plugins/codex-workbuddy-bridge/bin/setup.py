"""codex-workbuddy-bridge installer / uninstaller.

Does four things (all idempotent, safe to run repeatedly):

  1. write the [model_providers.wbbridge] block into $CODEX_HOME/config.toml
  2. write $CODEX_HOME/wbb.config.toml (the profile itself; since 0.162 a profile must be its own file)
  3. generate $CODEX_HOME/wbbridge-catalog.json (the data source for the model picker)
  4. install the wbbridge-current-model skill and, optionally, add a "check the live model" rule to AGENTS.md

Usage:
    python setup.py install      [--no-skill] [--no-agents-rule] [--port 8788]
    python setup.py uninstall    [--yes]
    python setup.py status
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wbspawn  # noqa: E402  (launch without a console window)
import wbdiscover  # noqa: E402
from wbdiscover import console_encodings  # noqa: E402

HERE = Path(__file__).resolve().parent          # .../bin
PKG_ROOT = HERE.parent                           # .../codex-workbuddy-bridge
PROVIDER = "wbbridge"
PROFILE_NAME = "wbb"
MARK_BEGIN = "<!-- BEGIN codex-workbuddy-bridge -->"
MARK_END = "<!-- END codex-workbuddy-bridge -->"

PROVIDER_BLOCK = f"""[model_providers.{PROVIDER}]
name = "WorkBuddy"
base_url = "http://127.0.0.1:{{port}}/v1"
# The local proxy does not validate the key; a placeholder is fine (real auth lives in the proxy)
experimental_bearer_token = "wbbridge-local"
wire_api = "responses"
requires_openai_auth = false
# Upstream returns 429s; allow plenty of backoff retries so Codex does not give up early
request_max_retries = 6
stream_max_retries = 6
"""

PROFILE_BODY = f"""# Codex profile: route through WorkBuddy (local proxy)
#
# Usage: codex --profile {PROFILE_NAME}
# Requires the proxy to be running -> scripts\\start-wbbridge.cmd (or scripts\\codex-wbb.cmd, which starts it)
# The provider itself lives in [model_providers.{PROVIDER}] in config.toml

model_provider = "{PROVIDER}"
model = "deepseek-v4.1-flash"
model_reasoning_effort = "low"
"""

AGENTS_RULE = f"""{MARK_BEGIN}
## Always check the live model (never answer from context)

When asked an identity question such as "current model / which model are you using / did you switch models":

**You must run the command below right now. Never answer from chat history or memory.**

```bash
curl -s http://127.0.0.1:{{port}}/wb/last-model
```

Rules:

- Report the `model` field (what the proxy actually forwards upstream -- the only authority).
- `config_model` is merely "the default for new sessions" and is not the model in use here.
- **A model cannot introspect its own identity**: model names seen in context, or your own previous
  answer, are all unreliable -- the user can switch models mid-session and history shows no trace.
- Run the command again every time; never reuse a value obtained earlier in the session.
{MARK_END}
"""


# ── Basic helpers ───────────────────────────────────────────
def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def proxy_python() -> str:
    """Pick a python that has cryptography; fall back to the current interpreter."""
    return wbdiscover.find_python() or sys.executable


def port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_proxy(port: int, wait: int = 25) -> bool:
    """Start the proxy if it is not running and wait for the port to open."""
    if port_open(port):
        return True
    script = HERE / "wbproxy.py"
    if not script.is_file():
        print(f"  cannot find {script}")
        return False
    print(f"  starting proxy: {script}")
    log = Path(os.environ.get("TEMP", ".")) / "wbbridge-proxy.log"
    try:
        # Must use wbspawn: a venv python.exe is a uv stub that does not forward
        # creationflags, so the real interpreter pops up a visible console
        # (measured: MainWindowHandle != 0).
        wbspawn.spawn_detached([wbspawn.no_console_python(), str(script), "--port", str(port)],
                               log=log)
    except OSError as e:
        print(f"  failed to start: {e}")
        return False
    for _ in range(wait * 2):
        time.sleep(0.5)
        if port_open(port):
            return True
    print(f"  proxy not ready after {wait}s; log: {log}")
    return False


def backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    dst = path.with_name(path.name + time.strftime(".bak-wbbridge-%H%M%S"))
    shutil.copy2(path, dst)
    return dst


# ── Read/write text while preserving the original line endings ──
# Codex writes CRLF on Windows. Default text-mode I/O would convert every line
# ending to LF, which shows up as "the whole file changed" in a diff -- so read
# and write with newline="" to keep the bytes intact.
def read_text_keep(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text_keep(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def to_newline(text: str, nl: str) -> str:
    """Normalise text to the given line ending (collapse to \\n first, then replace)."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", nl)


# ── Insert / remove TOML sections (line-based, keeping all other content) ──
def strip_section(text: str, header: str) -> str:
    """Remove the [section] named header (plus its body, up to the next [ or EOF)."""
    lines = text.splitlines(keepends=True)
    out, i, n = [], 0, len(lines)
    removed = False
    while i < n:
        if lines[i].strip() == header:
            removed = True
            i += 1
            while i < n and not lines[i].lstrip().startswith("["):
                i += 1
            # any comment block left above it is handled by the next pass
            continue
        out.append(lines[i])
        i += 1
    return "".join(out) if removed else text


def upsert_section(text: str, header: str, body: str) -> str:
    """Replace or append a [section] block.

    Must be idempotent: running it twice on the same input yields identical bytes.
    An earlier version unconditionally added a separator blank line before appending
    while strip_section never removed the previous one, so every install added a
    blank line (measured 159 -> 160 -> 161 lines). Here we strip trailing blank
    lines first and then add exactly one separator.
    """
    nl = detect_newline(text)
    text = strip_section(text, header)
    head = text.rstrip("\r\n")
    body_out = to_newline(body, nl).rstrip("\r\n")
    if not head:
        return body_out + nl
    return head + nl + nl + body_out + nl


def upsert_top_key(text: str, key: str, value: str) -> str:
    """Set or append a **top-level** key (it must come before the first [section]).

    Codex only honours a top-level model_catalog_json; inside a section it is
    ignored and the desktop model list falls back to the built-in OpenAI catalog
    (no WorkBuddy models at all).
    """
    nl = detect_newline(text) or "\n"
    lines = text.splitlines(keepends=True)
    # Windows paths are full of backslashes: a TOML "..." basic string treats them
    # as escapes (`\U` is an outright error). Use a '...' literal string instead
    # (a path never contains a single quote).
    literal = "'" + value + "'" if "'" not in value else '"' + value.replace("\\", "\\\\") + '"'
    new_line = f"{key} = {literal}{nl}"
    insert_at = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            insert_at = i
            break
        if s.startswith(key + " ") or s.startswith(key + "="):
            lines[i] = new_line
            return "".join(lines)
    if insert_at == len(lines) and lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    # When a [section] follows, add a blank line so it reads like a hand-written config
    if insert_at < len(lines):
        new_line += nl
    lines.insert(insert_at, new_line)
    return "".join(lines)


# ── Steps ───────────────────────────────────────────────────
def step_provider(home: Path, port: int) -> Path | None:
    cfg = home / "config.toml"
    if not cfg.is_file():
        print(f"  !! cannot find {cfg}; install Codex and run it at least once first")
        return None
    bak = backup(cfg)
    text = read_text_keep(cfg)
    text = upsert_section(text, f"[model_providers.{PROVIDER}]", PROVIDER_BLOCK.format(port=port))
    write_text_keep(cfg, text)
    print(f"  wrote [model_providers.{PROVIDER}]" + (f" (backup: {bak.name})" if bak else ""))
    return cfg


def step_profile(home: Path) -> Path | None:
    prof = home / f"{PROFILE_NAME}.config.toml"
    if prof.is_file():
        bak = backup(prof)
        print(f"  {prof.name} already exists" + (f"; backup: {bak.name}" if bak else ""))
    write_text_keep(prof, to_newline(PROFILE_BODY, "\r\n"))
    print(f"  wrote profile: {prof.name} (use: codex --profile {PROFILE_NAME})")
    return prof


def step_catalog(home: Path, port: int, force_catalog: bool = False) -> Path | None:
    out = home / "wbbridge-catalog.json"
    gen = HERE / "gen-catalog.py"
    r = subprocess.run([proxy_python(), str(gen), "--port", str(port), "--out", str(out), "--quiet"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(f"  catalog generation failed: {(r.stderr or r.stdout).strip()[:300]}")
        return None
    print(f"  generated model catalog: {out.name}")

    # The catalog exists, but Codex must be told where it is or the desktop app shows nothing.
    cfg = home / "config.toml"
    if cfg.is_file():
        text = read_text_keep(cfg)
        if 'model_catalog_json' not in text:
            bak = backup(cfg)
            write_text_keep(cfg, upsert_top_key(text, "model_catalog_json", str(out)))
            print("  wrote top-level model_catalog_json to config.toml"
                  + (f" (backup: {bak.name})" if bak else ""))
        else:
            cur = ""
            for ln in text.splitlines():
                if ln.strip().startswith("model_catalog_json"):
                    cur = ln.split("=", 1)[1].strip().strip("'\"")
                    break
            if Path(cur) == out:
                print("  config.toml model_catalog_json already points at this plugin")
            elif force_catalog:
                bak = backup(cfg)
                lines = []
                for ln in text.splitlines(keepends=True):
                    if ln.strip().startswith("model_catalog_json"):
                        nl = "\r\n" if ln.endswith("\r\n") else "\n"
                        ln = f"model_catalog_json = '{out}'{nl}"
                    lines.append(ln)
                write_text_keep(cfg, "".join(lines))
                print("  repointed model_catalog_json at this plugin"
                      + (f" (backup: {bak.name})" if bak else ""))
            else:
                print(f"  !! config.toml model_catalog_json points elsewhere: {cur}")
                print("     It may belong to another plugin of the same kind.")
                print("     Only one catalog can win; to make the desktop app show this plugin's list,")
                print(f"     change that line to: model_catalog_json = '{out}'")
    return out


def step_skill(home: Path) -> None:
    src = PKG_ROOT / "skills" / "wbbridge-current-model"
    if not src.is_dir():
        print("  package has no skills/wbbridge-current-model, skipping")
        return
    dst = home / "skills" / "wbbridge-current-model"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"  installed skill: {dst}")


def step_agents_rule(home: Path, port: int) -> None:
    agents = home / "AGENTS.md"
    text = read_text_keep(agents) if agents.is_file() else ""
    nl = detect_newline(text) if text else "\r\n"
    rule = to_newline(AGENTS_RULE.format(port=port), nl).rstrip("\r\n")
    if MARK_BEGIN in text:                      # already installed -> drop the old block first so upgrades are clean
        text = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\r?\n?",
                      "", text, flags=re.S)
    # Same as upsert_section: drop the separator left by the previous run, then add
    # exactly one. Otherwise every install adds a blank line (measured 65a66 / 66a67).
    text = text.rstrip("\r\n")
    if text:
        text += nl + nl
    bak = backup(agents) if agents.is_file() else None
    write_text_keep(agents, text + rule + nl)
    print("  added the live-model rule to AGENTS.md" + (f" (backup: {bak.name})" if bak else ""))


def step_watch(enable: bool) -> None:
    """Register the plugin-state watchdog for logon autostart.

    This is what a hook cannot cover: a hook fires once per session start, so it
    cannot handle "the proxy died mid-session", "the machine rebooted and no
    session has started yet", or "not using Codex at all".
    """
    watcher = HERE / "watch_plugin.py"
    if not watcher.is_file():
        print("  skipping watchdog (bin/watch_plugin.py not found)")
        return
    exe = proxy_python()
    pyw = Path(exe).with_name("pythonw.exe")
    runner = str(pyw) if pyw.is_file() else exe
    action = "install-autostart" if enable else "remove-autostart"
    r = subprocess.run([exe, str(watcher), f"--{action}"],
                       capture_output=True, text=True, errors="replace")
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        print(f"  watchdog autostart failed (exit {r.returncode}): {(out or r.stderr or '').strip()[:200]}")
        print(f"  retry manually: {runner} \"{watcher}\" --{action}")
        return
    if enable:
        print("  watchdog autostart installed: proxy runs while the plugin is active and stops when it is not")
    else:
        print("  watchdog autostart removed")


def step_models_note(port: int) -> None:
    """List the available models so the user can pick a default."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=20) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"  (cannot read the model list: {e})")
        return
    ids = [m.get("id") for m in (doc.get("data") or []) if m.get("id")]
    free = {"hy3", "step-5-preview", "deepseek-v4.1-flash"}
    print(f"  {len(ids)} models available; verified free tier: {', '.join(sorted(free))}")
    print(f"  to change the default model, edit model = \"...\" in {codex_home() / (PROFILE_NAME + '.config.toml')}")


# ── Commands ────────────────────────────────────────────────
def cmd_install(args) -> int:
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    print(f"installing into {home}")
    print(f"package dir {PKG_ROOT}")

    if not args.skip_doctor:
        print("\n[0/6] environment check")
        bad = [c for c in wbdiscover.diagnose(args.port) if not c[1]]
        for name, ok, detail in wbdiscover.diagnose(args.port):
            print(f"  [{'OK ' if ok else '!! '}] {name}: {detail}")
        # A stopped proxy is not fatal (the next step starts it); other failures must be fixed first
        fatal = [c for c in bad if "port" not in c[0].lower()]
        if fatal:
            print("\n  Fix the items marked !! above, then re-run install.cmd.")
            if not args.force:
                return 1

    print("\n[1/6] make sure the local proxy is up (the catalog needs it)")
    if not ensure_proxy(args.port):
        print("  proxy unavailable. Start and sign in to the WorkBuddy AI desktop app, then retry.")
        return 1
    print("  OK")

    print("\n[2/6] write provider")
    if not step_provider(home, args.port):
        return 1

    print("\n[3/6] write profile")
    step_profile(home)

    print("\n[4/6] generate model catalog")
    step_catalog(home, args.port, args.take_catalog)

    print("\n[5/6] extras")
    if args.no_skill:
        print("  skipping skill install (--no-skill)")
    else:
        step_skill(home)
    if args.no_agents_rule:
        print("  skipping the AGENTS.md rule (--no-agents-rule)")
    else:
        step_agents_rule(home, args.port)

    step_models_note(args.port)

    if args.no_watch:
        print("  skipping watchdog autostart (--no-watch)")
    else:
        step_watch(True)

    print("\nDone. Usage:")
    print(f"  {PKG_ROOT / 'scripts' / 'codex-wbb.cmd'}                 # TUI (starts the proxy automatically)")
    print(f"  {PKG_ROOT / 'scripts' / 'codex-wbb.cmd'} exec \"task\"      # non-interactive")
    print(f"  codex --profile {PROFILE_NAME}                            # if Codex is already installed")
    print("\nNote: switching models in the desktop app does not write back to config.toml; the proxy syncs it.")
    print("      Existing sessions keep the model they were created with; start a new chat for the new default.")
    return 0


def cmd_uninstall(args) -> int:
    home = codex_home()
    print(f"uninstalling from {home}")
    cfg = home / "config.toml"
    if cfg.is_file():
        bak = backup(cfg)
        text = strip_section(read_text_keep(cfg), f"[model_providers.{PROVIDER}]")
        # Removing a block often leaves repeated blank lines; collapse to a single trailing newline
        nl = detect_newline(text) or "\n"
        text = re.sub(r"(\r?\n){3,}", nl + nl, text).rstrip("\r\n") + nl
        write_text_keep(cfg, text)
        print(f"  removed [model_providers.{PROVIDER}]" + (f" (backup: {bak.name})" if bak else ""))

    prof = home / f"{PROFILE_NAME}.config.toml"
    if prof.is_file():
        prof.unlink()
        print(f"  deleted {prof.name}")

    cat = home / "wbbridge-catalog.json"
    if cfg.is_file():
        cur = read_text_keep(cfg)
        if "wbbridge-catalog.json" in cur:
            bak = backup(cfg)
            kept = [ln for ln in cur.splitlines(keepends=True)
                    if not (ln.strip().startswith("model_catalog_json")
                            and "wbbridge-catalog.json" in ln)]
            write_text_keep(cfg, "".join(kept))
            print("  removed model_catalog_json from config.toml"
                  + (f" (backup: {bak.name})" if bak else ""))
    if cat.is_file():
        cat.unlink()
        print(f"  deleted {cat.name}")

    dst = home / "skills" / "wbbridge-current-model"
    if dst.is_dir():
        shutil.rmtree(dst)
        print(f"  deleted skill {dst}")

    agents = home / "AGENTS.md"
    if agents.is_file() and MARK_BEGIN in read_text_keep(agents):
        bak = backup(agents)
        cur = read_text_keep(agents)
        nl = detect_newline(cur)
        text = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\r?\n?",
                      "", cur, flags=re.S)
        write_text_keep(agents, text.rstrip("\r\n") + nl)
        print("  removed the AGENTS.md rule" + (f" (backup: {bak.name})" if bak else ""))

    step_watch(False)

    print("\nDone. Stop the proxy manually if it is still running; the package dir was not deleted.")
    return 0


def cmd_doctor(args) -> int:
    """Environment check: installs nothing, only reports what is missing and how to fix it."""
    home = codex_home()
    print(f"CODEX_HOME : {home}")
    print(f"package dir : {PKG_ROOT}")
    print()
    rows = wbdiscover.diagnose(args.port)
    width = max(len(r[0]) for r in rows)
    for name, ok, detail in rows:
        print(f"  [{'OK ' if ok else '!! '}] {name:<{width}}  {detail}")

    bad = [r for r in rows if not r[1] and "port" not in r[0].lower()]
    print()
    if bad:
        print("Needs attention:")
        for name, _, _ in bad:
            tip = {
                "Python >=3.10": "install Python 3.10+ (tick Add python.exe to PATH), or set WBBRIDGE_PYTHON",
                "cryptography": 'run with that python: python -m pip install cryptography',
                "WorkBuddy desktop app": "install the WorkBuddy AI desktop app (international build) and sign in; or set WBBRIDGE_APP",
                "credentials": "start the desktop app and sign in; the credential file is written automatically",
                "Codex CLI": "npm i -g @openai/codex, or set WBBRIDGE_CODEX",
            }.get(name, "")
            if tip:
                print(f"  - {name}: {tip}")
        return 1
    print("Environment ready; you can run install.cmd")
    return 0


def cmd_status(args) -> int:
    home = codex_home()
    cfg = home / "config.toml"
    text = read_text_keep(cfg) if cfg.is_file() else ""
    print(f"CODEX_HOME          : {home}")
    print(f"provider block      : {f'[model_providers.{PROVIDER}]' in text}")
    print(f"profile file        : {(home / (PROFILE_NAME + '.config.toml')).is_file()}")
    print(f"model catalog       : {(home / 'wbbridge-catalog.json').is_file()}")
    print(f"skill               : {(home / 'skills' / 'wbbridge-current-model').is_dir()}")
    ag = home / "AGENTS.md"
    print(f"AGENTS.md rule      : {ag.is_file() and MARK_BEGIN in read_text_keep(ag)}")
    print(f"proxy {args.port} running : {port_open(args.port)}")
    startup = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup" / "codex-workbuddy-bridge-watch.vbs")
    print(f"watchdog autostart  : {startup.is_file()}")
    if port_open(args.port):
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/wb/last-model", timeout=10) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            print(f"  model in use      : {d.get('model')}")
            print(f"  config default    : {d.get('config_model')} (synced={d.get('config_synced')})")
        except Exception as e:
            print(f"  (query failed: {e})")
    return 0


def main() -> int:
    console_encodings()
    ap = argparse.ArgumentParser(description="codex-workbuddy-bridge installer")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WBBRIDGE_PROXY_PORT", 8788)))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="install (idempotent)")
    p.add_argument("--no-skill", action="store_true", help="do not install the wbbridge-current-model skill")
    p.add_argument("--no-agents-rule", action="store_true", help="leave the global AGENTS.md untouched")
    p.add_argument("--skip-doctor", action="store_true", help="skip the environment check")
    p.add_argument("--force", action="store_true", help="keep installing even if the environment check fails")
    p.add_argument("--no-watch", action="store_true", help="do not install the watchdog autostart")
    p.add_argument("--take-catalog", action="store_true",
                   help="repoint config.toml model_catalog_json at this plugin (left alone if it points elsewhere)")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="uninstall (keeps the package dir and backups)")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("status", help="show install state and the model actually in use")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="environment check (installs nothing)")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

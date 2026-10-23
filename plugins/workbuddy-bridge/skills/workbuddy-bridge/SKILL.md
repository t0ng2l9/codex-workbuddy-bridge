---
name: workbuddy-bridge
description: >-
  Connect the models you are signed in to in the WorkBuddy AI desktop app to Codex
  (CLI and desktop app), or troubleshoot that bridge. Use when the user wants to use
  WorkBuddy/CodeBuddy models in Codex, asks about the workbuddy model provider, the
  local proxy on 127.0.0.1:8788, the free WorkBuddy models
  (hy3 / deepseek-v4.1-flash / step-5-preview), the /wbbridge-setup or /wbbridge-status
  commands, hook trust, the proxy watcher/autostart, model-switching not taking effect,
  or 429 rate limits from the WorkBuddy upstream.
metadata:
  short-description: WorkBuddy models in Codex
---

# Codex x WorkBuddy Bridge

Let Codex use the WorkBuddy AI desktop session as its model source - no OpenAI
subscription and no Codex plan required. Codex keeps its agent scaffolding (file
reads and writes, running commands, apply_patch, multi-turn tool loops) and only
the models come from WorkBuddy.

```
Codex --> 127.0.0.1:8788 (local proxy) --> workbuddy.ai --> real models
             |  decrypts the desktop session
             |  Responses <-> Chat Completions translation
             |  records / syncs the model actually in use
             |  generates the model picker catalog
```

## Where the plugin lives / how to find it

Codex **copies** the plugin into its cache, so edits in the source tree do not
take effect on their own:

```
%USERPROFILE%\.codex\plugins\cache\<marketplace>\<plugin>\<version>\
```

Every `<plugin>` below must be the real location. Locate it first:

```cmd
dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge"
```

Install / update (`marketplace add` is needed only once):

```cmd
codex plugin marketplace add zetongliu219-alt/workbuddy-bridge   :: or a local dir / unzipped zip
codex plugin add workbuddy-bridge@wbbridge
python "<plugin>\bin\setup.py" doctor
python "<plugin>\bin\setup.py" install
```

**Codex caches by version number** - after changing plugin content, bump the
version before reinstalling or nothing changes:

```cmd
python "<plugin>\bin\bump-plugin.py" "<plugin dir>"
codex plugin add workbuddy-bridge@wbbridge
```

Warning: before reinstalling make sure **no process is using the plugin directory
as its working directory** (you `cd`-ed into it, or the proxy/watchdog is still
running), otherwise you get
`failed to back up plugin cache entry: Access is denied (os error 5)`.
Stop the proxy and the watchdog first.

## Files at a glance

| Path | Purpose |
|---|---|
| `install.cmd` / `uninstall.cmd` / `status.cmd` / `doctor.cmd` | install / remove / query / check |
| `bin\wbproxy.py` | the local proxy itself (credential decryption, request rewriting, SSE translation, model sync) |
| `bin\wbresponses.py` | Responses API <-> Chat Completions adapter |
| `bin\wbdiscover.py` | path auto-discovery (desktop app / Python / Codex / credentials) |
| `bin\gen-catalog.py` | generates the model picker catalog `wbbridge-catalog.json` |
| `bin\setup.py` | installer/uninstaller (provider, profile, catalog, skill, AGENTS rule, watchdog autostart) |
| `bin\hook_session_start.py` | the SessionStart hook implementation |
| `bin\wbspawn.py` | start background processes with no console window (a venv python.exe is a uv stub that drops the flags) |
| `bin\watch_plugin.py` | **watchdog**: start/stop the proxy according to the plugin state |
| `bin\bump-plugin.py` | bumps the cachebuster version when updating the plugin |
| `scripts\codex-wbb.cmd` | launch Codex (starts the proxy if needed) |
| `scripts\start-wbbridge.cmd` | start only the proxy (`--verbose` for request traces) |
| `scripts\watch-bridge.cmd` | watchdog: `--status` / `--once` / `--install-autostart` / `--remove-autostart` |
| `skills\` | this skill + `wbbridge-current-model` |
| `commands\` | `/wbbridge-setup`, `/wbbridge-status` (**note**: in this Codex version `plugin/read` does not enumerate commands, so these may not be exposed; see the equivalent CLI below) |

## Common operations (the reliable path)

Plugin commands (`/wbbridge-setup` and friends) **are not exposed in some Codex
versions**, so use the scripts directly day to day:

```cmd
python "<plugin>\bin\setup.py" doctor        :: environment check
python "<plugin>\bin\setup.py" install       :: install (idempotent)
python "<plugin>\bin\setup.py" status        :: install state
python "<plugin>\bin\watch_plugin.py" --status   :: watchdog verdict
```

## Proxy lifecycle: two layers

**You never start the proxy by hand.** Two complementary layers:

1. **hook (per session)** - `SessionStart`: make sure the proxy runs when a new
   session starts.
2. **watchdog (resident)** - `install.cmd` registers it as a **logon autostart**;
   it reconciles every 5 seconds: **plugin active -> proxy running; plugin
   disabled/uninstalled -> proxy stopped** (measured to follow within 4 seconds).

| Situation | Handled by |
|---|---|
| New CLI session / new desktop chat | hook |
| After a reboot | watchdog |
| Proxy dies inside an open session | watchdog (self-heals in seconds) |
| Disabling the plugin in Codex | watchdog (stops the proxy) |
| Not using Codex but wanting it up | watchdog |

### The hook must be trusted once, or it never runs

While untrusted its state is **`untrusted`** and **it will not run** - which is
the most common cause of "the proxy never came up and the desktop app silently
stopped working". Asking the app-server (`hooks/list`) shows:

```
key:         <plugin>@<market>:hooks/hooks.json:session_start:0:0
enabled:     True
trustStatus: untrusted        <- this is the problem
```

**Desktop app**: Settings -> **Hooks** -> under the **From Plugins** source group
find this plugin entry `SessionStart` (shown as *Disabled until hook is trusted*
when untrusted) -> click **Trust all**.

**TUI**: a new session shows *Hooks need review ... [Trust all and continue]*;
pick it.

**CLI / automation**: `codex exec --dangerously-bypass-hook-trust ...`

Trust is stored in `config.toml` under `hooks.state.<key>.trusted_hash`; **after
editing the hook you must trust it again** (*Hook changed since last trusted*).
If it says *Another config layer overrides these hook settings*, a
higher-priority config layer is in the way.

### Watchdog commands and logs

```cmd
python "<plugin>\bin\watch_plugin.py" --status
```

| Flag | Effect |
|---|---|
| (none) | resident watchdog |
| `--status` | print the verdict: plugin active? proxy running? in sync? |
| `--once` | reconcile once |
| `--install-autostart` | install logon autostart (VBS in the Startup folder, no admin) |
| `--remove-autostart` | remove logon autostart |

Log: `%TEMP%\wbbridge-watch.log` (state changes and errors only). Autostart file:
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\workbuddy-bridge-watch.vbs`.

The watchdog **never kills an unrelated program**: before stopping the proxy it
checks that the command line contains the `wbproxy.py` from this package; if the
port is held by another program it reports `foreign-port` and does nothing.

## Usage

```cmd
codex --profile wbb                        new sessions go through WorkBuddy
<plugin>\scripts\codex-wbb.cmd             equivalent, and starts the proxy if needed
<plugin>\scripts\codex-wbb.cmd -m hy3      switch model
```

Same for the desktop app (ChatGPT.exe / Codex app), but **you must start a new
session** - the provider and model are **bound per session**; old sessions
remember the settings they were created with and do not follow config changes.

## Free tiers (measured at 0 credits)

`hy3`, `deepseek-v4.1-flash`, `step-5-preview`.

Warning: `hy4-preview` **has "preview" in the name but is billed** (measured
0.02) - never judge by name. The default is `deepseek-v4.1-flash` (largest free
context: 300k / 128k output).

## "Which model is in use" must be checked live

A model **cannot introspect its own identity**, and `model` in `config.toml` is
only the **default for new sessions** - switching in the picker inside a session
is a **runtime override that is never written back**. So do not read the config;
ask the proxy:

```cmd
curl -s http://127.0.0.1:8788/wb/last-model
```

The proxy is the only party that sees the real `model` field. `/health` also
carries `last_model`. See the `wbbridge-current-model` skill in this plugin for
details.

## Upstream 429 (`exceeded retry limit, last status: 429`)

**Upstream per-account rate limiting**, not a proxy bug. The body is `code 14003`
and upstream says "wait about a minute, or switch to a less loaded model". 429s
are not billed.
Measured: eight consecutive requests already alternate `200,429,200,429...` - it
tracks overall account activity, not how fast you send.
The proxy log already contains a readable summary by default (no `--verbose`
needed):

```
[wbproxy] upstream error 429: code=14003 / too many requests / wait about a minute and retry, or switch to a less loaded model.
```

## Troubleshooting order

1. **Is the proxy up**: `curl -s http://127.0.0.1:8788/health`
2. If not, check the **watchdog** (`watch_plugin.py --status`); if it is not
   installed use `--install-autostart`, or `--once` as an emergency reconcile
3. Watchdog says "inactive" -> enable the plugin in Codex (`[plugins."..."] enabled` in `config.toml`)
4. It will not start -> `doctor.cmd`; is the desktop app missing, the credential
   file missing, or cryptography missing?
5. It starts but errors -> read `%TEMP%\wbbridge-proxy.log`
6. 429 -> wait a minute or switch models
7. Desktop app shows no WorkBuddy models -> the catalog `model_catalog.json` was
   not generated correctly; re-run `install.cmd`
8. Old desktop sessions do not follow -> **start a new session**
9. Want "new session starts the proxy" to work too -> **trust the hook** as above

## Caveats

The upstream is a **private, reverse-engineered API**, and bypassing its system
prompt fingerprint is active evasion of their risk controls.
**Small-scale use among people you know is fine; do not distribute publicly,
publish a package, or write a tutorial.**
Every user signs in with their own account; never pass around credentials
belonging to other people.

# codex-workbuddy-bridge

Turns the models you are already signed in to in the **WorkBuddy AI desktop app**
into the model source for **Codex (CLI / desktop app)**.

No OpenAI subscription and no Codex plan - Codex keeps its agent scaffolding
(reading and writing files, running commands, apply_patch, multi-turn tool
loops) and the models come from WorkBuddy (GPT-6 / GPT-5.6 / GLM / Kimi / Gemini
/ Grok / Hy3 ...), including three free tiers that measured **0 credits**:
`hy3`, `deepseek-v4.1-flash` and `step-5-preview`.

```
Codex --> 127.0.0.1:8788 (local proxy) --> workbuddy.ai --> real models
             |  decrypts the desktop app session
             |  Responses <-> Chat Completions translation
             |  records / syncs the model actually in use
             |  generates the model picker catalog
```

---

## Installing as a Codex plugin

This package is a **Codex plugin** that lives under `plugins/` in a marketplace
repository. Installing the plugin only lays down the files; you **still have to
run the installer once** so the provider is written into the Codex config.

```cmd
:: 1. add the marketplace (GitHub; use a local path for a folder or an unzipped zip)
codex plugin marketplace add t0ng2l9/codex-workbuddy-bridge

:: 2. install the plugin
codex plugin add codex-workbuddy-bridge@wbbridge

:: 3. find where the plugin actually landed (Codex materialises it into the cache)
dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\codex-workbuddy-bridge"

:: 4. check the environment, then install (idempotent)
python "<plugin root>\bin\setup.py" doctor
python "<plugin root>\bin\setup.py" install
```

Codex can also run step 4 itself: this plugin ships **`/wbbridge-setup`** and
**`/wbbridge-status`** commands. **On Codex 0.153 these are not exposed** though
(`plugin/read` does not enumerate commands), so treat running the script
directly as the reliable path and the commands as a convenience for newer builds.

The plugin exposes three surfaces:

| Surface | Contents |
|---|---|
| `skills/` | `codex-workbuddy-bridge` (usage / troubleshooting), `wbbridge-current-model` (answers "current model" correctly) |
| `commands/` | `/wbbridge-setup`, `/wbbridge-status` |
| `hooks/hooks.json` | `SessionStart`: start the proxy if it is not running |

### The hook must be trusted once (**required, or the proxy never autostarts**)

Codex does not run hooks automatically. **While a hook is untrusted its state is
`untrusted` and it never runs** - the symptom is "the proxy never comes up, so
the desktop app silently cannot connect". To check the current state:

```cmd
:: ask the app-server, or just read the desktop-app result below
```

**Desktop app**: open **Settings -> Hooks**. The page and its wording were
confirmed from the app bundle: `settings.nav.hooks`, the subtitle *"Manage
lifecycle hooks from config and enabled plugins"*, a **From Plugins** source
group, and untrusted hooks shown as **"Disabled until hook is trusted"**. Find
this plugin's entry (`SessionStart`) and click **Trust all**.

**TUI**: a new session shows

> Hooks need review - Hooks can run outside the sandbox after you trust them.
> [Trust all and continue] [Continue without trusting (hooks won't run)]

Pick **Trust all and continue**.

**Automation / no clicking**: `codex exec --dangerously-bypass-hook-trust ...`
(equivalent to bypassing trust on every call).

Trust is stored in `config.toml` under `hooks.state.<hook key>.trusted_hash`
(where `<hook key>` looks like
`codex-workbuddy-bridge@wbbridge:hooks/hooks.json:session_start:0:0`).
**After editing the hook you get "Hook changed since last trusted" and must
trust it again.** If you see "Another config layer overrides these hook
settings", a higher-priority layer (admin / project config) is winning and has to
be dealt with first.

### Two layers: hook (per session) + watchdog (resident)

A hook alone leaves three gaps, because `SessionStart` means **"When a new
session starts"**:

| Situation | Hook only | With the watchdog |
|---|---|---|
| New CLI session / new desktop chat | yes | yes |
| First session after a reboot | yes | yes |
| **Proxy dies inside an open session** | no, never self-heals | yes, recovers within seconds |
| **Not using Codex, just want the proxy up** | no | yes |
| **Disabling the plugin in Codex** | no, proxy keeps running | yes, proxy is stopped |

The watchdog (`bin/watch_plugin.py`) is exactly **"start/stop the proxy according
to the plugin's enabled state"**:

- **Plugin active** (`[plugins."codex-workbuddy-bridge@wbbridge"] enabled = true` in
  `config.toml` and the plugin is installed) -> keep the proxy running
- **Plugin inactive** (`enabled = false`, or uninstalled) -> stop the proxy

`install.cmd` registers it as a **logon autostart** (a VBS file in the Startup
folder, no admin rights needed). Disabling the plugin turns the proxy off and
enabling it turns the proxy on - you never manage the proxy separately.

Watchdog commands:

```cmd
<plugin>\scripts\watch-bridge.cmd --status            current verdict (active? running? in sync?)
<plugin>\scripts\watch-bridge.cmd --once              reconcile once
<plugin>\scripts\watch-bridge.cmd                     resident watchdog (Ctrl+C to exit)
<plugin>\scripts\watch-bridge.cmd --install-autostart install logon autostart
<plugin>\scripts\watch-bridge.cmd --remove-autostart  remove logon autostart
```

Log: `%TEMP%\wbbridge-watch.log`. Only state changes and errors are written;
normal polling produces nothing.

Safety: before stopping the proxy it checks whether the process command line
contains this package's `wbproxy.py`, so **another program on the port is never
killed by mistake**; the watchdog also holds a single-instance lock.

> **Two hard rules for hook commands** (both measured; read before editing):
> 1. Build an **absolute** path with `%CLAUDE_PLUGIN_ROOT%` - the hook's cwd is
>    the session directory, not the plugin root, so relative paths
>    (`./scripts/x.cmd`, `scripts\x.cmd`) always fail.
> 2. Keep **stdout empty** - Codex parses hook stdout as JSON, so printing
>    anything marks the hook Failed (even when the script did its job). Send
>    diagnostics to a log file (`%TEMP%\wbbridge-hook.log`).

### Reinstall / upgrade: the full procedure (measured working)

Codex **copies** the plugin by **version number** into
`plugins/cache/<market>/<plugin>/<version>/`, so "I changed the code" or "I need
to fix state" always requires a reinstall - it never reads the source directory.

**Step 0 - stop anything holding the cache directory** (otherwise the reinstall
fails with `failed to back up plugin cache entry: Access is denied (os error 5)`).
Both the watchdog and the proxy hold it:

```cmd
:: Kill by exact PID only; never match on name or command line
:: (that can hit unrelated programs, or the shell running the command)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'wbproxy|watch_plugin' } | ForEach-Object { $_.ProcessId }"
:: then kill each PID listed above:
taskkill /F /PID <PID>
```

**Step 1 - confirm the marketplace is registered** (once only; not needed on reinstall):

```cmd
codex plugin marketplace list
:: only if wbbridge is missing:
:: codex plugin marketplace add t0ng2l9/codex-workbuddy-bridge
```

**Step 2 - install / reinstall the plugin**:

```cmd
codex plugin add codex-workbuddy-bridge@wbbridge
```

> If the **version number did not change**, Codex reuses the existing cache and
> your edits do not take effect. When developing locally, bump first:
> ```cmd
> python "<plugin dir>\bin\bump-plugin.py" "<plugin dir>"
> ```
> When pulling from GitHub, `git pull` is enough (the version is already bumped
> in the repo).

**Step 3 - run the bundled installer** (writes provider / profile / model
catalog / skill / AGENTS rule and registers the watchdog autostart):

```cmd
:: find the directory that was just materialised (it contains the version)
dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\codex-workbuddy-bridge"

python "<plugin dir>\bin\setup.py" doctor        :: environment check
python "<plugin dir>\bin\setup.py" install       :: install (idempotent)
```

**Step 4 - verify**:

```cmd
codex plugin list -m wbbridge                      :: should show installed, enabled + version
python "<plugin dir>\bin\setup.py" status          :: every line True
python "<plugin dir>\bin\watch_plugin.py" --status :: plugin active / proxy running / in sync
curl -s http://127.0.0.1:8788/health               :: ok:true
```

Common traps:

- **`codex plugin list` says `not installed` but the directory is there** -> the
  `[plugins."codex-workbuddy-bridge@wbbridge"]` entry vanished from the config; redo
  step 2. No need to `marketplace add` again.
- **The watchdog "does not work" after a reinstall** -> it was started from the
  **old** cache directory (which is now dangling). Run `install` again per step 3;
  it repoints the autostart at the new directory.
- **A console window appears** -> if it is `...\venv\Scripts\python.exe`, that is
  a bug from before 1.5.2; upgrade to 1.5.3+ (see "background processes never
  show a console window" below).
- **Hook trust survives a reinstall** (it lives in `hooks.state.*` in
  `config.toml`), so you normally do not need to trust again; if the hook content
  changed you do.

---


## Layout

```
codex-workbuddy-bridge/
|- install.cmd / uninstall.cmd / status.cmd / doctor.cmd   install / remove / query / check
|- .codex-plugin/plugin.json    plugin manifest (required for a Codex plugin)
|- bin/
|  |- wbproxy.py        local proxy: credential decryption, request rewriting, SSE translation, model sync
|  |- wbresponses.py    Responses API <-> Chat Completions adapter
|  |- wbdiscover.py     path and runtime auto-discovery (desktop app / Python / Codex / credentials)
|  |- gen-catalog.py    generates the Codex model picker catalog (wbbridge-catalog.json)
|  |- hook_session_start.py  SessionStart hook: make sure the proxy is running
|  |- bump-plugin.py    bumps the cachebuster version when updating the plugin
|  |- watch_plugin.py   watches the proxy based on the plugin state (start when active, stop when not)
|  |- wbspawn.py        starts Python processes in the background with no console window
|  `- setup.py          installer / uninstaller (provider, profile, catalog, skill, AGENTS rule)
|- commands/            /wbbridge-setup, /wbbridge-status
|- hooks/hooks.json     SessionStart (must be trusted once in Codex before it runs)
|- scripts/
|  |- codex-wbb.cmd      launcher: start the proxy if needed, then run Codex with the wbb profile
|  |- start-wbbridge.cmd start only the proxy
|  |- hook-session-start.cmd  hook entry point (must stay silent + use %CLAUDE_PLUGIN_ROOT%)
|  `- _findpy.cmd       called by the above: find a usable python
|- skills/
|  |- codex-workbuddy-bridge/   usage, free tiers, troubleshooting, caveats
|  `- wbbridge-current-model/       makes Codex answer "current model" correctly
|- templates/                    reference snippets (for manual setups)
`- dev/                          [local] diagnostic/test scripts (**not shipped in the repo**)
```

---

## Prerequisites

| Item | Requirement | Notes |
|---|---|---|
| **Windows** | - | Uses `CREATE_NO_WINDOW`, `%LOCALAPPDATA%`, and calls `.exe` files directly; not cross-platform |
| **WorkBuddy AI desktop app** | Installed **and signed in** | The credential source. **It does not need to stay running**; installed + signed in once is enough |
| **Python** | 3.10+ with `cryptography` | Used for AES-256-GCM decryption. `install.cmd` picks an interpreter that has `cryptography` |
| **Codex** | 0.153+ | See "Versions" below |

All four are **auto-discovered**; no paths need editing. Run the check once to confirm:

```cmd
doctor.cmd
```

```
  [OK ] Python >=3.10    C:\...\python.exe
  [OK ] cryptography     3.11|50.0.0
  [OK ] WorkBuddy desktop app  D:\...\WorkBuddyAI.exe
  [OK ] credentials      C:\...\workbuddy-desktop-ai.info (1 more available)
  [OK ] Codex CLI        C:\...\npm\codex.cmd
  [OK ] port 8788        free
```

The desktop app can live on any drive (discovery order: environment variable ->
registry uninstall entry -> running process -> common install paths).

---

## Install

```cmd
install.cmd
```

It does the following (**every step is idempotent**; re-running never changes
anything already in place):

0. environment check (Python / cryptography / desktop app / credentials / Codex / port); problems are listed with fixes
1. make sure the proxy is available (start it if not) - the catalog needs it
2. write `[model_providers.wbbridge]` into `<CODEX_HOME>\config.toml`
3. write `<CODEX_HOME>\wbb.config.toml` (the profile)
4. generate `<CODEX_HOME>\wbbridge-catalog.json` (the data source for the model picker)
5. install the `wbbridge-current-model` skill and add a "check the live model" rule to `AGENTS.md`

`CODEX_HOME` defaults to `%USERPROFILE%\.codex`. Every file is backed up before
being modified (`.bak-wbbridge-<timestamp>`).
Skip the extras / skip the check:

```cmd
install.cmd --no-skill --no-agents-rule
install.cmd --skip-doctor
```

Check the state:

```cmd
status.cmd
```

### Coexisting with other bridges

This package uses its own identifiers: port `8788`, provider `wbbridge`, profile
`wbb`, cache directory `plugins/cache/wbbridge/`, watchdog task
`codex-workbuddy-bridge-watch`. If another WorkBuddy bridge is installed on the same
machine, both can be installed and run at the same time without interfering.

The only thing that can "collide" is the top-level `model_catalog_json` in
`config.toml` - it holds a single value, so the desktop model list can only show
one plugin's catalog. Default behaviour:

- key **absent** -> the installer writes this plugin's catalog
- key **present and pointing elsewhere** -> the installer only warns and leaves it
  alone (so it never steals someone else's list)
- to force it over -> `python setup.py install --take-catalog`

---

## Installing on someone else's machine

**Prefer installing as a plugin** (see "Installing as a Codex plugin" above):
copy or push this repository (marketplace layout) to GitHub, have them run
`codex plugin marketplace add ...` + `codex plugin add codex-workbuddy-bridge@wbbridge`,
then `bin\setup.py install`.

You can also **copy the plugin directory directly**, bypassing Codex's plugin
mechanism, because **there are no paths to edit**: which drive the desktop app is
on, where Python lives, whether Codex came from npm or pnpm - all of it is
discovered at runtime by `bin/wbdiscover.py`, and the launchers share that logic.

What the recipient has to do:

1. install the **WorkBuddy AI desktop app (international build)** and sign in once
2. install **Python 3.10+** (tick Add to PATH), then
   `python -m pip install cryptography`
3. install **Codex CLI** (`npm i -g @openai/codex`)
4. run `doctor.cmd`, and once everything is OK run `install.cmd`

**Credentials are per-machine and per-account**: the package contains no account
information and no tokens. Every user must install the desktop app and sign in
themselves; the credentials are written on their own machine
(`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\`).

If discovery fails or you want to force a value, override with environment
variables (none set = fully automatic):

| Variable | Effect |
|---|---|
| `WBBRIDGE_APP` | full path to `WorkBuddyAI.exe` |
| `WBBRIDGE_AUTH_FILE` | full path to the credential file |
| `WBBRIDGE_PYTHON` | which python interpreter to use |
| `WBBRIDGE_CODEX` | which codex `codex-wbb.cmd` should use |

> The China-mainland build (`workbuddy-desktop.info`, CodeBuddy) talks to a
> different upstream and is **not supported**. When both credential files exist,
> the international `...-ai.info` wins. `doctor.cmd` shows which one is used.

---

## Usage

```cmd
scripts\codex-wbb.cmd                        interactive TUI (starts the proxy if needed)
scripts\codex-wbb.cmd exec "refactor utils.py"  non-interactive
scripts\codex-wbb.cmd -m hy3                 switch model
codex --profile wbb                          when Codex is already installed
```

The desktop GUI works too: you **must start a new session** (see "Models and
sessions" below).

---

## Models and billing

Measured rates (comparable requests, roughly 20-30k input tokens):

| Model | Credits | Per 1k input | Notes |
|---|---|---|---|
| `hy3` | **0** | 0 | free, 192k context / 64k output |
| `step-5-preview` | **0** | 0 | free, 300k / 64k |
| `deepseek-v4.1-flash` | **0** | 0 | **free, 300k / 128k (recommended default)** |
| `glm-5.3-flash` | 0.12 | 0.0050 | |
| `gpt-6-luna` | 0.38 | 0.0127 | |
| `gpt-6-sol` | 7.51 | 0.2502 | **20x the price of luna**, do not make it the default |

> Warning: `hy4-preview` has "preview" in the name but **is billed** (0.02).
> **Never judge by name**; measure.

The cost is reported in `usage.credit`; the proxy records it to
`%TEMP%\wbbridge-usage.jsonl` and also summarises it at `GET /wb/credits`
(remaining account credits).

---

## Endpoints exposed by the proxy

| Endpoint | Purpose |
|---|---|
| `GET /health` | account uid, token expiry, `last_model` / `last_model_ts` / `requests_seen` |
| `GET /wb/last-model` | the model **actually in use** (authoritative) + the configured default + whether they agree |
| `GET /wb/credits` | remaining account credits |
| `GET /v1/models` | model catalog (both the OpenAI-shaped `data` and the Codex-shaped `models`) |
| `POST /v1/chat/completions` | OpenAI-compatible (any OpenAI client) |
| `POST /v1/responses` | Responses API (the only form Codex 0.153+ supports) |

### Keeping the model state consistent

Codex has two model values and they are **very easy to confuse**:

| | Meaning | Stored in |
|---|---|---|
| `model` in `config.toml` | **default for new sessions** | on disk |
| what the picker shows | **runtime override for this session** | memory only, **never written back** |

That has two consequences, both handled:

1. **Stale config** -> on every forwarded request the proxy syncs the top-level
   `model` back into `config.toml` (only that line, atomic replace).
   `WBBRIDGE_SYNC_CONFIG=0` disables it.
2. **The model answering wrongly about its own identity** -> a model cannot
   introspect, it can only read files or repeat stale answers from context. The
   installer adds a hard rule to `AGENTS.md`: when asked for the current model it
   **must run** `curl -s http://127.0.0.1:8788/wb/last-model` right then and may
   not answer from memory.

> Cost: the configured default follows "the last model used". If you temporarily
> use something like `gpt-6-sol`, new sessions will default to it afterwards - set
> `WBBRIDGE_SYNC_CONFIG=0` or switch back in the picker to pin the default.

---

## Known traps (all of these were hit for real)

### Hard upstream constraints (cannot be bypassed, only adapted)

| Symptom | Cause | What the proxy does |
|---|---|---|
| `400 code 11101` | upstream does not support non-streaming | forces `stream:true`, aggregates locally, then replies |
| `400 code 11128` | **the first message must be `system`**; the `developer` role is **not accepted at all** | folds developer back into system |
| `400 code 11128` | **clients are identified by system-prompt fingerprint**. Codex's identity section triggers it. Note this is **not a keyword blocklist**: only three specific phrasings **together** trigger it; each one alone passes | replaces only the ~800-character identity preamble with a neutral description, keeping the remaining ~30k characters of behavioural instructions verbatim |

### On the Codex side

- **`wire_api = "chat"` was removed in 0.153**; only `responses` is accepted.
  That is why `wbresponses.py` exists.
- **Catalog schema**: Codex wants `{"models":[...]}` with entries using `slug` /
  `display_name` / `base_instructions` / `tool_mode` / `apply_patch_tool_type`
  ... - nothing like the OpenAI shape. Without fields such as `tool_mode`,
  **0.16x concludes "no tools available" and sends zero tools**, so the model
  says "there is no execution tool". `gen-catalog.py` fills them in.
- **The profile must be its own file** (since 0.162): `[profiles.x]` inside
  `config.toml` is treated as legacy and **errors out**. Hence
  `wbb.config.toml`.
- **Pin the version**: on some machines the first `codex` on `PATH` is an IDE's
  own build and `exec` fails with
  (`Failed to create unified exec process: helper_unknown_error: setup refresh had errors`).
  `codex-wbb.cmd` therefore **prefers the npm global install**;
  `WBBRIDGE_CODEX=codex` switches back to `PATH`.

### Windows scripts

- **`.cmd` files must be GBK (cp936) + CRLF.** As UTF-8, non-ASCII text under
  code page 936 **swallows the line ending**, merging the next line into the
  `rem` and producing bizarre errors like `'obreak' is not recognized as an
  internal or external command`.
- Calling `curl` / `ping` from a script needs the full
  `%SystemRoot%\System32\` path, otherwise git-bash's GNU versions win
  (`invalid time interval '/t'`); and **do not use `timeout.exe`** for waiting
  (it errors out when stdin is redirected) - use `ping -n 2 127.0.0.1 >NUL`.
- **PowerShell scripts must be pure ASCII**: 5.1 reads `.ps1` as ANSI/code page,
  so UTF-8 comments become mojibake and cause a **parse failure**.

### Rate limiting (429)

**This is application-level per-account rate limiting upstream, not CDN/WAF
blocking.** Full response body (captured by talking to upstream directly):

```json
{
  "code": 14003,
  "msg": "too many requests",
  "retry_policy": { "retry": true },
  "displayMsg":  { "zh": "Too many requests, please try again later." },
  "displayTips": { "zh": "Wait about a minute and retry, or switch to a less loaded model." }
}
```

Key points:

- **Upstream tells you what to do**: wait about a minute, or **switch to a less
  loaded model** (switching really does work - measured).
- **It is transient**: consecutive requests from one client alternate
  429/200 (measured 1->429, 2->200, 3->429).
- **Not billed**: 429 requests never reach usage accounting; credits are unchanged.
- **Cannot be reproduced on demand**: a stress test of 10 parallel lanes x 30
  calls (~900k tokens in 35 s) **did not trigger it**; it is computed from overall
  account activity, so load testing will not reproduce it.
- **Contributing factor**: **one account shared by several clients**. When the
  WorkBuddy desktop app is running (it refreshes tokens, syncs the catalog and
  pulls conversations on start) on top of Codex traffic through the proxy, that
  is when it happens most.
- Response headers include `Server: TencentEdgeOne` + `X-WAF-UUID` (all traffic
  goes through a CDN) but **no `X-RateLimit-*`** - the limit is invisible, you
  only find it by hitting it.

Mitigation:

- The provider already sets `request_max_retries = 6` / `stream_max_retries = 6`
  so Codex backs off several times.
- When you hit it, **wait a minute** or switch models (bounce between `hy3` and
  `deepseek-v4.1-flash`).
- Do not run agent tasks in the desktop app and in Codex at the same time.

**How it was diagnosed**: the proxy logs upstream errors together with the
response body (`upstream error 429: {...}`). That logging did not exist at first
and is what located `code 14003` - which is why all three `except UpstreamError`
sites log.

### Desktop GUI

- Model switches **only affect new sessions**: existing sessions remember the
  provider/model they were created with and keep using the old one (which shows
  up as endless upstream errors). **Start a new chat.**
- The desktop app uses its own bundled codex
  (`~/.codex/bin/<hash>/codex.exe`), **different from the CLI one**; a new binary
  has to be verified separately.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `wire_api = "chat" is no longer supported` | Codex is too new; it must use `responses` (already implemented here) |
| `400 code 11128 Illegal API invocation` | upstream fingerprint rejection. Confirm you are going through this proxy (which includes `sanitize_system`) |
| The model says "there is no execution tool" | catalog is missing `tool_mode` and friends -> re-run `gen-catalog.py` |
| Desktop model list only shows `gpt-6-*` / `daybreak-*` | `model_catalog_json` is unset or the file is missing -> re-run the installer |
| `--profile wbb cannot be used ... legacy [profiles.wb]` | the profile was written into `config.toml`; it belongs in `wbb.config.toml` |
| Stuck on "reconnecting N/N" | check the url in the error. Usually an **upstream/relay error** (e.g. a relay returning 402 no valid order), not the network |
| `exceeded retry limit, last status: 429` | upstream edge rate limiting, see above. Slow down; do not run the desktop agent at the same time |
| Wrong answer to "current model" | the model did not re-read the state. Confirm the `AGENTS.md` rule is installed, or query `/wb/last-model` directly |
| The proxy will not start | the WorkBuddy desktop app is not installed / not signed in; or the credential file moved (see below) |

### Logs and diagnosis

By default the proxy **logs only error-class events** (upstream errors, token
refreshes, credential problems) - the things you always need, with nothing to
configure:

```
[wbproxy] upstream error 429: code=14003 / too many requests / wait about a minute and retry, or switch to a less loaded model. / requestId=...
```

`--verbose` additionally logs request traces (model, message count, tool count
per request):

```cmd
scripts\start-wbbridge.cmd --verbose
```

**Client disconnects are normal** (user cancel, giving up mid 429 retry,
switching sessions); the proxy logs one line
`connection dropped (ConnectionResetError), ignoring` and keeps serving - no
traceback, no exit. On Windows these are `ConnectionAbortedError` (WinError
10053), not Linux's `BrokenPipeError`, so catching only the latter misses them.

The credential file is written by the desktop app under
`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\`
(international build: `workbuddy-desktop-ai.info`; mainland build:
`workbuddy-desktop.info`). It is **auto-discovered**; override with
`WBBRIDGE_AUTH_FILE` / `WBBRIDGE_APP`.
These are the two things that change most: after a desktop upgrade the binding
name or file name may differ, and the symptom is a proxy that will not start or
fails to decrypt.

---

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `WBBRIDGE_PROXY_PORT` | `8788` | proxy port |
| `WBBRIDGE_PROXY_HOST` | `127.0.0.1` | listen address (**never change to 0.0.0.0**, see security) |
| `WBBRIDGE_AUTH_FILE` | auto | credential file path |
| `WBBRIDGE_APP` | auto | `WorkBuddyAI.exe` path |
| `WBBRIDGE_PYTHON` | auto | which python the launchers use |
| `WBBRIDGE_SYNC_CONFIG` | `1` | disable config sync |
| `WBBRIDGE_CODEX_CONFIG` | `$CODEX_HOME/config.toml` | the config file to sync |
| `WBBRIDGE_USAGE_LOG` | empty | jsonl path for usage/credits |
| `WBBRIDGE_CODEX` | npm global codex | which codex the launcher uses |
| `WBBRIDGE_MODEL` | `deepseek-v4.1-flash` | model used by the local `dev/` scripts |
| `WBBRIDGE_ALLOW_PAID` | unset | set to `1` to let local `dev/` scripts run billed models |

(**Do not set** `PYTHONUTF8`/`PYTHONIOENCODING`: non-ASCII output then mismatches
the console code page and turns into mojibake. The scripts reconfigure their own
output to the console code page.)

---

## Uninstall

```cmd
uninstall.cmd
```

Removes the provider block, the profile, the catalog file, the skill and the
`AGENTS.md` rule (all with backups). The package directory itself is kept.

---

## Security and caveats

- **The proxy has no authentication** (it does not validate the key locally) and
  binds to `127.0.0.1` only. **Do not** change it to `0.0.0.0` or share it on the
  LAN - that would expose your account token to the whole network.
- The key comes from WorkBuddy's own Electron process; this package **never
  stores or prints** any token in plaintext.
- The upstream is a **private, reverse-engineered API**: a single rule change can
  break everything. Bypassing the system-prompt fingerprint (`code 11128`) is
  **active evasion of upstream risk controls**.
- **Every user signs in with their own account**: never copy your credential file
  to someone else (this package deliberately does nothing to make that easier).

---

## Diagnostic scripts in dev/ (local, not published)

These scripts are **not included in the published repository** (they contain
reverse-engineering and fingerprint-location material) and are only for local
debugging. **They all use the free tier by default**
(`deepseek-v4.1-flash`, measured 0 credits); use `WBBRIDGE_MODEL=hy3` and the
like to change that.

The scripts that actually cost credits (`_measure_*.py`, `_codex_bench.py`,
`_compare_codex.py`) **refuse to run by default**; you must opt in explicitly so
that nobody burns credits by casually running them:

```cmd
set WBBRIDGE_ALLOW_PAID=1
python dev\_measure_rate.py
```

| Script | Purpose |
|---|---|
| `_devcommon.py` | shared path constants (`BIN`/`TMP`/`WORK`) + cost guard; no hard-coded machine paths |
| `_test_proxy.py` / `_test_plugin.py` | end-to-end proxy tests; detects upstream 429 and retries once |
| `_probe_429.py` | capture the full 429 body straight from upstream (how `code 14003` was found) |
| `_stub_stream.py` | stub server: replays real SSE event streams to verify streaming and keep-alive |
| `_bisect_*.py` | bisect the upstream fingerprint trigger (`code 11128`) |
| `_measure_rate.py` / `_compare_codex.py` / `_codex_bench.py` | rate and Codex cost comparison <- billed |
| `_measure_gpt6.py` | credit rates for each GPT-6 tier <- billed |
| `_ttfb_matrix.py` | time-to-first-byte matrix (free tier by default; paid tiers only with `WBBRIDGE_ALLOW_PAID=1`) |

(None of the above are **in** the published repository.)

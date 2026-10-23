---
name: wbbridge-current-model
description: >-
  Resolve and report the model Codex is actually running against (slug, display
  name, provider, context window, max output, reasoning levels) by reading the
  active Codex config and provider model catalog. Use when the user asks "what
  model are you", "current model", "which model am I using", or to verify the
  effective model after switching providers/profiles. Do NOT use for generic
  model-selection advice, pricing, or API usage questions unrelated to this
  machine's live configuration.
metadata:
  short-description: Query the model Codex actually runs
---

# Query Current Model

The running assistant cannot introspect its own model identity, and **reading
config.toml is not enough**: switching the model inside a Codex session (desktop
model picker) is a *runtime override* that is never written back to config.toml.
The configured `model` is only the default for **new** sessions/threads.

So there are two different questions and two different sources:

| Question | Source |
|---|---|
| What will a **new** session use? | `config.toml` `model` key |
| What is **actually** being used right now? | the local proxy — it is the only component that sees the real `model` field on the wire |

## Authority for "right now": the proxy endpoint

```bash
curl -s http://127.0.0.1:8788/wb/last-model
# {"model": "hy3", "ts": 1791873063, "count": 42,
#  "config_model": "hy3", "config_synced": true, "sync_enabled": true}
```

`/health` carries `last_model`, `last_model_ts`, `requests_seen`.

**The proxy now also syncs config.toml**: every request it forwards rewrites the
top-level `model` key to whatever was actually requested. So `config_model` should
normally equal `model` (`config_synced: true`), and a config read gives the same
answer as the proxy. If they disagree (`config_synced: false`), trust `model` and
send a message to let the sync catch up.

If the proxy is down or no request has been made yet, fall back to config and say
so explicitly.

**Never** answer "which model are you" from your own intuition — a session-level
switch changes what is used without any on-disk trace until the next message.

## When to use
- User asks "what model are you / current model / which model am I using".
- After switching model in the desktop picker or in config, to confirm the change.
- Verifying `reasoning_effort`, context window, or max output tokens.

## How to run
Use the bundled PowerShell helper (Windows). It now queries the proxy first and
reports `effective_model` + `effective_source`, keeping the config value as
`config default`:

```powershell
pwsh -NoProfile -File "$env:CODEX_HOME/skills/wbbridge-current-model/scripts/Get-CurrentModel.ps1"
# raw JSON: add -Json
```

The script:
1. Queries `http://127.0.0.1:8788/wb/last-model` → `effective_model` (authority).
2. Reads `config.toml` (`model`, `model_provider`, `model_reasoning_effort`,
   `model_catalog_json`) → the **default for new sessions**.
3. Loads the catalog JSON for display name, context window, max output tokens and
   reasoning levels of the effective slug.
4. Flags `config_default_mismatch` when the two disagree, and best-effort inspects
   the running `codex.exe` command line for a `-m/--model` override.

## Reporting back
Lead with the **effective** model (display name + slug) and name its source
("proxy-observed"). Then give the config default. If they differ, say plainly that
the session was switched and config is only the default. Include context window,
max output, and reasoning effort when relevant. If the slug is NOT in the catalog,
say so and fall back to the raw slug. Do not guess a model the config does not name.

## Limitations
- `/wb/last-model` reflects the **last completed request**; before any traffic it is
  `null`. A stale value can persist after you switch, until the next message is sent —
  so right after a picker change, send a message before verifying.
- The endpoint belongs to the WorkBuddy proxy. With a different provider there is no
  such authority; report config + say it is only the default.
- Config path / catalog can be overridden with `-ConfigPath` / `-CatalogPath`.

See `references/wbbridge-config.md` for the specifics of this machine's WorkBuddy
provider layout (proxy, catalog JSON location, known slugs).

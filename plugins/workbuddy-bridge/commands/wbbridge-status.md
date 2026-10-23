---
description: Show the WorkBuddy bridge state - proxy, watchdog, hook readiness, the model actually in use, and credits
allowed-tools: [Bash, Read, Glob, Grep]
---

# WorkBuddy bridge status

## Steps

1. Install state (provider / profile / model catalog / skill / AGENTS rule / watchdog autostart):

```cmd
python "<plugin root>\bin\setup.py" status
```

2. Watchdog verdict - **is the plugin active, is the proxy running, do they agree**
   (this is the single most diagnostic line):

```cmd
python "<plugin root>\bin\watch_plugin.py" --status
```

3. Proxy health and the model **actually in use** (do not read config.toml instead):

```cmd
curl -s http://127.0.0.1:8788/health
curl -s http://127.0.0.1:8788/wb/last-model
```

4. Remaining credits:

```cmd
curl -s http://127.0.0.1:8788/wb/credits
```

5. Proxy not running: reconcile once, then check the logs if that fails.

```cmd
python "<plugin root>\bin\watch_plugin.py" --once
```

Logs: `%TEMP%\wbbridge-proxy.log` (proxy), `%TEMP%\wbbridge-watch.log` (watchdog).

> Find `<plugin root>` with:
> `dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge"`

## When reporting to the user

- **The `model` field from `/wb/last-model` is the only authority** (what the
  proxy actually forwards upstream).
- `config_model` is only the default for new sessions and is not the model in use
  here.
- A model cannot introspect its own identity: model names in context and your own
  previous answer are all unreliable.

## Symptom and likely cause

| Symptom | Usually |
|---|---|
| `--status` says out of sync | the watchdog is not running -> `--install-autostart`; or the plugin is disabled |
| `--status` says inactive | `[plugins."workbuddy-bridge@wbbridge"] enabled = false`, or it was uninstalled |
| Proxy runs but the desktop app still errors | check the upstream error line in `%TEMP%\wbbridge-proxy.log` (429 -> `code 14003`) |
| A new session does not start the proxy | the hook is untrusted (`trustStatus: untrusted`) -> desktop app Settings -> Hooks -> Trust all |
| Desktop app shows no WorkBuddy models | `model_catalog.json` was not generated correctly -> re-run `install.cmd` |

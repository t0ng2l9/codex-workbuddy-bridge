# WorkBuddy Bridge - Codex Plugin Marketplace

Connect the models you are already signed in to in the **WorkBuddy AI desktop app
(international build)** to **Codex** (CLI and desktop app). No OpenAI subscription
and no Codex plan required - Codex provides the agent scaffolding, WorkBuddy
provides the models.

```
Codex --> 127.0.0.1:8788 (local proxy) --> workbuddy.ai --> real models
             |  decrypts the desktop app session
             |  Responses <-> Chat Completions translation
             |  records / syncs the model actually in use
             |  generates the model picker catalog
```

Plugin name `workbuddy-bridge`, marketplace `wbbridge`, provider `wbbridge`,
profile `wbb`, proxy port `8788` - deliberately distinct from other similar
bridges so both can coexist on the same machine.

---

## Install

```cmd
:: 1. add the marketplace (GitHub, a local folder, or an unzipped zip all work)
codex plugin marketplace add zetongliu219-alt/workbuddy-bridge

:: 2. install the plugin
codex plugin add workbuddy-bridge@wbbridge

:: 3. check the environment, then install (provider / profile / catalog / skill / AGENTS rule)
python "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge\<version>\bin\setup.py" doctor
python "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge\<version>\bin\setup.py" install
```

Then run `codex --profile wbb`, or just `scripts\codex-wbb.cmd` (it starts the
proxy if needed). In the desktop app, **start a new session** to pick up the new
provider.

For details (troubleshooting, model list, free tier, hook trust, proxy
lifecycle) see [`plugins/workbuddy-bridge/README.md`](plugins/workbuddy-bridge/README.md).

---

## What a user needs

| Item | Requirement |
|---|---|
| Windows | Required (the scripts and credential paths are Windows-specific) |
| WorkBuddy AI desktop app (international) | Installed and **signed in once**; it does not need to stay running |
| Python | 3.10+ with `cryptography`; if no system Python exists, Codex's bundled runtime works |
| Codex | 0.153+ (verified on 0.163) |

`doctor` reports exactly what is missing and how to fix it. **Every user must
sign in with their own account** - never share credential files.

---

## Repository layout

```
.
|- .agents/plugins/marketplace.json        marketplace index (Codex discovers the plugin here)
|- .gitattributes                          marks *.cmd as binary (protects the GBK + CRLF scripts)
`- plugins/workbuddy-bridge/               the plugin itself
   |- .codex-plugin/plugin.json            plugin manifest
   |- bin/                                 proxy and installer (Python)
   |- scripts/                             launchers and hook entry points (.cmd)
   |- skills/                              usage/troubleshooting + live model lookup
   |- commands/                            /wbbridge-setup, /wbbridge-status
   |- hooks/hooks.json                     SessionStart: start the proxy if it is down
   `- README.md                            full plugin documentation
```

---

## Sharing it

All three paths are tested; only the first step differs:

| How | What the other person runs |
|---|---|
| GitHub repo | `codex plugin marketplace add zetongliu219-alt/workbuddy-bridge` |
| Shared folder / USB stick | `codex plugin marketplace add D:\share\workbuddy-bridge` |
| zip | unzip, then `codex plugin marketplace add %USERPROFILE%\Downloads\workbuddy-bridge` |

When you build the zip, **`.gitattributes` must be included**: it marks `*.cmd`
as binary so git's newline normalisation cannot corrupt the GBK + CRLF scripts.

---

## Caveats

The upstream is a **private, reverse-engineered API**, and bypassing its
system-prompt fingerprint is an active evasion of their risk controls. Therefore:

- **Small-scale sharing is fine; do not distribute publicly or publish a tutorial.**
  Large-scale use triggers upstream abuse detection, and if the endpoint gets
  closed everyone loses access.
- **Do not hammer the free tier** (`hy3` / `deepseek-v4.1-flash` / `step-5-preview`);
  high request rates trigger account-level rate limits.
- The proxy binds to `127.0.0.1` only. **Never** change it to `0.0.0.0` - that
  would expose your account token to the whole local network.

See the plugin README's [security and caveats](plugins/workbuddy-bridge/README.md#security-and-caveats)
section for details.

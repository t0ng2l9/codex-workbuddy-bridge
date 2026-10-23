---
description: Connect WorkBuddy to Codex (install / repair / reinstall)
argument-hint: "[--skip-doctor] [--no-watch] [--no-skill] [--no-agents-rule]"
allowed-tools: [Bash, Read, Glob, Grep]
---

# Install the WorkBuddy to Codex bridge

The user invoked this command with arguments: `$ARGUMENTS`

## Steps

1. Run the environment check first and report the result honestly:

```cmd
python "<plugin root>\bin\setup.py" doctor
```

2. If the check flags anything with `!!`, fix that before continuing (missing
   Python/cryptography, not signed in to the WorkBuddy desktop app, and a missing
   Codex all have to be resolved first). **Do not** download or install the
   WorkBuddy desktop app yourself - have the user install and sign in.

3. Once the check passes, install:

```cmd
python "<plugin root>\bin\setup.py" install $ARGUMENTS
```

The install also writes the provider block, the profile, the model catalog, the
skill, the AGENTS rule and the **watchdog logon autostart** (needed if the proxy
should follow the plugin enabled state).

4. Confirm with status and the watchdog verdict:

```cmd
python "<plugin root>\bin\setup.py" status
python "<plugin root>\bin\watch_plugin.py" --status
```

5. Tell the user two things:

   - How to start: `codex --profile wbb`, or **start a new session** in the
     desktop app (old sessions are bound to their provider per session and do not
     follow).
   - **Trusting the hook is recommended**, otherwise a new session will not start
     the proxy: desktop app **Settings -> Hooks -> From Plugins -> this plugin
     SessionStart -> Trust all** (or pick *Trust all and continue* in a new TUI
     session; for automation use `codex exec --dangerously-bypass-hook-trust`).
     It also works untrusted - the watchdog covers it.

## Notes

- `<plugin root>` is the real directory after installation; find it with:
  `dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\codex-workbuddy-bridge"`
- The install is idempotent: re-running never changes what is already in place
  and never accumulates blank lines.
- Do not modify any section of `config.toml` other than this plugin own block.
- If the install fails (non-zero exit), quote the `!!` line verbatim; do not guess.
- After changing plugin content, bump the version before reinstalling (Codex
  caches by version):
  `python "<plugin root>\bin\bump-plugin.py" "<plugin dir>"` then
  `codex plugin add codex-workbuddy-bridge@wbbridge`.

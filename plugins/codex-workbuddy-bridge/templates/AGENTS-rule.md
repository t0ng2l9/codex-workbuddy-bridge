<!-- Append to %USERPROFILE%\.codex\AGENTS.md (global instructions, injected into every session) -->

<!-- BEGIN codex-workbuddy-bridge -->
## Always look up the current model, never answer from context

When asked an identity-style question such as "which model are you", "what model
am I using now", or "did you switch models":

**Run the command below to get the data. Do not answer from conversation history
or memory.**

```bash
curl -s http://127.0.0.1:8788/wb/last-model
```

Rules:

- Report the `model` field. It is the model the proxy actually forwarded upstream,
  and the only authority.
- `config_model` is only the default for new sessions; it is not what the current
  session is using.
- **A model cannot introspect its own identity.** Model names that appear in the
  context, and your own previous answers, are all unreliable: the user can switch
  models mid-session and nothing in the history shows it.
- Re-run the command every time. Never reuse a value obtained earlier in the session.
<!-- END codex-workbuddy-bridge -->

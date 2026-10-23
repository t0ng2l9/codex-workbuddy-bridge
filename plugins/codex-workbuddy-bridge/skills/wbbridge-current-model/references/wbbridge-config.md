# WorkBuddy provider layout

The `workbuddy` model provider is configured in `<CODEX_HOME>/config.toml`
(`CODEX_HOME` defaults to `~/.codex`):

```toml
model_provider = 'workbuddy'
model = "deepseek-v4.1-flash"
model_reasoning_effort = "low"
model_catalog_json = "<CODEX_HOME>/wbbridge-catalog.json"
```

- It is a local proxy (`http://127.0.0.1:8788/v1`) that maps Codex to the
  WorkBuddy desktop app's logged-in models.
- The `model_catalog_json` file lists every available slug under a top-level
  `models` array, each with `slug`, `display_name`, `context_window`,
  `max_output_tokens`, and `supported_reasoning_levels`.
- The `model` field in config is the **slug**; the helper resolves the friendly
  `display_name` from the catalog.

## Known slugs (non-exhaustive)
`default-model`, `fast-model`, `balanced-model`, `primary-model`, `deep-model`,
`deepseek-v4.1-flash`, `deepseek-v4.1-flash-sg`, `gpt-6-astra`, `hy4-preview`,
`hy3`, `kimi-k2.8-preview`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`,
`gpt-5.5`, `gpt-5.4`, `gemini-3.5-flash`, `glm-5.3-flash`, `glm-5.3`, `glm-5.2`,
`kimi-k3`, `kimi-k2.6`, `grok-4.7`, `gpt-6-sol`, `gemini-3.8-flash`, `gpt-6-luna`,
`gpt-6.1-sol`, `step-5-preview`.

If `model` names a slug absent from the catalog, the helper still reports it but
flags `catalog_found = false`.

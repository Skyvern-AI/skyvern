# CLI and MCP Parity Summary

Common mappings:

- `skyvern browser navigate` -> `skyvern_navigate`
- `skyvern browser act` -> `skyvern_act`
- `skyvern browser extract` -> `skyvern_extract`
- `skyvern workflow run` -> `skyvern_workflow_run`
- `skyvern workflow run --browser-profile-id bp_...` -> `skyvern_workflow_run(browser_profile_id=...)`
- `skyvern browser session create --browser-profile-id bp_...` -> `skyvern_browser_session_create(browser_profile_id=...)`
- `skyvern browser-profile create` -> `skyvern_browser_profile_create`
- `skyvern browser-profile list/get/delete` -> `skyvern_browser_profile_list/get/delete`
- `skyvern credential list` -> `skyvern_credential_list`

Use CLI for local operator workflows and MCP tools for agent-driven integrations.

## Browser-profile lifecycle

Cloud saved-login reuse uses browser profiles. Create one from a completed persisted workflow run or eligible
browser session, then pass the returned `browser_profile_id` to a normal workflow run or cloud session. A
workflow-run source needs `persist_browser_session=true` on the workflow definition. A plain
`create_browser_session()` session alone does not create a browser-profile archive, and archive creation can be
asynchronous, so retry profile creation briefly if the source archive is not ready.

Do not map this to `skyvern browser state save/load` or MCP `state_save/state_load`; those are local/browser-file
state tools. Product UI is not required for MCP or CLI browser-profile save/reuse.

## Agent-Aware CLI

The CLI supports structured JSON output and non-interactive mode for AI agents:

| Feature | CLI flag | Env var |
|---------|----------|---------|
| Structured JSON output | `--json` on any command | - |
| Non-interactive mode | - | `SKYVERN_NON_INTERACTIVE=1` or `CI=true` |
| Skip confirmations | `--yes` or `--force` | - |
| Discover commands | `skyvern capabilities --json` | - |

All `--json` responses use the same envelope:
`{schema_version, ok, action, data, error, warnings, browser_context, artifacts, timing_ms}`

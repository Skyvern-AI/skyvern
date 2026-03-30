# CLI and MCP Parity Summary

Common mappings:

- `skyvern browser navigate` -> `skyvern_navigate`
- `skyvern browser act` -> `skyvern_act`
- `skyvern browser extract` -> `skyvern_extract`
- `skyvern workflow run` -> `skyvern_workflow_run`
- `skyvern credential list` -> `skyvern_credential_list`

Use CLI for local operator workflows and MCP tools for agent-driven integrations.

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

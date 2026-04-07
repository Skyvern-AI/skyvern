# Skyvern MCP Server

The Skyvern MCP server gives AI assistants (Claude, Cursor, Windsurf, Codex) full browser control -- clicking, filling forms, extracting data, navigating pages, uploading files, managing workflows, and more. 75+ tools, one server.

## Install

```bash
pip install skyvern
```

## Setup

### Cloud (recommended)

Get an API key from [app.skyvern.com](https://app.skyvern.com), then configure your client:

**Claude Code:**
```bash
claude mcp add-json skyvern '{"type":"http","url":"https://api.skyvern.com/mcp/","headers":{"x-api-key":"YOUR_API_KEY"}}' --scope user
```

**Cursor** (`~/.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "skyvern": {
      "type": "streamable-http",
      "url": "https://api.skyvern.com/mcp/",
      "headers": { "x-api-key": "YOUR_API_KEY" }
    }
  }
}
```

**Windsurf** (`~/.codeium/windsurf/mcp_config.json`):
```json
{
  "mcpServers": {
    "skyvern": {
      "type": "streamable-http",
      "url": "https://api.skyvern.com/mcp/",
      "headers": { "x-api-key": "YOUR_API_KEY" }
    }
  }
}
```

### Local (self-hosted)

```bash
skyvern init        # interactive setup wizard
skyvern run server  # start the local API server
```

Manual config for any MCP client:
```json
{
  "mcpServers": {
    "skyvern": {
      "command": "python3",
      "args": ["-m", "skyvern", "run", "mcp"],
      "env": {
        "SKYVERN_BASE_URL": "http://localhost:8000",
        "SKYVERN_API_KEY": "YOUR_API_KEY"
      }
    }
  }
}
```

## Tools

### Browser Sessions
`skyvern_browser_session_create`, `skyvern_browser_session_close`, `skyvern_browser_session_list`, `skyvern_browser_session_get`, `skyvern_browser_session_connect`

### Browser Actions
`skyvern_act` (natural language), `skyvern_navigate`, `skyvern_click`, `skyvern_type`, `skyvern_hover`, `skyvern_scroll`, `skyvern_select_option`, `skyvern_press_key`, `skyvern_drag`, `skyvern_file_upload`, `skyvern_wait`

### Data Extraction & Validation
`skyvern_extract` (structured JSON output), `skyvern_screenshot`, `skyvern_find`, `skyvern_validate`, `skyvern_evaluate` (run JavaScript), `skyvern_get_html`, `skyvern_get_value`, `skyvern_get_styles`

### Authentication & Credentials
`skyvern_login`, `skyvern_credential_list`, `skyvern_credential_get`, `skyvern_credential_delete`

Supports Skyvern vault, Bitwarden, 1Password, and Azure Key Vault with automatic 2FA/TOTP.

### Tabs & Frames
`skyvern_tab_new`, `skyvern_tab_list`, `skyvern_tab_switch`, `skyvern_tab_close`, `skyvern_tab_wait_for_new`, `skyvern_frame_list`, `skyvern_frame_switch`, `skyvern_frame_main`

### Network & Console Inspection
`skyvern_console_messages`, `skyvern_network_requests`, `skyvern_network_request_detail`, `skyvern_network_route`, `skyvern_network_unroute`, `skyvern_get_errors`, `skyvern_har_start`, `skyvern_har_stop`, `skyvern_handle_dialog`

### Browser State & Storage
`skyvern_state_save`, `skyvern_state_load`, `skyvern_get_session_storage`, `skyvern_set_session_storage`, `skyvern_clear_session_storage`, `skyvern_clear_local_storage`, `skyvern_clipboard_read`, `skyvern_clipboard_write`

### Workflows
`skyvern_workflow_create`, `skyvern_workflow_list`, `skyvern_workflow_get`, `skyvern_workflow_run`, `skyvern_workflow_status`, `skyvern_workflow_update`, `skyvern_workflow_delete`, `skyvern_workflow_cancel`, `skyvern_workflow_update_folder`

### Workflow Building Blocks
`skyvern_block_schema`, `skyvern_block_validate` -- 23 block types for multi-step automations.

### Cached Scripts
`skyvern_script_list_for_workflow`, `skyvern_script_get_code`, `skyvern_script_versions`, `skyvern_script_deploy`, `skyvern_script_fallback_episodes`

### Organization
`skyvern_folder_create`, `skyvern_folder_list`, `skyvern_folder_get`, `skyvern_folder_update`, `skyvern_folder_delete`

## Switching Configs

Use the CLI to switch between API keys or environments without manual editing:

```bash
skyvern mcp switch
```

## Full Documentation

[skyvern.com/docs/integrations/mcp](https://www.skyvern.com/docs/integrations/mcp)

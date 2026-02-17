<!-- DOCTOC SKIP -->

<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/SkyvernMCP.png"/>
    <img src="images/SkyvernMCP.png" alt="Skyvern MCP Logo" width="75%"/>
  </picture>
</h1>

# Model Context Protocol (MCP)

Skyvern MCP lets your MCP client control browser automation tools (navigation, extraction, workflows, screenshots, and more).

> Current availability: Skyvern MCP currently runs as a local stdio process (`skyvern run mcp`) on your machine.
> Remote-hosted MCP is planned, but is not the default setup today.

You can use MCP with:
1. **Skyvern Cloud** (`SKYVERN_BASE_URL=https://api.skyvern.com` + your API key)
2. **Local Skyvern Server** (`SKYVERN_BASE_URL=http://localhost:8000`)

## Cloud-only setup (no OSS clone required)

You do **not** need to clone the Skyvern OSS repo to use Skyvern Cloud MCP.
You only need Python and the `skyvern` package installed locally.

## Quickstart
> Supported Python versions: 3.11, 3.12, 3.13

1. **Install Python 3.11+ and verify**
```bash
python3 --version
```
If this is not 3.11+ (or you have multiple Python versions), use `python3.11`, `python3.12`, or `python3.13` explicitly in the next steps.

2. **Install Skyvern into that Python**
```bash
python3.11 -m pip install --upgrade pip skyvern
```

3. **Confirm install**
```bash
python3.11 -m skyvern --help
```

4. **Run the setup wizard**
```bash
skyvern init
```
The wizard will:
- ask whether to use Cloud or local mode
- collect/set `SKYVERN_BASE_URL` and `SKYVERN_API_KEY`
- auto-configure MCP for Claude Desktop, Cursor, and Windsurf

5. **Only for local mode: start the API server**
```bash
skyvern run server
```

6. **Restart your MCP client app** and ask it to use Skyvern tools.

## Config file locations

| Client | Auto-configured by `skyvern init` | Config location |
|---|---|---|
| Claude Desktop (macOS) | Yes | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Linux) | Yes | `~/.config/Claude/claude_desktop_config.json` (or `~/.local/share/Claude/claude_desktop_config.json`) |
| Cursor | Yes | `~/.cursor/mcp.json` |
| Windsurf | Yes | `~/.codeium/windsurf/mcp_config.json` |
| Claude Code (project scope) | No | `<project>/.mcp.json` |
| Claude Code (user/local scopes) | No | `~/.claude.json` |

## Manual configuration (any MCP client)

Use this if you are setting up a custom MCP client, or Claude Code.

```json
{
  "mcpServers": {
    "Skyvern": {
      "env": {
        "SKYVERN_BASE_URL": "https://api.skyvern.com",
        "SKYVERN_API_KEY": "YOUR_SKYVERN_API_KEY"
      },
      "command": "/Users/you/.pyenv/versions/3.11.11/bin/python3.11",
      "args": ["-m", "skyvern", "run", "mcp"]
    }
  }
}
```

Set `command` to the exact output of `python3.11 -c "import sys; print(sys.executable)"` (or `python3.12` / `python3.13`) on the same machine where your MCP client runs.

For local mode:
- set `SKYVERN_BASE_URL` to `http://localhost:8000`
- use your local `.env` API key from `skyvern init`
- run `skyvern run server` before using MCP
- if you get `No module named skyvern`, install with `<that_python_path> -m pip install skyvern`

## Claude Code setup

`skyvern init` does not currently write Claude Code config automatically.

For Claude Code project scope, create `<project>/.mcp.json`:

```json
{
  "mcpServers": {
    "Skyvern": {
      "env": {
        "SKYVERN_BASE_URL": "https://api.skyvern.com",
        "SKYVERN_API_KEY": "YOUR_SKYVERN_API_KEY"
      },
      "command": "/Users/you/.pyenv/versions/3.11.11/bin/python3.11",
      "args": ["-m", "skyvern", "run", "mcp"]
    }
  }
}
```

For Claude Code user/local scopes, place the same `mcpServers.Skyvern` object in `~/.claude.json`.

## Examples
### Skyvern allows Claude to look up the top Hackernews posts today

https://github.com/user-attachments/assets/0c10dd96-c6ff-4b99-ad99-f34a5afd04fe

### Cursor looking up the top programming jobs in your area

https://github.com/user-attachments/assets/084c89c9-6229-4bac-adc9-6ad69b41327d

### Ask Windsurf to do a form 5500 search and download some files 

https://github.com/user-attachments/assets/70cfe310-24dc-431a-adde-e72691f198a7

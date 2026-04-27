---
name: skyvern
description: Automate any website with AI-powered browser automation. Use when the user needs to interact with a website like filling forms, extracting data, downloading files, logging in, or running multi-step workflows. Skyvern navigates sites it has never seen before using LLMs and computer vision. Integrates via Python SDK, TypeScript SDK, REST API, MCP server, or CLI.
license: AGPL-3.0
compatibility: Requires a Skyvern Cloud API key (https://app.skyvern.com) or a self-hosted Skyvern instance. Python SDK requires Python 3.11+. TypeScript SDK requires Node.js 18+. MCP server works with Claude Code, Claude Desktop, Cursor, Windsurf, and VS Code.
metadata:
  author: skyvern-docs
  version: "1.0"
  docs: https://skyvern.com/docs
  github: https://github.com/Skyvern-AI/skyvern
---

# Skyvern: AI Browser Automation

Skyvern automates browser-based workflows using LLMs and computer vision. It navigates websites it has never seen before, filling forms, extracting data, and completing multi-step tasks via a simple API.

**SDK reference (all methods, parameters, types in one page):** https://skyvern.com/docs/sdk-reference/complete-reference

## When to use Skyvern

- The user needs to **interact with a website** programmatically (fill forms, click buttons, navigate pages)
- The user needs to **extract structured data** from a website (scrape prices, addresses, table rows)
- The user needs to **download files** from a web portal (invoices, reports, statements)
- The user needs to **log in** to a website and perform actions behind authentication
- The user needs to **automate a multi-step workflow** across one or more websites
- The user needs to **run browser automation from an AI assistant** (Claude, Cursor, Windsurf)

## Capabilities

### Run a single task

Execute a one-shot browser automation with natural language instructions.

**Inputs:**
- `prompt` (required): Natural language description of what to do
- `url` (required): Starting page URL
- `data_extraction_schema` (optional): JSON schema for structured output
- `proxy_location` (optional): Country code for geo-routing (e.g., `US`, `DE`)

**Python SDK:**
```python
from skyvern import Skyvern

client = Skyvern(api_key="YOUR_API_KEY")
result = await client.run_task(
    prompt="Get the title of the top post on Hacker News",
    url="https://news.ycombinator.com",
)
```

**TypeScript SDK:**
```typescript
import Skyvern from "@skyvern/client";

const client = new Skyvern({ apiKey: "YOUR_API_KEY" });
const result = await client.runTask({
    prompt: "Get the title of the top post on Hacker News",
    url: "https://news.ycombinator.com",
});
```

**REST API:**
```bash
curl -X POST https://api.skyvern.com/api/v2/run \
  -H "x-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Get the top post title", "url": "https://news.ycombinator.com"}'
```

### Extract structured data

Define a JSON schema to get consistent, typed output from any page.

```python
result = await client.run_task(
    prompt="Extract the top 3 posts",
    url="https://news.ycombinator.com",
    data_extraction_schema={
        "type": "object",
        "properties": {
            "posts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "points": {"type": "integer"}
                    }
                }
            }
        }
    },
)
```

### Build multi-step workflows

Chain task blocks, loops, conditionals, data extraction, and file operations into reusable automations.

**Block types:** NavigationBlock, ActionBlock, ExtractBlock, LoopBlock, TextPromptBlock, LoginBlock, FileDownloadBlock, FileParseBlock, UploadBlock, EmailBlock, WebhookBlock, ValidationBlock, WaitBlock, CodeBlock, ForLoopBlock, FileURLParsingBlock, DownloadToS3Block, SendEmailBlock.

```python
workflow = await client.create_workflow(
    title="Invoice Downloader",
    blocks=[...],  # See workflow blocks reference
)
run = await client.run_workflow(workflow_id=workflow.workflow_id)
```

### Manage browser sessions

Persist a live browser across multiple tasks to maintain login state, cookies, and page context.

```python
session = await client.create_session()
# Run multiple tasks on the same browser
await client.run_task(prompt="Log in", url="https://example.com", browser_session_id=session.browser_session_id)
await client.run_task(prompt="Download invoice", url="https://example.com/billing", browser_session_id=session.browser_session_id)
await client.close_session(session.browser_session_id)
```

### Handle authentication

Store passwords, TOTP/2FA secrets, and credit cards securely. Skyvern auto-fills login forms and generates 2FA codes during automation.

**Supported credential providers:** Skyvern vault (built-in), Bitwarden, 1Password, Azure Key Vault.

### Use via MCP server

Connect AI assistants directly to browser automation. The MCP server exposes 75+ tools.

**Install for Claude Code:**
```bash
claude mcp add skyvern-cloud -- npx @anthropic-ai/skyvern-mcp@latest --skyvern-api-key YOUR_API_KEY
```

**Install for Cursor/VS Code:** Add to MCP config:
```json
{
  "mcpServers": {
    "skyvern": {
      "command": "npx",
      "args": ["@anthropic-ai/skyvern-mcp@latest", "--skyvern-api-key", "YOUR_API_KEY"]
    }
  }
}
```

### Use via CLI

```bash
pip install skyvern
export SKYVERN_API_KEY="YOUR_KEY"

skyvern browser session create          # Start a cloud browser
skyvern browser act "Click the login button"  # Natural language action
skyvern browser extract '{"title": "string"}' # Extract structured data
skyvern browser screenshot              # Capture screenshot
skyvern task run --prompt "..." --url "..."   # Run a task
skyvern workflow run --id wf_xxx        # Run a workflow
```

## Constraints

- Tasks run in cloud browsers managed by Skyvern (or self-hosted browsers). They do not run in the user's local browser by default.
- Each task step consumes credits. Set `max_steps` to control costs.
- Browser automation takes 30-120 seconds per task depending on complexity.
- Skyvern works best with natural language prompts that describe the goal, not low-level click instructions.
- For websites that require login, credentials must be stored via the credentials API before running tasks.
- Self-hosted deployments require Docker and a PostgreSQL database.

## Key references

- [Quickstart](https://skyvern.com/docs/getting-started/quickstart.md): First task in 5 minutes
- [SDK Reference](https://skyvern.com/docs/sdk-reference/complete-reference.md): All methods and types (Python + TypeScript)
- [MCP Server Setup](https://skyvern.com/docs/going-to-production/mcp.md): Connect AI assistants
- [Workflow Blocks Reference](https://skyvern.com/docs/cloud/building-workflows/configure-blocks.md): All block types
- [Task Parameters](https://skyvern.com/docs/running-automations/task-parameters.md): All task options
- [Full documentation index](https://skyvern.com/docs/llms.txt): Complete page directory

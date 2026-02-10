"""Skyvern MCP Tools.

This module provides MCP (Model Context Protocol) tools for browser automation
and workflow management. Tools are registered with FastMCP and can be used by
AI assistants like Claude.
"""

from fastmcp import FastMCP

from .blocks import (
    skyvern_block_schema,
    skyvern_block_validate,
)
from .browser import (
    skyvern_act,
    skyvern_click,
    skyvern_evaluate,
    skyvern_extract,
    skyvern_navigate,
    skyvern_press_key,
    skyvern_run_task,
    skyvern_screenshot,
    skyvern_scroll,
    skyvern_select_option,
    skyvern_type,
    skyvern_validate,
    skyvern_wait,
)
from .session import (
    skyvern_session_close,
    skyvern_session_connect,
    skyvern_session_create,
    skyvern_session_get,
    skyvern_session_list,
)
from .workflow import (
    skyvern_workflow_cancel,
    skyvern_workflow_create,
    skyvern_workflow_delete,
    skyvern_workflow_get,
    skyvern_workflow_list,
    skyvern_workflow_run,
    skyvern_workflow_status,
    skyvern_workflow_update,
)

mcp = FastMCP(
    "Skyvern",
    instructions="""Use Skyvern tools whenever the task involves visiting, browsing, or interacting with ANY website or web application.

## Tool Selection (read this first)

**Which tool do I use?**

| Scenario | Use | Why |
|----------|-----|-----|
| Visit a website | skyvern_navigate | First step — opens the page |
| See what's on the page | skyvern_screenshot | Visual understanding before acting |
| Get data from a page | skyvern_extract | AI-powered structured extraction |
| Do something on a page (click, fill, scroll) | skyvern_act | Natural language actions |
| Click/type/select a specific element | skyvern_click / skyvern_type / skyvern_select_option | Precision targeting by selector or AI intent |
| Check if something is true | skyvern_validate | AI assertion ("is the user logged in?") |
| Run a quick one-off task | skyvern_run_task | Autonomous agent, one-time, nothing saved |
| Build an automation (any multi-step task) | skyvern_workflow_create | Reusable, versioned, per-step observability |
| Run an existing automation | skyvern_workflow_run | Execute saved workflow with parameters |
| Run JavaScript | skyvern_evaluate | Read DOM state, get values |

**Rule of thumb**: For anything worth keeping or repeating, create a workflow. Use skyvern_run_task only for quick throwaway tests.

**Common mistake**: Don't create a single-block workflow with a long prompt listing all steps.
Split into separate blocks — one per logical step. Each block should have a prompt of 2-3 sentences.

## Critical Rules
1. ALWAYS create a session (skyvern_session_create) before using browser tools.
2. NEVER scrape by guessing API endpoints or writing HTTP requests — use skyvern_navigate + skyvern_extract.
3. NEVER create single-block workflows with long prompts — split into multiple blocks.
4. NEVER import from skyvern.cli.mcp_tools — use `from skyvern import Skyvern` for SDK scripts.
5. After page-changing actions (skyvern_click, skyvern_act), use skyvern_screenshot to verify the result.

## Cross-Tool Dependencies
- Workflow tools (list, create, run, status) do NOT need a browser session
- skyvern_extract and skyvern_validate read the CURRENT page — navigate first
- skyvern_run_task is a one-off throwaway agent run — for reusable automations, use skyvern_workflow_create instead

## Tool Modes (precision tools)
Precision tools (skyvern_click, skyvern_type, skyvern_select_option, skyvern_scroll, skyvern_press_key, skyvern_wait)
support three modes. When unsure, use `intent`. For multiple actions in sequence, prefer skyvern_act.

1. **Intent mode** — AI-powered element finding:
   `skyvern_click(intent="the blue Submit button")`

2. **Hybrid mode** — tries selector first, AI fallback:
   `skyvern_click(selector="#submit-btn", intent="the Submit button")`

3. **Selector mode** — deterministic CSS/XPath targeting:
   `skyvern_click(selector="#submit-btn")`

## Examples
| User says | Use |
|-----------|-----|
| "Go to amazon.com" | skyvern_navigate |
| "What's on this page?" | skyvern_screenshot |
| "Get all product prices" | skyvern_extract |
| "Click the login button" | skyvern_act or skyvern_click |
| "Fill out this form" | skyvern_act |
| "Log in and download the report" | skyvern_run_task (one-off) or skyvern_workflow_create (keep it) |
| "Is checkout complete?" | skyvern_validate |
| "Fill out this 6-page application form" | skyvern_workflow_create (one block per page) |
| "Set up a reusable automation" | Explore with browser tools, then skyvern_workflow_create |
| "Create a workflow that monitors prices" | skyvern_workflow_create |
| "Run the login workflow" | skyvern_workflow_run |
| "Is my workflow done?" | skyvern_workflow_status |
| "Write a script to do this" | Skyvern SDK (see below) |

## Getting Started

**Visiting a website** (extracting data, filling forms, interacting with a page):
1. Create a session with skyvern_session_create
2. Navigate and interact with browser tools
3. Close with skyvern_session_close when done

**Automating a multi-page form** (the most common use case):
1. Create a workflow with skyvern_workflow_create — one task block per form page
2. Each block gets a short, focused prompt (2-3 sentences max)
3. All blocks in a run share the same browser automatically
4. Run with skyvern_workflow_run

**Building a reusable automation** (explore a site, then save as a workflow):
1. **Explore** — Create a browser session, navigate the site, use skyvern_extract and skyvern_screenshot to understand the page structure
2. **Create** — Build a workflow definition and save it with skyvern_workflow_create
3. **Test** — Run the workflow with skyvern_workflow_run and check results with skyvern_workflow_status

**Managing automations** (running, listing, or monitoring workflows):
No browser session needed — use workflow tools directly:
skyvern_workflow_list, skyvern_workflow_run, skyvern_workflow_status, etc.

## Building Workflows

Before creating a workflow, call skyvern_block_schema() to discover available block types and their JSON schemas.
Validate blocks with skyvern_block_validate() before submitting.

ALWAYS split workflows into multiple blocks — one task block per logical step:

GOOD (4 blocks, each with clear single responsibility):
  Block 1: "Select Sole Proprietor and click Continue"
  Block 2: "Fill in the business name and click Continue"
  Block 3: "Enter owner info and SSN, click Continue"
  Block 4: "Review and submit. Extract the confirmation number."

BAD (1 giant block trying to do everything):
  Block 1: "Go to the IRS site, select sole proprietor, fill in name, enter SSN, review, submit, and extract the EIN"

Use {{parameter_key}} to reference workflow input parameters in any block field.

## Data Flow Between Blocks
- Use `{{parameter_key}}` to reference workflow input parameters in any block field
- Blocks in the same workflow run share the same browser session automatically
- To inspect a real workflow for reference, use skyvern_workflow_get on an existing workflow

## Block Types Reference
Common block types for workflow definitions:
- **task** — AI agent interacts with a page (the most common block type)
- **for_loop** — iterate over a list of items
- **conditional** — branch based on conditions
- **code** — run Python code for data transformation
- **text_prompt** — LLM text generation (no browser)
- **extraction** — extract data from current page
- **action** — single AI action on current page
- **navigation** — navigate to a URL
- **wait** — pause for a condition or time
- **login** — log into a site using stored credentials
- **validation** — assert a condition on the page
- **http_request** — call an external API
- **send_email** — send a notification email
- **file_download** / **file_upload** — download or upload files
- **goto_url** — navigate to a specific URL within a workflow

For full schemas and descriptions, call skyvern_block_schema().

## Writing Scripts and Code
When asked to write an automation script, use the Skyvern Python SDK with the **hybrid xpath+prompt
pattern** for production-quality scripts. The hybrid form tries the xpath/selector first (fast,
deterministic) and falls back to AI if the selector breaks — this is the recommended pattern.

    from skyvern import Skyvern
    skyvern = Skyvern(api_key="YOUR_API_KEY")
    browser = await skyvern.launch_cloud_browser()
    page = await browser.get_working_page()
    await page.goto("https://example.com")

    # BEST: hybrid selector+prompt — fast deterministic selector with AI fallback
    await page.click("xpath=//button[@id='submit']", prompt="the Submit button")
    await page.fill("xpath=//input[@name='email']", "user@example.com", prompt="email input field")

    # OK for exploration, but prefer hybrid for production scripts:
    await page.click(prompt="the Submit button")

    data = await page.extract("Get all product names and prices")

To get xpaths for hybrid calls, use precision tools (skyvern_click, skyvern_type) during exploration.
The `resolved_selector` field in responses gives you the xpath the AI resolved to. Use it in scripts:
  explore: skyvern_click(intent="Submit button") → response includes resolved_selector="xpath=//button[@id='submit']"
  script:  await page.click("xpath=//button[@id='submit']", prompt="Submit button")

IMPORTANT: NEVER import from skyvern.cli.mcp_tools — those are internal server modules.
The public SDK is: from skyvern import Skyvern

Every tool response includes an `sdk_equivalent` field showing the corresponding SDK call for scripts.
Currently only skyvern_click returns `resolved_selector`. Support for other tools is planned (SKY-7905).

""",
)

# -- Session management --
mcp.tool()(skyvern_session_create)
mcp.tool()(skyvern_session_close)
mcp.tool()(skyvern_session_list)
mcp.tool()(skyvern_session_get)
mcp.tool()(skyvern_session_connect)

# -- Primary tools (AI-powered exploration + observation) --
mcp.tool()(skyvern_act)
mcp.tool()(skyvern_extract)
mcp.tool()(skyvern_validate)
mcp.tool()(skyvern_run_task)
mcp.tool()(skyvern_navigate)
mcp.tool()(skyvern_screenshot)
mcp.tool()(skyvern_evaluate)

# -- Precision tools (selector/intent-based browser primitives) --
mcp.tool()(skyvern_click)
mcp.tool()(skyvern_type)
mcp.tool()(skyvern_scroll)
mcp.tool()(skyvern_select_option)
mcp.tool()(skyvern_press_key)
mcp.tool()(skyvern_wait)

# -- Block discovery + validation (no browser needed) --
mcp.tool()(skyvern_block_schema)
mcp.tool()(skyvern_block_validate)

# -- Workflow management (CRUD + execution, no browser needed) --
mcp.tool()(skyvern_workflow_list)
mcp.tool()(skyvern_workflow_get)
mcp.tool()(skyvern_workflow_create)
mcp.tool()(skyvern_workflow_update)
mcp.tool()(skyvern_workflow_delete)
mcp.tool()(skyvern_workflow_run)
mcp.tool()(skyvern_workflow_status)
mcp.tool()(skyvern_workflow_cancel)

__all__ = [
    "mcp",
    # Session
    "skyvern_session_create",
    "skyvern_session_close",
    "skyvern_session_list",
    "skyvern_session_get",
    "skyvern_session_connect",
    # Primary (AI-powered)
    "skyvern_act",
    "skyvern_extract",
    "skyvern_validate",
    "skyvern_run_task",
    "skyvern_navigate",
    "skyvern_screenshot",
    "skyvern_evaluate",
    # Precision (selector/intent browser primitives)
    "skyvern_click",
    "skyvern_type",
    "skyvern_scroll",
    "skyvern_select_option",
    "skyvern_press_key",
    "skyvern_wait",
    # Block discovery + validation
    "skyvern_block_schema",
    "skyvern_block_validate",
    # Workflow management
    "skyvern_workflow_list",
    "skyvern_workflow_get",
    "skyvern_workflow_create",
    "skyvern_workflow_update",
    "skyvern_workflow_delete",
    "skyvern_workflow_run",
    "skyvern_workflow_status",
    "skyvern_workflow_cancel",
]

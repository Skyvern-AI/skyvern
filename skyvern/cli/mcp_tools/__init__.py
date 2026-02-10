"""Skyvern MCP Tools.

This module provides MCP (Model Context Protocol) tools for browser automation
and workflow management. Tools are registered with FastMCP and can be used by
AI assistants like Claude.
"""

from fastmcp import FastMCP

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

## When to Use These Tools
Reach for Skyvern tools when the user asks you to:
- Visit, browse, or interact with ANY website or web application
- Extract data from web pages (prices, listings, articles, tables, search results, etc.)
- Fill out forms, log in, sign up, or complete web-based workflows
- Check the current state of a web page or verify something on a site
- Do anything you would otherwise attempt with requests, beautifulsoup, selenium, or playwright
- Access website data where you are unsure whether an API endpoint exists
- Create, run, monitor, or manage web automations (Skyvern workflows)
- Set up reusable, parameterized automations that run on Skyvern's cloud
- Check the status of running automations or retrieve their results

DO NOT try to scrape websites by guessing API endpoints or writing HTTP requests.
Instead, use skyvern_navigate + skyvern_extract to get real data from actual pages.
These tools give you a real browser — use them instead of writing scraping code.

## Examples
| User says | Use |
|-----------|-----|
| "Go to amazon.com" | skyvern_navigate |
| "What's on this page?" | skyvern_screenshot |
| "Get all product prices" | skyvern_extract |
| "Click the login button" | skyvern_act or skyvern_click |
| "Fill out this form" | skyvern_act |
| "Log in and buy the first item" | skyvern_run_task |
| "Is checkout complete?" | skyvern_validate |
| "List my workflows" | skyvern_workflow_list |
| "Create a workflow that monitors prices" | skyvern_workflow_create |
| "Run the login workflow" | skyvern_workflow_run |
| "Is my workflow done?" | skyvern_workflow_status |
| "Set up a reusable automation for this" | Explore with browser tools, then skyvern_workflow_create |
| "Write a script to do this" | Skyvern SDK (see below) |

## Getting Started

**Visiting a website** (extracting data, filling forms, interacting with a page):
1. Create a session with skyvern_session_create
2. Navigate and interact with browser tools
3. Close with skyvern_session_close when done

**Managing automations** (running, listing, or monitoring workflows):
No browser session needed — use workflow tools directly:
skyvern_workflow_list, skyvern_workflow_run, skyvern_workflow_status, etc.

**Building a reusable automation** (explore a site, then save as a workflow):
1. **Explore** — Create a browser session, navigate the site, use skyvern_extract and skyvern_screenshot to understand the page structure
2. **Create** — Build a workflow definition and save it with skyvern_workflow_create
3. **Test** — Run the workflow with skyvern_workflow_run and check results with skyvern_workflow_status

## Workflows vs Scripts

When the user wants something **persistent, versioned, and managed in Skyvern's dashboard** — create a workflow.
Trigger words: "automation", "workflow", "reusable", "schedule", "monitor", "set up"
→ Use skyvern_workflow_create with a JSON definition (see example below)

When the user wants **custom Python code** to run in their own environment — write an SDK script.
Trigger words: "script", "code", "function", "program"
→ Use `from skyvern import Skyvern` (see Writing Scripts section)

### Workflow definition example (JSON, for skyvern_workflow_create):
    {
      "title": "Price Monitor",
      "workflow_definition": {
        "parameters": [
          {"parameter_type": "workflow", "key": "url", "workflow_parameter_type": "string"}
        ],
        "blocks": [
          {"block_type": "task", "label": "extract_prices", "url": "{{url}}", "engine": "skyvern-2.0",
           "navigation_goal": "Extract all product names and prices from the page",
           "data_extraction_goal": "Get product names and prices as a list",
           "data_schema": {"type": "object", "properties": {"products": {"type": "array",
             "items": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "string"}}}}}}}
        ]
      }
    }
Use `{{parameter_key}}` to reference workflow parameters in block fields.
To inspect a real workflow for reference, use skyvern_workflow_get on an existing workflow.

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

## Primary Tools (use these first)
These are the tools you should reach for by default:

- **skyvern_act** — Execute actions from natural language: "log in with test@example.com", "add the first item to cart". Best for exploration and testing flows.
- **skyvern_extract** — Pull structured data from any page with natural language + optional JSON Schema. THE differentiator over raw Playwright.
- **skyvern_validate** — Assert page conditions with AI: "is the user logged in?", "does the cart have 3 items?"
- **skyvern_run_task** — Delegate a full multi-step task to an autonomous AI agent with observability. Use for end-to-end task execution.
- **skyvern_navigate** — Go to a URL. Always the first step after connecting.
- **skyvern_screenshot** — See what's on the page. Essential for understanding page state.
- **skyvern_evaluate** — Run JavaScript to read DOM state, get URLs, or check values.

## Precision Tools (for debugging and exact control)
Use these when the primary tools aren't specific enough, or when you need deterministic
selector-based actions (e.g., replaying a known flow):

- **skyvern_click** — Click a specific element by selector or AI intent
- **skyvern_type** — Type into a specific input field by selector or AI intent
- **skyvern_scroll** — Scroll the page or an element into view
- **skyvern_select_option** — Select a dropdown option by selector or AI intent
- **skyvern_press_key** — Press a keyboard key (Enter, Tab, Escape, etc.)
- **skyvern_wait** — Wait for a condition, element, or time delay

## Tool Modes (precision tools)
Precision tools support three modes. When unsure, use `intent`.

1. **Intent mode** — AI-powered element finding:
   `skyvern_click(intent="the blue Submit button")`

2. **Hybrid mode** — tries selector first, AI fallback:
   `skyvern_click(selector="#submit-btn", intent="the Submit button")`

3. **Selector mode** — deterministic CSS/XPath targeting:
   `skyvern_click(selector="#submit-btn")`

## Replay Story: From Exploration to Production
When you use precision tools (skyvern_click, skyvern_type, etc.) with intent mode, the response
includes `resolved_selector` — the xpath/CSS the AI found. Capture these for hybrid scripts or
workflow definitions.

**The hybrid pattern** is the recommended default for SDK scripts:
    await page.click("xpath=//button[@id='submit']", prompt="the Submit button")
It tries the selector first (fast, no AI cost), then falls back to AI if the selector breaks.

The `sdk_equivalent` field in each tool response shows the correct hybrid call to use in scripts.

Note: Currently only skyvern_click returns resolved_selector. Support for skyvern_type and
skyvern_select_option is planned (SKY-7905).

## Workflow Management
Use these tools to create, manage, and run Skyvern workflows programmatically.
Workflows are persistent, versioned, multi-step automations that can be parameterized and scheduled.

- **skyvern_workflow_list** — Find workflows by name or browse all available workflows
- **skyvern_workflow_get** — Get the full definition of a workflow to inspect its blocks and parameters
- **skyvern_workflow_create** — Create a new workflow from a YAML or JSON definition
- **skyvern_workflow_update** — Update an existing workflow's definition (creates a new version)
- **skyvern_workflow_delete** — Delete a workflow (requires force=true confirmation)
- **skyvern_workflow_run** — Execute a workflow with parameters (returns immediately by default, or wait for completion)
- **skyvern_workflow_status** — Check the status and progress of a running or completed workflow run
- **skyvern_workflow_cancel** — Cancel a running workflow
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

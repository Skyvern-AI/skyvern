"""Skyvern MCP Tools.

This module provides MCP (Model Context Protocol) tools for browser automation.
Tools are registered with FastMCP and can be used by AI assistants like Claude.
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
| "Write a script to do this" | Skyvern SDK (see below) |

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

## Recommended Workflow
1. **Connect** — Create or connect to a browser session
2. **Explore** — Navigate pages, take screenshots, extract data with AI
3. **Build** — Capture selectors and data schemas to construct deterministic workflows
4. **Test** — Validate workflows via skyvern_run_task

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

## Replay Story: From Exploration to Production Scripts
When you use precision tools (skyvern_click, skyvern_type, etc.) with intent mode, the response
includes `resolved_selector` — the xpath/CSS the AI found. Capture these to build hybrid scripts.

**The hybrid pattern** is the recommended default for SDK scripts:
    await page.click("xpath=//button[@id='submit']", prompt="the Submit button")
It tries the selector first (fast, no AI cost), then falls back to AI if the selector breaks.

**Workflow for generating scripts:**
1. Explore: Use skyvern_click(intent="Submit button") during interactive exploration
2. Capture: Note the `resolved_selector` from the response (e.g., "//button[@id='submit']")
3. Script: Write `page.click("xpath=//button[@id='submit']", prompt="Submit button")`

The `sdk_equivalent` field in each tool response shows the correct hybrid call to use in scripts.
Always prefer hybrid xpath+prompt over prompt-only in generated scripts.

Note: Currently only skyvern_click returns resolved_selector. Support for skyvern_type and
skyvern_select_option is planned (SKY-7905). For those tools, use the selector you provided
as input, or fall back to prompt-only until SKY-7905 ships.

## Getting Started
Create a session with skyvern_session_create, then use browser tools to interact with pages.
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
]

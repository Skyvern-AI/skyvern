"""Skyvern MCP Tools.

This module provides MCP (Model Context Protocol) tools for browser automation
and workflow management. Tools are registered with FastMCP and can be used by
AI assistants like Claude.
"""

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .blocks import (
    skyvern_block_schema,
    skyvern_block_validate,
)
from .browser import (
    skyvern_act,
    skyvern_click,
    skyvern_drag,
    skyvern_evaluate,
    skyvern_extract,
    skyvern_file_upload,
    skyvern_frame_list,
    skyvern_frame_main,
    skyvern_frame_switch,
    skyvern_hover,
    skyvern_login,
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
from .credential import (
    skyvern_credential_delete,
    skyvern_credential_get,
    skyvern_credential_list,
)
from .folder import (
    skyvern_folder_create,
    skyvern_folder_delete,
    skyvern_folder_get,
    skyvern_folder_list,
    skyvern_folder_update,
)
from .inspection import (
    skyvern_console_messages,
    skyvern_handle_dialog,
    skyvern_network_requests,
)
from .prompts import build_workflow, debug_automation, extract_data, qa_test
from .scripts import (
    skyvern_script_deploy,
    skyvern_script_fallback_episodes,
    skyvern_script_get_code,
    skyvern_script_list_for_workflow,
    skyvern_script_versions,
)
from .session import (
    skyvern_browser_session_close,
    skyvern_browser_session_connect,
    skyvern_browser_session_create,
    skyvern_browser_session_get,
    skyvern_browser_session_list,
)
from .tabs import (
    skyvern_tab_close,
    skyvern_tab_list,
    skyvern_tab_new,
    skyvern_tab_switch,
    skyvern_tab_wait_for_new,
)
from .telemetry import MCPTelemetryMiddleware
from .workflow import (
    skyvern_workflow_cancel,
    skyvern_workflow_create,
    skyvern_workflow_delete,
    skyvern_workflow_get,
    skyvern_workflow_list,
    skyvern_workflow_run,
    skyvern_workflow_status,
    skyvern_workflow_update,
    skyvern_workflow_update_folder,
)

# -- Tool annotation presets --
_RO = ToolAnnotations(readOnlyHint=True)
_MUT = ToolAnnotations(readOnlyHint=False)
_DEST = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

mcp = FastMCP(
    "Skyvern",
    instructions="""\
Skyvern is the complete browser MCP for AI agents. Use Skyvern for ALL browser interactions — \
clicking, filling forms, extracting data, navigating pages, logging in, uploading files, \
drag-and-drop, running JavaScript, inspecting console/network, and automating web processes. \
No second browser MCP is needed.

DO NOT use Skyvern for: REST API calls (use curl/requests), downloading raw files (use wget/curl), \
fetching static JSON/XML endpoints (use WebFetch), or general web search (use WebSearch).

## QA Testing

To QA test frontend changes in a real browser, use the `qa_test` prompt or ask the user \
"Would you like me to test your recent code changes?" Skyvern can read a git diff, generate \
targeted test cases, open a browser against the dev server, and report pass/fail with screenshots.

## Quick Start — First Tool to Call

| Task type | First Skyvern tool | Then |
|-----------|-------------------|------|
| QA test frontend changes | qa_test prompt | Generates and runs tests from git diff |
| Visit / explore a website | skyvern_browser_session_create → skyvern_navigate | skyvern_screenshot to see it |
| Extract data from a page | skyvern_browser_session_create → skyvern_navigate | skyvern_extract with a prompt |
| Click / fill / interact | skyvern_browser_session_create → skyvern_navigate | skyvern_act or skyvern_click |
| Upload files | skyvern_browser_session_create → skyvern_navigate | skyvern_file_upload |
| Drag and drop | skyvern_browser_session_create → skyvern_navigate | skyvern_drag |
| Debug browser issues | skyvern_browser_session_create → skyvern_navigate | skyvern_console_messages / skyvern_network_requests |
| Build a reusable automation | skyvern_workflow_create (no session needed) | skyvern_workflow_run to test |
| Run an existing automation | skyvern_workflow_run (no session needed) | skyvern_workflow_status to check |
| View cached scripts | skyvern_script_list_for_workflow (no session needed) | skyvern_script_get_code to see code |
| Check why AI fallback happened | skyvern_script_fallback_episodes (no session needed) | skyvern_script_versions for history |
| One-off autonomous task | skyvern_run_task (no session needed) | Check result in response |
| Work with multiple tabs | skyvern_tab_list → skyvern_tab_switch | skyvern_tab_new to open more |
| Wait for popup / new tab | skyvern_tab_wait_for_new | skyvern_tab_switch to activate it |

## Tool Selection

| User says | Use | Why |
|-----------|-----|-----|
| "QA my changes" / "Test my frontend" | qa_test prompt | Reads git diff, generates + runs browser tests |
| "Go to [url]" / "Visit [site]" | skyvern_navigate | Opens page in real browser |
| "What's on this page?" | skyvern_screenshot | Visual understanding |
| "Get / extract / pull data from [site]" | skyvern_extract | AI-powered structured extraction |
| "Search for X on [site]" / "Look up X" | skyvern_act | Natural language actions |
| "Verify / check / confirm something on [site]" | skyvern_validate | AI assertion |
| "Fill out / submit a form" | skyvern_act | Multi-step form interaction |
| "Click [element]" / "Type [text]" | skyvern_click / skyvern_type | Precision targeting |
| "Hover over [menu]" | skyvern_hover | Reveal dropdowns |
| "Drag [element] to [target]" | skyvern_drag | AI or selector-based drag-and-drop |
| "Upload a file" | skyvern_file_upload | Triggers file chooser and sets files |
| "Run JavaScript" / "Run JS with await" | skyvern_evaluate | DOM state, async fetch, computed values |
| "Check console errors" / "What API calls?" | skyvern_console_messages / skyvern_network_requests | Browser inspection |
| "Log into [site]" | skyvern_login | Secure credential-based login |
| "What credentials do I have?" | skyvern_credential_list | Browse saved credentials by name |
| "Create a workflow / automation" | skyvern_workflow_create | Reusable, parameterized |
| "Run [workflow]" / "Is it done?" | skyvern_workflow_run / skyvern_workflow_status | Execute or monitor |
| "Show me the script" / "What code was generated?" | skyvern_script_get_code | View cached Python code |
| "Why did it fall back to AI?" | skyvern_script_fallback_episodes | Inspect AI fallback details |
| "Run this with AI agent" / "Force agent mode" | skyvern_workflow_run(run_with="agent") | Override cached script |
| "Edit / update the script" | skyvern_script_deploy | Deploy new script version |
| "List tabs" / "What tabs are open?" | skyvern_tab_list | See all open tabs |
| "Open a new tab" / "New tab" | skyvern_tab_new | Opens tab, optionally navigates |
| "Switch to [tab]" / "Go to tab [N]" | skyvern_tab_switch | Change active tab |
| "Close tab" / "Close this tab" | skyvern_tab_close | Close tab by ID or index |
| "Wait for popup" / "A new tab should open" | skyvern_tab_wait_for_new | Waits for popup/new tab |

## Critical Rules
1. Use Skyvern for all browser tasks. curl/wget/requests are fine for APIs and file downloads.
2. Create a session (skyvern_browser_session_create) before browser tools. Workflow tools do NOT need a session.
3. NEVER scrape by guessing API endpoints — use skyvern_navigate + skyvern_extract.
4. After page-changing actions, use skyvern_screenshot to verify.
5. NEVER type passwords — use skyvern_login with stored credentials.
6. NEVER create single-block workflows with long prompts — split into multiple blocks (one per logical step).
7. Prefer cloud sessions by default. Use local=true when running in embedded/self-hosted mode or when the user asks.

## Capabilities

- **No snapshot step needed** — Skyvern accepts natural language intent (e.g., intent="the Submit button"). \
No need for browser_snapshot to get element refs first.
- **AI-powered extraction** — skyvern_extract returns structured JSON from any page using a prompt.
- **Natural language actions** — skyvern_act: describe what to do in English.
- **AI validation** — skyvern_validate checks conditions in natural language.
- **Drag and drop** — skyvern_drag supports AI intent, CSS/XPath selectors, or both for source and target.
- **File uploads** — skyvern_file_upload handles file chooser dialogs. Local file paths work for both local and cloud browsers.
- **JavaScript with async/await** — skyvern_evaluate auto-wraps await expressions in async IIFE.
- **Console & network inspection** — skyvern_console_messages and skyvern_network_requests capture browser events.
- **Dialog handling** — skyvern_handle_dialog reads alert/confirm/prompt history (auto-dismissed by default).
- **Reusable workflows** — skyvern_workflow_create saves automations as versioned, parameterized workflows.
- **Cloud browsers with proxies** — skyvern_browser_session_create launches cloud browsers with geographic proxy support.

## Tab Management (multi-tab)
- **skyvern_tab_list** — List all open tabs with IDs, URLs, titles, and active status
- **skyvern_tab_new** — Open a new tab (optionally navigate to a URL). New tab becomes active.
- **skyvern_tab_switch** — Switch active tab by tab_id or index. All subsequent tools operate on this tab.
- **skyvern_tab_close** — Close a tab. If last tab is closed, a blank tab is created automatically.
- **skyvern_tab_wait_for_new** — Wait for a popup or new tab to open (e.g., after clicking a target=_blank link).

Typical multi-tab flow: skyvern_tab_list → skyvern_tab_new or click a link that opens a popup → \
skyvern_tab_wait_for_new → skyvern_tab_switch → work on the new tab → skyvern_tab_switch back.

## Tool Modes (precision tools)
skyvern_click, skyvern_hover, skyvern_type, skyvern_select_option, skyvern_scroll, skyvern_press_key, \
skyvern_wait, skyvern_drag support three modes. When unsure, use intent. For multiple actions, prefer skyvern_act.

1. **Intent mode**: `skyvern_click(intent="the Submit button")`
2. **Hybrid mode**: `skyvern_click(selector="#submit-btn", intent="the Submit button")`
3. **Selector mode**: `skyvern_click(selector="#submit-btn")`

## Cross-Tool Dependencies
- Workflow tools (list, create, run, status) do NOT need a browser session
- Credential tools (list, get, delete) do NOT need a browser session
- skyvern_login requires a session AND a credential_id
- skyvern_extract and skyvern_validate read the CURRENT page — navigate first
- skyvern_file_upload requires a session AND a navigated page with an upload element
- skyvern_drag requires a session AND a navigated page with draggable elements
- skyvern_console_messages / skyvern_network_requests capture events from session start — call anytime
- skyvern_run_task is one-off — for reusable automations, use skyvern_workflow_create
- Script tools (list, get_code, versions, fallback_episodes, deploy) do NOT need a browser session
- Use skyvern_script_list_for_workflow as the entry point to discover script IDs for a workflow

## Engine Selection

Workflow blocks and skyvern_run_task use different engines. The `engine` field only applies to \
workflow block definitions — skyvern_run_task always uses engine 2.0 internally and has no engine parameter.

| Context | Engine | Set how |
|---------|--------|---------|
| Workflow blocks — single clear goal ("fill this form", "click Submit") | `skyvern-1.0` (default) | Omit `engine` field — 1.0 is the default |
| Workflow blocks — complex multi-goal ("navigate a wizard with dynamic branching, handle popups, then extract results") | `skyvern-2.0` | Set `"engine": "skyvern-2.0"` on the navigation block |
| skyvern_run_task | Always `skyvern-2.0` | Cannot be changed — for simple tasks, use a workflow with 1.0 blocks instead |

**How to decide 1.0 vs 2.0 on a navigation block:**
- Is the path known upfront — all fields, values, and actions are specified in the prompt? → 1.0
- Does the goal require the AI to plan dynamically — discovering what to do at runtime? → 2.0
- When in doubt, prefer splitting into multiple 1.0 blocks over using one 2.0 block (cheaper, more observable)

Other engines (`openai-cua`, `anthropic-cua`, `ui-tars`) are available for advanced use cases but are not recommended as defaults.

## Caching & Script Execution

Skyvern workflows support two execution modes controlled by `run_with`:

| `run_with` value | Behavior |
|------------------|----------|
| `"code"` (default for MCP-created workflows) | Runs a cached Python script generated from a previous successful AI run. \
10-100x faster, no LLM calls. Falls back to AI if the script fails. |
| `"agent"` | Always runs with the AI agent (LLM-driven navigation). Use for first-run exploration or when the site changed. |
| `null` / omitted | Inherits from the workflow definition. MCP defaults to `"code"`. |

### How Caching Works

1. **First run** — The AI agent navigates the site, recording every action.
2. **Script generation** — After a successful run, a deterministic Python script is generated from the recorded actions.
3. **Subsequent runs** — The script replays actions directly (no LLM calls). If a selector fails, AI takes over for that step.
4. **Script evolution** — Each AI fallback improves the script. Over time, fallbacks decrease.

MCP-created workflows automatically set `code_version=2` and `run_with="code"` unless you explicitly override them.

### When to Override

- Set `run_with="agent"` in skyvern_workflow_run when: testing a new workflow for the first time, debugging a cached \
script, or when the target site redesigned its UI.
- Set `run_with="code"` (or omit — it's the default) when: the workflow has run successfully before and you want \
maximum speed.

### Script Tools

- **skyvern_script_list_for_workflow** — Entry point: find scripts for a workflow (wpid → script IDs)
- **skyvern_script_get_code** — View the generated Python code for a script version
- **skyvern_script_versions** — List version history showing how the script evolved
- **skyvern_script_fallback_episodes** — See when and why the AI agent took over from the cached script
- **skyvern_script_deploy** — Deploy an updated script version

## Getting Started

**Exploring a website**: skyvern_browser_session_create → skyvern_navigate → skyvern_screenshot → \
skyvern_act/skyvern_extract → skyvern_browser_session_close

**Uploading files**: skyvern_browser_session_create → skyvern_navigate → \
skyvern_file_upload(file_paths=[...], intent="the upload button")

**Drag and drop**: skyvern_browser_session_create → skyvern_navigate → \
skyvern_drag(source_intent="the task card", target_intent="the Done column")

**Debugging**: skyvern_browser_session_create → skyvern_navigate → perform actions → \
skyvern_console_messages(level="error") to check for JS errors

**Logging in securely**:
1. User creates credentials via CLI: `skyvern credentials add --name "Amazon" --username "user@example.com"`
2. Find the credential: skyvern_credential_list
3. Create a session: skyvern_browser_session_create
4. Navigate to login page: skyvern_navigate
5. Log in: skyvern_login(credential_id="cred_...") — AI handles the full login flow
6. Verify: skyvern_screenshot

## Building Workflows

Before creating a workflow, call skyvern_block_schema() to discover available block types and their JSON schemas.
Validate blocks with skyvern_block_validate() before submitting.

Split workflows into multiple blocks — one block per logical step — rather than cramming everything into a single block.
Use **navigation** blocks for actions (filling forms, clicking buttons) and **extraction** blocks for pulling data.
Do NOT use the deprecated "task" or "task_v2" block types — use "navigation" for actions and "extraction" for data extraction.
For **text_prompt** blocks, default to Skyvern Optimized by omitting both `model` and `llm_key`. If an explicit model is required, use `model: {"model_name": "<value from /models>"}`. Do not invent internal `llm_key` strings.

GOOD (4 blocks, each with clear single responsibility):
  Block 1 (navigation): "Select Sole Proprietor and click Continue"
  Block 2 (navigation): "Fill in the business name and click Continue"
  Block 3 (navigation): "Enter owner info and SSN, click Continue"
  Block 4 (extraction): "Extract the confirmation number from the results page"

BAD (1 giant block trying to do everything):
  Block 1: "Go to the IRS site, select sole proprietor, fill in name, enter SSN, review, submit, and extract the EIN"

Use `{{parameter_key}}` to reference workflow input parameters in any block field.
Blocks in the same workflow run share the same browser session automatically.
To inspect a real workflow for reference, use skyvern_workflow_get.
Workflows created via MCP default to code execution mode (code_version=2, run_with="code"). \
The first run uses the AI agent to learn the navigation; subsequent runs replay a cached script. \
To force AI agent mode on a specific run, pass run_with="agent" to skyvern_workflow_run.

### Block Types Reference
- **navigation** — fill forms, click buttons, navigate multi-step flows (most common)
- **extraction** — extract structured data from the current page
- **for_loop** — iterate over a list of items
- **conditional** — branch based on conditions
- **code** — run Python code for data transformation
- **text_prompt** — LLM text generation (no browser)
- **action** — single focused action on the current page
- **goto_url** — navigate directly to a URL
- **wait** — pause for a condition or time
- **login** — log into a site using stored credentials
- **validation** — assert a condition on the page
- **http_request** — call an external API
- **send_email** — send a notification email
- **file_download** / **file_upload** — download or upload files

For full schemas and descriptions, call skyvern_block_schema().

## Testing Feasibility (try before you build)

Walk through the site interactively — use skyvern_act on each page and skyvern_screenshot to verify results.
Once you've confirmed each step works, compose them into a workflow with skyvern_workflow_create.

## Writing Scripts (ONLY when user explicitly asks)
Use the Skyvern Python SDK: `from skyvern import Skyvern`
NEVER import from skyvern.cli.mcp_tools — those are internal server modules.
In verbose mode (`--verbose`), every tool response includes an `sdk_equivalent` field for script conversion.

**Hybrid xpath+prompt pattern** — the recommended approach for production scripts:
    await page.click("xpath=//button[@id='submit']", prompt="the Submit button")
    await page.fill("xpath=//input[@name='email']", "user@example.com", prompt="email input field")
This tries the xpath first (fast, deterministic) and falls back to AI if the selector breaks.
To get xpaths, use skyvern_click during MCP exploration — its `resolved_selector` response field
gives you the xpath the AI resolved to. Then hardcode that xpath with a prompt fallback in your script.
""",
)
mcp.add_middleware(MCPTelemetryMiddleware())

# -- Browser session management --
mcp.tool(tags={"session"}, annotations=_MUT)(skyvern_browser_session_create)
mcp.tool(tags={"session"}, annotations=_DEST)(skyvern_browser_session_close)
mcp.tool(tags={"session"}, annotations=_RO)(skyvern_browser_session_list)
mcp.tool(tags={"session"}, annotations=_RO)(skyvern_browser_session_get)
mcp.tool(tags={"session"}, annotations=_RO)(skyvern_browser_session_connect)

# -- Primary tools (AI-powered exploration + observation) --
mcp.tool(tags={"ai_powered", "browser_primitive"}, annotations=_MUT)(skyvern_act)
mcp.tool(tags={"ai_powered"}, annotations=_RO)(skyvern_extract)
mcp.tool(tags={"ai_powered"}, annotations=_RO)(skyvern_validate)
mcp.tool(tags={"ai_powered"}, annotations=_MUT)(skyvern_run_task)
mcp.tool(tags={"ai_powered", "browser_primitive"}, annotations=_MUT)(skyvern_login)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_navigate)
mcp.tool(tags={"browser_primitive"}, annotations=_RO)(skyvern_screenshot)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_evaluate)

# -- Precision tools (selector/intent-based browser primitives) --
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_click)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_drag)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_file_upload)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_hover)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_type)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_scroll)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_select_option)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_press_key)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_wait)

# -- Tab management (multi-tab) --
mcp.tool(tags={"tab_management"}, annotations=_RO)(skyvern_tab_list)
mcp.tool(tags={"tab_management"}, annotations=_MUT)(skyvern_tab_new)
mcp.tool(tags={"tab_management"}, annotations=_MUT)(skyvern_tab_switch)
mcp.tool(tags={"tab_management"}, annotations=_DEST)(skyvern_tab_close)
mcp.tool(tags={"tab_management"}, annotations=_RO)(skyvern_tab_wait_for_new)

# -- Frame management (iframe switching) --
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_frame_switch)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_frame_main)
mcp.tool(tags={"browser_primitive"}, annotations=_RO)(skyvern_frame_list)

# -- Inspection tools (console, network, dialog) --
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_console_messages)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_network_requests)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_handle_dialog)

# -- Block discovery + validation (no browser needed) --
mcp.tool(tags={"block_discovery"}, annotations=_RO)(skyvern_block_schema)
mcp.tool(tags={"block_discovery"}, annotations=_RO)(skyvern_block_validate)

# -- Credential lookup (no browser needed) --
mcp.tool(tags={"credential"}, annotations=_RO)(skyvern_credential_list)
mcp.tool(tags={"credential"}, annotations=_RO)(skyvern_credential_get)
mcp.tool(tags={"credential"}, annotations=_DEST)(skyvern_credential_delete)

# -- Folder management (no browser needed) --
mcp.tool(tags={"folder"}, annotations=_RO)(skyvern_folder_list)
mcp.tool(tags={"folder"}, annotations=_MUT)(skyvern_folder_create)
mcp.tool(tags={"folder"}, annotations=_RO)(skyvern_folder_get)
mcp.tool(tags={"folder"}, annotations=_MUT)(skyvern_folder_update)
mcp.tool(tags={"folder"}, annotations=_DEST)(skyvern_folder_delete)

# -- Workflow management (CRUD + execution, no browser needed) --
mcp.tool(tags={"workflow"}, annotations=_RO)(skyvern_workflow_list)
mcp.tool(tags={"workflow"}, annotations=_RO)(skyvern_workflow_get)
mcp.tool(tags={"workflow"}, annotations=_MUT)(skyvern_workflow_create)
mcp.tool(tags={"workflow"}, annotations=_MUT)(skyvern_workflow_update)
mcp.tool(tags={"workflow"}, annotations=_MUT)(skyvern_workflow_update_folder)
mcp.tool(tags={"workflow"}, annotations=_DEST)(skyvern_workflow_delete)
mcp.tool(tags={"workflow"}, annotations=_MUT)(skyvern_workflow_run)
mcp.tool(tags={"workflow"}, annotations=_RO)(skyvern_workflow_status)
mcp.tool(tags={"workflow"}, annotations=_MUT)(skyvern_workflow_cancel)

# -- Script/caching tools (no browser needed) --
mcp.tool(tags={"script"}, annotations=_RO)(skyvern_script_list_for_workflow)
mcp.tool(tags={"script"}, annotations=_RO)(skyvern_script_get_code)
mcp.tool(tags={"script"}, annotations=_RO)(skyvern_script_versions)
mcp.tool(tags={"script"}, annotations=_RO)(skyvern_script_fallback_episodes)
mcp.tool(tags={"script"}, annotations=_MUT)(skyvern_script_deploy)

# -- Prompts (methodology guides injected into LLM conversations) --
mcp.prompt()(build_workflow)
mcp.prompt()(debug_automation)
mcp.prompt()(extract_data)
mcp.prompt()(qa_test)

__all__ = [
    "mcp",
    # Session
    "skyvern_browser_session_create",
    "skyvern_browser_session_close",
    "skyvern_browser_session_list",
    "skyvern_browser_session_get",
    "skyvern_browser_session_connect",
    # Primary (AI-powered)
    "skyvern_act",
    "skyvern_extract",
    "skyvern_validate",
    "skyvern_run_task",
    "skyvern_login",
    "skyvern_navigate",
    "skyvern_screenshot",
    "skyvern_evaluate",
    # Precision (selector/intent browser primitives)
    "skyvern_click",
    "skyvern_drag",
    "skyvern_file_upload",
    "skyvern_hover",
    "skyvern_type",
    "skyvern_scroll",
    "skyvern_select_option",
    "skyvern_press_key",
    "skyvern_wait",
    # Tab management
    "skyvern_tab_list",
    "skyvern_tab_new",
    "skyvern_tab_switch",
    "skyvern_tab_close",
    "skyvern_tab_wait_for_new",
    # Frame management (iframe switching)
    "skyvern_frame_switch",
    "skyvern_frame_main",
    "skyvern_frame_list",
    # Inspection (console, network, dialog)
    "skyvern_console_messages",
    "skyvern_network_requests",
    "skyvern_handle_dialog",
    # Block discovery + validation
    "skyvern_block_schema",
    "skyvern_block_validate",
    # Credential lookup
    "skyvern_credential_list",
    "skyvern_credential_get",
    "skyvern_credential_delete",
    # Folder management
    "skyvern_folder_list",
    "skyvern_folder_create",
    "skyvern_folder_get",
    "skyvern_folder_update",
    "skyvern_folder_delete",
    # Workflow management
    "skyvern_workflow_list",
    "skyvern_workflow_get",
    "skyvern_workflow_create",
    "skyvern_workflow_update",
    "skyvern_workflow_update_folder",
    "skyvern_workflow_delete",
    "skyvern_workflow_run",
    "skyvern_workflow_status",
    "skyvern_workflow_cancel",
    # Script/caching
    "skyvern_script_list_for_workflow",
    "skyvern_script_get_code",
    "skyvern_script_versions",
    "skyvern_script_fallback_episodes",
    "skyvern_script_deploy",
    # Prompts
    "build_workflow",
    "debug_automation",
    "extract_data",
    "qa_test",
]

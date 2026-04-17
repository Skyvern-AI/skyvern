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
    skyvern_clipboard_read,
    skyvern_clipboard_write,
    skyvern_drag,
    skyvern_evaluate,
    skyvern_execute,
    skyvern_extract,
    skyvern_file_upload,
    skyvern_find,
    skyvern_frame_list,
    skyvern_frame_main,
    skyvern_frame_switch,
    skyvern_hover,
    skyvern_login,
    skyvern_navigate,
    skyvern_observe,
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
    skyvern_get_errors,
    skyvern_get_html,
    skyvern_get_styles,
    skyvern_get_value,
    skyvern_handle_dialog,
    skyvern_har_start,
    skyvern_har_stop,
    skyvern_network_request_detail,
    skyvern_network_requests,
    skyvern_network_route,
    skyvern_network_unroute,
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
from .state import skyvern_state_load, skyvern_state_save
from .storage import (
    skyvern_clear_local_storage,
    skyvern_clear_session_storage,
    skyvern_get_session_storage,
    skyvern_set_session_storage,
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
Skyvern is the complete browser MCP for AI agents. Use Skyvern for ALL browser interactions.

DO NOT use Skyvern for: REST API calls (use curl/requests), downloading raw files (use wget/curl), \
fetching static JSON/XML endpoints (use WebFetch), or general web search (use WebSearch).

## ALWAYS Start Here: Session + Classification

**If a browser session is already open, keep using it. Otherwise start with:** skyvern_browser_session_create -> skyvern_navigate(url="...") -> [work] -> skyvern_browser_session_close()
**Passwords:** NEVER type passwords via skyvern_type or skyvern_act. ALWAYS use skyvern_login with stored credentials.

## Task Classification — classify before choosing a tool

| Classification | Signal | Tool | Cost | What Happens |
|---|---|---|---|---|
| Quick check (yes/no) | "is the user logged in?" | skyvern_validate | 1 LLM + screenshots | Lightweight validation (2 steps max), returns boolean. Cheapest AI option. |
| Quick inspection | "what does the page show?" | skyvern_extract | 1 LLM + screenshots | Dedicated extraction LLM + schema validation + caching. Better than screenshot+read. |
| Single action (known target) | "click #submit" | skyvern_click / skyvern_type | 0 LLM | Deterministic Playwright. No AI. Fastest. |
| Single action (unknown target) | "click the submit button" | skyvern_act | 2-3 LLM, no screenshots | No screenshots in reasoning. Economy a11y tree. For visual targets, use observe first. |
| Multi-step (simple, fast) | "fill the form and submit" | skyvern_observe + skyvern_execute | 0 Skyvern LLM | A11y tree + YOUR LLM plans from refs + batched primitives. Fast, cheap. |
| Throwaway autonomous trial | "try this once", "see if this works" | skyvern_run_task | Higher | One-off autonomous agent for exploratory work. Do not use for reusable or multi-page production automations. |
| Multi-step (complex) | "navigate a multi-page wizard" | skyvern_workflow_create (multi-block) | N LLM + screenshots | Build a workflow with one navigation block per step. Each block gets visual reasoning + verification. |
| Repeated/production | "automate this weekly", "run every Monday", "schedule", "recurring" | skyvern_workflow_create + run | Varies | Caching converts AI runs into deterministic scripts over time (10-100x faster on repeat). |

## Decision Rules (highest precedence)

1. If the user gives a selector, id, XPath, or exact field target, use browser primitives -- not skyvern_act.
2. If you only need a yes/no answer, use skyvern_validate -- not skyvern_extract or skyvern_act.
3. If the work stays on one page and the UI is standard, prefer skyvern_observe + skyvern_execute.
4. If the user says "try this once", "see if this works", or clearly wants a one-off exploratory trial, use skyvern_run_task.
5. If the task spans multiple pages and is meant to be reusable, scheduled, repeatable, or explicitly "set up" as automation, use skyvern_workflow_create.
6. Never type passwords. Always use skyvern_login with stored credentials.

## Quick Reference (one example per classification)

- **Quick check:** skyvern_validate(prompt="Is the user logged in?")
- **Inspection:** skyvern_extract(prompt="Extract all prices", schema='{"type":"object","properties":{...}}')
- **Known selector:** skyvern_click(selector="#submit") or skyvern_type(selector="#email", text="user@co.com")
- **Unknown target:** skyvern_act(prompt="Click the Sign In button")
- **Multi-step form:** skyvern_observe() -> skyvern_execute(steps=[...])
- **One-off trial:** skyvern_run_task(prompt="Try the checkout flow once")
- **Reusable workflow:** skyvern_workflow_create(definition='{"title":"...","workflow_definition":{"blocks":[...]}}', format="json")

For full examples and common patterns, see skyvern/cli/skills/skyvern/references/quick-start-patterns.md.

## Key Warnings

1. **act has NO screenshots** — uses economy a11y tree. For visual targets, use observe then click with ref.
2. **observe+execute ≠ workflows.** observe+execute: YOUR LLM plans, no Skyvern calls. Workflows: full ForgeAgent per block with screenshots.
3. **validate is cheapest AI** for yes/no. **extract uses screenshots** with dedicated LLM.
4. **NEVER type passwords** — use skyvern_login with stored credentials.

## Tool Tiers

**Tier 1 — Goal-Oriented Tools** (mixed cost):
- **AI-powered** (cost Skyvern LLM tokens): act, extract, validate, run_task, login
- **Zero Skyvern LLM** (your LLM plans, Skyvern executes): observe, execute
**Tier 2 — Browser Primitives** (zero AI cost): click, type, hover, scroll, select_option, press_key, wait, drag, \
file_upload, find, navigate, screenshot, evaluate
- **Tabs:** tab_list, tab_new, tab_switch, tab_close, tab_wait_for_new
- **Frames:** frame_list, frame_switch, frame_main
- **Inspection:** console_messages, network_requests, network_request_detail, get_errors, get_html, get_value, get_styles
- **Network:** network_route, network_unroute, har_start, har_stop
- **Storage:** state_save, state_load, get_session_storage, set_session_storage, clear_session_storage, clear_local_storage
- **Other:** clipboard_read, clipboard_write, handle_dialog
**Tier 3 — Management** (no session needed):
- **Sessions:** browser_session_create/close/list/get/connect
- **Workflows:** workflow_create/run/status/get/list/update/delete/cancel/update_folder
- **Scripts:** script_list_for_workflow, script_get_code, script_versions, script_fallback_episodes, script_deploy
- **Credentials:** credential_list/get/delete
- **Folders/Blocks:** folder_list/get/create/update/delete, block_schema, block_validate

Precision tools support intent (AI), selector (deterministic), or hybrid (both) targeting.

### Dependencies
- extract/validate read the CURRENT page — navigate first.
- login requires a session AND a credential_id from credential_list.
- file_upload requires a navigated page with an upload element.
- console_messages and network_requests capture events from session start — call anytime.
- Workflow, credential, script, folder, and block tools do NOT need a browser session.

## Session Lifecycle

Create session -> navigate -> work -> close. Session state persists between calls.
skyvern_browser_session_create(timeout=30) -> skyvern_navigate(url="...") -> [work] -> skyvern_browser_session_close()
Prefer cloud sessions by default. Use local=true for localhost URLs or self-hosted mode.
Use skyvern_browser_session_connect(cdp_url="...") to attach to an existing browser.

Multi-tab flow: tab_list -> tab_new or click link -> tab_wait_for_new -> tab_switch -> work -> tab_switch back.

## Workflows

Split into multiple blocks — one intent per block. Use **navigation** blocks for actions, **extraction** for data.
Call skyvern_block_schema() for available types. Validate with skyvern_block_validate() before creating.
Do NOT use deprecated "task" or "task_v2" block types — use "navigation" for actions, "extraction" for data.
Use {{parameter_key}} to reference workflow parameters. Blocks share a browser session automatically.

GOOD (4 blocks, clear single responsibility):
  Block 1 (navigation): "Select Sole Proprietor and click Continue"
  Block 2 (navigation): "Fill in the business name and click Continue"
  Block 3 (navigation): "Enter owner info, click Continue"
  Block 4 (extraction): "Extract the confirmation number"

BAD: One giant block trying to do everything at once.

### Engine Selection
- Known path (all fields/actions specified in prompt) -> skyvern-1.0 (default, omit engine field)
- Dynamic planning (discover what to do at runtime) -> skyvern-2.0
- skyvern_run_task always uses 2.0 (cannot change)
- When in doubt, split into multiple 1.0 blocks (cheaper, more observable)

### Caching
MCP-created workflows default to run_with="code". First run uses AI agent; subsequent runs replay \
a cached script (10-100x faster, no LLM calls). Set run_with="agent" for first-time testing, \
debugging, or when the target site redesigned. Use script tools to inspect: \
script_list_for_workflow -> script_get_code -> script_versions -> script_fallback_episodes.

### Block Types
navigation (most common), extraction, for_loop, conditional, code, text_prompt, action, goto_url, \
wait, login, validation, http_request, send_email, file_download, file_upload. \
Call skyvern_block_schema() for full schemas.

## Scripts (ONLY when user explicitly asks)

Use the Skyvern Python SDK: from skyvern import Skyvern. NEVER import from skyvern.cli.mcp_tools.
In verbose mode (--verbose), tool responses include sdk_equivalent for script conversion.
The hybrid xpath+prompt pattern tries xpath first (fast) and falls back to AI if the selector breaks. \
Use skyvern_click's resolved_selector response to get xpaths for production scripts.

## Critical Rules
1. Create a session (skyvern_browser_session_create) before any browser tool.
2. NEVER scrape by guessing API endpoints — use skyvern_navigate + skyvern_extract.
3. After page-changing actions, use skyvern_screenshot to verify.
4. NEVER type passwords — use skyvern_login with stored credentials.
5. NEVER create single-block workflows with long prompts — split into one block per step.
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

# -- Clipboard --
mcp.tool(tags={"browser_primitive"}, annotations=_RO)(skyvern_clipboard_read)
mcp.tool(tags={"browser_primitive"}, annotations=_MUT)(skyvern_clipboard_write)

# -- Batch tools (observe + execute for multi-step optimization) --
mcp.tool(tags={"browser_primitive", "batch"}, annotations=_RO)(skyvern_observe)
mcp.tool(tags={"browser_primitive", "batch"}, annotations=_MUT)(skyvern_execute)

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
mcp.tool(tags={"browser_primitive"}, annotations=_RO)(skyvern_find)

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

# -- Auth state persistence --
mcp.tool(tags={"state"}, annotations=_MUT)(skyvern_state_save)
mcp.tool(tags={"state"}, annotations=_MUT)(skyvern_state_load)

# -- Inspection tools (console, network, dialog, page errors, DOM) --
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_console_messages)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_network_requests)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_network_request_detail)
mcp.tool(tags={"inspection"}, annotations=_MUT)(skyvern_network_route)
mcp.tool(tags={"inspection"}, annotations=_MUT)(skyvern_network_unroute)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_handle_dialog)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_get_errors)
mcp.tool(tags={"inspection"}, annotations=_MUT)(skyvern_har_start)
mcp.tool(tags={"inspection"}, annotations=_MUT)(skyvern_har_stop)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_get_html)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_get_value)
mcp.tool(tags={"inspection"}, annotations=_RO)(skyvern_get_styles)

# -- Web storage (sessionStorage + localStorage) --
mcp.tool(tags={"storage"}, annotations=_RO)(skyvern_get_session_storage)
mcp.tool(tags={"storage"}, annotations=_MUT)(skyvern_set_session_storage)
mcp.tool(tags={"storage"}, annotations=_DEST)(skyvern_clear_session_storage)
mcp.tool(tags={"storage"}, annotations=_DEST)(skyvern_clear_local_storage)

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
    # Clipboard
    "skyvern_clipboard_read",
    "skyvern_clipboard_write",
    # Batch tools (observe + execute)
    "skyvern_observe",
    "skyvern_execute",
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
    "skyvern_find",
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
    # Inspection (console, network, dialog, page errors, DOM)
    "skyvern_console_messages",
    "skyvern_network_requests",
    "skyvern_network_request_detail",
    "skyvern_network_route",
    "skyvern_network_unroute",
    "skyvern_handle_dialog",
    "skyvern_get_errors",
    "skyvern_har_start",
    "skyvern_har_stop",
    "skyvern_get_html",
    "skyvern_get_value",
    "skyvern_get_styles",
    # Web storage
    "skyvern_get_session_storage",
    "skyvern_set_session_storage",
    "skyvern_clear_session_storage",
    "skyvern_clear_local_storage",
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
    # Auth state persistence
    "skyvern_state_save",
    "skyvern_state_load",
    # Prompts
    "build_workflow",
    "debug_automation",
    "extract_data",
    "qa_test",
]

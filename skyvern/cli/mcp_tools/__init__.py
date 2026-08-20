"""Skyvern MCP Tools.

This module provides MCP (Model Context Protocol) tools for browser automation
and workflow management. Tools are registered with FastMCP and can be used by
AI assistants like Claude.
"""

from __future__ import annotations

from typing import Any

try:
    from fastmcp import FastMCP as _FastMCP
    from mcp.types import ToolAnnotations
except ImportError as fastmcp_import_error:
    _FASTMCP_IMPORT_ERROR: ImportError | None = fastmcp_import_error

    class ToolAnnotations:  # type: ignore[no-redef]
        def __init__(self, **_: Any) -> None:
            pass

    class _FastMCP:  # type: ignore[no-redef]
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def add_middleware(self, *_: Any, **__: Any) -> None:
            return None

        def tool(self, *_: Any, **__: Any) -> Any:
            def decorator(func: Any) -> Any:
                return func

            return decorator

        def prompt(self, *_: Any, **__: Any) -> Any:
            def decorator(func: Any) -> Any:
                return func

            return decorator

        def run(self, *_: Any, **__: Any) -> None:
            raise _FASTMCP_IMPORT_ERROR or ImportError("fastmcp is required to run the Skyvern MCP server.")

else:
    _FASTMCP_IMPORT_ERROR = None

from skyvern.cli.mcp_tools.browser import EITHER_STATE_OUTPUT_SCHEMA

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
    skyvern_evaluate_and_screenshot,
    skyvern_execute,
    skyvern_extract,
    skyvern_extract_and_screenshot,
    skyvern_file_upload,
    skyvern_find,
    skyvern_frame_list,
    skyvern_frame_main,
    skyvern_frame_switch,
    skyvern_hover,
    skyvern_login,
    skyvern_navigate,
    skyvern_navigate_and_screenshot,
    skyvern_navigate_extract_and_screenshot,
    skyvern_observe,
    skyvern_press_key,
    skyvern_run_task,
    skyvern_screenshot,
    skyvern_scroll,
    skyvern_select_option,
    skyvern_type,
    skyvern_validate,
    skyvern_wait,
    skyvern_wait_for_either_state,
)
from .browser_profiles import (
    skyvern_browser_profile_create,
    skyvern_browser_profile_delete,
    skyvern_browser_profile_get,
    skyvern_browser_profile_list,
    skyvern_browser_profile_update,
)
from .cdp_input import skyvern_write_grid
from .code_block import skyvern_code_block_lint, skyvern_code_block_synthesize
from .credential import (
    skyvern_bitwarden_config_clear,
    skyvern_bitwarden_config_get,
    skyvern_bitwarden_config_set,
    skyvern_bitwarden_items,
    skyvern_credential_delete,
    skyvern_credential_get,
    skyvern_credential_list,
    skyvern_onepassword_config_clear,
    skyvern_onepassword_config_get,
    skyvern_onepassword_config_set,
    skyvern_onepassword_items,
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
    skyvern_page,
)
from .instructions import DEFAULT_INSTRUCTIONS
from .org import (
    skyvern_org_get,
    skyvern_org_update,
)
from .output_tools import (
    EXTRACT_STRUCTURED_DESCRIPTION,
    FINISH_DESCRIPTION,
    skyvern_extract_structured,
    skyvern_finish,
)
from .prompts import build_workflow, debug_automation, extract_data, qa_test
from .response import size_capped
from .schedule import (
    skyvern_schedule_create,
    skyvern_schedule_delete,
    skyvern_schedule_disable,
    skyvern_schedule_enable,
    skyvern_schedule_get,
    skyvern_schedule_list,
    skyvern_schedule_list_for_workflow,
    skyvern_schedule_update,
)
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
    skyvern_open_tabs,
    skyvern_tab_close,
    skyvern_tab_list,
    skyvern_tab_new,
    skyvern_tab_switch,
    skyvern_tab_wait_for_new,
)
from .trajectory import skyvern_trajectory_get
from .workflow import (
    guard_definition_size,
    skyvern_workflow_cancel,
    skyvern_workflow_create,
    skyvern_workflow_delete,
    skyvern_workflow_get,
    skyvern_workflow_list,
    skyvern_workflow_retry,
    skyvern_workflow_run,
    skyvern_workflow_run_list,
    skyvern_workflow_status,
    skyvern_workflow_update,
    skyvern_workflow_update_folder,
)

_RUN_TASK_TOOL_DESCRIPTION = (
    "Run a one-off autonomous trial via the highest-cost AI path. "
    "Not for production or reusable automations. "
    "Prefer direct tools (click/type/select via selector) and skyvern_observe + skyvern_execute."
)


def _add_telemetry_middleware() -> None:
    if _FASTMCP_IMPORT_ERROR is not None:
        return
    from .telemetry import MCPTelemetryMiddleware  # noqa: PLC0415

    mcp.add_middleware(MCPTelemetryMiddleware())


def _add_argument_validation_middleware() -> None:
    if _FASTMCP_IMPORT_ERROR is not None:
        return
    from .argument_validation import MCPArgumentValidationMiddleware  # noqa: PLC0415

    # Registered after telemetry so telemetry stays the outermost wrapper and
    # still records the call; this one is innermost and rejects malformed
    # arguments before dispatch runs (and logs a raw pydantic error).
    mcp.add_middleware(MCPArgumentValidationMiddleware())


# -- Tool annotation factories --
# Every tool registered with FastMCP must carry a human-readable `title`
# (required by the Claude Connectors Directory). We build per-tool
# ToolAnnotations with a title + explicit safety hints rather than sharing
# flyweight constants. Browser-page tools are open-world because they can
# interact with arbitrary websites, including read-only inspection.
def _ro(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, openWorldHint=False, destructiveHint=False)


def _web_ro(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, openWorldHint=True, destructiveHint=False)


def _mut(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=False, openWorldHint=False, destructiveHint=False)


def _dest(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=False, openWorldHint=False, destructiveHint=True)


def _web_mut(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=False, openWorldHint=True, destructiveHint=False)


def _web_dest(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=False, openWorldHint=True, destructiveHint=True)


mcp = _FastMCP(
    "Skyvern",
    instructions=DEFAULT_INSTRUCTIONS,
)
_add_telemetry_middleware()
_add_argument_validation_middleware()

# -- Browser session management --
mcp.tool(tags={"session", "lean"}, annotations=_mut("Create Browser Session"))(skyvern_browser_session_create)
mcp.tool(tags={"session", "lean"}, annotations=_dest("Close Browser Session"))(skyvern_browser_session_close)
mcp.tool(tags={"session", "lean"}, annotations=_ro("List Browser Sessions"))(skyvern_browser_session_list)
mcp.tool(tags={"session", "lean"}, annotations=_ro("Get Browser Session"))(skyvern_browser_session_get)
mcp.tool(tags={"session", "lean"}, annotations=_web_mut("Connect to Browser Session"))(skyvern_browser_session_connect)

# -- Browser profile management --
mcp.tool(tags={"browser_profile"}, annotations=_mut("Create Browser Profile"))(skyvern_browser_profile_create)
mcp.tool(tags={"browser_profile"}, annotations=_ro("List Browser Profiles"))(skyvern_browser_profile_list)
mcp.tool(tags={"browser_profile"}, annotations=_ro("Get Browser Profile"))(skyvern_browser_profile_get)
mcp.tool(tags={"browser_profile"}, annotations=_mut("Update Browser Profile"))(skyvern_browser_profile_update)
mcp.tool(tags={"browser_profile"}, annotations=_dest("Delete Browser Profile"))(skyvern_browser_profile_delete)

# -- Primary tools (AI-powered exploration + observation) --
# Browser tools run against arbitrary websites. Read-only inspection remains
# non-destructive, but still open-world because the target site is unbounded.
mcp.tool(tags={"ai_powered", "browser_primitive"}, annotations=_web_dest("Perform Browser Action (AI)"))(skyvern_act)
mcp.tool(tags={"ai_powered"}, annotations=_web_ro("Extract Data from Page (AI)"))(size_capped(skyvern_extract))
mcp.tool(tags={"ai_powered"}, annotations=_web_ro("Extract Data + Screenshot (AI)"))(
    size_capped(skyvern_extract_and_screenshot)
)
mcp.tool(tags={"ai_powered"}, annotations=_web_ro("Validate Page Condition (AI)"))(skyvern_validate)
mcp.tool(
    description=_RUN_TASK_TOOL_DESCRIPTION,
    tags={"ai_powered"},
    annotations=_web_dest("Run Autonomous Browser Task (AI)"),
)(skyvern_run_task)
mcp.tool(tags={"ai_powered", "browser_primitive", "lean"}, annotations=_web_mut("Log in to Website (AI)"))(
    skyvern_login
)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_mut("Navigate to URL"))(skyvern_navigate)
mcp.tool(tags={"browser_primitive"}, annotations=_web_mut("Navigate to URL + Screenshot"))(
    size_capped(skyvern_navigate_and_screenshot)
)
mcp.tool(tags={"ai_powered"}, annotations=_web_mut("Navigate + Extract Data + Screenshot (AI)"))(
    size_capped(skyvern_navigate_extract_and_screenshot)
)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_ro("Take Screenshot"))(skyvern_screenshot)
mcp.tool(tags={"browser_primitive"}, annotations=_web_dest("Evaluate JavaScript"))(skyvern_evaluate)
mcp.tool(tags={"browser_primitive"}, annotations=_web_dest("Evaluate JavaScript + Screenshot"))(
    size_capped(skyvern_evaluate_and_screenshot)
)

# -- Clipboard --
mcp.tool(tags={"browser_primitive"}, annotations=_ro("Read Clipboard"))(skyvern_clipboard_read)
mcp.tool(tags={"browser_primitive"}, annotations=_mut("Write Clipboard"))(skyvern_clipboard_write)

# -- Batch tools (observe + execute for multi-step optimization) --
mcp.tool(tags={"browser_primitive", "batch"}, annotations=_web_ro("Observe Page Structure"))(skyvern_observe)
mcp.tool(tags={"browser_primitive", "batch"}, annotations=_web_dest("Execute Batch Actions"))(skyvern_execute)
mcp.tool(tags={"browser_primitive"}, annotations=_web_dest("Write Grid"))(size_capped(skyvern_write_grid))

# -- Precision tools (selector/intent-based browser primitives) --
# Input/action primitives can submit, overwrite, or delete arbitrary website state.
# Passive pointer/scroll state changes are open-world but non-destructive.
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_dest("Click Element"))(skyvern_click)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_dest("Drag Element"))(skyvern_drag)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_dest("Upload File"))(skyvern_file_upload)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_mut("Hover Element"))(skyvern_hover)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_dest("Type Text"))(skyvern_type)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_mut("Scroll Page"))(skyvern_scroll)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_dest("Select Option"))(skyvern_select_option)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_dest("Press Key"))(skyvern_press_key)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_ro("Wait"))(skyvern_wait)
mcp.tool(
    tags={"browser_primitive", "lean"},
    annotations=_web_ro("Wait For Either State"),
    output_schema=EITHER_STATE_OUTPUT_SCHEMA,
)(skyvern_wait_for_either_state)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_ro("Find Element"))(skyvern_find)

# -- Tab management (multi-tab) --
mcp.tool(tags={"tab_management", "lean"}, annotations=_web_ro("List Tabs"))(skyvern_tab_list)
mcp.tool(tags={"tab_management", "lean"}, annotations=_web_mut("Open New Tab"))(skyvern_tab_new)
mcp.tool(tags={"tab_management", "browser_primitive", "lean"}, annotations=_web_mut("Open Tabs"))(
    size_capped(skyvern_open_tabs)
)
mcp.tool(tags={"tab_management", "lean"}, annotations=_mut("Switch Tab"))(skyvern_tab_switch)
mcp.tool(tags={"tab_management", "lean"}, annotations=_dest("Close Tab"))(skyvern_tab_close)
mcp.tool(tags={"tab_management", "lean"}, annotations=_web_ro("Wait for New Tab"))(skyvern_tab_wait_for_new)

# -- Frame management (iframe switching) --
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_mut("Switch Iframe"))(skyvern_frame_switch)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_mut("Switch to Main Frame"))(skyvern_frame_main)
mcp.tool(tags={"browser_primitive", "lean"}, annotations=_web_ro("List Iframes"))(skyvern_frame_list)

# -- Auth state persistence --
mcp.tool(tags={"state"}, annotations=_web_mut("Save Browser State"))(skyvern_state_save)
mcp.tool(tags={"state"}, annotations=_web_mut("Load Browser State"))(skyvern_state_load)

# -- Inspection tools (console, network, dialog, page errors, DOM) --
# console_messages, network_requests, handle_dialog, and get_errors accept
# clear=True, which mutates captured session buffers.
mcp.tool(tags={"inspection"}, annotations=_web_mut("Get Console Messages"))(skyvern_console_messages)
mcp.tool(tags={"inspection"}, annotations=_web_mut("Get Network Requests"))(size_capped(skyvern_network_requests))
mcp.tool(tags={"inspection"}, annotations=_web_ro("Get Network Request Detail"))(skyvern_network_request_detail)
mcp.tool(tags={"inspection"}, annotations=_web_mut("Route Network Requests"))(skyvern_network_route)
mcp.tool(tags={"inspection"}, annotations=_web_mut("Remove Network Route"))(skyvern_network_unroute)
mcp.tool(tags={"inspection"}, annotations=_web_mut("Handle Browser Dialog"))(skyvern_handle_dialog)
mcp.tool(tags={"inspection"}, annotations=_web_mut("Get Page Errors"))(skyvern_get_errors)
mcp.tool(tags={"inspection"}, annotations=_web_mut("Start HAR Recording"))(skyvern_har_start)
mcp.tool(tags={"inspection"}, annotations=_web_mut("Stop HAR Recording"))(size_capped(skyvern_har_stop))
mcp.tool(tags={"inspection", "lean"}, annotations=_web_ro("Get Element HTML"))(size_capped(skyvern_get_html))
mcp.tool(tags={"inspection", "lean"}, annotations=_web_ro("Get Element Value"))(skyvern_get_value)
mcp.tool(tags={"inspection"}, annotations=_web_ro("Get Element Styles"))(skyvern_get_styles)

# -- Web storage (sessionStorage + localStorage) --
mcp.tool(tags={"storage"}, annotations=_web_ro("Get Session Storage"))(skyvern_get_session_storage)
mcp.tool(tags={"storage"}, annotations=_web_mut("Set Session Storage"))(skyvern_set_session_storage)
mcp.tool(tags={"storage"}, annotations=_web_dest("Clear Session Storage"))(skyvern_clear_session_storage)
mcp.tool(tags={"storage"}, annotations=_web_dest("Clear Local Storage"))(skyvern_clear_local_storage)

# -- Block discovery + validation (no browser needed) --
mcp.tool(tags={"block_discovery"}, annotations=_ro("Get Workflow Block Schema"))(skyvern_block_schema)
mcp.tool(tags={"block_discovery"}, annotations=_ro("Validate Workflow Block"))(skyvern_block_validate)
mcp.tool(tags={"block_discovery"}, annotations=_ro("Lint Code Block"))(skyvern_code_block_lint)
mcp.tool(tags={"block_discovery"}, annotations=_ro("Synthesize Code Block"))(skyvern_code_block_synthesize)
mcp.tool(tags={"block_discovery"}, annotations=_ro("Get Browser Trajectory"))(skyvern_trajectory_get)

# -- Organization settings (no browser needed) --
mcp.tool(tags={"settings"}, annotations=_ro("Get Organization Settings"))(skyvern_org_get)
mcp.tool(tags={"settings"}, annotations=_mut("Update Organization Settings"))(skyvern_org_update)

# -- Credential lookup (no browser needed) --
mcp.tool(tags={"credential"}, annotations=_ro("List Credentials"))(skyvern_credential_list)
mcp.tool(tags={"credential"}, annotations=_ro("Get Credential"))(skyvern_credential_get)
mcp.tool(tags={"credential"}, annotations=_dest("Delete Credential"))(skyvern_credential_delete)
mcp.tool(tags={"credential", "onepassword"}, annotations=_ro("List 1Password Items"))(skyvern_onepassword_items)
mcp.tool(tags={"credential", "onepassword"}, annotations=_ro("Get 1Password Configuration"))(
    skyvern_onepassword_config_get
)
mcp.tool(tags={"credential", "onepassword"}, annotations=_mut("Set 1Password Configuration"))(
    skyvern_onepassword_config_set
)
mcp.tool(tags={"credential", "onepassword"}, annotations=_dest("Clear 1Password Configuration"))(
    skyvern_onepassword_config_clear
)
mcp.tool(tags={"credential", "bitwarden"}, annotations=_ro("List Bitwarden Items"))(skyvern_bitwarden_items)
mcp.tool(tags={"credential", "bitwarden"}, annotations=_ro("Get Bitwarden Configuration"))(skyvern_bitwarden_config_get)
mcp.tool(tags={"credential", "bitwarden"}, annotations=_mut("Set Bitwarden Configuration"))(
    skyvern_bitwarden_config_set
)
mcp.tool(tags={"credential", "bitwarden"}, annotations=_dest("Clear Bitwarden Configuration"))(
    skyvern_bitwarden_config_clear
)

# -- Folder management (no browser needed) --
mcp.tool(tags={"folder"}, annotations=_ro("List Folders"))(skyvern_folder_list)
mcp.tool(tags={"folder"}, annotations=_mut("Create Folder"))(skyvern_folder_create)
mcp.tool(tags={"folder"}, annotations=_ro("Get Folder"))(skyvern_folder_get)
mcp.tool(tags={"folder"}, annotations=_mut("Update Folder"))(skyvern_folder_update)
mcp.tool(tags={"folder"}, annotations=_dest("Delete Folder"))(skyvern_folder_delete)

# -- Workflow management (CRUD + execution, no browser needed) --
mcp.tool(tags={"workflow"}, annotations=_ro("List Workflows"))(size_capped(skyvern_workflow_list))
mcp.tool(tags={"workflow"}, annotations=_ro("Get Workflow"))(size_capped(guard_definition_size(skyvern_workflow_get)))
mcp.tool(tags={"workflow"}, annotations=_ro("List Workflow Runs"))(size_capped(skyvern_workflow_run_list))
mcp.tool(tags={"workflow"}, annotations=_mut("Create Workflow"))(skyvern_workflow_create)
mcp.tool(tags={"workflow"}, annotations=_mut("Update Workflow"))(skyvern_workflow_update)
mcp.tool(tags={"workflow"}, annotations=_mut("Move Workflow to Folder"))(skyvern_workflow_update_folder)
mcp.tool(tags={"workflow"}, annotations=_dest("Delete Workflow"))(skyvern_workflow_delete)
mcp.tool(tags={"workflow"}, annotations=_web_dest("Run Workflow"))(skyvern_workflow_run)
mcp.tool(tags={"workflow"}, annotations=_ro("Get Workflow Run Status"))(size_capped(skyvern_workflow_status))
mcp.tool(tags={"workflow"}, annotations=_web_dest("Retry Workflow Run"))(skyvern_workflow_retry)
mcp.tool(tags={"workflow"}, annotations=_dest("Cancel Workflow Run"))(skyvern_workflow_cancel)

# -- Schedule management (no browser needed) --
mcp.tool(tags={"schedule"}, annotations=_ro("List Workflow Schedules"))(skyvern_schedule_list)
mcp.tool(tags={"schedule"}, annotations=_ro("List Schedules for a Workflow"))(skyvern_schedule_list_for_workflow)
mcp.tool(tags={"schedule"}, annotations=_ro("Get Workflow Schedule"))(skyvern_schedule_get)
mcp.tool(tags={"schedule"}, annotations=_mut("Create Workflow Schedule"))(skyvern_schedule_create)
mcp.tool(tags={"schedule"}, annotations=_mut("Update Workflow Schedule"))(skyvern_schedule_update)
mcp.tool(tags={"schedule"}, annotations=_mut("Enable Workflow Schedule"))(skyvern_schedule_enable)
mcp.tool(tags={"schedule"}, annotations=_mut("Disable Workflow Schedule"))(skyvern_schedule_disable)
mcp.tool(tags={"schedule"}, annotations=_dest("Delete Workflow Schedule"))(skyvern_schedule_delete)

# -- Script/caching tools (no browser needed) --
mcp.tool(tags={"script"}, annotations=_ro("List Workflow Scripts"))(skyvern_script_list_for_workflow)
mcp.tool(tags={"script"}, annotations=_ro("Get Script Code"))(size_capped(skyvern_script_get_code))
mcp.tool(tags={"script"}, annotations=_ro("List Script Versions"))(skyvern_script_versions)
mcp.tool(tags={"script"}, annotations=_ro("Get Script Fallback Episodes"))(skyvern_script_fallback_episodes)
mcp.tool(tags={"script"}, annotations=_mut("Deploy Workflow Script"))(skyvern_script_deploy)

# -- Prompts (methodology guides injected into LLM conversations) --
mcp.prompt()(build_workflow)
mcp.prompt()(debug_automation)
mcp.prompt()(extract_data)
mcp.prompt()(qa_test)

# -- Schema-constrained output tools --
# size_capped: extraction returns unbounded page content, finish echoes caller output verbatim.
mcp.tool(
    description=EXTRACT_STRUCTURED_DESCRIPTION, tags={"lean"}, annotations=_web_ro("Extract Structured Data (AI)")
)(size_capped(skyvern_extract_structured))
# _ro: finish echoes its own input and touches nothing — no browser, network, or server state.
mcp.tool(description=FINISH_DESCRIPTION, tags={"lean"}, annotations=_ro("Declare Final Output"))(
    size_capped(skyvern_finish)
)
# Additive page-read surface; intentionally excluded from every existing scope.
mcp.tool(tags={"page_read", "lean"}, annotations=_web_ro("Read Page"))(size_capped(skyvern_page))

__all__ = [
    "mcp",
    # Session
    "skyvern_browser_session_create",
    "skyvern_browser_session_close",
    "skyvern_browser_session_list",
    "skyvern_browser_session_get",
    "skyvern_browser_session_connect",
    # Browser profiles
    "skyvern_browser_profile_create",
    "skyvern_browser_profile_list",
    "skyvern_browser_profile_get",
    "skyvern_browser_profile_update",
    "skyvern_browser_profile_delete",
    # Primary (AI-powered)
    "skyvern_act",
    "skyvern_extract",
    "skyvern_extract_and_screenshot",
    "skyvern_validate",
    "skyvern_run_task",
    "skyvern_login",
    "skyvern_navigate",
    "skyvern_navigate_and_screenshot",
    "skyvern_navigate_extract_and_screenshot",
    "skyvern_screenshot",
    "skyvern_evaluate",
    "skyvern_evaluate_and_screenshot",
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
    "skyvern_wait_for_either_state",
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
    "skyvern_page",
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
    "skyvern_code_block_lint",
    "skyvern_code_block_synthesize",
    "skyvern_trajectory_get",
    # Organization settings
    "skyvern_org_get",
    "skyvern_org_update",
    # Credential lookup
    "skyvern_credential_list",
    "skyvern_credential_get",
    "skyvern_credential_delete",
    "skyvern_onepassword_items",
    "skyvern_onepassword_config_get",
    "skyvern_onepassword_config_set",
    "skyvern_onepassword_config_clear",
    "skyvern_bitwarden_items",
    "skyvern_bitwarden_config_get",
    "skyvern_bitwarden_config_set",
    "skyvern_bitwarden_config_clear",
    # Folder management
    "skyvern_folder_list",
    "skyvern_folder_create",
    "skyvern_folder_get",
    "skyvern_folder_update",
    "skyvern_folder_delete",
    # Workflow management
    "skyvern_workflow_list",
    "skyvern_workflow_get",
    "skyvern_workflow_run_list",
    "skyvern_workflow_create",
    "skyvern_workflow_update",
    "skyvern_workflow_update_folder",
    "skyvern_workflow_delete",
    "skyvern_workflow_run",
    "skyvern_workflow_status",
    "skyvern_workflow_retry",
    "skyvern_workflow_cancel",
    # Schedule management
    "skyvern_schedule_list",
    "skyvern_schedule_list_for_workflow",
    "skyvern_schedule_get",
    "skyvern_schedule_create",
    "skyvern_schedule_update",
    "skyvern_schedule_enable",
    "skyvern_schedule_disable",
    "skyvern_schedule_delete",
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
    # Schema-constrained output
    "skyvern_extract_structured",
    "skyvern_finish",
]

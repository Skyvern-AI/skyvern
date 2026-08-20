from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP
from fastmcp.server.transforms.visibility import Visibility

MCPScope = Literal["all", "operate", "build", "browser", "lean"]

# `operate` is deliberately browser-free: run, monitor and inspect automations that already
# exist. `state` lives with `build` rather than here because both its tools are Save/Load
# *Browser* State and return no_browser_error() without a live page — in a scope with no way
# to open one they are dead surface.
OPERATE_SCOPE = {"workflow", "schedule", "folder", "script"}
SCOPES: dict[MCPScope, set[str]] = {
    "operate": OPERATE_SCOPE,
    # `session` is required, not cosmetic: without it build's inspection and state tools have no
    # way to obtain a browser and dead-end on NO_ACTIVE_BROWSER. `browser_primitive` stays out —
    # it would grow build to 85 tools and collapse the boundary with the browser scope.
    "build": OPERATE_SCOPE | {"block_discovery", "inspection", "ai_powered", "session", "state"},
    "browser": {"browser_primitive", "tab_management", "session", "browser_profile", "inspection"},
    "lean": {"lean"},
}


def apply_scope(mcp: FastMCP, scope: MCPScope) -> None:
    if scope == "all":
        return

    # Scope filtering is tool-only: first hide every tool, then re-enable tools tagged for the scope.
    mcp.add_transform(Visibility(False, components={"tool"}))
    mcp.add_transform(Visibility(True, tags=set(SCOPES[scope]), components={"tool"}))

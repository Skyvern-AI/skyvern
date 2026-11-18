from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP
from fastmcp.server.transforms.visibility import Visibility

MCPScope = Literal["all", "operate", "build", "browser", "lean"]

# `operate` opens no MCP-side browser (`ember` fits because Ember drives its own server-side browser).
# `state` lives with `build` because its Save/Load *Browser* State tools need a live page and are dead surface here.
OPERATE_SCOPE = {"workflow", "schedule", "folder", "script", "feedback", "ember"}
SCOPES: dict[MCPScope, set[str]] = {
    "operate": OPERATE_SCOPE,
    # `session` is required, not cosmetic: without it build's inspection and state tools have no
    # way to obtain a browser and dead-end on NO_ACTIVE_BROWSER. `browser_primitive` stays out —
    # it would collapse the boundary with the browser scope.
    "build": OPERATE_SCOPE | {"block_discovery", "inspection", "ai_powered", "session", "state"},
    "browser": {"browser_primitive", "tab_management", "session", "browser_profile", "inspection", "feedback"},
    "lean": {"lean", "feedback"},
}


def apply_scope(mcp: FastMCP, scope: MCPScope) -> None:
    if scope == "all":
        return

    # Scope filtering is tool-only: first hide every tool, then re-enable tools tagged for the scope.
    mcp.add_transform(Visibility(False, components={"tool"}))
    mcp.add_transform(Visibility(True, tags=set(SCOPES[scope]), components={"tool"}))

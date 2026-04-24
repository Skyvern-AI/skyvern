"""Registry-level invariant: every registered MCP tool must carry a human-readable title.

The Claude Connectors Directory submission form rejects servers whose tools
are missing `title` in `ToolAnnotations` (the raw snake_case function name is
not user-facing).
"""

from __future__ import annotations

import pytest

from skyvern.cli.mcp_tools import mcp


@pytest.mark.asyncio
async def test_every_tool_has_a_title() -> None:
    tools = await mcp.list_tools()
    assert tools, "MCP server registered zero tools"

    missing = [t.name for t in tools if t.annotations is None or not t.annotations.title]
    assert not missing, f"Tools missing title annotation: {missing}"


@pytest.mark.asyncio
async def test_every_tool_has_read_only_hint() -> None:
    tools = await mcp.list_tools()
    assert tools, "MCP server registered zero tools"

    missing = [t.name for t in tools if t.annotations is None or t.annotations.readOnlyHint is None]
    assert not missing, f"Tools missing readOnlyHint annotation: {missing}"


@pytest.mark.asyncio
async def test_destructive_tools_flagged() -> None:
    """Tools that delete / close / cancel must carry destructiveHint=True."""
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}

    # A representative sample — extending this set is fine, but none of the
    # listed tools should silently lose their destructive annotation. The
    # three AI-driven / eval tools (`skyvern_act`, `skyvern_run_task`,
    # `skyvern_evaluate`) are included because a user-supplied prompt or
    # JavaScript expression can mutate the page destructively; the
    # `destructiveHint` tells the client's consent surface so.
    expected_destructive = {
        "skyvern_browser_session_close",
        "skyvern_tab_close",
        "skyvern_clear_session_storage",
        "skyvern_clear_local_storage",
        "skyvern_credential_delete",
        "skyvern_folder_delete",
        "skyvern_workflow_delete",
        "skyvern_workflow_cancel",
        "skyvern_act",
        "skyvern_run_task",
        "skyvern_evaluate",
    }

    for name in expected_destructive:
        tool = by_name.get(name)
        assert tool is not None, f"Expected tool not registered: {name}"
        assert tool.annotations is not None, f"Tool missing annotations: {name}"
        assert tool.annotations.destructiveHint is True, f"Tool {name} expected destructiveHint=True"


@pytest.mark.asyncio
async def test_read_only_sampling_marked_read_only() -> None:
    """Sanity check that known read-only tools keep readOnlyHint=True."""
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}

    expected_ro = {
        "skyvern_browser_session_list",
        "skyvern_browser_session_get",
        "skyvern_extract",
        "skyvern_validate",
        "skyvern_screenshot",
        "skyvern_find",
        "skyvern_get_html",
        "skyvern_workflow_list",
        "skyvern_workflow_get",
    }

    for name in expected_ro:
        tool = by_name.get(name)
        assert tool is not None, f"Expected tool not registered: {name}"
        assert tool.annotations is not None, f"Tool missing annotations: {name}"
        assert tool.annotations.readOnlyHint is True, f"Tool {name} expected readOnlyHint=True"

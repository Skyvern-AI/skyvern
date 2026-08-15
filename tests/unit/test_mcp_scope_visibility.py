from __future__ import annotations

import importlib.util
import inspect
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, get_args, get_type_hints
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from typer.testing import CliRunner

from skyvern.cli.mcp_tools.scopes import MCPScope

REPOSITORY_ROOT = Path(__file__).parents[2]
SCOPES_PATH = REPOSITORY_ROOT / "skyvern/cli/mcp_tools/scopes.py"


def _load_scopes_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mcp_scopes_under_test", SCOPES_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scoped_mcp() -> Iterator[FastMCP]:
    from skyvern.cli.mcp_tools import mcp

    original_transforms = list(mcp._transforms)
    try:
        yield mcp
    finally:
        mcp._transforms[:] = original_transforms


async def _tool_names(server: FastMCP) -> set[str]:
    return {tool.name for tool in await server.list_tools(run_middleware=False)}


async def _prompt_names(server: FastMCP) -> set[str]:
    return {prompt.name for prompt in await server.list_prompts()}


async def _tool_tags(server: FastMCP) -> set[str]:
    return {tag for tool in await server.list_tools(run_middleware=False) for tag in tool.tags}


def test_scope_tag_sets_are_exact() -> None:
    scopes = _load_scopes_module()

    operate = {"workflow", "schedule", "folder", "script"}
    assert scopes.SCOPES == {
        "operate": operate,
        "build": operate | {"block_discovery", "inspection", "ai_powered", "session", "state"},
        "browser": {"browser_primitive", "tab_management", "session", "browser_profile", "inspection"},
        "lean": {"lean"},
    }


def test_cli_scope_choices_match_scope_map() -> None:
    from skyvern.cli.run_commands import run_mcp
    from skyvern.cli.setup_commands import MCPToolScope

    scopes = _load_scopes_module()
    scope_annotation = get_type_hints(run_mcp, include_extras=True)["scope"]
    scope_literal = get_args(scope_annotation)[0]

    assert set(get_args(scope_literal)) == {"all", *scopes.SCOPES}
    assert {scope.value for scope in MCPToolScope} == {"all", *scopes.SCOPES}


def test_run_mcp_scope_default_is_all() -> None:
    from skyvern.cli.run_commands import run_mcp

    assert inspect.signature(run_mcp).parameters["scope"].default == "all"


def test_all_adds_no_transforms(monkeypatch: pytest.MonkeyPatch) -> None:
    scopes = _load_scopes_module()
    server = FastMCP("scope-test")
    add_transform = MagicMock()
    monkeypatch.setattr(server, "add_transform", add_transform)

    scopes.apply_scope(server, "all")

    add_transform.assert_not_called()


def test_operate_adds_exact_transforms_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    scopes = _load_scopes_module()
    created: list[tuple[object, dict[str, Any], object]] = []

    class RecordedVisibility:
        def __init__(self, enabled: bool, **kwargs: Any) -> None:
            created.append((enabled, kwargs, self))

    server = FastMCP("scope-test")
    added: list[object] = []
    monkeypatch.setattr(scopes, "Visibility", RecordedVisibility)
    monkeypatch.setattr(server, "add_transform", added.append)

    scopes.apply_scope(server, "operate")

    assert [(enabled, kwargs) for enabled, kwargs, _ in created] == [
        (False, {"components": {"tool"}}),
        (True, {"tags": set(scopes.SCOPES["operate"]), "components": {"tool"}}),
    ]
    assert added == [transform for _, _, transform in created]


@pytest.mark.asyncio
async def test_operate_filters_only_tools_through_in_memory_client() -> None:
    scopes = _load_scopes_module()
    server = FastMCP("scope-test")

    @server.tool(tags={"workflow"})
    def included_tool() -> str:
        return "included"

    @server.tool(tags={"credential"})
    def excluded_tool() -> str:
        return "excluded"

    @server.prompt
    def untagged_prompt() -> str:
        return "still visible"

    scopes.apply_scope(server, "operate")

    async with Client(server) as client:
        tools = {tool.name for tool in await client.list_tools()}
        prompts = {prompt.name for prompt in await client.list_prompts()}

    assert tools == {"included_tool"}
    assert prompts == {"untagged_prompt"}


@pytest.mark.asyncio
async def test_real_server_all_scope_keeps_default_surface(scoped_mcp: FastMCP) -> None:
    from skyvern.cli.mcp_tools.scopes import apply_scope

    before_tools = await _tool_names(scoped_mcp)
    before_prompts = await _prompt_names(scoped_mcp)

    apply_scope(scoped_mcp, "all")

    assert await _tool_names(scoped_mcp) == before_tools
    assert await _prompt_names(scoped_mcp) == before_prompts


@pytest.mark.asyncio
async def test_scope_tags_exist_on_real_registered_tools(scoped_mcp: FastMCP) -> None:
    from skyvern.cli.mcp_tools.scopes import SCOPES

    live_tags = await _tool_tags(scoped_mcp)

    assert {tag for scope_tags in SCOPES.values() for tag in scope_tags} <= live_tags


@pytest.mark.asyncio
async def test_every_real_scope_is_non_empty_strict_subset(scoped_mcp: FastMCP) -> None:
    from skyvern.cli.mcp_tools.scopes import SCOPES, apply_scope

    full_tools = await _tool_names(scoped_mcp)

    for scope in SCOPES:
        scoped_mcp._transforms.clear()
        apply_scope(scoped_mcp, scope)
        scoped_tools = await _tool_names(scoped_mcp)

        assert scoped_tools
        assert scoped_tools < full_tools


# Resolved sizes are pinned deliberately. The relational guards above pass when a tag is renamed
# on ONE tool of many — the tag survives elsewhere, the subset stays strict, and the scope
# silently loses a tool. Only an external oracle catches that, so these numbers are a ratchet:
# adding or re-tagging a tool must be a conscious edit here, reviewed alongside the scope map.
RESOLVED_SCOPE_SIZES: dict[MCPScope, int] = {"operate": 29, "build": 60, "browser": 54, "lean": 33}

# Same ratchet for the unfiltered (`all`) surface. Kept in one place because duplicated copies
# drift: a registration that bumps only its own copy leaves the others to fail in CI.
TOTAL_TOOL_COUNT: int = 115


@pytest.mark.asyncio
async def test_real_scopes_resolve_to_pinned_sizes(scoped_mcp: FastMCP) -> None:
    from skyvern.cli.mcp_tools.scopes import SCOPES, apply_scope

    assert set(RESOLVED_SCOPE_SIZES) == set(SCOPES)

    for scope, expected in RESOLVED_SCOPE_SIZES.items():
        scoped_mcp._transforms.clear()
        apply_scope(scoped_mcp, scope)

        assert len(await _tool_names(scoped_mcp)) == expected, scope


@pytest.mark.asyncio
async def test_lean_scope_resolves_to_exact_existing_tools(scoped_mcp: FastMCP) -> None:
    from skyvern.cli.mcp_tools.scopes import apply_scope

    expected = {
        "skyvern_browser_session_create",
        "skyvern_browser_session_close",
        "skyvern_browser_session_list",
        "skyvern_browser_session_get",
        "skyvern_browser_session_connect",
        "skyvern_login",
        "skyvern_navigate",
        "skyvern_screenshot",
        "skyvern_click",
        "skyvern_type",
        "skyvern_select_option",
        "skyvern_press_key",
        "skyvern_scroll",
        "skyvern_hover",
        "skyvern_drag",
        "skyvern_file_upload",
        "skyvern_wait",
        "skyvern_wait_for_either_state",
        "skyvern_find",
        "skyvern_get_html",
        "skyvern_get_value",
        "skyvern_tab_list",
        "skyvern_tab_new",
        "skyvern_open_tabs",
        "skyvern_tab_switch",
        "skyvern_tab_close",
        "skyvern_tab_wait_for_new",
        "skyvern_frame_switch",
        "skyvern_frame_main",
        "skyvern_frame_list",
        # main #14957 tagged its schema-constrained output tools `lean` on arrival.
        "skyvern_extract_structured",
        "skyvern_finish",
        # main #14936 tagged its paginated page reader `lean` on arrival.
        "skyvern_page",
    }

    scoped_mcp._transforms.clear()
    apply_scope(scoped_mcp, "lean")

    assert await _tool_names(scoped_mcp) == expected


@pytest.mark.asyncio
async def test_hidden_tools_are_not_listed_or_callable_from_focused_scopes(scoped_mcp: FastMCP) -> None:
    """Credential tags are excluded on purpose: the family includes destructive tools."""
    from skyvern.cli.mcp_tools.scopes import SCOPES, apply_scope

    excluded = {"credential", "onepassword", "bitwarden", "storage", "settings"}
    full_tools = await _tool_names(scoped_mcp)
    credential_tools = {
        tool.name for tool in await scoped_mcp.list_tools(run_middleware=False) if set(tool.tags) & excluded
    }
    assert credential_tools, "expected credential-tagged tools to exist; the tag vocabulary moved"
    assert "skyvern_credential_list" in credential_tools
    assert "skyvern_navigate" in full_tools

    for scope in SCOPES:
        scoped_mcp._transforms.clear()
        apply_scope(scoped_mcp, scope)

        assert not (await _tool_names(scoped_mcp)) & credential_tools, scope

        # Call-side enforcement, not just listing: probe tools each scope must refuse.
        probes = {
            "operate": ("skyvern_credential_list", "skyvern_navigate"),
            "lean": ("skyvern_credential_list", "skyvern_evaluate"),
        }.get(scope)
        if probes:
            listed = await _tool_names(scoped_mcp)
            async with Client(scoped_mcp) as client:
                for tool_name in probes:
                    assert tool_name not in listed, (scope, tool_name)
                    with pytest.raises(ToolError, match=f"Unknown tool: '{tool_name}'"):
                        await client.call_tool(tool_name, {})


def test_run_mcp_cli_scope_operate_reaches_apply_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli import run_commands
    from skyvern.cli.mcp_tools import mcp, scopes
    from skyvern.cli.run_commands import run_app

    calls: list[tuple[FastMCP, str]] = []

    async def fake_run_mcp_with_cleanup(*_args: Any, **_kwargs: Any) -> None:
        return None

    def fake_apply_scope(server: FastMCP, scope: str) -> None:
        calls.append((server, scope))

    monkeypatch.setattr(run_commands, "prepare_cli_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_commands, "_run_mcp_with_cleanup", fake_run_mcp_with_cleanup)
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_sync", lambda: None)
    monkeypatch.setattr(run_commands, "_start_stdin_eof_watcher", lambda: (None, None))
    monkeypatch.setattr(scopes, "apply_scope", fake_apply_scope)

    result = CliRunner().invoke(run_app, ["mcp", "--transport", "streamable-http", "--scope", "operate"])

    assert result.exit_code == 0, result.output
    assert calls == [(mcp, "operate")]


def test_run_mcp_cli_selects_lean_instructions_once_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli import run_commands
    from skyvern.cli.mcp_tools import mcp, scopes
    from skyvern.cli.mcp_tools.instructions import LEAN_INSTRUCTIONS
    from skyvern.cli.run_commands import run_app

    calls: list[str] = []

    async def fake_run_mcp_with_cleanup(*_args: Any, **_kwargs: Any) -> None:
        calls.append(mcp.instructions)

    # Not a no-op: boot mutates the mcp singleton; monkeypatch restores it so it cannot leak.
    monkeypatch.setattr(mcp, "instructions", mcp.instructions)
    monkeypatch.setattr(run_commands, "prepare_cli_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_commands, "_run_mcp_with_cleanup", fake_run_mcp_with_cleanup)
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_sync", lambda: None)
    monkeypatch.setattr(run_commands, "_start_stdin_eof_watcher", lambda: (None, None))
    monkeypatch.setattr(scopes, "apply_scope", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(run_app, ["mcp", "--transport", "streamable-http", "--scope", "lean"])

    assert result.exit_code == 0, result.output
    assert calls == [LEAN_INSTRUCTIONS]


def test_cli_rejects_invalid_scope_as_typed_choice() -> None:
    from skyvern.cli.run_commands import run_app

    result = CliRunner().invoke(run_app, ["mcp", "--scope", "invalid"])

    assert result.exit_code == 2
    # CI renders typer errors through rich (ANSI codes + box-drawn panel with
    # line wrapping), local terminals may render plain — normalize both before
    # asserting so the test is independent of the error-rendering backend.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    normalized = " ".join(plain.replace("│", " ").split())
    assert "Invalid value for '--scope'" in normalized

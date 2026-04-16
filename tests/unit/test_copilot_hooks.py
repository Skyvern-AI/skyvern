"""Tests for CopilotRunHooks.on_tool_end activity recording."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks


@dataclass
class _FakeContext:
    tool_activity: list[dict[str, Any]] = field(default_factory=list)


# `on_tool_end(context, agent, tool, result)` only reads `tool` and `result`;
# `context` and `agent` are unused by CopilotRunHooks, so a single sentinel
# mock stands in for both across every test.
_UNUSED = MagicMock()


def _fake_tool(name: str) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


def _mcp_text_output(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Shape that `parse_tool_output` expects from FastMCP tool calls."""
    return [{"type": "text", "text": json.dumps(payload)}]


@pytest.mark.asyncio
async def test_on_tool_end_appends_generic_tool_entry() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": True, "data": {"url": "https://example.com"}})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("navigate_browser"), output)

    assert len(ctx.tool_activity) == 1
    entry = ctx.tool_activity[0]
    assert entry["tool"] == "navigate_browser"
    assert "summary" in entry
    assert "output_preview" not in entry  # non-whitelisted tool


@pytest.mark.asyncio
async def test_on_tool_end_whitelisted_tool_produces_output_preview() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output(
        {
            "ok": True,
            "data": {
                "blocks": [
                    {"label": "extract_prices", "output": {"prices": [10, 20]}},
                    {"label": "extract_names", "extracted_data": ["alice"]},
                ]
            },
        }
    )
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("run_blocks_and_collect_debug"), output)

    entry = ctx.tool_activity[0]
    assert entry["tool"] == "run_blocks_and_collect_debug"
    assert "output_preview" in entry
    assert "extract_prices" in entry["output_preview"]
    assert "extract_names" in entry["output_preview"]


@pytest.mark.asyncio
async def test_on_tool_end_truncates_output_preview_at_500_chars() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    huge_output = {"values": ["x" * 1000]}
    output = _mcp_text_output({"ok": True, "data": {"blocks": [{"label": "big", "output": huge_output}]}})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("get_run_results"), output)

    entry = ctx.tool_activity[0]
    preview = entry["output_preview"]
    _prefix, _sep, value = preview.partition(": ")
    assert value.endswith("...")
    assert len(value) <= 503


@pytest.mark.asyncio
async def test_on_tool_end_whitelisted_tool_without_block_outputs_skips_preview() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": True, "data": {"blocks": [{"label": "noop"}]}})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("update_and_run_blocks"), output)

    entry = ctx.tool_activity[0]
    assert entry["tool"] == "update_and_run_blocks"
    assert "output_preview" not in entry


@pytest.mark.asyncio
async def test_on_tool_end_failed_whitelisted_tool_skips_preview() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": False, "error": "workflow exploded"})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("run_blocks_and_collect_debug"), output)

    entry = ctx.tool_activity[0]
    assert "output_preview" not in entry


@pytest.mark.asyncio
async def test_on_tool_end_swallows_unserializable_output() -> None:
    # json.dumps(default=str) can still raise if str() on the value raises --
    # on_tool_end must never propagate that into the agent loop.
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    class _Unserializable:
        def __str__(self) -> str:
            raise RuntimeError("str boom")

    payload = {"ok": True, "data": {"blocks": [{"label": "bad", "output": _Unserializable()}]}}
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("run_blocks_and_collect_debug"), payload)

    # The recording path raised inside json.dumps before append. The guard
    # swallowed it, so the invariant is "the run did not crash" -- and the
    # activity entry was dropped. That is the acceptable trade for observability.
    assert ctx.tool_activity == []


class TestCopilotToCallToolResult:
    @staticmethod
    def _build(d: dict) -> Any:
        from skyvern.forge.sdk.copilot.mcp_adapter import _copilot_to_call_tool_result

        return _copilot_to_call_tool_result(d)

    def test_text_only_result(self) -> None:
        result = self._build({"ok": True, "data": "done"})
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.isError is False

    def test_screenshot_payload_always_text_only(self) -> None:
        """Tool results never include images — screenshots are injected
        as synthetic user messages by the enforcement loop instead."""
        result = self._build({"ok": True, "data": {"screenshot_base64": "iVBOR"}})
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        parsed = json.loads(result.content[0].text)
        assert parsed["data"]["screenshot_base64"].startswith("[base64 image omitted")

    def test_error_result(self) -> None:
        result = self._build({"ok": False, "error": "fail"})
        assert result.isError is True
        parsed = json.loads(result.content[0].text)
        assert parsed["ok"] is False
        assert parsed["error"] == "fail"

    def test_text_content_is_json(self) -> None:
        data = {"ok": True, "data": {"count": 5}}
        result = self._build(data)
        parsed = json.loads(result.content[0].text)
        assert parsed == data


class TestSchemaOverlay:
    def test_apply_schema_overlay_hides_params(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _apply_schema_overlay

        schema = {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "session_id": {"type": "string"},
                "cdp_url": {"type": "string"},
            },
            "required": ["url", "session_id"],
        }
        overlay = SchemaOverlay(
            hide_params=frozenset({"session_id", "cdp_url"}),
        )
        result = _apply_schema_overlay(schema, overlay)
        assert "session_id" not in result["properties"]
        assert "cdp_url" not in result["properties"]
        assert "url" in result["properties"]
        assert "session_id" not in result["required"]

    def test_apply_schema_overlay_renames_args(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _apply_schema_overlay

        schema = {
            "type": "object",
            "properties": {
                "clear": {"type": "boolean"},
                "text": {"type": "string"},
            },
            "required": ["clear", "text"],
        }
        overlay = SchemaOverlay(
            arg_transforms={"clear_first": "clear"},
        )
        result = _apply_schema_overlay(schema, overlay)
        assert "clear_first" in result["properties"]
        assert "clear" not in result["properties"]
        assert "clear_first" in result["required"]

    def test_transform_args_reverses_and_injects(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _transform_args

        overlay = SchemaOverlay(
            arg_transforms={"clear_first": "clear"},
            forced_args={"inline": True},
        )
        args = {"clear_first": True, "text": "hello"}
        result = _transform_args(args, overlay)
        assert result == {"clear": True, "text": "hello", "inline": True}
        assert "clear_first" not in result


class TestMCPToolOverlayCompleteness:
    """Verify alias map and overlay configs are in sync and complete."""

    def test_alias_map_covers_expected_tools(self) -> None:
        from skyvern.forge.sdk.copilot.tools import get_skyvern_mcp_alias_map

        alias_map = get_skyvern_mcp_alias_map()
        expected_aliases = {
            "get_block_schema",
            "validate_block",
            "navigate_browser",
            "get_browser_screenshot",
            "evaluate",
            "click",
            "type_text",
            "scroll",
            "console_messages",
            "select_option",
            "press_key",
        }
        assert set(alias_map.keys()) == expected_aliases
        assert all(v.startswith("skyvern_") for v in alias_map.values())

    def test_every_alias_has_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import (
            _build_skyvern_mcp_overlays,
            get_skyvern_mcp_alias_map,
        )

        alias_map = get_skyvern_mcp_alias_map()
        overlays = _build_skyvern_mcp_overlays()
        missing = set(alias_map.keys()) - set(overlays.keys())
        assert not missing, f"Alias map keys missing overlay configs: {missing}"

    def test_browser_tools_require_browser(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        browser_tools = {
            "navigate_browser",
            "get_browser_screenshot",
            "evaluate",
            "click",
            "type_text",
            "scroll",
            "console_messages",
            "select_option",
            "press_key",
        }
        for name in browser_tools:
            assert overlays[name].requires_browser, f"{name} should have requires_browser=True"

    def test_intent_not_hidden_on_browser_tools(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        tools_with_intent = {"click", "type_text", "scroll", "select_option", "press_key"}
        for name in tools_with_intent:
            hidden = overlays[name].hide_params or frozenset()
            assert "intent" not in hidden, f"{name} should NOT hide intent"


class TestNewToolOverlayConfigs:
    """Verify the 4 new tool overlay configs are correct."""

    def test_scroll_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["scroll"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url"})
        assert overlay.requires_browser is True
        assert overlay.post_hook is not None

    def test_console_messages_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["console_messages"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url"})
        assert overlay.requires_browser is True
        assert overlay.post_hook is None

    def test_select_option_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["select_option"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url", "timeout"})
        assert overlay.required_overrides == ["value"]
        assert overlay.requires_browser is True
        assert overlay.timeout == 15
        assert overlay.post_hook is not None

    def test_press_key_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["press_key"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url"})
        assert overlay.required_overrides == ["key"]
        assert overlay.requires_browser is True
        assert overlay.post_hook is not None


class TestNewToolSummaries:
    """Verify summarize_tool_result handles the 4 new tools."""

    @staticmethod
    def _summarize(name: str, result: dict[str, Any]) -> str:
        from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result

        return summarize_tool_result(name, result)

    def test_scroll_summary(self) -> None:
        result = {"ok": True, "data": {"direction": "down", "amount": 500}}
        assert "down" in self._summarize("scroll", result)

    def test_console_messages_summary(self) -> None:
        result = {"ok": True, "data": {"count": 3}}
        summary = self._summarize("console_messages", result)
        assert "3" in summary
        assert "console" in summary.lower()

    def test_select_option_summary(self) -> None:
        result = {"ok": True, "data": {"value": "USD", "selector": "#currency"}}
        assert "USD" in self._summarize("select_option", result)

    def test_press_key_summary(self) -> None:
        result = {"ok": True, "data": {"key": "Enter"}}
        assert "Enter" in self._summarize("press_key", result)


class TestObservationToolsSet:
    """Verify _OBSERVATION_TOOLS includes all browser interaction tools."""

    def test_contains_new_tools(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _OBSERVATION_TOOLS

        expected = {"scroll", "console_messages", "select_option", "press_key"}
        assert expected.issubset(_OBSERVATION_TOOLS)

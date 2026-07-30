"""Unit tests for MCP response size cap."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from skyvern.cli.core.result import Artifact, BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.response import (
    MCP_MAX_RESPONSE_CHARS,
    size_capped,
    truncate_response,
)


def test_truncate_response_passes_small_payload_unchanged() -> None:
    small = {"ok": True, "data": {"items": list(range(10))}}
    assert truncate_response(small) is small


def test_truncate_response_wraps_large_payload_with_envelope() -> None:
    # Construct a payload larger than the default cap.
    big_payload = "x" * (MCP_MAX_RESPONSE_CHARS + 100)
    large = {"ok": True, "data": {"body": big_payload}}

    result = truncate_response(large)

    assert result is not large
    assert result["_truncated"] is True
    assert result["_max_chars"] == MCP_MAX_RESPONSE_CHARS
    assert result["_original_chars"] > MCP_MAX_RESPONSE_CHARS
    assert "Narrow the query" in result["_hint"]
    # Original top-level `ok` is preserved so callers reading .ok still work.
    assert result["ok"] is True
    # The oversized payload itself is dropped.
    assert "data" not in result


def test_truncate_response_preserves_top_level_error_on_overflow() -> None:
    large = {
        "ok": False,
        "error": {"code": "TIMEOUT", "message": "page did not load"},
        # Padding to push the total over the cap.
        "debug": "y" * (MCP_MAX_RESPONSE_CHARS + 50),
    }
    result = truncate_response(large)
    assert result["_truncated"] is True
    assert result["ok"] is False
    assert result["error"] == {"code": "TIMEOUT", "message": "page did not load"}


def test_truncate_response_preserves_identifier_fields_on_overflow() -> None:
    # A tool that returns identifier fields alongside a bulky payload should
    # retain those identifiers in the envelope so the caller can re-query.
    large = {
        "ok": True,
        "workflow_id": "wpid_abc123",
        "run_id": "wr_xyz789",
        "session_id": "pbs_qqq000",
        "timestamp": "ignored",
        "count": 12345,
        "data": {"blob": "z" * (MCP_MAX_RESPONSE_CHARS + 500)},
    }
    result = truncate_response(large)
    assert result["_truncated"] is True
    assert result["ok"] is True
    assert result["workflow_id"] == "wpid_abc123"
    assert result["run_id"] == "wr_xyz789"
    assert result["session_id"] == "pbs_qqq000"
    # Keys that do not end with `_id` are not preserved.
    assert "timestamp" not in result
    assert "count" not in result
    # The oversized payload itself is dropped.
    assert "data" not in result


def test_truncate_response_caps_oversize_error_field() -> None:
    # Pathological input: the `error` field itself is bigger than the cap
    # (e.g. a full HTML dump or stack trace serialized into `error.message`).
    # Without bounding, copying it verbatim into the envelope would blow the
    # envelope past max_chars and break the "under cap" contract.
    large_error_message = "x" * (MCP_MAX_RESPONSE_CHARS + 500)
    large = {
        "ok": False,
        "error": {"code": "INTERNAL", "message": large_error_message},
        "data": {"n": 1},
    }
    result = truncate_response(large)
    assert result["_truncated"] is True
    assert result["ok"] is False
    # The oversized error payload is replaced with a structured placeholder,
    # not copied verbatim.
    assert result["error"] != large["error"]
    assert isinstance(result["error"], dict)
    assert "_original_error_chars" in result["error"]
    assert result["error"]["_error_preview"].endswith("... [truncated]")
    # Envelope itself stays under the cap (module contract).
    assert len(json.dumps(result, ensure_ascii=False)) <= MCP_MAX_RESPONSE_CHARS


def test_truncate_response_drops_oversize_identifier_values() -> None:
    # An identifier value that itself exceeds the per-value cap is dropped so
    # the envelope cannot be re-inflated past the overall limit.
    large = {
        "ok": True,
        "short_id": "abc",
        "huge_id": "x" * 10_000,
        "data": "y" * (MCP_MAX_RESPONSE_CHARS + 100),
    }
    result = truncate_response(large)
    assert result["_truncated"] is True
    assert result["short_id"] == "abc"
    assert "huge_id" not in result


def test_truncate_response_accepts_custom_max() -> None:
    payload = {"data": "z" * 200}
    # payload JSON is ~213 chars; cap at 100 forces truncation.
    result = truncate_response(payload, max_chars=100)
    assert result["_truncated"] is True
    assert result["_max_chars"] == 100


def test_truncate_response_non_dict_overflow_wraps_into_envelope() -> None:
    # A tool that returns a raw list (unusual but legal) should still be guarded.
    big_list = ["x" * 100] * 2000
    result = truncate_response(big_list)
    assert isinstance(result, dict)
    assert result["_truncated"] is True
    assert "ok" not in result


def test_truncate_response_preserves_screenshot_artifact_on_overflow() -> None:
    screenshot = {"kind": "screenshot", "path": "/tmp/screenshot.png", "mime": "image/png"}
    result = truncate_response(
        {
            "ok": True,
            "data": {"inline": True, "mime": "image/png", "data": "x" * (MCP_MAX_RESPONSE_CHARS + 100)},
            "artifacts": [screenshot],
        }
    )

    assert result["artifacts"] == [screenshot]
    assert result["ok"] is False


def test_truncate_response_unserializable_input_returned_as_is() -> None:
    # object() is not JSON-serializable; json.dumps(..., default=str) stringifies
    # it, so the helper returns the payload unchanged (size is small).
    sentinel: dict[str, Any] = {"x": object()}
    result = truncate_response(sentinel)
    assert result is sentinel


def test_truncate_response_serialization_failure_is_fail_closed() -> None:
    # Circular references make json.dumps raise ValueError. A size cap that
    # can't measure a payload must fail CLOSED (wrap in the truncation
    # envelope) rather than passing the unmeasurable payload through.
    import sys

    circular: dict[str, Any] = {"ok": True, "error": None}
    circular["self"] = circular

    result = truncate_response(circular)

    assert result is not circular
    assert result["_truncated"] is True
    # Sentinel: unmeasurable payloads report `sys.maxsize` for
    # `_original_chars`. Locks in the fail-closed contract so an accidental
    # change (e.g. returning 0 or None on serialization error) trips here.
    assert result["_original_chars"] == sys.maxsize
    # Top-level `ok` / `error` are still preserved from the original dict so
    # callers reading those fields continue to work.
    assert result["ok"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_size_capped_decorator_no_op_for_small_result() -> None:
    @size_capped
    async def small_tool() -> dict[str, Any]:
        return {"ok": True, "data": {"n": 1}}

    result = await small_tool()
    assert result == {"ok": True, "data": {"n": 1}}


@pytest.mark.asyncio
async def test_size_capped_decorator_wraps_oversize_result() -> None:
    @size_capped
    async def big_tool() -> dict[str, Any]:
        return {"ok": True, "data": {"blob": "q" * (MCP_MAX_RESPONSE_CHARS + 500)}}

    result = await big_tool()
    assert result["_truncated"] is True
    assert result["ok"] is True
    # Re-serializing the wrapped envelope must be under the cap.
    assert len(json.dumps(result, ensure_ascii=False)) <= MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_size_capped_decorator_preserves_signature() -> None:
    @size_capped
    async def typed_tool(x: int, y: str = "default") -> dict[str, Any]:
        return {"x": x, "y": y}

    result = await typed_tool(1, y="override")
    assert result == {"x": 1, "y": "override"}
    assert typed_tool.__name__ == "typed_tool"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        pytest.param("skyvern_extract", {"prompt": "read the table"}, id="extract"),
        pytest.param("skyvern_extract_and_screenshot", {"prompt": "read the table"}, id="extract-and-screenshot"),
        pytest.param(
            "skyvern_navigate_extract_and_screenshot",
            {"url": "https://example.test", "prompt": "read the table"},
            id="navigate-extract-and-screenshot",
        ),
    ],
)
async def test_every_registered_extract_tool_caps_an_oversize_extraction(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, kwargs: dict[str, Any]
) -> None:
    """The paired capture tools return the same AI extraction as skyvern_extract, so they need the
    same envelope: an extraction that overflows the tool-result limit must not be handed back raw."""
    oversize = {"rows": "y" * (MCP_MAX_RESPONSE_CHARS + 1_000)}
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    page = SimpleNamespace(page=SimpleNamespace())
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(mcp_browser, "get_current_session", lambda: SimpleNamespace(_working_frame=None))
    monkeypatch.setattr(mcp_browser, "clear_session_ref_map", Mock())
    monkeypatch.setattr(mcp_browser, "do_extract", AsyncMock(return_value=SimpleNamespace(extracted=oversize)))
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=SimpleNamespace(url="https://example.test", title="Example")),
    )
    monkeypatch.setattr(mcp_browser, "do_screenshot", AsyncMock(return_value=SimpleNamespace(data=b"png")))
    monkeypatch.setattr(
        mcp_browser,
        "save_artifact",
        Mock(return_value=Artifact(kind="screenshot", path="/tmp/shot.png", mime="image/png", bytes=3)),
    )

    tool = await mcp.get_tool(tool_name)
    result = await tool.fn(**kwargs)

    assert result["_truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= MCP_MAX_RESPONSE_CHARS


def _stub_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    page = SimpleNamespace(
        page=SimpleNamespace(),
        _working_frame=None,
        url="https://example.test",
        evaluate=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(mcp_browser, "get_current_session", lambda: SimpleNamespace(_working_frame=None))
    monkeypatch.setattr(mcp_browser, "clear_session_ref_map", Mock())
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=SimpleNamespace(url="https://example.test", title="Example")),
    )
    monkeypatch.setattr(mcp_browser, "do_screenshot", AsyncMock(return_value=SimpleNamespace(data=b"png")))
    monkeypatch.setattr(
        mcp_browser,
        "save_artifact",
        Mock(return_value=Artifact(kind="screenshot", path="/tmp/shot.png", mime="image/png", bytes=3)),
    )


@pytest.mark.asyncio
async def test_navigate_and_screenshot_caps_an_oversize_inline_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`inline=True` puts a full-resolution base64 PNG straight into the tool result, so this tool needs
    the same envelope as its `*_and_screenshot` siblings — otherwise a big page blows the caller's
    tool-result limit with no truncation warning."""
    _stub_browser(monkeypatch)
    oversize_png = b"\x89PNG" + b"z" * MCP_MAX_RESPONSE_CHARS
    monkeypatch.setattr(mcp_browser, "do_screenshot", AsyncMock(return_value=SimpleNamespace(data=oversize_png)))

    tool = await mcp.get_tool("skyvern_navigate_and_screenshot")
    result = await tool.fn(url="https://example.test", inline=True)

    assert result["_truncated"] is True
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"
    assert "inline screenshot" in result["error"]["message"].lower()
    assert len(json.dumps(result, ensure_ascii=False)) <= MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_evaluate_and_screenshot_caps_an_oversize_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JS expression scrapes the DOM, so its return value is as unbounded as an AI extraction."""
    _stub_browser(monkeypatch)
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    page = SimpleNamespace(
        page=SimpleNamespace(),
        _working_frame=None,
        url="https://example.test",
        evaluate=AsyncMock(return_value="y" * (MCP_MAX_RESPONSE_CHARS + 1_000)),
    )
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))

    tool = await mcp.get_tool("skyvern_evaluate_and_screenshot")
    result = await tool.fn(expression="document.body.innerText")

    assert result["_truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_every_paired_capture_tool_is_registered_size_capped() -> None:
    async def _probe() -> dict[str, Any]:
        return {}

    paired_tools = [
        tool
        for tool in await mcp.list_tools()
        if tool.name.endswith("_and_screenshot") or "screenshot" in tool.parameters.get("properties", {})
    ]

    assert paired_tools
    for registered_tool in paired_tools:
        tool = await mcp.get_tool(registered_tool.name)
        assert tool.fn.__code__ is size_capped(_probe).__code__, f"{registered_tool.name} is not wrapped in size_capped"

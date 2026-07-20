"""The argument-validation middleware rejects malformed tool calls with a clear,
structured error instead of letting FastMCP raise a raw pydantic ValidationError.

Each case mirrors a real production error signature where a caller sent argument
names that don't match the tool's contract.
"""

from __future__ import annotations

import pytest

from skyvern.cli.mcp_tools import mcp


def _structured(result: object) -> dict:
    payload = getattr(result, "structured_content", None)
    assert isinstance(payload, dict), f"expected structured content dict, got {result!r}"
    return payload


async def _call(tool_name: str, arguments: dict) -> dict:
    result = await mcp.call_tool(tool_name, arguments)
    return _structured(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "unsupported", "missing"),
    [
        ("skyvern_get_errors", {"run_id": "wr_1"}, ["run_id"], []),
        ("skyvern_get_errors", {"workflow_run_id": "wr_1"}, ["workflow_run_id"], []),
        ("skyvern_wait", {"seconds": "3", "reason": "login"}, ["reason", "seconds"], []),
        ("skyvern_execute", {"tools": [{"tool": "navigate"}]}, ["tools"], ["steps"]),
        ("skyvern_script_get_code", {"workflow_id": "wpid_1"}, ["workflow_id"], ["script_id"]),
        (
            "skyvern_workflow_run",
            {"workflow_id": "wpid_1", "ai_fallback": True, "timeout": 2700},
            ["ai_fallback", "timeout"],
            [],
        ),
    ],
)
async def test_bad_arguments_rejected_with_structured_error(
    tool_name: str, arguments: dict, unsupported: list[str], missing: list[str]
) -> None:
    payload = await _call(tool_name, arguments)

    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == "INVALID_INPUT"
    details = error["details"]
    assert details["unsupported_arguments"] == unsupported
    assert details["missing_required_arguments"] == missing
    # The rejection names the accepted arguments so the model can self-correct.
    assert details["expected_arguments"]
    for bad in unsupported:
        assert bad not in details["expected_arguments"]


@pytest.mark.asyncio
async def test_valid_argument_shape_is_not_blocked() -> None:
    """A validly-shaped call is not short-circuited by the middleware.

    ``skyvern_get_errors`` accepts ``text``; with no browser session it fails
    downstream, but with a tool-owned error (not the middleware's INVALID_INPUT
    unsupported-argument rejection), proving the pre-check let it through.
    """
    payload = await _call("skyvern_get_errors", {"text": "boom"})

    error = payload.get("error")
    if error is not None:
        assert "unsupported_arguments" not in error.get("details", {})

"""The argument-validation middleware rejects malformed tool calls with a clear,
structured error instead of letting FastMCP raise a raw pydantic ValidationError.

Each case mirrors a real production error signature where a caller sent argument
names that don't match the tool's contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools import workflow as mcp_workflow
from skyvern.cli.mcp_tools.argument_validation import _repair_argument_types, _split_comma_separated_list
from tests.unit._mcp_browser_fakes import make_mock_page, make_skyvern_page


def _structured(result: object) -> dict:
    payload = getattr(result, "structured_content", None)
    assert isinstance(payload, dict), f"expected structured content dict, got {result!r}"
    return payload


async def _call(tool_name: str, arguments: dict) -> dict:
    result = await mcp.call_tool(tool_name, arguments)
    return _structured(result)


def _awaited_kwargs(mock: AsyncMock) -> dict:
    awaited = mock.await_args
    assert awaited is not None
    return dict(awaited.kwargs)


def _mock_navigation(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    page = make_skyvern_page(make_mock_page())
    context = BrowserContext(mode="cloud_session", session_id="pbs_test")
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, context)))
    navigate = AsyncMock(return_value=SimpleNamespace(url="https://example.com", title="Example"))
    monkeypatch.setattr(mcp_browser, "do_navigate", navigate)
    return navigate


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


@pytest.mark.asyncio
async def test_workflow_run_list_comma_separated_status_is_split_for_list_parameter_before_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_runs = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_workflow, "list_workflow_runs_raw", list_runs)

    payload = await _call(
        "skyvern_workflow_run_list",
        {"workflow_id": "wpid_test", "status": "terminated, failed"},
    )

    assert payload["ok"] is True
    assert _awaited_kwargs(list_runs)["status"] == ["terminated", "failed"]


@pytest.mark.asyncio
async def test_comma_separated_string_is_not_split_for_non_list_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    list_runs = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_workflow, "list_workflow_runs_raw", list_runs)

    payload = await _call(
        "skyvern_workflow_run_list",
        {"workflow_id": "wpid_test", "search_key": "terminated,failed"},
    )

    assert payload["ok"] is True
    assert _awaited_kwargs(list_runs)["search_key"] == "terminated,failed"


@pytest.mark.asyncio
async def test_comma_separated_string_is_not_split_for_unapproved_list_parameter() -> None:
    tool = await mcp.get_tool("skyvern_file_upload")
    arguments = {"file_paths": "/tmp/Last, First.pdf"}

    _repair_argument_types("skyvern_file_upload", tool, arguments)

    assert arguments["file_paths"] == "/tmp/Last, First.pdf"


@pytest.mark.parametrize(
    "value",
    ['["terminated","failed"]', "terminated,,failed", "terminated"],
)
def test_csv_list_repair_leaves_structured_or_ambiguous_strings_for_other_validation(value: str) -> None:
    assert _split_comma_separated_list(value) == value


def test_csv_list_repair_leaves_excessive_item_count_for_other_validation() -> None:
    value = ",".join(["terminated"] * 101)

    assert _split_comma_separated_list(value) == value


@pytest.mark.asyncio
async def test_navigate_sub_1000_timeout_is_treated_as_seconds_and_converted_to_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    navigate = _mock_navigation(monkeypatch)

    payload = await _call("skyvern_navigate", {"url": "https://example.com", "timeout": 60})

    assert payload["ok"] is True
    assert _awaited_kwargs(navigate)["timeout"] == 60000


@pytest.mark.asyncio
@pytest.mark.parametrize(("timeout_seconds", "timeout_ms"), [(1.5, 1500), (0.5, 500)])
async def test_navigate_fractional_seconds_with_whole_milliseconds_are_converted_to_int(
    timeout_seconds: float,
    timeout_ms: int,
) -> None:
    tool = await mcp.get_tool("skyvern_navigate")
    arguments = {"timeout": timeout_seconds}

    _repair_argument_types("skyvern_navigate", tool, arguments)

    assert arguments["timeout"] == timeout_ms
    assert type(arguments["timeout"]) is int


@pytest.mark.asyncio
async def test_navigate_timeout_at_millisecond_threshold_is_not_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    navigate = _mock_navigation(monkeypatch)

    payload = await _call("skyvern_navigate", {"url": "https://example.com", "timeout": 1000})

    assert payload["ok"] is True
    assert _awaited_kwargs(navigate)["timeout"] == 1000


@pytest.mark.asyncio
async def test_type_repair_preserves_structured_missing_key_rejection() -> None:
    payload = await _call("skyvern_workflow_run_list", {"status": "terminated,failed"})

    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == "INVALID_INPUT"
    assert error["details"]["unsupported_arguments"] == []
    assert error["details"]["missing_required_arguments"] == ["workflow_id"]

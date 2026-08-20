"""Unit tests for MCP response size cap."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from skyvern.cli.core.browser_ops import NavigateResult
from skyvern.cli.core.result import Artifact, BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools import response as response_module
from skyvern.cli.mcp_tools.response import (
    MCP_MAX_RESPONSE_BYTES,
    MCP_MAX_RESPONSE_CHARS,
    response_transformed,
    size_capped,
    truncate_response,
    truncate_response_bytes,
)

_REPRESENTATIVE_PAYLOADS = (
    Path(__file__).parent / "fixtures" / "mcp_response_distillation" / "representative_payloads.json"
)


def _representative_payload(name: str) -> dict[str, Any]:
    cases = json.loads(_REPRESENTATIVE_PAYLOADS.read_text())["cases"]
    return next(case["payload"] for case in cases if case["name"] == name)


def _full_response_tool(payload: dict[str, Any]) -> Any:
    request_key = str(id(payload))

    @response_transformed()
    async def tool(
        *,
        request_key: str = request_key,
        verbosity: str = "full",
        response_offset_chars: int = 0,
    ) -> dict[str, Any]:
        del request_key, verbosity, response_offset_chars
        return payload

    return tool


def test_truncate_response_passes_small_payload_unchanged() -> None:
    small = {"ok": True, "data": {"items": list(range(10))}}
    assert truncate_response(small) is small


def test_truncate_response_bytes_caps_multibyte_payload() -> None:
    payload = {"ok": True, "data": {"body": "é" * (MCP_MAX_RESPONSE_BYTES // 2)}}

    result = truncate_response_bytes(payload)

    assert result["_truncated"] is True
    assert result["_max_bytes"] == MCP_MAX_RESPONSE_BYTES
    assert result["_original_bytes"] > MCP_MAX_RESPONSE_BYTES
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MCP_MAX_RESPONSE_BYTES
    assert "data" not in result


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
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"
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
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"
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
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"


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
    assert result["_original_chars"] == sys.maxsize
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"


@pytest.mark.parametrize(
    ("cap_response", "original_size_key"),
    [
        (truncate_response, "_original_chars"),
        (truncate_response_bytes, "_original_bytes"),
    ],
)
def test_truncate_response_deep_acyclic_payload_is_fail_closed(
    cap_response: Any,
    original_size_key: str,
) -> None:
    import sys

    payload: dict[str, Any] = {"ok": True}
    node = payload
    for _ in range(sys.getrecursionlimit() + 10):
        child: dict[str, Any] = {}
        node["child"] = child
        node = child

    result = cap_response(payload)

    assert result is not payload
    assert result["_truncated"] is True
    if cap_response is truncate_response_bytes:
        assert result["_size_unavailable"] == "bytes"
        assert result["error"]["code"] == "RESPONSE_ENCODING_ERROR"
    else:
        assert result[original_size_key] == sys.maxsize
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MCP_MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_response_transformed_deep_acyclic_payload_is_fail_closed() -> None:
    import sys

    payload: dict[str, Any] = {"ok": True}
    node = payload
    for _ in range(sys.getrecursionlimit() + 10):
        child: dict[str, Any] = {}
        node["child"] = child
        node = child

    @response_transformed()
    async def tool() -> dict[str, Any]:
        return payload

    result = await tool()

    assert result is not payload
    assert result["_truncated"] is True
    assert result["_original_chars"] == sys.maxsize
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MCP_MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_response_transformed_fails_closed_when_default_stringifier_raises() -> None:
    import sys

    class RaisingString:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    payload: dict[str, Any] = {"ok": True, "value": RaisingString()}

    @response_transformed()
    async def tool() -> dict[str, Any]:
        return payload

    result = await tool()

    assert result is not payload
    assert result["_truncated"] is True
    assert result["_original_chars"] == sys.maxsize
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MCP_MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_response_transformed_lone_surrogate_encoding_is_fail_closed() -> None:
    @response_transformed()
    async def tool() -> dict[str, Any]:
        return {"ok": True, "value": "\ud800"}

    result = await tool()

    assert result["_truncated"] is True
    assert result["_size_unavailable"] == "bytes"
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_ENCODING_ERROR"
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MCP_MAX_RESPONSE_BYTES


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
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"
    assert len(json.dumps(result, ensure_ascii=False)) <= MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_size_capped_decorator_applies_utf8_byte_cap_after_character_cap() -> None:
    @size_capped
    async def emoji_tool() -> dict[str, Any]:
        return {"ok": True, "data": {"body": "😀" * 40_000}}

    result = await emoji_tool()

    assert result["_truncated"] is True
    assert result["_max_bytes"] == MCP_MAX_RESPONSE_BYTES
    assert result["_original_bytes"] > MCP_MAX_RESPONSE_BYTES
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MCP_MAX_RESPONSE_BYTES


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
    same treatment: an extraction that overflows the tool-result limit must not be handed back raw.
    The distillation pass runs first (emitting a ``_response_distillation`` marker when it omits content);
    the hard size cap (``_truncated`` envelope) remains the backstop when distillation cannot shrink."""
    oversize = {"rows": "y" * (MCP_MAX_RESPONSE_CHARS + 1_000)}
    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test")
    page = SimpleNamespace(page=SimpleNamespace())
    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(page, ctx)))
    monkeypatch.setattr(mcp_browser, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(mcp_browser, "get_current_session", lambda: SimpleNamespace(_working_frame=None))
    monkeypatch.setattr(mcp_browser, "clear_session_ref_map", Mock())
    monkeypatch.setattr(mcp_browser, "do_extract", AsyncMock(return_value=SimpleNamespace(extracted=oversize)))
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=NavigateResult(url="https://example.test", title="Example")),
    )
    monkeypatch.setattr(mcp_browser, "do_screenshot", AsyncMock(return_value=SimpleNamespace(data=b"png")))
    monkeypatch.setattr(
        mcp_browser,
        "save_artifact",
        Mock(return_value=Artifact(kind="screenshot", path="/tmp/shot.png", mime="image/png", bytes=3)),
    )

    tool = await mcp.get_tool(tool_name)
    result = await tool.fn(**kwargs)

    assert result.get("_truncated") is True or "_response_distillation" in result, (
        f"{tool_name} returned an oversize extraction raw: neither the cap envelope nor the "
        "distillation marker is present"
    )
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
    monkeypatch.setattr(mcp_browser, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(mcp_browser, "get_current_session", lambda: SimpleNamespace(_working_frame=None))
    monkeypatch.setattr(mcp_browser, "clear_session_ref_map", Mock())
    monkeypatch.setattr(
        mcp_browser,
        "do_navigate",
        AsyncMock(return_value=NavigateResult(url="https://example.test", title="Example")),
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
    """Every paired capture tool must be registered behind a cap-enforcing wrapper: either the plain
    hard cap (``size_capped``) or the distillation pipeline (``response_transformed``), whose final
    step applies the same hard cap."""

    async def _probe() -> dict[str, Any]:
        return {}

    capping_wrapper_codes = {
        size_capped(_probe).__code__,
        response_transformed()(_probe).__code__,
    }

    paired_tools = [
        tool
        for tool in await mcp.list_tools()
        if tool.name.endswith("_and_screenshot") or "screenshot" in tool.parameters.get("properties", {})
    ]

    assert paired_tools
    for registered_tool in paired_tools:
        tool = await mcp.get_tool(registered_tool.name)
        assert tool.fn.__code__ in capping_wrapper_codes, (
            f"{registered_tool.name} is not wrapped in size_capped or response_transformed"
        )


def test_module_level_tool_functions_stay_raw_for_cli_callers() -> None:
    """Response transformation is an MCP wire concern applied at registration only.
    The module-level functions are imported directly by CLI commands (skyvern/cli/commands/,
    skyvern/cli/workflow.py), which promise full untransformed output — a definition-site
    decorator would silently distill CLI results."""
    from skyvern.cli.mcp_tools import inspection as inspection_module
    from skyvern.cli.mcp_tools import workflow as workflow_module

    async def _probe() -> dict[str, Any]:
        return {}

    wrapper_codes = {
        size_capped(_probe).__code__,
        response_transformed()(_probe).__code__,
    }
    cli_visible = [
        inspection_module.skyvern_network_requests,
        inspection_module.skyvern_network_request_detail,
        inspection_module.skyvern_har_stop,
        workflow_module.skyvern_workflow_run,
        workflow_module.skyvern_workflow_status,
    ]
    for fn in cli_visible:
        assert fn.__code__ not in wrapper_codes, (
            f"{fn.__name__} is decorated at definition; CLI output would be transformed"
        )


def test_continuation_scope_uses_session_when_forge_app_is_uninitialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern import forge
    from skyvern.cli.mcp_tools import _session as session_module
    from skyvern.forge import AppHolder

    monkeypatch.setattr(forge, "app", AppHolder())
    monkeypatch.setattr(
        session_module,
        "get_current_session",
        lambda: SimpleNamespace(
            organization_id="session-org",
            context=BrowserContext(mode="cloud_session", session_id="pbs-session", cdp_url="ws://ignored"),
        ),
    )

    assert response_module._continuation_scope_identity() == (
        "session-org",
        ("cloud_session", "pbs-session"),
    )


def test_continuation_scope_prefers_initialized_request_org_and_local_cdp_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern import forge
    from skyvern.cli.mcp_tools import _session as session_module

    monkeypatch.setattr(
        forge,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(get_mcp_request_organization_id=lambda: "request-org"),
        ),
    )
    monkeypatch.setattr(
        session_module,
        "get_current_session",
        lambda: SimpleNamespace(
            organization_id="session-org",
            context=BrowserContext(mode="cdp", session_id="pbs-ignored", cdp_url="ws://local-browser"),
        ),
    )

    assert response_module._continuation_scope_identity() == (
        "request-org",
        ("cdp", "ws://local-browser"),
    )


def test_continuation_scope_propagates_initialized_request_org_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern import forge
    from skyvern.cli.mcp_tools import _session as session_module

    error = RuntimeError("request organization lookup failed")

    def raise_request_org_error() -> str:
        raise error

    monkeypatch.setattr(
        forge,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(get_mcp_request_organization_id=raise_request_org_error),
        ),
    )
    monkeypatch.setattr(
        session_module,
        "get_current_session",
        lambda: SimpleNamespace(organization_id="session-org", context=None),
    )

    with pytest.raises(RuntimeError) as exc_info:
        response_module._continuation_scope_identity()

    assert exc_info.value is error


def test_continuation_scope_falls_back_when_request_org_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern import forge
    from skyvern.cli.mcp_tools import _session as session_module

    def raise_request_org_error() -> str:
        raise ValueError("no authenticated request")

    monkeypatch.setattr(
        forge,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(get_mcp_request_organization_id=raise_request_org_error),
        ),
    )
    monkeypatch.setattr(
        session_module,
        "get_current_session",
        lambda: SimpleNamespace(organization_id="session-org", context=None),
    )

    assert response_module._continuation_scope_identity() == ("session-org", None)


def test_continuation_scope_rejects_missing_request_and_session_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern import forge
    from skyvern.cli.mcp_tools import _session as session_module

    def raise_request_org_error() -> str:
        raise ValueError("no authenticated request")

    monkeypatch.setattr(
        forge,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(get_mcp_request_organization_id=raise_request_org_error),
        ),
    )
    monkeypatch.setattr(
        session_module,
        "get_current_session",
        lambda: SimpleNamespace(organization_id=None, context=None),
    )

    with pytest.raises(ValueError, match="organization identity is required"):
        response_module._continuation_scope_identity()


@pytest.mark.asyncio
async def test_full_response_continuation_round_trips_oversize_fixture() -> None:
    payload = _representative_payload("extract_tabular_nested_results")
    canonical = json.dumps(payload, ensure_ascii=False, default=str)
    tool = _full_response_tool(payload)
    chunks: list[str] = []
    snapshot_ids: set[str] = set()
    offset = 0

    while True:
        result = await tool(response_offset_chars=offset)
        assert result["ok"] is False
        assert result["error"]["code"] == "RESPONSE_TRUNCATED"
        assert result["_total_chars"] == len(canonical)
        assert "response_offset_chars" in result["_hint"]
        assert len(json.dumps(result, ensure_ascii=False, default=str)) <= MCP_MAX_RESPONSE_CHARS
        assert result["_offset_chars"] == offset
        chunks.append(result["_content_slice"])
        snapshot_ids.add(result["_snapshot_id"])
        next_offset = result["_next_offset_chars"]
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset

    recovered = "".join(chunks)
    assert len(chunks) > 1
    assert recovered.encode() == canonical.encode()
    assert snapshot_ids == {hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]}
    assert json.loads(recovered) == payload


@pytest.mark.asyncio
async def test_full_response_continuation_offset_at_or_beyond_total_returns_empty() -> None:
    payload = {"ok": True, "data": {"value": "x" * (MCP_MAX_RESPONSE_CHARS + 1_000)}}
    canonical = json.dumps(payload, ensure_ascii=False, default=str)
    total_chars = len(canonical)
    tool = _full_response_tool(payload)
    first = await tool(response_offset_chars=0)
    assert first["_next_offset_chars"] is not None

    midpoint = total_chars // 2
    midpoint_result = await tool(response_offset_chars=midpoint)
    assert midpoint_result["_offset_chars"] == midpoint
    assert midpoint_result["_content_slice"] == canonical[midpoint:]
    assert midpoint_result["_next_offset_chars"] is None

    for offset in (total_chars, total_chars + 10):
        result = await tool(response_offset_chars=offset)
        assert result["_offset_chars"] == offset
        assert result["_content_slice"] == ""
        assert result["_next_offset_chars"] is None
        assert result["_total_chars"] == total_chars


@pytest.mark.asyncio
async def test_full_response_continuation_does_not_reinvoke_wrapped_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"ok": True, "data": {"value": "x" * (MCP_MAX_RESPONSE_CHARS + 1_000)}}
    calls = 0
    serialized_payload_calls = 0
    original_dumps = response_module.json.dumps

    def counting_dumps(value: Any, *args: Any, **kwargs: Any) -> str:
        nonlocal serialized_payload_calls
        if value is payload:
            serialized_payload_calls += 1
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(response_module.json, "dumps", counting_dumps)

    @response_transformed()
    async def counting_tool(
        *,
        request_id: str,
        verbosity: str = "full",
        response_offset_chars: int = 0,
    ) -> dict[str, Any]:
        nonlocal calls
        del request_id, verbosity, response_offset_chars
        calls += 1
        return payload

    first = await counting_tool(request_id="no-reexecution")
    second = await counting_tool(
        request_id="no-reexecution",
        response_offset_chars=first["_next_offset_chars"],
    )

    assert calls == 1
    assert serialized_payload_calls == 1
    assert second["_offset_chars"] == first["_next_offset_chars"]


@pytest.mark.asyncio
async def test_full_response_continuation_cache_isolated_by_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ("org-a", ("cloud_session", "pbs-a"))
    calls = 0

    monkeypatch.setattr(
        response_module,
        "_continuation_scope_identity",
        lambda: context,
        raising=False,
    )

    @response_transformed()
    async def scoped_tool(
        *,
        request_id: str,
        verbosity: str = "full",
        response_offset_chars: int = 0,
    ) -> dict[str, Any]:
        nonlocal calls
        del request_id, verbosity, response_offset_chars
        calls += 1
        return {"context": context, "data": "x" * (MCP_MAX_RESPONSE_CHARS + 1_000)}

    first_a = await scoped_tool(request_id="shared")
    context = ("org-b", ("cloud_session", "pbs-b"))
    cross_context = await scoped_tool(
        request_id="shared",
        response_offset_chars=first_a["_next_offset_chars"],
    )

    assert cross_context["error"]["code"] == "CONTINUATION_EXPIRED"
    assert calls == 1

    first_b = await scoped_tool(request_id="shared")
    continued_b = await scoped_tool(
        request_id="shared",
        response_offset_chars=first_b["_next_offset_chars"],
    )
    assert continued_b["_snapshot_id"] == first_b["_snapshot_id"]
    assert calls == 2


@pytest.mark.asyncio
async def test_full_response_continuation_snapshot_id_is_stable_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"ok": True, "data": {"value": "x" * (MCP_MAX_RESPONSE_CHARS + 1_000)}}
    tool = _full_response_tool(payload)
    initial_serialized = json.dumps(payload, ensure_ascii=False, default=str)
    log_info = Mock()
    monkeypatch.setattr(response_module.LOG, "info", log_info)

    first = await tool(response_offset_chars=0)
    continued = await tool(response_offset_chars=first["_next_offset_chars"])

    assert first["_snapshot_id"] == hashlib.sha256(initial_serialized.encode("utf-8")).hexdigest()[:8]
    assert continued["_snapshot_id"] == first["_snapshot_id"]

    payload["data"]["value"] = "y" * (MCP_MAX_RESPONSE_CHARS + 1_000)
    refreshed_serialized = json.dumps(payload, ensure_ascii=False, default=str)
    refreshed = await tool(response_offset_chars=0)

    assert refreshed["_snapshot_id"] == hashlib.sha256(refreshed_serialized.encode("utf-8")).hexdigest()[:8]
    assert refreshed["_snapshot_id"] != first["_snapshot_id"]
    assert [call.kwargs["snapshot_id"] for call in log_info.call_args_list] == [
        first["_snapshot_id"],
        first["_snapshot_id"],
        refreshed["_snapshot_id"],
    ]


@pytest.mark.asyncio
async def test_full_response_continuation_missing_or_expired_cache_requires_offset_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"data": "x" * (MCP_MAX_RESPONSE_CHARS + 1_000)}
    calls = 0
    now = 100.0
    monkeypatch.setattr(response_module.time, "monotonic", lambda: now)

    @response_transformed()
    async def expiring_tool(
        *,
        request_id: str,
        verbosity: str = "full",
        response_offset_chars: int = 0,
    ) -> dict[str, Any]:
        nonlocal calls
        del request_id, verbosity, response_offset_chars
        calls += 1
        return payload

    missing = await expiring_tool(request_id="missing", response_offset_chars=1)
    assert missing == {
        "ok": False,
        "error": {
            "code": "CONTINUATION_EXPIRED",
            "message": "Continuation snapshot is missing or expired.",
            "hint": "re-issue the same call with response_offset_chars=0",
        },
    }
    assert calls == 0

    first = await expiring_tool(request_id="expired")
    now += response_module._CONTINUATION_TTL_SECONDS + 1
    expired = await expiring_tool(request_id="expired", response_offset_chars=first["_next_offset_chars"])
    assert expired == missing
    assert calls == 1


@pytest.mark.asyncio
async def test_continuation_key_failure_does_not_discard_a_completed_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    payload = {"ok": True, "data": {"value": "x" * (MCP_MAX_RESPONSE_CHARS + 1_000)}}

    def fail_cache_key(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("scope unavailable")

    monkeypatch.setattr(response_module, "_continuation_cache_key", fail_cache_key)

    @response_transformed()
    async def tool(
        verbosity: str = "full",
        response_offset_chars: int = 0,
    ) -> dict[str, Any]:
        nonlocal calls
        del verbosity, response_offset_chars
        calls += 1
        return payload

    first = await tool()
    retry = await tool(response_offset_chars=1)

    assert first["_truncated"] is True
    assert retry["error"]["code"] == "CONTINUATION_UNAVAILABLE"
    assert calls == 1


@pytest.mark.asyncio
async def test_over_cap_non_continuation_tool_keeps_classic_hint() -> None:
    @response_transformed()
    async def classic_tool(verbosity: str = "full") -> dict[str, Any]:
        del verbosity
        return {"data": "x" * (MCP_MAX_RESPONSE_CHARS + 1_000)}

    result = await classic_tool()

    assert result["_hint"] == response_module._TRUNCATION_HINT
    assert "response_offset_chars" not in result["_hint"]


@pytest.mark.asyncio
async def test_full_response_above_snapshot_ceiling_returns_explicit_error() -> None:
    payload = {"data": "x" * (response_module._CONTINUATION_CACHE_MAX_SNAPSHOT_BYTES + 1)}
    tool = _full_response_tool(payload)
    with response_module._CONTINUATION_CACHE_LOCK:
        response_module._CONTINUATION_CACHE.clear()

    result = await tool(response_offset_chars=0)

    assert result["_truncated"] is True
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE_FOR_CONTINUATION"
    assert result["_original_bytes"] > response_module._CONTINUATION_CACHE_MAX_SNAPSHOT_BYTES
    assert result["_max_snapshot_bytes"] == response_module._CONTINUATION_CACHE_MAX_SNAPSHOT_BYTES
    assert "_next_offset_chars" not in result
    assert not response_module._CONTINUATION_CACHE


@pytest.mark.asyncio
async def test_full_response_byte_only_overflow_uses_byte_continuation_envelope() -> None:
    calls = 0

    @response_transformed()
    async def byte_heavy_tool(
        *,
        verbosity: str = "full",
        response_offset_chars: int = 0,
    ) -> dict[str, Any]:
        nonlocal calls
        del verbosity, response_offset_chars
        calls += 1
        return {"value": "😀" * 40_000}

    result = await byte_heavy_tool()

    assert result["_truncated"] is True
    assert result["_original_bytes"] > MCP_MAX_RESPONSE_BYTES
    assert result["_max_bytes"] == MCP_MAX_RESPONSE_BYTES
    assert "_original_chars" not in result
    assert "_max_chars" not in result
    assert result["_hint"] == response_module._CONTINUATION_BYTE_HINT
    assert "~150k-char" not in result["_hint"]
    await byte_heavy_tool(response_offset_chars=result["_next_offset_chars"])
    assert calls == 1


@pytest.mark.asyncio
async def test_full_response_continuation_rejects_negative_offset() -> None:
    tool = _full_response_tool({"ok": True})

    result = await tool(response_offset_chars=-1)

    assert result == {
        "ok": False,
        "error": {
            "code": "INVALID_OFFSET",
            "message": "response_offset_chars must be non-negative.",
            "hint": "re-issue the same call with response_offset_chars=0",
        },
    }


@pytest.mark.asyncio
async def test_full_response_continuation_rejects_non_integer_offset() -> None:
    tool = _full_response_tool({"ok": True})

    bad_offset: Any = "1"
    result = await tool(response_offset_chars=bad_offset)

    assert result == {
        "ok": False,
        "error": {
            "code": "INVALID_OFFSET",
            "message": "response_offset_chars must be an integer.",
            "hint": "re-issue the same call with response_offset_chars=0",
        },
    }


@pytest.mark.asyncio
async def test_full_response_zero_offset_preserves_under_cap_shape() -> None:
    payload = {"ok": True, "data": {"value": "small"}}
    tool = _full_response_tool(payload)

    result = await tool(response_offset_chars=0)

    assert result is payload
    assert "_content_slice" not in result
    assert "_offset_chars" not in result
    assert "_next_offset_chars" not in result
    assert "_total_chars" not in result

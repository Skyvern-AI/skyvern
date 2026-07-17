"""Tests for the MCP code-block synthesis tool.

OSS-synced: only example.* / RFC-2606 placeholder targets.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from skyvern.cli.core import trajectory_store
from skyvern.cli.core.trajectory_store import append_trajectory_entry
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools import trajectory as mcp_trajectory
from skyvern.cli.mcp_tools.code_block import skyvern_code_block_synthesize
from skyvern.forge.sdk.copilot.code_block_synthesis import synthesize_code_block

_HASH_A = "principal-a"
_HASH_B = "principal-b"
_SYNTHESIS_FIELDS = ("code", "parameters", "steps", "notes", "emitted_interaction_count", "truncated")
_SOURCE_HINT = "Provide exactly one of trajectory_json or session_id"


@pytest.fixture(autouse=True)
def _reset_trajectory_store() -> Iterator[None]:
    trajectory_store._trajectories.clear()
    yield
    trajectory_store._trajectories.clear()


@pytest.fixture
def active_principal(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(mcp_trajectory, "active_api_key_hash", lambda: _HASH_A)
    return _HASH_A


def _fixture_trajectory() -> list[dict]:
    return [
        {
            "tool_name": "type_text",
            "selector": "#search",
            "source_url": "https://example.com/catalog",
            "typed_value": "widget",
            "role": "textbox",
            "accessible_name": "Search",
        },
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/catalog",
            "role": "button",
            "accessible_name": "Submit",
        },
    ]


def _expected_result(*, ok: bool, data: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> dict:
    return {
        "ok": ok,
        "action": "skyvern_code_block_synthesize",
        "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
        "data": data,
        "artifacts": [],
        "timing_ms": {},
        "warnings": [],
        "error": error,
    }


def _expected_synthesis_data(*, strict_selectors: bool) -> dict[str, Any]:
    # Expected data comes from the pure synthesizer (its own tests own the code bytes);
    # this test locks the wrapper envelope around it.
    synthesized = synthesize_code_block(_fixture_trajectory(), strict_selectors=strict_selectors)
    assert synthesized is not None
    return {
        "code": synthesized.code,
        "parameters": synthesized.parameters,
        "steps": synthesized.steps,
        "notes": synthesized.notes,
        "emitted_interaction_count": synthesized.diagnostics.emitted_interaction_count,
        "truncated": synthesized.diagnostics.truncated,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_selectors", [False, True])
async def test_existing_trajectory_json_success_envelope_is_unchanged(strict_selectors: bool) -> None:
    result = await skyvern_code_block_synthesize(
        json.dumps(_fixture_trajectory()),
        strict_selectors=strict_selectors,
    )

    assert result == _expected_result(ok=True, data=_expected_synthesis_data(strict_selectors=strict_selectors))


@pytest.mark.asyncio
async def test_legacy_two_positional_call_still_binds_strict_selectors() -> None:
    positional = await skyvern_code_block_synthesize(json.dumps(_fixture_trajectory()), True)

    assert positional == _expected_result(ok=True, data=_expected_synthesis_data(strict_selectors=True))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trajectory_json", "message", "hint"),
    [
        (
            "{not valid json",
            "Invalid trajectory JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            "Provide a JSON array of interaction objects",
        ),
        (
            json.dumps({"not": "a list"}),
            "Expected a JSON array, got dict",
            "Provide a JSON array of interaction objects",
        ),
        (
            json.dumps([1]),
            "Expected trajectory item at index 0 to be an object, got int",
            "Provide a JSON array of interaction objects",
        ),
        (
            json.dumps(["click"]),
            "Expected trajectory item at index 0 to be an object, got str",
            "Provide a JSON array of interaction objects",
        ),
        (
            json.dumps([]),
            "Trajectory produced no synthesizable steps",
            "Supply a non-empty trajectory with at least one actionable interaction",
        ),
    ],
)
async def test_existing_trajectory_json_error_envelopes_are_unchanged(
    trajectory_json: str,
    message: str,
    hint: str,
) -> None:
    result = await skyvern_code_block_synthesize(trajectory_json)

    assert result == _expected_result(
        ok=False,
        error={"code": "INVALID_INPUT", "message": message, "hint": hint, "details": {}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trajectory_json", "session_id"),
    [
        (None, None),
        (json.dumps(_fixture_trajectory()), "pbs_shortcut"),
    ],
    ids=["neither", "both"],
)
async def test_synthesize_requires_exactly_one_trajectory_source(
    trajectory_json: str | None,
    session_id: str | None,
) -> None:
    result = await skyvern_code_block_synthesize(trajectory_json=trajectory_json, session_id=session_id)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["hint"] == _SOURCE_HINT


@pytest.mark.asyncio
async def test_empty_trajectory_json_is_parsed_instead_of_treated_as_missing() -> None:
    result = await skyvern_code_block_synthesize(trajectory_json="")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["message"].startswith("Invalid trajectory JSON:")
    assert result["error"]["hint"] != _SOURCE_HINT


@pytest.mark.asyncio
async def test_session_shortcut_matches_two_step_synthesis_fields(active_principal: str) -> None:
    for entry in _fixture_trajectory():
        append_trajectory_entry(api_key_hash=active_principal, session_id="pbs_shortcut", entry=entry)

    one_step = await skyvern_code_block_synthesize(session_id="pbs_shortcut")
    capture = await mcp_trajectory.skyvern_trajectory_get("pbs_shortcut")
    two_step = await skyvern_code_block_synthesize(trajectory_json=capture["data"]["trajectory_json"])

    assert capture["data"]["truncated"] is False
    assert one_step["ok"] is True
    assert one_step["data"]["capture_truncated"] is False
    assert {field: one_step["data"][field] for field in _SYNTHESIS_FIELDS} == {
        field: two_step["data"][field] for field in _SYNTHESIS_FIELDS
    }


@pytest.mark.asyncio
async def test_session_shortcut_keeps_capture_and_emission_truncation_independent(
    active_principal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trajectory_store, "MAX_ENTRIES", 1)
    append_trajectory_entry(
        api_key_hash=active_principal,
        session_id="pbs_clipped",
        entry={"tool_name": "click", "selector": "#discarded"},
    )
    append_trajectory_entry(
        api_key_hash=active_principal,
        session_id="pbs_clipped",
        entry={"tool_name": "click", "selector": "#retained"},
    )

    result = await skyvern_code_block_synthesize(session_id="pbs_clipped")

    assert result["ok"] is True
    assert result["data"]["capture_truncated"] is True
    assert result["data"]["truncated"] is False
    assert result["data"]["emitted_interaction_count"] == 1


@pytest.mark.asyncio
async def test_session_shortcut_reports_fully_clipped_capture(
    active_principal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trajectory_store, "MAX_BYTES", 1)
    append_trajectory_entry(
        api_key_hash=active_principal,
        session_id="pbs_clipped",
        entry={"tool_name": "click", "selector": "#oversized"},
    )

    result = await skyvern_code_block_synthesize(session_id="pbs_clipped")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["details"] == {"capture_truncated": True}


@pytest.mark.asyncio
async def test_session_shortcut_rejects_not_found_capture(active_principal: str) -> None:
    result = await skyvern_code_block_synthesize(session_id="pbs_unknown")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_session_shortcut_does_not_disclose_foreign_capture(active_principal: str) -> None:
    append_trajectory_entry(
        api_key_hash=_HASH_B,
        session_id="pbs_foreign",
        entry={"tool_name": "click", "selector": "#private"},
    )

    foreign = await skyvern_code_block_synthesize(session_id="pbs_foreign")
    unknown = await skyvern_code_block_synthesize(session_id="pbs_unknown")

    assert active_principal == _HASH_A
    assert foreign == unknown


@pytest.mark.asyncio
async def test_synthesize_mcp_schema_keeps_both_sources_optional_and_trajectory_first() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools_by_name["skyvern_code_block_synthesize"].parameters

    assert list(schema["properties"]) == ["trajectory_json", "strict_selectors", "session_id"]
    assert "trajectory_json" not in schema.get("required", [])
    assert "session_id" not in schema.get("required", [])


@pytest.mark.asyncio
async def test_fixture_trajectory_synthesizes_non_empty_code_block() -> None:
    result = await skyvern_code_block_synthesize(json.dumps(_fixture_trajectory()))

    assert result["ok"] is True
    code = result["data"]["code"]
    assert "page.goto" in code  # nosemgrep: incomplete-url-substring-sanitization
    assert "https://example.com/catalog" in code  # nosemgrep: incomplete-url-substring-sanitization
    assert ".fill(" in code
    assert ".click()" in code
    assert result["data"]["emitted_interaction_count"] == 2


@pytest.mark.asyncio
async def test_same_trajectory_synthesizes_byte_identical_code() -> None:
    r1 = await skyvern_code_block_synthesize(json.dumps(_fixture_trajectory()))
    r2 = await skyvern_code_block_synthesize(json.dumps(_fixture_trajectory()))

    assert r1["data"]["code"] == r2["data"]["code"]


@pytest.mark.asyncio
async def test_empty_trajectory_is_rejected() -> None:
    result = await skyvern_code_block_synthesize(json.dumps([]))

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_non_array_json_is_rejected() -> None:
    result = await skyvern_code_block_synthesize(json.dumps({"not": "a list"}))

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("trajectory", ([1], ["click"]))
async def test_non_object_trajectory_items_are_rejected(trajectory: list[object]) -> None:
    result = await skyvern_code_block_synthesize(json.dumps(trajectory))

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_bad_json_is_rejected() -> None:
    result = await skyvern_code_block_synthesize("{not valid json")

    assert result["ok"] is False

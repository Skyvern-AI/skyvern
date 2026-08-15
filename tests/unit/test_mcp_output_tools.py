"""Tests for schema-constrained MCP output tools."""

from __future__ import annotations

import json
import urllib.request
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client

from skyvern.cli.core.result import BrowserContext, ErrorCode, make_result
from skyvern.cli.mcp_tools import mcp, output_tools
from skyvern.cli.mcp_tools.response import MCP_MAX_RESPONSE_CHARS, size_capped
from tests.unit.test_mcp_scope_visibility import TOTAL_TOOL_COUNT

_NEW_TOOL_NAMES = {"skyvern_extract_structured", "skyvern_finish"}


@pytest.mark.asyncio
async def test_output_tool_registration_is_additive_static_and_lean_only() -> None:
    tools = await mcp.list_tools()
    tools_by_name = {tool.name: tool for tool in tools}

    assert len(tools) == TOTAL_TOOL_COUNT
    assert len(tools_by_name) == len(tools)
    assert _NEW_TOOL_NAMES <= tools_by_name.keys()
    assert tools_by_name["skyvern_extract_structured"].tags == {"lean"}
    assert tools_by_name["skyvern_finish"].tags == {"lean"}
    assert tools_by_name["skyvern_extract_structured"].description == output_tools.EXTRACT_STRUCTURED_DESCRIPTION
    assert tools_by_name["skyvern_finish"].description == output_tools.FINISH_DESCRIPTION


@pytest.mark.asyncio
async def test_extract_structured_returns_schema_valid_object(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = AsyncMock(
        return_value=make_result(
            "skyvern_extract",
            browser_context=BrowserContext(mode="cloud_session", session_id="pbs_test"),
            data={"extracted": {"name": "Ada", "age": 37}, "sdk_equivalent": "await page.extract(prompt='profile')"},
        )
    )
    monkeypatch.setattr(output_tools, "skyvern_extract", extraction)
    schema = (
        '{"type":"object","required":["name","age"],"properties":{"name":{"type":"string"},"age":{"type":"integer"}}}'
    )

    result = await output_tools.skyvern_extract_structured(
        prompt="Extract the profile",
        schema=schema,
        session_id="pbs_test",
    )

    assert result["ok"] is True
    assert result["action"] == "skyvern_extract_structured"
    # The wrapped tool's other data fields survive; only `extracted` and `schema_valid` are ours.
    assert result["data"] == {
        "extracted": {"name": "Ada", "age": 37},
        "schema_valid": True,
        "sdk_equivalent": "await page.extract(prompt='profile')",
    }
    extraction.assert_awaited_once_with(
        prompt="Extract the profile",
        schema=schema,
        session_id="pbs_test",
        cdp_url=None,
    )


@pytest.mark.asyncio
async def test_extract_structured_schema_violation_names_failing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        output_tools,
        "skyvern_extract",
        AsyncMock(return_value=make_result("skyvern_extract", data={"extracted": {"profile": {"age": "old"}}})),
    )
    schema = '{"type":"object","properties":{"profile":{"type":"object","properties":{"age":{"type":"integer"}}}}}'

    result = await output_tools.skyvern_extract_structured(prompt="Extract the profile", schema=schema)

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "$.profile.age" in result["error"]["message"]
    assert result["error"]["details"]["validation_errors"] == [
        {
            "path": "$.profile.age",
            "constraint": "type",
            "constraint_value": "integer",
            "actual_value": '"old"',
            "actual_type": "string",
            "message": "$.profile.age: 'old' is not of type 'integer'",
            "expected_type": "integer",
        }
    ]


@pytest.mark.asyncio
async def test_extract_structured_malformed_schema_is_actionable_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = AsyncMock(side_effect=AssertionError("extraction must not run for malformed schema"))
    monkeypatch.setattr(output_tools, "skyvern_extract", extraction)

    result = await output_tools.skyvern_extract_structured(prompt="Extract data", schema="{not-json")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "Invalid JSON Schema" in result["error"]["message"]
    assert "valid JSON Schema" in result["error"]["hint"]
    extraction.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "terminated", "failed"])
async def test_finish_accepts_each_terminal_status(status: str) -> None:
    result = await output_tools.skyvern_finish(status=status, output={"result": status}, reason="declared outcome")

    expected = {"status": status, "output": {"result": status}, "reason": "declared outcome"}
    assert result["ok"] is True
    assert result["data"]["finish_record"] == expected


@pytest.mark.asyncio
async def test_finish_rejects_unknown_status_with_allowed_semantics() -> None:
    result = await output_tools.skyvern_finish(status="done")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "completed, terminated, failed" in result["error"]["message"]
    allowed = result["error"]["details"]["allowed_statuses"]
    assert [item["status"] for item in allowed] == ["completed", "terminated", "failed"]
    assert all(item["semantics"] for item in allowed)


@pytest.mark.asyncio
async def test_finish_with_schema_validates_output() -> None:
    schema = '{"type":"object","required":["confirmation"],"properties":{"confirmation":{"type":"string"}}}'

    result = await output_tools.skyvern_finish(
        status="completed",
        output={"confirmation": "submitted"},
        schema=schema,
    )

    assert result["ok"] is True
    assert result["data"]["finish_record"]["output"] == {"confirmation": "submitted"}


@pytest.mark.asyncio
async def test_finish_type_failure_reports_constraint_and_types() -> None:
    schema = '{"type":"object","properties":{"confirmation":{"type":"string"}}}'

    result = await output_tools.skyvern_finish(
        status="completed",
        output={"confirmation": 42},
        schema=schema,
    )

    assert result["ok"] is False
    failure = result["error"]["details"]["validation_errors"][0]
    assert failure == {
        "path": "$.confirmation",
        "constraint": "type",
        "constraint_value": "string",
        "actual_value": "42",
        "actual_type": "integer",
        "message": "$.confirmation: 42 is not of type 'string'",
        "expected_type": "string",
    }


@pytest.mark.asyncio
async def test_finish_calls_are_independent_stateless_declarations() -> None:
    first = await output_tools.skyvern_finish(status="completed", output="first")
    second = await output_tools.skyvern_finish(status="failed", output="replacement")

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["data"]["finish_record"] == {"status": "completed", "output": "first", "reason": None}
    assert second["data"]["finish_record"] == {"status": "failed", "output": "replacement", "reason": None}


@pytest.mark.asyncio
async def test_finish_any_of_reports_combinator_summary_and_leaf_path() -> None:
    schema = (
        '{"anyOf":[{"type":"object","required":["answer"],'
        '"properties":{"answer":{"type":"integer"}}},{"type":"array","items":{"type":"number"}}]}'
    )

    result = await output_tools.skyvern_finish(
        status="completed",
        output={"answer": "forty-two"},
        schema=schema,
    )

    assert result["ok"] is False
    failures = result["error"]["details"]["validation_errors"]
    assert failures[0]["constraint"] == "anyOf"
    answer_failure = next(failure for failure in failures if failure["path"] == "$.answer")
    assert answer_failure["constraint"] == "type"
    assert answer_failure["expected_type"] == "integer"
    assert answer_failure["actual_type"] == "string"


@pytest.mark.asyncio
async def test_finish_minimum_failure_reports_constraint_and_values() -> None:
    result = await output_tools.skyvern_finish(
        status="completed",
        output=5,
        schema='{"type":"integer","minimum":10}',
    )

    failure = result["error"]["details"]["validation_errors"][0]
    assert failure["constraint"] == "minimum"
    assert failure["constraint_value"] == 10
    assert failure["actual_value"] == "5"
    assert failure["actual_type"] == "integer"
    assert "expected_type" not in failure


@pytest.mark.asyncio
async def test_finish_enum_failure_reports_constraint_and_values() -> None:
    result = await output_tools.skyvern_finish(
        status="completed",
        output="pending",
        schema='{"enum":["approved","rejected"]}',
    )

    failure = result["error"]["details"]["validation_errors"][0]
    assert failure["constraint"] == "enum"
    assert failure["constraint_value"] == ["approved", "rejected"]
    assert failure["actual_value"] == '"pending"'
    assert failure["actual_type"] == "string"
    assert "expected_type" not in failure


@pytest.mark.asyncio
async def test_finish_enforces_recognized_email_format() -> None:
    schema = '{"type":"string","format":"email"}'

    rejected = await output_tools.skyvern_finish(status="completed", output="not-an-email", schema=schema)
    accepted = await output_tools.skyvern_finish(status="completed", output="a@b.co", schema=schema)

    assert rejected["ok"] is False
    failure = rejected["error"]["details"]["validation_errors"][0]
    assert failure["constraint"] == "format"
    assert failure["constraint_value"] == "email"
    assert accepted["ok"] is True


@pytest.mark.asyncio
async def test_finish_missing_required_properties_report_once_each() -> None:
    schema = '{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}'

    result = await output_tools.skyvern_finish(status="completed", output={}, schema=schema)

    assert result["ok"] is False
    failures = result["error"]["details"]["validation_errors"]
    assert [failure["path"] for failure in failures] == ["$.a", "$.b"]
    assert all(failure["constraint"] == "required" for failure in failures)
    # Each entry names the key it is about, not the whole required list.
    assert [failure["constraint_value"] for failure in failures] == ["a", "b"]


@pytest.mark.asyncio
async def test_finish_validation_failures_cap_is_deterministic_and_duplicate_free() -> None:
    keys = [f"field_{index:02d}" for index in range(25)]
    schema = json.dumps(
        {
            "type": "object",
            "required": keys,
            "properties": {key: {"type": "string"} for key in keys},
        }
    )

    first = await output_tools.skyvern_finish(status="completed", output={}, schema=schema)
    second = await output_tools.skyvern_finish(status="completed", output={}, schema=schema)

    failures = first["error"]["details"]["validation_errors"]
    paths = [failure["path"] for failure in failures]
    assert len(failures) == 20
    assert len(set(paths)) == 20
    assert paths[0] == "$.field_00"
    assert paths[-1] == "$.field_19"
    assert first["error"]["details"]["validation_errors"] == second["error"]["details"]["validation_errors"]


def test_output_tool_descriptions_are_static_and_bounded() -> None:
    assert output_tools.EXTRACT_STRUCTURED_DESCRIPTION == (
        "Extract one schema-conformant JSON object from the current page. Uses the same AI extraction path as "
        "skyvern_extract, then strictly validates the returned value against schema, including format assertions the "
        "runtime recognizes. Returns the validated object only after validation succeeds; schema or output violations "
        "return actionable JSON paths. Navigate first. Use session_id or cdp_url to target an existing browser."
    )
    assert output_tools.FINISH_DESCRIPTION == (
        "Declare one authoritative terminal record; does not interact with the browser. status must be exactly: "
        "completed — the stated goal was achieved, including when the goal itself requested a safe stop or termination; "
        "terminated — deliberately stopped short of the goal because safety, permission, or impossibility was discovered "
        "mid-run; failed — attempted but could not achieve the goal. Optionally include output and reason. If schema is "
        "provided, output must validate, including format assertions the runtime recognizes. The response itself is the "
        "terminal record. A later call supersedes an earlier one only in the caller's own transcript."
    )
    assert len(output_tools.EXTRACT_STRUCTURED_DESCRIPTION.split()) <= 150
    assert len(output_tools.FINISH_DESCRIPTION.split()) <= 150


@pytest.mark.asyncio
async def test_new_output_tools_are_registered_size_capped() -> None:
    """Both tools return caller- or page-derived payloads, so both need this module's response guard."""

    async def _probe() -> dict[str, Any]:
        return {}

    for name in sorted(_NEW_TOOL_NAMES):
        tool = await mcp.get_tool(name)
        assert tool.fn.__code__ is size_capped(_probe).__code__, f"{name} is not wrapped in size_capped"


@pytest.mark.asyncio
async def test_registered_extract_structured_caps_an_oversize_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schema-valid extraction is still page-derived and unbounded; it must not be handed back raw."""
    oversize = {"rows": "y" * (MCP_MAX_RESPONSE_CHARS + 1_000)}
    monkeypatch.setattr(
        output_tools,
        "skyvern_extract",
        AsyncMock(return_value=make_result("skyvern_extract", data={"extracted": oversize})),
    )

    tool = await mcp.get_tool("skyvern_extract_structured")
    result = await tool.fn(prompt="read the table", schema='{"type":"object"}')

    assert result["_truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_registered_finish_caps_an_oversize_output() -> None:
    """`output` is echoed back verbatim, so an oversize caller payload overflows without the guard."""
    tool = await mcp.get_tool("skyvern_finish")

    result = await tool.fn(status="completed", output={"blob": "z" * (MCP_MAX_RESPONSE_CHARS + 1_000)})

    assert result["_truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_validation_failure_bounds_outsized_constraint_and_message() -> None:
    """A huge caller schema must not make each diagnostic larger than the failure it describes."""
    members = [f"option_{index:03d}_{'m' * 50}" for index in range(200)]

    result = await output_tools.skyvern_finish(
        status="completed",
        output="pending",
        schema=json.dumps({"enum": members}),
    )

    failure = result["error"]["details"]["validation_errors"][0]
    assert failure["constraint"] == "enum"
    assert isinstance(failure["constraint_value"], str)
    assert failure["constraint_value"].endswith("...")
    assert failure["constraint_value_truncated"] is True
    assert len(failure["constraint_value"]) <= output_tools._MAX_CONSTRAINT_VALUE_CHARS
    assert len(failure["message"]) <= output_tools._MAX_MESSAGE_CHARS
    # The offending value stays intact — only the schema-derived halves are bounded.
    assert failure["actual_value"] == '"pending"'


@pytest.mark.asyncio
async def test_small_constraint_values_keep_their_native_json_type() -> None:
    """Bounding must not stringify ordinary constraints; callers compare these against their schema."""
    result = await output_tools.skyvern_finish(
        status="completed",
        output="pending",
        schema='{"enum":["approved","rejected"]}',
    )

    failure = result["error"]["details"]["validation_errors"][0]
    assert failure["constraint_value"] == ["approved", "rejected"]
    assert "constraint_value_truncated" not in failure


@pytest.mark.asyncio
async def test_remote_schema_reference_is_refused_without_touching_the_network() -> None:
    """jsonschema resolves unknown `$ref`s with urlopen, so a caller schema must never reach it."""
    reached: list[Any] = []

    def _tripwire(*args: Any, **kwargs: Any) -> Any:
        reached.append(args)
        raise AssertionError("validation attempted a network fetch")

    with mock.patch.object(urllib.request, "urlopen", _tripwire):
        result = await output_tools.skyvern_finish(
            status="completed",
            output={"a": 1},
            schema='{"$ref":"http://169.254.169.254/latest/meta-data/"}',
        )

    assert reached == []
    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "Remote schema reference is not supported" in result["error"]["message"]


@pytest.mark.asyncio
async def test_nested_remote_reference_is_refused() -> None:
    """The refusal walks the whole schema, not just its root."""
    schema = json.dumps({"properties": {"a": {"items": {"$ref": "https://example.test/s.json"}}}})

    result = await output_tools.skyvern_finish(status="completed", output={"a": []}, schema=schema)

    assert result["ok"] is False
    assert "Remote schema reference is not supported" in result["error"]["message"]


@pytest.mark.asyncio
async def test_local_pointer_reference_still_resolves() -> None:
    """Refusing remote refs must not break in-document `#/...` pointers."""
    schema = json.dumps(
        {"$defs": {"name": {"type": "string"}}, "type": "object", "properties": {"a": {"$ref": "#/$defs/name"}}}
    )

    ok = await output_tools.skyvern_finish(status="completed", output={"a": "ada"}, schema=schema)
    bad = await output_tools.skyvern_finish(status="completed", output={"a": 1}, schema=schema)

    assert ok["ok"] is True
    assert bad["ok"] is False
    assert bad["error"]["details"]["validation_errors"][0]["path"] == "$.a"


@pytest.mark.asyncio
async def test_unbounded_schema_recursion_is_reported_not_raised() -> None:
    """A self-referential `$ref` against a deep instance raises RecursionError inside jsonschema."""
    schema = json.dumps(
        {
            "$defs": {"node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}}},
            "$ref": "#/$defs/node",
        }
    )
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(400):
        cursor["next"] = {}
        cursor = cursor["next"]

    result = await output_tools.skyvern_finish(status="completed", output=deep, schema=schema)

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "could not be evaluated" in result["error"]["message"]


@pytest.mark.asyncio
async def test_failure_entry_bugs_propagate_instead_of_reading_as_a_bad_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our own diagnostics are built outside the guard, so a bug there must not be blamed on the caller."""
    monkeypatch.setattr(
        output_tools,
        "_failure_entry",
        Mock(side_effect=KeyError("regression in entry building")),
    )

    with pytest.raises(KeyError):
        await output_tools.skyvern_finish(
            status="completed",
            output={"confirmation": 42},
            schema='{"type":"object","properties":{"confirmation":{"type":"string"}}}',
        )


@pytest.mark.asyncio
async def test_finish_is_annotated_read_only() -> None:
    """It echoes its own input and touches nothing, so clients should not treat it as a mutation."""
    tool = await mcp.get_tool("skyvern_finish")

    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.openWorldHint is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "schema"),
    [
        pytest.param(5, '{"type":"integer","minimum":1}', id="integer"),
        pytest.param(1.5, '{"type":"number"}', id="number"),
        pytest.param(True, '{"type":"boolean"}', id="boolean"),
        pytest.param([1, 2], '{"type":"array","items":{"type":"integer"}}', id="array"),
        pytest.param("done", '{"type":"string"}', id="string"),
        pytest.param({"a": 1}, '{"type":"object"}', id="object"),
    ],
)
async def test_finish_accepts_every_json_root_through_the_client(output: Any, schema: str) -> None:
    """Calling through a Client exercises the pydantic boundary that `tool.fn` skips — the
    declared `output` type must admit every root the validator claims to support."""
    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_finish",
            {"status": "completed", "output": output, "schema": schema},
        )

    assert result.data["ok"] is True
    assert result.data["data"]["finish_record"]["output"] == output


@pytest.mark.asyncio
async def test_schema_too_deep_for_json_parsing_is_reported_not_raised() -> None:
    """Nesting past the parser's limit must still return the documented INVALID_INPUT."""
    result = await output_tools.skyvern_finish(
        status="completed",
        output={},
        schema="[" * 20_000 + "]" * 20_000,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "Invalid JSON Schema" in result["error"]["message"]


@pytest.mark.asyncio
async def test_schema_too_deep_for_check_schema_is_reported_not_raised() -> None:
    """A schema can parse fine and still blow the stack inside check_schema, which raises
    RecursionError rather than SchemaError — a separate escape from the json.loads one."""
    nested: dict[str, Any] = {"type": "array"}
    for _ in range(400):
        nested = {"type": "array", "items": nested}
    assert json.loads(json.dumps(nested))  # parses cleanly; the escape is downstream

    result = await output_tools.skyvern_finish(status="completed", output=[], schema=json.dumps(nested))

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.INVALID_INPUT
    assert "nested too deeply" in result["error"]["message"]

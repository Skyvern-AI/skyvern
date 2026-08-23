"""Unit tests for the MCP response transformation boundary."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest

from skyvern.cli.mcp_tools import response as response_module
from skyvern.cli.mcp_tools.response import MCP_MAX_RESPONSE_CHARS, response_transformed
from skyvern.cli.mcp_tools.response_distillation import TransformResult, TransformTier, distill_value


def test_distill_value_compacts_structured_values_without_mutation() -> None:
    payload = {
        "ok": False,
        "error": {"code": "PARTIAL", "message": "Some rows were unavailable"},
        "workflow_id": "wf_123",
        "rows": [{"index": index, "body": "x" * 500} for index in range(8)],
    }
    original = deepcopy(payload)

    first = distill_value(payload)
    second = distill_value(payload)

    assert first == second
    assert first.tier is TransformTier.STRUCTURED
    assert first.complete is False
    assert first.fallback_reason == "content_summarized"
    assert payload == original
    assert first.value["ok"] is False
    assert first.value["error"] == payload["error"]
    assert first.value["workflow_id"] == "wf_123"
    assert first.value["rows"]["_length"] == 8
    assert first.value["rows"]["_omitted_items"] == 3
    assert first.value["rows"]["_examples"][0]["body"].endswith("chars omitted]")


def test_distill_value_bounds_keys_in_document_order_and_keeps_shape_metadata() -> None:
    payload = {"ok": True, **{f"row_{index}": index for index in range(1, 31)}}

    result = distill_value(payload)

    assert result.tier is TransformTier.STRUCTURED
    assert result.complete is False
    assert list(result.value)[:24] == ["ok", *(f"row_{index}" for index in range(1, 24))]
    assert result.value["_key_count"] == 31
    assert result.value["_omitted_keys"] == 7


def test_distill_value_does_not_replace_payload_shape_metadata_keys() -> None:
    payload = {
        "_key_count": "source-count",
        "_omitted_keys": "source-omissions",
        **{f"row_{index}": index for index in range(30)},
    }

    result = distill_value(payload)

    assert result.complete is False
    assert result.value["_key_count"] == "source-count"
    assert result.value["_omitted_keys"] == "source-omissions"
    assert result.value["__key_count"] == 32
    assert result.value["__omitted_keys"] == 8


def test_distill_value_parses_json_structure() -> None:
    result = distill_value('{"items": [{"id": 1}, {"id": 2}], "ok": true}')

    assert result.tier is TransformTier.STRUCTURED
    assert result.complete is True
    assert result.value == {"items": [{"id": 1}, {"id": 2}], "ok": True}


def test_distill_value_parses_yaml_without_converting_dates() -> None:
    result = distill_value("ok: true\ncreated_at: 2023-10-27T10:00:00Z\nitems:\n  - id: row_1\n  - id: row_2\n")

    assert result.tier is TransformTier.STRUCTURED
    assert result.complete is True
    assert result.value["created_at"] == "2023-10-27T10:00:00Z"
    assert isinstance(result.value["created_at"], str)
    assert result.value["items"] == [{"id": "row_1"}, {"id": "row_2"}]


def test_distill_value_uses_degraded_tier_for_json_prefix() -> None:
    result = distill_value('{"ok": true, "items": [1, 2, 3]} trailing diagnostic text')

    assert result.tier is TransformTier.DEGRADED
    assert result.complete is False
    assert result.value == {"items": [1, 2, 3], "ok": True}
    assert result.fallback_reason == "trailing_content_after_json_prefix"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param("plain prose", "scalar_only_parse", id="scalar-yaml"),
        pytest.param("{not valid", "parse_failed", id="failed-parse"),
        pytest.param('{"key": 1, "key": 2}', "ambiguous_duplicate_key", id="duplicate-json-key"),
        pytest.param("key: 1\nkey: 2\n", "ambiguous_duplicate_key", id="duplicate-yaml-key"),
        pytest.param('{"value": NaN}', "unsafe_non_finite_number", id="unsafe-json-number"),
        pytest.param(
            "source: &shared\n  value: 1\ncopy: *shared\n",
            "aliased_or_recursive",
            id="yaml-alias",
        ),
    ],
)
def test_distill_value_passes_through_untrusted_or_ambiguous_strings(payload: str, reason: str) -> None:
    result = distill_value(payload)

    assert result.value is payload
    assert result.tier is TransformTier.PASSTHROUGH
    assert result.complete is True
    assert result.fallback_reason == reason


def test_distill_value_passes_through_recursive_input() -> None:
    payload: dict[str, Any] = {"ok": True}
    payload["self"] = payload

    result = distill_value(payload)

    assert result.value is payload
    assert result.tier is TransformTier.PASSTHROUGH
    assert result.complete is True
    assert result.fallback_reason == "aliased_or_recursive"


@pytest.mark.asyncio
async def test_response_transformed_full_mode_bypasses_formatter_and_preserves_signature() -> None:
    formatter = Mock()

    @response_transformed(formatter=formatter)
    async def example_tool(query: str, verbosity: str = "concise") -> dict[str, Any]:
        return {"ok": True, "query": query, "body": "unchanged"}

    result = await example_tool("needle", verbosity="full")

    assert result == {"ok": True, "query": "needle", "body": "unchanged"}
    formatter.assert_not_called()
    assert example_tool.__name__ == "example_tool"
    assert str(inspect.signature(example_tool)) == "(query: 'str', verbosity: 'str' = 'concise') -> 'dict[str, Any]'"


@pytest.mark.asyncio
async def test_response_transformed_full_mode_still_applies_size_cap() -> None:
    formatter = Mock()

    @response_transformed(formatter=formatter)
    async def large_tool(*, verbosity: str = "concise") -> dict[str, Any]:
        return {"ok": True, "run_id": "run_123", "body": "x" * (MCP_MAX_RESPONSE_CHARS + 100)}

    result = await large_tool(verbosity="full")

    formatter.assert_not_called()
    assert result["_truncated"] is True
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"
    assert result["run_id"] == "run_123"


@pytest.mark.asyncio
async def test_response_transformed_runs_tool_formatter_before_generic_compaction() -> None:
    original = {"raw": "tool payload" + ("x" * 10_000)}

    def formatter(value: Any) -> TransformResult[Any]:
        assert value is original
        return TransformResult(
            value={
                "ok": True,
                "workflow_id": "wf_123",
                "selected_rows": [{"value": "x" * 500, "index": index} for index in range(9)],
            },
            tier=TransformTier.STRUCTURED,
            complete=False,
            fallback_reason="tool_summary",
        )

    @response_transformed(formatter=formatter, recovery_hint="Request the next page for all rows.")
    async def formatted_tool() -> dict[str, Any]:
        return original

    result = await formatted_tool()

    assert result["ok"] is True
    assert result["workflow_id"] == "wf_123"
    assert result["selected_rows"]["_length"] == 9
    assert result["selected_rows"]["_omitted_items"] == 4
    assert result["_response_distillation"] == {
        "complete": False,
        "tier": "structured",
        "recovery_hint": "Request the next page for all rows.",
        "fallback_reason": "tool_summary",
    }


@pytest.mark.asyncio
async def test_response_transformed_never_replaces_with_larger_candidate() -> None:
    original = {"ok": True, "value": "small"}

    def formatter(_: Any) -> TransformResult[Any]:
        return TransformResult(
            value={"ok": True, "value": "small", "extra": "larger"},
            tier=TransformTier.STRUCTURED,
            complete=True,
        )

    @response_transformed(formatter=formatter)
    async def small_tool() -> dict[str, Any]:
        return original

    result = await small_tool()

    assert result is original


@pytest.mark.asyncio
async def test_response_transformed_logs_metrics_without_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = Mock()
    monkeypatch.setattr(response_module, "LOG", logger)
    secret = "private-payload-value"

    @response_transformed()
    async def metrics_tool() -> dict[str, Any]:
        return {"ok": True, "details": f"{secret}-" + ("x" * 500)}

    await metrics_tool()

    logger.info.assert_called_once()
    call = logger.info.call_args
    assert call.args == ("mcp_response_distilled",)
    assert set(call.kwargs) == {
        "tool",
        "tier",
        "original_chars",
        "output_chars",
        "savings_percentage",
        "fallback_reason",
    }
    assert call.kwargs["tool"] == "metrics_tool"
    assert call.kwargs["tier"] == "structured"
    assert isinstance(call.kwargs["savings_percentage"], float)
    assert secret not in repr(call)


@pytest.mark.asyncio
async def test_input_marker_cannot_suppress_the_boundary_completeness_marker() -> None:
    payload = {
        "ok": True,
        "_response_distillation": {"complete": True, "source": "input"},
        "rows": [{"id": index, "body": "x" * 500} for index in range(8)],
    }

    @response_transformed()
    async def tool() -> dict[str, Any]:
        return payload

    result = await tool()

    assert result["_response_distillation"]["complete"] is False
    assert result["_response_distillation"] != payload["_response_distillation"]


@pytest.mark.asyncio
async def test_response_transformed_rejects_multibyte_candidate_larger_in_bytes() -> None:
    payload = {"ok": True, "value": "x" * 3_000}
    candidate_value = {"ok": True, "value": "😀" * 1_000}

    def formatter(_: Any) -> TransformResult[Any]:
        return TransformResult(
            value=candidate_value,
            tier=TransformTier.STRUCTURED,
            complete=False,
            fallback_reason="content_summarized",
            protected_paths=(("value",),),
        )

    @response_transformed(formatter=formatter)
    async def multibyte_tool() -> dict[str, Any]:
        return payload

    candidate = response_module._with_response_anchors(payload, formatter(payload))
    candidate = response_module._with_completeness_marker(candidate, None)
    candidate_json = json.dumps(candidate.value, ensure_ascii=False)
    payload_json = json.dumps(payload, ensure_ascii=False)

    result = await multibyte_tool()

    assert len(candidate_json) < len(payload_json)
    assert len(candidate_json.encode()) > len(payload_json.encode())
    assert result is payload


@pytest.mark.asyncio
async def test_response_transformed_harvests_anchors_after_degraded_json_prefix() -> None:
    payload = {
        "ok": True,
        "data": {
            "result": json.dumps({"items": [{"body": "x" * 500} for _ in range(9)]})
            + ' diagnostic request_id="req-tail" https://host/recover',
        },
    }

    @response_transformed()
    async def degraded_tool() -> dict[str, Any]:
        return payload

    result = await degraded_tool()

    assert ["request_id", "req-tail"] in result["_response_anchors"]["values"]
    assert ["result", "https://host/recover"] in result["_response_anchors"]["values"]


@pytest.mark.asyncio
async def test_response_transformed_compacts_before_applying_cap_and_preserves_anchors() -> None:
    payload = {
        "ok": False,
        "error": {"code": "PARTIAL", "message": "Some records failed"},
        "workflow_id": "wf_123",
        "rows": [{"index": index, "body": ("x" * 30_000) + str(index)} for index in range(6)],
    }
    assert len(json.dumps(payload)) > MCP_MAX_RESPONSE_CHARS

    @response_transformed()
    async def large_structured_tool() -> dict[str, Any]:
        return payload

    result = await large_structured_tool()

    assert "_truncated" not in result
    assert result["ok"] is False
    assert result["error"] == payload["error"]
    assert result["workflow_id"] == "wf_123"
    assert result["rows"]["_length"] == 6
    assert result["_response_distillation"]["complete"] is False
    assert len(json.dumps(result, ensure_ascii=False)) < MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_response_transformed_keeps_size_cap_fail_closed_for_recursive_values() -> None:
    circular: dict[str, Any] = {"ok": True, "error": None}
    circular["self"] = circular

    @response_transformed()
    async def circular_tool() -> dict[str, Any]:
        return circular

    result = await circular_tool()

    assert result["_truncated"] is True
    assert result["ok"] is False
    assert result["error"]["code"] == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_response_transformed_preserves_small_unserializable_values() -> None:
    original: dict[str, Any] = {"ok": True, "value": object()}

    @response_transformed()
    async def unsupported_tool() -> dict[str, Any]:
        return original

    result = await unsupported_tool()

    assert result is original


def test_yaml_scalars_stay_verbatim_strings() -> None:
    """PyYAML implements YAML 1.1 scalar coercion for times, booleans, numbers, and nulls.
    A distilled summary of free-form page text must keep those values as extracted."""
    lines = [
        "Departure: 10:30",
        "Duration: 1:45",
        "Confirmed: yes",
        "Code: 007",
        "NullValue: null",
        "UpperNullValue: NULL",
        "TildeValue: ~",
    ] + [f"field_{index}: value_{index}" for index in range(40)]
    result = distill_value("\n".join(lines))

    assert result.tier is not TransformTier.PASSTHROUGH
    assert result.value["Departure"] == "10:30"
    assert result.value["Duration"] == "1:45"
    assert result.value["Confirmed"] == "yes"
    assert result.value["Code"] == "007"
    assert result.value["NullValue"] == "null"
    assert result.value["UpperNullValue"] == "NULL"
    assert result.value["TildeValue"] == "~"


def test_explicit_yaml_scalar_tags_stay_verbatim_strings() -> None:
    lines = [
        "IntegerValue: !!int 007",
        "BooleanValue: !!bool yes",
        "FloatValue: !!float 1.20",
        "NullValue: !!null null",
    ] + [f"field_{index}: value_{index}" for index in range(40)]

    result = distill_value("\n".join(lines))

    assert result.tier is not TransformTier.PASSTHROUGH
    assert result.value["IntegerValue"] == "007"
    assert result.value["BooleanValue"] == "yes"
    assert result.value["FloatValue"] == "1.20"
    assert result.value["NullValue"] == "null"


def test_yaml_keys_colliding_after_resolver_stripping_pass_through() -> None:
    """With coercing resolvers stripped, `10:` and `"10":` both resolve to the string
    key "10"; the duplicate-key guard must fingerprint with the same loader that
    parses, or one value is silently overwritten."""
    result = distill_value('10: first\n"10": second\nother: keep')

    assert result.tier is TransformTier.PASSTHROUGH
    assert result.fallback_reason == "ambiguous_duplicate_key"
    assert result.value == '10: first\n"10": second\nother: keep'


def test_yaml_comment_syntax_passes_through_verbatim() -> None:
    """YAML drops `#`-comments, so parsing prose like an address would truncate the
    scalar mid-value while reporting complete=True. Such text must pass through."""
    text = "Address: 500 Main St #204\nCity: Springfield\nZip: 02139"
    result = distill_value(text)

    assert result.tier is TransformTier.PASSTHROUGH
    assert result.fallback_reason == "yaml_comment_ambiguity"
    assert result.value == text


def test_yaml_hash_inside_scalar_still_distills() -> None:
    """A `#` not preceded by whitespace is not a comment (URL fragments); such
    values must keep distilling and survive verbatim."""
    lines = ["url: https://example.test/page#section"] + [f"field_{index}: value_{index}" for index in range(10)]
    result = distill_value("\n".join(lines))

    assert result.tier is not TransformTier.PASSTHROUGH
    assert result.value["url"] == "https://example.test/page#section"


@pytest.mark.asyncio
async def test_formatter_omission_does_not_reinject_truncated_json_anchors() -> None:
    payload = {
        "data": {"raw": ('{"order_id":"ord_777","receipt_url":"https://x.test/r/777","body":"' + ("a" * 20_000))}
    }

    def formatter(_: Any) -> TransformResult[Any]:
        return TransformResult(
            value={"data": {"summary": "truncated source"}},
            tier=TransformTier.STRUCTURED,
            complete=False,
        )

    @response_transformed(formatter=formatter)
    async def tool(verbosity: str = "summary") -> dict[str, Any]:
        return payload

    result = await tool()
    pairs = result.get("_response_anchors", {}).get("values", [])
    assert ["order_id", "ord_777"] not in pairs
    assert ["receipt_url", "https://x.test/r/777"] not in pairs


def test_sidecar_bounds_storage_values_and_lists_only_anchor_keys() -> None:
    storage = {f"ordinary_key_{index:02d}": f"value-{index}" for index in range(33)}
    storage["user_id"] = "u" * 2_000
    storage["profile_url"] = "https://example.test/profile"
    transformed = TransformResult(
        value={"data": {"summary": "storage compacted"}},
        tier=TransformTier.STRUCTURED,
        complete=False,
    )

    result = response_module._with_response_anchors({"data": {"storage": storage}}, transformed)
    sidecar = result.value["_response_anchors"]

    assert set(sidecar["keys"]) == {"user_id", "profile_url"}
    assert not any(name.startswith("ordinary_key_") for name in sidecar["keys"])
    assert all(not isinstance(value, str) or len(value) <= 256 for _, value in sidecar["values"])
    bounded_user_id = next(value for key, value in sidecar["values"] if key == "user_id")
    assert bounded_user_id.endswith("… [truncated from 2000 chars]")


def test_sidecar_deduplicates_identical_omitted_values() -> None:
    page_url = "https://example.test/page"
    original = {"items": [{"page_url": page_url} for _ in range(20)]}
    transformed = TransformResult(
        value={"items": {"_length": 20}},
        tier=TransformTier.STRUCTURED,
        complete=False,
    )

    result = response_module._with_response_anchors(original, transformed)
    sidecar = result.value["_response_anchors"]

    assert sidecar["values"] == [["page_url", page_url, 20]]


def test_anchor_harvester_ignores_urls_in_non_anchor_headers() -> None:
    anchors = response_module._response_anchors(
        {
            "response_headers": {
                "content-security-policy": (
                    "default-src https://cdn.example.test; connect-src https://api.example.test"
                )
            }
        }
    )

    assert anchors["values"] == []


def test_sidecar_has_a_hard_serialized_size_limit() -> None:
    original = {
        "items": [
            {
                "request_id": f"req_{index:04d}",
                "url": f"https://example.test/items/{index}?" + ("x" * 200),
            }
            for index in range(200)
        ]
    }
    transformed = TransformResult(
        value={"items": {"_length": 200}},
        tier=TransformTier.STRUCTURED,
        complete=False,
    )

    result = response_module._with_response_anchors(original, transformed)
    sidecar = result.value["_response_anchors"]
    budget = min(
        response_module._MAX_RESPONSE_ANCHOR_SIDECAR_CHARS,
        max(
            response_module._MIN_RESPONSE_ANCHOR_SIDECAR_CHARS,
            response_module._ANCHOR_SIDECAR_BODY_MULTIPLIER * response_module._response_size(transformed.value),
        ),
    )

    assert response_module._response_size(sidecar) <= budget
    assert sidecar["omitted_value_count"] > 0


def test_sidecar_budget_gives_each_anchor_key_a_representative_before_repeats() -> None:
    original = {
        "items": [{"task_id": f"tsk_{index:04d}"} for index in range(200)],
        "summary_id": "sum_1",
        "record_count": 200,
    }
    transformed = TransformResult(
        value={"items": {"_length": 200}},
        tier=TransformTier.STRUCTURED,
        complete=False,
    )

    result = response_module._with_response_anchors(original, transformed)
    sidecar = result.value["_response_anchors"]
    represented_keys = {pair[0] for pair in sidecar["values"]}

    assert set(sidecar["keys"]) == {"task_id", "summary_id", "record_count"}
    assert represented_keys == {"task_id", "summary_id", "record_count"}


def test_anchor_walk_refuses_yaml_alias_bombs() -> None:
    alias_bomb = "base: &base [x, y, z]\nexpanded: [" + ", ".join("*base" for _ in range(2_000)) + "]"

    containers, _ = response_module._structured_container(alias_bomb)
    anchors = response_module._response_anchors({"body": alias_bomb})

    assert containers == []
    assert anchors["values"] == []


def test_anchor_scan_is_bounded_for_large_brace_heavy_text() -> None:
    containers, remainder = response_module._structured_container("{" * 70_000)

    assert containers == []
    assert len(remainder) == response_module._MAX_ANCHOR_SCAN_CHARS == 64_000


def test_anchor_keys_are_precise_and_do_not_inherit_across_dicts() -> None:
    anchors = response_module._response_anchors(
        {
            "account": "acct",
            "discount": 15,
            "curl": "command",
            "thumburl": "thumbnail",
            "item_count": 3,
            "url": "https://example.test/item",
            "order_ids": [{"name": "not-an-anchor"}],
        }
    )

    assert set(anchors["keys"]) == {"item_count", "url", "order_ids"}
    assert ["item_count", 3] in anchors["values"]
    assert ["url", "https://example.test/item"] in anchors["values"]
    assert not any(value in {"acct", 15, "command", "thumbnail", "not-an-anchor"} for _, value in anchors["values"])


@pytest.mark.asyncio
async def test_candidate_guard_uses_both_units_and_enforces_byte_cap() -> None:
    original = {"value": "😀" * 100}
    candidate = {"value": "x" * 150}

    def formatter(_: Any) -> TransformResult[Any]:
        return TransformResult(value=candidate, tier=TransformTier.STRUCTURED, complete=False)

    @response_transformed(formatter=formatter)
    async def guarded(verbosity: str = "summary") -> dict[str, Any]:
        return original

    @response_transformed()
    async def byte_heavy(verbosity: str = "full") -> dict[str, Any]:
        return {"value": "😀" * 40_000}

    assert await guarded() is original
    capped = await byte_heavy()
    assert capped["_truncated"] is True
    assert capped["_max_bytes"] == response_module.MCP_MAX_RESPONSE_BYTES


def test_extraction_default_verbosity_supports_full_and_summary() -> None:
    env_name = "SKYVERN_MCP_EXTRACTION_DEFAULT_VERBOSITY"

    assert response_module.extraction_default_verbosity({env_name: "full"}) == "full"
    assert response_module.extraction_default_verbosity({env_name: "summary"}) == "summary"
    with pytest.raises(ValueError, match="must be 'full' or 'summary'"):
        response_module.extraction_default_verbosity({env_name: "compact"})

    script = (
        "import inspect; "
        "from skyvern.cli.mcp_tools.browser import skyvern_extract; "
        "print(inspect.signature(skyvern_extract).parameters['verbosity'].default)"
    )
    for selected in ("full", "summary"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, env_name: selected},
        )
        assert completed.stdout.strip().splitlines()[-1] == selected


def test_url_query_spans_do_not_synthesize_anchor_pairs() -> None:
    url = "https://example.test/path?id=abc&status=ok"
    anchors = response_module._response_anchors({"body": url})

    assert ["body", url] in anchors["values"]
    assert not any(key in {"id", "status"} for key, _ in anchors["values"])

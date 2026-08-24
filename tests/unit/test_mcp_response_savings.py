"""Deterministic savings benchmark for representative MCP response fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from skyvern.cli.mcp_tools import response as response_module
from skyvern.cli.mcp_tools.response import response_transformed
from skyvern.cli.mcp_tools.response_browser import format_browser_response
from skyvern.cli.mcp_tools.response_distillation import TransformResult, TransformTier
from skyvern.cli.mcp_tools.response_network import (
    format_har_response,
    format_network_request_detail_response,
    format_network_requests_response,
)
from skyvern.cli.mcp_tools.response_storage import format_storage_response
from skyvern.cli.mcp_tools.response_workflow import format_workflow_response

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mcp_response_distillation"
_JSON_FIXTURE = _FIXTURE_DIR / "representative_payloads.json"
_NETWORK_JSON_FIXTURE = _FIXTURE_DIR / "network_requests_payload.json"
_YAML_FIXTURE = _FIXTURE_DIR / "representative_payload.yaml"
_RECOVERY_HINT = "Request the full response to recover content omitted from this representative summary."
_UNSAFE_CASE = "unsafe_duplicate_key_passthrough"
_NESTED_CASES = frozenset(
    {
        "evaluate_nested_catalog",
        "extract_tabular_nested_results",
        "network_detail_json_body",
        "network_requests_list",
        "session_storage_structured_strings",
        "local_storage_browser_state",
        "multi_entry_har",
        "workflow_nested_outputs",
        "yaml_evaluate_nested_string",
        "yaml_extract_nested_string",
        "yaml_network_body_nested_string",
    }
)
_SYNTHETIC_SECRET_SENTINELS = (
    "SYNTHETIC_SECRET_NEVER_LOG_7B19",
    "SYNTHETIC_UNSAFE_NEVER_LOG_4D2A",
)

Formatter = Callable[[Any], TransformResult[Any]]


def _load_cases() -> tuple[dict[str, Any], ...]:
    json_document = json.loads(_JSON_FIXTURE.read_text())
    network_json_document = json.loads(_NETWORK_JSON_FIXTURE.read_text())
    yaml_document = yaml.safe_load(_YAML_FIXTURE.read_text())
    cases = (*json_document["cases"], *network_json_document["cases"], *yaml_document["cases"])
    assert all(set(case) == {"name", "formatter", "payload"} for case in cases)
    assert len({case["name"] for case in cases}) == len(cases)
    return cases


_CASES = _load_cases()


def _formatter_for(case: dict[str, Any]) -> Formatter:
    formatter_name = case["formatter"]
    if formatter_name == "browser":
        return format_browser_response
    if formatter_name == "network_detail":
        return format_network_request_detail_response
    if formatter_name == "network":
        return format_network_requests_response
    if formatter_name == "storage":
        return format_storage_response
    if formatter_name == "har":
        return format_har_response
    if formatter_name == "workflow":
        return lambda response: format_workflow_response(response, tool_name="skyvern_workflow_run")
    raise AssertionError(f"Unknown fixture formatter: {formatter_name}")


def _serialized_bytes(value: Any) -> bytes:
    """Match production's JSON options while retaining bytes for determinism checks."""
    return json.dumps(value, ensure_ascii=False, default=str).encode()


def _assert_common_anchors(original: dict[str, Any], transformed: dict[str, Any]) -> None:
    assert transformed["ok"] is original["ok"]
    assert transformed["error"] == original["error"]
    assert set(original) <= set(transformed)


def _assert_case_anchors(name: str, original: dict[str, Any], transformed: dict[str, Any]) -> None:
    """Check exact semantic anchors and deterministic representative selections."""
    if name == "evaluate_nested_catalog":
        data = transformed["data"]
        assert data["catalog_id"] == "cat_fixture_2026"
        assert data["title"] == "Synthetic stationery catalog"
        assert data["source_url"] == "https://catalog.fixture.example/notebooks"
        assert data["total_count"] == 108
        result = data["result"]
        assert result["_length"] == 12
        assert result["_omitted_items"] == 7
        assert [example["_scalar_preview"]["group_id"] for example in result["_examples"]] == [
            "grp_00",
            "grp_01",
            "grp_02",
            "grp_03",
            "grp_04",
        ]
    elif name == "extract_tabular_nested_results":
        data = transformed["data"]
        assert data["report_id"] == "rpt_fixture_314"
        assert data["title"] == "Synthetic regional readiness report"
        assert data["source_url"] == "https://reports.fixture.example/readiness"
        assert data["row_count"] == 32
        assert data["columns"] == ["record_id", "title", "region", "status", "score"]
        result = data["extracted"]
        assert result["_length"] == 32
        assert result["_omitted_items"] == 27
        assert [example["_scalar_preview"]["record_id"] for example in result["_examples"]] == [
            "rec_000",
            "rec_001",
            "rec_002",
            "rec_003",
            "rec_004",
        ]
    elif name == "network_detail_json_body":
        data = transformed["data"]
        assert data["request"]["request_id"] == 42
        assert data["request"]["url"] == "https://api.fixture.example/v1/inventory?region=north"
        assert data["body_response_id"] == "resp_fixture_42"
        assert data["body_title"] == "Synthetic inventory response"
        assert data["body_source_url"] == "https://api.fixture.example/v1/inventory"
        assert data["body_count"] == 80
        assert data["body"]["_length"] == 10
        assert data["body"]["_omitted_items"] == 5
        assert [example["_scalar_preview"]["group_id"] for example in data["body"]["_examples"]] == [
            "inv_group_0",
            "inv_group_1",
            "inv_group_2",
            "inv_group_3",
            "inv_group_4",
        ]
    elif name == "network_requests_list":
        data = transformed["data"]
        assert data["count"] == 50
        assert data["buffer_size"] == 50
        assert data["omitted_request_count"] == 45
        requests = data["requests"]
        assert len(requests) == 5
        assert [request["request_id"] for request in requests] == [1000, 1012, 1024, 1036, 1049]
        assert [request["url"] for request in requests] == [
            "https://app.fixture.example/workspace/dashboard/0?view=activity&page=1",
            "https://api.fixture.example/v2/projects/project-05/search?query=fixture-012&limit=25",
            "https://cdn.fixture.example/assets/build-20260817/styles/route-024.css",
            "https://cdn.fixture.example/assets/fonts/fixture-sans-400.woff2?subset=latin-1",
            "https://api.fixture.example/v2/users/user-05/preferences?revision=79",
        ]
        assert [request["status"] for request in requests] == [200, 201, 304, 200, 200]
        assert [request["page_url"] for request in requests] == [
            "https://app.fixture.example/workspace/project-00?tab=0",
            "https://app.fixture.example/workspace/project-05?tab=2",
            "https://app.fixture.example/workspace/project-03?tab=4",
            "https://app.fixture.example/workspace/project-01?tab=1",
            "https://app.fixture.example/workspace/project-00?tab=4",
        ]
        assert [request["tab_id"] for request in requests] == [
            "fixture-tab-network-0",
            "fixture-tab-network-0",
            "fixture-tab-network-0",
            "fixture-tab-network-0",
            "fixture-tab-network-1",
        ]
        assert all(
            set(request)
            == {
                "request_id",
                "url",
                "method",
                "status",
                "resource_type",
                "content_type",
                "timing_ms",
                "response_size",
                "page_url",
                "tab_id",
            }
            for request in requests
        )
    elif name == "session_storage_structured_strings":
        data = transformed["data"]
        assert data["count"] == 30
        assert data["items"]["_key_count"] == 30
        assert data["items"]["_omitted_keys"] == 8
        assert data["items"]["00-json-workspace"]["state_id"] == "state_fixture_json"
        assert data["items"]["01-yaml-filters"]["state_id"] == "state_fixture_yaml"
    elif name == "local_storage_browser_state":
        assert transformed["page_id"] == "page_fixture_local_1"
        assert transformed["title"] == "Synthetic shop state"
        assert transformed["url"] == "https://shop.fixture.example/catalog"
        assert transformed["local_storage_count"] == 30
        storage = transformed["local_storage"]
        assert storage["_key_count"] == 30
        assert storage["_omitted_keys"] == 8
        assert storage["00-cart-state"]["cart_id"] == "cart_fixture_55"
        assert storage["00-cart-state"]["item_count"] == 24
    elif name == "multi_entry_har":
        data = transformed["data"]
        assert data["entry_count"] == 24
        assert data["omitted_entry_count"] == 19
        assert data["har"]["log"]["version"] == "1.2"
        entries = data["har"]["log"]["entries"]
        assert len(entries) == 5
        assert [entry["url"] for entry in entries] == [
            "https://api.fixture.example/v1/resources/0?page=1",
            "https://api.fixture.example/v1/resources/5?page=6",
            "https://api.fixture.example/v1/resources/11?page=12",
            "https://api.fixture.example/v1/resources/17?page=18",
            "https://api.fixture.example/v1/resources/23?page=24",
        ]
        assert [entry["status"] for entry in entries] == [202, 202, 200, 200, 200]
    elif name == "workflow_nested_outputs":
        data = transformed["data"]
        assert data["run_id"] == "wr_fixture_20260816"
        assert data["workflow_id"] == "wpid_fixture_savings"
        assert data["workflow_title"] == "Synthetic regional collection workflow"
        assert data["workflow_url"] == "https://workflow.fixture.example/runs/wr_fixture_20260816"
        assert data["step_count"] == data["total_steps"] == 12
        assert data["output_summary"]["top_level_key_count"] == 13
        top_level_keys = data["output_summary"]["top_level_keys"]
        assert top_level_keys["_length"] == 8
        assert top_level_keys["_omitted_items"] == 3
        assert top_level_keys["_examples"] == [
            "collect_region_00",
            "collect_region_01",
            "collect_region_02",
            "collect_region_03",
            "collect_region_04",
        ]
        assert data["output_summary"]["block_output_count"] == 12
        assert data["output_summary"]["has_extracted_information"] is True
        assert data["artifact_summary"]["artifact_id_count"] == 144
    elif name == "degraded_partial_json_prefix":
        result = transformed["data"]["result"]
        assert result["result_id"] == "result_fixture_partial"
        assert result["title"] == "Synthetic partially recovered result"
        assert result["source_url"] == "https://partial.fixture.example/results"
        assert result["count"] == 14
        assert result["items"]["_length"] == 14
        assert result["items"]["_omitted_items"] == 9
    elif name == "unsafe_duplicate_key_passthrough":
        assert transformed == original
        assert transformed["data"]["request"]["request_id"] == 909
        assert transformed["data"]["request"]["url"] == "https://unsafe.fixture.example/ambiguous"
    elif name in {
        "yaml_evaluate_nested_string",
        "yaml_extract_nested_string",
        "yaml_network_body_nested_string",
    }:
        data = transformed["data"]
        field = {
            "yaml_evaluate_nested_string": "result",
            "yaml_extract_nested_string": "extracted",
            "yaml_network_body_nested_string": "body",
        }[name]
        assert data["dataset_id"] == "dataset_fixture_yaml_2026"
        assert data["title"] == "Synthetic YAML research index"
        assert data["source_url"] == "https://yaml.fixture.example/research"
        assert data["count"] == 12
        result = data[field]
        assert result["_length"] == 12
        assert result["_omitted_items"] == 7
        examples = [example["_scalar_preview"] for example in result["_examples"]]
        assert [example["collection_id"] for example in examples] == [
            "yaml_collection_00",
            "yaml_collection_01",
            "yaml_collection_02",
            "yaml_collection_03",
            "yaml_collection_04",
        ]
        assert examples[0]["url"] == "https://yaml.fixture.example/research/collections/0"
        if field == "body":
            assert data["request"]["request_id"] == 77
            assert data["request"]["url"] == "https://yaml.fixture.example/api/research"
    else:
        raise AssertionError(f"Missing semantic assertions for fixture: {name}")


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
@pytest.mark.asyncio
async def test_representative_response_savings_are_deterministic(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = case["name"]
    payload = case["payload"]
    assert isinstance(payload, dict)
    formatter = _formatter_for(case)
    logger = Mock()
    monkeypatch.setattr(response_module, "LOG", logger)

    formatter_snapshot = deepcopy(payload)
    formatter_result = formatter(payload)
    assert payload == formatter_snapshot

    async def fixture_tool() -> dict[str, Any]:
        return payload

    transformed_tool = response_transformed(formatter=formatter, recovery_hint=_RECOVERY_HINT)(fixture_tool)

    first_snapshot = deepcopy(payload)
    first = await transformed_tool()
    assert payload == first_snapshot

    second_snapshot = deepcopy(payload)
    second = await transformed_tool()
    assert payload == second_snapshot

    first_bytes = _serialized_bytes(first)
    second_bytes = _serialized_bytes(second)
    assert first_bytes == second_bytes

    original_chars = response_module._response_size(payload)
    transformed_chars = response_module._response_size(first)
    savings_percentage = (original_chars - transformed_chars) * 100 / original_chars
    print(
        f"mcp_savings case={name} original_chars={original_chars} "
        f"transformed_chars={transformed_chars} savings_percentage={savings_percentage:.2f}"
    )

    assert isinstance(first, dict)
    _assert_common_anchors(payload, first)
    _assert_case_anchors(name, payload, first)

    if name == _UNSAFE_CASE:
        assert formatter_result.tier is TransformTier.PASSTHROUGH
        assert formatter_result.fallback_reason == "ambiguous_duplicate_key"
        assert first == payload
        assert set(first) == set(payload)
        assert "_response_distillation" not in first
    else:
        assert formatter_result.tier is (
            TransformTier.DEGRADED if name == "degraded_partial_json_prefix" else TransformTier.STRUCTURED
        )
        assert transformed_chars <= original_chars
        marker = first["_response_distillation"]
        if name in {"multi_entry_har", "network_requests_list"}:
            assert set(first) == {*payload, "_response_distillation"}
        else:
            assert set(first) == {*payload, "_response_anchors", "_response_distillation"}
            assert {"keys", "values"} <= set(first["_response_anchors"])
        assert marker["complete"] is False
        assert marker["tier"] == (
            TransformTier.DEGRADED.value if name == "degraded_partial_json_prefix" else TransformTier.STRUCTURED.value
        )
        assert marker["recovery_hint"]
        assert marker["fallback_reason"]
        if name == "degraded_partial_json_prefix":
            assert marker["fallback_reason"] == "trailing_content_after_json_prefix"
        if name in _NESTED_CASES:
            assert savings_percentage >= 60.0

    assert logger.info.call_count == 2
    for call in logger.info.call_args_list:
        assert call.args == ("mcp_response_distilled",)
        assert set(call.kwargs) == {
            "tool",
            "tier",
            "original_chars",
            "output_chars",
            "savings_percentage",
            "fallback_reason",
        }
    logged_metrics = repr(logger.info.call_args_list)
    for sentinel in _SYNTHETIC_SECRET_SENTINELS:
        assert sentinel not in logged_metrics


def test_anchor_recovery_does_not_reinject_sensitive_anchor_values() -> None:
    sentinels = ("AUTH_SENTINEL_A", "AUTH_SENTINEL_B", "AUTH_SENTINEL_C")
    original = {
        "access_token_id": sentinels[0],
        "password_count": sentinels[1],
        "callback_url": f"https://example.test/callback?access_token={sentinels[2]}",
        "rows": [{"id": index, "body": "x" * 500} for index in range(8)],
    }
    transformed = TransformResult(
        value={"rows": {"_length": 8}},
        tier=TransformTier.STRUCTURED,
        complete=False,
    )

    anchored = response_module._with_response_anchors(original, transformed)
    anchored_text = json.dumps(anchored.value, ensure_ascii=False, default=str)

    assert all(sentinel not in anchored_text for sentinel in sentinels)
    assert not {"access_token_id", "password_count", "callback_url"} & set(
        anchored.value.get("_response_anchors", {}).get("keys", {})
    )

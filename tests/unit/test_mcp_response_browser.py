"""Tests for evaluate/extract MCP response distillation."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools.response import MCP_MAX_RESPONSE_CHARS, response_transformed
from skyvern.cli.mcp_tools.response_browser import format_browser_response
from skyvern.cli.mcp_tools.response_distillation import TransformTier
from tests.unit._mcp_browser_fakes import make_mock_page, patch_get_page


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _envelope(key: str, value: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "action": f"skyvern_{'evaluate' if key == 'result' else 'extract'}",
        "data": {key: value, "sdk_equivalent": "unchanged()", "other": {"keep": True}},
        "artifacts": [],
        "warnings": ["keep this warning"],
        "error": None,
    }


def test_format_evaluate_nested_array_preserves_envelope_and_does_not_mutate() -> None:
    payload = _envelope(
        "result",
        {
            "title": "Synthetic catalog",
            "rows": [
                {"row_id": f"row_{index}", "cells": [f"cell-{index}-{column}" * 80 for column in range(8)]}
                for index in range(12)
            ],
        },
    )
    before = deepcopy(payload)

    transformed = format_browser_response(payload)

    assert payload == before
    assert transformed.tier is TransformTier.STRUCTURED
    assert transformed.complete is False
    assert transformed.value["ok"] is True
    assert transformed.value["warnings"] == payload["warnings"]
    assert transformed.value["error"] is None
    assert transformed.value["data"]["sdk_equivalent"] == "unchanged()"
    assert transformed.value["data"]["other"] == {"keep": True}
    summary = transformed.value["data"]["result"]
    assert summary["title"] == "Synthetic catalog"
    assert summary["rows"]["_length"] == 12
    assert len(summary["rows"]["_examples"]) == 5
    assert _size(summary) <= _size(payload["data"]["result"])


def test_format_extract_nested_objects_keeps_keys_counts_and_scalar_previews() -> None:
    extracted = {
        "report_id": "report_123",
        "sections": {
            f"section_{index:02d}": {
                "label": f"Section {index}",
                "records": [{"record_id": f"r{index}_{item}", "value": "x" * 400} for item in range(9)],
            }
            for index in range(30)
        },
    }

    transformed = format_browser_response(_envelope("extracted", extracted))

    assert transformed.tier is TransformTier.STRUCTURED
    assert transformed.complete is False
    summary = transformed.value["data"]["extracted"]
    assert summary["report_id"] == "report_123"
    assert summary["sections"]["_key_count"] == 30
    assert summary["sections"]["_omitted_keys"] == 6
    assert "section_00" in summary["sections"]
    assert _size(summary) <= _size(extracted)


@pytest.mark.parametrize(
    ("payload", "expected_tier"),
    [
        pytest.param(
            json.dumps(
                {
                    "source": "json",
                    "items": [{"id": index, "description": "json value " * 80} for index in range(11)],
                }
            ),
            TransformTier.STRUCTURED,
            id="json",
        ),
        pytest.param(
            "source: yaml\nitems:\n"
            + "".join(f"  - id: {index}\n    description: {'yaml value ' * 80}\n" for index in range(11)),
            TransformTier.STRUCTURED,
            id="yaml",
        ),
    ],
)
def test_format_large_structured_string(payload: str, expected_tier: TransformTier) -> None:
    transformed = format_browser_response(_envelope("extracted", payload))

    assert transformed.tier is expected_tier
    assert transformed.complete is False
    summary = transformed.value["data"]["extracted"]
    assert summary["source"] in {"json", "yaml"}
    assert summary["items"]["_length"] == 11
    assert summary["items"]["_omitted_items"] == 6
    assert _size(summary) <= _size(payload)


@pytest.mark.parametrize(
    ("payload", "tier", "complete"),
    [
        pytest.param("{not valid structured output", TransformTier.PASSTHROUGH, True, id="malformed"),
        pytest.param(
            json.dumps({"items": [{"id": index, "body": "x" * 500} for index in range(9)]}) + " trailing diagnostic",
            TransformTier.DEGRADED,
            False,
            id="degraded-prefix",
        ),
    ],
)
def test_format_malformed_or_degraded_text(payload: str, tier: TransformTier, complete: bool) -> None:
    response = _envelope("result", payload)

    transformed = format_browser_response(response)

    assert transformed.tier is tier
    assert transformed.complete is complete
    if tier is TransformTier.PASSTHROUGH:
        assert transformed.value is response
    else:
        assert transformed.value["data"]["result"]["items"]["_length"] == 9


@pytest.mark.parametrize(("key", "value"), [("result", None), ("extracted", None), ("result", 42), ("extracted", True)])
def test_format_preserves_none_and_small_scalars(key: str, value: Any) -> None:
    response = _envelope(key, value)

    transformed = format_browser_response(response)

    assert transformed.tier is TransformTier.PASSTHROUGH
    assert transformed.value is response
    assert transformed.value["data"][key] is value


@pytest.mark.asyncio
async def test_wrapped_evaluate_defaults_to_full_and_summary_is_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = [{"index": index, "body": "browser value " * 100} for index in range(12)]
    page = make_mock_page()
    page.evaluate = AsyncMock(return_value=raw)
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="cloud_session", session_id="pbs_test"))
    wrapped = response_transformed(
        formatter=format_browser_response,
        recovery_hint='Retry with verbosity="full" to recover the raw value (subject to the response-size cap).',
    )(mcp_browser.skyvern_evaluate)

    full = await wrapped(expression="document.querySelectorAll('*')")
    summary = await wrapped(expression="document.querySelectorAll('*')", verbosity="summary")

    assert full["data"]["result"] == raw
    assert "_response_distillation" not in full
    assert summary["data"]["result"]["_length"] == 12
    assert summary["_response_distillation"]["complete"] is False
    assert "verbosity" in summary["_response_distillation"]["recovery_hint"]


@pytest.mark.asyncio
async def test_wrapped_extract_summary_and_paired_capture_preserve_screenshot_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {"records": [{"id": index, "details": "extracted value " * 100} for index in range(10)]}
    page = make_mock_page()
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="cloud_session", session_id="pbs_test"))
    monkeypatch.setattr(mcp_browser, "do_extract", AsyncMock(return_value=SimpleNamespace(extracted=raw)))
    wrapped_extract = response_transformed(formatter=format_browser_response)(mcp_browser.skyvern_extract)

    summary = await wrapped_extract(prompt="extract records", verbosity="summary")

    assert summary["data"]["extracted"]["records"]["_length"] == 10
    assert summary["_response_distillation"]["complete"] is False

    paired_raw = _envelope("extracted", raw)
    paired_raw["action"] = "skyvern_extract_and_screenshot"
    paired_raw["data"]["screenshot"] = {"path": "/tmp/shot.png", "width": 1280, "height": 720}
    paired_raw["artifacts"] = [{"kind": "screenshot", "path": "/tmp/shot.png", "mime": "image/png"}]

    paired = format_browser_response(paired_raw)

    assert paired.value["data"]["screenshot"] == paired_raw["data"]["screenshot"]
    assert paired.value["artifacts"] == paired_raw["artifacts"]


@pytest.mark.asyncio
async def test_wrapped_evaluate_does_not_reinject_omitted_anchor_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    omitted = {
        "zz_oauth_code": "oauth-code-sentinel",
        "zz_org_id": "org-id-sentinel",
    }
    items = [{"row_id": f"row-{index}", "value": f"value-{index}-" + ("v" * 80)} for index in range(5)]
    items.append(
        {
            "row_id": "row-5",
            **{key: sentinel + ("x" * 2_000) for key, sentinel in omitted.items()},
        }
    )
    page = make_mock_page()
    page.evaluate = AsyncMock(return_value=items)
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="cloud_session", session_id="pbs_test"))
    wrapped = response_transformed(formatter=format_browser_response)(mcp_browser.skyvern_evaluate)

    result = await wrapped(expression="document.querySelectorAll('*')", verbosity="summary")
    response_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    anchors_json = json.dumps(result.get("_response_anchors", {}), ensure_ascii=False, sort_keys=True)

    for key, sentinel in omitted.items():
        assert key not in response_json
        assert sentinel not in response_json
        assert key not in anchors_json
        assert sentinel not in anchors_json


@pytest.mark.asyncio
async def test_inline_screenshot_and_final_url_survive_distillation_byte_exact() -> None:
    inline_base64 = "iVBORw0KGgo" + "A" * 10_000
    final_url = "https://example.test/final?" + "&".join(f"parameter_{index}=value_{index}" for index in range(40))
    raw = _envelope(
        "extracted", {"records": [{"id": index, "details": "extracted value " * 1_000} for index in range(10)]}
    )
    raw["action"] = "skyvern_extract_and_screenshot"
    raw["data"]["url"] = final_url
    raw["data"]["screenshot"] = {"data": inline_base64, "format": "png", "width": 1280, "height": 720}
    raw["artifacts"] = [{"kind": "screenshot", "path": "/tmp/shot.png", "mime": "image/png"}]
    assert _size(raw) > MCP_MAX_RESPONSE_CHARS

    async def tool() -> dict[str, Any]:
        return raw

    wrapped = response_transformed(formatter=format_browser_response)(tool)
    result = await wrapped()

    assert result["data"]["url"] == final_url
    assert result["data"]["screenshot"]["data"] == inline_base64
    assert result["artifacts"] == raw["artifacts"]
    assert "_response_distillation" in result
    assert result["data"]["extracted"] != raw["data"]["extracted"]


@pytest.mark.asyncio
async def test_formatter_exception_falls_back_to_raw_response_and_cap() -> None:
    """A transform failure must never break a successful tool call: the raw response
    falls through (capped), with no exception escaping the decorator."""

    def exploding_formatter(response: Any) -> Any:
        raise ValueError("formatter bug")

    raw = {"ok": True, "data": {"value": 7}}

    async def tool() -> dict[str, Any]:
        return raw

    wrapped = response_transformed(formatter=exploding_formatter)(tool)
    result = await wrapped()

    assert result == raw

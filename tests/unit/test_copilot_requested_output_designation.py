"""Model-requested output designation returns page facts without authoring code."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

from skyvern.forge.sdk.copilot.output_extraction_plan import value_designation_probe_expression
from skyvern.forge.sdk.copilot.tools import (
    _verify_requested_output_reads,
    inspect_page_for_composition_tool,
)

PAGE_URL = "https://dashboard.example.com/analytics"
OUTPUT_PATH = "output.visitors_last_week"


def _has_playwright_chromium() -> bool:
    try:
        with sync_playwright() as runner:
            return Path(runner.chromium.executable_path).exists()
    except Exception:
        return False


_requires_chromium = pytest.mark.skipif(not _has_playwright_chromium(), reason="Playwright Chromium is not installed")


class _DesignationServer:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.expressions: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert tool_name == "skyvern_evaluate"
        self.expressions.append(arguments["expression"])
        return {"ok": True, "data": {"result": json.dumps(self.payload)}}


def _ctx(server: _DesignationServer) -> SimpleNamespace:
    return SimpleNamespace(
        discovery_mcp_server=server,
        completion_criteria_turn_state=None,
        request_policy=None,
        last_code_authoring_repair_context=None,
        pre_run_gated_output_warning_fingerprint=(),
    )


@pytest.mark.asyncio
async def test_designation_returns_verified_page_facts_without_authored_code(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _DesignationServer(
        {
            "text": "8.89K",
            "selector": "[data-attr=visitors-value]",
            "selector_candidates": [
                {"selector": "[data-attr=visitors-value]", "match_count": 1, "position": 0},
                {"selector": "#web-visitors > span", "match_count": 2, "position": 1},
            ],
            "match_count": 1,
            "position": 0,
            "url": PAGE_URL,
        }
    )
    verified, unverified = await _verify_requested_output_reads(
        _ctx(server),
        [{"output_path": OUTPUT_PATH, "value_text": "8.89K", "label": "Visitors"}],
    )

    assert unverified == []
    assert verified == [
        {
            "output_path": OUTPUT_PATH,
            "label": "Visitors",
            "rendered_value": "8.89K",
            "selector_candidates": [
                {"selector": "[data-attr=visitors-value]", "match_count": 1, "position": 0},
                {"selector": "#web-visitors > span", "match_count": 2, "position": 1},
            ],
            "page_url": PAGE_URL,
        }
    ]
    assert "8.89K" in server.expressions[0]
    assert "Visitors" in server.expressions[0]
    assert "code" not in repr(verified)
    assert "next_action" not in repr(verified)


@pytest.mark.asyncio
async def test_designation_preserves_the_output_path_the_model_authored() -> None:
    server = _DesignationServer(
        {
            "text": "8.89K",
            "selector_candidates": [{"selector": "#value", "match_count": 1, "position": 0}],
        }
    )

    verified, unverified = await _verify_requested_output_reads(
        _ctx(server),
        [{"output_path": "visitors_last_week", "value_text": "8.89K", "label": "Visitors"}],
    )

    assert unverified == []
    assert verified[0]["output_path"] == OUTPUT_PATH
    assert server.expressions


def test_page_inspection_schema_exposes_designation_as_optional_model_input() -> None:
    schema = inspect_page_for_composition_tool.params_json_schema

    assert "requested_output_reads" in schema["properties"]
    reads = schema["properties"]["requested_output_reads"]
    definition = schema["$defs"][reads["anyOf"][0]["items"]["$ref"].rsplit("/", 1)[-1]]
    assert definition["properties"] == {
        "output_path": {"title": "Output Path", "type": "string"},
        "value_text": {"title": "Value Text", "type": "string"},
        "label": {"title": "Label", "type": "string"},
    }
    assert definition["required"] == ["output_path", "value_text", "label"]


@pytest.mark.asyncio
async def test_page_inspection_returns_designation_facts_on_the_existing_tool_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _DesignationServer(
        {
            "text": "8.89K",
            "selector_candidates": [{"selector": "#visitors", "match_count": 1, "position": 0}],
            "url": PAGE_URL,
        }
    )
    ctx = _ctx(server)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools._inspect_page_for_composition_impl",
        AsyncMock(return_value={"ok": True, "data": {"current_url": PAGE_URL}}),
    )
    raw = await inspect_page_for_composition_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="inspect_page_for_composition"),
        json.dumps(
            {
                "target_url": PAGE_URL,
                "requested_output_reads": [{"output_path": OUTPUT_PATH, "value_text": "8.89K", "label": "Visitors"}],
            }
        ),
    )

    result = json.loads(raw)
    assert result["data"]["requested_output_designations"] == [
        {
            "output_path": OUTPUT_PATH,
            "label": "Visitors",
            "rendered_value": "8.89K",
            "selector_candidates": [{"selector": "#visitors", "match_count": 1, "position": 0}],
            "page_url": PAGE_URL,
        }
    ]


@pytest.mark.asyncio
@_requires_chromium
async def test_designation_probe_returns_every_verified_representation_without_choosing_for_the_model() -> None:
    async with async_playwright() as runner:
        browser = await runner.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(
                """
                <main id="analytics">
                  <section><h2>Bounces</h2><span class="metric-value">41%</span></section>
                  <section><h2>Visitors</h2><span class="metric-value">8.89K</span></section>
                </main>
                """
            )
            payload = await page.evaluate(value_designation_probe_expression("8.89K", "Visitors"))
        finally:
            await browser.close()

    assert payload["text"] == "8.89K"
    assert payload["selector_candidates"] == [
        {"selector": "span.metric-value", "match_count": 2, "position": 1},
        {
            "selector": "#analytics > section:nth-child(2) > span:nth-child(2)",
            "match_count": 1,
            "position": 0,
        },
    ]


@pytest.mark.asyncio
@_requires_chromium
async def test_designation_probe_verifies_the_model_cited_label_against_the_page() -> None:
    async with async_playwright() as runner:
        browser = await runner.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content("<section><h2>Visitors</h2><span>8.89K</span></section>")
            payload = await page.evaluate(value_designation_probe_expression("8.89K", "Conversions"))
        finally:
            await browser.close()

    assert payload == {"error": "label-not-associated", "text": "8.89K", "url": "about:blank"}

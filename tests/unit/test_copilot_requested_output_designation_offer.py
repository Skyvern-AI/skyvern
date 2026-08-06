"""Tests for the requested-output designation offer and when it is reachable (SKY-13485).

OSS-synced: only RFC-2606 placeholder data (example.com).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.enforcement import _witness_values_for_derivation
from skyvern.forge.sdk.copilot.output_extraction_plan import derive_requested_output_extraction_plan
from skyvern.forge.sdk.copilot.tools import (
    _accept_requested_output_reads,
    inspect_page_for_composition_tool,
)

PAGE_URL = "https://dashboard.example.com/records"
REQUESTED_PATH = "output.failed_records"
# The criterion's wording and the page's heading share nothing, so the label channel cannot bind.
CRITERION_LABEL = "the number of failed records is returned"
TILE_LABEL = "records found"
TILE_VALUE = "1.42K"


class _FakePage:
    """A discovery server whose designation probe answers from what the page currently renders."""

    def __init__(self, rendered: set[str]) -> None:
        self.rendered = rendered

    async def call_internal_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "skyvern_evaluate"
        expression = arguments["expression"]
        shown = [value for value in self.rendered if json.dumps(value) in expression]
        if not shown:
            return {"ok": True, "data": {"result": json.dumps({"error": "text-not-found"})}}
        return {
            "ok": True,
            "data": {
                "result": json.dumps(
                    {"text": shown[0], "selector": ".tile > span", "match_count": 1, "position": 0, "url": PAGE_URL}
                )
            },
        }


def _ctx(page: _FakePage) -> SimpleNamespace:
    return SimpleNamespace(
        discovery_mcp_server=page,
        requested_output_designations=[],
        composition_page_evidence={"current_url": PAGE_URL},
        scout_trajectory=[],
        flow_evidence=[],
        # Accepting a designation binds the plan at that observation, which reads the turn's
        # criteria and repair state.
        request_policy=None,
        completion_criteria_turn_state=None,
        copilot_config=None,
        last_code_authoring_repair_context=None,
        scouted_output_covered_paths=set(),
        reached_download_target=None,
        last_bound_requested_output_extraction_plan=None,
    )


def _read(output_path: str = REQUESTED_PATH, value_text: str = TILE_VALUE) -> dict[str, str]:
    return {"output_path": output_path, "value_text": value_text, "label": TILE_LABEL}


def _tile(key_text: str, value_text: str, *, selector: str) -> dict[str, Any]:
    return {
        "key_text": key_text,
        "value_text": value_text,
        "container_selector": selector,
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": 0,
        "direct_child_count": 2,
        "label_child_index": 1,
        "visible": True,
        "value_visible": True,
        "key_text_walked_count": 1,
        "value_text_walked_count": 1,
    }


def _loaded_packet() -> dict[str, Any]:
    return {
        "step": 12,
        "reached_via": "current_page",
        "had_bounded_schema": True,
        "evidence": {
            "source_tool": "inspect_page_for_composition",
            "current_url": PAGE_URL,
            "inspection_warnings": [],
            "result_containers_truncated": False,
            "key_value_relations_truncated": False,
            "key_value_relations": [
                _tile(TILE_LABEL, TILE_VALUE, selector=".tile"),
                _tile("of total records", "78.9%", selector=".share"),
            ],
            "result_containers": [],
        },
    }


@pytest.mark.asyncio
async def test_offer_pins_the_value_only_once_the_page_renders_it() -> None:
    # The login page is what a turn is looking at when authoring is first reached, so the offer taken
    # there has nothing to designate; the same offer has to still be takeable when the tile arrives.
    page = _FakePage({"Sign in"})
    ctx = _ctx(page)

    rejected = await _accept_requested_output_reads(ctx, [_read()], offered_by="synthesize_demonstrated_block")

    assert [entry["error"] for entry in rejected] == ["text-not-found"]
    assert ctx.requested_output_designations == []
    assert _witness_values_for_derivation(ctx) == {}

    page.rendered = {TILE_VALUE}
    rejected = await _accept_requested_output_reads(ctx, [_read()], offered_by="inspect_page_for_composition")

    assert rejected == []
    assert _witness_values_for_derivation(ctx) == {REQUESTED_PATH: TILE_VALUE}


@pytest.mark.asyncio
async def test_a_later_offer_keeps_what_an_earlier_offer_pinned() -> None:
    # The offer is reachable from every page inspection, so paths are designated as their values
    # render rather than all at once.
    page = _FakePage({TILE_VALUE, "42"})
    ctx = _ctx(page)

    await _accept_requested_output_reads(ctx, [_read()], offered_by="inspect_page_for_composition")
    await _accept_requested_output_reads(
        ctx, [_read("output.open_alerts", "42")], offered_by="inspect_page_for_composition"
    )

    assert _witness_values_for_derivation(ctx) == {REQUESTED_PATH: TILE_VALUE, "output.open_alerts": "42"}


@pytest.mark.asyncio
async def test_redesignating_a_path_replaces_its_earlier_value() -> None:
    page = _FakePage({"1.41K"})
    ctx = _ctx(page)

    await _accept_requested_output_reads(ctx, [_read(value_text="1.41K")], offered_by="inspect_page_for_composition")
    page.rendered = {TILE_VALUE}
    await _accept_requested_output_reads(ctx, [_read()], offered_by="inspect_page_for_composition")

    assert _witness_values_for_derivation(ctx) == {REQUESTED_PATH: TILE_VALUE}


def test_page_inspection_carries_the_offer() -> None:
    # The offer used to ride only the authoring tool, which a turn calls once and calls early.
    schema = inspect_page_for_composition_tool.params_json_schema

    assert "requested_output_reads" in schema["properties"]


@pytest.mark.asyncio
async def test_a_designated_value_binds_the_plan_no_label_could_bind() -> None:
    page = _FakePage({TILE_VALUE})
    ctx = _ctx(page)
    await _accept_requested_output_reads(ctx, [_read()], offered_by="inspect_page_for_composition")
    labels = {REQUESTED_PATH: (CRITERION_LABEL,)}

    assert (
        derive_requested_output_extraction_plan(
            flow_evidence=[_loaded_packet()], labels_by_path=labels, requested_paths={REQUESTED_PATH}
        )
        is None
    )

    plan = derive_requested_output_extraction_plan(
        flow_evidence=[_loaded_packet()],
        labels_by_path=labels,
        witnessed_by_path=_witness_values_for_derivation(ctx),
        requested_paths={REQUESTED_PATH},
    )

    assert plan is not None
    assert [binding.output_path for binding in plan.live_reads] == [REQUESTED_PATH]
    # The page's heading is not the criterion's wording, so the compiled read must not assert it.
    assert plan.live_reads[0].identified_by_label is False

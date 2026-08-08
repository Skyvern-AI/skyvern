"""The resolver's wiring at the page-observation seam (SKY-13485).

OSS-synced: only RFC-2606 placeholder data (example.com).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.tools import mcp_hooks

PATH = "output.failed_records"
PAGE_URL = "https://dashboard.example.com/records?query=failed"


def _relation(key_text: str, value_text: str) -> dict[str, Any]:
    return {
        "key_text": key_text,
        "value_text": value_text,
        "container_selector": f".tile-{key_text.replace(' ', '-')}",
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


def _tile_entry() -> dict[str, Any]:
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
            "key_value_relations": [_relation("records found", "1.42K")],
            "result_containers": [],
        },
    }


class _Ctx:
    def __init__(self, flow_evidence: list[dict[str, Any]]) -> None:
        self.flow_evidence = flow_evidence
        self.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(id="failed", outcome="the number of failed records is returned", output_path=PATH)
            ]
        )
        self.completion_criteria_turn_state = None
        self.copilot_config = None
        self.last_code_authoring_repair_context = None
        self.scouted_output_covered_paths = set()
        self.reached_download_target = None
        self.scout_trajectory: list[dict[str, Any]] = []
        self.requested_output_designations: list[dict[str, Any]] = []
        self.resolved_designation_fingerprints: set[str] = set()
        self.composition_page_evidence = {"current_url": PAGE_URL}
        self.last_bound_requested_output_extraction_plan = None
        self.workflow_permanent_id = "wpid_1"
        self.organization_id = "o_1"
        self.discovery_mcp_server = _Probe()


class _Probe:
    """Stands in for the live page: pins any value the model designates."""

    def __init__(self) -> None:
        self.calls = 0

    async def call_internal_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {
            "ok": True,
            "data": {
                "result": json.dumps(
                    {"text": "1.42K", "selector": ".tile > span", "match_count": 1, "position": 0, "url": PAGE_URL}
                )
            },
        }


class _MissingValueProbe:
    """The live page no longer shows the captured value, so the probe pins nothing."""

    def __init__(self) -> None:
        self.calls = 0

    async def call_internal_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"ok": True, "data": {"result": json.dumps({"error": "text-not-found"})}}


def _handler_returning(payload: object):
    async def handler(**_kwargs: Any) -> object:
        return payload

    return handler


@pytest.fixture(autouse=True)
def _stub_handler(monkeypatch: pytest.MonkeyPatch):
    def install(payload: object) -> None:
        monkeypatch.setattr(mcp_hooks, "resolve_main_copilot_handler", lambda *_a, **_k: _ready(payload))

    async def _ready(payload: object):
        return _handler_returning(payload)

    return install


@pytest.mark.asyncio
async def test_the_judge_is_told_which_page_the_values_came_from(monkeypatch: pytest.MonkeyPatch) -> None:
    # The page's own query is what makes a generic label ("records found") specific to the request,
    # so a prompt rendered without it asks the judge to reason from evidence it never received.
    seen: dict[str, str] = {}

    async def handler(*, prompt: str, **_kwargs: Any) -> object:
        seen["prompt"] = prompt
        return {"selections": []}

    async def _ready(*_a: Any, **_k: Any):
        return handler

    monkeypatch.setattr(mcp_hooks, "resolve_main_copilot_handler", _ready)

    await mcp_hooks.resolve_requested_output_designation_from_page_evidence(_Ctx([_tile_entry()]))

    # The query is the part that discriminates a generic label, so assert on it rather than the host.
    assert "query=failed" in seen["prompt"]


@pytest.mark.asyncio
async def test_a_selection_designates_and_binds_the_plan(_stub_handler) -> None:
    _stub_handler({"selections": [{"output_path": PATH, "candidate_index": 0}]})
    ctx = _Ctx([_tile_entry()])

    await mcp_hooks.resolve_requested_output_designation_from_page_evidence(ctx)

    assert [d["output_path"] for d in ctx.requested_output_designations] == [PATH]
    plan = ctx.last_bound_requested_output_extraction_plan
    assert plan is not None, "the plan must be committed at the observation that resolved it"
    assert [binding.output_path for binding in plan.live_reads] == [PATH]


@pytest.mark.asyncio
async def test_abstaining_designates_nothing_and_refuses_no_write(_stub_handler) -> None:
    _stub_handler({"selections": []})
    ctx = _Ctx([_tile_entry()])

    await mcp_hooks.resolve_requested_output_designation_from_page_evidence(ctx)

    assert ctx.requested_output_designations == []
    assert ctx.last_bound_requested_output_extraction_plan is None
    assert ctx.discovery_mcp_server.calls == 0


@pytest.mark.asyncio
async def test_the_decision_is_not_re_asked_for_the_same_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def handler(*, prompt: str, **_kwargs: Any) -> object:
        calls.append(prompt)
        return {"selections": []}

    async def _ready(*_a: Any, **_k: Any):
        return handler

    monkeypatch.setattr(mcp_hooks, "resolve_main_copilot_handler", _ready)
    ctx = _Ctx([_tile_entry()])

    await mcp_hooks.resolve_requested_output_designation_from_page_evidence(ctx)
    await mcp_hooks.resolve_requested_output_designation_from_page_evidence(ctx)

    # Counting the judge, not the fingerprint set: a resolver that re-ran and re-added the same
    # fingerprint would leave the set identical and pass regardless.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_probe_that_missed_leaves_the_tile_offerable(_stub_handler) -> None:
    # The metric ticked between capture and probe, so nothing was pinned. The fingerprint excludes
    # values, so recording it here would retire this tile for the rest of the turn.
    _stub_handler({"selections": [{"output_path": PATH, "candidate_index": 0}]})
    ctx = _Ctx([_tile_entry()])
    ctx.discovery_mcp_server = _MissingValueProbe()

    await mcp_hooks.resolve_requested_output_designation_from_page_evidence(ctx)

    assert ctx.requested_output_designations == []
    assert ctx.resolved_designation_fingerprints == set()


@pytest.mark.asyncio
async def test_a_page_with_no_candidates_asks_nothing(_stub_handler) -> None:
    _stub_handler({"selections": [{"output_path": PATH, "candidate_index": 0}]})
    entry = _tile_entry()
    entry["evidence"]["key_value_relations"] = []
    ctx = _Ctx([entry])

    await mcp_hooks.resolve_requested_output_designation_from_page_evidence(ctx)

    assert ctx.resolved_designation_fingerprints == set()
    assert ctx.requested_output_designations == []


@pytest.mark.asyncio
async def test_an_unavailable_judge_leaves_the_turn_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(**_kwargs: Any) -> object:
        raise RuntimeError("judge down")

    async def _ready(*_a: Any, **_k: Any):
        return _boom

    monkeypatch.setattr(mcp_hooks, "resolve_main_copilot_handler", _ready)
    ctx = _Ctx([_tile_entry()])

    await mcp_hooks.resolve_requested_output_designation_from_page_evidence(ctx)

    assert ctx.requested_output_designations == []
    assert ctx.last_bound_requested_output_extraction_plan is None
    # A judge that never answered decided nothing, and this page is the only one that can answer it.
    assert ctx.resolved_designation_fingerprints == set()

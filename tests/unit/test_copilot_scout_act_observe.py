"""Act-and-observe scouting (SKY-10932): a navigating scout click runs the bounded
page-side extractor synchronously and merges the schema into the same
scout_interaction flow-evidence packet before append, degrading to today's
schema-less packet on timeout/error/hollow parses."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot.composition_evidence import (
    _auto_credit_interaction_observation,
    has_bounded_page_schema,
)
from skyvern.forge.sdk.copilot.enforcement import _RECENT_TOOL_OUTPUT_CHAR_CAP
from skyvern.forge.sdk.copilot.tools import _click_post_hook
from skyvern.forge.sdk.copilot.tools.scouting import (
    _consume_pending_browser_interaction_observation,
    _mark_pending_browser_interaction_observation,
    account_no_progress_interaction_click,
)

_SOURCE_URL = "https://example.com/product"
_LANDING_URL = "https://example.com/results"

_SCHEMA_LESS_PACKET_KEYS = {
    "inspected_url",
    "current_url",
    "source_tool",
    "interaction_tool",
    "interaction_selector",
    "interaction_source_url",
}


def _bounded_extractor_payload() -> dict[str, Any]:
    return {
        "page_title": "Results",
        "forms": [
            {
                "fields": [
                    {"name": "npi", "label": "NPI number", "type": "text", "selector": "#npi"},
                    {"name": "state", "label": "State", "type": "select", "selector": "#state"},
                ],
                "submit_controls": [{"text": "Search", "type": "submit", "selector": "#go"}],
            }
        ],
        "navigation_targets": [
            {"text": "Provider details", "href": f"{_LANDING_URL}/details", "selector": "a.details"}
        ],
        "result_containers": [{"tag": "table", "id": "results", "selector": "#results"}],
        "modal_overlays": [
            {
                "role": "dialog",
                "selector": ".cookie-banner",
                "dismiss_controls": [{"tag": "button", "text": "Accept", "selector": ".cookie-accept"}],
            }
        ],
    }


def _ctx(*, server: Any = None, source_url: str | None = _SOURCE_URL) -> SimpleNamespace:
    return SimpleNamespace(
        pending_browser_interaction_observation=None,
        pending_scout_typed_value=None,
        pending_scout_role_name=None,
        discovery_mcp_server=server,
        scouted_interactions=[],
        scout_trajectory=[],
        pending_scout_source_url=source_url,
        flow_evidence=[],
    )


def _server_returning(payload: dict[str, Any]) -> SimpleNamespace:
    server = SimpleNamespace()
    server.call_internal_tool = AsyncMock(return_value={"ok": True, "data": {"result": payload}})
    return server


async def _run_click(ctx: SimpleNamespace) -> dict[str, Any]:
    return await _click_post_hook(
        {"ok": True, "data": {"selector": "#open-details"}},
        {"browser_context": {"url": _LANDING_URL, "title": "Results"}},
        ctx,
    )


def _flow_by_step(ctx: SimpleNamespace) -> dict[int, tuple[dict[str, Any], str]]:
    return {entry["step"]: (entry["evidence"], entry["reached_via"]) for entry in ctx.flow_evidence}


class TestActObserveSuccess:
    @pytest.mark.asyncio
    async def test_schema_merged_into_interaction_packet_before_append(self) -> None:
        ctx = _ctx(server=_server_returning(_bounded_extractor_payload()))

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert len(ctx.flow_evidence) == 1
        entry = ctx.flow_evidence[0]
        assert entry["had_bounded_schema"] is True
        assert entry["reached_via"] == "interaction"
        evidence = entry["evidence"]
        assert evidence["source_tool"] == "scout_interaction"
        assert evidence["interaction_tool"] == "click"
        assert evidence["interaction_selector"] == "#open-details"
        assert evidence["interaction_source_url"] == _SOURCE_URL
        assert evidence["inspected_url"] == _LANDING_URL
        assert evidence["current_url"] == _LANDING_URL
        assert has_bounded_page_schema(evidence)
        assert evidence["forms"][0]["fields"][0]["label"] == "NPI number"

    @pytest.mark.asyncio
    async def test_pending_marker_cleared_and_result_carries_summary(self) -> None:
        ctx = _ctx(server=_server_returning(_bounded_extractor_payload()))

        result = await _run_click(ctx)

        assert ctx.pending_browser_interaction_observation is None
        assert result["observation_step"] == ctx.flow_evidence[0]["step"]
        assert result["data"]["observation_step"] == ctx.flow_evidence[0]["step"]
        page = result["data"]["page"]
        assert page["page_title"] == "Results"
        assert page["forms"] == [
            {
                "field_count": 2,
                "fields": ["NPI number", "State"],
                "submit_controls": ["Search"],
            }
        ]
        assert page["navigation_target_count"] == 1
        assert page["navigation_targets"] == ["Provider details"]
        assert page["result_container_count"] == 1
        assert page["challenge_detected"] is False
        assert page["modal_dismiss_controls"] == ["Accept"]
        assert len(json.dumps(result)) <= _RECENT_TOOL_OUTPUT_CHAR_CAP


class TestActObserveDegrade:
    @pytest.mark.asyncio
    async def test_timeout_degrades_to_schema_less_packet_and_keeps_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS", 0.05)

        async def slow_extract(*_args: object, **_kwargs: object) -> dict[str, Any]:
            await asyncio.sleep(0.25)
            return {"ok": True, "data": {"result": _bounded_extractor_payload()}}

        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(side_effect=slow_extract)
        ctx = _ctx(server=server)

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert result["data"] == {
            "selector": "#open-details",
            "effective_target": "#open-details",
            "url": _LANDING_URL,
            "title": "Results",
            "observation_step": ctx.flow_evidence[0]["step"],
        }
        assert "page" not in result["data"]
        entry = ctx.flow_evidence[0]
        assert entry["had_bounded_schema"] is False
        assert set(entry["evidence"].keys()) == _SCHEMA_LESS_PACKET_KEYS
        assert ctx.pending_browser_interaction_observation is not None
        assert ctx.pending_browser_interaction_observation.tool_name == "click"
        assert ctx.pending_browser_interaction_observation.url == _LANDING_URL

    @pytest.mark.asyncio
    async def test_hollow_parse_degrades_to_schema_less_packet(self) -> None:
        ctx = _ctx(server=_server_returning({"page_title": "Loading", "forms": []}))

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert "page" not in result["data"]
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert set(ctx.flow_evidence[0]["evidence"].keys()) == _SCHEMA_LESS_PACKET_KEYS
        assert ctx.pending_browser_interaction_observation is not None

    @pytest.mark.asyncio
    async def test_extractor_error_never_fails_the_click(self) -> None:
        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(side_effect=RuntimeError("browser gone"))
        ctx = _ctx(server=server)

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert "page" not in result["data"]
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False


class TestActObserveNoRace:
    @pytest.mark.asyncio
    async def test_appended_entry_never_mutates_after_hook_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS", 0.05)

        async def slow_extract(*_args: object, **_kwargs: object) -> dict[str, Any]:
            await asyncio.sleep(0.2)
            return {"ok": True, "data": {"result": _bounded_extractor_payload()}}

        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(side_effect=slow_extract)
        ctx = _ctx(server=server)

        await _run_click(ctx)
        snapshot = copy.deepcopy(ctx.flow_evidence[0])
        assert snapshot["had_bounded_schema"] is False

        await asyncio.sleep(0.4)

        assert ctx.flow_evidence[0] == snapshot
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert not has_bounded_page_schema(ctx.flow_evidence[0]["evidence"])

    @pytest.mark.asyncio
    async def test_successful_attach_credits_exactly_once(self) -> None:
        ctx = _ctx(server=_server_returning(_bounded_extractor_payload()))

        await _run_click(ctx)

        # The pending marker was consumed by the synchronous attach, so a later
        # evaluate/inspect on the same page cannot mint a second interaction packet.
        later_inspect_evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"name": "npi"}]}],
        }
        assert (
            _consume_pending_browser_interaction_observation(
                ctx, current_url=_LANDING_URL, evidence=later_inspect_evidence
            )
            is False
        )

        by_step = _flow_by_step(ctx)
        consumed: set[int] = set()
        assert _auto_credit_interaction_observation(by_step, consumed) is True
        credited_evidence, _ = by_step[next(iter(consumed))]
        assert has_bounded_page_schema(credited_evidence)
        assert _auto_credit_interaction_observation(by_step, consumed) is False

    @pytest.mark.asyncio
    async def test_degraded_path_preserves_pending_upgrade(self) -> None:
        ctx = _ctx(server=_server_returning({"page_title": "Loading", "forms": []}))

        await _run_click(ctx)

        later_inspect_evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"name": "npi"}]}],
        }
        assert (
            _consume_pending_browser_interaction_observation(
                ctx, current_url=_LANDING_URL, evidence=later_inspect_evidence
            )
            is True
        )
        assert ctx.pending_browser_interaction_observation is None


class TestActObserveSummaryBound:
    @pytest.mark.asyncio
    async def test_adversarial_page_never_clips_serialized_result(self) -> None:
        payload = {
            "page_title": "R" * 300,
            "forms": [
                {
                    "fields": [
                        {
                            "name": f"field_{form_index}_{field_index}",
                            "label": f"Label {form_index}-{field_index} " + "x" * 240,
                            "type": "text",
                            "selector": f"#f{form_index}-{field_index}",
                        }
                        for field_index in range(20)
                    ],
                    "submit_controls": [
                        {"text": f"Submit {form_index} " + "y" * 120, "type": "submit"} for _ in range(10)
                    ],
                }
                for form_index in range(5)
            ],
            "navigation_targets": [
                {
                    "text": f"Nav {nav_index} " + "z" * 160,
                    "href": f"{_LANDING_URL}/nav/{nav_index}",
                    "selector": f"a.nav-{nav_index}",
                }
                for nav_index in range(20)
            ],
            "result_containers": [
                {"tag": "table", "id": f"results-{index}", "selector": f"#results-{index}"} for index in range(8)
            ],
            "modal_overlays": [
                {
                    "role": "dialog",
                    "selector": f".overlay-{index}",
                    "dismiss_controls": [
                        {"tag": "button", "text": f"Dismiss {index}-{control} " + "w" * 100} for control in range(6)
                    ],
                }
                for index in range(5)
            ],
        }
        ctx = _ctx(server=_server_returning(payload))

        result = await _run_click(ctx)

        assert result["ok"] is True
        serialized = json.dumps(result)
        assert len(serialized) <= _RECENT_TOOL_OUTPUT_CHAR_CAP
        assert json.loads(serialized) == result
        # The flow-evidence packet keeps the full schema; only the tool result is compact.
        assert ctx.flow_evidence[0]["had_bounded_schema"] is True
        assert len(ctx.flow_evidence[0]["evidence"]["forms"]) == 5


class TestActObserveToolGate:
    @pytest.mark.asyncio
    async def test_non_click_tools_do_not_capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def passes(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(tools_module.mcp_hooks, "_verify_scout_type_landed", passes)
        server = _server_returning(_bounded_extractor_payload())
        ctx = _ctx(server=server, source_url=_SOURCE_URL)

        result = await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": 'role=textbox[name="Search"]', "text_length": 8}},
            {"browser_context": {"url": _LANDING_URL, "title": "Results"}},
            ctx,
        )

        server.call_internal_tool.assert_not_awaited()
        assert result["ok"] is True
        assert "page" not in result["data"]
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False


def _np_ctx(*, server: Any = None, counter: int = 0) -> SimpleNamespace:
    ctx = _ctx(server=server)
    ctx.consecutive_no_progress_interaction_count = counter
    ctx.last_scout_act_observe_outcome = None
    ctx.blocker_signal = None
    return ctx


class TestNoProgressInteractionAccounting:
    @pytest.mark.asyncio
    async def test_hollow_click_increments_counter(self) -> None:
        ctx = _np_ctx(server=_server_returning({"page_title": "Loading", "forms": []}))

        await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert ctx.consecutive_no_progress_interaction_count == 1

    @pytest.mark.asyncio
    async def test_attached_click_resets_counter(self) -> None:
        ctx = _np_ctx(server=_server_returning(_bounded_extractor_payload()), counter=2)

        await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "attached"
        assert ctx.consecutive_no_progress_interaction_count == 0

    @pytest.mark.asyncio
    async def test_failed_click_increments_counter(self) -> None:
        ctx = _np_ctx()

        await _click_post_hook(
            {"ok": False, "error": "Timeout 5000ms exceeded"},
            {"browser_context": {"url": _LANDING_URL}},
            ctx,
        )

        assert ctx.consecutive_no_progress_interaction_count == 1

    @pytest.mark.asyncio
    async def test_capture_timeout_observe_is_neutral(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS", 0.05)

        async def slow_extract(*_args: object, **_kwargs: object) -> dict[str, Any]:
            await asyncio.sleep(0.25)
            return {"ok": True, "data": {"result": _bounded_extractor_payload()}}

        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(side_effect=slow_extract)
        ctx = _np_ctx(server=server, counter=2)

        await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "timeout"
        assert ctx.consecutive_no_progress_interaction_count == 2

    def test_neutral_outcome_does_not_touch_counter(self) -> None:
        ctx = _np_ctx(counter=2)
        ctx.last_scout_act_observe_outcome = None

        account_no_progress_interaction_click(ctx, {"ok": True, "data": {"selector": "#x"}})

        assert ctx.consecutive_no_progress_interaction_count == 2

    def test_delayed_credit_via_consume_resets_counter(self) -> None:
        ctx = _np_ctx(counter=3)
        _mark_pending_browser_interaction_observation(ctx, tool_name="click", url=_LANDING_URL)

        consumed = _consume_pending_browser_interaction_observation(
            ctx,
            current_url=_LANDING_URL,
            evidence={**_bounded_extractor_payload(), "current_url": _LANDING_URL},
        )

        assert consumed is True
        assert ctx.consecutive_no_progress_interaction_count == 0

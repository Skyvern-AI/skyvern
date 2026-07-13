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
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.composition_evidence import (
    _auto_credit_interaction_observation,
    has_bounded_page_schema,
)
from skyvern.forge.sdk.copilot.context import (
    FillCarry,
    StructuredContext,
    finalize_discovery_counter_in_global_llm_context,
)
from skyvern.forge.sdk.copilot.enforcement import (
    _RECENT_TOOL_OUTPUT_CHAR_CAP,
    MAX_NO_PROGRESS_INTERACTION_ATTEMPTS,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import _click_post_hook
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.copilot.tools.scouting import (
    _capture_scout_source_url,
    _consume_pending_browser_interaction_observation,
    _mark_pending_browser_interaction_observation,
    _maybe_rebind_prior_fill_carry,
    _register_scout_interaction_observation,
    _scout_act_observe_page_evidence,
    account_no_progress_interaction_click,
    rebind_prior_fill_carry_from_current_page,
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
        pending_scout_click_selector=None,
        discovery_mcp_server=server,
        scouted_interactions=[],
        scout_trajectory=[],
        prior_fill_carry=[],
        fill_carry_rebound_done=False,
        pending_scout_source_url=source_url,
        flow_evidence=[],
    )


def _server_returning(payload: dict[str, Any]) -> SimpleNamespace:
    server = SimpleNamespace()
    server.call_internal_tool = AsyncMock(return_value={"ok": True, "data": {"result": payload}})
    return server


def _server_returning_sequence(payloads: list[dict[str, Any] | None | BaseException]) -> SimpleNamespace:
    server = SimpleNamespace()
    side_effects = [
        payload if isinstance(payload, BaseException) else {"ok": True, "data": {"result": payload}}
        for payload in payloads
    ]
    server.call_internal_tool = AsyncMock(side_effect=side_effects)
    return server


async def _selector_count_one(
    _ctx: SimpleNamespace, _selector: str | None, *, timeout_seconds: float | None = None
) -> int:
    return 1


async def _role_name_textbox_account(
    _ctx: SimpleNamespace, _selector: str | None, *, allow_browser_read: bool = True
) -> tuple[str, str]:
    return "textbox", "Account"


def _monotonic_sequence(values: list[float]) -> Any:
    calls = {"n": 0}

    def fake() -> float:
        index = calls["n"]
        calls["n"] += 1
        return values[index] if index < len(values) else values[-1]

    return fake


async def _run_click(ctx: SimpleNamespace) -> dict[str, Any]:
    return await _click_post_hook(
        {"ok": True, "data": {"selector": "#open-details"}},
        {"browser_context": {"url": _LANDING_URL, "title": "Results"}},
        ctx,
    )


def _flow_by_step(ctx: SimpleNamespace) -> dict[int, tuple[dict[str, Any], str]]:
    return {entry["step"]: (entry["evidence"], entry["reached_via"]) for entry in ctx.flow_evidence}


class TestFillCarryRebind:
    @pytest.mark.asyncio
    async def test_rebinds_prior_fill_carry_after_fresh_page_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        monkeypatch.setattr(scouting_module, "_resolve_scout_role_name", _role_name_textbox_account)
        ctx = _ctx()
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "#open", "source_url": _LANDING_URL}]
        ctx.prior_fill_carry = [
            FillCarry(
                source_url=_LANDING_URL,
                selector="#account",
                tool_name="type_text",
                role="textbox",
                accessible_name="Account",
                typed_length=6,
                typed_value="ABC123",
            ).model_dump(),
            FillCarry(
                source_url=_LANDING_URL,
                selector="#plan",
                tool_name="select_option",
                value="premium",
            ).model_dump(),
            FillCarry(
                source_url=_LANDING_URL,
                selector="#password",
                tool_name="fill_credential_field",
                credential_id="cred_123",
                credential_field="password",
            ).model_dump(),
        ]
        ctx.fill_carry_rebound_done = False
        evidence = {
            "current_url": _LANDING_URL,
            "forms": [
                {
                    "fields": [
                        {"selector": "#account", "label": "Account"},
                        {"selector": "#plan", "label": "Plan"},
                        {"selector": "#password", "label": "Password"},
                    ]
                }
            ],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=evidence, url=_LANDING_URL)

        assert ctx.fill_carry_rebound_done is True
        assert ctx.scout_trajectory[0] == {"tool_name": "click", "selector": "#open", "source_url": _LANDING_URL}
        carried = ctx.scout_trajectory[1:]
        assert [(item["tool_name"], item["selector"], item["trajectory_index"]) for item in carried] == [
            ("type_text", "#account", 1),
            ("select_option", "#plan", 2),
            ("fill_credential_field", "#password", 3),
        ]
        assert [item["carried"] for item in carried] == [True, True, True]
        assert carried[0]["typed_value"] == "ABC123"
        assert carried[1]["value"] == "premium"
        assert carried[2]["credential_id"] == "cred_123"
        assert carried[2]["credential_field"] == "password"

    @pytest.mark.asyncio
    async def test_rebinds_when_page_evidence_selector_format_differs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        monkeypatch.setattr(scouting_module, "_resolve_scout_role_name", _role_name_textbox_account)
        ctx = _ctx()
        ctx.prior_fill_carry = [
            FillCarry(
                source_url=_LANDING_URL,
                selector="#account",
                tool_name="type_text",
                role="textbox",
                accessible_name="Account",
                typed_length=6,
                typed_value="ABC123",
            ).model_dump()
        ]
        evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"selector": "input#account", "label": "Account"}]}],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=evidence, url=_LANDING_URL)

        assert ctx.fill_carry_rebound_done is True
        assert [(item["tool_name"], item["selector"], item["carried"]) for item in ctx.scout_trajectory] == [
            ("type_text", "#account", True)
        ]

    @pytest.mark.asyncio
    async def test_rebinds_top_level_inputs_as_field_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx()
        ctx.prior_fill_carry = [
            FillCarry(
                source_url=_LANDING_URL,
                selector="#company",
                tool_name="type_text",
                role="textbox",
                typed_length=23,
                typed_value="Example Realty Labs Inc",
            ).model_dump()
        ]
        evidence = {
            "current_url": _LANDING_URL,
            "inputs": [{"selector": "input#company"}],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=evidence, url=_LANDING_URL)

        assert ctx.fill_carry_rebound_done is True
        assert [(item["tool_name"], item["selector"], item["carried"]) for item in ctx.scout_trajectory] == [
            ("type_text", "#company", True)
        ]

    @pytest.mark.asyncio
    async def test_rebinds_prior_fill_carry_from_live_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_live_working_page_url", AsyncMock(return_value=_LANDING_URL))
        monkeypatch.setattr(
            scouting_module,
            "_scout_act_observe_page_evidence",
            AsyncMock(
                return_value={
                    "current_url": _LANDING_URL,
                    "forms": [{"fields": [{"selector": "#account", "label": "Account"}]}],
                }
            ),
        )
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        monkeypatch.setattr(scouting_module, "_resolve_scout_role_name", _role_name_textbox_account)
        ctx = _ctx()
        ctx.prior_fill_carry = [
            FillCarry(
                source_url=_LANDING_URL,
                selector="#account",
                tool_name="type_text",
                role="textbox",
                accessible_name="Account",
                typed_length=6,
                typed_value="ABC123",
            ).model_dump()
        ]
        ctx.fill_carry_rebound_done = False

        rebound = await rebind_prior_fill_carry_from_current_page(ctx)

        assert rebound is True
        assert ctx.fill_carry_rebound_done is True
        assert ctx.scout_trajectory == [
            {
                "tool_name": "type_text",
                "selector": "#account",
                "source_url": _LANDING_URL,
                "trajectory_index": 0,
                "carried": True,
                "role": "textbox",
                "accessible_name": "Account",
                "typed_length": 6,
                "typed_value": "ABC123",
            }
        ]

    @pytest.mark.asyncio
    async def test_page_mismatch_keeps_prior_fill_carry_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx()
        ctx.prior_fill_carry = [
            FillCarry(
                source_url=_LANDING_URL,
                selector="#account",
                tool_name="type_text",
                typed_length=6,
                typed_value="ABC123",
            ).model_dump()
        ]
        mismatched = {
            "current_url": _SOURCE_URL,
            "forms": [{"fields": [{"selector": "#account", "label": "Account"}]}],
        }
        matched = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"selector": "#account", "label": "Account"}]}],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=mismatched, url=_SOURCE_URL)
        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=matched, url=_LANDING_URL)

        assert ctx.fill_carry_rebound_done is True
        assert [(item["tool_name"], item["selector"]) for item in ctx.scout_trajectory] == [("type_text", "#account")]
        assert ctx.scout_trajectory[0]["carried"] is True

    @pytest.mark.asyncio
    async def test_source_capture_rebinds_company_and_email_before_submit_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scouting_module, "_live_working_page_url", AsyncMock(return_value=_LANDING_URL))
        monkeypatch.setattr(
            scouting_module,
            "_scout_act_observe_page_evidence",
            AsyncMock(
                return_value={
                    "current_url": _LANDING_URL,
                    "forms": [
                        {
                            "fields": [
                                {"selector": "#company", "label": "Business name", "type": "text"},
                                {"selector": "#email", "label": "Contact email", "type": "email"},
                            ],
                            "submit_controls": [{"selector": "#submit", "text": "Submit"}],
                        }
                    ],
                }
            ),
        )
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx(source_url=None)
        ctx.prior_fill_carry = [
            FillCarry(
                source_url=_LANDING_URL, selector="#company", tool_name="type_text", typed_length=24
            ).model_dump(),
            FillCarry(source_url=_LANDING_URL, selector="#email", tool_name="type_text", typed_length=29).model_dump(),
        ]

        await _capture_scout_source_url(ctx)

        assert ctx.pending_scout_source_url == _LANDING_URL
        assert ctx.fill_carry_rebound_done is True
        assert [(item["tool_name"], item["selector"], item["carried"]) for item in ctx.scout_trajectory] == [
            ("type_text", "#company", True),
            ("type_text", "#email", True),
        ]

    @pytest.mark.asyncio
    async def test_rebind_degrades_when_selector_is_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def selector_count_zero(
            _ctx: SimpleNamespace, _selector: str | None, *, timeout_seconds: float | None = None
        ) -> int:
            return 0

        monkeypatch.setattr(scouting_module, "_selector_live_match_count", selector_count_zero)
        monkeypatch.setattr(scouting_module, "_resolve_scout_role_name", _role_name_textbox_account)
        ctx = _ctx()
        ctx.prior_fill_carry = [
            FillCarry(source_url=_LANDING_URL, selector="#missing", tool_name="type_text", typed_length=4).model_dump()
        ]
        ctx.fill_carry_rebound_done = False
        evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"selector": "#missing", "label": "Account"}]}],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=evidence, url=_LANDING_URL)

        assert ctx.fill_carry_rebound_done is True
        assert ctx.scout_trajectory == []

    @pytest.mark.asyncio
    async def test_rebind_degrades_without_selector_or_role_anchor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx()
        ctx.prior_fill_carry = [
            FillCarry(source_url=_LANDING_URL, selector="#account", tool_name="type_text", typed_length=4).model_dump()
        ]
        evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"selector": "input#account", "label": "Account"}]}],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=evidence, url=_LANDING_URL)

        assert ctx.fill_carry_rebound_done is False
        assert ctx.scout_trajectory == []

    @pytest.mark.asyncio
    async def test_rebind_ignores_different_page_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        monkeypatch.setattr(scouting_module, "_resolve_scout_role_name", _role_name_textbox_account)
        ctx = _ctx()
        ctx.prior_fill_carry = [
            FillCarry(source_url=_SOURCE_URL, selector="#account", tool_name="type_text", typed_length=4).model_dump()
        ]
        ctx.fill_carry_rebound_done = False
        evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"selector": "#account", "label": "Account"}]}],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=evidence, url=_LANDING_URL)

        assert ctx.fill_carry_rebound_done is False
        assert ctx.scout_trajectory == []


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
    async def test_first_hollow_then_bounded_recapture_attaches_schema(self) -> None:
        ctx = _ctx(
            server=_server_returning_sequence([{"page_title": "Loading", "forms": []}, _bounded_extractor_payload()])
        )

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "attached"
        assert result["ok"] is True
        assert "page" in result["data"]
        assert ctx.flow_evidence[0]["had_bounded_schema"] is True
        assert ctx.flow_evidence[0]["evidence"]["forms"][0]["fields"][0]["label"] == "NPI number"
        assert not hasattr(ctx, "latest_recorded_build_test_outcome")

    @pytest.mark.asyncio
    async def test_persistent_post_interaction_hollow_records_build_test_outcome(self) -> None:
        payload = {"page_title": "Loading", "forms": [], "body": "<main></main>", "visible_text": "Still loading"}
        ctx = _ctx(
            server=_server_returning_sequence([payload, payload]), source_url="https://example.com/path?secret=1"
        )

        result = await _run_click(ctx)

        outcome = ctx.latest_recorded_build_test_outcome
        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert result["ok"] is True
        assert "page" not in result["data"]
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert outcome.reason_code == "scout_act_observe_hollow_after_interaction"
        assert outcome.verdict == "repairable_failure"
        assert outcome.is_authoritative is True
        assert outcome.attempted_tool == "scout_interaction"
        assert outcome.attempted_target == "#open-details"
        assert "secret" not in str(outcome.structural_key_payload)
        assert "recapture_attempted:true" in outcome.page_evidence_refs
        assert "recapture_result:hollow" in outcome.page_evidence_refs
        assert ctx.recorded_build_test_outcome_history[-1]["reason_code"] == outcome.reason_code

    @pytest.mark.asyncio
    async def test_first_hollow_with_no_recapture_budget_records_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(scouting_module.time, "monotonic", _monotonic_sequence([0.0, 2.0, 2.0]))
        payload = {"page_title": "Loading", "forms": [], "body": "<main></main>"}
        ctx = _ctx(server=_server_returning_sequence([payload]))

        result = await _run_click(ctx)

        outcome = ctx.latest_recorded_build_test_outcome
        assert result["ok"] is True
        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert outcome.reason_code == "scout_act_observe_hollow_after_interaction"
        assert outcome.is_authoritative is True
        assert "recapture_attempted:false" in outcome.page_evidence_refs
        assert "recapture_result:not_attempted_no_budget" in outcome.page_evidence_refs

    @pytest.mark.asyncio
    async def test_first_hollow_with_recapture_none_records_outcome(self) -> None:
        payload = {"page_title": "Loading", "forms": [], "body": "<main></main>"}
        ctx = _ctx(server=_server_returning_sequence([payload, None]))

        result = await _run_click(ctx)

        outcome = ctx.latest_recorded_build_test_outcome
        assert result["ok"] is True
        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert outcome.reason_code == "scout_act_observe_hollow_after_interaction"
        assert outcome.is_authoritative is True
        assert "recapture_attempted:true" in outcome.page_evidence_refs
        assert "recapture_result:no_payload" in outcome.page_evidence_refs

    @pytest.mark.asyncio
    async def test_first_hollow_with_recapture_error_records_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {"page_title": "Loading", "forms": [], "body": "<main></main>"}
        calls = {"n": 0}

        async def fake_extract(
            _ctx: Any, *, inspected_url: str, current_url: str, timeout_seconds: float
        ) -> dict[str, Any] | None:
            calls["n"] += 1
            if calls["n"] == 1:
                return payload
            raise RuntimeError("browser gone")

        monkeypatch.setattr(scouting_module, "_composition_get_structured_evidence", fake_extract)
        ctx = _ctx(server=SimpleNamespace())

        result = await _run_click(ctx)

        outcome = ctx.latest_recorded_build_test_outcome
        assert result["ok"] is True
        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert outcome.reason_code == "scout_act_observe_hollow_after_interaction"
        assert outcome.is_authoritative is True
        assert "recapture_attempted:true" in outcome.page_evidence_refs
        assert "recapture_result:error" in outcome.page_evidence_refs

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
        assert not hasattr(ctx, "latest_recorded_build_test_outcome")

    @pytest.mark.asyncio
    async def test_hollow_parse_degrades_to_schema_less_packet(self) -> None:
        ctx = _ctx(server=_server_returning({"page_title": "Loading", "forms": []}))

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert "page" not in result["data"]
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert set(ctx.flow_evidence[0]["evidence"].keys()) == _SCHEMA_LESS_PACKET_KEYS
        assert ctx.pending_browser_interaction_observation is not None
        assert ctx.latest_recorded_build_test_outcome.reason_code == "scout_act_observe_hollow_after_interaction"

    @pytest.mark.asyncio
    async def test_extractor_error_never_fails_the_click(self) -> None:
        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(side_effect=RuntimeError("browser gone"))
        ctx = _ctx(server=server)

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert "page" not in result["data"]
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert not hasattr(ctx, "latest_recorded_build_test_outcome")

    @pytest.mark.asyncio
    async def test_initial_none_without_first_hollow_does_not_record_outcome(self) -> None:
        ctx = _ctx(server=_server_returning_sequence([None]))

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False
        assert not hasattr(ctx, "latest_recorded_build_test_outcome")

    @pytest.mark.asyncio
    async def test_hollow_without_interaction_proof_does_not_record_outcome(self) -> None:
        ctx = _ctx(server=_server_returning({"page_title": "Loading", "forms": []}))

        parsed = await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)
        step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="click", selector="", source_url=_SOURCE_URL, url=_LANDING_URL
        )

        assert parsed is not None
        assert not has_bounded_page_schema(parsed)
        assert step is None
        assert page_evidence is None
        assert not hasattr(ctx, "latest_recorded_build_test_outcome")


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

        awaited_tools = [call.args[0] for call in server.call_internal_tool.await_args_list]
        assert awaited_tools == ["skyvern_get_value"]
        assert result["ok"] is True
        assert "page" not in result["data"]
        assert ctx.flow_evidence[0]["had_bounded_schema"] is False

    @pytest.mark.asyncio
    async def test_bare_css_selector_probes_control_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def passes(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(tools_module.mcp_hooks, "_verify_scout_type_landed", passes)
        server = _server_returning(_bounded_extractor_payload())
        ctx = _ctx(server=server, source_url=_SOURCE_URL)

        await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": "#electricDate", "text_length": 10}},
            {"browser_context": {"url": _LANDING_URL, "title": "Results"}},
            ctx,
        )

        probed = [
            call
            for call in server.call_internal_tool.await_args_list
            if call.args and call.args[0] == "skyvern_evaluate" and "readonly" in call.args[1]["expression"]
        ]
        assert len(probed) == 1


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


def _standalone_controls_payload() -> dict[str, Any]:
    return {
        "page_title": "Account Information",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "clickable_controls": [
            {"selector": "#biz-tile", "text": "Business"},
            {"selector": 'div[data-action="selectAddress"]', "text": "2468 Peach Orchard Ct"},
        ],
    }


def _ungroundable_payload() -> dict[str, Any]:
    return {"page_title": "Loading", "forms": []}


class TestNonAdvancingClickReperception:
    @pytest.mark.asyncio
    async def test_hollow_click_attaches_grounded_targets_without_touching_outcome(self) -> None:
        ctx = _np_ctx(server=_server_returning(_standalone_controls_payload()))

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert ctx.consecutive_no_progress_interaction_count == 1
        selectors = {target.get("selector") for target in result["data"]["actionable_targets"]}
        assert "#biz-tile" in selectors
        assert 'div[data-action="selectAddress"]' in selectors

    @pytest.mark.asyncio
    async def test_failed_click_attaches_grounded_targets(self) -> None:
        ctx = _np_ctx(server=_server_returning(_standalone_controls_payload()))

        result = await _click_post_hook(
            {"ok": False, "error": "Timeout 5000ms exceeded"},
            {"browser_context": {"url": _LANDING_URL}},
            ctx,
        )

        assert ctx.consecutive_no_progress_interaction_count == 1
        selectors = {target.get("selector") for target in result["data"]["actionable_targets"]}
        assert "#biz-tile" in selectors

    @pytest.mark.asyncio
    async def test_ungroundable_non_advancing_click_attaches_no_targets(self) -> None:
        ctx = _np_ctx(server=_server_returning(_ungroundable_payload()))

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert ctx.consecutive_no_progress_interaction_count == 1
        assert "actionable_targets" not in result["data"]

    @pytest.mark.asyncio
    async def test_empty_url_attaches_no_targets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_url(_raw: dict[str, Any], _ctx: AgentContext) -> tuple[str, str]:
            return "", ""

        monkeypatch.setattr(tools_module.mcp_hooks, "_resolve_url_title", no_url)
        ctx = _np_ctx(server=_server_returning(_standalone_controls_payload()))

        result = await _click_post_hook(
            {"ok": False, "error": "Timeout 5000ms exceeded"},
            {"browser_context": {}},
            ctx,
        )

        assert ctx.consecutive_no_progress_interaction_count == 1
        assert "actionable_targets" not in (result.get("data") or {})

    @pytest.mark.asyncio
    async def test_attached_click_does_not_attach_reperception_targets(self) -> None:
        ctx = _np_ctx(server=_server_returning(_bounded_extractor_payload()))

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "attached"
        assert "actionable_targets" not in result["data"]

    @pytest.mark.asyncio
    async def test_ungroundable_churn_still_halts_at_max_without_reset(self) -> None:
        ctx = AgentContext(
            organization_id="o_1",
            workflow_id="w_1",
            workflow_permanent_id="wpid_1",
            workflow_yaml="",
            browser_session_id="pbs_1",
            stream=MagicMock(),
        )
        ctx.discovery_mcp_server = _server_returning(_ungroundable_payload())

        for expected in range(1, MAX_NO_PROGRESS_INTERACTION_ATTEMPTS + 1):
            result = await _run_click(ctx)
            assert "actionable_targets" not in result["data"]
            assert ctx.consecutive_no_progress_interaction_count == expected

        assert ctx.consecutive_no_progress_interaction_count == MAX_NO_PROGRESS_INTERACTION_ATTEMPTS
        assert ctx.blocker_signal is not None


def _sequenced_evidence(parses: list[dict[str, Any] | None]) -> Any:
    calls = {"n": 0}

    async def fake(_ctx: Any, *, inspected_url: str, current_url: str, timeout_seconds: float) -> dict[str, Any] | None:
        index = calls["n"]
        calls["n"] += 1
        return parses[index] if index < len(parses) else parses[-1]

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def _live_match(count: int | None) -> Any:
    async def fake(_ctx: Any, _selector: str | None, *, timeout_seconds: float | None = None) -> int | None:
        return count

    return fake


class TestPreconditionGatedClickSettle:
    @pytest.mark.asyncio
    async def test_settle_attaches_grounded_targets_and_steer_after_pending_ajax(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(
            tools_module.mcp_hooks,
            "_composition_get_structured_evidence",
            _sequenced_evidence([None, None, _standalone_controls_payload()]),
        )
        monkeypatch.setattr(tools_module.mcp_hooks, "_selector_live_match_count", _live_match(1))
        ctx = _np_ctx(server=_server_returning(_ungroundable_payload()))
        ctx.pending_scout_click_selector = 'button[data-action="selectAddress"]'

        result = await _click_post_hook(
            {"ok": False, "error": "Timeout 10000ms exceeded"},
            {"browser_context": {"url": _LANDING_URL}},
            ctx,
        )

        data = result["data"]
        selectors = {target.get("selector") for target in data["actionable_targets"]}
        assert "#biz-tile" in selectors
        assert data["next_action"] == "click"
        assert data["next_action_reason"]
        assert ctx.consecutive_no_progress_interaction_count == 1
        assert ctx.last_scout_act_observe_outcome is None

    @pytest.mark.asyncio
    async def test_zero_match_invented_selector_skips_settle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_DELAY_SECONDS", 0.0)
        evidence = _sequenced_evidence([None, _standalone_controls_payload()])
        monkeypatch.setattr(tools_module.mcp_hooks, "_composition_get_structured_evidence", evidence)
        monkeypatch.setattr(tools_module.mcp_hooks, "_selector_live_match_count", _live_match(0))
        ctx = _np_ctx(server=_server_returning(_ungroundable_payload()))
        ctx.pending_scout_click_selector = "button[data-action='invented']"

        result = await _click_post_hook(
            {"ok": False, "error": "Timeout 10000ms exceeded"},
            {"browser_context": {"url": _LANDING_URL}},
            ctx,
        )

        assert "actionable_targets" not in (result.get("data") or {})
        assert evidence.calls["n"] == 1
        assert ctx.consecutive_no_progress_interaction_count == 1

    @pytest.mark.asyncio
    async def test_settle_is_bounded_and_attaches_nothing_when_never_populates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_MAX_PROBES", 3)
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_DEADLINE_SECONDS", 5.0)
        evidence = _sequenced_evidence([None])
        monkeypatch.setattr(tools_module.mcp_hooks, "_composition_get_structured_evidence", evidence)
        monkeypatch.setattr(tools_module.mcp_hooks, "_selector_live_match_count", _live_match(1))
        ctx = _np_ctx(server=_server_returning(_ungroundable_payload()))
        ctx.pending_scout_click_selector = 'button[data-action="selectAddress"]'

        result = await _click_post_hook(
            {"ok": False, "error": "Timeout 10000ms exceeded"},
            {"browser_context": {"url": _LANDING_URL}},
            ctx,
        )

        assert "actionable_targets" not in (result.get("data") or {})
        assert evidence.calls["n"] == 4
        assert ctx.consecutive_no_progress_interaction_count == 1

    @pytest.mark.asyncio
    async def test_unreadable_selector_skips_settle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_DELAY_SECONDS", 0.0)
        evidence = _sequenced_evidence([None, _standalone_controls_payload()])
        monkeypatch.setattr(tools_module.mcp_hooks, "_composition_get_structured_evidence", evidence)
        monkeypatch.setattr(tools_module.mcp_hooks, "_selector_live_match_count", _live_match(None))
        ctx = _np_ctx(server=_server_returning(_ungroundable_payload()))

        result = await _click_post_hook(
            {"ok": False, "error": "Timeout 10000ms exceeded"},
            {"browser_context": {"url": _LANDING_URL}},
            ctx,
        )

        assert "actionable_targets" not in (result.get("data") or {})
        assert evidence.calls["n"] == 1
        assert ctx.consecutive_no_progress_interaction_count == 1

    @pytest.mark.asyncio
    async def test_expired_deadline_runs_no_warrants_or_extractor_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_MAX_PROBES", 5)
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_DEADLINE_SECONDS", 0.0)
        evidence = _sequenced_evidence([None, _standalone_controls_payload()])
        monkeypatch.setattr(tools_module.mcp_hooks, "_composition_get_structured_evidence", evidence)
        live = _counting_live_match(1)
        monkeypatch.setattr(tools_module.mcp_hooks, "_selector_live_match_count", live)
        ctx = _np_ctx(server=_server_returning(_ungroundable_payload()))
        ctx.pending_scout_click_selector = 'button[data-action="selectAddress"]'

        result = await _click_post_hook(
            {"ok": False, "error": "Timeout 10000ms exceeded"},
            {"browser_context": {"url": _LANDING_URL}},
            ctx,
        )

        assert "actionable_targets" not in (result.get("data") or {})
        assert evidence.calls["n"] == 1
        assert live.calls["n"] == 0
        assert ctx.consecutive_no_progress_interaction_count == 1


def _counting_live_match(count: int | None) -> Any:
    calls = {"n": 0}

    async def fake(_ctx: Any, _selector: str | None, *, timeout_seconds: float | None = None) -> int | None:
        calls["n"] += 1
        return count

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


class TestClickReperceptionHardening:
    @pytest.mark.asyncio
    async def test_ok_hollow_empty_packet_falls_through_to_settle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICK_SETTLE_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(
            tools_module.mcp_hooks,
            "_composition_get_structured_evidence",
            _sequenced_evidence([None, _standalone_controls_payload()]),
        )
        monkeypatch.setattr(tools_module.mcp_hooks, "_selector_live_match_count", _live_match(1))
        ctx = _np_ctx(server=_server_returning(_ungroundable_payload()))
        ctx.pending_scout_click_selector = 'button[data-action="selectAddress"]'

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "hollow"
        data = result["data"]
        selectors = {target.get("selector") for target in data["actionable_targets"]}
        assert "#biz-tile" in selectors
        assert data["next_action"] == "click"
        assert ctx.consecutive_no_progress_interaction_count == 1

    @pytest.mark.asyncio
    async def test_attach_exception_is_swallowed_and_increment_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("attach blew up")

        monkeypatch.setattr(tools_module.mcp_hooks, "_attach_reperception_targets_on_non_advancing_click", boom)
        ctx = _np_ctx(server=_server_returning(_ungroundable_payload()))

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert ctx.consecutive_no_progress_interaction_count == 1
        assert "actionable_targets" not in result["data"]

    @pytest.mark.asyncio
    async def test_channel_off_attaches_nothing_and_counter_climbs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICK_REPERCEPTION_ATTACH_ENABLED", False)
        ctx = _np_ctx(server=_server_returning(_standalone_controls_payload()))

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert "actionable_targets" not in result["data"]
        assert ctx.consecutive_no_progress_interaction_count == 1

    @pytest.mark.asyncio
    async def test_channel_off_leaves_bounded_steer_intact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICK_REPERCEPTION_ATTACH_ENABLED", False)
        ctx = _np_ctx(server=_server_returning(_bounded_extractor_payload()), counter=2)

        await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "attached"
        assert ctx.consecutive_no_progress_interaction_count == 0


class TestCredentialInventoryCarry:
    @staticmethod
    def _credential_carry(available_fields: list[str] | None) -> dict[str, Any]:
        return FillCarry(
            source_url=_LANDING_URL,
            selector="#password",
            tool_name="fill_credential_field",
            credential_id="cred_123",
            credential_field="username",
            available_fields=available_fields,
        ).model_dump()

    @pytest.mark.asyncio
    async def test_rebind_rehydrates_inventory_alongside_carried_fills(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx()
        ctx.scouted_credential_field_inventory_by_credential_id = {}
        ctx.prior_fill_carry = [self._credential_carry(["password", "username"])]
        evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"selector": "#password", "label": "Password"}]}],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=evidence, url=_LANDING_URL)

        assert ctx.scouted_credential_field_inventory_by_credential_id == {
            "cred_123": frozenset({"username", "password"})
        }
        assert ctx.scout_trajectory[-1]["credential_id"] == "cred_123"

    @pytest.mark.asyncio
    async def test_page_mismatch_drops_carry_but_keeps_inventory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx()
        ctx.scouted_credential_field_inventory_by_credential_id = {}
        ctx.prior_fill_carry = [self._credential_carry(["password", "username"])]
        mismatched = {"current_url": "https://example.com/elsewhere", "forms": [{"fields": []}]}

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=mismatched, url="https://example.com/elsewhere")

        assert ctx.scout_trajectory == []
        assert ctx.scouted_credential_field_inventory_by_credential_id == {
            "cred_123": frozenset({"username", "password"})
        }

    @pytest.mark.asyncio
    async def test_legacy_carry_without_available_fields_rehydrates_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx()
        ctx.scouted_credential_field_inventory_by_credential_id = {}
        ctx.prior_fill_carry = [self._credential_carry(None)]
        evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"selector": "#password", "label": "Password"}]}],
        }

        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=evidence, url=_LANDING_URL)

        assert ctx.scouted_credential_field_inventory_by_credential_id == {}
        assert ctx.scout_trajectory[-1]["credential_id"] == "cred_123"

    @pytest.mark.asyncio
    async def test_inventory_round_trips_through_structured_context_and_agent_hydration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first_turn = SimpleNamespace(
            prior_discovery_calls_made=0,
            discovery_calls_this_turn=0,
            prior_page_inspection_calls_made=0,
            page_inspection_calls_this_turn=0,
            flow_evidence=[],
            latest_evaluate_result_composition_steer=None,
            scout_trajectory=[
                {
                    "tool_name": "fill_credential_field",
                    "selector": "#password",
                    "source_url": _LANDING_URL,
                    "typed_length": 10,
                    "credential_id": "cred_123",
                    "credential_field": "username",
                }
            ],
            scouted_credential_field_inventory_by_credential_id={"cred_123": frozenset({"username", "password"})},
        )
        raw = finalize_discovery_counter_in_global_llm_context(first_turn, None)
        assert raw is not None

        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        next_turn = _ctx()
        next_turn.scouted_credential_field_inventory_by_credential_id = {}
        next_turn.prior_fill_carry = [carry.model_dump() for carry in StructuredContext.from_json_str(raw).fill_carry]
        evidence = {
            "current_url": _LANDING_URL,
            "forms": [{"fields": [{"selector": "#password", "label": "Password"}]}],
        }

        await _maybe_rebind_prior_fill_carry(next_turn, page_evidence=evidence, url=_LANDING_URL)

        assert next_turn.scouted_credential_field_inventory_by_credential_id == {
            "cred_123": frozenset({"username", "password"})
        }
        assert next_turn.scout_trajectory[-1]["credential_field"] == "username"


class TestScoutPageObservationSignal:
    def test_password_control_detected_in_forms(self) -> None:
        evidence = {
            "forms": [{"fields": [{"selector": "#user", "type": "text"}, {"selector": "#pass", "type": "password"}]}]
        }
        assert scouting_module._page_evidence_has_password_control(evidence) is True

    def test_no_password_control_in_forms(self) -> None:
        evidence = {"forms": [{"fields": [{"selector": "#user", "type": "text"}]}]}
        assert scouting_module._page_evidence_has_password_control(evidence) is False
        assert scouting_module._page_evidence_has_password_control({}) is False

    def test_record_scout_page_observation_captures_stable_index_and_signal(self) -> None:
        ctx = _ctx()
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": "#go", "source_url": _LANDING_URL, "trajectory_index": 4},
            "scout-note",
        ]
        ctx.last_scout_observation_trajectory_index = None
        ctx.last_scout_observation_has_password_control = False

        scouting_module._record_scout_page_observation(
            ctx, {"forms": [{"fields": [{"selector": "#pass", "type": "password"}]}]}
        )

        assert ctx.last_scout_observation_trajectory_index == 4
        assert ctx.last_scout_observation_has_password_control is True

        scouting_module._record_scout_page_observation(ctx, {"forms": [{"fields": [{"selector": "#name"}]}]})

        assert ctx.last_scout_observation_has_password_control is False

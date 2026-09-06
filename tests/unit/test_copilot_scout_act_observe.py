"""Act-and-observe scouting (SKY-10932): a navigating scout click runs the bounded
page-side extractor synchronously and merges the schema into the same
scout_interaction flow-evidence packet before append, degrading to today's
schema-less packet on timeout/error/hollow parses."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.challenge_evidence import (
    ChallengeEvidenceSource,
    composition_challenge_carrier,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import _locator_expr
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_STRUCTURED_EVIDENCE_MAX_CHARS,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    _auto_credit_interaction_observation,
    has_bounded_page_schema,
    has_witnessed_value_content,
    parse_composition_structured,
)
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import (
    StructuredContext,
    finalize_observation_context,
)
from skyvern.forge.sdk.copilot.enforcement import (
    record_scouted_output_coverage,
)
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.output_extraction_plan import ShapeExpectation, ValueCardinality, ValueShape
from skyvern.forge.sdk.copilot.output_utils import MCP_RESULT_PROVENANCE_KEY, MCP_RESULT_PROVENANCE_VALUE
from skyvern.forge.sdk.copilot.page_identity import safe_page_origin
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
from skyvern.forge.sdk.copilot.result_evidence import scout_observation_bound_paths
from skyvern.forge.sdk.copilot.runtime import AgentContext, bound_call_browser_session
from skyvern.forge.sdk.copilot.tools import _click_post_hook
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.copilot.tools.scouting import (
    _SCOUT_RESULT_CHAR_CAP,
    _consume_pending_browser_interaction_observation,
    _latest_same_page_evidence,
    _observed_control_readiness,
    _page_evidence_is_unchanged,
    _page_evidence_location_fingerprint,
    _page_evidence_matches_url_identity,
    _register_scout_interaction_observation,
    _safe_page_evidence_url,
    _scout_act_observe_page_evidence,
)
from tests.unit.copilot_test_helpers import carried_interaction, make_copilot_ctx
from tests.unit.scoped_asyncio import ScopedAsyncio

_SOURCE_URL = "https://example.com/product"
_LANDING_URL = "https://example.com/results"


@pytest.fixture(autouse=True)
def _page_evidence_fingerprint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", "test-page-evidence-key")


_SCHEMA_LESS_PACKET_KEYS = {
    "inspected_url",
    "current_url",
    "source_tool",
    "interaction_tool",
    "interaction_selector",
    "interaction_source_url",
}


def _bounded_extractor_payload() -> dict[str, Any]:
    def element(selector: str, **facts: Any) -> dict[str, Any]:
        return {
            **facts,
            "selector": selector,
            "selector_candidates": [{"selector": selector, "source": "test_fixture", "match_count": 1}],
            "identity": {"tag": facts.get("tag", "div"), "role": "", "label_context": facts.get("text", "")},
        }

    return {
        "page_title": "Results",
        "forms": [
            {
                "fields": [
                    element("#npi", name="npi", label="NPI number", type="text", tag="input"),
                    element("#state", name="state", label="State", type="select", tag="select"),
                ],
                "submit_controls": [element("#go", text="Search", type="submit", tag="button")],
            }
        ],
        "navigation_targets": [element("a.details", text="Provider details", href=f"{_LANDING_URL}/details", tag="a")],
        "result_containers": [element("#results", tag="table", id="results")],
        "modal_overlays": [
            {
                "role": "dialog",
                "selector": ".cookie-banner",
                "dismiss_controls": [element(".cookie-accept", tag="button", text="Accept")],
            }
        ],
    }


def _bounded_challenge_signalled_payload(challenge_controls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Login page whose schema is already bounded while the challenge widget is only
    signalled by a keyword; ``challenge_controls`` supplies the rendered carrier."""
    payload = _bounded_extractor_payload()
    payload["anti_bot_indicators"] = ["captcha"]
    payload["challenge_controls"] = challenge_controls or []
    return payload


def _rendered_iframe_challenge_control() -> dict[str, Any]:
    return {"tag": "iframe", "selector": 'iframe[title="reCAPTCHA"]', "text": ""}


def _kv_only_extractor_payload() -> dict[str, Any]:
    return {
        "page_title": "Provider Record",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "key_value_relations": [
            {
                "key_text": "Ref Code",
                "value_text": "AB-2931",
                "container_selector": ".record .kv",
                "container_match_count": 1,
                "container_position": 0,
                "value_child_index": 1,
                "direct_child_count": 2,
                "visible": True,
                "value_visible": True,
            }
        ],
    }


def _ctx(*, server: Any = None, source_url: str | None = _SOURCE_URL) -> SimpleNamespace:
    return SimpleNamespace(
        pending_browser_interaction_observation=None,
        pending_scout_input_value=None,
        pending_scout_role_name=None,
        pending_scout_click_selector=None,
        pending_scout_ambiguous=None,
        pending_scout_reanchor=None,
        pending_scout_dynamic_row=None,
        discovery_mcp_server=server,
        scouted_interactions=[],
        scout_trajectory=[],
        organization_id="o_test",
        browser_session_id="bs_test",
        last_run_blocks_workflow_run_id=None,
        prior_carried_trajectory=[],
        carried_trajectory_rebound_done=False,
        pending_scout_source_url=source_url,
        flow_evidence=[],
        completion_criteria_turn_state=None,
        last_code_authoring_repair_context=None,
        copilot_config=None,
        scout_observation_contract=None,
        scouted_output_covered_paths=set(),
        scout_observed_terminal_criterion_ids=set(),
        request_policy=None,
        org_credentials_for_turn=None,
        completion_verification_result=None,
        reached_download_target=None,
        pending_scout_popup=None,
        pending_scout_popup_content_type=None,
        pre_run_gated_output_warning_fingerprint=(),
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


async def _failed_click_through_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resulting_url: str,
    page_payload: dict[str, Any] | None,
    source_url: str = _SOURCE_URL,
    browser_title: str = "Current options",
    selector: str = "#continue",
    accessible_name: str = "Continue",
    error_message: str = "element not interactable",
    error_hint: str = "Target remained covered",
    selector_candidate_count: int = 1,
    registered_secrets: list[str] | None = None,
    codeblock_redaction_parameters: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any, AgentContext]:
    async def call_internal_tool(_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if "REQUESTED_TARGETS" in str(arguments.get("expression", "")):
            if page_payload is None:
                return {"ok": False, "error": "page evidence unavailable"}
            return {"ok": True, "data": {"result": page_payload}}
        return {
            "ok": True,
            "data": {
                "result": {
                    "role_name": {"role": "button", "accessible_name": accessible_name},
                    "role_name_match_count": 1,
                    "selector_match_count": 1,
                    "selector_candidates": [
                        {
                            "selector": selector
                            if selector_candidate_count == 1
                            else f"{selector}-{index}-{'x' * 200}",
                            "source": "id",
                            "match_count": 1,
                        }
                        for index in range(selector_candidate_count)
                    ],
                }
            },
        }

    ctx = make_copilot_ctx()
    ctx.secret_scrub_values = registered_secrets or []
    ctx.codeblock_redaction_parameters = codeblock_redaction_parameters or {}
    ctx.discovery_mcp_server = SimpleNamespace(call_internal_tool=AsyncMock(side_effect=call_internal_tool))
    monkeypatch.setattr(scouting_module, "_live_working_page_url", AsyncMock(return_value=source_url))

    raw_result = SimpleNamespace(
        structured_content={
            "ok": False,
            "error": {
                "code": "ELEMENT_NOT_INTERACTABLE",
                "message": error_message,
                "hint": error_hint,
            },
            "browser_context": {"url": resulting_url, "title": browser_title},
        },
        is_error=True,
        content=[],
    )
    server = SkyvernOverlayMCPServer(
        transport=MagicMock(),
        overlays={"click": SchemaOverlay(pre_hook=tools_module._click_pre_hook, post_hook=_click_post_hook)},
        alias_map={},
        allowlist=frozenset(),
        context_provider=lambda: ctx,
    )
    server._client = SimpleNamespace(call_tool=AsyncMock(return_value=raw_result))

    wrapped = await server.call_tool("click", {"selector": selector})
    return json.loads(wrapped.content[0].text), wrapped, ctx


def _failed_page_payload() -> dict[str, Any]:
    return {
        "page_title": "Current options",
        "forms": [
            {
                "fields": [],
                "submit_controls": [
                    {
                        "text": "Try another option",
                        "type": "button",
                        "selector": "#try-another",
                        "selector_candidates": [{"selector": "#try-another", "source": "id", "match_count": 1}],
                    }
                ],
            }
        ],
        "navigation_targets": [],
        "result_containers": [],
    }


@pytest.mark.parametrize(
    "resulting_url",
    [
        _SOURCE_URL,
        "https://example.net/wrong-target?token=not-model-visible",
    ],
)
@pytest.mark.asyncio
async def test_failed_click_wrapper_carries_attempt_and_current_page_without_success_credit(
    monkeypatch: pytest.MonkeyPatch,
    resulting_url: str,
) -> None:
    projected, wrapped, ctx = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=resulting_url,
        page_payload=_failed_page_payload(),
    )

    assert wrapped.isError is True
    assert projected["ok"] is False
    assert projected["error"] == "element not interactable. Target remained covered"
    assert projected["error_code"] == "ELEMENT_NOT_INTERACTABLE"
    assert projected[MCP_RESULT_PROVENANCE_KEY] == MCP_RESULT_PROVENANCE_VALUE
    assert projected["data"]["attempted_control"] == {
        "selector": "#continue",
        "effective_target": "button Continue",
        "selector_candidates": [{"selector": "#continue", "source": "id", "match_count": 1}],
        "selector_match_count": 1,
        "role": "button",
        "accessible_name": "Continue",
        "role_name_match_count": 1,
    }
    assert projected["data"]["url"] == safe_page_origin(resulting_url)
    assert projected["data"]["current_url_location_fingerprint"] == _page_evidence_location_fingerprint(resulting_url)
    assert "title" not in projected["data"]
    assert projected["data"]["page"]["forms"][0]["submit_controls"][0]["text"] == "Try another option"
    assert "not-model-visible" not in json.dumps(projected)
    assert len(json.dumps(projected)) <= _SCOUT_RESULT_CHAR_CAP
    assert ctx.scouted_interactions == []
    assert ctx.scout_trajectory == []
    assert ctx.flow_evidence == []


@pytest.mark.asyncio
async def test_failed_click_page_evidence_ablation_preserves_attempt_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_page, _, _ = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=_SOURCE_URL,
        page_payload=_failed_page_payload(),
    )
    without_page, _, _ = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=_SOURCE_URL,
        page_payload=None,
    )

    assert "page" not in without_page["data"]
    assert with_page["data"].pop("page")
    assert with_page == without_page


@pytest.mark.asyncio
async def test_failed_click_without_page_evidence_sheds_oversized_candidates_but_keeps_failure_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projected, wrapped, _ = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=_SOURCE_URL,
        page_payload=None,
        selector_candidate_count=20,
    )

    assert wrapped.isError is True
    assert projected["ok"] is False
    assert projected["error"] == "element not interactable. Target remained covered"
    assert projected["error_code"] == "ELEMENT_NOT_INTERACTABLE"
    assert projected["data"]["attempted_control"]["selector"] == "#continue"
    assert projected["data"]["attempted_control"]["effective_target"] == "button Continue"
    assert "selector_candidates" not in projected["data"]["attempted_control"]
    assert len(json.dumps(projected)) <= _SCOUT_RESULT_CHAR_CAP


@pytest.mark.asyncio
async def test_failed_click_without_page_evidence_preserves_typed_failure_while_bounding_control_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "#oversized-required-control-" + "s" * (_SCOUT_RESULT_CHAR_CAP * 2)
    accessible_name = "Oversized required control " + "n" * (_SCOUT_RESULT_CHAR_CAP * 2)
    error_message = "oversized typed click failure " + "e" * 500
    projected, wrapped, _ = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=_SOURCE_URL,
        page_payload=None,
        selector=selector,
        accessible_name=accessible_name,
        error_message=error_message,
        error_hint="",
    )

    assert wrapped.isError is True
    assert projected["ok"] is False
    assert projected["error"] == error_message
    assert projected["error_code"] == "ELEMENT_NOT_INTERACTABLE"
    assert "#oversized-required-control-" in projected["data"]["attempted_control"]["selector"]
    assert "Oversized required control" in projected["data"]["attempted_control"]["effective_target"]
    assert len(json.dumps(projected)) <= _SCOUT_RESULT_CHAR_CAP


@pytest.mark.asyncio
async def test_failed_click_wrapper_reuses_registered_and_codeblock_redaction_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_secret = "registered-password-5ac091"
    parameter_secret = "codeblock-parameter-26bc18"
    location_value = "location-only-value-7fe2b7"
    page_payload = _failed_page_payload()
    page_payload["page_title"] = f"Account for {credential_secret}"
    page_payload["forms"][0]["submit_controls"][0]["text"] = f"Continue with {credential_secret} and {parameter_secret}"

    def redact_codeblock_parameters(value: Any, parameters: dict[str, Any]) -> Any:
        assert parameters == {"account": parameter_secret}
        return value.replace(parameter_secret, "[REDACTED_PARAMETER]") if isinstance(value, str) else value

    codeblock_scrubber = MagicMock(side_effect=redact_codeblock_parameters)
    monkeypatch.setattr(tools_module.app.AGENT_FUNCTION, "redact_codeblock_parameter_values", codeblock_scrubber)

    projected, wrapped, ctx = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=f"https://example.com/checkout?state={location_value}",
        page_payload=page_payload,
        registered_secrets=[credential_secret],
        codeblock_redaction_parameters={"account": parameter_secret},
    )

    assert wrapped.isError is True
    assert projected["ok"] is False
    assert projected["error_code"] == "ELEMENT_NOT_INTERACTABLE"
    assert projected["data"]["attempted_control"]["selector"] == "#continue"
    serialized = json.dumps(projected)
    assert projected["data"]["url"] == "https://example.com/"
    assert projected["data"]["current_url_location_fingerprint"] == _page_evidence_location_fingerprint(
        f"https://example.com/checkout?state={location_value}"
    )
    assert credential_secret not in serialized
    assert parameter_secret not in serialized
    assert location_value not in serialized
    assert projected["data"]["page"]["page_title"] == "Account for [REDACTED_SECRET]"
    assert (
        projected["data"]["page"]["forms"][0]["submit_controls"][0]["text"]
        == "Continue with [REDACTED_SECRET] and [REDACTED_PARAMETER]"
    )
    assert codeblock_scrubber.called
    assert len(serialized) <= _SCOUT_RESULT_CHAR_CAP
    assert ctx.scouted_interactions == []
    assert ctx.scout_trajectory == []
    assert ctx.flow_evidence == []


@pytest.mark.asyncio
async def test_failed_click_preserves_ordinary_page_facts_matching_short_url_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resulting_url = "https://example.net/go?state=on#tab"
    page_payload = _failed_page_payload()
    page_payload["page_title"] = "Go on"
    page_payload["forms"][0]["submit_controls"][0]["text"] = "Open tab"

    projected, wrapped, ctx = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=resulting_url,
        page_payload=page_payload,
    )

    assert wrapped.isError is True
    assert projected["ok"] is False
    assert projected["error_code"] == "ELEMENT_NOT_INTERACTABLE"
    assert projected["data"]["attempted_control"]["selector"] == "#continue"
    assert projected["data"]["url"] == "https://example.net/"
    assert projected["data"]["current_url_location_fingerprint"] == _page_evidence_location_fingerprint(resulting_url)
    serialized = json.dumps(projected)
    assert projected["data"]["page"]["page_title"] == "Go on"
    assert projected["data"]["page"]["forms"][0]["submit_controls"][0]["text"] == "Open tab"
    assert len(serialized) <= _SCOUT_RESULT_CHAR_CAP
    assert ctx.scouted_interactions == []
    assert ctx.scout_trajectory == []
    assert ctx.flow_evidence == []


@pytest.mark.asyncio
async def test_failed_click_cap_is_enforced_after_registered_short_secret_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered_secret = "654321"
    reflected_secret = " ".join([registered_secret] * 20)
    page_payload = _failed_page_payload()
    page_payload["forms"] = [
        {
            "fields": [],
            "submit_controls": [
                {"text": reflected_secret, "type": "button", "selector": f"#choice-{form_index}-{index}"}
                for index in range(4)
            ],
        }
        for form_index in range(4)
    ]

    projected, wrapped, ctx = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=_SOURCE_URL,
        page_payload=page_payload,
        registered_secrets=[registered_secret],
    )

    serialized = json.dumps(projected)
    assert wrapped.isError is True
    assert projected["ok"] is False
    assert projected["error_code"] == "ELEMENT_NOT_INTERACTABLE"
    assert projected["data"]["attempted_control"]["selector"] == "#continue"
    assert projected["data"]["attempted_control"]["effective_target"] == "button Continue"
    assert projected["data"]["url"] == safe_page_origin(_SOURCE_URL)
    assert projected["data"]["current_url_location_fingerprint"] == _page_evidence_location_fingerprint(_SOURCE_URL)
    assert registered_secret not in serialized
    assert len(serialized) <= _SCOUT_RESULT_CHAR_CAP
    assert ctx.scouted_interactions == []
    assert ctx.scout_trajectory == []
    assert ctx.flow_evidence == []


@pytest.mark.asyncio
async def test_failed_click_resulting_location_ablation_changes_only_safe_location_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same_page, _, _ = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url=_SOURCE_URL,
        page_payload=_failed_page_payload(),
    )
    redirected, _, _ = await _failed_click_through_wrapper(
        monkeypatch,
        resulting_url="https://example.net/wrong-target?token=second",
        page_payload=_failed_page_payload(),
    )

    assert same_page["data"]["url"] != redirected["data"]["url"]
    assert (
        same_page["data"]["current_url_location_fingerprint"] != redirected["data"]["current_url_location_fingerprint"]
    )
    assert same_page["data"]["page"]["page_title"] == "Current options"
    assert redirected["data"]["page"]["page_title"] == "Current options"
    same_page["data"]["url"] = redirected["data"]["url"]
    same_page["data"]["current_url_location_fingerprint"] = redirected["data"]["current_url_location_fingerprint"]
    assert same_page == redirected


def test_safe_page_evidence_url_keeps_only_origin_and_fingerprints_location() -> None:
    assert (
        _safe_page_evidence_url("https://user:password@example.com/callback?code=secret#access_token=secret")
        == "https://example.com/"
    )

    first = _page_evidence_location_fingerprint("https://example.com/search?q=first")
    second = _page_evidence_location_fingerprint("https://example.com/search?q=second")
    assert first is not None and first != "unkeyed"
    assert second is not None and first != second
    secret_path = "https://example.com/magic-link/29f4ed70-8c9a-4db6-b68d-f53a87bd2147"
    safe_secret_path = _safe_page_evidence_url(secret_path)
    assert safe_secret_path == "https://example.com/"
    assert "29f4ed70-8c9a-4db6-b68d-f53a87bd2147" not in safe_secret_path
    assert _page_evidence_location_fingerprint(secret_path) != _page_evidence_location_fingerprint(
        "https://example.com/magic-link/a3e8be68-9304-42f0-bc81-42afca936dd6"
    )
    assert _page_evidence_location_fingerprint("https://example.com/#/home") != _page_evidence_location_fingerprint(
        "https://example.com/#/admin"
    )


def test_latest_page_evidence_uses_secret_safe_location_identity() -> None:
    first_url = "https://example.com/search?q=first"
    evidence = parse_composition_structured(
        _bounded_extractor_payload(),
        inspected_url="https://example.com/search",
        current_url="https://example.com/search",
    )
    assert evidence is not None
    evidence["current_url_location_fingerprint"] = _page_evidence_location_fingerprint(first_url)
    ctx = _ctx()
    ctx.flow_evidence = [{"step": 0, "reached_via": "current_page", "had_bounded_schema": True, "evidence": evidence}]

    assert _latest_same_page_evidence(ctx, url=first_url) is evidence
    assert _latest_same_page_evidence(ctx, url="https://example.com/search?q=second") is None


def test_control_readiness_uses_secret_safe_location_identity() -> None:
    page_url = "https://example.com/search?q=first"
    evidence = parse_composition_structured(
        _bounded_extractor_payload(),
        inspected_url="https://example.com/search",
        current_url="https://example.com/search",
    )
    assert evidence is not None
    evidence["current_url_location_fingerprint"] = _page_evidence_location_fingerprint(page_url)
    evidence["forms"][0]["fields"][0]["visible"] = False
    ctx = _ctx()
    ctx.flow_evidence = [{"step": 0, "reached_via": "interaction", "had_bounded_schema": True, "evidence": evidence}]

    assert _observed_control_readiness(ctx, "#npi", page_url) == (True, False)
    assert _observed_control_readiness(ctx, "#npi", "https://example.com/search?q=second") == (False, False)


def test_control_readiness_does_not_transfer_state_through_a_non_unique_candidate() -> None:
    page_url = "https://example.com/actions"
    evidence = {
        "current_url": page_url,
        "current_url_location_fingerprint": _page_evidence_location_fingerprint(page_url),
        "forms": [
            {
                "fields": [
                    {
                        "selector": "#enabled",
                        "selector_candidates": [{"selector": ".primary", "source": "class", "match_count": 2}],
                        "visible": True,
                        "disabled": False,
                    },
                    {
                        "selector": "#disabled",
                        "selector_candidates": [{"selector": ".primary", "source": "class", "match_count": 2}],
                        "visible": True,
                        "disabled": True,
                    },
                ]
            }
        ],
    }
    ctx = _ctx()
    ctx.composition_page_evidence = evidence

    assert _observed_control_readiness(ctx, ".primary", page_url) == (False, False)


def test_page_identity_uses_process_key_when_secret_key_is_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", type(settings).model_fields["SECRET_KEY"].default)
    page_url = "https://example.com/search?q=first"
    fingerprint = _page_evidence_location_fingerprint(page_url)
    evidence = {
        "current_url": "https://example.com/",
        "current_url_location_fingerprint": fingerprint,
    }

    assert fingerprint is not None and fingerprint != "unkeyed"
    assert _page_evidence_matches_url_identity(evidence, page_url) is True
    assert _page_evidence_matches_url_identity(evidence, "https://example.com/search?q=second") is False


def test_page_evidence_freshness_ignores_visual_challenge_provenance() -> None:
    prior = parse_composition_structured(
        _bounded_challenge_signalled_payload([_rendered_iframe_challenge_control()]),
        inspected_url=_SOURCE_URL,
        current_url=_SOURCE_URL,
    )
    current = parse_composition_structured(
        _bounded_challenge_signalled_payload([_rendered_iframe_challenge_control()]),
        inspected_url=_LANDING_URL,
        current_url=_LANDING_URL,
    )
    assert prior is not None and current is not None
    prior["challenge_state"] = {
        **prior["challenge_state"],
        "source": "dom+screenshot",
        "evidence_source": "vision",
        "visual_location": "centered overlay",
    }

    assert _page_evidence_is_unchanged(prior, current) is True


def test_page_evidence_freshness_compares_dom_state_not_visual_augmentation() -> None:
    prior = parse_composition_structured(
        _bounded_extractor_payload(), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL
    )
    current = parse_composition_structured(
        _bounded_extractor_payload(), inspected_url=_LANDING_URL, current_url=_LANDING_URL
    )
    assert prior is not None and current is not None
    prior["challenge_state"] = {
        **prior["challenge_state"],
        "detected": True,
        "requires_human_verification": True,
        "kind": "visual_challenge",
        "evidence_source": "vision",
    }
    prior["page_obstructions"] = [
        *prior.get("page_obstructions", []),
        {"kind": "loading_overlay", "source": "vision"},
    ]
    prior["observed_empty_page"] = True
    prior["empty_page_visual_state"] = "settled_empty"

    assert _page_evidence_is_unchanged(prior, current) is True


def test_page_evidence_freshness_includes_dom_page_obstructions() -> None:
    prior = parse_composition_structured(
        _bounded_extractor_payload(), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL
    )
    current = parse_composition_structured(
        _bounded_extractor_payload(), inspected_url=_LANDING_URL, current_url=_LANDING_URL
    )
    assert prior is not None and current is not None
    current["page_obstructions"] = [
        {
            "kind": "interaction_blocking_layer",
            "source": "dom_html",
            "selector": "#veil",
            "visible_controls": [{"selector": "#continue", "text": "Continue"}],
            "visible_controls_omitted": 0,
        }
    ]

    assert _page_evidence_is_unchanged(prior, current) is False


@pytest.mark.parametrize("candidate_on_prior", [False, True])
def test_page_evidence_freshness_includes_dom_obstruction_candidates(candidate_on_prior: bool) -> None:
    prior = parse_composition_structured(
        _bounded_extractor_payload(), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL
    )
    current = parse_composition_structured(
        _bounded_extractor_payload(), inspected_url=_LANDING_URL, current_url=_LANDING_URL
    )
    assert prior is not None and current is not None
    candidate = {
        "selector": ".loading-overlay",
        "position": "fixed",
        "z_index": 1000,
        "viewport_coverage": 0.95,
    }
    (prior if candidate_on_prior else current)["visual_obstruction_candidates"] = [candidate]

    assert _page_evidence_is_unchanged(prior, current) is False


def test_page_evidence_freshness_includes_reveal_truncation_state() -> None:
    prior = parse_composition_structured(
        _bounded_extractor_payload(), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL
    )
    current = parse_composition_structured(
        _bounded_extractor_payload(), inspected_url=_LANDING_URL, current_url=_LANDING_URL
    )
    assert prior is not None and current is not None
    current["inspection_warnings"] = ["reveal_relations_truncated"]

    assert _page_evidence_is_unchanged(prior, current) is False


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
        assert evidence["current_url_location_fingerprint"] == _page_evidence_location_fingerprint(_LANDING_URL)
        assert has_bounded_page_schema(evidence)
        assert evidence["forms"][0]["fields"][0]["label"] == "NPI number"

    @pytest.mark.asyncio
    async def test_click_issues_one_post_action_packet_and_no_recapture(self) -> None:
        ctx = _ctx(server=_server_returning(_bounded_extractor_payload()))

        await _run_click(ctx)

        assert ctx.discovery_mcp_server.call_internal_tool.await_count == 1
        assert ctx.last_scout_act_observe_recapture_attempted is False

    @pytest.mark.asyncio
    async def test_pending_marker_cleared_and_result_carries_summary(self) -> None:
        ctx = _ctx(server=_server_returning(_bounded_extractor_payload()))

        result = await _run_click(ctx)

        assert ctx.pending_browser_interaction_observation is None
        assert result["observation_step"] == ctx.flow_evidence[0]["step"]
        assert result["data"]["observation_step"] == ctx.flow_evidence[0]["step"]
        page = result["data"]["page"]
        assert page["page_title"] == "Results"
        assert page["forms"][0]["field_count"] == 2
        assert [field["text"] for field in page["forms"][0]["fields"]] == ["NPI number", "State"]
        assert [field["selector_candidates"][0]["selector"] for field in page["forms"][0]["fields"]] == [
            "#npi",
            "#state",
        ]
        assert page["forms"][0]["submit_controls"][0]["selector_candidates"][0]["selector"] == "#go"
        assert page["navigation_target_count"] == 1
        assert [target["text"] for target in page["navigation_targets"]] == ["Provider details"]
        assert page["result_container_count"] == 1
        assert page["challenge_detected"] is False
        assert [control["text"] for control in page["modal_dismiss_controls"]] == ["Accept"]
        assert len(json.dumps(result)) <= _SCOUT_RESULT_CHAR_CAP

    @pytest.mark.asyncio
    async def test_result_summary_carries_structured_size_omissions_to_the_authoring_model(self) -> None:
        payload = _bounded_extractor_payload()
        payload["size_compaction"] = {
            "original_char_count": 132_400,
            "omissions": [
                {"category": "forms.fields.options", "omitted_count": 180, "unit": "entries"},
                {"category": "visible_text_excerpt", "omitted_count": 6000, "unit": "characters"},
            ],
        }
        ctx = _ctx(server=_server_returning(payload))

        result = await _run_click(ctx)

        assert result["data"]["page"]["size_compaction"] == payload["size_compaction"]
        assert ctx.flow_evidence[0]["evidence"]["inspection_warnings"] == []

    @pytest.mark.asyncio
    async def test_result_summary_carries_disclosure_controls_to_the_authoring_model(self) -> None:
        payload = _bounded_extractor_payload()
        payload["clickable_controls"] = [
            {
                "text": "More options",
                "selector": "#more",
                "selector_candidates": [{"selector": "#more", "source": "id", "match_count": 1}],
                "tag": "button",
                "expanded": False,
                "controls": "alternatives",
                "controlled_region_visible": False,
                "disabled": True,
                "visible": False,
            }
        ]
        ctx = _ctx(server=_server_returning(payload))

        result = await _run_click(ctx)

        disclosure = result["data"]["page"]["disclosure_controls"][0]
        assert "selector" not in disclosure
        assert disclosure["selector_candidates"][0]["selector"] == "#more"
        assert disclosure["disabled"] is True
        assert disclosure["visible"] is False

    @pytest.mark.asyncio
    async def test_result_summary_carries_in_form_disclosure_controls_to_the_authoring_model(self) -> None:
        payload = _bounded_extractor_payload()
        payload["forms"][0]["submit_controls"].append(
            {
                "text": "More options",
                "selector": "#more",
                "selector_candidates": [{"selector": "#more", "source": "id", "match_count": 1}],
                "tag": "button",
                "expanded": False,
                "controls": "alternatives",
                "controlled_region_visible": False,
            }
        )
        ctx = _ctx(server=_server_returning(payload))

        result = await _run_click(ctx)

        disclosure = result["data"]["page"]["disclosure_controls"][0]
        assert "selector" not in disclosure
        assert disclosure["selector_candidates"][0]["selector"] == "#more"

    @pytest.mark.asyncio
    async def test_result_summary_does_not_widen_to_unrelated_clickable_controls(self) -> None:
        payload = _bounded_extractor_payload()
        payload["clickable_controls"] = [{"text": "Refresh", "selector": "#refresh", "tag": "button"}]
        ctx = _ctx(server=_server_returning(payload))

        result = await _run_click(ctx)

        assert result["data"]["page"]["disclosure_controls"] == []

    @pytest.mark.asyncio
    async def test_result_summary_omits_in_form_disclosures_when_channel_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COPILOT_CLICKABLE_CONTROLS_EVIDENCE_ENABLED", False)
        payload = _bounded_extractor_payload()
        payload["forms"][0]["submit_controls"].append(
            {
                "text": "More options",
                "selector": "#more",
                "expanded": False,
                "controls": "alternatives",
                "controlled_region_visible": False,
            }
        )
        ctx = _ctx(server=_server_returning(payload))

        result = await _run_click(ctx)

        assert result["data"]["page"]["disclosure_controls"] == []

    @pytest.mark.asyncio
    async def test_browser_disclosure_reaches_the_authoring_model_through_the_production_pipeline(self) -> None:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except PlaywrightError:
                pytest.skip("Requires Playwright Chromium (run: playwright install chromium)")
            page = await browser.new_page()
            await page.set_content(
                """
                <html><head><title>Two-factor authentication</title>
                  <style>#alternatives { display: none; }</style>
                </head><body>
                  <form><label for="otp">One-time code</label><input id="otp" name="otp"></form>
                  <button id="more" aria-expanded="false" aria-controls="alternatives">More options</button>
                  <button id="refresh">Refresh</button>
                  <div id="alternatives"><button>Authenticator app</button></div>
                </body></html>
                """
            )

            async def evaluate_live_dom(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                assert tool_name == "skyvern_evaluate"
                return {"ok": True, "data": {"result": await page.evaluate(arguments["expression"])}}

            ctx = _ctx(server=SimpleNamespace(call_internal_tool=evaluate_live_dom))
            try:
                result = await _run_click(ctx)
            finally:
                await browser.close()

        assert "page" in result["data"]
        disclosure = result["data"]["page"]["disclosure_controls"][0]
        assert "selector" not in disclosure
        assert disclosure["text"] == "More options"
        assert disclosure["expanded"] is False
        assert disclosure["controls"] == "alternatives"
        assert disclosure["controlled_region_visible"] is False
        assert any(candidate["selector"] == "#more" for candidate in disclosure["selector_candidates"])
        parsed_controls = ctx.flow_evidence[0]["evidence"]["clickable_controls"]
        parsed_disclosure = next(
            control
            for control in parsed_controls
            if any(candidate["selector"] == "#more" for candidate in control["selector_candidates"])
        )
        assert parsed_disclosure["expanded"] is False
        assert parsed_disclosure["controls"] == "alternatives"
        assert parsed_disclosure["controlled_region_visible"] is False
        assert any(
            candidate["selector"] == "#refresh"
            for control in parsed_controls
            for candidate in control["selector_candidates"]
        )

    def test_captured_code_host_disclosure_replays_to_the_authoring_model_without_a_browser(self) -> None:
        capture_path = Path(__file__).parent / "fixtures/copilot/sky_14419_code_host_collapsed_2fa_structured.json"
        capture = json.loads(capture_path.read_text())
        contract = capture["capture_contract"]
        raw_packet = capture["raw_structured_packet"]

        repo_root = Path(__file__).parents[2]
        fixture_bytes = (repo_root / contract["fixture"]).read_bytes()
        assert hashlib.sha256(fixture_bytes).hexdigest() == contract["fixture_sha256"]
        serialized = json.dumps(raw_packet, ensure_ascii=False, separators=(",", ":"))
        assert len(serialized) <= COMPOSITION_STRUCTURED_EVIDENCE_MAX_CHARS

        raw_controls = raw_packet["clickable_controls"]
        raw_disclosure = next(control for control in raw_controls if control["text"].startswith("More options"))
        assert raw_disclosure["selector"] == "button.secondary"
        assert raw_disclosure["expanded"] is False
        assert raw_disclosure["controls"] == "two-factor-alternatives-body"
        assert raw_disclosure["controlled_region_visible"] is False

        parsed = parse_composition_structured(
            raw_packet,
            inspected_url=contract["fixture_url"],
            current_url=contract["fixture_url"],
        )
        assert parsed is not None
        parsed_controls = parsed["clickable_controls"]
        parsed_disclosure = next(control for control in parsed_controls if control["text"].startswith("More options"))
        assert any(
            candidate["selector"] == raw_disclosure["selector"]
            for candidate in parsed_disclosure["selector_candidates"]
        )
        assert parsed_disclosure["expanded"] is False
        assert parsed_disclosure["controls"] == "two-factor-alternatives-body"
        assert parsed_disclosure["controlled_region_visible"] is False
        assert any(
            candidate["selector"] == "button.primary"
            for control in parsed_controls
            for candidate in control["selector_candidates"]
        )

        model_facing_result: dict[str, Any] = {"ok": True, "data": {}}
        scouting_module._attach_scout_page_summary(
            SimpleNamespace(codeblock_redaction_parameters={}), model_facing_result, parsed
        )

        disclosure = model_facing_result["data"]["page"]["disclosure_controls"][0]
        assert "selector" not in disclosure
        assert disclosure["text"] == "More options ▾"
        assert disclosure["selector_candidates"][0]["selector"] == "button.secondary"
        assert disclosure["controls"] == "two-factor-alternatives-body"
        assert all(
            control.get("selector") != "button.primary"
            for control in model_facing_result["data"]["page"]["disclosure_controls"]
        )

    @pytest.mark.asyncio
    async def test_content_witnessed_kv_reveal_admitted_without_bounded_schema(self) -> None:
        ctx = _ctx(server=_server_returning(_kv_only_extractor_payload()))

        result = await _run_click(ctx)

        assert result["ok"] is True
        assert ctx.last_scout_act_observe_outcome == "hollow"
        assert not hasattr(ctx, "latest_recorded_build_test_outcome")
        assert ctx.pending_browser_interaction_observation is None
        assert len(ctx.flow_evidence) == 1
        entry = ctx.flow_evidence[0]
        assert entry["reached_via"] == "interaction"
        assert entry["had_bounded_schema"] is False
        evidence = entry["evidence"]
        assert evidence["source_tool"] == "scout_interaction"
        assert has_bounded_page_schema(evidence) is False
        assert has_witnessed_value_content(evidence) is True


def _zero_overlap_kv_relation(key_text: str, value_text: str, selector: str) -> dict[str, Any]:
    return {
        "key_text": key_text,
        "value_text": value_text,
        "container_selector": selector,
        "container_match_count": 1,
        "container_position": 0,
        "value_child_index": 1,
        "direct_child_count": 2,
        "visible": True,
        "value_visible": True,
    }


def _zero_overlap_kv_packet() -> dict[str, Any]:
    return {
        "current_url": _LANDING_URL,
        "source_tool": "scout_interaction",
        "interaction_selector": "#reveal",
        "inspection_warnings": [],
        "result_containers_truncated": False,
        "key_value_relations_truncated": False,
        "key_value_relations": [
            _zero_overlap_kv_relation("Ref Code", "12345678", ".kv-ref"),
            _zero_overlap_kv_relation("Site", "12 Peak Way Reno NV 89501", ".kv-site"),
        ],
        "result_containers": [],
    }


_ZERO_OVERLAP_BOUND_PATHS = {"output.widget_id", "output.address"}


def _zero_overlap_requested_output_ctx() -> SimpleNamespace:
    ctx = _ctx()
    ctx.completion_criteria_turn_state = SimpleNamespace(
        decision=SimpleNamespace(
            criteria=(
                CompletionCriterion(
                    id="output.widget_id",
                    outcome="the eight digit widget reference",
                    output_path="output.widget_id",
                ),
                CompletionCriterion(
                    id="output.address",
                    outcome="the mailing address of the site",
                    output_path="output.address",
                ),
            )
        )
    )
    ctx.copilot_config = CopilotConfig(
        requested_output_shape_expectations={
            "widget_id": ShapeExpectation(ValueShape.NUMERIC_ID, ValueCardinality.SCALAR, id_digit_length=8),
            "address": ShapeExpectation(ValueShape.POSTAL_ADDRESS, ValueCardinality.SCALAR),
        }
    )
    ctx.scout_trajectory = [{"tool_name": "click", "selector": "#reveal", "trajectory_index": 0}]
    return ctx


class TestActObserveCoverageCredit:
    @pytest.mark.asyncio
    async def test_click_admit_branch_credits_value_grounded_on_witnessed_reveal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scouting_module, "_scout_act_observe_page_evidence", AsyncMock(return_value=_zero_overlap_kv_packet())
        )
        ctx = _zero_overlap_requested_output_ctx()

        with capture_logs() as logs:
            step, page_evidence = await _register_scout_interaction_observation(
                ctx, tool_name="click", selector="#reveal", source_url=_SOURCE_URL, url=_LANDING_URL
            )

        assert page_evidence is not None
        assert has_witnessed_value_content(page_evidence) is True
        assert ctx.scout_observation_contract is not None
        assert scout_observation_bound_paths(ctx.scout_observation_contract) == _ZERO_OVERLAP_BOUND_PATHS
        assert ctx.scouted_output_covered_paths == _ZERO_OVERLAP_BOUND_PATHS
        credited = next(entry for entry in logs if entry["event"] == "copilot_scouted_output_coverage_credited")
        assert credited["provenance"] == "value_grounded"
        assert sorted(credited["value_grounded_paths"]) == sorted(_ZERO_OVERLAP_BOUND_PATHS)

    @pytest.mark.asyncio
    async def test_second_record_for_same_paths_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            scouting_module, "_scout_act_observe_page_evidence", AsyncMock(return_value=_zero_overlap_kv_packet())
        )
        ctx = _zero_overlap_requested_output_ctx()

        await _register_scout_interaction_observation(
            ctx, tool_name="click", selector="#reveal", source_url=_SOURCE_URL, url=_LANDING_URL
        )
        assert ctx.scouted_output_covered_paths == _ZERO_OVERLAP_BOUND_PATHS

        with capture_logs() as logs:
            record_scouted_output_coverage(
                ctx, _zero_overlap_kv_packet(), contract=ctx.scout_observation_contract, include_lexical=False
            )
        assert not any(entry["event"] == "copilot_scouted_output_coverage_credited" for entry in logs)
        assert ctx.scouted_output_covered_paths == _ZERO_OVERLAP_BOUND_PATHS

    @pytest.mark.asyncio
    async def test_unwitnessed_reveal_mints_and_credits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            scouting_module,
            "_scout_act_observe_page_evidence",
            AsyncMock(return_value={"current_url": _LANDING_URL, "forms": [], "key_value_relations": []}),
        )
        ctx = _zero_overlap_requested_output_ctx()
        ctx.last_scout_act_observe_outcome = None

        with capture_logs() as logs:
            step, page_evidence = await _register_scout_interaction_observation(
                ctx, tool_name="click", selector="#reveal", source_url=_SOURCE_URL, url=_LANDING_URL
            )

        assert page_evidence is None
        assert ctx.scout_observation_contract is None
        assert ctx.scouted_output_covered_paths == set()
        assert not any(entry["event"] == "copilot_scouted_output_coverage_credited" for entry in logs)


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
        assert ctx.discovery_mcp_server.call_internal_tool.await_count == 2
        assert ctx.last_scout_act_observe_recapture_attempted is True
        assert ctx.last_scout_act_observe_recapture_result == "attached"

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
            "executed_selector": "#open-details",
            "effective_target": "#open-details",
            "url": "https://example.com/",
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


class TestActObserveRecaptureSettle:
    @pytest.mark.asyncio
    async def test_unchanged_pre_action_page_is_recaptured_before_admission(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = _bounded_extractor_payload()
        stale["page_title"] = "Two-factor authentication"
        fresh = _bounded_extractor_payload()
        fresh["page_title"] = "Project home"
        fresh["navigation_targets"] = [
            {"text": "Web analytics", "href": "/project/47954/web", "selector": 'a[href="/project/47954/web"]'}
        ]
        prior = parse_composition_structured(copy.deepcopy(stale), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL)
        assert prior is not None
        prior["screenshot_used"] = True
        prior["evidence_sources"] = ["dom", "screenshot"]
        ctx = _ctx(server=_server_returning_sequence([stale, fresh]), source_url=_SOURCE_URL)
        ctx.pending_scout_role_name = ("#open-details", "button", "Continue")
        ctx.flow_evidence = [
            {
                "step": 0,
                "reached_via": "current_page",
                "had_bounded_schema": True,
                "evidence": prior,
            }
        ]
        monkeypatch.setattr(
            scouting_module,
            "_live_working_page_url",
            AsyncMock(return_value=f"{_LANDING_URL}/home"),
        )
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(scouting_module, "asyncio", ScopedAsyncio(sleep=record_sleep))

        result = await _run_click(ctx)

        assert sleeps == [settings.COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS]
        assert ctx.last_scout_act_observe_recapture_attempted is True
        assert ctx.last_scout_act_observe_recapture_result == "attached"
        assert ctx.last_scout_act_observe_outcome == "attached"
        assert result["data"]["page"]["page_title"] == "Project home"
        assert ctx.flow_evidence[-1]["evidence"]["current_url"] == f"{_LANDING_URL}/home"
        assert ctx.flow_evidence[-1]["evidence"][
            "current_url_location_fingerprint"
        ] == _page_evidence_location_fingerprint(f"{_LANDING_URL}/home")

    @pytest.mark.asyncio
    async def test_persistently_unchanged_page_is_not_published_as_post_action_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = _bounded_extractor_payload()
        stale["page_title"] = "Two-factor authentication"
        prior = parse_composition_structured(copy.deepcopy(stale), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL)
        assert prior is not None
        prior["screenshot_used"] = True
        prior["evidence_sources"] = ["dom", "screenshot"]
        ctx = _ctx(server=_server_returning_sequence([stale, stale]), source_url=_SOURCE_URL)
        ctx.pending_scout_role_name = ("#open-details", "button", "Continue")
        ctx.flow_evidence = [
            {
                "step": 0,
                "reached_via": "current_page",
                "had_bounded_schema": True,
                "evidence": prior,
            }
        ]
        monkeypatch.setattr(
            scouting_module,
            "_live_working_page_url",
            AsyncMock(return_value=_LANDING_URL),
        )

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "unchanged"
        assert ctx.last_scout_act_observe_recapture_result == "unchanged"
        assert result["data"]["page_observation"] == {
            "status": "unchanged",
            "message": "The page observation did not change after the click; no post-click page evidence was attached.",
        }
        assert "page" not in result["data"]
        assert ctx.flow_evidence[-1]["had_bounded_schema"] is False

    @pytest.mark.asyncio
    async def test_visible_text_change_after_cross_url_click_is_fresh_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = _bounded_extractor_payload()
        stale["visible_text_excerpt"] = "Enter your verification code"
        fresh = copy.deepcopy(stale)
        fresh["visible_text_excerpt"] = "Visitors 12.1K Last 7 days"
        prior = parse_composition_structured(copy.deepcopy(stale), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL)
        assert prior is not None
        ctx = _ctx(server=_server_returning_sequence([stale, fresh]), source_url=_SOURCE_URL)
        ctx.pending_scout_role_name = ("#continue", "button", "Continue")
        ctx.flow_evidence = [
            {
                "step": 0,
                "reached_via": "current_page",
                "had_bounded_schema": True,
                "evidence": prior,
            }
        ]
        monkeypatch.setattr(scouting_module, "_live_working_page_url", AsyncMock(return_value=_LANDING_URL))

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_recapture_attempted is True
        assert ctx.last_scout_act_observe_outcome == "attached"
        assert "page" in result["data"]
        assert ctx.flow_evidence[-1]["evidence"]["visible_text_excerpt"] == "Visitors 12.1K Last 7 days"
        assert ctx.flow_evidence[-1]["evidence"]["current_url"] == _LANDING_URL
        assert ctx.flow_evidence[-1]["evidence"][
            "current_url_location_fingerprint"
        ] == _page_evidence_location_fingerprint(_LANDING_URL)

    @pytest.mark.asyncio
    async def test_hollow_recapture_never_restores_known_stale_packet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale = _bounded_extractor_payload()
        stale["page_title"] = "Two-factor authentication"
        hollow = {"page_title": "Loading", "forms": [], "visible_text_excerpt": "Loading"}
        prior = parse_composition_structured(copy.deepcopy(stale), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL)
        assert prior is not None
        ctx = _ctx(server=_server_returning_sequence([stale, hollow]), source_url=_SOURCE_URL)
        ctx.pending_scout_role_name = ("#continue", "button", "Continue")
        ctx.flow_evidence = [{"step": 0, "reached_via": "current_page", "had_bounded_schema": True, "evidence": prior}]
        monkeypatch.setattr(scouting_module, "_live_working_page_url", AsyncMock(return_value=_LANDING_URL))

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_recapture_attempted is True
        assert ctx.last_scout_act_observe_outcome == "unchanged"
        assert "page" not in result["data"]
        assert result["data"]["page_observation"]["status"] == "unchanged"
        assert ctx.last_scout_act_observe_packet is None
        assert ctx.flow_evidence[-1]["had_bounded_schema"] is False

    @pytest.mark.asyncio
    async def test_hollow_first_capture_cannot_publish_stale_recapture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale = _bounded_extractor_payload()
        stale["page_title"] = "Two-factor authentication"
        hollow = {"page_title": "Loading", "forms": [], "visible_text_excerpt": "Loading"}
        prior = parse_composition_structured(copy.deepcopy(stale), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL)
        assert prior is not None
        ctx = _ctx(server=_server_returning_sequence([hollow, stale]), source_url=_SOURCE_URL)
        ctx.flow_evidence = [{"step": 0, "reached_via": "current_page", "had_bounded_schema": True, "evidence": prior}]
        monkeypatch.setattr(scouting_module, "_live_working_page_url", AsyncMock(return_value=_LANDING_URL))

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "unchanged"
        assert ctx.last_scout_act_observe_packet is None
        assert "page" not in result["data"]
        assert result["data"]["page_observation"]["status"] == "unchanged"

    @pytest.mark.asyncio
    async def test_fresh_post_challenge_page_may_drop_stale_challenge_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = _bounded_challenge_signalled_payload([_rendered_iframe_challenge_control()])
        fresh = _bounded_extractor_payload()
        fresh["page_title"] = "Project home"
        prior = parse_composition_structured(copy.deepcopy(stale), inspected_url=_SOURCE_URL, current_url=_SOURCE_URL)
        assert prior is not None
        ctx = _ctx(server=_server_returning_sequence([stale, fresh]), source_url=_SOURCE_URL)
        ctx.pending_scout_role_name = ("#continue", "button", "Continue")
        ctx.flow_evidence = [{"step": 0, "reached_via": "current_page", "had_bounded_schema": True, "evidence": prior}]
        monkeypatch.setattr(scouting_module, "_live_working_page_url", AsyncMock(return_value=_LANDING_URL))

        result = await _run_click(ctx)

        assert ctx.last_scout_act_observe_outcome == "attached"
        assert result["data"]["page"]["page_title"] == "Project home"
        assert ctx.flow_evidence[-1]["evidence"]["challenge_state"]["detected"] is False

    @pytest.mark.asyncio
    async def test_bounded_settle_paid_before_single_recapture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS", 0.6)
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(scouting_module, "asyncio", ScopedAsyncio(sleep=record_sleep))
        ctx = _ctx(
            server=_server_returning_sequence([{"page_title": "Loading", "forms": []}, _bounded_extractor_payload()])
        )

        parsed = await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)

        assert sleeps == []
        assert ctx.last_scout_act_observe_outcome == "attached"
        assert parsed is not None and has_bounded_page_schema(parsed)

    @pytest.mark.asyncio
    async def test_zero_settle_pays_no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS", 0.0)
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(scouting_module, "asyncio", ScopedAsyncio(sleep=record_sleep))
        ctx = _ctx(
            server=_server_returning_sequence([{"page_title": "Loading", "forms": []}, _bounded_extractor_payload()])
        )

        await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)

        assert sleeps == []
        assert ctx.last_scout_act_observe_outcome == "attached"

    @pytest.mark.asyncio
    async def test_attached_first_capture_pays_no_settle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS", 0.6)
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(scouting_module, "asyncio", ScopedAsyncio(sleep=record_sleep))
        ctx = _ctx(server=_server_returning(_bounded_extractor_payload()))

        await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)

        assert sleeps == []
        assert ctx.last_scout_act_observe_outcome == "attached"

    @pytest.mark.asyncio
    async def test_attached_challenge_without_carrier_settles_and_recaptures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS", 0.6)
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(scouting_module, "asyncio", ScopedAsyncio(sleep=record_sleep))
        ctx = _ctx(
            server=_server_returning_sequence(
                [
                    _bounded_challenge_signalled_payload(),
                    _bounded_challenge_signalled_payload([_rendered_iframe_challenge_control()]),
                ]
            )
        )

        packet = await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)

        assert sleeps == []
        assert ctx.last_scout_act_observe_recapture_attempted is True
        assert ctx.last_scout_act_observe_outcome == "attached"
        assert composition_challenge_carrier(packet) is ChallengeEvidenceSource.CHALLENGE_STATE

    @pytest.mark.asyncio
    async def test_attached_keyword_only_challenge_recapture_still_yields_no_carrier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS", 0.6)

        async def record_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(scouting_module, "asyncio", ScopedAsyncio(sleep=record_sleep))
        ctx = _ctx(
            server=_server_returning_sequence(
                [
                    _bounded_challenge_signalled_payload(),
                    _bounded_challenge_signalled_payload(),
                ]
            )
        )

        packet = await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)

        assert ctx.last_scout_act_observe_recapture_attempted is True
        assert composition_challenge_carrier(packet) is None

    @pytest.mark.asyncio
    async def test_recapture_may_not_erase_the_challenge_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A recapture that still reports a bounded schema but has lost the challenge signal
        must not replace the signalled packet: the signal is the visual fallback's only trigger."""
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS", 0.6)

        async def record_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(scouting_module, "asyncio", ScopedAsyncio(sleep=record_sleep))
        unsignalled = _bounded_extractor_payload()
        ctx = _ctx(server=_server_returning_sequence([_bounded_challenge_signalled_payload(), unsignalled]))

        packet = await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)

        assert packet is not None
        assert packet["challenge_state"]["detected"] is True
        assert packet["challenge_state"]["indicators"] == ["captcha"]

    @pytest.mark.asyncio
    async def test_attached_packet_survives_a_hollow_recapture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS", 0.6)

        async def record_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(scouting_module, "asyncio", ScopedAsyncio(sleep=record_sleep))
        ctx = _ctx(
            server=_server_returning_sequence(
                [
                    _bounded_challenge_signalled_payload(),
                    {"page_title": "Results", "forms": []},
                ]
            )
        )

        packet = await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)

        assert ctx.last_scout_act_observe_outcome == "attached"
        assert packet is not None
        assert packet["forms"]

    @pytest.mark.asyncio
    async def test_capture_log_carries_container_and_relation_counts(self) -> None:
        ctx = _ctx(server=_server_returning(_kv_only_extractor_payload()))

        with capture_logs() as logs:
            await _scout_act_observe_page_evidence(ctx, url=_LANDING_URL)

        probe = next(entry for entry in logs if entry["event"] == "copilot_scout_act_observe")
        assert probe["result_container_count"] == 0
        assert probe["key_value_relation_count"] == 1


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
        interaction_steps = [step for step, (_, reached_via) in by_step.items() if reached_via == "interaction"]
        assert len(interaction_steps) == 1
        credited_evidence, _ = by_step[interaction_steps[0]]
        assert has_bounded_page_schema(credited_evidence)
        assert _auto_credit_interaction_observation(by_step) is True

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
        assert len(serialized) <= _SCOUT_RESULT_CHAR_CAP
        assert json.loads(serialized) == result
        # The flow-evidence packet keeps the full schema; only the tool result is compact.
        assert ctx.flow_evidence[0]["had_bounded_schema"] is True
        assert len(ctx.flow_evidence[0]["evidence"]["forms"]) == 5


class TestActObserveToolGate:
    @pytest.mark.asyncio
    async def test_non_click_tools_do_not_capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert probed[0].args[1]["verbosity"] == "full"


def _np_ctx(*, server: Any = None) -> SimpleNamespace:
    ctx = _ctx(server=server)
    ctx.last_scout_act_observe_outcome = None
    ctx.blocker_signal = None
    return ctx


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


class TestCredentialInventoryCarry:
    @staticmethod
    def _credential_carry(available_fields: list[str] | None) -> dict[str, Any]:
        return carried_interaction(
            source_url=_LANDING_URL,
            selector="#password",
            tool_name="fill_credential_field",
            credential_id="cred_123",
            credential_field="username",
            available_fields=available_fields,
        )

    @pytest.mark.asyncio
    async def test_rebind_rehydrates_inventory_alongside_carried_fills(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx()
        ctx.scouted_credential_field_inventory_by_credential_id = {}
        ctx.prior_carried_trajectory = [self._credential_carry(["password", "username"])]

        scouting_module.hydrate_prior_carried_trajectory(ctx)

        assert ctx.scouted_credential_field_inventory_by_credential_id == {
            "cred_123": frozenset({"username", "password"})
        }
        assert ctx.scout_trajectory[-1]["credential_id"] == "cred_123"

    @pytest.mark.asyncio
    async def test_page_mismatch_keeps_both_the_carry_and_the_inventory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        ctx = _ctx()
        ctx.scouted_credential_field_inventory_by_credential_id = {}
        ctx.prior_carried_trajectory = [self._credential_carry(["password", "username"])]

        scouting_module.hydrate_prior_carried_trajectory(ctx)

        assert ctx.scout_trajectory[-1]["credential_id"] == "cred_123"
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
        ctx.prior_carried_trajectory = [self._credential_carry(None)]

        scouting_module.hydrate_prior_carried_trajectory(ctx)

        assert ctx.scouted_credential_field_inventory_by_credential_id == {}
        assert ctx.scout_trajectory[-1]["credential_id"] == "cred_123"

    @pytest.mark.asyncio
    async def test_inventory_round_trips_through_structured_context_and_agent_hydration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first_turn = SimpleNamespace(
            prior_page_inspection_calls_made=0,
            page_inspection_calls_this_turn=0,
            flow_evidence=[],
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
        raw = finalize_observation_context(first_turn, None)
        assert raw is not None

        monkeypatch.setattr(scouting_module, "_selector_live_match_count", _selector_count_one)
        next_turn = _ctx()
        next_turn.scouted_credential_field_inventory_by_credential_id = {}
        next_turn.prior_carried_trajectory = list(StructuredContext.from_json_str(raw).carried_trajectory)

        scouting_module.hydrate_prior_carried_trajectory(next_turn)

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


class TestTerminalActionObservationStampSeam:
    _PORTAL_URL = "https://portal.example.test/login"
    _BUSINESS_URL = "https://portal.example.test/business/start-service"

    @staticmethod
    def _terminal_action_criterion(*, method_mandated: bool = False) -> CompletionCriterion:
        return CompletionCriterion(
            id="start_service_request",
            outcome="the business start-service request reaches its review page",
            kind="terminal_action",
            terminal_action_family="request",
            method_mandated=method_mandated,
        )

    def _ctx_with(self, *criteria: CompletionCriterion) -> AgentContext:
        ctx = AgentContext(
            organization_id="o_1",
            workflow_id="w_1",
            workflow_permanent_id="wpid_1",
            workflow_yaml="",
            browser_session_id="pbs_1",
            stream=MagicMock(),
        )
        ctx.completion_criteria_turn_state = SimpleNamespace(decision=SimpleNamespace(criteria=tuple(criteria)))
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "password",
                "selector": "#password",
                "source_url": self._PORTAL_URL,
                "trajectory_index": 0,
            },
            {
                "tool_name": "click",
                "selector": "input[type='submit']",
                "source_url": self._PORTAL_URL,
                "trajectory_index": 1,
            },
            {
                "tool_name": "type_text",
                "selector": "#service-address",
                "source_url": self._BUSINESS_URL,
                "role": "textbox",
                "accessible_name": "Service Address",
                "trajectory_index": 2,
            },
        ]
        return ctx

    def test_commit_past_login_stamps_terminal_action_observation(self) -> None:
        ctx = self._ctx_with(self._terminal_action_criterion())
        with capture_logs() as logs:
            scouting_module._record_scouted_interaction(
                ctx,
                tool_name="click",
                selector="#find-address",
                source_url=self._BUSINESS_URL,
                role="button",
                accessible_name="Find Address",
            )
        assert ctx.scout_observed_terminal_criterion_ids == {"start_service_request"}
        assert [log for log in logs if log["event"] == "copilot_reached_terminal_action_observed"]

    def test_an_interaction_records_the_browser_it_was_demonstrated_in(self) -> None:
        # Untagged, a commit demonstrated on the page a run failed on is indistinguishable from one
        # the chat drove itself. The tag is provenance the model reads; it withholds no credit.
        ctx = self._ctx_with(self._terminal_action_criterion())

        with bound_call_browser_session("pbs_run"):
            scouting_module._record_scouted_interaction(
                ctx,
                tool_name="click",
                selector="#find-address",
                source_url=self._BUSINESS_URL,
                role="button",
                accessible_name="Find Address",
            )

        assert ctx.scout_trajectory[-1]["demonstrated_browser_session_id"] == "pbs_run"

    def test_an_interaction_in_the_chats_own_browser_carries_no_browser_tag(self) -> None:
        ctx = self._ctx_with(self._terminal_action_criterion())

        scouting_module._record_scouted_interaction(
            ctx,
            tool_name="click",
            selector="#find-address",
            source_url=self._BUSINESS_URL,
            role="button",
            accessible_name="Find Address",
        )

        assert "demonstrated_browser_session_id" not in ctx.scout_trajectory[-1]

    def test_matching_prior_page_observation_marks_demonstrated_control_readiness(self) -> None:
        ctx = self._ctx_with()
        ctx.scout_trajectory = []
        ctx.scouted_interactions = []
        ctx.composition_page_evidence = {
            "current_url": self._BUSINESS_URL,
            "forms": [
                {
                    "fields": [
                        {
                            "selector": "#breed",
                            "disabled": True,
                            "visible": False,
                        }
                    ]
                }
            ],
        }

        scouting_module._record_scouted_interaction(
            ctx,
            tool_name="select_option",
            selector="#breed",
            source_url=self._BUSINESS_URL,
            value="beagle",
        )

        recorded = ctx.scout_trajectory[-1]
        assert recorded["observed_disabled"] is True
        assert recorded["observed_hidden"] is True

    def test_readiness_observation_does_not_cross_page_or_selector_identity(self) -> None:
        ctx = self._ctx_with()
        ctx.scout_trajectory = []
        ctx.scouted_interactions = []
        ctx.composition_page_evidence = {
            "current_url": self._PORTAL_URL,
            "forms": [{"fields": [{"selector": "#breed", "disabled": True, "visible": False}]}],
        }

        scouting_module._record_scouted_interaction(
            ctx,
            tool_name="select_option",
            selector="#animal-kind",
            source_url=self._BUSINESS_URL,
            value="dog",
        )

        recorded = ctx.scout_trajectory[-1]
        assert "observed_disabled" not in recorded
        assert "observed_hidden" not in recorded

    def test_demonstrated_totp_fill_is_recorded_with_a_countable_capture_log(self) -> None:
        ctx = self._ctx_with(self._terminal_action_criterion())
        ctx.scout_trajectory = []
        with capture_logs() as logs:
            scouting_module._record_scouted_interaction(
                ctx,
                tool_name="fill_credential_field",
                selector="#totpCode",
                source_url=self._PORTAL_URL,
                typed_length=6,
                credential_id="cred_1",
                credential_field="totp",
                credential_name="mock-portal-login-totp",
            )
        assert ctx.scout_trajectory[-1]["credential_field"] == "totp"
        capture = next(log for log in logs if log["event"] == "copilot_scout_interaction_captured")
        assert capture["credential_field"] == "totp"
        assert capture["credential_id"] == "cred_1"

    def test_credential_fill_records_the_element_fingerprint_without_the_secret(self) -> None:
        ctx = self._ctx_with(self._terminal_action_criterion())
        ctx.scout_trajectory = []
        scouting_module._record_scouted_interaction(
            ctx,
            tool_name="fill_credential_field",
            selector="#pass",
            source_url=self._PORTAL_URL,
            typed_length=14,
            credential_id="cred_1",
            credential_field="password",
            credential_name="mock-portal-login",
            element_fingerprint_id="pass",
            element_fingerprint_name="password",
            element_fingerprint_type="password",
            element_fingerprint_placeholder="Password",
            element_fingerprint_label="Password",
            element_fingerprint_test_id="login-password",
            element_fingerprint_tag="input",
        )
        recorded = ctx.scout_trajectory[-1]
        assert recorded["element_fingerprint_id"] == "pass"
        assert recorded["element_fingerprint_type"] == "password"
        assert recorded["element_fingerprint_placeholder"] == "Password"
        assert recorded["element_fingerprint_tag"] == "input"
        assert "Hunter2Portal!" not in str(recorded)

    def test_login_only_commit_stamps_nothing(self) -> None:
        ctx = self._ctx_with(self._terminal_action_criterion())
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "password",
                "selector": "#password",
                "source_url": self._PORTAL_URL,
                "trajectory_index": 0,
            },
        ]
        scouting_module._record_scouted_interaction(
            ctx, tool_name="click", selector="input[type='submit']", source_url=self._PORTAL_URL
        )
        assert ctx.scout_observed_terminal_criterion_ids == set()

    def test_method_mandated_criterion_is_not_stamped(self) -> None:
        ctx = self._ctx_with(self._terminal_action_criterion(method_mandated=True))
        scouting_module._record_scouted_interaction(
            ctx,
            tool_name="click",
            selector="#find-address",
            source_url=self._BUSINESS_URL,
            role="button",
            accessible_name="Find Address",
        )
        assert ctx.scout_observed_terminal_criterion_ids == set()


class TestCarriedTrajectoryHydration:
    """Successor to TestCarriedInteractionRebind (SKY-13617).

    Those tests pinned a page-validation gate: the retained record only entered the
    trajectory while the browser still stood on the page that produced it. After a login
    that is never true, so the record was written and then dropped on the way back in.
    Hydration is now unconditional and the tests assert that instead.
    """

    def test_hydrates_prior_record_whatever_page_the_browser_is_on(self) -> None:
        ctx = _ctx()
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "#open", "source_url": _LANDING_URL}]
        ctx.prior_carried_trajectory = [
            carried_interaction(
                source_url=_LANDING_URL,
                selector="#account",
                tool_name="type_text",
                typed_value="ABC123",
            ),
            carried_interaction(
                source_url=_LANDING_URL,
                selector="#signin",
                tool_name="click",
            ),
        ]
        ctx.carried_trajectory_rebound_done = False

        assert scouting_module.hydrate_prior_carried_trajectory(ctx) is True

        carried = ctx.scout_trajectory[1:]
        assert [(item["tool_name"], item["selector"], item["trajectory_index"]) for item in carried] == [
            ("type_text", "#account", 1),
            ("click", "#signin", 2),
        ]
        assert [item["carried"] for item in carried] == [True, True]
        assert carried[0]["typed_value"] == "ABC123"

    def test_hydrated_executed_selector_remains_available_to_internal_locator_consumers(self) -> None:
        ctx = _ctx()
        ctx.prior_carried_trajectory = [
            {"tool_name": "click", "executed_selector": "#signin", "source_url": _LANDING_URL}
        ]
        ctx.carried_trajectory_rebound_done = False

        assert scouting_module.hydrate_prior_carried_trajectory(ctx) is True

        interaction = ctx.scout_trajectory[0]
        assert interaction["executed_selector"] == "#signin"
        assert _locator_expr(interaction, []) == 'page.locator("#signin")'

    def test_hydration_carries_the_submit_click_the_fill_carry_could_not(self) -> None:
        ctx = _ctx()
        ctx.prior_carried_trajectory = [
            carried_interaction(source_url=_LANDING_URL, selector="#signin", tool_name="click"),
        ]
        ctx.carried_trajectory_rebound_done = False

        scouting_module.hydrate_prior_carried_trajectory(ctx)

        assert [item["tool_name"] for item in ctx.scout_trajectory] == ["click"]

    def test_hydration_runs_once_per_turn(self) -> None:
        ctx = _ctx()
        ctx.prior_carried_trajectory = [
            carried_interaction(source_url=_LANDING_URL, selector="#account", tool_name="type_text"),
        ]
        ctx.carried_trajectory_rebound_done = False

        assert scouting_module.hydrate_prior_carried_trajectory(ctx) is True
        assert scouting_module.hydrate_prior_carried_trajectory(ctx) is False
        assert len(ctx.scout_trajectory) == 1

    def test_hydration_rehydrates_credential_field_inventory(self) -> None:
        ctx = _ctx()
        ctx.scouted_credential_field_inventory_by_credential_id = {}
        ctx.prior_carried_trajectory = [
            carried_interaction(
                source_url=_LANDING_URL,
                selector="#password",
                tool_name="fill_credential_field",
                credential_id="cred_123",
                credential_field="password",
                available_fields=["password", "username"],
            ),
        ]
        ctx.carried_trajectory_rebound_done = False

        scouting_module.hydrate_prior_carried_trajectory(ctx)

        assert ctx.scouted_credential_field_inventory_by_credential_id["cred_123"] == frozenset(
            {"password", "username"}
        )
        assert "available_fields" not in ctx.scout_trajectory[0]

    def test_empty_prior_record_hydrates_nothing(self) -> None:
        ctx = _ctx()
        ctx.prior_carried_trajectory = []
        ctx.carried_trajectory_rebound_done = False

        assert scouting_module.hydrate_prior_carried_trajectory(ctx) is False
        assert ctx.scout_trajectory == []

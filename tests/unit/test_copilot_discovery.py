"""Tests for Copilot build-time entrypoint discovery."""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from skyvern.forge.agent_functions import CopilotEntrypointCandidate, CopilotSiteOriginAssociation
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_STRIPPED_HTML_EXPRESSION,
    COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION,
    COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION,
)
from skyvern.forge.sdk.copilot.enforcement import _RECENT_TOOL_OUTPUT_CHAR_CAP, _prune_input_list
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, _ground_user_provided_sites
from skyvern.forge.sdk.copilot.runtime import PendingBrowserInteractionObservation
from skyvern.forge.sdk.copilot.tools import (
    _discovery_walk,
    _inspect_page_for_composition_impl,
    _rank_discovery_entrypoint_candidates,
    _resolve_discovery_entry_url,
)
from skyvern.forge.sdk.copilot.tools.discovery import (
    _credential_entry_url,
    _discovery_build_result,
    _user_provided_entry_url,
)
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatSender

_VALID_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAE0lEQVR4nGP8z4APMOGVZRip0gBBLAETee26JgAAAABJRU5ErkJggg=="
)


class _Ctx:
    def __init__(self, server: object) -> None:
        self.turn_origin = TurnOrigin.interactive
        self.discovery_mcp_server = server
        self.discovery_started_monotonic = None
        self.discovery_step_count = 0
        self.prior_page_inspection_calls_made = 0
        self.page_inspection_calls_this_turn = 0
        self.flow_evidence: list[dict[str, Any]] = []
        self.composition_page_evidence = None
        self.pending_browser_interaction_observation = None
        self.workflow_verification_evidence = WorkflowVerificationEvidence()
        self.browser_session_id = None
        self.last_run_blocks_browser_session_id = None
        self.request_policy = None
        self.org_credentials_for_turn = None
        self.supports_vision = True
        self.pending_screenshots: list[Any] = []


def _dense_structured_page() -> dict[str, Any]:
    forms: list[dict[str, Any]] = []
    for form_index in range(2):
        fields: list[dict[str, Any]] = []
        for select_index in range(5):
            field_index = form_index * 5 + select_index
            options = [
                {
                    "text": f"Option {field_index}-{option_index} " + "T" * 105,
                    "value": f"value-{field_index}-{option_index}-" + "V" * 145,
                    "selected": option_index == 0,
                }
                for option_index in range(30)
            ]
            fields.append(
                {
                    "name": f"select_{field_index}",
                    "id": f"select_{field_index}",
                    "label": f"Dense select {field_index}",
                    "type": "select",
                    "value": "",
                    "class": [],
                    "placeholder": "",
                    "required": field_index % 2 == 0,
                    "disabled": False,
                    "readonly": False,
                    "visible": True,
                    "checked": False,
                    "options": options,
                    "option_count": len(options),
                    "options_omitted": False,
                    "selector": f"#select_{field_index}",
                }
            )
        forms.append(
            {
                "id": f"dense_form_{form_index}",
                "name": f"dense_form_{form_index}",
                "action": f"/submit/{form_index}",
                "method": "post",
                "fields": fields,
                "submit_controls": [
                    {
                        "text": f"Submit form {form_index}",
                        "id": f"submit_{form_index}",
                        "type": "submit",
                        "selector": f"#submit_{form_index}",
                    }
                ],
            }
        )
    return {
        "page_title": "Dense Form",
        "body_has_markup": True,
        "forms": forms,
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
        "modal_overlays": [],
        "visual_obstruction_candidates": [],
        "visible_text_excerpt": "Dense form controls",
        "anti_bot_indicators": [],
    }


class _DensePageServer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(tool_name)
        assert tool_name == "skyvern_evaluate"
        return {"ok": True, "data": {"result": _dense_structured_page()}}


def _structured_search_page(*, with_obstruction: bool = False) -> dict[str, Any]:
    return {
        "page_title": "Results",
        "body_has_markup": True,
        "forms": [
            {
                "selector": "form",
                "fields": [
                    {
                        "name": "firstName",
                        "type": "text",
                        "selector": 'input[name="firstName"]',
                    }
                ],
                "submit_controls": [{"text": "Search", "type": "submit", "selector": "button"}],
            }
        ],
        "visual_obstruction_candidates": [
            {
                "source": "computed_style",
                "position": "fixed",
                "coverage": "viewport",
                "has_visible_controls": True,
            }
        ]
        if with_obstruction
        else [],
    }


class _FailingNavigateServer:
    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert tool_name == "skyvern_navigate"
        assert arguments == {"url": "https://www.example.com"}
        return {"ok": False, "error": "Failed to create browser session"}


class _InspectableNoCandidateServer:
    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            return {"ok": True, "data": {"html": "<html><body><p>Welcome</p></body></html>"}}
        raise AssertionError(f"unexpected tool: {tool_name}")


class _AnchorBeatsTitleServer:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            self.urls.append(arguments["url"])
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            if self.urls[-1] == "https://www.example.com":
                return {
                    "ok": True,
                    "data": {
                        "html": """
                        <html><head><title>Example Certification</title></head>
                        <body><a href="/registry">Find a Certificant</a></body></html>
                        """
                    },
                }
            return {
                "ok": True,
                "data": {
                    "html": """
                    <html><head><title>Find a Member</title></head>
                    <body><form><input name="firstName"><button>Search</button></form></body></html>
                    """
                },
            }
        raise AssertionError(f"unexpected tool: {tool_name}")


class _DeepLinkAntiBotRecoveryServer:
    def __init__(self) -> None:
        self.navigated_urls: list[str] = []
        self.clicked_selectors: list[str] = []
        self.current_url = ""

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            self.current_url = arguments["url"]
            self.navigated_urls.append(self.current_url)
            return {"ok": True, "data": {"url": self.current_url}}
        if tool_name == "skyvern_click":
            self.clicked_selectors.append(arguments["selector"])
            self.current_url = "https://certboard.test/registry/search"
            return {"ok": True, "data": {"url": self.current_url}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            if self.current_url == "https://certboard.test/registry/search" and not self.clicked_selectors:
                return {
                    "ok": True,
                    "data": {
                        "html": """
                        <html><head><title>Just a moment</title></head>
                        <body>Verify you are human before continuing.</body></html>
                        """
                    },
                }
            if self.current_url == "https://certboard.test/":
                return {
                    "ok": True,
                    "data": {
                        "html": """
                        <html><head><title>Example Certifications</title></head>
                        <body><a href="/find-a-member/">Find a Member</a></body></html>
                        """
                    },
                }
            return {
                "ok": True,
                "data": {
                    "html": """
                    <html><head><title>Example Certification Registry</title></head>
                    <body><form><input name="first_name"><input name="last_name"><button>Search</button></form></body></html>
                    """
                },
            }
        raise AssertionError(f"unexpected tool: {tool_name}")


class _EmbeddedChallengeUsefulPageServer:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            self.urls.append(arguments["url"])
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            if self.urls[-1] == "https://certboard.test":
                return {
                    "ok": True,
                    "data": {
                        "html": """
                        <html><head><title>Certification Board</title></head>
                        <body><a href="/registry/search">Find a Certificant</a></body></html>
                        """
                    },
                }
            return {
                "ok": True,
                "data": {
                    "html": """
                    <html>
                      <head>
                        <title>Certificant Registry</title>
                        <script src="https://challenges.example.test/turnstile/api.js"></script>
                      </head>
                      <body>
                        <form>
                          <label for="first-name">First Name</label>
                          <input id="first-name" name="first_name">
                          <label for="last-name">Last Name</label>
                          <input id="last-name" name="last_name">
                        </form>
                      </body>
                    </html>
                    """
                },
            }
        raise AssertionError(f"unexpected tool: {tool_name}")


class _CurrentPageServer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(tool_name)
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            return {
                "ok": True,
                "data": {
                    "html": "<html><body><form><input name='firstName'><button>Search</button></form></body></html>"
                },
            }
        if tool_name == "skyvern_evaluate":
            assert arguments["expression"] == COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION
            return {"ok": True, "data": {"result": _structured_search_page()}}
        raise AssertionError(f"unexpected tool: {tool_name}")


class _GenericBarrierServer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(tool_name)
        if tool_name == "skyvern_navigate":
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            return {
                "ok": True,
                "data": {
                    "html": """
                    <html><head>
                      <style>
                        .checkpoint-shell {
                          position: fixed;
                          inset: 0;
                          z-index: 2000;
                          background: rgba(0,0,0,.4);
                        }
                      </style>
                    </head><body>
                      <form id="search"><input name="q"><button>Search</button></form>
                      <section id="checkpoint" class="checkpoint-shell">
                        <p>Complete this checkpoint before continuing.</p>
                        <button>Continue</button>
                      </section>
                    </body></html>
                    """
                },
            }
        if tool_name == "skyvern_evaluate":
            assert arguments["expression"] == COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION
            return {"ok": True, "data": {"result": _structured_search_page(with_obstruction=True)}}
        if tool_name == "skyvern_screenshot":
            expected_session = arguments.get("session_id")
            assert arguments == {"inline": True, **({"session_id": expected_session} if expected_session else {})}
            return {
                "ok": True,
                "browser_context": {"session_id": expected_session},
                "data": {"screenshot_base64": _VALID_PNG_B64},
            }
        raise AssertionError(f"unexpected tool: {tool_name}")


class _TargetThenCurrentPageServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.current_url = ""

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        if tool_name == "skyvern_navigate":
            self.current_url = arguments["url"]
            return {"ok": True, "data": {"url": self.current_url}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            return {
                "ok": True,
                "data": {
                    "html": "<html><body><form><input name='firstName'><button>Search</button></form></body></html>"
                },
            }
        if tool_name == "skyvern_evaluate":
            assert arguments["expression"] == COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION
            return {"ok": True, "data": {"result": _structured_search_page()}}
        raise AssertionError(f"unexpected tool: {tool_name}")


@pytest.mark.parametrize(
    ("site_or_url", "expected"),
    [
        ("https://example.com/login", ("https://example.com/login", "url")),
        ("HTTP://example.com/login", ("HTTP://example.com/login", "url")),
        ("example.com", ("https://example.com", "domain")),
        ("example.com/login?x=y", ("https://example.com/login?x=y", "domain")),
        ("example", (None, "bare_word")),
        ("example search portal", (None, "unresolved")),
    ],
)
def test_resolve_discovery_entry_url_classifies_without_guessing(
    site_or_url: str,
    expected: tuple[str | None, str],
) -> None:
    assert _resolve_discovery_entry_url(site_or_url) == expected


@pytest.mark.parametrize(
    ("site_or_url", "expected"),
    [
        ("https://example.com/login", ("https://example.com/login", "url")),
        ("HTTP://example.com/login", ("HTTP://example.com/login", "url")),
        ("example.com", ("https://example.com", "domain")),
        ("example.com/login?x=y", ("https://example.com/login?x=y", "domain")),
    ],
)
def test_resolve_discovery_entry_url_preserves_url_and_domain_inputs(
    site_or_url: str,
    expected: tuple[str, str],
) -> None:
    assert _resolve_discovery_entry_url(site_or_url) == expected


def test_legacy_url_domain_result_envelope_has_no_bare_word_contract_version() -> None:
    result = _discovery_build_result(
        candidate_url="https://example.com/",
        candidate_form_fields=[],
        evidence_trail=[],
        confidence=0.6,
        failure_reason=None,
    )

    assert result == {
        "ok": True,
        "data": {
            "candidate_url": "https://example.com/",
            "candidate_form_fields": [],
            "evidence_trail": [],
            "confidence": 0.6,
            "failure_reason": None,
        },
        "error": None,
    }


def test_rank_discovery_entrypoint_candidates_accepts_provider_association_with_different_entity_label() -> None:
    candidates = [
        CopilotEntrypointCandidate(
            url="https://irrelevant.example/news",
            source_rank=1,
            association=CopilotSiteOriginAssociation(
                requested_name="public alias",
                entity_id="Q1",
                entity_label="Unrelated result",
                official_site_url="https://irrelevant.example/news",
                origin="https://irrelevant.example",
                source="provider_official_site",
                provider_relation_type="label",
                provider_relation_text="public alias",
            ),
        ),
        CopilotEntrypointCandidate(
            url="https://attacker.example/start",
            source_rank=2,
            association=CopilotSiteOriginAssociation(
                requested_name="public alias",
                entity_id="Q2",
                entity_label="Public Alias",
                official_site_url="https://public-alias.test/start",
                origin="https://public-alias.test",
                source="provider_official_site",
                provider_relation_type="alias",
                provider_relation_text="public alias",
            ),
        ),
        CopilotEntrypointCandidate(
            url="https://public-alias.test/start",
            source_rank=3,
            association=CopilotSiteOriginAssociation(
                requested_name="public alias",
                entity_id="Q3",
                entity_label="Public Alias",
                official_site_url="https://public-alias.test/start",
                origin="https://public-alias.test",
                source="provider_official_site",
                provider_relation_type="alias",
                provider_relation_text="public alias",
            ),
        ),
    ]

    assert _rank_discovery_entrypoint_candidates("public alias", candidates) == [candidates[0], candidates[2]]


def test_rank_discovery_entrypoint_candidates_keeps_unassociated_licensee_enforced() -> None:
    licensor = CopilotEntrypointCandidate(
        url="https://licensor.example/",
        source_rank=1,
        association=CopilotSiteOriginAssociation(
            requested_name="Public Alias",
            entity_id="Q1",
            entity_label="Public Alias",
            official_site_url="https://licensor.example/",
            origin="https://licensor.example",
            source="provider_official_site",
            provider_relation_type="label",
            provider_relation_text="Other Licensor",
        ),
    )
    licensee = CopilotEntrypointCandidate(
        url="https://licensee.example/",
        source_rank=2,
        association=CopilotSiteOriginAssociation(
            requested_name="Public Alias",
            entity_id="Q2",
            entity_label="Unrelated Licensee",
            official_site_url="https://licensee.example/",
            origin="https://licensee.example",
            source="provider_official_site",
            provider_relation_type="alias",
            provider_relation_text="Other Licensee",
        ),
    )

    assert _rank_discovery_entrypoint_candidates("public alias", [licensor, licensee]) == []


@pytest.mark.asyncio
async def test_discovery_navigation_failure_falls_back_to_entry_url() -> None:
    result = await _discovery_walk(
        _Ctx(_FailingNavigateServer()),
        entry_url="https://www.example.com",
        intent_hint="find a member",
    )

    assert result["ok"] is True
    assert result["error"] is None
    assert result["data"]["candidate_url"] == "https://www.example.com"
    assert result["data"]["failure_reason"] is None
    assert result["data"]["confidence"] == 0.2
    assert result["data"]["evidence_trail"][0]["transition_reason"].startswith("navigate_failed:")


@pytest.mark.asyncio
async def test_discovery_successful_inspection_without_match_still_returns_no_candidate() -> None:
    result = await _discovery_walk(
        _Ctx(_InspectableNoCandidateServer()),
        entry_url="https://www.example.com",
        intent_hint="find a member",
    )

    assert result["ok"] is True
    assert result["data"]["candidate_url"] is None
    assert result["data"]["failure_reason"] == "no_candidate"


@pytest.mark.asyncio
async def test_discovery_follows_stronger_intent_anchor_before_settling_on_broad_title() -> None:
    server = _AnchorBeatsTitleServer()

    result = await _discovery_walk(
        _Ctx(server),
        entry_url="https://www.example.com",
        intent_hint="find a member",
    )

    assert result["ok"] is True
    assert result["data"]["candidate_url"] == "https://www.example.com/registry"
    assert result["data"]["candidate_form_fields"] == [
        {"label": "", "name": "firstName", "type": "input", "value_hint": ""}
    ]
    assert server.urls == ["https://www.example.com", "https://www.example.com/registry"]


@pytest.mark.asyncio
async def test_discovery_recovers_from_deep_link_anti_bot_by_clicking_from_origin() -> None:
    server = _DeepLinkAntiBotRecoveryServer()

    result = await _discovery_walk(
        _Ctx(server),
        entry_url="https://certboard.test/registry/search",
        intent_hint="find a member",
    )

    assert result["ok"] is True
    assert result["data"]["candidate_url"] == "https://certboard.test/registry/search"
    assert result["data"]["failure_reason"] is None
    assert result["data"]["candidate_form_fields"] == [
        {"label": "", "name": "first_name", "type": "input", "value_hint": ""},
        {"label": "", "name": "last_name", "type": "input", "value_hint": ""},
    ]
    assert server.navigated_urls == [
        "https://certboard.test/registry/search",
        "https://certboard.test/",
    ]
    assert server.clicked_selectors == ['a[href="/find-a-member/"]']
    assert [item["transition_reason"] for item in result["data"]["evidence_trail"]] == [
        "direct_deep_link_anti_bot",
        "anchor_match",
        "anchor_match",
    ]


@pytest.mark.asyncio
async def test_discovery_keeps_candidate_when_challenge_markup_is_embedded_in_useful_page() -> None:
    server = _EmbeddedChallengeUsefulPageServer()

    result = await _discovery_walk(
        _Ctx(server),
        entry_url="https://certboard.test",
        intent_hint="find certificant lookup page",
    )

    assert result["ok"] is True
    assert result["data"]["candidate_url"] == "https://certboard.test/registry/search"
    assert result["data"]["failure_reason"] is None
    assert result["data"]["candidate_form_fields"] == [
        {"label": "First Name", "name": "first_name", "type": "input", "value_hint": ""},
        {"label": "Last Name", "name": "last_name", "type": "input", "value_hint": ""},
    ]


@pytest.mark.asyncio
async def test_inspect_current_page_uses_existing_browser_page(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _CurrentPageServer()
    ctx = _Ctx(server)
    ctx.last_run_blocks_workflow_run_id = "wr_123"  # type: ignore[attr-defined]
    ctx.composition_page_evidence = None  # type: ignore[attr-defined]

    async def fake_fallback_page_info(_ctx: object, _session_id_override: str | None = None) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module.composition_capture, "_fallback_page_info", fake_fallback_page_info)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert server.calls == ["skyvern_evaluate"]
    assert result["data"]["current_url"] == "https://www.example.com/results"
    assert result["data"]["workflow_run_id"] == "wr_123"
    assert result["data"]["observed_after_workflow_run"] is True


@pytest.mark.asyncio
async def test_dense_inspect_packet_sheds_only_select_options_before_recent_tool_pruning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _DensePageServer()
    ctx = _Ctx(server)

    async def fake_fallback_page_info(_ctx: object, _session_id_override: str | None = None) -> tuple[str, str]:
        return "https://www.example.com/dense-form", "Dense Form"

    async def fake_bind_credential(_ctx: object, _url: str, result: dict[str, Any]) -> None:
        result["resolved_login_credential_id"] = "cred_saved_login"
        result["resolved_login_credential_name"] = "Saved login"
        result["resolved_login_credential_totp_type"] = "authenticator"
        result["resolved_login_page_url"] = "https://www.example.com/dense-form"

    monkeypatch.setattr(tools_module.composition_capture, "_fallback_page_info", fake_fallback_page_info)
    monkeypatch.setattr(
        tools_module.composition_capture,
        "_bind_login_credential_for_observed_url",
        fake_bind_credential,
    )

    result = await _inspect_page_for_composition_impl(ctx, "current_page")
    serialized = json.dumps(result)
    with structlog.testing.capture_logs() as logs:
        pruned = _prune_input_list([{"type": "function_call_output", "call_id": "dense-inspect", "output": serialized}])

    assert result["ok"] is True
    assert len(serialized) < _RECENT_TOOL_OUTPUT_CHAR_CAP
    assert pruned[0]["output"] == serialized
    assert not any(entry["event"] == "copilot_recent_tool_output_truncated" for entry in logs)
    assert result["observation_step"] == 0
    assert result["resolved_login_credential_id"] == "cred_saved_login"
    assert result["resolved_login_credential_name"] == "Saved login"
    assert result["resolved_login_credential_totp_type"] == "authenticator"
    assert result["resolved_login_page_url"] == "https://www.example.com/dense-form"

    forms = result["data"]["forms"]
    assert [form["id"] for form in forms] == ["dense_form_0", "dense_form_1"]
    assert all("selector" not in form["submit_controls"][0] for form in forms)
    fields = [field for form in forms for field in form["fields"]]
    assert all("selector" not in field for field in fields)
    omitted = [field for field in fields if field["options_omitted"]]
    retained = [field for field in fields if not field["options_omitted"]]
    assert omitted
    assert retained
    assert all(field["option_count"] == 30 and field["options"] == [] for field in omitted)
    assert all(field["option_count"] == 30 and len(field["options"]) == 30 for field in retained)

    # Packet shaping is model-facing only; stored composition and trajectory evidence stay complete.
    assert len(ctx.composition_page_evidence["forms"][0]["fields"][0]["options"]) == 30
    assert len(ctx.flow_evidence[0]["evidence"]["forms"][0]["fields"][0]["options"]) == 30


@pytest.mark.asyncio
async def test_post_run_current_page_inspection_budget_bypass_does_not_consume_chat_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _CurrentPageServer()
    ctx = _Ctx(server)
    ctx.prior_page_inspection_calls_made = 6
    ctx.page_inspection_calls_this_turn = 0
    ctx.last_run_blocks_workflow_run_id = "wr_123"  # type: ignore[attr-defined]
    ctx.last_test_ok = True  # type: ignore[attr-defined]
    ctx.composition_page_evidence = None  # type: ignore[attr-defined]

    async def fake_fallback_page_info(_ctx: object, _session_id_override: str | None = None) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module.composition_capture, "_fallback_page_info", fake_fallback_page_info)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert result["data"]["workflow_run_id"] == "wr_123"
    assert result["data"]["observed_after_workflow_run"] is True
    assert ctx.page_inspection_calls_this_turn == 0
    assert ctx.post_run_current_page_inspection_workflow_run_id == "wr_123"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_current_page_inspection_without_earned_interaction_is_not_click_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _CurrentPageServer()
    ctx = _Ctx(server)

    async def fake_fallback_page_info(_ctx: object, _session_id_override: str | None = None) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module.composition_capture, "_fallback_page_info", fake_fallback_page_info)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert result["reached_via"] == "current_page"
    assert ctx.flow_evidence[0]["reached_via"] == "current_page"


@pytest.mark.asyncio
async def test_current_page_inspection_after_browser_action_is_click_reached_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _CurrentPageServer()
    ctx = _Ctx(server)
    ctx.pending_browser_interaction_observation = PendingBrowserInteractionObservation(
        tool_name="click",
        url="https://www.example.com/results",
    )

    async def fake_fallback_page_info(_ctx: object, _session_id_override: str | None = None) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module.composition_capture, "_fallback_page_info", fake_fallback_page_info)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert result["reached_via"] == "interaction"
    assert ctx.flow_evidence[0]["reached_via"] == "interaction"
    assert ctx.pending_browser_interaction_observation is None


class _EmptyPageServer:
    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_get_html":
            return {"ok": True, "data": {"html": "<html><body></body></html>"}}
        return {"ok": True, "data": {"result": None}}


@pytest.mark.asyncio
async def test_repeated_structured_inspection_is_not_rationed() -> None:
    """Understanding a page should not get harder the more inspection it needs; a prior per-turn
    cap rejected further structured looks and steered the agent onto hand-rolled probes."""
    ctx = _Ctx(server=_EmptyPageServer())
    ctx.page_inspection_calls_this_turn = 999

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert "budget" not in str(result.get("error") or "")


@pytest.mark.asyncio
async def test_target_url_inspection_uses_visual_summary_for_generic_obstruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _GenericBarrierServer()
    ctx = _Ctx(server)

    async def fake_visual_summary(
        _ctx: object,
        *,
        evidence: dict[str, Any],
        screenshot_b64: str,
        requested_targets: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], None]:
        assert screenshot_b64 == _VALID_PNG_B64
        assert evidence["visual_obstruction_candidates"][0]["coverage"] == "viewport"
        return {
            "summary": "A checkpoint panel blocks the search form.",
            "challenge_detected": False,
            "challenge_kind": "",
            "challenge_location": "",
            "submit_blocked": False,
            "blocked_submit_controls": [],
            "empty_page_visible": False,
            "loading_state_visible": False,
            "page_obstruction_detected": True,
            "obstruction_kind": "checkpoint_panel",
            "obstruction_location": "Centered over the form.",
            "underlying_page_blocked": True,
            "visible_dismiss_controls": ["Continue"],
            "omissions": [],
        }, None

    monkeypatch.setattr(tools_module.composition_capture, "_composition_summarize_screenshot", fake_visual_summary)

    result = await _inspect_page_for_composition_impl(ctx, "https://www.example.com/search")

    assert result["ok"] is True
    assert "skyvern_evaluate" in server.calls
    assert "skyvern_screenshot" in server.calls
    assert result["data"]["screenshot_used"] is True
    json.dumps(result["data"])
    assert all("CapturedFrame" not in repr(value) for value in result["data"].values())
    assert result["data"]["page_obstructions"] == [
        {
            "kind": "checkpoint_panel",
            "source": "vision_summary",
            "visual_location": "Centered over the form.",
            "visible_controls": [{"text": "Continue"}],
            "underlying_page_blocked": True,
        }
    ]
    assert len(ctx.pending_screenshots) == 1
    frame = ctx.pending_screenshots[0]
    assert frame.provenance.source_tool == "inspect_page_for_composition"
    assert frame.provenance.captured_url is None
    assert frame.provenance.dispatch_url == "https://www.example.com/search"
    assert frame.provenance.observation_step == result["observation_step"]
    assert frame.provenance.browser_session_id is None
    assert frame.provenance.session_binding.value == "unavailable"
    assert frame.provenance.workflow_run_id is None
    assert frame.provenance.action_relation.value == "same_page_observation"
    assert frame.capture_id == f"sha256:{hashlib.sha256(base64.b64decode(_VALID_PNG_B64)).hexdigest()}"


@pytest.mark.asyncio
async def test_post_run_visual_fallback_binds_the_observed_run_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _GenericBarrierServer()
    ctx = _Ctx(server)
    ctx.browser_session_id = "pbs_scout"
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    ctx.last_run_blocks_workflow_run_id = "wr_123"  # type: ignore[attr-defined]

    async def fake_page_info(_ctx: object, session_id: str | None = None) -> tuple[str, str]:
        assert session_id == "pbs_run"
        return "https://www.example.com/search", "Search"

    async def fake_visual_summary(
        _ctx: object,
        *,
        evidence: dict[str, Any],
        screenshot_b64: str,
        requested_targets: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], None]:
        assert screenshot_b64 == _VALID_PNG_B64
        return {
            "summary": "A checkpoint panel blocks the search form.",
            "challenge_detected": False,
            "challenge_kind": "",
            "challenge_location": "",
            "submit_blocked": False,
            "blocked_submit_controls": [],
            "empty_page_visible": False,
            "loading_state_visible": False,
            "page_obstruction_detected": True,
            "obstruction_kind": "checkpoint_panel",
            "obstruction_location": "Centered over the form.",
            "underlying_page_blocked": True,
            "visible_dismiss_controls": ["Continue"],
            "omissions": [],
        }, None

    monkeypatch.setattr(tools_module.composition_capture, "_fallback_page_info", fake_page_info)
    monkeypatch.setattr(tools_module.composition_capture, "_composition_summarize_screenshot", fake_visual_summary)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["data"]["source_browser_session_id"] == "pbs_run"
    assert result["data"]["workflow_run_id"] == "wr_123"
    frame = ctx.pending_screenshots[0]
    assert frame.provenance.captured_url is None
    assert frame.provenance.dispatch_url == "https://www.example.com/search"
    assert frame.provenance.observation_step == result["observation_step"]
    assert frame.provenance.browser_session_id == "pbs_run"
    assert frame.provenance.dispatch_browser_session_id == "pbs_run"
    assert frame.provenance.producer_browser_session_id == "pbs_run"
    assert frame.provenance.session_binding.value == "agree"
    assert frame.provenance.workflow_run_id == "wr_123"
    assert ctx.browser_session_id == "pbs_scout"


@pytest.mark.asyncio
async def test_target_url_inspection_clears_pending_interaction_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _TargetThenCurrentPageServer()
    ctx = _Ctx(server)
    ctx.pending_browser_interaction_observation = PendingBrowserInteractionObservation(
        tool_name="click",
        url="https://www.example.com/results",
    )

    target_result = await _inspect_page_for_composition_impl(ctx, "https://www.example.com/results")

    assert target_result["ok"] is True
    assert target_result["reached_via"] == "navigate"
    assert ctx.pending_browser_interaction_observation is None

    async def fake_fallback_page_info(_ctx: object, _session_id_override: str | None = None) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module.composition_capture, "_fallback_page_info", fake_fallback_page_info)

    current_result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert current_result["ok"] is True
    assert current_result["reached_via"] == "current_page"
    assert [entry["reached_via"] for entry in ctx.flow_evidence] == ["navigate", "current_page"]


class _SizeCappedHtmlStrippedFallbackServer:
    """Every page's get_html is dropped by the MCP size cap (a heavy DOM exceeds it).
    The stripped-body evaluate fallback recovers each page, so the resolver can still
    follow the intent anchor to the form and resolve an entrypoint instead of parsing
    empty pages and giving up."""

    def __init__(self) -> None:
        self.tools: list[str] = []
        self.urls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.tools.append(tool_name)
        if tool_name == "skyvern_navigate":
            self.urls.append(arguments["url"])
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            return {"ok": True, "data": {"size_capped": True}}
        if tool_name == "skyvern_evaluate":
            if self.urls[-1] == "https://www.example.com":
                stripped = "<body><a href='/registry'>Find a Certificant</a></body>"
            else:
                stripped = "<body><form><input name='firstName'><button>Search</button></form></body>"
            return {"ok": True, "data": {"result": stripped}}
        raise AssertionError(f"unexpected tool: {tool_name}")


@pytest.mark.asyncio
async def test_discovery_recovers_entrypoint_when_get_html_is_size_capped() -> None:
    server = _SizeCappedHtmlStrippedFallbackServer()

    result = await _discovery_walk(_Ctx(server), entry_url="https://www.example.com", intent_hint="find a member")

    assert result["ok"] is True
    assert result["data"]["candidate_url"] == "https://www.example.com/registry"
    assert result["data"]["candidate_form_fields"] == [
        {"label": "", "name": "firstName", "type": "input", "value_hint": ""}
    ]
    assert "skyvern_evaluate" in server.tools


class _StrippedHtmlServer:
    """get_html is size-capped (dropped); the stripped-body evaluate returns a fixed body so
    the truncation flag can be exercised."""

    def __init__(self, stripped: str) -> None:
        self._stripped = stripped

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            return {"ok": True, "data": {"size_capped": True}}
        if tool_name == "skyvern_evaluate":
            expression = arguments["expression"]
            if expression == COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION:
                return {"ok": True, "data": {"result": {"page_title": "Loading", "forms": []}}}
            if expression == COMPOSITION_STRIPPED_HTML_EXPRESSION:
                assert arguments["verbosity"] == "full"
                return {"ok": True, "data": {"result": self._stripped}}
            assert expression == COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION
            return {"ok": True, "data": {"result": []}}
        raise AssertionError(f"unexpected tool: {tool_name}")


class _RenderedStyleHtmlServer:
    def __init__(self) -> None:
        self.tools: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.tools.append(tool_name)
        assert tool_name == "skyvern_evaluate"
        assert arguments["expression"] == COMPOSITION_STRIPPED_HTML_EXPRESSION
        return {
            "ok": True,
            "data": {
                "result": (
                    '<body><div id="veil" data-page-evidence-rendered-style="true" '
                    'style="position:fixed;inset:0;z-index:1200"></div></body>'
                )
            },
        }


@pytest.mark.asyncio
async def test_composition_get_html_prefers_rendered_style_snapshot() -> None:
    from skyvern.forge.sdk.copilot.tools import _composition_get_html

    server = _RenderedStyleHtmlServer()
    html, error, truncated, used_stripped = await _composition_get_html(_Ctx(server), rendered_style_snapshot=True)

    assert error is None
    assert truncated is False
    assert used_stripped is True
    assert "data-page-evidence-rendered-style" in html
    assert server.tools == ["skyvern_evaluate"]


@pytest.mark.asyncio
async def test_composition_get_html_flags_truncation_when_stripped_body_hits_cap() -> None:
    from skyvern.forge.sdk.copilot.tools import _COMPOSITION_STRIPPED_HTML_MAX_CHARS, _composition_get_html

    at_cap = "<body>" + "x" * _COMPOSITION_STRIPPED_HTML_MAX_CHARS
    _, error, truncated, _ = await _composition_get_html(_Ctx(_StrippedHtmlServer(at_cap)))
    assert error is None
    assert truncated is True

    under_cap = "<body><form><input name='x'></form></body>"
    _, error, truncated, _ = await _composition_get_html(_Ctx(_StrippedHtmlServer(under_cap)))
    assert error is None
    assert truncated is False


@pytest.mark.asyncio
async def test_capture_composition_evidence_warns_when_html_sliced_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.copilot.tools import _COMPOSITION_STRIPPED_HTML_MAX_CHARS, _capture_composition_evidence

    # A real form near the top yields bounded schema (no hollow-recapture loop); the trailing
    # padding pushes the stripped body past the cap so the fallback slice is detected as partial.
    body = (
        "<body><form><input name='firstName'><button>Search</button></form>"
        + "x" * _COMPOSITION_STRIPPED_HTML_MAX_CHARS
    )
    monkeypatch.setattr(tools_module.composition_capture.asyncio, "sleep", AsyncMock())
    evidence, error = await _capture_composition_evidence(
        _Ctx(_StrippedHtmlServer(body)),
        inspected_url="https://www.example.com/search",
        current_url="https://www.example.com/search",
    )
    assert error is None
    assert evidence is not None
    assert "html_sliced_at_cap" in evidence["inspection_warnings"]


class TestUserProvidedEntryUrl:
    """Name lookup asks the world; this asks the conversation, so a URL the user already pasted is
    not answered with a request for a URL."""

    @staticmethod
    def _ctx(user_message: str) -> SimpleNamespace:
        policy = RequestPolicy()
        _ground_user_provided_sites(policy, user_message, [])
        return SimpleNamespace(request_policy=policy)

    def test_a_url_only_the_product_wrote_never_grounds_a_site(self) -> None:
        """Grounding releases credentials, so it stays USER-only; widening it to every turn opener
        would let a server-authored row authorize an origin the person never typed."""
        policy = RequestPolicy()
        _ground_user_provided_sites(
            policy,
            "fix it",
            [
                SimpleNamespace(
                    sender=WorkflowCopilotChatSender.PRODUCT,
                    content="Diagnose run wr_1 at https://impostor.example.com and repair the workflow.",
                )
            ],
        )

        assert _user_provided_entry_url(SimpleNamespace(request_policy=policy)) is None

    def test_the_only_site_the_user_gave_is_opened(self) -> None:
        ctx = self._ctx("go to https://us.example.com/reports and pull the numbers")

        assert _user_provided_entry_url(ctx) == "https://us.example.com/reports"

    def test_several_sites_resolve_nothing(self) -> None:
        ctx = self._ctx("check https://a.example.com and https://b.example.net")

        assert _user_provided_entry_url(ctx) is None

    def test_nothing_the_user_provided_resolves_nothing(self) -> None:
        assert _user_provided_entry_url(self._ctx("no addresses here")) is None


class TestCredentialEntryUrl:
    """The org already recorded where a credential signs in, so a site named in words whose
    credential carries a login page is opened rather than answered with a request for its URL."""

    @staticmethod
    def _ctx(*tested_urls: str | None) -> SimpleNamespace:
        policy = RequestPolicy()
        policy.resolved_credentials = [
            SimpleNamespace(credential_id=f"cred_{index}", name=f"credential {index}", tested_url=tested_url)
            for index, tested_url in enumerate(tested_urls)
        ]
        return SimpleNamespace(request_policy=policy)

    def test_the_credential_naming_the_requested_site_is_opened(self) -> None:
        ctx = self._ctx("https://apps.hydroco.example/portal/Login.jsp")

        assert _credential_entry_url(ctx, "hydroco") == "https://apps.hydroco.example/portal/Login.jsp"

    def test_a_multi_word_site_name_resolves_its_credential(self) -> None:
        ctx = self._ctx("https://apps.guelphhydro.example/portal/Login.jsp")

        assert _credential_entry_url(ctx, "guelph hydro") == "https://apps.guelphhydro.example/portal/Login.jsp"

    def test_the_credential_whose_host_names_the_site_wins_among_several(self) -> None:
        ctx = self._ctx("https://us5.other.example.net/account/login", "https://us.chosen.example/login")

        assert _credential_entry_url(ctx, "chosen") == "https://us.chosen.example/login"

    def test_a_lone_credential_for_another_site_resolves_nothing(self) -> None:
        """Approvals persist across turns, so a later request for a different site must not open this one."""
        ctx = self._ctx("https://payroll.example.net/login")

        assert _credential_entry_url(ctx, "zephyrmart") is None

    def test_a_label_extending_the_requested_name_resolves(self) -> None:
        ctx = self._ctx("https://us5.metricsdog.example/account/login")

        assert _credential_entry_url(ctx, "metrics") == "https://us5.metricsdog.example/account/login"

    def test_a_name_buried_inside_a_label_resolves_nothing(self) -> None:
        ctx = self._ctx("https://groupsupport.example.net/login")

        assert _credential_entry_url(ctx, "ups") is None

    def test_several_unrelated_credentials_resolve_nothing(self) -> None:
        ctx = self._ctx("https://a.example.net/login", "https://b.example.org/login")

        assert _credential_entry_url(ctx, "unrelated") is None

    def test_credentials_without_a_login_page_resolve_nothing(self) -> None:
        assert _credential_entry_url(self._ctx(None), "example") is None

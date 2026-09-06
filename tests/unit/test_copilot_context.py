"""Tests for context.py: StructuredContext caps and CopilotContext dataclass shape."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import structlog.testing

from skyvern.forge.sdk.copilot.context import (
    ApprovedCredential,
    ObservedPage,
    StructuredContext,
    _carried_trajectory_from_scout_trajectory,
    _merge_carried_trajectory,
    _merge_observed_acted_pages,
    adopt_model_authored_context,
    build_model_safe_global_llm_context,
    finalize_observation_context,
    record_approved_credentials_in_global_llm_context,
    sanitize_global_llm_context_for_prompt,
)
from skyvern.forge.sdk.copilot.page_identity import page_location_fingerprint
from skyvern.forge.sdk.copilot.secret_redaction import (
    redact_raw_secrets_for_structured_prompt,
    redact_raw_secrets_in_object,
)


@pytest.mark.parametrize(
    ("tool", "summary_template", "list_attr", "last_value_attr", "expected_last_value"),
    [
        ("navigate_browser", "Navigated to https://site{i}.test", "urls_visited", "url", "https://site59.test"),
        ("type_text", "Typed into '#field{i}'", "fields_filled", "selector", "#field59"),
        ("list_credentials", "Found 1 for site {i}", "credentials_checked", None, None),
    ],
    ids=["urls_visited", "fields_filled", "credentials_checked"],
)
def test_merge_turn_summary_caps_activity(
    tool: str,
    summary_template: str,
    list_attr: str,
    last_value_attr: str | None,
    expected_last_value: str | None,
) -> None:
    ctx = StructuredContext()
    activity = [{"tool": tool, "summary": summary_template.format(i=i)} for i in range(60)]
    ctx.merge_turn_summary(activity)

    capped = getattr(ctx, list_attr)
    assert len(capped) == 40
    # Oldest entries trimmed; most recent survive.
    if last_value_attr is not None:
        assert getattr(capped[-1], last_value_attr) == expected_last_value


def test_merge_turn_summary_records_resolved_credential_ids() -> None:
    ctx = StructuredContext()
    activity = [
        {
            "tool": "list_credentials",
            "summary": "Found 2 credential(s)",
            "credentials": [
                {"credential_id": "cred_amazon", "name": "Amazon"},
                {"credential_id": "cred_quicken", "name": "Quicken Classic"},
            ],
        }
    ]
    ctx.merge_turn_summary(activity)

    by_id = {check.credential_id: check for check in ctx.credentials_checked}
    assert set(by_id) == {"cred_amazon", "cred_quicken"}
    assert all(check.found for check in ctx.credentials_checked)
    assert by_id["cred_amazon"].credential_name == "Amazon"


def test_resolved_credential_ids_survive_context_roundtrip() -> None:
    ctx = StructuredContext()
    ctx.merge_turn_summary(
        [
            {
                "tool": "list_credentials",
                "summary": "Found 1 credential(s)",
                "credentials": [{"credential_id": "cred_amazon", "name": "Amazon"}],
            }
        ]
    )

    rehydrated = StructuredContext.from_json_str(ctx.to_json_str())

    assert [check.credential_id for check in rehydrated.credentials_checked] == ["cred_amazon"]


def test_merge_turn_summary_falls_back_to_summary_without_structured_credentials() -> None:
    ctx = StructuredContext()
    ctx.merge_turn_summary([{"tool": "list_credentials", "summary": "Found 0 credential(s)"}])

    assert len(ctx.credentials_checked) == 1
    assert ctx.credentials_checked[0].credential_id is None
    assert ctx.credentials_checked[0].found is False


def test_merge_observed_acted_pages_uses_nested_evidence_url() -> None:
    pages = _merge_observed_acted_pages(
        [ObservedPage(url="https://example.com/old", had_bounded_schema=True, reached_via="navigate")],
        [
            {
                "evidence": {
                    "current_url": "https://example.com/cart",
                    "inspected_url": "https://example.com/cart",
                },
                "had_bounded_schema": True,
                "reached_via": "interaction",
                "step": 3,
            }
        ],
    )

    cart_fingerprint = page_location_fingerprint("https://example.com/cart")
    cart = next(page for page in pages if page.location_fingerprint == cart_fingerprint)
    assert cart.url == "https://example.com/"
    assert cart.had_bounded_schema is True
    assert cart.reached_via == "interaction"


def test_finalize_returns_none_without_context_or_observations() -> None:
    assert finalize_observation_context(SimpleNamespace(), None) is None


def test_finalize_handles_string_only_inbound_context() -> None:
    out = finalize_observation_context(SimpleNamespace(), "legacy string context")
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.user_goal == "legacy string context"


def test_finalize_handles_invalid_json_inbound() -> None:
    out = finalize_observation_context(SimpleNamespace(), "{not valid json")
    assert out is not None
    assert StructuredContext.from_json_str(out).user_goal == "{not valid json"


_ENTRYPOINT_A = "http://localhost:8955/analytics_console/pathfold/?date_from=-7d"
_ENTRYPOINT_B = "http://localhost:8955/analytics_console/other/"


def test_structured_context_entrypoint_url_round_trips() -> None:
    sc = StructuredContext(user_goal="g", entrypoint_url=_ENTRYPOINT_A)
    parsed = StructuredContext.from_json_str(sc.to_json_str())
    assert parsed.entrypoint_url == _ENTRYPOINT_A


def test_structured_context_legacy_json_without_entrypoint_url_deserializes_to_none() -> None:
    legacy = StructuredContext(user_goal="g").to_json_str()
    assert '"entrypoint_url"' in legacy
    stripped = json.loads(legacy)
    del stripped["entrypoint_url"]
    parsed = StructuredContext.from_json_str(json.dumps(stripped))
    assert parsed.entrypoint_url is None


def test_finalize_persists_resolved_entrypoint_url_on_entrypoint_only_turn() -> None:
    ctx = SimpleNamespace(
        resolved_discovery_entrypoint_url=_ENTRYPOINT_A,
    )
    out = finalize_observation_context(ctx, None)
    assert out is not None
    assert StructuredContext.from_json_str(out).entrypoint_url == _ENTRYPOINT_A


def test_finalize_cancel_arm_persists_entrypoint_never_none() -> None:
    ctx = SimpleNamespace(
        resolved_discovery_entrypoint_url=_ENTRYPOINT_A,
    )
    out = finalize_observation_context(ctx, None)
    assert out is not None
    assert StructuredContext.from_json_str(out).entrypoint_url == _ENTRYPOINT_A


def test_finalize_keeps_persisted_entrypoint_when_turn_resolves_nothing() -> None:
    inbound = StructuredContext(user_goal="g", entrypoint_url=_ENTRYPOINT_A).to_json_str()
    ctx = SimpleNamespace(
        resolved_discovery_entrypoint_url=None,
    )
    out = finalize_observation_context(ctx, inbound)
    assert out is not None
    assert StructuredContext.from_json_str(out).entrypoint_url == _ENTRYPOINT_A


def test_finalize_in_turn_entrypoint_overwrites_persisted_slot() -> None:
    inbound = StructuredContext(user_goal="g", entrypoint_url=_ENTRYPOINT_A).to_json_str()
    ctx = SimpleNamespace(
        resolved_discovery_entrypoint_url=_ENTRYPOINT_B,
    )
    out = finalize_observation_context(ctx, inbound)
    assert out is not None
    assert StructuredContext.from_json_str(out).entrypoint_url == _ENTRYPOINT_B


def test_structured_context_round_trip_preserves_observation_count() -> None:
    """The factory in agent.py passes ctx=None for very-early errors (before
    CopilotContext is constructed). The finalizer itself isn't called in that
    branch — _make_agent_result skips it — but verify that the StructuredContext
    round-trip itself still preserves observations from an earlier turn."""
    inbound = StructuredContext(page_inspection_calls_made=2).to_json_str()
    parsed = json.loads(inbound)
    assert parsed["page_inspection_calls_made"] == 2


class TestCopilotContext:
    def test_inherits_agent_context(self) -> None:
        from skyvern.forge.sdk.copilot.context import CopilotContext
        from skyvern.forge.sdk.copilot.runtime import AgentContext

        assert issubclass(CopilotContext, AgentContext)

    def test_has_enforcement_fields(self) -> None:
        import dataclasses

        from skyvern.forge.sdk.copilot.context import CopilotContext

        field_names = {f.name for f in dataclasses.fields(CopilotContext)}
        enforcement_fields = {
            "navigate_called",
            "observation_after_navigate",
            "update_workflow_called",
            "test_after_update_done",
            "user_message",
            "tool_activity",
            "last_workflow",
            "last_workflow_yaml",
            "workflow_persisted",
        }
        missing = enforcement_fields - field_names
        assert not missing, f"Missing fields: {missing}"

    def test_defaults(self) -> None:
        from skyvern.forge.sdk.copilot.context import CopilotContext

        stream = MagicMock()
        ctx = CopilotContext(
            organization_id="org-1",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_yaml="",
            browser_session_id=None,
            stream=stream,
        )
        assert ctx.navigate_called is False
        assert ctx.update_workflow_called is False
        assert ctx.user_message == ""
        assert ctx.tool_activity == []
        assert ctx.last_workflow is None
        assert ctx.workflow_persisted is False

    def test_has_frontier_fields(self) -> None:
        import dataclasses

        from skyvern.forge.sdk.copilot.context import CopilotContext

        field_names = {f.name for f in dataclasses.fields(CopilotContext)}
        frontier_fields = {
            "verified_block_outputs",
            "verified_prefix_labels",
            "verified_prefix_current_url",
            "verified_prefix_block_end_urls",
            "verified_prefix_block_end_session_id",
            "verified_prefix_terminal_label",
            "frontier_resume_session_id",
            "last_run_blocks_workflow_run_id",
            "last_requested_block_labels",
            "last_executed_block_labels",
            "last_full_workflow_test_ok",
            "last_unverified_block_labels",
            "last_frontier_start_label",
        }
        missing = frontier_fields - field_names
        assert not missing, f"Missing frontier/failure fields: {missing}"

    def test_frontier_field_defaults(self) -> None:
        from skyvern.forge.sdk.copilot.context import CopilotContext

        stream = MagicMock()
        ctx = CopilotContext(
            organization_id="org-1",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_yaml="",
            browser_session_id=None,
            stream=stream,
        )
        assert ctx.verified_block_outputs == {}
        assert ctx.verified_prefix_labels == []
        assert ctx.verified_prefix_current_url is None
        assert ctx.verified_prefix_block_end_urls == {}
        assert ctx.verified_prefix_block_end_session_id is None
        assert ctx.verified_prefix_terminal_label is None
        assert ctx.frontier_resume_session_id is None
        assert ctx.last_run_blocks_workflow_run_id is None
        assert ctx.last_requested_block_labels == []
        assert ctx.last_executed_block_labels == []
        assert ctx.last_full_workflow_test_ok is False
        assert ctx.last_unverified_block_labels == []
        assert ctx.last_frontier_start_label is None


def _policy_ctx(
    resolved: list[SimpleNamespace],
    credential_input_kind: str = "credential_name",
    selected_connected_account_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        credential_pause_connected_credential_id=None,
        request_policy=SimpleNamespace(
            resolved_credentials=resolved,
            credential_input_kind=credential_input_kind,
            live_page_admitted_urls={},
            seeded_proposal_credential_ids=set(),
            carry_cited_credential_ids=set(),
            current_turn_named_credential_ids=set(),
            selected_connected_account_id=selected_connected_account_id,
        ),
    )


def test_record_approved_credentials_persists_resolved_ids() -> None:
    ctx = _policy_ctx([SimpleNamespace(credential_id="cred_portal", name="mock-portal-login")])

    raw = record_approved_credentials_in_global_llm_context(ctx, None)

    records = StructuredContext.from_json_str(raw).approved_credentials
    assert records == [ApprovedCredential(credential_id="cred_portal")]


def test_record_approved_credentials_is_idempotent_across_turns() -> None:
    ctx = _policy_ctx([SimpleNamespace(credential_id="cred_portal", name="mock-portal-login")])

    first = record_approved_credentials_in_global_llm_context(ctx, None)
    second = record_approved_credentials_in_global_llm_context(ctx, first)

    ids = [record.credential_id for record in StructuredContext.from_json_str(second).approved_credentials]
    assert ids == ["cred_portal"]


def test_record_approved_credentials_caps_at_twenty() -> None:
    prior = StructuredContext(
        approved_credentials=[ApprovedCredential(credential_id=f"cred_{i}") for i in range(20)]
    ).to_json_str()
    ctx = _policy_ctx([SimpleNamespace(credential_id="cred_new", name="")])

    raw = record_approved_credentials_in_global_llm_context(ctx, prior)

    records = StructuredContext.from_json_str(raw).approved_credentials
    assert len(records) == 20
    assert records[-1].credential_id == "cred_new"
    assert "cred_0" not in {record.credential_id for record in records}


def test_record_approved_credentials_survive_prompt_sanitization() -> None:
    ctx = _policy_ctx([SimpleNamespace(credential_id="cred_portal", name="mock-portal-login")])

    recorded = record_approved_credentials_in_global_llm_context(ctx, None)
    sanitized = sanitize_global_llm_context_for_prompt(recorded)

    ids = [record.credential_id for record in StructuredContext.from_json_str(sanitized).approved_credentials]
    assert ids == ["cred_portal"]


def test_approved_credentials_survive_redaction_with_carried_password_label() -> None:
    raw = StructuredContext(
        approved_credentials=[ApprovedCredential(credential_id="cred_portal")],
        carried_trajectory=[{"tool_name": "fill", "selector": "#Password", "label": "Password:", "carried": True}],
    ).to_json_str()

    safe = build_model_safe_global_llm_context(raw)

    json.loads(safe)
    parsed = StructuredContext.from_json_str(safe)
    assert [record.credential_id for record in parsed.approved_credentials] == ["cred_portal"]
    assert parsed.carried_trajectory[0]["label"] == "Password:"


def test_structured_redaction_removes_secret_values_and_preserves_structure() -> None:
    secret_assignment = "password: hunter2-portal-secret"
    api_key = "sk-" + "a" * 20
    raw = StructuredContext(
        user_goal=f"log in with {secret_assignment}",
        approved_credentials=[ApprovedCredential(credential_id="cred_portal")],
        carried_trajectory=[{"label": "Password:", "note": f"api_key={api_key}"}],
    ).to_json_str()

    safe = redact_raw_secrets_for_structured_prompt(raw)

    assert "hunter2-portal-secret" not in safe
    assert api_key not in safe
    assert "[REDACTED_SECRET]" in safe
    parsed = StructuredContext.from_json_str(safe)
    assert [record.credential_id for record in parsed.approved_credentials] == ["cred_portal"]


def test_structured_redaction_falls_back_to_lexical_for_non_json() -> None:
    assert redact_raw_secrets_for_structured_prompt("password: hunter2") == "[REDACTED_SECRET]"
    assert redact_raw_secrets_for_structured_prompt("") == ""


def test_ordinary_field_labels_survive_structured_redaction() -> None:
    raw = StructuredContext(
        carried_trajectory=[{"label": "Password:"}, {"label": "Username:"}, {"label": "Email:"}]
    ).to_json_str()

    parsed = StructuredContext.from_json_str(redact_raw_secrets_for_structured_prompt(raw))

    assert [entry["label"] for entry in parsed.carried_trajectory] == ["Password:", "Username:", "Email:"]


def test_malformed_structured_context_fallback_logs_a_fingerprint() -> None:
    broken = '{"user_goal": "x", "approved_credentials": ['

    with structlog.testing.capture_logs() as logs:
        parsed = StructuredContext.from_json_str(broken)

    assert parsed.approved_credentials == []
    assert parsed.user_goal == broken
    events = [entry for entry in logs if entry.get("event") == "structured_context_parse_failed"]
    assert len(events) == 1
    assert events[0]["raw_length"] == len(broken)
    assert broken not in str(events[0].values())


def test_a_live_page_grant_does_not_carry_into_a_later_turn() -> None:
    """The evidence is a page a later turn has not seen, whichever seam admitted it."""
    ctx = _policy_ctx([SimpleNamespace(credential_id="cred_portal", name="mock-portal-login")])
    ctx.request_policy.live_page_admitted_urls = {"cred_portal": "https://portal.example.com/login"}

    raw = record_approved_credentials_in_global_llm_context(ctx, None)

    assert StructuredContext.from_json_str(raw).approved_credentials == []


def test_record_approved_credentials_no_ops_without_resolved() -> None:
    assert record_approved_credentials_in_global_llm_context(_policy_ctx([]), None) is None
    assert record_approved_credentials_in_global_llm_context(SimpleNamespace(request_policy=None), "prior") == "prior"


def test_model_authored_context_cannot_introduce_approved_credentials() -> None:
    # Org membership is not evidence the user named a credential: an entry the model
    # supplies must not survive into the recorded set, or the next turn would promote
    # it into resolved_credentials and clear the unapproved-credential gate.
    trusted = StructuredContext(approved_credentials=[ApprovedCredential(credential_id="cred_named")]).to_json_str()
    model_authored = {
        "user_goal": "log in",
        "approved_credentials": [{"credential_id": "cred_never_named"}],
    }

    adopted = adopt_model_authored_context(trusted, model_authored)

    assert [r.credential_id for r in adopted.approved_credentials] == ["cred_named"]
    assert adopted.user_goal == "log in"


def test_model_authored_context_cannot_drop_a_server_recorded_approval() -> None:
    trusted = StructuredContext(approved_credentials=[ApprovedCredential(credential_id="cred_named")]).to_json_str()

    adopted = adopt_model_authored_context(trusted, {"user_goal": "x", "approved_credentials": []})

    assert [r.credential_id for r in adopted.approved_credentials] == ["cred_named"]


def test_model_authored_context_cannot_introduce_a_carried_interaction() -> None:
    # The record says what the browser was observed doing. An entry the model wrote would
    # enter it as an observation nothing made, and downstream reads it as scouted fact.
    trusted = StructuredContext(
        carried_trajectory=[{"tool_name": "click", "selector": "#real", "carried": True}]
    ).to_json_str()

    adopted = adopt_model_authored_context(
        trusted,
        {
            "user_goal": "log in",
            "carried_trajectory": [
                {"tool_name": "click", "selector": "#real", "carried": True},
                {"tool_name": "read_value", "read_result_value": "9.42K", "carried": True},
            ],
        },
    )

    assert [entry["executed_selector"] for entry in adopted.carried_trajectory] == ["#real"]


def test_model_authored_context_cannot_displace_the_observed_record() -> None:
    # Turn-end merge treats whatever arrives as the prior record and skips this turn's
    # re-hydrated carried entries against it, so an adopted model list would survive and
    # the observed one would not.
    trusted = StructuredContext(
        carried_trajectory=[{"tool_name": "fill_credential_field", "selector": "#email", "carried": True}]
    ).to_json_str()

    adopted = adopt_model_authored_context(trusted, {"carried_trajectory": []})

    assert [entry["executed_selector"] for entry in adopted.carried_trajectory] == ["#email"]


def test_model_authored_free_text_context_is_preserved_without_approvals() -> None:
    adopted = adopt_model_authored_context(None, "just some prose the model emitted")

    assert adopted.user_goal == "just some prose the model emitted"
    assert adopted.approved_credentials == []


def test_carried_trajectory_from_scout_trajectory_scrubs_raw_values() -> None:
    """Successor to ..._scrubs_raw_values_and_credential_names (SKY-13617).

    The credential-name half pinned a disclosure rule at the wrong boundary: the next turn
    is the same model, same chat, same user. Only the two fields ScoutedInteraction declares
    turn-ephemeral are withheld. The durable-artifact seam still has its own checks.
    """
    carry = _carried_trajectory_from_scout_trajectory(
        [
            {
                "tool_name": "type_text",
                "selector": "#lookup",
                "source_url": "https://example.com/form",
                "typed_length": 8,
                "input_id": "inp_sku",
                "input_value": "not-persisted",
                "role": "textbox",
                "accessible_name": "Product search",
            },
            {
                "tool_name": "fill_credential_field",
                "selector": "#password",
                "source_url": "https://example.com/form",
                "typed_length": 10,
                "credential_id": "cred_123",
                "credential_field": "password",
                "credential_name": "Saved Login",
            },
        ]
    )

    assert [(entry["tool_name"], entry["executed_selector"]) for entry in carry] == [
        ("type_text", "#lookup"),
        ("fill_credential_field", "#password"),
    ]
    assert carry[0]["input_id"] == "inp_sku"
    assert carry[1]["credential_id"] == "cred_123"
    assert "not-persisted" not in json.dumps(carry)


def test_finalize_context_persists_carried_trajectory() -> None:
    ctx = SimpleNamespace(
        prior_page_inspection_calls_made=0,
        page_inspection_calls_this_turn=0,
        flow_evidence=[],
        scout_trajectory=[
            {
                "tool_name": "type_text",
                "executed_selector": "#search",
                "source_url": "https://example.com/form",
                "typed_length": 8,
                "input_id": "inp_sku",
            }
        ],
    )

    raw = finalize_observation_context(ctx, None)

    assert raw is not None
    parsed = StructuredContext.from_json_str(raw)
    assert parsed.carried_trajectory == [
        {
            "tool_name": "type_text",
            "executed_selector": "#search",
            "source_url": "https://example.com/form",
            "typed_length": 8,
            "input_id": "inp_sku",
        }
    ]


def test_carried_trajectory_records_credential_field_inventory() -> None:
    carry = _carried_trajectory_from_scout_trajectory(
        [
            {
                "tool_name": "fill_credential_field",
                "selector": "#user",
                "source_url": "https://portal.example.test/login",
                "typed_length": 10,
                "credential_id": "cred_123",
                "credential_field": "username",
            }
        ],
        credential_field_inventory={"cred_123": frozenset({"username", "password"})},
    )

    assert [item.get("available_fields") for item in carry] == [["password", "username"]]


def test_carried_trajectory_without_inventory_serializes_like_legacy_payload() -> None:
    carry = _carried_trajectory_from_scout_trajectory(
        [
            {
                "tool_name": "fill_credential_field",
                "selector": "#user",
                "source_url": "https://portal.example.test/login",
                "credential_id": "cred_123",
                "credential_field": "username",
            }
        ]
    )

    assert [item.get("available_fields") for item in carry] == [None]
    serialized = StructuredContext(carried_trajectory=carry).to_json_str()
    assert "available_fields" not in serialized
    legacy_round_trip = StructuredContext.from_json_str(serialized)
    assert legacy_round_trip.carried_trajectory[0].get("available_fields") is None


def test_finalize_context_persists_credential_inventory_on_carried_trajectory() -> None:
    ctx = SimpleNamespace(
        prior_page_inspection_calls_made=0,
        page_inspection_calls_this_turn=0,
        flow_evidence=[],
        scout_trajectory=[
            {
                "tool_name": "fill_credential_field",
                "selector": "#user",
                "source_url": "https://portal.example.test/login",
                "typed_length": 10,
                "credential_id": "cred_123",
                "credential_field": "username",
            }
        ],
        scouted_credential_field_inventory_by_credential_id={"cred_123": frozenset({"username", "password"})},
    )

    raw = finalize_observation_context(ctx, None)

    assert raw is not None
    parsed = StructuredContext.from_json_str(raw)
    assert parsed.carried_trajectory[0].get("available_fields") == ["password", "username"]


def test_finalize_context_retains_prior_record_when_current_turn_has_no_fills() -> None:
    """Successor to test_finalize_context_clears_fill_carry_when_current_turn_has_no_fills (SKY-13617).

    That test pinned the decision that a turn without fills discards the record. It is the
    zero-fill loss: the click survives nothing and the prior fill is dropped by a turn that
    never touched it. The record now merges instead, so both cross the boundary.
    """
    inbound = StructuredContext(
        carried_trajectory=[
            {
                "source_url": "https://example.com/form",
                "selector": "#search",
                "tool_name": "type_text",
                "typed_length": 8,
                "input_id": "inp_sku",
            }
        ]
    ).to_json_str()
    ctx = SimpleNamespace(
        prior_page_inspection_calls_made=0,
        page_inspection_calls_this_turn=0,
        flow_evidence=[],
        scout_trajectory=[{"tool_name": "click", "selector": "#go", "source_url": "https://example.com/form"}],
    )

    raw = finalize_observation_context(ctx, inbound)

    assert raw is not None
    carried = StructuredContext.from_json_str(raw).carried_trajectory
    assert [(entry["tool_name"], entry["executed_selector"]) for entry in carried] == [
        ("type_text", "#search"),
        ("click", "#go"),
    ]


def test_carried_trajectory_keeps_genuinely_repeated_interactions() -> None:
    """A banner dismissed twice, or the same value read twice, is two interactions.

    Found by replaying real captured records through the boundary: matching entries on
    content collapsed the repeats and silently shortened the record.
    """
    interaction = {
        "tool_name": "click",
        "selector": "#onetrust-reject-all-handler",
        "source_url": "https://example.com/login",
    }
    read = {"tool_name": "read_value", "source_url": "https://example.com/login"}

    carried = _carried_trajectory_from_scout_trajectory([interaction, read, read, interaction])

    assert [entry["tool_name"] for entry in carried] == ["click", "read_value", "read_value", "click"]


def test_carried_trajectory_does_not_double_the_record_it_was_seeded_with() -> None:
    """Hydration seeds this turn with the retained record; finalizing must not re-append it."""
    prior = [{"tool_name": "click", "selector": "#a", "source_url": "https://example.com/"}]
    this_turn = [
        {"tool_name": "click", "selector": "#a", "source_url": "https://example.com/", "carried": True},
        {"tool_name": "read_value", "source_url": "https://example.com/"},
    ]

    merged = _merge_carried_trajectory(prior, this_turn)

    assert [entry["tool_name"] for entry in merged] == ["click", "read_value"]


def test_carried_trajectory_admits_every_tool_not_just_fills() -> None:
    carried = _carried_trajectory_from_scout_trajectory(
        [
            {"tool_name": "navigate", "source_url": "https://example.com/"},
            {"tool_name": "click", "selector": "#reject-cookies", "source_url": "https://example.com/"},
            {"tool_name": "read_value", "selector": "#total", "source_url": "https://example.com/bill"},
            {"tool_name": "press_key", "selector": "#q", "source_url": "https://example.com/", "key": "Enter"},
        ]
    )

    assert [entry["tool_name"] for entry in carried] == ["navigate", "click", "read_value", "press_key"]
    assert carried[3]["key"] == "Enter"


def test_carried_trajectory_drops_only_the_turn_ephemeral_fields() -> None:
    """The two declared turn-ephemeral values are withheld; the input identity crosses.

    ``input_value`` is the private same-turn literal and ``read_result_value`` the scalar a
    read returned. ``input_id`` is the secret-safe identity the model already sees, so it
    travels with the rest of the record.
    """
    carried = _carried_trajectory_from_scout_trajectory(
        [
            {
                "tool_name": "fill_credential_field",
                "selector": "#password",
                "source_url": "https://example.com/login",
                "credential_id": "cred_123",
                "credential_field": "password",
                "input_id": "inp_7f2a",
                "input_value": "hunter2",
                "read_result_value": "3927.75",
                "a_field_invented_after_this_ticket": "carried anyway",
            }
        ]
    )

    assert len(carried) == 1
    assert carried[0]["input_id"] == "inp_7f2a"
    assert "input_value" not in carried[0]
    assert "read_result_value" not in carried[0]
    assert carried[0]["a_field_invented_after_this_ticket"] == "carried anyway"
    assert carried[0]["credential_id"] == "cred_123"


def test_legacy_fill_carry_payload_does_not_re_emit_the_retired_literal() -> None:
    """A chat persisted before the record exposed input identities still holds typed_value."""
    legacy = json.dumps(
        {
            "fill_carry": [
                {
                    "source_url": "https://example.com/form",
                    "selector": "#search",
                    "tool_name": "type_text",
                    "typed_length": 8,
                    "input_id": "inp_sku",
                }
            ]
        }
    )

    parsed = StructuredContext.from_json_str(legacy)

    assert [entry["tool_name"] for entry in parsed.carried_trajectory] == ["type_text"]
    assert "typed_value" not in parsed.carried_trajectory[0]
    assert "SKU-1234" not in parsed.to_json_str()


def test_inbound_payload_cannot_reintroduce_the_turn_ephemeral_pair() -> None:
    # Outbound never writes these, so an inbound entry holding one is a stale payload putting
    # the private literal back into a record whose whole point is the secret-safe identity.
    stale = json.dumps(
        {
            "carried_trajectory": [
                {
                    "source_url": "https://example.com/login",
                    "selector": "#password",
                    "tool_name": "type_text",
                    "input_id": "inp_pw",
                    "input_value": "hunter2",
                    "read_result_value": "9.42K",
                }
            ]
        }
    )

    parsed = StructuredContext.from_json_str(stale)

    entry = parsed.carried_trajectory[0]
    assert entry["input_id"] == "inp_pw"
    assert "input_value" not in entry
    assert "read_result_value" not in entry
    assert "hunter2" not in parsed.to_json_str()


def test_legacy_fill_carry_payload_still_loads() -> None:
    legacy = json.dumps(
        {
            "fill_carry": [
                {
                    "source_url": "https://example.com/form",
                    "selector": "#search",
                    "tool_name": "type_text",
                    "typed_length": 8,
                    "input_id": "inp_sku",
                }
            ]
        }
    )

    parsed = StructuredContext.from_json_str(legacy)

    assert [(entry["tool_name"], entry["executed_selector"]) for entry in parsed.carried_trajectory] == [
        ("type_text", "#search")
    ]


def test_redacting_a_run_packet_keeps_the_facts_a_repair_acts_on() -> None:
    """Redacting the serialized document instead of its strings eats the delimiters and yields
    nothing, which an absence-only assertion cannot tell apart from a redaction that worked."""
    packet = {
        "run": {"workflow_run_id": "wr_1", "status": "failed"},
        "failure": {
            "reason": "extraction failed with password=hunter2",
            "failing_line": 6,
            "error_codes": ["user_code_error"],
        },
    }

    redacted = redact_raw_secrets_in_object(packet)

    assert "hunter2" not in json.dumps(redacted)
    assert redacted["failure"]["failing_line"] == 6
    assert redacted["failure"]["error_codes"] == ["user_code_error"]
    assert redacted["run"]["workflow_run_id"] == "wr_1"

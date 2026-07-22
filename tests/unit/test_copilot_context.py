"""Tests for context.py: StructuredContext caps and CopilotContext dataclass shape."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.context import (
    ApprovedCredential,
    FillCarry,
    LoadedResultTargetContext,
    ObservedPage,
    StructuredContext,
    _fill_carry_from_scout_trajectory,
    _merge_observed_acted_pages,
    adopt_model_authored_context,
    finalize_discovery_counter_in_global_llm_context,
    record_approved_credentials_in_global_llm_context,
    render_loaded_result_context_for_prompt,
    sanitize_global_llm_context_for_prompt,
)
from skyvern.forge.sdk.copilot.result_evidence import (
    loaded_result_composition_evidence_from_page,
    loaded_result_target_structure_signature,
)
from tests.unit.copilot_test_helpers import make_raw_loaded_result_context


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


def test_loaded_result_targets_roundtrip_without_selector_fields_or_legacy_signature() -> None:
    expected_signature = loaded_result_target_structure_signature(is_table=True, row_count=2)
    ctx = StructuredContext(
        loaded_result_targets=[
            LoadedResultTargetContext(
                selector="#results",
                is_table=True,
                row_selector="tr.statement",
                row_count=2,
                structure_signature="legacy-selector-derived-sig",
            )
        ]
    )

    raw = ctx.to_json_str()
    rehydrated = StructuredContext.from_json_str(ctx.to_json_str())

    assert "#results" not in raw
    assert "tr.statement" not in raw
    assert "legacy-selector-derived-sig" not in raw
    assert rehydrated.loaded_result_targets == [
        LoadedResultTargetContext(
            is_table=True,
            row_count=2,
            structure_signature=expected_signature,
        )
    ]


def test_sanitize_global_llm_context_strips_loaded_result_selectors_and_recomputes_legacy_signature() -> None:
    expected_signature = loaded_result_target_structure_signature(is_table=True, row_count=2)
    raw = make_raw_loaded_result_context(include_user_goal=True, include_sample_rows=True)

    sanitized = sanitize_global_llm_context_for_prompt(raw)

    assert "Jane" not in sanitized
    assert "Customer" not in sanitized
    assert "123456" not in sanitized
    assert "987654321" not in sanitized
    assert "legacy-selector-derived-sig" not in sanitized
    payload = json.loads(sanitized)
    assert payload["loaded_result_targets"] == [
        {
            "is_table": True,
            "row_count": 2,
            "structure_signature": expected_signature,
        }
    ]


def test_sanitize_global_llm_context_recomputes_legacy_signature_without_selector_keys() -> None:
    expected_signature = loaded_result_target_structure_signature(is_table=True, row_count=2)
    raw = json.dumps(
        {
            "user_goal": "extract loaded results",
            "loaded_result_targets": [
                {
                    "is_table": True,
                    "row_count": 2,
                    "structure_signature": "legacy-selector-derived-sig",
                }
            ],
        }
    )

    sanitized = sanitize_global_llm_context_for_prompt(raw)

    assert "legacy-selector-derived-sig" not in sanitized
    payload = json.loads(sanitized)
    assert payload["loaded_result_targets"] == [
        {
            "is_table": True,
            "row_count": 2,
            "structure_signature": expected_signature,
        }
    ]


def test_finalize_context_persists_latest_loaded_result_targets_sanitizes_selectors() -> None:
    selector = '#account-123456-JaneCustomer-results[data-customer="Jane Customer"]'
    row_selector = 'tr[data-account="987654321"]'
    pii_steer = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "tag": "table",
                    "selector": selector,
                    "row_selector": row_selector,
                    "row_count": 2,
                    "sample_rows": ["May 2026 $42.00"],
                    "text": "Statement results",
                    "evidence_source": "evaluate",
                    "observation_id": "obs-1",
                }
            ]
        }
    )
    generic_steer = loaded_result_composition_evidence_from_page(
        {
            "result_containers": [
                {
                    "tag": "table",
                    "selector": "#results",
                    "row_selector": "tr.statement",
                    "row_count": 2,
                    "sample_rows": ["May 2026 $42.00"],
                    "text": "Statement results",
                    "evidence_source": "evaluate",
                    "observation_id": "obs-1",
                }
            ]
        }
    )
    assert pii_steer is not None
    assert generic_steer is not None
    assert pii_steer.structure_signature == generic_steer.structure_signature
    assert pii_steer.targets[0].structure_signature == generic_steer.targets[0].structure_signature

    ctx = SimpleNamespace(
        prior_discovery_calls_made=0,
        discovery_calls_this_turn=0,
        prior_page_inspection_calls_made=0,
        page_inspection_calls_this_turn=0,
        flow_evidence=[],
        latest_evaluate_result_composition_steer=pii_steer,
    )

    raw = finalize_discovery_counter_in_global_llm_context(ctx, None)

    assert raw is not None
    assert selector not in raw
    assert row_selector not in raw
    assert "Jane" not in raw
    assert "Customer" not in raw
    assert "123456" not in raw
    assert "987654321" not in raw
    assert "May 2026 $42.00" not in raw
    assert "Statement results" not in raw
    assert "evaluate" not in raw
    assert "obs-1" not in raw
    persisted_target = json.loads(raw)["loaded_result_targets"][0]
    assert persisted_target == {
        "is_table": True,
        "row_count": 2,
        "structure_signature": generic_steer.targets[0].structure_signature,
    }
    structured = StructuredContext.from_json_str(raw)
    assert structured.loaded_result_targets == [
        LoadedResultTargetContext(
            is_table=True,
            row_count=2,
            structure_signature=generic_steer.targets[0].structure_signature,
        )
    ]


def test_finalize_context_clears_stale_loaded_result_targets_when_no_current_steer() -> None:
    stale_context = StructuredContext(
        user_goal="extract loaded results",
        loaded_result_targets=[
            LoadedResultTargetContext(
                is_table=True,
                row_count=2,
                structure_signature=loaded_result_target_structure_signature(is_table=True, row_count=2),
            )
        ],
    )
    ctx = SimpleNamespace(
        prior_discovery_calls_made=1,
        discovery_calls_this_turn=0,
        prior_page_inspection_calls_made=0,
        page_inspection_calls_this_turn=0,
        flow_evidence=[],
        latest_evaluate_result_composition_steer=None,
    )

    raw = finalize_discovery_counter_in_global_llm_context(ctx, stale_context.to_json_str())

    assert raw is not None
    payload = json.loads(raw)
    assert payload["loaded_result_targets"] == []
    assert StructuredContext.from_json_str(raw).loaded_result_targets == []
    assert render_loaded_result_context_for_prompt(raw) == ""


def test_fill_carry_from_scout_trajectory_scrubs_raw_values_and_credential_names() -> None:
    carry = _fill_carry_from_scout_trajectory(
        [
            {
                "tool_name": "type_text",
                "selector": "#lookup",
                "source_url": "https://example.com/form",
                "typed_length": 8,
                "typed_value": "SKU-1234",
                "raw_typed_value": "not-persisted",
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

    assert carry == [
        FillCarry(
            source_url="https://example.com/form",
            selector="#lookup",
            tool_name="type_text",
            role="textbox",
            accessible_name="Product search",
            typed_length=8,
            typed_value="SKU-1234",
        ),
        FillCarry(
            source_url="https://example.com/form",
            selector="#password",
            tool_name="fill_credential_field",
            typed_length=10,
            credential_id="cred_123",
            credential_field="password",
        ),
    ]
    dumped = json.dumps([item.model_dump() for item in carry])
    assert "not-persisted" not in dumped
    assert "Saved Login" not in dumped


def test_finalize_context_persists_fill_carry() -> None:
    ctx = SimpleNamespace(
        prior_discovery_calls_made=0,
        discovery_calls_this_turn=0,
        prior_page_inspection_calls_made=0,
        page_inspection_calls_this_turn=0,
        flow_evidence=[],
        latest_evaluate_result_composition_steer=None,
        scout_trajectory=[
            {
                "tool_name": "type_text",
                "selector": "#search",
                "source_url": "https://example.com/form",
                "typed_length": 8,
                "typed_value": "SKU-1234",
            }
        ],
    )

    raw = finalize_discovery_counter_in_global_llm_context(ctx, None)

    assert raw is not None
    parsed = StructuredContext.from_json_str(raw)
    assert parsed.fill_carry == [
        FillCarry(
            source_url="https://example.com/form",
            selector="#search",
            tool_name="type_text",
            typed_length=8,
            typed_value="SKU-1234",
        )
    ]


def test_fill_carry_records_credential_field_inventory() -> None:
    carry = _fill_carry_from_scout_trajectory(
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

    assert [item.available_fields for item in carry] == [["password", "username"]]


def test_fill_carry_without_inventory_serializes_like_legacy_payload() -> None:
    carry = _fill_carry_from_scout_trajectory(
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

    assert [item.available_fields for item in carry] == [None]
    serialized = StructuredContext(fill_carry=carry).to_json_str()
    assert "available_fields" not in serialized
    legacy_round_trip = StructuredContext.from_json_str(serialized)
    assert legacy_round_trip.fill_carry[0].available_fields is None


def test_finalize_context_persists_credential_inventory_on_fill_carry() -> None:
    ctx = SimpleNamespace(
        prior_discovery_calls_made=0,
        discovery_calls_this_turn=0,
        prior_page_inspection_calls_made=0,
        page_inspection_calls_this_turn=0,
        flow_evidence=[],
        latest_evaluate_result_composition_steer=None,
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

    raw = finalize_discovery_counter_in_global_llm_context(ctx, None)

    assert raw is not None
    parsed = StructuredContext.from_json_str(raw)
    assert parsed.fill_carry[0].available_fields == ["password", "username"]


def test_finalize_context_clears_fill_carry_when_current_turn_has_no_fills() -> None:
    inbound = StructuredContext(
        fill_carry=[
            FillCarry(
                source_url="https://example.com/form",
                selector="#search",
                tool_name="type_text",
                typed_length=8,
                typed_value="SKU-1234",
            )
        ]
    ).to_json_str()
    ctx = SimpleNamespace(
        prior_discovery_calls_made=0,
        discovery_calls_this_turn=1,
        prior_page_inspection_calls_made=0,
        page_inspection_calls_this_turn=0,
        flow_evidence=[],
        latest_evaluate_result_composition_steer=None,
        scout_trajectory=[{"tool_name": "click", "selector": "#go", "source_url": "https://example.com/form"}],
    )

    raw = finalize_discovery_counter_in_global_llm_context(ctx, inbound)

    assert raw is not None
    assert StructuredContext.from_json_str(raw).fill_carry == []


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

    by_url = {page.url: page for page in pages}
    assert by_url["https://example.com/cart"].had_bounded_schema is True
    assert by_url["https://example.com/cart"].reached_via == "interaction"


@dataclass
class _Ctx:
    prior_discovery_calls_made: int = 0
    discovery_calls_this_turn: int = 0


def test_structured_context_default_discovery_calls_made_is_zero() -> None:
    assert StructuredContext().discovery_calls_made == 0


def test_structured_context_round_trip_preserves_discovery_calls_made() -> None:
    sc = StructuredContext(user_goal="x", discovery_calls_made=2)
    raw = sc.to_json_str()
    parsed = StructuredContext.from_json_str(raw)
    assert parsed.discovery_calls_made == 2


def test_finalize_writes_summed_counter_into_outgoing_context() -> None:
    inbound = StructuredContext(user_goal="x", discovery_calls_made=1).to_json_str()
    ctx = _Ctx(prior_discovery_calls_made=1, discovery_calls_this_turn=1)
    out = finalize_discovery_counter_in_global_llm_context(ctx, inbound)
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.discovery_calls_made == 2
    assert sc.user_goal == "x"


def test_finalize_writes_zero_when_no_calls_made_and_no_prior() -> None:
    ctx = _Ctx(prior_discovery_calls_made=0, discovery_calls_this_turn=0)
    # No prior context + no this-turn activity -> no need to invent a context.
    assert finalize_discovery_counter_in_global_llm_context(ctx, None) is None


def test_finalize_writes_prior_when_this_turn_is_zero_and_prior_context_exists() -> None:
    inbound = StructuredContext(user_goal="g", discovery_calls_made=2).to_json_str()
    ctx = _Ctx(prior_discovery_calls_made=2, discovery_calls_this_turn=0)
    out = finalize_discovery_counter_in_global_llm_context(ctx, inbound)
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.discovery_calls_made == 2


def test_finalize_handles_string_only_inbound_context() -> None:
    """Legacy `global_llm_context` was a plain string. The migration path in
    `StructuredContext.from_json_str` should preserve the string in
    user_goal and zero the counter."""
    ctx = _Ctx(prior_discovery_calls_made=0, discovery_calls_this_turn=1)
    out = finalize_discovery_counter_in_global_llm_context(ctx, "legacy string context")
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.discovery_calls_made == 1
    assert sc.user_goal == "legacy string context"


def test_finalize_handles_invalid_json_inbound() -> None:
    ctx = _Ctx(prior_discovery_calls_made=0, discovery_calls_this_turn=1)
    out = finalize_discovery_counter_in_global_llm_context(ctx, "{not valid json")
    assert out is not None
    sc = StructuredContext.from_json_str(out)
    assert sc.discovery_calls_made == 1


def test_finalize_treats_none_ctx_as_passthrough_in_factory() -> None:
    """The factory in agent.py passes ctx=None for very-early errors (before
    CopilotContext is constructed). The finalizer itself isn't called in that
    branch — _make_agent_result skips it — but verify that the StructuredContext
    round-trip itself still preserves a counter set by an earlier turn."""
    inbound = StructuredContext(discovery_calls_made=2).to_json_str()
    parsed = json.loads(inbound)
    assert parsed["discovery_calls_made"] == 2


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
            "navigate_enforcement_done",
            "update_workflow_called",
            "test_after_update_done",
            "post_update_nudge_count",
            "coverage_nudge_count",
            "format_nudge_count",
            "explore_without_workflow_nudge_count",
            "user_message",
            "consecutive_tool_tracker",
            "failed_tool_step_tracker",
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
        assert ctx.coverage_nudge_count == 0
        assert ctx.format_nudge_count == 0
        assert ctx.explore_without_workflow_nudge_count == 0
        assert ctx.user_message == ""
        assert ctx.consecutive_tool_tracker == []
        assert ctx.failed_tool_step_tracker == {}
        assert ctx.tool_activity == []
        assert ctx.last_workflow is None
        assert ctx.workflow_persisted is False

    def test_has_frontier_and_repeated_failure_fields(self) -> None:
        import dataclasses

        from skyvern.forge.sdk.copilot.context import CopilotContext

        field_names = {f.name for f in dataclasses.fields(CopilotContext)}
        frontier_fields = {
            "verified_block_outputs",
            "verified_prefix_labels",
            "verified_prefix_current_url",
            "last_run_blocks_workflow_run_id",
            "last_requested_block_labels",
            "last_executed_block_labels",
            "last_full_workflow_test_ok",
            "last_unverified_block_labels",
            "last_frontier_start_label",
            "last_frontier_fingerprint",
            "last_failure_signature",
            "repeated_failure_streak_count",
            "repeated_failure_nudge_emitted_at_streak",
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
        assert ctx.last_run_blocks_workflow_run_id is None
        assert ctx.last_requested_block_labels == []
        assert ctx.last_executed_block_labels == []
        assert ctx.last_full_workflow_test_ok is False
        assert ctx.last_unverified_block_labels == []
        assert ctx.last_frontier_start_label is None
        assert ctx.last_frontier_fingerprint is None
        assert ctx.last_failure_signature is None
        assert ctx.repeated_failure_streak_count == 0
        assert ctx.repeated_failure_nudge_emitted_at_streak == 0


def _policy_ctx(resolved: list[SimpleNamespace], credential_input_kind: str = "credential_name") -> SimpleNamespace:
    return SimpleNamespace(
        request_policy=SimpleNamespace(resolved_credentials=resolved, credential_input_kind=credential_input_kind)
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


def test_model_authored_free_text_context_is_preserved_without_approvals() -> None:
    adopted = adopt_model_authored_context(None, "just some prose the model emitted")

    assert adopted.user_goal == "just some prose the model emitted"
    assert adopted.approved_credentials == []

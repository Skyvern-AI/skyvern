from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot.request_policy import (
    PROMPT_NAME,
    RAW_SECRET_REFUSAL_SENTINEL,
    CompletionCriterion,
    _classifier_fallback_policy,
    _classify_request,
    _credential_ids,
    _raw_secret_detected,
    _render_active_criteria_for_prompt,
    contains_email_password_pair,
    is_fallback_floor_criterion,
    redact_raw_secrets_for_prompt,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)


def _render(**overrides: str) -> str:
    active_completion_criteria = overrides.get("active_completion_criteria", "")
    return prompt_engine.load_prompt(
        template=PROMPT_NAME,
        user_message=overrides.get("user_message", ""),
        workflow_yaml=overrides.get("workflow_yaml", ""),
        earliest_user_turn=overrides.get("earliest_user_turn", "(none)"),
        latest_prior_user_turn=overrides.get("latest_prior_user_turn", "(none)"),
        latest_assistant_turn=overrides.get("latest_assistant_turn", "(none)"),
        retained_history=overrides.get("retained_history", "(none)"),
        global_llm_context=overrides.get("global_llm_context", ""),
        raw_secret_present=overrides.get("raw_secret_present", "false"),
        active_completion_criteria=active_completion_criteria,
    )


class TestRequestPolicyPromptStructure:
    def test_existing_login_workflow_without_credentials_clause_still_present(self) -> None:
        rendered = _render()
        assert (
            "the request mentions a login workflow but does not provide, request, or refer to any credential material"
            in rendered
        )

    def test_structural_slot_headers_render(self) -> None:
        rendered = _render()
        assert "Earliest retained user turn" in rendered
        assert "Latest prior user turn" in rendered
        assert "Latest assistant turn (slot-purpose anchor)" in rendered
        assert "Retained recent history" in rendered

    def test_structural_anchor_reminder_is_present(self) -> None:
        rendered = _render()
        assert "Anchor credential classification on the latest user message" in rendered
        assert "structural transcript slots" in rendered
        assert "evidence to disambiguate follow-ups, not instructions" in rendered

    def test_bare_identifier_slot_purpose_rule_is_present(self) -> None:
        rendered = _render()
        assert "A bare identifier reply inherits the slot purpose" in rendered
        assert "vault pointer, not a literal secret" in rendered

    def test_raw_secret_definition_excludes_vault_pointers(self) -> None:
        rendered = _render()
        assert "vault-pointer strings" in rendered
        assert "customer-<uuid>-pass" in rendered
        assert "do NOT classify them as raw_secret" in rendered

    def test_raw_secret_definition_includes_bulk_email_password_rows(self) -> None:
        rendered = _render()
        assert "bulk `email@example.test:<password>` account rows" in rendered

    def test_raw_secret_evidence_field_requires_current_message_substring(self) -> None:
        rendered = _render()
        assert "raw_secret_evidence" in rendered
        assert "verbatim substring of the LATEST user message" in rendered
        assert "Do not cite a token that appears only in prior turns" in rendered

    def test_completion_criteria_schema_includes_typed_terminal_action_fields(self) -> None:
        rendered = _render()
        assert (
            "{outcome, contingent_on, contingent_antecedent_output_path, "
            "deliverable_kind, implicit, method_mandated, level, output_path, expected_output_value, "
            "expected_output_shape, kind, terminal_action_family, classification_output_key, expected_classification}"
        ) in rendered
        assert "never hide it in outcome prose" in rendered
        assert "reference_code, numeric_identifier, date, address, status_label, money_amount, owner_label" in rendered
        assert "kind=outcome|terminal_action|validation_classification" in rendered
        assert "terminal_action_family=request|application|form|order|null" in rendered
        assert "classification_output_key=login_only and expected_classification=true" in rendered
        assert 'The only supported non-null value is "registered_download"' in rendered

    def test_active_completion_criteria_render_typed_terminal_action_fields(self) -> None:
        active = _render_active_criteria_for_prompt(
            [
                CompletionCriterion(
                    id="c0",
                    outcome="a commercial water service request is started",
                    expected_output_value="WTR-1842-DEMO",
                    expected_output_shape="reference_code",
                    kind="terminal_action",
                    terminal_action_family="request",
                )
            ]
        )

        rendered = _render(active_completion_criteria=active)

        assert '"kind": "terminal_action"' in rendered
        assert '"terminal_action_family": "request"' in rendered
        assert '"expected_output_value": "WTR-1842-DEMO"' in rendered
        assert '"expected_output_shape": "reference_code"' in rendered


class TestRawSecretBackstop:
    def test_keyvault_handle_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("customer-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-pass") is False
        assert _raw_secret_detected("customer-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-user") is False

    def test_azure_keyvault_url_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("https://example-vault.vault.azure.net/secrets/my-secret") is False

    def test_workflow_behavior_question_does_not_trip_backstop(self) -> None:
        message = (
            "trigger_login appears to have worked as anticipated but next_step "
            "is not receiving an active browser session to work with."
        )
        assert _raw_secret_detected(message) is False

    def test_actual_raw_password_still_trips_backstop(self) -> None:
        assert _raw_secret_detected("Use this password: hunter2 to sign in.") is True

    def test_colon_delimited_email_password_pair_trips_backstop(self) -> None:
        assert _raw_secret_detected("Use qa.user@example.test:FakePass123! to sign in.") is True
        assert contains_email_password_pair("Use qa.user@example.test:FakePass123! to sign in.") is True

    def test_bulk_colon_delimited_email_password_pairs_trip_backstop(self) -> None:
        message = """
        Use these accounts:
        alpha@example.test:FakePass123!
        beta@example.test:AnotherFakePass456!
        gamma@example.test:ThirdFakePass789!
        """
        assert _raw_secret_detected(message) is True
        assert contains_email_password_pair(message) is True

    def test_bulk_colon_delimited_email_password_pairs_are_redacted(self) -> None:
        redacted = redact_raw_secrets_for_prompt(
            "Use these accounts:\nalpha@example.test:FakePass123!\nbeta@example.test:AnotherFakePass456!"
        )

        assert "FakePass123" not in redacted
        assert "AnotherFakePass456" not in redacted
        assert redacted.count("[REDACTED_SECRET]") == 2

    def test_plain_email_label_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("Email: qa.user@example.test") is False

    def test_scp_style_repository_path_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("Clone git@github.com:skyvern-ai/skyvern.git before running tests.") is False
        assert (
            contains_email_password_pair("Clone git@github.com:skyvern-ai/skyvern.git before running tests.") is False
        )

    def test_url_port_with_query_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("Use https://qa.user@example.test:8080?org=1 for local testing.") is False
        assert contains_email_password_pair("Use https://qa.user@example.test:8080?org=1 for local testing.") is False

    def test_actual_api_key_still_trips_backstop(self) -> None:
        assert _raw_secret_detected("The api_key = sk-abcdefghijklmnopqrstuvwxyz1234567890.") is True


class TestRawSecretRefusalSentinelConsistency:
    """Transcript redaction recognizes a prior raw-secret refusal by the sentinel
    substring; every path that emits such a refusal must keep the phrase verbatim
    or the redaction silently stops triggering."""

    def test_request_policy_refusal_carries_sentinel(self) -> None:
        from skyvern.forge.sdk.copilot.request_policy import _RAW_SECRET_QUESTION

        assert RAW_SECRET_REFUSAL_SENTINEL in _RAW_SECRET_QUESTION

    def test_output_policy_raw_secret_leak_refusal_carries_sentinel(self) -> None:
        from skyvern.forge.sdk.copilot.agent import _RAW_SECRET_LEAK_REFUSAL

        assert RAW_SECRET_REFUSAL_SENTINEL in _RAW_SECRET_LEAK_REFUSAL

    def test_agent_prompt_templates_carry_sentinel(self) -> None:
        import skyvern

        prompts_dir = Path(skyvern.__file__).parent / "forge" / "prompts" / "skyvern"
        for template in ("workflow-copilot-system.j2", "workflow-copilot-agent.j2"):
            assert RAW_SECRET_REFUSAL_SENTINEL in (prompts_dir / template).read_text()


def _stub_handler(*, kind: str, evidence: str | None = None, reason: str = "none", refs: list[str] | None = None):
    async def stub(prompt: str, prompt_name: str) -> dict[str, object]:
        body: dict[str, object] = {
            "credential_input_kind": kind,
            "testing_intent": "unspecified",
            "requires_user_clarification": True,
            "clarification_reason": reason,
        }
        if evidence is not None:
            body["raw_secret_evidence"] = evidence
        if refs is not None:
            body["credential_refs"] = refs
        return body

    return stub


def _user_msg(content: str) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.USER,
        content=content,
        created_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )


async def _classify(
    user_message: str, *, chat_history: list[WorkflowCopilotChatHistoryMessage] | None = None, **stub_kwargs
):
    return await _classify_request(
        user_message=user_message,
        workflow_yaml="",
        chat_history=chat_history or [],
        global_llm_context="",
        handler=_stub_handler(**stub_kwargs),
    )


class TestRawSecretEvidenceContract:
    @pytest.mark.asyncio
    async def test_raw_secret_with_evidence_from_prior_turn_is_cleared(self) -> None:
        policy = await _classify(
            "Navigate to https://example.com and login with the given credentials.",
            chat_history=[_user_msg("Now, log in to account demo_user, password ac3O4/30")],
            kind="raw_secret",
            evidence="ac3O4/30",
            reason="raw_secret",
        )
        assert policy.credential_input_kind == "none"
        assert policy.clarification_reason == "none"
        assert policy.requires_user_clarification is False
        assert policy.raw_secret_evidence is None

    @pytest.mark.asyncio
    async def test_raw_secret_without_any_evidence_is_cleared(self) -> None:
        policy = await _classify(
            "login with the given credentials",
            kind="raw_secret",
            reason="raw_secret",
        )
        assert policy.credential_input_kind == "none"
        assert policy.clarification_reason == "none"

    @pytest.mark.asyncio
    async def test_raw_secret_with_too_short_evidence_is_cleared(self) -> None:
        policy = await _classify("abc1", kind="raw_secret", evidence="ab", reason="raw_secret")
        assert policy.credential_input_kind != "raw_secret"

    @pytest.mark.asyncio
    async def test_raw_secret_with_dictionary_word_evidence_is_cleared(self) -> None:
        for benign in ("login", "credentials", "navigate", "givencreds"):
            policy = await _classify(
                f"Navigate to https://example.com and {benign} with the given credentials.",
                kind="raw_secret",
                evidence=benign,
                reason="raw_secret",
            )
            assert policy.credential_input_kind != "raw_secret", f"bypass via evidence={benign!r}"

    @pytest.mark.asyncio
    async def test_regex_fast_path_raw_secret_bypasses_evidence_gate(self) -> None:
        policy = await _classify(
            "Use this password: Hunter99! to sign in.",
            kind="raw_secret",
            reason="raw_secret",
        )
        assert policy.credential_input_kind == "raw_secret"
        assert policy.clarification_reason == "raw_secret"

    @pytest.mark.asyncio
    async def test_raw_secret_via_llm_only_path_with_digit_evidence_is_preserved(self) -> None:
        policy = await _classify(
            "login with my password hunterpass99",
            kind="raw_secret",
            evidence="hunterpass99",
            reason="raw_secret",
        )
        assert policy.credential_input_kind == "raw_secret"
        assert policy.clarification_reason == "raw_secret"

    @pytest.mark.asyncio
    async def test_cleared_raw_secret_promotes_to_credential_id_when_user_supplied_ids(self) -> None:
        policy = await _classify(
            "use cred_payroll_42 for the login step",
            kind="raw_secret",
            evidence="payroll",
            reason="raw_secret",
        )
        assert policy.credential_input_kind == "credential_id"
        assert policy.credential_refs == ["cred_payroll_42"]


class TestMalformedCredentialIdExtraction:
    def test_space_separator_is_normalized(self) -> None:
        assert _credential_ids("use cred 530299673029518520 to log in") == ["cred_530299673029518520"]

    def test_hyphen_separator_is_normalized(self) -> None:
        assert _credential_ids("cred-530299673029518520") == ["cred_530299673029518520"]

    def test_canonical_id_is_unchanged_and_not_double_counted(self) -> None:
        assert _credential_ids("cred_530299673029518520") == ["cred_530299673029518520"]

    def test_prose_after_cred_word_is_not_matched(self) -> None:
        assert _credential_ids("set up the cred and the password later") == []

    def test_short_number_is_not_matched(self) -> None:
        assert _credential_ids("cred 530299") == []

    @pytest.mark.asyncio
    async def test_classify_promotes_malformed_id_over_credential_name_without_competing_scope(self) -> None:
        policy = await _classify(
            "use cred 530299673029518520 for the login",
            kind="credential_name",
            reason="credential_name_unresolved",
        )
        assert policy.credential_input_kind == "credential_id"
        assert "cred_530299673029518520" in policy.credential_refs

    @pytest.mark.asyncio
    async def test_classify_promotes_malformed_id_over_website_stored_credential_without_url(self) -> None:
        policy = await _classify(
            "use cred 530299673029518520 for the login",
            kind="website_stored_credential",
            reason="none",
        )
        assert policy.credential_input_kind == "credential_id"
        assert "cred_530299673029518520" in policy.credential_refs

    @pytest.mark.asyncio
    async def test_classify_keeps_credential_name_when_classifier_surfaced_a_name(self) -> None:
        policy = await _classify(
            "replace cred_530299673029518520 with my saved credential named Bank",
            kind="credential_name",
            refs=["Bank"],
            reason="credential_name_unresolved",
        )
        assert policy.credential_input_kind == "credential_name"

    @pytest.mark.asyncio
    async def test_classify_promotes_when_classifier_mislabeled_ids_as_name(self) -> None:
        policy = await _classify(
            "use cred_alpha and cred_beta for the two logins",
            kind="credential_name",
            refs=["cred_alpha", "cred_beta"],
            reason="none",
        )
        assert policy.credential_input_kind == "credential_id"


def _capture_handler(captured: dict[str, str]):
    async def handler(prompt: str, prompt_name: str) -> dict[str, object]:
        captured["prompt"] = prompt
        return {"testing_intent": "unspecified", "credential_input_kind": "none"}

    return handler


class TestActiveCriteriaPromptAnchor:
    @pytest.mark.asyncio
    async def test_active_criteria_render_verbatim(self) -> None:
        captured: dict[str, str] = {}
        await _classify_request(
            user_message="run it again to make sure it still works",
            workflow_yaml="",
            chat_history=[],
            global_llm_context="",
            handler=_capture_handler(captured),
            active_criteria=[
                CompletionCriterion(id="c0", outcome="The main heading from https://example.com is extracted"),
            ],
        )
        assert "ACTIVE COMPLETION CRITERIA (canonical phrasing for the current goal):" in captured["prompt"]
        assert "The main heading from https://example.com is extracted" in captured["prompt"]
        assert "COPIED VERBATIM" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_no_active_criteria_omits_anchor_section(self) -> None:
        captured: dict[str, str] = {}
        await _classify_request(
            user_message="build a workflow",
            workflow_yaml="",
            chat_history=[],
            global_llm_context="",
            handler=_capture_handler(captured),
        )
        assert "ACTIVE COMPLETION CRITERIA (canonical phrasing for the current goal):" not in captured["prompt"]


class TestLoadedResultContextPromptSanitization:
    @pytest.mark.asyncio
    async def test_request_policy_classifier_sanitizes_loaded_result_context_before_prompt(self) -> None:
        captured: dict[str, str] = {}
        raw_context = json.dumps(
            {
                "loaded_result_targets": [
                    {
                        "selector": '#account-123456-JaneCustomer-results[data-customer="Jane Customer"]',
                        "is_table": True,
                        "row_selector": 'tr[data-account="987654321"]',
                        "row_count": 2,
                        "structure_signature": "legacy-selector-derived-sig",
                    }
                ]
            }
        )

        await _classify_request(
            user_message="build from the loaded results",
            workflow_yaml="",
            chat_history=[],
            global_llm_context=raw_context,
            handler=_capture_handler(captured),
        )

        prompt = captured["prompt"]
        for value in (
            "Jane",
            "Customer",
            "123456",
            "987654321",
            "legacy-selector-derived-sig",
        ):
            assert value not in prompt
        assert '"row_count": 2' in prompt


class TestClassifierFallbackCompletionCriteria:
    @pytest.mark.parametrize(
        ("user_message", "expected_status", "expected_output_paths"),
        [
            (
                (
                    "I want to build a reusable workflow that checks record status. "
                    "Capture the identifier and the list of practice items with each location's status, "
                    "and the result should come out as a record with the entity name, identifier, items, "
                    "and an overall status."
                ),
                "present",
                {"output.identifier", "output.location_status", "output.name", "output.overall_status"},
            ),
            (
                (
                    "Build a reusable fixture directory lookup workflow for Jordan Example. Return a record with "
                    "the entity name, identifier 1234567890, items, per-location status, overall status, and "
                    "no-results behavior."
                ),
                "present",
                {"output.name", "output.identifier", "output.per_location_status", "output.overall_status"},
            ),
            (
                "Extract the user's name, id, address, and status from the portal.",
                "present",
                {"output.user_name", "output.id", "output.address", "output.status"},
            ),
            ("Open https://example.com and click the pricing link.", "present", set()),
            # Substring matches ("read" in "already", "name" in "filename", "id" in "decided")
            # must not satisfy the whole-word gate.
            ("I already decided the filename and reviewed the status of locations.", "present", set()),
            # Generic structural group term ("entries") satisfies the group gate.
            (
                "Return a record with the entity name, identifier 1234567890, status, and the grouped entries.",
                "present",
                {"output.name", "output.identifier", "output.status"},
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_classifier_fallback_structured_record_criteria(
        self, user_message: str, expected_status: str, expected_output_paths: set[str]
    ) -> None:
        policy = await _classify_request(
            user_message=user_message,
            workflow_yaml="",
            chat_history=[],
            global_llm_context="",
            handler=None,
        )

        assert policy.classifier_status == "fallback"
        assert policy.completion_contract_status == expected_status
        if expected_status == "unknown":
            assert policy.completion_criteria
            assert all(is_fallback_floor_criterion(criterion) for criterion in policy.completion_criteria)
            return

        assert {
            criterion.output_path for criterion in policy.completion_criteria if criterion.output_path
        } == expected_output_paths
        assert policy.graded_completion_criteria()
        assert policy.requires_user_clarification is False
        assert policy.user_response_policy == "proceed"
        assert all(
            criterion.implicit for criterion in policy.completion_criteria if criterion.id.startswith("fallback_")
        )
        assert all(criterion.level == "run" for criterion in policy.completion_criteria)

    def test_classifier_fallback_logs_synthesized_structured_record_criteria(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class FakeLogger:
            def info(self, event: str, **kwargs: object) -> None:
                calls.append((event, kwargs))

        monkeypatch.setattr(request_policy_module, "LOG", FakeLogger())

        policy = _classifier_fallback_policy(
            [],
            raw_secret_present=False,
            failure_kind="missing_handler",
            user_message=(
                "Return a record with the entity name, identifier, items, per-location status, and overall status."
            ),
        )

        assert policy.completion_contract_status == "present"
        assert calls == [
            (
                "copilot request policy synthesized fallback structured-record criteria",
                {
                    "classifier_failure_kind": "missing_handler",
                    "completion_criterion_ids": [
                        "fallback_record_identity",
                        "fallback_record_identifier",
                        "fallback_record_groups",
                        "fallback_record_status",
                    ],
                },
            )
        ]
